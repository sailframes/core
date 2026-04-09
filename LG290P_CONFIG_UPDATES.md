# SailFrames E1 — LG290P Configuration & Firmware Updates

## Date: April 6, 2026 (Updated)

---

## Critical Discovery: MSM7 Configuration Requires QGNSS

**The LG290P cannot be configured for MSM7 output via ESP32 UART commands.** The firmware (AANR01A06S) returns `ERROR` for MSM7 message rate commands sent via UART. Configuration must be done via **QGNSS software on Windows** connected via USB.

Additionally, there is a **firmware quirk requiring a two-stage reset process** - receiver mode changes need a reset before MSM7 configuration will persist.

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

## MSM7 Configuration for PPK (CRITICAL)

### Problem
Without MSM7 messages, PPK post-processing will fail. The default LG290P output only includes:
- Message 1006 (Reference Station ARP) — not useful for rover PPK
- Ephemeris messages (1019/1020/1042/1046) — satellite orbit data

MSM7 messages (1077/1087/1097/1127) contain the raw pseudorange and carrier phase observations **required** for PPK processing with RTKLIB.

### Why ESP32 Commands Fail
```
[CMD] $PQTMCFGMSGRATE,W,RTCM3-1077,1*5C
[RSP] $PQTMCFGMSGRATE,ERROR,1*68
```

The firmware AANR01A06S does not accept MSM7 configuration via UART. The `PQTMCFGRTCM` command configures RTK correction **input**, not RTCM3 **output**.

### Solution: Two-Stage QGNSS Configuration

The LG290P has a firmware quirk where **receiver mode changes require a reset before additional configuration commands will persist**. Without this two-stage process, MSM7 settings revert to MSM4 on power cycle.

---

## Step-by-Step QGNSS Configuration (MUST FOLLOW EXACTLY)

### Prerequisites
- Windows PC with QGNSS installed (download from Quectel)
- USB cable to connect LG290P directly (bypass ESP32)

### Step 1: Connect to LG290P via USB
1. Disconnect LG290P from ESP32
2. Connect LG290P to Windows PC via USB-C
3. Open QGNSS
4. Select correct COM port (check Device Manager for CH343 port)
5. Set baud rate to **460800**
6. Click Connect

### Step 2: Open QConsole
- Click **View → QConsole** (or find QConsole window)
- This allows sending raw PQTM commands

### Step 3: First Reset Cycle (Receiver Mode)
Send these commands **one at a time**, pressing Enter after each:

```
$PQTMCFGRCVRMODE,W,1*2B
$PQTMSAVEPAR*5A
$PQTMSRR*4B
```

**Wait 6-8 seconds** for the module to reset. QGNSS will show "Disconnected" then reconnect automatically.

### Step 4: Configure MSM7 Output (After Reconnect)
Once QGNSS shows connected again, send these commands **one at a time**:

```
$PQTMCFGMSGRATE,W,RTCM3-1077,1,0*40
$PQTMCFGMSGRATE,W,RTCM3-1087,1,0*4F
$PQTMCFGMSGRATE,W,RTCM3-1097,1,0*4E
$PQTMCFGMSGRATE,W,RTCM3-1127,1,0*44
$PQTMCFGMSGRATE,W,RTCM3-1019,1,0*48
$PQTMCFGMSGRATE,W,RTCM3-1020,1,0*42
$PQTMCFGMSGRATE,W,RTCM3-1042,1,0*46
$PQTMCFGMSGRATE,W,RTCM3-1046,1,0*42
$PQTMCFGMSGRATE,W,RTCM3-1006,10,0*7E
```

**Expected response for each:** `$PQTMCFGMSGRATE,OK,...`

If you see `ERROR`, the command syntax is wrong or the module didn't complete the first reset. Start over from Step 3.

### Step 5: Second Save + Reset
```
$PQTMSAVEPAR*5A
$PQTMSRR*4B
```

**Wait 6-8 seconds** for reset.

### Step 6: Verify Configuration
After QGNSS reconnects, query the rates to verify:

```
$PQTMCFGMSGRATE,R,RTCM3-1077*44
$PQTMCFGMSGRATE,R,RTCM3-1087*4B
$PQTMCFGMSGRATE,R,RTCM3-1097*4A
$PQTMCFGMSGRATE,R,RTCM3-1127*40
```

**Expected response:** `$PQTMCFGMSGRATE,OK,RTCM3-XXXX,1,0*XX` with rate=1

If you see `ERROR` or rate=0, the configuration did not persist. Repeat from Step 3.

### Step 7: Disconnect and Reconnect to ESP32
1. Close QGNSS
2. Disconnect USB from LG290P
3. Reconnect LG290P to ESP32 via UART (SH1.0 cable)
4. Power on ESP32

---

## Verification on ESP32

After reconnecting to ESP32, the serial monitor should show:

```
[RTCM3] === 30s Summary ===
[RTCM3] Total frames: 180 (sync bytes: 185)
[RTCM3] MSM7 (PPK): GPS=30 GLO=30 GAL=30 BDS=30
[RTCM3] Eph: GPS=3 GLO=3 BDS=3 GAL=3
[RTCM3] Ref: 1006=3, Other=0
```

**If MSM7 counts are still 0:**
- The two-stage reset was not performed correctly
- Repeat the QGNSS configuration from Step 3

---

## Command Syntax Notes

### Correct MSM7 Command Format
```
$PQTMCFGMSGRATE,W,RTCM3-1077,1,0*40
                              ↑ ↑
                              │ └─ offset (REQUIRED)
                              └─── rate
```

