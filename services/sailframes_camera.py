#!/usr/bin/env python3
"""
SailFrames Camera Service
Records 1080p/30fps video from Pi Camera Module 3 Wide.
Segments video into configurable-length files.
Supports multiple cameras (cockpit, sails) via command line argument.

Usage:
    sailframes_camera.py cockpit   # Record from camera 0 (cockpit)
    sailframes_camera.py sails     # Record from camera 1 (sails)
"""

import os
import sys
import time
import signal
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path

from picamera2 import Picamera2
from picamera2.encoders import H264Encoder, Quality
from picamera2.outputs import FfmpegOutput
import yaml

# Camera ID to index mapping (Pi 5 has CSI-0 and CSI-1)
CAMERA_MAP = {
    'cockpit': 0,  # CSI-0 - cockpit camera
    'sails': 1,    # CSI-1 - sails camera
}

# Will be set from command line argument
CAMERA_ID = 'cockpit'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [CAMERA] %(levelname)s %(message)s'
)
logger = logging.getLogger('sailframes.camera')

running = True

def signal_handler(sig, frame):
    global running
    logger.info("Shutdown signal received")
    running = False

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


def load_config():
    config_paths = [
        '/etc/sailframes/sailframes.yaml',
        os.path.join(os.path.dirname(__file__), '..', 'config', 'sailframes.yaml')
    ]
    for path in config_paths:
        if os.path.exists(path):
            with open(path) as f:
                return yaml.safe_load(f)
    logger.error("No config file found")
    sys.exit(1)


def get_data_dir(config, camera_id):
    base = config['storage']['data_dir']
    today = datetime.now().strftime('%Y-%m-%d')  # Local time
    # Store videos in camera-specific subdirectory
    data_dir = Path(base) / today / 'video' / camera_id
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def run(config, camera_id):
    global running
    cam_config = config['camera']
    width, height = cam_config['resolution']
    fps = cam_config['framerate']
    segment_min = cam_config['segment_duration_min']
    bitrate = cam_config['bitrate_mbps'] * 1_000_000
    camera_index = CAMERA_MAP.get(camera_id, 0)

    logger.info(f"Initializing {camera_id} camera (index {camera_index}): "
                f"{width}x{height} @ {fps}fps, "
                f"{cam_config['bitrate_mbps']}Mbps, {segment_min}min segments")

    try:
        picam2 = Picamera2(camera_num=camera_index)
    except Exception as e:
        logger.error(f"Camera {camera_id} initialization failed: {e}")
        logger.error("Is the camera connected and enabled?")
        sys.exit(1)

    # Configure for video recording
    # Set autofocus in the initial configuration for Pi Camera Module 3
    af_mode = cam_config.get('autofocus', 'continuous')
    af_mode_value = {"continuous": 2, "single": 1, "manual": 0}.get(af_mode, 2)

    video_config = picam2.create_video_configuration(
        main={"size": (width, height), "format": "RGB888"},
        controls={
            "FrameRate": fps,
            "AfMode": af_mode_value,      # 0=Manual, 1=Auto, 2=Continuous
            "AfSpeed": 1,                  # 0=Normal, 1=Fast
            "AfRange": 0,                  # 0=Normal, 1=Macro, 2=Full
        }
    )
    picam2.configure(video_config)
    logger.info(f"Autofocus mode: {af_mode} (AfMode={af_mode_value}, AfSpeed=Fast)")

    # Apply rotation if configured
    rotation = cam_config.get('rotation', 0)
    if rotation:
        picam2.set_controls({"Rotation": rotation})

    # H.264 encoder
    encoder = H264Encoder(bitrate=bitrate)

    picam2.start()
    logger.info(f"Camera {camera_id} started")

    # Trigger autofocus after start for continuous mode
    if af_mode == 'continuous':
        picam2.set_controls({"AfTrigger": 1})  # Start continuous AF
        logger.info("Continuous autofocus triggered")

    # Allow auto-exposure and autofocus to settle
    time.sleep(3)

    data_dir = get_data_dir(config, camera_id)
    segment_count = 0

    try:
        while running:
            # Create new segment file (local time for readability)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filepath = data_dir / f'{camera_id}_{timestamp}.mp4'
            output = FfmpegOutput(str(filepath))

            logger.info(f"Recording {camera_id} segment {segment_count}: {filepath}")
            picam2.start_recording(encoder, output)

            # Record for segment_duration_min or until shutdown
            segment_end = time.monotonic() + (segment_min * 60)

            while running and time.monotonic() < segment_end:
                time.sleep(1)

                # Check disk space
                stat = os.statvfs(str(data_dir))
                free_gb = (stat.f_frsize * stat.f_bavail) / (1024 ** 3)
                if free_gb < 1.0:
                    logger.warning(f"Low disk space: {free_gb:.1f}GB remaining")
                    if free_gb < 0.2:
                        logger.error("Critically low disk space, stopping recording")
                        running = False
                        break

            picam2.stop_recording()
            segment_count += 1
            file_size_mb = filepath.stat().st_size / (1024 * 1024)
            logger.info(f"Segment complete: {file_size_mb:.1f}MB")

            # Rotate directory at midnight (local time)
            current_date = datetime.now().strftime('%Y-%m-%d')
            if data_dir.parent.parent.name != current_date:
                data_dir = get_data_dir(config, camera_id)

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        try:
            picam2.stop_recording()
        except Exception:
            pass
        picam2.stop()
        logger.info(f"Camera service stopped. {segment_count} segments recorded.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SailFrames Camera Service')
    parser.add_argument('camera_id', choices=['cockpit', 'sails'],
                        help='Camera identifier (cockpit or sails)')
    args = parser.parse_args()

    # Update logger to include camera ID
    logging.basicConfig(
        level=logging.INFO,
        format=f'%(asctime)s [CAMERA-{args.camera_id.upper()}] %(levelname)s %(message)s',
        force=True
    )

    config = load_config()
    if not config['camera']['enabled']:
        logger.info("Camera disabled in config, exiting")
        sys.exit(0)
    run(config, args.camera_id)
