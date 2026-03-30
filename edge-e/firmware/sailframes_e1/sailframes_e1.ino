/*
 * SailFrames E1 — Fleet Tracker Firmware
 * 
 * Hardware:
 *   - ESP32 DevKit V1
 *   - Waveshare LG290P GNSS (UART2: TX=GPIO17, RX=GPIO16)
 *   - MPU-6050 IMU (I2C: 0x68)
 *   - SSD1306 OLED 128x64 (I2C: 0x3C)
 *   - MicroSD card module (SPI: MOSI=23, MISO=19, CLK=18, CS=5)
 *   - 18650 Battery Shield (5V → VIN)
 * 
 * Behavior:
 *   Power on → init all sensors → wait for GPS fix → auto-log to SD
 *   When yacht club Wi-Fi detected → auto-upload to AWS S3
 *   Power off → done
 * 
 * Log files per session:
 *   /sailframes/YYYY-MM-DD/E1_YYYYMMDD_HHMMSS_nav.csv
 *   /sailframes/YYYY-MM-DD/E1_YYYYMMDD_HHMMSS_imu.csv
 *   /sailframes/YYYY-MM-DD/E1_YYYYMMDD_HHMMSS_raw.rtcm
 * 
 * License: Apache 2.0
 * Project: https://github.com/sailframes
 */

#include <Wire.h>
#include <SPI.h>
#include <SD.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// ============================================================
// PIN DEFINITIONS
// ============================================================
#define GPS_RX_PIN    16    // ESP32 RX2 ← LG290P TX
#define GPS_TX_PIN    17    // ESP32 TX2 → LG290P RX
#define SD_CS_PIN     5     // SD card chip select
#define SDA_PIN       21    // I2C data
#define SCL_PIN       22    // I2C clock

// ============================================================
// I2C ADDRESSES
// ============================================================
#define MPU6050_ADDR  0x68
#define OLED_ADDR     0x3C

// ============================================================
// OLED DISPLAY
// ============================================================
#define SCREEN_WIDTH  128
#define SCREEN_HEIGHT 64
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

// ============================================================
// CONFIGURATION — loaded from SD card config.txt
// ============================================================
struct Config {
  char device_id[16]   = "E1";
  char wifi_ssid[64]   = "";
  char wifi_pass[64]   = "";
  char upload_url[256] = "";
  char api_key[128]    = "";
} config;

// ============================================================
// GPS STATE
// ============================================================
struct GPSData {
  float lat        = 0.0;
  float lon        = 0.0;
  float sog_knots  = 0.0;
  float cog        = 0.0;
  float alt        = 0.0;
  int   satellites  = 0;
  int   fix_quality = 0;
  char  utc_time[16] = "";
  char  utc_date[16] = "";
  bool  valid      = false;
} gps;

// ============================================================
// IMU STATE
// ============================================================
struct IMUData {
  float accel_x = 0.0;
  float accel_y = 0.0;
  float accel_z = 0.0;
  float gyro_x  = 0.0;
  float gyro_y  = 0.0;
  float gyro_z  = 0.0;
  float heel    = 0.0;  // roll angle in degrees
  float pitch   = 0.0;  // pitch angle in degrees
} imu;

// ============================================================
// LOGGING STATE
// ============================================================
File navFile;
File imuFile;
File rawFile;
bool sdReady       = false;
bool logging       = false;
bool wifiUploading = false;
unsigned long logStartTime = 0;
unsigned long lastGPSTime  = 0;
unsigned long lastIMUTime  = 0;
unsigned long lastDisplayTime = 0;
unsigned long lastWiFiCheck   = 0;
unsigned long totalBytesLogged = 0;
char sessionDir[64]  = "";
char navFilePath[96] = "";
char imuFilePath[96] = "";
char rawFilePath[96] = "";

// ============================================================
// NMEA PARSING BUFFER
// ============================================================
char nmeaBuffer[256];
int  nmeaIndex = 0;

