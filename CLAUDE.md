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
- **S3 Bucket:** `sailframes-fleet-data-prod` (unified bucket for both S1 and E1 devices)
- **Fleet:** 6 boats — Sonar 23 and J/80 class, Boston Harbor

The system has two hardware tiers:
- **S1** (primary analysis boat): Raspberry Pi 5, high-precision sensors, camera, sunlight-readable display
- **E1** (fleet tracker, ×20 regatta boats): ESP32, PPK-capable GNSS, IMU, OLED, SD logging

The system captures GPS, IMU, wind, pressure, and camera data during races,
syncs to AWS after each session, and provides web-based race analysis and replay.

**Strategic differentiator:** SailFrames is the first sailing analytics platform
using PPK (Post-Processed Kinematic) GNSS, leveraging free NOAA CORS base station
data via RTKLIB for sub-meter accuracy without any base station hardware.

---

## Repository Structure (Monorepo)

```
sailframes/core/
├── CLAUDE.md              # This file — project context for Claude Code
├── edge-s/                # Raspberry Pi edge device (S1 = first gen)
│   ├── services/          # Sensor acquisition (GPS, IMU, wind, pressure, camera)
│   ├── scripts/           # Install, start, stop, Wi-Fi mode, GPS config
│   ├── config/            # Device config (sailframes.yaml)
│   └── tests/             # Sensor connectivity tests
├── edge-e/                # ESP32 edge device (E1 = first gen)
│   ├── hardware/          # KiCad PCB designs (schematic complete, layout pending)
│   └── firmware/          # ESP32 Arduino firmware (sailframes_e1.ino)
├── web/                   # Dashboard web application
│   ├── api/               # FastAPI backend
│   └── frontend/          # React frontend
├── processing/            # Data processing / analytics engine
│   ├── maneuvers.py       # Tack/gybe detection + per-maneuver metrics
│   ├── straight_lines.py  # Upwind/downwind leg segmentation + stats
│   ├── wind.py            # Wind direction calculation + fallback estimation
│   ├── polar.py           # Polar diagram generation
│   ├── vmg.py             # VMG computation
│   ├── stats.py           # Violin plot data, STD, correlations
│   └── models.py          # Data schemas (session, boat, maneuver, leg)
├── lambda/                # AWS Lambda functions (post-race processing)
├── export/                # Report & social media export
│   ├── pdf_report.py
│   ├── social_video.py    # FFmpeg vertical video w/ data overlay
│   └── graph_export.py    # PNG/clipboard export
├── infrastructure/        # AWS CDK/Terraform
├── scripts/               # Utility scripts
├── services/              # Systemd service definitions
├── config/                # Shared configuration
└── tests/                 # Integration tests
```

---

## S1 Hardware Stack (Raspberry Pi Analysis Boat)

| Component | Part | Interface | Notes |
|---|---|---|---|
| SBC | Raspberry Pi 5 | — | Primary compute |
| GPS | u-blox ZED-F9P | USB (CH340 serial) | Dual-band L1+L5, PPK-capable, 10Hz nav rate |
| IMU | GY-BNO08X (BNO085) | I2C @ 0x4A | 9DOF AHRS, 400kHz I2C required |
| Wind | Calypso Ultrasonic Portable Mini | BLE 5.1 | Wireless only, open-source protocol, 1Hz, IPX8 |
| Pressure | Adafruit DPS310 | I2C @ 0x77 (STEMMA QT) | Needs Gore-Tex vent in sealed enclosure |
| Camera | Pi Camera 3 Wide (IMX708) | CSI (22-pin MIPI) | Requires Pi 5 adapter cable |
| Display | Newhaven NHD-5.0-HDMI-N-RSXP | HDMI | 1100-nit sunlight-readable, 800×480, 5V/560mA |
| Power | 50,000mAh USB-C power bank | USB-C | Replaced PiSugar 3 Plus (pogo-pin vibration issues) |
| Storage (boot) | SanDisk Extreme 128GB microSD (A2) | — | Near-read-only boot volume |
| Storage (data) | Foresee XP1000F 128GB NVMe | M.2 2280 M-key | In Lemorele slim NVMe enclosure, mounted at `/data` |
| Enclosure | QILIPSU IP67 | — | Daily install/remove, no permanent boat modifications |

**Future / Considering:**
- OAK-D Pro Wide — 3D sail shape analysis (adds ~2-4W power draw)

### S1 I2C Address Map (No Conflicts)

| Device | Address | Status |
|---|---|---|
| BNO085 IMU | 0x4A | Active |
| DPS310 Pressure | 0x77 | Active |

Note: PiSugar battery hat (0x57, 0x68) removed due to vibration issues.
1602 LCD (0x27) replaced by Newhaven HDMI display.

Verify all devices after wiring:
```bash
sudo i2cdetect -y 1
# Expected: 0x4a, 0x77
```

