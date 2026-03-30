#!/usr/bin/env python3
"""
SailFrames Battery Logger
Logs battery metrics and process power usage for analysis.
- Tracks battery sessions (unplug to plug)
- Logs voltage, current, percentage over time
- Records top CPU-consuming processes
- Stores data in CSV files for dashboard review
"""

import os
import sys
import csv
import time
import signal
import logging
import psutil
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [BATTERY] %(levelname)s %(message)s'
)
logger = logging.getLogger('sailframes.battery')

# Configuration
LOG_INTERVAL = 30  # Log every 30 seconds
DATA_DIR = Path('/mnt/sailframes-data/battery')
INA219_ADDR = 0x43
SHUNT_OHMS = 0.1
VOUT_FULL = 4.2
VOUT_EMPTY = 3.4

# Voltage warning thresholds
VOLTAGE_WARNING = 3.6      # Warning threshold
VOLTAGE_CRITICAL = 3.5     # Critical threshold - shutdown imminent
VOLTAGE_SAG_THRESHOLD = 0.1  # Warn if voltage drops this much between readings

running = True
current_session = None


def signal_handler(sig, frame):
    global running
    logger.info("Shutdown signal received")
    running = False


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


def get_battery_info():
    """Read battery metrics from INA219."""
    try:
        import smbus2
        bus = smbus2.SMBus(1)

        # Read voltage
        raw_bus = bus.read_word_data(INA219_ADDR, 0x02)
        raw_bus = ((raw_bus & 0xFF) << 8) | ((raw_bus >> 8) & 0xFF)
        voltage = (raw_bus >> 3) * 0.004

        # Read current
        raw_shunt = bus.read_word_data(INA219_ADDR, 0x01)
        raw_shunt = ((raw_shunt & 0xFF) << 8) | ((raw_shunt >> 8) & 0xFF)
        if raw_shunt > 32767:
            raw_shunt -= 65536
        shunt_mv = raw_shunt * 0.01
        current_ma = shunt_mv / SHUNT_OHMS

        bus.close()

        # Calculate percentage
        percent = (voltage - VOUT_EMPTY) / (VOUT_FULL - VOUT_EMPTY) * 100
        percent = max(0, min(100, percent))

        # Power in milliwatts
        power_mw = voltage * abs(current_ma)

        return {
            'voltage': round(voltage, 3),
            'current_ma': round(current_ma, 1),
            'percent': round(percent, 1),
            'power_mw': round(power_mw, 1),
            'charging': current_ma < 0,
        }
    except Exception as e:
        logger.error(f"Battery read error: {e}")
        return None


