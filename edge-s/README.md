# SailFrames Edge-S

Raspberry Pi-based edge device for sailboat racing data acquisition.

Part of [sailframes/core](https://github.com/sailframes/core). See [edge-e](../edge-e) for ESP32-based device.

## Variants

- **S1** - First generation Raspberry Pi 5 device (current)

## Hardware (S1)

| Component | Part | Interface |
|-----------|------|-----------|
| Compute | Raspberry Pi 5 | — |
| GPS | u-blox ZED-F9P | USB |
| Wind | Calypso Ultrasonic Mini | BLE 5.1 |
| IMU | BNO085 (GY-BNO08X) | I2C @ 0x4A |
| Pressure | DPS310 | I2C @ 0x77 |
| Camera | Pi Camera 3 Wide | CSI |
| Display | 1602 LCD + PCF8574T | I2C @ 0x27 |

## Directory Structure

```
edge-s/
├── services/
│   ├── sailframes_gps.py        # GPS acquisition (ZED-F9P via USB)
│   ├── sailframes_imu.py        # IMU acquisition (BNO085 via I2C)
│   ├── sailframes_pressure.py   # Barometric pressure (DPS310 via I2C)
│   ├── sailframes_wind.py       # Wind sensor (Calypso via BLE)
│   ├── sailframes_camera.py     # Video capture (Pi Camera 3 Wide)
│   └── sailframes_monitor.py    # System health & dashboard
├── scripts/
│   ├── install.sh               # Setup script
│   ├── start.sh                 # Start all services
│   ├── stop.sh                  # Stop all services
│   └── wifi-mode.sh             # Toggle AP/client mode
├── config/
│   └── sailframes.yaml          # Device configuration
└── tests/
    ├── test_gps.py
    ├── test_imu.py
    ├── test_pressure.py
    ├── test_wind.py
    └── test_camera.py
```

## Quick Start

```bash
# Clone the monorepo
git clone https://github.com/sailframes/core.git
cd core/edge-s

# Run the installer
sudo bash scripts/install.sh

# Test all sensors
python3 tests/test_gps.py
python3 tests/test_imu.py
python3 tests/test_pressure.py
python3 tests/test_wind.py
python3 tests/test_camera.py

# Start all services
sudo bash scripts/start.sh

# Check status
sudo systemctl status sailframes-*
```

## Data Output

All sensor data is timestamped with GPS time (UTC):

```
/mnt/sailframes-data/
├── 2026-03-15/
│   ├── gps/track_20260315_140000.csv
│   ├── imu/imu_20260315_140000.csv
│   ├── pressure/pressure_20260315_140000.csv
│   ├── wind/wind_20260315_140000.csv
│   └── video/cockpit_20260315_140000.mp4
```

## Wi-Fi Modes

The device operates as a Wi-Fi access point during races, serving a live dashboard to crew devices. Post-race, switch to client mode for data sync.

```bash
# Switch to AP mode (default for racing)
sudo bash scripts/wifi-mode.sh ap

# Switch to client mode (for sync)
sudo bash scripts/wifi-mode.sh client
```

## License

Apache 2.0
