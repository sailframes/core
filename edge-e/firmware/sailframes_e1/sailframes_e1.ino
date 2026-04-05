/*
 * SailFrames E1 — Fleet Tracker Firmware v2.1
 *
 * Hardware:
 *   - ESP32 DevKit V1 (ELEGOO)
 *   - Waveshare LG290P GNSS (UART2: RX=GPIO16, TX=GPIO17, 460800 baud)
 *   - BNO085 IMU (I2C: 0x4A) — heel, pitch, heading
 *   - SSD1309 OLED 2.42" 128x64 (I2C: 0x3C)
 *   - MicroSD card module (SPI: MOSI=23, MISO=19, CLK=18, CS=5)
 *   - Calypso Mini wind sensor (BLE) — apparent wind speed/direction
 *   - 18650 Battery Shield (5V → VIN)
 *
 * Behavior:
 *   Power on → init sensors → configure LG290P for raw RTCM3 output
 *   → scan for Calypso wind sensor (BLE) → wait for GPS fix
 *   → auto-log to SD (NMEA CSV + IMU CSV + Wind CSV + RTCM3 binary)
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
 *   /sf/YYYYMMDD/E1_YYYYMMDD_HHMMSS_wind.csv
 *   /sf/YYYYMMDD/E1_YYYYMMDD_HHMMSS_raw.rtcm3
 *
 * License: Apache 2.0
 * Project: https://github.com/sailframes
 */

#include <Wire.h>
#include <SPI.h>
#include <SD.h>
#include <WiFi.h>
#include <WiFiClient.h>
#include <WiFiClientSecure.h>
#include <ArduinoOTA.h>
#include <HTTPClient.h>
#include <U8g2lib.h>
#include <Adafruit_BNO08x.h>
// NimBLE configuration - disable unused features to reduce size
#define CONFIG_BT_NIMBLE_ROLE_CENTRAL 1
#define CONFIG_BT_NIMBLE_ROLE_PERIPHERAL 0
#define CONFIG_BT_NIMBLE_ROLE_OBSERVER 1
#define CONFIG_BT_NIMBLE_ROLE_BROADCASTER 0
#define CONFIG_BT_NIMBLE_MAX_CONNECTIONS 1
#define CONFIG_BT_NIMBLE_MAX_BONDS 1
#define CONFIG_BT_NIMBLE_SVC_GAP_DEVICE_NAME "SailFrames-E1"
#include <NimBLEDevice.h>
#include <string>

// ============================================================
// PIN DEFINITIONS
// ============================================================
#define GPS_RX_PIN    16
#define GPS_TX_PIN    17
#define SD_CS_PIN     5
#define SDA_PIN       21
#define SCL_PIN       22
#define LED_PIN       2   // Built-in LED blinks during logging

// Battery monitoring (PowerBoost 1000C)
#define BATT_VOLTAGE_PIN  34   // ADC pin for voltage divider
#define LOW_BATT_PIN      35   // LBO pin from PowerBoost (LOW = critical)

// Power control: Use hardware slide switch on PowerBoost 1000C EN pin
// No software deep sleep - hardware switch cuts all power when OFF

// ============================================================
// CONFIGURATION
// ============================================================
#define GPS_BAUD      460800  // LG290P configured rate
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

// Wind sensor (Calypso Mini BLE)
#define ENABLE_WIND         true
#define WIND_SCAN_TIMEOUT_MS    10000
#define WIND_RECONNECT_MS       30000
#define WIND_INTERVAL_MS        1000   // Log at 1Hz

// Calypso BLE UUIDs (Environmental Sensing Service 0x181A)
static NimBLEUUID WIND_SERVICE_UUID("181A");
static NimBLEUUID WIND_SPEED_CHAR_UUID("2A72");      // Apparent Wind Speed (uint16, m/s * 100)
static NimBLEUUID WIND_DIR_CHAR_UUID("2A73");        // Apparent Wind Direction (uint16, degrees * 100)
static NimBLEUUID BATTERY_SERVICE_UUID("180F");
static NimBLEUUID BATTERY_CHAR_UUID("2A19");         // Battery Level (uint8, 0-100%)
// ============================================================
// NMEA CHECKSUM + PQTM SENDER
// ============================================================
void sendPQTM(const char* body) {
  uint8_t cs = 0;
  for (int i = 0; body[i] != '\0'; i++) cs ^= body[i];
  char buf[128];
  snprintf(buf, sizeof(buf), "$%s*%02X\r\n", body, cs);
  Serial2.print(buf);
  Serial.printf("[CMD] %s", buf);

  // Wait for and print response
  delay(150);
  char resp[256];
  int idx = 0;
  unsigned long start = millis();
  while (millis() - start < 200 && idx < (int)sizeof(resp) - 1) {
    if (Serial2.available()) {
      char c = Serial2.read();
      if (c == '\n' || c == '\r') {
        if (idx > 0) break;  // End of response line
      } else {
        resp[idx++] = c;
      }
    }
  }
  resp[idx] = '\0';
  if (idx > 0) {
    Serial.printf("[RSP] %s\n", resp);
  }
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
  float heel = 0, pitch = 0, heading = 0;
} imu;

struct WindData {
  float speed_kts = 0;      // Apparent wind speed in knots
  float speed_mps = 0;      // Apparent wind speed in m/s
  int angle_deg = 0;        // Apparent wind angle (0-360)
  int battery = -1;         // Battery level (0-100, -1 = unknown)
  bool connected = false;
  bool newData = false;
  unsigned long lastUpdate = 0;
  char deviceName[32] = "";
  char deviceAddr[20] = "";
} wind;

struct BatteryData {
  float voltage = 0;        // Battery voltage (3.0-4.2V for LiPo)
  int percent = 0;          // Estimated percentage (0-100)
  bool critical = false;    // LBO pin triggered (< 3.2V)
  bool valid = false;       // Have we read the battery yet?
  unsigned long lastRead = 0;
} battery;

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
  char upload_url[256] = "https://p9s9eia0t6.execute-api.us-east-1.amazonaws.com/prod/upload";
  char boat_id[16] = "E1";
  int gps_rate_hz = 10;
  char wind_mac[20] = "C3:09:6D:1E:8A:FC";  // Calypso Mini MAC (can override in config.txt)
  bool wind_enabled = true;
  // Recording thresholds
  float start_speed_knots = 1.5;
  float stop_speed_knots = 0.5;
  int start_delay_sec = 10;
  int stop_delay_sec = 180;
} config;

// ============================================================
// GLOBALS
// ============================================================
// U8g2 for SSD1309 128x64 I2C - native support, no scrolling issues
U8G2_SSD1309_128X64_NONAME0_F_HW_I2C u8g2(U8G2_R0, /* reset=*/ U8X8_PIN_NONE);
Adafruit_BNO08x bno08x(-1);  // No reset pin
sh2_SensorValue_t sensorValue;
File navFile, imuFile, rawFile, windFile;
bool sdOK = false, imuOK = false, oledOK = false, logging = false;
bool useIMU_BNO = false;
bool uploading = false;
bool wifiConnected = false;
bool otaInProgress = false;
char connectedSSID[64] = "";
int uploadCount = 0, uploadTotal = 0;
int satsInView = 0;
// GSV constellation counts (satellites in view per system)
int gsvGP = 0, gsvGL = 0, gsvGA = 0, gsvGB = 0, gsvGQ = 0, gsvGI = 0;
unsigned long lastValidGPS = 0;  // Track when we last had a valid fix

// IMU calibration offsets (stored on SD card)
float imuHeelOffset = 0.0;
float imuPitchOffset = 0.0;

// Telnet server for remote console
WiFiServer telnetServer(23);
WiFiClient telnetClient;
String telnetBuffer = "";
unsigned long logStart = 0, lastDisp = 0, lastFlush = 0, lastIMU = 0, lastWind = 0;
unsigned long lastWindScan = 0;

// BLE client for Calypso wind sensor
NimBLEClient* pWindClient = nullptr;
NimBLERemoteCharacteristic* pWindSpeedChar = nullptr;
NimBLERemoteCharacteristic* pWindDirChar = nullptr;
NimBLERemoteCharacteristic* pBatteryChar = nullptr;
bool windScanning = false;
bool windOK = false;
bool bleInitialized = false;  // Track BLE init state for safe deinit
unsigned long totalBytes = 0;
unsigned long rtcmFrameCount = 0;  // Count RTCM3 frames for debugging
unsigned long rtcmSyncCount = 0;   // Count 0xD3 sync bytes seen (debug)
char nmeaBuf[256];
int nmeaIdx = 0;

// ============================================================
// GPS SPEED-TRIGGERED RECORDING
// ============================================================
enum RecordState { REC_IDLE, REC_ARMED, REC_RECORDING, REC_STOPPING };
RecordState recState = REC_IDLE;
unsigned long armStartTime = 0;      // when speed first exceeded start threshold
unsigned long stopStartTime = 0;     // when speed first dropped below stop threshold
int sessionCount = 0;                // increments each recording session

// Recording thresholds (configurable via config.txt)
float startSpeedKnots = 1.5;         // Start recording above this speed
float stopSpeedKnots = 0.5;          // Stop recording below this speed
unsigned long startDelayMs = 10000;  // 10 seconds sustained before start
unsigned long stopDelayMs = 180000;  // 3 minutes sustained before stop

// Dual-core upload
volatile bool triggerUpload = false;
SemaphoreHandle_t sdMutex = NULL;
TaskHandle_t uploadTaskHandle = NULL;

// ============================================================
// WIND SENSOR (CALYPSO BLE)
// ============================================================
#if ENABLE_WIND

// BLE notification callback for wind speed
void windSpeedNotifyCallback(NimBLERemoteCharacteristic* pChar, uint8_t* pData, size_t length, bool isNotify) {
  if (length >= 2) {
    uint16_t raw = pData[0] | (pData[1] << 8);
    wind.speed_mps = raw / 100.0;
    wind.speed_kts = wind.speed_mps * 1.94384;
    wind.newData = true;
    wind.lastUpdate = millis();
  }
}

// BLE notification callback for wind direction
void windDirNotifyCallback(NimBLERemoteCharacteristic* pChar, uint8_t* pData, size_t length, bool isNotify) {
  if (length >= 2) {
    uint16_t raw = pData[0] | (pData[1] << 8);
    wind.angle_deg = raw / 100;  // 0.01 degree resolution
    wind.newData = true;
    wind.lastUpdate = millis();
  }
}

// BLE notification callback for battery
void batteryNotifyCallback(NimBLERemoteCharacteristic* pChar, uint8_t* pData, size_t length, bool isNotify) {
  if (length >= 1) {
    wind.battery = pData[0];
  }
}

// BLE client callbacks
class WindClientCallbacks : public NimBLEClientCallbacks {
  void onConnect(NimBLEClient* pClient) {
    Serial.println("[WIND] BLE connected");
    wind.connected = true;
  }

  void onDisconnect(NimBLEClient* pClient) {
    Serial.println("[WIND] BLE disconnected");
    wind.connected = false;
    windOK = false;
    pWindSpeedChar = nullptr;
    pWindDirChar = nullptr;
    pBatteryChar = nullptr;
  }
};

static WindClientCallbacks windClientCallbacks;

// Save discovered MAC to config for faster reconnection
void saveWindMAC(const char* mac) {
  strncpy(config.wind_mac, mac, sizeof(config.wind_mac) - 1);

  // Save to SD card
  File f = SD.open("/wind_mac.txt", FILE_WRITE);
  if (f) {
    f.println(mac);
    f.close();
    Serial.printf("[WIND] Saved MAC %s for auto-reconnect\n", mac);
  }
}

// Load saved wind MAC from SD
void loadWindMAC() {
  File f = SD.open("/wind_mac.txt", FILE_READ);
  if (f) {
    String mac = f.readStringUntil('\n');
    mac.trim();
    if (mac.length() > 0) {
      mac.toCharArray(config.wind_mac, sizeof(config.wind_mac));
      Serial.printf("[WIND] Loaded saved MAC: %s\n", config.wind_mac);
    }
    f.close();
  }
}

// Scan for Calypso wind sensor
bool scanForCalypso() {
  Serial.println("[WIND] Scanning for Calypso...");
  windScanning = true;

  NimBLEScan* pScan = NimBLEDevice::getScan();
  pScan->setActiveScan(true);
  pScan->setInterval(100);
  pScan->setWindow(99);
  pScan->clearResults();

  // NimBLE 2.x: start() is non-blocking, must wait for completion
  if (!pScan->start(WIND_SCAN_TIMEOUT_MS / 1000, false)) {
    Serial.println("[WIND] Scan failed to start");
    windScanning = false;
    return false;
  }

  // Wait for scan to complete
  unsigned long scanStart = millis();
  while (pScan->isScanning() && millis() - scanStart < WIND_SCAN_TIMEOUT_MS + 2000) {
    delay(100);
  }

  NimBLEScanResults results = pScan->getResults();
  Serial.printf("[WIND] Scan found %d devices\n", results.getCount());

  for (int i = 0; i < results.getCount(); i++) {
    const NimBLEAdvertisedDevice* pDevice = results.getDevice(i);
    if (!pDevice) continue;

    String name = pDevice->getName().c_str();
    String nameLower = name;
    nameLower.toLowerCase();

    Serial.printf("[WIND]   %d: \"%s\" @ %s\n", i+1, name.c_str(),
      pDevice->getAddress().toString().c_str());

    // Look for Calypso devices
    if (nameLower.indexOf("calypso") >= 0 || nameLower.indexOf("ultrasonic") >= 0) {
      Serial.printf("[WIND] Found Calypso: %s at %s\n",
        pDevice->getName().c_str(), pDevice->getAddress().toString().c_str());

      strncpy(wind.deviceName, pDevice->getName().c_str(), sizeof(wind.deviceName) - 1);
      strncpy(wind.deviceAddr, pDevice->getAddress().toString().c_str(), sizeof(wind.deviceAddr) - 1);

      // Save MAC for faster reconnection
      saveWindMAC(wind.deviceAddr);

      windScanning = false;
      pScan->clearResults();
      return true;
    }
  }

  Serial.println("[WIND] Calypso not found");
  windScanning = false;
  pScan->clearResults();
  return false;
}