// ============================================================
// SETUP
// ============================================================
void setup() {
  // Debug serial
  Serial.begin(115200);
  Serial.println();
  Serial.println("=================================");
  Serial.println("  SailFrames E1 — Fleet Tracker");
  Serial.println("=================================");

  // I2C
  Wire.begin(SDA_PIN, SCL_PIN);

  // OLED
  if (display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
    displaySplash();
    Serial.println("[OLED] OK");
  } else {
    Serial.println("[OLED] FAILED");
  }

  // SD Card
displayStatus("SD Card...", "");
Serial.println("[SD] Trying CS pin 5...");
Serial.print("[SD] MOSI=23, MISO=19, CLK=18, CS=5");
Serial.println();

SPI.begin(18, 19, 23, 5);  // CLK, MISO, MOSI, CS

if (SD.begin(SD_CS_PIN)) {
  sdReady = true;
  Serial.println("[SD] OK");
  
  // Test write
  File testFile = SD.open("/test.txt", FILE_WRITE);
  if (testFile) {
    testFile.println("SailFrames E1 test");
    testFile.close();
    Serial.println("[SD] Test file written OK");
  }
} else {
  Serial.println("[SD] FAILED — check wiring");
  Serial.println("[SD] Possible causes:");
  Serial.println("[SD]   - No card inserted");
  Serial.println("[SD]   - Card not FAT32");
  Serial.println("[SD]   - Wiring wrong");
  Serial.println("[SD]   - Card damaged");
}
  delay(500);

  // Load config from SD
  if (sdReady) {
    loadConfig();
  }

  // MPU-6050
  displayStatus("IMU...", "");
  if (initMPU6050()) {
    Serial.println("[IMU] MPU-6050 OK at 0x68");
    displayStatus("IMU...", "OK (0x68)");
  } else {
    Serial.println("[IMU] MPU-6050 FAILED");
    displayStatus("IMU...", "FAILED");
  }
  delay(500);

  // GPS UART
  displayStatus("GPS...", "");
  Serial2.begin(460800, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
  Serial.println("[GPS] UART2 started at 460800 baud");
  displayStatus("GPS...", "SEARCHING");

  // Wait for GPS fix
  waitForFix();

  // Create log files
  if (sdReady && gps.valid) {
    createLogFiles();
    logging = true;
    logStartTime = millis();
    Serial.println("[LOG] Recording started");
  }
}

// ============================================================
// MAIN LOOP
// ============================================================
void loop() {
  unsigned long now = millis();

  // Read GPS data continuously
  readGPS();

  // Read IMU at 20Hz (every 50ms)
  if (now - lastIMUTime >= 50) {
    readMPU6050();
    lastIMUTime = now;

    // Log IMU data
    if (logging && imuFile) {
      logIMU();
    }
  }

  // Log GPS data when new fix available (up to 10Hz)
  if (gps.valid && logging && navFile) {
    if (now - lastGPSTime >= 100) {
      logNav();
      lastGPSTime = now;
    }
  }

  // Update display at 2Hz
  if (now - lastDisplayTime >= 500) {
    updateDisplay();
    lastDisplayTime = now;
  }

  // Check for Wi-Fi every 30 seconds
  if (!wifiUploading && now - lastWiFiCheck >= 30000) {
    checkWiFiUpload();
    lastWiFiCheck = now;
  }
}

// ============================================================
// GPS — Read and parse NMEA sentences
// ============================================================
void readGPS() {
  while (Serial2.available()) {
    char c = Serial2.read();

    // Also write raw data to RTCM file for PPK
    if (logging && rawFile) {
      rawFile.write(c);
      totalBytesLogged++;
    }

    // Parse NMEA
    if (c == '$') {
      nmeaIndex = 0;
    }

    if (nmeaIndex < sizeof(nmeaBuffer) - 1) {
      nmeaBuffer[nmeaIndex++] = c;
    }

    if (c == '\n') {
      nmeaBuffer[nmeaIndex] = '\0';
      parseNMEA(nmeaBuffer);
      nmeaIndex = 0;
    }
  }
}

void parseNMEA(const char* sentence) {
  // Parse GGA — position, fix quality, satellites
  if (strstr(sentence, "GGA")) {
    parseGGA(sentence);
  }
  // Parse RMC — speed, course, date
  else if (strstr(sentence, "RMC")) {
    parseRMC(sentence);
  }
}

void parseGGA(const char* sentence) {
  char fields[15][32];
  int fieldCount = splitNMEA(sentence, fields, 15);

  if (fieldCount < 10) return;

  // UTC time
  strncpy(gps.utc_time, fields[1], sizeof(gps.utc_time) - 1);

  // Latitude
  if (strlen(fields[2]) > 0) {
    float rawLat = atof(fields[2]);
    int degrees = (int)(rawLat / 100);
    float minutes = rawLat - (degrees * 100);
    gps.lat = degrees + (minutes / 60.0);
    if (fields[3][0] == 'S') gps.lat = -gps.lat;
  }

  // Longitude
  if (strlen(fields[4]) > 0) {
    float rawLon = atof(fields[4]);
    int degrees = (int)(rawLon / 100);
    float minutes = rawLon - (degrees * 100);
    gps.lon = degrees + (minutes / 60.0);
    if (fields[5][0] == 'W') gps.lon = -gps.lon;
  }

  // Fix quality (0=invalid, 1=GPS, 2=DGPS, 4=RTK, 5=float RTK)
  gps.fix_quality = atoi(fields[6]);

  // Satellites
  gps.satellites = atoi(fields[7]);

  // Altitude
  if (strlen(fields[9]) > 0) {
    gps.alt = atof(fields[9]);
  }

  gps.valid = (gps.fix_quality >= 1 && gps.satellites >= 4);
}

void parseRMC(const char* sentence) {
  char fields[15][32];
  int fieldCount = splitNMEA(sentence, fields, 15);

  if (fieldCount < 10) return;

  // Speed over ground in knots
  if (strlen(fields[7]) > 0) {
    gps.sog_knots = atof(fields[7]);
  }

  // Course over ground
  if (strlen(fields[8]) > 0) {
    gps.cog = atof(fields[8]);
  }

  // Date (DDMMYY)
  if (strlen(fields[9]) > 0) {
    strncpy(gps.utc_date, fields[9], sizeof(gps.utc_date) - 1);
  }
}

int splitNMEA(const char* sentence, char fields[][32], int maxFields) {
  int fieldIndex = 0;
  int charIndex = 0;

  for (int i = 0; sentence[i] != '\0' && fieldIndex < maxFields; i++) {
    if (sentence[i] == ',' || sentence[i] == '*') {
      fields[fieldIndex][charIndex] = '\0';
      fieldIndex++;
      charIndex = 0;
    } else {
      if (charIndex < 31) {
        fields[fieldIndex][charIndex++] = sentence[i];
      }
    }
  }

  if (charIndex > 0 && fieldIndex < maxFields) {
    fields[fieldIndex][charIndex] = '\0';
    fieldIndex++;
  }

  return fieldIndex;
}

// ============================================================
// GPS — Wait for initial fix
// ============================================================
void waitForFix() {
  Serial.println("[GPS] Waiting for fix...");
  unsigned long startWait = millis();

  while (!gps.valid) {
    readGPS();

    // Update display every second while waiting
    if (millis() - lastDisplayTime >= 1000) {
      display.clearDisplay();
      display.setTextSize(1);
      display.setTextColor(SSD1306_WHITE);
      display.setCursor(0, 0);
      display.println("SailFrames E1");
      display.println();
      display.print("GPS: SEARCHING");
      display.println();
      display.print("SAT: ");
      display.println(gps.satellites);
      display.print("Time: ");
      int elapsed = (millis() - startWait) / 1000;
      display.print(elapsed);
      display.println("s");
      display.display();
      lastDisplayTime = millis();
    }

    // Timeout after 5 minutes — start logging anyway
    if (millis() - startWait > 300000) {
      Serial.println("[GPS] Fix timeout — starting without fix");
      break;
    }

    delay(10);
  }

  if (gps.valid) {
    Serial.print("[GPS] Fix acquired! SAT=");
    Serial.print(gps.satellites);
    Serial.print(" Quality=");
    Serial.println(gps.fix_quality);
  }
}

// ============================================================
// IMU — MPU-6050
// ============================================================
bool initMPU6050() {
  // Check if device responds
  Wire.beginTransmission(MPU6050_ADDR);
  if (Wire.endTransmission() != 0) {
    return false;
  }

  // Wake up MPU-6050 (clear sleep bit)
  Wire.beginTransmission(MPU6050_ADDR);
  Wire.write(0x6B);  // PWR_MGMT_1 register
  Wire.write(0x00);  // Clear sleep bit
  Wire.endTransmission(true);

  // Set accelerometer range to ±4g
  Wire.beginTransmission(MPU6050_ADDR);
  Wire.write(0x1C);  // ACCEL_CONFIG register
  Wire.write(0x08);  // ±4g
  Wire.endTransmission(true);

  // Set gyroscope range to ±500 deg/s
  Wire.beginTransmission(MPU6050_ADDR);
  Wire.write(0x1B);  // GYRO_CONFIG register
  Wire.write(0x08);  // ±500 deg/s
  Wire.endTransmission(true);

  // Set low-pass filter to 20Hz (good for boat motion)
  Wire.beginTransmission(MPU6050_ADDR);
  Wire.write(0x1A);  // CONFIG register
  Wire.write(0x04);  // DLPF 20Hz
  Wire.endTransmission(true);

  delay(100);
  return true;
}

void readMPU6050() {
  Wire.beginTransmission(MPU6050_ADDR);
  Wire.write(0x3B);  // Start at ACCEL_XOUT_H
  Wire.endTransmission(false);
  Wire.requestFrom(MPU6050_ADDR, 14, true);

  // Read raw values (16-bit, big-endian)
  int16_t rawAx = (Wire.read() << 8) | Wire.read();
  int16_t rawAy = (Wire.read() << 8) | Wire.read();
  int16_t rawAz = (Wire.read() << 8) | Wire.read();
  int16_t rawTemp = (Wire.read() << 8) | Wire.read();  // skip temp
  int16_t rawGx = (Wire.read() << 8) | Wire.read();
  int16_t rawGy = (Wire.read() << 8) | Wire.read();
  int16_t rawGz = (Wire.read() << 8) | Wire.read();

  // Convert to physical units (±4g range = 8192 LSB/g)
  imu.accel_x = rawAx / 8192.0;
  imu.accel_y = rawAy / 8192.0;
  imu.accel_z = rawAz / 8192.0;

  // Convert gyro (±500 deg/s = 65.5 LSB/(deg/s))
  imu.gyro_x = rawGx / 65.5;
  imu.gyro_y = rawGy / 65.5;
  imu.gyro_z = rawGz / 65.5;

  // Simple heel/pitch from accelerometer (works in steady state)
  imu.heel  = atan2(imu.accel_y, imu.accel_z) * 180.0 / PI;
  imu.pitch = atan2(-imu.accel_x, sqrt(imu.accel_y * imu.accel_y + imu.accel_z * imu.accel_z)) * 180.0 / PI;
}

// ============================================================
// SD CARD — Config loading
// ============================================================
void loadConfig() {
  File configFile = SD.open("/config.txt");
  if (!configFile) {
    Serial.println("[CFG] No config.txt found — using defaults");
    return;
  }

  Serial.println("[CFG] Loading config.txt");
  while (configFile.available()) {
    String line = configFile.readStringUntil('\n');
    line.trim();

    if (line.startsWith("device_id=")) {
      strncpy(config.device_id, line.substring(10).c_str(), sizeof(config.device_id) - 1);
    } else if (line.startsWith("wifi_ssid=")) {
      strncpy(config.wifi_ssid, line.substring(10).c_str(), sizeof(config.wifi_ssid) - 1);
    } else if (line.startsWith("wifi_pass=")) {
      strncpy(config.wifi_pass, line.substring(10).c_str(), sizeof(config.wifi_pass) - 1);
    } else if (line.startsWith("upload_url=")) {
      strncpy(config.upload_url, line.substring(11).c_str(), sizeof(config.upload_url) - 1);
    } else if (line.startsWith("api_key=")) {
      strncpy(config.api_key, line.substring(8).c_str(), sizeof(config.api_key) - 1);
    }
  }
  configFile.close();

  Serial.print("[CFG] Device ID: ");
  Serial.println(config.device_id);
  Serial.print("[CFG] WiFi SSID: ");
  Serial.println(config.wifi_ssid);
}

// ============================================================
// SD CARD — Create log files
// ============================================================
void createLogFiles() {
  // Build date string from GPS
  char dateStr[16] = "unknown";
  char timeStr[16] = "000000";

  if (strlen(gps.utc_date) >= 6) {
    // GPS date is DDMMYY, convert to YYYY-MM-DD
    char dd[3] = {gps.utc_date[0], gps.utc_date[1], '\0'};
    char mm[3] = {gps.utc_date[2], gps.utc_date[3], '\0'};
    char yy[3] = {gps.utc_date[4], gps.utc_date[5], '\0'};
    snprintf(dateStr, sizeof(dateStr), "20%s-%s-%s", yy, mm, dd);
  }

  if (strlen(gps.utc_time) >= 6) {
    strncpy(timeStr, gps.utc_time, 6);
    timeStr[6] = '\0';
  }

  // Create directory
  snprintf(sessionDir, sizeof(sessionDir), "/sailframes/%s", dateStr);
  SD.mkdir("/sailframes");
  SD.mkdir(sessionDir);

  // Create file paths
  snprintf(navFilePath, sizeof(navFilePath), "%s/%s_%s_%s_nav.csv",
           sessionDir, config.device_id, dateStr, timeStr);
  snprintf(imuFilePath, sizeof(imuFilePath), "%s/%s_%s_%s_imu.csv",
           sessionDir, config.device_id, dateStr, timeStr);
  snprintf(rawFilePath, sizeof(rawFilePath), "%s/%s_%s_%s_raw.bin",
           sessionDir, config.device_id, dateStr, timeStr);

  // Remove dashes from date in filename for compactness
  // Files are already in a dated directory

  // Open nav file and write header
  navFile = SD.open(navFilePath, FILE_WRITE);
  if (navFile) {
    navFile.println("timestamp,lat,lon,sog_kts,cog,alt,satellites,fix_quality");
    Serial.print("[LOG] Nav: ");
    Serial.println(navFilePath);
  }

  // Open IMU file and write header
  imuFile = SD.open(imuFilePath, FILE_WRITE);
  if (imuFile) {
    imuFile.println("timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z,heel,pitch");
    Serial.print("[LOG] IMU: ");
    Serial.println(imuFilePath);
  }

  // Open raw file for RTCM/NMEA data (for PPK processing)
  rawFile = SD.open(rawFilePath, FILE_WRITE);
  if (rawFile) {
    Serial.print("[LOG] Raw: ");
    Serial.println(rawFilePath);
  }
}

// ============================================================
// SD CARD — Log navigation data
// ============================================================
void logNav() {
  if (!navFile) return;

  unsigned long elapsed = millis() - logStartTime;

  navFile.print(elapsed);
  navFile.print(",");
  navFile.print(gps.lat, 8);
  navFile.print(",");
  navFile.print(gps.lon, 8);
  navFile.print(",");
  navFile.print(gps.sog_knots, 2);
  navFile.print(",");
  navFile.print(gps.cog, 1);
  navFile.print(",");
  navFile.print(gps.alt, 1);
  navFile.print(",");
  navFile.print(gps.satellites);
  navFile.print(",");
  navFile.println(gps.fix_quality);

  // Flush every 10 seconds to prevent data loss on power cut
  static unsigned long lastFlush = 0;
  if (elapsed - lastFlush > 10000) {
    navFile.flush();
    imuFile.flush();
    rawFile.flush();
    lastFlush = elapsed;
  }

  totalBytesLogged += 80;  // approximate bytes per nav line
}

// ============================================================
// SD CARD — Log IMU data
// ============================================================
void logIMU() {
  if (!imuFile) return;

  unsigned long elapsed = millis() - logStartTime;

  imuFile.print(elapsed);
  imuFile.print(",");
  imuFile.print(imu.accel_x, 4);
  imuFile.print(",");
  imuFile.print(imu.accel_y, 4);
  imuFile.print(",");
  imuFile.print(imu.accel_z, 4);
  imuFile.print(",");
  imuFile.print(imu.gyro_x, 2);
  imuFile.print(",");
  imuFile.print(imu.gyro_y, 2);
  imuFile.print(",");
  imuFile.print(imu.gyro_z, 2);
  imuFile.print(",");
  imuFile.print(imu.heel, 1);
  imuFile.print(",");
  imuFile.println(imu.pitch, 1);

  totalBytesLogged += 100;  // approximate bytes per IMU line
}

// ============================================================
// OLED DISPLAY
// ============================================================
void displaySplash() {
  display.clearDisplay();
  display.setTextSize(2);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(10, 8);
  display.println("SAIL");
  display.setCursor(10, 28);
  display.println("FRAMES");
  display.setTextSize(1);
  display.setCursor(10, 52);
  display.print("E1 Fleet Tracker");
  display.display();
  delay(1500);
}

void displayStatus(const char* component, const char* status) {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.println("SailFrames E1 Init");
  display.println();
  display.print(component);
  display.print(" ");
  display.println(status);
  display.display();
}

void updateDisplay() {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);

  // Line 1: GPS status
  display.setCursor(0, 0);
  display.print("SAT:");
  display.print(gps.satellites);
  display.print(" FIX:");
  switch (gps.fix_quality) {
    case 0: display.print("NONE"); break;
    case 1: display.print("GPS");  break;
    case 2: display.print("DGPS"); break;
    case 4: display.print("RTK");  break;
    case 5: display.print("FRTK"); break;
    default: display.print(gps.fix_quality); break;
  }

  // Line 2: Speed and heading
  display.setCursor(0, 12);
  display.print("SOG:");
  display.print(gps.sog_knots, 1);
  display.print("kt HDG:");
  display.print((int)gps.cog);

  // Line 3: Heel and pitch
  display.setCursor(0, 24);
  display.print("HEEL:");
  display.print(imu.heel, 0);
  display.print((char)247);  // degree symbol
  display.print(" TRIM:");
  display.print(imu.pitch, 0);
  display.print((char)247);

  // Line 4: Logging status
  display.setCursor(0, 36);
  if (logging) {
    display.print("SD:REC ");
    float mb = totalBytesLogged / (1024.0 * 1024.0);
    display.print(mb, 1);
    display.print("MB");
  } else {
    display.print("SD:---");
  }

  // Line 5: Upload status or position
  display.setCursor(0, 48);
  if (wifiUploading) {
    display.print("WiFi: UPLOADING...");
  } else {
    display.print(gps.lat, 4);
    display.print(",");
    display.print(gps.lon, 4);
  }

  // Runtime in top right
  display.setCursor(100, 0);
  unsigned long runtime = millis() / 1000;
  int hours = runtime / 3600;
  int mins = (runtime % 3600) / 60;
  char rtBuf[8];
  snprintf(rtBuf, sizeof(rtBuf), "%d:%02d", hours, mins);
  display.print(rtBuf);

  display.display();
}

