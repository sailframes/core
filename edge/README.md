# SailFrames 🚤

Open-source sailboat racing data logger with AI vision analysis.

🌐 [sailframes.com](https://sailframes.com) · 📦 [github.com/sailframes](https://github.com/sailframes) · **Apache 2.0 License**

## What is SailFrames?

SailFrames is a self-contained, waterproof data acquisition device for competitive sailboat racing. It captures high-precision GPS tracks, wind speed and direction, boat motion (heel/pitch/heading), barometric pressure, and cockpit video — all synchronized with GPS timestamps. Post-race, AI vision processing analyzes crew positions and sail trim from the video.

## Hardware

- **Compute:** Raspberry Pi 4 (8GB recommended)
- **GPS:** u-blox ZED-F9P (dual-band GNSS, RTK-capable)
- **Wind:** Calypso Mini CMI1022 (BLE ultrasonic anemometer)
- **IMU:** BNO085 (9DOF with on-chip sensor fusion)
- **Pressure:** DPS310 (precision barometric sensor)
- **Camera:** Raspberry Pi Camera Module 3 Wide (120° FoV)
- **Storage:** USB 3.0 SSD
- **Power:** UPS HAT with 21700 Li-ion cells
- **Enclosure:** IP67 150×100×70mm with clear lid

## Software Architecture

```
sailframes/
├── services/
│   ├── sailframes_gps.py        # GPS data acquisition (ZED-F9P via USB/UART)
│   ├── sailframes_imu.py        # IMU data acquisition (BNO085 via I2C)
│   ├── sailframes_pressure.py   # Barometric pressure (DPS310 via I2C)
│   ├── sailframes_wind.py       # Wind sensor (Calypso Mini via BLE)
│   ├── sailframes_camera.py     # Video capture (Pi Camera 3 Wide via CSI)
│   ├── sailframes_sync.py       # Data upload to AWS S3
│   └── sailframes_monitor.py    # System health monitoring & local dashboard
├── config/
│   └── sailframes.yaml          # Central configuration
├── scripts/
│   ├── install.sh            # One-shot setup script
│   ├── start.sh              # Start all services
│   └── stop.sh               # Stop all services
├── tests/
│   ├── test_gps.py           # GPS connectivity test
│   ├── test_imu.py           # IMU connectivity test
│   ├── test_pressure.py      # Pressure sensor test
│   ├── test_wind.py          # BLE wind sensor test
│   └── test_camera.py        # Camera capture test
├── systemd/
│   ├── sailframes-gps.service
│   ├── sailframes-imu.service
│   ├── sailframes-pressure.service
│   ├── sailframes-wind.service
│   ├── sailframes-camera.service
│   └── sailframes-monitor.service
├── requirements.txt
└── README.md
```

## Quick Start

```bash
# Clone the repo
git clone https://github.com/sailframes/sailframes.git
cd sailframes

# Run the installer (sets up OS, dependencies, services)
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

## Data Format

All sensor data is timestamped with GPS time (UTC) and stored on the USB SSD:

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

## Target Boats

- Sonar 23
- J/80
- Similar one-design keelboats

## Operating Area

Boston Harbor, Massachusetts

## License

Apache 2.0
