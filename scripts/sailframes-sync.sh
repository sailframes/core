#!/bin/bash
#
# SailFrames Data Sync to AWS S3 via API Gateway
# Uploads race data from Pi device to S3 using HTTP (no AWS credentials needed)
#
# Usage: sailframes-sync.sh [--force]
#
# Options:
#   --force    Sync all files, ignoring last sync state
#

set -e

# Configuration
CONFIG_FILE="${SAILFRAMES_CONFIG:-/etc/sailframes/sailframes.yaml}"
DATA_DIR="${SAILFRAMES_DATA_DIR:-/mnt/sailframes-data}"
UPLOAD_URL="${SAILFRAMES_UPLOAD_URL:-https://p9s9eia0t6.execute-api.us-east-1.amazonaws.com/prod/upload}"
SYNC_LOG="/var/log/sailframes/sync.log"
SYNC_STATE="/var/lib/sailframes/last-sync"
UPLOADED_MARKER=".uploaded"
LOCK_FILE="/var/run/sailframes-sync.lock"

# Parse device ID from config
get_device_id() {
    if [ -f "$CONFIG_FILE" ]; then
        grep -E "^\s*id:" "$CONFIG_FILE" 2>/dev/null | head -1 | awk '{print $2}' | tr -d '"' || echo "S1"
    else
        hostname
    fi
}

DEVICE_ID=$(get_device_id)

# Ensure directories exist
mkdir -p /var/log/sailframes
mkdir -p /var/lib/sailframes
mkdir -p "$(dirname "$LOCK_FILE")"

# Logging function
log() {
    local level="$1"
    shift
    local message="$*"
    local timestamp
    timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    echo "${timestamp} [${level}] ${message}" | tee -a "$SYNC_LOG"
}

# Cleanup on exit
cleanup() {
    rm -f "$LOCK_FILE"
}
trap cleanup EXIT

# Check for existing lock
check_lock() {
    if [ -f "$LOCK_FILE" ]; then
        local pid
        pid=$(cat "$LOCK_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            log "WARN" "Another sync is running (PID $pid)"
            exit 0
        else
            log "WARN" "Removing stale lock file"
            rm -f "$LOCK_FILE"
        fi
    fi
    echo $$ > "$LOCK_FILE"
}

# Check network connectivity
check_network() {
    local retries=3
    local delay=5

    for i in $(seq 1 $retries); do
        if ping -c 1 -W 5 8.8.8.8 >/dev/null 2>&1; then
            return 0
        fi
        log "WARN" "Network check failed (attempt $i/$retries)"
        sleep $delay
    done

    log "ERROR" "No network connectivity after $retries attempts"
    return 1
}

# Format bytes to human readable
format_size() {
    local bytes="$1"
    if [ "$bytes" -gt 1073741824 ]; then
        echo "$(echo "scale=2; $bytes / 1073741824" | bc) GB"
    elif [ "$bytes" -gt 1048576 ]; then
        echo "$(echo "scale=2; $bytes / 1048576" | bc) MB"
    elif [ "$bytes" -gt 1024 ]; then
        echo "$(echo "scale=2; $bytes / 1024" | bc) KB"
    else
        echo "${bytes} B"
    fi
}

# Size threshold for presigned URLs (5 MB)
PRESIGN_THRESHOLD=5242880

# Upload a single file via HTTP
upload_file() {
    local filepath="$1"
    local filename
    filename=$(basename "$filepath")

    # Skip if already uploaded
    if [ -f "${filepath}${UPLOADED_MARKER}" ]; then
        return 0
    fi

    # Skip temp files
    case "$filename" in
        *.tmp|*.swp|.DS_Store|*.pyc)
            return 0
            ;;
    esac

    local filesize
    filesize=$(stat -c %s "$filepath" 2>/dev/null || stat -f %z "$filepath")

    log "INFO" "  Uploading: $filename ($(format_size "$filesize"))"

    local response
    local http_code

    # Use presigned URL for large files (>5MB)
    if [ "$filesize" -gt "$PRESIGN_THRESHOLD" ]; then
        # Request presigned URL from API
        http_code=$(curl -s -w "%{http_code}" -o /tmp/presign_response.txt \
            -X POST \
            "${UPLOAD_URL}?boat=${DEVICE_ID}&file=${filepath}&presign=1&size=${filesize}" \
            -H "Content-Type: application/json" \
            --max-time 30 \
            2>/dev/null)

        if [ "$http_code" != "200" ]; then
            response=$(cat /tmp/presign_response.txt 2>/dev/null || echo "No response")
            log "ERROR" "    FAILED to get presigned URL (HTTP $http_code): $response"
            return 1
        fi

        # Extract presigned URL from response using jq
        local presigned_url
        local content_type
        presigned_url=$(jq -r '.url // empty' /tmp/presign_response.txt 2>/dev/null)
        content_type=$(jq -r '.content_type // "application/octet-stream"' /tmp/presign_response.txt 2>/dev/null)

        if [ -z "$presigned_url" ]; then
            log "ERROR" "    FAILED to parse presigned URL"
            cat /tmp/presign_response.txt | head -c 200
            return 1
        fi

        # Upload directly to S3 using presigned URL
        http_code=$(curl -s -w "%{http_code}" -o /tmp/upload_response.txt \
            -X PUT \
            "$presigned_url" \
            --data-binary "@${filepath}" \
            -H "Content-Type: ${content_type:-application/octet-stream}" \
            --max-time 600 \
            2>/dev/null)

        if [ "$http_code" = "200" ]; then
            touch "${filepath}${UPLOADED_MARKER}"
            log "INFO" "    OK (presigned)"
            return 0
        else
            response=$(cat /tmp/upload_response.txt 2>/dev/null || echo "No response")
            log "ERROR" "    FAILED S3 upload (HTTP $http_code): $response"
            return 1
        fi
    else
        # Direct upload via API Gateway for small files
        http_code=$(curl -s -w "%{http_code}" -o /tmp/upload_response.txt \
            -X POST \
            "${UPLOAD_URL}?boat=${DEVICE_ID}&file=${filepath}" \
            --data-binary "@${filepath}" \
            -H "Content-Type: application/octet-stream" \
            --max-time 300 \
            2>/dev/null)

        if [ "$http_code" = "200" ]; then
            touch "${filepath}${UPLOADED_MARKER}"
            log "INFO" "    OK"
            return 0
        else
            response=$(cat /tmp/upload_response.txt 2>/dev/null || echo "No response")
            log "ERROR" "    FAILED (HTTP $http_code): $response"
            return 1
        fi
    fi
}

