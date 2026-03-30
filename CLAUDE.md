# SailFrames — Sailboat Racing Data Logger
## Project Context for Claude Code

---

## Project Overview

**SailFrames** is an open-source sailboat racing data logger and analytics platform.
- **License:** Apache 2.0
- **GitHub org:** github.com/sailframes
- **Main repo:** github.com/sailframes/core
- **Domain:** sailframes.com (registered via AWS)
- **Cloud:** AWS
- **Fleet:** 6 devices — Sonar 23 and J/80 class boats, Boston Harbor

The system captures GPS, IMU, wind, pressure, and camera data during races,
syncs to AWS after each session, and provides web-based race analysis and replay.

---

## Repository Structure (Monorepo)

```
sailframes/core/
├── edge-s/            # Raspberry Pi edge device (S1 = first gen)
│   ├── services/      # Sensor acquisition (GPS, IMU, wind, pressure, camera)
│   ├── scripts/       # Install, start, stop, Wi-Fi mode
│   ├── config/        # Device config (sailframes.yaml)
│   └── tests/         # Sensor connectivity tests
├── edge-e/            # ESP32 edge device (E1 = first gen)
│   ├── hardware/      # KiCad PCB designs
│   └── firmware/      # ESP32 Arduino firmware
├── web/               # Dashboard web application
│   ├── api/           # FastAPI backend
│   └── frontend/      # React frontend
├── lambda/            # AWS Lambda functions (post-race processing)
├── processing/        # Data processing scripts
├── infrastructure/    # AWS CDK/Terraform
├── scripts/           # Utility scripts
├── services/          # Systemd service definitions
├── config/            # Shared configuration
└── tests/             # Integration tests
```

---

## Hardware Stack (Per Device)

| Component | Part | Interface | Notes |
|---|---|---|---|
| SBC | Raspberry Pi 5 | — | Primary compute |
| GPS | u-blox ZED-F9P | USB (preferred) or UART | RTK-capable, PointPerfect PPP-RTK ~$5-8/mo, ~10cm accuracy |
| IMU | GY-BNO08X (BNO085) | I2C @ 0x4A | 9DOF AHRS, 400kHz I2C required |
| Wind | Calypso Ultrasonic Portable Mini | BLE 5.1 | Wireless only, open-source protocol, 1Hz, IPX8 |
| Pressure | Adafruit DPS310 | I2C @ 0x77 (STEMMA QT) | Needs Gore-Tex vent (Amphenol LTW VENT-PS1) in sealed enclosure |
| Camera | Pi Camera 3 Wide (IMX708) | CSI (22-pin MIPI) | Requires Pi 5 adapter cable |
| UPS/Battery | PiSugar 3 Plus | I2C @ 0x57/0x68 (pogo pins) | 21700 cells, Pi 5 confirmed compatible |
| Display | 1602 LCD + PCF8574T I2C backpack | I2C @ 0x27 | 5V VCC, I2C at 3.3V |
| Storage | SanDisk Extreme 128GB microSD (A2) | — | Preferred over PNY 256GB A1 for OS |

**Future / Considering:**
- OAK-D Pro Wide — 3D sail shape analysis (adds ~2-4W power draw)

---

## I2C Address Map (No Conflicts)

| Device | Address | Status |
|---|---|---|
| LCD Display | 0x27 | Active |
| BNO085 IMU | 0x4A | Active |
| DPS310 Pressure | 0x77 | Active |

Note: PiSugar battery hat (0x57, 0x68) was removed due to vibration issues.
Now using USB-C power bank for power.

**DS3231 RTC not needed:** The SparkFun ZED-F9P board has a rechargeable backup
battery that enables warm-start GPS fix in 1-5 seconds. Combined with GPS time
sync via chrony, the Pi clock syncs within seconds of boot. A separate RTC would
only help during the brief window before GPS lock.

