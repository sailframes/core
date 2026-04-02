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
DATA_BUCKET = os.environ.get('DATA_BUCKET', 'sailframes-fleet-data-prod')


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
    """Process a single CSV file.

    Supports two path structures:
    - S1 format: raw/{device_id}/{date}/{sensor_type}/{filename}.csv (5+ parts)
    - E1 format: raw/{device_id}/{date}/{filename}.csv (4 parts, sensor in filename)
    """
    parts = key.split('/')

    if len(parts) >= 5:
        # S1 format: raw/{device}/{date}/{sensor}/{file}.csv
        device_id = parts[1]
        date = parts[2]
        sensor_type = parts[3]
    elif len(parts) == 4:
        # E1 format: raw/{device}/{date}/{file}.csv
        device_id = parts[1]
        date = parts[2]
        filename = parts[3]
        # Extract sensor type from filename suffix (e.g., E1_20260401_120000_nav.csv)
        if '_nav.csv' in filename:
            sensor_type = 'gps'
        elif '_imu.csv' in filename:
            sensor_type = 'imu'
        elif '_pressure.csv' in filename or '_baro.csv' in filename:
            sensor_type = 'pressure'
        elif '_wind.csv' in filename:
            sensor_type = 'wind'
        else:
            logger.warning(f"Unknown E1 file type: {filename}")
            return
    else:
        logger.warning(f"Invalid path structure: {key}")
        return

    # Download CSV
    response = s3.get_object(Bucket=bucket, Key=key)
    csv_content = response['Body'].read().decode('utf-8')

    # Parse and downsample (pass date for E1 timestamp generation)
    if sensor_type == 'gps':
        data = process_gps(csv_content, date)
    elif sensor_type == 'imu':
        data = process_imu(csv_content, date)
    elif sensor_type == 'pressure':
        data = process_pressure(csv_content)
    elif sensor_type == 'wind':
        data = process_wind(csv_content, date)
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


