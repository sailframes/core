"""
SailFrames PPK Processing Lambda

Performs Post-Processed Kinematic (PPK) GNSS processing using RTKLIB.
Combines rover observations (from E1 RTCM3 files) with base station
observations (from NOAA CORS) to achieve sub-meter positioning accuracy.

This Lambda runs as a container image with RTKLIB tools installed:
- convbin: Convert RTCM3 to RINEX
- rnx2rtkp: Post-process to generate PPK solution

Triggered by:
1. CORS download Lambda after base data is available
2. Manual invocation for reprocessing
"""

import json
import os
import boto3
import logging
import subprocess
import tempfile
import gzip
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
DATA_BUCKET = os.environ.get('DATA_BUCKET', 'sailframes-fleet-data-prod')

# RTKLIB binary paths (in container)
CONVBIN = os.environ.get('CONVBIN_PATH', '/opt/rtklib/bin/convbin')
RNX2RTKP = os.environ.get('RNX2RTKP_PATH', '/opt/rtklib/bin/rnx2rtkp')

# PPK configuration
PPK_CONFIG = {
    'pos_mode': 'kinematic',  # kinematic, static, ppp-kine, ppp-static
    'freq': 'l1+l2',  # l1, l1+l2, l1+l2+l5
    'solution_type': 'forward',  # forward, backward, combined
    'elevation_mask': 15,  # degrees
    'snr_mask': 35,  # dB-Hz
    'dynamics': 'on',
    'ambiguity_res': 'continuous',  # continuous, instantaneous, fix-and-hold
}


def lambda_handler(event, context):
    """
    Main PPK processing handler.

    Event format:
    {
        "device_id": "E1",
        "folder": "2026-04-04-s001-000061",
        "date": "2026-04-04",
        "cors_station": "mami"
    }
    """
    try:
        device_id = event['device_id']
        folder = event['folder']
        date = event['date']
        cors_station = event.get('cors_station', 'mami')

        logger.info(f"Starting PPK processing for {device_id}/{folder}")

        # Update status to processing
        update_manifest_ppk_status(device_id, folder, 'processing')

        # Get session info from manifest for CORS file selection and RTCM3 file
        session_hour = None
        rtcm3_s3_key = None
        try:
            manifest_key = f"processed/{device_id}/{folder}/manifest.json"
            response = s3.get_object(Bucket=DATA_BUCKET, Key=manifest_key)
            manifest = json.loads(response['Body'].read().decode('utf-8'))
            start_time = manifest.get('start_time', '')
            if start_time:
                # Parse ISO timestamp to get hour (e.g., "2026-04-07T14:18:29Z")
                session_hour = int(start_time[11:13])
                logger.info(f"Session start time: {start_time}, hour: {session_hour}")
            # Get RTCM3 file path from manifest
            if 'sensors' in manifest and 'rtcm3' in manifest['sensors']:
                rtcm3_s3_key = manifest['sensors']['rtcm3'].get('s3_key')
                logger.info(f"RTCM3 file from manifest: {rtcm3_s3_key}")
        except Exception as e:
            logger.warning(f"Could not get session info from manifest: {e}")

        # Create temp directory for processing
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Download rover RTCM3 file
            rtcm3_path = download_rover_data(device_id, folder, tmpdir, rtcm3_s3_key)

            if not rtcm3_path:
                raise ValueError("No RTCM3 file found for session")

            # Download CORS base station data (with session hour for correct file selection)
            base_files = download_cors_data(date, cors_station, tmpdir, session_hour)

            if not base_files.get('obs'):
                raise ValueError("CORS observation file not available")

            # Convert RTCM3 to RINEX
            rover_obs, rover_nav = convert_rtcm3_to_rinex(rtcm3_path, tmpdir)

            # Run PPK processing
            solution_file = run_ppk_processing(
                rover_obs=rover_obs,
                rover_nav=rover_nav,
                base_obs=base_files['obs'],
                base_nav=base_files.get('nav'),
                base_gnav=base_files.get('gnav'),
                output_dir=tmpdir
            )

            # Parse solution and upload results
            ppk_data = parse_ppk_solution(solution_file)

            # Upload PPK results to S3
            upload_ppk_results(device_id, folder, ppk_data, solution_file)

            # Calculate average accuracy from positions
            positions = ppk_data.get('positions', [])
            avg_sdn = sum(p.get('sdn', 0) for p in positions) / len(positions) if positions else 0
            avg_sde = sum(p.get('sde', 0) for p in positions) / len(positions) if positions else 0
            avg_sdu = sum(p.get('sdu', 0) for p in positions) / len(positions) if positions else 0

            # Update manifest with success and detailed stats
            update_manifest_ppk_status(
                device_id, folder, 'completed',
                stats={
                    'fix_rate': ppk_data.get('fix_rate', 0),
                    'points': len(positions),
                    'fix_count': ppk_data.get('fix_count', 0),
                    'float_count': ppk_data.get('float_count', 0),
                    'single_count': ppk_data.get('single_count', 0),
                    'avg_sdn': round(avg_sdn, 3),
                    'avg_sde': round(avg_sde, 3),
                    'avg_sdu': round(avg_sdu, 3),
                    'cors_station': cors_station,
                    'processed_at': datetime.now(timezone.utc).isoformat()
                }
            )

            return {
                'statusCode': 200,
                'body': json.dumps({
                    'status': 'completed',
                    'session': f"{device_id}/{folder}",
                    'fix_rate': ppk_data.get('fix_rate', 0),
                    'points': len(ppk_data.get('positions', []))
                })
            }

    except Exception as e:
        logger.error(f"PPK processing error: {e}", exc_info=True)

        # Update manifest with error
        if 'device_id' in event and 'folder' in event:
            update_manifest_ppk_status(
                event['device_id'],
                event['folder'],
                'failed',
                error=str(e)
            )

        return {
            'statusCode': 500,
            'body': json.dumps({
                'status': 'error',
                'error': str(e)
            })
        }


