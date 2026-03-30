#!/usr/bin/env python3
"""
SailFrames GPS Service
Reads u-blox ZED-F9P via USB serial, logs position/speed/heading at 10Hz.
"""

import os
import sys
import csv
import time
import signal
import logging
from datetime import datetime, timezone
from pathlib import Path

import json
import serial
import pynmea2
import yaml

# Status file for dashboard
GPS_STATUS_FILE = Path('/tmp/sailframes-gps-status.json')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [GPS] %(levelname)s %(message)s'
)
logger = logging.getLogger('sailframes.gps')

# Global flag for clean shutdown
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
    """Create today's GPS data directory."""
    base = config['storage']['data_dir']
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    data_dir = Path(base) / today / 'gps'
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def create_csv_writer(data_dir):
    """Create a new CSV file with headers."""
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    filepath = data_dir / f'track_{timestamp}.csv'
    f = open(filepath, 'w', newline='')
    writer = csv.writer(f)
    writer.writerow([
        'utc_time',           # ISO 8601 UTC timestamp
        'latitude',           # Decimal degrees
        'longitude',          # Decimal degrees
        'altitude_m',         # Meters above sea level
        'speed_knots',        # Speed over ground in knots
        'speed_mps',          # Speed over ground in m/s
        'course_deg',         # Course over ground in degrees true
        'fix_quality',        # 0=none, 1=GPS, 2=DGPS, 4=RTK fixed, 5=RTK float
        'satellites',         # Number of satellites in use
        'hdop',               # Horizontal dilution of precision
        'gps_timestamp',      # Raw GPS time string
    ])
    logger.info(f"Logging to {filepath}")
    return f, writer


def parse_gga(msg):
    """Extract data from GGA sentence."""
    return {
        'latitude': msg.latitude if msg.latitude else None,
        'longitude': msg.longitude if msg.longitude else None,
        'altitude_m': msg.altitude if msg.altitude else None,
        'fix_quality': int(msg.gps_qual) if msg.gps_qual else 0,
        'satellites': int(msg.num_sats) if msg.num_sats else 0,
        'hdop': float(msg.horizontal_dil) if msg.horizontal_dil else None,
        'gps_timestamp': str(msg.timestamp) if msg.timestamp else '',
    }


def parse_rmc(msg):
    """Extract data from RMC sentence."""
    return {
        'latitude': msg.latitude if msg.latitude else None,
        'longitude': msg.longitude if msg.longitude else None,
        'speed_knots': float(msg.spd_over_grnd) if msg.spd_over_grnd else None,
        'course_deg': float(msg.true_course) if msg.true_course else None,
        'gps_timestamp': str(msg.timestamp) if msg.timestamp else '',
    }


def update_gps_status(current):
    """Write current GPS state to status file for dashboard."""
    try:
        fix_quality = current.get('fix_quality', 0)
        fix_types = {0: 'No Fix', 1: 'GPS', 2: 'DGPS', 4: 'RTK Fixed', 5: 'RTK Float'}
        fix_type = fix_types.get(fix_quality, f'Unknown ({fix_quality})')

        # HDOP quality rating
        hdop = current.get('hdop')
        if hdop is None:
            hdop_rating = 'N/A'
        elif hdop < 1:
            hdop_rating = 'Ideal'
        elif hdop < 2:
            hdop_rating = 'Excellent'
        elif hdop < 5:
            hdop_rating = 'Good'
        elif hdop < 10:
            hdop_rating = 'Moderate'
        else:
            hdop_rating = 'Poor'

        # Calculate accuracy estimate (rough: HDOP * 2.5m for consumer GPS)
        accuracy_cm = int(hdop * 250) if hdop else None

        # Speed in different units
        speed_knots = current.get('speed_knots')
        speed_mph = round(speed_knots * 1.15078, 1) if speed_knots else None

        status = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'latitude': current.get('latitude'),
            'longitude': current.get('longitude'),
            'altitude_m': current.get('altitude_m'),
            'speed_knots': round(speed_knots, 2) if speed_knots else None,
            'speed_mph': speed_mph,
            'course_deg': current.get('course_deg'),
            'fix_quality': fix_quality,
            'fix_type': fix_type,
            'satellites': current.get('satellites', 0),
            'hdop': hdop,
            'hdop_rating': hdop_rating,
            'accuracy_cm': accuracy_cm,
        }

        with open(GPS_STATUS_FILE, 'w') as f:
            json.dump(status, f)
    except Exception as e:
        logger.warning(f"Failed to update GPS status file: {e}")


