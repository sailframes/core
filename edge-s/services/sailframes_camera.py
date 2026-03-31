#!/usr/bin/env python3
"""
SailFrames Smart Camera Service

Supports three modes:
- "smart": Photos every N seconds, video recording during maneuvers (default)
- "continuous": Continuous video recording (legacy mode)
- "photo_only": Only capture timelapse photos

Maneuver detection uses GPS heading rate and IMU acceleration to trigger
video recording with a 2-second pre-buffer captured via CircularOutput.

Usage:
    sailframes_camera.py cockpit   # Record from camera 0 (cockpit)
    sailframes_camera.py sails     # Record from camera 1 (sails)
"""

import os
import sys
import json
import time
import signal
import logging
import argparse
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any

from picamera2 import Picamera2
from picamera2.encoders import H264Encoder, JpegEncoder, Quality
from picamera2.outputs import FfmpegOutput, CircularOutput, FileOutput
import yaml

# Camera ID to index mapping (Pi 5 has CSI-0 and CSI-1)
CAMERA_MAP = {
    'cockpit': 0,  # CSI-0 - cockpit camera
    'sails': 1,    # CSI-1 - sails camera
}

# Status files for sensor data (read by ManeuverDetector)
GPS_STATUS_FILE = Path('/tmp/sailframes-gps-status.json')
IMU_STATUS_FILE = Path('/tmp/sailframes-imu-status.json')

# Camera status file (written by this service for dashboard)
CAMERA_STATUS_FILE = Path('/tmp/sailframes-camera-{camera_id}-status.json')

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


class CameraMode(Enum):
    PHOTO = "photo"        # Taking timelapse photos
    VIDEO = "video"        # Recording maneuver video
    PREBUFFER = "prebuffer"  # Buffering frames, ready to record