def get_top_processes(n=5):
    """Get top N CPU-consuming processes."""
    processes = []
    for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'cmdline']):
        try:
            pinfo = proc.info
            if pinfo['cpu_percent'] > 0:
                name = pinfo['name']
                # For python processes, extract the script name
                cmdline = pinfo.get('cmdline') or []
                if name in ('python3', 'python') and len(cmdline) >= 2:
                    # cmdline is like ['python3', '/path/to/script.py', ...]
                    script = cmdline[1]
                    # Extract just the filename without path
                    if '/' in script:
                        script = script.split('/')[-1]
                    # Remove .py extension for cleaner display
                    if script.endswith('.py'):
                        script = script[:-3]
                    name = script
                processes.append({
                    'pid': pinfo['pid'],
                    'name': name[:30],
                    'cpu_percent': round(pinfo['cpu_percent'], 1),
                    'memory_percent': round(pinfo['memory_percent'], 1),
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Sort by CPU usage
    processes.sort(key=lambda x: x['cpu_percent'], reverse=True)
    return processes[:n]


def get_system_stats():
    """Get system-wide stats."""
    return {
        'cpu_percent': psutil.cpu_percent(),
        'memory_percent': psutil.virtual_memory().percent,
        'cpu_temp': get_cpu_temp(),
    }


def get_cpu_temp():
    """Read CPU temperature."""
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            return round(int(f.read().strip()) / 1000, 1)
    except:
        return None


class BatterySession:
    """Tracks a single battery discharge session."""

    def __init__(self, start_time, start_percent, start_voltage):
        self.session_id = start_time.strftime('%Y%m%d_%H%M%S')
        self.start_time = start_time
        self.start_percent = start_percent
        self.start_voltage = start_voltage
        self.end_time = None
        self.end_percent = None
        self.end_voltage = None
        self.min_voltage = start_voltage
        self.max_current_ma = 0
        self.total_power_mwh = 0
        self.sample_count = 0

        # Create session log file
        self.log_file = DATA_DIR / f'session_{self.session_id}.csv'
        self.process_file = DATA_DIR / f'processes_{self.session_id}.csv'

        # Initialize CSV files
        with open(self.log_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['timestamp', 'voltage', 'current_ma', 'percent',
                           'power_mw', 'cpu_percent', 'memory_percent', 'cpu_temp'])

        with open(self.process_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['timestamp', 'pid', 'name', 'cpu_percent', 'memory_percent'])

        logger.info(f"Started battery session {self.session_id} at {start_percent}%")

    def log_sample(self, battery, system, processes):
        """Log a single sample to the session."""
        timestamp = datetime.now(timezone.utc).isoformat()

        # Update session stats
        self.sample_count += 1
        if battery['voltage'] < self.min_voltage:
            self.min_voltage = battery['voltage']
        if battery['current_ma'] > self.max_current_ma:
            self.max_current_ma = battery['current_ma']

        # Accumulate power (mWh = mW * hours)
        hours = LOG_INTERVAL / 3600
        self.total_power_mwh += battery['power_mw'] * hours

        # Write battery log
        with open(self.log_file, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                timestamp,
                battery['voltage'],
                battery['current_ma'],
                battery['percent'],
                battery['power_mw'],
                system['cpu_percent'],
                system['memory_percent'],
                system['cpu_temp'],
            ])

        # Write process log
        with open(self.process_file, 'a', newline='') as f:
            writer = csv.writer(f)
            for proc in processes:
                writer.writerow([
                    timestamp,
                    proc['pid'],
                    proc['name'],
                    proc['cpu_percent'],
                    proc['memory_percent'],
                ])

    def end(self, end_percent, end_voltage):
        """End the session and write summary if meaningful."""
        self.end_time = datetime.now(timezone.utc)
        self.end_percent = end_percent
        self.end_voltage = end_voltage

        duration = self.end_time - self.start_time
        duration_mins = duration.total_seconds() / 60
        percent_used = self.start_percent - self.end_percent

        # Skip sessions that are too short or didn't use meaningful power
        # This filters out noise from USB-C current fluctuations
        MIN_SAMPLES = 2  # At least 2 samples (~1 minute at 30s interval)
        MIN_POWER_MWH = 1.0  # At least 1 mWh used

        if self.sample_count < MIN_SAMPLES or self.total_power_mwh < MIN_POWER_MWH:
            logger.info(f"Discarding short session {self.session_id}: "
                       f"{self.sample_count} samples, {self.total_power_mwh:.1f} mWh")
            # Clean up temp files for discarded session
            try:
                self.log_file.unlink(missing_ok=True)
                self.process_file.unlink(missing_ok=True)
            except Exception:
                pass
            return None

        summary = {
            'session_id': self.session_id,
            'start_time': self.start_time.isoformat(),
            'end_time': self.end_time.isoformat(),
            'duration_minutes': round(duration_mins, 1),
            'start_percent': self.start_percent,
            'end_percent': self.end_percent,
            'percent_used': round(percent_used, 1),
            'start_voltage': self.start_voltage,
            'end_voltage': self.end_voltage,
            'min_voltage': self.min_voltage,
            'max_current_ma': self.max_current_ma,
            'total_power_mwh': round(self.total_power_mwh, 1),
            'sample_count': self.sample_count,
        }

        # Append to sessions summary file
        summary_file = DATA_DIR / 'sessions.csv'
        write_header = not summary_file.exists()

        with open(summary_file, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=summary.keys())
            if write_header:
                writer.writeheader()
            writer.writerow(summary)

        logger.info(f"Ended session {self.session_id}: {duration_mins:.1f} min, "
                   f"{percent_used:.1f}% used, {self.total_power_mwh:.1f} mWh")

        return summary


def get_daily_log_file():
    """Get path to today's continuous battery log file."""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return DATA_DIR / f'battery_{today}.csv'


def ensure_daily_log_header(log_file):
    """Create daily log file with header if it doesn't exist."""
    if not log_file.exists():
        with open(log_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp', 'voltage', 'current_ma', 'percent', 'power_mw',
                'charging', 'cpu_percent', 'memory_percent', 'cpu_temp'
            ])
        return True
    return False


