"""
SailFrames Data Processing Lambda
Triggered when CSV files are uploaded to raw/ prefix.
Downsamples sensor data and outputs JSON for web visualization.

Session Merging Logic:
- Sessions on the same day with GPS timestamps less than 10 minutes apart are merged
- This handles cases where E1 device creates multiple recording sessions during a single sailing day
"""

import json
import os
import boto3
import csv
from io import StringIO
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
DATA_BUCKET = os.environ.get('DATA_BUCKET', 'sailframes-fleet-data-prod')

# Maximum gap between sessions to consider them part of the same sailing day
SESSION_MERGE_GAP_MINUTES = 10


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


def find_session_actual_date(bucket: str, device_id: str, session_id: str, path_date: str) -> str:
    """Find the actual date for a session by looking for existing manifests.

    When processing non-GPS files, the GPS file might have already been processed
    with the correct date (from gps_date column). This function searches for
    existing manifests with the same session_id to find the correct date.

    Args:
        bucket: S3 bucket name
        device_id: Device ID (e.g., 'E1')
        session_id: Session ID (e.g., 's001-000061')
        path_date: Date from the upload path (may be incorrect)

    Returns:
        The actual date string (YYYY-MM-DD) if found, or path_date as fallback
    """
    # Search for manifests with this session_id
    # Pattern: processed/{device_id}/YYYY-MM-DD-{session_id}/manifest.json
    prefix = f"processed/{device_id}/"

    try:
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.endswith('/manifest.json') and session_id in key:
                    # Extract date from folder name
                    # Format: processed/E1/2026-04-04-s001-000061/manifest.json
                    parts = key.split('/')
                    if len(parts) >= 3:
                        folder = parts[2]  # e.g., "2026-04-04-s001-000061"
                        # Check if this folder contains our session_id
                        if folder.endswith(f"-{session_id}"):
                            # Extract date (first 10 chars: YYYY-MM-DD)
                            found_date = folder[:10]
                            if found_date != path_date:
                                logger.info(f"Found existing manifest at {key} with date {found_date}")
                                return found_date
    except Exception as e:
        logger.warning(f"Error searching for existing session: {e}")

    return path_date


def find_session_to_merge(bucket: str, device_id: str, date: str, new_start_time: str) -> str:
    """Find an existing session on the same day to merge with based on time gap.

    Sessions are merged if the gap between the end of an existing session
    and the start of the new data is less than SESSION_MERGE_GAP_MINUTES.

    Args:
        bucket: S3 bucket name
        device_id: Device ID (e.g., 'E1')
        date: Date string (YYYY-MM-DD)
        new_start_time: ISO timestamp of the first record in new data

    Returns:
        Folder name of the session to merge with, or None if no match found
    """
    if not new_start_time:
        return None

    try:
        new_start_dt = datetime.fromisoformat(new_start_time.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        logger.warning(f"Could not parse new_start_time: {new_start_time}")
        return None

    # List all sessions for this device on this date
    prefix = f"processed/{device_id}/{date}"
    candidates = []

    try:
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.endswith('/manifest.json'):
                    # Extract folder from key: processed/E1/2026-04-04-s001-000061/manifest.json
                    parts = key.split('/')
                    if len(parts) >= 3:
                        folder = parts[2]
                        # Only consider folders that start with this date
                        if folder.startswith(date):
                            candidates.append((folder, key))
    except Exception as e:
        logger.warning(f"Error listing sessions for merge check: {e}")
        return None

    # Check each candidate session for time proximity
    best_match = None
    smallest_gap = timedelta(minutes=SESSION_MERGE_GAP_MINUTES + 1)  # Start with gap larger than threshold

    for folder, manifest_key in candidates:
        try:
            response = s3.get_object(Bucket=bucket, Key=manifest_key)
            manifest = json.loads(response['Body'].read().decode('utf-8'))

            end_time_str = manifest.get('end_time')
            if not end_time_str:
                continue

            end_dt = datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))

            # Calculate gap: new session starts after existing session ends
            if new_start_dt >= end_dt:
                gap = new_start_dt - end_dt
            else:
                # New data starts before existing session ends - they overlap, definitely merge
                gap = timedelta(seconds=0)

            logger.info(f"Session {folder}: end={end_time_str}, new_start={new_start_time}, gap={gap}")

            if gap < timedelta(minutes=SESSION_MERGE_GAP_MINUTES):
                if gap < smallest_gap:
                    smallest_gap = gap
                    best_match = folder
                    logger.info(f"Found merge candidate: {folder} (gap: {gap})")

        except Exception as e:
            logger.warning(f"Error reading manifest {manifest_key}: {e}")
            continue

    if best_match:
        logger.info(f"Will merge new data into existing session: {best_match}")
    else:
        logger.info(f"No nearby session found for merging on {date}")

    return best_match


