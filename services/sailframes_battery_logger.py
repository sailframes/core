#!/usr/bin/env python3
"""
SailFrames Power Usage Logger
Logs system resource usage and process stats for power analysis.
- Records top CPU-consuming processes
- Tracks CPU, memory, temperature over time
- Optionally logs battery metrics if INA219 sensor present
- Works without battery sensor (USB-C power bank mode)
- Stores data in CSV files for dashboard review
"""

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
    except Exception:
        # No battery sensor present - this is normal for USB-C power bank mode
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


def get_daily_log_file():
    """Get path to today's continuous power log file."""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return DATA_DIR / f'power_{today}.csv'


def get_daily_process_file():
    """Get path to today's process log file."""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return DATA_DIR / f'processes_{today}.csv'


def ensure_daily_log_header(log_file):
    """Create daily log file with header if it doesn't exist."""
    if not log_file.exists():
        with open(log_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp', 'cpu_percent', 'memory_percent', 'cpu_temp',
                'voltage', 'current_ma', 'percent', 'power_mw', 'charging'
            ])
        return True
    return False


def ensure_process_log_header(log_file):
    """Create process log file with header if it doesn't exist."""
    if not log_file.exists():
        with open(log_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['timestamp', 'pid', 'name', 'cpu_percent', 'memory_percent'])
        return True
    return False


def run():
    global running

    # Ensure data directory exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"Power usage logger started, logging to {DATA_DIR}")

    # Initial CPU measurement to prime psutil
    psutil.cpu_percent()
    for proc in psutil.process_iter(['cpu_percent']):
        try:
            proc.cpu_percent()
        except:
            pass
    time.sleep(1)

    rows_logged = 0
    current_log_file = None
    current_process_file = None
    last_voltage = None
    last_warning_time = 0
    battery_available = None  # None = unknown, True/False after first check

    while running:
        # Get system stats (always available)
        system = get_system_stats()
        processes = get_top_processes()

        # Try to get battery info (optional - may not have sensor)
        battery = get_battery_info()

        # Log battery sensor availability once
        if battery_available is None:
            battery_available = battery is not None
            if battery_available:
                logger.info("Battery sensor (INA219) detected")
            else:
                logger.info("No battery sensor - logging system stats only (USB-C power bank mode)")

        # Battery voltage warnings (only if sensor present)
        if battery:
            voltage = battery['voltage']
            current_time = time.time()

            if current_time - last_warning_time > 60:
                if voltage < VOLTAGE_CRITICAL:
                    logger.warning(f"CRITICAL: Battery voltage {voltage:.3f}V - shutdown imminent!")
                    last_warning_time = current_time
                elif voltage < VOLTAGE_WARNING:
                    logger.warning(f"LOW VOLTAGE: Battery at {voltage:.3f}V ({battery['percent']:.0f}%)")
                    last_warning_time = current_time

            # Detect voltage sag
            if last_voltage is not None:
                voltage_drop = last_voltage - voltage
                if voltage_drop >= VOLTAGE_SAG_THRESHOLD:
                    logger.warning(f"VOLTAGE SAG: Dropped {voltage_drop:.3f}V")
            last_voltage = voltage

        # Ensure daily log files exist
        daily_log = get_daily_log_file()
        process_log = get_daily_process_file()

        if daily_log != current_log_file:
            if ensure_daily_log_header(daily_log):
                logger.info(f"Created new daily log: {daily_log}")
            current_log_file = daily_log
            rows_logged = 0

        if process_log != current_process_file:
            if ensure_process_log_header(process_log):
                logger.info(f"Created new process log: {process_log}")
            current_process_file = process_log

        timestamp = datetime.now(timezone.utc).isoformat()

        # Log system stats (with optional battery data)
        with open(daily_log, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                timestamp,
                system['cpu_percent'],
                system['memory_percent'],
                system['cpu_temp'],
                battery['voltage'] if battery else '',
                battery['current_ma'] if battery else '',
                battery['percent'] if battery else '',
                battery['power_mw'] if battery else '',
                ('charging' if battery['charging'] else 'discharging') if battery else '',
            ])

        # Log top processes
        with open(process_log, 'a', newline='') as f:
            writer = csv.writer(f)
            for proc in processes:
                writer.writerow([
                    timestamp,
                    proc['pid'],
                    proc['name'],
                    proc['cpu_percent'],
                    proc['memory_percent'],
                ])

        rows_logged += 1

        # Log status every 60 samples (~30 min)
        if rows_logged % 60 == 0:
            if battery:
                state = 'charging' if battery['charging'] else 'discharging'
                logger.info(f"Battery: {battery['percent']}% {battery['voltage']}V "
                           f"{battery['current_ma']}mA ({state}) | "
                           f"CPU: {system['cpu_percent']}% Temp: {system['cpu_temp']}C")
            else:
                logger.info(f"CPU: {system['cpu_percent']}% Mem: {system['memory_percent']}% "
                           f"Temp: {system['cpu_temp']}C")

        time.sleep(LOG_INTERVAL)

    logger.info("Power usage logger stopped")


if __name__ == '__main__':
    run()
