"""
SailFrames Video Transcoding Lambda
Triggered when MP4 files are uploaded to raw/ prefix.
Creates AWS MediaConvert jobs to transcode to HLS for web playback.
"""

import json
import os
import re
import boto3
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import unquote_plus

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
mediaconvert = boto3.client('mediaconvert', endpoint_url=os.environ.get('MEDIACONVERT_ENDPOINT'))

DATA_BUCKET = os.environ.get('DATA_BUCKET', 'sailframes-data-prod')
MEDIACONVERT_ROLE = os.environ.get('MEDIACONVERT_ROLE')
CLOUDFRONT_DOMAIN = os.environ.get('CLOUDFRONT_DOMAIN', 'sailframes.com')


def lambda_handler(event, context):
    """Process uploaded MP4 files and create HLS transcoding jobs."""
    for record in event.get('Records', []):
        bucket = record['s3']['bucket']['name']
        key = unquote_plus(record['s3']['object']['key'])

        # Only process MP4 files in video directories
        if not key.endswith('.mp4') or '/video/' not in key:
            logger.info(f"Skipping non-video file: {key}")
            continue

        logger.info(f"Processing video: {bucket}/{key}")

        try:
            create_transcode_job(bucket, key)
        except Exception as e:
            logger.error(f"Failed to create transcode job for {key}: {e}")
            raise

    return {'statusCode': 200, 'body': 'OK'}


def create_transcode_job(bucket: str, key: str):
    """Create MediaConvert job to transcode MP4 to HLS."""
    # Parse path: raw/{device_id}/{date}/video/{camera}/{filename}.mp4
    parts = key.split('/')
    if len(parts) < 5:
        logger.warning(f"Invalid path structure: {key}")
        return

    device_id = parts[1]
    date = parts[2]
    camera = parts[4]
    filename = parts[5]

    # Extract timestamp from filename (e.g., cockpit_20260324_130339.mp4)
    ts_match = re.search(r'(\d{8}_\d{6})', filename)
    if not ts_match:
        logger.warning(f"Could not extract timestamp from filename: {filename}")
        return

    ts_str = ts_match.group(1)

    # Convert local time (ET) to UTC for consistent naming
    local_tz = ZoneInfo('America/New_York')
    local_dt = datetime.strptime(ts_str, '%Y%m%d_%H%M%S')
    local_dt = local_dt.replace(tzinfo=local_tz)
    utc_dt = local_dt.astimezone(timezone.utc)
    utc_ts = utc_dt.strftime('%Y%m%d_%H%M%S')

    # Output path for HLS segments
    output_prefix = f"hls/{device_id}/{date}/{camera}/"
    segment_prefix = f"segment_{utc_ts}_"

    input_file = f"s3://{bucket}/{key}"
    output_path = f"s3://{bucket}/{output_prefix}"

    logger.info(f"Creating transcode job: {input_file} -> {output_path}")

    # MediaConvert job settings
    job_settings = {
        "Inputs": [
            {
                "FileInput": input_file,
                "AudioSelectors": {
                    "Audio Selector 1": {
                        "DefaultSelection": "DEFAULT"
                    }
                },
                "VideoSelector": {},
                "TimecodeSource": "ZEROBASED"
            }
        ],
        "OutputGroups": [
            {
                "Name": "HLS",
                "OutputGroupSettings": {
                    "Type": "HLS_GROUP_SETTINGS",
                    "HlsGroupSettings": {
                        "Destination": output_path,
                        "SegmentLength": 6,
                        "MinSegmentLength": 0,
                        "SegmentControl": "SEGMENTED_FILES",
                        "ManifestDurationFormat": "FLOATING_POINT",
                        "OutputSelection": "MANIFESTS_AND_SEGMENTS",
                        "StreamInfResolution": "INCLUDE",
                        "ClientCache": "ENABLED",
                        "ManifestCompression": "NONE",
                        "DirectoryStructure": "SINGLE_DIRECTORY"
                    }
                },
                "Outputs": [
                    {
                        "NameModifier": f"_{utc_ts}",
                        "ContainerSettings": {
                            "Container": "M3U8",
                            "M3u8Settings": {}
                        },
                        "VideoDescription": {
                            "Width": 1920,
                            "Height": 1080,
                            "CodecSettings": {
                                "Codec": "H_264",
                                "H264Settings": {
                                    "RateControlMode": "CBR",
                                    "Bitrate": 5000000,
                                    "CodecProfile": "MAIN",
                                    "CodecLevel": "AUTO",
                                    "FramerateControl": "INITIALIZE_FROM_SOURCE",
                                    "GopSize": 2,
                                    "GopSizeUnits": "SECONDS",
                                    "InterlaceMode": "PROGRESSIVE"
                                }
                            }
                        },
                        "AudioDescriptions": [
                            {
                                "CodecSettings": {
                                    "Codec": "AAC",
                                    "AacSettings": {
                                        "Bitrate": 128000,
                                        "CodingMode": "CODING_MODE_2_0",
                                        "SampleRate": 48000,
                                        "RateControlMode": "CBR"
                                    }
                                }
                            }
                        ]
                    }
                ]
            }
        ],
        "TimecodeConfig": {
            "Source": "ZEROBASED"
        }
    }

    response = mediaconvert.create_job(
        Role=MEDIACONVERT_ROLE,
        Settings=job_settings,
        UserMetadata={
            'device_id': device_id,
            'date': date,
            'camera': camera,
            'source_file': key
        },
        StatusUpdateInterval="SECONDS_60",
        AccelerationSettings={
            "Mode": "DISABLED"
        },
        Priority=0
    )

    job_id = response['Job']['Id']
    logger.info(f"Created MediaConvert job: {job_id}")

    return job_id


def update_manifest_with_video(bucket: str, device_id: str, date: str, camera: str,
                                start_time: str, end_time: str, duration: float):
    """Update session manifest with video information."""
    manifest_key = f"processed/{device_id}/{date}/manifest.json"

    # Load existing manifest
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
        Bucket=bucket,
        Key=manifest_key,
        Body=json.dumps(manifest, indent=2),
        ContentType='application/json'
    )
    logger.info(f"Updated manifest with video info: {manifest_key}")