def find_merged_session_for_id(bucket: str, device_id: str, date: str, session_id: str) -> str:
    """Find if a session_id was merged into another session.

    When GPS data is merged, the manifest records which session_ids were combined.
    This function looks for a session that contains the given session_id.

    Args:
        bucket: S3 bucket name
        device_id: Device ID (e.g., 'E1')
        date: Date string (YYYY-MM-DD)
        session_id: Session ID to look for (e.g., 's001-000061')

    Returns:
        Folder name of the session containing this session_id, or None
    """
    prefix = f"processed/{device_id}/{date}"

    try:
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.endswith('/manifest.json'):
                    parts = key.split('/')
                    if len(parts) >= 3:
                        folder = parts[2]
                        # Skip if this is the exact folder for this session_id
                        if folder == f"{date}-{session_id}":
                            continue

                        try:
                            response = s3.get_object(Bucket=bucket, Key=key)
                            manifest = json.loads(response['Body'].read().decode('utf-8'))

                            # Check if this session contains data from our session_id
                            merged_sessions = manifest.get('merged_sessions', [])
                            if session_id in merged_sessions:
                                logger.info(f"Found session_id {session_id} merged into {folder}")
                                return folder
                        except Exception as e:
                            logger.warning(f"Error reading manifest {key}: {e}")
                            continue
    except Exception as e:
        logger.warning(f"Error searching for merged session: {e}")

    return None


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

    E1 filenames: E1_s001_000464_nav.csv, E1_boot17_122131_nav.csv

    The session ID includes both the boot/session identifier AND the numeric suffix
    to distinguish multiple recording sessions on the same boot.
    Returns session ID (e.g., 's001-000464', 'boot17-122131') or empty string if not found.
    """
    import re
    parts = filename.replace('.csv', '').replace('.rtcm3', '').split('_')

    session_part = None
    numeric_part = None

    for part in parts:
        # Match session patterns: s001, s002, boot17, etc.
        if re.match(r'^(s\d+|boot\d+)$', part):
            session_part = part
        # Match any 6-digit numeric part (recording ID or time)
        elif len(part) == 6 and part.isdigit():
            numeric_part = part

    if session_part and numeric_part:
        return f"{session_part}-{numeric_part}"
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
        elif '_raw.rtcm3' in filename or filename.endswith('.rtcm3'):
            sensor_type = 'rtcm3'
        else:
            logger.warning(f"Unknown E1 file type: {filename}")
            return
    else:
        logger.warning(f"Invalid path structure: {key}")
        return

    # Extract start time from filename for old E1 format fallback
    start_time = extract_start_time_from_filename(filename)
    logger.info(f"Extracted start time from filename: {start_time}")

    # Track actual GPS date (may differ from path date for E1)
    actual_date = date

    # For E1 non-GPS/non-RTCM3 files, check if GPS was already processed with a different date
    if session_id and sensor_type not in ('gps', 'rtcm3'):
        correct_date = find_session_actual_date(bucket, device_id, session_id, date)
        if correct_date and correct_date != date:
            logger.info(f"Found existing session with date {correct_date}, using instead of path date {date}")
            actual_date = correct_date

    # Handle RTCM3 files (raw GNSS data for PPK processing) - BEFORE downloading as text
    if sensor_type == 'rtcm3':
        # RTCM3 files are binary - just update manifest to track PPK status
        logger.info(f"RTCM3 file detected: {filename}")

        # Check if this session was merged into another
        merge_folder = None
        source_session_id = None
        if session_id:
            merge_folder = find_merged_session_for_id(bucket, device_id, actual_date, session_id)
            if merge_folder:
                output_folder = merge_folder
                source_session_id = session_id
                logger.info(f"RTCM3: Merging into existing session folder: {output_folder}")
            else:
                output_folder = f"{actual_date}-{session_id}"
        else:
            output_folder = actual_date

        update_manifest_rtcm3(bucket, device_id, output_folder, key, source_session_id)
        return

    # Download CSV (only for text-based sensor files)
    response = s3.get_object(Bucket=bucket, Key=key)
    csv_content = response['Body'].read().decode('utf-8')

    # Parse and downsample (pass date and start_time for E1 timestamp generation)
    if sensor_type == 'gps':
        # process_gps returns (data, actual_gps_date) tuple
        data, actual_date = process_gps(csv_content, date, start_time)
        if actual_date != date:
            logger.info(f"Using GPS date {actual_date} instead of path date {date}")
    elif sensor_type == 'imu':
        data = process_imu(csv_content, actual_date, start_time)
    elif sensor_type == 'pressure':
        data = process_pressure(csv_content)
    elif sensor_type == 'wind':
        data = process_wind(csv_content, actual_date, start_time)
    else:
        logger.warning(f"Unknown sensor type: {sensor_type}")
        return

    # Session merging: Check if this data should be merged with an existing session
    # Based on GPS UTC time gap (sessions < 10 min apart on same day get merged)
    merge_folder = None
    if sensor_type == 'gps' and data:
        # Get the start time of the new GPS data
        new_start_time = data[0].get('t') if data else None
        if new_start_time:
            merge_folder = find_session_to_merge(bucket, device_id, actual_date, new_start_time)

    # For non-GPS files, check if there's already a merged session for this session_id
    # by looking for manifests that might have absorbed this session
    if sensor_type != 'gps' and session_id:
        # Look for the session this data belongs to (may have been merged)
        original_folder = f"{actual_date}-{session_id}"
        # Check if this session was merged into another
        merge_folder = find_merged_session_for_id(bucket, device_id, actual_date, session_id)

    # Determine output folder: use merge target if found, otherwise create new
    if merge_folder:
        output_folder = merge_folder
        logger.info(f"Merging into existing session folder: {output_folder}")
    elif session_id:
        output_folder = f"{actual_date}-{session_id}"
    else:
        output_folder = actual_date
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

    # Update manifest with merged data (not just new data) to preserve correct bounds
    # If merging into another session, track the source session_id
    source_session_id = session_id if merge_folder and session_id else None
    update_manifest(bucket, device_id, output_folder, sensor_type, merged, source_session_id)


def extract_gps_date_from_csv(csv_content: str) -> str:
    """Extract the actual UTC date from GPS CSV data.

    E1 CSV has 'gps_date' column in DDMMYY format (e.g., "040426" for 2026-04-04).
    Falls back to None if not found.

    Returns: Date string in YYYY-MM-DD format, or None
    """
    reader = csv.DictReader(StringIO(csv_content))
    for row in reader:
        gps_date = row.get('gps_date', '')
        if gps_date and len(gps_date) == 6:
            try:
                # Parse DDMMYY format
                day = int(gps_date[:2])
                month = int(gps_date[2:4])
                year = 2000 + int(gps_date[4:6])  # Assumes 20xx
                if 1 <= day <= 31 and 1 <= month <= 12:
                    return f"{year}-{month:02d}-{day:02d}"
            except ValueError:
                pass
    return None


def process_gps(csv_content: str, date: str = None, start_time: str = None) -> tuple:
    """Downsample GPS from 10Hz to 1Hz, keeping max speed per second.

    Supports two CSV formats:
    - S1: utc_time,latitude,longitude,speed_knots,course_deg,fix_quality,satellites
    - E1: ms,utc,lat,lon,alt,sog,cog,sat,hdop,fix,gps_date

    Args:
        csv_content: CSV data as string
        date: Date string (YYYY-MM-DD) for E1 timestamp generation (fallback)
        start_time: Start time (HHMMSS) from filename for old E1 format

    Returns:
        Tuple of (data_list, actual_gps_date) where actual_gps_date may differ
        from the input date if GPS data contains a different date.
    """
    from datetime import timedelta

    reader = csv.DictReader(StringIO(csv_content))
    rows = list(reader)
    if not rows:
        return [], date

    # Detect format based on column names
    first_row = rows[0]
    is_e1_format = 'utc' in first_row and 'lat' in first_row

    # Extract actual GPS date from E1 data if available
    actual_date = date
    if is_e1_format:
        # Re-parse to extract date from gps_date column
        gps_date = extract_gps_date_from_csv(csv_content)
        if gps_date:
            if gps_date != date:
                logger.info(f"GPS date {gps_date} differs from path date {date}, using GPS date")
            actual_date = gps_date

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
                # Use actual GPS date (extracted from gps_date column)
                second = f"{actual_date}T{hours:02d}:{minutes:02d}:{seconds:02d}"
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

    return result, actual_date


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


def update_manifest(bucket: str, device_id: str, folder: str, sensor_type: str, data: list,
                    source_session_id: str = None):
    """Update or create session manifest with metadata.

    Args:
        bucket: S3 bucket name
        device_id: Device ID (e.g., 'E1')
        folder: Folder name - either date (YYYY-MM-DD) or date-session (YYYY-MM-DD-s001)
        sensor_type: Sensor type (gps, imu, wind, pressure)
        data: Processed data records
        source_session_id: Original session ID if data was merged from another session
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
            'merged_sessions': [],
            'created_at': datetime.now(timezone.utc).isoformat()
        }

    # Track merged session IDs (for non-GPS files to find their merged session)
    if source_session_id:
        merged_sessions = manifest.get('merged_sessions', [])
        if source_session_id not in merged_sessions:
            merged_sessions.append(source_session_id)
            manifest['merged_sessions'] = merged_sessions
            logger.info(f"Added {source_session_id} to merged_sessions for {folder}")

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