// Connect to Calypso wind sensor
bool connectToCalypso() {
  // Use saved MAC if available
  const char* targetAddr = strlen(config.wind_mac) > 0 ? config.wind_mac : wind.deviceAddr;

  if (strlen(targetAddr) == 0) {
    // Need to scan first
    if (!scanForCalypso()) {
      return false;
    }
    targetAddr = wind.deviceAddr;
  }

  Serial.printf("[WIND] Connecting to %s...\n", targetAddr);

  if (pWindClient == nullptr) {
    pWindClient = NimBLEDevice::createClient();
    pWindClient->setClientCallbacks(&windClientCallbacks);
  }

  // NimBLE 2.x: NimBLEAddress requires std::string and address type
  // Address type 1 = random (most BLE devices use random addresses)
  NimBLEAddress addr(std::string(targetAddr), 1);
  if (!pWindClient->connect(addr)) {
    Serial.println("[WIND] Connection failed");
    // Clear saved MAC if connection failed - might be wrong device
    if (strlen(config.wind_mac) > 0) {
      Serial.println("[WIND] Clearing saved MAC, will scan next time");
      config.wind_mac[0] = '\0';
    }
    return false;
  }

  // Get Wind Service
  NimBLERemoteService* pWindService = pWindClient->getService(WIND_SERVICE_UUID);
  if (pWindService == nullptr) {
    Serial.println("[WIND] Wind service not found");
    pWindClient->disconnect();
    return false;
  }

  // Subscribe to wind speed notifications
  pWindSpeedChar = pWindService->getCharacteristic(WIND_SPEED_CHAR_UUID);
  if (pWindSpeedChar && pWindSpeedChar->canNotify()) {
    pWindSpeedChar->subscribe(true, windSpeedNotifyCallback);
    Serial.println("[WIND] Subscribed to wind speed");
  }

  // Subscribe to wind direction notifications
  pWindDirChar = pWindService->getCharacteristic(WIND_DIR_CHAR_UUID);
  if (pWindDirChar && pWindDirChar->canNotify()) {
    pWindDirChar->subscribe(true, windDirNotifyCallback);
    Serial.println("[WIND] Subscribed to wind direction");
  }

  // Try to get battery service
  NimBLERemoteService* pBattService = pWindClient->getService(BATTERY_SERVICE_UUID);
  if (pBattService) {
    pBatteryChar = pBattService->getCharacteristic(BATTERY_CHAR_UUID);
    if (pBatteryChar) {
      if (pBatteryChar->canNotify()) {
        pBatteryChar->subscribe(true, batteryNotifyCallback);
      } else if (pBatteryChar->canRead()) {
        uint8_t batt = pBatteryChar->readValue<uint8_t>();
        wind.battery = batt;
        Serial.printf("[WIND] Battery: %d%%\n", wind.battery);
      }
    }
  }

  wind.connected = true;
  windOK = true;
  Serial.println("[WIND] Connected and streaming");
  return true;
}

// Initialize BLE for wind sensor
void initWindSensor() {
  if (!config.wind_enabled) {
    Serial.println("[WIND] Disabled in config");
    return;
  }

  Serial.println("[WIND] Initializing BLE...");
  NimBLEDevice::init("SailFrames-E1");
  bleInitialized = true;
  NimBLEDevice::setPower(ESP_PWR_LVL_P9);  // Max power for range

  // Load saved MAC from SD
  if (sdOK) {
    loadWindMAC();
  }

  // Try to connect
  connectToCalypso();
}

// Check wind connection and reconnect if needed
void checkWindConnection() {
  if (!config.wind_enabled) return;

  unsigned long now = millis();

  // If connected, nothing to do
  if (wind.connected && windOK) {
    return;
  }

  // Throttle reconnection attempts
  if (now - lastWindScan < WIND_RECONNECT_MS) {
    return;
  }
  lastWindScan = now;

  Serial.println("[WIND] Attempting reconnect...");
  connectToCalypso();
}

// Log wind data to CSV
void logWind() {
  if (!windFile || !logging || !wind.newData) return;

  unsigned long e = millis() - logStart;
  windFile.printf("%lu,%s,%.2f,%.2f,%d,%d\n",
    e, gps.utc_time, wind.speed_kts, wind.speed_mps, wind.angle_deg, wind.battery);
  totalBytes += 60;
  wind.newData = false;
}

#endif // ENABLE_WIND

// ============================================================
// SETUP
// ============================================================
void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(500);
  Serial.println("\n=================================");
  Serial.println("  SailFrames E1 v2.1 — PPK Logger");
  Serial.println("  Hardware Power Switch Edition");
  Serial.println("=================================");

  // Create SD mutex for dual-core safety
  sdMutex = xSemaphoreCreateMutex();
  if (sdMutex == NULL) {
    Serial.println("[ERR] Failed to create SD mutex!");
  }

  // Initialize WiFi FIRST before any peripherals touch GPIO2
  // This must happen before Wire, SPI, or any other peripheral init
  pinMode(2, INPUT);  // Release GPIO2
  WiFi.mode(WIFI_STA);
  WiFi.disconnect(true);
  delay(100);
  Serial.println("[WIFI] PHY initialized early");

  if (LED_PIN >= 0) pinMode(LED_PIN, OUTPUT);

  // Battery monitoring (PowerBoost 1000C)
  setupBattery();
  updateBattery();  // Initial reading
  Serial.printf("[BATT] Initial: %.2fV (%d%%)\n", battery.voltage, battery.percent);
  if (battery.critical) {
    Serial.println("[BATT] WARNING: Battery critical on startup!");
  }

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
      loadIMUCalibration();
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
    Serial.println("[IMU] BNO085 detected, enabling reports");
    // Enable Game Rotation Vector for heel/pitch (no magnetometer drift)
    if (!bno08x.enableReport(SH2_GAME_ROTATION_VECTOR, IMU_INTERVAL_MS * 1000)) {
      Serial.println("[IMU] WARNING: Failed to enable game rotation vector");
    }
    // Enable Rotation Vector for heading (includes magnetometer)
    if (!bno08x.enableReport(SH2_ROTATION_VECTOR, IMU_INTERVAL_MS * 1000)) {
      Serial.println("[IMU] WARNING: Failed to enable rotation vector");
    }
    // Also enable accelerometer for raw data
    if (!bno08x.enableReport(SH2_ACCELEROMETER, IMU_INTERVAL_MS * 1000)) {
      Serial.println("[IMU] WARNING: Failed to enable accelerometer");
    }
    Serial.println("[IMU] BNO085 OK");
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
  Serial.printf("[GPS] UART2 at %d baud (RX=GPIO%d, TX=GPIO%d)\n", GPS_BAUD, GPS_RX_PIN, GPS_TX_PIN);

  // Diagnostic: check for incoming data
  Serial.println("[GPS] Checking for data (2 sec)...");
  delay(100);
  unsigned long testStart = millis();
  int byteCount = 0;
  while (millis() - testStart < 2000) {
    while (Serial2.available()) {
      char c = Serial2.read();
      byteCount++;
      if (byteCount <= 100) Serial.print(c);  // Print first 100 chars
    }
    delay(1);
  }
  Serial.printf("\n[GPS] Received %d bytes in 2 sec\n", byteCount);
  if (byteCount == 0) {
    Serial.println("[GPS] WARNING: No data received! Check:");
    Serial.println("[GPS]   - Wiring: GPS TXD3 -> ESP32 GPIO16");
    Serial.println("[GPS]   - Baud rate: try 115200 or 460800");
    Serial.println("[GPS]   - GPS power and antenna");
  }

  delay(500);
  configureLG290P();

  // Don't block waiting for GPS fix - let main loop handle it
  // This allows WiFi/telnet access while GPS is searching
  Serial.println("[GPS] Will acquire fix in background...");

  // Initialize wind sensor (Calypso BLE)
#if ENABLE_WIND
  initWindSensor();
#endif

  // Connect to WiFi EARLY (for OTA and telnet access during GPS search)
  if (config.wifi_count > 0) {
    Serial.println("[WIFI] Connecting at boot...");
    if (oledOK) {
      u8g2.clearBuffer();
      u8g2.setFont(u8g2_font_6x10_tr);
      u8g2.drawStr(0, 20, "Connecting WiFi...");
      u8g2.sendBuffer();
    }
    connectWiFi();
  }

  // Apply recording thresholds from config
  startSpeedKnots = config.start_speed_knots;
  stopSpeedKnots = config.stop_speed_knots;
  startDelayMs = config.start_delay_sec * 1000UL;
  stopDelayMs = config.stop_delay_sec * 1000UL;
  Serial.printf("[REC] Thresholds: start>%.1f kt (%ds), stop<%.1f kt (%ds)\n",
    startSpeedKnots, config.start_delay_sec, stopSpeedKnots, config.stop_delay_sec);

  // DON'T start logging immediately - GPS speed state machine controls this
  // Recording will auto-start when GPS speed > threshold
  recState = REC_IDLE;
  Serial.println("[REC] Auto-recording enabled - waiting for GPS speed trigger");

  // Create upload task on Core 0 (sensor reading stays on Core 1)
  xTaskCreatePinnedToCore(
    uploadTaskFunc,     // Function
    "uploadTask",       // Name
    8192,               // Stack size
    NULL,               // Parameters
    1,                  // Priority
    &uploadTaskHandle,  // Handle
    0                   // Core 0
  );

  Serial.println("[SETUP] Complete - WiFi/telnet available, GPS acquiring in background");
  Serial.println("[SETUP] Press and hold button >2s to shutdown");
}

// ============================================================
// CONFIGURE LG290P FOR PPK
// ============================================================
// RTCM3 MSM7 + ephemeris configuration is done via QGNSS and saved to NVM.
// See LG290P_CONFIG_UPDATES.md for QGNSS configuration steps.
// This function only configures NMEA messages and fix rate at runtime.
//
// Saved NVM config (via QGNSS):
//   - MSM7: 1077 (GPS), 1087 (GLO), 1097 (GAL), 1127 (BDS)
//   - Ephemeris: 1019, 1020, 1042, 1046
//   - Station ref: 1006 (every 10 epochs)
// ============================================================
void configureLG290P() {
  Serial.println("[GPS] Configuring LG290P...");

  // NMEA messages (essential for position data parsing)
  // Syntax: PQTMCFGMSGRATE,W,<msg>,<rate> — NO offset parameter on AANR01A06S
  sendPQTM("PQTMCFGMSGRATE,W,GGA,1");   // Position + fix quality
  sendPQTM("PQTMCFGMSGRATE,W,RMC,1");   // Speed + course + date
  sendPQTM("PQTMCFGMSGRATE,W,GSA,1");   // Satellites used
  sendPQTM("PQTMCFGMSGRATE,W,GSV,1");   // Satellites in view

  // RTCM3 MSM7 configuration for PPK
  // Use PQTMCFGRTCM to enable MSM7 globally (not PQTMCFGMSGRATE for individual messages)
  // Parameters: MSMType=7, Reserved=0, ElevMask=-90, GNSSMask=07, SigMask=06, Smoothing=1, Reserved=0
  Serial.println("[GPS] Enabling RTCM3 MSM7...");
  sendPQTM("PQTMCFGRTCM,W,7,0,-90,07,06,1,0");

  // Ephemeris messages — use rate only, NO offset parameter
  Serial.println("[GPS] Enabling ephemeris messages...");
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1019,1");  // GPS ephemeris
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1020,1");  // GLONASS ephemeris
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1042,1");  // BeiDou ephemeris
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1046,1");  // Galileo ephemeris

  // Station reference position every 10 epochs
  sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1006,10");

  // Fix rate (ms per fix)
  char cmd[64];
  snprintf(cmd, sizeof(cmd), "PQTMCFGFIXRATE,W,%d", 1000 / config.gps_rate_hz);
  sendPQTM(cmd);

  // Save configuration to NVM
  Serial.println("[GPS] Saving to NVM...");
  sendPQTM("PQTMSAVEPAR");
  delay(500);

  // Hot restart to apply all changes
  Serial.println("[GPS] Restarting module...");
  sendPQTM("PQTMHOT");
  delay(2000);
  while (Serial2.available()) Serial2.read();

  Serial.println("[GPS] Configuration complete:");
  Serial.println("[GPS]   NMEA: GGA, RMC, GSA, GSV");
  Serial.println("[GPS]   RTCM3: MSM7 (1077/1087/1097/1127)");
  Serial.println("[GPS]   Ephemeris: 1019, 1020, 1042, 1046");
  Serial.println("[GPS]   Station ref: 1006 (every 10 epochs)");
  Serial.printf("[GPS]   Rate: %d Hz\n", config.gps_rate_hz);
}