def run():
    global current_session, running

    # Ensure data directory exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"Battery logger started, logging to {DATA_DIR}")

    # Initial CPU measurement to prime psutil
    psutil.cpu_percent()
    for proc in psutil.process_iter(['cpu_percent']):
        try:
            proc.cpu_percent()
        except:
            pass
    time.sleep(1)

    last_charging = None
    # Debounce counter - require multiple consecutive readings to change state
    # This prevents false sessions from momentary current fluctuations
    DEBOUNCE_COUNT = 3  # Require 3 consecutive readings (~1.5 min) to confirm state change
    discharge_count = 0
    charge_count = 0
    rows_logged = 0
    current_log_file = None
    last_voltage = None
    last_warning_time = 0

    while running:
        battery = get_battery_info()
        if battery is None:
            time.sleep(LOG_INTERVAL)
            continue

        voltage = battery['voltage']
        current_time = time.time()

        # Voltage warnings (throttled to once per minute)
        if current_time - last_warning_time > 60:
            if voltage < VOLTAGE_CRITICAL:
                logger.warning(f"CRITICAL: Battery voltage {voltage:.3f}V - shutdown imminent!")
                last_warning_time = current_time
            elif voltage < VOLTAGE_WARNING:
                logger.warning(f"LOW VOLTAGE: Battery at {voltage:.3f}V ({battery['percent']:.0f}%)")
                last_warning_time = current_time

        # Detect voltage sag (sudden drop)
        if last_voltage is not None:
            voltage_drop = last_voltage - voltage
            if voltage_drop >= VOLTAGE_SAG_THRESHOLD:
                logger.warning(f"VOLTAGE SAG: Dropped {voltage_drop:.3f}V ({last_voltage:.3f}V -> {voltage:.3f}V) - possible power issue")
        last_voltage = voltage

        system = get_system_stats()
        processes = get_top_processes()

        # Always log to daily continuous file
        daily_log = get_daily_log_file()
        if daily_log != current_log_file:
            if ensure_daily_log_header(daily_log):
                logger.info(f"Created new daily log: {daily_log}")
            current_log_file = daily_log
            rows_logged = 0

        timestamp = datetime.now(timezone.utc).isoformat()
        with open(daily_log, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                timestamp,
                battery['voltage'],
                battery['current_ma'],
                battery['percent'],
                battery['power_mw'],
                'charging' if battery['charging'] else 'discharging',
                system['cpu_percent'],
                system['memory_percent'],
                system['cpu_temp'],
            ])
        rows_logged += 1

        # Log status every 60 samples (~30 min)
        if rows_logged % 60 == 0:
            state = 'charging' if battery['charging'] else 'discharging'
            logger.info(f"Battery: {battery['percent']}% {battery['voltage']}V "
                       f"{battery['current_ma']}mA ({state})")

        # Debounced charging state detection for session tracking
        if battery['charging']:
            charge_count += 1
            discharge_count = 0
        else:
            discharge_count += 1
            charge_count = 0

        # Detect charging state change with debouncing
        if last_charging is not None:
            if last_charging and discharge_count >= DEBOUNCE_COUNT:
                # Confirmed discharging - begin new session
                current_session = BatterySession(
                    datetime.now(timezone.utc),
                    battery['percent'],
                    battery['voltage']
                )
                logger.info(f"Started discharge session at {battery['percent']}%")
            elif not last_charging and charge_count >= DEBOUNCE_COUNT:
                # Confirmed charging - end session
                if current_session:
                    current_session.end(battery['percent'], battery['voltage'])
                    current_session = None

        # Update last_charging only when state is confirmed
        if charge_count >= DEBOUNCE_COUNT:
            last_charging = True
        elif discharge_count >= DEBOUNCE_COUNT:
            last_charging = False
        elif last_charging is None:
            # Initial state - use current reading
            last_charging = battery['charging']

        # Also log to session file if we're in a discharge session
        if current_session and not battery['charging']:
            current_session.log_sample(battery, system, processes)

        time.sleep(LOG_INTERVAL)

    # Clean shutdown - end any active session
    if current_session:
        battery = get_battery_info()
        if battery:
            current_session.end(battery['percent'], battery['voltage'])

    logger.info("Battery logger stopped")


if __name__ == '__main__':
    run()
