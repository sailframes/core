# SailFrames E1 v2.0 — Fleet Tracker + PPK Logger

## What's New in v2.0
- **RTCM3 MSM7 raw data logging** — captures full pseudorange, carrier phase,
  doppler, and CNR from GPS, GLONASS, Galileo, and BeiDou for PPK post-processing
- **Ephemeris logging** — GPS/GLONASS/BeiDou/Galileo ephemeris captured for
  standalone RINEX conversion
- **Auto-configures LG290P** at startup — no manual PQTM setup needed
- **BNO085 support** with MPU-6050 fallback
- **RTCM3 frame parser** — separates binary RTCM3 from NMEA text on shared UART

## Wiring

### ESP32 DevKit V1 Pin Assignments
| ESP32 Pin | Function     | Device          |
|-----------|-------------|-----------------|
| GPIO16    | UART RX2    | LG290P TX       |
| GPIO17    | UART TX2    | LG290P RX       |
| GPIO21    | I2C SDA     | BNO085 + OLED   |
| GPIO22    | I2C SCL     | BNO085 + OLED   |
| GPIO23    | SPI MOSI    | SD Card         |
| GPIO19    | SPI MISO    | SD Card         |
| GPIO18    | SPI CLK     | SD Card         |
| GPIO5     | SPI CS      | SD Card         |
| VIN       | 5V Power    | Battery Shield  |
| GND       | Ground      | All devices     |

### LG290P Connection (via SH1.0 4-pin UART cable)
| SH1.0 Wire | ESP32 Pin   |
|------------|-------------|
| TX         | GPIO16 (RX2)|
| RX         | GPIO17 (TX2)|
| VCC        | 3V3         |
| GND        | GND         |

## Log Files

Per session, three files are created on the SD card:
```
/sf/20260331/E1_20260331_141523_nav.csv     ← parsed GPS (lat,lon,sog,cog)
/sf/20260331/E1_20260331_141523_imu.csv     ← heel, pitch, accel, gyro
/sf/20260331/E1_20260331_141523.rtcm3       ← raw RTCM3 binary for PPK
```

## PPK Post-Processing Workflow

### 1. Convert RTCM3 to RINEX
Open **RTKCONV** (part of RTKLIB):
- Input file: `E1_20260331_141523.rtcm3`
- Input format: **RTCM3**
- Click Convert → produces .obs (observations) and .nav (navigation) files

### 2. Download CORS Base Station Data
Go to NOAA UFCORS: https://geodesy.noaa.gov/UFCORS/
- Select nearest station to Boston Harbor
- Set date/time to match your race window
- Download RINEX observation + navigation files

### 3. Process PPK
Open **RTKPOST** (part of RTKLIB):
- Rover: your converted .obs file
- Base: CORS .obs file
- Navigation: .nav files from both
- Solution mode: Kinematic
- Click Execute → centimeter-level positions

## Libraries Required
Install via Arduino Library Manager:
- **U8g2** — OLED display driver (native SSD1309 support, no scrolling issues)
- **Adafruit BNO08x** — BNO085 IMU driver (uses SHTP protocol)

## Arduino IDE Settings
- Board: ESP32 Dev Module
- Upload Speed: 921600
- Flash Frequency: 80MHz
- Partition Scheme: Default 4MB

## SD Card Setup
1. Format as FAT32
2. Copy `config.txt` to root
3. Edit boat_id, Wi-Fi credentials, GPS rate