def run(config):
    """Main GPS acquisition loop."""
    gps_config = config['gps']
    device = gps_config['device']
    baud = gps_config['baud_rate']

    logger.info(f"Connecting to GPS on {device} at {baud} baud")

    # Wait for GPS device to appear
    retries = 0
    while not os.path.exists(device) and running:
        retries += 1
        if retries % 10 == 0:
            logger.warning(f"GPS device {device} not found, waiting...")
        time.sleep(1)

    if not running:
        return

    ser = serial.Serial(device, baud, timeout=1)
    logger.info(f"GPS connected on {device}")

    data_dir = get_data_dir(config)
    csv_file, writer = create_csv_writer(data_dir)

    # Current state - merge GGA and RMC data
    current = {
        'latitude': None, 'longitude': None, 'altitude_m': None,
        'speed_knots': None, 'speed_mps': None, 'course_deg': None,
        'fix_quality': 0, 'satellites': 0, 'hdop': None,
        'gps_timestamp': '',
    }

    last_write = 0
    write_interval = 1.0 / gps_config['update_rate_hz']
    rows_written = 0

    try:
        while running:
            try:
                line = ser.readline().decode('ascii', errors='replace').strip()
            except (serial.SerialException, OSError) as e:
                logger.error(f"Serial read error: {e}")
                time.sleep(1)
                continue

            if not line.startswith('$'):
                continue

            try:
                msg = pynmea2.parse(line)
            except pynmea2.ParseError:
                continue

            # Update current state from parsed sentence
            if isinstance(msg, pynmea2.types.talker.GGA):
                gga = parse_gga(msg)
                current.update({k: v for k, v in gga.items() if v is not None})

            elif isinstance(msg, pynmea2.types.talker.RMC):
                rmc = parse_rmc(msg)
                current.update({k: v for k, v in rmc.items() if v is not None})

                # Compute m/s from knots
                if current['speed_knots'] is not None:
                    current['speed_mps'] = round(current['speed_knots'] * 0.514444, 3)

            # Write at configured rate
            now = time.monotonic()
            if now - last_write >= write_interval and current['latitude'] is not None:
                utc_now = datetime.now(timezone.utc).isoformat()
                writer.writerow([
                    utc_now,
                    f"{current['latitude']:.8f}" if current['latitude'] else '',
                    f"{current['longitude']:.8f}" if current['longitude'] else '',
                    current['altitude_m'] or '',
                    current['speed_knots'] or '',
                    current['speed_mps'] or '',
                    current['course_deg'] or '',
                    current['fix_quality'],
                    current['satellites'],
                    current['hdop'] or '',
                    current['gps_timestamp'],
                ])
                rows_written += 1
                last_write = now

                # Update status file for dashboard (every 10th write to reduce I/O)
                if rows_written % 10 == 0:
                    update_gps_status(current)

                # Flush periodically
                if rows_written % 100 == 0:
                    csv_file.flush()

                # Rotate file at midnight UTC
                current_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                if data_dir.parent.name != current_date:
                    csv_file.close()
                    data_dir = get_data_dir(config)
                    csv_file, writer = create_csv_writer(data_dir)
                    rows_written = 0

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        csv_file.close()
        ser.close()
        logger.info(f"GPS service stopped. {rows_written} rows written.")


if __name__ == '__main__':
    config = load_config()
    if not config['gps']['enabled']:
        logger.info("GPS disabled in config, exiting")
        sys.exit(0)
    run(config)