The offset parameter `,0` is **required** for MSM7 messages. Without it, the command fails.

### Incorrect Format (Will Fail)
```
$PQTMCFGMSGRATE,W,RTCM3-1077,1*5C    ← Missing offset, returns ERROR
```

### Pre-computed Checksums
All commands above include correct checksums. If editing commands, recalculate:
```
checksum = XOR of all characters between $ and *
```

---

## RTCM3 Message Types Reference

| Message | Type | Purpose | Needed for PPK |
|---------|------|---------|----------------|
| 1006 | Reference Station | Base station position | No (rover mode) |
| 1019 | GPS Ephemeris | GPS satellite orbits | Yes |
| 1020 | GLONASS Ephemeris | GLONASS satellite orbits | Yes |
| 1042 | BeiDou Ephemeris | BeiDou satellite orbits | Yes |
| 1046 | Galileo Ephemeris | Galileo satellite orbits | Yes |
| **1077** | **GPS MSM7** | **GPS raw observations** | **REQUIRED** |
| **1087** | **GLONASS MSM7** | **GLONASS raw observations** | **REQUIRED** |
| **1097** | **Galileo MSM7** | **Galileo raw observations** | **REQUIRED** |
| **1127** | **BeiDou MSM7** | **BeiDou raw observations** | **REQUIRED** |

---

## E1 Firmware Debug Commands

The E1 firmware includes RTCM3 debugging via telnet (port 23) or serial:

| Command | Description |
|---------|-------------|
| `rtcm` | Show detailed RTCM3 message type counts |
| `rtcmreset` | Reset all RTCM3 counters |
| `status` | Show summary including RTCM3 stats |
| `gpscfg` | Reconfigure GPS (won't fix MSM7 - needs QGNSS) |

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

## Constellation Configuration

### GLONASS Was Disabled by Default
On the tested module (firmware AANR01A06S, Sept 2025), GLONASS showed 0 satellites. After enabling via QGNSS:

```
$PQTMCFGCNST,W,1,1,1,1,1,1*   (GPS,GLO,GAL,BDS,QZSS,NavIC all enabled)
$PQTMSAVEPAR*5A
$PQTMSRR*4B
```

GLONASS satellites appeared (6 tracked).

### Query Constellation Config
```
$PQTMCFGCNST,R*
```
Response format: `$PQTMCFGCNST,OK,<GPS>,<GLO>,<GAL>,<BDS>,<QZSS>,<NavIC>*`

---

## PPK Processing Pipeline

Once MSM7 is configured:

1. **E1 logs RTCM3** to SD card: `/sf/YYYYMMDD/E1_YYYYMMDD_HHMMSS_raw.rtcm3`
2. **Upload to S3** via WiFi: `raw/{device_id}/{date}/E1_*_raw.rtcm3`
3. **CORS Lambda** downloads NOAA base station data (MAMI station)
4. **PPK Lambda** runs RTKLIB:
   - `convbin` converts RTCM3 → RINEX
   - `rnx2rtkp` processes rover + base → PPK solution
5. **Results** stored in `processed/{device_id}/{folder}/ppk_gps.json`

### Files Generated per Session
```
/sf/YYYYMMDD/E1_YYYYMMDD_HHMMSS_nav.csv      ← parsed NMEA (lat,lon,sog,cog,sat,hdop)
/sf/YYYYMMDD/E1_YYYYMMDD_HHMMSS_imu.csv      ← heel, pitch, accel, gyro (20Hz)
/sf/YYYYMMDD/E1_YYYYMMDD_HHMMSS_raw.rtcm3    ← raw RTCM3 binary for PPK
```

---

## macOS Software Setup

### PyGPSClient
- Install: `pip install pygpsclient` (in venv)
- Launch: `/Users/paul2/path/to/venv/bin/pygpsclient`
- Supports LG290P PQTM commands via NMEA Config dialog
- **Cannot configure MSM7** — use QGNSS on Windows

### CH343 USB Driver for macOS
- Install via Homebrew: `brew install --cask wch-ch34x-usb-serial-driver`
- Or download from: https://www.wch-ic.com/downloads/CH34XSER_MAC_ZIP.html
- After install: open Launchpad → CH34xVCPDriver → click Install
- Enable in: System Settings → General → Login Items & Extensions → Driver Extensions
- Serial port appears as: `/dev/cu.wchusbserial5B5E0696771`
- Baud rate: 460800

---

## Module Info
- Hardware: Quectel LG290P03
- Firmware: AANR01A06S 2025/09/18-15:57:00
- Waveshare breakout board (no RTC battery version)
- USB chip: CH343 (Vendor ID 0x1a86)
- macOS serial port: `/dev/cu.wchusbserial5B5E0696771`

---

## References

- [Recording raw data with LG290P - Quectel Forums](https://forums.quectel.com/t/recording-raw-data-with-lg290p/46036)
- [MSM7 CFGRTCM not being respected on LG290P - Quectel Forums](https://forums.quectel.com/t/msm7-cfgrtcm-not-being-respected-on-lg290p-after-a-restore-unless-its-done-again/39327)
- [Quectel LG290P GNSS Protocol Specification v1.0](https://cdn.sparkfun.com/assets/f/7/b/c/c/Quectel_LG290P_GNSS_Protocol_Specification_v1-0.pdf)
- [SparkFun LG290P Software Overview](https://docs.sparkfun.com/SparkFun_LG290P_Quadband_GNSS_RTK_Breakout/software_overview/)

---

*Last updated: April 6, 2026*