def download_rover_data(device_id: str, folder: str, tmpdir: Path, rtcm3_s3_key: str = None) -> Path:
    """Download RTCM3 rover data from S3.

    Args:
        device_id: Device identifier
        folder: Session folder name
        tmpdir: Temporary directory for downloads
        rtcm3_s3_key: Optional direct S3 key from manifest (preferred)
    """
    rtcm3_key = rtcm3_s3_key  # Use manifest key if provided

    # If no key from manifest, search for RTCM3 file
    if not rtcm3_key:
        date = folder[:10]
        prefix = f"raw/{device_id}/{date}/"

        logger.info(f"Searching for RTCM3 files in {prefix}")

        response = s3.list_objects_v2(
            Bucket=DATA_BUCKET,
            Prefix=prefix
        )

        # Find RTCM3 file matching session
        session_id = folder[11:] if len(folder) > 10 else None
        session_id_underscore = session_id.replace('-', '_') if session_id else None

        logger.info(f"Session ID: {session_id}, underscore version: {session_id_underscore}")
        logger.info(f"Found {len(response.get('Contents', []))} objects in {prefix}")

        for obj in response.get('Contents', []):
            key = obj['Key']
            logger.info(f"Checking file: {key}")
            if key.endswith('.rtcm3'):
                logger.info(f"Found RTCM3 file: {key}")
                # Check if session ID matches (try both formats)
                if session_id_underscore and session_id_underscore in key:
                    logger.info(f"Matched by underscore session ID")
                    rtcm3_key = key
                    break
                elif session_id and session_id in key:
                    logger.info(f"Matched by hyphen session ID")
                    rtcm3_key = key
                    break
                elif not session_id:
                    logger.info(f"No session ID filter, using first RTCM3")
                    rtcm3_key = key
                    break

    if not rtcm3_key:
        logger.warning(f"No RTCM3 file found for {device_id}/{folder}")
        return None

    # Download RTCM3 file
    local_path = tmpdir / 'rover.rtcm3'
    logger.info(f"Downloading {rtcm3_key}")

    s3.download_file(DATA_BUCKET, rtcm3_key, str(local_path))

    logger.info(f"Downloaded rover data: {local_path.stat().st_size} bytes")
    return local_path


