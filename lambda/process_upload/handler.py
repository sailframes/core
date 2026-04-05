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


def extract_start_time_from_filename(filename: str) -> str:
    """Extract start time (HHMMSS) from E1 filename.

    E1 filenames: E1_boot24_163325_nav.csv or E1_20260402_163325_nav.csv
    Returns HHMMSS string (e.g., '163325') or empty string if not found.
    """
    import re
    # Match 6 digits that look like a time (HHMMSS)
    # In E1_boot24_163325_nav.csv, the time is the third part
    parts = filename.replace('.csv', '').split('_')
    for part in parts:
        if len(part) == 6 and part.isdigit():
            # Validate it looks like a time (HH < 24, MM < 60, SS < 60)
            hh, mm, ss = int(part[:2]), int(part[2:4]), int(part[4:6])
            if hh < 24 and mm < 60 and ss < 60:
                return part
    return ''


def extract_session_id_from_filename(filename: str) -> str:
    """Extract session ID from E1 filename.

    E1 filenames: E1_s001_000061_nav.csv, E1_boot17_122131_nav.csv

    The session ID includes both the boot/session identifier AND the start time
    to distinguish multiple recording sessions on the same boot.
    Returns session ID (e.g., 's001-000061', 'boot17-122131') or empty string if not found.
    """
    import re
    parts = filename.replace('.csv', '').replace('.rtcm3', '').split('_')

    session_part = None
    time_part = None

    for part in parts:
        # Match session patterns: s001, s002, boot17, etc.
        if re.match(r'^(s\d+|boot\d+)$', part):
            session_part = part
        # Match time patterns: 6 digits like 122131 (HHMMSS)
        if len(part) == 6 and part.isdigit():
            hh, mm, ss = int(part[:2]), int(part[2:4]), int(part[4:6])
            if hh < 24 and mm < 60 and ss < 60:
                time_part = part

    if session_part and time_part:
        return f"{session_part}-{time_part}"
    elif session_part:
        return session_part
    return ''


def process_file(bucket: str, key: str):
    """Process a single CSV file.

    Supports two path structures:
    - S1 format: raw/{device_id}/{date}/{sensor_type}/{filename}.csv (5+ parts)
    - E1 format: raw/{device_id}/{date}/{filename}.csv (4 parts, sensor in filename)

    For E1 files, extracts session ID (s001, boot17, etc.) from filename and
    creates separate processed folders per session.
    """
    parts = key.split('/')

    session_id = None  # Only used for E1 files

    if len(parts) >= 5:
        # S1 format: raw/{device}/{date}/{sensor}/{file}.csv
        device_id = parts[1]
        date = parts[2]
        sensor_type = parts[3]
        filename = parts[4]
    elif len(parts) == 4:
        # E1 format: raw/{device}/{date}/{file}.csv
        device_id = parts[1]
        date = parts[2]
        filename = parts[3]
        # Extract session ID for E1 files
        session_id = extract_session_id_from_filename(filename)
        logger.info(f"Extracted session ID from filename: {session_id}")
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

    # Extract start time from filename for old E1 format fallback
    start_time = extract_start_time_from_filename(filename)
    logger.info(f"Extracted start time from filename: {start_time}")

    # Download CSV
    response = s3.get_object(Bucket=bucket, Key=key)
    csv_content = response['Body'].read().decode('utf-8')

    # Parse and downsample (pass date and start_time for E1 timestamp generation)
    if sensor_type == 'gps':
        data = process_gps(csv_content, date, start_time)
    elif sensor_type == 'imu':
        data = process_imu(csv_content, date, start_time)
    elif sensor_type == 'pressure':
        data = process_pressure(csv_content)
    elif sensor_type == 'wind':
        data = process_wind(csv_content, date, start_time)
    else:
        logger.warning(f"Unknown sensor type: {sensor_type}")
        return

    # Merge with existing processed JSON (don't overwrite)
    # For E1 files with session ID, create separate folder per session
    if session_id:
        output_folder = f"{date}-{session_id}"
    else:
        output_folder = date
    output_key = f"processed/{device_id}/{output_folder}/{sensor_type}.json"

    # Try to load existing data
    existing_data = []
    try:
        response = s3.get_object(Bucket=bucket, Key=output_key)
        existing_data = json.loads(response['Body'].read().decode('utf-8'))
        logger.info(f"Loaded {len(existing_data)} existing records from {output_key}")
    except s3.exceptions.NoSuchKey:
        pass
    except Exception as e:
        logger.warning(f"Could not load existing data: {e}")

    # Merge: combine existing + new, dedupe by timestamp, sort
    all_data = existing_data + data
    seen = set()
    merged = []
    for item in all_data:
        t = item.get('t', '')
        if t and t not in seen:
            seen.add(t)
            merged.append(item)
    merged.sort(key=lambda x: x.get('t', ''))

    logger.info(f"Merged: {len(existing_data)} existing + {len(data)} new = {len(merged)} total")

    # Upload merged JSON
    s3.put_object(
        Bucket=bucket,
        Key=output_key,
        Body=json.dumps(merged, default=str),
        ContentType='application/json'
    )
    logger.info(f"Wrote {len(merged)} records to {output_key}")

    # Update manifest
    update_manifest(bucket, device_id, output_folder, sensor_type, data)