**DS3231 RTC not needed:** The SparkFun ZED-F9P board has a rechargeable backup
battery that enables warm-start GPS fix in 1-5 seconds. Combined with GPS time
sync via chrony, the Pi clock syncs within seconds of boot.

---

## E1 Hardware Stack (ESP32 Fleet Tracker)

| Component | Part | Interface | Notes |
|---|---|---|---|
| MCU | ELEGOO ESP32 DevKit V1 (CP2102) | USB-C | Dual-core 240MHz, Wi-Fi + BLE |
| GPS | Waveshare LG290P GNSS module | UART2 (GPIO16 TX2, GPIO17 RX2) | Quad-band, PPK-capable, ~$109 with antenna |
| IMU | GY-BNO08X (BNO085) | I2C (GPIO21 SDA, GPIO22 SCL) @ 0x4A | Heel/pitch, use GAME_ROTATION_VECTOR mode |
| Display | HiLetgo 2.42" SSD1309 OLED | I2C (shared bus) yellow | Status display |
| Storage | microSD breakout + 16GB card | SPI (GPIO23 MOSI, 19 MISO, 18 CLK, 5 CS) | CSV + raw binary for PPK |
| Power | Adafruit PowerBoost 1000C + 18650 cells | 5V output to ESP32 VIN | Hardware power switch on EN pin |
| Enclosure | YETLEBOX IP67 ABS 5.9"×3.9"×2.8" clear lid | — | Daily install/remove |

### E1 Power Management

**Hardware power switch:** A slide switch on the PowerBoost 1000C EN (enable) pin
controls power to the entire system. This replaces software deep sleep which had
issues with immediate wake-up and GPS staying powered.

**Battery monitoring:** GPIO34 reads battery voltage via voltage divider (2x 200Ω,
ratio 2.0). GPIO35 reads PowerBoost LBO (Low Battery Output) pin for critical
battery warning.

| Pin | Function | Notes |
|-----|----------|-------|
| GPIO34 | Battery voltage (ADC) | Via 2:1 voltage divider |
| GPIO35 | Low battery warning | PowerBoost LBO pin, active low |

**Battery percentage displayed** on OLED instead of heading (MAG replaced with BAT).

### E1 Wiring Summary

```
LG290P GPS:
  TXD3 → GPIO16 (ESP32 RX2)     ⚠️ UART2, NOT GPIO21/22 (I2C)
  RXD3 → GPIO17 (ESP32 TX2)
  5V   → 5V from battery shield
  GND  → GND

BNO085 IMU (shared I2C bus):
  SDA  → GPIO21
  SCL  → GPIO22
  VCC  → 3V3 from ESP32
  GND  → GND

SSD1309 OLED (shared I2C bus):
  SDA  → GPIO21
  SCL  → GPIO22
  VCC  → 3V3 from ESP32
  GND  → GND

SD Card (SPI):
  3V3  → 3V3
  CS   → GPIO5
  MOSI → GPIO23
  CLK  → GPIO18
  MISO → GPIO19
  GND  → GND
  (Pin order on board silkscreen: 3V3, CS, MOSI, CLK, MISO, GND)

Power + Battery Monitoring:
  PowerBoost 1000C 5V → ESP32 VIN
  PowerBoost BAT → voltage divider (2x 200Ω) → GPIO34 (ADC)
  PowerBoost LBO → GPIO35 (low battery warning)
  Slide switch on PowerBoost EN pin (hardware power control)
  ESP32 3V3 → all sensors
```

### E1 KiCad Schematic Status

- **Complete:** ESP32 DevKitV1 symbol (custom library from GitHub), all connectors
  with net labels, ERC clean
- **Key fix applied:** GPS net labels (GPS_TX/GPS_RX) on UART2 (GPIO16/17),
  NOT on GPIO21/22 (I2C bus). Edit net labels on schematic sheet, not symbol definition.
- **J3 BNO085:** Updated to 10-pin connector with no-connect flags on unused pins
  (ADO, CS, INT, RST, PS1, PS0)
- **J1 SD Card:** Pin order corrected to match physical silkscreen
- **Footprint libraries:** Assigned; module courtyard outlines added for layout
- **Freerouting:** Installed via KiCad Plugin Manager (requires Java 21 Eclipse Temurin)
- **Next:** PCB layout → Gerbers → JLCPCB (~$2-5 per 5 boards)

### E1 Firmware (sailframes_e1.ino)

- NMEA parsing (GGA/RMC/GSA/GSV sentences from LG290P)
- RTCM3 binary parsing and logging for PPK post-processing
- BNO085 reading at 20Hz with heel/pitch calculation
- SD logging: CSV (human-readable) + raw RTCM3 binary (`.rtcm3` files)
- OLED status display: satellites (in fix/in view), heel, battery %, recording status
- Battery monitoring via ADC (voltage divider on GPIO34)
- Wi-Fi auto-upload to AWS S3 on yacht club network detection
- GPS speed-triggered auto-recording (starts >2kt, stops after 5min <0.5kt)
- Power button recording control (short press toggles recording)
- Configuration loaded from SD card `config.txt`
- **Libraries:** U8g2 (OLED), NimBLE-Arduino (wind sensor BLE), ESP32 by Espressif Systems
- **Arduino IDE:** ESP32 board support installed (v3.3.7)

