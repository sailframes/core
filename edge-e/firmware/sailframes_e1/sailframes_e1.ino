/*
 * SailFrames E1 — Fleet Tracker Firmware v2.0
 * 
 * Hardware:
 *   - ESP32 DevKit V1 (ELEGOO)
 *   - Waveshare LG290P GNSS (UART2: RX=GPIO16, TX=GPIO17, 460800 baud)
 *   - BNO085 IMU (I2C: 0x4A) — heel, pitch, heading
 *   - SSD1309 OLED 2.42" 128x64 (I2C: 0x3C)
 *   - MicroSD card module (SPI: MOSI=23, MISO=19, CLK=18, CS=5)
 *   - 18650 Battery Shield (5V → VIN)
 * 
 * Behavior:
 *   Power on → init sensors → configure LG290P for raw RTCM3 output
 *   → wait for GPS fix → auto-log to SD (NMEA CSV + RTCM3 binary)
 *   → when yacht club Wi-Fi detected → auto-upload to AWS S3
 *   Power off → done
 * 
 * PPK Workflow:
 *   1. Collect *_raw.rtcm3 from SD card
 *   2. Convert to RINEX using RTKCONV (input format: RTCM3)
 *   3. Download CORS RINEX from NOAA UFCORS for matching time window
 *   4. Process with RTKPOST → centimeter-level positions
 * 
 * Log files per session:
 *   /sf/YYYYMMDD/E1_YYYYMMDD_HHMMSS_nav.csv
 *   /sf/YYYYMMDD/E1_YYYYMMDD_HHMMSS_imu.csv
 *   /sf/YYYYMMDD/E1_YYYYMMDD_HHMMSS_raw.rtcm3
 * 
 * License: Apache 2.0
 * Project: https://github.com/sailframes
 */

#include <Wire.h>
#include <SPI.h>
#include <SD.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <U8g2lib.h>
#include <Adafruit_BNO08x.h>

// ============================================================
// PIN DEFINITIONS
// ============================================================
#define GPS_RX_PIN    16
#define GPS_TX_PIN    17
#define SD_CS_PIN     5
#define SDA_PIN       21
#define SCL_PIN       22
#define LED_PIN       2

// ============================================================
// CONFIGURATION
// ============================================================
#define GPS_BAUD      460800
#define SERIAL_BAUD   115200
#define SCREEN_WIDTH  128
#define SCREEN_HEIGHT 64
#define OLED_ADDR     0x3C
#define BNO085_ADDR   0x4B  // Alternate address (some boards use 0x4A)
#define MPU6050_ADDR  0x68
#define GPS_FIX_TIMEOUT_MS  300000
#define DISPLAY_UPDATE_MS   1000  // Slower updates reduce I2C contention
#define FLUSH_INTERVAL_MS   10000
#define IMU_INTERVAL_MS     50

// BNO085 IMU enabled
#define ENABLE_BNO085       true
// ============================================================
// NMEA CHECKSUM + PQTM SENDER
// ============================================================
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

// ============================================================
// DATA STRUCTURES
// ============================================================
struct GPSData {
  float lat = 0, lon = 0, alt = 0;
  float speed_kts = 0, course = 0, hdop = 99.9;
  int satellites = 0, fix_quality = 0;
  char utc_time[12] = "000000.00";
  char date[8] = "010100";
  bool valid = false;
  bool newGGA = false;
} gps;

struct IMUData {
  float accel_x = 0, accel_y = 0, accel_z = 0;
  float gyro_x = 0, gyro_y = 0, gyro_z = 0;
  float heel = 0, pitch = 0;
} imu;

struct RTCM3Parser {
  enum State { WAIT_SYNC, READ_HEADER, READ_PAYLOAD };
  State state = WAIT_SYNC;
  uint8_t header[3];
  int headerIdx = 0;
  uint16_t payloadLen = 0;
  uint8_t frameBuf[1200];
  int frameIdx = 0;
  int frameTotal = 0;
} rtcm;

#define MAX_WIFI_NETWORKS 5

struct WiFiNetwork {
  char ssid[64];
  char pass[64];
};

struct Config {
  WiFiNetwork wifi[MAX_WIFI_NETWORKS];
  int wifi_count = 0;
  char upload_url[256] = "";
  char boat_id[16] = "E1";
  int gps_rate_hz = 10;
} config;

// ============================================================
// GLOBALS
// ============================================================
// U8g2 for SSD1309 128x64 I2C - native support, no scrolling issues
U8G2_SSD1309_128X64_NONAME0_F_HW_I2C u8g2(U8G2_R0, /* reset=*/ U8X8_PIN_NONE);
Adafruit_BNO08x bno08x(-1);  // No reset pin
sh2_SensorValue_t sensorValue;
File navFile, imuFile, rawFile;
bool sdOK = false, imuOK = false, oledOK = false, logging = false;
bool useIMU_BNO = false;
unsigned long logStart = 0, lastDisp = 0, lastFlush = 0, lastIMU = 0;
unsigned long totalBytes = 0;
char nmeaBuf[256];
int nmeaIdx = 0;

