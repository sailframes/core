#!/bin/bash
# upload-gopro-s3.sh - Upload GoPro videos to AWS S3
# Usage: ./upload-gopro-s3.sh <date> [input_dir]
# Example: ./upload-gopro-s3.sh 2026-04-03
#
# By default reads from GoPro SD card at /Volumes/Untitled/DCIM/100GOPRO
# and uploads only .LRV proxy videos. Prompts to also upload 1080p MP4s.
#
# Extracts GPS time from embedded GoPro telemetry for accurate video sync.

set -e

AWS_PROFILE="${AWS_PROFILE:-sailframes}"
S3_BUCKET="sailframes-fleet-data-prod"
GOPRO_SD_PATH="/Volumes/Untitled/DCIM/100GOPRO"

# Check for exiftool (required for GPS extraction)
if ! command -v exiftool &> /dev/null; then
    echo "Error: exiftool is required but not installed."
    echo "Install with: brew install exiftool"
    exit 1
fi

# Default to today's date if not specified
DATE="${1:-$(date +%Y-%m-%d)}"
INPUT_DIR="${2:-}"

if [[ "$1" == "-h" || "$1" == "--help" ]]; then
    echo "Usage: $0 [date] [input_dir]"
    echo "  date: Sail date in YYYY-MM-DD format (default: today)"
    echo "  input_dir: Directory containing video files (optional)"
    echo ""
    echo "If input_dir not specified, reads from GoPro SD card:"
    echo "  $GOPRO_SD_PATH"
    echo ""
    echo "By default uploads only .LRV proxy videos (~5MB each)."
    echo "You will be asked if you also want to upload 1080p MP4 files."
    echo ""
    echo "Environment variables:"
    echo "  AWS_PROFILE - AWS profile to use (default: sailframes)"
    exit 0
fi

# Default input directory: GoPro SD card
if [[ -z "$INPUT_DIR" ]]; then
    INPUT_DIR="$GOPRO_SD_PATH"
fi

if [[ ! -d "$INPUT_DIR" ]]; then
    echo "Error: Directory does not exist: $INPUT_DIR"
    if [[ "$INPUT_DIR" == "$GOPRO_SD_PATH" ]]; then
        echo ""
        echo "Is the GoPro SD card mounted? Check:"
        echo "  ls /Volumes/"
    fi
    exit 1
fi

S3_PREFIX="raw/gopro/${DATE}/video"

echo "Upload Configuration:"
echo "  Source: $INPUT_DIR"
echo "  Destination: s3://${S3_BUCKET}/${S3_PREFIX}/"
echo "  AWS Profile: $AWS_PROFILE"
echo ""

