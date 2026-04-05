#!/bin/bash
# process-gopro-sail.sh - Rotate and upload GoPro videos from a sailing session
# Usage: ./process-gopro-sail.sh <date> [--rotate-only | --upload-only]
#
# Examples:
#   ./process-gopro-sail.sh 2026-04-03           # Rotate and upload
#   ./process-gopro-sail.sh 2026-04-03 --rotate-only   # Just rotate
#   ./process-gopro-sail.sh 2026-04-03 --upload-only   # Just upload (assumes already rotated)

set -e

AWS_PROFILE="${AWS_PROFILE:-sailframes}"
S3_BUCKET="sailframes-fleet-data-prod"
DATA_DIR="/Users/paul2/sailframes/data"

DATE="${1}"
MODE="${2:-full}"  # full, --rotate-only, --upload-only

if [[ -z "$DATE" ]]; then
    echo "Usage: $0 <date> [--rotate-only | --upload-only]"
    echo ""
    echo "Examples:"
    echo "  $0 2026-04-03           # Rotate and upload"
    echo "  $0 2026-04-03 --rotate-only   # Just rotate"
    echo "  $0 2026-04-03 --upload-only   # Just upload (assumes already rotated)"
    exit 1
fi

SAIL_DIR="$DATA_DIR/sail_$DATE"
GOPRO_INPUT="$SAIL_DIR/GoPro/DCIM/100GOPRO"
GOPRO_OUTPUT="$SAIL_DIR/GoPro/rotated"
S3_PREFIX="raw/gopro/${DATE}/video"

echo "=========================================="
echo "GoPro Sail Video Processor"
echo "=========================================="
echo "Date: $DATE"
echo "Mode: $MODE"
echo ""

# Check input directory exists
if [[ ! -d "$SAIL_DIR/GoPro" ]]; then
    echo "Error: No GoPro directory found at $SAIL_DIR/GoPro"
    exit 1
fi

# Find source MP4 files
shopt -s nullglob nocaseglob
if [[ -d "$GOPRO_INPUT" ]]; then
    SOURCE_FILES=("$GOPRO_INPUT"/*.mp4)
else
    SOURCE_FILES=("$SAIL_DIR/GoPro"/*.mp4)
fi

if [[ ${#SOURCE_FILES[@]} -eq 0 ]]; then
    echo "Error: No MP4 files found"
    exit 1
fi

echo "Found ${#SOURCE_FILES[@]} source video(s)"
echo ""

# ============ ROTATION ============
if [[ "$MODE" != "--upload-only" ]]; then
    echo "Step 1: Rotating videos 180 degrees"
    echo "----------------------------------------"

    mkdir -p "$GOPRO_OUTPUT"

    # Check for hardware acceleration (macOS VideoToolbox)
    # Use bitrate matching original GoPro footage (~30Mbps for 1080p)
    HW_ACCEL=""
    if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_videotoolbox; then
        HW_ACCEL="-c:v h264_videotoolbox -b:v 30M"
        echo "Using hardware acceleration (VideoToolbox) at 30Mbps"
    else
        HW_ACCEL="-c:v libx264 -preset fast -crf 20"
        echo "Using software encoding (libx264)"
    fi
    echo ""

    ROTATED=0
    for INPUT_FILE in "${SOURCE_FILES[@]}"; do
        BASENAME=$(basename "$INPUT_FILE")
        OUTPUT_FILE="$GOPRO_OUTPUT/$BASENAME"

        if [[ -f "$OUTPUT_FILE" ]]; then
            echo "Skipping (already exists): $BASENAME"
            ((ROTATED++))
            continue
        fi

        echo "Rotating: $BASENAME"
        if ffmpeg -hide_banner -loglevel warning -i "$INPUT_FILE" \
            -vf "hflip,vflip" \
            $HW_ACCEL \
            -c:a copy \
            -map_metadata 0 \
            -movflags +faststart \
            "$OUTPUT_FILE"; then
            echo "  Done: $OUTPUT_FILE"
            ((ROTATED++))
        else
            echo "  Failed: $BASENAME"
        fi
    done

    echo ""
    echo "Rotation complete: $ROTATED videos processed"
    echo ""
fi

# ============ UPLOAD ============
if [[ "$MODE" != "--rotate-only" ]]; then
    echo "Step 2: Uploading to S3"
    echo "----------------------------------------"
    echo "Destination: s3://${S3_BUCKET}/${S3_PREFIX}/"
    echo ""

    # Find rotated files
    ROTATED_FILES=("$GOPRO_OUTPUT"/*.mp4 "$GOPRO_OUTPUT"/*.MP4)

    if [[ ${#ROTATED_FILES[@]} -eq 0 ]]; then
        echo "Error: No rotated videos found in $GOPRO_OUTPUT"
        echo "Run with --rotate-only first, or check rotation completed successfully."
        exit 1
    fi

    UPLOADED=0
    FAILED=0

    for INPUT_FILE in "${ROTATED_FILES[@]}"; do
        [[ -f "$INPUT_FILE" ]] || continue

        BASENAME=$(basename "$INPUT_FILE")
        S3_PATH="s3://${S3_BUCKET}/${S3_PREFIX}/${BASENAME}"

        echo "Uploading: $BASENAME"

        if aws s3 cp "$INPUT_FILE" "$S3_PATH" \
            --profile "$AWS_PROFILE" \
            --storage-class STANDARD_IA; then
            echo "  Uploaded: $S3_PATH"
            echo "  Deleting local file..."
            rm "$INPUT_FILE"
            ((UPLOADED++))
        else
            echo "  Failed: $BASENAME"
            ((FAILED++))
        fi
        echo ""
    done

    echo "Upload complete: $UPLOADED uploaded, $FAILED failed"
    echo "S3 Location: s3://${S3_BUCKET}/${S3_PREFIX}/"
fi

echo ""
echo "=========================================="
echo "Processing complete!"
echo "=========================================="
