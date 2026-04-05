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
    """Handle session API requests (list and delete)."""
    # HTTP API v2 uses requestContext.http.method, REST API uses httpMethod
    http_method = event.get('requestContext', {}).get('http', {}).get('method') or event.get('httpMethod', 'GET')
    path = event.get('rawPath', '') or event.get('path', '')

    logger.info(f"Request: {http_method} {path} pathParams={event.get('pathParameters')}")

    headers = {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, DELETE, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type'
    }

    # Handle CORS preflight
    if http_method == 'OPTIONS':
        return {'statusCode': 200, 'headers': headers, 'body': ''}

    try:
        # DELETE /api/sessions/{device_id}/{date}
        if http_method == 'DELETE':
            path_params = event.get('pathParameters', {}) or {}
            device_id = path_params.get('device_id')
            date = path_params.get('date')

            if not device_id or not date:
                return {
                    'statusCode': 400,
                    'headers': headers,
                    'body': json.dumps({'error': 'device_id and date are required'})
                }

            deleted_count = delete_session(device_id, date)
            if deleted_count == 0:
                return {
                    'statusCode': 404,
                    'headers': headers,
                    'body': json.dumps({'error': f'Session not found: {device_id}/{date}'})
                }

            return {
                'statusCode': 200,
                'headers': headers,
                'body': json.dumps({
                    'status': 'deleted',
                    'device_id': device_id,
                    'date': date,
                    'files_deleted': deleted_count
                })
            }

        # GET /api/sessions - list all sessions
        sessions = list_sessions()
        return {
            'statusCode': 200,
            'headers': headers,
            'body': json.dumps({'sessions': sessions})
        }
    except Exception as e:
        logger.error(f"Error handling request: {e}")
        return {
            'statusCode': 500,
            'headers': headers,
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

    # Sort by start_time descending (more accurate than date for multiple sessions per day)
    # Fall back to date if start_time not available
    sessions.sort(key=lambda s: s.get('start_time') or s.get('date', ''), reverse=True)

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


def delete_session(device_id: str, date: str) -> int:
    """Delete a session and all its data. Returns count of deleted objects."""
    total_deleted = 0

    # Delete from all prefixes where session data may exist
    prefixes = [
        f"processed/{device_id}/{date}/",
        f"raw/{device_id}/{date}/",
        f"hls/{device_id}/{date}/"
    ]

    for prefix in prefixes:
        deleted = _delete_s3_prefix(prefix)
        total_deleted += deleted
        if deleted > 0:
            logger.info(f"Deleted {deleted} objects from {prefix}")

    return total_deleted


def _delete_s3_prefix(prefix: str) -> int:
    """Delete all objects under an S3 prefix. Returns count of deleted objects."""
    deleted = 0
    paginator = s3.get_paginator('list_objects_v2')

    for page in paginator.paginate(Bucket=DATA_BUCKET, Prefix=prefix):
        objects = page.get('Contents', [])
        if objects:
            s3.delete_objects(
                Bucket=DATA_BUCKET,
                Delete={'Objects': [{'Key': obj['Key']} for obj in objects]}
            )
            deleted += len(objects)

    return deleted
