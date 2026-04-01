"""
SailFrames E1 Fleet Data Upload Handler

Receives files from E1 devices and stores them in S3.
Triggered via API Gateway PUT/POST requests.

Expected query parameters:
  - boat: Boat identifier (e.g., "E1", "E2")
  - file: Original file path (e.g., "/sf/20260331/E1_20260331_143022_nav.csv")

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
from datetime import datetime
from urllib.parse import unquote

s3 = boto3.client('s3')
BUCKET = os.environ.get('BUCKET_NAME', 'sailframes-data-prod')


def lambda_handler(event, context):
    """Handle file upload from E1 device."""
    try:
        # Extract query parameters
        params = event.get('queryStringParameters', {}) or {}
        boat_id = params.get('boat', 'unknown')
        file_path = unquote(params.get('file', 'unknown'))

        # Extract filename from path
        filename = file_path.split('/')[-1] if '/' in file_path else file_path

        # Extract date from filename (E1_YYYYMMDD_HHMMSS_type.ext)
        parts = filename.split('_')
        if len(parts) >= 2 and len(parts[1]) == 8:
            date_str = parts[1]
            date_folder = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        else:
            date_folder = datetime.now().strftime('%Y-%m-%d')

        # Build S3 key
        s3_key = f"raw/{boat_id}/{date_folder}/{filename}"

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