def update_manifest_rtcm3(bucket: str, device_id: str, folder: str, rtcm3_key: str,
                         source_session_id: str = None):
    """Update manifest when RTCM3 file is uploaded, setting PPK status.

    Args:
        bucket: S3 bucket name
        device_id: Device ID (e.g., 'E1')
        folder: Folder name (e.g., '2026-04-04-s001-000061')
        rtcm3_key: S3 key of the RTCM3 file
        source_session_id: Original session ID if data was merged from another session
    """
    manifest_key = f"processed/{device_id}/{folder}/manifest.json"

    # Parse folder name to extract date and session_id
    parts = folder.split('-')
    if len(parts) > 3:
        date = '-'.join(parts[:3])
        session_id = '-'.join(parts[3:])
    else:
        date = folder
        session_id = None

    # Try to load existing manifest or create new one
    try:
        response = s3.get_object(Bucket=bucket, Key=manifest_key)
        manifest = json.loads(response['Body'].read().decode('utf-8'))
    except s3.exceptions.NoSuchKey:
        manifest = {
            'device_id': device_id,
            'date': date,
            'session_id': session_id,
            'sensors': {},
            'merged_sessions': [],
            'created_at': datetime.now(timezone.utc).isoformat()
        }

    # Track merged session IDs
    if source_session_id:
        merged_sessions = manifest.get('merged_sessions', [])
        if source_session_id not in merged_sessions:
            merged_sessions.append(source_session_id)
            manifest['merged_sessions'] = merged_sessions
            logger.info(f"Added {source_session_id} to merged_sessions for {folder}")

    # Get RTCM3 file size
    try:
        response = s3.head_object(Bucket=bucket, Key=rtcm3_key)
        rtcm3_size = response['ContentLength']
    except Exception:
        rtcm3_size = 0

    # Update RTCM3 info in sensors
    manifest['sensors']['rtcm3'] = {
        's3_key': rtcm3_key,
        'size_bytes': rtcm3_size,
        'uploaded_at': datetime.now(timezone.utc).isoformat()
    }

    # Set PPK status to awaiting_cors if not already processed
    if manifest.get('ppk_status') not in ['completed', 'processing']:
        manifest['ppk_status'] = 'awaiting_cors'
        manifest['ppk_updated_at'] = datetime.now(timezone.utc).isoformat()
        logger.info(f"Set PPK status to awaiting_cors for {device_id}/{folder}")

    manifest['updated_at'] = datetime.now(timezone.utc).isoformat()

    # Save manifest
    s3.put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=json.dumps(manifest, indent=2),
        ContentType='application/json'
    )
    logger.info(f"Updated manifest with RTCM3: {manifest_key}")