// ============================================================
// SETUP
// ============================================================
void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(500);
  Serial.println("\n=================================");
  Serial.println("  SailFrames E1 v2.0 — PPK Logger");
  Serial.println("=================================");

  pinMode(LED_PIN, OUTPUT);
  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);  // 100kHz for SSD1309 stability

  // I2C Scanner - quick check for expected devices only
  Serial.println("[I2C] Checking devices...");
  Wire.beginTransmission(OLED_ADDR);
  bool oledFound = (Wire.endTransmission() == 0);
  Wire.beginTransmission(BNO085_ADDR);
  bool bnoFound = (Wire.endTransmission() == 0);
  Wire.beginTransmission(MPU6050_ADDR);
  bool mpuFound = (Wire.endTransmission() == 0);
  Serial.printf("[I2C] OLED 0x3C: %s\n", oledFound ? "YES" : "NO");
  Serial.printf("[I2C] BNO085 0x4A: %s\n", bnoFound ? "YES" : "NO");
  Serial.printf("[I2C] MPU6050 0x68: %s\n", mpuFound ? "YES" : "NO");

  // OLED - SSD1309 2.42" 128x64 using U8g2
  u8g2.begin();
  oledOK = true;  // U8g2 doesn't return status, assume OK if no crash
  Serial.println("[OLED] U8g2 SSD1309 initialized");

  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_helvB14_tr);  // 14px bold font
  u8g2.drawStr(10, 20, "SAIL");
  u8g2.drawStr(10, 42, "FRAMES");
  u8g2.setFont(u8g2_font_6x10_tr);     // Small font
  u8g2.drawStr(10, 58, "E1 v2.0 PPK");
  u8g2.sendBuffer();
  delay(1500);

  // SD Card - try multiple speeds
  Serial.println("[SD] Initializing SPI...");
  Serial.println("[SD] Pins: CLK=18, MISO=19, MOSI=23, CS=5");
  SPI.begin(18, 19, 23, SD_CS_PIN);
  pinMode(SD_CS_PIN, OUTPUT);
  digitalWrite(SD_CS_PIN, HIGH);
  delay(100);

  // Try different SPI speeds
  Serial.println("[SD] Trying 4MHz...");
  sdOK = SD.begin(SD_CS_PIN, SPI, 4000000);
  if (!sdOK) {
    Serial.println("[SD] 4MHz failed, trying 1MHz...");
    sdOK = SD.begin(SD_CS_PIN, SPI, 1000000);
  }
  if (!sdOK) {
    Serial.println("[SD] 1MHz failed, trying 400kHz...");
    sdOK = SD.begin(SD_CS_PIN, SPI, 400000);
  }

  if (sdOK) {
    uint8_t cardType = SD.cardType();
    if (cardType == CARD_NONE) {
      Serial.println("[SD] No card detected!");
      sdOK = false;
    } else {
      uint64_t cardSize = SD.cardSize() / (1024 * 1024);
      Serial.printf("[SD] OK - Card size: %llu MB\n", cardSize);
      Serial.printf("[SD] Card type: %s\n",
        cardType == CARD_MMC ? "MMC" :
        cardType == CARD_SD ? "SD" :
        cardType == CARD_SDHC ? "SDHC" : "UNKNOWN");
      loadConfig();
    }
  } else {
    Serial.println("[SD] === SD CARD FAILED ===");
    Serial.println("[SD] Troubleshooting:");
    Serial.println("[SD]   1. Check wiring:");
    Serial.println("[SD]      VCC -> 5V (NOT 3.3V for most modules!)");
    Serial.println("[SD]      GND -> GND");
    Serial.println("[SD]      CS  -> GPIO5");
    Serial.println("[SD]      MOSI-> GPIO23");
    Serial.println("[SD]      MISO-> GPIO19 (may be labeled DO)");
    Serial.println("[SD]      CLK -> GPIO18 (may be labeled SCK)");
    Serial.println("[SD]   2. Card must be FAT32 (not exFAT)");
    Serial.println("[SD]   3. Try a different SD card");
    Serial.println("[SD]   4. Some modules need card inserted before power");
  }

  // IMU — try BNO085 first (needs proper SHTP init), then MPU-6050
