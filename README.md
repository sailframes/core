# SailFrames

Open-source sailboat racing data logger and analytics platform.

[sailframes.com](https://sailframes.com) | [github.com/sailframes](https://github.com/sailframes) | Apache 2.0 License

## What is SailFrames?

SailFrames is a self-contained, waterproof data acquisition device for competitive sailboat racing. It captures high-precision GPS tracks, wind speed and direction, boat motion (heel/pitch/heading), barometric pressure, and cockpit video — all synchronized with GPS timestamps. Data syncs to AWS after each session for web-based race analysis and replay.

## Repository Structure

```
sailframes/core/
├── edge-s/            # Raspberry Pi edge device
│   ├── services/      # Sensor acquisition (GPS, IMU, wind, pressure, camera)
│   ├── scripts/       # Install, start, stop, Wi-Fi mode
│   ├── config/        # Device configuration
│   └── tests/         # Sensor connectivity tests
├── edge-e/            # ESP32 edge device
│   ├── hardware/      # KiCad PCB designs
│   └── firmware/      # ESP32 Arduino firmware
├── web/               # Dashboard web application
│   ├── api/           # Backend API
│   └── frontend/      # React frontend
├── lambda/            # AWS Lambda functions
├── processing/        # Post-race data processing
├── infrastructure/    # AWS CDK/Terraform
├── scripts/           # Utility scripts
├── services/          # Systemd service definitions
├── config/            # Shared configuration
└── tests/             # Integration tests
```

## Hardware

| Component | Part | Interface |
|-----------|------|-----------|
| Compute | Raspberry Pi 5 | — |
| GPS | u-blox ZED-F9P | USB |
| Wind | Calypso Ultrasonic Mini | BLE 5.1 |
| IMU | BNO085 (GY-BNO08X) | I2C @ 0x4A |
| Pressure | DPS310 | I2C @ 0x77 |
| Camera | Pi Camera 3 Wide | CSI |
| Display | 1602 LCD + PCF8574T | I2C @ 0x27 |
| Enclosure | IP67 sealed | Gore-Tex vent |

## Quick Start (Edge Device)

```bash
# Clone the repo
git clone https://github.com/sailframes/core.git
cd core

# Run the installer on Raspberry Pi
sudo bash edge-s/scripts/install.sh

# Test all sensors
python3 edge-s/tests/test_gps.py
python3 edge-s/tests/test_imu.py
python3 edge-s/tests/test_pressure.py
python3 edge-s/tests/test_wind.py
python3 edge-s/tests/test_camera.py

# Start all services
sudo bash edge-s/scripts/start.sh

# Check status
sudo systemctl status sailframes-*
```

## Data Format

All sensor data is timestamped with GPS time (UTC):

```
/mnt/sailframes-data/
├── 2026-03-15/
│   ├── gps/
│   │   └── track_20260315_140000.csv
│   ├── imu/
│   │   └── imu_20260315_140000.csv
│   ├── pressure/
│   │   └── pressure_20260315_140000.csv
│   ├── wind/
│   │   └── wind_20260315_140000.csv
│   └── video/
│       └── cockpit_20260315_140000.mp4
```

## Fleet

- 6 devices deployed
- Sonar 23 and J/80 class boats
- Boston Harbor, Massachusetts

## License

Apache 2.0
