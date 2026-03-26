"""
SailFrames Data Processing Lambda
Triggered when CSV files are uploaded to raw/ prefix.
Downsamples sensor data and outputs JSON for web visualization.
"""

import json
import os
import boto3
import csv
from io import StringIO
from datetime import datetime, timezone
from collections import defaultdict
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
DATA_BUCKET = os.environ.get('DATA_BUCKET', 'sailframes-data-prod')


def lambda_handler(event, context):
    """Process uploaded CSV files and create downsampled JSON."""
    for record in event.get('Records', []):
        bucket = record['s3']['bucket']['name']
        key = record['s3']['object']['key']

        logger.info(f"Processing {bucket}/{key}")

        try:
            process_file(bucket, key)
        except Exception as e:
            logger.error(f"Failed to process {key}: {e}")
            raise

    return {'statusCode': 200, 'body': 'OK'}


def process_file(bucket: str, key: str):
    """Process a single CSV file."""
    # Parse path: raw/{device_id}/{date}/{sensor_type}/{filename}.csv
    parts = key.split('/')
    if len(parts) < 5:
        logger.warning(f"Invalid path structure: {key}")
        return

    device_id = parts[1]
    date = parts[2]
    sensor_type = parts[3]

    # Download CSV
    response = s3.get_object(Bucket=bucket, Key=key)
    csv_content = response['Body'].read().decode('utf-8')

    # Parse and downsample
    if sensor_type == 'gps':
        data = process_gps(csv_content)
    elif sensor_type == 'imu':
        data = process_imu(csv_content)
    elif sensor_type == 'pressure':
        data = process_pressure(csv_content)
    elif sensor_type == 'wind':
        data = process_wind(csv_content)
    else:
        logger.warning(f"Unknown sensor type: {sensor_type}")
        return

    # Upload processed JSON
    output_key = f"processed/{device_id}/{date}/{sensor_type}.json"
    s3.put_object(
        Bucket=bucket,
        Key=output_key,
        Body=json.dumps(data, default=str),
        ContentType='application/json'
    )
    logger.info(f"Wrote {len(data)} records to {output_key}")

    # Update manifest
    update_manifest(bucket, device_id, date, sensor_type, data)


def process_gps(csv_content: str) -> list:
    """Downsample GPS from 10Hz to 1Hz, keeping max speed per second."""
    from datetime import timedelta

    # Time correction: Pi5 clock was ~41 minutes ahead (sail started 1pm ET = 17:00 UTC)
    TIME_CORRECTION_SECONDS = -2460  # -41 minutes

    reader = csv.DictReader(StringIO(csv_content))

    # Group by second with time correction
    by_second = defaultdict(list)
    for row in reader:
        ts = row.get('utc_time', '')
        if not ts:
            continue
        try:
            dt = datetime.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S')
            dt_corrected = dt + timedelta(seconds=TIME_CORRECTION_SECONDS)
            second = dt_corrected.strftime('%Y-%m-%dT%H:%M:%S')
        except ValueError:
            second = ts[:19]
        by_second[second].append(row)

    # Take sample with max speed per second
    result = []
    for second, samples in sorted(by_second.items()):
        best = max(samples, key=lambda r: float(r.get('speed_knots', 0) or 0))
        result.append({
            't': second + 'Z',
            'lat': float(best.get('latitude', 0) or 0),
            'lon': float(best.get('longitude', 0) or 0),
            'speed_kn': round(float(best.get('speed_knots', 0) or 0), 2),
            'course': round(float(best.get('course_deg', 0) or 0), 1),
            'fix': int(best.get('fix_quality', 0) or 0),
            'sats': int(best.get('satellites', 0) or 0)
        })

    return result


def process_imu(csv_content: str) -> list:
    """Downsample IMU from 50Hz to 1Hz, averaging values."""
    from datetime import timedelta

    # Time correction: Pi5 clock was ~41 minutes ahead (sail started 1pm ET = 17:00 UTC)
    TIME_CORRECTION_SECONDS = -2460  # -41 minutes

    reader = csv.DictReader(StringIO(csv_content))

    # Group by second with time correction
    by_second = defaultdict(list)
    for row in reader:
        ts = row.get('utc_time', '')
        if not ts:
            continue
        try:
            dt = datetime.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S')
            dt_corrected = dt + timedelta(seconds=TIME_CORRECTION_SECONDS)
            second = dt_corrected.strftime('%Y-%m-%dT%H:%M:%S')
        except ValueError:
            second = ts[:19]
        by_second[second].append(row)

    # Average values per second
    result = []
    for second, samples in sorted(by_second.items()):
        n = len(samples)
        avg = lambda field: sum(float(r.get(field, 0) or 0) for r in samples) / n

        # Heading correction: IMU mounted 180° (X-axis toward stern)
        raw_heading = avg('heading_deg')
        corrected_heading = (raw_heading + 180) % 360

        # Heel correction: IMU mounted 90° off
        raw_heel = avg('heel_deg')
        corrected_heel = raw_heel + 90

        result.append({
            't': second + 'Z',
            'heel': round(corrected_heel, 1),
            'pitch': round(avg('pitch_deg'), 1),
            'heading': round(corrected_heading, 1),
            'accel_x': round(avg('accel_x_mps2'), 2),
            'accel_y': round(avg('accel_y_mps2'), 2),
            'accel_z': round(avg('accel_z_mps2'), 2)
        })

    return result