#if ENABLE_BNO085
  Serial.println("[IMU] Initializing BNO085...");
  if (bno08x.begin_I2C(BNO085_ADDR, &Wire)) {
    imuOK = true;
    useIMU_BNO = true;
    Serial.println("[IMU] BNO085 detected, enabling GAME_ROTATION_VECTOR");
    // Enable Game Rotation Vector (6DOF, no magnetometer - better for boats)
    if (!bno08x.enableReport(SH2_GAME_ROTATION_VECTOR, IMU_INTERVAL_MS * 1000)) {
      Serial.println("[IMU] WARNING: Failed to enable rotation vector");
    }
    // Also enable accelerometer for raw data
    if (!bno08x.enableReport(SH2_ACCELEROMETER, IMU_INTERVAL_MS * 1000)) {
      Serial.println("[IMU] WARNING: Failed to enable accelerometer");
    }
    Serial.println("[IMU] BNO085 OK at 0x4A");
  } else {
    Serial.println("[IMU] BNO085 not found, trying MPU-6050...");
#else
  Serial.println("[IMU] BNO085 disabled, trying MPU-6050...");
  {
#endif
    Wire.beginTransmission(MPU6050_ADDR);
    if (Wire.endTransmission() == 0) {
      Wire.beginTransmission(MPU6050_ADDR);
      Wire.write(0x6B); Wire.write(0x00);  // Wake up MPU-6050
      Wire.endTransmission();
      imuOK = true;
      Serial.println("[IMU] MPU-6050 OK at 0x68");
    } else {
      Serial.println("[IMU] No IMU found");
    }
  }

  // GPS
  Serial2.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
  Serial.printf("[GPS] UART2 at %d baud\n", GPS_BAUD);
  delay(1000);
  configureLG290P();

  // Wait for fix
  Serial.println("[GPS] Waiting for fix...");
  unsigned long t0 = millis();
  while (!gps.valid && millis() - t0 < GPS_FIX_TIMEOUT_MS) {
    readGPS();
    if (oledOK && millis() - lastDisp > 1000) {
      char buf[32];
      int dots = ((millis() - t0) / 500) % 4;
      char dotStr[5] = "";
      for (int i = 0; i < dots; i++) strcat(dotStr, ".");

      u8g2.clearBuffer();
      u8g2.setFont(u8g2_font_6x10_tr);
      u8g2.drawStr(0, 10, "SailFrames E1");
      snprintf(buf, sizeof(buf), "GPS: Searching%s", dotStr);
      u8g2.drawStr(0, 22, buf);
      snprintf(buf, sizeof(buf), "SAT: %d", gps.satellites);
      u8g2.drawStr(0, 34, buf);
      snprintf(buf, sizeof(buf), "HDOP: %.1f", gps.hdop);
      u8g2.drawStr(0, 46, buf);
      snprintf(buf, sizeof(buf), "Elapsed: %ds", (int)((millis() - t0) / 1000));
      u8g2.drawStr(0, 58, buf);
      u8g2.sendBuffer();
      lastDisp = millis();
    }
  }

  Serial.printf("[GPS] %s — SAT:%d HDOP:%.1f\n",
    gps.valid ? "FIX" : "TIMEOUT", gps.satellites, gps.hdop);

  // Start logging immediately if SD is OK (don't wait for GPS fix)
  if (sdOK) {
    startLogging();
  } else {
    Serial.println("[LOG] Cannot start logging - SD card not available");
  }
}

// ============================================================
// CONFIGURE LG290P FOR PPK
// ============================================================
void configureLG290P() {
  Serial.println("[GPS] Configuring raw RTCM3 output for PPK...");

  // RTCM3 MSM7 — full pseudorange + phase + doppler + CNR
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1077,1,0");  // GPS
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1087,1,0");  // GLONASS
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1097,1,0");  // Galileo
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1127,1,0");  // BeiDou

  // Ephemeris (needed for RINEX conversion)
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1019,1,0");  // GPS eph
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1020,1,0");  // GLONASS eph
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1042,1,0");  // BeiDou eph
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1046,1,0");  // Galileo eph

  // Station reference position
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1006,10,0");

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

  Serial.println("[GPS] Configured:");
  Serial.println("[GPS]   MSM7: GPS/GLO/GAL/BDS");
  Serial.println("[GPS]   Ephemeris: GPS/GLO/BDS/GAL");
  Serial.printf("[GPS]   Rate: %d Hz\n", config.gps_rate_hz);
}

// ============================================================
// READ GPS — NMEA text + RTCM3 binary
// ============================================================
void readGPS() {
  while (Serial2.available()) {
    uint8_t c = Serial2.read();

    // RTCM3 sync
    if (c == 0xD3 && rtcm.state == RTCM3Parser::WAIT_SYNC) {
      rtcm.state = RTCM3Parser::READ_HEADER;
      rtcm.header[0] = c;
      rtcm.headerIdx = 1;
      continue;
    }

    if (rtcm.state == RTCM3Parser::READ_HEADER) {
      rtcm.header[rtcm.headerIdx++] = c;
      if (rtcm.headerIdx >= 3) {
        rtcm.payloadLen = ((rtcm.header[1] & 0x03) << 8) | rtcm.header[2];
        if (rtcm.payloadLen > 1023) {
          rtcm.state = RTCM3Parser::WAIT_SYNC;
          if (c == '$') { nmeaBuf[0] = '$'; nmeaIdx = 1; }
          continue;
        }
        rtcm.frameTotal = 3 + rtcm.payloadLen + 3;
        memcpy(rtcm.frameBuf, rtcm.header, 3);
        rtcm.frameIdx = 3;
        rtcm.state = RTCM3Parser::READ_PAYLOAD;
      }
      continue;
    }

    if (rtcm.state == RTCM3Parser::READ_PAYLOAD) {
      if (rtcm.frameIdx < (int)sizeof(rtcm.frameBuf))
        rtcm.frameBuf[rtcm.frameIdx++] = c;
      if (rtcm.frameIdx >= rtcm.frameTotal) {
        if (rawFile && logging)
          rawFile.write(rtcm.frameBuf, rtcm.frameTotal);
        totalBytes += rtcm.frameTotal;
        rtcm.state = RTCM3Parser::WAIT_SYNC;
      }
      continue;
    }

    // NMEA parsing
    if (c == '$') {
      nmeaIdx = 0;
      nmeaBuf[nmeaIdx++] = c;
    } else if (c == '\n' || c == '\r') {
      if (nmeaIdx > 5) {
        nmeaBuf[nmeaIdx] = '\0';
        parseNMEA(nmeaBuf);
        nmeaIdx = 0;
      }
    } else if (nmeaIdx < (int)sizeof(nmeaBuf) - 1) {
      nmeaBuf[nmeaIdx++] = c;
    }
  }
}