class ManeuverDetector:
    """
    Detects tacks and gybes by monitoring GPS heading rate and IMU acceleration.

    Reads sensor data from status files written by GPS and IMU services.
    Triggers video recording when any detection threshold is exceeded.
    """

    def __init__(self, config: Dict[str, Any]):
        self.heading_threshold = config.get('maneuver_heading_rate_threshold', 15)  # deg/sec
        self.accel_threshold = config.get('maneuver_accel_threshold', 0.5)  # m/s²
        self.heel_rate_threshold = config.get('maneuver_heel_rate_threshold', 5)  # deg/sec
        self.calm_duration = config.get('maneuver_calm_duration_sec', 5)  # seconds

        # History buffers for rate calculations
        self.heading_history = deque(maxlen=20)  # 2 sec @ 10Hz GPS
        self.accel_history = deque(maxlen=50)    # 1 sec @ 50Hz IMU
        self.heel_history = deque(maxlen=50)     # 1 sec @ 50Hz IMU

        self.last_gps_update = 0
        self.last_imu_update = 0
        self.calm_start_time: Optional[float] = None
        self.maneuver_start_time: Optional[float] = None
        self.trigger_reason: Optional[str] = None

        # Maneuver metadata
        self.maneuver_data = {
            'heading_start': None,
            'heading_end': None,
            'max_heel': 0,
            'speed_before': None,
            'speed_min': None,
            'position_start': None,
        }

    def update(self) -> None:
        """Read latest sensor data from status files."""
        now = time.time()

        # Read GPS status (10Hz max)
        if now - self.last_gps_update >= 0.1:
            gps = self._read_gps_status()
            if gps:
                self.heading_history.append({
                    'time': now,
                    'course': gps.get('course_deg'),
                    'speed': gps.get('speed_knots'),
                    'lat': gps.get('latitude'),
                    'lon': gps.get('longitude'),
                })
                self.last_gps_update = now

        # Read IMU status (50Hz max)
        if now - self.last_imu_update >= 0.02:
            imu = self._read_imu_status()
            if imu:
                self.accel_history.append({
                    'time': now,
                    'accel_x': imu.get('accel_x_mps2'),
                })
                self.heel_history.append({
                    'time': now,
                    'heel': imu.get('heel_deg'),
                })
                self.last_imu_update = now

    def _read_gps_status(self) -> Optional[Dict]:
        """Read GPS status file."""
        try:
            if GPS_STATUS_FILE.exists():
                with open(GPS_STATUS_FILE, 'r') as f:
                    return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
        return None

    def _read_imu_status(self) -> Optional[Dict]:
        """Read IMU status file."""
        try:
            if IMU_STATUS_FILE.exists():
                with open(IMU_STATUS_FILE, 'r') as f:
                    return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
        return None

    def _calc_heading_rate(self) -> float:
        """Calculate heading change rate in degrees/second."""
        if len(self.heading_history) < 2:
            return 0.0

        recent = [h for h in self.heading_history if h['course'] is not None]
        if len(recent) < 2:
            return 0.0

        # Use first and last valid headings
        h1, h2 = recent[0], recent[-1]
        dt = h2['time'] - h1['time']
        if dt < 0.1:
            return 0.0

        # Handle wrap-around at 360
        delta = h2['course'] - h1['course']
        if delta > 180:
            delta -= 360
        elif delta < -180:
            delta += 360

        return abs(delta / dt)

    def _calc_accel_change(self) -> float:
        """Calculate lateral acceleration change in m/s²."""
        if len(self.accel_history) < 2:
            return 0.0

        recent = [a for a in self.accel_history if a['accel_x'] is not None]
        if len(recent) < 2:
            return 0.0

        # Compare recent acceleration to baseline
        baseline = sum(a['accel_x'] for a in list(recent)[:5]) / min(5, len(recent))
        current = recent[-1]['accel_x']

        return abs(current - baseline)

    def _calc_heel_rate(self) -> float:
        """Calculate heel change rate in degrees/second."""
        if len(self.heel_history) < 2:
            return 0.0

        recent = [h for h in self.heel_history if h['heel'] is not None]
        if len(recent) < 2:
            return 0.0

        h1, h2 = recent[0], recent[-1]
        dt = h2['time'] - h1['time']
        if dt < 0.1:
            return 0.0

        return abs((h2['heel'] - h1['heel']) / dt)

    def check_start(self) -> bool:
        """
        Check if a maneuver is starting.
        Returns True if any detection threshold is exceeded.
        """
        heading_rate = self._calc_heading_rate()
        accel_change = self._calc_accel_change()
        heel_rate = self._calc_heel_rate()

        triggered = False
        reason = None

        if heading_rate > self.heading_threshold:
            triggered = True
            reason = 'heading_rate'
        elif accel_change > self.accel_threshold:
            triggered = True
            reason = 'acceleration'
        elif heel_rate > self.heel_rate_threshold:
            triggered = True
            reason = 'heel_rate'

        if triggered:
            self.trigger_reason = reason
            if self.maneuver_start_time is None:
                self.maneuver_start_time = time.time()
                # Capture start conditions
                if self.heading_history:
                    recent = [h for h in self.heading_history if h['course'] is not None]
                    if recent:
                        self.maneuver_data['heading_start'] = recent[-1]['course']
                        self.maneuver_data['speed_before'] = recent[-1]['speed']
                        self.maneuver_data['position_start'] = {
                            'lat': recent[-1]['lat'],
                            'lon': recent[-1]['lon'],
                        }
                self.maneuver_data['max_heel'] = 0
                self.maneuver_data['speed_min'] = self.maneuver_data['speed_before']

            self.calm_start_time = None

        # Track max heel and min speed during maneuver
        if self.maneuver_start_time:
            if self.heel_history:
                recent_heel = [h['heel'] for h in self.heel_history if h['heel'] is not None]
                if recent_heel:
                    current_heel = abs(recent_heel[-1])
                    self.maneuver_data['max_heel'] = max(self.maneuver_data['max_heel'], current_heel)
            if self.heading_history:
                recent_speed = [h['speed'] for h in self.heading_history if h['speed'] is not None]
                if recent_speed and self.maneuver_data['speed_min'] is not None:
                    self.maneuver_data['speed_min'] = min(self.maneuver_data['speed_min'], recent_speed[-1])

        return triggered

    def check_end(self) -> bool:
        """
        Check if a maneuver has ended.
        Returns True if all conditions have been calm for calm_duration seconds.
        """
        if self.maneuver_start_time is None:
            return False

        # Check if currently calm
        if not self.check_start():
            if self.calm_start_time is None:
                self.calm_start_time = time.time()
            elif time.time() - self.calm_start_time >= self.calm_duration:
                # Capture end conditions
                if self.heading_history:
                    recent = [h for h in self.heading_history if h['course'] is not None]
                    if recent:
                        self.maneuver_data['heading_end'] = recent[-1]['course']

                # Reset state
                self.calm_start_time = None
                self.maneuver_start_time = None
                return True

        return False

    def get_maneuver_metadata(self) -> Dict[str, Any]:
        """Get metadata about the current/just-ended maneuver."""
        # Determine maneuver type from heading change
        maneuver_type = 'unknown'
        heading_change = 0
        if self.maneuver_data['heading_start'] and self.maneuver_data['heading_end']:
            heading_change = self.maneuver_data['heading_end'] - self.maneuver_data['heading_start']
            if heading_change > 180:
                heading_change -= 360
            elif heading_change < -180:
                heading_change += 360

            if abs(heading_change) > 60:
                maneuver_type = 'tack' if abs(heading_change) < 120 else 'gybe'

        return {
            'type': maneuver_type,
            'trigger': self.trigger_reason,
            'heading_change_deg': round(heading_change, 1) if heading_change else None,
            'max_heel_deg': round(self.maneuver_data['max_heel'], 1),
            'speed_before_kts': round(self.maneuver_data['speed_before'], 1) if self.maneuver_data['speed_before'] else None,
            'speed_min_kts': round(self.maneuver_data['speed_min'], 1) if self.maneuver_data['speed_min'] else None,
            'position': self.maneuver_data['position_start'],
        }

    def reset_maneuver_data(self):
        """Reset maneuver metadata for next detection."""
        self.maneuver_data = {
            'heading_start': None,
            'heading_end': None,
            'max_heel': 0,
            'speed_before': None,
            'speed_min': None,
            'position_start': None,
        }
        self.trigger_reason = None


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


def get_photo_dir(config, camera_id):
    """Create today's photo directory."""
    base = config['storage']['data_dir']
    today = datetime.now().strftime('%Y-%m-%d')  # Local time
    photo_dir = Path(base) / today / 'photos' / camera_id
    photo_dir.mkdir(parents=True, exist_ok=True)
    return photo_dir


def get_maneuver_dir(config, camera_id):
    """Create today's maneuver video directory."""
    base = config['storage']['data_dir']
    today = datetime.now().strftime('%Y-%m-%d')  # Local time
    maneuver_dir = Path(base) / today / 'maneuvers' / camera_id
    maneuver_dir.mkdir(parents=True, exist_ok=True)
    return maneuver_dir


def get_video_dir(config, camera_id):
    """Create today's video directory (for continuous mode)."""
    base = config['storage']['data_dir']
    today = datetime.now().strftime('%Y-%m-%d')  # Local time
    video_dir = Path(base) / today / 'video' / camera_id
    video_dir.mkdir(parents=True, exist_ok=True)
    return video_dir


def update_camera_status(camera_id: str, mode: str, photo_count: int, maneuver_count: int,
                         recording: bool = False, error: str = None):
    """Write camera status to file for dashboard."""
    try:
        status_file = Path(str(CAMERA_STATUS_FILE).format(camera_id=camera_id))
        status = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'camera_id': camera_id,
            'mode': mode,
            'photo_count': photo_count,
            'maneuver_count': maneuver_count,
            'recording_maneuver': recording,
            'error': error,
        }
        with open(status_file, 'w') as f:
            json.dump(status, f)
    except Exception as e:
        logger.warning(f"Failed to update camera status: {e}")


def capture_photo(picam2, camera_id: str, photo_dir: Path, quality: int = 90) -> Optional[Path]:
    """Capture a single JPEG photo."""
    try:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filepath = photo_dir / f'{camera_id}_{timestamp}.jpg'

        # Capture to file using JPEG encoder
        picam2.capture_file(str(filepath), format='jpeg')

        file_size_kb = filepath.stat().st_size / 1024
        logger.debug(f"Photo captured: {filepath.name} ({file_size_kb:.0f}KB)")
        return filepath
    except Exception as e:
        logger.error(f"Photo capture failed: {e}")
        return None


def write_maneuver_metadata(video_path: Path, camera_id: str, start_time: datetime,
                            end_time: datetime, prebuffer_sec: float,
                            maneuver_meta: Dict[str, Any]):
    """Write JSON sidecar file for maneuver video."""
    try:
        metadata = {
            'type': maneuver_meta.get('type', 'unknown'),
            'camera': camera_id,
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'duration_sec': round((end_time - start_time).total_seconds(), 1),
            'prebuffer_sec': prebuffer_sec,
            'trigger': maneuver_meta.get('trigger'),
            'heading_change_deg': maneuver_meta.get('heading_change_deg'),
            'max_heel_deg': maneuver_meta.get('max_heel_deg'),
            'speed_before_kts': maneuver_meta.get('speed_before_kts'),
            'speed_min_kts': maneuver_meta.get('speed_min_kts'),
            'position': maneuver_meta.get('position'),
        }

        json_path = video_path.with_suffix('.json')
        with open(json_path, 'w') as f:
            json.dump(metadata, f, indent=2)

        logger.info(f"Maneuver metadata written: {json_path.name}")
    except Exception as e:
        logger.error(f"Failed to write maneuver metadata: {e}")


def run_smart_mode(config, camera_id: str):
    """
    Run camera in smart mode: photos normally, video during maneuvers.
    Uses CircularOutput for 2-second pre-buffer.
    """
    global running
    cam_config = config['camera']
    width, height = cam_config['resolution']
    fps = cam_config['framerate']
    camera_index = CAMERA_MAP.get(camera_id, 0)

    # Smart mode settings
    photo_interval = cam_config.get('photo_interval_sec', 5)
    photo_quality = cam_config.get('photo_quality', 90)
    prebuffer_sec = cam_config.get('maneuver_prebuffer_sec', 2)
    postbuffer_sec = cam_config.get('maneuver_postbuffer_sec', 2)
    video_bitrate = cam_config.get('maneuver_bitrate_mbps', 4) * 1_000_000

    logger.info(f"Initializing {camera_id} camera (index {camera_index}) in SMART mode: "
                f"{width}x{height} @ {fps}fps, photos every {photo_interval}s")

    try:
        picam2 = Picamera2(camera_num=camera_index)
    except Exception as e:
        logger.error(f"Camera {camera_id} initialization failed: {e}")
        sys.exit(1)

    # Configure for video (also used for stills via capture_file)
    video_config = picam2.create_video_configuration(
        main={"size": (width, height), "format": "RGB888"},
        controls={"FrameRate": fps}
    )
    picam2.configure(video_config)

    # Apply focus settings
    af_mode = cam_config.get('autofocus', 'infinity')
    picam2.start()

    if af_mode == 'infinity':
        picam2.set_controls({"AfMode": 0, "LensPosition": 0.0})
        logger.info("Manual focus set to infinity")
    elif af_mode == 'auto':
        picam2.set_controls({"AfMode": 1, "AfSpeed": 1})
        time.sleep(0.1)
        picam2.set_controls({"AfTrigger": 1})
        logger.info("Autofocus triggered")
    else:
        picam2.set_controls({"AfMode": 2, "AfSpeed": 1})
        logger.info("Continuous autofocus enabled")

    time.sleep(2)  # Allow auto-exposure to settle

    # Initialize H264 encoder with circular buffer for pre-roll
    # Buffer size: prebuffer_sec * fps * estimated_frame_size
    frame_size_estimate = 50000  # ~50KB per frame at moderate bitrate
    buffer_size = int(prebuffer_sec * fps * frame_size_estimate)

    encoder = H264Encoder(bitrate=video_bitrate)
    circular = CircularOutput(buffersize=buffer_size)

    # Start encoder with circular buffer (low CPU, continuous buffering)
    picam2.start_encoder(encoder, circular)
    logger.info(f"Circular buffer started: {prebuffer_sec}s pre-roll, {buffer_size/1024/1024:.1f}MB buffer")

    # Initialize directories and state
    photo_dir = get_photo_dir(config, camera_id)
    maneuver_dir = get_maneuver_dir(config, camera_id)

    maneuver_detector = ManeuverDetector(cam_config)
    mode = CameraMode.PHOTO

    photo_count = 0
    maneuver_count = 0
    last_photo_time = 0
    maneuver_start_time: Optional[datetime] = None
    current_video_path: Optional[Path] = None
    file_output: Optional[FileOutput] = None

    try:
        while running:
            # Update maneuver detector with latest sensor data
            maneuver_detector.update()

            if mode == CameraMode.PHOTO:
                # Check disk space
                stat = os.statvfs(str(photo_dir))
                free_gb = (stat.f_frsize * stat.f_bavail) / (1024 ** 3)
                if free_gb < 0.5:
                    logger.warning(f"Low disk space: {free_gb:.1f}GB remaining")
                    if free_gb < 0.1:
                        logger.error("Critically low disk space, stopping")
                        break

                # Capture photo at interval
                now = time.monotonic()
                if now - last_photo_time >= photo_interval:
                    # Need to stop encoder briefly to capture still
                    picam2.stop_encoder()

                    if capture_photo(picam2, camera_id, photo_dir, photo_quality):
                        photo_count += 1
                    last_photo_time = now

                    # Restart encoder with circular buffer
                    picam2.start_encoder(encoder, circular)

                    # Update status
                    update_camera_status(camera_id, 'smart', photo_count, maneuver_count)

                # Check for maneuver start
                if maneuver_detector.check_start():
                    logger.info(f"Maneuver detected! Trigger: {maneuver_detector.trigger_reason}")
                    mode = CameraMode.VIDEO
                    maneuver_start_time = datetime.now(timezone.utc)

                    # Create video file
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    current_video_path = maneuver_dir / f'maneuver_{timestamp}.mp4'

                    # Switch from circular buffer to file output
                    # This captures the pre-buffer and continues recording
                    file_output = FfmpegOutput(str(current_video_path))
                    circular.fileoutput = file_output
                    circular.start()

                    logger.info(f"Recording maneuver to: {current_video_path.name}")
                    update_camera_status(camera_id, 'smart', photo_count, maneuver_count, recording=True)
                else:
                    time.sleep(0.1)  # Small sleep in photo mode

            elif mode == CameraMode.VIDEO:
                # Continue recording, check for maneuver end
                if maneuver_detector.check_end():
                    # Record post-buffer
                    logger.info(f"Maneuver ended, recording {postbuffer_sec}s post-buffer")
                    time.sleep(postbuffer_sec)

                    # Stop recording
                    circular.stop()

                    maneuver_end_time = datetime.now(timezone.utc)
                    maneuver_count += 1

                    # Write metadata
                    if current_video_path and current_video_path.exists():
                        file_size_mb = current_video_path.stat().st_size / (1024 * 1024)
                        logger.info(f"Maneuver {maneuver_count} recorded: {file_size_mb:.1f}MB")

                        write_maneuver_metadata(
                            current_video_path, camera_id,
                            maneuver_start_time, maneuver_end_time,
                            prebuffer_sec, maneuver_detector.get_maneuver_metadata()
                        )

                    # Reset state
                    maneuver_detector.reset_maneuver_data()
                    mode = CameraMode.PHOTO
                    maneuver_start_time = None
                    current_video_path = None

                    update_camera_status(camera_id, 'smart', photo_count, maneuver_count, recording=False)
                else:
                    time.sleep(0.05)  # Faster polling during video

            # Rotate directories at midnight
            current_date = datetime.now().strftime('%Y-%m-%d')
            if photo_dir.parent.parent.name != current_date:
                photo_dir = get_photo_dir(config, camera_id)
                maneuver_dir = get_maneuver_dir(config, camera_id)

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        try:
            circular.stop()
        except Exception:
            pass
        try:
            picam2.stop_encoder()
        except Exception:
            pass
        picam2.stop()
        logger.info(f"Smart camera stopped. {photo_count} photos, {maneuver_count} maneuvers.")


