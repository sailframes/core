# SailFrames E1 — LG290P Configuration & Firmware Updates

## Date: April 4, 2026

---

## LG290P Waveshare Board — Hardware Findings

### SH1.0 Connectors
The Waveshare LG290P board has **two SH1.0 4-pin connectors** on the bottom:
- **Left connector:** labeled **I2C** (SDA, SCL, power) — I2C is marked "reserved", not functional yet
- **Right connector:** labeled **UART** — wired to UART3 (TXD3/RXD3)

The **UART SH1.0 connector can be used** to connect LG290P to ESP32. No need to solder to castellated pads. The SH1.0 cable has female DuPont ends on the other side for direct connection to ESP32 pins.

### UART SH1.0 Cable Wiring to ESP32
| SH1.0 Wire | ESP32 Pin |
|------------|-----------|
| TX | GPIO16 (RX2) |
| RX | GPIO17 (TX2) |
| VCC | 3V3 |
| GND | GND |

**Important:** Verify pin order on the SH1.0 connector — could be TX/RX/VCC/GND or GND/VCC/TX/RX. Check Waveshare silkscreen.

### RST Button
- Single press: reboots the module (same as `$PQTMSRR*4B`)
- No long-press or factory reset function
- Factory reset requires explicit command: `$PQTMRESTOREPAR*13`
- Saved config in NVM is safe from accidental button presses

### USB-C Port
- For connecting to PC (QGNSS on Windows, PyGPSClient on macOS)
- Cannot be used for ESP32 connection (ESP32 is not a USB host)
- Uses CH343 USB-to-serial chip

### Board Pinout (from silkscreen, back of board)
**Left side (top to bottom):** 5V, GND, 3V3, GND, RXD3, TXD3, RST, PPS, EVENT
**Right side (top to bottom):** 5V, GND, SDA, 3V3, PWR, RXD2, TXD2, SCL, SDA, RTK
**Bottom (left to right):** RXD3, TXD3, GND, 3V3, GND, SDA, SCL, 3V3, GND

---

## LG290P UART Configuration

### Default Protocol Settings
All 4 UARTs output NMEA + RTCM3 + QGC by default (bitmask `00000007`):
- Bit 0 = NMEA
- Bit 1 = QGC (Quectel binary)
- Bit 2 = RTCM3

### Query UART Protocol
```
$PQTMCFGPROT,R,1,<PortID>*
```
Where PortID: 1=UART1, 2=UART2, 3=UART3, 4=UART4

Example response: `$PQTMCFGPROT,OK,1,3,00000007,00000007*69`

### Default Baud Rate
460800 baud on all UART ports.

---

## RTCM3 Raw Data Configuration for PPK

### Enabling MSM7 Output (Critical for PPK)
Use `PQTMCFGRTCM` command — this enables MSM messages globally:
```
$PQTMCFGRTCM,W,7,0,-90,07,06,1,0*
```
- First parameter `7` = MSM7 (vs MSM4 default)
- Enables GPS, GLONASS, Galileo, BeiDou MSM7 messages (1077/1087/1097/1127)

**Do NOT use** `PQTMCFGMSGRATE` for individual MSM messages — firmware AANR01A06S returns `ERROR,1` for that syntax.

### Enabling Ephemeris Messages
Use `PQTMCFGMSGRATE` with **two parameters** (message name + rate, NO offset):
```
$PQTMCFGMSGRATE,W,RTCM3-1019,1*    GPS ephemeris
$PQTMCFGMSGRATE,W,RTCM3-1020,1*    GLONASS ephemeris
$PQTMCFGMSGRATE,W,RTCM3-1042,1*    BeiDou ephemeris
$PQTMCFGMSGRATE,W,RTCM3-1046,1*    Galileo ephemeris
```

**Note:** The three-parameter syntax `PQTMCFGMSGRATE,W,RTCM3-1019,1,0*` (with offset) causes `ERROR,1` on firmware AANR01A06S. Use rate only.

### Enabling Station Reference
```
$PQTMCFGMSGRATE,W,RTCM3-1006,10*   Station coords every 10 epochs
```

### Save and Restart
```
$PQTMSAVEPAR*
$PQTMHOT*
```

### Complete RTCM3 Messages After Configuration
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

### Ephemeris Note
Ephemeris messages don't appear every epoch. They broadcast every 30 seconds to several minutes. For PPK, they will be captured many times during a race session. If ephemeris is missing from the RTCM3 log, RTKCONV can still generate RINEX observation files — navigation/ephemeris data can be supplemented from NOAA CORS or IGS downloads.

---

## Firmware v2.0 Updates Required

### configureLG290P() — Corrected Command Syntax
```cpp
void configureLG290P() {
  // Enable MSM7 for all constellations (single command)
  sendPQTM("PQTMCFGRTCM,W,7,0,-90,07,06,1,0");

  // Enable ephemeris — NO offset parameter
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1019,1");  // GPS eph
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1020,1");  // GLONASS eph
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1042,1");  // BeiDou eph
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1046,1");  // Galileo eph

  // Station reference every 10 epochs
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1006,10");

  // Fix rate
  char cmd[64];
  snprintf(cmd, sizeof(cmd), "PQTMCFGFIXRATE,W,%d", 1000 / config.gps_rate_hz);
  sendPQTM(cmd);

  // Save + restart
  sendPQTM("PQTMSAVEPAR");
  delay(200);
  sendPQTM("PQTMHOT");
  delay(2000);
  while (Serial2.available()) Serial2.read();
}
```