// ============================================================
// NMEA PARSER
// ============================================================
bool getField(const char* s, int n, char* out, int mx) {
  int f = 0, i = 0, o = 0;
  while (s[i]) {
    if (s[i] == ',') {
      if (++f == n) {
        i++;
        while (s[i] && s[i] != ',' && s[i] != '*' && o < mx - 1)
          out[o++] = s[i++];
        out[o] = '\0';
        return o > 0;
      }
    }
    i++;
  }
  return false;
}

void parseNMEA(const char* s) {
  if (strstr(s, "GGA")) {
    char f[32];
    if (getField(s, 1, f, sizeof(f))) strncpy(gps.utc_time, f, sizeof(gps.utc_time) - 1);
    if (getField(s, 2, f, sizeof(f))) {
      float raw = atof(f);
      int deg = (int)(raw / 100);
      gps.lat = deg + (raw - deg * 100) / 60.0;
      char ns[4];
      if (getField(s, 3, ns, sizeof(ns)) && ns[0] == 'S') gps.lat = -gps.lat;
    }
    if (getField(s, 4, f, sizeof(f))) {
      float raw = atof(f);
      int deg = (int)(raw / 100);
      gps.lon = deg + (raw - deg * 100) / 60.0;
      char ew[4];
      if (getField(s, 5, ew, sizeof(ew)) && ew[0] == 'W') gps.lon = -gps.lon;
    }
    if (getField(s, 6, f, sizeof(f))) {
      gps.fix_quality = atoi(f);
      gps.valid = gps.fix_quality > 0;
    }
    if (getField(s, 7, f, sizeof(f))) gps.satellites = atoi(f);
    if (getField(s, 8, f, sizeof(f))) gps.hdop = atof(f);
    if (getField(s, 9, f, sizeof(f))) gps.alt = atof(f);
    gps.newGGA = true;
  } else if (strstr(s, "RMC")) {
    char f[32];
    if (getField(s, 7, f, sizeof(f))) gps.speed_kts = atof(f);
    if (getField(s, 8, f, sizeof(f))) gps.course = atof(f);
    if (getField(s, 9, f, sizeof(f))) strncpy(gps.date, f, sizeof(gps.date) - 1);
  }
}

// ============================================================
// READ IMU
// ============================================================
void readIMU() {
  if (!imuOK) return;

#if ENABLE_BNO085
  if (useIMU_BNO) {
    // BNO085 using Adafruit library with SHTP protocol
    if (bno08x.wasReset()) {
      Serial.println("[IMU] BNO085 was reset, re-enabling reports");
      bno08x.enableReport(SH2_GAME_ROTATION_VECTOR, IMU_INTERVAL_MS * 1000);
      bno08x.enableReport(SH2_ACCELEROMETER, IMU_INTERVAL_MS * 1000);
    }

    // Read only a few events to avoid blocking display updates
    int maxReads = 3;
    while (maxReads-- > 0 && bno08x.getSensorEvent(&sensorValue)) {
      switch (sensorValue.sensorId) {
        case SH2_GAME_ROTATION_VECTOR: {
          // Quaternion to Euler angles for heel and pitch
          float qr = sensorValue.un.gameRotationVector.real;
          float qi = sensorValue.un.gameRotationVector.i;
          float qj = sensorValue.un.gameRotationVector.j;
          float qk = sensorValue.un.gameRotationVector.k;

          // Roll (heel) = rotation around X axis
          float sinr_cosp = 2.0 * (qr * qi + qj * qk);
          float cosr_cosp = 1.0 - 2.0 * (qi * qi + qj * qj);
          imu.heel = atan2(sinr_cosp, cosr_cosp) * 180.0 / PI;

          // Pitch = rotation around Y axis
          float sinp = 2.0 * (qr * qj - qk * qi);
          if (fabs(sinp) >= 1)
            imu.pitch = copysign(90.0, sinp);
          else
            imu.pitch = asin(sinp) * 180.0 / PI;
          break;
        }
        case SH2_ACCELEROMETER:
          imu.accel_x = sensorValue.un.accelerometer.x;
          imu.accel_y = sensorValue.un.accelerometer.y;
          imu.accel_z = sensorValue.un.accelerometer.z;
          break;
      }
    }
  } else
#endif
  {
    // MPU-6050 direct register read fallback
    Wire.beginTransmission(MPU6050_ADDR);
    Wire.write(0x3B);
    Wire.endTransmission(false);
    Wire.requestFrom(MPU6050_ADDR, (uint8_t)14, (uint8_t)true);
    if (Wire.available() >= 14) {
      int16_t ax = (Wire.read() << 8) | Wire.read();
      int16_t ay = (Wire.read() << 8) | Wire.read();
      int16_t az = (Wire.read() << 8) | Wire.read();
      Wire.read(); Wire.read();  // Skip temperature
      int16_t gx = (Wire.read() << 8) | Wire.read();
      int16_t gy = (Wire.read() << 8) | Wire.read();
      int16_t gz = (Wire.read() << 8) | Wire.read();
      imu.accel_x = ax / 16384.0;
      imu.accel_y = ay / 16384.0;
      imu.accel_z = az / 16384.0;
      imu.gyro_x = gx / 131.0;
      imu.gyro_y = gy / 131.0;
      imu.gyro_z = gz / 131.0;
      imu.heel = atan2(imu.accel_y, imu.accel_z) * 180.0 / PI;
      imu.pitch = atan2(-imu.accel_x,
        sqrt(imu.accel_y * imu.accel_y + imu.accel_z * imu.accel_z)) * 180.0 / PI;
    }
  }
}

