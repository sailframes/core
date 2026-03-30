#!/usr/bin/env python3
"""
SailFrames Power Manager
Monitors USB-C power and enables/disables desktop to save battery.
- USB-C connected: Start desktop session + browsers
- On battery: Stop entire desktop session (saves ~200-400mA)
"""

import os
import sys
import time
import glob
import signal
import subprocess
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [POWER] %(levelname)s %(message)s'
)
logger = logging.getLogger('sailframes.power')

# Configuration
CHECK_INTERVAL = 10  # seconds between checks
STARTUP_DELAY = 60   # wait before first power check (let desktop initialize)
CURRENT_THRESHOLD = 0  # negative current = USB-C connected
INA219_ADDR = 0x43
REG_SHUNT_VOLTAGE = 0x01
SHUNT_OHMS = 0.1

running = True
display_enabled = None  # Track current state


def signal_handler(sig, frame):
    global running
    logger.info("Shutdown signal received")
    running = False


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


def get_current_ma():
    """Read current from INA219. Negative = USB-C charging."""
    try:
        import smbus2
        bus = smbus2.SMBus(1)
        raw_shunt = bus.read_word_data(INA219_ADDR, REG_SHUNT_VOLTAGE)
        raw_shunt = ((raw_shunt & 0xFF) << 8) | ((raw_shunt >> 8) & 0xFF)
        if raw_shunt > 32767:
            raw_shunt -= 65536
        shunt_mv = raw_shunt * 0.01
        current_ma = shunt_mv / SHUNT_OHMS
        bus.close()
        return current_ma
    except Exception as e:
        logger.error(f"Failed to read current: {e}")
        return None


def is_usb_connected():
    """Check if USB-C is providing power (negative current = charging)."""
    current = get_current_ma()
    if current is None:
        return None
    return current < CURRENT_THRESHOLD


def enable_display():
    """Start desktop session and browsers."""
    global display_enabled
    if display_enabled:
        return  # Already enabled

    logger.info("USB-C connected - starting desktop session")

    # Start the display manager (brings up full desktop)
    try:
        result = subprocess.run(
            ['sudo', 'systemctl', 'start', 'lightdm'],
            capture_output=True, timeout=30
        )
        if result.returncode == 0:
            logger.info("Started display manager")
        else:
            logger.warning(f"Display manager start returned: {result.returncode}")
    except Exception as e:
        logger.warning(f"Could not start display manager: {e}")

    # Wait for desktop to initialize, then start browsers
    time.sleep(10)

    # Start dashboard browsers
    env = os.environ.copy()
    env['DISPLAY'] = ':0'
    env['WAYLAND_DISPLAY'] = 'wayland-1'

    dashboard_script = '/home/paul/sailframes-dashboard.sh'
    if os.path.exists(dashboard_script):
        try:
            subprocess.Popen(
                ['sudo', '-u', 'paul', dashboard_script],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            logger.info("Started dashboard browsers")
        except Exception as e:
            logger.warning(f"Could not start dashboard: {e}")

    display_enabled = True


def disable_display():
    """Stop entire desktop session to save power."""
    global display_enabled
    if display_enabled is False:
        return  # Already disabled

    logger.info("On battery - stopping desktop session to save power")

    # Stop the display manager (kills labwc, pipewire, browsers, panel, etc.)
    try:
        result = subprocess.run(
            ['sudo', 'systemctl', 'stop', 'lightdm'],
            capture_output=True, timeout=30
        )
        if result.returncode == 0:
            logger.info("Stopped display manager - desktop session ended")
        else:
            logger.warning(f"Display manager stop returned: {result.returncode}")
    except Exception as e:
        logger.warning(f"Could not stop display manager: {e}")

    # Also kill any remaining GUI processes
    for proc in ['chromium', 'labwc', 'pipewire', 'wireplumber', 'wf-panel-pi']:
        try:
            subprocess.run(['pkill', '-9', proc], capture_output=True, timeout=5)
        except Exception:
            pass

    display_enabled = False


def run():
    global display_enabled
    logger.info("Power manager started")

    # Wait for system to fully boot before managing power
    logger.info(f"Waiting {STARTUP_DELAY}s for system initialization...")
    time.sleep(STARTUP_DELAY)

    # Check initial state
    usb_connected = is_usb_connected()
    logger.info(f"Initial state: USB connected = {usb_connected}")

    if usb_connected is True:
        display_enabled = True  # Assume desktop is already running
        logger.info("USB-C connected at startup - desktop stays on")
    elif usb_connected is False:
        display_enabled = True  # Mark as enabled so disable_display() will run
        disable_display()

    while running:
        time.sleep(CHECK_INTERVAL)

        usb_connected = is_usb_connected()
        if usb_connected is None:
            continue  # Read error, skip this cycle

        if usb_connected and not display_enabled:
            enable_display()
        elif not usb_connected and display_enabled:
            disable_display()


if __name__ == '__main__':
    run()
