#!/bin/bash
# rotate-gopro-180.sh - Rotate GoPro videos 180 degrees
# Usage: ./rotate-gopro-180.sh <input_dir> [output_dir]
# If output_dir not specified, creates "rotated" subdirectory in input_dir

set -e

INPUT_DIR="${1:-.}"
OUTPUT_DIR="${2:-$INPUT_DIR/rotated}"

if [[ ! -d "$INPUT_DIR" ]]; then
    echo "Error: Input directory does not exist: $INPUT_DIR"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

# Find all MP4 files (GoPro naming: GOPR*.MP4, GP*.MP4)
shopt -s nullglob nocaseglob
MP4_FILES=("$INPUT_DIR"/*.mp4 "$INPUT_DIR"/DCIM/100GOPRO/*.mp4)

if [[ ${#MP4_FILES[@]} -eq 0 ]]; then
    echo "No MP4 files found in $INPUT_DIR or $INPUT_DIR/DCIM/100GOPRO/"
    exit 1
fi

echo "Found ${#MP4_FILES[@]} video(s) to rotate"
echo "Output directory: $OUTPUT_DIR"
echo ""

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

PROCESSED=0
FAILED=0

for INPUT_FILE in "${MP4_FILES[@]}"; do
    BASENAME=$(basename "$INPUT_FILE")
    OUTPUT_FILE="$OUTPUT_DIR/$BASENAME"

    echo "Processing: $BASENAME"

    if ffmpeg -hide_banner -loglevel warning -i "$INPUT_FILE" \
        -vf "hflip,vflip" \
        $HW_ACCEL \
        -c:a copy \
        -map_metadata 0 \
        -movflags +faststart \
        "$OUTPUT_FILE"; then
        echo "  ✓ Done: $OUTPUT_FILE"
        ((PROCESSED++))
    else
        echo "  ✗ Failed: $BASENAME"
        ((FAILED++))
    fi
    echo ""
done

echo "================================"
echo "Rotation complete!"
echo "  Processed: $PROCESSED"
echo "  Failed: $FAILED"
echo "  Output: $OUTPUT_DIR"
