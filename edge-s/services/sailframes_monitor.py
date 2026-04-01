#!/usr/bin/env python3
"""
SailFrames Monitor Service
Monitors system health: CPU temp, battery level, disk usage, sensor status.
Provides a local web dashboard on port 8080.
Triggers clean shutdown on low battery.
"""

import os
import sys
import csv
import json
import time
import signal
import logging
import threading
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psutil
from flask import Flask, jsonify, render_template_string, request, send_file
import yaml

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [MONITOR] %(levelname)s %(message)s'
)
logger = logging.getLogger('sailframes.monitor')

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


def get_cpu_temp():
    """Read Pi CPU temperature."""
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            return round(int(f.read().strip()) / 1000, 1)
    except Exception:
        return None


def get_battery_info():
    """
    Read battery status from Waveshare UPS HAT (D) via I2C, or return external
    power bank status if no HAT is detected.
    - INA219 at 0x43: voltage/current monitoring
    - Battery percentage calculated from output voltage:
      - Full (charging): ~4.2V output
      - Empty (shutdown): ~3.4V output
    """
    INA219_ADDR = 0x43
    REG_BUS_VOLTAGE = 0x02
    REG_SHUNT_VOLTAGE = 0x01
    SHUNT_OHMS = 0.1

    # Output voltage range observed on UPS HAT (D)
    VOUT_FULL = 4.2   # Output voltage when fully charged
    VOUT_EMPTY = 3.4  # Output voltage when battery depleted

    try:
        import smbus2
        bus = smbus2.SMBus(1)

        # Try to read from INA219 - will fail if no HAT present
        try:
            raw_bus = bus.read_word_data(INA219_ADDR, REG_BUS_VOLTAGE)
        except OSError:
            bus.close()
            # No HAT detected - using external USB power bank
            return {
                'type': 'external',
                'name': 'USB Power Bank',
                'capacity': '50Ah',
                'voltage': None,
                'percent': None,
                'current_ma': None,
                'charging': None,
            }

        # Read output voltage from INA219
        raw_bus = ((raw_bus & 0xFF) << 8) | ((raw_bus >> 8) & 0xFF)
        voltage = (raw_bus >> 3) * 0.004

        # Read current from INA219 shunt
        raw_shunt = bus.read_word_data(INA219_ADDR, REG_SHUNT_VOLTAGE)
        raw_shunt = ((raw_shunt & 0xFF) << 8) | ((raw_shunt >> 8) & 0xFF)
        if raw_shunt > 32767:
            raw_shunt -= 65536
        shunt_mv = raw_shunt * 0.01
        current_ma = shunt_mv / SHUNT_OHMS

        bus.close()

        # Calculate battery percentage from output voltage
        percent = (voltage - VOUT_EMPTY) / (VOUT_FULL - VOUT_EMPTY) * 100.0
        percent = max(0, min(100, percent))

        # Charging detection: current is negative when USB powers system and charges battery
        # Positive current = discharging (battery powering Pi)
        # Negative current = charging (USB powering Pi + charging battery)
        charging = current_ma < 0

        # Estimate remaining/charging time (UPS HAT D has 2x 18650)
        # Conservative estimate: 3000mAh usable (accounting for cutoff voltage and efficiency)
        BATTERY_CAPACITY_MAH = 3000
        # Minimum realistic current draw for Pi 5 with services running
        MIN_DISCHARGE_MA = 200
        remaining_hours = None
        remaining_str = None
        empty_time = None
        charge_hours = None
        charge_str = None
        full_time = None

        from datetime import timedelta

        if not charging and current_ma > 10:  # Discharging with meaningful current
            # Use the higher of actual current or minimum realistic draw
            # (low readings may indicate USB-C hybrid mode)
            effective_current = max(current_ma, MIN_DISCHARGE_MA)
            remaining_mah = (percent / 100.0) * BATTERY_CAPACITY_MAH
            remaining_hours = remaining_mah / effective_current
            hours = int(remaining_hours)
            minutes = int((remaining_hours - hours) * 60)
            remaining_str = f"{hours}h {minutes}m"

            # Calculate estimated empty time
            empty_dt = datetime.now() + timedelta(hours=remaining_hours)
            empty_time = empty_dt.strftime('%I:%M %p')

        elif charging and current_ma < -10 and percent < 100:  # Charging with meaningful current
            # current_ma is negative when charging, so use absolute value
            charge_current = abs(current_ma)
            remaining_to_full = ((100 - percent) / 100.0) * BATTERY_CAPACITY_MAH
            charge_hours = remaining_to_full / charge_current
            hours = int(charge_hours)
            minutes = int((charge_hours - hours) * 60)
            charge_str = f"{hours}h {minutes}m"

            # Calculate estimated full time
            full_dt = datetime.now() + timedelta(hours=charge_hours)
            full_time = full_dt.strftime('%I:%M %p')

        return {
            'type': 'hat',
            'voltage': round(voltage, 2),
            'percent': round(percent, 1),
            'current_ma': round(current_ma, 0),
            'charging': charging,
            'remaining_hours': round(remaining_hours, 1) if remaining_hours else None,
            'remaining_str': remaining_str,
            'empty_time': empty_time,
            'charge_hours': round(charge_hours, 1) if charge_hours else None,
            'charge_str': charge_str,
            'full_time': full_time,
        }
    except Exception as e:
        logger.debug(f"Battery read error: {e}")
        # Assume external power bank if we can't read battery
        return {
            'type': 'external',
            'name': 'USB Power Bank',
            'capacity': '50Ah',
            'voltage': None,
            'percent': None,
            'current_ma': None,
            'charging': None,
        }


def get_disk_usage(mount_point):
    """Get disk usage for data storage."""
    try:
        usage = psutil.disk_usage(mount_point)
        return {
            'total_gb': round(usage.total / (1024**3), 1),
            'used_gb': round(usage.used / (1024**3), 1),
            'free_gb': round(usage.free / (1024**3), 1),
            'percent': usage.percent,
        }
    except Exception:
        return {'total_gb': 0, 'used_gb': 0, 'free_gb': 0, 'percent': 0}


