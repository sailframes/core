#!/bin/bash
# upload-gopro-s3.sh - Upload GoPro videos to AWS S3
# Usage: ./upload-gopro-s3.sh <date> [input_dir]
# Example: ./upload-gopro-s3.sh 2026-04-03 /path/to/GoPro/rotated

set -e

AWS_PROFILE="${AWS_PROFILE:-sailframes}"
S3_BUCKET="sailframes-fleet-data-prod"

DATE="${1}"
INPUT_DIR="${2:-}"

if [[ -z "$DATE" ]]; then
    echo "Usage: $0 <date> [input_dir]"
    echo "  date: Sail date in YYYY-MM-DD format"
    echo "  input_dir: Directory containing MP4 files (optional)"
    echo ""
    echo "If input_dir not specified, looks for:"
    echo "  /Users/paul2/sailframes/data/sail_<date>/GoPro/rotated"
    echo ""
    echo "Environment variables:"
    echo "  AWS_PROFILE - AWS profile to use (default: sailframes)"
    exit 1
fi

# Default input directory if not specified
if [[ -z "$INPUT_DIR" ]]; then
    INPUT_DIR="/Users/paul2/sailframes/data/sail_${DATE}/GoPro/rotated"
fi

if [[ ! -d "$INPUT_DIR" ]]; then
    echo "Error: Directory does not exist: $INPUT_DIR"
    exit 1
fi

S3_PREFIX="raw/gopro/${DATE}/video"

echo "Upload Configuration:"
echo "  Source: $INPUT_DIR"
echo "  Destination: s3://${S3_BUCKET}/${S3_PREFIX}/"
echo "  AWS Profile: $AWS_PROFILE"
echo ""

# Find MP4 files
shopt -s nullglob nocaseglob
MP4_FILES=("$INPUT_DIR"/*.mp4)

if [[ ${#MP4_FILES[@]} -eq 0 ]]; then
    echo "No MP4 files found in $INPUT_DIR"
    exit 1
fi

echo "Found ${#MP4_FILES[@]} video(s) to upload:"
for f in "${MP4_FILES[@]}"; do
    SIZE=$(du -h "$f" | cut -f1)
    echo "  $(basename "$f") ($SIZE)"
done
echo ""

read -p "Proceed with upload? [y/N] " -n 1 -r
echo ""

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Upload cancelled."
    exit 0
fi

echo ""
echo "Uploading..."

UPLOADED=0
FAILED=0

for INPUT_FILE in "${MP4_FILES[@]}"; do
    BASENAME=$(basename "$INPUT_FILE")
    S3_PATH="s3://${S3_BUCKET}/${S3_PREFIX}/${BASENAME}"

    echo "Uploading: $BASENAME"

    if aws s3 cp "$INPUT_FILE" "$S3_PATH" \
        --profile "$AWS_PROFILE" \
        --storage-class STANDARD_IA; then
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
