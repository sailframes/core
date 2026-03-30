#!/usr/bin/env python3
"""
SailFrames GPS Service
Reads u-blox ZED-F9P via USB serial, logs position/speed/heading at 10Hz.
Also logs raw UBX data for RTKLib post-processing.
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


def get_ubx_dir(config):
    """Create today's UBX data directory."""
    base = config['storage']['data_dir']
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    ubx_dir = Path(base) / today / 'ubx'
    ubx_dir.mkdir(parents=True, exist_ok=True)
    return ubx_dir


def create_ubx_file(ubx_dir):
    """Create a new UBX raw data file."""
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    filepath = ubx_dir / f'raw_{timestamp}.ubx'
    f = open(filepath, 'wb')
    logger.info(f"Logging raw UBX to {filepath}")
    return f, filepath


def extract_nmea_lines(buffer):
    """Extract complete NMEA sentences from buffer.
    Returns (list of lines, remaining buffer)."""
    lines = []
    while b'\r\n' in buffer:
        idx = buffer.index(b'\r\n')
        line_bytes = buffer[:idx]
        buffer = buffer[idx + 2:]
        try:
            line = line_bytes.decode('ascii', errors='replace').strip()
            if line.startswith('$'):
                lines.append(line)
        except:
            pass
    return lines, buffer


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


# Constellation and signal band tracking
CONSTELLATION_NAMES = {
    'GP': 'GPS',
    'GL': 'GLONASS',
    'GA': 'Galileo',
    'GB': 'BeiDou',
    'GQ': 'QZSS',
    'GI': 'NavIC',
}

# Signal IDs per constellation (from NMEA 4.11)
SIGNAL_BANDS = {
    'GP': {0: 'L1', 1: 'L1', 6: 'L2', 7: 'L2'},  # GPS: 1=L1 C/A, 6=L2 CL, 7=L2 CM
    'GL': {0: 'L1', 1: 'L1', 3: 'L2'},            # GLONASS: 1=L1 OF, 3=L2 OF
    'GA': {0: 'E1', 2: 'E1', 3: 'E5a', 4: 'E5b', 7: 'E5'},  # Galileo
    'GB': {0: 'B1', 1: 'B1', 2: 'B1C', 5: 'B2a'}, # BeiDou
    'GQ': {0: 'L1', 1: 'L1', 5: 'L5'},            # QZSS
}


def parse_gsv(line, constellation_data):
    """
    Parse GSV sentence for satellite constellation and signal info.
    Updates constellation_data dict in place.

    GSV format: $xxGSV,numMsg,msgNum,numSV,{prn,elev,azim,snr}*4,signalId*checksum
    """
    try:
        # Extract constellation from talker ID (first 2 chars after $)
        talker = line[1:3]
        if talker not in CONSTELLATION_NAMES:
            return

        constellation = CONSTELLATION_NAMES[talker]

        # Parse the sentence
        parts = line.split('*')[0].split(',')
        if len(parts) < 4:
            return

        num_sv = int(parts[3]) if parts[3] else 0

        # Get signal ID (last field before checksum, if present)
        signal_id = 0
        if len(parts) > 4:
            # Check if last field is a signal ID (single digit)
            last_field = parts[-1]
            if last_field.isdigit() and len(last_field) == 1:
                signal_id = int(last_field)

        # Determine signal band
        signal_band = SIGNAL_BANDS.get(talker, {}).get(signal_id, f'L{signal_id}')

        # Count satellites with SNR (actually tracking)
        sats_tracking = 0
        for i in range(4, len(parts) - 1, 4):
            if i + 3 < len(parts):
                snr = parts[i + 3]
                if snr and snr.isdigit() and int(snr) > 0:
                    sats_tracking += 1

        # Update constellation data
        if constellation not in constellation_data:
            constellation_data[constellation] = {
                'total': 0,
                'tracking': 0,
                'signals': set()
            }

        # Only update total from first message in sequence
        msg_num = int(parts[2]) if parts[2] else 1
        if msg_num == 1:
            constellation_data[constellation]['total'] = num_sv
            constellation_data[constellation]['tracking'] = 0

        constellation_data[constellation]['tracking'] += sats_tracking
        constellation_data[constellation]['signals'].add(signal_band)

    except (ValueError, IndexError) as e:
        pass  # Ignore malformed GSV sentences


def parse_gsa(line, sats_used_by_constellation):
    """
    Parse GSA sentence to track which satellites are used for fix per constellation.
    GSA format: $xxGSA,mode,fix,prn1-prn12,pdop,hdop,vdop[,systemId]*cs

    For GNGSA (multi-GNSS), system ID is the last field:
    1=GPS, 2=GLONASS, 3=Galileo, 4=BeiDou, 5=QZSS

    Updates sats_used_by_constellation dict: {'GPS': 10, 'Galileo': 7, ...}
    """
    try:
        talker = line[1:3]
        talker_map = {
            'GP': 'GPS',
            'GL': 'GLONASS',
            'GA': 'Galileo',
            'GB': 'BeiDou',
            'GQ': 'QZSS',
        }

        parts = line.split('*')[0].split(',')
        if len(parts) < 18:
            return

        fix_type = int(parts[2]) if parts[2] else 0
        if fix_type < 2:  # No fix (1=no fix, 2=2D, 3=3D)
            return

        # Determine constellation
        if talker == 'GN':
            # GNGSA: system ID is last field (index 18 for standard GSA with 12 PRN slots)
            # Fields: msg,mode,fix,prn*12,pdop,hdop,vdop,sysid
            system_id = parts[-1] if len(parts) > 18 else parts[17] if len(parts) > 17 else ''
            system_map = {'1': 'GPS', '2': 'GLONASS', '3': 'Galileo', '4': 'BeiDou', '5': 'QZSS'}
            constellation = system_map.get(system_id, None)
            if not constellation:
                return
        else:
            constellation = talker_map.get(talker)
            if not constellation:
                return

        # Count PRNs used (fields 3-14, indices 3 to 14 inclusive)
        prn_count = 0
        for i in range(3, 15):
            if i < len(parts) and parts[i] and parts[i].isdigit():
                prn_count += 1

        if prn_count > 0:
            sats_used_by_constellation[constellation] = prn_count

    except (ValueError, IndexError):
        pass


def update_gps_status(current, constellation_data=None, sats_used_by_constellation=None, usb_connected=True, receiving_nmea=True):
    """Write current GPS state to status file for dashboard."""
    try:
        fix_quality = current.get('fix_quality', 0)
        has_fix = fix_quality > 0 and current.get('latitude') is not None
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

        # Build constellation summary
        constellations = {}
        all_signals = set()
        sats_used = sats_used_by_constellation or {}
        if constellation_data:
            for name, data in constellation_data.items():
                constellations[name] = {
                    'in_view': data.get('total', 0),
                    'in_use': sats_used.get(name, 0),
                    'tracking': data.get('tracking', 0),
                    'signals': sorted(list(data.get('signals', set())))
                }
                all_signals.update(data.get('signals', set()))

        status = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'usb_connected': usb_connected,
            'receiving_nmea': receiving_nmea,
            'has_fix': has_fix,
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
            'constellations': constellations,
            'signals_in_use': sorted(list(all_signals)),
        }

        with open(GPS_STATUS_FILE, 'w') as f:
            json.dump(status, f)
    except Exception as e:
        logger.warning(f"Failed to update GPS status file: {e}")


def find_gps_device(preferred_device, baud_rate):
    """
    Find GPS device, checking preferred device first then scanning USB ports.
    Returns (device_path, serial_connection) or (None, None) if not found.
    """
    import glob

    # Try preferred device first
    if os.path.exists(preferred_device):
        try:
            ser = serial.Serial(preferred_device, baud_rate, timeout=1)
            # Check if it's actually a GPS by reading a line
            line = ser.readline().decode('ascii', errors='replace')
            if line.startswith('$G'):
                logger.info(f"GPS found on preferred device {preferred_device}")
                return preferred_device, ser
            ser.close()
        except Exception as e:
            logger.debug(f"Preferred device {preferred_device} failed: {e}")

    # Scan all USB serial ports
    patterns = ['/dev/ttyACM*', '/dev/ttyUSB*']
    for pattern in patterns:
        for port in sorted(glob.glob(pattern)):
            if port == preferred_device:
                continue  # Already tried
            try:
                ser = serial.Serial(port, baud_rate, timeout=1)
                line = ser.readline().decode('ascii', errors='replace')
                if line.startswith('$G'):
                    logger.info(f"GPS found on {port} (scanned)")
                    return port, ser
                ser.close()
            except Exception as e:
                logger.debug(f"Port {port} failed: {e}")

    return None, None


