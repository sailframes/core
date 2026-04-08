"""
SailFrames Fleet Data Upload Handler

Receives files from S1/E1 devices and stores them in S3.
Triggered via API Gateway PUT/POST requests.

Endpoints:
  POST /upload?boat=X&file=PATH
    - Direct upload for small files (<5MB)
    - Body contains file data

  POST /upload?boat=X&file=PATH&presign=1&size=BYTES
    - Request presigned URL for large files (>5MB)
    - Returns presigned URL for direct S3 upload

Expected query parameters:
  - boat: Boat identifier (e.g., "E1", "S1")
  - file: Original file path (e.g., "/mnt/sailframes-data/2026-04-01/gps/track.csv")
  - presign: Set to "1" to request presigned URL instead of direct upload
  - size: File size in bytes (required when presign=1)

S3 storage structure:
  s3://bucket/
    raw/
      {boat_id}/
        {date}/
          {original_filename}
    processed/
      ... (for post-processed PPK data)
"""

import boto3
import json
import os
import base64
import re
from datetime import datetime
from urllib.parse import unquote

s3 = boto3.client('s3')
BUCKET = os.environ.get('BUCKET_NAME', 'sailframes-fleet-data-prod')

# Presigned URL expiry (1 hour)
PRESIGN_EXPIRY = 3600


def extract_date_from_path(file_path):
    """Extract date folder from file path or filename.

    Handles formats:
      - /mnt/sailframes-data/2026-04-01/gps/track.csv -> 2026-04-01
      - E1_20260401_143022_nav.csv -> 2026-04-01

    Returns None if date cannot be extracted (caller should try CSV content).
    """
    # First try to extract YYYY-MM-DD from path
    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', file_path)
    if date_match:
        return date_match.group(1)

    # Try to extract from filename with YYYYMMDD format
    filename = file_path.split('/')[-1]
    parts = filename.split('_')
    for part in parts:
        if len(part) == 8 and part.isdigit():
            return f"{part[:4]}-{part[4:6]}-{part[6:8]}"

    # Return None - caller should try to extract from CSV content
    return None


def extract_date_from_csv_content(csv_content, filename):
    """Extract date from CSV content.

    For E1 nav files with gps_date column (DDMMYY format).
    Falls back to today's date if not found.
    """
    if not filename.endswith('.csv') or '_nav.csv' not in filename:
        return datetime.now().strftime('%Y-%m-%d')

    try:
        # Parse CSV to find gps_date column
        lines = csv_content.split('\n')
        if len(lines) < 2:
            return datetime.now().strftime('%Y-%m-%d')

        # Parse header to find gps_date column index
        header = lines[0].strip().split(',')
        gps_date_idx = None
        for i, col in enumerate(header):
            if col.strip() == 'gps_date':
                gps_date_idx = i
                break

        if gps_date_idx is None:
            print(f"No gps_date column in CSV, using today's date")
            return datetime.now().strftime('%Y-%m-%d')

        # Look for first row with valid gps_date
        for line in lines[1:]:
            if not line.strip():
                continue
            fields = line.strip().split(',')
            if len(fields) > gps_date_idx:
                gps_date = fields[gps_date_idx].strip()
                if len(gps_date) == 6:
                    # Parse DDMMYY format
                    day = int(gps_date[:2])
                    month = int(gps_date[2:4])
                    year = 2000 + int(gps_date[4:6])
                    if 1 <= day <= 31 and 1 <= month <= 12:
                        result = f"{year}-{month:02d}-{day:02d}"
                        print(f"Extracted date from CSV gps_date column: {result}")
                        return result
    except Exception as e:
        print(f"Error parsing CSV for date: {e}")

    return datetime.now().strftime('%Y-%m-%d')


def lambda_handler(event, context):
    """Handle file upload or presigned URL request."""
    try:
        # Extract query parameters
        params = event.get('queryStringParameters', {}) or {}
        boat_id = params.get('boat', 'unknown')
        file_path = unquote(params.get('file', 'unknown'))
        request_presign = params.get('presign', '0') == '1'
        file_size = int(params.get('size', '0'))

        # Extract filename from path
        filename = file_path.split('/')[-1] if '/' in file_path else file_path

        # Extract date folder (may be None if not in path/filename)
        date_folder = extract_date_from_path(file_path)

        # For presigned URLs, we can't read CSV content, so fall back to today
        if date_folder is None and request_presign:
            date_folder = datetime.now().strftime('%Y-%m-%d')
            print(f"Presigned URL request without date in path, using today: {date_folder}")

        # Build S3 key (may be updated later if we extract date from CSV content)
        s3_key = f"raw/{boat_id}/{date_folder}/{filename}" if date_folder else None

        # Handle presigned URL request for large files
        if request_presign:
            # Determine content type
            content_type = 'application/octet-stream'
            if filename.endswith('.csv'):
                content_type = 'text/csv'
            elif filename.endswith('.json'):
                content_type = 'application/json'

            # Generate presigned URL for PUT
            presigned_url = s3.generate_presigned_url(
                'put_object',
                Params={
                    'Bucket': BUCKET,
                    'Key': s3_key,
                    'ContentType': content_type,
                    'Metadata': {
                        'boat_id': boat_id,
                        'original_path': file_path,
                    }
                },
                ExpiresIn=PRESIGN_EXPIRY
            )

            # Convert to HTTP for ESP32 E1 devices (TLS is broken on Arduino Core 3.3.7)
            # S3 supports both HTTP and HTTPS - fleet data is not sensitive
            presigned_url = presigned_url.replace('https://', 'http://', 1)

            print(f"Generated presigned URL (HTTP) for: {s3_key} ({file_size} bytes)")

            return {
                'statusCode': 200,
                'headers': {
                    'Content-Type': 'application/json'
                },
                'body': json.dumps({
                    'status': 'presigned',
                    'url': presigned_url,
                    'bucket': BUCKET,
                    'key': s3_key,
                    'content_type': content_type,
                    'expires_in': PRESIGN_EXPIRY
                })
            }

        # Get request body
        body = event.get('body', '')
        if not body:
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Empty request body'})
            }

        # Decode if base64 encoded
        if event.get('isBase64Encoded', False):
            body = base64.b64decode(body)
        elif isinstance(body, str):
            body = body.encode('utf-8')

        # If date_folder wasn't in path/filename, try to extract from CSV content
        if date_folder is None:
            csv_content = body.decode('utf-8', errors='ignore') if isinstance(body, bytes) else body
            date_folder = extract_date_from_csv_content(csv_content, filename)
            s3_key = f"raw/{boat_id}/{date_folder}/{filename}"
            print(f"Extracted date from CSV content: {date_folder}")

        # Determine content type
        content_type = 'application/octet-stream'
        if filename.endswith('.csv'):
            content_type = 'text/csv'
        elif filename.endswith('.rtcm3'):
            content_type = 'application/octet-stream'

        # Upload to S3
        s3.put_object(
            Bucket=BUCKET,
            Key=s3_key,
            Body=body,
            ContentType=content_type,
            Metadata={
                'boat_id': boat_id,
                'original_path': file_path,
                'upload_time': datetime.utcnow().isoformat(),
            }
        )

        print(f"Uploaded: {s3_key} ({len(body)} bytes)")

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json'
            },
            'body': json.dumps({
                'status': 'success',
                'bucket': BUCKET,
                'key': s3_key,
                'size': len(body)
            })
        }

    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }
