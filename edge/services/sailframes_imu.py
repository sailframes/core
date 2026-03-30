#!/usr/bin/env python3
"""
SailFrames IMU Service
Reads BNO085 via I2C, logs heel/pitch/heading and linear acceleration.
"""

import os
import sys
import csv
import json
import math
import time
import signal
import logging
from datetime import datetime, timezone
from pathlib import Path

import board
import busio
import adafruit_bno08x
from adafruit_bno08x.i2c import BNO08X_I2C
import yaml

# Shared status file for dashboard to read current IMU data
IMU_STATUS_FILE = Path('/tmp/sailframes-imu-status.json')
# Calibration offsets file (written by dashboard, read by this service)
IMU_CALIBRATION_FILE = Path('/etc/sailframes/imu-calibration.json')

# Global calibration offsets (loaded at startup and when file changes)
calibration_offsets = {
    'heel_offset': 0.0,
    'pitch_offset': 0.0,
    'invert_heel': False,
    'invert_pitch': False,
    'swap_axes': False,
}
calibration_mtime = 0

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [IMU] %(levelname)s %(message)s'
)
logger = logging.getLogger('sailframes.imu')

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


def get_data_dir(config):
    base = config['storage']['data_dir']
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    data_dir = Path(base) / today / 'imu'
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def create_csv_writer(data_dir):
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    filepath = data_dir / f'imu_{timestamp}.csv'
    f = open(filepath, 'w', newline='')
    writer = csv.writer(f)
    writer.writerow([
        'utc_time',
        'quat_i', 'quat_j', 'quat_k', 'quat_real',  # Rotation vector quaternion
        'heading_deg',       # Magnetic heading (0-360)
        'pitch_deg',         # Bow up/down (-90 to 90)
        'heel_deg',          # Roll port/starboard (-180 to 180)
        'accel_x_mps2',     # Linear acceleration X (forward)
        'accel_y_mps2',     # Linear acceleration Y (starboard)
        'accel_z_mps2',     # Linear acceleration Z (down)
        'accuracy',          # Rotation vector accuracy estimate (radians)
    ])
    logger.info(f"Logging to {filepath}")
    return f, writer


def quaternion_to_euler(i, j, k, real):
    """
    Convert quaternion to nautical Euler angles.
    Returns (heading, pitch, heel) in degrees.
    
    Heading: 0-360, 0=North, 90=East
    Pitch: -90 to 90, positive = bow up
    Heel: -180 to 180, positive = starboard heel
    """
    # Roll (heel) - rotation around X axis
    sinr_cosp = 2.0 * (real * i + j * k)
    cosr_cosp = 1.0 - 2.0 * (i * i + j * j)
    heel = math.degrees(math.atan2(sinr_cosp, cosr_cosp))

    # Pitch - rotation around Y axis
    sinp = 2.0 * (real * j - k * i)
    if abs(sinp) >= 1:
        pitch = math.degrees(math.copysign(math.pi / 2, sinp))
    else:
        pitch = math.degrees(math.asin(sinp))

    # Yaw (heading) - rotation around Z axis
    siny_cosp = 2.0 * (real * k + i * j)
    cosy_cosp = 1.0 - 2.0 * (j * j + k * k)
    heading = math.degrees(math.atan2(siny_cosp, cosy_cosp))

    # Normalize heading to 0-360
    if heading < 0:
        heading += 360.0

    return heading, pitch, heel


def load_calibration_offsets():
    """Load calibration offsets from file. Called periodically to pick up changes."""
    global calibration_offsets, calibration_mtime
    try:
        if not IMU_CALIBRATION_FILE.exists():
            return calibration_offsets

        # Check if file has been modified
        mtime = IMU_CALIBRATION_FILE.stat().st_mtime
        if mtime == calibration_mtime:
            return calibration_offsets

        with open(IMU_CALIBRATION_FILE, 'r') as f:
            data = json.load(f)

        calibration_offsets['heel_offset'] = float(data.get('heel_offset', 0.0))
        calibration_offsets['pitch_offset'] = float(data.get('pitch_offset', 0.0))
        calibration_offsets['invert_heel'] = bool(data.get('invert_heel', False))
        calibration_offsets['invert_pitch'] = bool(data.get('invert_pitch', False))
        calibration_offsets['swap_axes'] = bool(data.get('swap_axes', False))
        calibration_mtime = mtime
        logger.info(f"Loaded calibration: heel_offset={calibration_offsets['heel_offset']:.2f}°, "
                   f"pitch_offset={calibration_offsets['pitch_offset']:.2f}°, "
                   f"invert_heel={calibration_offsets['invert_heel']}, "
                   f"invert_pitch={calibration_offsets['invert_pitch']}, "
                   f"swap_axes={calibration_offsets['swap_axes']}")
    except Exception as e:
        logger.warning(f"Failed to load calibration: {e}")

    return calibration_offsets


def apply_calibration(heel, pitch):
    """Apply calibration offsets, inversions, and axis swap to heel and pitch values."""
    cal = load_calibration_offsets()
    # Swap axes first if needed (IMU mounted rotated 90°)
    if cal['swap_axes']:
        heel, pitch = pitch, heel
    # Apply inversion (before offset)
    if cal['invert_heel']:
        heel = -heel
    if cal['invert_pitch']:
        pitch = -pitch
    # Then apply offset
    calibrated_heel = heel - cal['heel_offset']
    calibrated_pitch = pitch - cal['pitch_offset']
    return calibrated_heel, calibrated_pitch


def update_imu_status(heading, pitch, heel, quat, accel, accuracy, connected=True):
    """Write current IMU data to status file for dashboard."""
    try:
        i, j, k, real = quat if quat else (None, None, None, None)
        ax, ay, az = accel if accel else (None, None, None)

        # Apply calibration offsets
        cal_heel, cal_pitch = apply_calibration(heel, pitch) if heel is not None and pitch is not None else (None, None)
        cal = load_calibration_offsets()

        # Calculate total linear acceleration magnitude
        accel_magnitude = None
        if ax is not None and ay is not None and az is not None:
            accel_magnitude = math.sqrt(ax*ax + ay*ay + az*az)

        status = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'connected': connected,
            # Calibrated Euler angles (most useful for sailors)
            'heading_deg': round(heading, 2) if heading is not None else None,
            'pitch_deg': round(cal_pitch, 2) if cal_pitch is not None else None,
            'heel_deg': round(cal_heel, 2) if cal_heel is not None else None,
            # Raw (uncalibrated) values
            'raw_pitch_deg': round(pitch, 2) if pitch is not None else None,
            'raw_heel_deg': round(heel, 2) if heel is not None else None,
            # Current calibration offsets and inversions
            'heel_offset': cal['heel_offset'],
            'pitch_offset': cal['pitch_offset'],
            'invert_heel': cal['invert_heel'],
            'invert_pitch': cal['invert_pitch'],
            'swap_axes': cal['swap_axes'],
            # Quaternion components (for advanced users)
            'quat_i': round(i, 6) if i is not None else None,
            'quat_j': round(j, 6) if j is not None else None,
            'quat_k': round(k, 6) if k is not None else None,
            'quat_real': round(real, 6) if real is not None else None,
            # Linear acceleration (gravity removed)
            'accel_x_mps2': round(ax, 4) if ax is not None else None,
            'accel_y_mps2': round(ay, 4) if ay is not None else None,
            'accel_z_mps2': round(az, 4) if az is not None else None,
            'accel_magnitude_mps2': round(accel_magnitude, 4) if accel_magnitude is not None else None,
            # Accuracy
            'accuracy_rad': round(accuracy, 4) if accuracy is not None and accuracy != '' else None,
        }
        with open(IMU_STATUS_FILE, 'w') as f:
            json.dump(status, f)
    except Exception as e:
        logger.warning(f"Failed to update status file: {e}")


def clear_imu_status():
    """Clear status file when disconnected."""
    try:
        status = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'connected': False,
        }
        with open(IMU_STATUS_FILE, 'w') as f:
            json.dump(status, f)
    except Exception:
        pass


def run(config):
    imu_config = config['imu']
    sample_rate = imu_config['sample_rate_hz']
    sample_interval = 1.0 / sample_rate

    logger.info(f"Initializing BNO085 on I2C bus {imu_config['i2c_bus']} "
                f"at address 0x{imu_config['i2c_address']:02X}")

    # Initialize I2C at 400kHz for BNO085
    i2c = busio.I2C(board.SCL, board.SDA, frequency=imu_config['i2c_frequency'])

    retries = 0
    bno = None
    while bno is None and running:
        try:
            bno = BNO08X_I2C(i2c, address=imu_config['i2c_address'])
            logger.info("BNO085 connected")
        except Exception as e:
            retries += 1
            if retries % 5 == 0:
                logger.warning(f"BNO085 not responding: {e}")
            time.sleep(2)

    if not running:
        return

    # Enable sensor reports
    bno.enable_feature(adafruit_bno08x.BNO_REPORT_ROTATION_VECTOR)
    bno.enable_feature(adafruit_bno08x.BNO_REPORT_LINEAR_ACCELERATION)
    logger.info("Sensor reports enabled: rotation_vector, linear_acceleration")

    data_dir = get_data_dir(config)
    csv_file, writer = create_csv_writer(data_dir)
    rows_written = 0

    try:
        while running:
            loop_start = time.monotonic()

            try:
                quat = bno.quaternion
                linear_accel = bno.linear_acceleration
            except Exception as e:
                logger.warning(f"Read error: {e}")
                time.sleep(0.1)
                continue

            if quat is None or any(q is None for q in quat):
                time.sleep(0.01)
                continue

            i, j, k, real = quat
            heading, pitch, heel = quaternion_to_euler(i, j, k, real)

            ax, ay, az = linear_accel if linear_accel else (0, 0, 0)

            # Get accuracy estimate if available
            accuracy = ''
            try:
                accuracy = bno.quaternion_accuracy
            except Exception:
                pass

            utc_now = datetime.now(timezone.utc).isoformat()
            writer.writerow([
                utc_now,
                f"{i:.6f}", f"{j:.6f}", f"{k:.6f}", f"{real:.6f}",
                f"{heading:.2f}",
                f"{pitch:.2f}",
                f"{heel:.2f}",
                f"{ax:.4f}", f"{ay:.4f}", f"{az:.4f}",
                f"{accuracy}" if accuracy != '' else '',
            ])
            rows_written += 1

            # Update status file for dashboard (every 10th sample to reduce I/O)
            if rows_written % 10 == 0:
                update_imu_status(heading, pitch, heel, quat, linear_accel, accuracy)

            if rows_written % 500 == 0:
                csv_file.flush()

            # Rotate at midnight
            current_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            if data_dir.parent.name != current_date:
                csv_file.close()
                data_dir = get_data_dir(config)
                csv_file, writer = create_csv_writer(data_dir)
                rows_written = 0

            # Maintain sample rate
            elapsed = time.monotonic() - loop_start
            sleep_time = sample_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        csv_file.close()
        clear_imu_status()
        logger.info(f"IMU service stopped. {rows_written} rows written.")


if __name__ == '__main__':
    config = load_config()
    if not config['imu']['enabled']:
        logger.info("IMU disabled in config, exiting")
        sys.exit(0)
    run(config)