Verify all devices after wiring:
```bash
sudo i2cdetect -y 1
# Expected: 0x27, 0x4a, 0x77
```

---

## OS & Software Environment

- **OS:** Raspberry Pi OS Bookworm (64-bit)
- **Python:** 3.11+, async preferred
- **Config file:** `/boot/firmware/config.txt` (Bookworm location)
- **Camera stack:** `rpicam-*` commands (libcamera is deprecated on Bookworm)

### Required I2C config (`/boot/firmware/config.txt`)
```ini
dtparam=i2c_arm=on
dtparam=i2c_arm_baudrate=400000   # Required for BNO085 clock stretching fix
```

### Key Python Libraries
```bash
pip install adafruit-circuitpython-bno08x --break-system-packages
pip install adafruit-circuitpython-dps310 --break-system-packages
pip install bleak              # BLE for Calypso wind sensor
pip install pyserial           # ZED-F9P UART fallback
```

### GPS Time Sync (chrony + gpsd)

The system uses GPS time to keep the clock accurate while offline (on the water).
Without this, the Pi's clock can drift significantly on power cycles.

**Configuration files:**
- `/etc/default/gpsd` — GPS device settings
- `/etc/chrony/conf.d/gps.conf` — GPS as time source

**Key settings in `/etc/chrony/conf.d/gps.conf`:**
```ini
# GPS via gpsd shared memory
refclock SHM 0 refid GPS precision 1e-1 offset 0.0 delay 0.2 poll 3 trust
makestep 1 -1   # Allow large time corrections (important after power loss)
local stratum 10
```

**Verify GPS time source:**
```bash
chronyc sources    # Shows GPS as #* or #+ when active
chronyc tracking   # Shows time sync status
gpspipe -w -n 3    # Verify GPS is receiving data
```

**Behavior:**
- When online: Uses NTP servers (more accurate)
- When offline: Falls back to GPS (~50ms accuracy via NMEA)
- On boot with wrong clock: Auto-corrects from GPS within seconds

### Persistent Journald (Debug Logs)

Logs are stored persistently to survive reboots, enabling post-sail debugging.

**Configuration:** `/etc/systemd/journald.conf.d/sailframes.conf`
```ini
[Journal]
Storage=persistent      # Survives reboots
Compress=yes            # Save disk space
SystemMaxUse=500M       # Max disk usage
SystemKeepFree=100M     # Keep disk space free
MaxRetentionSec=2week   # Keep 2 weeks of logs
MaxFileSec=1day         # Rotate daily
MaxLevelStore=debug     # Store all log levels
SyncIntervalSec=1m      # Sync every minute (balance safety vs SD wear)
```

**Post-sail debugging commands:**
```bash
# List all boots (previous sail sessions)
journalctl --list-boots

# View logs from previous boot
journalctl -b -1

# View errors/warnings from previous boot
journalctl -b -1 -p warning

# View sailframes services from a specific time
journalctl --since "2026-03-24 13:00" --until "2026-03-24 16:00" -u "sailframes*"

# View kernel messages (power issues, USB disconnects)
journalctl -b -1 -k | grep -iE "usb|power|voltage|under"

# Export logs for analysis
journalctl --since "2026-03-24" --output=json > /tmp/sail-logs.json
```

---

## Sensor Wiring Summary

### BNO085 (GY-BNO08X breakout)
```
VCC  → 3.3V
GND  → GND
SCL  → SCL1 (GPIO3, Pin 5)
SDA  → SDA1 (GPIO2, Pin 3)
ADO  → unconnected  (I2C addr 0x4A)
CS   → unconnected  (I2C mode)
PS0  → unconnected
PS1  → unconnected
```

### DPS310 (Adafruit breakout)
```
VIN  → 3.3V
GND  → GND
SCL  → SCL1 (shared bus)
SDA  → SDA1 (shared bus)
SDO  → unconnected (addr 0x77)
CS   → unconnected
```
Connect via STEMMA QT cable: Black=GND, Red=3.3V, Blue=SDA, Yellow=SCL

