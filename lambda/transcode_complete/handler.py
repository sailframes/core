"""
SailFrames Transcode Complete Lambda
Triggered by MediaConvert job completion events.
Merges HLS playlists from multiple video segments into a single playlist.
"""

import json
import os
import re
import boto3
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')

DATA_BUCKET = os.environ.get('DATA_BUCKET', 'sailframes-data-prod')
CLOUDFRONT_DOMAIN = os.environ.get('CLOUDFRONT_DOMAIN', 'sailframes.com')


def lambda_handler(event, context):
    """Handle MediaConvert job completion events."""
    logger.info(f"Received event: {json.dumps(event)}")

    # Parse CloudWatch event
    detail = event.get('detail', {})
    status = detail.get('status')
    user_metadata = detail.get('userMetadata', {})

    if status != 'COMPLETE':
        logger.warning(f"Job status is {status}, not processing")
        return {'statusCode': 200, 'body': f'Job status: {status}'}

    device_id = user_metadata.get('device_id')
    date = user_metadata.get('date')
    camera = user_metadata.get('camera')

    if not all([device_id, date, camera]):
        logger.error(f"Missing metadata: {user_metadata}")
        return {'statusCode': 400, 'body': 'Missing metadata'}

    logger.info(f"Processing completed transcode for {device_id}/{date}/{camera}")

    try:
        # Merge all HLS playlists for this camera
        merge_playlists(device_id, date, camera)

        # Update manifest
        update_manifest(device_id, date, camera)

        return {'statusCode': 200, 'body': 'OK'}
    except Exception as e:
        logger.error(f"Error processing transcode completion: {e}")
        raise


def merge_playlists(device_id: str, date: str, camera: str):
    """Merge individual segment playlists into a single master playlist."""
    prefix = f"hls/{device_id}/{date}/{camera}/"

    # Find all individual playlists
    playlists = []
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=DATA_BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            # Match playlists like: _20260324_170500.m3u8
            if key.endswith('.m3u8') and re.search(r'_\d{8}_\d{6}\.m3u8$', key):
                playlists.append(key)

    if not playlists:
        logger.warning(f"No playlists found in {prefix}")
        return

    # Sort by timestamp in filename
    playlists.sort()
    logger.info(f"Found {len(playlists)} playlists to merge")

    # Build merged playlist
    merged_lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:6",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD"
    ]

    segment_count = 0
    total_duration = 0.0

    for playlist_key in playlists:
        try:
            response = s3.get_object(Bucket=DATA_BUCKET, Key=playlist_key)
            content = response['Body'].read().decode('utf-8')

            for line in content.split('\n'):
                line = line.strip()

                # Skip header lines
                if line.startswith('#EXTM3U') or line.startswith('#EXT-X-VERSION'):
                    continue
                if line.startswith('#EXT-X-TARGETDURATION') or line.startswith('#EXT-X-MEDIA-SEQUENCE'):
                    continue
                if line.startswith('#EXT-X-PLAYLIST-TYPE'):
                    continue
                if line.startswith('#EXT-X-ENDLIST'):
                    continue
                if not line:
                    continue

                # Include segment info and segment files
                if line.startswith('#EXTINF:'):
                    merged_lines.append(line)
                    # Extract duration
                    dur_match = re.search(r'#EXTINF:([\d.]+)', line)
                    if dur_match:
                        total_duration += float(dur_match.group(1))
                elif line.endswith('.ts'):
                    merged_lines.append(line)
                    segment_count += 1
                elif line.startswith('#EXT-X-PROGRAM-DATE-TIME'):
                    merged_lines.append(line)

        except Exception as e:
            logger.error(f"Error reading playlist {playlist_key}: {e}")
            continue

    merged_lines.append("#EXT-X-ENDLIST")

    # Write merged playlist
    merged_key = f"{prefix}playlist.m3u8"
    merged_content = '\n'.join(merged_lines)

    s3.put_object(
        Bucket=DATA_BUCKET,
        Key=merged_key,
        Body=merged_content.encode('utf-8'),
        ContentType='application/x-mpegURL',
        CacheControl='max-age=31536000'
    )

    logger.info(f"Created merged playlist: {merged_key} ({segment_count} segments, {total_duration:.1f}s)")


def update_manifest(device_id: str, date: str, camera: str):
    """Update session manifest with video information."""
    prefix = f"hls/{device_id}/{date}/{camera}/"
    manifest_key = f"processed/{device_id}/{date}/manifest.json"

    # Get video time range from segment filenames
    start_time, end_time, duration = get_video_times(prefix)

    if not start_time:
        logger.warning("Could not determine video times")
        return

    # Load existing manifest
    try:
        response = s3.get_object(Bucket=DATA_BUCKET, Key=manifest_key)
        manifest = json.loads(response['Body'].read().decode('utf-8'))
    except Exception:
        manifest = {
            'device_id': device_id,
            'date': date,
            'sensors': {},
            'created_at': datetime.now(timezone.utc).isoformat()
        }

    # Add video info
    video_key = f"video_{camera}"
    manifest['sensors'][video_key] = {
        'start_time': start_time,
        'end_time': end_time,
        'duration_seconds': duration,
        'playlist_url': f"https://{CLOUDFRONT_DOMAIN}/hls/{device_id}/{date}/{camera}/playlist.m3u8"
    }

    manifest['has_video'] = True
    manifest['updated_at'] = datetime.now(timezone.utc).isoformat()

    # Save manifest
    s3.put_object(
        Bucket=DATA_BUCKET,
        Key=manifest_key,
        Body=json.dumps(manifest, indent=2),
        ContentType='application/json'
    )

    logger.info(f"Updated manifest: {manifest_key}")


def get_video_times(prefix: str) -> tuple:
    """Get video time range from HLS segments."""
    local_tz = ZoneInfo('America/New_York')

    segments = []
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=DATA_BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key.endswith('.ts'):
                # Extract timestamp from segment filename
                ts_match = re.search(r'(\d{8}_\d{6})', key)
                if ts_match:
                    ts_str = ts_match.group(1)
                    try:
                        # Parse as local time, convert to UTC
                        local_dt = datetime.strptime(ts_str, '%Y%m%d_%H%M%S')
                        local_dt = local_dt.replace(tzinfo=local_tz)
                        utc_dt = local_dt.astimezone(timezone.utc)
                        segments.append(utc_dt)
                    except ValueError:
                        pass

    if not segments:
        return None, None, 0

    segments.sort()

    # Calculate duration from playlist
    playlist_key = f"{prefix}playlist.m3u8"
    duration = 0.0
    try:
        response = s3.get_object(Bucket=DATA_BUCKET, Key=playlist_key)
        content = response['Body'].read().decode('utf-8')
        for line in content.split('\n'):
            if line.startswith('#EXTINF:'):
                dur_match = re.search(r'#EXTINF:([\d.]+)', line)
                if dur_match:
                    duration += float(dur_match.group(1))
    except Exception:
        pass

    start_time = segments[0].strftime('%Y-%m-%dT%H:%M:%SZ')
    end_time = segments[-1].strftime('%Y-%m-%dT%H:%M:%SZ')

    return start_time, end_time, round(duration, 1)
