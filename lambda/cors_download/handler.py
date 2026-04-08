"""
SailFrames CORS Data Download Lambda

Downloads RINEX observation and navigation data from NOAA CORS stations
for Post-Processed Kinematic (PPK) GNSS processing.

Triggered hourly by CloudWatch Events to check for sessions needing CORS data.

CORS Data Source: https://geodesy.noaa.gov/corsdata/
Station: MAMI (Massachusetts)

Data typically available ~1 hour after real-time.
"""

import json
import os
import boto3
import logging
import urllib.request
from datetime import datetime, timezone, timedelta
from io import BytesIO
import gzip

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
DATA_BUCKET = os.environ.get('DATA_BUCKET', 'sailframes-fleet-data-prod')

# CORS station configuration
CORS_STATION = os.environ.get('CORS_STATION', 'mami')
CORS_BASE_URL = 'https://geodesy.noaa.gov/corsdata/rinex'

# IGS broadcast navigation sources (for ephemeris data)
IGS_NAV_SOURCES = [
    # BKG (Germany) - no authentication required
    'https://igs.bkg.bund.de/root_ftp/IGS/BRDC/{year}/{doy:03d}/',
    # IGN (France) - backup source
    'https://igs.ign.fr/pub/igs/data/daily/{year}/{doy:03d}/{yy:02d}n/',
]

# Station coordinates for MAMI (approximate - will be in RINEX header)
STATION_COORDS = {
    'mami': {
        'name': 'Massachusetts Maritime Academy',
        'lat': 41.7417,
        'lon': -70.6167,
        'alt': 10.0
    }
}