**OLED Display Layout:**
- Line 1: Satellites (in fix / in view), HDOP
- Line 2: Speed (kt), Course (°)
- Line 3: Heel (°), Pitch (°), Battery (%)
- Line 4: Recording status, session info

**Serial/Telnet Commands:**
| Command | Description |
|---------|-------------|
| `start` | Start recording session |
| `stop` | Stop recording session |
| `upload` | Manually trigger Wi-Fi upload |
| `clearmarkers` | Delete `.uploaded` marker files to retry failed uploads |
| `status` | Show current sensor status (GPS, IMU, SD, WiFi, RTCM frame count) |
| `gps` | Show GPS debug info |
| `gpsraw` | Show raw GPS data stream (10 seconds) |
| `gpscfg` | Reconfigure LG290P (resend PQTM commands) |
| `config` | Show current configuration |

**E1 SD Card Directory Structure:**
```
/sf/
├── 20260405_225030/                    # Session folder (GPS datetime)
│   ├── E1_20260405_225030_nav.csv      # Parsed NMEA (lat,lon,sog,cog,sat,hdop,fix)
│   ├── E1_20260405_225030_imu.csv      # IMU (heel,pitch,accel,gyro)
│   ├── E1_20260405_225030_raw.rtcm3    # Raw RTCM3 binary for PPK
│   └── E1_20260405_225030_wind.csv     # Wind sensor data (if enabled)
├── session_001/                         # Fallback if no GPS datetime
│   └── ...
└── config.txt                           # Device configuration
```

Session folders use GPS datetime when available (`YYYYMMDD_HHMMSS`), falling back
to sequential `session_NNN` if GPS date/time is not valid at recording start.

### E1 Wi-Fi Upload Architecture

The E1 uses a two-tier upload strategy to handle API Gateway's 29-second timeout:

| File Size | Method | Endpoint |
|-----------|--------|----------|
| < 1 MB | Direct POST | API Gateway → Lambda → S3 |
| ≥ 1 MB | Presigned URL | API Gateway → Lambda (get URL) → Direct S3 PUT |

**Upload flow for large files:**
1. Request presigned URL via POST to `/upload?boat=E1&file=PATH&presign=1&size=BYTES`
2. Lambda generates presigned S3 PUT URL (1-hour expiry)
3. ESP32 uploads directly to S3 using plain HTTP (no TLS overhead)
4. S3 accepts the upload with metadata (boat_id, original_path)

**Why HTTP instead of HTTPS for S3:**
- ESP32 TLS is slow and memory-constrained
- Fleet data (GPS, IMU) is not sensitive
- S3 supports both HTTP and HTTPS
- Reduces upload time significantly for large RTCM3 files

**Upload marker files:**
- After successful upload, creates `filename.uploaded` marker
- Prevents re-uploading same file on subsequent upload runs
- Use `clearmarkers` command to delete markers and retry failed uploads

### E1 BLE/Wi-Fi Radio Conflict

**Issue:** ESP32's shared radio and **TLS memory requirements** (~40-50KB heap for
SSL buffers) cause conflicts when running BLE + Wi-Fi + HTTPS simultaneously.

**Symptoms:**
- HTTP error -1 (CONNECTION_REFUSED/TIMEOUT) during uploads
- TLS handshake failures even with good Wi-Fi signal
- Heap exhaustion crashes

**Current behavior:** BLE is fully deinitialized before Wi-Fi uploads, then
reinitialized after. This is conservative but reliable.

**Note:** Plain HTTP (no TLS) has much lower memory pressure and may work with
BLE still active. Future optimization: skip BLE deinit when upload_url uses HTTP.

**Solution — BLE deinit sequence before Wi-Fi operations:**
```cpp
// 1. Disconnect BLE client first
if (pWindClient && pWindClient->isConnected()) {
    pWindClient->disconnect();
    delay(100);
}

// 2. Disconnect Wi-Fi if connected (before BLE deinit!)
if (WiFi.status() == WL_CONNECTED) {
    WiFi.disconnect(true);
    delay(200);
}

// 3. Fully deinitialize BLE stack
NimBLEDevice::deinit(false);  // MUST use false, not true!
bleInitialized = false;
delay(500);

// 4. Now safe to use Wi-Fi
WiFi.begin(ssid, password);
```

**Important:** `NimBLEDevice::deinit(true)` causes heap corruption crashes.
Always use `deinit(false)` which preserves some internal state safely.

---