// ============================================================
// READ GPS — NMEA text + RTCM3 binary
// ============================================================
void readGPS() {
  while (Serial2.available()) {
    uint8_t c = Serial2.read();

    // RTCM3 sync - count all 0xD3 bytes for debugging
    if (c == 0xD3) {
      rtcmSyncCount++;
      if (rtcm.state == RTCM3Parser::WAIT_SYNC) {
        rtcm.state = RTCM3Parser::READ_HEADER;
        rtcm.header[0] = c;
        rtcm.headerIdx = 1;
        continue;
      }
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
        rtcmFrameCount++;
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
      int fq = atoi(f);
      // Validate: 0=none, 1=GPS, 2=DGPS, 4=RTK, 5=RTK float
      if (fq >= 0 && fq <= 5) {
        gps.fix_quality = fq;
        gps.valid = fq > 0;
        if (gps.valid) lastValidGPS = millis();
      }
    }
    if (getField(s, 7, f, sizeof(f))) {
      int sats = atoi(f);
      if (sats >= 0 && sats <= 50) gps.satellites = sats;
    }
    if (getField(s, 8, f, sizeof(f))) {
      float hdop = atof(f);
      if (hdop > 0.1 && hdop < 50) gps.hdop = hdop;  // HDOP is never 0 or near-zero
    }
    if (getField(s, 9, f, sizeof(f))) gps.alt = atof(f);
    gps.newGGA = true;
  } else if (strstr(s, "RMC")) {
    char f[32];
    if (getField(s, 7, f, sizeof(f))) {
      float spd = atof(f);
      if (spd >= 0 && spd < 100) gps.speed_kts = spd;  // Reject impossible speeds (>100kt)
    }
    if (getField(s, 8, f, sizeof(f))) {
      float crs = atof(f);
      if (crs >= 0 && crs <= 360) gps.course = crs;  // Reject invalid course
    }
    if (getField(s, 9, f, sizeof(f))) strncpy(gps.date, f, sizeof(gps.date) - 1);
  } else if (strstr(s, "GSV")) {
    // GSV sentences: GPGSV (GPS), GLGSV (GLONASS), GAGSV (Galileo), GBGSV (BeiDou), GNGSV (combined)
    // Field 1 = total messages, Field 2 = message number, Field 3 = total sats in view
    // Only parse if sentence looks valid (starts with $G and has reasonable length)
    if (s[0] == '$' && s[1] == 'G' && strlen(s) > 20) {
      char f[32];
      if (getField(s, 2, f, sizeof(f))) {
        int msgNum = atoi(f);
        if (msgNum == 1) {  // First message in sequence has total count
          if (getField(s, 3, f, sizeof(f))) {
            int count = atoi(f);
            // Sanity check: count should be 0-50
            if (count >= 0 && count <= 50) {
              // Track each constellation separately (global vars for status display)
              if (strstr(s, "GPGSV")) {
                gsvGP = count;
              } else if (strstr(s, "GLGSV")) {
                gsvGL = count;
              } else if (strstr(s, "GAGSV")) {
                gsvGA = count;
              } else if (strstr(s, "GBGSV")) {
                gsvGB = count;
              } else if (strstr(s, "GQGSV")) {
                gsvGQ = count;  // QZSS
              } else if (strstr(s, "GIGSV")) {
                gsvGI = count;  // NavIC
              }

              // Sum all constellations
              satsInView = gsvGP + gsvGL + gsvGA + gsvGB + gsvGQ + gsvGI;
            }
          }
        }
      }
    }
  }
}

// ============================================================
// BATTERY MONITORING (PowerBoost 1000C)
// ============================================================
// Voltage divider: 2x 200Ω resistors, ratio = 2.0
// TODO: Replace with 100kΩ resistors to reduce idle current
const float BATT_DIVIDER_RATIO = 2.0;

void setupBattery() {
  // GPIO35 is input-only, no internal pull-up available
  // PowerBoost LBO has external pull-up on the board
  pinMode(LOW_BATT_PIN, INPUT);
  analogReadResolution(12);  // 12-bit ADC (0-4095)
  analogSetAttenuation(ADC_11db);  // Full 0-3.3V range
  Serial.println("[BATT] Battery monitoring initialized");
}

float readBatteryVoltage() {
  // Average multiple readings to reduce noise
  int sum = 0;
  for (int i = 0; i < 10; i++) {
    sum += analogRead(BATT_VOLTAGE_PIN);
    delayMicroseconds(100);
  }
  float raw = sum / 10.0;
  float adcVoltage = (raw / 4095.0) * 3.3;  // Voltage at ADC pin
  float voltage = adcVoltage * BATT_DIVIDER_RATIO;  // Actual battery voltage

  // Debug: print every 10 seconds (controlled by caller)
  static unsigned long lastDebug = 0;
  if (millis() - lastDebug >= 10000) {
    Serial.printf("[BATT] ADC raw=%.0f, ADC voltage=%.2fV, Battery=%.2fV (%d%%)\n",
                  raw, adcVoltage, voltage, getBatteryPercent(voltage));
    lastDebug = millis();
  }
  return voltage;
}

int getBatteryPercent(float voltage) {
  // LiPo discharge curve (approximate linear mapping)
  // 4.2V = 100%, 3.7V = ~50%, 3.0V = 0%
  if (voltage >= 4.2) return 100;
  if (voltage <= 3.0) return 0;
  return (int)((voltage - 3.0) / 1.2 * 100);
}

bool isBatteryCritical() {
  // PowerBoost LBO goes LOW when battery < 3.2V
  // Only consider critical if voltage is also measurable (> 0.5V)
  // This prevents false shutdown when battery hardware not connected
  bool lboLow = digitalRead(LOW_BATT_PIN) == LOW;
  bool voltageValid = battery.voltage > 0.5;
  return lboLow && voltageValid;
}

void updateBattery() {
  battery.voltage = readBatteryVoltage();
  battery.percent = getBatteryPercent(battery.voltage);
  battery.critical = isBatteryCritical();
  battery.valid = true;
  battery.lastRead = millis();
}

void handleLowBattery() {
  if (!battery.critical) return;

  Serial.println("[BATT] CRITICAL LOW BATTERY - Please flip power switch OFF!");

  // Flush and close all open files to prevent corruption
  if (logging) {
    if (navFile) { navFile.flush(); navFile.close(); }
    if (imuFile) { imuFile.flush(); imuFile.close(); }
    if (rawFile) { rawFile.flush(); rawFile.close(); }
    if (windFile) { windFile.flush(); windFile.close(); }
    logging = false;
  }

  // Display warning - user must flip hardware power switch
  if (oledOK) {
    u8g2.clearBuffer();
    u8g2.setFont(u8g2_font_helvB14_tr);
    u8g2.drawStr(10, 20, "LOW BATTERY");
    u8g2.setFont(u8g2_font_helvR10_tr);
    u8g2.drawStr(5, 45, "Flip power switch");
    u8g2.drawStr(25, 60, "to OFF");
    u8g2.sendBuffer();
  }

  // Halt here - user must use hardware switch
  while (true) {
    delay(1000);
  }
}

// Draw battery percentage in top-right corner
void drawBatteryPercent(int x, int y) {
  if (!battery.valid) return;

  char buf[8];
  snprintf(buf, sizeof(buf), "%d%%", battery.percent);

  // Use small font for battery %
  u8g2.setFont(u8g2_font_helvR08_tr);

  // Blink if critical
  if (battery.critical && (millis() / 500) % 2 == 0) {
    // Don't draw (blink off)
  } else {
    u8g2.drawStr(x, y + 8, buf);
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
      bno08x.enableReport(SH2_ROTATION_VECTOR, IMU_INTERVAL_MS * 1000);
      bno08x.enableReport(SH2_ACCELEROMETER, IMU_INTERVAL_MS * 1000);
    }

    // Read only a few events to avoid blocking display updates
    int maxReads = 3;
    while (maxReads-- > 0 && bno08x.getSensorEvent(&sensorValue)) {
      switch (sensorValue.sensorId) {
        case SH2_GAME_ROTATION_VECTOR:
          // Not using quaternion for heel/pitch - accelerometer is more reliable
          // Just ignore this report, we calculate from accelerometer below
          break;
        case SH2_ACCELEROMETER:
          imu.accel_x = sensorValue.un.accelerometer.x;
          imu.accel_y = sensorValue.un.accelerometer.y;
          imu.accel_z = sensorValue.un.accelerometer.z;

          // Calculate heel and pitch from accelerometer (gravity reference)
          // Chip is mounted with X pointing UP, Y pointing STARBOARD, Z pointing BOW
          // X ≈ 9.8 when level (gravity)
          // Y changes with heel (port/starboard tilt)
          // Z changes with pitch (bow up/down tilt)

          // Heel: positive = starboard down, negative = port down
          imu.heel = atan2(imu.accel_y, imu.accel_x) * 180.0 / PI;

          // Pitch: positive = bow up, negative = bow down
          imu.pitch = atan2(-imu.accel_z, sqrt(imu.accel_y * imu.accel_y + imu.accel_x * imu.accel_x)) * 180.0 / PI;

          // Apply calibration offsets
          imu.heel -= imuHeelOffset;
          imu.pitch -= imuPitchOffset;

          // Normalize to -180 to +180 range
          while (imu.heel > 180) imu.heel -= 360;
          while (imu.heel < -180) imu.heel += 360;
          while (imu.pitch > 180) imu.pitch -= 360;
          while (imu.pitch < -180) imu.pitch += 360;
          break;

        case SH2_ROTATION_VECTOR: {
          // Full rotation vector with magnetometer - use for heading only
          float qr = sensorValue.un.rotationVector.real;
          float qi = sensorValue.un.rotationVector.i;
          float qj = sensorValue.un.rotationVector.j;
          float qk = sensorValue.un.rotationVector.k;

          // Yaw (heading) = rotation around Z axis
          float siny_cosp = 2.0 * (qr * qk + qi * qj);
          float cosy_cosp = 1.0 - 2.0 * (qj * qj + qk * qk);
          float heading = atan2(siny_cosp, cosy_cosp) * 180.0 / PI;

          // Convert to 0-360 range
          if (heading < 0) heading += 360.0;
          imu.heading = heading;
          break;
        }
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
      imu.pitch = atan2(imu.accel_y, imu.accel_z) * 180.0 / PI;
      imu.heel = atan2(-imu.accel_x,
        sqrt(imu.accel_y * imu.accel_y + imu.accel_z * imu.accel_z)) * 180.0 / PI;

      // Apply calibration offsets
      imu.heel -= imuHeelOffset;
      imu.pitch -= imuPitchOffset;

      // Normalize to -180 to +180 range
      while (imu.heel > 180) imu.heel -= 360;
      while (imu.heel < -180) imu.heel += 360;
      while (imu.pitch > 180) imu.pitch -= 360;
      while (imu.pitch < -180) imu.pitch += 360;
    }
  }
}

// ============================================================
// CONFIG
// ============================================================
void loadIMUCalibration() {
  File f = SD.open("/imu_cal.txt", FILE_READ);
  if (!f) {
    Serial.println("[IMU] No calibration file, using defaults");
    return;
  }

  while (f.available()) {
    String line = f.readStringUntil('\n');
    line.trim();
    if (line.startsWith("heel_offset=")) {
      imuHeelOffset = line.substring(12).toFloat();
    } else if (line.startsWith("pitch_offset=")) {
      imuPitchOffset = line.substring(13).toFloat();
    }
  }
  f.close();
  Serial.printf("[IMU] Loaded calibration: heel=%.1f, pitch=%.1f\n",
    imuHeelOffset, imuPitchOffset);
}

void saveIMUCalibration() {
  File f = SD.open("/imu_cal.txt", FILE_WRITE);
  if (!f) {
    Serial.println("[IMU] Failed to save calibration");
    return;
  }
  f.printf("heel_offset=%.2f\n", imuHeelOffset);
  f.printf("pitch_offset=%.2f\n", imuPitchOffset);
  f.close();
  Serial.printf("[IMU] Saved calibration: heel=%.1f, pitch=%.1f\n",
    imuHeelOffset, imuPitchOffset);
}

void calibrateIMU() {
  if (!imuOK) {
    Serial.println("[IMU] No IMU available for calibration");
    return;
  }

  // Read current raw values (before offset applied)
  float rawHeel = imu.heel + imuHeelOffset;  // Undo current offset to get raw
  float rawPitch = imu.pitch + imuPitchOffset;

  // Set new offsets so current position becomes zero
  imuHeelOffset = rawHeel;
  imuPitchOffset = rawPitch;

  // Save to SD card
  saveIMUCalibration();

  Serial.printf("[IMU] Calibrated: new offsets heel=%.1f, pitch=%.1f\n",
    imuHeelOffset, imuPitchOffset);
}

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
    else if (k == "wind_enabled") config.wind_enabled = (v == "true" || v == "1");
    else if (k == "wind_mac") v.toCharArray(config.wind_mac, sizeof(config.wind_mac));
    // Recording thresholds
    else if (k == "start_speed_knots") config.start_speed_knots = v.toFloat();
    else if (k == "stop_speed_knots") config.stop_speed_knots = v.toFloat();
    else if (k == "start_delay_sec") config.start_delay_sec = v.toInt();
    else if (k == "stop_delay_sec") config.stop_delay_sec = v.toInt();
  }
  f.close();

  Serial.printf("[CFG] Boat: %s, Rate: %dHz, WiFi networks: %d\n",
    config.boat_id, config.gps_rate_hz, config.wifi_count);
  for (int i = 0; i < config.wifi_count; i++) {
    Serial.printf("[CFG]   %d: %s\n", i + 1, config.wifi[i].ssid);
  }
  Serial.printf("[CFG] Wind: %s", config.wind_enabled ? "enabled" : "disabled");
  if (strlen(config.wind_mac) > 0) {
    Serial.printf(" (MAC: %s)", config.wind_mac);
  }
  Serial.println();
}

