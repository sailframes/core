# SailFrames E1 — Fleet Tracker Firmware

## Required Arduino Libraries

Install these via Arduino IDE → Sketch → Include Library → Manage Libraries:

1. **Adafruit SSD1306** — OLED display driver
2. **Adafruit GFX Library** — graphics dependency for SSD1306

All other libraries (Wire, SPI, SD, WiFi, HTTPClient) are built into the ESP32 Arduino core.

## Arduino IDE Settings

- Board: **ESP32 Dev Module**
- Upload Speed: 921600
- Flash Frequency: 80MHz
- Partition Scheme: Default 4MB with spiffs
- Port: (select your ESP32's serial port)

## SD Card Setup

1. Format a 16GB microSD card as FAT32
2. Copy `config.txt` to the root of the card
3. Edit `config.txt` with your yacht club Wi-Fi credentials
4. Insert the card into the SD card module

## Wiring

| ESP32 Pin | Function | Device |
|-----------|----------|--------|
| GPIO16 | UART RX2 | LG290P TX |
| GPIO17 | UART TX2 | LG290P RX |
| GPIO21 | I2C SDA | MPU-6050, OLED |
| GPIO22 | I2C SCL | MPU-6050, OLED |
| GPIO23 | SPI MOSI | SD Card |
| GPIO19 | SPI MISO | SD Card |
| GPIO18 | SPI CLK | SD Card |
| GPIO5 | SPI CS | SD Card |
| 3V3 | Power | All sensors |
| GND | Ground | All sensors |
| VIN | 5V input | Battery Shield |

## Behavior

1. Power on → splash screen → init sensors
2. Wait for GPS fix (up to 5 minutes)
3. Auto-start logging to SD card
4. OLED shows: satellites, speed, heading, heel, pitch, data size
5. When yacht club Wi-Fi detected → auto-upload to AWS S3
6. Power off → done

## Log Files

Per session, three files are created:
- `*_nav.csv` — GPS position, speed, heading (10Hz)
- `*_imu.csv` — accelerometer, gyroscope, heel, pitch (20Hz)
- `*_raw.bin` — raw GNSS data for PPK post-processing

## PPK Post-Processing

1. Copy `*_raw.bin` from the SD card
2. Convert to RINEX using RTKCONV
3. Download CORS base station data from NOAA UFCORS
4. Process with RTKPOST → centimeter-level positions
