"""
SailFrames Video Linking Lambda
Extracts metadata from GoPro videos and links them to E1 sessions.

Metadata sources (in priority order):
1. S3 object metadata (gps-start-time) - extracted by upload script using exiftool
2. MP4 moov/mvhd atom creation_time - fallback if S3 metadata not present

GPS time from embedded GoPro telemetry is preferred because:
- Already in UTC (no timezone conversion needed)
- Millisecond precision
- Synced to atomic clocks (same as LG290P/ZED-F9P)
"""

import json
import os
import re
import struct
import boto3
import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote_plus
from io import BytesIO

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
DATA_BUCKET = os.environ.get('DATA_BUCKET', 'sailframes-fleet-data-prod')

# MP4 epoch starts at 1904-01-01
MP4_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)

# Fallback timezone offset for creation_time (EDT = UTC-4, EST = UTC-5)
# Only used if GPS time not available in S3 metadata
DEFAULT_TZ_OFFSET_HOURS = -4  # EDT


def lambda_handler(event, context):
    """Link videos to sessions.

    Modes:
    1. S3 trigger: Process uploaded videos, extract metadata, link to sessions
    2. Manual trigger with date: Process all videos for a date
    3. Manual with explicit metadata: Link with provided timestamps
    """
    # Manual invocation with explicit metadata (bypass extraction)
    if 'videos' in event and event.get('skip_extraction'):
        return handle_manual_link(event)

    # Manual trigger to process all videos for a date
    if 'date' in event and 'Records' not in event:
        tz_offset = event.get('tz_offset_hours', DEFAULT_TZ_OFFSET_HOURS)
        return process_date(event.get('date'), event.get('device_id', 'E1'), tz_offset)

    # S3 trigger mode
    for record in event.get('Records', []):
        bucket = record['s3']['bucket']['name']
        key = unquote_plus(record['s3']['object']['key'])

        # Process both MP4 and LRV uploads
        if key.lower().endswith('.mp4') or key.lower().endswith('.lrv'):
            logger.info(f"Processing video upload: {key}")
            try:
                process_video_upload(bucket, key)
            except Exception as e:
                logger.error(f"Failed to process {key}: {e}")
                raise

    return {'statusCode': 200, 'body': 'OK'}


def get_s3_metadata(bucket: str, key: str) -> dict:
    """Get S3 object metadata including custom GPS metadata.

    Returns dict with 'gps_start_time' (datetime) and 'duration_seconds' (float) if present.
    """
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        metadata = head.get('Metadata', {})

        result = {}

        # GPS start time from embedded telemetry (uploaded by exiftool extraction)
        gps_time = metadata.get('gps-start-time')
        if gps_time:
            try:
                # Parse ISO format: 2026-04-07T17:44:29.180Z
                if gps_time.endswith('Z'):
                    gps_time = gps_time[:-1] + '+00:00'
                result['gps_start_time'] = datetime.fromisoformat(gps_time)
                logger.info(f"Found GPS time in S3 metadata: {result['gps_start_time'].isoformat()}")
            except ValueError as e:
                logger.warning(f"Could not parse GPS time '{gps_time}': {e}")

        # Duration
        duration = metadata.get('duration-seconds')
        if duration:
            try:
                result['duration_seconds'] = float(duration)
            except ValueError:
                pass

        return result
    except Exception as e:
        logger.warning(f"Could not get S3 metadata for {key}: {e}")
        return {}