// ============================================================
// CONFIG
// ============================================================
void loadConfig() {
  File f = SD.open("/config.txt", FILE_READ);
  if (!f) { Serial.println("[CFG] No config.txt"); return; }
  Serial.println("[CFG] Loading config.txt");

  // Temp storage for parsing wifi entries
  char tempSSID[64] = "";
  char tempPass[64] = "";
  int currentWifiIdx = -1;

  while (f.available()) {
    String line = f.readStringUntil('\n');
    line.trim();
    if (line.startsWith("#") || line.length() == 0) continue;
    int eq = line.indexOf('=');
    if (eq < 0) continue;
    String k = line.substring(0, eq); k.trim();
    String v = line.substring(eq + 1); v.trim();

    // Parse wifi1_ssid, wifi2_ssid, etc. (1-indexed in config file)
    if (k.startsWith("wifi") && k.endsWith("_ssid")) {
      int idx = k.substring(4, k.length() - 5).toInt() - 1;  // wifi1 -> index 0
      if (idx >= 0 && idx < MAX_WIFI_NETWORKS) {
        v.toCharArray(config.wifi[idx].ssid, sizeof(config.wifi[idx].ssid));
        if (idx >= config.wifi_count) config.wifi_count = idx + 1;
      }
    }
    else if (k.startsWith("wifi") && k.endsWith("_pass")) {
      int idx = k.substring(4, k.length() - 5).toInt() - 1;
      if (idx >= 0 && idx < MAX_WIFI_NETWORKS) {
        v.toCharArray(config.wifi[idx].pass, sizeof(config.wifi[idx].pass));
      }
    }
    // Also support legacy single wifi_ssid/wifi_pass
    else if (k == "wifi_ssid") {
      v.toCharArray(config.wifi[0].ssid, sizeof(config.wifi[0].ssid));
      if (config.wifi_count == 0) config.wifi_count = 1;
    }
    else if (k == "wifi_pass") {
      v.toCharArray(config.wifi[0].pass, sizeof(config.wifi[0].pass));
    }
    else if (k == "upload_url") v.toCharArray(config.upload_url, sizeof(config.upload_url));
    else if (k == "boat_id") v.toCharArray(config.boat_id, sizeof(config.boat_id));
    else if (k == "gps_rate_hz") config.gps_rate_hz = v.toInt();
  }
  f.close();

  Serial.printf("[CFG] Boat: %s, Rate: %dHz, WiFi networks: %d\n",
    config.boat_id, config.gps_rate_hz, config.wifi_count);
  for (int i = 0; i < config.wifi_count; i++) {
    Serial.printf("[CFG]   %d: %s\n", i + 1, config.wifi[i].ssid);
  }
}

