"""
SailFrames API - List Sessions
Returns all available race sessions with metadata.
"""

import json
import os
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
DATA_BUCKET = os.environ.get('DATA_BUCKET', 'sailframes-fleet-data-prod')


def lambda_handler(event, context):
    """List all available race sessions."""
    try:
        sessions = list_sessions()
        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'sessions': sessions})
        }
    except Exception as e:
        logger.error(f"Error listing sessions: {e}")
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': str(e)})
        }


def list_sessions() -> list:
    """Scan processed/ prefix for session manifests."""
    sessions = []

    # List all manifest.json files
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=DATA_BUCKET, Prefix='processed/'):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key.endswith('/manifest.json'):
                try:
                    response = s3.get_object(Bucket=DATA_BUCKET, Key=key)
                    manifest = json.loads(response['Body'].read().decode('utf-8'))

                    # Add computed fields
                    manifest['has_video'] = check_video_exists(
                        manifest.get('device_id'),
                        manifest.get('date')
                    )

                    # Calculate duration
                    if manifest.get('start_time') and manifest.get('end_time'):
                        from datetime import datetime
                        start = datetime.fromisoformat(manifest['start_time'].replace('Z', '+00:00'))
                        end = datetime.fromisoformat(manifest['end_time'].replace('Z', '+00:00'))
                        manifest['duration_minutes'] = int((end - start).total_seconds() / 60)

                    sessions.append(manifest)
                except Exception as e:
                    logger.warning(f"Failed to read manifest {key}: {e}")

    # Sort by date descending
    sessions.sort(key=lambda s: s.get('date', ''), reverse=True)

    return sessions


def check_video_exists(device_id: str, date: str) -> bool:
    """Check if HLS video exists for this session."""
    if not device_id or not date:
        return False

    try:
        response = s3.list_objects_v2(
            Bucket=DATA_BUCKET,
            Prefix=f"hls/{device_id}/{date}/",
            MaxKeys=1
        )
        return response.get('KeyCount', 0) > 0
    except Exception:
        return False