def run(config):
    """Main GPS acquisition loop."""
    gps_config = config['gps']
    preferred_device = gps_config['device']
    baud = gps_config['baud_rate']

    logger.info(f"Looking for GPS (preferred: {preferred_device}, baud: {baud})")

    # Try to find GPS device, with retries
    device = None
    ser = None
    retries = 0
    while device is None and running:
        device, ser = find_gps_device(preferred_device, baud)
        if device is None:
            retries += 1
            if retries % 10 == 0:
                logger.warning(f"GPS not found on any port, retrying...")
            time.sleep(1)

    if not running:
        return

    logger.info(f"GPS connected on {device}")

    data_dir = get_data_dir(config)
    csv_file, writer = create_csv_writer(data_dir)

    # Create UBX raw data file for RTKLib post-processing
    ubx_dir = get_ubx_dir(config)
    ubx_file, ubx_filepath = create_ubx_file(ubx_dir)
    ubx_bytes_written = 0

    # Current state - merge GGA and RMC data
    current = {
        'latitude': None, 'longitude': None, 'altitude_m': None,
        'speed_knots': None, 'speed_mps': None, 'course_deg': None,
        'fix_quality': 0, 'satellites': 0, 'hdop': None,
        'gps_timestamp': '',
    }

    # Constellation tracking (reset every second)
    constellation_data = {}
    sats_used_by_constellation = {}  # Track which constellations are used for fix

    last_write = 0
    last_status_update = 0
    last_ubx_rotate = time.time()
    ubx_rotate_interval = 3600  # Rotate UBX file hourly
    write_interval = 1.0 / gps_config['update_rate_hz']
    rows_written = 0
    nmea_received = False  # Track if we're receiving any NMEA data
    nmea_buffer = b''  # Buffer for extracting NMEA lines from raw data

    try:
        while running:
            # Read raw bytes from serial
            try:
                if ser.in_waiting > 0:
                    raw_data = ser.read(ser.in_waiting)
                else:
                    raw_data = ser.read(256)  # Blocking read with timeout
            except (serial.SerialException, OSError) as e:
                logger.error(f"Serial read error: {e}")
                update_gps_status(current, constellation_data, sats_used_by_constellation, usb_connected=False, receiving_nmea=False)
                time.sleep(1)
                continue

            if not raw_data:
                # Timeout - update status periodically
                now = time.monotonic()
                if now - last_status_update >= 1.0:
                    update_gps_status(current, constellation_data, sats_used_by_constellation, usb_connected=True, receiving_nmea=nmea_received)
                    last_status_update = now
                continue

            # Write raw data to UBX file (includes NMEA + UBX binary)
            ubx_file.write(raw_data)
            ubx_bytes_written += len(raw_data)

            # Flush UBX file periodically
            if ubx_bytes_written % 10000 < len(raw_data):
                ubx_file.flush()

            # Extract NMEA lines from buffer
            nmea_buffer += raw_data
            lines, nmea_buffer = extract_nmea_lines(nmea_buffer)

            # Prevent buffer from growing too large
            if len(nmea_buffer) > 4096:
                nmea_buffer = nmea_buffer[-2048:]

            # Process each NMEA line
            for line in lines:
                nmea_received = True

                # Parse GSV sentences for constellation info (before pynmea2 parsing)
                if 'GSV' in line:
                    parse_gsv(line, constellation_data)
                    continue  # GSV sentences don't need further processing

                # Parse GSA sentences for satellites used per constellation
                if 'GSA' in line:
                    parse_gsa(line, sats_used_by_constellation)
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

            # Update status periodically even without fix (for dashboard)
            now = time.monotonic()
            if now - last_status_update >= 1.0:
                update_gps_status(current, constellation_data, sats_used_by_constellation, usb_connected=True, receiving_nmea=nmea_received)
                last_status_update = now

            # Write at configured rate (only if we have a position fix)
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
                    update_gps_status(current, constellation_data, sats_used_by_constellation, usb_connected=True, receiving_nmea=True)
                    last_status_update = time.monotonic()
                    # Reset constellation data periodically to get fresh counts
                    constellation_data = {}
                    sats_used_by_constellation = {}

                # Flush CSV periodically
                if rows_written % 100 == 0:
                    csv_file.flush()

            # Rotate files at midnight UTC or UBX hourly
            current_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            now_time = time.time()

            # Rotate CSV at midnight
            if data_dir.parent.name != current_date:
                csv_file.close()
                data_dir = get_data_dir(config)
                csv_file, writer = create_csv_writer(data_dir)
                rows_written = 0

            # Rotate UBX hourly or at midnight
            if now_time - last_ubx_rotate >= ubx_rotate_interval or ubx_dir.parent.name != current_date:
                ubx_file.close()
                ubx_dir = get_ubx_dir(config)
                ubx_file, ubx_filepath = create_ubx_file(ubx_dir)
                last_ubx_rotate = now_time
                logger.info(f"UBX logged {ubx_bytes_written / (1024*1024):.1f} MB, rotated to new file")
                ubx_bytes_written = 0

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        csv_file.close()
        ubx_file.close()
        ser.close()
        logger.info(f"GPS service stopped. {rows_written} CSV rows, {ubx_bytes_written / (1024*1024):.1f} MB UBX data.")


if __name__ == '__main__':
    config = load_config()
    if not config['gps']['enabled']:
        logger.info("GPS disabled in config, exiting")
        sys.exit(0)
    run(config)