// ============================================================
// START LOGGING
// ============================================================
void startLogging() {
  Serial.println("[LOG] Starting logging...");

  // Use millis-based timestamp if no GPS date available
  char dd[24], ds[16], ts[12];
  bool hasGpsDate = (strlen(gps.date) >= 6 && gps.date[0] != '0');

  if (hasGpsDate) {
    // GPS date format is DDMMYY, convert to YYYYMMDD
    snprintf(dd, sizeof(dd), "/sf/20%c%c%c%c%c%c",
      gps.date[4], gps.date[5], gps.date[2], gps.date[3], gps.date[0], gps.date[1]);
    snprintf(ds, sizeof(ds), "20%c%c%c%c%c%c",
      gps.date[4], gps.date[5], gps.date[2], gps.date[3], gps.date[0], gps.date[1]);
  } else {
    // Fallback to boot-based folder
    unsigned long bootMs = millis();
    snprintf(dd, sizeof(dd), "/sf/boot_%lu", bootMs / 1000);
    snprintf(ds, sizeof(ds), "boot%lu", bootMs / 1000);
    Serial.printf("[LOG] No GPS date, using boot timestamp: %s\n", dd);
  }

  if (strlen(gps.utc_time) >= 6 && gps.utc_time[0] != '0') {
    snprintf(ts, sizeof(ts), "%c%c%c%c%c%c",
      gps.utc_time[0], gps.utc_time[1], gps.utc_time[2],
      gps.utc_time[3], gps.utc_time[4], gps.utc_time[5]);
  } else {
    snprintf(ts, sizeof(ts), "%06lu", (millis() / 1000) % 1000000);
  }

  // Create directories
  Serial.println("[LOG] Creating /sf directory...");
  if (!SD.mkdir("/sf")) {
    Serial.println("[LOG] /sf mkdir failed (may already exist)");
  }
  Serial.printf("[LOG] Creating %s directory...\n", dd);
  if (!SD.mkdir(dd)) {
    Serial.printf("[LOG] %s mkdir failed (may already exist)\n", dd);
  }

  // Build file paths
  char np[64], ip[64], rp[64];
  snprintf(np, sizeof(np), "%s/%s_%s_%s_nav.csv", dd, config.boat_id, ds, ts);
  snprintf(ip, sizeof(ip), "%s/%s_%s_%s_imu.csv", dd, config.boat_id, ds, ts);
  snprintf(rp, sizeof(rp), "%s/%s_%s_%s.rtcm3", dd, config.boat_id, ds, ts);

  Serial.printf("[LOG] Opening NAV: %s\n", np);
  navFile = SD.open(np, FILE_WRITE);
  Serial.printf("[LOG] NAV file %s\n", navFile ? "OK" : "FAILED");

  Serial.printf("[LOG] Opening IMU: %s\n", ip);
  imuFile = SD.open(ip, FILE_WRITE);
  Serial.printf("[LOG] IMU file %s\n", imuFile ? "OK" : "FAILED");

  Serial.printf("[LOG] Opening RAW: %s\n", rp);
  rawFile = SD.open(rp, FILE_WRITE);
  Serial.printf("[LOG] RAW file %s\n", rawFile ? "OK" : "FAILED");

  if (navFile) {
    logging = true;
    logStart = millis();
    navFile.println("ms,utc,lat,lon,alt,sog,cog,sat,hdop,fix");
    navFile.flush();
    if (imuFile) {
      imuFile.println("ms,ax,ay,az,gx,gy,gz,heel,pitch");
      imuFile.flush();
    }
    Serial.println("[LOG] ========================================");
    Serial.printf("[LOG] NAV: %s\n", np);
    Serial.printf("[LOG] IMU: %s\n", ip);
    Serial.printf("[LOG] RAW: %s\n", rp);
    Serial.println("[LOG] RTCM3 MSM7 raw data -> PPK via RTKLIB");
    Serial.println("[LOG] ========================================");
  } else {
    Serial.println("[LOG] ERROR: Failed to open NAV file!");
    Serial.println("[LOG] Check SD card is properly inserted and formatted FAT32");
  }
}

// ============================================================
// LOG NAV + IMU
// ============================================================
void logNav() {
  if (!navFile || !logging) return;
  unsigned long e = millis() - logStart;
  navFile.printf("%lu,%s,%.10f,%.10f,%.3f,%.3f,%.2f,%d,%.2f,%d\n",
    e, gps.utc_time, gps.lat, gps.lon, gps.alt,
    gps.speed_kts, gps.course, gps.satellites, gps.hdop, gps.fix_quality);
  totalBytes += 80;
}

void logIMU() {
  if (!imuFile || !logging) return;
  unsigned long e = millis() - logStart;
  imuFile.printf("%lu,%.4f,%.4f,%.4f,%.2f,%.2f,%.2f,%.1f,%.1f\n",
    e, imu.accel_x, imu.accel_y, imu.accel_z,
    imu.gyro_x, imu.gyro_y, imu.gyro_z, imu.heel, imu.pitch);
  totalBytes += 100;
}

