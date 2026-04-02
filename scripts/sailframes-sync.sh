#!/bin/bash
#
# SailFrames Data Sync to AWS S3
# Syncs race data from Pi device to AWS S3 bucket
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
S3_BUCKET="${SAILFRAMES_S3_BUCKET:-sailframes-fleet-data-prod}"
SYNC_LOG="/var/log/sailframes/sync.log"
SYNC_STATE="/var/lib/sailframes/last-sync"
LOCK_FILE="/var/run/sailframes-sync.lock"

# Parse device ID from config
get_device_id() {
    if [ -f "$CONFIG_FILE" ]; then
        grep -E "^device_id:" "$CONFIG_FILE" 2>/dev/null | awk '{print $2}' | tr -d '"' || echo "unknown"
    else
        hostname
    fi
}

DEVICE_ID=$(get_device_id)
S3_PREFIX="raw/${DEVICE_ID}"

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

# Check AWS credentials
check_credentials() {
    if ! aws sts get-caller-identity >/dev/null 2>&1; then
        log "ERROR" "AWS credentials not configured or invalid"
        return 1
    fi
    return 0
}

# Get total size of files to sync
get_sync_size() {
    local folder="$1"
    du -sb "$folder" 2>/dev/null | awk '{print $1}' || echo "0"
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

# Sync a single date folder
sync_date() {
    local date_folder="$1"
    local date_name
    date_name=$(basename "$date_folder")

    log "INFO" "Syncing $date_name..."

    local size
    size=$(get_sync_size "$date_folder")
    log "INFO" "  Size: $(format_size "$size")"

    # Sync with aws cli
    # --size-only: skip files that match in size (faster for unchanged files)
    # --exclude: skip temporary and system files
    if aws s3 sync \
        "$date_folder" \
        "s3://${S3_BUCKET}/${S3_PREFIX}/${date_name}/" \
        --exclude "*.tmp" \
        --exclude "*.swp" \
        --exclude ".DS_Store" \
        --exclude "*.pyc" \
        --exclude "__pycache__/*" \
        --size-only \
        --no-progress \
        2>&1 | tee -a "$SYNC_LOG"; then

        log "INFO" "SUCCESS: Synced $date_name"
        echo "$date_name" >> "$SYNC_STATE"
        return 0
    else
        log "ERROR" "Failed to sync $date_name"
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
                shift
                ;;
            *)
                shift
                ;;
        esac
    done

    log "INFO" "========================================="
    log "INFO" "Starting SailFrames sync"
    log "INFO" "Device: ${DEVICE_ID}"
    log "INFO" "Data dir: ${DATA_DIR}"
    log "INFO" "S3 bucket: ${S3_BUCKET}"
    log "INFO" "========================================="

    # Acquire lock
    check_lock

    # Pre-flight checks
    if ! check_network; then
        exit 1
    fi

    if ! check_credentials; then
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

        # Skip if already synced (unless --force)
        if [ "$force" = false ] && grep -q "^${date_name}$" "$SYNC_STATE" 2>/dev/null; then
            # Check if folder was modified since last sync
            local folder_mtime
            local state_mtime

            folder_mtime=$(stat -c %Y "$date_folder" 2>/dev/null || stat -f %m "$date_folder")
            state_mtime=$(stat -c %Y "$SYNC_STATE" 2>/dev/null || stat -f %m "$SYNC_STATE")

            if [ "$folder_mtime" -lt "$state_mtime" ]; then
                log "INFO" "Skipping $date_name (unchanged since last sync)"
                ((total_skipped++))
                continue
            fi
        fi

        if sync_date "$date_folder"; then
            ((total_synced++))
        else
            ((total_failed++))
        fi
    done

    log "INFO" "========================================="
    log "INFO" "Sync complete"
    log "INFO" "  Synced: $total_synced"
    log "INFO" "  Failed: $total_failed"
    log "INFO" "  Skipped: $total_skipped"
    log "INFO" "========================================="

    # Return non-zero if any failed
    [ "$total_failed" -eq 0 ]
}

# Run main
main "$@"