## GNSS Strategy

### Tiered Approach

| Tier | Receiver | Use | Cost | Accuracy |
|---|---|---|---|---|
| S1 | u-blox ZED-F9P | Primary analysis boat | ~$250 | Sub-meter (PPK) |
| E1 | Quectel LG290P (Waveshare) | Fleet trackers (×20) | ~$109 | Sub-meter (PPK) |

Both receivers log raw observations for PPK post-processing. The LG290P is the
cheapest PPK-capable module available (~$109 quad-band with antenna via Waveshare).

### PPK Post-Processing Workflow

1. Log raw GNSS observations during race (UBX-RXM-RAWX/SFRBX on ZED-F9P; raw binary on LG290P)
2. Download CORS base station data from NOAA: `geodesy.noaa.gov/UFCORS` (free, available ~1hr after recording)
3. Process in RTKLIB (RTKPOST): rover obs + base obs + nav data → fixed solution
4. No base station hardware needed — uses existing NOAA infrastructure

### ZED-F9P Configuration

Connected via CH340 USB-serial (COM4 in u-center on Windows).
Dual-frequency L1+L5. 10Hz nav rate confirmed.

**Message Configuration:**

| Message | Purpose | Required For |
|---------|---------|--------------|
| NMEA GGA | Position, fix quality, satellites | Dashboard, CSV logging |
| NMEA RMC | Speed, course, date/time | Dashboard, CSV logging |
| NMEA GSV | Satellites in view per constellation | Dashboard constellation display |
| NMEA GSA | Satellites used for fix per constellation | Dashboard "in use" count |
| UBX-RXM-RAWX | Raw measurements (pseudorange, carrier phase) | RTKLIB post-processing |
| UBX-RXM-SFRBX | Navigation message subframes | RTKLIB (ephemeris data) |

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

### Raw UBX Logging for RTKLIB

The GPS service automatically logs raw UBX data for PPK:
- **Location:** `/mnt/sailframes-data/YYYY-MM-DD/ubx/raw_*.ubx`
- **Contains:** All serial data (NMEA + UBX binary messages)
- **Rotation:** Hourly file rotation
- **Usage with RTKLIB:**
  ```bash
  # Convert to RINEX
  convbin raw_20260330_140000.ubx -r ubx -o sail.obs
  # Or load .ubx directly in RTKPOST
  ```

### AssistNow Predictive Orbits (ZED-F9P)

Free via u-blox Thingstream registration. Provides up to 14-day orbit predictions
for ~5-10 second TTFF. Valuable given daily install/remove pattern where every boot
is a cold or warm start. Legacy developer tokens phasing out by May 2028.

**Note:** Quectel LG290P has no equivalent free A-GNSS service. SBAS helps acquisition.

### LG290P Configuration (E1)

The Waveshare LG290P module uses Quectel's PQTM command protocol. Configuration
is best done via **QGNSS on Windows** — PyGPSClient on macOS has limited support.

**Hardware connections:**
- USB-C port: For PC configuration (CH343 USB-serial chip)
- UART SH1.0 connector: For ESP32 connection (TXD3/RXD3 at 460800 baud)
- RST button: Single press reboots; no factory reset function

**RTCM3 PPK configuration (saved to NVM via QGNSS):**
```
$PQTMCFGRTCM,W,7,0,-90,07,06,1,0*     # Enable MSM7 for all constellations
$PQTMCFGMSGRATE,W,RTCM3-1019,1*       # GPS ephemeris
$PQTMCFGMSGRATE,W,RTCM3-1020,1*       # GLONASS ephemeris
$PQTMCFGMSGRATE,W,RTCM3-1042,1*       # BeiDou ephemeris
$PQTMCFGMSGRATE,W,RTCM3-1046,1*       # Galileo ephemeris
$PQTMCFGMSGRATE,W,RTCM3-1006,10*      # Station reference every 10 epochs
$PQTMCFGCNST,W,1,1,1,1,1,1*           # Enable all constellations (GPS,GLO,GAL,BDS,QZSS,NavIC)
$PQTMSAVEPAR*                          # Save to NVM
$PQTMHOT*                              # Hot restart
```

**Firmware syntax note (AANR01A06S):** The `PQTMCFGMSGRATE` command uses TWO
parameters (message, rate) — NOT three. Adding an offset parameter causes `ERROR,1`.

**E1 firmware configures both NMEA and RTCM3 at boot:**
```cpp
void configureLG290P() {
    // NMEA messages
    sendPQTM("PQTMCFGMSGRATE,W,GGA,1");
    sendPQTM("PQTMCFGMSGRATE,W,RMC,1");
    sendPQTM("PQTMCFGMSGRATE,W,GSA,1");
    sendPQTM("PQTMCFGMSGRATE,W,GSV,1");

    // RTCM3 MSM7 for PPK (configured at every boot, saved to NVM)
    sendPQTM("PQTMCFGRTCM,W,7,0,-90,07,06,1,0");  // Enable MSM7
    sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1019,1");    // GPS ephemeris
    sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1020,1");    // GLONASS ephemeris
    sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1042,1");    // BeiDou ephemeris
    sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1046,1");    // Galileo ephemeris
    sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1006,10");   // Station reference

    sendPQTM("PQTMSAVEPAR");  // Save to NVM
    sendPQTM("PQTMHOT");      // Hot restart
}
```