def process_date(date: str, device_id: str, tz_offset_hours: int = DEFAULT_TZ_OFFSET_HOURS):
    """Process all GoPro videos for a specific date.

    Args:
        date: Date string (YYYY-MM-DD)
        device_id: Device to link to (e.g., 'E1')
        tz_offset_hours: Fallback timezone offset if GPS time not available
    """
    prefix = f"raw/gopro/{date}/video/"
    logger.info(f"Processing videos for {date}")

    videos = []
    try:
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=DATA_BUCKET, Prefix=prefix):
            for obj in page.get('Contents', []):
                key = obj['Key']
                # Include both MP4 and LRV files
                if key.lower().endswith('.mp4') or key.lower().endswith('.lrv'):
                    videos.append({
                        's3_key': key,
                        'filename': key.split('/')[-1],
                        'size': obj['Size']
                    })
    except Exception as e:
        logger.error(f"Error listing videos: {e}")
        return {'statusCode': 500, 'body': str(e)}

    if not videos:
        return {'statusCode': 404, 'body': f'No videos found for {date}'}

    logger.info(f"Found {len(videos)} video files for {date}")

    # Prefer LRV files (smaller, same GPS metadata) but use MP4 if no LRV
    # Group by recording ID and file type
    lrv_videos = [v for v in videos if v['filename'].lower().endswith('.lrv')]
    mp4_videos = [v for v in videos if v['filename'].lower().endswith('.mp4')]

    # Use LRV if available, otherwise MP4
    videos_to_process = lrv_videos if lrv_videos else mp4_videos
    logger.info(f"Using {'LRV' if lrv_videos else 'MP4'} files for processing")

    # Group by recording ID (GoPro chapter groups)
    recordings = {}
    for video in videos_to_process:
        recording_id = get_gopro_recording_id(video['filename'])
        if recording_id:
            if recording_id not in recordings:
                recordings[recording_id] = []
            recordings[recording_id].append(video)

    # Sort chapters within each recording
    for recording_id in recordings:
        recordings[recording_id].sort(key=lambda x: get_gopro_chapter(x['filename']))

    # Get sessions for context
    sessions = get_sessions_for_date(device_id, date)
    session_info = [{'folder': s['folder'], 'start': s['start_time'], 'end': s['end_time']} for s in sessions]
    logger.info(f"Sessions for {date}: {json.dumps(session_info)}")

    # Extract metadata and link each recording
    results = []
    for recording_id, chapters in recordings.items():
        logger.info(f"Processing recording {recording_id} with {len(chapters)} chapters")

        # Extract metadata from first chapter (has recording start time)
        first_chapter = chapters[0]

        # Try S3 metadata first (GPS time from exiftool extraction)
        s3_meta = get_s3_metadata(DATA_BUCKET, first_chapter['s3_key'])
        gps_start_time = s3_meta.get('gps_start_time')

        if gps_start_time:
            # Use GPS time (already UTC, high precision)
            utc_creation_time = gps_start_time
            time_source = 'GPS'
            logger.info(f"Recording {recording_id}: GPS time={utc_creation_time.isoformat()}")
        else:
            # Fallback to MP4 creation_time with timezone conversion
            metadata = extract_mp4_metadata(DATA_BUCKET, first_chapter['s3_key'])
            if not metadata:
                logger.warning(f"Could not extract metadata from {first_chapter['s3_key']}")
                results.append({
                    'recording_id': recording_id,
                    'error': 'Could not extract metadata',
                    'chapters': len(chapters)
                })
                continue

            local_creation_time = metadata['creation_time']
            utc_creation_time = local_creation_time - timedelta(hours=tz_offset_hours)
            time_source = 'MP4'
            logger.info(f"Recording {recording_id}: MP4 time (local)={local_creation_time.isoformat()}, UTC={utc_creation_time.isoformat()} (fallback)")

        # Build video list with timestamps
        video_list = []
        chapter_start = utc_creation_time

        for chapter in chapters:
            # Try S3 metadata for duration first
            chapter_s3_meta = get_s3_metadata(DATA_BUCKET, chapter['s3_key'])
            duration = chapter_s3_meta.get('duration_seconds')

            if not duration:
                # Fallback to MP4 parsing
                chapter_meta = extract_mp4_metadata(DATA_BUCKET, chapter['s3_key'])
                duration = chapter_meta['duration_sec'] if chapter_meta else 600  # Default 10min

            chapter_end = chapter_start + timedelta(seconds=duration)

            video_list.append({
                's3_key': chapter['s3_key'],
                'filename': chapter['filename'],
                'start_time': chapter_start.isoformat(),
                'end_time': chapter_end.isoformat(),
                'duration_sec': duration,
                'chapter': get_gopro_chapter(chapter['filename'])
            })

            chapter_start = chapter_end  # Next chapter starts where this ends

        # Link to sessions
        linked = link_videos_to_sessions(DATA_BUCKET, device_id, date, video_list)
        results.append({
            'recording_id': recording_id,
            'chapters': len(chapters),
            'time_source': time_source,
            'start_time_utc': utc_creation_time.isoformat(),
            'total_duration_sec': sum(v['duration_sec'] for v in video_list),
            'linked_sessions': linked
        })

    return {
        'statusCode': 200,
        'body': json.dumps({
            'recordings': results,
            'sessions': session_info
        })
    }


def process_video_upload(bucket: str, key: str):
    """Process a single video upload."""
    parts = key.split('/')
    if len(parts) < 4:
        logger.warning(f"Invalid video path: {key}")
        return

    date = parts[2]
    process_date(date, 'E1', DEFAULT_TZ_OFFSET_HOURS)


