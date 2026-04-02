"""
SailFrames API - Get Video Manifest
Returns HLS playlist URLs for session video streams.
"""

import json
import os
import boto3
import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
DATA_BUCKET = os.environ.get('DATA_BUCKET', 'sailframes-fleet-data-prod')
CLOUDFRONT_DOMAIN = os.environ.get('CLOUDFRONT_DOMAIN', 'sailframes.com')


def lambda_handler(event, context):
    """Return video stream information for a session."""
    try:
        path_params = event.get('pathParameters', {})
        device_id = path_params.get('device_id')
        date = path_params.get('date')

        if not device_id or not date:
            return error_response(400, 'Missing device_id or date')

        streams = get_video_streams(device_id, date)

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'streams': streams})
        }
    except Exception as e:
        logger.error(f"Error getting video info: {e}")
        return error_response(500, str(e))


def error_response(status: int, message: str) -> dict:
    return {
        'statusCode': status,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps({'error': message})
    }


def get_video_streams(device_id: str, date: str) -> dict:
    """Get HLS stream info for all cameras."""
    streams = {}

    # Try to get correct times from session manifest
    manifest_times = get_manifest_video_times(device_id, date)

    for camera in ['cockpit', 'sails']:
        prefix = f"hls/{device_id}/{date}/{camera}/"

        # Check for playlist
        playlist_key = f"{prefix}playlist.m3u8"
        try:
            s3.head_object(Bucket=DATA_BUCKET, Key=playlist_key)
        except s3.exceptions.ClientError:
            continue

        # Get video duration from playlist
        duration = get_playlist_duration(playlist_key)

        # Use manifest times if available (more accurate than filename timestamps)
        manifest_key = f"video_{camera}"
        if manifest_key in manifest_times:
            start_time = manifest_times[manifest_key]['start_time']
            end_time = manifest_times[manifest_key].get('end_time')
        else:
            # Fallback: extract from first segment filename
            start_time, end_time = get_times_from_segments(prefix)

        streams[camera] = {
            'playlist_url': f"https://{CLOUDFRONT_DOMAIN}/hls/{device_id}/{date}/{camera}/playlist.m3u8",
            'start_time': start_time,
            'end_time': end_time,
            'duration_seconds': duration
        }

    return streams


def get_manifest_video_times(device_id: str, date: str) -> dict:
    """Get video times from session manifest."""
    manifest_key = f"processed/{device_id}/{date}/manifest.json"
    try:
        response = s3.get_object(Bucket=DATA_BUCKET, Key=manifest_key)
        manifest = json.loads(response['Body'].read())
        sensors = manifest.get('sensors', {})
        return {k: v for k, v in sensors.items() if k.startswith('video_')}
    except Exception as e:
        logger.warning(f"Could not read manifest: {e}")
        return {}


def get_playlist_duration(playlist_key: str) -> float:
    """Calculate total duration from HLS playlist."""
    try:
        response = s3.get_object(Bucket=DATA_BUCKET, Key=playlist_key)
        content = response['Body'].read().decode('utf-8')
        duration = 0.0
        for line in content.split('\n'):
            if line.startswith('#EXTINF:'):
                dur_str = line.replace('#EXTINF:', '').rstrip(',')
                try:
                    duration += float(dur_str)
                except ValueError:
                    pass
        return round(duration, 1)
    except Exception as e:
        logger.warning(f"Could not parse playlist duration: {e}")
        return 0.0


def get_times_from_segments(prefix: str) -> tuple:
    """Fallback: extract times from segment filenames.

    Video filenames use local time (America/New_York), so we convert to UTC.
    """
    segments = []
    local_tz = ZoneInfo('America/New_York')

    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=DATA_BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key.endswith('.ts'):
                filename = key.split('/')[-1]
                ts_match = re.search(r'(\d{8}_\d{6})', filename)
                if ts_match:
                    ts_str = ts_match.group(1)
                    try:
                        # Parse as local time (ET)
                        local_dt = datetime.strptime(ts_str, '%Y%m%d_%H%M%S')
                        # Attach timezone and convert to UTC
                        local_dt = local_dt.replace(tzinfo=local_tz)
                        utc_dt = local_dt.astimezone(timezone.utc)
                        segments.append(utc_dt)
                    except ValueError:
                        pass

    if segments:
        segments.sort()
        # Return as ISO format with Z suffix (UTC)
        return segments[0].strftime('%Y-%m-%dT%H:%M:%SZ'), segments[-1].strftime('%Y-%m-%dT%H:%M:%SZ')
    return None, None