The firmware sends RTCM configuration at every boot to ensure PPK data is captured
regardless of prior QGNSS configuration state. Commands and responses are logged
to Serial for debugging.

**RTCM3 messages output after configuration:**
| RTCM3 ID | Content | Rate |
|----------|---------|------|
| 1077 | GPS MSM7 (pseudorange + phase + doppler + CNR) | Every epoch |
| 1087 | GLONASS MSM7 | Every epoch |
| 1097 | Galileo MSM7 | Every epoch |
| 1127 | BeiDou MSM7 | Every epoch |
| 1019 | GPS ephemeris | Periodic (~30s) |
| 1020 | GLONASS ephemeris | Periodic |
| 1042 | BeiDou ephemeris | Periodic |
| 1046 | Galileo ephemeris | Periodic |
| 1006 | Station reference position | Every 10 epochs |

### GPS Warm/Hot Start

The ZED-F9P has a rechargeable backup battery that preserves satellite data:

| Start Type | Conditions | Time to Fix |
|------------|------------|-------------|
| Hot start | Power off < 4 hours, same location | 1-2 sec |
| Warm start | Power off < 2-4 days | 25-30 sec |
| Cold start | Power off > 2-4 days, or moved >100km | 30-60 sec |

### Antenna Evaluation

Comparing u-blox ANN-MB-00 vs. Beitian BT-560:
- **Method:** Per-satellite C/N0 comparison outdoors with matched satellite IDs
- **Tool:** RXM-RAWX view in u-center (not MON-RF, which reflects aggregate receiver state)
- **Note:** MON-RF AGC and noise floor are dominated by RF environment, not antenna characteristics

### Dashboard GPS Display

- **Satellites in use for fix:** From GSA sentences (actually contributing to position)
- **Satellites in view:** From GSV sentences (visible but may not be used)
- **Per-constellation breakdown:** GPS, GLONASS, Galileo, BeiDou with color coding

---

## BNO085 IMU Configuration

### Recommended Mode: GAME_ROTATION_VECTOR

Use 6DOF mode (accel + gyro, no magnetometer) for heel and pitch on a boat:

```python
import adafruit_bno08x
bno.enable_feature(adafruit_bno08x.BNO_REPORT_GAME_ROTATION_VECTOR)
# Read with bno.game_quaternion instead of bno.quaternion
```

**Why:** Full 9DOF ROTATION_VECTOR includes magnetometer, which is unreliable on
sailboats (lead keel, stainless rigging, engine cause 10-20° heading drift).
Magnetometer interference can also corrupt roll/pitch outputs. GPS COG provides
more reliable heading above 2-3 knots.

### Calibration

- **Startup tare:** Record current orientation as "zero" when device is mounted
  and boat is level at dock. Accounts for mounting angle inside enclosure.
- **Daily boat changes:** Require software calibration routine keyed to known
  heading at startup rather than physical alignment
- **Gyro:** Auto-zeros on startup (hold still 2-3 seconds)
- **Accelerometer:** Gravity reference handles heel/pitch automatically
- **Log calibration accuracy bits** for post-race data quality assessment

---

## S1 OS & Software Environment

- **OS:** Raspberry Pi OS Bookworm (64-bit)
- **Python:** 3.11+, async preferred
- **Config file:** `/boot/firmware/config.txt` (Bookworm location)
- **Camera stack:** `rpicam-*` commands (libcamera is deprecated on Bookworm)
- **Storage:** All writes directed to NVMe `/data` mount; SD card as near-read-only boot volume

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

## S1 Sensor Wiring Summary

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

### Newhaven Display (NHD-5.0-HDMI-N-RSXP)
```
Pi 5 micro-HDMI0 ──── HDMI cable ────► Display HDMI input
Pi 5 GPIO Pin 2 (5V) ──────────────► Display VDD (5V power)
Pi 5 GPIO GND (Pin 6) ─────────────► Display GND
Pi 5 GPIO 18 (PWM) ────────────────► Display PWM (optional dimming)
```

**Raspberry Pi 5 config** — add to `/boot/firmware/cmdline.txt` (append to existing line):
```
video=HDMI-A-1:800x480@60D
```

The `D` flag forces the display to be enabled even without proper EDID data.
The old `hdmi_group`/`hdmi_mode` settings in config.txt do NOT work with Pi 5's KMS driver.

Mount display face-up behind clear enclosure lid. At 1100 nits, readable in direct sunlight.

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