// ============================================================
// DISPLAY
// ============================================================
void updateDisplay() {
  if (!oledOK) return;

  char buf[32];

  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_6x10_tr);  // 6x10 monospace font

  // Row 0: GPS status (y=10 for first line with this font)
  snprintf(buf, sizeof(buf), "SAT:%d FIX:%s", gps.satellites,
    gps.fix_quality == 0 ? "---" :
    gps.fix_quality == 1 ? "GPS" :
    gps.fix_quality == 2 ? "DGPS" : "RTK");
  u8g2.drawStr(0, 10, buf);

  // Row 1: Speed and course
  snprintf(buf, sizeof(buf), "SOG:%.1fkt COG:%d", gps.speed_kts, (int)gps.course);
  u8g2.drawStr(0, 20, buf);

  // Row 2: Heel and pitch
  snprintf(buf, sizeof(buf), "HEEL:%.0f PITCH:%.0f", imu.heel, imu.pitch);
  u8g2.drawStr(0, 30, buf);

  // Row 3: Recording status
  if (logging) {
    unsigned long secs = (millis() - logStart) / 1000;
    snprintf(buf, sizeof(buf), "REC %02d:%02d %s",
      (int)(secs/60), (int)(secs%60),
      ((millis() / 500) % 2) ? "*" : "");
  } else {
    snprintf(buf, sizeof(buf), "IDLE");
  }
  u8g2.drawStr(0, 40, buf);

  // Row 4: Hardware status
  snprintf(buf, sizeof(buf), "SD:%s IMU:%s",
    sdOK ? "OK" : "--",
    imuOK ? (useIMU_BNO ? "BNO" : "MPU") : "---");
  u8g2.drawStr(0, 50, buf);

  // Row 5: Boat ID and data size
  snprintf(buf, sizeof(buf), "%s %luKB", config.boat_id, totalBytes / 1024);
  u8g2.drawStr(0, 60, buf);

  u8g2.sendBuffer();
}

// ============================================================
// WI-FI UPLOAD TO AWS S3
// ============================================================

// Check if file has been uploaded (marker file exists)
bool isUploaded(const char* filepath) {
  char marker[128];
  snprintf(marker, sizeof(marker), "%s.uploaded", filepath);
  return SD.exists(marker);
}

// Mark file as uploaded
void markUploaded(const char* filepath) {
  char marker[128];
  snprintf(marker, sizeof(marker), "%s.uploaded", filepath);
  File f = SD.open(marker, FILE_WRITE);
  if (f) {
    f.printf("uploaded:%lu\n", millis());
    f.close();
  }
}

// Upload a single file to S3 via HTTP PUT
// Expects upload_url to be an API Gateway endpoint that returns a pre-signed S3 URL
// Or a direct pre-signed S3 PUT URL
bool uploadFile(const char* filepath) {
  File file = SD.open(filepath, FILE_READ);
  if (!file) {
    Serial.printf("[UPLOAD] Cannot open: %s\n", filepath);
    return false;
  }

  size_t fileSize = file.size();
  Serial.printf("[UPLOAD] Uploading %s (%u bytes)...\n", filepath, fileSize);

  HTTPClient http;

  // Build the upload URL with filename as query param
  String url = String(config.upload_url);
  url += "?boat=";
  url += config.boat_id;
  url += "&file=";
  url += filepath;

  http.begin(url);
  http.addHeader("Content-Type", "application/octet-stream");
  http.addHeader("Content-Length", String(fileSize));

  // Stream upload - read in chunks
  WiFiClient* stream = http.getStreamPtr();

  int httpCode = http.sendRequest("PUT", &file, fileSize);

  file.close();
  http.end();

  if (httpCode == 200 || httpCode == 201 || httpCode == 204) {
    Serial.printf("[UPLOAD] Success: %s (HTTP %d)\n", filepath, httpCode);
    return true;
  } else {
    Serial.printf("[UPLOAD] Failed: %s (HTTP %d)\n", filepath, httpCode);
    return false;
  }
}

// Scan directory and upload all un-uploaded files
void uploadDirectory(const char* dirname) {
  File root = SD.open(dirname);
  if (!root || !root.isDirectory()) return;

  File file = root.openNextFile();
  while (file) {
    char filepath[128];
    snprintf(filepath, sizeof(filepath), "%s/%s", dirname, file.name());

    if (file.isDirectory()) {
      // Recurse into subdirectories
      uploadDirectory(filepath);
    } else {
      // Skip marker files and already uploaded files
      String name = String(file.name());
      if (!name.endsWith(".uploaded") && !isUploaded(filepath)) {
        if (uploadFile(filepath)) {
          markUploaded(filepath);
        }
        delay(500);  // Brief pause between uploads
      }
    }
    file = root.openNextFile();
  }
}

// Try to connect to any configured WiFi network
// Returns true if connected
bool connectWiFi() {
  if (config.wifi_count == 0) {
    Serial.println("[WIFI] No networks configured");
    return false;
  }

  // Try each configured network
  for (int i = 0; i < config.wifi_count; i++) {
    if (strlen(config.wifi[i].ssid) == 0) continue;

    Serial.printf("[WIFI] Trying %s (%d/%d)...\n",
      config.wifi[i].ssid, i + 1, config.wifi_count);

    WiFi.begin(config.wifi[i].ssid, config.wifi[i].pass);

    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts++ < 20) {
      delay(500);
      Serial.print(".");
    }
    Serial.println();

    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("[WIFI] Connected to %s! IP: %s\n",
        config.wifi[i].ssid, WiFi.localIP().toString().c_str());
      return true;
    }

    WiFi.disconnect(true);
    delay(100);
  }

  Serial.println("[WIFI] All networks failed");
  return false;
}