def lambda_handler(event, context):
    """
    Main handler - checks for sessions needing CORS data and downloads it.

    Can be triggered by:
    1. CloudWatch scheduled event (hourly check)
    2. Direct invocation with specific session
    """
    try:
        # Check if specific session requested
        if event.get('session'):
            device_id = event['session'].get('device_id')
            session_folder = event['session'].get('folder')
            date = event['session'].get('date')
            return process_single_session(device_id, session_folder, date)

        # Otherwise, scan for all sessions needing CORS data
        sessions = find_sessions_needing_cors()

        results = []
        for session in sessions:
            try:
                result = process_single_session(
                    session['device_id'],
                    session['folder'],
                    session['date']
                )
                results.append(result)
            except Exception as e:
                logger.error(f"Error processing session {session}: {e}")
                results.append({
                    'session': session,
                    'status': 'error',
                    'error': str(e)
                })

        return {
            'statusCode': 200,
            'body': json.dumps({
                'processed': len(results),
                'results': results
            })
        }
    except Exception as e:
        logger.error(f"CORS download error: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }


def find_sessions_needing_cors():
    """Find sessions with RTCM3 data that haven't been PPK processed."""
    sessions = []

    # Scan processed manifests for sessions with ppk_status = awaiting_cors
    paginator = s3.get_paginator('list_objects_v2')

    for page in paginator.paginate(Bucket=DATA_BUCKET, Prefix='processed/'):
        for obj in page.get('Contents', []):
            if obj['Key'].endswith('/manifest.json'):
                try:
                    response = s3.get_object(Bucket=DATA_BUCKET, Key=obj['Key'])
                    manifest = json.loads(response['Body'].read().decode('utf-8'))

                    # Check if session needs CORS data
                    ppk_status = manifest.get('ppk_status', '')
                    if ppk_status in ['awaiting_cors', 'cors_downloading']:
                        # Extract session info from key
                        parts = obj['Key'].split('/')
                        device_id = parts[1]
                        folder = parts[2]
                        date = manifest.get('date', folder[:10])

                        sessions.append({
                            'device_id': device_id,
                            'folder': folder,
                            'date': date,
                            'start_time': manifest.get('start_time'),
                            'end_time': manifest.get('end_time')
                        })
                except Exception as e:
                    logger.warning(f"Error reading manifest {obj['Key']}: {e}")

    logger.info(f"Found {len(sessions)} sessions needing CORS data")
    return sessions


def process_single_session(device_id: str, folder: str, date: str):
    """Download CORS data for a single session."""
    logger.info(f"Processing CORS download for {device_id}/{folder}")

    # Parse date
    try:
        dt = datetime.strptime(date, '%Y-%m-%d')
    except ValueError:
        dt = datetime.strptime(date[:10], '%Y-%m-%d')

    year = dt.year
    doy = dt.timetuple().tm_yday  # Day of year

    # Check if CORS data already exists
    cors_key = f"cors/{CORS_STATION}/{year}/{doy:03d}/"
    existing = check_cors_exists(cors_key)

    if existing:
        logger.info(f"CORS data already exists at {cors_key}")
        # Update manifest and trigger PPK
        update_manifest_ppk_status(device_id, folder, 'cors_ready')
        return {
            'session': f"{device_id}/{folder}",
            'status': 'cors_ready',
            'cors_key': cors_key
        }

    # Download CORS data
    try:
        downloaded = download_cors_data(year, doy)

        if downloaded:
            update_manifest_ppk_status(device_id, folder, 'cors_ready')

            # Trigger PPK processing
            trigger_ppk_processing(device_id, folder, date)

            return {
                'session': f"{device_id}/{folder}",
                'status': 'cors_downloaded',
                'cors_key': cors_key,
                'files': downloaded
            }
        else:
            # CORS data not yet available (typically ~1 hour delay)
            update_manifest_ppk_status(device_id, folder, 'awaiting_cors')
            return {
                'session': f"{device_id}/{folder}",
                'status': 'cors_not_available',
                'message': 'CORS data not yet available, will retry'
            }
    except Exception as e:
        logger.error(f"Error downloading CORS data: {e}")
        update_manifest_ppk_status(device_id, folder, 'cors_error', str(e))
        raise


def check_cors_exists(prefix: str) -> bool:
    """Check if CORS data already exists in S3."""
    try:
        response = s3.list_objects_v2(
            Bucket=DATA_BUCKET,
            Prefix=prefix,
            MaxKeys=1
        )
        return response.get('KeyCount', 0) > 0
    except Exception:
        return False


def download_cors_data(year: int, doy: int) -> list:
    """
    Download RINEX observation and navigation data from NOAA CORS.

    File naming convention:
    - Observation: {station}{doy}0.{yy}o.gz (e.g., mami0910.24o.gz)
    - Navigation: {station}{doy}0.{yy}n.gz (e.g., mami0910.24n.gz)

    Returns list of downloaded files, or empty list if not available.
    """
    yy = year % 100
    station = CORS_STATION.lower()

    # CORS URL pattern
    base_url = f"{CORS_BASE_URL}/{year}/{doy:03d}/{station}/"

    downloaded = []

    # File patterns to download
    # NOAA CORS hourly files use letters a-x for hours 0-23
    files_to_download = [
        # Hourly observation files (a=hour 0, b=hour 1, ... x=hour 23)
        *[f"{station}{doy:03d}{chr(ord('a') + h)}.{yy:02d}o.gz" for h in range(24)],
        # Daily observation file (ends with '0')
        f"{station}{doy:03d}0.{yy:02d}o.gz",
        # Navigation files (CORS doesn't typically provide these)
        f"{station}{doy:03d}0.{yy:02d}n.gz",
        f"{station}{doy:03d}0.{yy:02d}g.gz",  # GLONASS nav
    ]

    # Try to download each file
    for filename in files_to_download:
        url = f"{base_url}{filename}"
        s3_key = f"cors/{station}/{year}/{doy:03d}/{filename}"

        try:
            logger.info(f"Attempting to download: {url}")

            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'SailFrames-PPK/1.0')

            with urllib.request.urlopen(req, timeout=30) as response:
                data = response.read()

                # Upload to S3
                s3.put_object(
                    Bucket=DATA_BUCKET,
                    Key=s3_key,
                    Body=data,
                    ContentType='application/gzip'
                )

                downloaded.append({
                    'file': filename,
                    's3_key': s3_key,
                    'size': len(data)
                })
                logger.info(f"Downloaded {filename} ({len(data)} bytes)")

        except urllib.error.HTTPError as e:
            if e.code == 404:
                logger.debug(f"File not found (may not be available yet): {filename}")
            else:
                logger.warning(f"HTTP error downloading {filename}: {e}")
        except Exception as e:
            logger.warning(f"Error downloading {filename}: {e}")

    # Also download IGS broadcast navigation (for GPS ephemeris)
    igs_nav = download_igs_navigation(year, doy)
    downloaded.extend(igs_nav)

    return downloaded