def download_cors_data(date: str, station: str, tmpdir: Path, session_hour: int = None) -> dict:
    """Download CORS base station data from S3.

    Args:
        date: Date string (YYYY-MM-DD)
        station: CORS station ID (e.g., 'mami')
        tmpdir: Temporary directory for downloads
        session_hour: Hour of session start (0-23) to select correct hourly file

    Returns:
        Dict with 'obs', 'nav', 'gnav' paths
    """
    dt = datetime.strptime(date[:10], '%Y-%m-%d')
    year = dt.year
    doy = dt.timetuple().tm_yday

    cors_prefix = f"cors/{station}/{year}/{doy:03d}/"

    logger.info(f"Downloading CORS data from {cors_prefix}, session_hour={session_hour}")

    files = {}

    response = s3.list_objects_v2(
        Bucket=DATA_BUCKET,
        Prefix=cors_prefix
    )

    # NOAA CORS hourly files: a=hour 0, b=hour 1, ... o=hour 14, ... x=hour 23
    # Each letter represents one hour of data
    # Full day file ends with '0' (e.g., mami0970.26o.gz)
    hourly_letter = None
    if session_hour is not None:
        # Convert hour 0-23 to letter a-x
        hourly_letter = chr(ord('a') + session_hour)
        logger.info(f"Session hour {session_hour} maps to hourly file letter '{hourly_letter}'")

    # Collect all available observation files
    obs_files = []
    for obj in response.get('Contents', []):
        key = obj['Key']
        filename = key.split('/')[-1]

        if filename.endswith('.gz') and 'o' in filename[-6:-3]:
            obs_files.append((key, filename))

    logger.info(f"Found {len(obs_files)} observation files: {[f[1] for f in obs_files]}")

    # Select best observation file:
    # 1. Prefer full-day file (ends with '0' before extension)
    # 2. Otherwise use the correct hourly file based on session time
    selected_obs = None
    for key, filename in obs_files:
        # Check for full-day file (e.g., mami0970.26o.gz - has '0' before the extension)
        base = filename.replace('.gz', '')  # mami0970.26o
        if len(base) >= 5 and base[-5] == '0':
            selected_obs = (key, filename)
            logger.info(f"Found full-day file: {filename}")
            break

        # Check for matching hourly file
        if hourly_letter and len(base) >= 5 and base[-5] == hourly_letter:
            selected_obs = (key, filename)
            logger.info(f"Found matching hourly file: {filename}")

    # Fallback: use first available if no match
    if not selected_obs and obs_files:
        selected_obs = obs_files[0]
        logger.warning(f"No matching CORS file, using fallback: {selected_obs[1]}")

    # Download selected observation file
    if selected_obs:
        key, filename = selected_obs
        local_gz = tmpdir / filename
        s3.download_file(DATA_BUCKET, key, str(local_gz))

        local_file = tmpdir / filename[:-3]
        with gzip.open(local_gz, 'rb') as f_in:
            with open(local_file, 'wb') as f_out:
                f_out.write(f_in.read())

        files['obs'] = local_file
        logger.info(f"Using observation file: {local_file}")

    # Download navigation files (from CORS or IGS broadcast)
    nav_files_downloaded = []
    for key, filename in [(obj['Key'], obj['Key'].split('/')[-1])
                          for obj in response.get('Contents', [])]:
        if not filename.endswith('.gz'):
            continue

        # IGS RINEX 3 mixed navigation (preferred - has all constellations)
        if filename.startswith('BRDC') and filename.endswith('.rnx.gz'):
            local_gz = tmpdir / filename
            s3.download_file(DATA_BUCKET, key, str(local_gz))
            local_file = tmpdir / filename[:-3]
            with gzip.open(local_gz, 'rb') as f_in:
                with open(local_file, 'wb') as f_out:
                    f_out.write(f_in.read())
            files['nav'] = local_file
            nav_files_downloaded.append(filename)
            logger.info(f"Using IGS mixed navigation file: {local_file}")
            break  # Prefer RINEX 3 mixed nav

    # If no RINEX 3 mixed nav, look for RINEX 2 files
    if 'nav' not in files:
        for key, filename in [(obj['Key'], obj['Key'].split('/')[-1])
                              for obj in response.get('Contents', [])]:
            if not filename.endswith('.gz'):
                continue

            base = filename.replace('.gz', '')

            # GPS Navigation file (brdc*.n or station*.n)
            if ('n' in filename[-6:-3] or filename.endswith('n.gz')) and 'nav' not in files:
                local_gz = tmpdir / filename
                s3.download_file(DATA_BUCKET, key, str(local_gz))
                local_file = tmpdir / filename[:-3]
                with gzip.open(local_gz, 'rb') as f_in:
                    with open(local_file, 'wb') as f_out:
                        f_out.write(f_in.read())
                files['nav'] = local_file
                nav_files_downloaded.append(filename)
                logger.info(f"Using GPS navigation file: {local_file}")

            # GLONASS nav
            elif ('g' in filename[-6:-3] or filename.endswith('g.gz')) and 'gnav' not in files:
                local_gz = tmpdir / filename
                s3.download_file(DATA_BUCKET, key, str(local_gz))
                local_file = tmpdir / filename[:-3]
                with gzip.open(local_gz, 'rb') as f_in:
                    with open(local_file, 'wb') as f_out:
                        f_out.write(f_in.read())
                files['gnav'] = local_file
                nav_files_downloaded.append(filename)
                logger.info(f"Using GLONASS navigation file: {local_file}")

    logger.info(f"Navigation files downloaded: {nav_files_downloaded}")
    if 'nav' not in files:
        logger.warning("No GPS navigation file found - PPK will likely fail!")

    return files