### Camera Power Management

Camera is the dominant power consumer. Strategy:
- **Maneuver-triggered recording:** Camera off by default, GPS-predictive turn-on near marks
- **Rolling pre-buffer:** Keeps camera running but only saves on trigger
- **Target 1080p/15fps** instead of 4K to reduce power and storage

### Camera Mounting

Lens-through-lid approach: drill ~8mm hole in enclosure lid, seal with marine silicone.
GoPro Hero 5 as supplemental recorder with GPS timestamp sync.

### Camera Preview During Recording

Dashboard preview extracts frames from **completed** video segments
(not the currently recording file). MP4 moov atom is written at end of file.
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
| Newhaven display (1100 nit) | ~2.8W |
| BNO085 | negligible |
| DPS310 | negligible |
| Wi-Fi AP mode | +0.1–0.2W |
| **Total** | **~7–10W** |

50,000mAh USB-C power bank (~185Wh):
- With display + camera: ~18–26 hours (covers multiple race days)
- With display, no camera: ~24+ hours

**Power saving tips:**
- Duty-cycle camera (burst recording on maneuver detection)
- PWM dim display in overcast conditions
- Record raw video, do CV/ML analysis in AWS post-race
- Disable second HDMI: add `hdmi_blanking=2` to config.txt

---

## Networking & Dashboard

- Pi 5 runs as **Wi-Fi Access Point** (hostapd) during races
- Dashboard served on port 8080 (Flask + Jinja2 templates)
- Crew connects via browser — no app install required
- Each boat = isolated network, no inter-boat interference
- Wi-Fi client config via netplan YAML for `wlan0`
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
│   │   └── raw_20260330_140000.ubx      # Raw UBX for RTKLIB
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

## Enclosure & Mounting

### S1 Enclosure (QILIPSU IP67)
- Daily install/remove on boat — no permanent cables, under 30 seconds
- Display mounted face-up against clear enclosure lid
- DPS310 requires Gore-Tex pressure vent (Amphenol LTW VENT-PS1, ~$2-5)
- Camera: lens-through-lid approach (~8mm hole, marine silicone seal)
- NVMe SSD in Lemorele slim enclosure mounted internally

### E1 Enclosure (YETLEBOX IP67 ABS)
- Clear lid for OLED visibility
- 5.9"×3.9"×2.8" — fits ESP32 + all breakout modules

### Wind Sensor Mounting
- RecPro suction cup mount + 1/4"-20 compatible extension pole
- Carbon fiber or aluminum, no permanent boat modifications

---

## Analytics Platform Roadmap

### Sensor-to-Feature Mapping

| Feature | ZED-F9P GPS | BNO085 IMU | Calypso Mini | DPS310 | Camera |
|---|---|---|---|---|---|
| SOG / COG | ✅ direct | | | | |
| VMG | ✅ SOG | | ✅ TWD | | |
| Polar diagram | ✅ SOG+COG | | ✅ TWD | | |
| Heel angle | | ✅ direct | | | |
| Pitch angle | | ✅ direct | | | |
| Turning rate | | ✅ direct (yaw) | | | |
| Maneuver detection | ✅ COG change | ✅ yaw rate trigger | ✅ TWA flip confirms | | |
| Wind direction | | | ✅ direct | | |
| TWA | ✅ COG | | ✅ TWD | | |
| Barometric pressure | | | | ✅ direct | |
| Distance to start | ✅ position | | | | |
| Sail shape analysis | | | | | ✅ CV/ML |
| Video sync | ✅ timestamps | | | | ✅ footage |

### Build Phases

**Phase 1 — Data Foundation:** Boat profiles, session management, data import/export,
raw data storage schema. Sensor data flowing from device → S3 → database.

**Phase 2 — Core Analytics:** Wind calculation, maneuver detection, straight line
segmentation, all per-maneuver and per-leg metrics. Computational heart.

**Phase 3 — Visualization:** Polar diagram, wind graph, session summary, maneuver
charts (SOG/VMG/heel/pitch/turning rate/TWA/distance loss), map player with
trajectory replay.

**Phase 4 — Statistical Analysis:** Violin plots, STD plots, correlation plots
with trendlines, straight line tables with sorting/filtering. Multi-boat comparison.

**Phase 5 — Video Integration:** Video upload, timestamp sync with offset,
synchronized playback with data graphs, frame-by-frame controls.

**Phase 6 — Advanced Tools:** Rig analyzer (canvas drawing + measurement), social
video export, PDF reports, leaderboard.

**Phase 7 — SailFrames Originals:** Sail shape analysis from camera, fleet-wide
6-boat comparison, real-time on-boat dashboard, NOAA/weather integration,
GOES satellite imagery overlay, current field reconstruction from fleet GPS data.

### Data Flow