def run_continuous_mode(config, camera_id: str):
    """
    Run camera in continuous video mode (legacy behavior).
    Records segmented video files continuously.
    """
    global running
    cam_config = config['camera']
    width, height = cam_config['resolution']
    fps = cam_config['framerate']
    segment_min = cam_config.get('segment_duration_min', 5)
    bitrate = cam_config.get('bitrate_mbps', 8) * 1_000_000
    camera_index = CAMERA_MAP.get(camera_id, 0)

    logger.info(f"Initializing {camera_id} camera (index {camera_index}) in CONTINUOUS mode: "
                f"{width}x{height} @ {fps}fps, {cam_config.get('bitrate_mbps', 8)}Mbps, "
                f"{segment_min}min segments")

    try:
        picam2 = Picamera2(camera_num=camera_index)
    except Exception as e:
        logger.error(f"Camera {camera_id} initialization failed: {e}")
        sys.exit(1)

    video_config = picam2.create_video_configuration(
        main={"size": (width, height), "format": "RGB888"},
        controls={"FrameRate": fps}
    )
    picam2.configure(video_config)

    # Apply rotation if configured
    rotation = cam_config.get('rotation', 0)
    if rotation:
        picam2.set_controls({"Rotation": rotation})

    encoder = H264Encoder(bitrate=bitrate)

    picam2.start()
    logger.info(f"Camera {camera_id} started")

    # Set focus
    af_mode = cam_config.get('autofocus', 'infinity')
    if af_mode == 'infinity':
        picam2.set_controls({"AfMode": 0, "LensPosition": 0.0})
        logger.info("Manual focus set to infinity")
    elif af_mode == 'auto':
        picam2.set_controls({"AfMode": 1, "AfSpeed": 1})
        time.sleep(0.1)
        picam2.set_controls({"AfTrigger": 1})
        logger.info("Autofocus triggered")
    else:
        picam2.set_controls({"AfMode": 2, "AfSpeed": 1})
        logger.info("Continuous autofocus enabled")

    time.sleep(2)

    data_dir = get_video_dir(config, camera_id)
    segment_count = 0

    try:
        while running:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filepath = data_dir / f'{camera_id}_{timestamp}.mp4'
            output = FfmpegOutput(str(filepath))

            logger.info(f"Recording {camera_id} segment {segment_count}: {filepath}")
            picam2.start_recording(encoder, output)

            segment_end = time.monotonic() + (segment_min * 60)

            while running and time.monotonic() < segment_end:
                time.sleep(1)

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

            current_date = datetime.now().strftime('%Y-%m-%d')
            if data_dir.parent.parent.name != current_date:
                data_dir = get_video_dir(config, camera_id)

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        try:
            picam2.stop_recording()
        except Exception:
            pass
        picam2.stop()
        logger.info(f"Continuous camera stopped. {segment_count} segments recorded.")