def process_pressure(csv_content: str) -> list:
    """Process pressure data (already 1Hz, minimal transformation)."""
    from datetime import timedelta

    # Time correction: Pi5 clock was ~41 minutes ahead (sail started 1pm ET = 17:00 UTC)
    TIME_CORRECTION_SECONDS = -2460  # -41 minutes

    reader = csv.DictReader(StringIO(csv_content))

    result = []
    for row in reader:
        ts = row.get('utc_time', '')
        if not ts:
            continue

        # Apply time correction
        try:
            dt = datetime.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S')
            dt_corrected = dt + timedelta(seconds=TIME_CORRECTION_SECONDS)
            corrected_ts = dt_corrected.strftime('%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            corrected_ts = ts[:19] + 'Z'

        result.append({
            't': corrected_ts,
            'hpa': round(float(row.get('pressure_hpa', 0) or 0), 1),
            'temp_c': round(float(row.get('temperature_c', 0) or 0), 1),
            'trend': row.get('pressure_trend', '')
        })

    return result


def process_wind(csv_content: str) -> list:
    """Process wind data (already 1Hz, minimal transformation)."""
    from datetime import timedelta

    # Time correction: Pi5 clock was ~41 minutes ahead (sail started 1pm ET = 17:00 UTC)
    TIME_CORRECTION_SECONDS = -2460  # -41 minutes

    reader = csv.DictReader(StringIO(csv_content))

    result = []
    for row in reader:
        ts = row.get('utc_time', '')
        if not ts:
            continue

        # Apply time correction
        try:
            dt = datetime.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S')
            dt_corrected = dt + timedelta(seconds=TIME_CORRECTION_SECONDS)
            corrected_ts = dt_corrected.strftime('%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            corrected_ts = ts[:19] + 'Z'

        result.append({
            't': corrected_ts,
            'aws_kn': round(float(row.get('apparent_wind_speed_knots', 0) or 0), 1),
            'awa': round(float(row.get('apparent_wind_angle_deg', 0) or 0), 0),
            'heading': round((float(row.get('compass_heading_deg', 0) or 0) + 180) % 360, 1)
        })

    return result


def update_manifest(bucket: str, device_id: str, date: str, sensor_type: str, data: list):
    """Update or create session manifest with metadata."""
    manifest_key = f"processed/{device_id}/{date}/manifest.json"

    # Try to load existing manifest
    try:
        response = s3.get_object(Bucket=bucket, Key=manifest_key)
        manifest = json.loads(response['Body'].read().decode('utf-8'))
    except s3.exceptions.NoSuchKey:
        manifest = {
            'device_id': device_id,
            'date': date,
            'sensors': {},
            'created_at': datetime.now(timezone.utc).isoformat()
        }

    # Update sensor info
    if data:
        times = [d['t'] for d in data if 't' in d]
        manifest['sensors'][sensor_type] = {
            'samples': len(data),
            'start_time': min(times) if times else None,
            'end_time': max(times) if times else None
        }

        # Update session bounds
        all_times = []
        for sensor_info in manifest['sensors'].values():
            if sensor_info.get('start_time'):
                all_times.append(sensor_info['start_time'])
            if sensor_info.get('end_time'):
                all_times.append(sensor_info['end_time'])

        if all_times:
            manifest['start_time'] = min(all_times)
            manifest['end_time'] = max(all_times)

        # Calculate track bounds from GPS
        if sensor_type == 'gps' and data:
            lats = [d['lat'] for d in data if d.get('lat')]
            lons = [d['lon'] for d in data if d.get('lon')]
            if lats and lons:
                manifest['track_bounds'] = {
                    'north': max(lats),
                    'south': min(lats),
                    'east': max(lons),
                    'west': min(lons)
                }

    manifest['updated_at'] = datetime.now(timezone.utc).isoformat()

    # Save manifest
    s3.put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=json.dumps(manifest, indent=2),
        ContentType='application/json'
    )
    logger.info(f"Updated manifest: {manifest_key}")