# Sync a single date folder
sync_date() {
    local date_folder="$1"
    local date_name
    date_name=$(basename "$date_folder")

    log "INFO" "Syncing $date_name..."

    local total_files=0
    local uploaded_files=0
    local failed_files=0
    local skipped_files=0

    # Find all data files (csv, json, rtcm3, ubx, mp4)
    while IFS= read -r -d '' file; do
        ((total_files++))

        # Skip marker files
        if [[ "$file" == *"$UPLOADED_MARKER" ]]; then
            continue
        fi

        # Skip if already uploaded
        if [ -f "${file}${UPLOADED_MARKER}" ]; then
            ((skipped_files++))
            continue
        fi

        if upload_file "$file"; then
            ((uploaded_files++))
        else
            ((failed_files++))
        fi

    done < <(find "$date_folder" -type f \( -name "*.csv" -o -name "*.json" -o -name "*.rtcm3" -o -name "*.ubx" -o -name "*.mp4" -o -name "*.h264" \) -print0)

    log "INFO" "  $date_name: $uploaded_files uploaded, $skipped_files skipped, $failed_files failed"

    if [ "$failed_files" -eq 0 ]; then
        echo "$date_name" >> "$SYNC_STATE"
        return 0
    else
        return 1
    fi
}

# Main sync logic
main() {
    local force=false

    # Parse arguments
    while [ $# -gt 0 ]; do
        case "$1" in
            --force)
                force=true
                # Remove uploaded markers if forcing
                find "$DATA_DIR" -name "*${UPLOADED_MARKER}" -delete 2>/dev/null || true
                shift
                ;;
            *)
                shift
                ;;
        esac
    done

    log "INFO" "========================================="
    log "INFO" "Starting SailFrames sync (HTTP mode)"
    log "INFO" "Device: ${DEVICE_ID}"
    log "INFO" "Data dir: ${DATA_DIR}"
    log "INFO" "Upload URL: ${UPLOAD_URL}"
    log "INFO" "========================================="

    # Acquire lock
    check_lock

    # Pre-flight checks
    if ! check_network; then
        exit 1
    fi

    # Check data directory exists
    if [ ! -d "$DATA_DIR" ]; then
        log "ERROR" "Data directory not found: $DATA_DIR"
        exit 1
    fi

    # Find date folders (YYYY-MM-DD format)
    local total_synced=0
    local total_failed=0
    local total_skipped=0

    for date_folder in "$DATA_DIR"/????-??-??/; do
        if [ ! -d "$date_folder" ]; then
            continue
        fi

        local date_name
        date_name=$(basename "$date_folder")

        if sync_date "$date_folder"; then
            ((total_synced++))
        else
            ((total_failed++))
        fi
    done

    log "INFO" "========================================="
    log "INFO" "Sync complete"
    log "INFO" "  Folders synced: $total_synced"
    log "INFO" "  Folders with errors: $total_failed"
    log "INFO" "========================================="

    # Return non-zero if any failed
    [ "$total_failed" -eq 0 ]
}

# Run main
main "$@"