// ============================================================
// WIFI — Auto-upload when yacht club network detected
// ============================================================
void checkWiFiUpload() {
  // Skip if no WiFi configured
  if (strlen(config.wifi_ssid) == 0) return;

  // Skip if already uploading
  if (wifiUploading) return;

  // Try to connect
  Serial.print("[WiFi] Scanning for: ");
  Serial.println(config.wifi_ssid);

  WiFi.mode(WIFI_STA);
  int n = WiFi.scanNetworks(false, false, false, 300);

  bool found = false;
  for (int i = 0; i < n; i++) {
    if (WiFi.SSID(i) == String(config.wifi_ssid)) {
      found = true;
      break;
    }
  }

  WiFi.scanDelete();

  if (!found) {
    WiFi.mode(WIFI_OFF);
    return;
  }

  Serial.println("[WiFi] Network found! Connecting...");
  WiFi.begin(config.wifi_ssid, config.wifi_pass);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 20) {
    delay(500);
    attempts++;
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WiFi] Connection failed");
    WiFi.mode(WIFI_OFF);
    return;
  }

  Serial.print("[WiFi] Connected! IP: ");
  Serial.println(WiFi.localIP());

  // Stop logging, flush and close files
  logging = false;
  if (navFile) { navFile.flush(); navFile.close(); }
  if (imuFile) { imuFile.flush(); imuFile.close(); }
  if (rawFile) { rawFile.flush(); rawFile.close(); }

  // Upload files
  wifiUploading = true;
  uploadFile(navFilePath);
  uploadFile(imuFilePath);
  uploadFile(rawFilePath);
  wifiUploading = false;

  WiFi.disconnect();
  WiFi.mode(WIFI_OFF);
  Serial.println("[WiFi] Upload complete, WiFi off");

  // Show completion on display
  display.clearDisplay();
  display.setTextSize(2);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(10, 20);
  display.println("UPLOAD");
  display.println("  DONE!");
  display.display();
}

