"""
SailFrames API - Get Session Data
Returns time-synchronized sensor data for a session.
"""

import json
import os
import boto3
import logging
from datetime import datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
DATA_BUCKET = os.environ.get('DATA_BUCKET', 'sailframes-fleet-data-prod')


def lambda_handler(event, context):
    """Return sensor data for a session."""
    try:
        # Parse path parameters
        path_params = event.get('pathParameters', {})
        device_id = path_params.get('device_id')
        date = path_params.get('date')

        if not device_id or not date:
            return error_response(400, 'Missing device_id or date')

        # Parse query parameters
        query_params = event.get('queryStringParameters') or {}
        sensors = query_params.get('sensors', 'gps,imu,wind,pressure,ppk').split(',')
        start_time = query_params.get('start')
        end_time = query_params.get('end')
        resolution = query_params.get('resolution', 'high')

        # Load data
        data = load_session_data(device_id, date, sensors, start_time, end_time, resolution)

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps(data)
        }
    except Exception as e:
        logger.error(f"Error getting session data: {e}")
        return error_response(500, str(e))


def error_response(status: int, message: str) -> dict:
    return {
        'statusCode': status,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps({'error': message})
    }


def get_manifest(device_id: str, date: str) -> dict:
    """Load session manifest."""
    key = f"processed/{device_id}/{date}/manifest.json"
    try:
        response = s3.get_object(Bucket=DATA_BUCKET, Key=key)
        return json.loads(response['Body'].read().decode('utf-8'))
    except Exception as e:
        logger.warning(f"Could not load manifest: {e}")
        return {}


def load_session_data(device_id: str, date: str, sensors: list,
                      start_time: str, end_time: str, resolution: str) -> dict:
    """Load and merge sensor data for the session."""
    # Load manifest for session bounds (includes video times)
    manifest = get_manifest(device_id, date)

    result = {
        'device_id': device_id,
        'date': date,
        'data': []
    }

    # Load each sensor file
    sensor_data = {}
    # Map sensor names to filenames (most are {sensor}.json, ppk is ppk_gps.json)
    sensor_file_map = {'ppk': 'ppk_gps'}
    for sensor in sensors:
        sensor = sensor.strip()
        filename = sensor_file_map.get(sensor, sensor)
        key = f"processed/{device_id}/{date}/{filename}.json"
        try:
            response = s3.get_object(Bucket=DATA_BUCKET, Key=key)
            data = json.loads(response['Body'].read().decode('utf-8'))
            sensor_data[sensor] = {d['t']: d for d in data}
            logger.info(f"Loaded {len(data)} records from {sensor}")
        except s3.exceptions.NoSuchKey:
            logger.warning(f"Sensor data not found: {key}")
        except Exception as e:
            logger.error(f"Error loading {key}: {e}")

    if not sensor_data:
        return result

    # Get all unique timestamps
    all_times = set()
    for data in sensor_data.values():
        all_times.update(data.keys())

    # Filter by time range if specified
    if start_time:
        all_times = {t for t in all_times if t >= start_time}
    if end_time:
        all_times = {t for t in all_times if t <= end_time}

    # Sort timestamps
    sorted_times = sorted(all_times)

    # Downsample if low resolution requested
    if resolution == 'low' and len(sorted_times) > 100:
        step = len(sorted_times) // 100
        sorted_times = sorted_times[::step]

    # Merge data by timestamp
    merged = []
    for t in sorted_times:
        point = {'t': t}

        for sensor, data in sensor_data.items():
            if t in data:
                sensor_point = data[t].copy()
                del sensor_point['t']
                point[sensor] = sensor_point

        merged.append(point)

    result['data'] = merged
    result['sample_count'] = len(merged)

    # Use manifest session times (includes video) if available, else fall back to sensor times
    if manifest.get('start_time'):
        result['start_time'] = manifest['start_time']
        result['end_time'] = manifest.get('end_time') or (sorted_times[-1] if sorted_times else None)
    else:
        result['start_time'] = sorted_times[0] if sorted_times else None
        result['end_time'] = sorted_times[-1] if sorted_times else None

    # Include trim bounds if present
    if manifest.get('trim'):
        result['trim'] = manifest['trim']

    # Include session name and boat if present
    if manifest.get('name'):
        result['name'] = manifest['name']
    if manifest.get('boat'):
        result['boat'] = manifest['boat']

    return result