### 1602 LCD (with PCF8574T I2C backpack)
```
VCC  → 5V  ⚠️ (needs 5V, not 3.3V)
GND  → GND
SDA  → SDA1 (shared bus)
SCL  → SCL1 (shared bus)
```

### ZED-F9P GPS
Preferred: USB → any Pi 5 USB port → `/dev/ttyACM0` or `/dev/sailframes-gps` symlink
Fallback UART: TX→GPIO15(RX), RX→GPIO14(TX)

**USB auto-detection:** The GPS service automatically scans `/dev/ttyACM*` and `/dev/ttyUSB*`
if the preferred device is not found.

**Udev rule for persistent symlink** (`/etc/udev/rules.d/99-sailframes-gps.rules`):
```
SUBSYSTEM=="tty", ATTRS{idVendor}=="1546", ATTRS{idProduct}=="01a9", SYMLINK+="sailframes-gps"
```

### Calypso Wind Sensor
BLE only — no wires. Pi 5 built-in Bluetooth.
- BLE 5.1, open-source protocol
- Hardware open source: contact info@calypsoinstruments.com
- 1Hz sample rate, apparent wind direction + speed
- Must orient bow-mark toward bow when installing

---

## ZED-F9P Configuration

### Message Configuration (via u-center or script)

The GPS outputs NMEA sentences for position and UBX binary for raw measurements:

| Message | Purpose | Required For |
|---------|---------|--------------|
| NMEA GGA | Position, fix quality, satellites | Dashboard, CSV logging |
| NMEA RMC | Speed, course, date/time | Dashboard, CSV logging |
| NMEA GSV | Satellites in view per constellation | Dashboard constellation display |
| NMEA GSA | Satellites used for fix per constellation | Dashboard "in use" count |
| UBX-RXM-RAWX | Raw measurements (pseudorange, carrier phase) | RTKLib post-processing |
| UBX-RXM-SFRBX | Navigation message subframes | RTKLib (ephemeris data) |

**Configure via u-center:**
1. Connect to GPS via USB
2. Go to UBX → CFG → MSG
3. Enable messages for USB port (rate = 1)
4. Go to UBX → CFG → PRT → ensure USB has UBX protocol output enabled
5. Go to UBX → CFG → CFG → Save to Flash

**Or run configuration script:**
```bash
python3 edge-s/scripts/configure_gps.py /dev/sailframes-gps
```

### Raw UBX Logging for RTKLib

The GPS service automatically logs raw UBX data for post-processed kinematic (PPK) positioning:
- **Location:** `/mnt/sailframes-data/YYYY-MM-DD/ubx/raw_*.ubx`
- **Contains:** All serial data (NMEA + UBX binary messages)
- **Rotation:** Hourly file rotation
- **Usage with RTKLib:**
  ```bash
  # Convert to RINEX
  convbin raw_20260330_140000.ubx -r ubx -o sail.obs
  # Or load .ubx directly in RTKPOST
  ```

### GPS Warm/Hot Start

The ZED-F9P has a rechargeable backup battery that preserves satellite data:

| Start Type | Conditions | Time to Fix |
|------------|------------|-------------|
| Hot start | Power off < 4 hours, same location | 1-2 sec |
| Warm start | Power off < 2-4 days | 25-30 sec |
| Cold start | Power off > 2-4 days, or moved >100km | 30-60 sec |

### Dashboard GPS Display

The dashboard shows:
- **Satellites in use for fix:** From GSA sentences (actually contributing to position)
- **Satellites in view:** From GSV sentences (visible but may not be used)
- **Per-constellation breakdown:** GPS, GLONASS, Galileo, BeiDou with color coding

---

## Pi Camera 3 Wide