def process_gps(csv_content: str, date: str = None) -> list:
    """Downsample GPS from 10Hz to 1Hz, keeping max speed per second.

    Supports two CSV formats:
    - S1: utc_time,latitude,longitude,speed_knots,course_deg,fix_quality,satellites
    - E1: ms,utc,lat,lon,alt,sog,cog,sat,hdop,fix

    Args:
        csv_content: CSV data as string
        date: Date string (YYYY-MM-DD) for E1 timestamp generation
    """
    from datetime import timedelta

    reader = csv.DictReader(StringIO(csv_content))
    rows = list(reader)
    if not rows:
        return []

    # Detect format based on column names
    first_row = rows[0]
    is_e1_format = 'utc' in first_row and 'lat' in first_row

    # Time correction only for S1 (Pi5 clock was ~41 minutes ahead)
    TIME_CORRECTION_SECONDS = 0 if is_e1_format else -2460

    # Group by second
    by_second = defaultdict(list)
    for row in rows:
        if is_e1_format:
            # E1 format: utc is HHMMSS.mmm (e.g., "123756.100")
            utc_raw = row.get('utc', '')
            if not utc_raw:
                continue
            try:
                # Parse HHMMSS.mmm format
                utc_float = float(utc_raw)
                hours = int(utc_float // 10000)
                minutes = int((utc_float % 10000) // 100)
                seconds = int(utc_float % 100)
                # Combine date from path with time from CSV
                second = f"{date}T{hours:02d}:{minutes:02d}:{seconds:02d}"
            except ValueError:
                continue
        else:
            # S1 format: utc_time is ISO timestamp
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
        if is_e1_format:
            best = max(samples, key=lambda r: float(r.get('sog', 0) or 0))
            result.append({
                't': second + 'Z',
                'lat': float(best.get('lat', 0) or 0),
                'lon': float(best.get('lon', 0) or 0),
                'speed_kn': round(float(best.get('sog', 0) or 0), 2),
                'course': round(float(best.get('cog', 0) or 0), 1),
                'fix': int(best.get('fix', 0) or 0),
                'sats': int(best.get('sat', 0) or 0)
            })
        else:
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


def process_imu(csv_content: str, date: str = None) -> list:
    """Downsample IMU from 50Hz to 1Hz, averaging values.

    Supports two CSV formats:
    - S1: utc_time,heel_deg,pitch_deg,heading_deg,accel_x_mps2,accel_y_mps2,accel_z_mps2
    - E1 (new): ms,utc,ax,ay,az,gx,gy,gz,heel,pitch
    - E1 (old): ms,ax,ay,az,gx,gy,gz,heel,pitch

    Args:
        csv_content: CSV data as string
        date: Date string (YYYY-MM-DD) for E1 timestamp generation
    """
    from datetime import timedelta

    reader = csv.DictReader(StringIO(csv_content))
    rows = list(reader)
    if not rows:
        return []

    # Detect format based on column names
    first_row = rows[0]
    is_e1_format = 'ms' in first_row and 'ax' in first_row
    has_utc = 'utc' in first_row  # New E1 format with GPS time

    # Time correction only for S1 (Pi5 clock was ~41 minutes ahead)
    TIME_CORRECTION_SECONDS = 0 if is_e1_format else -2460

    # Group by second
    by_second = defaultdict(list)
    for row in rows:
        if is_e1_format:
            if has_utc:
                # New E1 format: utc is HHMMSS.mmm from GPS
                utc_raw = row.get('utc', '')
                if not utc_raw:
                    continue
                try:
                    utc_float = float(utc_raw)
                    hours = int(utc_float // 10000)
                    minutes = int((utc_float % 10000) // 100)
                    seconds = int(utc_float % 100)
                    second = f"{date}T{hours:02d}:{minutes:02d}:{seconds:02d}"
                except ValueError:
                    continue
            else:
                # Old E1 format: ms is milliseconds since boot (relative time only)
                ms = row.get('ms', '')
                if not ms:
                    continue
                try:
                    sec = int(float(ms) // 1000)
                    hours = sec // 3600
                    minutes = (sec % 3600) // 60
                    secs = sec % 60
                    second = f"{date}T{hours:02d}:{minutes:02d}:{secs:02d}"
                except ValueError:
                    continue
        else:
            # S1 format: utc_time is ISO timestamp
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

        if is_e1_format:
            # E1 has heel and pitch directly, ax/ay/az for accel
            result.append({
                't': second + 'Z',
                'heel': round(avg('heel'), 1),
                'pitch': round(avg('pitch'), 1),
                'heading': 0,  # E1 doesn't have heading from IMU
                'accel_x': round(avg('ax'), 2),
                'accel_y': round(avg('ay'), 2),
                'accel_z': round(avg('az'), 2)
            })
        else:
            # S1 format with corrections for mounting orientation
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


def process_wind(csv_content: str, date: str = None) -> list:
    """Process wind data (already 1Hz, minimal transformation).

    Supports two CSV formats:
    - S1: utc_time,apparent_wind_speed_knots,apparent_wind_angle_deg,compass_heading_deg
    - E1 (new): ms,utc,aws_kts,aws_mps,awa_deg,battery
    - E1 (old): ms,aws_kts,aws_mps,awa_deg,battery

    Args:
        csv_content: CSV data as string
        date: Date string (YYYY-MM-DD) for E1 timestamp generation
    """
    from datetime import timedelta

    reader = csv.DictReader(StringIO(csv_content))
    rows = list(reader)
    if not rows:
        return []

    # Detect format based on column names
    first_row = rows[0]
    is_e1_format = 'ms' in first_row and 'aws_kts' in first_row
    has_utc = 'utc' in first_row  # New E1 format with GPS time

    # Time correction only for S1 (Pi5 clock was ~41 minutes ahead)
    TIME_CORRECTION_SECONDS = 0 if is_e1_format else -2460

    # Group by second for E1 (may have multiple samples)
    if is_e1_format:
        by_second = defaultdict(list)
        for row in rows:
            if has_utc:
                # New E1 format: utc is HHMMSS.mmm from GPS
                utc_raw = row.get('utc', '')
                if not utc_raw:
                    continue
                try:
                    utc_float = float(utc_raw)
                    hours = int(utc_float // 10000)
                    minutes = int((utc_float % 10000) // 100)
                    seconds = int(utc_float % 100)
                    second = f"{date}T{hours:02d}:{minutes:02d}:{seconds:02d}"
                except ValueError:
                    continue
            else:
                # Old E1 format: ms is milliseconds since boot
                ms = row.get('ms', '')
                if not ms:
                    continue
                try:
                    sec = int(float(ms) // 1000)
                    hours = sec // 3600
                    minutes = (sec % 3600) // 60
                    seconds = sec % 60
                    second = f"{date}T{hours:02d}:{minutes:02d}:{seconds:02d}"
                except ValueError:
                    continue
            by_second[second].append(row)

        result = []
        for second, samples in sorted(by_second.items()):
            # Take last sample per second (most recent)
            best = samples[-1]
            result.append({
                't': second + 'Z',
                'aws_kn': round(float(best.get('aws_kts', 0) or 0), 1),
                'awa': round(float(best.get('awa_deg', 0) or 0), 0),
                'heading': 0  # E1 wind sensor doesn't provide heading
            })
        return result
    else:
        # S1 format
        result = []
        for row in rows:
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