// ============================================================
// OTA + TELNET SETUP
// ============================================================
void setupOTA() {
  ArduinoOTA.setHostname(config.boat_id);  // Use boat ID as hostname
  ArduinoOTA.setPassword("sailframes");     // OTA password

  ArduinoOTA.onStart([]() {
    otaInProgress = true;
    String type = (ArduinoOTA.getCommand() == U_FLASH) ? "firmware" : "filesystem";
    Serial.printf("[OTA] Start updating %s\n", type.c_str());

    // Show on display
    if (oledOK) {
      u8g2.clearBuffer();
      u8g2.setFont(u8g2_font_helvB14_tr);
      u8g2.drawStr(10, 25, "OTA");
      u8g2.drawStr(10, 45, "UPDATE");
      u8g2.setFont(u8g2_font_6x10_tr);
      u8g2.drawStr(10, 60, "DO NOT POWER OFF");
      u8g2.sendBuffer();
    }

    // Close log files before OTA
    if (logging) {
      navFile.close();
      if (imuFile) imuFile.close();
      rawFile.close();
      logging = false;
    }
  });

  ArduinoOTA.onEnd([]() {
    otaInProgress = false;
    Serial.println("\n[OTA] Complete! Rebooting...");
    if (oledOK) {
      u8g2.clearBuffer();
      u8g2.setFont(u8g2_font_helvB14_tr);
      u8g2.drawStr(10, 35, "REBOOTING");
      u8g2.sendBuffer();
    }
  });

  ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
    int pct = progress / (total / 100);
    Serial.printf("[OTA] Progress: %u%%\r", pct);

    // Update progress bar on display
    if (oledOK) {
      u8g2.clearBuffer();
      u8g2.setFont(u8g2_font_helvB14_tr);
      u8g2.drawStr(10, 20, "UPDATING");
      u8g2.setFont(u8g2_font_6x10_tr);

      // Progress bar
      u8g2.drawFrame(10, 30, 108, 16);
      u8g2.drawBox(12, 32, (104 * pct) / 100, 12);

      char buf[16];
      snprintf(buf, sizeof(buf), "%d%%", pct);
      u8g2.drawStr(50, 58, buf);
      u8g2.sendBuffer();
    }
  });

  ArduinoOTA.onError([](ota_error_t error) {
    otaInProgress = false;
    Serial.printf("[OTA] Error[%u]: ", error);
    if (error == OTA_AUTH_ERROR) Serial.println("Auth Failed");
    else if (error == OTA_BEGIN_ERROR) Serial.println("Begin Failed");
    else if (error == OTA_CONNECT_ERROR) Serial.println("Connect Failed");
    else if (error == OTA_RECEIVE_ERROR) Serial.println("Receive Failed");
    else if (error == OTA_END_ERROR) Serial.println("End Failed");

    if (oledOK) {
      u8g2.clearBuffer();
      u8g2.setFont(u8g2_font_6x10_tr);
      u8g2.drawStr(10, 30, "OTA ERROR!");
      u8g2.sendBuffer();
    }
  });

  ArduinoOTA.begin();
  Serial.println("[OTA] Ready");
}

void startTelnetServer() {
  telnetServer.begin();
  telnetServer.setNoDelay(true);
  Serial.println("[TELNET] Server started on port 23");
}

void handleTelnet() {
  // Check for new clients
  if (telnetServer.hasClient()) {
    if (!telnetClient || !telnetClient.connected()) {
      if (telnetClient) telnetClient.stop();
      telnetClient = telnetServer.available();
      telnetClient.println("\n=================================");
      telnetClient.println("  SailFrames E1 Telnet Console");
      telnetClient.printf("  Boat: %s\n", config.boat_id);
      telnetClient.println("  Type 'help' for commands");
      telnetClient.println("=================================\n");
      telnetClient.print("> ");
      Serial.println("[TELNET] Client connected");
    } else {
      // Reject additional clients
      telnetServer.available().stop();
    }
  }

  // Handle client input
  if (telnetClient && telnetClient.connected()) {
    while (telnetClient.available()) {
      char c = telnetClient.read();
      if (c == '\n' || c == '\r') {
        if (telnetBuffer.length() > 0) {
          telnetClient.println();  // Echo newline
          processCommand(telnetBuffer, true);
          telnetBuffer = "";
          telnetClient.print("> ");
        }
      } else if (c == 127 || c == 8) {  // Backspace
        if (telnetBuffer.length() > 0) {
          telnetBuffer.remove(telnetBuffer.length() - 1);
          telnetClient.print("\b \b");  // Erase character
        }
      } else if (c >= 32 && c < 127) {  // Printable
        telnetBuffer += c;
        telnetClient.print(c);  // Echo
      }
    }
  }
}

// Print to both Serial and Telnet if connected
void tprint(const char* msg) {
  Serial.print(msg);
  if (telnetClient && telnetClient.connected()) {
    telnetClient.print(msg);
  }
}

void tprintf(const char* fmt, ...) {
  char buf[256];
  va_list args;
  va_start(args, fmt);
  vsnprintf(buf, sizeof(buf), fmt, args);
  va_end(args);
  tprint(buf);
}

void tprintln(const char* msg) {
  Serial.println(msg);
  if (telnetClient && telnetClient.connected()) {
    telnetClient.println(msg);
  }
}

// ============================================================
// SESSION COUNTER (for fallback folder naming)
// ============================================================
int getNextSessionNumber() {
  int n = 1;
  File f = SD.open("/sf/session.txt", FILE_READ);
  if (f) {
    n = f.parseInt() + 1;
    f.close();
  }
  f = SD.open("/sf/session.txt", FILE_WRITE);
  if (f) {
    f.print(n);
    f.close();
  }
  return n;
}