def process_gps(csv_content: str, date: str = None, start_time: str = None) -> list:
    """Downsample GPS from 10Hz to 1Hz, keeping max speed per second.

    Supports two CSV formats:
    - S1: utc_time,latitude,longitude,speed_knots,course_deg,fix_quality,satellites
    - E1: ms,utc,lat,lon,alt,sog,cog,sat,hdop,fix

    Args:
        csv_content: CSV data as string
        date: Date string (YYYY-MM-DD) for E1 timestamp generation
        start_time: Start time (HHMMSS) from filename for old E1 format
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


def process_imu(csv_content: str, date: str = None, start_time: str = None) -> list:
    """Downsample IMU from 50Hz to 1Hz, averaging values.

    Supports two CSV formats:
    - S1: utc_time,heel_deg,pitch_deg,heading_deg,accel_x_mps2,accel_y_mps2,accel_z_mps2
    - E1 (new): ms,utc,ax,ay,az,gx,gy,gz,heel,pitch
    - E1 (old): ms,ax,ay,az,gx,gy,gz,heel,pitch

    Args:
        csv_content: CSV data as string
        date: Date string (YYYY-MM-DD) for E1 timestamp generation
        start_time: Start time (HHMMSS) from filename for old E1 format
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

    # Parse start_time for old E1 format
    base_seconds = 0
    if start_time and len(start_time) == 6:
        base_seconds = int(start_time[:2]) * 3600 + int(start_time[2:4]) * 60 + int(start_time[4:6])

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
                # Old E1 format: ms is milliseconds since boot, add to start_time from filename
                ms = row.get('ms', '')
                if not ms:
                    continue
                try:
                    sec_offset = int(float(ms) // 1000)
                    total_sec = base_seconds + sec_offset
                    hours = (total_sec // 3600) % 24
                    minutes = (total_sec % 3600) // 60
                    secs = total_sec % 60
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


def process_wind(csv_content: str, date: str = None, start_time: str = None) -> list:
    """Process wind data (already 1Hz, minimal transformation).

    Supports two CSV formats:
    - S1: utc_time,apparent_wind_speed_knots,apparent_wind_angle_deg,compass_heading_deg
    - E1 (new): ms,utc,aws_kts,aws_mps,awa_deg,battery
    - E1 (old): ms,aws_kts,aws_mps,awa_deg,battery

    Args:
        csv_content: CSV data as string
        date: Date string (YYYY-MM-DD) for E1 timestamp generation
        start_time: Start time (HHMMSS) from filename for old E1 format
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

    # Parse start_time for old E1 format
    base_seconds = 0
    if start_time and len(start_time) == 6:
        base_seconds = int(start_time[:2]) * 3600 + int(start_time[2:4]) * 60 + int(start_time[4:6])

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
                # Old E1 format: ms is milliseconds since boot, add to start_time from filename
                ms = row.get('ms', '')
                if not ms:
                    continue
                try:
                    sec_offset = int(float(ms) // 1000)
                    total_sec = base_seconds + sec_offset
                    hours = (total_sec // 3600) % 24
                    minutes = (total_sec % 3600) // 60
                    seconds = total_sec % 60
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


def update_manifest(bucket: str, device_id: str, folder: str, sensor_type: str, data: list):
    """Update or create session manifest with metadata.

    Args:
        bucket: S3 bucket name
        device_id: Device ID (e.g., 'E1')
        folder: Folder name - either date (YYYY-MM-DD) or date-session (YYYY-MM-DD-s001)
        sensor_type: Sensor type (gps, imu, wind, pressure)
        data: Processed data records
    """
    manifest_key = f"processed/{device_id}/{folder}/manifest.json"

    # Parse folder name to extract date and optional session_id
    # Format: YYYY-MM-DD or YYYY-MM-DD-s001
    parts = folder.split('-')
    if len(parts) > 3:
        # Has session ID: YYYY-MM-DD-s001
        date = '-'.join(parts[:3])
        session_id = '-'.join(parts[3:])
    else:
        date = folder
        session_id = None

    # Try to load existing manifest
    try:
        response = s3.get_object(Bucket=bucket, Key=manifest_key)
        manifest = json.loads(response['Body'].read().decode('utf-8'))
    except s3.exceptions.NoSuchKey:
        manifest = {
            'device_id': device_id,
            'date': date,
            'session_id': session_id,
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