def extract_mp4_metadata(bucket: str, key: str) -> dict:
    """Extract creation_time and duration from MP4 file.

    Reads the moov/mvhd atom which contains:
    - creation_time: seconds since 1904-01-01
    - duration: in timescale units
    - timescale: units per second
    """
    try:
        # First try reading from the beginning (fast-start MP4)
        metadata = try_extract_moov(bucket, key, from_start=True)
        if metadata:
            return metadata

        # If moov not at start, read from end (standard MP4)
        metadata = try_extract_moov(bucket, key, from_start=False)
        if metadata:
            return metadata

        logger.warning(f"Could not find moov atom in {key}")
        return None

    except Exception as e:
        logger.error(f"Error extracting metadata from {key}: {e}")
        return None


def try_extract_moov(bucket: str, key: str, from_start: bool = True) -> dict:
    """Try to extract moov atom from start or end of file."""

    # Get file size
    head = s3.head_object(Bucket=bucket, Key=key)
    file_size = head['ContentLength']

    if from_start:
        # Read first 50MB (moov is usually small but can be large)
        range_header = 'bytes=0-52428800'
    else:
        # Read last 50MB
        start = max(0, file_size - 52428800)
        range_header = f'bytes={start}-{file_size-1}'

    response = s3.get_object(Bucket=bucket, Key=key, Range=range_header)
    data = response['Body'].read()

    return parse_moov_from_data(data)


def parse_moov_from_data(data: bytes) -> dict:
    """Parse MP4 data to find moov/mvhd atom and extract metadata."""

    # Find moov atom
    moov_pos = find_atom(data, b'moov')
    if moov_pos is None:
        return None

    # Parse moov atom to find mvhd
    moov_size = struct.unpack('>I', data[moov_pos:moov_pos+4])[0]
    moov_data = data[moov_pos+8:moov_pos+moov_size]  # Skip size and type

    mvhd_pos = find_atom(moov_data, b'mvhd')
    if mvhd_pos is None:
        return None

    mvhd_size = struct.unpack('>I', moov_data[mvhd_pos:mvhd_pos+4])[0]
    mvhd_data = moov_data[mvhd_pos+8:mvhd_pos+mvhd_size]  # Skip size and type

    # Parse mvhd atom
    # Version 0: 4-byte fields, Version 1: 8-byte fields
    version = mvhd_data[0]

    if version == 0:
        creation_time = struct.unpack('>I', mvhd_data[4:8])[0]
        timescale = struct.unpack('>I', mvhd_data[12:16])[0]
        duration = struct.unpack('>I', mvhd_data[16:20])[0]
    else:  # version 1
        creation_time = struct.unpack('>Q', mvhd_data[4:12])[0]
        timescale = struct.unpack('>I', mvhd_data[20:24])[0]
        duration = struct.unpack('>Q', mvhd_data[24:32])[0]

    # Convert creation_time from MP4 epoch (1904) to datetime
    creation_dt = MP4_EPOCH + timedelta(seconds=creation_time)

    # Convert duration to seconds
    duration_sec = duration / timescale if timescale > 0 else 0

    logger.info(f"Extracted: creation_time={creation_dt.isoformat()}, duration={duration_sec:.1f}s")

    return {
        'creation_time': creation_dt,
        'duration_sec': round(duration_sec, 1),
        'timescale': timescale
    }


def find_atom(data: bytes, atom_type: bytes) -> int:
    """Find atom position in MP4 data."""
    pos = 0
    while pos < len(data) - 8:
        try:
            size = struct.unpack('>I', data[pos:pos+4])[0]
            atype = data[pos+4:pos+8]

            if size == 0:  # Atom extends to end of file
                break
            if size == 1:  # 64-bit size
                size = struct.unpack('>Q', data[pos+8:pos+16])[0]

            if atype == atom_type:
                return pos

            if size < 8:  # Invalid atom
                break

            pos += size
        except:
            break

    return None


def get_gopro_recording_id(filename: str) -> str:
    """Extract recording ID from GoPro filename (MP4 or LRV)."""
    match = re.search(r'(?:GOPR|GP\d{2})(\d{4})\.(?:MP4|LRV)', filename, re.IGNORECASE)
    return match.group(1) if match else None