### sendPQTM() — Checksum Calculator
```cpp
void sendPQTM(const char* body) {
  uint8_t cs = 0;
  for (int i = 0; body[i] != '\0'; i++) cs ^= body[i];
  char buf[128];
  snprintf(buf, sizeof(buf), "$%s*%02X\r\n", body, cs);
  Serial2.print(buf);
  Serial.print("[CMD] ");
  Serial.print(buf);
  delay(100);
}
```

---

## Constellation Configuration

### GLONASS Was Disabled by Default
On the tested module (firmware AANR01A06S, Sept 2025), GLONASS showed 0 satellites. After enabling via QGNSS:

```
$PQTMCFGCNST,W,1,1,1,1,1,1*   (GPS,GLO,GAL,BDS,QZSS,NavIC all enabled)
$PQTMSAVEPAR*
$PQTMHOT*
```

GLONASS satellites appeared (6 tracked). The constellation config command syntax in PyGPSClient NMEA Config dialog was rejected — use QGNSS on Windows instead.

### Query Constellation Config
```
$PQTMCFGCNST,R*
```
Response format: `$PQTMCFGCNST,OK,<GPS>,<GLO>,<GAL>,<BDS>,<QZSS>,<NavIC>*`

---

## macOS Software Setup

### PyGPSClient
- Install: `pip install pygpsclient` (in venv)
- Launch: `/Users/paul2/path/to/venv/bin/pygpsclient`
- Supports LG290P PQTM commands via NMEA Config dialog
- Some PQTM commands may be rejected — use QGNSS on Windows for complex config

### CH343 USB Driver for macOS
- Install via Homebrew: `brew install --cask wch-ch34x-usb-serial-driver`
- Or download from: https://www.wch-ic.com/downloads/CH34XSER_MAC_ZIP.html
- After install: open Launchpad → CH34xVCPDriver → click Install
- Enable in: System Settings → General → Login Items & Extensions → Driver Extensions
- Serial port appears as: `/dev/cu.wchusbserial5B5E0696771`
- Baud rate: 460800

### PyGPSClient Settings for LG290P
- Serial port: `/dev/cu.wchusbserial5B5E0696771`
- Baud rate: 460800 (NOT 115200)
- Protocols: NMEA ✓, RTCM ✓

---

## PPK Post-Processing Workflow

### Files Generated per Session
```
/sf/YYYYMMDD/E1_YYYYMMDD_HHMMSS_nav.csv      ← parsed NMEA (lat,lon,sog,cog,sat,hdop)
/sf/YYYYMMDD/E1_YYYYMMDD_HHMMSS_imu.csv      ← heel, pitch, accel, gyro (20Hz)
/sf/YYYYMMDD/E1_YYYYMMDD_HHMMSS.rtcm3        ← raw RTCM3 binary for PPK
```

### Step 1: Convert RTCM3 to RINEX
Open RTKCONV (part of RTKLIB):
- Input: `*.rtcm3` file
- Format: **RTCM3**
- Output: `.obs` (observations) + `.nav` (navigation/ephemeris)

### Step 2: Download CORS Base Station Data
- NOAA UFCORS: https://geodesy.noaa.gov/UFCORS/
- Select nearest CORS station to Boston Harbor
- Match date/time window to race session
- Download RINEX observation + navigation files
- Also available on AWS S3: `noaa-cors-pds.s3.amazonaws.com`

### Step 3: Process PPK in RTKPOST
- Rover: converted `.obs` file from E1
- Base: CORS `.obs` file
- Navigation: `.nav` files
- Mode: Kinematic
- Output: centimeter-level position solution

---

## SBAS / WAAS Status
- Fix quality 2 (DGPS/SBAS) confirmed — WAAS corrections active
- WAAS satellites visible: PRN 44 (C/N0 ~36), PRN 46 (C/N0 ~40), PRN 48 (C/N0 ~35)
- SBAS provides ~1-3m real-time accuracy improvement
- SBAS corrections are irrelevant for PPK (CORS corrections are far more precise)

---

## Observed Performance (Brookline, MA — indoor near window)
- Satellites in use: 23-25
- Constellations: GPS (6), GLONASS (6), Galileo (7-8), BeiDou (5)
- HDOP: 0.54-0.99
- Fix quality: 2 (SBAS)
- Update rate: 10Hz (100ms epochs)
- Position stability (stationary): ~1m scatter

---

## Module Info
- Hardware: Quectel LG290P03
- Firmware: AANR01A06S 2025/09/18-15:57:00
- Waveshare breakout board (no RTC battery version)
- USB chip: CH343 (Vendor ID 0x1a86)
- macOS serial port: `/dev/cu.wchusbserial5B5E0696771`
