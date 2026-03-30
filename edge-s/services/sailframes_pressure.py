#!/usr/bin/env python3
"""
SailFrames Pressure Service
Reads DPS310 precision barometric pressure sensor via I2C.
Logs pressure, temperature, and computed sea-level pressure.
"""

import os
import sys
import csv
import json
import time
import signal
import logging
from datetime import datetime, timezone
from pathlib import Path

import board
import busio
from adafruit_dps310 import DPS310
from adafruit_dps310.advanced import Rate, SampleCount, Mode
import yaml

# Shared status file for dashboard to read current pressure data
PRESSURE_STATUS_FILE = Path('/tmp/sailframes-pressure-status.json')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [PRESSURE] %(levelname)s %(message)s'
)
logger = logging.getLogger('sailframes.pressure')

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
    data_dir = Path(base) / today / 'pressure'
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def create_csv_writer(data_dir):
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    filepath = data_dir / f'pressure_{timestamp}.csv'
    f = open(filepath, 'w', newline='')
    writer = csv.writer(f)
    writer.writerow([
        'utc_time',
        'pressure_hpa',       # Barometric pressure in hectopascals (mbar)
        'temperature_c',      # Temperature in Celsius
        'sea_level_hpa',      # Estimated sea-level pressure (assuming ~2m elevation on boat)
        'pressure_trend',     # Change in pressure over last 10 minutes (hPa)
    ])
    logger.info(f"Logging to {filepath}")
    return f, writer


def compute_sea_level_pressure(station_pressure_hpa, temp_c, elevation_m=2.0):
    """
    Convert station pressure to sea-level pressure using hypsometric formula.
    Elevation default is 2m (approximate deck height above sea level on a keelboat).
    """
    sea_level = station_pressure_hpa * (
        1 - (0.0065 * elevation_m) / (temp_c + 273.15 + 0.0065 * elevation_m)
    ) ** -5.257
    return round(sea_level, 3)


def pressure_to_inhg(hpa):
    """Convert hectopascals to inches of mercury."""
    return round(hpa * 0.02953, 2)


def describe_pressure_trend(trend_hpa):
    """Describe pressure trend in human-readable terms."""
    if trend_hpa is None or trend_hpa == '':
        return None
    trend = float(trend_hpa)
    if trend > 2.0:
        return 'Rapidly Rising'
    elif trend > 0.5:
        return 'Rising'
    elif trend > -0.5:
        return 'Steady'
    elif trend > -2.0:
        return 'Falling'
    else:
        return 'Rapidly Falling'


def update_pressure_status(pressure, temperature, sea_level, trend, connected=True):
    """Write current pressure data to status file for dashboard."""
    try:
        trend_desc = describe_pressure_trend(trend)

        status = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'connected': connected,
            # Pressure measurements
            'pressure_hpa': round(pressure, 2) if pressure is not None else None,
            'pressure_inhg': pressure_to_inhg(pressure) if pressure is not None else None,
            'sea_level_hpa': round(sea_level, 2) if sea_level is not None else None,
            'sea_level_inhg': pressure_to_inhg(sea_level) if sea_level is not None else None,
            # Temperature
            'temperature_c': round(temperature, 2) if temperature is not None else None,
            'temperature_f': round(temperature * 9/5 + 32, 1) if temperature is not None else None,
            # Trend (10-minute change)
            'trend_hpa': round(float(trend), 4) if trend is not None and trend != '' else None,
            'trend_desc': trend_desc,
        }
        with open(PRESSURE_STATUS_FILE, 'w') as f:
            json.dump(status, f)
    except Exception as e:
        logger.warning(f"Failed to update status file: {e}")


def clear_pressure_status():
    """Clear status file when disconnected."""
    try:
        status = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'connected': False,
        }
        with open(PRESSURE_STATUS_FILE, 'w') as f:
            json.dump(status, f)
    except Exception:
        pass


def run(config):
    press_config = config['pressure']
    sample_rate = press_config['sample_rate_hz']
    sample_interval = 1.0 / sample_rate

    logger.info(f"Initializing DPS310 on I2C bus {press_config['i2c_bus']} "
                f"at address 0x{press_config['i2c_address']:02X}")

    i2c = busio.I2C(board.SCL, board.SDA, frequency=400000)

    retries = 0
    dps = None
    while dps is None and running:
        try:
            dps = DPS310(i2c, address=press_config['i2c_address'])
            logger.info("DPS310 connected")
        except Exception as e:
            retries += 1
            if retries % 5 == 0:
                logger.warning(f"DPS310 not responding: {e}")
            time.sleep(2)

    if not running:
        return

    # Configure for highest precision
    dps.pressure_rate = Rate.RATE_1_HZ
    dps.pressure_oversample_count = SampleCount.COUNT_128
    dps.temperature_rate = Rate.RATE_1_HZ
    dps.temperature_oversample_count = SampleCount.COUNT_128
    dps.mode = Mode.CONT_PRESTEMP
    logger.info("DPS310 configured for high-precision continuous mode")

    data_dir = get_data_dir(config)
    csv_file, writer = create_csv_writer(data_dir)
    rows_written = 0

    # Pressure history for trend calculation (10-minute window at 1Hz = 600 samples)
    pressure_history = []
    trend_window_samples = 600  # 10 minutes at 1Hz

    try:
        while running:
            loop_start = time.monotonic()

            try:
                pressure = dps.pressure    # hPa
                temperature = dps.temperature  # Celsius
            except Exception as e:
                logger.warning(f"Read error: {e}")
                time.sleep(1)
                continue

            if pressure is None or temperature is None:
                time.sleep(0.1)
                continue

            sea_level = compute_sea_level_pressure(pressure, temperature)

            # Track pressure trend
            pressure_history.append(pressure)
            if len(pressure_history) > trend_window_samples:
                pressure_history.pop(0)

            # Compute trend: change over last 10 minutes
            trend = ''
            if len(pressure_history) >= trend_window_samples:
                trend = round(pressure_history[-1] - pressure_history[0], 4)

            utc_now = datetime.now(timezone.utc).isoformat()
            writer.writerow([
                utc_now,
                f"{pressure:.4f}",
                f"{temperature:.2f}",
                f"{sea_level:.3f}",
                trend if trend != '' else '',
            ])
            rows_written += 1

            # Update status file for dashboard every reading (1Hz is already low rate)
            update_pressure_status(pressure, temperature, sea_level, trend)

            if rows_written % 60 == 0:  # Flush every minute
                csv_file.flush()
                logger.debug(f"P={pressure:.2f}hPa T={temperature:.1f}°C "
                           f"SLP={sea_level:.2f}hPa trend={trend}")

            # Rotate at midnight
            current_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            if data_dir.parent.name != current_date:
                csv_file.close()
                data_dir = get_data_dir(config)
                csv_file, writer = create_csv_writer(data_dir)
                rows_written = 0

            elapsed = time.monotonic() - loop_start
            sleep_time = sample_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        csv_file.close()
        clear_pressure_status()
        logger.info(f"Pressure service stopped. {rows_written} rows written.")


if __name__ == '__main__':
    config = load_config()
    if not config['pressure']['enabled']:
        logger.info("Pressure disabled in config, exiting")
        sys.exit(0)
    run(config)