# Find video and thumbnail files
shopt -s nullglob nocaseglob
LRV_FILES=("$INPUT_DIR"/*.LRV)
MP4_FILES=("$INPUT_DIR"/*.MP4)
THM_FILES=("$INPUT_DIR"/*.THM)

if [[ ${#LRV_FILES[@]} -eq 0 && ${#MP4_FILES[@]} -eq 0 ]]; then
    echo "No video files found in $INPUT_DIR"
    exit 1
fi

# Calculate sizes
LRV_TOTAL=0
MP4_TOTAL=0
THM_TOTAL=0
for f in "${LRV_FILES[@]}"; do
    LRV_TOTAL=$((LRV_TOTAL + $(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null)))
done
for f in "${MP4_FILES[@]}"; do
    MP4_TOTAL=$((MP4_TOTAL + $(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null)))
done
for f in "${THM_FILES[@]}"; do
    THM_TOTAL=$((THM_TOTAL + $(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null)))
done

echo "Found ${#LRV_FILES[@]} LRV proxy video(s) ($(numfmt --to=iec $LRV_TOTAL 2>/dev/null || echo "$((LRV_TOTAL/1024/1024))MB"))"
echo "Found ${#THM_FILES[@]} THM thumbnail(s) ($(numfmt --to=iec $THM_TOTAL 2>/dev/null || echo "$((THM_TOTAL/1024))KB"))"
echo "Found ${#MP4_FILES[@]} MP4 1080p video(s) ($(numfmt --to=iec $MP4_TOTAL 2>/dev/null || echo "$((MP4_TOTAL/1024/1024/1024))GB"))"
echo ""

# Build list of files to upload
FILES_TO_UPLOAD=()
INCLUDE_MP4=false

if [[ ${#LRV_FILES[@]} -gt 0 ]]; then
    echo "LRV proxy videos:"
    for f in "${LRV_FILES[@]}"; do
        SIZE=$(du -h "$f" | cut -f1)
        echo "  $(basename "$f") ($SIZE)"
    done
    FILES_TO_UPLOAD+=("${LRV_FILES[@]}")
    echo ""
fi

# THM thumbnails are tiny - always include them
if [[ ${#THM_FILES[@]} -gt 0 ]]; then
    echo "THM thumbnails:"
    for f in "${THM_FILES[@]}"; do
        SIZE=$(du -h "$f" | cut -f1)
        echo "  $(basename "$f") ($SIZE)"
    done
    FILES_TO_UPLOAD+=("${THM_FILES[@]}")
    echo ""
fi

if [[ ${#MP4_FILES[@]} -gt 0 ]]; then
    read -p "Also upload ${#MP4_FILES[@]} 1080p MP4 file(s)? [y/N] " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        INCLUDE_MP4=true
        echo ""
        echo "MP4 1080p videos:"
        for f in "${MP4_FILES[@]}"; do
            SIZE=$(du -h "$f" | cut -f1)
            echo "  $(basename "$f") ($SIZE)"
        done
        FILES_TO_UPLOAD+=("${MP4_FILES[@]}")
    fi
    echo ""
fi

if [[ ${#FILES_TO_UPLOAD[@]} -eq 0 ]]; then
    echo "No files selected for upload."
    exit 0
fi

echo "Will upload ${#FILES_TO_UPLOAD[@]} file(s):"
for f in "${FILES_TO_UPLOAD[@]}"; do
    echo "  $(basename "$f")"
done
echo ""
read -p "Proceed with upload? [Y/n] " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Nn]$ ]]; then
    echo "Upload cancelled."
    exit 0
fi

echo ""
echo "Extracting GPS metadata and uploading..."

UPLOADED=0
FAILED=0

# Function to extract GPS start time from video (first GPS DateTime entry)
# Both LRV and MP4 files have embedded GPS telemetry
extract_gps_time() {
    local file="$1"

    # Extract first GPS DateTime (UTC) - returns format like "2026:04:07 17:44:29.180"
    local gps_time
    gps_time=$(exiftool -api largefilesupport=1 -ee -GPSDateTime -s3 "$file" 2>/dev/null | head -1)

    if [[ -n "$gps_time" ]]; then
        # Convert "2026:04:07 17:44:29.180" to ISO format "2026-04-07T17:44:29.180Z"
        echo "$gps_time" | sed 's/\([0-9]\{4\}\):\([0-9]\{2\}\):\([0-9]\{2\}\) /\1-\2-\3T/' | sed 's/$/.000Z/' | sed 's/\.\([0-9]\{3\}\)\.000Z/.\1Z/'
    fi
}

# Function to extract video duration in seconds
extract_duration() {
    local file="$1"
    local duration
    duration=$(exiftool -api largefilesupport=1 -Duration -s3 "$file" 2>/dev/null | head -1)

    # Handle different formats: "1062.12 s", "0:17:42", "17:42"
    if [[ "$duration" =~ ^[0-9]+(\.[0-9]+)?\ s$ ]]; then
        # Already in seconds format (e.g., "1062.12 s")
        echo "$duration" | sed 's/ s$//'
    elif [[ "$duration" =~ ^([0-9]+):([0-9]+):([0-9]+)$ ]]; then
        # H:MM:SS format
        local h="${BASH_REMATCH[1]}"
        local m="${BASH_REMATCH[2]}"
        local s="${BASH_REMATCH[3]}"
        echo $((h * 3600 + m * 60 + s))
    elif [[ "$duration" =~ ^([0-9]+):([0-9]+)$ ]]; then
        # MM:SS format
        local m="${BASH_REMATCH[1]}"
        local s="${BASH_REMATCH[2]}"
        echo $((m * 60 + s))
    else
        echo ""
    fi
}

for INPUT_FILE in "${FILES_TO_UPLOAD[@]}"; do
    BASENAME=$(basename "$INPUT_FILE")
    S3_PATH="s3://${S3_BUCKET}/${S3_PREFIX}/${BASENAME}"
    EXT="${BASENAME##*.}"
    EXT_LOWER=$(echo "$EXT" | tr '[:upper:]' '[:lower:]')

    echo "Uploading: $BASENAME"

    # Extract metadata for video files (LRV and MP4)
    METADATA_ARGS=""
    if [[ "$EXT_LOWER" == "lrv" || "$EXT_LOWER" == "mp4" ]]; then
        GPS_TIME=$(extract_gps_time "$INPUT_FILE")
        DURATION=$(extract_duration "$INPUT_FILE")

        if [[ -n "$GPS_TIME" ]]; then
            echo "  GPS time: $GPS_TIME"
            METADATA_ARGS="--metadata gps-start-time=$GPS_TIME"
        fi

        if [[ -n "$DURATION" ]]; then
            echo "  Duration: ${DURATION}s"
            METADATA_ARGS="$METADATA_ARGS --metadata duration-seconds=$DURATION"
        fi
    fi

    if aws s3 cp "$INPUT_FILE" "$S3_PATH" \
        --profile "$AWS_PROFILE" \
        --storage-class STANDARD_IA \
        $METADATA_ARGS; then
        echo "  ✓ Uploaded: $S3_PATH"
        ((UPLOADED++))
    else
        echo "  ✗ Failed: $BASENAME"
        ((FAILED++))
    fi
done

echo ""
echo "================================"
echo "Upload complete!"
echo "  Uploaded: $UPLOADED"
echo "  Failed: $FAILED"
echo "  S3 Location: s3://${S3_BUCKET}/${S3_PREFIX}/"