void uploadFile(const char* filePath) {
  if (strlen(config.upload_url) == 0) {
    Serial.println("[Upload] No upload URL configured");
    return;
  }

  File file = SD.open(filePath);
  if (!file) {
    Serial.print("[Upload] Cannot open: ");
    Serial.println(filePath);
    return;
  }

  Serial.print("[Upload] Uploading: ");
  Serial.print(filePath);
  Serial.print(" (");
  Serial.print(file.size());
  Serial.println(" bytes)");

  HTTPClient http;
  String url = String(config.upload_url) + filePath;
  http.begin(url);
  http.addHeader("Content-Type", "application/octet-stream");
  if (strlen(config.api_key) > 0) {
    http.addHeader("x-api-key", config.api_key);
  }

  int httpCode = http.sendRequest("PUT", &file, file.size());

  if (httpCode == 200 || httpCode == 201) {
    Serial.println("[Upload] Success!");
  } else {
    Serial.print("[Upload] Failed, HTTP ");
    Serial.println(httpCode);
  }

  http.end();
  file.close();
}

// ============================================================
// LG290P — Configure for SBAS and raw data output
// ============================================================
void configureLG290P() {
  // Enable SBAS (WAAS for North America)
  Serial2.println("$PQTMCFGSBAS,W,1,Auto*");
  delay(200);

  // Save configuration
  Serial2.println("$PQTMSAVEPAR*5A");
  delay(200);

  Serial.println("[GPS] LG290P configured: SBAS Auto");
}