- Sensor: IMX708, 4608×2592, 10-bit RGGB
- Requires **22-pin Pi 5 adapter cable** (not the stock 15-pin cable)
- Detect: `rpicam-hello --list-cameras`
- Expected output: `imx708_wide [4608x2592]`
- Config override if not autodetected: `dtoverlay=imx708,cam0` in config.txt

### Autofocus Configuration

Pi Camera Module 3 defaults to manual focus (AfMode=0), causing out-of-focus recordings.
The camera service sets autofocus in the initial video configuration:

```python
video_config = picam2.create_video_configuration(
    controls={
        "AfMode": 2,      # 0=Manual, 1=Auto, 2=Continuous
        "AfSpeed": 1,     # 0=Normal, 1=Fast
        "AfRange": 0,     # 0=Normal, 1=Macro, 2=Full
    }
)
# After start, trigger continuous AF:
picam2.set_controls({"AfTrigger": 1})
```

**Verify autofocus is working:**
```bash
# Check camera metadata during recording
rpicam-still --list-cameras
# AfState=3 means "Focused", LensPosition should change as scene changes
```

### Camera Preview During Recording

The dashboard camera preview extracts frames from **completed** video segments
(not the currently recording file). This is because MP4 files can't be read
until recording completes (moov atom is written at end of file).

- Preview shows frame from most recent completed 5-minute segment
- During first segment: "preview available after first segment completes"
- When not recording: uses `rpicam-still` for live capture

---

## Power Budget (Approximate)

| Component | Draw |
|---|---|
| Pi 5 (typical load) | 3–5W |
| ZED-F9P | ~0.5W |
| Camera 3 Wide (active) | ~1–2W |
| BNO085 | negligible |
| DPS310 | negligible |
| Wi-Fi AP mode | +0.1–0.2W |
| OAK-D Pro Wide (if added) | +2–4W |
| **Total (no OAK-D)** | **~5–7W** |

PiSugar 3 Plus with 21700 cells (~18.5Wh/cell):
- 1 cell: ~2.5–3.5 hours
- 2 cells: ~5–7 hours (covers typical Boston Harbor race)

**Power saving tips:**
- Duty-cycle camera (burst recording on maneuver detection)
- Disable HDMI: add `hdmi_blanking=2` to config.txt
- Record raw video, do CV/ML analysis in AWS post-race
- Target 1080p/15fps minimum instead of 4K

---

## Networking & Dashboard

- Pi 5 runs as **Wi-Fi Access Point** (hostapd) during races
- Dashboard served on port 8080 (Flask + Jinja2 templates)
- Crew connects via browser — no app install required
- Each boat = isolated network, no inter-boat interference
- Post-race: sync to AWS S3

### Dashboard Pages

| Page | URL | Purpose |
|------|-----|---------|
| Main | `/` | Live sensor data, recording controls, system status |
| GPS Details | `/gps` | Detailed GPS info, constellation tracking |
| Battery History | `/battery` | Battery logging sessions |
| Video Review | `/video` | Browse and play recorded videos |
| Data Management | `/data` | View storage usage, delete old data by date |

### Recording Controls

- **Start Recording:** Starts all sensor services and cameras
- **Stop Recording:** Stops cameras only; sensors keep running for live dashboard data
- Confirmation dialog before stopping to prevent accidental data loss
- Sensors continue logging CSV data; only video recording stops

### Data Management Page (`/data`)

- Lists all dates with recorded data
- Shows storage breakdown: GPS, IMU, Pressure, Wind (CSV) + Video (MP4)
- Color-coded sensor tags with file counts and sizes
- Delete button per date with confirmation
- Cannot delete today's data (services may be writing)

### Data Directory Structure

```
/mnt/sailframes-data/
├── 2026-03-30/
│   ├── gps/
│   │   └── track_20260330_140000.csv    # Parsed position data
│   ├── ubx/
│   │   └── raw_20260330_140000.ubx      # Raw UBX for RTKLib
│   ├── imu/
│   │   └── imu_20260330_140000.csv
│   ├── pressure/
│   │   └── pressure_20260330_140000.csv
│   ├── wind/
│   │   └── wind_20260330_140000.csv
│   └── video/
│       ├── cockpit/
│       │   └── cockpit_20260330_140000.mp4
│       └── sails/
│           └── sails_20260330_140000.mp4
```

