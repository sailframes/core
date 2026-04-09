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
        'Access-Control-Allow-Methods': 'GET, DELETE, PATCH, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type'
    }

    # Handle CORS preflight
    if http_method == 'OPTIONS':
        return {
            'statusCode': 200,
            'headers': headers,
            'body': json.dumps({'status': 'ok'})
        }

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

        # PATCH /api/sessions/{device_id}/{date} - update trim
        if http_method == 'PATCH':
            path_params = event.get('pathParameters', {}) or {}
            device_id = path_params.get('device_id')
            date = path_params.get('date')

            if not device_id or not date:
                return {
                    'statusCode': 400,
                    'headers': headers,
                    'body': json.dumps({'error': 'device_id and date are required'})
                }

            # Parse request body
            body = event.get('body', '{}')
            if isinstance(body, str):
                body = json.loads(body)

            # Handle different update types
            result = update_session(device_id, date, body)
            if result.get('error'):
                return {
                    'statusCode': 404,
                    'headers': headers,
                    'body': json.dumps(result)
                }

            return {
                'statusCode': 200,
                'headers': headers,
                'body': json.dumps(result)
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


def update_session(device_id: str, date: str, updates: dict) -> dict:
    """Update session manifest with provided fields (trim, name, boat).

    When trim is provided, actually deletes sensor data outside the trim bounds.
    This is a destructive operation - data is permanently removed.
    """
    manifest_key = f"processed/{device_id}/{date}/manifest.json"

    try:
        # Read existing manifest
        response = s3.get_object(Bucket=DATA_BUCKET, Key=manifest_key)
        manifest = json.loads(response['Body'].read().decode('utf-8'))
    except s3.exceptions.NoSuchKey:
        return {'error': f'Session not found: {device_id}/{date}'}
    except Exception as e:
        logger.error(f"Failed to read manifest: {e}")
        return {'error': str(e)}

    updated_fields = []
    trim_stats = {}

    # Handle trim updates - actually delete data outside trim bounds
    if 'trim' in updates:
        trim = updates['trim']
        if trim is None:
            # Reset trim just clears the metadata (can't restore deleted data)
            manifest.pop('trim', None)
            logger.info(f"Removed trim metadata from {manifest_key}")
        else:
            # Apply trim - delete data outside bounds
            trim_start = trim.get('start')
            trim_end = trim.get('end')

            logger.info(f"Applying trim to {device_id}/{date}: start={trim_start}, end={trim_end}")

            # Trim each sensor data file
            for sensor_type in ['gps', 'imu', 'wind', 'pressure']:
                sensor_key = f"processed/{device_id}/{date}/{sensor_type}.json"
                result = _trim_sensor_data(sensor_key, trim_start, trim_end)
                if result:
                    trim_stats[sensor_type] = result
                    # Update manifest sensor info
                    if sensor_type in manifest.get('sensors', {}):
                        manifest['sensors'][sensor_type] = {
                            'samples': result['after'],
                            'start_time': result['new_start'],
                            'end_time': result['new_end']
                        }

            # Update manifest overall bounds from trimmed data
            all_starts = []
            all_ends = []
            for sensor_info in manifest.get('sensors', {}).values():
                if sensor_info.get('start_time'):
                    all_starts.append(sensor_info['start_time'])
                if sensor_info.get('end_time'):
                    all_ends.append(sensor_info['end_time'])

            if all_starts:
                manifest['start_time'] = min(all_starts)
            if all_ends:
                manifest['end_time'] = max(all_ends)

            # Clear trim metadata since we've applied it permanently
            manifest.pop('trim', None)
            logger.info(f"Trim applied and data deleted: {trim_stats}")

        updated_fields.append('trim')

    # Handle name updates
    if 'name' in updates:
        name = updates['name']
        if name is None or name == '':
            manifest.pop('name', None)
        else:
            manifest['name'] = name
        updated_fields.append('name')

    # Handle boat updates
    if 'boat' in updates:
        boat = updates['boat']
        if boat is None or boat == '':
            manifest.pop('boat', None)
        else:
            manifest['boat'] = boat
        updated_fields.append('boat')

    # Write updated manifest
    try:
        s3.put_object(
            Bucket=DATA_BUCKET,
            Key=manifest_key,
            Body=json.dumps(manifest, indent=2),
            ContentType='application/json'
        )
    except Exception as e:
        logger.error(f"Failed to write manifest: {e}")
        return {'error': str(e)}

    logger.info(f"Updated session {device_id}/{date}: {updated_fields}")

    result = {
        'status': 'updated',
        'device_id': device_id,
        'date': date,
        'updated': updated_fields
    }

    if trim_stats:
        result['trim_stats'] = trim_stats

    return result


def _trim_sensor_data(sensor_key: str, trim_start: str, trim_end: str) -> dict:
    """Trim sensor data to only include points within the trim bounds.

    Args:
        sensor_key: S3 key of the sensor JSON file
        trim_start: ISO timestamp for start bound (inclusive), or None
        trim_end: ISO timestamp for end bound (inclusive), or None

    Returns:
        Dict with before/after counts and new time bounds, or None if file doesn't exist
    """
    try:
        response = s3.get_object(Bucket=DATA_BUCKET, Key=sensor_key)
        data = json.loads(response['Body'].read().decode('utf-8'))
    except s3.exceptions.NoSuchKey:
        return None
    except Exception as e:
        logger.warning(f"Failed to read {sensor_key}: {e}")
        return None

    if not data:
        return None

    before_count = len(data)

    # Filter data to only include points within trim bounds
    filtered = []
    for point in data:
        t = point.get('t', '')
        if not t:
            continue

        # Check bounds (ISO string comparison works for timestamps)
        if trim_start and t < trim_start:
            continue
        if trim_end and t > trim_end:
            continue

        filtered.append(point)

    after_count = len(filtered)

    # Only write if data changed
    if after_count != before_count:
        try:
            s3.put_object(
                Bucket=DATA_BUCKET,
                Key=sensor_key,
                Body=json.dumps(filtered),
                ContentType='application/json'
            )
            logger.info(f"Trimmed {sensor_key}: {before_count} -> {after_count} samples")
        except Exception as e:
            logger.error(f"Failed to write trimmed data to {sensor_key}: {e}")
            return None

    # Get new time bounds
    times = [p['t'] for p in filtered if p.get('t')]
    new_start = min(times) if times else None
    new_end = max(times) if times else None

    return {
        'before': before_count,
        'after': after_count,
        'removed': before_count - after_count,
        'new_start': new_start,
        'new_end': new_end
    }


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

    logger.info(f"Attempting to delete objects with prefix: {prefix}")

    for page in paginator.paginate(Bucket=DATA_BUCKET, Prefix=prefix):
        objects = page.get('Contents', [])
        if objects:
            keys = [{'Key': obj['Key']} for obj in objects]
            logger.info(f"Deleting {len(keys)} objects: {[k['Key'] for k in keys]}")

            response = s3.delete_objects(
                Bucket=DATA_BUCKET,
                Delete={'Objects': keys}
            )

            # Check for errors
            errors = response.get('Errors', [])
            if errors:
                logger.error(f"Delete errors: {errors}")

            successful = response.get('Deleted', [])
            deleted += len(successful)
            logger.info(f"Successfully deleted {len(successful)} objects")

    return deleted