def convert_rtcm3_to_rinex(rtcm3_path: Path, tmpdir: Path) -> tuple:
    """Convert RTCM3 to RINEX using convbin."""
    obs_path = tmpdir / 'rover.obs'
    nav_path = tmpdir / 'rover.nav'

    cmd = [
        CONVBIN,
        '-r', 'rtcm3',  # Input format
        '-o', str(obs_path),  # Output observation file
        '-n', str(nav_path),  # Output navigation file
        '-d', str(tmpdir),  # Output directory
        '-od',  # Include doppler in output
        '-os',  # Include SNR in output
        '-f', '5',  # Number of frequencies (L1-L5)
        '-v', '2.11',  # RINEX 2.11 to match CORS files
        str(rtcm3_path)
    ]

    logger.info(f"Running convbin: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:
            logger.error(f"convbin stderr: {result.stderr}")
            raise RuntimeError(f"convbin failed: {result.stderr}")

        logger.info(f"convbin stdout: {result.stdout}")
        if result.stderr:
            logger.info(f"convbin stderr: {result.stderr}")

        # Check output files exist and have content
        if not obs_path.exists():
            raise RuntimeError("convbin did not produce observation file")

        obs_size = obs_path.stat().st_size
        nav_size = nav_path.stat().st_size if nav_path.exists() else 0
        logger.info(f"convbin output sizes: obs={obs_size} bytes, nav={nav_size} bytes")

        # Always log observation file header for debugging
        with open(obs_path, 'r') as f:
            header_lines = []
            for line in f:
                header_lines.append(line.rstrip())
                if 'END OF HEADER' in line:
                    break
            header = '\n'.join(header_lines)
        logger.info(f"Rover observation file header:\n{header}")

        if obs_size < 1000:
            logger.warning(f"Observation file very small ({obs_size} bytes)")

        return obs_path, nav_path if nav_path.exists() else None

    except subprocess.TimeoutExpired:
        raise RuntimeError("convbin timed out")


def run_ppk_processing(rover_obs: Path, rover_nav: Path,
                       base_obs: Path, base_nav: Path,
                       output_dir: Path, base_gnav: Path = None) -> Path:
    """Run PPK processing using rnx2rtkp."""
    solution_path = output_dir / 'solution.pos'

    # Build rnx2rtkp command
    cmd = [
        RNX2RTKP,
        '-k', '/opt/rtklib/ppk.conf',  # Configuration file
        '-p', '2',  # Positioning mode: 2=kinematic
        '-m', str(PPK_CONFIG['elevation_mask']),
        '-o', str(solution_path),
    ]

    # Add input files
    cmd.append(str(rover_obs))

    if base_obs:
        cmd.append(str(base_obs))

    # Add navigation files (GPS ephemeris is critical!)
    nav_files_added = []
    if base_nav and base_nav.exists():
        # Prefer base (IGS) navigation - has complete GPS ephemeris
        cmd.append(str(base_nav))
        nav_files_added.append(f"base_nav: {base_nav.name}")
    if rover_nav and rover_nav.exists() and rover_nav.stat().st_size > 100:
        # Add rover nav only if it has content (may have partial ephemeris)
        cmd.append(str(rover_nav))
        nav_files_added.append(f"rover_nav: {rover_nav.name}")
    if base_gnav and base_gnav.exists():
        # Add GLONASS navigation if available
        cmd.append(str(base_gnav))
        nav_files_added.append(f"base_gnav: {base_gnav.name}")

    logger.info(f"Navigation files for PPK: {nav_files_added}")

    logger.info(f"Running rnx2rtkp: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600
        )

        logger.info(f"rnx2rtkp stdout: {result.stdout}")
        if result.stderr:
            logger.info(f"rnx2rtkp stderr: {result.stderr}")

        if result.returncode != 0:
            logger.error(f"rnx2rtkp failed with code {result.returncode}")
            # rnx2rtkp may return non-zero but still produce output
            if not solution_path.exists():
                raise RuntimeError(f"rnx2rtkp failed: {result.stderr}")

        return solution_path

    except subprocess.TimeoutExpired:
        raise RuntimeError("rnx2rtkp timed out")


def parse_ppk_solution(solution_file: Path) -> dict:
    """Parse RTKLIB solution file (.pos) to JSON format."""
    positions = []
    fix_count = 0
    float_count = 0
    single_count = 0

    with open(solution_file, 'r') as f:
        for line in f:
            line = line.strip()

            # Skip header lines
            if line.startswith('%') or not line:
                continue

            parts = line.split()
            if len(parts) < 8:
                continue

            try:
                # RTKLIB .pos format:
                # Date Time Lat Lon Height Q ns sdn sde sdu
                date = parts[0]
                time = parts[1]
                lat = float(parts[2])
                lon = float(parts[3])
                height = float(parts[4])
                quality = int(parts[5])  # 1=fix, 2=float, 5=single
                num_sats = int(parts[6])

                # Parse standard deviations if available
                sdn = float(parts[7]) if len(parts) > 7 else 0
                sde = float(parts[8]) if len(parts) > 8 else 0
                sdu = float(parts[9]) if len(parts) > 9 else 0

                # Count solution types
                if quality == 1:
                    fix_count += 1
                elif quality == 2:
                    float_count += 1
                else:
                    single_count += 1

                # Build ISO timestamp
                timestamp = f"{date}T{time}Z"

                positions.append({
                    't': timestamp,
                    'lat': lat,
                    'lon': lon,
                    'alt': height,
                    'quality': quality,  # 1=fix, 2=float, 5=single
                    'sats': num_sats,
                    'sdn': sdn,  # North std dev (m)
                    'sde': sde,  # East std dev (m)
                    'sdu': sdu,  # Up std dev (m)
                })

            except (ValueError, IndexError) as e:
                logger.warning(f"Error parsing line: {line}: {e}")

    total = len(positions)
    fix_rate = (fix_count / total * 100) if total > 0 else 0

    logger.info(f"Parsed {total} positions: {fix_count} fix, {float_count} float, {single_count} single")
    logger.info(f"Fix rate: {fix_rate:.1f}%")

    return {
        'positions': positions,
        'fix_rate': round(fix_rate, 1),
        'fix_count': fix_count,
        'float_count': float_count,
        'single_count': single_count,
        'total': total
    }


def upload_ppk_results(device_id: str, folder: str, ppk_data: dict, solution_file: Path):
    """Upload PPK results to S3."""
    # Upload JSON positions
    ppk_json_key = f"processed/{device_id}/{folder}/ppk_gps.json"

    s3.put_object(
        Bucket=DATA_BUCKET,
        Key=ppk_json_key,
        Body=json.dumps(ppk_data['positions'], indent=2),
        ContentType='application/json'
    )
    logger.info(f"Uploaded PPK positions: {ppk_json_key}")

    # Upload raw solution file
    solution_key = f"processed/{device_id}/{folder}/ppk_solution.pos"

    s3.upload_file(
        str(solution_file),
        DATA_BUCKET,
        solution_key
    )
    logger.info(f"Uploaded solution file: {solution_key}")


def update_manifest_ppk_status(device_id: str, folder: str, status: str,
                                error: str = None, stats: dict = None):
    """Update session manifest with PPK status."""
    manifest_key = f"processed/{device_id}/{folder}/manifest.json"

    try:
        response = s3.get_object(Bucket=DATA_BUCKET, Key=manifest_key)
        manifest = json.loads(response['Body'].read().decode('utf-8'))
    except s3.exceptions.NoSuchKey:
        logger.warning(f"Manifest not found: {manifest_key}")
        return

    manifest['ppk_status'] = status
    manifest['ppk_updated_at'] = datetime.now(timezone.utc).isoformat()

    if error:
        manifest['ppk_error'] = error
    elif 'ppk_error' in manifest:
        del manifest['ppk_error']

    if stats:
        manifest['ppk_stats'] = stats

    s3.put_object(
        Bucket=DATA_BUCKET,
        Key=manifest_key,
        Body=json.dumps(manifest, indent=2),
        ContentType='application/json'
    )
    logger.info(f"Updated manifest PPK status: {status}")