```
[On Boat]
  Sensors → services/ → local storage (NVMe/SD)
  Wi-Fi AP → live dashboard to crew phones

[Post Race]
  S1 data → S3 upload (scripts/sailframes-sync.sh) → s3://sailframes-fleet-data-prod/raw/{device}/{date}/{sensor}/
  E1 data → API Gateway → upload Lambda → s3://sailframes-fleet-data-prod/raw/{device}/{date}/
  S3 → Lambda trigger (process_upload) → processed JSON
  Processed results → PostgreSQL
  Weather APIs → Lambda → PostgreSQL

[Web App]
  Browser → web/frontend/ (React)
  React → web/api/ (FastAPI) → PostgreSQL + S3
  Video player ↔ data graphs (synchronized via timestamps)
  Export → PDF, PNG, social video (FFmpeg)
```

**S3 Path Formats:**
- S1: `raw/{device_id}/{date}/{sensor_type}/{filename}.csv` (e.g., `raw/sailframes-01/2026-04-01/gps/track_20260401_140000.csv`)
- E1: `raw/{device_id}/{date}/{filename}.csv` (e.g., `raw/E1/2026-04-01/E1_20260401_140000_nav.csv`)

### Claude Code Session Strategy

Parallel sessions, each scoped to a module:

| Session | Directory | Scope |
|---|---|---|
| 1 | `processing/` | Maneuver detection, straight line segmentation, wind calc, VMG, polar, stats |
| 2 | `web/api/` | FastAPI backend: boat profiles, sessions, data serving |
| 3 | `web/frontend/` | React: charts, map player, video player, violin plots |
| 4 | `export/` | PDF reports, social video, graph export |
| 5 | `aws/` | S3 sync, Lambda pipelines, weather integration |

All sessions read this CLAUDE.md automatically. If one session changes an API
contract or data schema, update this file so other sessions pick it up.

---

## Known Issues & Gotchas

1. **BNO085 I2C clock stretching** — must set baudrate=400000 in config.txt.
   Default 100kHz causes intermittent read errors. 400kHz is the fix (not slower).

2. **Pi 4 vs Pi 5** — project uses Pi 5. Pi 4 images do NOT boot on Pi 5.
   Reflash with Pi Imager selecting Pi 5 as target device.

3. **Camera cable** — Pi Camera 3 Wide ships with 15-pin cable.
   Pi 5 requires 22-pin adapter cable. Must swap before connecting.

4. **DPS310 in sealed enclosure** — without a pressure vent, the sensor
   reads internal enclosure pressure, not ambient. Gore-Tex vent is required.

5. **Calypso wind sensor BLE** — only one device can connect at a time.
   Pi 5 BLE client will claim the connection; disconnect other devices first.

6. **Monitor service file descriptors** — excessive subprocess calls can exhaust
   file descriptors (~1024 per process). Service status checks are limited to
   every 60 seconds to prevent "Too many open files" crashes.

7. **Camera busy during recording** — Pi Camera can only be accessed by one
   process. Dashboard preview extracts frames from completed segments instead
   of direct capture when camera service is recording.

8. **DOP reflects geometry, not accuracy** — Good HDOP/VDOP values indicate
   favorable satellite geometry but do not guarantee positional accuracy,
   especially indoors.

9. **E1 GPIO conflict** — GPS UART must use GPIO16/17 (UART2), NOT GPIO21/22
   which are the I2C bus shared by BNO085 and OLED. Edit net labels on schematic
   sheet, not component symbol definition.

10. **KiCad Footprint Editor** — access from the KiCad main project launcher,
    not from within the schematic editor.

11. **ESP32 BLE/Wi-Fi radio conflict** — ESP32 has a single shared radio for BLE
    and Wi-Fi. Both cannot operate reliably simultaneously. Must fully deinitialize
    BLE before Wi-Fi uploads, then reinitialize BLE after. See E1 Wi-Fi Upload section.

12. **NimBLEDevice::deinit(true) crashes** — calling `deinit(true)` causes heap
    corruption and crashes. Always use `deinit(false)`. Also: disconnect Wi-Fi
    BEFORE deinitializing BLE, not after.

13. **macOS Spotlight files on SD card** — when SD card is inserted into Mac,
    Spotlight creates `.Spotlight-V100` and `.fseventsd` directories. These are
    harmless but appear in file listings. The firmware skips hidden files during upload.

14. **LG290P PQTM command syntax** — firmware AANR01A06S uses two-parameter syntax
    for `PQTMCFGMSGRATE` (message, rate). Three-parameter syntax (with offset)
    returns `ERROR,1`. PyGPSClient on macOS has limited LG290P support — use QGNSS
    on Windows for configuration.

15. **API Gateway 29-second timeout** — Lambda functions behind API Gateway have
    a hard 29-second timeout. Large file uploads fail with HTTP -3 (SEND_PAYLOAD_FAILED).
    Solution: use presigned S3 URLs for files ≥1MB.

