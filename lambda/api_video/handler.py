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
        session = path_params.get('date')  # Can be date or date-session_id

        if not device_id or not session:
            return error_response(400, 'Missing device_id or session')

        streams = get_video_streams(device_id, session)

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


def get_video_streams(device_id: str, session: str) -> dict:
    """Get video stream info - supports HLS playlists or direct video files.

    Args:
        device_id: Device ID (e.g., 'E1')
        session: Session folder name (e.g., '2026-04-02-boot47-162554')
    """
    streams = {}

    # Extract date from session (first 10 chars: YYYY-MM-DD)
    date = session[:10] if len(session) >= 10 else session

    # Try to get correct times from session manifest
    manifest_times = get_manifest_video_times(device_id, session)

    # First try HLS streams (transcoded videos)
    for camera in ['cockpit', 'sails']:
        prefix = f"hls/{device_id}/{session}/{camera}/"

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
            'playlist_url': f"https://{CLOUDFRONT_DOMAIN}/hls/{device_id}/{session}/{camera}/playlist.m3u8",
            'start_time': start_time,
            'end_time': end_time,
            'duration_seconds': duration
        }

    # If no HLS streams, check for direct video files from manifest
    if not streams:
        streams = get_direct_video_streams(device_id, session)

    return streams


def get_direct_video_streams(device_id: str, session: str) -> dict:
    """Get direct video file URLs from manifest (for non-transcoded videos).

    Prefers concatenated proxy video (LRV) if available for smooth playback.

    Args:
        device_id: Device ID (e.g., 'E1')
        session: Session folder name (e.g., '2026-04-02-boot47-162554' or just '2026-04-02')
    """
    manifest_key = f"processed/{device_id}/{session}/manifest.json"
    try:
        response = s3.get_object(Bucket=DATA_BUCKET, Key=manifest_key)
        manifest = json.loads(response['Body'].read())

        # Prefer proxy video (concatenated LRV) for smooth playback
        video_proxy = manifest.get('video_proxy')
        if video_proxy and video_proxy.get('s3_key'):
            try:
                presigned_url = s3.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': DATA_BUCKET, 'Key': video_proxy['s3_key']},
                    ExpiresIn=3600  # 1 hour
                )
                return {
                    'video_proxy': {
                        'direct_url': presigned_url,
                        'filename': video_proxy.get('filename', 'video_proxy.mp4'),
                        'start_time': video_proxy.get('start_time'),
                        'end_time': video_proxy.get('end_time'),
                        'duration_seconds': video_proxy.get('duration_sec', 0),
                        'offset_seconds': video_proxy.get('offset_sec', 0),
                        'resolution': video_proxy.get('resolution', '240p'),
                        'is_proxy': True
                    }
                }
            except Exception as e:
                logger.warning(f"Could not generate presigned URL for proxy video: {e}")
                # Fall through to individual chapters

        # Fallback to individual chapter videos
        videos = manifest.get('videos', [])
        if not videos:
            return {}

        streams = {}
        for i, video in enumerate(videos):
            # Generate presigned URL for direct access
            # Support both 's3_key' (new format) and 'url' (old format)
            video_key = video.get('s3_key') or video.get('url', '')
            if not video_key:
                continue

            try:
                presigned_url = s3.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': DATA_BUCKET, 'Key': video_key},
                    ExpiresIn=3600  # 1 hour
                )
            except Exception as e:
                logger.warning(f"Could not generate presigned URL for {video_key}: {e}")
                continue

            camera_name = f"video_{i+1}"
            streams[camera_name] = {
                'direct_url': presigned_url,
                'filename': video.get('filename', ''),
                'start_time': video.get('start_time'),
                'end_time': video.get('end_time'),
                'duration_seconds': video.get('duration_sec', 0),
                'offset_seconds': video.get('offset_sec', 0),
                'chapter': video.get('chapter', 0)
            }

        return streams
    except s3.exceptions.NoSuchKey:
        logger.warning(f"Manifest not found: {manifest_key}")
        return {}
    except Exception as e:
        logger.warning(f"Could not read manifest for direct videos: {e}")
        return {}


def get_manifest_video_times(device_id: str, session: str) -> dict:
    """Get video times from session manifest."""
    manifest_key = f"processed/{device_id}/{session}/manifest.json"
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