// Main upload check - runs when stationary and WiFi configured
void checkWiFiUpload() {
  // Skip if no WiFi or upload URL configured
  if (config.wifi_count == 0 || !strlen(config.upload_url)) return;

  // Only upload when stationary (speed < 0.5 kt for 30 seconds)
  static unsigned long stationaryStart = 0;
  if (gps.speed_kts < 0.5) {
    if (stationaryStart == 0) stationaryStart = millis();
    if (millis() - stationaryStart < 30000) return;  // Wait 30s
  } else {
    stationaryStart = 0;
    return;
  }

  // Try to connect to any available WiFi
  if (connectWiFi()) {
    // Upload all files in /sf directory
    Serial.println("[UPLOAD] Scanning for files to upload...");
    uploadDirectory("/sf");
    Serial.println("[UPLOAD] Done");

    WiFi.disconnect(true);
    Serial.println("[WIFI] Disconnected");
  }

  // Reset stationary timer to avoid rapid retries
  stationaryStart = 0;
}

// ============================================================
// SERIAL COMMANDS
// ============================================================
void listDir(const char* dirname, int depth = 0) {
  File root = SD.open(dirname);
  if (!root || !root.isDirectory()) {
    Serial.printf("Failed to open %s\n", dirname);
    return;
  }

  File file = root.openNextFile();
  while (file) {
    for (int i = 0; i < depth; i++) Serial.print("  ");
    if (file.isDirectory()) {
      Serial.printf("[DIR]  %s/\n", file.name());
      char path[128];
      snprintf(path, sizeof(path), "%s/%s", dirname, file.name());
      listDir(path, depth + 1);
    } else {
      Serial.printf("[FILE] %s (%lu bytes)\n", file.name(), file.size());
    }
    file = root.openNextFile();
  }
}

void handleSerialCommand() {
  if (!Serial.available()) return;

  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  if (cmd.length() == 0) return;

  Serial.printf("\n> %s\n", cmd.c_str());

  if (cmd == "ls" || cmd == "list") {
    if (!sdOK) {
      Serial.println("SD card not available");
      return;
    }
    Serial.println("=== SD Card Contents ===");
    listDir("/");
    Serial.println("========================");

  } else if (cmd == "status") {
    Serial.println("=== Status ===");
    Serial.printf("GPS: %s, SAT:%d, HDOP:%.1f\n",
      gps.valid ? "FIX" : "NO FIX", gps.satellites, gps.hdop);
    Serial.printf("IMU: %s\n", imuOK ? (useIMU_BNO ? "BNO085" : "MPU6050") : "NONE");
    Serial.printf("SD:  %s\n", sdOK ? "OK" : "FAILED");
    Serial.printf("Logging: %s\n", logging ? "YES" : "NO");
    Serial.printf("Data: %lu KB\n", totalBytes / 1024);
    Serial.println("===============");

  } else if (cmd.startsWith("cat ")) {
    String path = cmd.substring(4);
    path.trim();
    if (!sdOK) {
      Serial.println("SD card not available");
      return;
    }
    File f = SD.open(path.c_str());
    if (!f) {
      Serial.printf("Cannot open: %s\n", path.c_str());
      return;
    }
    Serial.printf("=== %s (%lu bytes) ===\n", path.c_str(), f.size());
    int lines = 0;
    while (f.available() && lines < 20) {
      Serial.println(f.readStringUntil('\n'));
      lines++;
    }
    if (f.available()) Serial.println("... (truncated)");
    f.close();

  } else if (cmd == "upload") {
    if (!sdOK) {
      Serial.println("SD card not available");
      return;
    }
    if (config.wifi_count == 0) {
      Serial.println("WiFi not configured in config.txt");
      return;
    }
    Serial.println("Starting manual upload...");
    if (connectWiFi()) {
      uploadDirectory("/sf");
      WiFi.disconnect(true);
      Serial.println("Upload complete, WiFi disconnected");
    }

  } else if (cmd == "help") {
    Serial.println("=== Commands ===");
    Serial.println("  ls, list  - List SD card files");
    Serial.println("  cat <file> - Show file contents (first 20 lines)");
    Serial.println("  status    - Show device status");
    Serial.println("  upload    - Manual upload to AWS S3");
    Serial.println("  help      - Show this help");
    Serial.println("================");

  } else {
    Serial.printf("Unknown command: %s (type 'help')\n", cmd.c_str());
  }
}

// ============================================================
// MAIN LOOP
// ============================================================
void loop() {
  unsigned long now = millis();

  // Check for serial commands
  handleSerialCommand();

  readGPS();

  if (now - lastIMU >= IMU_INTERVAL_MS) {
    readIMU();
    if (logging) logIMU();
    lastIMU = now;
  }

  if (logging && gps.newGGA) {
    logNav();
    gps.newGGA = false;
  }

  if (now - lastDisp >= DISPLAY_UPDATE_MS) {
    updateDisplay();
    if (logging) digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    lastDisp = now;
  }

  if (logging && now - lastFlush >= FLUSH_INTERVAL_MS) {
    navFile.flush();
    if (imuFile) imuFile.flush();
    rawFile.flush();
    lastFlush = now;
  }

  static unsigned long lastWifi = 0;
  if (now - lastWifi >= 60000) { checkWiFiUpload(); lastWifi = now; }
}