// ============================================================
// START LOGGING
// ============================================================
void startLogging() {
  Serial.println("[LOG] Starting logging...");

  // Folder naming: GPS datetime preferred, session counter as fallback
  char dd[32], ds[20], ts[12];
  // Check for valid GPS date: year portion (chars 4-5) must not be "00" (default)
  // Date format is DDMMYY, so "050426" = April 5, 2026
  bool hasGpsDate = (strlen(gps.date) >= 6 && (gps.date[4] != '0' || gps.date[5] != '0'));
  // Check for valid GPS time: must have a fix and time length OK
  bool hasGpsTime = gps.valid && (strlen(gps.utc_time) >= 6);

  if (hasGpsDate && hasGpsTime) {
    // Best case: GPS date + time as folder name (e.g., /sf/20260402_163325/)
    snprintf(dd, sizeof(dd), "/sf/20%c%c%c%c%c%c_%c%c%c%c%c%c",
      gps.date[4], gps.date[5], gps.date[2], gps.date[3], gps.date[0], gps.date[1],
      gps.utc_time[0], gps.utc_time[1], gps.utc_time[2],
      gps.utc_time[3], gps.utc_time[4], gps.utc_time[5]);
    snprintf(ds, sizeof(ds), "20%c%c%c%c%c%c",
      gps.date[4], gps.date[5], gps.date[2], gps.date[3], gps.date[0], gps.date[1]);
    snprintf(ts, sizeof(ts), "%c%c%c%c%c%c",
      gps.utc_time[0], gps.utc_time[1], gps.utc_time[2],
      gps.utc_time[3], gps.utc_time[4], gps.utc_time[5]);
  } else {
    // Fallback: sequential session number (e.g., /sf/session_001/)
    int sessionNum = getNextSessionNumber();
    snprintf(dd, sizeof(dd), "/sf/session_%03d", sessionNum);
    snprintf(ds, sizeof(ds), "s%03d", sessionNum);
    // Use millis for timestamp portion
    snprintf(ts, sizeof(ts), "%06lu", (millis() / 1000) % 1000000);
    Serial.printf("[LOG] No GPS datetime, using session_%03d\n", sessionNum);
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
  char np[64], ip[64], rp[64], wp[64];
  snprintf(np, sizeof(np), "%s/%s_%s_%s_nav.csv", dd, config.boat_id, ds, ts);
  snprintf(ip, sizeof(ip), "%s/%s_%s_%s_imu.csv", dd, config.boat_id, ds, ts);
  snprintf(rp, sizeof(rp), "%s/%s_%s_%s_raw.rtcm3", dd, config.boat_id, ds, ts);
  snprintf(wp, sizeof(wp), "%s/%s_%s_%s_wind.csv", dd, config.boat_id, ds, ts);

  Serial.printf("[LOG] Opening NAV: %s\n", np);
  navFile = SD.open(np, FILE_WRITE);
  Serial.printf("[LOG] NAV file %s\n", navFile ? "OK" : "FAILED");

  Serial.printf("[LOG] Opening IMU: %s\n", ip);
  imuFile = SD.open(ip, FILE_WRITE);
  Serial.printf("[LOG] IMU file %s\n", imuFile ? "OK" : "FAILED");

  Serial.printf("[LOG] Opening RAW: %s\n", rp);
  rawFile = SD.open(rp, FILE_WRITE);
  Serial.printf("[LOG] RAW file %s\n", rawFile ? "OK" : "FAILED");

#if ENABLE_WIND
  if (config.wind_enabled) {
    Serial.printf("[LOG] Opening WIND: %s\n", wp);
    windFile = SD.open(wp, FILE_WRITE);
    Serial.printf("[LOG] WIND file %s\n", windFile ? "OK" : "FAILED");
  }
#endif

  if (navFile) {
    logging = true;
    logStart = millis();
    navFile.println("ms,utc,lat,lon,alt,sog,cog,sat,hdop,fix");
    navFile.flush();
    if (imuFile) {
      imuFile.println("ms,utc,ax,ay,az,gx,gy,gz,heel,pitch");
      imuFile.flush();
    }
#if ENABLE_WIND
    if (windFile) {
      windFile.println("ms,utc,aws_kts,aws_mps,awa_deg,battery");
      windFile.flush();
    }
#endif
    Serial.println("[LOG] ========================================");
    Serial.printf("[LOG] NAV: %s\n", np);
    Serial.printf("[LOG] IMU: %s\n", ip);
#if ENABLE_WIND
    if (config.wind_enabled) Serial.printf("[LOG] WIND: %s\n", wp);
#endif
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
  imuFile.printf("%lu,%s,%.4f,%.4f,%.4f,%.2f,%.2f,%.2f,%.1f,%.1f\n",
    e, gps.utc_time, imu.accel_x, imu.accel_y, imu.accel_z,
    imu.gyro_x, imu.gyro_y, imu.gyro_z, imu.heel, imu.pitch);
  totalBytes += 120;
}

// ============================================================
// DISPLAY
// ============================================================

// Display mode: 1 = D1 (original), 2 = D2 (sailing data)
int displayMode = 2;

// D1: Original display (SOG/COG, HEEL/MAG, status bar)
void updateDisplayD1() {
  if (!oledOK) return;

  char buf[32];
  u8g2.clearBuffer();

  // Check for problems first
  bool hasWarning = false;
  if (!sdOK) {
    u8g2.setFont(u8g2_font_helvB12_tr);
    u8g2.drawStr(21, 16, "NO SD CARD!");
    hasWarning = true;
  } else if (lastValidGPS == 0 && millis() > 120000) {
    u8g2.setFont(u8g2_font_helvB12_tr);
    u8g2.drawStr(21, 16, "NO GPS FIX!");
    hasWarning = true;
  } else if (lastValidGPS > 0 && millis() - lastValidGPS > 60000) {
    u8g2.setFont(u8g2_font_helvB12_tr);
    u8g2.drawStr(21, 16, "GPS LOST!");
    hasWarning = true;
  } else if (!imuOK) {
    u8g2.setFont(u8g2_font_helvB12_tr);
    u8g2.drawStr(21, 16, "NO IMU!");
    hasWarning = true;
  }

  // Vertical labels (rotated 90° CCW) - tiny font
  u8g2.setFont(u8g2_font_5x7_tr);
  u8g2.setFontDirection(3);  // 270° = 90° counter-clockwise
  if (!hasWarning) {
    u8g2.drawStr(6, 26, "SOG");
    u8g2.drawStr(70, 26, "COG");
  }
  u8g2.drawStr(6, 52, "HEEL");
  u8g2.drawStr(70, 52, "BAT");
  u8g2.setFontDirection(0);  // Reset to normal

  // Row 1: SOG and COG (larger font) - skip if warning shown
  u8g2.setFont(u8g2_font_helvB18_tr);
  if (!hasWarning) {
    snprintf(buf, sizeof(buf), "%.1f", gps.speed_kts);
    u8g2.drawStr(9, 24, buf);
    snprintf(buf, sizeof(buf), "%03d", (int)gps.course);
    u8g2.drawStr(73, 24, buf);
  }

  // Row 2: Heel and Battery % (larger font)
  u8g2.setFont(u8g2_font_helvB18_tr);
  snprintf(buf, sizeof(buf), "%+.0f", imu.heel);
  u8g2.drawStr(9, 50, buf);
  snprintf(buf, sizeof(buf), "%d%%", battery.percent);
  u8g2.drawStr(73, 50, buf);

  // Row 3: Status bar
  u8g2.setFont(u8g2_font_5x7_tr);
  int dispSats = (gps.satellites >= 0 && gps.satellites <= 50) ? gps.satellites : 0;
  int dispView = (satsInView >= 0 && satsInView <= 60) ? satsInView : 0;
  // Show actual values (satsInView may be < dispSats if GLGSV/GAGSV not enabled)
  float dispHdop = (gps.hdop >= 0 && gps.hdop < 50) ? gps.hdop : 99.9;
  const char* fixStr = "---";
  if (gps.fix_quality == 1) fixStr = "GPS";
  else if (gps.fix_quality == 2) fixStr = "SBAS";

  // Recording state
  const char* recStr = getRecStateStr();

  char statusStr[16] = "";
  strcat(statusStr, recStr);
  if (uploading) strcat(statusStr, " UP");
  else if (wifiConnected) strcat(statusStr, " W");
#if ENABLE_WIND
  if (wind.connected) strcat(statusStr, " C");
#endif

  // Status line with recording state
  snprintf(buf, sizeof(buf), "%s %s %d/%d",
    config.boat_id, statusStr, dispSats, dispView);
  u8g2.drawStr(1, 64, buf);

  u8g2.sendBuffer();
}

// D2: Sailing data display (AWS/AWA, TWS/TWA, SOG/COG, HEEL/BAT, SAT/HDOP)
void updateDisplayD2() {
  if (!oledOK) return;

  char buf[32];
  u8g2.clearBuffer();

  // Calculate true wind from apparent wind + boat speed
  float aws = 0, awa = 0, tws = 0, twa = 0;
#if ENABLE_WIND
  if (wind.connected && wind.lastUpdate > 0 && millis() - wind.lastUpdate < 5000) {
    aws = wind.speed_kts;
    awa = wind.angle_deg;
    // Convert AWA to radians (-180 to 180)
    float awaRad = awa * PI / 180.0;
    if (awaRad > PI) awaRad -= 2 * PI;
    // True wind calculation
    float sog = gps.speed_kts;
    // TWS = sqrt(AWS² + SOG² - 2*AWS*SOG*cos(AWA))
    tws = sqrt(aws*aws + sog*sog + 2*aws*sog*cos(awaRad));
    // TWA = atan2(AWS*sin(AWA), AWS*cos(AWA) + SOG)
    float twaRad = atan2(aws * sin(awaRad), aws * cos(awaRad) + sog);
    twa = twaRad * 180.0 / PI;
    if (twa < 0) twa += 360;
  }
#endif

  // Row 1: Wind data (AWS, AWA, TWS, TWA) - larger font
  u8g2.setFont(u8g2_font_5x7_tr);
  u8g2.drawStr(0, 7, "AWS");
  u8g2.drawStr(32, 7, "AWA");
  u8g2.drawStr(64, 7, "TWS");
  u8g2.drawStr(96, 7, "TWA");

  u8g2.setFont(u8g2_font_helvB14_tr);
  snprintf(buf, sizeof(buf), "%.0f", aws);
  u8g2.drawStr(0, 24, buf);
  snprintf(buf, sizeof(buf), "%03d", (int)awa);
  u8g2.drawStr(32, 24, buf);
  snprintf(buf, sizeof(buf), "%.0f", tws);
  u8g2.drawStr(64, 24, buf);
  snprintf(buf, sizeof(buf), "%03d", (int)twa);
  u8g2.drawStr(96, 24, buf);

  // Row 2: Nav data (SOG, COG, HEEL, BAT) - larger font
  u8g2.setFont(u8g2_font_5x7_tr);
  u8g2.drawStr(0, 33, "SOG");
  u8g2.drawStr(32, 33, "COG");
  u8g2.drawStr(64, 33, "HEL");
  u8g2.drawStr(96, 33, "BAT");

  u8g2.setFont(u8g2_font_helvB14_tr);
  snprintf(buf, sizeof(buf), "%.1f", gps.speed_kts);
  u8g2.drawStr(0, 50, buf);
  snprintf(buf, sizeof(buf), "%03d", (int)gps.course);
  u8g2.drawStr(32, 50, buf);
  snprintf(buf, sizeof(buf), "%+.0f", imu.heel);
  u8g2.drawStr(64, 50, buf);
  snprintf(buf, sizeof(buf), "%d%%", battery.percent);
  u8g2.drawStr(96, 50, buf);

  // Row 3: Status line with pitch
  u8g2.setFont(u8g2_font_5x7_tr);
  int dispSats = (gps.satellites >= 0 && gps.satellites <= 50) ? gps.satellites : 0;
  int dispView = (satsInView >= 0 && satsInView <= 60) ? satsInView : 0;
  // Show actual values (satsInView may be < dispSats if GLGSV/GAGSV not enabled)
  float dispHdop = (gps.hdop >= 0 && gps.hdop < 50) ? gps.hdop : 99.9;

  const char* fixStr = "---";
  if (gps.fix_quality == 1) fixStr = "GPS";
  else if (gps.fix_quality == 2) fixStr = "SBA";

  // Recording state indicator
  const char* recStr = getRecStateStr();

  char statusStr[16] = "";
  strcat(statusStr, recStr);
  if (uploading) strcat(statusStr, " UP");
  else if (wifiConnected) strcat(statusStr, " W");
#if ENABLE_WIND
  if (wind.connected) strcat(statusStr, " C");
#endif

  // Warning indicators
  char warnStr[16] = "";
  if (!sdOK) strcat(warnStr, "!SD ");
  if (!imuOK) strcat(warnStr, "!IMU ");
  if (lastValidGPS > 0 && millis() - lastValidGPS > 60000) strcat(warnStr, "!GPS");

  // Status line with recording state
  snprintf(buf, sizeof(buf), "%s %d/%d %.1f %s",
    fixStr, dispSats, dispView, dispHdop, statusStr);
  u8g2.drawStr(0, 62, buf);

  u8g2.sendBuffer();
}

// Main display router
void updateDisplay() {
  if (displayMode == 1) {
    updateDisplayD1();
  } else {
    updateDisplayD2();
  }
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

// Delete files that have been uploaded (have .uploaded marker)
int deleteUploadedFiles(const char* dirname) {
  int count = 0;
  File root = SD.open(dirname);
  if (!root || !root.isDirectory()) return 0;

  // First pass: collect files to delete (can't delete while iterating)
  String filesToDelete[50];
  int fileCount = 0;

  File file = root.openNextFile();
  while (file && fileCount < 50) {
    char filepath[128];
    snprintf(filepath, sizeof(filepath), "%s/%s", dirname, file.name());
    String name = String(file.name());

    if (file.isDirectory()) {
      file.close();
      count += deleteUploadedFiles(filepath);  // Recurse
    } else if (name.endsWith(".uploaded")) {
      // This is a marker file - get the original filename
      String original = String(filepath);
      original = original.substring(0, original.length() - 9);  // Remove ".uploaded"
      filesToDelete[fileCount++] = original;
      filesToDelete[fileCount++] = String(filepath);  // Also delete marker
    }
    file.close();
    file = root.openNextFile();
    yield();
  }
  root.close();

  // Second pass: delete collected files
  for (int i = 0; i < fileCount; i++) {
    if (SD.remove(filesToDelete[i].c_str())) {
      Serial.printf("[CLEANUP] Deleted: %s\n", filesToDelete[i].c_str());
      count++;
    }
    yield();
  }

  return count;
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

// Threshold for using presigned URL (1MB) - larger files bypass API Gateway
#define PRESIGN_THRESHOLD 1000000

// Extract presigned URL from JSON response
// Returns empty string if not found
String extractPresignedUrl(const String& json) {
  // Try with space: "url": "
  int urlStart = json.indexOf("\"url\": \"");
  if (urlStart >= 0) {
    urlStart += 8;  // Skip past "url": "
  } else {
    // Try without space: "url":"
    urlStart = json.indexOf("\"url\":\"");
    if (urlStart >= 0) {
      urlStart += 7;  // Skip past "url":"
    }
  }
  if (urlStart < 0) return "";
  int urlEnd = json.indexOf("\"", urlStart);
  if (urlEnd < 0) return "";
  return json.substring(urlStart, urlEnd);
}

// Upload directly to S3 using presigned URL (for large files)
bool uploadToS3Presigned(const char* filepath, File& file, size_t fileSize, const String& presignedUrl) {
  Serial.println("[UPLOAD] Using presigned S3 URL (direct upload)");
  Serial.printf("[UPLOAD] File size: %u bytes, heap: %u\n", fileSize, ESP.getFreeHeap());

  // Convert HTTPS to HTTP - S3 supports both, HTTP is faster (no TLS overhead)
  String httpUrl = presignedUrl;
  if (httpUrl.startsWith("https://")) {
    httpUrl = "http://" + httpUrl.substring(8);
    Serial.println("[UPLOAD] Using HTTP (no TLS) for faster upload");
  }

  WiFiClient s3Client;  // Plain HTTP, no TLS
  s3Client.setTimeout(300);  // 5 minute timeout for large files

  HTTPClient s3Http;
  s3Http.setTimeout(300000);  // 5 minute timeout
  s3Http.setReuse(false);

  // Connect to S3
  Serial.println("[UPLOAD] Connecting to S3...");
  if (!s3Http.begin(s3Client, httpUrl)) {
    Serial.println("[UPLOAD] Failed to begin S3 HTTP");
    return false;
  }

  // Determine content type
  String contentType = "application/octet-stream";
  if (String(filepath).endsWith(".csv")) {
    contentType = "text/csv";
  }
  s3Http.addHeader("Content-Type", contentType);
  s3Http.addHeader("Content-Length", String(fileSize));

  yield();

  Serial.printf("[UPLOAD] Starting PUT (%u bytes)...\n", fileSize);
  unsigned long startTime = millis();

  // Upload file directly to S3
  int httpCode = s3Http.sendRequest("PUT", &file, fileSize);

  unsigned long elapsed = (millis() - startTime) / 1000;
  Serial.printf("[UPLOAD] Request completed in %lu sec, heap: %u\n", elapsed, ESP.getFreeHeap());

  String response = s3Http.getString();
  s3Http.end();

  if (httpCode == 200 || httpCode == 201 || httpCode == 204) {
    Serial.printf("[UPLOAD] S3 Success: %s (HTTP %d)\n", filepath, httpCode);
    return true;
  } else {
    Serial.printf("[UPLOAD] S3 Failed: %s (HTTP %d)\n", filepath, httpCode);
    if (response.length() > 0 && response.length() < 500) {
      Serial.printf("[UPLOAD] S3 Response: %s\n", response.c_str());
    }
    return false;
  }
}

// Request presigned URL from API Gateway
String requestPresignedUrl(const char* filepath, size_t fileSize) {
  WiFiClientSecure client;
  client.setInsecure();
  client.setTimeout(30);

  HTTPClient http;
  http.setTimeout(30000);
  http.setReuse(false);

  // Build presign request URL
  String url = String(config.upload_url);
  url += "?boat=";
  url += config.boat_id;
  url += "&file=";
  url += filepath;
  url += "&presign=1&size=";
  url += String(fileSize);

  Serial.println("[UPLOAD] Requesting presigned URL...");

  if (!http.begin(client, url)) {
    Serial.println("[UPLOAD] Failed to begin presign request");
    return "";
  }

  // Use POST with empty body (API Gateway only accepts POST/PUT)
  http.addHeader("Content-Type", "application/octet-stream");
  http.addHeader("Content-Length", "0");
  int httpCode = http.POST("");

  if (httpCode != 200) {
    Serial.printf("[UPLOAD] Presign request failed: HTTP %d\n", httpCode);
    http.end();
    return "";
  }

  String response = http.getString();
  http.end();

  Serial.printf("[UPLOAD] Response length: %d\n", response.length());
  if (response.length() < 500) {
    Serial.printf("[UPLOAD] Response: %s\n", response.c_str());
  } else {
    Serial.printf("[UPLOAD] Response (first 200): %.200s\n", response.c_str());
  }

  String presignedUrl = extractPresignedUrl(response);
  if (presignedUrl.length() == 0) {
    Serial.println("[UPLOAD] Failed to parse presigned URL from response");
    return "";
  }

  Serial.printf("[UPLOAD] Got presigned URL (%d chars)\n", presignedUrl.length());
  return presignedUrl;
}

// Upload a single file to S3
// Small files (<1MB): Direct upload via API Gateway
// Large files (>=1MB): Request presigned URL, upload directly to S3
bool uploadFile(const char* filepath) {
  uploadCount++;
  updateDisplay();

  // Feed watchdog before file operations
  yield();
  delay(10);

  // Verify WiFi is still connected
  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("[UPLOAD] WiFi disconnected, skipping: %s\n", filepath);
    return false;
  }

  File file = SD.open(filepath, FILE_READ);
  if (!file) {
    Serial.printf("[UPLOAD] Cannot open: %s\n", filepath);
    return false;
  }

  size_t fileSize = file.size();

  // Skip empty files
  if (fileSize == 0) {
    Serial.printf("[UPLOAD] Skipping empty file: %s\n", filepath);
    file.close();
    return true;  // Mark as success so it gets .uploaded marker
  }

  Serial.printf("[UPLOAD] Uploading %s (%u bytes)... Free heap: %u\n", filepath, fileSize, ESP.getFreeHeap());

  // Feed watchdog
  yield();
  delay(10);

  // Check minimum heap for SSL (~45KB needed for TLS handshake)
  if (ESP.getFreeHeap() < 45000) {
    Serial.printf("[UPLOAD] Warning: Low heap (%u bytes), SSL may fail\n", ESP.getFreeHeap());
  }

  bool success = false;

  // Large files: Use presigned URL for direct S3 upload (bypasses API Gateway timeout)
  if (fileSize >= PRESIGN_THRESHOLD) {
    String presignedUrl = requestPresignedUrl(filepath, fileSize);
    if (presignedUrl.length() > 0) {
      success = uploadToS3Presigned(filepath, file, fileSize, presignedUrl);
    }
    file.close();
    yield();
    delay(50);
    return success;
  }

  // Small files: Direct upload via API Gateway
  WiFiClientSecure client;
  client.setInsecure();
  client.setTimeout(30);

  HTTPClient http;
  http.setTimeout(60000);
  http.setReuse(false);

  // Build the upload URL with filename as query param
  String url = String(config.upload_url);
  url += "?boat=";
  url += config.boat_id;
  url += "&file=";
  url += filepath;

  yield();

  Serial.printf("[UPLOAD] Direct upload via API Gateway\n");

  if (!http.begin(client, url)) {
    Serial.printf("[UPLOAD] Failed to begin HTTP: %s\n", filepath);
    file.close();
    return false;
  }

  http.addHeader("Content-Type", "application/octet-stream");
  http.addHeader("Content-Length", String(fileSize));

  yield();

  int httpCode = http.sendRequest("PUT", &file, fileSize);

  file.close();
  http.end();

  yield();
  delay(50);

  if (httpCode == 200 || httpCode == 201 || httpCode == 204) {
    Serial.printf("[UPLOAD] Success: %s (HTTP %d)\n", filepath, httpCode);
    return true;
  } else {
    const char* errMsg = "";
    if (httpCode == -1) errMsg = "CONNECTION_REFUSED/TIMEOUT";
    else if (httpCode == -2) errMsg = "SEND_HEADER_FAILED";
    else if (httpCode == -3) errMsg = "SEND_PAYLOAD_FAILED";
    else if (httpCode == -4) errMsg = "NOT_CONNECTED";
    else if (httpCode == -5) errMsg = "CONNECTION_LOST";
    else if (httpCode == -11) errMsg = "READ_TIMEOUT";
    Serial.printf("[UPLOAD] Failed: %s (HTTP %d %s)\n", filepath, httpCode, errMsg);
    return false;
  }
}

// Count files to upload in directory
int countFilesToUpload(const char* dirname) {
  int count = 0;
  File root = SD.open(dirname);
  if (!root || !root.isDirectory()) return 0;

  File file = root.openNextFile();
  while (file) {
    char filepath[128];
    snprintf(filepath, sizeof(filepath), "%s/%s", dirname, file.name());

    if (file.isDirectory()) {
      count += countFilesToUpload(filepath);
    } else {
      String name = String(file.name());
      if (!name.endsWith(".uploaded") && !isUploaded(filepath)) {
        count++;
      }
    }
    file = root.openNextFile();
    yield();  // Feed watchdog
  }
  return count;
}

// Scan directory and upload all un-uploaded files
void uploadDirectory(const char* dirname) {
  // Feed watchdog before directory operations
  yield();
  delay(10);

  Serial.printf("[UPLOAD] Opening dir: %s\n", dirname);
  Serial.printf("[UPLOAD] Heap: %u, Stack: %u\n", ESP.getFreeHeap(), uxTaskGetStackHighWaterMark(NULL));

  File root = SD.open(dirname);
  if (!root) {
    Serial.printf("[UPLOAD] Failed to open dir: %s\n", dirname);
    return;
  }
  if (!root.isDirectory()) {
    Serial.printf("[UPLOAD] Not a directory: %s\n", dirname);
    root.close();
    return;
  }

  Serial.println("[UPLOAD] Dir opened OK");
  yield();
  delay(50);

  Serial.println("[UPLOAD] Getting first file...");
  File file = root.openNextFile();
  while (file) {
    // Feed watchdog on each iteration
    yield();

    char filepath[128];
    snprintf(filepath, sizeof(filepath), "%s/%s", dirname, file.name());

    if (file.isDirectory()) {
      // Recurse into subdirectories
      file.close();  // Close before recursing
      uploadDirectory(filepath);
    } else {
      // Skip marker files and already uploaded files
      String name = String(file.name());
      file.close();  // Close file handle before upload

      if (!name.endsWith(".uploaded") && !isUploaded(filepath)) {
        // Feed watchdog before upload
        yield();
        delay(100);

        if (uploadFile(filepath)) {
          markUploaded(filepath);
        }

        // Longer pause between uploads to prevent crashes
        // Also service OTA and telnet during this time
        for (int i = 0; i < 10; i++) {
          yield();
          delay(50);
          ArduinoOTA.handle();
          handleTelnet();
        }

        // Update display
        updateDisplay();
      }
    }

    // Feed watchdog before getting next file
    yield();
    file = root.openNextFile();
  }
  root.close();
}

// Try to connect to any configured WiFi network
// Returns true if connected, stores SSID in connectedSSID
bool connectWiFi() {
  connectedSSID[0] = '\0';

  if (config.wifi_count == 0) {
    Serial.println("[WIFI] No networks configured");
    return false;
  }

  // Scan for networks first
  Serial.println("[WIFI] Scanning...");
  int n = WiFi.scanNetworks();
  Serial.printf("[WIFI] Found %d networks:\n", n);
  for (int i = 0; i < n && i < 10; i++) {
    wifi_auth_mode_t auth = WiFi.encryptionType(i);
    const char* authStr =
      auth == WIFI_AUTH_OPEN ? "OPEN" :
      auth == WIFI_AUTH_WEP ? "WEP" :
      auth == WIFI_AUTH_WPA_PSK ? "WPA" :
      auth == WIFI_AUTH_WPA2_PSK ? "WPA2" :
      auth == WIFI_AUTH_WPA_WPA2_PSK ? "WPA/WPA2" :
      auth == WIFI_AUTH_WPA3_PSK ? "WPA3" :
      auth == WIFI_AUTH_WPA2_WPA3_PSK ? "WPA2/WPA3" : "OTHER";
    Serial.printf("[WIFI]   %d: %s (%d dBm) %s ch%d\n",
      i + 1, WiFi.SSID(i).c_str(), WiFi.RSSI(i), authStr, WiFi.channel(i));
  }
  WiFi.scanDelete();

  // Try each configured network
  for (int i = 0; i < config.wifi_count; i++) {
    if (strlen(config.wifi[i].ssid) == 0) continue;

    Serial.printf("[WIFI] Trying %s (%d/%d)...\n",
      config.wifi[i].ssid, i + 1, config.wifi_count);

    // Show on display
    if (oledOK) {
      u8g2.clearBuffer();
      u8g2.setFont(u8g2_font_6x10_tr);
      u8g2.drawStr(0, 10, "CONNECTING...");
      char buf[32];
      snprintf(buf, sizeof(buf), "WiFi: %s", config.wifi[i].ssid);
      u8g2.drawStr(0, 25, buf);
      snprintf(buf, sizeof(buf), "(%d/%d)", i + 1, config.wifi_count);
      u8g2.drawStr(0, 40, buf);
      u8g2.sendBuffer();
    }

    Serial.printf("[WIFI] Credentials: SSID='%s' PASS='%s'\n",
      config.wifi[i].ssid, config.wifi[i].pass);

    // Ensure clean state before connecting
    WiFi.disconnect(true);
    delay(100);
    WiFi.mode(WIFI_STA);
    WiFi.persistent(false);  // Don't save to flash
    WiFi.setAutoReconnect(false);
    WiFi.begin(config.wifi[i].ssid, config.wifi[i].pass);

    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts++ < 30) {  // Increased to 30
      delay(500);
      Serial.print(".");
      if (attempts % 10 == 0) {
        // Print WiFi status for debugging
        int status = WiFi.status();
        const char* statusStr =
          status == WL_IDLE_STATUS ? "IDLE" :
          status == WL_NO_SSID_AVAIL ? "NO_SSID" :
          status == WL_SCAN_COMPLETED ? "SCAN_DONE" :
          status == WL_CONNECTED ? "CONNECTED" :
          status == WL_CONNECT_FAILED ? "FAILED" :
          status == WL_CONNECTION_LOST ? "LOST" :
          status == WL_DISCONNECTED ? "DISCONNECTED" : "UNKNOWN";
        Serial.printf(" [%s] ", statusStr);
      }
      yield();  // Feed watchdog
    }
    Serial.println();

    if (WiFi.status() == WL_CONNECTED) {
      strncpy(connectedSSID, config.wifi[i].ssid, sizeof(connectedSSID) - 1);
      Serial.printf("[WIFI] Connected to %s! IP: %s\n",
        connectedSSID, WiFi.localIP().toString().c_str());
      wifiConnected = true;

      // Start OTA and Telnet services
      setupOTA();
      startTelnetServer();

      // Show IP on display
      if (oledOK) {
        u8g2.clearBuffer();
        u8g2.setFont(u8g2_font_6x10_tr);
        u8g2.drawStr(0, 10, "WiFi CONNECTED");
        char buf[32];
        snprintf(buf, sizeof(buf), "SSID: %s", connectedSSID);
        u8g2.drawStr(0, 22, buf);
        snprintf(buf, sizeof(buf), "IP: %s", WiFi.localIP().toString().c_str());
        u8g2.drawStr(0, 34, buf);
        u8g2.drawStr(0, 46, "Telnet: port 23");
        u8g2.drawStr(0, 58, "OTA: enabled");
        u8g2.sendBuffer();
        delay(2000);
      }
      return true;
    }

    WiFi.disconnect(true);
    delay(100);
    yield();  // Feed watchdog
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

#if ENABLE_WIND
  // BLE interferes with WiFi TLS - must fully deinit BLE before upload
  if (bleInitialized) {
    Serial.println("[UPLOAD] Preparing for upload (BLE/WiFi radio conflict)...");

    // Disconnect BLE
    if (pWindClient && pWindClient->isConnected()) {
      Serial.println("[UPLOAD] Disconnecting wind sensor...");
      pWindClient->disconnect();
      delay(100);
    }

    // Deinit BLE
    Serial.println("[UPLOAD] Deinitializing BLE...");
    wind.connected = false;
    // Don't null pointers before deinit - let NimBLE clean up
    NimBLEDevice::deinit(false);  // false = don't clear all, just deinit
    // Now safe to null pointers
    pWindClient = nullptr;
    pWindSpeedChar = nullptr;
    pWindDirChar = nullptr;
    pBatteryChar = nullptr;
    bleInitialized = false;
    delay(500);
    Serial.printf("[UPLOAD] BLE deinit done, heap: %u bytes\n", ESP.getFreeHeap());
  }
#endif

  // Connect to WiFi (BLE is now fully off)
  if (connectWiFi()) {
    // Give system time to stabilize after WiFi connect
    Serial.println("[UPLOAD] Waiting for connection to stabilize...");
    for (int i = 0; i < 30; i++) {
      delay(100);
      yield();
      ArduinoOTA.handle();
    }

    uploading = true;
    uploadCount = 0;

    // Test connectivity before uploading
    Serial.println("[UPLOAD] Testing TLS connectivity...");
    Serial.printf("[UPLOAD] Free heap: %u bytes\n", ESP.getFreeHeap());
    WiFiClientSecure testClient;
    testClient.setInsecure();
    testClient.setTimeout(30);
    if (testClient.connect("p9s9eia0t6.execute-api.us-east-1.amazonaws.com", 443)) {
      Serial.println("[UPLOAD] Connection test OK");
      testClient.stop();
    } else {
      Serial.println("[UPLOAD] Connection test FAILED - aborting upload");
      uploading = false;
#if ENABLE_WIND
      if (config.wind_enabled && strlen(config.wind_mac) > 0) {
        Serial.println("[UPLOAD] Reinitializing wind sensor...");
        initWindSensor();
      }
#endif
      return;
    }

    // Count files to upload
    Serial.println("[UPLOAD] Counting files...");
    yield();
    uploadTotal = countFilesToUpload("/sf");
    Serial.printf("[UPLOAD] Found %d files to upload\n", uploadTotal);

    if (uploadTotal > 0) {
      updateDisplay();

      // Upload all files in /sf directory
      Serial.printf("[UPLOAD] Starting upload... (Free heap: %u bytes)\n", ESP.getFreeHeap());
      uploadDirectory("/sf");
      Serial.println("[UPLOAD] Done");

    } else {
      Serial.println("[UPLOAD] No new files to upload");
    }

    uploading = false;
    Serial.println("[UPLOAD] Complete, WiFi stays connected for OTA/telnet");

#if ENABLE_WIND
    // Reinitialize BLE and reconnect to wind sensor
    if (config.wind_enabled && strlen(config.wind_mac) > 0) {
      Serial.println("[UPLOAD] Reinitializing wind sensor...");
      initWindSensor();
    }
#endif
  }

  // Reset stationary timer to avoid rapid retries
  stationaryStart = 0;
}

// ============================================================
// SERIAL/TELNET COMMANDS
// ============================================================
void listDirOutput(const char* dirname, int depth, bool toTelnet) {
  File root = SD.open(dirname);
  if (!root || !root.isDirectory()) {
    tprintf("Failed to open %s\n", dirname);
    return;
  }

  File file = root.openNextFile();
  while (file) {
    char indent[32] = "";
    for (int i = 0; i < depth && i < 10; i++) strcat(indent, "  ");
    if (file.isDirectory()) {
      tprintf("%s[DIR]  %s/\n", indent, file.name());
      char path[128];
      snprintf(path, sizeof(path), "%s/%s", dirname, file.name());
      file.close();  // Close before recursing to free file descriptor
      listDirOutput(path, depth + 1, toTelnet);
    } else {
      tprintf("%s[FILE] %s (%lu bytes)\n", indent, file.name(), file.size());
      file.close();  // Close file after reading info
    }
    file = root.openNextFile();
    yield();
  }
  root.close();  // Close directory when done
}

// Process command from serial or telnet
void processCommand(String cmd, bool fromTelnet) {
  cmd.trim();
  if (cmd.length() == 0) return;

  if (cmd == "ls" || cmd == "list") {
    if (!sdOK) {
      tprintln("SD card not available");
      return;
    }
    tprintln("=== SD Card Contents ===");
    listDirOutput("/", 0, fromTelnet);
    tprintln("========================");

  } else if (cmd == "lssf") {
    if (!sdOK) {
      tprintln("SD card not available");
      return;
    }
    tprintln("=== /sf/ Contents ===");
    if (SD.exists("/sf")) {
      listDirOutput("/sf", 0, fromTelnet);
    } else {
      tprintln("/sf directory does not exist!");
      tprintln("Creating /sf...");
      if (SD.mkdir("/sf")) {
        tprintln("Created /sf successfully");
      } else {
        tprintln("Failed to create /sf");
      }
    }
    tprintf("Logging: %s\n", logging ? "ACTIVE" : "STOPPED");

  } else if (cmd == "status") {
    tprintln("=== Status ===");
    tprintf("GPS: %s, SAT:%d, HDOP:%.1f\n",
      gps.valid ? "FIX" : "NO FIX", gps.satellites, gps.hdop);
    tprintf("Position: %.6f, %.6f\n", gps.lat, gps.lon);
    tprintf("Speed: %.1f kt, Course: %.0f\n", gps.speed_kts, gps.course);
    tprintf("IMU: %s (heel:%.0f pitch:%.0f)\n",
      imuOK ? (useIMU_BNO ? "BNO085" : "MPU6050") : "NONE", imu.heel, imu.pitch);
    tprintf("SD:  %s\n", sdOK ? "OK" : "FAILED");
    tprintf("Logging: %s\n", logging ? "YES" : "NO");
    tprintf("Data: %lu KB, RTCM: %lu frames (0xD3: %lu)\n", totalBytes / 1024, rtcmFrameCount, rtcmSyncCount);
    tprintf("WiFi: %s\n", wifiConnected ? connectedSSID : "disconnected");
    if (wifiConnected) {
      tprintf("IP: %s\n", WiFi.localIP().toString().c_str());
    }
#if ENABLE_WIND
    if (config.wind_enabled) {
      if (wind.connected) {
        tprintf("Wind: %.1f kt @ %d deg", wind.speed_kts, wind.angle_deg);
        if (wind.battery >= 0) tprintf(" (%d%%)", wind.battery);
        tprintln("");
      } else {
        tprintln("Wind: scanning...");
      }
    } else {
      tprintln("Wind: disabled");
    }
#endif
    tprintln("===============");

  } else if (cmd.startsWith("cat ")) {
    String path = cmd.substring(4);
    path.trim();
    if (!sdOK) {
      tprintln("SD card not available");
      return;
    }
    File f = SD.open(path.c_str());
    if (!f) {
      tprintf("Cannot open: %s\n", path.c_str());
      return;
    }
    tprintf("=== %s (%lu bytes) ===\n", path.c_str(), f.size());
    int lines = 0;
    while (f.available() && lines < 50) {
      String line = f.readStringUntil('\n');
      tprintf("%s\n", line.c_str());
      lines++;
      yield();
    }
    if (f.available()) tprintln("... (truncated at 50 lines)");
    f.close();

  } else if (cmd == "upload") {
    if (!sdOK) {
      tprintln("SD card not available");
      return;
    }
    if (config.wifi_count == 0) {
      tprintln("WiFi not configured in config.txt");
      return;
    }
    tprintln("Starting manual upload...");

#if ENABLE_WIND
    // BLE interferes with WiFi TLS - must fully deinit BLE
    // Disconnect WiFi first, then deinit BLE, then reconnect WiFi
    if (bleInitialized) {
      tprintln("Preparing for upload (BLE/WiFi radio conflict)...");

      // Step 1: Disconnect BLE
      if (pWindClient && pWindClient->isConnected()) {
        tprintln("Disconnecting wind sensor...");
        pWindClient->disconnect();
        delay(100);
      }

      // Step 2: Disconnect WiFi if connected
      if (wifiConnected || WiFi.status() == WL_CONNECTED) {
        tprintln("Disconnecting WiFi...");
        WiFi.disconnect(true);
        wifiConnected = false;
        delay(200);
      }

      // Step 3: Deinit BLE (safe now that WiFi is disconnected)
      tprintln("Deinitializing BLE...");
      wind.connected = false;
      // Don't null pointers before deinit - let NimBLE clean up
      NimBLEDevice::deinit(false);  // false = don't clear all, just deinit
      // Now safe to null pointers
      pWindClient = nullptr;
      pWindSpeedChar = nullptr;
      pWindDirChar = nullptr;
      pBatteryChar = nullptr;
      bleInitialized = false;
      delay(500);
      tprintf("BLE deinit done, heap: %u bytes\n", ESP.getFreeHeap());
    }
#endif

    // Now connect WiFi (BLE is fully off)
    tprintln("Connecting to WiFi...");
    if (connectWiFi()) {
      tprintf("Connected to: %s, IP: %s\n", connectedSSID, WiFi.localIP().toString().c_str());
      tprintf("Free heap: %u bytes\n", ESP.getFreeHeap());
      tprintf("Stack high water: %u bytes\n", uxTaskGetStackHighWaterMark(NULL));

      // Test connectivity - first check DNS
      tprintln("Testing DNS resolution...");
      IPAddress ip;
      if (!WiFi.hostByName("p9s9eia0t6.execute-api.us-east-1.amazonaws.com", ip)) {
        tprintln("DNS resolution FAILED");
#if ENABLE_WIND
        if (config.wind_enabled && strlen(config.wind_mac) > 0) {
          tprintln("Reinitializing wind sensor...");
          initWindSensor();
        }
#endif
        return;
      }
      tprintf("DNS OK: %s\n", ip.toString().c_str());

      // Test TLS directly to AWS (skip intermediate tests now that BLE is off)
      tprintln("Testing TLS to AWS API Gateway...");
      tprintf("Free heap: %u bytes\n", ESP.getFreeHeap());
      WiFiClientSecure testClient;
      testClient.setInsecure();
      testClient.setTimeout(30);
      if (testClient.connect("p9s9eia0t6.execute-api.us-east-1.amazonaws.com", 443)) {
        tprintln("AWS TLS OK");
        testClient.stop();
      } else {
        tprintf("AWS TLS FAILED (heap: %u)\n", ESP.getFreeHeap());
#if ENABLE_WIND
        // Reinit BLE and reconnect to wind sensor
        if (config.wind_enabled && strlen(config.wind_mac) > 0) {
          tprintln("Reinitializing wind sensor...");
          initWindSensor();
        }
#endif
        return;
      }

      tprintln("Calling uploadDirectory...");
      yield();
      delay(100);
      uploadDirectory("/sf");
      tprintln("Upload complete");
#if ENABLE_WIND
      // Reinitialize BLE and reconnect to wind sensor
      if (config.wind_enabled && strlen(config.wind_mac) > 0) {
        tprintln("Reinitializing wind sensor...");
        initWindSensor();
      }
#endif
    } else {
#if ENABLE_WIND
      // WiFi failed, reinit BLE
      if (config.wind_enabled && strlen(config.wind_mac) > 0) {
        tprintln("WiFi failed, reinitializing wind sensor...");
        initWindSensor();
      }
#endif
    }

  } else if (cmd == "wifi") {
    if (wifiConnected) {
      tprintf("Already connected to %s\n", connectedSSID);
      tprintf("IP: %s\n", WiFi.localIP().toString().c_str());
    } else {
      tprintln("Connecting to WiFi...");
      if (connectWiFi()) {
        tprintf("Connected to %s\n", connectedSSID);
        tprintf("IP: %s\n", WiFi.localIP().toString().c_str());
      } else {
        tprintln("WiFi connection failed");
      }
    }

  } else if (cmd == "disconnect") {
    if (wifiConnected) {
      WiFi.disconnect(true);
      wifiConnected = false;
      connectedSSID[0] = '\0';
      tprintln("WiFi disconnected");
    } else {
      tprintln("Not connected");
    }

  } else if (cmd == "reboot") {
    tprintln("Rebooting...");
    delay(500);
    ESP.restart();

  } else if (cmd == "gps") {
    tprintln("=== GPS Details ===");
    tprintf("Fix: %s (quality %d)\n", gps.valid ? "YES" : "NO", gps.fix_quality);
    tprintf("Satellites in fix: %d\n", gps.satellites);
    tprintf("Satellites in view: %d (GP:%d GL:%d GA:%d GB:%d)\n",
            satsInView, gsvGP, gsvGL, gsvGA, gsvGB);
    if (satsInView < gps.satellites) {
      tprintln("  NOTE: GLGSV/GAGSV messages may not be enabled");
    }
    tprintf("HDOP: %.1f%s\n", gps.hdop, gps.hdop > 50 ? " (no data)" : "");
    tprintf("Position: %.8f, %.8f\n", gps.lat, gps.lon);
    tprintf("Altitude: %.1f m\n", gps.alt);
    tprintf("Speed: %.2f kt\n", gps.speed_kts);
    tprintf("Course: %.1f deg\n", gps.course);
    tprintf("UTC: %s\n", gps.utc_time);
    tprintf("Date: %s\n", gps.date);
    tprintln("===================");

  } else if (cmd == "imu") {
    tprintln("=== IMU Details ===");
    tprintf("Type: %s\n", imuOK ? (useIMU_BNO ? "BNO085" : "MPU6050") : "NONE");
    tprintf("Heading: %.0f deg (magnetic)\n", imu.heading);
    tprintf("Heel: %.1f deg (starboard +, port -)\n", imu.heel);
    tprintf("Pitch: %.1f deg (bow up +, bow down -)\n", imu.pitch);
    tprintf("Accel: X=%.2f Y=%.2f Z=%.2f\n", imu.accel_x, imu.accel_y, imu.accel_z);
    tprintf("Calibration offsets: heel=%.1f, pitch=%.1f\n", imuHeelOffset, imuPitchOffset);
    tprintln("===================");

  } else if (cmd == "imutest") {
    tprintln("=== IMU Axis Test (10 seconds) ===");
    tprintln("Tilt the device and watch which values change:");
    tprintln("  - Heel should change when tilting PORT/STARBOARD");
    tprintln("  - Pitch should change when tilting BOW UP/DOWN");
    tprintln("");
    unsigned long start = millis();
    while (millis() - start < 10000) {
      readIMU();
      // Show raw values (before calibration offset)
      float rawHeel = imu.heel + imuHeelOffset;
      float rawPitch = imu.pitch + imuPitchOffset;
      tprintf("H:%+6.1f P:%+6.1f  Accel X:%+5.2f Y:%+5.2f Z:%+5.2f\r",
        rawHeel, rawPitch, imu.accel_x, imu.accel_y, imu.accel_z);
      delay(200);
      yield();
    }
    tprintln("\n=== Test complete ===");
    tprintln("If axes are wrong, note which accel axis changes for each tilt");

  } else if (cmd == "cal" || cmd == "calibrate") {
    tprintln("=== IMU Calibration ===");
    tprintln("Place boat level on flat surface");
    tprintln("Current readings:");
    tprintf("  Heel: %.1f, Pitch: %.1f\n", imu.heel, imu.pitch);
    tprintln("Setting current position as zero...");
    calibrateIMU();
    tprintln("Calibration saved to SD card");
    tprintf("New offsets: heel=%.1f, pitch=%.1f\n", imuHeelOffset, imuPitchOffset);
    tprintln("=======================");

  } else if (cmd == "calreset") {
    tprintln("Resetting IMU calibration to defaults...");
    imuHeelOffset = 0.0;
    imuPitchOffset = 0.0;
    saveIMUCalibration();
    tprintln("Calibration reset to zero");

  } else if (cmd == "cleanup" || cmd == "delup") {
    if (!sdOK) {
      tprintln("SD card not available");
      return;
    }
    tprintln("Deleting uploaded files...");
    int deleted = deleteUploadedFiles("/sf");
    tprintf("Deleted %d files\n", deleted);

  } else if (cmd == "clearmarkers") {
    if (!sdOK) {
      tprintln("SD card not available");
      return;
    }
    tprintln("Clearing .uploaded markers (keeping data files)...");
    int count = 0;
    // Clear markers in all session directories
    File root = SD.open("/sf");
    if (root) {
      File dir = root.openNextFile();
      while (dir) {
        if (dir.isDirectory()) {
          String dirPath = String("/sf/") + dir.name();
          File subdir = SD.open(dirPath);
          if (subdir) {
            File f = subdir.openNextFile();
            while (f) {
              String fname = String(f.name());
              f.close();
              if (fname.endsWith(".uploaded")) {
                String fullPath = dirPath + "/" + fname;
                if (SD.remove(fullPath.c_str())) {
                  count++;
                }
              }
              f = subdir.openNextFile();
            }
            subdir.close();
          }
        }
        dir.close();
        dir = root.openNextFile();
      }
      root.close();
    }
    // Also clear markers in /sf root
    File sfRoot = SD.open("/sf");
    if (sfRoot) {
      File f = sfRoot.openNextFile();
      while (f) {
        String fname = String(f.name());
        f.close();
        if (fname.endsWith(".uploaded")) {
          String fullPath = String("/sf/") + fname;
          if (SD.remove(fullPath.c_str())) {
            count++;
          }
        }
        f = sfRoot.openNextFile();
      }
      sfRoot.close();
    }
    tprintf("Cleared %d marker files\n", count);

  } else if (cmd == "gpsraw") {
    tprintln("=== Raw GPS data (10 seconds) ===");
    tprintln("Press any key to stop early...");
    unsigned long start = millis();
    while (millis() - start < 10000) {
      while (Serial2.available()) {
        char c = Serial2.read();
        if (c >= 32 || c == '\n' || c == '\r') {
          Serial.print(c);
          if (telnetClient && telnetClient.connected()) {
            telnetClient.print(c);
          }
        }
      }
      // Check for keypress to stop
      if (Serial.available() || (telnetClient && telnetClient.available())) {
        while (Serial.available()) Serial.read();
        while (telnetClient && telnetClient.available()) telnetClient.read();
        break;
      }
      yield();
    }
    tprintln("\n=== End raw GPS ===");

  } else if (cmd == "gpscfg") {
    tprintln("Reconfiguring GPS...");
    configureLG290P();
    tprintln("GPS reconfigured");

  } else if (cmd == "wind") {
#if ENABLE_WIND
    tprintln("=== Wind Sensor ===");
    tprintf("Enabled: %s\n", config.wind_enabled ? "yes" : "no");
    tprintf("Connected: %s\n", wind.connected ? "yes" : "no");
    if (strlen(wind.deviceName) > 0) {
      tprintf("Device: %s (%s)\n", wind.deviceName, wind.deviceAddr);
    }
    if (wind.connected) {
      tprintf("Speed: %.1f kts (%.1f m/s)\n", wind.speed_kts, wind.speed_mps);
      tprintf("Direction: %d deg (apparent)\n", wind.angle_deg);
      if (wind.battery >= 0) {
        tprintf("Battery: %d%%\n", wind.battery);
      }
      tprintf("Last update: %lu ms ago\n", millis() - wind.lastUpdate);
    }
    if (strlen(config.wind_mac) > 0) {
      tprintf("Saved MAC: %s\n", config.wind_mac);
    }
    tprintln("===================");
#else
    tprintln("Wind sensor support not compiled in");
#endif

  } else if (cmd == "windscan") {
#if ENABLE_WIND
    tprintln("Scanning for Calypso wind sensors...");
    if (scanForCalypso()) {
      tprintf("Found: %s at %s\n", wind.deviceName, wind.deviceAddr);
      tprintln("Attempting connection...");
      if (connectToCalypso()) {
        tprintln("Connected successfully!");
      } else {
        tprintln("Connection failed");
      }
    } else {
      tprintln("No Calypso device found");
    }
#else
    tprintln("Wind sensor support not compiled in");
#endif

  } else if (cmd == "blescan") {
#if ENABLE_WIND
    tprintln("BLE scan (5 sec)...");
    NimBLEScan* pScan = NimBLEDevice::getScan();
    pScan->setActiveScan(true);
    pScan->clearResults();
    pScan->start(5, false);
    delay(6000);
    pScan->stop();
    NimBLEScanResults r = pScan->getResults();
    tprintf("Found %d\n", r.getCount());
    for (int i = 0; i < r.getCount(); i++) {
      const NimBLEAdvertisedDevice* d = r.getDevice(i);
      if (d) tprintf("%s %s\n", d->getAddress().toString().c_str(), d->getName().c_str());
    }
#else
    tprintln("No BLE");
#endif

  } else if (cmd == "bledeinit") {
#if ENABLE_WIND
    if (!bleInitialized) {
      tprintln("BLE not initialized");
      return;
    }
    tprintln("Deinitializing BLE to free memory...");
    tprintf("Heap before: %u bytes\n", ESP.getFreeHeap());
    if (pWindClient && pWindClient->isConnected()) {
      pWindClient->disconnect();
    }
    pWindClient = nullptr;
    pWindSpeedChar = nullptr;
    pWindDirChar = nullptr;
    pBatteryChar = nullptr;
    wind.connected = false;
    NimBLEDevice::deinit(true);
    bleInitialized = false;
    delay(500);
    tprintf("Heap after: %u bytes\n", ESP.getFreeHeap());
    tprintln("BLE disabled. Run 'bleinit' to restart.");
#else
    tprintln("No BLE");
#endif

  } else if (cmd == "bleinit") {
#if ENABLE_WIND
    tprintln("Reinitializing BLE...");
    tprintf("Heap before: %u bytes\n", ESP.getFreeHeap());
    if (bleInitialized) {
      tprintln("Deinitializing first...");
      NimBLEDevice::deinit(true);
      bleInitialized = false;
      delay(500);
    }
    tprintln("Calling NimBLEDevice::init()...");
    NimBLEDevice::init("SailFrames-E1");
    bleInitialized = true;
    NimBLEDevice::setPower(ESP_PWR_LVL_P9);
    tprintf("BLE address: %s\n", NimBLEDevice::getAddress().toString().c_str());
    tprintf("Heap after: %u bytes\n", ESP.getFreeHeap());
    tprintln("Done. Try 'blescan' now.");
#else
    tprintln("BLE not compiled in");
#endif

  } else if (cmd.startsWith("bleconnect ")) {
#if ENABLE_WIND
    String mac = cmd.substring(11);
    mac.trim();
    tprintf("Connecting to %s...\n", mac.c_str());
    strncpy(wind.deviceAddr, mac.c_str(), sizeof(wind.deviceAddr) - 1);
    strncpy(config.wind_mac, mac.c_str(), sizeof(config.wind_mac) - 1);
    if (connectToCalypso()) {
      tprintln("Connected! Saving MAC.");
      saveWindMAC(mac.c_str());
    } else {
      tprintln("Connection failed");
    }
#else
    tprintln("No BLE");
#endif

  } else if (cmd == "display") {
    displayMode = (displayMode == 1) ? 2 : 1;
    tprintf("Display mode: D%d\n", displayMode);
    updateDisplay();

  } else if (cmd == "heap") {
    tprintln("=== Memory Status ===");
    tprintf("Free heap: %u bytes\n", ESP.getFreeHeap());
    tprintf("Min free heap: %u bytes\n", ESP.getMinFreeHeap());
    tprintf("Max alloc heap: %u bytes\n", ESP.getMaxAllocHeap());
    tprintf("PSRAM: %u bytes free\n", ESP.getFreePsram());
    tprintf("Sketch size: %u bytes\n", ESP.getSketchSize());
    tprintf("Free sketch space: %u bytes\n", ESP.getFreeSketchSpace());
    tprintln("SSL needs ~45KB free heap");

  } else if (cmd == "testssl") {
    tprintf("Free heap before test: %u bytes\n", ESP.getFreeHeap());
    tprintln("Testing SSL to google.com:443...");
    WiFiClientSecure client;
    client.setInsecure();
    client.setTimeout(10);  // 10 second timeout
    if (client.connect("www.google.com", 443)) {
      tprintln("Google SSL OK!");
      client.stop();
    } else {
      tprintln("Google SSL FAILED");
    }

    tprintln("Testing SSL to AWS API Gateway...");
    WiFiClientSecure client2;
    client2.setInsecure();
    client2.setTimeout(10);
    if (client2.connect("p9s9eia0t6.execute-api.us-east-1.amazonaws.com", 443)) {
      tprintln("AWS SSL OK!");
      client2.println("PUT /prod/upload?boat=E1&file=test.txt HTTP/1.1");
      client2.println("Host: p9s9eia0t6.execute-api.us-east-1.amazonaws.com");
      client2.println("Content-Type: text/plain");
      client2.println("Content-Length: 4");
      client2.println("Connection: close");
      client2.println();
      client2.print("test");
      delay(2000);
      while (client2.available()) {
        String line = client2.readStringUntil('\n');
        tprintf("%s\n", line.c_str());
      }
      client2.stop();
    } else {
      tprintln("AWS SSL FAILED");
      char errBuf[128];
      client2.lastError(errBuf, sizeof(errBuf));
      tprintf("Error: %s\n", errBuf);
    }

    tprintln("Testing plain HTTP to httpbin...");
    WiFiClient client3;
    if (client3.connect("httpbin.org", 80)) {
      tprintln("HTTP connected!");
      client3.println("GET /get HTTP/1.1");
      client3.println("Host: httpbin.org");
      client3.println("Connection: close");
      client3.println();
      delay(1000);
      int lines = 0;
      while (client3.available() && lines < 5) {
        String line = client3.readStringUntil('\n');
        tprintf("%s\n", line.c_str());
        lines++;
      }
      client3.stop();
    } else {
      tprintln("HTTP FAILED");
    }

  } else if (cmd == "rec" || cmd == "startrec") {
    // Manual start recording
    if (logging) {
      tprintln("Already recording");
    } else if (!sdOK) {
      tprintln("SD card not available");
    } else {
      tprintln("Starting recording manually...");
      sessionCount++;
      if (xSemaphoreTake(sdMutex, pdMS_TO_TICKS(1000))) {
        startLogging();
        xSemaphoreGive(sdMutex);
      }
      recState = REC_RECORDING;
      tprintf("Recording session %d started\n", sessionCount);
    }

  } else if (cmd == "stoprec") {
    // Manual stop recording
    if (!logging) {
      tprintln("Not recording");
    } else {
      tprintln("Stopping recording...");
      if (xSemaphoreTake(sdMutex, pdMS_TO_TICKS(1000))) {
        navFile.flush(); navFile.close();
        imuFile.flush(); imuFile.close();
        rawFile.flush(); rawFile.close();
        if (windFile) { windFile.flush(); windFile.close(); }
        xSemaphoreGive(sdMutex);
      }
      logging = false;
      recState = REC_IDLE;
      tprintf("Recording session %d stopped\n", sessionCount);
    }

  } else if (cmd == "recstate") {
    // Show recording state
    tprintln("=== Recording State ===");
    tprintf("State: %s\n", getRecStateStr());
    tprintf("Logging: %s\n", logging ? "YES" : "NO");
    tprintf("Session: %d\n", sessionCount);
    tprintf("Speed: %.1f kt\n", gps.speed_kts);
    tprintf("Start threshold: >%.1f kt for %d sec\n", config.start_speed_knots, config.start_delay_sec);
    tprintf("Stop threshold: <%.1f kt for %d sec\n", config.stop_speed_knots, config.stop_delay_sec);

  } else if (cmd == "help") {
    tprintln("=== Commands ===");
    tprintln("  status     - Show device status");
    tprintln("  gps        - Detailed GPS info");
    tprintln("  gpsraw     - Show raw GPS serial data");
    tprintln("  gpscfg     - Reconfigure GPS module");
    tprintln("  imu        - Detailed IMU info");
    tprintln("  imutest    - Test IMU axes (5 sec)");
    tprintln("  cal        - Calibrate IMU (set level)");
    tprintln("  calreset   - Reset IMU calibration");
    tprintln("  rec        - Manual start recording");
    tprintln("  stoprec    - Manual stop recording");
    tprintln("  recstate   - Show recording state");
    tprintln("  wind       - Wind sensor info");
    tprintln("  windscan   - Scan for wind sensor");
    tprintln("  blescan    - Scan ALL BLE devices");
    tprintln("  bledeinit  - Deinit BLE (free memory)");
    tprintln("  bleinit    - Reinitialize BLE");
    tprintln("  bleconnect <mac> - Connect to BLE MAC");
    tprintln("  display    - Toggle display mode (D1/D2)");
    tprintln("  heap       - Show memory status");
    tprintln("  testssl    - Test SSL connection");
    tprintln("  ls, list   - List SD card files");
    tprintln("  cat <file> - Show file contents");
    tprintln("  upload     - Manual upload to S3");
    tprintln("  cleanup    - Delete uploaded files");
    tprintln("  wifi       - Connect to WiFi");
    tprintln("  disconnect - Disconnect WiFi");
    tprintln("  reboot     - Restart device");
    tprintln("  help       - Show this help");
    tprintln("================");

  } else {
    tprintf("Unknown command: %s (type 'help')\n", cmd.c_str());
  }
}

void handleSerialCommand() {
  if (!Serial.available()) return;

  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  if (cmd.length() == 0) return;

  Serial.printf("\n> %s\n", cmd.c_str());
  processCommand(cmd, false);
}

// ============================================================
// GPS SPEED-TRIGGERED RECORDING STATE MACHINE
// ============================================================
// Power control: Use hardware slide switch on PowerBoost EN pin
// No software shutdown - hardware switch cuts all power
void updateRecordingState() {
  float speed = gps.speed_kts;
  unsigned long now = millis();

  // Use config values
  float startThresh = config.start_speed_knots;
  float stopThresh = config.stop_speed_knots;
  unsigned long startDelay = config.start_delay_sec * 1000UL;
  unsigned long stopDelay = config.stop_delay_sec * 1000UL;

  switch (recState) {
    case REC_IDLE:
      if (gps.valid && speed > startThresh) {
        recState = REC_ARMED;
        armStartTime = now;
        Serial.printf("[REC] Arming... speed=%.1f kt\n", speed);
      }
      break;

    case REC_ARMED:
      if (speed <= startThresh) {
        // Speed dropped, reset
        recState = REC_IDLE;
        Serial.println("[REC] Speed dropped, back to idle");
      } else if (now - armStartTime >= startDelay) {
        // Sustained speed — start recording
        sessionCount++;
        if (xSemaphoreTake(sdMutex, pdMS_TO_TICKS(1000))) {
          startLogging();
          xSemaphoreGive(sdMutex);
        }
        recState = REC_RECORDING;
        Serial.printf("[REC] Recording STARTED — session %d\n", sessionCount);
      }
      break;

    case REC_RECORDING:
      if (speed < stopThresh) {
        recState = REC_STOPPING;
        stopStartTime = now;
        Serial.printf("[REC] Speed low, stopping timer started... speed=%.1f kt\n", speed);
      }
      break;

    case REC_STOPPING:
      if (speed >= stopThresh) {
        // Speed picked up, keep recording
        recState = REC_RECORDING;
        Serial.println("[REC] Speed recovered, continuing recording");
      } else if (now - stopStartTime >= stopDelay) {
        // Sustained slow — stop recording
        if (xSemaphoreTake(sdMutex, pdMS_TO_TICKS(1000))) {
          navFile.flush(); navFile.close();
          imuFile.flush(); imuFile.close();
          rawFile.flush(); rawFile.close();
          if (windFile) { windFile.flush(); windFile.close(); }
          xSemaphoreGive(sdMutex);
        }
        logging = false;
        recState = REC_IDLE;
        Serial.printf("[REC] Recording STOPPED — session %d complete\n", sessionCount);

        // Trigger Wi-Fi upload of completed files (on Core 0)
        triggerUpload = true;
      }
      break;
  }
}

const char* getRecStateStr() {
  switch (recState) {
    case REC_IDLE: return gps.valid ? "READY" : "NO GPS";
    case REC_ARMED: return "ARMING";
    case REC_RECORDING: return "REC";
    case REC_STOPPING: return "STOPPING";
    default: return "?";
  }
}

// ============================================================
// UPLOAD TASK (RUNS ON CORE 0)
// ============================================================
void uploadTaskFunc(void* param) {
  Serial.println("[UPLOAD] Task started on Core 0");

  while (true) {
    if (triggerUpload && !logging) {
      triggerUpload = false;
      Serial.println("[UPLOAD] Upload triggered, attempting connection...");

      // Try to connect to WiFi
      if (!wifiConnected) {
        connectWiFi();
      }

      if (wifiConnected) {
        Serial.printf("[UPLOAD] Connected, heap: %u bytes\n", ESP.getFreeHeap());

        // Count and upload files
        if (xSemaphoreTake(sdMutex, pdMS_TO_TICKS(5000))) {
          uploadDirectory("/sf");
          xSemaphoreGive(sdMutex);
        }
        Serial.println("[UPLOAD] Upload complete");
      } else {
        Serial.println("[UPLOAD] WiFi connection failed");
      }
    }

    vTaskDelay(pdMS_TO_TICKS(5000));  // Check every 5 seconds
  }
}

// ============================================================
// MAIN LOOP
// ============================================================
void loop() {
  unsigned long now = millis();

  // Handle OTA updates (highest priority)
  if (wifiConnected) {
    ArduinoOTA.handle();
    if (otaInProgress) return;  // Don't do anything else during OTA

    // Handle telnet connections
    handleTelnet();
  }

  // Check for serial commands
  handleSerialCommand();

  readGPS();

  // Update GPS speed-triggered recording state machine
  updateRecordingState();

  if (now - lastIMU >= IMU_INTERVAL_MS) {
    readIMU();
    if (logging) logIMU();
    lastIMU = now;
  }

  if (logging && gps.newGGA) {
    logNav();
    gps.newGGA = false;
  }

#if ENABLE_WIND
  // Handle wind sensor
  if (config.wind_enabled) {
    // Check connection and reconnect if needed
    checkWindConnection();

    // Log wind data at configured interval
    if (logging && now - lastWind >= WIND_INTERVAL_MS) {
      logWind();
      lastWind = now;
    }
  }
#endif

  if (now - lastDisp >= DISPLAY_UPDATE_MS) {
    if (!uploading) {  // Don't override upload display
      updateDisplay();
    }
    if (logging && LED_PIN >= 0) digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    lastDisp = now;
  }

  if (logging && now - lastFlush >= FLUSH_INTERVAL_MS) {
    navFile.flush();
    if (imuFile) imuFile.flush();
    rawFile.flush();
#if ENABLE_WIND
    if (windFile) windFile.flush();
#endif
    lastFlush = now;
  }

  // Auto WiFi upload check (every 60 seconds when not already connected)
  static unsigned long lastWifiCheck = 0;
  if (!wifiConnected && now - lastWifiCheck >= 60000) {
    checkWiFiUpload();
    lastWifiCheck = now;
  }

  // Battery monitoring (every 10 seconds)
  static unsigned long lastBattCheck = 0;
  if (now - lastBattCheck >= 10000) {
    updateBattery();
    handleLowBattery();  // Will warn and halt if critical
    lastBattCheck = now;
  }
}