---

## Enclosure

- **IP67** sealed enclosure
- Daily install/remove on boat — no permanent cables
- DPS310 requires Gore-Tex pressure vent (Amphenol LTW VENT-PS1, ~$2-5)
  to allow pressure equalization while maintaining waterproofing
- Mast mounting: RAM Mounts tube clamp system (RAM-B-231ZU or similar)

---

## Known Issues & Gotchas

1. **BNO085 I2C clock stretching** — must set baudrate=400000 in config.txt.
   Default 100kHz causes intermittent read errors. 400kHz is the fix (not slower).

2. **Pi 4 vs Pi 5** — project uses Pi 5. Pi 4 images do NOT boot on Pi 5.
   Reflash with Pi Imager selecting Pi 5 as target device.

3. **Pi 4 Rev 1.1 USB-C bug** — older Pi 4 (Rev 1.1, 2018) requires
   non-e-marked USB-C cable for bench power. Not relevant for Pi 5.

4. **Camera cable** — Pi Camera 3 Wide ships with 15-pin cable.
   Pi 5 requires 22-pin adapter cable. Must swap before connecting.

5. **DPS310 in sealed enclosure** — without a pressure vent, the sensor
   reads internal enclosure pressure, not ambient. Gore-Tex vent is required.

6. **Calypso wind sensor BLE** — only one device can connect at a time.
   Pi 5 BLE client will claim the connection; disconnect other devices first.

7. **LCD needs 5V** — unlike other sensors (3.3V), the 1602 LCD VCC must
   connect to 5V rail. I2C data lines are safe at 3.3V via PCF8574T.

8. **Monitor service file descriptors** — excessive subprocess calls can exhaust
   file descriptors (~1024 per process). Service status checks are limited to
   every 60 seconds to prevent "Too many open files" crashes.

9. **Camera busy during recording** — Pi Camera can only be accessed by one
   process. Dashboard preview extracts frames from completed segments instead
   of direct capture when camera service is recording.

---

## Project History
- Originally named **TrimLog** (trimlog.com taken)
- Renamed to **SailFrames** — global find-and-replace done across all 18 files
- March 2026: Reorganized as monorepo `sailframes/core`
  - `edge-s/` — Raspberry Pi software (S1 = first generation device)
  - `edge-e/` — ESP32 hardware/firmware (E1 = first generation device)
  - Added `web/`, `lambda/`, `infrastructure/`, `processing/`
- March 30, 2026: Added raw UBX logging for RTKLib post-processing
- March 30, 2026: Added data management page for storage cleanup
- March 30, 2026: GPS constellation tracking with GSV/GSA parsing

---

## Competitive Landscape / Related Work
- ComVis-Sail (TU Delft, 2022) — CV-based sail analysis
- VSPARS — commercial sail shape system
- Njord Analytics, Kinetix AI, kTool, Fastrrr — commercial competitors
- Anemomind, Veetr — open-source sailing analytics
- Fastrrr DinghyEdition: ESP32 + u-blox NEO-M8N + ICM-20948 + MicroSD
  (simpler stack than SailFrames, no camera/cloud)

---

## Weather Data Integration
- GOES-16/19 imagery on AWS S3 (`s3://noaa-goes16/`, `s3://noaa-goes19/`)
- Use `goes2go` Python library for easy access
- Boston NWS office (BOX) provides regional GOES crops
- Useful layers: visible (sea breeze), water vapor, GeoColor composite
- Can overlay GPS tracks on GOES imagery for post-race analysis

---

*Last updated: March 30, 2026 — Added UBX logging, data management, GPS constellation tracking*