def get_service_resources():
    """Get CPU and memory usage for SailFrames services."""
    services = []
    try:
        # Get list of sailframes services
        result = subprocess.run(
            ['systemctl', 'list-units', 'sailframes*', '--no-pager', '-q', '--plain'],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            unit = line.split()[0] if line.split() else None
            if not unit:
                continue

            # Get PID
            pid_result = subprocess.run(
                ['systemctl', 'show', '-p', 'MainPID', '--value', unit],
                capture_output=True, text=True, timeout=2
            )
            pid = pid_result.stdout.strip()
            if not pid or pid == '0':
                continue

            # Get CPU and memory usage
            try:
                proc = psutil.Process(int(pid))
                cpu = proc.cpu_percent(interval=0.1)
                mem = proc.memory_percent()
                name = unit.replace('sailframes-', '').replace('.service', '')
                services.append({
                    'name': name,
                    'unit': unit,
                    'pid': int(pid),
                    'cpu': round(cpu, 1),
                    'mem': round(mem, 1),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        # Sort by CPU usage descending
        services.sort(key=lambda x: x['cpu'], reverse=True)
    except Exception as e:
        logger.debug(f"Service resources error: {e}")

    return services


def get_system_power_stats():
    """Get system power-related stats."""
    stats = {
        'temp': None,
        'voltage': None,
        'cpu_percent': psutil.cpu_percent(interval=0.1),
        'estimated_watts': None,
    }

    try:
        # Get CPU temperature
        result = subprocess.run(['vcgencmd', 'measure_temp'], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            # Parse "temp=57.6'C"
            temp_str = result.stdout.strip().replace("temp=", "").replace("'C", "")
            stats['temp'] = float(temp_str)
    except Exception:
        pass

    try:
        # Get core voltage
        result = subprocess.run(['vcgencmd', 'measure_volts'], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            # Parse "volt=0.9070V"
            volt_str = result.stdout.strip().replace("volt=", "").replace("V", "")
            stats['voltage'] = float(volt_str)
    except Exception:
        pass

    # Estimate power: Pi 5 idle ~3W, full load ~8W, plus peripherals
    # Rough estimate based on CPU usage
    base_power = 3.0  # Idle Pi 5
    cpu_power = (stats['cpu_percent'] / 100) * 5.0  # Up to 5W more at full CPU
    peripheral_power = 3.0  # Display, camera, sensors
    stats['estimated_watts'] = round(base_power + cpu_power + peripheral_power, 1)

    return stats


# Power logging state
_power_log_file = None
_power_log_date = None


def log_power_stats(data_dir):
    """Log power stats to CSV file for later analysis."""
    global _power_log_file, _power_log_date

    today = datetime.now().strftime('%Y-%m-%d')
    power_dir = Path(data_dir) / today / 'power'

    # Rotate file daily
    if _power_log_date != today or _power_log_file is None:
        if _power_log_file:
            try:
                _power_log_file.close()
            except Exception:
                pass
        power_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filepath = power_dir / f'power_{timestamp}.csv'
        _power_log_file = open(filepath, 'w', buffering=1)  # Line buffered
        # Write header
        _power_log_file.write('timestamp,cpu_temp,dps310_temp,cpu_percent,voltage,est_watts,')
        _power_log_file.write('camera_cpu,camera_mem,monitor_cpu,monitor_mem,')
        _power_log_file.write('gps_cpu,gps_mem,imu_cpu,imu_mem,pressure_cpu,pressure_mem,wind_cpu,wind_mem\n')
        _power_log_date = today
        logger.info(f"Power logging started: {filepath}")

    try:
        # Get current stats
        system = get_system_power_stats()
        services = get_service_resources()
        pressure = get_pressure_status() or {}

        # Build service lookup
        svc_data = {s['name']: s for s in services}

        # Get DPS310 temperature
        dps310_temp = pressure.get('temperature_c', 0) or 0

        # Format row
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        row = [
            ts,
            f"{system.get('temp', 0):.1f}",
            f"{dps310_temp:.1f}",
            f"{system.get('cpu_percent', 0):.1f}",
            f"{system.get('voltage', 0):.3f}",
            f"{system.get('estimated_watts', 0):.1f}",
        ]

        # Add per-service stats
        for name in ['camera-cockpit', 'monitor', 'gps', 'imu', 'pressure', 'wind']:
            svc = svc_data.get(name, {})
            row.append(f"{svc.get('cpu', 0):.1f}")
            row.append(f"{svc.get('mem', 0):.1f}")

        _power_log_file.write(','.join(row) + '\n')
    except Exception as e:
        logger.debug(f"Power logging error: {e}")


def get_power_log_files(data_dir, date=None):
    """Get list of power log files for a date."""
    if date is None:
        date = datetime.now().strftime('%Y-%m-%d')
    power_dir = Path(data_dir) / date / 'power'
    if not power_dir.exists():
        return []
    return sorted(power_dir.glob('power_*.csv'), reverse=True)


def get_power_history(data_dir, date=None, limit=500):
    """Read recent power log entries."""
    files = get_power_log_files(data_dir, date)
    if not files:
        return []

    rows = []
    for filepath in files:
        try:
            with open(filepath, 'r') as f:
                lines = f.readlines()
                # Skip header, get data rows
                for line in lines[1:]:
                    if line.strip():
                        rows.append(line.strip().split(','))
        except Exception:
            pass
        if len(rows) >= limit:
            break

    # Return most recent entries (last N rows)
    return rows[-limit:]


def check_service_status(service_name):
    """Check if a systemd service is running."""
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', service_name],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() == 'active'
    except Exception:
        return False


def get_camera_status(camera_id):
    """Read camera status from status file written by camera service."""
    status_file = Path(f'/tmp/sailframes-camera-{camera_id}-status.json')
    try:
        if not status_file.exists():
            return None

        with open(status_file, 'r') as f:
            data = json.load(f)

        # Check if data is stale (older than 30 seconds)
        if data.get('timestamp'):
            ts = datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00'))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > 30:
                return None

        return {
            'mode': data.get('mode', 'unknown'),
            'photo_count': data.get('photo_count', 0),
            'maneuver_count': data.get('maneuver_count', 0),
            'recording_maneuver': data.get('recording_maneuver', False),
        }
    except Exception:
        return None


def get_gps_status():
    """Read current GPS data from status file written by GPS service."""
    status_file = Path('/tmp/sailframes-gps-status.json')
    try:
        if not status_file.exists():
            return {'connected': False, 'status': 'no_status_file'}

        with open(status_file, 'r') as f:
            data = json.load(f)

        # Check if data is stale (older than 5 seconds for 10Hz GPS)
        if data.get('timestamp'):
            ts = datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00'))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            data['age_seconds'] = round(age, 1)
            if age > 5:
                data['connected'] = False
                data['status'] = 'stale'
            else:
                data['connected'] = True
                data['status'] = 'ok'
        else:
            data['connected'] = False
            data['status'] = 'no_timestamp'

        return data
    except Exception as e:
        return {'connected': False, 'status': f'error: {e}'}


def get_wind_status():
    """Read current wind data from status file written by wind service."""
    status_file = Path('/tmp/sailframes-wind-status.json')
    try:
        if not status_file.exists():
            return {'connected': False, 'status': 'no_status_file'}

        with open(status_file, 'r') as f:
            data = json.load(f)

        # Check if data is stale (older than 10 seconds)
        if data.get('timestamp'):
            ts = datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00'))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            data['age_seconds'] = round(age, 1)
            if age > 10:
                data['connected'] = False
                data['status'] = 'stale'
            else:
                data['status'] = 'ok'
        else:
            data['connected'] = False
            data['status'] = 'no_timestamp'

        return data
    except Exception as e:
        return {'connected': False, 'status': f'error: {e}'}


def get_imu_status():
    """Read current IMU data from status file written by IMU service."""
    status_file = Path('/tmp/sailframes-imu-status.json')
    try:
        if not status_file.exists():
            return None

        with open(status_file, 'r') as f:
            data = json.load(f)

        # Check if data is stale (older than 2 seconds for 50Hz sensor)
        if data.get('timestamp'):
            ts = datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00'))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > 2:
                data['connected'] = False

        return data
    except Exception:
        return None


def get_pressure_status():
    """Read current pressure data from status file written by pressure service."""
    status_file = Path('/tmp/sailframes-pressure-status.json')
    try:
        if not status_file.exists():
            return None

        with open(status_file, 'r') as f:
            data = json.load(f)

        # Check if data is stale (older than 5 seconds for 1Hz sensor)
        if data.get('timestamp'):
            ts = datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00'))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > 5:
                data['connected'] = False

        return data
    except Exception:
        return None


def get_latest_gps():
    """Get latest GPS data from most recent CSV file with detailed metrics."""
    try:
        data_dir = Path('/mnt/sailframes-data')
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        gps_dir = data_dir / today / 'gps'

        if not gps_dir.exists():
            return None

        # Find most recent CSV
        csv_files = sorted(gps_dir.glob('track_*.csv'), reverse=True)
        if not csv_files:
            return None

        # Read last lines for stats
        with open(csv_files[0], 'r') as f:
            lines = f.readlines()
            if len(lines) < 2:
                return None
            last_line = lines[-1].strip()
            # Get recent lines for stats (last 60 = ~1 minute at 1Hz)
            recent_lines = lines[-61:] if len(lines) > 60 else lines[1:]

        # Parse CSV line
        parts = last_line.split(',')
        if len(parts) < 10:
            return None

        # Fix quality descriptions with detailed explanations
        fix_info = {
            0: {'name': 'No Fix', 'accuracy_m': None, 'desc': 'No satellite signal received'},
            1: {'name': 'GPS (SPS)', 'accuracy_m': 2.5, 'desc': 'Standard Positioning Service - civilian GPS'},
            2: {'name': 'DGPS', 'accuracy_m': 1.0, 'desc': 'Differential GPS - uses SBAS corrections (WAAS/EGNOS)'},
            4: {'name': 'RTK Fixed', 'accuracy_m': 0.02, 'desc': 'Real-Time Kinematic - centimeter-level accuracy'},
            5: {'name': 'RTK Float', 'accuracy_m': 0.5, 'desc': 'RTK converging - decimeter accuracy, improving'},
            6: {'name': 'Dead Reckoning', 'accuracy_m': 10.0, 'desc': 'Estimated position based on last known + motion'},
        }

        fix_quality = int(parts[7]) if parts[7] else 0
        speed_knots = float(parts[4]) if parts[4] else 0
        hdop = float(parts[9]) if parts[9] else 99
        satellites = int(parts[8]) if parts[8] else 0

        # Calculate estimated accuracy
        # Base accuracy depends on fix type, multiplied by HDOP
        base_accuracy = fix_info.get(fix_quality, {}).get('accuracy_m', 5.0)
        if base_accuracy:
            estimated_accuracy_m = base_accuracy * hdop
            estimated_accuracy_cm = int(estimated_accuracy_m * 100)
        else:
            estimated_accuracy_m = None
            estimated_accuracy_cm = None

        # HDOP quality rating
        if hdop < 1:
            hdop_rating = 'Ideal'
            hdop_desc = 'Highest precision, satellites well distributed'
        elif hdop < 2:
            hdop_rating = 'Excellent'
            hdop_desc = 'Very high precision positioning'
        elif hdop < 5:
            hdop_rating = 'Good'
            hdop_desc = 'Good for most navigation'
        elif hdop < 10:
            hdop_rating = 'Moderate'
            hdop_desc = 'Fair accuracy, some satellites blocked'
        elif hdop < 20:
            hdop_rating = 'Fair'
            hdop_desc = 'Low accuracy, poor satellite geometry'
        else:
            hdop_rating = 'Poor'
            hdop_desc = 'Position unreliable'

        # Calculate statistics from recent data
        recent_hdops = []
        recent_sats = []
        for line in recent_lines:
            if line.startswith('utc') or line.startswith('20'):
                p = line.strip().split(',')
                if len(p) >= 10 and p[9]:
                    try:
                        recent_hdops.append(float(p[9]))
                        recent_sats.append(int(p[8]))
                    except:
                        pass

        avg_hdop = sum(recent_hdops) / len(recent_hdops) if recent_hdops else hdop
        min_hdop = min(recent_hdops) if recent_hdops else hdop
        max_hdop = max(recent_hdops) if recent_hdops else hdop
        avg_sats = sum(recent_sats) / len(recent_sats) if recent_sats else satellites

        # Get total points logged today
        total_points = len(lines) - 1  # Minus header

        return {
            # Position
            'latitude': float(parts[1]) if parts[1] else None,
            'longitude': float(parts[2]) if parts[2] else None,
            'altitude_m': float(parts[3]) if parts[3] else None,

            # Speed (multiple units)
            'speed_knots': round(speed_knots, 2),
            'speed_mph': round(speed_knots * 1.15078, 2),
            'speed_kmh': round(speed_knots * 1.852, 2),
            'speed_mps': round(speed_knots * 0.514444, 3),

            # Course
            'course_deg': float(parts[6]) if parts[6] else None,

            # Fix info
            'fix_quality': fix_quality,
            'fix_type': fix_info.get(fix_quality, {}).get('name', f'Unknown ({fix_quality})'),
            'fix_desc': fix_info.get(fix_quality, {}).get('desc', ''),

            # Satellites
            'satellites': satellites,
            'satellites_avg': round(avg_sats, 1),

            # Precision
            'hdop': round(hdop, 2),
            'hdop_rating': hdop_rating,
            'hdop_desc': hdop_desc,
            'hdop_avg': round(avg_hdop, 2),
            'hdop_min': round(min_hdop, 2),
            'hdop_max': round(max_hdop, 2),

            # Estimated accuracy
            'accuracy_m': round(estimated_accuracy_m, 2) if estimated_accuracy_m else None,
            'accuracy_cm': estimated_accuracy_cm,
            'accuracy_rating': hdop_rating,

            # Stats
            'total_points_today': total_points,
            'sample_rate_hz': 10,  # From config
            'timestamp': parts[0],
            'gps_time': parts[10] if len(parts) > 10 else '',
        }
    except Exception as e:
        logger.debug(f"GPS read error: {e}")
        return None


# ── System state (shared between monitor thread and web server) ──
system_state = {
    'device_id': '',
    'uptime_sec': 0,
    'cpu_temp_c': None,
    'cpu_percent': 0,
    'ram_percent': 0,
    'battery': {},
    'disk': {},
    'gps': {},
    'services': {},
    'last_update': '',
}


def monitor_loop(config):
    """Background thread that collects system stats."""
    global system_state, running

    monitor_config = config['monitor']
    interval = monitor_config['stats_interval_sec']
    shutdown_percent = monitor_config['battery_shutdown_percent']
    data_mount = config['storage']['ssd_mount']

    system_state['device_id'] = config['device']['id']

    # Service status check interval (less frequent to avoid file descriptor exhaustion)
    SERVICE_CHECK_INTERVAL = 60  # seconds
    last_service_check = 0

    while running:
        system_state['cpu_temp_c'] = get_cpu_temp()
        system_state['cpu_percent'] = psutil.cpu_percent(interval=1)
        system_state['ram_percent'] = psutil.virtual_memory().percent
        system_state['battery'] = get_battery_info()
        system_state['disk'] = get_disk_usage(data_mount)
        system_state['gps'] = get_latest_gps() or {}
        system_state['gps_status'] = get_gps_status() or {}
        system_state['wind'] = get_wind_status() or {}
        system_state['imu'] = get_imu_status() or {}
        system_state['pressure'] = get_pressure_status() or {}
        system_state['uptime_sec'] = int(time.monotonic())
        system_state['last_update'] = datetime.now(timezone.utc).isoformat()

        # Check service status less frequently (every 60s instead of every 10s)
        # This reduces subprocess calls from 60k/day to 10k/day, preventing fd exhaustion
        now = time.monotonic()
        if now - last_service_check >= SERVICE_CHECK_INTERVAL:
            last_service_check = now
            system_state['services'] = {
                'gps': check_service_status('sailframes-gps'),
                'imu': check_service_status('sailframes-imu'),
                'pressure': check_service_status('sailframes-pressure'),
                'wind': check_service_status('sailframes-wind'),
            }
            # Track cameras separately for individual control
            # Include mode and counts from camera status files
            cockpit_running = check_service_status('sailframes-camera-cockpit')
            sails_running = check_service_status('sailframes-camera-sails')
            cockpit_status = get_camera_status('cockpit') if cockpit_running else None
            sails_status = get_camera_status('sails') if sails_running else None

            system_state['cameras'] = {
                'cockpit': cockpit_running,
                'sails': sails_running,
                'cockpit_status': cockpit_status,
                'sails_status': sails_status,
            }
            # Netdata monitoring (optional, can be toggled)
            system_state['netdata'] = check_service_status('netdata')

            # Log power stats every minute for analysis
            log_power_stats(data_mount)

        # Low battery shutdown - only if HAT battery is present (not external power bank)
        # (voltage > 5V means batteries installed, not just HAT with no/dead batteries)
        battery_type = system_state['battery'].get('type')
        battery_pct = system_state['battery'].get('percent')
        battery_voltage = system_state['battery'].get('voltage')
        if (battery_type != 'external' and battery_pct is not None and battery_voltage is not None
            and battery_voltage > 5.0 and battery_pct < shutdown_percent):
            logger.warning(f"Battery at {battery_pct}% ({battery_voltage}V)! Initiating clean shutdown.")
            subprocess.run(['sudo', 'shutdown', '-h', 'now'])
            running = False
            return

        # Log summary periodically
        logger.info(
            f"CPU={system_state['cpu_temp_c']}°C "
            f"RAM={system_state['ram_percent']}% "
            f"Disk={system_state['disk'].get('free_gb', '?')}GB free "
            f"Batt={battery_pct or '?'}%"
        )

        time.sleep(interval)


# ── Web Dashboard ──
app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>SailFrames - {{ state.device_id }}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, sans-serif; background: #0a1628; color: #e0e8f0; padding: 16px; }
        h1 { color: #4fc3f7; margin-bottom: 16px; font-size: 24px; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        .card { background: #1a2a40; border-radius: 8px; padding: 14px; }
        .card h2 { font-size: 13px; color: #78909c; text-transform: uppercase; margin-bottom: 8px; }
        .value { font-size: 28px; font-weight: 700; color: #fff; }
        .unit { font-size: 14px; color: #78909c; }
        .status { display: inline-block; font-size: 11px; font-weight: 600; padding: 2px 6px; border-radius: 4px; margin-right: 8px; min-width: 36px; text-align: center; }
        .status.on { background: #1976d2; color: #fff; }
        .status.off { background: #455a64; color: #90a4ae; }
        .sub { font-size: 13px; color: #90a4ae; margin-top: 6px; }
        .charging { color: #1976d2; }
        .discharging { color: #ff9800; }
        .services { margin-top: 12px; }
        .svc-row { padding: 6px 0; font-size: 14px; border-bottom: 1px solid #233; }
        .updated { font-size: 11px; color: #546e7a; margin-top: 12px; text-align: center; }
        .header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
        .header h1 { margin: 0; }
        .header-right { display: flex; align-items: center; gap: 12px; }
        .clock { font-size: 20px; font-weight: 700; color: #4fc3f7; font-family: monospace; }
        .conn-indicators { display: flex; gap: 8px; }
        .conn-badge { font-size: 11px; font-weight: 600; padding: 4px 8px; border-radius: 4px; }
        .conn-ok { background: #1b5e20; color: #a5d6a7; }
        .conn-warn { background: #e65100; color: #ffcc80; }
        .conn-off { background: #37474f; color: #78909c; }
        /* Recording control styles */
        .recording-card { background: #1a2a40; border-radius: 8px; padding: 16px; margin-bottom: 12px; }
        .recording-card.recording { border: 2px solid #f44336; background: linear-gradient(135deg, #1a2a40 0%, #2d1a1a 100%); }
        .recording-btn { padding: 8px 16px; font-size: 13px; font-weight: 600; border: none; border-radius: 6px; cursor: pointer; transition: all 0.2s; }
        .recording-btn.start { background: #4caf50; color: white; }
        .recording-btn.start:hover { background: #66bb6a; }
        .recording-btn.stop { background: #f44336; color: white; }
        .recording-btn.stop:hover { background: #ef5350; }
        .recording-btn:disabled { background: #455a64; cursor: wait; }
        .sensor-status-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 12px; }
        .sensor-status { padding: 8px 12px; border-radius: 6px; background: #0d1929; text-align: center; }
        .sensor-status .name { font-size: 11px; color: #78909c; text-transform: uppercase; margin-bottom: 4px; }
        .sensor-status .indicator { font-size: 12px; font-weight: 600; }
        .sensor-status.recording .indicator { color: #4caf50; }
        .sensor-status.not-recording .indicator { color: #78909c; }
        .sensor-status.error .indicator { color: #f44336; }
        .rec-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; }
        .rec-dot.active { background: #f44336; animation: pulse 1s infinite; }
        .rec-dot.inactive { background: #455a64; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
    </style>
</head>
<body>
    <div class="header">
        <h1>⛵ SailFrames {{ state.device_id }}</h1>
        <div class="header-right">
            <div class="conn-indicators">
                <span id="gps-indicator" class="conn-badge {% if state.gps_status.connected %}conn-ok{% elif state.services.gps %}conn-warn{% else %}conn-off{% endif %}" title="GPS status">📍 GPS{% if state.services.gps and not state.gps_status.connected %} ⚠{% endif %}</span>
                <span id="wind-indicator" class="conn-badge {% if state.wind.connected %}conn-ok{% elif state.services.wind %}conn-warn{% else %}conn-off{% endif %}" title="Wind status">💨 Wind{% if state.services.wind and not state.wind.connected %} ⚠{% endif %}</span>
            </div>
            <div class="clock" id="clock"></div>
        </div>
    </div>

    <!-- Recording Control -->
    <div id="recording-card" class="recording-card">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
            <div>
                <h2 style="margin: 0; font-size: 16px; color: #fff;">Recording Control</h2>
                <div id="recording-status-text" style="font-size: 13px; color: #78909c; margin-top: 4px;">Sensors idle</div>
            </div>
            <div style="display: flex; align-items: center; gap: 12px;">
                <span id="rec-indicator" style="display: none;"><span class="rec-dot active"></span><span style="color: #f44336; font-weight: 600;">REC</span></span>
                <button id="recording-btn" class="recording-btn start" onclick="toggleRecording()">Start Recording</button>
            </div>
        </div>
        <div class="sensor-status-grid">
            <div id="sensor-gps" class="sensor-status not-recording">
                <div class="name">GPS</div>
                <div class="indicator"><span class="rec-dot inactive"></span>Off</div>
            </div>
            <div id="sensor-imu" class="sensor-status not-recording">
                <div class="name">IMU</div>
                <div class="indicator"><span class="rec-dot inactive"></span>Off</div>
            </div>
            <div id="sensor-wind" class="sensor-status not-recording">
                <div class="name">Wind</div>
                <div class="indicator"><span class="rec-dot inactive"></span>Off</div>
            </div>
            <div id="sensor-pressure" class="sensor-status not-recording">
                <div class="name">Pressure</div>
                <div class="indicator"><span class="rec-dot inactive"></span>Off</div>
            </div>
            <div id="sensor-camera-cockpit" class="sensor-status not-recording">
                <div class="name">Cam Cockpit</div>
                <div class="indicator"><span class="rec-dot inactive"></span>Off</div>
            </div>
            <div id="sensor-camera-sails" class="sensor-status not-recording">
                <div class="name">Cam Sails</div>
                <div class="indicator"><span class="rec-dot inactive"></span>Off</div>
            </div>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>CPU Temp</h2>
            <div class="value"><span id="cpu-temp">{{ state.cpu_temp_c or '—' }}</span><span class="unit">°C</span></div>
        </div>
        <div class="card">
            <h2>Disk Free</h2>
            <div class="value"><span id="disk-free">{{ state.disk.free_gb or '—' }}</span><span class="unit">GB</span></div>
        </div>
        <div class="card">
            <h2>RAM</h2>
            <div class="value"><span id="ram-percent">{{ state.ram_percent or '—' }}</span><span class="unit">%</span></div>
        </div>
    </div>

    <!-- GPS Section -->
    <div id="gps-section" class="card" style="margin-top: 12px;">
        <div id="gps-connected" style="display: {{ 'block' if state.gps_status and state.gps_status.connected else 'none' }};">
            <h2>📍 GPS — <span id="gps-fix-type">{{ state.gps.fix_type if state.gps else '' }}</span> (<span id="gps-sats">{{ state.gps.satellites if state.gps else 0 }}</span> sats) · ±<span id="gps-accuracy">{{ state.gps.accuracy_cm if state.gps else '?' }}</span>cm</h2>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 8px;">
                <div>
                    <div style="font-size: 11px; color: #78909c;">POSITION</div>
                    <div style="font-size: 14px; font-weight: 600;"><span id="gps-position">{{ "%.6f, %.6f"|format(state.gps.latitude, state.gps.longitude) if state.gps and state.gps.latitude else '—' }}</span></div>
                </div>
                <div>
                    <div style="font-size: 11px; color: #78909c;">ACCURACY</div>
                    <div style="font-size: 14px; font-weight: 600;">±<span id="gps-accuracy2">{{ state.gps.accuracy_cm if state.gps else '?' }}</span>cm (<span id="gps-hdop-rating">{{ state.gps.hdop_rating if state.gps else '' }}</span>)</div>
                </div>
                <div>
                    <div style="font-size: 11px; color: #78909c;">SPEED</div>
                    <div style="font-size: 14px; font-weight: 600;"><span id="gps-speed">{{ state.gps.speed_knots if state.gps else 0 }}</span> kts · <span id="gps-speed-mph">{{ state.gps.speed_mph if state.gps else 0 }}</span> mph</div>
                </div>
                <div>
                    <div style="font-size: 11px; color: #78909c;">ALTITUDE</div>
                    <div style="font-size: 14px; font-weight: 600;"><span id="gps-altitude">{{ state.gps.altitude_m|round(1) if state.gps and state.gps.altitude_m else '—' }}</span> m</div>
                </div>
            </div>
            <!-- Constellation Info -->
            <div id="gps-constellations" style="margin-top: 12px; padding-top: 10px; border-top: 1px solid #37474f;">
                {% set total_in_view = state.gps_status.constellations.values() | map(attribute='in_view') | sum if state.gps_status and state.gps_status.constellations else 0 %}
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <div>
                        <span style="font-size: 13px; color: #4caf50; font-weight: 600;" id="gps-sats-in-use">{{ state.gps_status.satellites or 0 }}</span>
                        <span style="font-size: 11px; color: #78909c;"> in use for fix</span>
                    </div>
                    <div>
                        <span style="font-size: 13px; color: #4fc3f7; font-weight: 600;" id="gps-sats-in-view">{{ total_in_view }}</span>
                        <span style="font-size: 11px; color: #78909c;"> in view</span>
                    </div>
                </div>
                <div style="font-size: 10px; color: #78909c; margin-bottom: 4px;">CONSTELLATIONS <span style="color: #4caf50;">● in use</span> / <span style="color: #4fc3f7;">in view</span></div>
                <div id="gps-constellation-grid" style="display: flex; flex-wrap: wrap; gap: 6px; font-size: 11px;">
                    {% if state.gps_status and state.gps_status.constellations %}
                    {% for name, data in state.gps_status.constellations.items() %}
                    <div style="background: #263238; padding: 3px 6px; border-radius: 4px; {% if data.in_use %}border-left: 2px solid #4caf50;{% endif %}">
                        <span style="color: {% if data.in_use %}#4caf50{% else %}#4fc3f7{% endif %}; font-weight: {% if data.in_use %}600{% else %}400{% endif %};">{{ name }}</span>:
                        {% if data.in_use %}<span style="color: #4caf50;">{{ data.in_use }}</span>/{% endif %}<span style="color: #4fc3f7;">{{ data.in_view }}</span>
                        {% if data.signals %}<span style="color: #546e7a; font-size: 9px;">({{ data.signals|join(', ') }})</span>{% endif %}
                    </div>
                    {% endfor %}
                    {% else %}
                    <div style="color: #78909c;">—</div>
                    {% endif %}
                </div>
                <div style="margin-top: 6px;">
                    <span style="font-size: 10px; color: #78909c;">SIGNAL BANDS: </span>
                    <span id="gps-signals" style="font-size: 11px; color: #81c784;">{{ state.gps_status.signals_in_use|join(', ') if state.gps_status and state.gps_status.signals_in_use else '—' }}</span>
                </div>
            </div>
            <div style="margin-top: 10px; display: flex; gap: 16px; font-size: 12px;">
                <a href="/gps" style="color: #4fc3f7;">📊 GPS Details</a>
                <a id="gps-map-link" href="https://www.google.com/maps?q={{ state.gps.latitude if state.gps else 0 }},{{ state.gps.longitude if state.gps else 0 }}" target="_blank" style="color: #4fc3f7;">🗺 Open Map ↗</a>
            </div>
        </div>
        <div id="gps-disconnected" style="display: {{ 'none' if state.gps_status and state.gps_status.connected else 'block' }};">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <h2 style="margin: 0;">📍 GPS</h2>
                    <div id="gps-status-msg" style="font-size: 13px; margin-top: 4px;">
                        {% if not state.gps_status.connected and state.services.gps %}
                        <span style="color: #f44336; font-weight: 600;">⚠️ DISCONNECTED — Check USB cable!</span>
                        <div style="color: #78909c; font-size: 11px; margin-top: 2px;">Service running but no data received</div>
                        {% elif state.gps_status.connected and not state.gps_status.has_fix and state.services.gps %}
                        <span style="color: #ff9800; font-weight: 600;">⚠️ NO FIX — Check antenna!</span>
                        {% set total_in_view = state.gps_status.constellations.values() | map(attribute='in_view') | sum if state.gps_status.constellations else 0 %}
                        <div style="color: #78909c; font-size: 11px; margin-top: 2px;">{{ total_in_view }} satellites in view, {{ state.gps_status.satellites or 0 }} tracking</div>
                        {% elif state.services.gps %}
                        <span style="color: #4caf50;">{{ state.gps_status.fix_type or 'Connected' }}</span>
                        {% else %}
                        <span style="color: #78909c;">GPS service not running</span>
                        {% endif %}
                    </div>
                </div>
                <button id="btn-gps-restart" onclick="restartGPS()" style="background: #1976d2; color: white; border: none; padding: 8px 12px; border-radius: 6px; font-size: 12px; cursor: pointer;">🔄 Restart</button>
            </div>
            <div style="margin-top: 8px;"><a href="/gps" style="color: #4fc3f7; font-size: 12px;">📊 GPS Details</a></div>
        </div>
    </div>

    <!-- Wind Section -->
    <div id="wind-section" class="card" style="margin-top: 12px;">
        <div id="wind-connected" style="display: {{ 'block' if state.wind and state.wind.connected else 'none' }};">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <h2 style="margin: 0;">💨 Wind — <span id="wind-device-name">{{ state.wind.device_name if state.wind else 'Connected' }}</span></h2>
                <button id="btn-wind-restart" onclick="restartWind()" style="background: #455a64; color: white; border: none; padding: 6px 12px; border-radius: 6px; font-size: 12px; cursor: pointer;">🔄 Restart</button>
            </div>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 8px;">
                <div>
                    <div style="font-size: 11px; color: #78909c;">APPARENT WIND SPEED</div>
                    <div style="font-size: 24px; font-weight: 700; color: #4fc3f7;"><span id="wind-speed">{{ "%.1f"|format(state.wind.speed_knots or 0) if state.wind else '0.0' }}</span> <span style="font-size: 14px; font-weight: 400;">kts</span></div>
                </div>
                <div>
                    <div style="font-size: 11px; color: #78909c;">APPARENT WIND ANGLE</div>
                    <div style="font-size: 24px; font-weight: 700; color: #4fc3f7;"><span id="wind-angle">{{ state.wind.angle_deg if state.wind else 0 }}</span>°</div>
                </div>
                <div id="wind-compass-row" style="display: {{ 'block' if state.wind and state.wind.compass_deg is not none else 'none' }};">
                    <div style="font-size: 11px; color: #78909c;">COMPASS HEADING</div>
                    <div style="font-size: 14px; font-weight: 600;"><span id="wind-compass">{{ "%.1f"|format(state.wind.compass_deg) if state.wind and state.wind.compass_deg else '' }}</span>°</div>
                </div>
                <div id="wind-temp-row" style="display: {{ 'block' if state.wind and state.wind.temperature is not none else 'none' }};">
                    <div style="font-size: 11px; color: #78909c;">TEMPERATURE</div>
                    <div style="font-size: 14px; font-weight: 600;"><span id="wind-temp">{{ "%.1f"|format(state.wind.temperature) if state.wind and state.wind.temperature else '' }}</span>°C</div>
                </div>
                <div id="wind-battery-row" style="display: {{ 'block' if state.wind and state.wind.battery is not none else 'none' }};">
                    <div style="font-size: 11px; color: #78909c;">BATTERY</div>
                    <div id="wind-battery-val" style="font-size: 14px; font-weight: 600; color: {{ '#f44336' if state.wind and state.wind.low_power_warning else 'inherit' }};"><span id="wind-battery">{{ state.wind.battery if state.wind else '' }}</span>%<span id="wind-battery-warn" style="display: {{ 'inline' if state.wind and state.wind.low_power_warning else 'none' }};"> ⚠️</span></div>
                </div>
                <div id="wind-firmware-row" style="display: {{ 'block' if state.wind and state.wind.firmware else 'none' }}; grid-column: span 2;">
                    <div style="font-size: 11px; color: #78909c;">DEVICE INFO</div>
                    <div style="font-size: 12px; color: #90a4ae;"><span id="wind-firmware">{{ state.wind.model if state.wind and state.wind.model else '' }}{% if state.wind and state.wind.firmware %} (fw {{ state.wind.firmware }}){% endif %}</span></div>
                </div>
            </div>
        </div>
        <div id="wind-disconnected" style="display: {{ 'none' if state.wind and state.wind.connected else 'block' }};">
            <h2>💨 Wind</h2>
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <div id="wind-status-msg">
                    {% if state.wind and state.wind.status == 'stale' and state.services.wind %}
                    <div style="color: #f44336; font-weight: 600;">⚠️ DISCONNECTED</div>
                    {% elif state.services.wind %}
                    <div style="color: #ff9800;">Searching for sensor...</div>
                    {% else %}
                    <div style="color: #78909c; font-style: italic;">Wind service not running</div>
                    {% endif %}
                </div>
                <div style="display: flex; gap: 8px;">
                    <button onclick="restartWind()" style="background: #455a64; color: white; border: none; padding: 8px 12px; border-radius: 6px; font-size: 13px; cursor: pointer;">🔄 Restart</button>
                    <button onclick="scanBluetooth()" style="background: #1976d2; color: white; border: none; padding: 8px 16px; border-radius: 6px; font-size: 13px; cursor: pointer;">Scan Bluetooth</button>
                </div>
            </div>
        </div>
    </div>

    <!-- IMU Section -->
    {% if state.imu and state.imu.connected %}
    <div class="card" style="margin-top: 12px;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <h2 style="margin: 0;">🧭 IMU — BNO085</h2>
            <button id="btn-imu-live" onclick="toggleIMULive()" style="
                background: #455a64; color: white; border: none; padding: 6px 14px;
                border-radius: 6px; font-size: 12px; cursor: pointer;
            ">▶ Live</button>
        </div>
        <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; margin-top: 8px;">
            <div>
                <div style="font-size: 11px; color: #78909c;">HEADING</div>
                <div id="imu-heading" style="font-size: 24px; font-weight: 700; color: #4fc3f7;">{{ "%.1f"|format(state.imu.heading_deg or 0) }}°</div>
            </div>
            <div>
                <div style="font-size: 11px; color: #78909c;">HEEL</div>
                <div id="imu-heel" style="font-size: 24px; font-weight: 700; color: #4fc3f7;">
                    {{ "%.1f"|format(state.imu.heel_deg or 0) }}°
                    <span id="imu-heel-dir" style="font-size: 12px; font-weight: 400; color: #78909c;">{{ 'STBD' if state.imu.heel_deg and state.imu.heel_deg > 0 else 'PORT' if state.imu.heel_deg and state.imu.heel_deg < 0 else '' }}</span>
                </div>
            </div>
            <div>
                <div style="font-size: 11px; color: #78909c;">PITCH</div>
                <div id="imu-pitch" style="font-size: 24px; font-weight: 700; color: #4fc3f7;">{{ "%.1f"|format(state.imu.pitch_deg or 0) }}°
                    <span id="imu-pitch-dir" style="font-size: 12px; font-weight: 400; color: #78909c;">{{ 'BOW UP' if state.imu.pitch_deg and state.imu.pitch_deg > 0 else 'BOW DN' if state.imu.pitch_deg and state.imu.pitch_deg < 0 else '' }}</span>
                </div>
            </div>
        </div>
        <!-- Linear Acceleration -->
        <div style="display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 8px; margin-top: 12px; padding-top: 12px; border-top: 1px solid #233;">
            <div>
                <div style="font-size: 11px; color: #78909c;">ACCEL X (FWD)</div>
                <div id="imu-accel-x" style="font-size: 14px; font-weight: 600;">{{ "%.2f"|format(state.imu.accel_x_mps2 or 0) }} <span style="color: #78909c;">m/s²</span></div>
            </div>
            <div>
                <div style="font-size: 11px; color: #78909c;">ACCEL Y (STBD)</div>
                <div id="imu-accel-y" style="font-size: 14px; font-weight: 600;">{{ "%.2f"|format(state.imu.accel_y_mps2 or 0) }} <span style="color: #78909c;">m/s²</span></div>
            </div>
            <div>
                <div style="font-size: 11px; color: #78909c;">ACCEL Z (DOWN)</div>
                <div id="imu-accel-z" style="font-size: 14px; font-weight: 600;">{{ "%.2f"|format(state.imu.accel_z_mps2 or 0) }} <span style="color: #78909c;">m/s²</span></div>
            </div>
            <div>
                <div style="font-size: 11px; color: #78909c;">TOTAL ACCEL</div>
                <div id="imu-accel-total" style="font-size: 14px; font-weight: 600;">{{ "%.2f"|format(state.imu.accel_magnitude_mps2 or 0) }} <span style="color: #78909c;">m/s²</span></div>
            </div>
        </div>
        <!-- Quaternion (advanced) -->
        <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 12px; padding-top: 12px; border-top: 1px solid #233;">
            <div>
                <div style="font-size: 10px; color: #546e7a;">QUAT I</div>
                <div id="imu-quat-i" style="font-size: 12px; font-family: monospace;">{{ "%.4f"|format(state.imu.quat_i or 0) }}</div>
            </div>
            <div>
                <div style="font-size: 10px; color: #546e7a;">QUAT J</div>
                <div id="imu-quat-j" style="font-size: 12px; font-family: monospace;">{{ "%.4f"|format(state.imu.quat_j or 0) }}</div>
            </div>
            <div>
                <div style="font-size: 10px; color: #546e7a;">QUAT K</div>
                <div id="imu-quat-k" style="font-size: 12px; font-family: monospace;">{{ "%.4f"|format(state.imu.quat_k or 0) }}</div>
            </div>
            <div>
                <div style="font-size: 10px; color: #546e7a;">QUAT REAL</div>
                <div id="imu-quat-real" style="font-size: 12px; font-family: monospace;">{{ "%.4f"|format(state.imu.quat_real or 0) }}</div>
            </div>
        </div>
        {% if state.imu.accuracy_rad is not none %}
        <div style="margin-top: 8px; font-size: 11px; color: #546e7a;">
            Accuracy: {{ "%.4f"|format(state.imu.accuracy_rad) }} rad
        </div>
        {% endif %}
        <!-- Calibration -->
        <div style="margin-top: 12px; padding-top: 12px; border-top: 1px solid #233;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                <div style="font-size: 12px; color: #78909c;">
                    {% if state.imu.heel_offset or state.imu.pitch_offset %}
                    Calibrated: heel {{ "%.1f"|format(state.imu.heel_offset or 0) }}°, pitch {{ "%.1f"|format(state.imu.pitch_offset or 0) }}° offset
                    {% else %}
                    Not calibrated (raw values)
                    {% endif %}
                </div>
                <button onclick="calibrateIMU()" id="btn-imu-cal" style="
                    background: #1976d2;
                    color: white;
                    border: none;
                    padding: 8px 16px;
                    border-radius: 6px;
                    font-size: 13px;
                    cursor: pointer;
                ">Zero Heel/Pitch</button>
            </div>
            <!-- Inversion toggles -->
            <div style="display: flex; gap: 12px; align-items: center;">
                <span style="font-size: 12px; color: #78909c;">Invert:</span>
                <button id="btn-invert-heel" onclick="toggleInvert('heel')" style="
                    background: {% if state.imu.invert_heel %}#ff9800{% else %}#455a64{% endif %};
                    color: white;
                    border: none;
                    padding: 6px 12px;
                    border-radius: 4px;
                    font-size: 12px;
                    cursor: pointer;
                ">Heel {% if state.imu.invert_heel %}✓{% endif %}</button>
                <button id="btn-invert-pitch" onclick="toggleInvert('pitch')" style="
                    background: {% if state.imu.invert_pitch %}#ff9800{% else %}#455a64{% endif %};
                    color: white;
                    border: none;
                    padding: 6px 12px;
                    border-radius: 4px;
                    font-size: 12px;
                    cursor: pointer;
                ">Pitch {% if state.imu.invert_pitch %}✓{% endif %}</button>
                <span style="color: #546e7a; margin: 0 4px;">|</span>
                <button id="btn-swap-axes" onclick="toggleSwapAxes()" style="
                    background: {% if state.imu.swap_axes %}#9c27b0{% else %}#455a64{% endif %};
                    color: white;
                    border: none;
                    padding: 6px 12px;
                    border-radius: 4px;
                    font-size: 12px;
                    cursor: pointer;
                ">Swap H↔P {% if state.imu.swap_axes %}✓{% endif %}</button>
            </div>
        </div>
    </div>
    {% else %}
    <div class="card" style="margin-top: 12px;">
        <h2>🧭 IMU</h2>
        <div style="color: #78909c; font-style: italic;">
            {% if state.services.imu %}
            Initializing BNO085...
            {% else %}
            IMU service not running
            {% endif %}
        </div>
    </div>
    {% endif %}

    <!-- Pressure Section -->
    {% if state.pressure and state.pressure.connected %}
    <div class="card" style="margin-top: 12px;">
        <h2>🌡️ Pressure — DPS310</h2>
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 8px;">
            <div>
                <div style="font-size: 11px; color: #78909c;">BAROMETRIC PRESSURE</div>
                <div style="font-size: 24px; font-weight: 700; color: #4fc3f7;">{{ "%.1f"|format(state.pressure.pressure_hpa or 0) }} <span style="font-size: 14px; font-weight: 400;">hPa</span></div>
                <div style="font-size: 12px; color: #78909c;">{{ state.pressure.pressure_inhg }} inHg</div>
            </div>
            <div>
                <div style="font-size: 11px; color: #78909c;">SEA LEVEL PRESSURE</div>
                <div style="font-size: 24px; font-weight: 700; color: #4fc3f7;">{{ "%.1f"|format(state.pressure.sea_level_hpa or 0) }} <span style="font-size: 14px; font-weight: 400;">hPa</span></div>
                <div style="font-size: 12px; color: #78909c;">{{ state.pressure.sea_level_inhg }} inHg</div>
            </div>
            <div>
                <div style="font-size: 11px; color: #78909c;">TEMPERATURE</div>
                <div style="font-size: 24px; font-weight: 700; color: #4fc3f7;">{{ "%.1f"|format(state.pressure.temperature_c or 0) }}°<span style="font-size: 14px; font-weight: 400;">C</span></div>
                <div style="font-size: 12px; color: #78909c;">{{ "%.1f"|format(state.pressure.temperature_f or 0) }}°F</div>
            </div>
            <div>
                <div style="font-size: 11px; color: #78909c;">10-MIN TREND</div>
                {% if state.pressure.trend_hpa is not none %}
                <div style="font-size: 24px; font-weight: 700; {% if state.pressure.trend_hpa > 0 %}color: #4caf50;{% elif state.pressure.trend_hpa < 0 %}color: #ff9800;{% else %}color: #4fc3f7;{% endif %}">
                    {{ '+' if state.pressure.trend_hpa > 0 else '' }}{{ "%.2f"|format(state.pressure.trend_hpa) }} <span style="font-size: 14px; font-weight: 400;">hPa</span>
                </div>
                <div style="font-size: 12px; color: #78909c;">{{ state.pressure.trend_desc or 'Calculating...' }}</div>
                {% else %}
                <div style="font-size: 14px; color: #78909c; font-style: italic;">Collecting data...</div>
                <div style="font-size: 11px; color: #546e7a;">Need 10 min of readings</div>
                {% endif %}
            </div>
        </div>
    </div>
    {% else %}
    <div class="card" style="margin-top: 12px;">
        <h2>🌡️ Pressure</h2>
        <div style="color: #78909c; font-style: italic;">
            {% if state.services.pressure %}
            Initializing DPS310...
            {% else %}
            Pressure service not running
            {% endif %}
        </div>
    </div>
    {% endif %}

    <div class="card services" style="margin-top: 12px;">
        <h2>Sensor Services</h2>
        {% for name, active in state.services.items() %}
        <div class="svc-row">
            <span class="status {{ 'on' if active else 'off' }}">{{ '✓ ON' if active else '✗ OFF' }}</span>
            {{ name }}
        </div>
        {% endfor %}
    </div>

    <!-- Camera Control -->
    <div class="card" style="margin-top: 12px;">
        <h2>Camera Control</h2>
        <div style="display: flex; flex-direction: column; gap: 12px;">
            <!-- Cockpit Camera -->
            <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">
                <div style="width: 80px; font-size: 14px; font-weight: 600;">Cockpit</div>
                <div style="flex: 1; font-size: 14px; min-width: 150px;">
                    {% if state.cameras.cockpit %}
                        {% if state.cameras.cockpit_status %}
                            {% if state.cameras.cockpit_status.mode == 'smart' %}
                            <span style="color: #4caf50;">● Smart</span>
                            <span style="color: #78909c; font-size: 12px; margin-left: 8px;">
                                📷 {{ state.cameras.cockpit_status.photo_count }}
                                🎬 {{ state.cameras.cockpit_status.maneuver_count }}
                                {% if state.cameras.cockpit_status.recording_maneuver %}<span style="color: #f44336;">⚡REC</span>{% endif %}
                            </span>
                            {% elif state.cameras.cockpit_status.mode == 'continuous' %}
                            <span style="color: #ff9800;">● Continuous</span>
                            {% elif state.cameras.cockpit_status.mode == 'photo_only' %}
                            <span style="color: #8e24aa;">● Photo</span>
                            <span style="color: #78909c; font-size: 12px; margin-left: 8px;">📷 {{ state.cameras.cockpit_status.photo_count }}</span>
                            {% else %}
                            <span style="color: #4caf50;">● Running</span>
                            {% endif %}
                        {% else %}
                        <span style="color: #4caf50;">● Running</span>
                        {% endif %}
                    {% else %}
                    <span style="color: #78909c;">○ Stopped</span>
                    {% endif %}
                </div>
                <button id="btn-snap-cockpit" onclick="snapCamera('cockpit')" style="
                    background: #455a64;
                    color: white; border: none; padding: 8px 12px; border-radius: 6px;
                    font-size: 13px; cursor: pointer;
                ">📷</button>
                <button id="btn-cockpit" onclick="toggleCamera('cockpit')" style="
                    background: {% if state.cameras.cockpit %}#c62828{% else %}#1976d2{% endif %};
                    color: white; border: none; padding: 8px 16px; border-radius: 6px;
                    font-size: 13px; font-weight: 600; cursor: pointer; min-width: 100px;
                ">{% if state.cameras.cockpit %}Stop{% else %}Start{% endif %}</button>
            </div>
            <!-- Sails Camera -->
            <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">
                <div style="width: 80px; font-size: 14px; font-weight: 600;">Sails</div>
                <div style="flex: 1; font-size: 14px; min-width: 150px;">
                    {% if state.cameras.sails %}
                        {% if state.cameras.sails_status %}
                            {% if state.cameras.sails_status.mode == 'smart' %}
                            <span style="color: #4caf50;">● Smart</span>
                            <span style="color: #78909c; font-size: 12px; margin-left: 8px;">
                                📷 {{ state.cameras.sails_status.photo_count }}
                                🎬 {{ state.cameras.sails_status.maneuver_count }}
                                {% if state.cameras.sails_status.recording_maneuver %}<span style="color: #f44336;">⚡REC</span>{% endif %}
                            </span>
                            {% elif state.cameras.sails_status.mode == 'continuous' %}
                            <span style="color: #ff9800;">● Continuous</span>
                            {% elif state.cameras.sails_status.mode == 'photo_only' %}
                            <span style="color: #8e24aa;">● Photo</span>
                            <span style="color: #78909c; font-size: 12px; margin-left: 8px;">📷 {{ state.cameras.sails_status.photo_count }}</span>
                            {% else %}
                            <span style="color: #4caf50;">● Running</span>
                            {% endif %}
                        {% else %}
                        <span style="color: #4caf50;">● Running</span>
                        {% endif %}
                    {% else %}
                    <span style="color: #78909c;">○ Stopped</span>
                    {% endif %}
                </div>
                <button id="btn-snap-sails" onclick="snapCamera('sails')" style="
                    background: #455a64;
                    color: white; border: none; padding: 8px 12px; border-radius: 6px;
                    font-size: 13px; cursor: pointer;
                ">📷</button>
                <button id="btn-sails" onclick="toggleCamera('sails')" style="
                    background: {% if state.cameras.sails %}#c62828{% else %}#1976d2{% endif %};
                    color: white; border: none; padding: 8px 16px; border-radius: 6px;
                    font-size: 13px; font-weight: 600; cursor: pointer; min-width: 100px;
                ">{% if state.cameras.sails %}Stop{% else %}Start{% endif %}</button>
            </div>
        </div>
    </div>

    <!-- WiFi Mode -->
    <div class="card" style="margin-top: 12px;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <div>
                <h2 style="margin: 0;">📶 WiFi Mode</h2>
                <div id="wifi-status" style="font-size: 12px; color: #78909c; margin-top: 4px;">Loading...</div>
            </div>
            <div style="display: flex; gap: 8px;">
                <button id="btn-wifi-client" onclick="setWiFiMode('client')" style="
                    background: #455a64; color: white; border: none; padding: 8px 14px;
                    border-radius: 6px; font-size: 13px; cursor: pointer;
                ">Client</button>
                <button id="btn-wifi-ap" onclick="setWiFiMode('ap')" style="
                    background: #455a64; color: white; border: none; padding: 8px 14px;
                    border-radius: 6px; font-size: 13px; cursor: pointer;
                ">AP Mode</button>
            </div>
        </div>
        <div id="wifi-details" style="margin-top: 8px; font-size: 12px; color: #546e7a;"></div>
    </div>

    <!-- Netdata Monitoring -->
    <div class="card" style="margin-top: 12px;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <div>
                <h2 style="margin: 0;">📊 Netdata</h2>
                <div id="netdata-status" style="font-size: 12px; color: #78909c; margin-top: 4px;">
                    {% if state.netdata %}
                    <span style="color: #4caf50;">● Running</span> — <a href="http://{{ request.host.split(':')[0] }}:19999" target="_blank" style="color: #4fc3f7;">Open Dashboard</a>
                    {% else %}
                    <span style="color: #78909c;">○ Stopped</span>
                    {% endif %}
                </div>
            </div>
            <button id="btn-netdata" onclick="toggleNetdata()" style="
                background: {% if state.netdata %}#c62828{% else %}#1976d2{% endif %};
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 6px;
                font-size: 13px;
                font-weight: 600;
                cursor: pointer;
                min-width: 80px;
            ">{% if state.netdata %}Stop{% else %}Start{% endif %}</button>
        </div>
    </div>

    <!-- System / Shutdown -->
    <div class="card" style="margin-top: 12px;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <div>
                <h2 style="margin: 0;">System</h2>
                <div style="font-size: 12px; color: #78909c; margin-top: 4px;">Uptime: {{ (state.uptime_sec // 3600) }}h {{ ((state.uptime_sec % 3600) // 60) }}m</div>
            </div>
            <button onclick="confirmShutdown()" style="
                background: #c62828;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 6px;
                font-size: 14px;
                font-weight: 600;
                cursor: pointer;
            ">Shutdown</button>
        </div>
    </div>

    <!-- Shutdown confirmation modal -->
    <div id="shutdown-modal" style="display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 100;">
        <div style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); background: #1a2a40; padding: 24px; border-radius: 8px; text-align: center; max-width: 320px;">
            <div style="font-size: 18px; margin-bottom: 12px;">Shutdown SailFrames?</div>
            <div style="color: #78909c; font-size: 13px; margin-bottom: 16px;">
                All recordings will stop. You'll need physical access to power it back on.
            </div>
            <div id="shutdown-status" style="color: #4caf50; font-size: 14px; margin-bottom: 16px; display: none;"></div>
            <div style="display: flex; gap: 12px; justify-content: center;">
                <button onclick="closeShutdownModal()" style="background: #455a64; color: white; border: none; padding: 10px 20px; border-radius: 6px; font-size: 14px; cursor: pointer;">Cancel</button>
                <button id="shutdown-btn" onclick="doShutdown()" style="background: #c62828; color: white; border: none; padding: 10px 20px; border-radius: 6px; font-size: 14px; font-weight: 600; cursor: pointer;">Shutdown</button>
            </div>
        </div>
    </div>

    <!-- Bluetooth scan modal -->
    <div id="bt-modal" style="display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 100;">
        <div style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); background: #1a2a40; padding: 24px; border-radius: 8px; max-width: 400px; width: 90%;">
            <div style="font-size: 18px; margin-bottom: 12px;">Bluetooth Wind Sensors</div>
            <div id="bt-status" style="color: #78909c; font-size: 13px; margin-bottom: 12px;">Scanning for devices...</div>
            <div id="bt-devices" style="max-height: 300px; overflow-y: auto;"></div>
            <div style="display: flex; gap: 12px; justify-content: center; margin-top: 16px;">
                <button onclick="closeBtModal()" style="background: #455a64; color: white; border: none; padding: 10px 20px; border-radius: 6px; font-size: 14px; cursor: pointer;">Close</button>
                <button id="bt-rescan" onclick="scanBluetooth()" style="background: #1976d2; color: white; border: none; padding: 10px 20px; border-radius: 6px; font-size: 14px; cursor: pointer;">Rescan</button>
            </div>
        </div>
    </div>

    <!-- Camera snapshot modal -->
    <div id="snap-modal" style="display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.9); z-index: 100;">
        <div style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); background: #1a2a40; padding: 16px; border-radius: 8px; max-width: 95vw; max-height: 95vh;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <div id="snap-title" style="font-size: 16px; font-weight: 600;">Camera Preview</div>
                <button onclick="closeSnapModal()" style="background: #455a64; color: white; border: none; padding: 6px 12px; border-radius: 4px; font-size: 14px; cursor: pointer;">✕ Close</button>
            </div>
            <div id="snap-loading" style="color: #78909c; padding: 40px; text-align: center;">Capturing...</div>
            <img id="snap-image" style="display: none; max-width: 90vw; max-height: 80vh; border-radius: 4px;" />
            <div id="snap-error" style="display: none; color: #ff5252; padding: 20px; text-align: center;"></div>
        </div>
    </div>

    <div class="updated">Updated {{ state.last_update }}</div>
    <div style="text-align: center; margin-top: 12px; display: flex; gap: 20px; justify-content: center; flex-wrap: wrap;">
        <a href="/gps" style="color: #4fc3f7; font-size: 13px;">📍 GPS Details</a>
        <a href="/battery" style="color: #4fc3f7; font-size: 13px;">Power Monitor</a>
        <a href="/video" style="color: #4fc3f7; font-size: 13px;">🎬 Video Review</a>
        <a href="/data" style="color: #4fc3f7; font-size: 13px;">🗂️ Data Management</a>
    </div>

    <script>
    function toggleCamera(camera) {
        const btn = document.getElementById('btn-' + camera);
        const isRecording = btn.textContent.trim() === 'Stop';
        const action = isRecording ? 'stop' : 'start';

        btn.disabled = true;
        btn.textContent = isRecording ? 'Stopping...' : 'Starting...';

        fetch('/api/camera/' + camera + '/' + action, { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    location.reload();
                } else {
                    alert('Error: ' + (data.error || 'Unknown error'));
                    btn.disabled = false;
                    btn.textContent = isRecording ? 'Stop' : 'Record';
                }
            })
            .catch(e => {
                alert('Error: ' + e);
                btn.disabled = false;
            });
    }

    function snapCamera(camera) {
        const btn = document.getElementById('btn-snap-' + camera);
        btn.disabled = true;
        btn.textContent = '⏳';

        // Show modal with loading state
        document.getElementById('snap-modal').style.display = 'block';
        document.getElementById('snap-title').textContent = camera.charAt(0).toUpperCase() + camera.slice(1) + ' Camera Preview';
        document.getElementById('snap-loading').style.display = 'block';
        document.getElementById('snap-image').style.display = 'none';
        document.getElementById('snap-error').style.display = 'none';

        fetch('/api/camera/' + camera + '/snapshot', { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                btn.disabled = false;
                btn.textContent = '📷';
                document.getElementById('snap-loading').style.display = 'none';

                if (data.success) {
                    const img = document.getElementById('snap-image');
                    img.src = data.url + '?t=' + Date.now();
                    img.style.display = 'block';
                } else {
                    document.getElementById('snap-error').textContent = 'Error: ' + (data.error || 'Failed to capture');
                    document.getElementById('snap-error').style.display = 'block';
                }
            })
            .catch(e => {
                btn.disabled = false;
                btn.textContent = '📷';
                document.getElementById('snap-loading').style.display = 'none';
                document.getElementById('snap-error').textContent = 'Error: ' + e;
                document.getElementById('snap-error').style.display = 'block';
            });
    }

    function closeSnapModal() {
        document.getElementById('snap-modal').style.display = 'none';
    }

    function confirmShutdown() {
        document.getElementById('shutdown-modal').style.display = 'block';
        document.getElementById('shutdown-status').style.display = 'none';
        document.getElementById('shutdown-btn').disabled = false;
    }

    function closeShutdownModal() {
        document.getElementById('shutdown-modal').style.display = 'none';
    }

    function doShutdown() {
        const btn = document.getElementById('shutdown-btn');
        const status = document.getElementById('shutdown-status');

        btn.disabled = true;
        btn.textContent = 'Shutting down...';

        fetch('/api/system/shutdown', { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    status.style.display = 'block';
                    status.innerHTML = '✓ Shutdown initiated.<br>Safe to turn off in 10 seconds.';
                    btn.style.display = 'none';
                } else {
                    alert('Error: ' + (data.error || 'Shutdown failed'));
                    btn.disabled = false;
                    btn.textContent = 'Shutdown';
                }
            })
            .catch(e => {
                alert('Error: ' + e);
                btn.disabled = false;
                btn.textContent = 'Shutdown';
            });
    }

    // Recording Control Functions
    let isRecording = false;

    function toggleRecording() {
        // Confirm before stopping cameras
        if (isRecording && !confirm('Stop camera recording?')) {
            return;
        }

        const btn = document.getElementById('recording-btn');
        btn.disabled = true;
        btn.textContent = isRecording ? 'Stopping...' : 'Starting...';

        const endpoint = isRecording ? '/api/recording/stop' : '/api/recording/start';
        fetch(endpoint, { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                btn.disabled = false;
                if (data.success) {
                    isRecording = !isRecording;
                }
                updateRecordingUI();
            })
            .catch(e => {
                console.error('Recording toggle error:', e);
                btn.disabled = false;
                updateRecordingUI();
            });
    }

    function updateRecordingUI() {
        fetch('/api/recording/status')
            .then(r => r.json())
            .then(data => {
                const card = document.getElementById('recording-card');
                const btn = document.getElementById('recording-btn');
                const indicator = document.getElementById('rec-indicator');
                const statusText = document.getElementById('recording-status-text');

                isRecording = data.any_recording;

                if (isRecording) {
                    card.classList.add('recording');
                    btn.className = 'recording-btn stop';
                    btn.textContent = 'Stop';
                    indicator.style.display = 'inline';
                    const recordingCount = Object.values(data.sensors).filter(s => s.recording).length;
                    statusText.textContent = recordingCount + ' sensor(s) recording';
                } else {
                    card.classList.remove('recording');
                    btn.className = 'recording-btn start';
                    btn.textContent = 'Start Recording';
                    indicator.style.display = 'none';
                    statusText.textContent = 'Sensors idle';
                }

                // Update individual sensor statuses
                updateSensorStatus('sensor-gps', data.sensors.gps);
                updateSensorStatus('sensor-imu', data.sensors.imu);
                updateSensorStatus('sensor-wind', data.sensors.wind);
                updateSensorStatus('sensor-pressure', data.sensors.pressure);
                updateSensorStatus('sensor-camera-cockpit', data.sensors.camera_cockpit);
                updateSensorStatus('sensor-camera-sails', data.sensors.camera_sails);
            })
            .catch(e => console.log('Recording status error:', e));
    }

    function updateSensorStatus(elementId, sensor) {
        const el = document.getElementById(elementId);
        if (!el) return;

        const indicator = el.querySelector('.indicator');
        const dot = indicator.querySelector('.rec-dot');

        if (sensor.recording) {
            el.className = 'sensor-status recording';
            dot.className = 'rec-dot active';
            indicator.innerHTML = '<span class="rec-dot active"></span>Recording';
        } else if (sensor.service_running && !sensor.connected) {
            el.className = 'sensor-status error';
            dot.className = 'rec-dot inactive';
            indicator.innerHTML = '<span class="rec-dot inactive"></span>No Signal';
        } else if (sensor.service_running) {
            el.className = 'sensor-status not-recording';
            dot.className = 'rec-dot inactive';
            indicator.innerHTML = '<span class="rec-dot inactive"></span>Waiting';
        } else {
            el.className = 'sensor-status not-recording';
            dot.className = 'rec-dot inactive';
            indicator.innerHTML = '<span class="rec-dot inactive"></span>Off';
        }
    }

    // Update recording status periodically
    setInterval(updateRecordingUI, 2000);
    // Initial load
    updateRecordingUI();

    function scanBluetooth() {
        document.getElementById('bt-modal').style.display = 'block';
        document.getElementById('bt-status').textContent = 'Scanning for devices (30s)...';
        document.getElementById('bt-devices').innerHTML = '';
        document.getElementById('bt-rescan').disabled = true;

        fetch('/api/bluetooth/scan', { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                document.getElementById('bt-rescan').disabled = false;
                if (data.devices && data.devices.length > 0) {
                    document.getElementById('bt-status').textContent = 'Found ' + data.devices.length + ' device(s):';
                    let html = '';
                    data.devices.forEach(d => {
                        const isWind = d.is_wind_sensor;
                        const bg = isWind ? '#1a3a2a' : '#1a2a40';
                        const border = isWind ? '1px solid #4caf50' : '1px solid #333';
                        html += '<div style="padding: 10px; margin: 8px 0; background: ' + bg + '; border: ' + border + '; border-radius: 6px;">';
                        html += '<div style="font-weight: 600;">' + (d.name || 'Unknown') + (isWind ? ' <span style="color: #4caf50;">✓ Wind</span>' : '') + '</div>';
                        html += '<div style="font-size: 12px; color: #78909c;">' + d.address + ' · RSSI: ' + d.rssi + '</div>';
                        if (isWind) {
                            html += '<button onclick="pairWind(\\'' + d.address + '\\')" style="margin-top: 8px; background: #1976d2; color: white; border: none; padding: 6px 12px; border-radius: 4px; font-size: 12px; cursor: pointer;">Set as Wind Sensor</button>';
                        }
                        html += '</div>';
                    });
                    document.getElementById('bt-devices').innerHTML = html;
                } else {
                    document.getElementById('bt-status').textContent = 'No devices found. Make sure sensor is on and nearby.';
                }
            })
            .catch(e => {
                document.getElementById('bt-rescan').disabled = false;
                document.getElementById('bt-status').textContent = 'Scan error: ' + e;
            });
    }

    function closeBtModal() {
        document.getElementById('bt-modal').style.display = 'none';
    }

    function pairWind(address) {
        fetch('/api/bluetooth/set-wind', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({address: address})
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                alert('Wind sensor MAC saved! Restart wind service to connect.');
                closeBtModal();
                location.reload();
            } else {
                alert('Error: ' + (data.error || 'Failed to save'));
            }
        })
        .catch(e => alert('Error: ' + e));
    }

    function calibrateIMU() {
        if (!confirm('Zero heel and pitch at current position?\\n\\nMake sure the boat is level at the dock.')) {
            return;
        }
        const btn = document.getElementById('btn-imu-cal');
        btn.disabled = true;
        btn.textContent = 'Calibrating...';

        fetch('/api/imu/calibrate', { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    alert('IMU calibrated!\\nHeel offset: ' + data.heel_offset.toFixed(1) + '°\\nPitch offset: ' + data.pitch_offset.toFixed(1) + '°');
                    location.reload();
                } else {
                    alert('Calibration failed: ' + (data.error || 'Unknown error'));
                    btn.disabled = false;
                    btn.textContent = 'Zero Heel/Pitch';
                }
            })
            .catch(e => {
                alert('Error: ' + e);
                btn.disabled = false;
                btn.textContent = 'Zero Heel/Pitch';
            });
    }

    function toggleInvert(axis) {
        const btn = document.getElementById('btn-invert-' + axis);
        btn.disabled = true;

        fetch('/api/imu/invert', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({axis: axis})
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                location.reload();
            } else {
                alert('Error: ' + (data.error || 'Failed to toggle inversion'));
                btn.disabled = false;
            }
        })
        .catch(e => {
            alert('Error: ' + e);
            btn.disabled = false;
        });
    }

    function toggleSwapAxes() {
        const btn = document.getElementById('btn-swap-axes');
        btn.disabled = true;

        fetch('/api/imu/swap', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                location.reload();
            } else {
                alert('Error: ' + (data.error || 'Failed to toggle swap'));
                btn.disabled = false;
            }
        })
        .catch(e => {
            alert('Error: ' + e);
            btn.disabled = false;
        });
    }

    function restartGPS() {
        const btn = document.getElementById('btn-gps-restart');
        btn.disabled = true;
        btn.textContent = '⏳ Restarting...';

        fetch('/api/gps/restart', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                alert('GPS service restarted. Wait for satellite fix...');
                location.reload();
            } else {
                alert('Error: ' + (data.error || 'Failed to restart GPS'));
                btn.disabled = false;
                btn.textContent = '🔄 Restart';
            }
        })
        .catch(e => {
            alert('Error: ' + e);
            btn.disabled = false;
            btn.textContent = '🔄 Restart';
        });
    }

    function restartWind() {
        const btn = document.getElementById('btn-wind-restart');
        if (btn) {
            btn.disabled = true;
            btn.textContent = '⏳ Restarting...';
        }

        fetch('/api/wind/restart', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                alert('Wind service restarted. Reconnecting to sensor...');
                location.reload();
            } else {
                alert('Error: ' + (data.error || 'Failed to restart wind service'));
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = '🔄 Restart';
                }
            }
        })
        .catch(e => {
            alert('Error: ' + e);
            if (btn) {
                btn.disabled = false;
                btn.textContent = '🔄 Restart';
            }
        });
    }

    // WiFi Mode
    function loadWiFiStatus() {
        fetch('/api/wifi/status')
            .then(r => r.json())
            .then(data => {
                const status = document.getElementById('wifi-status');
                const details = document.getElementById('wifi-details');
                const btnClient = document.getElementById('btn-wifi-client');
                const btnAP = document.getElementById('btn-wifi-ap');

                const isAP = data.current_mode === 'ap';

                // Highlight active button
                btnClient.style.background = isAP ? '#455a64' : '#1976d2';
                btnAP.style.background = isAP ? '#1976d2' : '#455a64';

                if (isAP) {
                    status.textContent = 'Access Point: ' + data.ap_ssid;
                    details.innerHTML = 'SSID: <strong>' + data.ap_ssid + '</strong> · Password: <strong>' + data.ap_password + '</strong> · IP: 192.168.4.1';
                } else {
                    status.textContent = 'Client: ' + (data.connection || data.client_ssid);
                    details.innerHTML = 'IP: <strong>' + (data.ip_address || 'obtaining...') + '</strong>';
                }

                if (data.saved_mode !== data.current_mode) {
                    details.innerHTML += ' <span style="color: #ff9800;">(boot default: ' + data.saved_mode + ')</span>';
                }
            })
            .catch(e => {
                document.getElementById('wifi-status').textContent = 'Error loading WiFi status';
            });
    }

    function setWiFiMode(mode) {
        const isAP = mode === 'ap';
        const msg = isAP
            ? 'Switch to Access Point mode?\\n\\nSSID: s1\\nPassword: hellowifi\\nIP: 192.168.4.1\\n\\nIf connected via Home-IOT, you will lose connection.'
            : 'Switch to Client mode?\\n\\nWill connect to Home-IOT.\\nIf connected via s1 AP, you will lose connection.';

        if (!confirm(msg)) return;

        document.getElementById('wifi-status').textContent = 'Switching...';
        document.getElementById('btn-wifi-client').disabled = true;
        document.getElementById('btn-wifi-ap').disabled = true;

        fetch('/api/wifi/mode', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({mode: mode})
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                const info = isAP
                    ? 'Switched to AP mode!\\n\\nConnect to WiFi: s1\\nPassword: hellowifi\\nDashboard: http://192.168.4.1:8080'
                    : 'Switched to Client mode!\\n\\nConnecting to Home-IOT...';
                alert(info);
                setTimeout(() => location.reload(), 3000);
            } else {
                alert('Failed: ' + (data.error || 'Unknown error'));
                loadWiFiStatus();
            }
        })
        .catch(e => {
            alert('Error: ' + e);
            loadWiFiStatus();
        });
    }

    function toggleNetdata() {
        const btn = document.getElementById('btn-netdata');
        const isRunning = btn.textContent.trim() === 'Stop';
        const action = isRunning ? 'stop' : 'start';

        btn.disabled = true;
        btn.textContent = isRunning ? 'Stopping...' : 'Starting...';

        fetch('/api/netdata/' + action, { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    location.reload();
                } else {
                    alert('Error: ' + (data.error || 'Unknown error'));
                    btn.disabled = false;
                    btn.textContent = isRunning ? 'Stop' : 'Start';
                }
            })
            .catch(e => {
                alert('Error: ' + e);
                btn.disabled = false;
                btn.textContent = isRunning ? 'Stop' : 'Start';
            });
    }

    // Clock - update every second
    function updateClock() {
        const now = new Date();
        const hours = now.getHours();
        const mins = now.getMinutes().toString().padStart(2, '0');
        const secs = now.getSeconds().toString().padStart(2, '0');
        const ampm = hours >= 12 ? 'PM' : 'AM';
        const h12 = hours % 12 || 12;
        document.getElementById('clock').textContent = h12 + ':' + mins + ':' + secs + ' ' + ampm;
    }
    updateClock();
    setInterval(updateClock, 1000);

    // Load WiFi status on page load
    document.addEventListener('DOMContentLoaded', loadWiFiStatus);

    // IMU Live Mode
    let imuLiveInterval = null;
    let imuLiveActive = false;

    function toggleIMULive() {
        const btn = document.getElementById('btn-imu-live');
        imuLiveActive = !imuLiveActive;

        if (imuLiveActive) {
            btn.style.background = '#4caf50';
            btn.textContent = '⏹ Stop';
            startIMULive();
        } else {
            btn.style.background = '#455a64';
            btn.textContent = '▶ Live';
            stopIMULive();
        }
    }

    function startIMULive() {
        if (imuLiveInterval) return;
        imuLiveInterval = setInterval(updateIMULive, 200);  // 5Hz updates
        updateIMULive();  // Immediate first update
    }

    function stopIMULive() {
        if (imuLiveInterval) {
            clearInterval(imuLiveInterval);
            imuLiveInterval = null;
        }
    }

    function updateIMULive() {
        fetch('/api/imu/status')
            .then(r => r.json())
            .then(data => {
                if (!data.connected) return;

                // Update heading
                document.getElementById('imu-heading').innerHTML = (data.heading_deg || 0).toFixed(1) + '°';

                // Update heel with direction and color
                const heel = data.heel_deg || 0;
                const heelDir = heel > 0 ? 'STBD' : (heel < 0 ? 'PORT' : '');
                const heelColor = Math.abs(heel) > 25 ? '#ff9800' : '#4fc3f7';
                document.getElementById('imu-heel').innerHTML = heel.toFixed(1) + '° <span style="font-size: 12px; font-weight: 400; color: #78909c;">' + heelDir + '</span>';
                document.getElementById('imu-heel').style.color = heelColor;

                // Update pitch with direction
                const pitch = data.pitch_deg || 0;
                const pitchDir = pitch > 0 ? 'BOW UP' : (pitch < 0 ? 'BOW DN' : '');
                document.getElementById('imu-pitch').innerHTML = pitch.toFixed(1) + '° <span style="font-size: 12px; font-weight: 400; color: #78909c;">' + pitchDir + '</span>';

                // Update accelerations
                document.getElementById('imu-accel-x').innerHTML = (data.accel_x_mps2 || 0).toFixed(2) + ' <span style="color: #78909c;">m/s²</span>';
                document.getElementById('imu-accel-y').innerHTML = (data.accel_y_mps2 || 0).toFixed(2) + ' <span style="color: #78909c;">m/s²</span>';
                document.getElementById('imu-accel-z').innerHTML = (data.accel_z_mps2 || 0).toFixed(2) + ' <span style="color: #78909c;">m/s²</span>';
                document.getElementById('imu-accel-total').innerHTML = (data.accel_magnitude_mps2 || 0).toFixed(2) + ' <span style="color: #78909c;">m/s²</span>';

                // Update quaternions
                document.getElementById('imu-quat-i').textContent = (data.quat_i || 0).toFixed(4);
                document.getElementById('imu-quat-j').textContent = (data.quat_j || 0).toFixed(4);
                document.getElementById('imu-quat-k').textContent = (data.quat_k || 0).toFixed(4);
                document.getElementById('imu-quat-real').textContent = (data.quat_real || 0).toFixed(4);
            })
            .catch(e => console.log('IMU update error:', e));
    }

    // AJAX Dashboard Update (no page reload)
    function updateDashboard() {
        fetch('/api/status')
            .then(r => r.json())
            .then(data => {
                // System metrics
                document.getElementById('cpu-temp').textContent = data.cpu_temp_c || '—';
                document.getElementById('ram-percent').textContent = data.ram_percent || '—';
                document.getElementById('disk-free').textContent = data.disk ? data.disk.free_gb : '—';

                // GPS indicator
                const gpsInd = document.getElementById('gps-indicator');
                if (data.gps_status && data.gps_status.connected) {
                    gpsInd.className = 'conn-badge conn-ok';
                    gpsInd.innerHTML = '📍 GPS';
                } else if (data.services && data.services.gps) {
                    gpsInd.className = 'conn-badge conn-warn';
                    gpsInd.innerHTML = '📍 GPS ⚠';
                } else {
                    gpsInd.className = 'conn-badge conn-off';
                    gpsInd.innerHTML = '📍 GPS';
                }

                // Wind indicator
                const windInd = document.getElementById('wind-indicator');
                if (data.wind && data.wind.connected) {
                    windInd.className = 'conn-badge conn-ok';
                    windInd.innerHTML = '💨 Wind';
                } else if (data.services && data.services.wind) {
                    windInd.className = 'conn-badge conn-warn';
                    windInd.innerHTML = '💨 Wind ⚠';
                } else {
                    windInd.className = 'conn-badge conn-off';
                    windInd.innerHTML = '💨 Wind';
                }

                // GPS data
                if (data.gps_status && data.gps_status.connected && data.gps && data.gps.latitude) {
                    document.getElementById('gps-connected').style.display = 'block';
                    document.getElementById('gps-disconnected').style.display = 'none';
                    document.getElementById('gps-fix-type').textContent = data.gps.fix_type || '';
                    document.getElementById('gps-sats').textContent = data.gps.satellites || 0;
                    document.getElementById('gps-accuracy').textContent = data.gps.accuracy_cm || '?';
                    document.getElementById('gps-accuracy2').textContent = data.gps.accuracy_cm || '?';
                    document.getElementById('gps-hdop-rating').textContent = data.gps.hdop_rating || '';
                    document.getElementById('gps-position').textContent = data.gps.latitude.toFixed(6) + ', ' + data.gps.longitude.toFixed(6);
                    document.getElementById('gps-speed').textContent = data.gps.speed_knots || 0;
                    document.getElementById('gps-speed-mph').textContent = data.gps.speed_mph || 0;
                    document.getElementById('gps-altitude').textContent = data.gps.altitude_m ? data.gps.altitude_m.toFixed(1) : '—';
                    document.getElementById('gps-map-link').href = 'https://www.google.com/maps?q=' + data.gps.latitude + ',' + data.gps.longitude;

                    // Update constellation info
                    if (data.gps_status && data.gps_status.constellations) {
                        // Update in use / in view counts
                        const inUse = data.gps_status.satellites || 0;
                        let inView = 0;
                        let constHtml = '';
                        for (const [name, cdata] of Object.entries(data.gps_status.constellations)) {
                            inView += cdata.in_view || 0;
                            const signals = cdata.signals ? cdata.signals.join(', ') : '';
                            const inUseCount = cdata.in_use || 0;
                            const borderStyle = inUseCount ? 'border-left: 2px solid #4caf50;' : '';
                            const nameColor = inUseCount ? '#4caf50' : '#4fc3f7';
                            const nameWeight = inUseCount ? '600' : '400';
                            const countDisplay = inUseCount ? `<span style="color: #4caf50;">${inUseCount}</span>/<span style="color: #4fc3f7;">${cdata.in_view}</span>` : `<span style="color: #4fc3f7;">${cdata.in_view}</span>`;
                            constHtml += `<div style="background: #263238; padding: 3px 6px; border-radius: 4px; ${borderStyle}">
                                <span style="color: ${nameColor}; font-weight: ${nameWeight};">${name}</span>: ${countDisplay}
                                ${signals ? `<span style="color: #546e7a; font-size: 9px;">(${signals})</span>` : ''}
                            </div>`;
                        }
                        document.getElementById('gps-sats-in-use').textContent = inUse;
                        document.getElementById('gps-sats-in-view').textContent = inView;
                        document.getElementById('gps-constellation-grid').innerHTML = constHtml || '<div style="color: #78909c;">—</div>';

                        // Update signals
                        const signals = data.gps_status.signals_in_use || [];
                        document.getElementById('gps-signals').textContent = signals.length > 0 ? signals.join(', ') : '—';
                    }
                } else {
                    document.getElementById('gps-connected').style.display = 'none';
                    document.getElementById('gps-disconnected').style.display = 'block';
                    let gpsMsg = '';
                    if (data.gps_status && !data.gps_status.connected && data.services && data.services.gps) {
                        gpsMsg = '<span style="color: #f44336; font-weight: 600;">⚠️ DISCONNECTED — Check USB cable!</span><div style="color: #78909c; font-size: 11px; margin-top: 2px;">Service running but no data received</div>';
                    } else if (data.gps_status && data.gps_status.connected && !data.gps_status.has_fix && data.services && data.services.gps) {
                        const tracking = data.gps_status.satellites || 0;
                        let inView = 0;
                        if (data.gps_status.constellations) {
                            for (const c of Object.values(data.gps_status.constellations)) {
                                inView += c.in_view || 0;
                            }
                        }
                        gpsMsg = '<span style="color: #ff9800; font-weight: 600;">⚠️ NO FIX — Check antenna!</span><div style="color: #78909c; font-size: 11px; margin-top: 2px;">' + inView + ' satellites in view, ' + tracking + ' tracking</div>';
                    } else if (data.services && data.services.gps) {
                        gpsMsg = '<span style="color: #ff9800;">Waiting for GPS data...</span>';
                    } else {
                        gpsMsg = '<span style="color: #78909c;">GPS service not running</span>';
                    }
                    document.getElementById('gps-status-msg').innerHTML = gpsMsg;
                }

                // Wind data
                if (data.wind && data.wind.connected) {
                    document.getElementById('wind-connected').style.display = 'block';
                    document.getElementById('wind-disconnected').style.display = 'none';
                    document.getElementById('wind-speed').textContent = (data.wind.speed_knots || 0).toFixed(1);
                    document.getElementById('wind-angle').textContent = data.wind.angle_deg || 0;
                    if (data.wind.device_name) document.getElementById('wind-device-name').textContent = data.wind.device_name;
                    if (data.wind.compass_deg != null) {
                        document.getElementById('wind-compass-row').style.display = 'block';
                        document.getElementById('wind-compass').textContent = data.wind.compass_deg.toFixed(1);
                    }
                    if (data.wind.temperature != null) {
                        document.getElementById('wind-temp-row').style.display = 'block';
                        document.getElementById('wind-temp').textContent = data.wind.temperature.toFixed(1);
                    }
                    if (data.wind.battery != null) {
                        document.getElementById('wind-battery-row').style.display = 'block';
                        document.getElementById('wind-battery').textContent = data.wind.battery;
                        // Low battery warning
                        const batteryVal = document.getElementById('wind-battery-val');
                        const batteryWarn = document.getElementById('wind-battery-warn');
                        if (data.wind.low_power_warning) {
                            batteryVal.style.color = '#f44336';
                            batteryWarn.style.display = 'inline';
                        } else {
                            batteryVal.style.color = 'inherit';
                            batteryWarn.style.display = 'none';
                        }
                    }
                    // Device firmware info
                    if (data.wind.model || data.wind.firmware) {
                        document.getElementById('wind-firmware-row').style.display = 'block';
                        let info = data.wind.model || '';
                        if (data.wind.firmware) info += (info ? ' (fw ' + data.wind.firmware + ')' : 'fw ' + data.wind.firmware);
                        document.getElementById('wind-firmware').textContent = info;
                    }
                } else {
                    document.getElementById('wind-connected').style.display = 'none';
                    document.getElementById('wind-disconnected').style.display = 'block';
                    let windMsg = '';
                    if (data.wind && data.wind.status === 'stale' && data.services && data.services.wind) {
                        windMsg = '<div style="color: #f44336; font-weight: 600;">⚠️ DISCONNECTED</div>';
                    } else if (data.services && data.services.wind) {
                        windMsg = '<div style="color: #ff9800;">Searching for sensor...</div>';
                    } else {
                        windMsg = '<div style="color: #78909c; font-style: italic;">Wind service not running</div>';
                    }
                    document.getElementById('wind-status-msg').innerHTML = windMsg;
                }

                // Update timestamp
                const updated = document.querySelector('.updated');
                if (updated) updated.textContent = 'Updated ' + data.last_update;
            })
            .catch(e => console.log('Dashboard update error:', e));
    }

    // Update dashboard every 2 seconds (no page reload)
    setInterval(updateDashboard, 2000);
    </script>
</body>
</html>
"""

@app.route('/')
def dashboard():
    return render_template_string(DASHBOARD_HTML, state=system_state)

@app.route('/api/status')
def api_status():
    return jsonify(system_state)


# ── GPS Details Page ──
GPS_PAGE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>GPS Details - SailFrames</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="5">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, sans-serif; background: #0a1628; color: #e0e8f0; padding: 16px; line-height: 1.5; }
        h1 { color: #4fc3f7; margin-bottom: 8px; font-size: 24px; }
        h2 { color: #78909c; font-size: 14px; text-transform: uppercase; margin: 20px 0 10px; }
        h3 { color: #4fc3f7; font-size: 16px; margin: 16px 0 8px; }
        a { color: #4fc3f7; text-decoration: none; }
        .nav { margin-bottom: 16px; font-size: 14px; }
        .card { background: #1a2a40; border-radius: 8px; padding: 16px; margin-bottom: 12px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; }
        .metric { background: #0d1929; border-radius: 6px; padding: 12px; text-align: center; }
        .metric-value { font-size: 24px; font-weight: 700; color: #fff; }
        .metric-label { font-size: 11px; color: #78909c; text-transform: uppercase; margin-top: 4px; }
        .metric-sub { font-size: 11px; color: #546e7a; margin-top: 2px; }
        .big-metric { grid-column: span 2; }
        .big-metric .metric-value { font-size: 32px; }
        .info-box { background: #0d1929; border-radius: 6px; padding: 12px; margin: 8px 0; font-size: 13px; }
        .info-box strong { color: #4fc3f7; }
        .fix-badge { display: inline-block; padding: 4px 12px; border-radius: 4px; font-weight: 600; font-size: 14px; }
        .fix-dgps { background: #1565c0; color: #fff; }
        .fix-rtk-fixed { background: #2e7d32; color: #fff; }
        .fix-rtk-float { background: #f9a825; color: #000; }
        .fix-gps { background: #455a64; color: #fff; }
        .fix-none { background: #c62828; color: #fff; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; margin: 8px 0; }
        th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #233; }
        th { color: #78909c; font-weight: 500; }
        .edu { font-size: 13px; color: #90a4ae; }
        .edu p { margin: 8px 0; }
        .highlight { color: #4fc3f7; font-weight: 600; }
        .coord { font-family: monospace; font-size: 16px; }
    </style>
</head>
<body>
    <div class="nav"><a href="/">← Back to Dashboard</a></div>
    <h1>📍 GPS Details — ZED-F9P</h1>

    {% if gps %}
    <!-- Current Fix Status -->
    <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
            <div>
                <span class="fix-badge fix-{{ gps.fix_type|lower|replace(' ', '-')|replace('(', '')|replace(')', '') }}">{{ gps.fix_type }}</span>
                <span style="margin-left: 12px; font-size: 14px;">{{ gps.satellites }} satellites · HDOP {{ gps.hdop }}</span>
            </div>
            <div style="font-size: 24px; font-weight: 700;">
                ±{{ gps.accuracy_cm }}<span style="font-size: 14px; color: #78909c;">cm</span>
            </div>
        </div>
        <div class="info-box">
            <strong>{{ gps.fix_type }}:</strong> {{ gps.fix_desc }}
        </div>
    </div>

    <!-- Position -->
    <h2>Position</h2>
    <div class="card">
        <div class="grid">
            <div class="metric big-metric">
                <div class="metric-value coord">{{ "%.8f"|format(gps.latitude) }}°</div>
                <div class="metric-label">Latitude</div>
                <div class="metric-sub">{{ "N" if gps.latitude >= 0 else "S" }} {{ "%d°%d'%.2f''"|format(gps.latitude|abs|int, ((gps.latitude|abs % 1) * 60)|int, ((gps.latitude|abs * 60) % 1) * 60) }}</div>
            </div>
            <div class="metric big-metric">
                <div class="metric-value coord">{{ "%.8f"|format(gps.longitude) }}°</div>
                <div class="metric-label">Longitude</div>
                <div class="metric-sub">{{ "E" if gps.longitude >= 0 else "W" }} {{ "%d°%d'%.2f''"|format(gps.longitude|abs|int, ((gps.longitude|abs % 1) * 60)|int, ((gps.longitude|abs * 60) % 1) * 60) }}</div>
            </div>
            <div class="metric">
                <div class="metric-value">{{ gps.altitude_m|round(1) }}</div>
                <div class="metric-label">Altitude (m)</div>
                <div class="metric-sub">{{ (gps.altitude_m * 3.28084)|round(1) }} ft</div>
            </div>
            <div class="metric">
                <div class="metric-value">{{ gps.course_deg|round(0)|int if gps.course_deg else '—' }}°</div>
                <div class="metric-label">Course</div>
                <div class="metric-sub">True North</div>
            </div>
        </div>
        <div style="margin-top: 12px; text-align: center;">
            <a href="https://www.google.com/maps?q={{ gps.latitude }},{{ gps.longitude }}&z=18" target="_blank" style="font-size: 14px;">🗺 Open in Google Maps ↗</a>
        </div>
    </div>

    <!-- Speed -->
    <h2>Speed</h2>
    <div class="card">
        <div class="grid">
            <div class="metric">
                <div class="metric-value">{{ gps.speed_knots }}</div>
                <div class="metric-label">Knots</div>
                <div class="metric-sub">Nautical</div>
            </div>
            <div class="metric">
                <div class="metric-value">{{ gps.speed_mph }}</div>
                <div class="metric-label">MPH</div>
                <div class="metric-sub">Statute</div>
            </div>
            <div class="metric">
                <div class="metric-value">{{ gps.speed_kmh }}</div>
                <div class="metric-label">km/h</div>
                <div class="metric-sub">Metric</div>
            </div>
            <div class="metric">
                <div class="metric-value">{{ gps.speed_mps }}</div>
                <div class="metric-label">m/s</div>
                <div class="metric-sub">SI Unit</div>
            </div>
        </div>
    </div>

    <!-- Accuracy & Precision -->
    <h2>Accuracy & Precision</h2>
    <div class="card">
        <div class="grid">
            <div class="metric big-metric">
                <div class="metric-value">±{{ gps.accuracy_cm }}<span style="font-size: 14px;">cm</span></div>
                <div class="metric-label">Estimated Accuracy</div>
                <div class="metric-sub">{{ gps.accuracy_m }}m · {{ gps.hdop_rating }}</div>
            </div>
            <div class="metric">
                <div class="metric-value">{{ gps.hdop }}</div>
                <div class="metric-label">HDOP</div>
                <div class="metric-sub">{{ gps.hdop_rating }}</div>
            </div>
            <div class="metric">
                <div class="metric-value">{{ gps.hdop_min }}</div>
                <div class="metric-label">HDOP Min</div>
                <div class="metric-sub">Last minute</div>
            </div>
            <div class="metric">
                <div class="metric-value">{{ gps.hdop_max }}</div>
                <div class="metric-label">HDOP Max</div>
                <div class="metric-sub">Last minute</div>
            </div>
            <div class="metric">
                <div class="metric-value">{{ gps.satellites }}</div>
                <div class="metric-label">Satellites</div>
                <div class="metric-sub">In use now</div>
            </div>
            <div class="metric">
                <div class="metric-value">{{ gps.satellites_avg }}</div>
                <div class="metric-label">Avg Satellites</div>
                <div class="metric-sub">Last minute</div>
            </div>
        </div>
        <div class="info-box">
            <strong>HDOP {{ gps.hdop }} ({{ gps.hdop_rating }}):</strong> {{ gps.hdop_desc }}
        </div>
    </div>

    <!-- Session Stats -->
    <h2>Today's Session</h2>
    <div class="card">
        <div class="grid">
            <div class="metric">
                <div class="metric-value">{{ "{:,}".format(gps.total_points_today) }}</div>
                <div class="metric-label">Points Logged</div>
            </div>
            <div class="metric">
                <div class="metric-value">{{ gps.sample_rate_hz }}</div>
                <div class="metric-label">Sample Rate Hz</div>
            </div>
        </div>
    </div>
    {% else %}
    <div class="card">
        <div style="text-align: center; padding: 20px; color: #78909c;">
            <div style="font-size: 48px; margin-bottom: 12px;">📡</div>
            <div style="font-size: 18px;">Waiting for GPS fix...</div>
            <div style="margin-top: 8px;">Make sure antenna has clear sky view</div>
        </div>
    </div>
    {% endif %}

    <!-- Education Section -->
    <h2>Understanding GPS</h2>

    <div class="card">
        <h3>🛰️ Fix Types Explained</h3>
        <table>
            <tr><th>Fix Type</th><th>Accuracy</th><th>Description</th></tr>
            <tr>
                <td><span class="fix-badge fix-gps" style="font-size: 11px;">GPS (SPS)</span></td>
                <td>~2.5m</td>
                <td>Standard Positioning Service. Basic civilian GPS using L1 C/A signal. Affected by ionospheric delays, multipath, and satellite geometry.</td>
            </tr>
            <tr>
                <td><span class="fix-badge fix-dgps" style="font-size: 11px;">DGPS</span></td>
                <td>~1m</td>
                <td><strong>Differential GPS.</strong> Uses SBAS corrections (WAAS in USA, EGNOS in Europe) broadcast from geostationary satellites. Corrects atmospheric delays. Your ZED-F9P receives these automatically!</td>
            </tr>
            <tr>
                <td><span class="fix-badge fix-rtk-float" style="font-size: 11px;">RTK Float</span></td>
                <td>~50cm</td>
                <td><strong>Real-Time Kinematic (converging).</strong> Receiving corrections from a base station but hasn't achieved integer ambiguity resolution. Accuracy improving toward fixed.</td>
            </tr>
            <tr>
                <td><span class="fix-badge fix-rtk-fixed" style="font-size: 11px;">RTK Fixed</span></td>
                <td>~2cm</td>
                <td><strong>RTK with integer ambiguity resolved.</strong> Centimeter-level accuracy! Requires corrections from a nearby base station (NTRIP/CORS). The holy grail of GPS.</td>
            </tr>
        </table>
    </div>

    <div class="card">
        <h3>📊 HDOP (Horizontal Dilution of Precision)</h3>
        <div class="edu">
            <p>HDOP measures how satellite geometry affects position accuracy. Lower is better.</p>
            <p>The GPS receiver calculates position by measuring distances to multiple satellites. If satellites are clustered together in the sky, small measurement errors cause large position errors. If satellites are spread out evenly, errors cancel out.</p>
        </div>
        <table>
            <tr><th>HDOP</th><th>Rating</th><th>Meaning</th></tr>
            <tr><td>&lt;1</td><td class="highlight">Ideal</td><td>Highest possible precision. Satellites perfectly distributed across the sky.</td></tr>
            <tr><td>1-2</td><td class="highlight">Excellent</td><td>Very high precision. Great for surveying and precision navigation.</td></tr>
            <tr><td>2-5</td><td>Good</td><td>Good for most navigation. Acceptable for general use.</td></tr>
            <tr><td>5-10</td><td>Moderate</td><td>Position usable but degraded. Some satellites may be blocked.</td></tr>
            <tr><td>10-20</td><td>Fair</td><td>Low confidence. Consider waiting for better geometry.</td></tr>
            <tr><td>&gt;20</td><td>Poor</td><td>Position unreliable. Significant obstructions or few satellites.</td></tr>
        </table>
        <div class="info-box">
            <strong>Accuracy Formula:</strong> Estimated accuracy ≈ Base accuracy × HDOP<br>
            Example: DGPS (1m base) × HDOP 0.5 = <strong>0.5m (50cm) accuracy</strong>
        </div>
    </div>

    <div class="card">
        <h3>📡 Your ZED-F9P Receiver</h3>
        <div class="edu">
            <p>The <strong>u-blox ZED-F9P</strong> is a high-precision GNSS module capable of:</p>
        </div>
        <table>
            <tr><th>Feature</th><th>Specification</th></tr>
            <tr><td>Constellations</td><td>GPS, GLONASS, Galileo, BeiDou (184 channels)</td></tr>
            <tr><td>Frequencies</td><td>L1 + L2 (dual-band for better accuracy)</td></tr>
            <tr><td>Standard Accuracy</td><td>~1m CEP (with SBAS/DGPS)</td></tr>
            <tr><td>RTK Accuracy</td><td>1cm + 1ppm CEP (with corrections)</td></tr>
            <tr><td>Update Rate</td><td>Up to 25Hz (currently: {{ gps.sample_rate_hz if gps else 10 }}Hz)</td></tr>
            <tr><td>Time to First Fix</td><td>Cold: 24s, Hot: 2s</td></tr>
            <tr><td>RTK Convergence</td><td>&lt;10 seconds to fixed</td></tr>
        </table>
    </div>

    <div class="card">
        <h3>🎯 Improving Accuracy</h3>
        <div class="edu">
            <p><strong>Current setup (DGPS ~1m):</strong> You're already getting SBAS corrections automatically.</p>
            <p><strong>To achieve RTK (~2cm):</strong></p>
            <ol style="margin-left: 20px; margin-top: 8px;">
                <li>Subscribe to an NTRIP service (free or paid CORS network)</li>
                <li>Configure ZED-F9P to receive RTCM corrections via USB or UART</li>
                <li>Use a nearby base station (&lt;35km for best results)</li>
            </ol>
            <p style="margin-top: 12px;"><strong>Free NTRIP sources:</strong></p>
            <ul style="margin-left: 20px;">
                <li>RTK2GO.com - Community-operated casters</li>
                <li>State DOT CORS networks (varies by state)</li>
                <li>UNAVCO - Academic research network</li>
            </ul>
        </div>
    </div>

    <div class="card">
        <h3>🔢 Coordinate Precision</h3>
        <div class="edu">
            <p>Each decimal place in latitude/longitude represents different precision:</p>
        </div>
        <table>
            <tr><th>Decimals</th><th>Precision</th><th>Use Case</th></tr>
            <tr><td>4 (0.0001°)</td><td>~11m</td><td>City block level</td></tr>
            <tr><td>5 (0.00001°)</td><td>~1.1m</td><td>Individual trees, parked cars</td></tr>
            <tr><td>6 (0.000001°)</td><td>~11cm</td><td>Surveying, precision agriculture</td></tr>
            <tr><td>7 (0.0000001°)</td><td>~1.1cm</td><td>RTK applications, construction</td></tr>
            <tr><td>8 (0.00000001°)</td><td>~1.1mm</td><td>Theoretical (beyond GPS capability)</td></tr>
        </table>
        <div class="info-box">
            Your ZED-F9P outputs <strong>8 decimal places</strong>. With RTK, you can actually use 7 of them meaningfully!
        </div>
    </div>

    <div class="card">
        <h3>⚡ ZED-F9P LED Guide</h3>
        <table>
            <tr><th>LED</th><th>Color</th><th>Off</th><th>Blinking</th><th>Solid</th></tr>
            <tr><td><strong>PWR</strong></td><td>Red</td><td>No power</td><td>—</td><td>Power OK</td></tr>
            <tr><td><strong>PPS</strong></td><td>Blue</td><td>No fix</td><td>1Hz = Valid fix</td><td>—</td></tr>
            <tr><td><strong>RTK</strong></td><td>Green</td><td>No RTK</td><td>RTK Float</td><td>RTK Fixed ✓</td></tr>
            <tr><td><strong>GEO</strong></td><td>Yellow</td><td>Normal</td><td>Geofence event</td><td>Inside fence</td></tr>
        </table>
    </div>

</body>
</html>
"""


@app.route('/gps')
def gps_page():
    """Detailed GPS information page."""
    gps_data = get_latest_gps()
    return render_template_string(GPS_PAGE_HTML, gps=gps_data)


# ── Battery History API ──
BATTERY_DATA_DIR = Path('/mnt/sailframes-data/battery')


def format_time_ny(iso_time):
    """Convert ISO UTC time to New York time formatted string."""
    try:
        # Parse ISO format
        dt = datetime.fromisoformat(iso_time.replace('Z', '+00:00'))
        # Convert to New York timezone
        ny_tz = ZoneInfo('America/New_York')
        dt_ny = dt.astimezone(ny_tz)
        return dt_ny.strftime('%Y-%m-%d %I:%M %p')
    except Exception:
        return iso_time[:16].replace('T', ' ')


def get_battery_sessions(limit=20):
    """Get list of battery discharge sessions."""
    sessions_file = BATTERY_DATA_DIR / 'sessions.csv'
    if not sessions_file.exists():
        return []

    sessions = []
    with open(sessions_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Add formatted times in New York timezone
            row['start_time_ny'] = format_time_ny(row['start_time'])
            row['end_time_ny'] = format_time_ny(row['end_time'])
            # Round duration to int
            row['duration_minutes'] = round(float(row['duration_minutes']))
            sessions.append(row)

    # Return most recent first
    sessions.reverse()
    return sessions[:limit]


def get_active_session():
    """Check if there's an active battery session (not yet in sessions.csv)."""
    if not BATTERY_DATA_DIR.exists():
        return None

    # Get session IDs from sessions.csv
    completed_ids = set()
    sessions_file = BATTERY_DATA_DIR / 'sessions.csv'
    if sessions_file.exists():
        with open(sessions_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                completed_ids.add(row['session_id'])

    # Find session files that aren't in completed sessions
    for session_file in sorted(BATTERY_DATA_DIR.glob('session_*.csv'), reverse=True):
        session_id = session_file.stem.replace('session_', '')
        if session_id not in completed_ids:
            # Found an active session - read its data
            samples = []
            with open(session_file, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    samples.append(row)

            if not samples:
                continue

            # Calculate session stats from samples
            first = samples[0]
            last = samples[-1]
            start_time = datetime.fromisoformat(first['timestamp'].replace('Z', '+00:00'))
            last_time = datetime.fromisoformat(last['timestamp'].replace('Z', '+00:00'))
            duration = last_time - start_time

            # Check if file was modified recently (within 2 minutes) to determine if truly active
            file_mtime = datetime.fromtimestamp(session_file.stat().st_mtime, tz=timezone.utc)
            now = datetime.now(timezone.utc)
            is_stale = (now - file_mtime).total_seconds() > 120

            return {
                'session_id': session_id,
                'start_time_ny': format_time_ny(first['timestamp']),
                'end_time_ny': format_time_ny(last['timestamp']),
                'duration_minutes': round(duration.total_seconds() / 60),
                'start_percent': float(first['percent']),
                'current_percent': float(last['percent']),
                'percent_used': round(float(first['percent']) - float(last['percent']), 1),
                'sample_count': len(samples),
                'current_voltage': float(last['voltage']),
                'current_ma': float(last['current_ma']),
                'is_stale': is_stale,  # True if session was interrupted (not actively updating)
            }

    return None


def finalize_orphan_session(session_id):
    """Finalize an orphan session by adding it to sessions.csv."""
    session_file = BATTERY_DATA_DIR / f'session_{session_id}.csv'
    process_file = BATTERY_DATA_DIR / f'processes_{session_id}.csv'

    if not session_file.exists():
        return False

    # Read session data
    samples = []
    with open(session_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            samples.append(row)

    if len(samples) < 2:
        return False

    # Calculate stats
    first = samples[0]
    last = samples[-1]
    start_time = datetime.fromisoformat(first['timestamp'].replace('Z', '+00:00'))
    end_time = datetime.fromisoformat(last['timestamp'].replace('Z', '+00:00'))
    duration = end_time - start_time

    # Calculate power usage
    total_power_mwh = 0
    max_current = 0
    min_voltage = float(first['voltage'])
    for sample in samples:
        power_mw = float(sample['voltage']) * abs(float(sample['current_ma']))
        total_power_mwh += power_mw * (30 / 3600)  # 30 second intervals
        max_current = max(max_current, float(sample['current_ma']))
        min_voltage = min(min_voltage, float(sample['voltage']))

    summary = {
        'session_id': session_id,
        'start_time': first['timestamp'],
        'end_time': last['timestamp'],
        'duration_minutes': round(duration.total_seconds() / 60, 1),
        'start_percent': float(first['percent']),
        'end_percent': float(last['percent']),
        'percent_used': round(float(first['percent']) - float(last['percent']), 1),
        'start_voltage': float(first['voltage']),
        'end_voltage': float(last['voltage']),
        'min_voltage': min_voltage,
        'max_current_ma': round(max_current, 1),
        'total_power_mwh': round(total_power_mwh, 1),
        'sample_count': len(samples),
    }

    # Append to sessions.csv
    sessions_file = BATTERY_DATA_DIR / 'sessions.csv'
    write_header = not sessions_file.exists()

    with open(sessions_file, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=summary.keys())
        if write_header:
            writer.writeheader()
        writer.writerow(summary)

    return True


def get_session_data(session_id):
    """Get detailed data for a specific session."""
    log_file = BATTERY_DATA_DIR / f'session_{session_id}.csv'
    process_file = BATTERY_DATA_DIR / f'processes_{session_id}.csv'

    data = {'samples': [], 'processes': {}}

    if log_file.exists():
        with open(log_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                data['samples'].append(row)

    if process_file.exists():
        with open(process_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row['name']
                if name not in data['processes']:
                    data['processes'][name] = {'total_cpu': 0, 'samples': 0}
                data['processes'][name]['total_cpu'] += float(row['cpu_percent'])
                data['processes'][name]['samples'] += 1

        # Calculate average CPU per process and add descriptions
        for name, stats in data['processes'].items():
            stats['avg_cpu'] = round(stats['total_cpu'] / stats['samples'], 1) if stats['samples'] > 0 else 0
            stats['description'] = get_process_description(name)

        # Sort by total CPU usage - show top 25
        data['top_processes'] = sorted(
            [{'name': k, **v} for k, v in data['processes'].items()],
            key=lambda x: x['total_cpu'],
            reverse=True
        )[:25]

    return data


# Process descriptions - what each process does and its origin
PROCESS_DESCRIPTIONS = {
    # System core (Raspberry Pi OS)
    'systemd': ('Init system', 'Core', 'Main system and service manager'),
    'systemd-journald': ('System logger', 'Core', 'Collects and stores logs'),
    'systemd-udevd': ('Device manager', 'Core', 'Handles hardware events'),
    'systemd-logind': ('Login manager', 'Core', 'Manages user sessions'),
    'systemd-timesyncd': ('Time sync', 'Core', 'Synchronizes system clock'),
    'dbus-daemon': ('Message bus', 'Core', 'Inter-process communication'),
    'dbus-broker': ('Message bus', 'Core', 'Modern D-Bus implementation'),

    # Desktop environment (Wayland/Wayfire)
    'labwc': ('Compositor', 'Desktop', 'Wayland window compositor'),
    'wayfire': ('Compositor', 'Desktop', 'Wayland compositor (alternative)'),
    'wf-panel-pi': ('Panel', 'Desktop', 'Desktop taskbar/panel'),
    'wireplumber': ('Audio routing', 'Desktop', 'PipeWire session manager'),
    'pipewire': ('Audio server', 'Desktop', 'Modern audio/video server'),
    'pipewire-pulse': ('PulseAudio compat', 'Desktop', 'PulseAudio compatibility'),
    'xdg-desktop-portal': ('Desktop portal', 'Desktop', 'App sandboxing support'),
    'pcmanfm': ('File manager', 'Desktop', 'Lightweight file manager'),
    'lxpolkit': ('Auth agent', 'Desktop', 'Policy kit authentication'),
    'lxsession': ('Session manager', 'Desktop', 'LXDE session manager'),
    'openbox': ('Window manager', 'Desktop', 'X11 window manager'),
    'mutter': ('Window manager', 'Desktop', 'GNOME window manager'),

    # Browsers
    'chromium': ('Web browser', 'Added', 'Chromium browser - HIGH POWER'),
    'chromium-browser': ('Web browser', 'Added', 'Chromium browser - HIGH POWER'),
    'chrome': ('Web browser', 'Added', 'Chrome browser - HIGH POWER'),
    'firefox': ('Web browser', 'Added', 'Firefox browser - HIGH POWER'),
    'firefox-esr': ('Web browser', 'Added', 'Firefox ESR - HIGH POWER'),

    # SailFrames services
    'sailframes_monitor': ('SailFrames', 'SailFrames', 'System monitoring dashboard'),
    'sailframes_gps': ('SailFrames', 'SailFrames', 'GPS sensor service'),
    'sailframes_imu': ('SailFrames', 'SailFrames', 'IMU sensor service'),
    'sailframes_pressure': ('SailFrames', 'SailFrames', 'Pressure sensor service'),
    'sailframes_wind': ('SailFrames', 'SailFrames', 'Wind sensor (BLE) service'),
    'sailframes_camera': ('SailFrames', 'SailFrames', 'Camera recording service'),
    'sailframes_battery': ('SailFrames', 'SailFrames', 'Battery logging service'),
    'power-manager': ('SailFrames', 'SailFrames', 'Power management daemon'),

    # Monitoring tools
    'netdata': ('Monitoring', 'Added', 'Real-time system monitoring'),
    'apps.plugin': ('Netdata plugin', 'Added', 'Netdata application monitor'),
    'proc.plugin': ('Netdata plugin', 'Added', 'Netdata process monitor'),
    'go.d.plugin': ('Netdata plugin', 'Added', 'Netdata Go collectors'),
    'cgroups.plugin': ('Netdata plugin', 'Added', 'Netdata cgroup monitor'),
    'ebpf.plugin': ('Netdata plugin', 'Added', 'Netdata eBPF monitor'),

    # Network services
    'NetworkManager': ('Network', 'Core', 'Network connection manager'),
    'wpa_supplicant': ('WiFi', 'Core', 'WiFi authentication'),
    'dhcpcd': ('DHCP client', 'Core', 'IP address management'),
    'avahi-daemon': ('mDNS', 'Core', 'Local network discovery'),
    'sshd': ('SSH server', 'Core', 'Remote access server'),
    'ssh': ('SSH client', 'Core', 'Remote access client'),

    # Bluetooth
    'bluetoothd': ('Bluetooth', 'Core', 'Bluetooth daemon'),
    'bt-adapter': ('Bluetooth', 'Core', 'Bluetooth adapter control'),

    # Python processes
    'python3': ('Python', 'Varies', 'Python interpreter - check parent'),
    'python': ('Python', 'Varies', 'Python interpreter - check parent'),

    # Hardware/GPU
    'Xorg': ('X server', 'Desktop', 'X11 display server'),
    'Xwayland': ('X compat', 'Desktop', 'X11 apps on Wayland'),

    # Camera/Video
    'libcamera-vid': ('Camera', 'Added', 'Camera video capture'),
    'libcamera-still': ('Camera', 'Added', 'Camera still capture'),
    'rpicam-vid': ('Camera', 'Added', 'Pi camera video'),
    'ffmpeg': ('Video encode', 'Added', 'Video encoding/streaming'),
    'gstreamer': ('Media', 'Added', 'Media framework'),

    # Other common
    'cron': ('Scheduler', 'Core', 'Task scheduler'),
    'rsyslogd': ('Logging', 'Core', 'System logging'),
    'polkitd': ('Auth', 'Core', 'Authorization framework'),
    'udisksd': ('Disks', 'Core', 'Disk management'),
    'accounts-daemon': ('Accounts', 'Core', 'User account service'),
    'gpsd': ('GPS daemon', 'Added', 'GPS device service'),
    'cups': ('Printing', 'Added', 'Print server - can disable'),
    'cupsd': ('Printing', 'Added', 'Print server - can disable'),
}


def get_process_description(name):
    """Get description for a process."""
    # Direct match
    if name in PROCESS_DESCRIPTIONS:
        return PROCESS_DESCRIPTIONS[name]

    # Partial matches
    name_lower = name.lower()
    for key, value in PROCESS_DESCRIPTIONS.items():
        if key.lower() in name_lower or name_lower in key.lower():
            return value

    # Check for sailframes processes
    if 'sailframes' in name_lower:
        return ('SailFrames', 'SailFrames', 'SailFrames service')

    # Check for common patterns
    if 'plugin' in name_lower:
        return ('Plugin', 'Added', 'Extension/plugin process')
    if 'kworker' in name_lower:
        return ('Kernel', 'Core', 'Kernel worker thread')
    if 'irq/' in name_lower:
        return ('Kernel', 'Core', 'Interrupt handler')

    return ('Unknown', 'Unknown', 'Process not recognized')


# ── Video Management ──

def get_video_list(data_dir, date_filter=None):
    """Get list of all recorded videos with metadata."""
    data_path = Path(data_dir)
    videos = []

    # Get all date directories or filter to specific date
    if date_filter:
        date_dirs = [data_path / date_filter]
    else:
        date_dirs = sorted(data_path.glob('20??-??-??'), reverse=True)

    for date_dir in date_dirs:
        video_dir = date_dir / 'video'
        if not video_dir.exists():
            continue

        # Scan both camera subdirectories and root video directory (for old videos)
        video_locations = [
            (video_dir / 'cockpit', 'cockpit'),
            (video_dir / 'sails', 'sails'),
            (video_dir, None),  # Root for old videos (cockpit_*.mp4)
        ]

        for scan_dir, camera_id in video_locations:
            if not scan_dir.exists():
                continue

            for video_file in scan_dir.glob('*.mp4'):
                # Skip if this is the root dir and file is in a subdirectory
                if camera_id is None and video_file.parent != video_dir:
                    continue

                try:
                    stat = video_file.stat()
                    size_mb = stat.st_size / (1024 * 1024)
                    # Estimate duration: ~8 Mbps = ~1 MB/s
                    duration_sec = int(size_mb)

                    # Detect camera from filename if not in subdirectory
                    detected_camera = camera_id
                    if detected_camera is None:
                        if video_file.name.startswith('cockpit_'):
                            detected_camera = 'cockpit'
                        elif video_file.name.startswith('sails_'):
                            detected_camera = 'sails'
                        else:
                            detected_camera = 'unknown'

                    videos.append({
                        'filename': video_file.name,
                        'filepath': str(video_file.relative_to(data_path)),
                        'date': date_dir.name,
                        'camera': detected_camera,
                        'size_mb': round(size_mb, 1),
                        'size_bytes': stat.st_size,
                        'duration_sec': duration_sec,
                        'duration_str': f"{duration_sec // 60}:{duration_sec % 60:02d}",
                        'created': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    })
                except Exception:
                    pass

    # Sort by created time descending
    videos.sort(key=lambda x: x['created'], reverse=True)
    return videos


def get_video_dates(data_dir):
    """Get list of dates that have videos."""
    data_path = Path(data_dir)
    dates = []

    for date_dir in sorted(data_path.glob('20??-??-??'), reverse=True):
        video_dir = date_dir / 'video'
        if video_dir.exists():
            video_count = len(list(video_dir.glob('*.mp4')))
            if video_count > 0:
                dates.append({
                    'date': date_dir.name,
                    'video_count': video_count
                })

    return dates


def validate_video_path(data_dir, filepath):
    """Security: validate that filepath is within data_dir and is a video file."""
    data_path = Path(data_dir).resolve()
    try:
        video_path = (data_path / filepath).resolve()
        # Ensure path is within data directory
        if not str(video_path).startswith(str(data_path)):
            return None
        # Ensure it's an MP4 file
        if video_path.suffix.lower() != '.mp4':
            return None
        # Ensure file exists
        if not video_path.exists():
            return None
        return video_path
    except Exception:
        return None


@app.route('/api/battery/sessions')
def api_battery_sessions():
    """Get list of battery sessions."""
    return jsonify(get_battery_sessions())


@app.route('/api/battery/session/<session_id>')
def api_battery_session(session_id):
    """Get detailed data for a session."""
    return jsonify(get_session_data(session_id))


@app.route('/api/battery/finalize', methods=['POST'])
def api_finalize_session():
    """Finalize an orphan battery session."""
    data = request.get_json() or {}
    session_id = data.get('session_id')
    if not session_id:
        return jsonify({'error': 'session_id required'}), 400

    if finalize_orphan_session(session_id):
        return jsonify({'success': True, 'message': f'Session {session_id} finalized'})
    return jsonify({'error': 'Failed to finalize session'}), 500


@app.route('/api/power/status')
def api_power_status():
    """Get current power/resource usage for services."""
    pressure = get_pressure_status() or {}
    system = get_system_power_stats()
    system['dps310_temp'] = pressure.get('temperature_c')
    return jsonify({
        'services': get_service_resources(),
        'system': system,
    })


@app.route('/api/power/history')
def api_power_history():
    """Get historical power data for charting."""
    date = request.args.get('date')
    limit = int(request.args.get('limit', 120))  # Default 2 hours at 1/min
    config = load_config()
    data_mount = config['storage']['ssd_mount']
    rows = get_power_history(data_mount, date, limit)

    # Parse rows into structured data
    # Header: timestamp,cpu_temp,dps310_temp,cpu_percent,voltage,est_watts,camera_cpu,...
    history = []
    for row in rows:
        if len(row) >= 6:
            history.append({
                'timestamp': row[0],
                'cpu_temp': float(row[1]) if row[1] else 0,
                'dps310_temp': float(row[2]) if row[2] else 0,
                'cpu_percent': float(row[3]) if row[3] else 0,
                'voltage': float(row[4]) if row[4] else 0,
                'est_watts': float(row[5]) if row[5] else 0,
                'camera_cpu': float(row[6]) if len(row) > 6 and row[6] else 0,
                'monitor_cpu': float(row[8]) if len(row) > 8 and row[8] else 0,
            })

    return jsonify(history)


BATTERY_HISTORY_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Power Monitor - SailFrames</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, sans-serif; background: #0a1628; color: #e0e8f0; padding: 16px; }
        h1 { color: #4fc3f7; margin-bottom: 8px; font-size: 24px; }
        h2 { color: #78909c; font-size: 16px; margin: 16px 0 8px; }
        a { color: #4fc3f7; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .nav { margin-bottom: 16px; font-size: 14px; }
        .card { background: #1a2a40; border-radius: 8px; padding: 14px; margin-bottom: 12px; }
        .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr)); gap: 12px; }
        .summary-item { text-align: center; }
        .summary-value { font-size: 28px; font-weight: 700; color: #fff; }
        .summary-label { font-size: 12px; color: #78909c; }
        table { width: 100%; border-collapse: collapse; font-size: 14px; }
        th, td { padding: 10px 8px; text-align: left; border-bottom: 1px solid #233; }
        th { color: #78909c; font-weight: 500; }
        .service-name { font-weight: 600; color: #4fc3f7; }
        .cpu-bar { height: 8px; background: #233; border-radius: 4px; overflow: hidden; min-width: 100px; }
        .cpu-fill { height: 100%; background: linear-gradient(90deg, #4fc3f7, #ff6b6b); transition: width 0.3s; }
        .cpu-value { font-weight: 600; min-width: 50px; text-align: right; }
        .cpu-high { color: #ff6b6b; }
        .cpu-medium { color: #ffb74d; }
        .cpu-low { color: #66bb6a; }
        .mem-value { color: #90a4ae; font-size: 12px; }
        .update-time { font-size: 12px; color: #546e7a; margin-top: 8px; }
        .power-source { display: inline-block; padding: 4px 10px; border-radius: 4px; font-size: 13px; font-weight: 500; }
        .power-usb { background: #1b5e20; color: #a5d6a7; }
    </style>
</head>
<body>
    <div class="nav"><a href="/">← Back to Dashboard</a></div>
    <h1>Power Monitor</h1>
    <p style="margin-bottom: 16px;"><span class="power-source power-usb">USB Power Bank (50Ah)</span></p>

    <h2>System Status</h2>
    <div class="card">
        <div class="summary-grid">
            <div class="summary-item">
                <div class="summary-value" id="sys-temp">--</div>
                <div class="summary-label">CPU Temp</div>
            </div>
            <div class="summary-item">
                <div class="summary-value" id="sys-enclosure-temp">--</div>
                <div class="summary-label">Enclosure</div>
            </div>
            <div class="summary-item">
                <div class="summary-value" id="sys-cpu">--</div>
                <div class="summary-label">CPU %</div>
            </div>
            <div class="summary-item">
                <div class="summary-value" id="sys-voltage">--</div>
                <div class="summary-label">Core V</div>
            </div>
            <div class="summary-item">
                <div class="summary-value" id="sys-watts">--</div>
                <div class="summary-label">Est. Watts</div>
            </div>
        </div>
    </div>

    <h2>Service Resource Usage</h2>
    <div class="card">
        <table>
            <thead>
                <tr><th>Service</th><th>CPU</th><th style="width: 120px;"></th><th>Memory</th></tr>
            </thead>
            <tbody id="services-table">
                <tr><td colspan="4" style="text-align: center; color: #546e7a;">Loading...</td></tr>
            </tbody>
        </table>
        <div class="update-time">Updates every 2 seconds · <span id="last-update">--</span></div>
    </div>

    <h2>Power History <span style="font-size: 12px; color: #546e7a;">(logged every minute)</span></h2>
    <div class="card">
        <div style="display: flex; gap: 12px; margin-bottom: 8px; font-size: 11px; flex-wrap: wrap;">
            <span><span style="color: #ff6b6b;">■</span> Est. Watts</span>
            <span><span style="color: #4fc3f7;">■</span> CPU %</span>
            <span><span style="color: #ffb74d;">■</span> CPU Temp</span>
            <span><span style="color: #e040fb;">■</span> Enclosure Temp</span>
            <span><span style="color: #66bb6a;">■</span> Camera CPU</span>
        </div>
        <canvas id="history-chart" width="800" height="200" style="width: 100%; height: 200px; background: #0d1929; border-radius: 4px;"></canvas>
        <div id="history-status" style="font-size: 12px; color: #546e7a; margin-top: 8px;">Loading history...</div>
    </div>

    <h2>Power Tips</h2>
    <div class="card" style="font-size: 13px; color: #90a4ae;">
        <p><strong>Camera</strong> uses most power (~2W). Stop recording when not needed.</p>
        <p style="margin-top: 8px;"><strong>Display</strong> at 1100 nits draws ~2.8W. PWM dimming available via GPIO18.</p>
        <p style="margin-top: 8px;"><strong>50Ah @ 5V = 250Wh</strong> → ~25-30 hours at typical 8-10W draw.</p>
    </div>

    <script>
    let historyData = [];

    function drawChart() {
        const canvas = document.getElementById('history-chart');
        const ctx = canvas.getContext('2d');
        const w = canvas.width;
        const h = canvas.height;

        // Clear
        ctx.fillStyle = '#0d1929';
        ctx.fillRect(0, 0, w, h);

        if (historyData.length < 2) {
            ctx.fillStyle = '#546e7a';
            ctx.font = '14px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('Collecting data... (logged every minute)', w/2, h/2);
            return;
        }

        const padding = 40;
        const chartW = w - padding * 2;
        const chartH = h - padding;

        // Draw grid lines
        ctx.strokeStyle = '#233';
        ctx.lineWidth = 1;
        for (let i = 0; i <= 4; i++) {
            const y = padding/2 + (chartH / 4) * i;
            ctx.beginPath();
            ctx.moveTo(padding, y);
            ctx.lineTo(w - padding/2, y);
            ctx.stroke();
        }

        // Y-axis labels (0-100 for percentage scale)
        ctx.fillStyle = '#78909c';
        ctx.font = '10px sans-serif';
        ctx.textAlign = 'right';
        for (let i = 0; i <= 4; i++) {
            const val = 100 - i * 25;
            const y = padding/2 + (chartH / 4) * i + 4;
            ctx.fillText(val.toString(), padding - 5, y);
        }

        // Draw lines
        const drawLine = (data, color, key, scale = 1) => {
            ctx.strokeStyle = color;
            ctx.lineWidth = 2;
            ctx.beginPath();
            data.forEach((d, i) => {
                const x = padding + (i / (data.length - 1)) * chartW;
                const val = Math.min((d[key] || 0) * scale, 100);
                const y = padding/2 + chartH - (val / 100) * chartH;
                if (i === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            });
            ctx.stroke();
        };

        drawLine(historyData, '#ff6b6b', 'est_watts', 10);  // Scale watts to %
        drawLine(historyData, '#4fc3f7', 'cpu_percent', 1);
        drawLine(historyData, '#ffb74d', 'cpu_temp', 1);      // CPU temp 0-100 scale
        drawLine(historyData, '#e040fb', 'dps310_temp', 1);   // Enclosure temp
        drawLine(historyData, '#66bb6a', 'camera_cpu', 1);

        // Time labels
        if (historyData.length > 0) {
            ctx.fillStyle = '#546e7a';
            ctx.font = '10px sans-serif';
            ctx.textAlign = 'center';
            const first = historyData[0].timestamp.split(' ')[1] || '';
            const last = historyData[historyData.length-1].timestamp.split(' ')[1] || '';
            ctx.fillText(first.substring(0,5), padding, h - 5);
            ctx.fillText(last.substring(0,5), w - padding, h - 5);
        }
    }

    function loadHistory() {
        fetch('/api/power/history?limit=120')
            .then(r => r.json())
            .then(data => {
                historyData = data;
                const status = document.getElementById('history-status');
                if (data.length > 0) {
                    const first = data[0].timestamp;
                    const last = data[data.length-1].timestamp;
                    status.textContent = `${data.length} samples · ${first} to ${last}`;
                } else {
                    status.textContent = 'No history yet. Data logged every minute.';
                }
                drawChart();
            })
            .catch(e => {
                document.getElementById('history-status').textContent = 'Error loading history';
            });
    }

    function updatePower() {
        fetch('/api/power/status')
            .then(r => r.json())
            .then(data => {
                // System stats
                const sys = data.system || {};
                document.getElementById('sys-temp').textContent = sys.temp ? sys.temp.toFixed(1) + '°C' : '--';
                document.getElementById('sys-enclosure-temp').textContent = sys.dps310_temp ? sys.dps310_temp.toFixed(1) + '°C' : '--';
                document.getElementById('sys-cpu').textContent = sys.cpu_percent ? sys.cpu_percent.toFixed(0) + '%' : '--';
                document.getElementById('sys-voltage').textContent = sys.voltage ? sys.voltage.toFixed(2) + 'V' : '--';
                document.getElementById('sys-watts').textContent = sys.estimated_watts ? '~' + sys.estimated_watts + 'W' : '--';

                // Services table
                const services = data.services || [];
                const tbody = document.getElementById('services-table');
                if (services.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: #546e7a;">No services running</td></tr>';
                } else {
                    tbody.innerHTML = services.map(s => {
                        const cpuClass = s.cpu > 50 ? 'cpu-high' : s.cpu > 20 ? 'cpu-medium' : 'cpu-low';
                        const cpuWidth = Math.min(s.cpu, 100);
                        return `<tr>
                            <td class="service-name">${s.name}</td>
                            <td class="cpu-value ${cpuClass}">${s.cpu.toFixed(1)}%</td>
                            <td><div class="cpu-bar"><div class="cpu-fill" style="width: ${cpuWidth}%"></div></div></td>
                            <td class="mem-value">${s.mem.toFixed(1)}%</td>
                        </tr>`;
                    }).join('');
                }

                document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
            })
            .catch(e => console.error('Power update error:', e));
    }

    updatePower();
    setInterval(updatePower, 2000);

    loadHistory();
    setInterval(loadHistory, 60000);  // Refresh history every minute
    </script>
</body>
</html>
"""


@app.route('/battery')
def battery_history():
    """Power monitoring dashboard."""
    return render_template_string(BATTERY_HISTORY_HTML)


@app.route('/battery/<session_id>')
def battery_session_detail(session_id):
    """Detailed view of a battery session."""
    sessions = get_battery_sessions(limit=100)
    session = next((s for s in sessions if s['session_id'] == session_id), None)
    if not session:
        return "Session not found", 404

    data = get_session_data(session_id)
    return render_template_string(
        BATTERY_HISTORY_HTML,
        sessions=None,
        session=session,
        active_session=None,
        top_processes=data.get('top_processes', [])
    )


# ── Video Review Page ──

VIDEO_PAGE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Video Review - SailFrames</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, sans-serif; background: #0a1628; color: #e0e8f0; padding: 16px; }
        h1 { color: #4fc3f7; margin-bottom: 8px; font-size: 24px; }
        h2 { color: #78909c; font-size: 14px; text-transform: uppercase; margin: 16px 0 8px; }
        a { color: #4fc3f7; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .nav { margin-bottom: 16px; font-size: 14px; }
        .card { background: #1a2a40; border-radius: 8px; padding: 14px; margin-bottom: 12px; }
        .date-nav { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
        .date-btn { background: #233; color: #e0e8f0; border: none; padding: 8px 12px; border-radius: 4px; cursor: pointer; font-size: 13px; }
        .date-btn.active { background: #1976d2; color: white; }
        .date-btn:hover { background: #344; }
        .video-item { display: flex; justify-content: space-between; align-items: center; padding: 12px; border-bottom: 1px solid #233; }
        .video-item:last-child { border-bottom: none; }
        .video-item:hover { background: #233; }
        .video-name { font-weight: 600; font-size: 14px; }
        .video-meta { font-size: 12px; color: #78909c; margin-top: 4px; }
        .video-actions { display: flex; gap: 8px; }
        .btn { padding: 6px 12px; border-radius: 4px; border: none; font-size: 12px; cursor: pointer; }
        .btn-play { background: #1976d2; color: white; }
        .btn-delete { background: #455a64; color: #e0e8f0; }
        .btn-delete:hover { background: #c62828; }
        .no-videos { color: #546e7a; font-style: italic; padding: 20px; text-align: center; }
        .video-player { margin: 16px 0; }
        .video-player video { width: 100%; max-height: 60vh; background: #000; border-radius: 8px; }
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 100; }
        .modal-content { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); background: #1a2a40; padding: 24px; border-radius: 8px; text-align: center; }
        .modal-buttons { margin-top: 16px; display: flex; gap: 12px; justify-content: center; }
        .summary { display: flex; gap: 24px; margin-bottom: 16px; font-size: 14px; }
        .summary-item { }
        .summary-value { font-size: 20px; font-weight: 700; color: #fff; }
        .summary-label { font-size: 11px; color: #78909c; }
    </style>
</head>
<body>
    <div class="nav"><a href="/">&larr; Back to Dashboard</a></div>
    <h1>Video Review</h1>

    <div class="summary">
        <div class="summary-item">
            <div class="summary-value">{{ total_videos }}</div>
            <div class="summary-label">Total Videos</div>
        </div>
        <div class="summary-item">
            <div class="summary-value">{{ total_size_gb }}GB</div>
            <div class="summary-label">Total Size</div>
        </div>
        <div class="summary-item">
            <div class="summary-value">{{ total_duration }}</div>
            <div class="summary-label">Total Duration</div>
        </div>
    </div>

    <div class="date-nav">
        <button class="date-btn {% if not selected_date %}active{% endif %}" onclick="location.href='/video'">All</button>
        {% for d in dates %}
        <button class="date-btn {% if selected_date == d.date %}active{% endif %}" onclick="location.href='/video?date={{ d.date }}'">{{ d.date }} ({{ d.video_count }})</button>
        {% endfor %}
        {% if selected_date and videos %}
        <button class="btn-delete" style="margin-left: auto; padding: 8px 16px;" onclick="confirmDeleteAll('{{ selected_date }}', {{ videos|length }})">Delete All {{ selected_date }}</button>
        {% endif %}
    </div>

    <div id="player-container" class="video-player" style="display: none;">
        <video id="video-player" controls></video>
        <div style="margin-top: 8px; font-size: 14px;">
            <span id="player-filename"></span>
            <button onclick="closePlayer()" style="float: right; background: #455a64; color: white; border: none; padding: 4px 12px; border-radius: 4px; cursor: pointer;">Close</button>
        </div>
    </div>

    <div class="card">
        <div class="video-list">
            {% if videos %}
            {% for v in videos %}
            <div class="video-item" id="video-{{ loop.index }}">
                <div>
                    <div class="video-name">
                        <span style="display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 10px; font-weight: 600; margin-right: 8px; background: {% if v.camera == 'cockpit' %}#1976d2{% elif v.camera == 'sails' %}#00796b{% else %}#455a64{% endif %}; color: white;">{{ v.camera|upper }}</span>
                        {{ v.filename }}
                    </div>
                    <div class="video-meta">{{ v.date }} | {{ v.size_mb }}MB | ~{{ v.duration_str }}</div>
                </div>
                <div class="video-actions">
                    <button class="btn btn-play" onclick="playVideo('{{ v.filepath }}', '{{ v.filename }}')">Play</button>
                    <button class="btn btn-delete" onclick="confirmDelete('{{ v.filepath }}', '{{ v.filename }}')">Delete</button>
                </div>
            </div>
            {% endfor %}
            {% else %}
            <div class="no-videos">No videos found{% if selected_date %} for {{ selected_date }}{% endif %}.</div>
            {% endif %}
        </div>
    </div>

    <!-- Delete confirmation modal -->
    <div id="delete-modal" class="modal">
        <div class="modal-content">
            <div style="font-size: 18px; margin-bottom: 8px;">Delete Video?</div>
            <div id="delete-filename" style="color: #78909c;"></div>
            <div class="modal-buttons">
                <button class="btn" style="background: #455a64; color: white;" onclick="closeDeleteModal()">Cancel</button>
                <button class="btn" style="background: #c62828; color: white;" onclick="doDelete()">Delete</button>
            </div>
        </div>
    </div>

    <!-- Delete All confirmation modal -->
    <div id="delete-all-modal" class="modal">
        <div class="modal-content">
            <div style="font-size: 18px; margin-bottom: 8px;">Delete All Videos?</div>
            <div id="delete-all-info" style="color: #78909c;"></div>
            <div class="modal-buttons">
                <button class="btn" style="background: #455a64; color: white;" onclick="closeDeleteAllModal()">Cancel</button>
                <button class="btn" style="background: #c62828; color: white;" onclick="doDeleteAll()">Delete All</button>
            </div>
        </div>
    </div>

    <script>
        let deleteFilepath = null;
        let deleteAllDate = null;

        function playVideo(filepath, filename) {
            const player = document.getElementById('video-player');
            const container = document.getElementById('player-container');
            document.getElementById('player-filename').textContent = filename;
            player.src = '/video/stream/' + filepath;
            container.style.display = 'block';
            player.play();
            container.scrollIntoView({ behavior: 'smooth' });
        }

        function closePlayer() {
            const player = document.getElementById('video-player');
            player.pause();
            player.src = '';
            document.getElementById('player-container').style.display = 'none';
        }

        function confirmDelete(filepath, filename) {
            deleteFilepath = filepath;
            document.getElementById('delete-filename').textContent = filename;
            document.getElementById('delete-modal').style.display = 'block';
        }

        function closeDeleteModal() {
            document.getElementById('delete-modal').style.display = 'none';
            deleteFilepath = null;
        }

        function doDelete() {
            if (!deleteFilepath) return;

            fetch('/api/video/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filepath: deleteFilepath })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    location.reload();
                } else {
                    alert('Error: ' + (data.error || 'Delete failed'));
                }
                closeDeleteModal();
            })
            .catch(e => {
                alert('Error: ' + e);
                closeDeleteModal();
            });
        }

        function confirmDeleteAll(date, count) {
            deleteAllDate = date;
            document.getElementById('delete-all-info').textContent = 'Delete ' + count + ' videos from ' + date + '?';
            document.getElementById('delete-all-modal').style.display = 'block';
        }

        function closeDeleteAllModal() {
            document.getElementById('delete-all-modal').style.display = 'none';
            deleteAllDate = null;
        }

        function doDeleteAll() {
            if (!deleteAllDate) return;

            fetch('/api/video/delete-date', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ date: deleteAllDate })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    location.href = '/video';
                } else {
                    alert('Error: ' + (data.error || 'Delete failed'));
                }
                closeDeleteAllModal();
            })
            .catch(e => {
                alert('Error: ' + e);
                closeDeleteAllModal();
            });
        }
    </script>
</body>
</html>
"""


# Store config globally for video routes
_config = None


@app.route('/video')
def video_page():
    """Video review page."""
    global _config
    if _config is None:
        _config = load_config()

    data_dir = _config['storage']['data_dir']
    selected_date = request.args.get('date')
    videos = get_video_list(data_dir, date_filter=selected_date)
    dates = get_video_dates(data_dir)

    # Calculate totals
    total_size_bytes = sum(v['size_bytes'] for v in videos)
    total_duration_sec = sum(v['duration_sec'] for v in videos)

    return render_template_string(
        VIDEO_PAGE_HTML,
        videos=videos,
        dates=dates,
        selected_date=selected_date,
        total_videos=len(videos),
        total_size_gb=round(total_size_bytes / (1024**3), 2),
        total_duration=f"{total_duration_sec // 3600}h {(total_duration_sec % 3600) // 60}m"
    )


@app.route('/video/stream/<path:filepath>')
def video_stream(filepath):
    """Serve video file for playback with range support."""
    global _config
    if _config is None:
        _config = load_config()

    data_dir = _config['storage']['data_dir']
    video_path = validate_video_path(data_dir, filepath)

    if video_path is None:
        return "Not found", 404

    return send_file(
        video_path,
        mimetype='video/mp4',
        conditional=True  # Enables HTTP range requests for seeking
    )


@app.route('/api/videos')
def api_videos():
    """List all recorded videos."""
    global _config
    if _config is None:
        _config = load_config()

    data_dir = _config['storage']['data_dir']
    date_filter = request.args.get('date')
    return jsonify(get_video_list(data_dir, date_filter=date_filter))


@app.route('/api/video/delete', methods=['POST'])
def api_video_delete():
    """Delete a video file."""
    global _config
    if _config is None:
        _config = load_config()

    data = request.get_json()
    filepath = data.get('filepath') if data else None

    if not filepath:
        return jsonify({'success': False, 'error': 'No filepath provided'}), 400

    data_dir = _config['storage']['data_dir']
    video_path = validate_video_path(data_dir, filepath)

    if video_path is None:
        return jsonify({'success': False, 'error': 'Invalid path or file not found'}), 404

    try:
        video_path.unlink()
        logger.info(f"Deleted video: {filepath}")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Failed to delete video {filepath}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/video/delete-date', methods=['POST'])
def api_video_delete_date():
    """Delete all videos for a specific date."""
    global _config
    if _config is None:
        _config = load_config()

    data = request.get_json()
    date = data.get('date') if data else None

    if not date:
        return jsonify({'success': False, 'error': 'No date provided'}), 400

    # Validate date format (YYYY-MM-DD)
    import re
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({'success': False, 'error': 'Invalid date format'}), 400

    data_dir = Path(_config['storage']['data_dir'])
    video_dir = data_dir / date / 'video'

    if not video_dir.exists():
        return jsonify({'success': False, 'error': 'No videos for this date'}), 404

    # Delete all mp4 files in the directory
    deleted = 0
    errors = []
    for video_file in video_dir.glob('*.mp4'):
        try:
            video_file.unlink()
            deleted += 1
        except Exception as e:
            errors.append(f"{video_file.name}: {e}")

    logger.info(f"Deleted {deleted} videos for {date}")

    if errors:
        return jsonify({
            'success': True,
            'deleted': deleted,
            'errors': errors
        })

    return jsonify({'success': True, 'deleted': deleted})


# ── Data Management Page ──

def get_data_by_date(data_dir):
    """Get data statistics grouped by date."""
    data_path = Path(data_dir)
    dates = []

    for date_dir in sorted(data_path.glob('20??-??-??'), reverse=True):
        date_info = {
            'date': date_dir.name,
            'sensors': {},
            'video': {'count': 0, 'size_bytes': 0},
            'photos': {'count': 0, 'size_bytes': 0},
            'maneuvers': {'count': 0, 'size_bytes': 0},
            'total_size_bytes': 0
        }

        # Sensor data directories
        sensor_types = ['gps', 'imu', 'pressure', 'wind']
        for sensor in sensor_types:
            sensor_dir = date_dir / sensor
            if sensor_dir.exists():
                files = list(sensor_dir.glob('*.csv'))
                size = sum(f.stat().st_size for f in files)
                date_info['sensors'][sensor] = {
                    'count': len(files),
                    'size_bytes': size
                }
                date_info['total_size_bytes'] += size

        # Video data (continuous mode)
        video_dir = date_dir / 'video'
        if video_dir.exists():
            # Count videos in root and subdirectories
            videos = list(video_dir.glob('*.mp4')) + list(video_dir.glob('*/*.mp4'))
            size = sum(v.stat().st_size for v in videos)
            date_info['video'] = {
                'count': len(videos),
                'size_bytes': size
            }
            date_info['total_size_bytes'] += size

        # Photos (smart mode timelapse)
        photos_dir = date_dir / 'photos'
        if photos_dir.exists():
            photos = list(photos_dir.glob('*.jpg')) + list(photos_dir.glob('*/*.jpg'))
            size = sum(p.stat().st_size for p in photos)
            date_info['photos'] = {
                'count': len(photos),
                'size_bytes': size
            }
            date_info['total_size_bytes'] += size

        # Maneuver clips (smart mode)
        maneuvers_dir = date_dir / 'maneuvers'
        if maneuvers_dir.exists():
            maneuvers = list(maneuvers_dir.glob('*.mp4')) + list(maneuvers_dir.glob('*/*.mp4'))
            size = sum(m.stat().st_size for m in maneuvers)
            date_info['maneuvers'] = {
                'count': len(maneuvers),
                'size_bytes': size
            }
            date_info['total_size_bytes'] += size

        # Only include dates with data
        if date_info['total_size_bytes'] > 0:
            dates.append(date_info)

    return dates


def format_size(size_bytes):
    """Format bytes to human readable string."""
    if size_bytes >= 1024**3:
        return f"{size_bytes / (1024**3):.1f} GB"
    elif size_bytes >= 1024**2:
        return f"{size_bytes / (1024**2):.1f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


DATA_MANAGEMENT_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Data Management - SailFrames</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, sans-serif; background: #0a1628; color: #e0e8f0; padding: 16px; }
        h1 { color: #4fc3f7; margin-bottom: 8px; font-size: 24px; }
        h2 { color: #78909c; font-size: 14px; text-transform: uppercase; margin: 16px 0 8px; }
        a { color: #4fc3f7; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .nav { margin-bottom: 16px; font-size: 14px; }
        .card { background: #1a2a40; border-radius: 8px; padding: 14px; margin-bottom: 12px; }
        .summary { display: flex; gap: 24px; margin-bottom: 16px; font-size: 14px; flex-wrap: wrap; }
        .summary-item { }
        .summary-value { font-size: 20px; font-weight: 700; color: #fff; }
        .summary-label { font-size: 11px; color: #78909c; }
        .date-row { display: flex; justify-content: space-between; align-items: center; padding: 14px; border-bottom: 1px solid #233; }
        .date-row:last-child { border-bottom: none; }
        .date-row:hover { background: #233; }
        .date-name { font-weight: 600; font-size: 16px; color: #4fc3f7; }
        .date-meta { font-size: 12px; color: #78909c; margin-top: 6px; }
        .sensor-tags { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
        .sensor-tag { display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 11px; background: #233; }
        .sensor-tag.gps { background: #1976d2; color: white; }
        .sensor-tag.imu { background: #7b1fa2; color: white; }
        .sensor-tag.pressure { background: #00796b; color: white; }
        .sensor-tag.wind { background: #c2185b; color: white; }
        .sensor-tag.video { background: #e65100; color: white; }
        .sensor-tag.photos { background: #8e24aa; color: white; }
        .sensor-tag.maneuvers { background: #d84315; color: white; }
        .btn { padding: 8px 16px; border-radius: 4px; border: none; font-size: 13px; cursor: pointer; }
        .btn-delete { background: #455a64; color: #e0e8f0; }
        .btn-delete:hover { background: #c62828; }
        .btn-delete:disabled { background: #333; color: #666; cursor: not-allowed; }
        .no-data { color: #546e7a; font-style: italic; padding: 20px; text-align: center; }
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 100; }
        .modal-content { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); background: #1a2a40; padding: 24px; border-radius: 8px; text-align: center; max-width: 90%; }
        .modal-buttons { margin-top: 16px; display: flex; gap: 12px; justify-content: center; }
        .checkbox-group { margin-top: 12px; text-align: left; }
        .checkbox-group label { display: block; padding: 8px; cursor: pointer; }
        .checkbox-group label:hover { background: #233; border-radius: 4px; }
        .checkbox-group input { margin-right: 8px; }
        .warning { color: #ff9800; font-size: 12px; margin-top: 8px; }
        .today-badge { display: inline-block; background: #4caf50; color: white; font-size: 10px; padding: 2px 6px; border-radius: 3px; margin-left: 8px; }
    </style>
</head>
<body>
    <div class="nav"><a href="/">&larr; Back to Dashboard</a></div>
    <h1>Data Management</h1>

    <div class="summary">
        <div class="summary-item">
            <div class="summary-value">{{ total_days }}</div>
            <div class="summary-label">Days with Data</div>
        </div>
        <div class="summary-item">
            <div class="summary-value">{{ total_size }}</div>
            <div class="summary-label">Total Storage Used</div>
        </div>
        <div class="summary-item">
            <div class="summary-value">{{ total_photos }}</div>
            <div class="summary-label">Total Photos</div>
        </div>
        <div class="summary-item">
            <div class="summary-value">{{ total_maneuvers }}</div>
            <div class="summary-label">Maneuver Clips</div>
        </div>
        <div class="summary-item">
            <div class="summary-value">{{ total_videos }}</div>
            <div class="summary-label">Continuous Videos</div>
        </div>
        <div class="summary-item">
            <div class="summary-value">{{ total_sensor_files }}</div>
            <div class="summary-label">Sensor Files</div>
        </div>
    </div>

    <div class="card">
        {% if dates %}
        {% for d in dates %}
        <div class="date-row" id="date-{{ d.date }}">
            <div>
                <div class="date-name">
                    {{ d.date }}
                    {% if d.date == today %}<span class="today-badge">TODAY</span>{% endif %}
                </div>
                <div class="date-meta">{{ d.total_size_str }} total</div>
                <div class="sensor-tags">
                    {% if d.sensors.gps %}<span class="sensor-tag gps">GPS: {{ d.sensors.gps.count }} files ({{ d.sensors.gps.size_str }})</span>{% endif %}
                    {% if d.sensors.imu %}<span class="sensor-tag imu">IMU: {{ d.sensors.imu.count }} files ({{ d.sensors.imu.size_str }})</span>{% endif %}
                    {% if d.sensors.pressure %}<span class="sensor-tag pressure">Pressure: {{ d.sensors.pressure.count }} files ({{ d.sensors.pressure.size_str }})</span>{% endif %}
                    {% if d.sensors.wind %}<span class="sensor-tag wind">Wind: {{ d.sensors.wind.count }} files ({{ d.sensors.wind.size_str }})</span>{% endif %}
                    {% if d.video.count > 0 %}<span class="sensor-tag video">Video: {{ d.video.count }} files ({{ d.video.size_str }})</span>{% endif %}
                    {% if d.photos.count > 0 %}<span class="sensor-tag photos">Photos: {{ d.photos.count }} ({{ d.photos.size_str }})</span>{% endif %}
                    {% if d.maneuvers.count > 0 %}<span class="sensor-tag maneuvers">Maneuvers: {{ d.maneuvers.count }} clips ({{ d.maneuvers.size_str }})</span>{% endif %}
                </div>
            </div>
            <button class="btn btn-delete" onclick="showDeleteModal('{{ d.date }}', '{{ d.total_size_str }}', {% if d.date == today %}true{% else %}false{% endif %})" {% if d.date == today %}title="Cannot delete today's data"{% endif %}>Delete</button>
        </div>
        {% endfor %}
        {% else %}
        <div class="no-data">No recorded data found.</div>
        {% endif %}
    </div>

    <!-- Delete confirmation modal -->
    <div id="delete-modal" class="modal">
        <div class="modal-content">
            <div style="font-size: 18px; margin-bottom: 8px;">Delete Data for <span id="modal-date"></span>?</div>
            <div id="modal-size" style="color: #78909c; margin-bottom: 12px;"></div>

            <div class="checkbox-group">
                <label><input type="checkbox" id="del-sensors" checked> Sensor data (GPS, IMU, Pressure, Wind)</label>
                <label><input type="checkbox" id="del-video" checked> Video recordings (continuous mode)</label>
                <label><input type="checkbox" id="del-photos" checked> Timelapse photos</label>
                <label><input type="checkbox" id="del-maneuvers" checked> Maneuver clips</label>
            </div>

            <div id="today-warning" class="warning" style="display: none;">
                Cannot delete today's data while services may be writing to it.
            </div>

            <div class="modal-buttons">
                <button class="btn" style="background: #455a64; color: white;" onclick="closeModal()">Cancel</button>
                <button class="btn" id="confirm-delete-btn" style="background: #c62828; color: white;" onclick="doDelete()">Delete Selected</button>
            </div>
        </div>
    </div>

    <script>
        let deleteDate = null;
        let isToday = false;

        function showDeleteModal(date, size, today) {
            deleteDate = date;
            isToday = today;
            document.getElementById('modal-date').textContent = date;
            document.getElementById('modal-size').textContent = 'Total size: ' + size;
            document.getElementById('today-warning').style.display = today ? 'block' : 'none';
            document.getElementById('confirm-delete-btn').disabled = today;
            document.getElementById('del-sensors').disabled = today;
            document.getElementById('del-video').disabled = today;
            document.getElementById('del-photos').disabled = today;
            document.getElementById('del-maneuvers').disabled = today;
            document.getElementById('delete-modal').style.display = 'block';
        }

        function closeModal() {
            document.getElementById('delete-modal').style.display = 'none';
            deleteDate = null;
        }

        function doDelete() {
            if (!deleteDate || isToday) return;

            const deleteSensors = document.getElementById('del-sensors').checked;
            const deleteVideo = document.getElementById('del-video').checked;
            const deletePhotos = document.getElementById('del-photos').checked;
            const deleteManeuvers = document.getElementById('del-maneuvers').checked;

            if (!deleteSensors && !deleteVideo && !deletePhotos && !deleteManeuvers) {
                alert('Please select at least one data type to delete.');
                return;
            }

            document.getElementById('confirm-delete-btn').disabled = true;
            document.getElementById('confirm-delete-btn').textContent = 'Deleting...';

            fetch('/api/data/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    date: deleteDate,
                    delete_sensors: deleteSensors,
                    delete_video: deleteVideo,
                    delete_photos: deletePhotos,
                    delete_maneuvers: deleteManeuvers
                })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    location.reload();
                } else {
                    alert('Error: ' + (data.error || 'Delete failed'));
                    document.getElementById('confirm-delete-btn').disabled = false;
                    document.getElementById('confirm-delete-btn').textContent = 'Delete Selected';
                }
            })
            .catch(e => {
                alert('Error: ' + e);
                document.getElementById('confirm-delete-btn').disabled = false;
                document.getElementById('confirm-delete-btn').textContent = 'Delete Selected';
            });
        }
    </script>
</body>
</html>
"""


# ── Sailing Dashboard ──
SAILING_DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Sailing - SailFrames</title>
    <meta name="viewport" content="width=800, height=480, initial-scale=1, user-scalable=no">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <meta http-equiv="refresh" content="60">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html, body {
            width: 800px;
            height: 480px;
            overflow: hidden;
            background: #0a1628;
            color: #e0e8f0;
            font-family: 'Roboto Mono', 'SF Mono', 'Monaco', monospace;
        }
        .container {
            display: flex;
            width: 800px;
            height: 480px;
            padding: 10px;
            gap: 10px;
        }
        .compass-section {
            width: 280px;
            height: 460px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .compass-container {
            position: relative;
            width: 260px;
            height: 260px;
        }
        .compass-svg {
            width: 100%;
            height: 100%;
        }
        .data-section {
            flex: 1;
            display: grid;
            grid-template-columns: 1fr 1fr;
            grid-template-rows: repeat(4, 1fr);
            gap: 8px;
        }
        .data-panel {
            background: #1a2a40;
            border-radius: 8px;
            padding: 4px 8px;
            display: flex;
            flex-direction: row;
            justify-content: flex-start;
            align-items: center;
            border: 1px solid #2a3a50;
        }
        .data-label {
            font-size: 24px;
            color: #7a8a9a;
            text-transform: uppercase;
            letter-spacing: 1px;
            writing-mode: vertical-rl;
            transform: rotate(180deg);
            margin-right: 8px;
            min-width: 32px;
        }
        .data-value {
            font-size: 90px;
            font-weight: bold;
            color: #00e0ff;
            line-height: 1;
            flex: 1;
            text-align: center;
        }
        .data-value.wind {
            color: #ff6b6b;
        }
        .data-value.speed {
            color: #4ecdc4;
        }
        .data-value.heel {
            color: #ffe66d;
        }
        .data-value.sat {
            color: #95e1d3;
            font-size: 48px;
        }
        .data-value.mag {
            color: #b39ddb;
        }
        .sat-section {
            position: absolute;
            bottom: 10px;
            left: 10px;
            width: 360px;
        }
        .sat-panel {
            padding: 8px 15px;
        }
        .sat-panel .data-value {
            font-size: 42px;
        }
        .data-unit {
            font-size: 12px;
            color: #5a6a7a;
            margin-top: 2px;
        }
        .data-panel.no-data .data-value {
            color: #3a4a5a;
        }
        /* Compass styles */
        .compass-ring {
            fill: none;
            stroke: #2a3a50;
            stroke-width: 2;
        }
        .compass-tick {
            stroke: #4a5a6a;
            stroke-width: 1;
        }
        .compass-tick-major {
            stroke: #7a8a9a;
            stroke-width: 2;
        }
        .compass-text {
            fill: #7a8a9a;
            font-size: 14px;
            text-anchor: middle;
            dominant-baseline: middle;
        }
        .compass-center {
            fill: #1a2a40;
            stroke: #3a4a5a;
            stroke-width: 1;
        }
        .boat-icon {
            fill: #4ecdc4;
            stroke: #2a8a7a;
            stroke-width: 1;
        }
        .wind-arrow {
            fill: none;
            stroke: #ff6b6b;
            stroke-width: 4;
            stroke-linecap: round;
        }
        .wind-arrow-head {
            fill: #ff6b6b;
        }
        .heading-indicator {
            fill: #00e0ff;
        }
        .heading-text {
            fill: #00e0ff;
            font-size: 24px;
            font-weight: bold;
            text-anchor: middle;
        }
        .center-label {
            fill: #5a6a7a;
            font-size: 10px;
            text-anchor: middle;
        }
        /* Connection status */
        .status-dot {
            position: absolute;
            top: 5px;
            right: 5px;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #3a4a5a;
        }
        .status-dot.connected {
            background: #4ecdc4;
        }
        .status-dot.warning {
            background: #ffe66d;
        }
        .status-dot.error {
            background: #ff6b6b;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="compass-section">
            <div class="compass-container">
                <svg class="compass-svg" viewBox="0 0 380 380">
                    <!-- Outer ring -->
                    <circle cx="190" cy="190" r="175" class="compass-ring"/>

                    <!-- Degree ticks -->
                    <g id="compass-ticks">
                        <!-- Generated by JS -->
                    </g>

                    <!-- Cardinal directions -->
                    <text x="190" y="30" class="compass-text">N</text>
                    <text x="350" y="195" class="compass-text">E</text>
                    <text x="190" y="360" class="compass-text">S</text>
                    <text x="30" y="195" class="compass-text">W</text>

                    <!-- Center background -->
                    <circle cx="190" cy="190" r="60" class="compass-center"/>

                    <!-- COG value in center -->
                    <text id="heading-text" x="190" y="190" class="heading-text">---</text>
                    <text x="190" y="210" class="center-label">COG</text>

                    <!-- Boat icon (always pointing up) -->
                    <g id="boat-icon" transform="translate(190, 190)">
                        <path d="M 0,-45 L 12,-15 L 10,40 L 0,50 L -10,40 L -12,-15 Z" class="boat-icon"/>
                    </g>

                    <!-- Wind arrow (rotates based on AWA) -->
                    <g id="wind-arrow" transform="translate(190, 190) rotate(0)">
                        <line x1="0" y1="-65" x2="0" y2="-140" class="wind-arrow"/>
                        <polygon points="0,-150 -8,-130 8,-130" class="wind-arrow-head"/>
                    </g>

                    <!-- Heading indicator on outer ring (shows compass direction) -->
                    <g id="heading-indicator" transform="translate(190, 190) rotate(0)">
                        <polygon points="0,-165 -8,-180 8,-180" class="heading-indicator"/>
                    </g>
                </svg>
            </div>
        </div>

        <div class="data-section">
            <div class="data-panel" id="panel-aws">
                <div class="data-label">AWS</div>
                <div class="data-value wind" id="val-aws">--</div>
            </div>
            <div class="data-panel" id="panel-awa">
                <div class="data-label">AWA</div>
                <div class="data-value wind" id="val-awa">--</div>
            </div>
            <div class="data-panel" id="panel-tws">
                <div class="data-label">TWS</div>
                <div class="data-value wind" id="val-tws">--</div>
            </div>
            <div class="data-panel" id="panel-twa">
                <div class="data-label">TWA</div>
                <div class="data-value wind" id="val-twa">--</div>
            </div>
            <div class="data-panel" id="panel-sog">
                <div class="data-label">SOG</div>
                <div class="data-value speed" id="val-sog">--</div>
            </div>
            <div class="data-panel" id="panel-cog">
                <div class="data-label">COG</div>
                <div class="data-value" id="val-cog">--</div>
            </div>
            <div class="data-panel" id="panel-heel">
                <div class="data-label">HEEL</div>
                <div class="data-value heel" id="val-heel">--</div>
            </div>
            <div class="data-panel" id="panel-mag">
                <div class="data-label">MAG</div>
                <div class="data-value mag" id="val-mag">--</div>
            </div>
        </div>
    </div>
    <!-- Satellite info below compass -->
    <div class="sat-section">
        <div class="data-panel sat-panel" id="panel-sat">
            <div class="data-label">SAT</div>
            <div class="data-value sat" id="val-sat">--</div>
        </div>
    </div>
    <div id="update-indicator" style="position:absolute;bottom:5px;right:10px;font-size:10px;color:#3a4a5a"></div>

    <script>
        // Generate compass ticks
        function generateCompassTicks() {
            const ticksGroup = document.getElementById('compass-ticks');
            const cx = 190, cy = 190, r = 175;

            for (let deg = 0; deg < 360; deg += 10) {
                const rad = (deg - 90) * Math.PI / 180;
                const isMajor = deg % 30 === 0;
                const innerR = isMajor ? r - 15 : r - 8;

                const x1 = cx + innerR * Math.cos(rad);
                const y1 = cy + innerR * Math.sin(rad);
                const x2 = cx + r * Math.cos(rad);
                const y2 = cy + r * Math.sin(rad);

                const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                line.setAttribute('x1', x1);
                line.setAttribute('y1', y1);
                line.setAttribute('x2', x2);
                line.setAttribute('y2', y2);
                line.setAttribute('class', isMajor ? 'compass-tick-major' : 'compass-tick');
                ticksGroup.appendChild(line);
            }
        }

        // Calculate true wind from apparent wind and boat speed
        function calcTrueWind(aws, awa, sog) {
            if (aws === null || awa === null || sog === null) {
                return { tws: null, twa: null };
            }

            // Convert AWA to radians (AWA is relative to bow, positive = starboard)
            const awaRad = awa * Math.PI / 180;

            // True wind speed using law of cosines
            const tws = Math.sqrt(aws * aws + sog * sog - 2 * aws * sog * Math.cos(awaRad));

            // True wind angle using atan2
            const twaRad = Math.atan2(aws * Math.sin(awaRad), aws * Math.cos(awaRad) - sog);
            let twa = twaRad * 180 / Math.PI;

            return {
                tws: tws.toFixed(1),
                twa: Math.round(twa)
            };
        }

        // Format angle with sign for port/starboard indication
        function formatAngle(angle) {
            if (angle === null || isNaN(angle)) return '--';
            const absAngle = Math.abs(Math.round(angle));
            const side = angle >= 0 ? 'S' : 'P';  // Starboard / Port
            return absAngle + '°' + side;
        }

        // Update dashboard with latest data from direct API endpoints
        function updateDashboard() {
            Promise.all([
                fetch('/api/imu/status').then(r => r.json()).catch(() => ({})),
                fetch('/api/gps/status').then(r => r.json()).catch(() => ({})),
                fetch('/api/wind/status').then(r => r.json()).catch(() => ({})),
                fetch('/api/status').then(r => r.json()).catch(() => ({}))
            ]).then(([imu, gpsLive, wind, status]) => {
                // IMU data (live)
                const heel = imu.heel_deg;
                const mag = imu.heading_deg;  // Magnetometer heading from BNO085

                // GPS data - combine live status and parsed CSV
                const gps = status.gps || {};
                const sog = gps.speed_knots;
                const cog = gps.course_deg;  // Course Over Ground from GPS
                const satsInUse = gpsLive.satellites || gps.satellites || 0;
                const hdop = gpsLive.hdop || gps.hdop;

                // Calculate total satellites in view from constellations
                let satsInView = 0;
                if (gpsLive.constellations) {
                    for (const c of Object.values(gpsLive.constellations)) {
                        satsInView += c.in_view || 0;
                    }
                }
                if (satsInView === 0) satsInView = satsInUse;

                // Wind data (live)
                const aws = wind.speed_knots;
                const awa = wind.angle_deg;

                // Calculate true wind
                const { tws, twa } = calcTrueWind(aws, awa, sog);

                // Update data panels
                updatePanel('aws', aws !== undefined && aws !== null ? aws.toFixed(1) : '--', aws !== null);
                updatePanel('awa', awa !== undefined && awa !== null ? formatAngle(awa) : '--', awa !== null);
                updatePanel('tws', tws !== null ? tws : '--', tws !== null);
                updatePanel('twa', twa !== null ? formatAngle(twa) : '--', twa !== null);
                updatePanel('sog', sog !== undefined && sog !== null ? sog.toFixed(1) : '--', sog !== null);
                updatePanel('cog', cog !== undefined && cog !== null ? Math.round(cog) + '°' : '--', cog !== null);
                updatePanel('heel', heel !== undefined && heel !== null ? Math.round(heel) + '°' : '--', heel !== null);
                updatePanel('mag', mag !== undefined && mag !== null ? Math.round(mag) + '°' : '--', mag !== null);

                // SAT panel: show fix/view and HDOP
                const hdopStr = hdop !== undefined && hdop !== null ? hdop.toFixed(1) : '--';
                updatePanel('sat', satsInUse + '/' + satsInView + ' H' + hdopStr, satsInUse > 0);

                // Update compass heading text (show COG from GPS)
                document.getElementById('heading-text').textContent =
                    cog !== null && cog !== undefined ? Math.round(cog) + '°' : '---';

                // Rotate heading indicator (COG on outer ring)
                if (cog !== null && cog !== undefined) {
                    document.getElementById('heading-indicator').setAttribute(
                        'transform', 'translate(190, 190) rotate(' + cog + ')'
                    );
                }

                // Rotate wind arrow based on AWA
                if (awa !== null && awa !== undefined) {
                    document.getElementById('wind-arrow').setAttribute(
                        'transform', 'translate(190, 190) rotate(' + awa + ')'
                    );
                }

                // Update indicator
                const now = new Date();
                document.getElementById('update-indicator').textContent =
                    now.toLocaleTimeString();
            }).catch(err => {
                console.error('Update error:', err);
                document.getElementById('update-indicator').textContent = 'ERR';
            });
        }

        function updatePanel(id, value, hasData) {
            const panel = document.getElementById('panel-' + id);
            const valEl = document.getElementById('val-' + id);

            valEl.textContent = value;

            if (hasData) {
                panel.classList.remove('no-data');
            } else {
                panel.classList.add('no-data');
            }
        }

        // Initialize
        try {
            generateCompassTicks();
        } catch(e) {
            console.error('Compass error:', e);
        }

        // Start updates immediately and 10x per second (10Hz)
        updateDashboard();
        setInterval(updateDashboard, 100);

        // Auto-redirect to sailing page on 5" display
        // (uncomment if desired)
        // if (window.innerWidth === 800 && window.innerHeight === 480) {
        //     if (window.location.pathname !== '/sailing') {
        //         window.location.href = '/sailing';
        //     }
        // }
    </script>
</body>
</html>
"""


@app.route('/sailing')
def sailing_dashboard():
    """Full-screen sailing dashboard optimized for 5" Newhaven display."""
    from flask import make_response
    response = make_response(render_template_string(SAILING_DASHBOARD_HTML, state=system_state))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/data')
def data_management_page():
    """Data management page for viewing and deleting recorded data."""
    global _config
    if _config is None:
        _config = load_config()

    data_dir = _config['storage']['data_dir']
    dates = get_data_by_date(data_dir)
    today = datetime.now().strftime('%Y-%m-%d')

    # Add formatted sizes
    for d in dates:
        d['total_size_str'] = format_size(d['total_size_bytes'])
        for sensor, info in d['sensors'].items():
            info['size_str'] = format_size(info['size_bytes'])
        d['video']['size_str'] = format_size(d['video']['size_bytes'])
        d['photos']['size_str'] = format_size(d['photos']['size_bytes'])
        d['maneuvers']['size_str'] = format_size(d['maneuvers']['size_bytes'])

    # Calculate totals
    total_size_bytes = sum(d['total_size_bytes'] for d in dates)
    total_videos = sum(d['video']['count'] for d in dates)
    total_photos = sum(d['photos']['count'] for d in dates)
    total_maneuvers = sum(d['maneuvers']['count'] for d in dates)
    total_sensor_files = sum(
        sum(s['count'] for s in d['sensors'].values())
        for d in dates
    )

    return render_template_string(
        DATA_MANAGEMENT_HTML,
        dates=dates,
        today=today,
        total_days=len(dates),
        total_size=format_size(total_size_bytes),
        total_videos=total_videos,
        total_photos=total_photos,
        total_maneuvers=total_maneuvers,
        total_sensor_files=total_sensor_files
    )


@app.route('/api/data/delete', methods=['POST'])
def api_data_delete():
    """Delete sensor and/or video data for a specific date."""
    global _config
    if _config is None:
        _config = load_config()

    data = request.get_json()
    date = data.get('date') if data else None
    delete_sensors = data.get('delete_sensors', True) if data else True
    delete_video = data.get('delete_video', True) if data else True
    delete_photos = data.get('delete_photos', True) if data else True
    delete_maneuvers = data.get('delete_maneuvers', True) if data else True

    if not date:
        return jsonify({'success': False, 'error': 'No date provided'}), 400

    # Validate date format (YYYY-MM-DD)
    import re
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({'success': False, 'error': 'Invalid date format'}), 400

    # Prevent deleting today's data
    today = datetime.now().strftime('%Y-%m-%d')
    if date == today:
        return jsonify({'success': False, 'error': 'Cannot delete today\'s data while services may be recording'}), 400

    data_dir = Path(_config['storage']['data_dir'])
    date_dir = data_dir / date

    if not date_dir.exists():
        return jsonify({'success': False, 'error': 'No data for this date'}), 404

    deleted = {'sensors': 0, 'videos': 0, 'photos': 0, 'maneuvers': 0}
    errors = []

    # Delete sensor data
    if delete_sensors:
        for sensor in ['gps', 'imu', 'pressure', 'wind']:
            sensor_dir = date_dir / sensor
            if sensor_dir.exists():
                for f in sensor_dir.glob('*.csv'):
                    try:
                        f.unlink()
                        deleted['sensors'] += 1
                    except Exception as e:
                        errors.append(f"{sensor}/{f.name}: {e}")
                # Remove empty directory
                try:
                    if sensor_dir.exists() and not list(sensor_dir.iterdir()):
                        sensor_dir.rmdir()
                except Exception:
                    pass

    # Delete video data
    if delete_video:
        video_dir = date_dir / 'video'
        if video_dir.exists():
            # Delete videos in root and subdirectories
            for v in list(video_dir.glob('*.mp4')) + list(video_dir.glob('*/*.mp4')):
                try:
                    v.unlink()
                    deleted['videos'] += 1
                except Exception as e:
                    errors.append(f"video/{v.name}: {e}")
            # Remove empty subdirectories
            for subdir in video_dir.iterdir():
                if subdir.is_dir():
                    try:
                        if not list(subdir.iterdir()):
                            subdir.rmdir()
                    except Exception:
                        pass
            # Remove video directory if empty
            try:
                if video_dir.exists() and not list(video_dir.iterdir()):
                    video_dir.rmdir()
            except Exception:
                pass

    # Delete photos (smart mode timelapse)
    if delete_photos:
        photos_dir = date_dir / 'photos'
        if photos_dir.exists():
            for p in list(photos_dir.glob('*.jpg')) + list(photos_dir.glob('*/*.jpg')):
                try:
                    p.unlink()
                    deleted['photos'] += 1
                except Exception as e:
                    errors.append(f"photos/{p.name}: {e}")
            # Remove empty subdirectories
            for subdir in photos_dir.iterdir():
                if subdir.is_dir():
                    try:
                        if not list(subdir.iterdir()):
                            subdir.rmdir()
                    except Exception:
                        pass
            # Remove photos directory if empty
            try:
                if photos_dir.exists() and not list(photos_dir.iterdir()):
                    photos_dir.rmdir()
            except Exception:
                pass

    # Delete maneuver clips (smart mode)
    if delete_maneuvers:
        maneuvers_dir = date_dir / 'maneuvers'
        if maneuvers_dir.exists():
            # Delete videos and JSON metadata
            for m in list(maneuvers_dir.glob('*.mp4')) + list(maneuvers_dir.glob('*/*.mp4')):
                try:
                    m.unlink()
                    deleted['maneuvers'] += 1
                    # Also delete JSON sidecar if exists
                    json_file = m.with_suffix('.json')
                    if json_file.exists():
                        json_file.unlink()
                except Exception as e:
                    errors.append(f"maneuvers/{m.name}: {e}")
            # Remove empty subdirectories
            for subdir in maneuvers_dir.iterdir():
                if subdir.is_dir():
                    try:
                        if not list(subdir.iterdir()):
                            subdir.rmdir()
                    except Exception:
                        pass
            # Remove maneuvers directory if empty
            try:
                if maneuvers_dir.exists() and not list(maneuvers_dir.iterdir()):
                    maneuvers_dir.rmdir()
            except Exception:
                pass

    # Remove date directory if empty
    try:
        if date_dir.exists() and not list(date_dir.iterdir()):
            date_dir.rmdir()
    except Exception:
        pass

    logger.info(f"Deleted data for {date}: {deleted['sensors']} sensor files, {deleted['videos']} videos, "
                f"{deleted['photos']} photos, {deleted['maneuvers']} maneuvers")

    if errors:
        return jsonify({
            'success': True,
            'deleted': deleted,
            'errors': errors
        })

    return jsonify({'success': True, 'deleted': deleted})


@app.route('/api/camera/<camera_id>/start', methods=['POST'])
def api_camera_start(camera_id):
    """Start camera recording service."""
    if camera_id not in ('cockpit', 'sails'):
        return jsonify({'success': False, 'error': 'Invalid camera ID'}), 400

    service_name = f'sailframes-camera-{camera_id}'
    try:
        result = subprocess.run(
            ['sudo', 'systemctl', 'start', service_name],
            capture_output=True, text=True, timeout=10
        )
        success = result.returncode == 0
        if success:
            logger.info(f"Camera {camera_id} started via API")
        else:
            logger.warning(f"Failed to start camera {camera_id}: {result.stderr}")
        return jsonify({'success': success, 'error': result.stderr if not success else None})
    except Exception as e:
        logger.error(f"Camera {camera_id} start error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/camera/<camera_id>/stop', methods=['POST'])
def api_camera_stop(camera_id):
    """Stop camera recording service."""
    if camera_id not in ('cockpit', 'sails'):
        return jsonify({'success': False, 'error': 'Invalid camera ID'}), 400

    service_name = f'sailframes-camera-{camera_id}'
    try:
        result = subprocess.run(
            ['sudo', 'systemctl', 'stop', service_name],
            capture_output=True, text=True, timeout=10
        )
        success = result.returncode == 0
        if success:
            logger.info(f"Camera {camera_id} stopped via API")
        else:
            logger.warning(f"Failed to stop camera {camera_id}: {result.stderr}")
        return jsonify({'success': success, 'error': result.stderr if not success else None})
    except Exception as e:
        logger.error(f"Camera {camera_id} stop error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/recording/start', methods=['POST'])
def api_recording_start():
    """Start recording on all sensor services."""
    results = {}
    services = ['sailframes-gps', 'sailframes-imu', 'sailframes-pressure', 'sailframes-wind',
                'sailframes-camera-cockpit', 'sailframes-camera-sails']

    for service in services:
        try:
            result = subprocess.run(
                ['sudo', 'systemctl', 'start', service],
                capture_output=True, text=True, timeout=10
            )
            results[service] = result.returncode == 0
        except Exception as e:
            results[service] = False
            logger.error(f"Failed to start {service}: {e}")

    success = all(results.values())
    if success:
        logger.info("All recording services started via API")
    else:
        failed = [s for s, ok in results.items() if not ok]
        logger.warning(f"Some services failed to start: {failed}")

    return jsonify({'success': success, 'results': results})


@app.route('/api/recording/stop', methods=['POST'])
def api_recording_stop():
    """Stop camera recording only. Sensor services keep running for live dashboard data."""
    results = {}
    # Only stop cameras - sensor services continue running for live data display
    services = ['sailframes-camera-cockpit', 'sailframes-camera-sails']

    for service in services:
        try:
            result = subprocess.run(
                ['sudo', 'systemctl', 'stop', service],
                capture_output=True, text=True, timeout=10
            )
            results[service] = result.returncode == 0
        except Exception as e:
            results[service] = False
            logger.error(f"Failed to stop {service}: {e}")

    success = all(results.values())
    if success:
        logger.info("Camera recording stopped via API (sensors still running)")
    else:
        failed = [s for s, ok in results.items() if not ok]
        logger.warning(f"Some cameras failed to stop: {failed}")

    return jsonify({'success': success, 'results': results})


@app.route('/api/recording/status')
def api_recording_status():
    """Get recording status for all sensors."""
    status = {
        'gps': {
            'service_running': check_service_status('sailframes-gps'),
            'connected': False,
            'recording': False,
        },
        'imu': {
            'service_running': check_service_status('sailframes-imu'),
            'connected': False,
            'recording': False,
        },
        'pressure': {
            'service_running': check_service_status('sailframes-pressure'),
            'connected': False,
            'recording': False,
        },
        'wind': {
            'service_running': check_service_status('sailframes-wind'),
            'connected': False,
            'recording': False,
        },
        'camera_cockpit': {
            'service_running': check_service_status('sailframes-camera-cockpit'),
            'connected': True,  # Camera assumed connected if service runs
            'recording': False,
        },
        'camera_sails': {
            'service_running': check_service_status('sailframes-camera-sails'),
            'connected': True,
            'recording': False,
        },
    }

    # Check actual sensor connections from status files
    gps_status = get_gps_status()
    if gps_status and gps_status.get('connected'):
        status['gps']['connected'] = True
        status['gps']['recording'] = status['gps']['service_running']

    imu_status = get_imu_status()
    if imu_status and imu_status.get('connected'):
        status['imu']['connected'] = True
        status['imu']['recording'] = status['imu']['service_running']

    pressure_status = get_pressure_status()
    if pressure_status and pressure_status.get('connected'):
        status['pressure']['connected'] = True
        status['pressure']['recording'] = status['pressure']['service_running']

    wind_status = get_wind_status()
    if wind_status and wind_status.get('connected'):
        status['wind']['connected'] = True
        status['wind']['recording'] = status['wind']['service_running']

    # Cameras record if service is running
    status['camera_cockpit']['recording'] = status['camera_cockpit']['service_running']
    status['camera_sails']['recording'] = status['camera_sails']['service_running']

    # Overall recording state
    any_recording = any(s['recording'] for s in status.values())
    all_recording = all(s['recording'] for s in status.values())

    return jsonify({
        'sensors': status,
        'any_recording': any_recording,
        'all_recording': all_recording,
    })


@app.route('/api/camera/<camera_id>/snapshot', methods=['POST'])
def api_camera_snapshot(camera_id):
    """Capture a single frame from the camera.

    If the camera is currently recording, extracts a frame from the latest
    video file using ffmpeg. Otherwise, uses rpicam-still for direct capture.
    """
    if camera_id not in ('cockpit', 'sails'):
        return jsonify({'success': False, 'error': 'Invalid camera'}), 400

    try:
        # Output path
        snap_dir = Path('/tmp/sailframes-snapshots')
        snap_dir.mkdir(exist_ok=True)
        snap_path = snap_dir / f'{camera_id}.jpg'

        # Check if camera service is recording (camera would be busy)
        service_name = f'sailframes-camera-{camera_id}'
        service_check = subprocess.run(
            ['systemctl', 'is-active', service_name],
            capture_output=True, text=True
        )
        camera_recording = service_check.stdout.strip() == 'active'

        if camera_recording:
            # Camera is busy recording - extract frame from current video
            # Find the latest video file for this camera
            today = datetime.now().strftime('%Y-%m-%d')
            _config = load_config()
            data_dir = _config['storage']['data_dir']
            video_dir = Path(f'{data_dir}/{today}/video/{camera_id}')

            if not video_dir.exists():
                return jsonify({'success': False, 'error': 'No video directory found'}), 404

            # Get completed mp4 files (not the one currently being recorded)
            # MP4 files can't be read until recording completes (moov atom at end)
            video_files = sorted(video_dir.glob(f'{camera_id}_*.mp4'), key=lambda p: p.stat().st_mtime, reverse=True)
            if not video_files:
                return jsonify({'success': False, 'error': 'No video files found'}), 404

            # Skip the newest file (currently recording), use the previous completed segment
            if len(video_files) > 1:
                completed_video = video_files[1]  # Second newest = most recent completed
            else:
                # Only one file exists and it's being recorded - can't preview yet
                return jsonify({
                    'success': False,
                    'error': 'Recording in progress - preview available after first segment completes'
                }), 503

            logger.info(f"Extracting frame from completed segment: {completed_video}")

            # Extract the last frame from completed video
            result = subprocess.run(
                [
                    'ffmpeg', '-y',
                    '-sseof', '-1',  # Seek to 1 second before end
                    '-i', str(completed_video),
                    '-frames:v', '1',
                    '-q:v', '2',  # High quality JPEG
                    str(snap_path)
                ],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0 and snap_path.exists():
                logger.info(f"Snapshot extracted from completed segment for {camera_id}")
                # Calculate age of the snapshot (time since segment ended)
                segment_age = int(time.time() - completed_video.stat().st_mtime)
                return jsonify({
                    'success': True,
                    'url': f'/api/camera/{camera_id}/snapshot.jpg',
                    'source': 'completed_segment',
                    'segment_age_sec': segment_age
                })
            else:
                error = result.stderr.split('\n')[-2] if result.stderr else 'Frame extraction failed'
                logger.warning(f"Frame extraction failed for {camera_id}: {error}")
                return jsonify({'success': False, 'error': error}), 500
        else:
            # Camera not recording - use direct capture
            camera_index = 0 if camera_id == 'cockpit' else 1

            result = subprocess.run(
                [
                    'rpicam-still',
                    '--camera', str(camera_index),
                    '-o', str(snap_path),
                    '--width', '1920',
                    '--height', '1080',
                    '--timeout', '1000',
                    '--nopreview',
                ],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0 and snap_path.exists():
                logger.info(f"Snapshot captured for {camera_id}")
                return jsonify({
                    'success': True,
                    'url': f'/api/camera/{camera_id}/snapshot.jpg',
                    'source': 'camera'
                })
            else:
                error = result.stderr or 'Capture failed'
                logger.warning(f"Snapshot failed for {camera_id}: {error}")
                return jsonify({'success': False, 'error': error}), 500

    except subprocess.TimeoutExpired:
        return jsonify({'success': False, 'error': 'Camera timeout'}), 500
    except Exception as e:
        logger.error(f"Snapshot error for {camera_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/camera/<camera_id>/snapshot.jpg')
def api_camera_snapshot_image(camera_id):
    """Serve the captured snapshot image."""
    if camera_id not in ('cockpit', 'sails'):
        return 'Invalid camera', 404

    snap_path = Path(f'/tmp/sailframes-snapshots/{camera_id}.jpg')
    if snap_path.exists():
        return send_file(snap_path, mimetype='image/jpeg')
    else:
        return 'No snapshot available', 404


@app.route('/api/system/shutdown', methods=['POST'])
def api_system_shutdown():
    """Safely shutdown the system - stops all services first to ensure data is saved."""
    try:
        logger.warning("Shutdown requested via API - initiating safe shutdown")

        # Stop all SailFrames services in order to ensure clean file closure
        # Order: cameras first (video files), then sensors, then battery logger
        services_to_stop = [
            'sailframes-camera-cockpit',
            'sailframes-camera-sails',
            'sailframes-gps',
            'sailframes-imu',
            'sailframes-pressure',
            'sailframes-wind',
            'sailframes-battery-logger',
        ]

        for service in services_to_stop:
            try:
                result = subprocess.run(
                    ['sudo', 'systemctl', 'stop', service],
                    capture_output=True, timeout=15
                )
                if result.returncode == 0:
                    logger.info(f"Stopped {service}")
            except subprocess.TimeoutExpired:
                logger.warning(f"Timeout stopping {service}")
            except Exception as e:
                logger.warning(f"Error stopping {service}: {e}")

        logger.info("All services stopped - initiating system shutdown")

        # Initiate shutdown
        subprocess.Popen(
            ['sudo', 'shutdown', '-h', '+0'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        return jsonify({'success': True, 'message': 'Shutdown initiated - all data saved'})
    except Exception as e:
        logger.error(f"Shutdown error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bluetooth/scan', methods=['POST'])
def api_bluetooth_scan():
    """Scan for Bluetooth LE devices."""
    import asyncio
    try:
        from bleak import BleakScanner
    except ImportError:
        return jsonify({'success': False, 'error': 'bleak not installed'}), 500

    async def do_scan():
        devices = await BleakScanner.discover(timeout=15)
        results = []
        for d in devices:
            name = d.name or ''
            is_wind = 'calypso' in name.lower() or 'ultrasonic' in name.lower() or 'wind' in name.lower()
            results.append({
                'name': d.name,
                'address': d.address,
                'rssi': d.rssi if hasattr(d, 'rssi') else None,
                'is_wind_sensor': is_wind,
            })
        # Sort: wind sensors first, then by signal strength
        results.sort(key=lambda x: (not x['is_wind_sensor'], -(x['rssi'] or -100)))
        return results

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        devices = loop.run_until_complete(do_scan())
        loop.close()
        return jsonify({'success': True, 'devices': devices})
    except Exception as e:
        logger.error(f"Bluetooth scan error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bluetooth/set-wind', methods=['POST'])
def api_bluetooth_set_wind():
    """Save wind sensor MAC address to config."""
    data = request.get_json() or {}
    address = data.get('address')
    if not address:
        return jsonify({'success': False, 'error': 'address required'}), 400

    # Update config file
    config_path = '/etc/sailframes/sailframes.yaml'
    if not os.path.exists(config_path):
        config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'sailframes.yaml')

    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

        config['wind']['ble_mac_address'] = address

        with open(config_path, 'w') as f:
            yaml.dump(config, f, default_flow_style=False)

        logger.info(f"Set wind sensor MAC to {address}")

        # Restart wind service to pick up new MAC
        subprocess.run(['sudo', 'systemctl', 'restart', 'sailframes-wind'],
                      capture_output=True, timeout=10)

        return jsonify({'success': True, 'address': address})
    except Exception as e:
        logger.error(f"Failed to save wind MAC: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/wind/status')
def api_wind_status():
    """Get current wind sensor status."""
    return jsonify(get_wind_status() or {'connected': False})


@app.route('/api/wind/restart', methods=['POST'])
def api_wind_restart():
    """Restart wind sensor service."""
    try:
        # Kill first (faster than stop), then start
        subprocess.run(
            ['sudo', 'systemctl', 'kill', 'sailframes-wind'],
            capture_output=True, text=True, timeout=5
        )
        time.sleep(1)
        result = subprocess.run(
            ['sudo', 'systemctl', 'start', 'sailframes-wind'],
            capture_output=True, text=True, timeout=10
        )
        success = result.returncode == 0
        if success:
            logger.info("Wind service restarted via API")
        else:
            logger.warning(f"Failed to restart wind service: {result.stderr}")
        return jsonify({'success': success, 'error': result.stderr if not success else None})
    except Exception as e:
        logger.error(f"Wind restart error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/imu/status')
def api_imu_status():
    """Get current IMU sensor status for live updates."""
    return jsonify(get_imu_status() or {'connected': False})


@app.route('/api/imu/calibrate', methods=['POST'])
def api_imu_calibrate():
    """
    Zero heel and pitch offsets based on current IMU readings.
    Call this when boat is level at the dock.
    """
    try:
        imu_status = get_imu_status()
        if not imu_status or not imu_status.get('connected'):
            return jsonify({'success': False, 'error': 'IMU not connected'}), 400

        # Get raw values (before any existing calibration)
        raw_heel = imu_status.get('raw_heel_deg')
        raw_pitch = imu_status.get('raw_pitch_deg')

        if raw_heel is None or raw_pitch is None:
            return jsonify({'success': False, 'error': 'No IMU readings available'}), 400

        calibration_file = Path('/etc/sailframes/imu-calibration.json')
        calibration_file.parent.mkdir(parents=True, exist_ok=True)

        # Load existing calibration to preserve settings like swap_axes, invert_*
        existing = {}
        if calibration_file.exists():
            try:
                with open(calibration_file, 'r') as f:
                    existing = json.load(f)
            except:
                pass

        # If swap is enabled, swap the raw values before calculating offsets
        swap_enabled = existing.get('swap_axes', False)
        if swap_enabled:
            raw_heel, raw_pitch = raw_pitch, raw_heel

        # If inversion is enabled, apply it before calculating offsets
        if existing.get('invert_heel', False):
            raw_heel = -raw_heel
        if existing.get('invert_pitch', False):
            raw_pitch = -raw_pitch

        # Update calibration, preserving other settings
        calibration = {
            **existing,  # Preserve swap_axes, invert_heel, invert_pitch
            'heel_offset': raw_heel,
            'pitch_offset': raw_pitch,
            'calibrated_at': datetime.now(timezone.utc).isoformat(),
        }

        with open(calibration_file, 'w') as f:
            json.dump(calibration, f, indent=2)

        logger.info(f"IMU calibrated: heel_offset={raw_heel:.2f}°, pitch_offset={raw_pitch:.2f}° (swap={swap_enabled})")

        return jsonify({
            'success': True,
            'heel_offset': raw_heel,
            'pitch_offset': raw_pitch,
        })

    except Exception as e:
        logger.error(f"IMU calibration failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/imu/calibration', methods=['GET'])
def api_imu_calibration():
    """Get current IMU calibration offsets."""
    try:
        calibration_file = Path('/etc/sailframes/imu-calibration.json')
        if calibration_file.exists():
            with open(calibration_file, 'r') as f:
                return jsonify(json.load(f))
        return jsonify({'heel_offset': 0, 'pitch_offset': 0, 'calibrated_at': None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/imu/calibration/reset', methods=['POST'])
def api_imu_calibration_reset():
    """Reset IMU calibration to zero offsets."""
    try:
        calibration_file = Path('/etc/sailframes/imu-calibration.json')
        if calibration_file.exists():
            calibration_file.unlink()
        logger.info("IMU calibration reset to zero")
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/imu/invert', methods=['POST'])
def api_imu_invert():
    """Toggle inversion of heel or pitch axis."""
    try:
        data = request.get_json()
        axis = data.get('axis')  # 'heel' or 'pitch'
        if axis not in ('heel', 'pitch'):
            return jsonify({'success': False, 'error': 'Invalid axis'}), 400

        calibration_file = Path('/etc/sailframes/imu-calibration.json')

        # Load existing calibration
        if calibration_file.exists():
            with open(calibration_file, 'r') as f:
                cal_data = json.load(f)
        else:
            cal_data = {}

        # Toggle the inversion
        key = f'invert_{axis}'
        cal_data[key] = not cal_data.get(key, False)

        # Save
        with open(calibration_file, 'w') as f:
            json.dump(cal_data, f, indent=2)

        logger.info(f"IMU {axis} inversion set to {cal_data[key]}")
        return jsonify({'success': True, 'axis': axis, 'inverted': cal_data[key]})
    except Exception as e:
        logger.error(f"Failed to toggle IMU inversion: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/imu/swap', methods=['POST'])
def api_imu_swap():
    """Toggle swapping of heel and pitch axes."""
    try:
        calibration_file = Path('/etc/sailframes/imu-calibration.json')

        # Load existing calibration
        if calibration_file.exists():
            with open(calibration_file, 'r') as f:
                cal_data = json.load(f)
        else:
            cal_data = {}

        # Toggle the swap
        cal_data['swap_axes'] = not cal_data.get('swap_axes', False)

        # Save
        with open(calibration_file, 'w') as f:
            json.dump(cal_data, f, indent=2)

        logger.info(f"IMU swap_axes set to {cal_data['swap_axes']}")
        return jsonify({'success': True, 'swap_axes': cal_data['swap_axes']})
    except Exception as e:
        logger.error(f"Failed to toggle IMU swap: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/gps/restart', methods=['POST'])
def api_gps_restart():
    """Restart the GPS service."""
    try:
        result = subprocess.run(
            ['sudo', 'systemctl', 'restart', 'sailframes-gps'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            logger.info("GPS service restarted")
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': result.stderr}), 500
    except Exception as e:
        logger.error(f"Failed to restart GPS: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/gps/status')
def api_gps_status():
    """Get current GPS connection status."""
    return jsonify(get_gps_status())


# ── WiFi Mode API ──

def get_wifi_status():
    """Get current WiFi mode and connection status."""
    try:
        # Get current mode from script
        result = subprocess.run(
            ['/home/paul/sailframes/scripts/wifi-mode.sh', 'current'],
            capture_output=True, text=True, timeout=5
        )
        current_mode = result.stdout.strip() if result.returncode == 0 else 'unknown'

        # Get saved mode
        mode_file = Path('/etc/sailframes/wifi-mode')
        saved_mode = mode_file.read_text().strip() if mode_file.exists() else 'ap'

        # Get connection details
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'GENERAL.CONNECTION,IP4.ADDRESS', 'device', 'show', 'wlan0'],
            capture_output=True, text=True, timeout=5
        )

        connection = None
        ip_address = None
        for line in result.stdout.split('\n'):
            if line.startswith('GENERAL.CONNECTION:'):
                connection = line.split(':', 1)[1]
            elif line.startswith('IP4.ADDRESS'):
                ip_address = line.split(':', 1)[1].split('/')[0] if ':' in line else None

        return {
            'current_mode': current_mode,
            'saved_mode': saved_mode,
            'connection': connection,
            'ip_address': ip_address,
            'ap_ssid': 's1',
            'ap_password': 'hellowifi',
            'client_ssid': 'Home-IOT',
        }
    except Exception as e:
        logger.error(f"WiFi status error: {e}")
        return {'current_mode': 'unknown', 'error': str(e)}


@app.route('/api/wifi/status')
def api_wifi_status():
    """Get current WiFi mode and status."""
    return jsonify(get_wifi_status())


@app.route('/api/wifi/mode', methods=['POST'])
def api_wifi_mode():
    """Switch WiFi mode (ap/client/toggle)."""
    try:
        data = request.get_json() or {}
        mode = data.get('mode', 'toggle')

        if mode not in ['ap', 'client', 'toggle']:
            return jsonify({'success': False, 'error': 'Invalid mode. Use: ap, client, or toggle'}), 400

        logger.info(f"Switching WiFi mode: {mode}")

        result = subprocess.run(
            ['sudo', '/home/paul/sailframes/scripts/wifi-mode.sh', mode],
            capture_output=True, text=True, timeout=30
        )

        if result.returncode != 0:
            return jsonify({
                'success': False,
                'error': result.stderr or 'WiFi mode switch failed'
            }), 500

        # Get new status
        status = get_wifi_status()

        return jsonify({
            'success': True,
            'mode': status['current_mode'],
            'output': result.stdout,
        })

    except subprocess.TimeoutExpired:
        return jsonify({'success': False, 'error': 'WiFi switch timed out'}), 500
    except Exception as e:
        logger.error(f"WiFi mode switch failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/netdata/start', methods=['POST'])
def netdata_start():
    """Start netdata service."""
    try:
        result = subprocess.run(
            ['sudo', 'systemctl', 'start', 'netdata'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            logger.info("Netdata started")
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': result.stderr}), 500
    except Exception as e:
        logger.error(f"Failed to start netdata: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/netdata/stop', methods=['POST'])
def netdata_stop():
    """Stop netdata service."""
    try:
        result = subprocess.run(
            ['sudo', 'systemctl', 'stop', 'netdata'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            logger.info("Netdata stopped")
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': result.stderr}), 500
    except Exception as e:
        logger.error(f"Failed to stop netdata: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def run(config):
    global _config
    _config = config  # Store config for video routes

    monitor_config = config['monitor']
    port = monitor_config['web_port']

    # Start monitor thread
    monitor_thread = threading.Thread(target=monitor_loop, args=(config,), daemon=True)
    monitor_thread.start()
    logger.info("Monitor thread started")

    # Start web dashboard
    logger.info(f"Dashboard at http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)


if __name__ == '__main__':
    config = load_config()
    if not config['monitor']['enabled']:
        logger.info("Monitor disabled in config, exiting")
        sys.exit(0)
    run(config)