def download_igs_navigation(year: int, doy: int) -> list:
    """
    Download IGS broadcast navigation data (GPS ephemeris).

    IGS provides global broadcast ephemeris - essential for PPK processing
    when rover RTCM3 doesn't include GPS ephemeris (message 1019).

    File naming:
    - RINEX 2: brdc{doy}0.{yy}n.gz (GPS)
    - RINEX 3: BRDC00IGS_R_{year}{doy}0000_01D_MN.rnx.gz (mixed)
    """
    yy = year % 100
    downloaded = []
    station = CORS_STATION.lower()

    # Files to try (in order of preference)
    nav_files = [
        # RINEX 3 mixed navigation from WRD (World Reference Data, all constellations)
        (f"BRDC00WRD_R_{year}{doy:03d}0000_01D_MN.rnx.gz", 'rnx3_mixed'),
        # Alternate naming: IGS format
        (f"BRDC00IGS_R_{year}{doy:03d}0000_01D_MN.rnx.gz", 'rnx3_mixed'),
        # RINEX 2 GPS navigation
        (f"brdc{doy:03d}0.{yy:02d}n.gz", 'rnx2_gps'),
        # RINEX 2 GLONASS navigation
        (f"brdc{doy:03d}0.{yy:02d}g.gz", 'rnx2_glo'),
    ]

    for nav_file, nav_type in nav_files:
        # Already have this file?
        s3_key = f"cors/{station}/{year}/{doy:03d}/{nav_file}"
        try:
            s3.head_object(Bucket=DATA_BUCKET, Key=s3_key)
            logger.info(f"IGS nav file already exists: {s3_key}")
            downloaded.append({
                'file': nav_file,
                's3_key': s3_key,
                'size': 0,
                'type': nav_type
            })
            continue
        except Exception:
            pass  # File doesn't exist, try to download

        # Try each IGS source
        for source_template in IGS_NAV_SOURCES:
            source_url = source_template.format(year=year, doy=doy, yy=yy)
            url = f"{source_url}{nav_file}"

            try:
                logger.info(f"Attempting IGS download: {url}")

                req = urllib.request.Request(url)
                req.add_header('User-Agent', 'SailFrames-PPK/1.0')

                with urllib.request.urlopen(req, timeout=60) as response:
                    data = response.read()

                    # Upload to S3 (same prefix as CORS data)
                    s3.put_object(
                        Bucket=DATA_BUCKET,
                        Key=s3_key,
                        Body=data,
                        ContentType='application/gzip'
                    )

                    downloaded.append({
                        'file': nav_file,
                        's3_key': s3_key,
                        'size': len(data),
                        'type': nav_type
                    })
                    logger.info(f"Downloaded IGS {nav_type}: {nav_file} ({len(data)} bytes)")
                    break  # Success, move to next file

            except urllib.error.HTTPError as e:
                if e.code == 404:
                    logger.debug(f"IGS nav not found at {url}")
                else:
                    logger.warning(f"HTTP error for IGS nav {url}: {e}")
            except Exception as e:
                logger.warning(f"Error downloading IGS nav {url}: {e}")

    if not downloaded:
        logger.warning("No IGS broadcast navigation files downloaded")

    return downloaded


def update_manifest_ppk_status(device_id: str, folder: str, status: str, error: str = None):
    """Update session manifest with PPK processing status."""
    manifest_key = f"processed/{device_id}/{folder}/manifest.json"

    try:
        # Read existing manifest
        response = s3.get_object(Bucket=DATA_BUCKET, Key=manifest_key)
        manifest = json.loads(response['Body'].read().decode('utf-8'))
    except s3.exceptions.NoSuchKey:
        logger.warning(f"Manifest not found: {manifest_key}")
        return

    # Update PPK status
    manifest['ppk_status'] = status
    manifest['ppk_updated_at'] = datetime.now(timezone.utc).isoformat()

    if error:
        manifest['ppk_error'] = error
    elif 'ppk_error' in manifest:
        del manifest['ppk_error']

    # Write updated manifest
    s3.put_object(
        Bucket=DATA_BUCKET,
        Key=manifest_key,
        Body=json.dumps(manifest, indent=2),
        ContentType='application/json'
    )
    logger.info(f"Updated manifest PPK status: {manifest_key} -> {status}")


def trigger_ppk_processing(device_id: str, folder: str, date: str):
    """Trigger PPK processing Lambda."""
    lambda_client = boto3.client('lambda')

    try:
        lambda_client.invoke(
            FunctionName=os.environ.get('PPK_PROCESS_FUNCTION', 'sailframes-ppk-process'),
            InvocationType='Event',  # Async invocation
            Payload=json.dumps({
                'device_id': device_id,
                'folder': folder,
                'date': date,
                'cors_station': CORS_STATION
            })
        )
        logger.info(f"Triggered PPK processing for {device_id}/{folder}")
    except Exception as e:
        logger.error(f"Failed to trigger PPK processing: {e}")