16. **E1 GPS session folder naming** — Session folders use GPS date/time when available
    (e.g., `/sf/20260405_225030/`). The validation checks: (a) year portion of date
    is not "00" (default), and (b) GPS has valid fix. Previously failed for days 1-9
    of month and times 00:00-09:59 UTC due to incorrect first-character check.

17. **E1 deep sleep removed** — Software deep sleep had issues: button still pressed
    caused immediate wake, GPS module stayed powered. Replaced with hardware slide
    switch on PowerBoost 1000C EN pin for reliable power control.

---

## Weather Data Integration

- GOES-16/19 imagery on AWS S3 (`s3://noaa-goes16/`, `s3://noaa-goes19/`)
- Use `goes2go` Python library for easy access
- Boston NWS office (BOX) provides regional GOES crops
- Useful layers: visible (sea breeze), water vapor, GeoColor composite
- Can overlay GPS tracks on GOES imagery for post-race analysis
- NOAA NDBC buoys: 44013, BHBM3
- NOAA Tides and Currents: station 8443970
- Open-Meteo for weather forecasts

---

## Competitive Landscape

SailFrames exists in a field with several commercial and open-source alternatives
for sailing performance analysis. Key differentiators for SailFrames:
- **PPK GNSS** — no other platform uses post-processed kinematic positioning
- **Multi-sensor hardware** — dedicated IMU, wind, pressure, camera (not phone-only)
- **Fleet-wide simultaneous logging** — 6+ boats on same course, same time
- **Open source** — Apache 2.0, full hardware and software stack
- **No permanent install** — daily install/remove under 30 seconds

**Note:** Do not include competitor brand names in SailFrames documentation or code.
Competitive analysis is maintained separately.

---

## Tools & Resources

- **GNSS:** u-center (ZED-F9P config/logging), QGNSS (LG290P config, Windows only),
  RTKLIB (PPK post-processing), GNSS View app (satellite visibility diagnostics),
  pyubx2 (UBX binary parsing), NOAA UFCORS (free base station data)
- **Development:** Arduino IDE (ESP32), KiCad (schematic + PCB),
  Freerouting (autorouter), JLCPCB (PCB fabrication)
- **Cloud/data:** AWS S3, AWS Lambda, PostgreSQL
- **Reference texts:** Groves 2013 (GNSS/INS integration), Kaplan & Hegarty,
  Teunissen & Montenbruck, Markley & Crassidis (attitude estimation), Madgwick

---

## Project History

- Originally named **TrimLog** (trimlog.com taken)
- Renamed to **SailFrames** — global find-and-replace done across all 18 files
- March 2026: Reorganized as monorepo `sailframes/core`
  - `edge-s/` — Raspberry Pi software (S1 = first generation device)
  - `edge-e/` — ESP32 hardware/firmware (E1 = first generation device)
  - Added `web/`, `lambda/`, `infrastructure/`, `processing/`, `export/`
- March 19, 2026: Initial CLAUDE.md created from Claude.ai project conversations
- March 23, 2026: Added analytics platform roadmap (7 phases)
- March 29, 2026: E1 KiCad schematic completed, firmware written
- March 30, 2026: Added raw UBX logging, data management page, GPS constellation tracking
- March 30, 2026: Updated CLAUDE.md — added E1 hardware/firmware/wiring, PPK strategy,
  Newhaven display, NVMe storage, power bank, BNO085 calibration, analytics roadmap,
  removed competitor brand names, corrected stale hardware references
- April 4, 2026: E1 Wi-Fi upload fixes and LG290P configuration
  - Fixed ESP32 BLE/Wi-Fi radio conflict (must deinit BLE before Wi-Fi uploads)
  - Implemented presigned S3 URLs for large files (bypass API Gateway timeout)
  - Switched to HTTP for S3 uploads (faster, no TLS overhead for non-sensitive data)
  - Added `clearmarkers` command to retry failed uploads
  - Documented LG290P RTCM3 configuration via QGNSS (PyGPSClient has limited support)
  - Added NimBLEDevice::deinit(false) requirement (deinit(true) causes heap corruption)
- April 5, 2026: E1 power management and RTCM3 fixes
  - Removed software deep sleep (replaced with hardware power switch on PowerBoost EN pin)
  - Enabled battery monitoring (GPIO34 ADC via voltage divider, GPIO35 LBO warning)
  - OLED display: battery % instead of MAG heading, satellites show "in fix/in view"
  - Firmware now sends RTCM3 configuration commands at boot (not relying on QGNSS pre-config)
  - Fixed GPS session folder naming for days 1-9 and times 00:00-09:59 UTC
  - Added `sendPQTM()` response logging for debugging configuration issues
  - Clarified BLE/WiFi conflict is specifically about TLS memory pressure

---

*Last updated: April 5, 2026 — E1 power management, battery monitoring, RTCM3 boot config*