def get_gopro_chapter(filename: str) -> int:
    """Get chapter number from GoPro filename."""
    if filename.upper().startswith('GOPR'):
        return 0
    match = re.match(r'GP(\d{2})', filename.upper())
    return int(match.group(1)) if match else 0


def get_sessions_for_date(device_id: str, date: str) -> list:
    """Get all sessions for a device and date."""
    sessions = []
    prefix = f"processed/{device_id}/"

    try:
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=DATA_BUCKET, Prefix=prefix):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.endswith('/manifest.json') and date in key:
                    try:
                        response = s3.get_object(Bucket=DATA_BUCKET, Key=key)
                        manifest = json.loads(response['Body'].read().decode('utf-8'))

                        if manifest.get('start_time') and manifest.get('end_time'):
                            folder = key.split('/')[2]
                            sessions.append({
                                'folder': folder,
                                'manifest_key': key,
                                'start_time': manifest['start_time'],
                                'end_time': manifest['end_time'],
                                'session_id': manifest.get('session_id')
                            })
                    except Exception as e:
                        logger.warning(f"Error reading manifest {key}: {e}")
    except Exception as e:
        logger.error(f"Error listing sessions: {e}")

    return sessions


def link_videos_to_sessions(bucket: str, device_id: str, date: str, videos: list) -> list:
    """Link videos to overlapping sessions."""
    sessions = get_sessions_for_date(device_id, date)

    if not sessions:
        logger.warning(f"No sessions found for {device_id}/{date}")
        return []

    logger.info(f"Found {len(sessions)} sessions for {device_id}/{date}")

    linked = []
    for session in sessions:
        session_start = datetime.fromisoformat(session['start_time'].replace('Z', '+00:00'))
        session_end = datetime.fromisoformat(session['end_time'].replace('Z', '+00:00'))

        matching_videos = []
        for video in videos:
            video_start = datetime.fromisoformat(video['start_time'].replace('Z', '+00:00'))
            video_end = datetime.fromisoformat(video['end_time'].replace('Z', '+00:00'))

            # Check for time overlap
            if video_start <= session_end and video_end >= session_start:
                # Calculate offset: positive = video starts before session
                offset_sec = (session_start - video_start).total_seconds()

                matching_videos.append({
                    's3_key': video['s3_key'],
                    'filename': video.get('filename', video['s3_key'].split('/')[-1]),
                    'start_time': video['start_time'],
                    'end_time': video['end_time'],
                    'duration_sec': video['duration_sec'],
                    'offset_sec': round(offset_sec, 1),
                    'chapter': video.get('chapter', 0)
                })

        if matching_videos:
            update_session_manifest_with_videos(bucket, device_id, session['folder'], matching_videos)
            linked.append({
                'session': session['folder'],
                'videos': len(matching_videos)
            })
            logger.info(f"Linked {len(matching_videos)} videos to {session['folder']}")

    return linked


def update_session_manifest_with_videos(bucket: str, device_id: str, folder: str, videos: list):
    """Update session manifest with video information."""
    manifest_key = f"processed/{device_id}/{folder}/manifest.json"

    try:
        response = s3.get_object(Bucket=bucket, Key=manifest_key)
        manifest = json.loads(response['Body'].read().decode('utf-8'))
    except s3.exceptions.NoSuchKey:
        logger.warning(f"Manifest not found: {manifest_key}")
        return

    manifest['videos'] = videos
    manifest['has_video'] = True
    manifest['updated_at'] = datetime.now(timezone.utc).isoformat()

    s3.put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=json.dumps(manifest, indent=2),
        ContentType='application/json'
    )


def handle_manual_link(event):
    """Handle manual linking with explicit metadata (no extraction)."""
    date = event.get('date')
    device_id = event.get('device_id', 'E1')
    videos = event.get('videos', [])

    if not date or not videos:
        return {'statusCode': 400, 'body': 'Missing date or videos'}

    # Convert video times to proper format
    video_list = []
    for video in videos:
        start = datetime.fromisoformat(video['start_time'].replace('Z', '+00:00'))
        duration = video.get('duration_sec', 0)
        end = start + timedelta(seconds=duration)

        video_list.append({
            's3_key': video['s3_key'],
            'filename': video['s3_key'].split('/')[-1],
            'start_time': start.isoformat(),
            'end_time': end.isoformat(),
            'duration_sec': duration,
            'chapter': video.get('chapter', 0)
        })

    linked = link_videos_to_sessions(DATA_BUCKET, device_id, date, video_list)

    return {
        'statusCode': 200,
        'body': json.dumps({'linked_sessions': linked})
    }