def run_photo_only_mode(config, camera_id: str):
    """
    Run camera in photo-only mode (lowest power consumption).
    Captures timelapse photos at configured interval.
    """
    global running
    cam_config = config['camera']
    width, height = cam_config.get('photo_resolution', cam_config['resolution'])
    camera_index = CAMERA_MAP.get(camera_id, 0)

    photo_interval = cam_config.get('photo_interval_sec', 5)
    photo_quality = cam_config.get('photo_quality', 90)

    logger.info(f"Initializing {camera_id} camera (index {camera_index}) in PHOTO_ONLY mode: "
                f"{width}x{height}, photos every {photo_interval}s")

    try:
        picam2 = Picamera2(camera_num=camera_index)
    except Exception as e:
        logger.error(f"Camera {camera_id} initialization failed: {e}")
        sys.exit(1)

    # Configure for stills
    still_config = picam2.create_still_configuration(
        main={"size": (width, height), "format": "RGB888"}
    )
    picam2.configure(still_config)

    picam2.start()
    logger.info(f"Camera {camera_id} started")

    # Set focus
    af_mode = cam_config.get('autofocus', 'infinity')
    if af_mode == 'infinity':
        picam2.set_controls({"AfMode": 0, "LensPosition": 0.0})
        logger.info("Manual focus set to infinity")
    elif af_mode == 'auto':
        picam2.set_controls({"AfMode": 1, "AfSpeed": 1})
        time.sleep(0.1)
        picam2.set_controls({"AfTrigger": 1})
        logger.info("Autofocus triggered")
    else:
        picam2.set_controls({"AfMode": 2, "AfSpeed": 1})
        logger.info("Continuous autofocus enabled")

    time.sleep(2)

    photo_dir = get_photo_dir(config, camera_id)
    photo_count = 0

    try:
        while running:
            # Check disk space
            stat = os.statvfs(str(photo_dir))
            free_gb = (stat.f_frsize * stat.f_bavail) / (1024 ** 3)
            if free_gb < 0.5:
                logger.warning(f"Low disk space: {free_gb:.1f}GB remaining")
                if free_gb < 0.1:
                    logger.error("Critically low disk space, stopping")
                    break

            # Capture photo
            if capture_photo(picam2, camera_id, photo_dir, photo_quality):
                photo_count += 1

            update_camera_status(camera_id, 'photo_only', photo_count, 0)

            # Rotate directory at midnight
            current_date = datetime.now().strftime('%Y-%m-%d')
            if photo_dir.parent.parent.name != current_date:
                photo_dir = get_photo_dir(config, camera_id)

            # Sleep until next photo
            time.sleep(photo_interval)

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        picam2.stop()
        logger.info(f"Photo-only camera stopped. {photo_count} photos captured.")


def run(config, camera_id: str):
    """Main entry point - dispatch to appropriate mode."""
    cam_config = config['camera']
    mode = cam_config.get('mode', 'smart')

    if mode == 'smart':
        run_smart_mode(config, camera_id)
    elif mode == 'continuous':
        run_continuous_mode(config, camera_id)
    elif mode == 'photo_only':
        run_photo_only_mode(config, camera_id)
    else:
        logger.error(f"Unknown camera mode: {mode}")
        sys.exit(1)


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
