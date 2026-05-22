/*
 * SailFrames E1 — Fleet Tracker Firmware
 *
 * Hardware:
 *   - ESP32 DevKit V1 (ELEGOO)
 *   - Waveshare LG290P GNSS (UART2: RX=GPIO16, TX=GPIO17, 460800 baud)
 *   - BNO085 IMU (I2C: 0x4A) — heel, pitch, heading
 *   - DPS310 Pressure/Temp (I2C: 0x77) — barometric pressure for gust detection
 *   - Hosyond 3.5" IPS ST7796U TFT 480x320 (SPI: CS=5, DC=2, RST=4, BL=25)
 *   - MicroSD standalone module (SPI shared: MOSI=23, MISO=19, CLK=18, CS=27)
 *   - Calypso Mini wind sensor (BLE) — apparent wind speed/direction
 *   - DWEII USB-C 5V Boost Converter + LiPo cell
 *   - 100K/100K voltage divider on GPIO34 for battery monitoring
 *
 * Behavior:
 *   Power on → init sensors → configure LG290P (Rover mode, 10 Hz NMEA)
 *   → scan for Calypso wind sensor (BLE) → wait for GPS fix
 *   → auto-log to SD (NMEA CSV + IMU CSV + Wind CSV + Pres CSV)
 *   → when yacht club Wi-Fi detected → auto-upload to AWS S3
 *   Power off → done
 *
 * NOTE: PPK / raw-RTCM3 capture was retired in firmware 2026.05.20.09.
 * See docs/RTCM_PPK_ARCHIVE.md for the previous architecture and
 * git SHA 08cdadfe for the last firmware revision that wrote .rtcm3.
 *
 * Log files per session:
 *   /sf/YYYYMMDD/E1_YYYYMMDD_HHMMSS_nav.csv  (10 Hz)
 *   /sf/YYYYMMDD/E1_YYYYMMDD_HHMMSS_imu.csv  (10 Hz)
 *   /sf/YYYYMMDD/E1_YYYYMMDD_HHMMSS_wind.csv (1 Hz when Calypso paired)
 *   /sf/YYYYMMDD/E1_YYYYMMDD_HHMMSS_pres.csv (0.1 Hz, DPS310 only)
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
#include <Update.h>
#include <HTTPClient.h>
#include "mbedtls/sha256.h"
#include "User_Setup.h"  // TFT_eSPI config (must be before TFT_eSPI.h)
#include <TFT_eSPI.h>
#include <Adafruit_BNO08x.h>
#include <Adafruit_DPS310.h>
#include <esp_heap_caps.h>
#include <esp_task_wdt.h>
// NimBLE configuration - disable unused features to reduce size
#define CONFIG_BT_NIMBLE_ROLE_CENTRAL 1
#define CONFIG_BT_NIMBLE_ROLE_PERIPHERAL 0
#define CONFIG_BT_NIMBLE_ROLE_OBSERVER 1
#define CONFIG_BT_NIMBLE_ROLE_BROADCASTER 0
#define CONFIG_BT_NIMBLE_MAX_CONNECTIONS 1
#define CONFIG_BT_NIMBLE_MAX_BONDS 1
#define CONFIG_BT_NIMBLE_SVC_GAP_DEVICE_NAME "SailFrames-E1"
#include <NimBLEDevice.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <string>
#include "v2_types.h"  // v2.0.0 foundation: HardwarePlatform/UnitRole/RadioMode enums
#include "mesh.h"      // v2.0.0 Stage 2: ESP-NOW peer-mesh wire types

// ============================================================
// PIN DEFINITIONS
// ============================================================
#define GPS_RX_PIN    16
#define GPS_TX_PIN    17
#define SDA_PIN       21
#define SCL_PIN       22

// TFT Display (Hosyond 3.5" IPS ST7796U) - SPI
#define TFT_CS_PIN    5   // LCD chip select
#define TFT_DC_PIN    2   // Data/Command (also used as LED_PIN on old board)
#define TFT_RST_PIN   4   // LCD reset
#define TFT_BL_PIN    25  // Backlight PWM

// SD Card - standalone module on SEPARATE HSPI bus (eliminates TFT flicker)
// HSPI pins - completely independent from TFT's VSPI bus
// NOTE: GPIO12 is a strapping pin - avoid it! Using GPIO35 for MISO instead
#define SD_CS_PIN     27  // SD card CS (GPIO27)
#define SD_CLK_PIN    14  // HSPI CLK
#define SD_MISO_PIN   35  // MISO on GPIO35 (input-only pin, perfect for MISO)
#define SD_MOSI_PIN   13  // HSPI MOSI

// LED_PIN disabled - was causing backlight to blink during logging
#define LED_PIN       -1  // Set to -1 to disable LED blinking

// Battery monitoring (DWEII USB-C Boost Converter)
// 100K/100K voltage divider from LiPo B+ to GPIO34
#define BATT_VOLTAGE_PIN  34   // ADC pin for voltage divider (input-only, no pullup)
// GPIO35 is now free for future use

// Power control: Hardware switch on boost converter
// No software deep sleep - hardware switch cuts all power when OFF

// ============================================================
// CONFIGURATION
// ============================================================
// Firmware version: YYYY.MM.DD.N (date + daily build number)
#define FW_VERSION    "2026.05.22.01"
// v2.0.0 foundation: HW platform / unit role / radio mode skeleton.
// 10 Hz GNSS + 10 Hz IMU are now baked-in firmware defaults (no longer
// per-boat config knobs). config.txt holds per-boat / per-club state
// only (WiFi creds, boat_id, wind sensor, role, etc.).

// Telnet listener is OFF by default. The 2026.05.03.04 fleet test confirmed
// (via diag heartbeat) that handleTelnet() blocks Core 1 inside LWIP when
// Core 0 is doing concurrent HTTP uploads — even with the wifiBusy gate,
// because Core 1 may already be INSIDE handleTelnet when the upload fires
// and the gate only prevents new entries. Easiest robust fix: don't run
// the listener during automated post-sail uploads. Set telnetEnabled=true
// at runtime via the serial 'telneton' command if you need to debug live.
#define TELNET_ENABLED_DEFAULT  false

// ArduinoOTA registers an mDNS multicast UDP listener. On ESP32 Arduino
// Core 3.3.7 with NimBLE active for the wind sensor, the mDNS init at
// WiFi-up time crosses into the BLE/WiFi shared-radio coexistence path
// and panics with "spinlock_release ... core_owner_id == lock->owner"
// (firmware 2026.05.02.04 fleet test). The user does not currently use
// ArduinoOTA — fleet firmware is flashed via USB, and Phase 2 OTA will
// pull binaries via Update.h on a manifest GET (no passive listener).
// Disable until we either (a) move off NimBLE, (b) add a deinit-before-
// connect dance that's known to be safe on 3.3.7, or (c) replace
// ArduinoOTA with a manifest-pull update that doesn't touch mDNS.
#define ENABLE_ARDUINO_OTA  0

// Home WiFi SSID. Boats prefer this network when in range. OTA pull
// is gated to this SSID — see performOTAUpdate. (Previously also gated
// the now-retired .rtcm3 PPK upload path.)
#define HOME_WIFI_SSID "Home-IOT"

#define GPS_BAUD      460800  // LG290P configured rate
#define SERIAL_BAUD   115200
#define SCREEN_WIDTH  320     // TFT portrait width
#define SCREEN_HEIGHT 480     // TFT portrait height
#define BNO085_ADDR   0x4B    // GY-BNO08X breakout (ADO pin high)
#define DPS310_ADDR   0x77    // Pressure/temperature sensor
#define GPS_FIX_TIMEOUT_MS  300000
#define DISPLAY_UPDATE_MS   500   // TFT can handle faster updates (no I2C contention)
#define FLUSH_INTERVAL_MS   10000
#define IMU_INTERVAL_MS     100    // 10 Hz BNO085 reports. Baked-in fleet default.
#define PRES_INTERVAL_MS    10000  // 0.1 Hz (every 10 sec - weather trends only)

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

// Calypso Data Service (0x180D) - combined wind+battery in single notification
// Format: [speed_lo, speed_hi, dir_lo, dir_hi, battery] where battery * 10 = %
static NimBLEUUID DATA_SERVICE_UUID("180D");
static NimBLEUUID DATA_CHAR_UUID("2A39");            // Combined wind+battery (5 bytes)

// Device Information Service (0x180A) - for reading firmware version
static NimBLEUUID DEVINFO_SERVICE_UUID("180A");
static NimBLEUUID FIRMWARE_CHAR_UUID("2A26");        // Firmware Revision String
// ============================================================
// NMEA CHECKSUM + PQTM SENDER
// ============================================================
bool sendPQTM(const char* body) {
  uint8_t cs = 0;
  for (int i = 0; body[i] != '\0'; i++) cs ^= body[i];
  char buf[128];
  snprintf(buf, sizeof(buf), "$%s*%02X\r\n", body, cs);

  // Flush any pending data before sending
  while (Serial2.available()) Serial2.read();

  Serial2.print(buf);
  Serial.printf("[CMD] %s", buf);

  // Wait longer for response (some commands take time)
  delay(100);

  // Read all response lines (may be multiple)
  char resp[256];
  int idx = 0;
  int lineCount = 0;
  bool gotOK = false;
  bool gotError = false;
  unsigned long start = millis();

  while (millis() - start < 500 && lineCount < 3) {
    if (Serial2.available()) {
      char c = Serial2.read();
      if (c == '\n') {
        if (idx > 0) {
          resp[idx] = '\0';
          // Check for PQTM response (not NMEA)
          if (resp[0] == '$' && resp[1] == 'P') {
            Serial.printf("[RSP] %s\n", resp);
            // Check for error response
            if (strstr(resp, "ERROR") || strstr(resp, "NACK")) {
              Serial.printf("[GPS] FAILED: %s\n", body);
              gotError = true;
            } else if (strstr(resp, "OK") || strstr(resp, "PQTM")) {
              gotOK = true;
            }
          }
          idx = 0;
          lineCount++;
        }
      } else if (c != '\r' && idx < (int)sizeof(resp) - 1) {
        resp[idx++] = c;
      }
    }
  }

  // Show if no response received
  if (lineCount == 0) {
    Serial.printf("[RSP] (no response for: %s)\n", body);
  }

  return gotOK && !gotError;
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
  float accel_x = 0, accel_y = 0, accel_z = 0;       // Raw acceleration (includes gravity)
  float gyro_x = 0, gyro_y = 0, gyro_z = 0;          // Angular velocity (deg/s)
  float linaccel_x = 0, linaccel_y = 0, linaccel_z = 0;  // Linear acceleration (gravity removed)
  float mag_x = 0, mag_y = 0, mag_z = 0;             // Magnetic field (uTesla) for interference analysis
  float heel = 0, pitch = 0, heading = 0;
  uint8_t stability = 0;    // 0=Unknown, 1=OnTable, 2=Stationary, 3=Stable, 4=Motion
  uint8_t accuracy = 0;     // Rotation vector accuracy (0-3, 3=highest)
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
  char firmware[16] = "";   // Firmware version from Device Information Service
} wind;

struct BatteryData {
  float voltage = 0;        // Battery voltage (3.0-4.2V for LiPo)
  int percent = 0;          // Estimated percentage (0-100)
  bool critical = false;    // Voltage below 3.3V (overdischarge threshold)
  bool valid = false;       // Have we read the battery yet?
  unsigned long lastRead = 0;
} battery;

struct PressureData {
  float pressure_hpa = 0;   // Barometric pressure in hPa (mbar)
  float temperature_c = 0;  // Temperature in Celsius
  float pressure_min = 9999; // Min pressure in current window (for gust detection)
  float pressure_max = 0;   // Max pressure in current window
  bool valid = false;
  unsigned long lastRead = 0;
} pressure;

// RTCM3 byte parser + per-message-type counters were removed in
// firmware 2026.05.20.09 alongside the rest of the PPK pipeline.
// See docs/RTCM_PPK_ARCHIVE.md and git SHA 08cdadfe for the previous
// implementation.

// v2.0.0 foundation globals. Types live in v2_types.h (included at top
// of file) so Arduino's auto-generated forward declarations can resolve
// them.
HardwarePlatform g_hw = HW_E1;
UnitRole         g_role = ROLE_RACING_BOAT;
RadioMode        g_radio_mode = MODE_BOOT;

// v2.0.0 Stage 2 — ESP-NOW peer mesh state
// MVP: always-on after WiFi PHY init, gated off only during HTTP uploads
// via wifiBusy. Radio-mode integration (init only in MODE_RACING/_RC_ACTIVE
// per spec) deferred to a later stage once mode transitions actually fire.
#define MESH_CHANNEL                 1     // ESP-NOW broadcast channel (configurable in spec; hardcoded for MVP)
#define MESH_BROADCAST_INTERVAL_MS   500   // 2 Hz boat-state broadcast
#define MESH_PEER_MAX                32    // 25 boats + RC + marks + spares + headroom
#define MESH_PEER_EXPIRY_MS          30000 // drop peers we haven't heard from in 30 s

struct MeshPeerState {
    uint32_t sender_id;
    int32_t  last_lat_e7;
    int32_t  last_lon_e7;
    int16_t  last_sog_cm_s;
    int16_t  last_cog_deg10;
    int16_t  last_heading_deg10;   // Stage 5: from BoatStatePayload.heading_deg10
    int8_t   last_heel_deg;
    uint8_t  unit_role;
    uint8_t  fix_quality;
    uint8_t  sat_count;
    uint32_t last_seen_ms;
    uint32_t msg_count;
    uint16_t last_seq;
    // Stage 5 — RC-side OCS computation per peer.
    float    rc_distance_m;        // signed perpendicular distance from RC view
    bool     rc_ocs_called;        // RC has broadcast MSG_INDIVIDUAL_RECALL for this boat
    uint32_t rc_ocs_called_at_ms;
};

MeshPeerState   g_mesh_peers[MESH_PEER_MAX];
volatile int    g_mesh_peer_count = 0;
volatile uint16_t g_mesh_seq = 0;
volatile bool   g_mesh_enabled = false;
volatile uint32_t g_mesh_rx_count = 0;
volatile uint32_t g_mesh_tx_count = 0;
volatile uint32_t g_mesh_tx_fail_count = 0;
volatile uint32_t g_mesh_rx_dropped_bad_magic = 0;
unsigned long   g_mesh_last_broadcast = 0;
uint32_t        g_mesh_local_sender_id = 0;
static const uint8_t MESH_BROADCAST_ADDR[6] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};

#define MAX_WIFI_NETWORKS 5

struct WiFiNetwork {
  char ssid[64];
  char pass[64];
};

struct Config {
  WiFiNetwork wifi[MAX_WIFI_NETWORKS];
  int wifi_count = 0;
  char upload_url[256] = "https://p9s9eia0t6.execute-api.us-east-1.amazonaws.com/prod/upload";  // Legacy, not used
  char s3_bucket[128] = "sailframes-fleet-data-prod";
  char s3_region[32] = "us-east-1";
  char boat_id[16] = "E1";
  char wind_mac[20] = "";  // Calypso Mini MAC (loaded from /wind_mac.txt if present)
  bool wind_enabled = false;  // Auto-enabled if /wind_mac.txt exists on SD
  int wind_offset = 0;  // Heading offset in degrees (added to raw AWA for sensor mounting correction)
  // Recording thresholds
  float start_speed_knots = 1.5;
  float stop_speed_knots = 0.5;
  int start_delay_sec = 10;
  int stop_delay_sec = 180;

  // v2.0.0 foundation (SF_FIRMWARE_V2_SPEC.md Stage 1)
  char hardware_platform[8] = "e1";       // "e1" or "b1"
  char unit_role[24]        = "racing_boat";
  int  config_version       = 0;          // bumped by cloud config sync (Stage 3)
} config;

// ============================================================
// GLOBALS
// ============================================================
// TFT Display - Hosyond 3.5" IPS ST7796U (480x320, SPI)
// Using TFT_eSPI library with User_Setup.h configuration
TFT_eSPI tft = TFT_eSPI();

// Color scheme for sailing dashboard - WHITE background, BLACK numbers
#define COLOR_BG        TFT_WHITE
#define COLOR_TEXT      TFT_BLACK
#define COLOR_VALUE     TFT_CYAN
#define COLOR_LABEL     TFT_DARKGREY
#define COLOR_GOOD      TFT_GREEN
#define COLOR_WARN      TFT_YELLOW
#define COLOR_ERROR     TFT_RED
#define COLOR_DIVIDER   0x4208  // Dark gray
Adafruit_BNO08x bno08x;  // No hardware reset pin
Adafruit_DPS310 dps;         // DPS310 pressure/temperature sensor
sh2_SensorValue_t sensorValue;
File navFile, imuFile, windFile, presFile;
bool sdOK = false, imuOK = false, oledOK = false, presOK = false, logging = false;
volatile bool sdWriting = false;  // Flag to skip display updates during SD writes
unsigned long lastDisplayUpdate = 0;
const unsigned long DISPLAY_UPDATE_INTERVAL = 200;  // Only update display every 200ms
bool uploading = false;
int pendingUploads = 0;  // N: sessions with files still to upload
bool wifiConnected = false;
bool d2LayoutDrawn = false;  // Display layout flag - reset to redraw full screen
bool otaInProgress = false;
char connectedSSID[64] = "";
int uploadCount = 0, uploadTotal = 0;
int uploadSuccess = 0, uploadFailed = 0;
char uploadCurrentFile[32] = "";  // Short name of file being uploaded

// Get short WiFi indicator based on connected SSID
const char* getWifiIndicator() {
  if (strcmp(connectedSSID, "Home-IOT") == 0) return "Home";
  if (strcmp(connectedSSID, "paul") == 0) return "P";
  if (strlen(connectedSSID) > 0) return "WiFi";
  return "";
}
int satsInView = 0;
// GSV constellation counts (satellites in view per system)
int gsvGP = 0, gsvGL = 0, gsvGA = 0, gsvGB = 0, gsvGQ = 0, gsvGI = 0;
unsigned long lastValidGPS = 0;  // Track when we last had a valid fix

// IMU calibration offsets (stored on SD card)
float imuHeelOffset = 0.0;
float imuPitchOffset = 0.0;

// IMU health watchdog. The BNO085 lives on a 4-pin I²C header; if a
// header pin works loose (vibration), getSensorEvent() returns false
// every call and `imu.heel` keeps its last value (initial 0.0). The
// device happily writes 0.0 to the CSV for hours, indistinguishable
// downstream from "boat sat perfectly flat". E2 hit exactly this on
// 2026-05-12 — caught only retroactively via S3 forensics.
//
// Track consecutive readIMU() calls that returned ZERO events; flip
// the failed flag after IMU_FAIL_THRESHOLD_S of no data. While
// failed, logIMU() writes empty cells for heel/pitch/heading/accuracy
// instead of stale numbers so the dashboard's parseFloat returns NaN
// and the row is naturally skipped. boot.log gets a one-line marker
// at the moment of failure transition (and again on recovery), so
// future similar events are visible without S3 grepping.
#define IMU_FAIL_THRESHOLD_S 60     // 60 s of no events → failed
unsigned long g_imuLastEventMs = 0; // millis() of last successful read
int g_imuSilentReads = 0;           // consecutive readIMU() calls with 0 events
bool g_imuFailed = false;           // sticky failure flag

// Telnet server for remote console
WiFiServer telnetServer(23);
WiFiClient telnetClient;
bool telnetEnabled = TELNET_ENABLED_DEFAULT;
bool telnetServerRunning = false;
String telnetBuffer = "";
unsigned long logStart = 0, lastDisp = 0, lastFlush = 0, lastIMU = 0, lastWind = 0;
unsigned long lastWindScan = 0;

// BLE client for Calypso wind sensor
NimBLEClient* pWindClient = nullptr;
NimBLERemoteCharacteristic* pWindSpeedChar = nullptr;
NimBLERemoteCharacteristic* pWindDirChar = nullptr;
NimBLERemoteCharacteristic* pBatteryChar = nullptr;
NimBLERemoteCharacteristic* pDataChar = nullptr;      // Combined wind+battery (0x2A39)
bool windScanning = false;
bool windOK = false;
bool bleInitialized = false;  // Track BLE init state for safe deinit
unsigned long totalBytes = 0;
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
// Set by Core 0 (upload task) when uploads are done and WiFi should be released.
// Honored by Core 1 (main loop), which owns the OTA/telnet handlers and is the
// only safe place to tear down the WiFi stack.
volatile bool wifiTeardownRequested = false;
SemaphoreHandle_t sdMutex = NULL;
TaskHandle_t uploadTaskHandle = NULL;
TaskHandle_t diagTaskHandle = NULL;

// Where Core 1's main loop currently is. Set by the loop, read by the diag
// task. When Core 1 hangs, the last value here pinpoints the stuck section.
volatile const char* g_loopSection = "boot";
volatile uint32_t    g_loopIter = 0;

// --- Hang watchdogs (added in 2026.05.05.08) ---
//
// Diagnoses the 2026-05-05 16:10-EDT 3-of-6 fleet hang: auto-OTA called
// from uploadTaskFunc spent forever inside HTTPClient on Home-IOT
// reconnect. wifiBusy/uploading were left set, no reset reason was
// logged, only manual power cycle recovered the device. These two
// shared variables let diagnosticsTask convert any future stuck state
// into a recoverable `reset=SW` instead of a silent brick.
//
// g_otaDeadlineMs = 0 means no OTA in flight. Otherwise it's the
// absolute millis() value at which the diag task will force an
// esp_restart() if performOTAUpdate() hasn't returned. 180 s is enough
// for a clean 1.4 MB pull at ~10 KB/s on weak Home-IOT WiFi while
// still bounding the worst case.
//
// g_loopWatchdog* tracks Core 1's main loop progress. If g_loopIter
// hasn't moved for OVER_LOOP_HANG_MS the diag task forces a restart —
// catches any future hang in any code path, not just OTA.
volatile unsigned long g_otaDeadlineMs = 0;
// One-shot guard: auto-OTA runs at most ONCE per boot. The first
// successful upload cycle (or any other code path that calls
// performOTAUpdate without manual=true) flips this to true and every
// subsequent auto-trigger no-ops. The serial / telnet `update`
// command bypasses this with manual=true so an operator can always
// force a re-check without rebooting. Simpler model than the prior
// "OTA after every clean upload" — predictable, less network churn,
// no surprise reboots mid-day if a new build is published.
volatile bool g_otaCheckedThisBoot = false;
// 600 s ceiling: at -84 dBm WiFi (E5 in garage, 2026-05-21 OTA fail) the
// download throughput was ~4.7 KB/s — full 1.5 MB needs ~5.5 min. The
// original 180 s was tuned for typical signal and aborted weak-signal
// pulls at ~55%. The stall watchdog (OTA_STALL_MS) still catches real
// hangs (no bytes for 20 s); the hard deadline is just a backup for
// "data flowing but not making progress" pathologies.
static const unsigned long OTA_MAX_MS         = 600UL * 1000UL;  // hard ceiling per OTA cycle
static const unsigned long OTA_STALL_MS       =  20UL * 1000UL;  // abort if no bytes received for 20 s
static const unsigned long LOOP_HANG_MS       =  90UL * 1000UL;  // Core 1 must tick at least every 90 s

// boot.log session record is written once per power cycle, after the first
// valid GPS time + date arrives. The diagnostics task then appends an
// "alive" heartbeat every 5 min so a missing tail in the next session
// distinguishes battery-died / crashed from clean power-off.
bool g_bootSessionLogged = false;
unsigned long g_lastAliveLog = 0;

// True while Core 0 is mid-WiFi-work: WiFi scan, connect, upload cycle.
// Core 1 must NOT touch the WiFi/LWIP stack (handleTelnet, telnetServer,
// WiFi.* APIs other than fast-status reads) during this window — under
// sustained Core 0 traffic, especially with weak signal, LWIP mutex
// contention blocks Core 1 inside otherwise-cheap calls and hangs the
// device (firmware 2026.05.03.03 fleet test pinpointed this with a diag
// heartbeat: Core 1 was frozen inside handleTelnet for the entire
// upload phase).
volatile bool wifiBusy = false;

// Upload state tracking
unsigned long lastUploadCheck = 0;
const unsigned long UPLOAD_CHECK_INTERVAL_MS = 30000;  // Check every 30 seconds
int uploadRetryCount = 0;
const int MAX_UPLOAD_RETRIES = 5;  // More attempts before 25-min backoff
unsigned long lastUploadAttempt = 0;
const unsigned long UPLOAD_RETRY_DELAY_MS = 30000;  // Wait 30 seconds between retries after failure

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

// BLE notification callback for battery (0x180F / 0x2A19)
void batteryNotifyCallback(NimBLERemoteCharacteristic* pChar, uint8_t* pData, size_t length, bool isNotify) {
  if (length >= 1) {
    wind.battery = pData[0];
  }
}

// BLE notification callback for combined Data Service (0x180D / 0x2A39)
// Format: [speed_lo, speed_hi, dir_lo, dir_hi, battery] where battery * 10 = %
void dataNotifyCallback(NimBLERemoteCharacteristic* pChar, uint8_t* pData, size_t length, bool isNotify) {
  if (length >= 5) {
    uint16_t speedRaw = pData[0] | (pData[1] << 8);
    uint16_t dirRaw = pData[2] | (pData[3] << 8);
    uint8_t battRaw = pData[4];

    wind.speed_mps = speedRaw / 100.0;
    wind.speed_kts = wind.speed_mps * 1.94384;
    wind.angle_deg = dirRaw;
    wind.battery = battRaw * 10;  // Manual says value * 10 = %
    wind.newData = true;
    wind.lastUpdate = millis();
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
    pDataChar = nullptr;
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

// Load wind MAC from SD - if /wind_mac.txt exists, enable wind sensor
void loadWindMAC() {
  File f = SD.open("/wind_mac.txt", FILE_READ);
  if (f) {
    String mac = f.readStringUntil('\n');
    mac.trim();
    if (mac.length() >= 17) {  // Valid MAC is 17 chars (XX:XX:XX:XX:XX:XX)
      mac.toCharArray(config.wind_mac, sizeof(config.wind_mac));
      config.wind_enabled = true;
      Serial.printf("[WIND] Loaded MAC from SD: %s - wind ENABLED\n", config.wind_mac);
    } else {
      Serial.println("[WIND] /wind_mac.txt exists but invalid MAC format");
    }
    f.close();
  } else {
    Serial.println("[WIND] No /wind_mac.txt on SD - wind DISABLED");
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

  // Try to get battery from Battery Service (0x180F)
  NimBLERemoteService* pBattService = pWindClient->getService(BATTERY_SERVICE_UUID);
  if (pBattService) {
    pBatteryChar = pBattService->getCharacteristic(BATTERY_CHAR_UUID);
    if (pBatteryChar) {
      if (pBatteryChar->canNotify()) {
        pBatteryChar->subscribe(true, batteryNotifyCallback);
      }
      if (pBatteryChar->canRead()) {
        wind.battery = pBatteryChar->readValue<uint8_t>();
        Serial.printf("[WIND] Battery: %d%%\n", wind.battery);
      }
    }
  }

  // Also try Data Service (0x180D) for combined wind+battery notifications
  NimBLERemoteService* pDataService = pWindClient->getService(DATA_SERVICE_UUID);
  if (pDataService) {
    pDataChar = pDataService->getCharacteristic(DATA_CHAR_UUID);
    if (pDataChar && pDataChar->canNotify()) {
      pDataChar->subscribe(true, dataNotifyCallback);
    }
  }

  // Read firmware version from Device Information Service (0x180A)
  NimBLERemoteService* pDevInfoService = pWindClient->getService(DEVINFO_SERVICE_UUID);
  if (pDevInfoService) {
    NimBLERemoteCharacteristic* pFwChar = pDevInfoService->getCharacteristic(FIRMWARE_CHAR_UUID);
    if (pFwChar && pFwChar->canRead()) {
      std::string fw = pFwChar->readValue();
      if (fw.length() > 0) {
        strncpy(wind.firmware, fw.c_str(), sizeof(wind.firmware) - 1);
        wind.firmware[sizeof(wind.firmware) - 1] = '\0';
        Serial.printf("[WIND] Firmware: %s\n", wind.firmware);
      }
    }
  }

  wind.connected = true;
  windOK = true;
  Serial.println("[WIND] Connected and streaming");

  // Display wind sensor status on TFT at startup
  if (oledOK) {
    tft.fillScreen(COLOR_BG);
    tft.setTextColor(COLOR_GOOD, COLOR_BG);
    tft.setTextDatum(MC_DATUM);
    tft.drawString("WIND SENSOR OK", SCREEN_WIDTH/2, 100, 4);

    char buf[32];
    tft.setTextColor(COLOR_TEXT, COLOR_BG);
    if (strlen(wind.deviceName) > 0) {
      snprintf(buf, sizeof(buf), "%s", wind.deviceName);
      tft.drawString(buf, SCREEN_WIDTH/2, 150, 2);
    }

    if (wind.battery >= 0) {
      snprintf(buf, sizeof(buf), "Battery: %d%%", wind.battery);
      tft.drawString(buf, SCREEN_WIDTH/2, 180, 2);
    } else {
      tft.drawString("Battery: --", SCREEN_WIDTH/2, 180, 2);
    }

    delay(2000);  // Show for 2 seconds
    d2LayoutDrawn = false;  // Force main display to redraw
  }

  return true;
}

// Initialize BLE for wind sensor
void initWindSensor() {
  // Check for /wind_mac.txt on SD first - this enables wind if file exists
  if (sdOK) {
    loadWindMAC();
  }

  if (!config.wind_enabled) {
    Serial.println("[WIND] No wind_mac.txt found - wind sensor disabled");
    return;
  }

  Serial.println("[WIND] Initializing BLE...");
  NimBLEDevice::init("SailFrames-E1");
  bleInitialized = true;
  NimBLEDevice::setPower(ESP_PWR_LVL_P9);  // Max power for range

  // Try to connect using MAC from wind_mac.txt
  connectToCalypso();
}

// Check wind connection and reconnect if needed
// Stop any in-flight BLE operations before WiFi work begins. ESP32 has a
// single shared radio for BLE and WiFi; under sustained WiFi traffic
// (large uploads, e.g. 400KB+ RTCM3 PUTs) NimBLE's host task can stall
// without timing out, hanging whichever core is blocked inside
// NimBLEScan::start()/getResults(). Symptoms: hard hang, no panic, no
// reboot — display and serial freeze (firmware 2026.05.03.01 fleet test).
void pauseBLEForWiFi() {
  NimBLEScan* pScanLocal = NimBLEDevice::getScan();
  if (pScanLocal && pScanLocal->isScanning()) {
    Serial.println("[BLE] Stopping active scan before WiFi work");
    pScanLocal->stop();
  }
  if (pWindClient && pWindClient->isConnected()) {
    Serial.println("[BLE] Disconnecting wind client before WiFi work");
    pWindClient->disconnect();
  }
}

void checkWindConnection() {
  if (!config.wind_enabled) return;

  // Don't run BLE work while WiFi is in use — shared radio. New scans
  // started here under WiFi load are the documented hang trigger.
  if (wifiConnected || uploading || triggerUpload) return;

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

  sdWriting = true;
  unsigned long e = millis() - logStart;
  // Apply user-configured wind_offset only (no 180° correction needed)
  int correctedAwa = (wind.angle_deg + config.wind_offset) % 360;
  if (correctedAwa < 0) correctedAwa += 360;
  windFile.printf("%lu,%s,%.2f,%.2f,%d,%d\n",
    e, gps.utc_time, wind.speed_kts, wind.speed_mps, correctedAwa, wind.battery);
  totalBytes += 60;
  wind.newData = false;
  sdWriting = false;
}

#endif // ENABLE_WIND

// ============================================================
// PRESSURE SENSOR (DPS310)
// ============================================================
void readPressure() {
  if (!presOK) return;

  sensors_event_t temp_event, pressure_event;
  if (dps.getEvents(&temp_event, &pressure_event)) {
    pressure.pressure_hpa = pressure_event.pressure;
    pressure.temperature_c = temp_event.temperature;
    pressure.valid = true;
    pressure.lastRead = millis();

    // Track min/max for gust detection
    if (pressure.pressure_hpa < pressure.pressure_min) {
      pressure.pressure_min = pressure.pressure_hpa;
    }
    if (pressure.pressure_hpa > pressure.pressure_max) {
      pressure.pressure_max = pressure.pressure_hpa;
    }
  }
}

void logPressure() {
  if (!presFile || !logging || !pressure.valid) return;

  sdWriting = true;
  unsigned long e = millis() - logStart;
  // Log: elapsed_ms, utc, date, pressure_hpa, temp_c, pressure_min, pressure_max
  presFile.printf("%lu,%s,%s,%.2f,%.2f,%.2f,%.2f\n",
    e, gps.utc_time, gps.date, pressure.pressure_hpa, pressure.temperature_c,
    pressure.pressure_min, pressure.pressure_max);
  totalBytes += 80;
  sdWriting = false;
}

void resetPressureMinMax() {
  // Reset min/max tracking (call this periodically, e.g., every 10 seconds)
  pressure.pressure_min = pressure.pressure_hpa;
  pressure.pressure_max = pressure.pressure_hpa;
}

// ============================================================
// SETUP
// ============================================================
// Convert esp_reset_reason_t to a short label so a reboot log line is
// readable without grepping ESP-IDF headers.
static const char* resetReasonStr(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON:    return "POWERON";
    case ESP_RST_EXT:        return "EXT";
    case ESP_RST_SW:         return "SW";
    case ESP_RST_PANIC:      return "PANIC";
    case ESP_RST_INT_WDT:    return "INT_WDT";
    case ESP_RST_TASK_WDT:   return "TASK_WDT";
    case ESP_RST_WDT:        return "WDT";
    case ESP_RST_DEEPSLEEP:  return "DEEPSLEEP";
    case ESP_RST_BROWNOUT:   return "BROWNOUT";
    case ESP_RST_SDIO:       return "SDIO";
    default:                 return "UNKNOWN";
  }
}

// Append a single line to /boot.log, taking sdMutex with a short timeout so
// a busy SD path (logging, upload) can't stall the caller. The setup-time
// "boot fw=…" record runs before the mutex exists and writes directly; this
// helper is for runtime appends from loop() and diagnosticsTask.
void appendBootLog(const char* line) {
  if (!sdOK) return;
  if (sdMutex && xSemaphoreTake(sdMutex, pdMS_TO_TICKS(200)) != pdTRUE) return;
  File f = SD.open("/boot.log", FILE_APPEND);
  if (f) {
    f.println(line);
    f.close();
  }
  if (sdMutex) xSemaphoreGive(sdMutex);
}

// v2.0.0 radio mode transition stub (SF_FIRMWARE_V2_SPEC.md Stage 1).
// Stage 1 ships the state variable and logging only — the actual WiFi/BLE/
// ESP-NOW teardown and bringup move into this function in Stage 2 once
// the existing implicit "WiFi STA + BLE-C" coexistence is converted to a
// single Core-1 owner. Callers may invoke this now to record intent.
void radioModeTransition(RadioMode target, const char* reason) {
  if (target == g_radio_mode) return;
  RadioMode prev = g_radio_mode;
  g_radio_mode = target;
  Serial.printf("[RADIO] %s -> %s (%s)\n",
                radioModeName(prev), radioModeName(target),
                reason ? reason : "");
  char line[96];
  snprintf(line, sizeof(line), "radio %s->%s reason=%s",
           radioModeName(prev), radioModeName(target),
           reason ? reason : "-");
  appendBootLog(line);
}

// ============================================================
// v2.0.0 Stage 2 — ESP-NOW peer mesh
// ============================================================
// Broadcast boat-state at 2 Hz on channel 1 with the wire types in
// mesh.h. Receive callback runs in the WiFi task context (Core 0);
// keep it short — parse + update peer table + return. Heavy work
// stays out. Peer table writes happen from the WiFi task, reads
// happen from Core 1 (telnet/status). Single-word writes are
// atomic on the ESP32 so the worst case is a torn read of one
// peer's lat/lon — acceptable for informational use.

// ESP-IDF 5.x changed the recv callback signature from
//   (const uint8_t* mac, const uint8_t* data, int len)
// to
//   (const esp_now_recv_info_t* info, const uint8_t* data, int len)
// where info->src_addr is the source MAC. We don't use the MAC for
// peer identification (sender_id in the MeshHeader is the canonical
// identifier — stable across MAC changes) so the info pointer is
// just ignored.
static void meshOnReceive(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
  (void)info;
  if (len < (int)sizeof(MeshHeader)) {
    g_mesh_rx_dropped_bad_magic++;
    return;
  }
  const MeshHeader* h = (const MeshHeader*)data;
  if (h->magic[0] != MESH_MAGIC_0 || h->magic[1] != MESH_MAGIC_1) {
    g_mesh_rx_dropped_bad_magic++;
    return;
  }
  if (h->version != MESH_VERSION) return;
  if (h->sender_id == g_mesh_local_sender_id) return;  // own packet (broadcast loopback)

  g_mesh_rx_count++;

  if (h->msg_type == MSG_BOAT_STATE &&
      len >= (int)(sizeof(MeshHeader) + sizeof(BoatStatePayload))) {
    const BoatStatePayload* p = (const BoatStatePayload*)(data + sizeof(MeshHeader));

    // Find or add peer in the in-memory table.
    int idx = -1;
    for (int i = 0; i < g_mesh_peer_count; i++) {
      if (g_mesh_peers[i].sender_id == h->sender_id) { idx = i; break; }
    }
    if (idx < 0) {
      if (g_mesh_peer_count >= MESH_PEER_MAX) return;  // full
      idx = g_mesh_peer_count++;
      g_mesh_peers[idx].sender_id = h->sender_id;
      g_mesh_peers[idx].msg_count = 0;
    }
    g_mesh_peers[idx].last_lat_e7       = p->lat_e7;
    g_mesh_peers[idx].last_lon_e7       = p->lon_e7;
    g_mesh_peers[idx].last_sog_cm_s     = p->sog_cm_s;
    g_mesh_peers[idx].last_cog_deg10    = p->cog_deg10;
    g_mesh_peers[idx].last_heading_deg10= p->heading_deg10;
    g_mesh_peers[idx].last_heel_deg     = p->heel_deg;
    g_mesh_peers[idx].unit_role         = p->unit_role;
    g_mesh_peers[idx].fix_quality       = p->fix_quality;
    g_mesh_peers[idx].sat_count         = p->sat_count;
    g_mesh_peers[idx].last_seen_ms      = millis();
    g_mesh_peers[idx].last_seq          = h->seq;
    g_mesh_peers[idx].msg_count++;
  }
  else if (h->msg_type == MSG_RACE_ARMED &&
           len >= (int)(sizeof(MeshHeader) + sizeof(RaceArmedPayload))) {
    // Stage 4.5 — race-armed broadcast. Translate relative
    // seconds_until_start into local millis() and arm boat-local OCS.
    // Forward-declared ocsArm in this TU (defined later in the file).
    const RaceArmedPayload* p = (const RaceArmedPayload*)(data + sizeof(MeshHeader));
    double pin_lat = p->pin_lat_e7 / 1e7;
    double pin_lon = p->pin_lon_e7 / 1e7;
    double rc_lat  = p->rc_lat_e7  / 1e7;
    double rc_lon  = p->rc_lon_e7  / 1e7;
    uint32_t start_ms = millis() + (uint32_t)(p->seconds_until_start * 1000);
    extern void ocsArm(double, double, double, double, uint32_t);
    ocsArm(pin_lat, pin_lon, rc_lat, rc_lon, start_ms);
    Serial.printf("[MESH] MSG_RACE_ARMED from 0x%08lx — race %d T+0 in %ds\n",
                  (unsigned long)h->sender_id, p->race_num, p->seconds_until_start);
  }
  else if (h->msg_type == MSG_INDIVIDUAL_RECALL &&
           len >= (int)(sizeof(MeshHeader) + sizeof(IndividualRecallPayload))) {
    // Stage 5 — RC unit recalled a specific boat. If that's us,
    // override boat-local OCS to over_line=true. RC is authoritative.
    const IndividualRecallPayload* p =
        (const IndividualRecallPayload*)(data + sizeof(MeshHeader));
    if (p->target_sender_id == g_mesh_local_sender_id) {
      // Forward-declare; defined later in the OCS section.
      extern void ocsForceOver(int16_t rc_distance_cm);
      ocsForceOver(p->distance_cm);
      Serial.printf("[MESH] INDIVIDUAL_RECALL for us! RC d=%d cm — forcing OCS=true\n",
                    p->distance_cm);
    }
  }
}

// Broadcast a MSG_RACE_ARMED to the fleet. Called from the telnet
// `race arm` command after we've armed our own OCS state. Other
// boats receive this in meshOnReceive and arm their own.
bool meshBroadcastRaceArmed(double pin_lat, double pin_lon,
                            double rc_lat, double rc_lon,
                            int seconds_until_start,
                            uint8_t race_num, uint8_t sequence_mode) {
  if (!g_mesh_enabled) return false;
  uint8_t buf[sizeof(MeshHeader) + sizeof(RaceArmedPayload)];
  MeshHeader* h = (MeshHeader*)buf;
  RaceArmedPayload* p = (RaceArmedPayload*)(buf + sizeof(MeshHeader));

  h->magic[0] = MESH_MAGIC_0;
  h->magic[1] = MESH_MAGIC_1;
  h->version  = MESH_VERSION;
  h->msg_type = MSG_RACE_ARMED;
  h->seq      = g_mesh_seq++;
  h->ttl      = 0;
  h->reserved = 0;
  h->sender_id = g_mesh_local_sender_id;
  h->gps_time_ms = 0;

  p->pin_lat_e7 = (int32_t)(pin_lat * 1e7);
  p->pin_lon_e7 = (int32_t)(pin_lon * 1e7);
  p->rc_lat_e7  = (int32_t)(rc_lat  * 1e7);
  p->rc_lon_e7  = (int32_t)(rc_lon  * 1e7);
  p->seconds_until_start = seconds_until_start;
  p->race_num = race_num;
  p->sequence_mode = sequence_mode;
  p->reserved[0] = p->reserved[1] = 0;

  // Send multiple times — small payload, race-critical, no ack mechanism
  // in MVP. Three transmissions ~100 ms apart raise reliability against
  // single-packet losses without saturating airtime.
  esp_err_t err = ESP_OK;
  for (int i = 0; i < 3; i++) {
    esp_err_t e = esp_now_send(MESH_BROADCAST_ADDR, buf, sizeof(buf));
    if (e == ESP_OK) g_mesh_tx_count++;
    else { g_mesh_tx_fail_count++; err = e; }
    if (i < 2) delay(100);
  }
  return err == ESP_OK;
}

// Stage 5 — RC broadcasts when it sees a boat over the line.
// Target boat receives in meshOnReceive and overrides its local
// OCS state. Sent 3x for resilience.
bool meshBroadcastIndividualRecall(uint32_t target_id, int16_t distance_cm) {
  if (!g_mesh_enabled) return false;
  uint8_t buf[sizeof(MeshHeader) + sizeof(IndividualRecallPayload)];
  MeshHeader* h = (MeshHeader*)buf;
  IndividualRecallPayload* p =
      (IndividualRecallPayload*)(buf + sizeof(MeshHeader));

  h->magic[0] = MESH_MAGIC_0;
  h->magic[1] = MESH_MAGIC_1;
  h->version  = MESH_VERSION;
  h->msg_type = MSG_INDIVIDUAL_RECALL;
  h->seq      = g_mesh_seq++;
  h->ttl      = 0;
  h->reserved = 0;
  h->sender_id = g_mesh_local_sender_id;
  h->gps_time_ms = 0;

  p->target_sender_id = target_id;
  p->distance_cm      = distance_cm;
  p->reserved[0] = p->reserved[1] = 0;

  esp_err_t err = ESP_OK;
  for (int i = 0; i < 3; i++) {
    esp_err_t e = esp_now_send(MESH_BROADCAST_ADDR, buf, sizeof(buf));
    if (e == ESP_OK) g_mesh_tx_count++;
    else { g_mesh_tx_fail_count++; err = e; }
    if (i < 2) delay(50);
  }
  return err == ESP_OK;
}

void meshInit() {
  if (g_mesh_enabled) return;
  g_mesh_local_sender_id = boatIdHash(config.boat_id);

  // ESP-NOW needs the WiFi radio enabled to transmit, not just the
  // driver initialised. Early-setup() does WiFi.mode(WIFI_STA) then
  // WiFi.disconnect(true) which turns the radio OFF (esp_wifi_stop).
  // Re-enable STA mode here. This is idempotent if WiFi was already
  // up from a later connectWiFi().
  WiFi.mode(WIFI_STA);

  esp_err_t err = esp_now_init();
  if (err != ESP_OK) {
    Serial.printf("[MESH] esp_now_init failed: %d\n", err);
    return;
  }

  esp_now_register_recv_cb(meshOnReceive);

  // Pin the WiFi channel explicitly so esp_now_send always knows which
  // channel to transmit on, even when STA is not associated with any
  // AP. .13 used peerInfo.channel=0 which means "use current STA
  // channel" — but when STA hasn't connected to anything yet, that
  // channel is undefined and esp_now_send returns ESP_ERR_ESPNOW_IF
  // (tx=0 fail=N pattern observed on the fleet's .13 boot).
  esp_wifi_set_channel(MESH_CHANNEL, WIFI_SECOND_CHAN_NONE);

  esp_now_peer_info_t peerInfo = {};
  memcpy(peerInfo.peer_addr, MESH_BROADCAST_ADDR, 6);
  peerInfo.channel = MESH_CHANNEL;     // explicit channel, not 0
  peerInfo.ifidx   = WIFI_IF_STA;      // be explicit; was zero-init
  peerInfo.encrypt = false;
  err = esp_now_add_peer(&peerInfo);
  if (err != ESP_OK) {
    Serial.printf("[MESH] add broadcast peer failed: %d\n", err);
    esp_now_deinit();
    return;
  }

  g_mesh_enabled = true;
  Serial.printf("[MESH] ESP-NOW init OK, sender_id=0x%08lx ch=%d\n",
                (unsigned long)g_mesh_local_sender_id, MESH_CHANNEL);
  appendBootLog("mesh init ok");
}

static void meshBuildAndSendBoatState() {
  uint8_t buf[sizeof(MeshHeader) + sizeof(BoatStatePayload)];
  MeshHeader* h = (MeshHeader*)buf;
  BoatStatePayload* p = (BoatStatePayload*)(buf + sizeof(MeshHeader));

  h->magic[0]   = MESH_MAGIC_0;
  h->magic[1]   = MESH_MAGIC_1;
  h->version    = MESH_VERSION;
  h->msg_type   = MSG_BOAT_STATE;
  h->seq        = g_mesh_seq++;
  h->ttl        = 0;     // peer-to-peer for MVP, no rebroadcast
  h->reserved   = 0;
  h->sender_id  = g_mesh_local_sender_id;
  h->gps_time_ms = 0;    // TODO: HHMMSS.sss -> ms-of-day; 0 = unknown for MVP

  p->lat_e7         = (int32_t)(gps.lat * 1e7);
  p->lon_e7         = (int32_t)(gps.lon * 1e7);
  // 1 kt = 51.4444 cm/s
  p->sog_cm_s       = (int16_t)(gps.speed_kts * 51.4444f);
  p->cog_deg10      = (int16_t)(gps.course * 10.0f);
  p->heading_deg10  = (int16_t)(imu.heading * 10.0f);
  p->heel_deg       = (int8_t)imu.heel;
  p->fix_quality    = (uint8_t)gps.fix_quality;
  p->sat_count      = (uint8_t)gps.satellites;
  p->unit_role      = (uint8_t)g_role;
  // reserved is uint8_t[2] in mesh.h (struct sized to 20 bytes per spec).
  // The earlier version of this line wrote reserved[0..2] which overran
  // by 1 byte off the end of buf[36] and crashed every meshTick call
  // via __stack_chk_fail — the .10/.11 fleet-brick bug.
  p->reserved[0] = p->reserved[1] = 0;

  esp_err_t err = esp_now_send(MESH_BROADCAST_ADDR, buf, sizeof(buf));
  if (err == ESP_OK) {
    g_mesh_tx_count++;
  } else {
    g_mesh_tx_fail_count++;
    static int s_logged = 0;
    if (s_logged < 5) {
      Serial.printf("[MESH] esp_now_send failed: 0x%x\n", err);
      s_logged++;
    }
    // ESP_ERR_ESPNOW_NOT_INIT (0x3001) fires when WiFi.disconnect(true)
    // in connectWiFi() called esp_wifi_stop() as a side effect, which
    // tears down ESP-NOW too. Recover by re-initializing on the next
    // tick. Observed on .15: tx=71 then fail=N with IDF logging
    // "E ESPNOW: esp now not init!" at 500 ms intervals after the
    // first upload-task WiFi reconnect cycle.
    if (err == ESP_ERR_ESPNOW_NOT_INIT) {
      g_mesh_enabled = false;
      Serial.println("[MESH] ESP-NOW torn down by WiFi cycle — re-init next tick");
    }
  }
}

// Called from the main loop. Cheap path: returns fast if disabled,
// gated, or interval not yet elapsed.
void meshTick() {
  // Auto-recover if ESP-NOW got torn down by a WiFi cycle. meshInit
  // is idempotent (returns early if already enabled), so calling it
  // here is safe both for normal operation and post-teardown.
  // Throttled to once per second so we don't spam if the radio is
  // genuinely down.
  if (!g_mesh_enabled && !wifiBusy && !uploading) {
    static unsigned long lastReinit = 0;
    unsigned long now2 = millis();
    if (now2 - lastReinit >= 1000) {
      lastReinit = now2;
      meshInit();
    }
    return;
  }
  if (!g_mesh_enabled) return;
  // Don't compete with HTTP uploads — esp_now_send is cheap but the
  // RF airtime steals from the upload. Existing wifiBusy + uploading
  // gates already protect telnet; we use the same convention.
  if (wifiBusy || uploading) return;

  unsigned long now = millis();
  if (now - g_mesh_last_broadcast >= MESH_BROADCAST_INTERVAL_MS) {
    g_mesh_last_broadcast = now;
    meshBuildAndSendBoatState();
  }

  // Expire stale peers (linear scan is fine for ≤32 peers).
  for (int i = g_mesh_peer_count - 1; i >= 0; i--) {
    if (now - g_mesh_peers[i].last_seen_ms > MESH_PEER_EXPIRY_MS) {
      g_mesh_peers[i] = g_mesh_peers[g_mesh_peer_count - 1];
      g_mesh_peer_count--;
    }
  }
}

// ============================================================
// v2.0.0 Stage 4 — OCS state machine (boat-local MVP)
// ============================================================
// Per-boat over-line detection at race start. Computes the boat's
// bow position from GPS + heading + bow_offset_m, projects it onto
// the start line PIN→RC, and tracks signed distance. After T+0, if
// the bow is on the course side of the line (signed distance below
// the negative threshold), over_line latches true.
//
// MVP scope:
//   - Manual arm via telnet `race arm <pin_lat> <pin_lon>
//     <rc_lat> <rc_lon> <seconds_from_now>`. No RC mesh reception
//     yet — that's Stage 5 with MSG_RACE_ARMED.
//   - Single hardcoded bow_offset_m (2.4 — Sonar 23). Class
//     registry CSV comes with Stage 5.
//   - Heading sourced from GPS COG when sog > 2 kt (reliable),
//     IMU heading otherwise (magnetic / fusion drift acceptable
//     near start when speeds are low).
//   - No LED (E1 has none; future B1 has LEDs). State surfaced
//     via telnet `ocs` command.

#define OCS_BOW_OFFSET_M           2.4f    // Sonar 23 default (used when class registry has no entry)
#define OCS_THRESHOLD_M            0.5f    // must be >50cm over to call OCS
#define OCS_CLEAR_THRESHOLD_M      0.5f    // must be >50cm back to clear
#define OCS_CLEAR_DWELL_MS         2000    // sustained-clear time before un-latching

// Stage 5.5 — per-class bow_offset registry (RC-only)
// Loaded from /sf/classes.csv at boot when role=rc_signal.
// CSV format (header optional):
//   boat_id,class,bow_offset_m
//   E1,Sonar23,2.4
//   E2,Sonar23,2.4
//   F1,J80,2.8
// FNV-1a hash of boat_id is the key — same hash used as ESP-NOW sender_id,
// so RC matches incoming MeshHeader.sender_id directly without re-hashing.
#define CLASS_REGISTRY_MAX  32

struct ClassRegistryEntry {
    uint32_t sender_id;        // FNV-1a hash of boat_id, used to match peer
    char     boat_id[16];      // raw boat_id for telnet display
    char     class_name[16];   // e.g. "Sonar23", "J80"
    float    bow_offset_m;
};

ClassRegistryEntry g_class_registry[CLASS_REGISTRY_MAX];
int g_class_registry_count = 0;
#define OCS_TICK_INTERVAL_MS       100     // 10 Hz — matches GPS fix rate

struct OCSState {
  bool     armed;
  uint32_t start_time_ms;        // millis() value at which T+0 fires
  double   pin_lat, pin_lon;
  double   rc_lat,  rc_lon;
  // Live:
  bool     over_line;
  bool     was_over_at_start;
  float    distance_to_line_m;   // signed, positive = pre-start side
  float    closure_rate_m_s;     // negative = approaching line from pre-start
  uint32_t over_since_ms;
  uint32_t cleared_at_ms;
  // Internal for closure-rate calc:
  float    _last_d;
  uint32_t _last_t_ms;
};
OCSState g_ocs = {};

unsigned long g_ocs_last_tick_ms = 0;

void ocsDisarm() {
  g_ocs.armed = false;
  g_ocs.over_line = false;
  g_ocs.was_over_at_start = false;
  g_ocs.distance_to_line_m = 0;
  g_ocs.closure_rate_m_s = 0;
  g_ocs.over_since_ms = 0;
  g_ocs.cleared_at_ms = 0;
  g_ocs._last_d = 0;
  g_ocs._last_t_ms = 0;
}

void ocsArm(double pin_lat, double pin_lon,
            double rc_lat,  double rc_lon,
            uint32_t start_time_ms) {
  ocsDisarm();
  g_ocs.armed = true;
  g_ocs.pin_lat = pin_lat;
  g_ocs.pin_lon = pin_lon;
  g_ocs.rc_lat = rc_lat;
  g_ocs.rc_lon = rc_lon;
  g_ocs.start_time_ms = start_time_ms;
}

void ocsTick() {
  if (!g_ocs.armed) return;
  if (!gps.valid) return;

  unsigned long now = millis();
  if (now - g_ocs_last_tick_ms < OCS_TICK_INTERVAL_MS) return;
  g_ocs_last_tick_ms = now;

  // Use GPS COG when boat is moving — reliable above ~2 kt. Below
  // that, use IMU heading (magnetic / fusion may drift but it's
  // the best we have when stationary or in low-speed pre-start
  // tactical maneuvering).
  float heading_deg = (gps.speed_kts > 2.0f) ? gps.course : imu.heading;
  float heading_rad = heading_deg * (float)PI / 180.0f;

  double ref_lat_rad = ((g_ocs.pin_lat + g_ocs.rc_lat) / 2.0) * PI / 180.0;
  double m_per_deg_lat = 111320.0;
  double m_per_deg_lon = 111320.0 * cos(ref_lat_rad);

  // Bow position = boat position + bow_offset along heading
  double bow_lat = gps.lat + (OCS_BOW_OFFSET_M * cos(heading_rad)) / m_per_deg_lat;
  double bow_lon = gps.lon + (OCS_BOW_OFFSET_M * sin(heading_rad)) / m_per_deg_lon;

  // Project bow onto line PIN(A) -> RC(B). Local equirectangular
  // meters frame anchored at PIN.
  double Bx = (g_ocs.rc_lon - g_ocs.pin_lon) * m_per_deg_lon;
  double By = (g_ocs.rc_lat - g_ocs.pin_lat) * m_per_deg_lat;
  double Px = (bow_lon - g_ocs.pin_lon) * m_per_deg_lon;
  double Py = (bow_lat - g_ocs.pin_lat) * m_per_deg_lat;

  // 2D cross product gives signed perpendicular distance × |AB|.
  // Sign convention: positive = "left" of AB walking PIN -> RC.
  // The user/RC convention (which side is pre-start) is decided
  // at arm time by how PIN and RC are passed. Standard fleet
  // convention: PIN on port hand approaching start, RC on stbd;
  // pre-start side is the side the cross-product convention
  // picks positive.
  double cross = Bx * Py - By * Px;
  double lenAB = sqrt(Bx * Bx + By * By);
  float d_signed = (lenAB > 0.001) ? (float)(cross / lenAB) : 0.0f;

  // Closure-rate numerical derivative over last tick.
  if (g_ocs._last_t_ms > 0) {
    float dt = (now - g_ocs._last_t_ms) / 1000.0f;
    if (dt > 0.005f) g_ocs.closure_rate_m_s = (d_signed - g_ocs._last_d) / dt;
  }
  g_ocs._last_d = d_signed;
  g_ocs._last_t_ms = now;
  g_ocs.distance_to_line_m = d_signed;

  // Snapshot whether we were over at T+0 (within ±500 ms window).
  int32_t time_to_start = (int32_t)(g_ocs.start_time_ms - now);  // positive = pre-start
  if (time_to_start > -500 && time_to_start < 500) {
    if (d_signed < -OCS_THRESHOLD_M) g_ocs.was_over_at_start = true;
  }

  // Over-line latching is only meaningful at/after T+0.
  if (time_to_start <= 0) {
    if (d_signed < -OCS_THRESHOLD_M) {
      if (!g_ocs.over_line) {
        g_ocs.over_line = true;
        g_ocs.over_since_ms = now;
        g_ocs.cleared_at_ms = 0;
        Serial.printf("[OCS] Bow over line: d=%.2f m\n", d_signed);
      }
    } else if (g_ocs.over_line && d_signed > OCS_CLEAR_THRESHOLD_M) {
      if (g_ocs.cleared_at_ms == 0) {
        g_ocs.cleared_at_ms = now;
      } else if (now - g_ocs.cleared_at_ms > OCS_CLEAR_DWELL_MS) {
        g_ocs.over_line = false;
        g_ocs.cleared_at_ms = 0;
        Serial.printf("[OCS] Bow cleared line: d=%.2f m\n", d_signed);
      }
    } else {
      g_ocs.cleared_at_ms = 0;
    }
  }
}

// Stage 5 — called from meshOnReceive when this boat is the
// target of MSG_INDIVIDUAL_RECALL. Overrides local OCS to true
// regardless of local computation. RC is authoritative.
//
// Stage 5.5 — also logs RC-vs-local OCS disagreement to
// /sf/ocs_disagree.log when the deltas exceed a threshold.
// Disagreement is interesting data: if RC's bow_offset_m for this
// boat is wrong, or the boat's IMU heading is bad, or there's
// large clock skew between fixes, the boat's local OCS state will
// not match RC's. Post-race we mine this log to tune the registry.
void ocsForceOver(int16_t rc_distance_cm) {
  float rc_d_m = rc_distance_cm / 100.0f;
  float local_d_m = g_ocs.distance_to_line_m;
  bool local_over = g_ocs.over_line;

  if (g_ocs.armed && !g_ocs.over_line) {
    g_ocs.over_line = true;
    g_ocs.over_since_ms = millis();
    g_ocs.cleared_at_ms = 0;
    Serial.printf("[OCS] Forced over_line by RC recall (rc_d=%.2fm, local_d=%.2fm)\n",
                  rc_d_m, local_d_m);
  }

  // Stage 5.5 — log every recall to /sf/ocs_disagree.log with
  // RC's view and our local state at the moment of recall. Files
  // are uploaded post-race like the rest of the session CSVs.
  char iso[24];
  bool have_time = formatGpsIso(iso, sizeof(iso));
  File f = SD.open("/sf/ocs_disagree.log", FILE_APPEND);
  if (f) {
    f.printf("t=%s armed=%d local_over=%d rc_d=%.2fm local_d=%.2fm delta=%.2fm\n",
             have_time ? iso : "no-fix",
             g_ocs.armed ? 1 : 0,
             local_over ? 1 : 0,
             rc_d_m, local_d_m, rc_d_m - local_d_m);
    f.close();
  }
}

// ============================================================
// v2.0.0 Stage 5 — RC unit fleet OCS aggregation
// ============================================================
// Only runs when g_role == ROLE_RC_SIGNAL. Computes OCS for every
// peer using the RC's authoritative line endpoints. When a peer
// crosses the OCS threshold post-T+0, RC broadcasts
// MSG_INDIVIDUAL_RECALL with the target sender_id. The boat
// receives this in meshOnReceive and forces its local
// over_line = true (RC is authoritative).
//
// Class registry / per-class bow_offset_m is deferred to Stage
// 5.5; MVP uses the same hardcoded OCS_BOW_OFFSET_M for every
// peer. Sonar 23 / J/80 fleets have very similar bow offsets
// (2.4-2.8 m); the difference at 5 kt is ~0.1 m / 80 ms, small
// vs the 0.5 m hysteresis threshold.

#define RC_TICK_INTERVAL_MS  200    // 5 Hz — receivers broadcast at 2 Hz so this is fast enough

unsigned long g_rc_last_tick_ms = 0;

void rcComputeFleetOCS() {
  if (g_role != ROLE_RC_SIGNAL) return;
  if (!g_ocs.armed) return;
  unsigned long now = millis();
  if (now - g_rc_last_tick_ms < RC_TICK_INTERVAL_MS) return;
  g_rc_last_tick_ms = now;

  int32_t time_to_start = (int32_t)(g_ocs.start_time_ms - now);
  // Only call OCS post-T+0 (with small grace window for clock drift)
  if (time_to_start > 500) return;

  double ref_lat_rad = ((g_ocs.pin_lat + g_ocs.rc_lat) / 2.0) * PI / 180.0;
  double m_per_deg_lat = 111320.0;
  double m_per_deg_lon = 111320.0 * cos(ref_lat_rad);

  double Bx = (g_ocs.rc_lon - g_ocs.pin_lon) * m_per_deg_lon;
  double By = (g_ocs.rc_lat - g_ocs.pin_lat) * m_per_deg_lat;
  double lenAB = sqrt(Bx * Bx + By * By);
  if (lenAB < 0.001) return;

  for (int i = 0; i < g_mesh_peer_count; i++) {
    MeshPeerState& peer = g_mesh_peers[i];
    // Skip if no recent fix
    if (peer.fix_quality == 0 || peer.last_lat_e7 == 0) continue;
    if (now - peer.last_seen_ms > 5000) continue;  // stale (>5s no msg)

    double peer_lat = peer.last_lat_e7 / 1e7;
    double peer_lon = peer.last_lon_e7 / 1e7;

    // Bow position: use heading from BoatStatePayload when boat is
    // slow, COG when fast. peer.last_sog_cm_s is cm/s; 100 cm/s ≈ 2 kt.
    float heading_deg = (peer.last_sog_cm_s > 100)
                          ? (peer.last_cog_deg10 / 10.0f)
                          : (peer.last_heading_deg10 / 10.0f);
    float heading_rad = heading_deg * (float)PI / 180.0f;

    // Stage 5.5 — per-class bow offset from /sf/classes.csv lookup.
    // Unknown peers fall through to OCS_BOW_OFFSET_M.
    float bow_offset = bowOffsetForSender(peer.sender_id);

    double bow_lat = peer_lat +
        (bow_offset * cos(heading_rad)) / m_per_deg_lat;
    double bow_lon = peer_lon +
        (bow_offset * sin(heading_rad)) / m_per_deg_lon;

    double Px = (bow_lon - g_ocs.pin_lon) * m_per_deg_lon;
    double Py = (bow_lat - g_ocs.pin_lat) * m_per_deg_lat;
    double cross = Bx * Py - By * Px;
    float d_signed = (float)(cross / lenAB);
    peer.rc_distance_m = d_signed;

    if (d_signed < -OCS_THRESHOLD_M && !peer.rc_ocs_called) {
      peer.rc_ocs_called = true;
      peer.rc_ocs_called_at_ms = now;
      int16_t d_cm = (int16_t)(d_signed * 100.0f);
      Serial.printf("[RC] OCS: peer 0x%08lx d=%.2fm — broadcasting recall\n",
                    (unsigned long)peer.sender_id, d_signed);
      meshBroadcastIndividualRecall(peer.sender_id, d_cm);
    }
  }
}

// Format gps.utc_time (HHMMSS) + gps.date (DDMMYY) into ISO8601 "YYYY-MM-DDTHH:MM:SSZ".
// Returns false if either field is not yet populated.
static bool formatGpsIso(char* out, size_t outSize) {
  if (strlen(gps.utc_time) < 6) return false;
  if (strlen(gps.date) < 6) return false;
  if (gps.date[4] == '0' && gps.date[5] == '0') return false;  // year 00 = invalid
  snprintf(out, outSize, "20%c%c-%c%c-%c%cT%c%c:%c%c:%c%cZ",
           gps.date[4], gps.date[5],          // YY
           gps.date[2], gps.date[3],          // MM
           gps.date[0], gps.date[1],          // DD
           gps.utc_time[0], gps.utc_time[1],  // HH
           gps.utc_time[2], gps.utc_time[3],  // MM
           gps.utc_time[4], gps.utc_time[5]); // SS
  return true;
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(500);

  // Capture WHY we last rebooted before any other init runs. The fleet
  // had a simultaneous-reboot event on 2026-05-03 with no serial captured;
  // this prints the cause at the top of the next boot log AND appends it
  // to /boot.log on SD so we can read it later if no USB was connected.
  esp_reset_reason_t resetReason = esp_reset_reason();

  Serial.println("\n=================================");
  Serial.printf("  SailFrames E1 %s — PPK Logger\n", FW_VERSION);
  Serial.println("  Hardware Power Switch Edition");
  Serial.printf("  Reset reason: %s (%d)\n", resetReasonStr(resetReason), (int)resetReason);
  Serial.printf("  Free heap: %u, min ever: %u\n",
                ESP.getFreeHeap(),
                (unsigned)esp_get_minimum_free_heap_size());
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
  Wire.setBufferSize(512);  // BNO085 SHTP needs larger buffer
  Wire.setClock(100000);  // Start slow for reliable init
  delay(100);  // Let I2C bus stabilize

  // BNO085 needs up to 1 second to boot after power-on
  Serial.println("[I2C] Waiting for BNO085 boot (1s)...");
  delay(1000);

  // I2C Scanner - check all addresses to debug
  Serial.println("[I2C] Scanning bus...");
  for (uint8_t addr = 0x08; addr < 0x78; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("[I2C] Found device at 0x%02X\n", addr);
    }
  }

  // Check expected devices
  Serial.println("[I2C] Checking expected devices...");
  Wire.beginTransmission(BNO085_ADDR);
  bool bnoFound = (Wire.endTransmission() == 0);
  Wire.beginTransmission(DPS310_ADDR);
  bool dpsFound = (Wire.endTransmission() == 0);
  Serial.printf("[I2C] BNO085 0x4B: %s\n", bnoFound ? "YES" : "NO");
  Serial.printf("[I2C] DPS310 0x77: %s\n", dpsFound ? "YES" : "NO");

  // IMU — BNO085 (init early, before SPI peripherals)
  Serial.println("[IMU] Initializing BNO085...");
  bool imuInitOK = bno08x.begin_I2C(BNO085_ADDR, &Wire);

  if (imuInitOK) {
    imuOK = true;
    Wire.setClock(400000);  // Switch to fast mode after init
    Serial.println("[IMU] BNO085 detected, enabling reports");
    if (!bno08x.enableReport(SH2_GAME_ROTATION_VECTOR, IMU_INTERVAL_MS * 1000)) {
      Serial.println("[IMU] WARNING: Failed to enable game rotation vector");
    }
    if (!bno08x.enableReport(SH2_ROTATION_VECTOR, IMU_INTERVAL_MS * 1000)) {
      Serial.println("[IMU] WARNING: Failed to enable rotation vector");
    }
    if (!bno08x.enableReport(SH2_ACCELEROMETER, IMU_INTERVAL_MS * 1000)) {
      Serial.println("[IMU] WARNING: Failed to enable accelerometer");
    }
    if (!bno08x.enableReport(SH2_GYROSCOPE_CALIBRATED, IMU_INTERVAL_MS * 1000)) {
      Serial.println("[IMU] WARNING: Failed to enable gyroscope");
    }
    if (!bno08x.enableReport(SH2_LINEAR_ACCELERATION, IMU_INTERVAL_MS * 1000)) {
      Serial.println("[IMU] WARNING: Failed to enable linear acceleration");
    }
    if (!bno08x.enableReport(SH2_STABILITY_CLASSIFIER, 500000)) {
      Serial.println("[IMU] WARNING: Failed to enable stability classifier");
    }
    if (!bno08x.enableReport(SH2_MAGNETIC_FIELD_CALIBRATED, IMU_INTERVAL_MS * 1000)) {
      Serial.println("[IMU] WARNING: Failed to enable magnetometer");
    }
    Serial.println("[IMU] BNO085 OK");
  } else {
    Serial.println("[IMU] BNO085 not found!");
  }

  // Set up CS pins before any SPI init
  pinMode(TFT_CS_PIN, OUTPUT);
  digitalWrite(TFT_CS_PIN, HIGH);
  pinMode(SD_CS_PIN, OUTPUT);
  digitalWrite(SD_CS_PIN, HIGH);
  delay(100);

  // SD Card - Initialize on HSPI bus (separate from TFT's VSPI)
  Serial.println("[SD] Initializing on HSPI bus (separate from TFT)...");
  Serial.printf("[SD] Pins: CLK=%d, MISO=%d, MOSI=%d, CS=%d\n", SD_CLK_PIN, SD_MISO_PIN, SD_MOSI_PIN, SD_CS_PIN);

  // Create HSPI instance for SD card - completely separate from TFT's VSPI
  static SPIClass sdSPI(HSPI);
  sdSPI.begin(SD_CLK_PIN, SD_MISO_PIN, SD_MOSI_PIN, SD_CS_PIN);  // CLK=14, MISO=12, MOSI=13, CS=27
  delay(50);

  // Try different SPI speeds
  Serial.println("[SD] Trying 4MHz...");
  sdOK = SD.begin(SD_CS_PIN, sdSPI, 4000000);
  if (!sdOK) {
    Serial.println("[SD] 4MHz failed, trying 1MHz...");
    sdOK = SD.begin(SD_CS_PIN, sdSPI, 1000000);
  }
  if (!sdOK) {
    Serial.println("[SD] 1MHz failed, trying 400kHz...");
    sdOK = SD.begin(SD_CS_PIN, sdSPI, 400000);
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
      loadClassRegistry();  // Stage 5.5 — RC-only, no-op for racing boats
      loadIMUCalibration();

      // Append a boot record so we can reconstruct reset history later
      // even without a USB cable attached at the moment of failure.
      File bootLog = SD.open("/boot.log", FILE_APPEND);
      if (bootLog) {
        bootLog.printf("boot fw=%s reset=%s heap=%u min_heap=%u\n",
                       FW_VERSION,
                       resetReasonStr(esp_reset_reason()),
                       ESP.getFreeHeap(),
                       (unsigned)esp_get_minimum_free_heap_size());
        bootLog.close();
        Serial.println("[SD] Boot record appended to /boot.log");
      }
    }
  } else {
    Serial.println("[SD] === SD CARD FAILED ===");
    Serial.println("[SD] Troubleshooting:");
    Serial.println("[SD]   1. Check SD module wiring (CS=GPIO27)");
    Serial.println("[SD]   2. Insert card before power-on");
    Serial.println("[SD]   3. Card must be FAT32 (not exFAT)");
    Serial.println("[SD]   4. Try different SD card");
  }

  // TFT Display - Initialize AFTER SD
  Serial.println("[TFT] Initializing ST7796U...");
  pinMode(TFT_BL_PIN, OUTPUT);
  digitalWrite(TFT_BL_PIN, HIGH);  // Backlight ON
  tft.init();
  tft.setRotation(2);  // Portrait orientation (180° from rotation 0)
  tft.invertDisplay(true);  // Required for correct colors on this ST7796 panel
  tft.fillScreen(COLOR_BG);
  oledOK = true;
  Serial.println("[TFT] ST7796U initialized (320x480 portrait)");

  // Splash screen - show device ID, domain, and firmware version
  tft.fillScreen(COLOR_BG);
  tft.setTextDatum(MC_DATUM);

  // Draw device ID in HUGE font (fill most of the screen)
  tft.setTextColor(COLOR_TEXT, COLOR_BG);
  tft.setTextSize(8);
  tft.drawString(config.boat_id, SCREEN_WIDTH/2, SCREEN_HEIGHT/2 - 60, 4);

  // "Sailframes.com" - black, medium size
  tft.setTextColor(TFT_BLACK, COLOR_BG);
  tft.setTextSize(1);
  tft.drawString("Sailframes.com", SCREEN_WIDTH/2, SCREEN_HEIGHT/2 + 80, 4);

  // Firmware version at bottom - large enough to read across the cabin
  tft.setTextColor(COLOR_LABEL, COLOR_BG);
  tft.setTextSize(2);
  tft.drawString(FW_VERSION, SCREEN_WIDTH/2, SCREEN_HEIGHT - 40, 4);
  tft.setTextSize(1);

  delay(2500);  // Show splash screen

  // Reset text size for rest of display
  tft.setTextSize(1);

  // DPS310 Pressure/Temperature sensor
  Serial.println("[PRES] Initializing DPS310...");
  if (dps.begin_I2C(DPS310_ADDR, &Wire)) {
    presOK = true;
    // Configure for high-rate sampling (good for gust detection)
    dps.configurePressure(DPS310_64HZ, DPS310_64SAMPLES);
    dps.configureTemperature(DPS310_64HZ, DPS310_64SAMPLES);
    Serial.println("[PRES] DPS310 OK");

    // Take initial reading
    sensors_event_t temp_event, pressure_event;
    if (dps.getEvents(&temp_event, &pressure_event)) {
      pressure.pressure_hpa = pressure_event.pressure;
      pressure.temperature_c = temp_event.temperature;
      pressure.valid = true;
      Serial.printf("[PRES] Initial: %.2f hPa, %.1f°C\n",
        pressure.pressure_hpa, pressure.temperature_c);
    }
  } else {
    Serial.println("[PRES] DPS310 not found");
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

  // RTCM3 raw-observation capture retired in .09 — the CFGRTCM probe
  // here previously drained the response buffer; configureLG290P() below
  // does its own command/response handling so no explicit probe is needed.

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
  // WiFi connection is handled by upload task on Core 0 - non-blocking
  // Don't connect at boot to avoid blocking the display
  if (config.wifi_count > 0) {
    Serial.println("[WIFI] WiFi configured - will connect in background when needed");
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

  // Watchdog timeout: 300s (5 min). HTTP PUTs of 600KB+ RTCM3 files at
  // marginal signal can stretch past 120s in a single sendRequest() —
  // we don't get to call esp_task_wdt_reset() inside HTTPClient. The
  // 2026-05-03 fleet simultaneous-reboot event is consistent with the
  // wdt firing on a slow PUT across multiple devices when signal briefly
  // degraded. 300s gives ~3.6 KB/s for a 1 MB file before tripping.
  esp_task_wdt_config_t wdt_config = {
    .timeout_ms = 300000,
    .idle_core_mask = 0,       // Don't monitor IDLE tasks on any core
    .trigger_panic = true
  };
  esp_task_wdt_reconfigure(&wdt_config);

  // Create upload task on Core 0 (sensor reading stays on Core 1)
  xTaskCreatePinnedToCore(
    uploadTaskFunc,     // Function
    "uploadTask",       // Name
    12288,              // Stack size (needs room for HTTP + BLE deinit/reinit)
    NULL,               // Parameters
    1,                  // Priority
    &uploadTaskHandle,  // Handle
    0                   // Core 0
  );

  // Subscribe the Arduino main loop task (Core 1) to the watchdog. The upload
  // task subscribes itself in its first iteration. Without this the wdt is
  // configured but watching nothing, so a Core 1 hang stays silent (firmware
  // 2026.05.03.01 hard hang). With this, a 120s stall produces a panic +
  // backtrace and reboots the device.
  esp_err_t wdt_err = esp_task_wdt_add(NULL);
  if (wdt_err != ESP_OK) {
    Serial.printf("[WDT] Failed to subscribe loopTask: %d\n", wdt_err);
  } else {
    Serial.println("[WDT] loopTask subscribed");
  }

  // Diagnostic heartbeat task. Runs independently of loopTask so it keeps
  // printing even when Core 1 hangs. The g_loopSection it prints is the
  // last section Core 1 entered before stalling.
  xTaskCreatePinnedToCore(
    diagnosticsTask,    // Function
    "diagTask",         // Name
    4096,               // Stack
    NULL,               // Params
    1,                  // Priority
    &diagTaskHandle,    // Handle
    0                   // Core 0 (Core 1 is the one we're watching)
  );

  // Stage 2 re-enabled in .13 after the root cause of the .10 fleet-brick
  // was found: a single-byte stack overflow in meshBuildAndSendBoatState
  // writing to p->reserved[2] when reserved was sized [2]. Fixed at the
  // source. setup() is now back to the .10 sequence (meshInit + radio
  // mode transition). __stack_chk_fail was a TRUE positive — exactly
  // doing its job catching the smash on every meshTick call.
  meshInit();
  radioModeTransition(MODE_IDLE, "setup complete");

  Serial.println("[SETUP] Complete - WiFi/telnet available, GPS acquiring in background");
}

// ============================================================
// CONFIGURE LG290P FOR PPK
// ============================================================
// RTCM3 MSM7 + ephemeris configuration is done via QGNSS and saved to NVM.
// See LG290P_CONFIG_UPDATES.md for QGNSS configuration steps.
// This function only configures NMEA messages and fix rate at runtime.
//
// Saved NVM config (via QGNSS):
// LG290P requires BASE STATION MODE for MSM output.
// MSM4 (1074/1084/1094/1124) is output even when MSM7 is requested.
// MSM4 is sufficient for centimeter-level PPK with RTKLIB.
//
// CFGRTCM does NOT persist — must be sent every boot.
// Do NOT save or restart after CFGRTCM.
// ============================================================
void configureLG290P() {
  // Configures the Waveshare LG290P for 10 Hz NMEA-only operation.
  //
  // PPK / RTCM3 raw-observation capture was removed in 2026.05.20.09 —
  // see docs/RTCM_PPK_ARCHIVE.md for the previous architecture and
  // git SHA 08cdadfe (firmware .08) for the last working PPK-era
  // configureLG290P + parsers + upload path. The trade-off:
  //   * 10 Hz nav fixes are critical for on-the-water OCS (over-line
  //     detection at race start) and per-tack motion analysis.
  //   * PPK gave decimeter accuracy *after* the race — useful but the
  //     LG290P's "MSM in Base mode only" lock meant we couldn't have
  //     both. OCS won.
  // The LG290P drives NMEA at the fix rate, so 10 Hz fix = 10 Hz
  // RMC/GGA/GSA/GSV. Rover mode is required to unlock the 10 Hz rate
  // per LG290P&LGx80P Protocol Spec v1.1 §2.3.28.
  Serial.println("[GPS] Configuring LG290P for Rover @ 10 Hz NMEA...");

  // Step 1: Query firmware version (for boot.log forensics)
  Serial.println("[GPS] Querying firmware version...");
  sendPQTM("PQTMVERNO");

  // Step 2: Check current receiver mode
  Serial.println("[GPS] Checking receiver mode...");
  sendPQTM("PQTMCFGRCVRMODE,R");
  delay(300);

  // Step 3: Set Rover mode (unlocks 10 Hz fix rate)
  Serial.println("[GPS] Setting Rover mode...");
  sendPQTM("PQTMCFGRCVRMODE,W,1");
  delay(200);

  // Step 4: Enable NMEA messages. Rover mode keeps NMEA on by default
  // but explicit rates ensure a previous base-mode session (which
  // auto-disabled NMEA) re-enables cleanly.
  Serial.println("[GPS] Enabling NMEA messages...");
  sendPQTM("PQTMCFGMSGRATE,W,GGA,1");
  sendPQTM("PQTMCFGMSGRATE,W,RMC,1");
  sendPQTM("PQTMCFGMSGRATE,W,GSA,1");
  sendPQTM("PQTMCFGMSGRATE,W,GSV,1");

  // Step 5: Set fix rate to 100 ms (10 Hz). BEFORE save+restart so the
  // new rate is in NVM and applied by the same-boot restart.
  Serial.println("[GPS] Setting fix rate to 10 Hz (100 ms)...");
  sendPQTM("PQTMCFGFIXRATE,W,100");
  delay(200);

  // Step 6: Save NVM + restart to apply mode + rate together
  Serial.println("[GPS] Saving to NVM...");
  sendPQTM("PQTMSAVEPAR");
  delay(500);

  Serial.println("[GPS] Restarting module...");
  sendPQTM("PQTMSRR");
  delay(6000);

  // Drain any buffered data after restart
  while (Serial2.available()) Serial2.read();

  // Step 7: Verify — read back active configuration
  Serial.println("[GPS] Verifying configuration...");
  sendPQTM("PQTMCFGRCVRMODE,R");
  sendPQTM("PQTMCFGFIXRATE,R");

  Serial.println("[GPS] Configuration complete:");
  Serial.println("[GPS]   Mode: Rover @ 10 Hz");
  Serial.println("[GPS]   NMEA: GGA, RMC, GSA, GSV @ 10 Hz");
  Serial.println("[GPS]   RTCM3: disabled (PPK retired — see docs/RTCM_PPK_ARCHIVE.md)");
}

// ============================================================
// READ GPS — NMEA text only (RTCM3 capture retired in .09)
// ============================================================
void readGPS() {
  while (Serial2.available()) {
    uint8_t c = Serial2.read();
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
// BATTERY MONITORING (DWEII USB-C Boost Converter)
// ============================================================
// 100K/100K voltage divider from LiPo B+ to GPIO34
// Divider ratio: nominal 2.0 (100K/100K), calibrated to 2.25
// ESP32 ADC has ~10-15% non-linearity without calibration
// Calibrated: 4.165V actual = 3.70V displayed → ratio = 4.165/3.70 * 2.0 = 2.25
// LiPo range 3.0V-4.2V → ADC sees 1.5V-2.1V (within ESP32 3.3V limit)
// Current drain: 0.021mA (negligible)
const float BATT_DIVIDER_RATIO = 2.25;
const int BATT_SAMPLES = 16;  // Average multiple readings for stability

void setupBattery() {
  // GPIO34 is input-only, no internal pull-up (ideal for ADC)
  analogReadResolution(12);  // 12-bit ADC (0-4095)
  analogSetAttenuation(ADC_11db);  // Full 0-3.3V range
}

float readBatteryVoltage() {
  // Average multiple readings to reduce noise
  uint32_t sum = 0;
  for (int i = 0; i < BATT_SAMPLES; i++) {
    sum += analogRead(BATT_VOLTAGE_PIN);
    delayMicroseconds(100);
  }
  float raw = (float)sum / BATT_SAMPLES;
  float adcVoltage = (raw / 4095.0) * 3.3;  // Voltage at ADC pin
  float voltage = adcVoltage * BATT_DIVIDER_RATIO;  // Actual battery voltage
  return voltage;
}

int getBatteryPercent(float voltage) {
  // Li-ion discharge curve (non-linear lookup table)
  // Based on typical LiPo discharge profile
  // Voltage drops quickly at start and end, flat in middle
  static const float voltageTable[] = {
    4.20, 4.15, 4.10, 4.05, 4.00, 3.90, 3.80, 3.70, 3.60, 3.50, 3.40, 3.30
  };
  static const int percentTable[] = {
    100,   95,   85,   75,   65,   50,   35,   20,   12,    6,    2,    0
  };
  static const int tableSize = sizeof(voltageTable) / sizeof(voltageTable[0]);

  if (voltage >= voltageTable[0]) return 100;
  if (voltage <= voltageTable[tableSize - 1]) return 0;

  // Find bracketing points and interpolate
  for (int i = 0; i < tableSize - 1; i++) {
    if (voltage >= voltageTable[i + 1]) {
      float vHigh = voltageTable[i];
      float vLow = voltageTable[i + 1];
      int pHigh = percentTable[i];
      int pLow = percentTable[i + 1];
      // Linear interpolation between points
      float ratio = (voltage - vLow) / (vHigh - vLow);
      return pLow + (int)(ratio * (pHigh - pLow));
    }
  }
  return 0;
}

bool isBatteryCritical() {
  // Critical if voltage drops below 3.3V (overdischarge protection threshold)
  // Only consider critical if voltage is measurable (> 0.5V)
  // This prevents false shutdown when battery hardware not connected
  bool voltageValid = battery.voltage > 0.5;
  bool voltageLow = battery.voltage < 3.3;
  return voltageValid && voltageLow;
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
    if (windFile) { windFile.flush(); windFile.close(); }
    if (presFile) { presFile.flush(); presFile.close(); }
    logging = false;
  }

  // Display warning - user must flip hardware power switch
  if (oledOK) {
    tft.fillScreen(COLOR_ERROR);
    tft.setTextColor(TFT_WHITE, COLOR_ERROR);
    tft.setTextDatum(MC_DATUM);
    tft.drawString("LOW BATTERY", SCREEN_WIDTH/2, SCREEN_HEIGHT/2 - 40, 4);
    tft.drawString("Flip power switch to OFF", SCREEN_WIDTH/2, SCREEN_HEIGHT/2 + 20, 2);
  }

  // Halt here - user must use hardware switch
  while (true) {
    delay(1000);
  }
}

// Draw battery percentage at specified position (legacy function, battery now in main display)
void drawBatteryPercent(int x, int y) {
  if (!battery.valid) return;

  char buf[8];
  snprintf(buf, sizeof(buf), "%d%%", battery.percent);

  // Blink if critical
  if (battery.critical && (millis() / 500) % 2 == 0) {
    // Don't draw (blink off)
  } else {
    uint16_t batColor = (battery.percent > 30) ? COLOR_GOOD :
                        (battery.percent > 15) ? COLOR_WARN : COLOR_ERROR;
    tft.setTextColor(batColor, COLOR_BG);
    tft.setTextDatum(TL_DATUM);
    tft.drawString(buf, x, y, 2);
  }
}

// ============================================================
// READ IMU
// ============================================================
void readIMU() {
  if (!imuOK) return;

  // BNO085 using Adafruit library with SHTP protocol
  if (bno08x.wasReset()) {
    Serial.println("[IMU] BNO085 was reset, re-enabling reports");
    bno08x.enableReport(SH2_GAME_ROTATION_VECTOR, IMU_INTERVAL_MS * 1000);
    bno08x.enableReport(SH2_ROTATION_VECTOR, IMU_INTERVAL_MS * 1000);
    bno08x.enableReport(SH2_ACCELEROMETER, IMU_INTERVAL_MS * 1000);
    bno08x.enableReport(SH2_GYROSCOPE_CALIBRATED, IMU_INTERVAL_MS * 1000);
    bno08x.enableReport(SH2_LINEAR_ACCELERATION, IMU_INTERVAL_MS * 1000);
    bno08x.enableReport(SH2_STABILITY_CLASSIFIER, 500000);
    bno08x.enableReport(SH2_MAGNETIC_FIELD_CALIBRATED, IMU_INTERVAL_MS * 1000);
  }

  // Read sensor events - we have 7 reports enabled so need enough reads
  int maxReads = 10;
  int eventsThisCall = 0;
  while (maxReads-- > 0 && bno08x.getSensorEvent(&sensorValue)) {
    eventsThisCall++;
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

        // Accuracy estimate (0-3, 3=highest calibration)
        imu.accuracy = sensorValue.status & 0x03;
        break;
      }

      case SH2_GYROSCOPE_CALIBRATED:
        // Angular velocity in rad/s - convert to deg/s for easier interpretation
        // Useful for detecting tack/gybe maneuvers (high yaw rate = turning)
        imu.gyro_x = sensorValue.un.gyroscope.x * 180.0 / PI;  // Roll rate (deg/s)
        imu.gyro_y = sensorValue.un.gyroscope.y * 180.0 / PI;  // Pitch rate (deg/s)
        imu.gyro_z = sensorValue.un.gyroscope.z * 180.0 / PI;  // Yaw/turn rate (deg/s)
        break;

      case SH2_LINEAR_ACCELERATION:
        // Acceleration with gravity removed - pure motion acceleration
        // Useful for detecting impacts, waves, sudden movements
        imu.linaccel_x = sensorValue.un.linearAcceleration.x;
        imu.linaccel_y = sensorValue.un.linearAcceleration.y;
        imu.linaccel_z = sensorValue.un.linearAcceleration.z;
        break;

      case SH2_STABILITY_CLASSIFIER:
        // Motion state: 0=Unknown, 1=OnTable, 2=Stationary, 3=Stable, 4=Motion
        // Useful for auto-detecting sailing vs at dock
        imu.stability = sensorValue.un.stabilityClassifier.classification;
        break;

      case SH2_MAGNETIC_FIELD_CALIBRATED:
        // Raw magnetic field in microtesla (uT)
        // Useful for analyzing magnetic interference from keel, rigging, engine
        // Earth's field is ~25-65 uT depending on location
        imu.mag_x = sensorValue.un.magneticField.x;
        imu.mag_y = sensorValue.un.magneticField.y;
        imu.mag_z = sensorValue.un.magneticField.z;
        break;
    }
  }

  // Health watchdog: at 1 Hz polling we expect at least one sensor
  // event per call when the BNO is alive. Track consecutive empty
  // reads and flip into "failed" after IMU_FAIL_THRESHOLD_S of silence.
  // boot.log gets one marker on each transition (failed / recovered).
  if (eventsThisCall > 0) {
    g_imuLastEventMs = millis();
    g_imuSilentReads = 0;
    if (g_imuFailed) {
      g_imuFailed = false;
      char isoBuf[24] = {0};
      bool haveIso = formatGpsIso(isoBuf, sizeof(isoBuf));
      char line[96];
      snprintf(line, sizeof(line), "imu ok t=%s recovered",
               haveIso ? isoBuf : "?");
      appendBootLog(line);
      Serial.println("[IMU] recovered, events resuming");
    }
  } else {
    g_imuSilentReads++;
    if (!g_imuFailed && g_imuSilentReads >= IMU_FAIL_THRESHOLD_S) {
      g_imuFailed = true;
      char isoBuf[24] = {0};
      bool haveIso = formatGpsIso(isoBuf, sizeof(isoBuf));
      char line[128];
      snprintf(line, sizeof(line),
               "imu fail t=%s reason=no_events silent_reads=%d",
               haveIso ? isoBuf : "?", g_imuSilentReads);
      appendBootLog(line);
      Serial.printf("[IMU] FAILED — %d s with no sensor events\n",
                    g_imuSilentReads);
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
    else if (k == "wind_enabled") config.wind_enabled = (v == "true" || v == "1");
    else if (k == "wind_mac") v.toCharArray(config.wind_mac, sizeof(config.wind_mac));
    else if (k == "wind_offset") config.wind_offset = v.toInt();
    // Recording thresholds
    else if (k == "start_speed_knots") config.start_speed_knots = v.toFloat();
    else if (k == "stop_speed_knots") config.stop_speed_knots = v.toFloat();
    else if (k == "start_delay_sec") config.start_delay_sec = v.toInt();
    else if (k == "stop_delay_sec") config.stop_delay_sec = v.toInt();
    // v2.0.0 foundation
    else if (k == "hardware_platform") v.toCharArray(config.hardware_platform, sizeof(config.hardware_platform));
    else if (k == "unit_role")         v.toCharArray(config.unit_role, sizeof(config.unit_role));
    else if (k == "config_version")    config.config_version = v.toInt();
  }
  f.close();

  // Map textual platform/role into typed globals. Unknown values fall back
  // to defaults rather than rejecting the config — keeps the device booting
  // even if a cloud-pushed config arrives with a future role name.
  if (strcasecmp(config.hardware_platform, "b1") == 0) g_hw = HW_B1;
  else                                                  g_hw = HW_E1;

  if      (strcasecmp(config.unit_role, "racing_boat")     == 0) g_role = ROLE_RACING_BOAT;
  else if (strcasecmp(config.unit_role, "rc_signal")       == 0) g_role = ROLE_RC_SIGNAL;
  else if (strcasecmp(config.unit_role, "rc_pin")          == 0) g_role = ROLE_RC_PIN;
  else if (strcasecmp(config.unit_role, "mark")            == 0) g_role = ROLE_MARK;
  else if (strcasecmp(config.unit_role, "committee_chase") == 0) g_role = ROLE_COMMITTEE_CHASE;
  else if (strcasecmp(config.unit_role, "spare")           == 0) g_role = ROLE_SPARE;
  else                                                            g_role = ROLE_RACING_BOAT;

  Serial.printf("[CFG] Boat: %s, WiFi networks: %d\n",
    config.boat_id, config.wifi_count);
  for (int i = 0; i < config.wifi_count; i++) {
    Serial.printf("[CFG]   %d: %s\n", i + 1, config.wifi[i].ssid);
  }
  Serial.printf("[CFG] Wind: %s", config.wind_enabled ? "enabled" : "disabled");
  if (strlen(config.wind_mac) > 0) {
    Serial.printf(" (MAC: %s)", config.wind_mac);
  }
  if (config.wind_offset != 0) {
    Serial.printf(" (offset: %d°)", config.wind_offset);
  }
  Serial.println();
  Serial.printf("[CFG] Platform: %s | Role: %s | Config version: %d\n",
                hwName(g_hw), roleName(g_role), config.config_version);
  Serial.printf("[CFG] Sample rates (firmware-baked): IMU %d ms | GNSS fix %d ms\n",
                IMU_INTERVAL_MS, 1000 / 10);  // GNSS via PQTMCFGFIXRATE,W,100
}

// Stage 5.5 — per-class bow_offset registry, loaded from /sf/classes.csv.
// Only meaningful on RC unit (role=rc_signal); racing boats just use their
// own OCS_BOW_OFFSET_M constant via ocsTick(). Quietly skipped on roles
// other than rc_signal to save SD I/O at boot.
//
// File format (header optional; case-insensitive):
//   boat_id,class,bow_offset_m
//   E1,Sonar23,2.4
//   F1,J80,2.8
//
// Lines starting with '#' or whitespace-only are skipped. Empty file
// or missing file is non-fatal — RC falls back to OCS_BOW_OFFSET_M for
// every peer.
void loadClassRegistry() {
  g_class_registry_count = 0;
  if (g_role != ROLE_RC_SIGNAL) return;

  File f = SD.open("/sf/classes.csv", FILE_READ);
  if (!f) f = SD.open("/classes.csv", FILE_READ);  // fallback to root
  if (!f) {
    Serial.println("[CLASS] No classes.csv — RC will use OCS_BOW_OFFSET_M for all peers");
    return;
  }
  Serial.println("[CLASS] Loading classes.csv");

  while (f.available() && g_class_registry_count < CLASS_REGISTRY_MAX) {
    String line = f.readStringUntil('\n');
    line.trim();
    if (line.length() == 0 || line.startsWith("#")) continue;
    // Skip header row if it starts with "boat_id" (case-insensitive)
    String low = line; low.toLowerCase();
    if (low.startsWith("boat_id")) continue;

    int c1 = line.indexOf(',');
    if (c1 < 0) continue;
    int c2 = line.indexOf(',', c1 + 1);
    if (c2 < 0) continue;

    String boat = line.substring(0, c1); boat.trim();
    String cls  = line.substring(c1 + 1, c2); cls.trim();
    String bow  = line.substring(c2 + 1); bow.trim();
    if (boat.length() == 0) continue;

    ClassRegistryEntry& e = g_class_registry[g_class_registry_count];
    boat.toCharArray(e.boat_id, sizeof(e.boat_id));
    cls.toCharArray(e.class_name, sizeof(e.class_name));
    e.bow_offset_m = bow.toFloat();
    if (e.bow_offset_m <= 0.0f) e.bow_offset_m = OCS_BOW_OFFSET_M;
    e.sender_id = boatIdHash(e.boat_id);
    g_class_registry_count++;
  }
  f.close();

  Serial.printf("[CLASS] Loaded %d entries:\n", g_class_registry_count);
  for (int i = 0; i < g_class_registry_count; i++) {
    Serial.printf("[CLASS]   %s (0x%08lx) class=%s bow=%.2fm\n",
                  g_class_registry[i].boat_id,
                  (unsigned long)g_class_registry[i].sender_id,
                  g_class_registry[i].class_name,
                  g_class_registry[i].bow_offset_m);
  }
}

// Lookup bow_offset_m for a given peer sender_id. Returns the
// hardcoded default when registry has no entry — safe fallback so
// new boats joining the fleet without a registry entry still get
// OCS computed (just with the class-default bow offset).
float bowOffsetForSender(uint32_t sender_id) {
  for (int i = 0; i < g_class_registry_count; i++) {
    if (g_class_registry[i].sender_id == sender_id)
      return g_class_registry[i].bow_offset_m;
  }
  return OCS_BOW_OFFSET_M;
}

// ============================================================
// OTA + TELNET SETUP
// ============================================================
void setupOTA() {
#if ENABLE_ARDUINO_OTA
  ArduinoOTA.setHostname(config.boat_id);  // Use boat ID as hostname
  ArduinoOTA.setPassword("sailframes");     // OTA password

  ArduinoOTA.onStart([]() {
    otaInProgress = true;
    String type = (ArduinoOTA.getCommand() == U_FLASH) ? "firmware" : "filesystem";
    Serial.printf("[OTA] Start updating %s\n", type.c_str());

    // Show on display
    if (oledOK) {
      tft.fillScreen(COLOR_WARN);
      tft.setTextColor(TFT_BLACK, COLOR_WARN);
      tft.setTextDatum(MC_DATUM);
      tft.drawString("OTA UPDATE", SCREEN_WIDTH/2, SCREEN_HEIGHT/2 - 30, 4);
      tft.drawString("DO NOT POWER OFF", SCREEN_WIDTH/2, SCREEN_HEIGHT/2 + 20, 2);
    }

    // Close log files before OTA
    if (logging) {
      navFile.close();
      if (imuFile) imuFile.close();
      logging = false;
    }
  });

  ArduinoOTA.onEnd([]() {
    otaInProgress = false;
    Serial.println("\n[OTA] Complete! Rebooting...");
    if (oledOK) {
      tft.fillScreen(COLOR_GOOD);
      tft.setTextColor(TFT_BLACK, COLOR_GOOD);
      tft.setTextDatum(MC_DATUM);
      tft.drawString("REBOOTING...", SCREEN_WIDTH/2, SCREEN_HEIGHT/2, 4);
    }
  });

  ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
    int pct = progress / (total / 100);
    Serial.printf("[OTA] Progress: %u%%\r", pct);

    // Update progress bar on display
    if (oledOK) {
      tft.fillScreen(COLOR_WARN);
      tft.setTextColor(TFT_BLACK, COLOR_WARN);
      tft.setTextDatum(MC_DATUM);
      tft.drawString("UPDATING FIRMWARE", SCREEN_WIDTH/2, SCREEN_HEIGHT/2 - 60, 4);

      // Progress bar background (400px wide, 30px tall)
      int barX = 40, barY = SCREEN_HEIGHT/2 - 15;
      int barW = 400, barH = 30;
      tft.drawRect(barX, barY, barW, barH, TFT_BLACK);
      tft.fillRect(barX + 2, barY + 2, (barW - 4) * pct / 100, barH - 4, COLOR_GOOD);

      // Percentage text
      char buf[16];
      snprintf(buf, sizeof(buf), "%d%%", pct);
      tft.drawString(buf, SCREEN_WIDTH/2, SCREEN_HEIGHT/2 + 50, 4);
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
      tft.fillScreen(COLOR_ERROR);
      tft.setTextColor(TFT_WHITE, COLOR_ERROR);
      tft.setTextDatum(MC_DATUM);
      tft.drawString("OTA ERROR!", SCREEN_WIDTH/2, SCREEN_HEIGHT/2, 4);
    }
  });

  ArduinoOTA.begin();
  Serial.println("[OTA] Ready");
#else
  Serial.println("[OTA] ArduinoOTA disabled in firmware (see ENABLE_ARDUINO_OTA)");
#endif
}

void startTelnetServer() {
  telnetServer.begin();
  telnetServer.setNoDelay(true);
  telnetServerRunning = true;
  Serial.println("[TELNET] Server started on port 23");
}

void handleTelnet() {
  // Bail if the listener was never started OR if Core 0 is mid-upload.
  // telnetServer.hasClient() goes through LWIP and deadlocks under
  // sustained Core 0 traffic (firmware 2026.05.03.04 fleet hang).
  if (!telnetServerRunning || wifiBusy) return;

  // Check for new clients
  if (telnetServer.hasClient()) {
    if (!telnetClient || !telnetClient.connected()) {
      if (telnetClient) telnetClient.stop();
      telnetClient = telnetServer.available();
      telnetClient.println("\n=================================");
      telnetClient.printf("  SailFrames E1 %s\n", FW_VERSION);
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

  // Build file paths (RTCM3 raw capture retired in .09 — see archive doc)
  char np[64], ip[64], wp[64], pp[64];
  snprintf(np, sizeof(np), "%s/%s_%s_%s_nav.csv", dd, config.boat_id, ds, ts);
  snprintf(ip, sizeof(ip), "%s/%s_%s_%s_imu.csv", dd, config.boat_id, ds, ts);
  snprintf(wp, sizeof(wp), "%s/%s_%s_%s_wind.csv", dd, config.boat_id, ds, ts);
  snprintf(pp, sizeof(pp), "%s/%s_%s_%s_pres.csv", dd, config.boat_id, ds, ts);

  Serial.printf("[LOG] Opening NAV: %s\n", np);
  navFile = SD.open(np, FILE_WRITE);
  Serial.printf("[LOG] NAV file %s\n", navFile ? "OK" : "FAILED");

  Serial.printf("[LOG] Opening IMU: %s\n", ip);
  imuFile = SD.open(ip, FILE_WRITE);
  Serial.printf("[LOG] IMU file %s\n", imuFile ? "OK" : "FAILED");

#if ENABLE_WIND
  if (config.wind_enabled) {
    Serial.printf("[LOG] Opening WIND: %s\n", wp);
    windFile = SD.open(wp, FILE_WRITE);
    Serial.printf("[LOG] WIND file %s\n", windFile ? "OK" : "FAILED");
  }
#endif

  // Pressure file (always open if sensor is present)
  if (presOK) {
    Serial.printf("[LOG] Opening PRES: %s\n", pp);
    presFile = SD.open(pp, FILE_WRITE);
    Serial.printf("[LOG] PRES file %s\n", presFile ? "OK" : "FAILED");
  }

  if (navFile) {
    logging = true;
    logStart = millis();
    navFile.println("ms,utc,lat,lon,alt,sog,cog,sat,hdop,fix,gps_date");
    navFile.flush();
    if (imuFile) {
      imuFile.println("ms,utc,ax,ay,az,gx,gy,gz,lax,lay,laz,mx,my,mz,heel,pitch,heading,stability,accuracy");
      imuFile.flush();
    }
#if ENABLE_WIND
    if (windFile) {
      windFile.println("ms,utc,aws_kts,aws_mps,awa_deg,battery");
      windFile.flush();
    }
#endif
    if (presFile) {
      presFile.println("ms,utc,date,pressure_hpa,temp_c,pres_min,pres_max");
      presFile.flush();
      resetPressureMinMax();  // Start fresh min/max tracking
    }
    Serial.println("[LOG] ========================================");
    Serial.printf("[LOG] NAV: %s\n", np);
    Serial.printf("[LOG] IMU: %s\n", ip);
#if ENABLE_WIND
    if (config.wind_enabled) Serial.printf("[LOG] WIND: %s\n", wp);
#endif
    if (presOK) Serial.printf("[LOG] PRES: %s\n", pp);
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
  sdWriting = true;
  unsigned long e = millis() - logStart;
  navFile.printf("%lu,%s,%.10f,%.10f,%.3f,%.3f,%.2f,%d,%.2f,%d,%s\n",
    e, gps.utc_time, gps.lat, gps.lon, gps.alt,
    gps.speed_kts, gps.course, gps.satellites, gps.hdop, gps.fix_quality, gps.date);
  totalBytes += 90;
  sdWriting = false;
}

void logIMU() {
  if (!imuFile || !logging) return;
  sdWriting = true;
  unsigned long e = millis() - logStart;
  if (g_imuFailed) {
    // BNO has gone silent — every field would be stale. Write empty
    // cells for the derived/orientation fields so downstream (dashboard
    // parseFloat → NaN → row skipped) doesn't treat the stale value as
    // a real reading. Keep ms + utc_time so the row alignment with GPS
    // is preserved for forensic inspection.
    imuFile.printf("%lu,%s,,,,,,,,,,,,,,,,,\n", e, gps.utc_time);
    totalBytes += 30;
  } else {
    imuFile.printf("%lu,%s,%.4f,%.4f,%.4f,%.2f,%.2f,%.2f,%.3f,%.3f,%.3f,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%u,%u\n",
      e, gps.utc_time,
      imu.accel_x, imu.accel_y, imu.accel_z,           // Raw acceleration (with gravity)
      imu.gyro_x, imu.gyro_y, imu.gyro_z,              // Angular velocity (deg/s)
      imu.linaccel_x, imu.linaccel_y, imu.linaccel_z,  // Linear acceleration (no gravity)
      imu.mag_x, imu.mag_y, imu.mag_z,                 // Magnetic field (uT)
      imu.heel, imu.pitch, imu.heading,                // Orientation
      imu.stability, imu.accuracy);                    // Motion state & calibration quality
    totalBytes += 210;
  }
  sdWriting = false;
}

// ============================================================
// DISPLAY
// ============================================================

// Display mode: 1 = D1 (simple big numbers), 2 = D2 (nav + wind), 3 = D3 (wind focus)
int displayMode = 2;  // D2 Vakaros-style nav + wind

// Previous values for efficient redraw (only update what changed)
static float prevSOG = -1, prevCOG = -1, prevHeel = -1, prevPitch = -1;
static int prevBattery = -1, prevSats = -1;
static bool prevRecording = false;
static unsigned long lastFullRedraw = 0;

// D1: Simple display - PORTRAIT 320x480 with large high-contrast numbers
void updateDisplayD1() {
  if (!oledOK) return;

  char buf[32];
  static bool layoutDrawn = false;
  bool forceRedraw = !layoutDrawn || (millis() - lastFullRedraw > 60000);  // Reduce blink: 60s

  // Check for warnings
  bool hasWarning = false;
  const char* warnMsg = nullptr;
  uint16_t warnColor = COLOR_ERROR;

  if (!sdOK) {
    warnMsg = "NO SD";
    hasWarning = true;
  } else if (lastValidGPS == 0 && millis() > 120000) {
    warnMsg = "NO GPS";
    warnColor = COLOR_WARN;
    hasWarning = true;
  } else if (lastValidGPS > 0 && millis() - lastValidGPS > 60000) {
    warnMsg = "GPS LOST";
    hasWarning = true;
  }

  // Full screen redraw only on first run or every 60s
  if (forceRedraw) {
    tft.fillScreen(COLOR_BG);
    lastFullRedraw = millis();
    layoutDrawn = true;

    // Draw static labels - high contrast white
    tft.setTextColor(TFT_DARKGREY, COLOR_BG);
    tft.setTextDatum(TC_DATUM);
    tft.drawString("SOG", SCREEN_WIDTH/2, 5, 4);
    tft.drawString("COG", SCREEN_WIDTH/2, 165, 4);
    tft.drawString("HEEL", SCREEN_WIDTH/4, 325, 2);
    tft.drawString("BAT", 3*SCREEN_WIDTH/4, 325, 2);

    // Divider lines
    tft.drawFastHLine(0, 160, SCREEN_WIDTH, COLOR_DIVIDER);
    tft.drawFastHLine(0, 320, SCREEN_WIDTH, COLOR_DIVIDER);
    tft.drawFastVLine(SCREEN_WIDTH/2, 320, 120, COLOR_DIVIDER);
    tft.drawFastHLine(0, 440, SCREEN_WIDTH, COLOR_DIVIDER);

    // Force value updates
    prevSOG = prevCOG = prevHeel = -999;
    prevBattery = -1;
  }

  // Warning banner at top
  if (hasWarning && warnMsg) {
    tft.fillRect(0, 0, SCREEN_WIDTH, 30, warnColor);
    tft.setTextColor(TFT_BLACK, warnColor);
    tft.setTextDatum(MC_DATUM);
    tft.drawString(warnMsg, SCREEN_WIDTH/2, 15, 4);
  }

  // SOG - HUGE white numbers (Font 8 = 75px)
  if (abs(gps.speed_kts - prevSOG) > 0.05 || forceRedraw) {
    prevSOG = gps.speed_kts;
    tft.fillRect(0, 35, SCREEN_WIDTH, 120, COLOR_BG);
    tft.setTextColor(TFT_WHITE, COLOR_BG);
    tft.setTextDatum(MC_DATUM);
    snprintf(buf, sizeof(buf), "%.1f", gps.speed_kts);
    tft.drawString(buf, SCREEN_WIDTH/2, 95, 8);  // Font 8 = 75px
  }

  // COG - HUGE white numbers (Font 8 = 75px)
  if (abs(gps.course - prevCOG) > 0.5 || forceRedraw) {
    prevCOG = gps.course;
    tft.fillRect(0, 195, SCREEN_WIDTH, 120, COLOR_BG);
    tft.setTextColor(TFT_WHITE, COLOR_BG);
    tft.setTextDatum(MC_DATUM);
    snprintf(buf, sizeof(buf), "%03d", (int)gps.course);
    tft.drawString(buf, SCREEN_WIDTH/2, 255, 8);  // Font 8 = 75px
  }

  // Heel - large (Font 7 = 48px)
  if (abs(imu.heel - prevHeel) > 0.5 || forceRedraw) {
    prevHeel = imu.heel;
    tft.fillRect(0, 345, SCREEN_WIDTH/2 - 5, 70, COLOR_BG);
    uint16_t heelColor = (abs(imu.heel) > 25) ? COLOR_WARN : TFT_WHITE;
    tft.setTextColor(heelColor, COLOR_BG);
    tft.setTextDatum(MC_DATUM);
    snprintf(buf, sizeof(buf), "%+.0f", imu.heel);
    tft.drawString(buf, SCREEN_WIDTH/4, 380, 7);
  }

  // Battery - large (Font 7 = 48px)
  if (battery.percent != prevBattery || forceRedraw) {
    prevBattery = battery.percent;
    tft.fillRect(SCREEN_WIDTH/2 + 5, 345, SCREEN_WIDTH/2 - 5, 70, COLOR_BG);
    uint16_t batColor = (battery.percent > 30) ? COLOR_GOOD :
                        (battery.percent > 15) ? COLOR_WARN : COLOR_ERROR;
    tft.setTextColor(batColor, COLOR_BG);
    tft.setTextDatum(MC_DATUM);
    snprintf(buf, sizeof(buf), "%d", battery.percent);
    tft.drawString(buf, 3*SCREEN_WIDTH/4, 380, 7);
  }

  // Status bar at bottom (always update - use overwrite)
  tft.fillRect(0, 442, SCREEN_WIDTH, 38, COLOR_BG);
  int dispSats = (gps.satellites >= 0 && gps.satellites <= 50) ? gps.satellites : 0;
  const char* fixStr = (gps.fix_quality == 2) ? "SBAS" : (gps.fix_quality == 1) ? "GPS" : "---";
  const char* recStr = getRecStateStr();

  // Left: Recording state
  uint16_t recColor = logging ? COLOR_GOOD : COLOR_LABEL;
  tft.setTextColor(recColor, COLOR_BG);
  tft.setTextDatum(TL_DATUM);
  tft.drawString(recStr, 5, 452, 4);

  // Right: Satellites
  snprintf(buf, sizeof(buf), "%s %d", fixStr, dispSats);
  tft.setTextColor(COLOR_LABEL, COLOR_BG);
  tft.setTextDatum(TR_DATUM);
  tft.drawString(buf, SCREEN_WIDTH - 5, 452, 4);
}

// D2: VAKAROS-STYLE - White background, black numbers, high contrast
// Static variables for change detection
static float prevTWS = -1, prevTWA = -1, prevTWD = -1, prevAWS2 = -1, prevAWA2 = -1;
static int prevDispSats = -1;
static int prevRecState = -1;
// d2LayoutDrawn declared globally near other display flags

// Compact firmware tag for the status bar — YY.MM.DD.N from
// FW_VERSION "YYYY.MM.DD.NN" (e.g. "26.05.20.2" from "2026.05.20.02").
// Lazy-cached.
static const char* fwShortTag() {
  static char buf[16] = {0};
  if (!buf[0]) {
    int yyyy = 0, mm = 0, dd = 0, n = 0;
    if (sscanf(FW_VERSION, "%d.%d.%d.%d", &yyyy, &mm, &dd, &n) == 4) {
      snprintf(buf, sizeof(buf), "%02d.%02d.%02d.%d", yyyy % 100, mm, dd, n);
    } else {
      snprintf(buf, sizeof(buf), "%s", FW_VERSION);
    }
  }
  return buf;
}

void updateDisplayD2() {
  if (!oledOK) return;

  // Throttle display updates (SD on separate HSPI bus, so no SPI contention)
  unsigned long now = millis();
  if (now - lastDisplayUpdate < DISPLAY_UPDATE_INTERVAL) return;  // 200ms
  lastDisplayUpdate = now;

  char buf[32];

  // Calculate true wind from apparent wind + boat speed
  float aws = 0, awa = 0, tws = 0, twa = 0, twd = 0;
#if ENABLE_WIND
  if (wind.connected && wind.lastUpdate > 0 && millis() - wind.lastUpdate < 5000) {
    aws = wind.speed_kts;
    awa = wind.angle_deg + config.wind_offset;
    if (awa < 0) awa += 360;
    if (awa >= 360) awa -= 360;
    float awaRad = awa * PI / 180.0;
    if (awaRad > PI) awaRad -= 2 * PI;
    float sog = gps.speed_kts;
    tws = sqrt(aws*aws + sog*sog - 2*aws*sog*cos(awaRad));
    float twaRad = atan2(aws * sin(awaRad), aws * cos(awaRad) - sog);
    twa = twaRad * 180.0 / PI;
    if (twa < 0) twa += 360;
    twd = gps.course + twa;
    if (twd >= 360) twd -= 360;
    if (twd < 0) twd += 360;
  }
#endif

  // Draw layout ONCE - labels and dividers
  if (!d2LayoutDrawn) {
    tft.fillScreen(COLOR_BG);
    d2LayoutDrawn = true;

    // VAKAROS-STYLE: White bg, bold black numbers
    // Top bar: BLACK bg, WHITE text
    // Bottom bar: BLACK bg, WHITE text (includes wind data if enabled)
    // Layout:
    // [BLACK: REC | SAT x HDOP x.x]  (30px)
    // [COG  000                   ]  (190px) - Font 8 x2
    // [SOG  00                    ]  (190px) - Font 8 x2
    // [BLACK: H P AWS AWA / BAT% W | WiFi N R ]  (50px, two rows)

    // BLACK bars for top and bottom
    tft.fillRect(0, 0, SCREEN_WIDTH, 30, TFT_BLACK);
    tft.fillRect(0, 440, SCREEN_WIDTH, 40, TFT_BLACK);

    // Divider between COG and SOG
    uint16_t lineColor = tft.color565(180, 180, 180);
    tft.drawFastHLine(0, 220, SCREEN_WIDTH, lineColor);

    // Labels for COG and SOG - LARGE (Font 4 = 26px)
    uint16_t labelColor = tft.color565(100, 100, 100);
    tft.setTextColor(labelColor, COLOR_BG);
    tft.setTextDatum(TL_DATUM);
    tft.drawString("COG", 5, 35, 4);
    tft.drawString("SOG", 5, 225, 4);

    // Reset prev values
    prevSOG = prevCOG = prevHeel = prevPitch = -999;
    prevTWS = prevTWA = prevTWD = prevAWS2 = prevAWA2 = -999;
    prevBattery = prevDispSats = prevRecState = -1;
  }

  // Status bar - with fix type, SAT count, HDOP
  int dispSats = (gps.satellites >= 0 && gps.satellites <= 50) ? gps.satellites : 0;
  static float prevHDOP = -1;
  static int prevFixQ = -1;
  if (dispSats != prevDispSats || recState != prevRecState ||
      abs(gps.hdop - prevHDOP) > 0.1 || gps.fix_quality != prevFixQ) {
    prevDispSats = dispSats;
    prevRecState = recState;
    prevHDOP = gps.hdop;
    prevFixQ = gps.fix_quality;

    // TOP BAR: WHITE text on BLACK background
    // Clear entire top bar first to prevent any ghosting
    tft.fillRect(0, 0, SCREEN_WIDTH, 30, TFT_BLACK);

    // Recording state (left side)
    const char* recStr;
    switch (recState) {
      case REC_IDLE: recStr = gps.valid ? "READY" : "NO GPS"; break;
      case REC_ARMED: recStr = "ARM"; break;
      case REC_RECORDING: recStr = "REC"; break;
      case REC_STOPPING: recStr = "STOP"; break;
      default: recStr = "---"; break;
    }
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    tft.setTextDatum(TL_DATUM);
    tft.drawString(recStr, 5, 7, 2);

    // Fix type (0=none, 1=GPS, 2=DGPS/SBAS, 4=RTK fix, 5=RTK float)
    const char* fixStr;
    switch (gps.fix_quality) {
      case 1: fixStr = "GPS"; break;
      case 2: fixStr = "SBAS"; break;
      case 4: fixStr = "RTK"; break;
      case 5: fixStr = "FLT"; break;
      default: fixStr = "---"; break;
    }
    tft.drawString(fixStr, 100, 7, 2);

    // SAT count
    tft.drawString("SAT", 145, 7, 2);
    snprintf(buf, sizeof(buf), "%2d", dispSats);
    tft.drawString(buf, 180, 7, 2);

    // HDOP
    tft.drawString("HDOP", 210, 7, 2);
    snprintf(buf, sizeof(buf), "%.1f", gps.hdop);
    tft.drawString(buf, 250, 7, 2);
    // WiFi status removed from top bar - shown on bottom bar instead
  }

  // COG - Font 8 x2 = 150px
  // COG area: 30-220 (190px), center at 130 (moved down to not overlap label)
  if (abs(gps.course - prevCOG) > 0.5) {
    prevCOG = gps.course;
    tft.fillRect(0, 60, SCREEN_WIDTH, 155, COLOR_BG);
    tft.setTextColor(TFT_BLACK, COLOR_BG);
    tft.setTextDatum(MC_DATUM);
    tft.setTextSize(2);
    snprintf(buf, sizeof(buf), "%03d", (int)gps.course);
    tft.drawString(buf, SCREEN_WIDTH/2, 130, 8);
    tft.setTextSize(1);
  }

  // SOG - Font 8 x2 = 150px. SOG area: 220-440 (190px), centre at 315.
  // Below 10 kt show one decimal ("8.9") so a tactical helmsman can
  // see the kn/10 trend; ≥10 kt show integer only ("12") because
  // three glyphs ("12.x") at this size run past the screen edges.
  // The narrow "." glyph keeps "9.9" the same effective width as
  // a two-digit "12", so the font size stays unchanged either way.
  static char prevSogBuf[8] = "";
  char newSogBuf[8];
  if (gps.speed_kts < 10.0f) {
    snprintf(newSogBuf, sizeof(newSogBuf), "%.1f", gps.speed_kts);
  } else {
    snprintf(newSogBuf, sizeof(newSogBuf), "%d", (int)(gps.speed_kts + 0.5f));
  }
  if (strcmp(newSogBuf, prevSogBuf) != 0) {
    strcpy(prevSogBuf, newSogBuf);
    prevSOG = gps.speed_kts;
    tft.fillRect(0, 250, SCREEN_WIDTH, 155, COLOR_BG);
    tft.setTextColor(TFT_BLACK, COLOR_BG);
    tft.setTextDatum(MC_DATUM);
    tft.setTextSize(2);
    tft.drawString(newSogBuf, SCREEN_WIDTH/2, 320, 8);
    tft.setTextSize(1);
  }

  // Bottom status bar - two rows on BLACK background
  static bool prevWindConnected = true;
  static bool prevWifiConnected = false;
  static unsigned long lastStatusUpdate = 0;

  static int prevPendingN = -1;
  bool statusChanged = (wind.connected != prevWindConnected) ||
                       (wifiConnected != prevWifiConnected) ||
                       (abs(imu.heel - prevHeel) > 0.5) ||
                       (abs(imu.pitch - prevPitch) > 0.5) ||
                       (abs(aws - prevAWS2) > 0.3) ||
                       (abs(awa - prevAWA2) > 1) ||
                       (battery.percent != prevBattery) ||
                       (pendingUploads != prevPendingN) ||
                       (millis() - lastStatusUpdate > 2000);

  if (statusChanged) {
    lastStatusUpdate = millis();
    prevWindConnected = wind.connected;
    prevWifiConnected = wifiConnected;
    prevHeel = imu.heel;
    prevPitch = imu.pitch;
    prevAWS2 = aws;
    prevAWA2 = awa;
    prevBattery = battery.percent;
    prevPendingN = pendingUploads;

    // BOTTOM BAR: Two rows - WHITE on BLACK
    // Clear entire bottom bar first (50px tall)
    tft.fillRect(0, 430, SCREEN_WIDTH, 50, TFT_BLACK);
    tft.setTextColor(TFT_WHITE, TFT_BLACK);

    // Row 1 (y=440): Heel + Pitch always; AWS + AWA appended when wind connected.
    // Single row keeps heel/pitch visible even with the wind sensor active.
    char row1[48];
    if (config.wind_enabled && wind.connected) {
      snprintf(row1, sizeof(row1), "H%+.0f P%+.0f  AWS%.1f AWA%03d",
               imu.heel, imu.pitch, aws, (int)awa);
    } else {
      snprintf(row1, sizeof(row1), "H %+.0f   P %+.0f", imu.heel, imu.pitch);
    }
    tft.setTextDatum(MC_DATUM);
    tft.drawString(row1, SCREEN_WIDTH/2, 440, 2);

    // Row 2 (y=458):
    //   Left:  "BAT N% W"   (W appears immediately after BAT% when wind connected)
    //   Right: WiFi indicator + upload counts (no W here — was blocking the counts)
    tft.setTextDatum(TL_DATUM);
    char left[32];
#if ENABLE_WIND
    bool windInd = (config.wind_enabled && wind.connected);
#else
    bool windInd = false;
#endif
    if (windInd) {
      snprintf(left, sizeof(left), "BAT %d%% W FW%s", battery.percent, fwShortTag());
    } else {
      snprintf(left, sizeof(left), "BAT %d%% FW%s", battery.percent, fwShortTag());
    }
    tft.drawString(left, 5, 456, 2);

    // Right side: WiFi + IP when idle, falls back to counts during traffic.
    // IP next to the SSID indicator gives a debug surface for telnet/curl
    // without needing the router DHCP table.
    char right[40];
    const char* wifiInd = wifiConnected ? getWifiIndicator() : "";

    if (uploading && uploadTotal > 0) {
      snprintf(right, sizeof(right), "%s %d/%d", wifiInd, uploadCount, uploadTotal);
    } else if (pendingUploads > 0) {
      snprintf(right, sizeof(right), "%s N%d", wifiInd, pendingUploads);
    } else if (wifiConnected) {
      snprintf(right, sizeof(right), "%s %s", wifiInd, WiFi.localIP().toString().c_str());
    } else {
      snprintf(right, sizeof(right), "%s", wifiInd);
    }
    tft.setTextDatum(TR_DATUM);
    tft.drawString(right, SCREEN_WIDTH - 5, 456, 2);
    tft.setTextDatum(TL_DATUM);
  }
}

// ============================================================
// D3: WIND + NAV - 7 values all at Font 8 (75px)
// ============================================================
static float prevD3AWS = -1, prevD3AWA = -1, prevD3TWS = -1, prevD3TWA = -1;
static float prevD3TWD = -1, prevD3SOG = -1, prevD3COG = -1, prevD3HDOP = -1;
static int prevD3RecState = -1, prevD3Sats = -1, prevD3FixQ = -1;
static bool d3LayoutDrawn = false;

void updateDisplayD3() {
  if (!oledOK) return;

  unsigned long now = millis();
  if (now - lastDisplayUpdate < DISPLAY_UPDATE_INTERVAL) return;
  lastDisplayUpdate = now;

  char buf[32];

  // Calculate true wind from apparent wind + boat speed
  float aws = 0, awa = 0, tws = 0, twa = 0, twd = 0;
#if ENABLE_WIND
  if (wind.connected && wind.lastUpdate > 0 && millis() - wind.lastUpdate < 5000) {
    aws = wind.speed_kts;
    awa = wind.angle_deg + config.wind_offset;
    if (awa < 0) awa += 360;
    if (awa >= 360) awa -= 360;
    float awaRad = awa * PI / 180.0;
    if (awaRad > PI) awaRad -= 2 * PI;
    float sog = gps.speed_kts;
    tws = sqrt(aws*aws + sog*sog - 2*aws*sog*cos(awaRad));
    float twaRad = atan2(aws * sin(awaRad), aws * cos(awaRad) - sog);
    twa = twaRad * 180.0 / PI;
    if (twa < 0) twa += 360;
    twd = gps.course + twa;
    if (twd >= 360) twd -= 360;
    if (twd < 0) twd += 360;
  }
#endif

  // Row geometry: 4 rows × 2 cols, each row 105px
  // y positions: row top, label at top+2, value centered at top+58
  const int ROW_H = 105;
  const int TOP_BAR = 30;
  const int R1 = TOP_BAR;            // 30
  const int R2 = TOP_BAR + ROW_H;    // 135
  const int R3 = TOP_BAR + ROW_H*2;  // 240
  const int R4 = TOP_BAR + ROW_H*3;  // 345
  const int BOT_BAR = TOP_BAR + ROW_H*4; // 450
  const int HALF = SCREEN_WIDTH / 2;

  // Draw layout ONCE
  if (!d3LayoutDrawn) {
    tft.fillScreen(COLOR_BG);
    d3LayoutDrawn = true;

    // Layout (480 tall):
    // [BLACK: REC | SAT | BAT]         30px
    // [  AWS       |  AWA     ]       105px
    // [  TWS       |  TWA     ]       105px
    // [  SOG       |  COG     ]       105px
    // [  TWD  full width      ]       105px
    // [BLACK: H P WiFi status ]        30px

    tft.fillRect(0, 0, SCREEN_WIDTH, TOP_BAR, TFT_BLACK);
    tft.fillRect(0, BOT_BAR, SCREEN_WIDTH, 30, TFT_BLACK);

    uint16_t lineColor = tft.color565(180, 180, 180);
    tft.drawFastHLine(0, R2, SCREEN_WIDTH, lineColor);
    tft.drawFastHLine(0, R3, SCREEN_WIDTH, lineColor);
    tft.drawFastHLine(0, R4, SCREEN_WIDTH, lineColor);
    tft.drawFastVLine(HALF, R1, ROW_H * 3, lineColor);  // vertical for first 3 rows

    uint16_t labelColor = tft.color565(100, 100, 100);
    tft.setTextColor(labelColor, COLOR_BG);
    tft.setTextDatum(TL_DATUM);
    if (config.wind_enabled) {
      tft.drawString("AWS", 5, R1 + 2, 2);
      tft.drawString("AWA", HALF + 5, R1 + 2, 2);
      tft.drawString("TWS", 5, R2 + 2, 2);
      tft.drawString("TWA", HALF + 5, R2 + 2, 2);
    }
    tft.drawString("SOG", 5, R3 + 2, 2);
    tft.drawString("COG", HALF + 5, R3 + 2, 2);
    if (config.wind_enabled) {
      tft.drawString("TWD", 5, R4 + 2, 2);
    }

    prevD3AWS = prevD3AWA = prevD3TWS = prevD3TWA = -999;
    prevD3TWD = prevD3SOG = prevD3COG = -999;
    prevD3RecState = prevD3Sats = prevD3FixQ = -1;
    prevD3HDOP = -1;
  }

  // Top status bar
  int dispSats = (gps.satellites >= 0 && gps.satellites <= 50) ? gps.satellites : 0;
  if (dispSats != prevD3Sats || recState != prevD3RecState ||
      gps.fix_quality != prevD3FixQ || abs(gps.hdop - prevD3HDOP) > 0.1) {
    prevD3Sats = dispSats;
    prevD3RecState = recState;
    prevD3FixQ = gps.fix_quality;
    prevD3HDOP = gps.hdop;

    tft.fillRect(0, 0, SCREEN_WIDTH, TOP_BAR, TFT_BLACK);
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    tft.setTextDatum(TL_DATUM);

    const char* recStr;
    switch (recState) {
      case REC_IDLE: recStr = gps.valid ? "READY" : "NO GPS"; break;
      case REC_ARMED: recStr = "ARMING"; break;
      case REC_RECORDING: recStr = "REC"; break;
      case REC_STOPPING: recStr = "STOP"; break;
      default: recStr = ""; break;
    }
    tft.drawString(recStr, 5, 7, 2);

    // Fix type (0=none, 1=GPS, 2=DGPS/SBAS, 4=RTK fix, 5=RTK float)
    const char* fixStr;
    switch (gps.fix_quality) {
      case 1: fixStr = "GPS"; break;
      case 2: fixStr = "SBAS"; break;
      case 4: fixStr = "RTK"; break;
      case 5: fixStr = "FLOAT"; break;
      default: fixStr = "---"; break;
    }
    tft.drawString(fixStr, 80, 7, 2);

    snprintf(buf, sizeof(buf), "SAT %d", dispSats);
    tft.drawString(buf, 140, 7, 2);

    snprintf(buf, sizeof(buf), "HDOP %.1f", gps.hdop);
    tft.setTextDatum(TR_DATUM);
    tft.drawString(buf, SCREEN_WIDTH - 5, 7, 2);
    tft.setTextDatum(TL_DATUM);
  }

  // Helper: value y-center for each row = row_top + 58 (label 16px + gap + 75px/2)
  // Clear area: row_top + 20 to row_top + 100 (80px tall, fits 75px font)

  // Wind values only if wind sensor enabled
  if (config.wind_enabled) {
    // AWS (row 1 left)
    if (abs(aws - prevD3AWS) > 0.2) {
      prevD3AWS = aws;
      tft.fillRect(0, R1 + 20, HALF - 2, 80, COLOR_BG);
      tft.setTextColor(TFT_BLACK, COLOR_BG);
      tft.setTextDatum(MC_DATUM);
      snprintf(buf, sizeof(buf), "%.1f", aws);
      tft.drawString(buf, HALF / 2, R1 + 58, 8);
    }

    // AWA (row 1 right)
    if (abs(awa - prevD3AWA) > 1) {
      prevD3AWA = awa;
      tft.fillRect(HALF + 2, R1 + 20, HALF - 2, 80, COLOR_BG);
      tft.setTextColor(TFT_BLACK, COLOR_BG);
      tft.setTextDatum(MC_DATUM);
      snprintf(buf, sizeof(buf), "%03d", (int)awa);
      tft.drawString(buf, HALF + HALF / 2, R1 + 58, 8);
    }

    // TWS (row 2 left)
    if (abs(tws - prevD3TWS) > 0.2) {
      prevD3TWS = tws;
      tft.fillRect(0, R2 + 20, HALF - 2, 80, COLOR_BG);
      tft.setTextColor(TFT_BLACK, COLOR_BG);
      tft.setTextDatum(MC_DATUM);
      snprintf(buf, sizeof(buf), "%.1f", tws);
      tft.drawString(buf, HALF / 2, R2 + 58, 8);
    }

    // TWA (row 2 right)
    if (abs(twa - prevD3TWA) > 1) {
      prevD3TWA = twa;
      tft.fillRect(HALF + 2, R2 + 20, HALF - 2, 80, COLOR_BG);
      tft.setTextColor(TFT_BLACK, COLOR_BG);
      tft.setTextDatum(MC_DATUM);
      snprintf(buf, sizeof(buf), "%03d", (int)twa);
      tft.drawString(buf, HALF + HALF / 2, R2 + 58, 8);
    }
  }

  // SOG (row 3 left)
  if (abs(gps.speed_kts - prevD3SOG) > 0.2) {
    prevD3SOG = gps.speed_kts;
    tft.fillRect(0, R3 + 20, HALF - 2, 80, COLOR_BG);
    tft.setTextColor(TFT_BLACK, COLOR_BG);
    tft.setTextDatum(MC_DATUM);
    snprintf(buf, sizeof(buf), "%.1f", gps.speed_kts);
    tft.drawString(buf, HALF / 2, R3 + 58, 8);
  }

  // COG (row 3 right)
  if (abs(gps.course - prevD3COG) > 0.5) {
    prevD3COG = gps.course;
    tft.fillRect(HALF + 2, R3 + 20, HALF - 2, 80, COLOR_BG);
    tft.setTextColor(TFT_BLACK, COLOR_BG);
    tft.setTextDatum(MC_DATUM);
    snprintf(buf, sizeof(buf), "%03d", (int)gps.course);
    tft.drawString(buf, HALF + HALF / 2, R3 + 58, 8);
  }

  // TWD (row 4 full width) - only if wind enabled
  if (config.wind_enabled) {
    if (abs(twd - prevD3TWD) > 1) {
      prevD3TWD = twd;
      tft.fillRect(0, R4 + 20, SCREEN_WIDTH, 80, COLOR_BG);
      tft.setTextColor(TFT_BLACK, COLOR_BG);
      tft.setTextDatum(MC_DATUM);
      snprintf(buf, sizeof(buf), "%03d", (int)twd);
      tft.drawString(buf, SCREEN_WIDTH / 2, R4 + 58, 8);
    }
  }

  // Bottom bar: Heel + WiFi/upload status
  static unsigned long lastD3Status = 0;
  if (millis() - lastD3Status > 2000) {
    lastD3Status = millis();
    tft.fillRect(0, BOT_BAR, SCREEN_WIDTH, 30, TFT_BLACK);
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    tft.setTextDatum(TL_DATUM);

    char line[32];
    snprintf(line, sizeof(line), "H%+.0f P%+.0f BAT%d%%", imu.heel, imu.pitch, battery.percent);
    tft.drawString(line, 3, BOT_BAR + 7, 2);

    // Right side: upload status + WiFi (shows IP next to SSID when idle).
    char right[40];
    const char* wifiInd = wifiConnected ? getWifiIndicator() : "";
    if (uploading && uploadTotal > 0) {
      snprintf(right, sizeof(right), "%d/%d %s", uploadCount, uploadTotal, wifiInd);
    } else if (pendingUploads > 0) {
      snprintf(right, sizeof(right), "N%d %s", pendingUploads, wifiInd);
    } else if (wifiConnected) {
      snprintf(right, sizeof(right), "%s %s", wifiInd, WiFi.localIP().toString().c_str());
    } else {
      snprintf(right, sizeof(right), "%s", wifiInd);
    }
    tft.setTextDatum(TR_DATUM);
    tft.drawString(right, SCREEN_WIDTH - 3, BOT_BAR + 7, 2);
    tft.setTextDatum(TL_DATUM);
  }
}

// Main display router
// TFT (VSPI) and SD (HSPI) are on separate buses - no conflicts.
// But TFT itself is shared between Core 0 (OTA progress paint) and
// Core 1 (this normal display loop). TFT_eSPI is not thread-safe —
// concurrent calls deadlock the VSPI peripheral. While OTA is in
// flight, Core 0 owns the TFT exclusively; Core 1 stands down here.
void updateDisplay() {
  if (otaInProgress) return;
  if (displayMode == 1) {
    updateDisplayD1();
  } else if (displayMode == 2) {
    updateDisplayD2();
  } else {
    updateDisplayD3();
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

// Threshold for using presigned URL - larger files bypass API Gateway timeout
// ESP32 uploads at ~20-50KB/s, API Gateway times out at 29s, so keep threshold low
#define PRESIGN_THRESHOLD 200000  // 200KB

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
  // Check WiFi before attempting request
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[UPLOAD] WiFi not connected, skipping presign request");
    return "";
  }

  Serial.printf("[UPLOAD] Requesting presigned URL (heap: %u)...\n", ESP.getFreeHeap());

  WiFiClientSecure client;
  client.setInsecure();
  client.setHandshakeTimeout(30);
  client.setTimeout(60);

  HTTPClient http;
  http.setTimeout(60000);  // 60 second timeout
  http.setReuse(false);

  // Build presign request URL
  String url = String(config.upload_url);
  url += "?boat=";
  url += config.boat_id;
  url += "&file=";
  url += filepath;
  url += "&presign=1&size=";
  url += String(fileSize);

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

// Extract date from filepath for S3 key
// Filepath format: /sf/20260405_225030/E1_nav.csv -> 2026-04-05
String extractDateFromPath(const char* filepath) {
  String path = String(filepath);

  // Find the session folder (e.g., "20260405_225030")
  int sfIdx = path.indexOf("/sf/");
  if (sfIdx >= 0) {
    int dateStart = sfIdx + 4;  // Skip "/sf/"
    if (path.length() > dateStart + 8) {
      String dateStr = path.substring(dateStart, dateStart + 8);
      // Convert YYYYMMDD to YYYY-MM-DD
      if (dateStr.length() == 8) {
        return dateStr.substring(0, 4) + "-" + dateStr.substring(4, 6) + "-" + dateStr.substring(6, 8);
      }
    }
  }

  // Fallback: use GPS date if available (format: DDMMYY)
  if (strlen(gps.date) >= 6 && (gps.date[4] != '0' || gps.date[5] != '0')) {
    // gps.date is DDMMYY, convert to YYYY-MM-DD
    char dateBuf[12];
    snprintf(dateBuf, sizeof(dateBuf), "20%c%c-%c%c-%c%c",
             gps.date[4], gps.date[5],  // YY
             gps.date[2], gps.date[3],  // MM
             gps.date[0], gps.date[1]); // DD
    return String(dateBuf);
  }

  // Last resort: use a placeholder
  return "unknown-date";
}

// Upload a single file directly to S3 via HTTP (no TLS)
// Bypasses API Gateway entirely - bucket policy allows unauthenticated PUT to raw/E1/*
bool uploadFile(const char* filepath) {
  uploadCount++;

  // Extract short filename for display (main loop will update display)
  const char* lastSlash = strrchr(filepath, '/');
  const char* shortName = lastSlash ? lastSlash + 1 : filepath;
  strncpy(uploadCurrentFile, shortName, sizeof(uploadCurrentFile) - 1);
  uploadCurrentFile[sizeof(uploadCurrentFile) - 1] = '\0';

  // Don't call updateDisplay() here - it runs on Core 1, we're on Core 0
  // The main loop will pick up uploadCount/uploadCurrentFile changes

  // Feed watchdog before file operations
  yield();
  delay(10);

  // Verify WiFi is still connected
  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("[UPLOAD] WiFi disconnected, skipping: %s\n", filepath);
    uploadFailed++;
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

  Serial.printf("[UPLOAD] %s (%u bytes) heap:%u rssi:%d\n",
    filepath, fileSize, ESP.getFreeHeap(), WiFi.RSSI());

  // Feed watchdog
  yield();
  delay(10);

  // Extract filename from path
  String pathStr = String(filepath);
  int slashIdx = pathStr.lastIndexOf('/');
  String filename = (slashIdx >= 0) ? pathStr.substring(slashIdx + 1) : pathStr;

  // Extract date for S3 path organization
  String dateFolder = extractDateFromPath(filepath);

  // Build S3 URL: http://{bucket}.s3.{region}.amazonaws.com/raw/{boat_id}/{date}/{filename}
  String s3Url = "http://";
  s3Url += config.s3_bucket;
  s3Url += ".s3.";
  s3Url += config.s3_region;
  s3Url += ".amazonaws.com/raw/";
  s3Url += config.boat_id;
  s3Url += "/";
  s3Url += dateFolder;
  s3Url += "/";
  s3Url += filename;

  Serial.printf("[UPLOAD] S3 HTTP PUT: %s\n", s3Url.c_str());

  // Use plain HTTP client (no TLS - bucket policy allows unauthenticated PUT)
  WiFiClient client;
  // WiFiClient.setTimeout takes SECONDS in ESP32 Arduino Core
  client.setTimeout(300);  // 5 minute timeout for large files

  HTTPClient http;
  // HTTPClient.setTimeout takes MILLISECONDS
  http.setTimeout(300000);  // 5 minute timeout
  http.setReuse(false);
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);  // Follow S3 redirects

  yield();

  if (!http.begin(client, s3Url)) {
    Serial.printf("[UPLOAD] Failed to begin HTTP: %s\n", filepath);
    file.close();
    return false;
  }

  // Determine content type
  String contentType = "application/octet-stream";
  if (filename.endsWith(".csv")) {
    contentType = "text/csv";
  } else if (filename.endsWith(".rtcm3")) {
    contentType = "application/octet-stream";
  }

  http.addHeader("Content-Type", contentType);
  http.addHeader("Content-Length", String(fileSize));

  // Add custom metadata headers (x-amz-meta-* prefix for S3)
  http.addHeader("x-amz-meta-boat-id", config.boat_id);
  http.addHeader("x-amz-meta-original-path", filepath);

  yield();

  unsigned long startTime = millis();
  int httpCode = http.sendRequest("PUT", &file, fileSize);
  unsigned long elapsed = (millis() - startTime) / 1000;

  String response = http.getString();
  file.close();
  http.end();

  yield();
  delay(50);

  if (httpCode == 200 || httpCode == 201 || httpCode == 204) {
    Serial.printf("[UPLOAD] Success: %s (HTTP %d, %lus)\n", filepath, httpCode, elapsed);
    uploadSuccess++;
    // Don't call updateDisplay() - runs on Core 1, main loop handles it
    return true;
  } else {
    const char* errMsg = "";
    if (httpCode == -1) errMsg = "CONNECTION_REFUSED/TIMEOUT";
    else if (httpCode == -2) errMsg = "SEND_HEADER_FAILED";
    else if (httpCode == -3) errMsg = "SEND_PAYLOAD_FAILED";
    else if (httpCode == -4) errMsg = "NOT_CONNECTED";
    else if (httpCode == -5) errMsg = "CONNECTION_LOST";
    else if (httpCode == -11) errMsg = "READ_TIMEOUT";
    else if (httpCode == 403) errMsg = "FORBIDDEN (check bucket policy)";
    else if (httpCode == 400) errMsg = "BAD_REQUEST";
    Serial.printf("[UPLOAD] Failed: %s (HTTP %d %s)\n", filepath, httpCode, errMsg);
    if (response.length() > 0 && response.length() < 500) {
      Serial.printf("[UPLOAD] Response: %s\n", response.c_str());
    }
    uploadFailed++;
    // Don't call updateDisplay() - runs on Core 1, main loop handles it
    return false;
  }
}

// Returns true if this filename should NOT be uploaded on the current
// connected SSID. RTCM3 PPK files are large and only uploaded on the
// home network — on hotspots they're deferred until back at base.
static bool isSkippedForCurrentNetwork(const String& filename) {
  if (!filename.endsWith(".rtcm3")) return false;
  return strcmp(connectedSSID, HOME_WIFI_SSID) != 0;
}

// Count files we will actually try to upload on the current SSID.
// Files that would be skipped (e.g. RTCM3 on hotspot) are NOT counted —
// otherwise the post-upload "remaining" check sees them, never reaches 0,
// and we never request WiFi teardown.
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
      if (!name.endsWith(".uploaded") &&
          !isUploaded(filepath) &&
          !isSkippedForCurrentNetwork(name)) {
        count++;
      }
    }
    file = root.openNextFile();
    yield();  // Feed watchdog
  }
  return count;
}

// Test S3 connectivity via HTTP (no TLS needed)
bool testS3Connection() {
  size_t freeHeap = ESP.getFreeHeap();
  Serial.printf("[UPLOAD] Testing S3 connectivity (heap: %u, RSSI: %d)...\n",
                freeHeap, WiFi.RSSI());

  // Build S3 hostname
  String s3Host = String(config.s3_bucket) + ".s3." + String(config.s3_region) + ".amazonaws.com";

  // Check RSSI to verify WiFi is actually connected (should be negative)
  int rssi = WiFi.RSSI();
  if (rssi >= 0) {
    Serial.printf("[UPLOAD] WiFi not ready (RSSI: %d, expected negative)\n", rssi);
    return false;
  }

  // Test DNS
  IPAddress ip;
  if (!WiFi.hostByName(s3Host.c_str(), ip)) {
    Serial.printf("[UPLOAD] DNS FAILED for %s\n", s3Host.c_str());
    return false;
  }

  // Validate DNS returned a real IP (ESP32 can return 0.0.0.0 when network not ready)
  if (ip == IPAddress(0, 0, 0, 0)) {
    Serial.println("[UPLOAD] DNS returned 0.0.0.0 - network not ready");
    return false;
  }
  Serial.printf("[UPLOAD] DNS OK: %s -> %s\n", s3Host.c_str(), ip.toString().c_str());
  yield();

  // Test TCP connection to port 80 (HTTP)
  WiFiClient testClient;
  testClient.setTimeout(10);
  if (!testClient.connect(ip, 80)) {
    Serial.println("[UPLOAD] TCP port 80 FAILED");
    return false;
  }
  testClient.stop();
  Serial.println("[UPLOAD] TCP OK (HTTP ready)");

  yield();
  delay(50);

  return true;
}

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
        // Defer RTCM3 PPK files until back on home WiFi — too large for
        // mobile hotspots and not needed for in-event analytics.
        if (isSkippedForCurrentNetwork(name)) {
          Serial.printf("[UPLOAD] Skipping RTCM3 on %s (%s only): %s\n",
                        connectedSSID, HOME_WIFI_SSID, name.c_str());
          // Don't mark as uploaded — will upload when on home WiFi.
        } else {
          // Check if boat started moving - abort upload to allow recording to start
          if (gps.speed_kts >= config.start_speed_knots || recState == REC_ARMED) {
            Serial.println("[UPLOAD] Boat moving, aborting upload to allow recording");
            root.close();
            return;  // Exit uploadDirectory immediately
          }

          // Feed watchdog before upload
          esp_task_wdt_reset();
          yield();
          delay(100);

          if (uploadFile(filepath)) {
            markUploaded(filepath);
          }
          esp_task_wdt_reset();  // and after — single PUT can be 8s+
        }

        // Longer pause between uploads to prevent crashes
        // Use vTaskDelay to properly yield to other tasks on this core
        vTaskDelay(pdMS_TO_TICKS(200));

        // Do NOT call ArduinoOTA.handle()/handleTelnet() here — those run on
        // Core 1 in the main loop. WiFi stack and telnet globals are not
        // thread-safe; calling from Core 0 corrupts heap and crashes after
        // upload finishes (see firmware 2026.05.01.4 fleet crashes).
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

#if ENABLE_WIND
  // Pause BLE before any WiFi operation — shared radio. The first WiFi
  // call below is WiFi.disconnect(true) which reconfigures the radio;
  // doing that with a NimBLE scan in flight corrupts NimBLE state and
  // hangs Core 1 the next time it touches BLE.
  pauseBLEForWiFi();
#endif

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

  // BSSID-aware AP selection. The ESP32 Arduino stack's default
  // WiFi.begin(ssid, pass) picks "first BSSID that auth-completes",
  // not "strongest BSSID for that SSID". On a multi-AP mesh that
  // means a boat can latch onto a far AP at -90 dBm even when an
  // identical SSID is broadcasting at -50 dBm next to it. Observed
  // 2026-05-21 with E5: stuck on Family room AP at -90 dBm with
  // ~2.5 KB/s OTA throughput while Office AP (same room as boat)
  // was available at -45 dBm.
  //
  // Walk the scan once, find the strongest BSSID for each configured
  // SSID, copy the BSSID + channel into local storage (scan results
  // get freed below), then pass them to WiFi.begin to pin association.
  struct BestAp {
    bool   seen;
    int    rssi;
    int32_t channel;
    uint8_t bssid[6];
  };
  BestAp best[MAX_WIFI_NETWORKS];
  for (int s = 0; s < MAX_WIFI_NETWORKS; s++) {
    best[s].seen = false;
    best[s].rssi = -200;
    best[s].channel = 0;
  }
  for (int i = 0; i < n; i++) {
    String ssid = WiFi.SSID(i);
    int rssi = WiFi.RSSI(i);
    for (int s = 0; s < config.wifi_count; s++) {
      if (strlen(config.wifi[s].ssid) == 0) continue;
      if (ssid == config.wifi[s].ssid && rssi > best[s].rssi) {
        best[s].seen = true;
        best[s].rssi = rssi;
        best[s].channel = WiFi.channel(i);
        memcpy(best[s].bssid, WiFi.BSSID(i), 6);
      }
    }
  }
  for (int s = 0; s < config.wifi_count; s++) {
    if (strlen(config.wifi[s].ssid) == 0) continue;
    if (best[s].seen) {
      Serial.printf("[WIFI] Best AP for %s: %02X:%02X:%02X:%02X:%02X:%02X ch%d %d dBm\n",
        config.wifi[s].ssid,
        best[s].bssid[0], best[s].bssid[1], best[s].bssid[2],
        best[s].bssid[3], best[s].bssid[4], best[s].bssid[5],
        (int)best[s].channel, best[s].rssi);
    } else {
      Serial.printf("[WIFI] %s not visible in scan — will skip\n",
        config.wifi[s].ssid);
    }
  }
  WiFi.scanDelete();

  // Try each configured network, in config order, but skip ones not
  // visible in the scan (saves the 20 s per-network timeout when the
  // iPhone hotspot isn't around).
  for (int i = 0; i < config.wifi_count; i++) {
    if (strlen(config.wifi[i].ssid) == 0) continue;
    if (!best[i].seen) continue;

    Serial.printf("[WIFI] Trying %s (%d/%d) — pinned to strongest BSSID at %d dBm...\n",
      config.wifi[i].ssid, i + 1, config.wifi_count, best[i].rssi);

    // No display update - WiFi connects silently in background

    // Ensure clean state before connecting
    WiFi.disconnect(true);
    delay(100);
    WiFi.mode(WIFI_STA);
    // Max TX power. The .04 era reduced this to 15 dBm to save battery,
    // but slow uploads at marginal signal are now the dominant operational
    // problem (suspected cause of the 2026-05-03 simultaneous reboot:
    // slow PUTs > previous 120s wdt budget). +4.5 dB of link margin
    // halves typical upload time when at the edge of AP range. WiFi only
    // runs during the post-sail upload window, so the average-current
    // cost across a day is ~1-2% of LiPo capacity. Watch /boot.log for
    // BROWNOUT entries — if low-SoC devices start tripping that, dial
    // back to 17 dBm or add an SoC-conditional setting.
    WiFi.setTxPower(WIFI_POWER_19_5dBm);
    WiFi.persistent(false);  // Don't save to flash
    WiFi.setAutoReconnect(false);
    // Pin to the strongest-BSSID + channel from the scan above. This
    // bypasses the ESP32 stack's "first-respond-wins" AP picker.
    WiFi.begin(config.wifi[i].ssid, config.wifi[i].pass,
               best[i].channel, best[i].bssid);

    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts++ < 40) {  // 40 attempts = 20 sec
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

      // Allow network stack to fully stabilize (DNS, routes, etc.)
      Serial.println("[WIFI] Waiting for network stack to stabilize...");
      delay(1000);
      yield();

      wifiConnected = true;

      // Start OTA. Telnet listener stays off by default — its WiFiServer/
      // WiFiClient calls into LWIP deadlock Core 1 when Core 0 is doing
      // concurrent HTTP uploads (firmware 2026.05.03.04 fleet hang).
      // Enable at runtime with serial command 'telneton'.
      setupOTA();
      if (telnetEnabled) {
        startTelnetServer();
      } else {
        Serial.println("[TELNET] Listener disabled (use 'telneton' to enable)");
      }

      // No display update - connection is silent, status shown in status bar

      // Trigger upload check on WiFi connect (reset timer so task checks immediately)
      lastUploadCheck = 0;
      uploadRetryCount = 0;  // Reset retries on new connection
      Serial.println("[WIFI] Upload check triggered on connect");

      return true;
    }

    WiFi.disconnect(true);
    delay(100);
    yield();  // Feed watchdog
  }

  Serial.println("[WIFI] All networks failed");
  return false;
}

// checkWiFiUpload() REMOVED — was racing with uploadTaskFunc() on Core 0.
// All upload logic now lives in uploadTaskFunc() (single owner, uses sdMutex).

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

// ============================================================
// OTA FIRMWARE UPDATE (manual, manifest-pull, plain HTTP)
// ============================================================
// CI publishes:
//   http://{bucket}.s3.{region}.amazonaws.com/firmware/{boat_id}/latest.json
//   { "version": "...", "url": "...", "size": N, "sha256": "..." }
// and the .bin at "url". TLS is broken in Core 3.3.7 so we go HTTP.
// Integrity comes from the SHA256 in the manifest; bucket-write IAM is
// split so the fleet's anonymous PUT credentials cannot replace either.

static String otaExtractJsonString(const String& json, const char* key) {
  String pattern = String("\"") + key + "\"";
  int k = json.indexOf(pattern);
  if (k < 0) return "";
  int colon = json.indexOf(':', k);
  if (colon < 0) return "";
  int q1 = json.indexOf('"', colon);
  if (q1 < 0) return "";
  int q2 = json.indexOf('"', q1 + 1);
  if (q2 < 0) return "";
  return json.substring(q1 + 1, q2);
}

static long otaExtractJsonNumber(const String& json, const char* key) {
  String pattern = String("\"") + key + "\"";
  int k = json.indexOf(pattern);
  if (k < 0) return -1;
  int colon = json.indexOf(':', k);
  if (colon < 0) return -1;
  int p = colon + 1;
  while (p < (int)json.length() && (json[p] == ' ' || json[p] == '\t')) p++;
  int q = p;
  while (q < (int)json.length() && isdigit((unsigned char)json[q])) q++;
  if (q == p) return -1;
  return json.substring(p, q).toInt();
}

static String otaHexDigest(const uint8_t* digest, size_t len) {
  static const char hex[] = "0123456789abcdef";
  String out;
  out.reserve(len * 2);
  for (size_t i = 0; i < len; i++) {
    out += hex[(digest[i] >> 4) & 0xF];
    out += hex[digest[i] & 0xF];
  }
  return out;
}

// OTA progress display — TFT was previously frozen on the last D2/D3
// frame during a 30-60 s firmware download, with no user-visible signal
// that anything was happening. Helper draws a one-time layout, then
// updates only the % number + progress bar on subsequent calls to keep
// SPI churn minimal during download. Pair the cadence to the existing
// 2-second serial log block so we never paint per-iteration.
static bool g_otaScreenDrawn = false;
static int  g_otaLastPctDrawn = -1;
static void drawOTAProgress(int percent, const char* targetVersion, const char* phase) {
  if (!oledOK) return;
  if (percent < 0) percent = 0;
  if (percent > 100) percent = 100;

  if (!g_otaScreenDrawn) {
    tft.fillScreen(COLOR_WARN);
    tft.setTextColor(TFT_BLACK, COLOR_WARN);
    tft.setTextDatum(MC_DATUM);
    // Header — font 4 is the largest text font (~26 px tall, full ASCII).
    tft.drawString("OTA UPDATE", SCREEN_WIDTH/2, 40, 4);
    // Target version — bumped from font 2 (~16 px) to font 4.
    if (targetVersion && targetVersion[0]) {
      tft.drawString(targetVersion, SCREEN_WIDTH/2, 90, 4);
    }
    // Progress bar frame — taller (40 px instead of 30).
    tft.drawRect(20, SCREEN_HEIGHT/2 + 60, SCREEN_WIDTH - 40, 40, TFT_BLACK);
    // Footer — also bumped to font 4 so it's legible from across the cockpit.
    tft.drawString("DO NOT POWER OFF", SCREEN_WIDTH/2, SCREEN_HEIGHT - 40, 4);
    g_otaScreenDrawn = true;
    g_otaLastPctDrawn = -1;
  }

  // Phase tag (e.g. "downloading", "verifying", "rebooting") shown above %.
  // Bumped to font 4 — the previous font 2 was unreadable across the cabin.
  if (phase && phase[0]) {
    tft.fillRect(0, 125, SCREEN_WIDTH, 34, COLOR_WARN);
    tft.setTextColor(TFT_BLACK, COLOR_WARN);
    tft.setTextDatum(MC_DATUM);
    tft.drawString(phase, SCREEN_WIDTH/2, 142, 4);
  }

  if (percent != g_otaLastPctDrawn) {
    char buf[8];
    snprintf(buf, sizeof(buf), "%d%%", percent);
    tft.fillRect(0, 175, SCREEN_WIDTH, 95, COLOR_WARN);
    tft.setTextColor(TFT_BLACK, COLOR_WARN);
    tft.setTextDatum(MC_DATUM);
    tft.drawString(buf, SCREEN_WIDTH/2, 220, 8);  // Font 8 = 75 px (digits)
    // Progress bar fill — matches the taller frame above.
    int barX = 21;
    int barY = SCREEN_HEIGHT/2 + 61;
    int barW = SCREEN_WIDTH - 42;
    int barH = 38;
    int fillW = (barW * percent) / 100;
    tft.fillRect(barX, barY, fillW, barH, TFT_BLACK);
    tft.fillRect(barX + fillW, barY, barW - fillW, barH, COLOR_WARN);
    g_otaLastPctDrawn = percent;
  }
}

// `manual` = true bypasses the one-shot per-boot guard. The serial
// `update` command sets it; auto-triggers from the upload task call
// with the default false, so they no-op after the first run.
bool performOTAUpdate(bool manual) {
  if (!manual && g_otaCheckedThisBoot) {
    Serial.println("[OTA] Already checked this boot — skipping. Use 'update' over serial to force a re-check.");
    return true;  // not an error; intended one-shot behaviour
  }
  // Mark BEFORE doing the work so a partial / failed run also counts
  // as "checked this boot". Prevents an upload-task retry loop from
  // hammering the manifest endpoint after every cycle.
  g_otaCheckedThisBoot = true;

  if (logging) {
    Serial.println("[OTA] Refusing: recording active. Stop recording first.");
    return false;
  }
  if (uploading || triggerUpload) {
    Serial.println("[OTA] Refusing: upload in flight.");
    return false;
  }

  if (!wifiConnected) {
    Serial.println("[OTA] WiFi not connected, attempting to connect...");
    if (!connectWiFi()) {
      Serial.println("[OTA] WiFi connect failed");
      return false;
    }
  }

  // OTA only on the home network. Hotspots have data caps and the
  // 1.5 MB pull would chew through a phone plan; also avoids surprise
  // updates when a boat associates with an unfamiliar AP.
  if (strcmp(connectedSSID, HOME_WIFI_SSID) != 0) {
    Serial.printf("[OTA] Refusing: only OTA on %s, currently on %s\n",
                  HOME_WIFI_SSID, connectedSSID);
    return false;
  }

  // Claim the radio for OTA. Same gates the upload task uses:
  //  - pauseBLEForWiFi() stops in-flight NimBLE scans / wind client.
  //  - uploading=true makes checkWindConnection() early-return.
  //  - wifiBusy=true blocks Core 1 LWIP-touching paths (telnet etc.).
  // Without this, BLE coexistence steals airtime mid-download and
  // throughput collapses to ~30 B/s.
  pauseBLEForWiFi();
  bool prevUploading = uploading;
  bool prevWifiBusy  = wifiBusy;
  uploading = true;
  wifiBusy  = true;

  // Park Core 1's display loop while we paint the OTA progress screen
  // from Core 0. TFT_eSPI is not thread-safe — concurrent calls from
  // both cores through the shared VSPI peripheral deadlock the bus
  // (observed on .15: 2 of 6 boats hung at "rebooting..." with
  // sect=display frozen, iter not advancing, while the diag task on
  // Core 0 kept printing). updateDisplay() early-returns when
  // otaInProgress is true, so only Core 0 touches the TFT during OTA.
  otaInProgress = true;

  // Arm the hang watchdog: diagnosticsTask will forcibly esp_restart()
  // if we don't return within OTA_MAX_MS. Clears at every exit point
  // below so a successful or cleanly-failing OTA never trips it.
  g_otaDeadlineMs = millis() + OTA_MAX_MS;
  appendBootLog("ota start");

  bool ok = performOTAUpdateBody();

  // On success the body calls ESP.restart() and never returns here;
  // on any failure path we land here and must release the radio AND
  // disarm the watchdog (otherwise diag would restart us shortly).
  g_otaDeadlineMs = 0;
  uploading = prevUploading;
  wifiBusy  = prevWifiBusy;
  otaInProgress = false;
  appendBootLog(ok ? "ota end ok" : "ota end fail");
  // After any OTA exit (other than the success path that restarts) we
  // may have painted the yellow OTA UPDATE screen. Force the next D2/D3
  // update to redraw fully so the user doesn't see the yellow framebuffer
  // bleed through with new numbers overlaid.
  d2LayoutDrawn = false;
  d3LayoutDrawn = false;
  g_otaScreenDrawn = false;
  return ok;
}

// ============================================================
// v2.0.0 Stage 3 — fleet health snapshot upload
// ============================================================
// Uploads a small JSON blob to S3 with current device state. Lets a
// cloud admin UI (TBD) get a fleet-wide health view without touching
// individual devices. Spec target: status/<boat_id>/latest.json.
// MVP target (this commit): raw/<boat_id>/_health.json so we stay
// under the existing FleetDirectHTTPUpload bucket policy (which
// covers raw/* only). Promote to status/<boat_id>/latest.json in a
// follow-up that also bumps the bucket policy.
//
// Called from the upload task after each successful WiFi acquisition.
// Cheap (sub-1 KB JSON, plain HTTP PUT). Failure is non-fatal — we
// just skip and try again next cycle.
static bool g_statusCheckedThisBoot = false;

bool uploadStatusSnapshot() {
  if (!wifiConnected) return false;

  // Build JSON in a stack-local buffer. Keep under 1 KB.
  char body[768];
  char ts[24] = "";
  formatGpsIso(ts, sizeof(ts));   // empty string if GPS time not yet valid

  const char* fixStr =
    gps.fix_quality == 2 ? "dgps" :
    gps.fix_quality == 1 ? "gps"  :
    "none";

  int written = snprintf(body, sizeof(body),
    "{"
    "\"version\":\"%s\","
    "\"boat_id\":\"%s\","
    "\"ts_iso\":\"%s\","
    "\"gps_fix\":\"%s\","
    "\"sats\":%d,"
    "\"last_position\":{\"lat\":%.7f,\"lon\":%.7f},"
    "\"battery_pct\":%d,"
    "\"battery_v\":%.2f,"
    "\"uptime_s\":%lu,"
    "\"free_heap\":%u,"
    "\"min_heap\":%u,"
    "\"espnow_peers\":%d,"
    "\"espnow_tx\":%lu,"
    "\"espnow_rx\":%lu,"
    "\"config_version\":%d,"
    "\"wifi_ssid\":\"%s\","
    "\"wifi_rssi\":%d,"
    "\"hardware_platform\":\"%s\","
    "\"unit_role\":\"%s\","
    "\"imu_ok\":%s,"
    "\"sd_ok\":%s"
    "}",
    FW_VERSION,
    config.boat_id,
    ts,
    fixStr,
    gps.satellites,
    gps.lat, gps.lon,
    battery.percent,
    battery.voltage,
    millis() / 1000UL,
    (unsigned)ESP.getFreeHeap(),
    (unsigned)esp_get_minimum_free_heap_size(),
    g_mesh_peer_count,
    (unsigned long)g_mesh_tx_count,
    (unsigned long)g_mesh_rx_count,
    config.config_version,
    connectedSSID,
    (int)WiFi.RSSI(),
    hwName(g_hw),
    roleName(g_role),
    imuOK ? "true" : "false",
    sdOK ? "true" : "false");
  if (written < 0 || written >= (int)sizeof(body)) {
    Serial.println("[STATUS] JSON truncated, skip");
    return false;
  }

  String host = String(config.s3_bucket) + ".s3." + String(config.s3_region) + ".amazonaws.com";
  String url = "http://" + host + "/raw/" + String(config.boat_id) + "/_health.json";

  WiFiClient client;
  HTTPClient http;
  http.setConnectTimeout(8000);
  http.setTimeout(10000);
  http.setReuse(false);
  if (!http.begin(client, url)) {
    Serial.println("[STATUS] http.begin failed");
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  int code = http.PUT((uint8_t*)body, written);
  http.end();

  if (code == 200) {
    Serial.printf("[STATUS] uploaded (%d bytes) -> %s\n", written, url.c_str());
    return true;
  }
  Serial.printf("[STATUS] upload failed: HTTP %d\n", code);
  return false;
}

// ============================================================
// v2.0.0 Stage 3.5 — cloud config sync (observe-only MVP)
// ============================================================
// Fetches config/<boat_id>/latest.json from S3, compares version
// against locally-stored config.config_version. Reports cloud
// version + any newer-than-local indication; does NOT modify
// /sf/config.txt or apply changes yet. That comes in Stage 3.6
// with atomic write + structural/feature-flag dispatch.
//
// This MVP exists so the plumbing is in place and validated on
// the live fleet before we start mutating SD-side config.
//
// Endpoint pattern matches OTA: anonymous HTTP GET.
static bool g_configSyncCheckedThisBoot = false;
static int  g_cloud_config_version = -1;   // -1 = unknown / not fetched

bool performConfigSync() {
  if (!wifiConnected) return false;

  String host = String(config.s3_bucket) + ".s3." + String(config.s3_region) + ".amazonaws.com";
  String url = "http://" + host + "/config/" + String(config.boat_id) + "/latest.json";
  Serial.printf("[CFGSYNC] Fetching %s\n", url.c_str());

  WiFiClient client;
  HTTPClient http;
  http.setConnectTimeout(8000);
  http.setTimeout(10000);
  http.setReuse(false);
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  if (!http.begin(client, url)) {
    Serial.println("[CFGSYNC] http.begin failed");
    return false;
  }
  int code = http.GET();
  if (code == 404) {
    Serial.println("[CFGSYNC] No cloud config (404) — nothing to do");
    http.end();
    return true;  // not an error; expected default state
  }
  if (code != 200) {
    Serial.printf("[CFGSYNC] HTTP %d\n", code);
    http.end();
    return false;
  }
  String body = http.getString();
  http.end();

  // Reuse the OTA JSON extractors. Cloud version is the only field
  // we look at for MVP — everything else is logged for visibility.
  String versionStr = otaExtractJsonString(body, "version");
  long cloudVersion = otaExtractJsonNumber(body, "version");
  if (cloudVersion < 0 && versionStr.length() > 0) cloudVersion = versionStr.toInt();
  if (cloudVersion < 0) {
    Serial.println("[CFGSYNC] cloud JSON missing 'version' — skipping");
    return false;
  }

  g_cloud_config_version = (int)cloudVersion;
  int localVersion = config.config_version;

  Serial.printf("[CFGSYNC] cloud v%d, local v%d\n", g_cloud_config_version, localVersion);
  if (g_cloud_config_version > localVersion) {
    Serial.printf("[CFGSYNC] cloud config NEWER (v%d > v%d) — pending apply\n",
                  g_cloud_config_version, localVersion);
    Serial.printf("[CFGSYNC] body: %s\n", body.c_str());
    char line[64];
    snprintf(line, sizeof(line), "cfgsync cloud=v%d local=v%d pending",
             g_cloud_config_version, localVersion);
    appendBootLog(line);
    // Stage 3.6: parse + validate + atomic write /sf/config.txt here.
    // For .18 MVP we just observe — no SD modification, no reboot.
  } else if (g_cloud_config_version == localVersion) {
    Serial.printf("[CFGSYNC] up to date (v%d)\n", g_cloud_config_version);
  } else {
    // Cloud is older than local — odd state, treat as no-op.
    Serial.printf("[CFGSYNC] cloud older than local (v%d < v%d) — ignoring\n",
                  g_cloud_config_version, localVersion);
  }
  return true;
}

static bool performOTAUpdateBody() {
  Serial.printf("[OTA] WiFi RSSI: %d dBm, free heap: %u\n", WiFi.RSSI(), ESP.getFreeHeap());

  String host = String(config.s3_bucket) + ".s3." + String(config.s3_region) + ".amazonaws.com";
  String manifestUrl = "http://" + host + "/firmware/" + String(config.boat_id) + "/latest.json";

  Serial.printf("[OTA] Fetching manifest: %s\n", manifestUrl.c_str());

  WiFiClient mClient;
  HTTPClient mHttp;
  // Tighter than the previous 30 s — manifest is 200 bytes, so anything
  // beyond ~10 s means we're stuck in DNS/TCP, not waiting for data.
  // setConnectTimeout bounds the connect phase (HTTPClient honors it
  // separately from setTimeout which only covers receive).
  mHttp.setConnectTimeout(8000);
  mHttp.setTimeout(10000);
  mHttp.setReuse(false);
  mHttp.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);

  if (!mHttp.begin(mClient, manifestUrl)) {
    Serial.println("[OTA] http.begin (manifest) failed");
    return false;
  }
  int code = mHttp.GET();
  if (code != 200) {
    Serial.printf("[OTA] Manifest GET failed: HTTP %d\n", code);
    mHttp.end();
    return false;
  }
  String manifest = mHttp.getString();
  mHttp.end();

  Serial.printf("[OTA] Manifest: %s\n", manifest.c_str());

  String version = otaExtractJsonString(manifest, "version");
  String binUrl  = otaExtractJsonString(manifest, "url");
  String sha256  = otaExtractJsonString(manifest, "sha256");
  long   size    = otaExtractJsonNumber(manifest, "size");

  if (version.isEmpty() || binUrl.isEmpty() || sha256.isEmpty() || size <= 0) {
    Serial.println("[OTA] Manifest missing required fields");
    return false;
  }

  Serial.printf("[OTA] Latest:  %s (%ld bytes)\n", version.c_str(), size);
  Serial.printf("[OTA] Current: %s\n", FW_VERSION);

  if (version == FW_VERSION) {
    Serial.println("[OTA] Already up to date.");
    return true;
  }

  if (binUrl.startsWith("https://")) {
    binUrl = "http://" + binUrl.substring(8);
  }
  Serial.printf("[OTA] Downloading: %s\n", binUrl.c_str());

  WiFiClient bClient;
  HTTPClient bHttp;
  // 300 s was much too generous and let HTTPClient's recv block for
  // five minutes on a stalled stream. The stall watchdog inside the
  // download loop now bounds true stalls at OTA_STALL_MS; this only
  // sets the per-call recv ceiling.
  bHttp.setConnectTimeout(8000);
  bHttp.setTimeout(20000);
  bHttp.setReuse(false);
  bHttp.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);

  if (!bHttp.begin(bClient, binUrl)) {
    Serial.println("[OTA] http.begin (bin) failed");
    return false;
  }
  code = bHttp.GET();
  if (code != 200) {
    Serial.printf("[OTA] Binary GET failed: HTTP %d\n", code);
    bHttp.end();
    return false;
  }
  int contentLen = bHttp.getSize();
  if (contentLen > 0 && contentLen != (int)size) {
    Serial.printf("[OTA] Size mismatch: manifest=%ld, header=%d\n", size, contentLen);
    bHttp.end();
    return false;
  }

  if (!Update.begin((size_t)size, U_FLASH)) {
    Serial.printf("[OTA] Update.begin failed: %s\n", Update.errorString());
    bHttp.end();
    return false;
  }

  // Paint the OTA screen now that we're committed to writing flash.
  // The 2-second throttled block below updates the % from here on.
  g_otaScreenDrawn = false;
  drawOTAProgress(0, version.c_str(), "downloading...");

  mbedtls_sha256_context shaCtx;
  mbedtls_sha256_init(&shaCtx);
  mbedtls_sha256_starts(&shaCtx, 0);

  WiFiClient* stream = bHttp.getStreamPtr();
  uint8_t buf[4096];  // matches ESP32 flash sector size — Update.write batches per sector
  size_t total = 0;
  unsigned long lastLog = millis();
  unsigned long lastByteMs = millis();   // stall watchdog

  esp_task_wdt_reset();
  while (total < (size_t)size && (bHttp.connected() || stream->available())) {
    // Hard ceiling — gives up regardless of socket state. The diag
    // task would also catch this, but bailing here lets us clean up
    // (Update.abort, free SHA ctx) before the restart instead of
    // leaving the partition in a half-written state.
    if (g_otaDeadlineMs && (long)(millis() - g_otaDeadlineMs) > 0) {
      Serial.println("[OTA] Hard deadline hit — aborting download");
      Update.abort();
      mbedtls_sha256_free(&shaCtx);
      bHttp.end();
      return false;
    }
    size_t avail = stream->available();
    if (avail) {
      size_t toRead = avail > sizeof(buf) ? sizeof(buf) : avail;
      int n = stream->readBytes(buf, toRead);
      if (n <= 0) break;
      mbedtls_sha256_update(&shaCtx, buf, (size_t)n);
      size_t w = Update.write(buf, (size_t)n);
      if (w != (size_t)n) {
        Serial.printf("[OTA] Update.write short: %u/%d (%s)\n",
                      (unsigned)w, n, Update.errorString());
        Update.abort();
        mbedtls_sha256_free(&shaCtx);
        bHttp.end();
        return false;
      }
      total += n;
      lastByteMs = millis();
      if (millis() - lastLog > 2000) {
        int pct = (int)((100.0 * total) / size);
        Serial.printf("[OTA] %u / %ld bytes (%d%%)\n",
                      (unsigned)total, size, pct);
        drawOTAProgress(pct, version.c_str(), "downloading...");
        lastLog = millis();
        esp_task_wdt_reset();
      }
    } else {
      // No bytes ready. CRITICAL: the previous version called
      // delay(5) here without resetting the wdt and without bounding
      // how long a stall could last. With bHttp.connected() returning
      // true (TCP keep-alive) but the server stopped sending, the
      // loop would spin forever, never feed the wdt (esp_task_wdt_reset
      // was inside `if (avail)`), and the upload task would be wedged
      // — which is exactly what happened to E2/E4/E5 at 16:10 EDT.
      esp_task_wdt_reset();
      if (millis() - lastByteMs > OTA_STALL_MS) {
        Serial.printf("[OTA] Stall: no bytes for %lu ms — aborting\n",
                      (unsigned long)(millis() - lastByteMs));
        Update.abort();
        mbedtls_sha256_free(&shaCtx);
        bHttp.end();
        return false;
      }
      delay(5);
    }
    yield();
  }

  bHttp.end();

  if (total != (size_t)size) {
    Serial.printf("[OTA] Short download: %u/%ld\n", (unsigned)total, size);
    Update.abort();
    mbedtls_sha256_free(&shaCtx);
    return false;
  }

  uint8_t digest[32];
  mbedtls_sha256_finish(&shaCtx, digest);
  mbedtls_sha256_free(&shaCtx);

  String got = otaHexDigest(digest, 32);
  String want = sha256;
  want.toLowerCase();
  got.toLowerCase();
  if (got != want) {
    Serial.printf("[OTA] SHA256 mismatch:\n  got:  %s\n  want: %s\n",
                  got.c_str(), want.c_str());
    Update.abort();
    return false;
  }
  Serial.println("[OTA] SHA256 OK");
  drawOTAProgress(100, version.c_str(), "verifying...");

  if (!Update.end(true)) {
    Serial.printf("[OTA] Update.end failed: %s\n", Update.errorString());
    return false;
  }

  drawOTAProgress(100, version.c_str(), "rebooting...");
  Serial.printf("[OTA] Update OK. Rebooting into %s...\n", version.c_str());
  delay(1000);
  ESP.restart();
  return true;  // unreachable
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
    tprintf("IMU: %s (heel:%.0f pitch:%.0f)%s\n",
      imuOK ? "BNO085" : "NONE", imu.heel, imu.pitch,
      g_imuFailed ? " ⚠ FAILED (no events)" :
        (g_imuSilentReads > 10 ? " (silent reads warning)" : ""));
    tprintf("Pres: %s", presOK ? "" : "NONE");
    if (presOK) tprintf("%.1f hPa, %.1f°C", pressure.pressure_hpa, pressure.temperature_c);
    tprintln("");
    tprintf("SD:  %s\n", sdOK ? "OK" : "FAILED");
    tprintf("Battery: %.2fV (%d%%)%s\n", battery.voltage, battery.percent,
      battery.critical ? " CRITICAL!" : "");
    tprintf("Logging: %s\n", logging ? "YES" : "NO");
    tprintf("Data logged: %lu KB\n", totalBytes / 1024);
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

  } else if (cmd == "telneton") {
    telnetEnabled = true;
    if (wifiConnected && !telnetServerRunning) {
      startTelnetServer();
      tprintln("Telnet listener enabled and started");
    } else {
      tprintln("Telnet enabled — will start on next WiFi connect");
    }
  } else if (cmd == "telnetoff") {
    telnetEnabled = false;
    if (telnetServerRunning) {
      if (telnetClient && telnetClient.connected()) telnetClient.stop();
      telnetServer.end();
      telnetServerRunning = false;
      tprintln("Telnet listener stopped");
    } else {
      tprintln("Telnet was not running");
    }
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
    // BLE deinit NOT needed — uploads use plain HTTP, no TLS memory pressure.
    // BLE and WiFi coexist fine for basic HTTP PUTs.

    tprintln("Connecting to WiFi...");
    if (connectWiFi()) {
      tprintf("Connected to: %s, IP: %s\n", connectedSSID, WiFi.localIP().toString().c_str());
      tprintf("Free heap: %u bytes\n", ESP.getFreeHeap());

      // Test S3 connectivity
      tprintln("Testing S3 connection...");
      if (!testS3Connection()) {
        tprintln("S3 connection FAILED");
        return;
      }
      tprintln("S3 OK");

      // Set uploading flag so Core 0 task doesn't interfere, and OLED shows progress
      uploading = true;
      uploadCount = 0;
      uploadSuccess = 0;
      uploadFailed = 0;
      uploadCurrentFile[0] = '\0';
      uploadTotal = countFilesToUpload("/sf");
      tprintf("Found %d files to upload\n", uploadTotal);

      tprintln("Calling uploadDirectory...");
      yield();
      delay(100);
      uploadDirectory("/sf");
      uploading = false;
      tprintln("Upload complete");
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
      tprintln("Disconnecting WiFi...");
      // Set flag first to stop Core 1 from using WiFi services
      wifiConnected = false;
      delay(100);  // Let any pending WiFi operations complete
      if (telnetClient && telnetClient.connected()) {
        telnetClient.stop();
      }
      telnetServer.end();
      WiFi.disconnect(true);
      connectedSSID[0] = '\0';
      Serial.println("[WIFI] Disconnected via command");
    } else {
      tprintln("Not connected");
    }

  } else if (cmd == "update" || cmd == "ota") {
    tprintln("Starting OTA update from manifest...");
    // manual=true bypasses the per-boot one-shot guard — operator
    // explicitly asked, so a re-check is allowed even if the upload
    // task already ran one this boot.
    bool ok = performOTAUpdate(true);
    if (!ok) tprintln("OTA: failed (see serial log)");

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
    tprintf("Type: %s\n", imuOK ? "BNO085" : "NONE");
    tprintf("Heading: %.0f deg (magnetic)\n", imu.heading);
    tprintf("Heel: %.1f deg (starboard +, port -)\n", imu.heel);
    tprintf("Pitch: %.1f deg (bow up +, bow down -)\n", imu.pitch);
    tprintf("Accel: X=%.2f Y=%.2f Z=%.2f\n", imu.accel_x, imu.accel_y, imu.accel_z);
    tprintf("Calibration offsets: heel=%.1f, pitch=%.1f\n", imuHeelOffset, imuPitchOffset);
    tprintln("===================");

  } else if (cmd == "pres" || cmd == "pressure") {
    tprintln("=== Pressure/Temperature ===");
    if (presOK) {
      tprintf("Pressure: %.2f hPa (mbar)\n", pressure.pressure_hpa);
      tprintf("Temperature: %.1f °C\n", pressure.temperature_c);
      tprintf("Pressure range (10s window):\n");
      tprintf("  Min: %.2f hPa\n", pressure.pressure_min);
      tprintf("  Max: %.2f hPa\n", pressure.pressure_max);
      tprintf("  Delta: %.2f hPa (gust indicator)\n",
        pressure.pressure_max - pressure.pressure_min);
    } else {
      tprintln("DPS310 not detected");
    }
    tprintln("============================");

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
    if (strlen(wind.firmware) > 0) {
      tprintf("Firmware: %s\n", wind.firmware);
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
    pDataChar = nullptr;
    wind.connected = false;
    NimBLEDevice::deinit(false);
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
      NimBLEDevice::deinit(false);
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
    displayMode = (displayMode >= 3) ? 1 : displayMode + 1;
    // Force layout redraw on mode switch
    d2LayoutDrawn = false;
    d3LayoutDrawn = false;
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
        if (windFile) { windFile.flush(); windFile.close(); }
        if (presFile) { presFile.flush(); presFile.close(); }
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

  // v2.0.0 foundation commands (SF_FIRMWARE_V2_SPEC.md Stage 1)
  } else if (cmd == "hwid") {
    tprintf("platform=%s (config=%s)\n", hwName(g_hw), config.hardware_platform);

  } else if (cmd == "role") {
    tprintf("role=%s radio_mode=%s\n", roleName(g_role), radioModeName(g_radio_mode));

  } else if (cmd == "configver") {
    tprintf("config_version=%d boat_id=%s fw=%s\n",
            config.config_version, config.boat_id, FW_VERSION);

  } else if (cmd == "flags") {
    tprintf("imu_interval_ms=%d (baked)\n", IMU_INTERVAL_MS);
    tprintf("gnss_fix_rate=10 Hz (baked, PQTMCFGFIXRATE)\n");
    tprintf("wind_enabled=%s\n", config.wind_enabled ? "on" : "off");
    tprintf("telnet=%s\n", telnetEnabled ? "on" : "off");

  } else if (cmd == "radiomode") {
    tprintf("radio_mode=%s\n", radioModeName(g_radio_mode));

  } else if (cmd == "statusup") {
    // Stage 3 — force a fleet health snapshot PUT now. Manual trigger
    // for testing / verification; the upload task does this once per
    // boot automatically.
    if (!wifiConnected) {
      tprintln("statusup: WiFi not connected; run `wifi` to bring it up");
    } else {
      bool ok = uploadStatusSnapshot();
      tprintf("statusup: %s\n", ok ? "OK" : "failed (see Serial)");
      if (ok) g_statusCheckedThisBoot = true;
    }

  } else if (cmd == "configsync") {
    // Stage 3.5 — force a cloud config check. Observe-only MVP.
    if (!wifiConnected) {
      tprintln("configsync: WiFi not connected");
    } else {
      bool ok = performConfigSync();
      tprintf("configsync: %s\n", ok ? "OK (see Serial for diff)" : "failed");
      tprintf("  local config_version = %d\n", config.config_version);
      tprintf("  cloud config_version = %d%s\n",
              g_cloud_config_version,
              g_cloud_config_version < 0 ? " (not fetched)" : "");
      if (ok) g_configSyncCheckedThisBoot = true;
    }

  } else if (cmd == "ocs") {
    // Stage 4 — boat-local OCS state.
    if (!g_ocs.armed) {
      tprintln("ocs: NOT ARMED. Use `race arm <pin_lat> <pin_lon> <rc_lat> <rc_lon> <secs>`");
    } else {
      uint32_t now = millis();
      int32_t to_start = (int32_t)(g_ocs.start_time_ms - now);
      tprintln("ocs: ARMED");
      tprintf("  PIN: %.7f, %.7f\n", g_ocs.pin_lat, g_ocs.pin_lon);
      tprintf("  RC:  %.7f, %.7f\n", g_ocs.rc_lat, g_ocs.rc_lon);
      if (to_start > 0) {
        tprintf("  Start in: %d s\n", to_start / 1000);
      } else {
        tprintf("  Started: %d s ago\n", -to_start / 1000);
      }
      const char* side = g_ocs.distance_to_line_m >= 0 ? "pre-start" : "course";
      tprintf("  Distance to line: %+.2f m (%s side)\n",
              g_ocs.distance_to_line_m, side);
      tprintf("  Closure rate: %+.2f m/s%s\n", g_ocs.closure_rate_m_s,
              g_ocs.closure_rate_m_s < 0 ? " (approaching line)" : "");
      tprintf("  Over line: %s\n", g_ocs.over_line ? "YES" : "no");
      tprintf("  Was over at start: %s\n",
              g_ocs.was_over_at_start ? "YES" : "no");
    }

  } else if (cmd.startsWith("race arm ")) {
    // race arm <pin_lat> <pin_lon> <rc_lat> <rc_lon> <secs_from_now>
    // Stage 4.5: also broadcasts MSG_RACE_ARMED over ESP-NOW so all
    // other boats arm at the same instant. 3x transmission for
    // reliability — single packet losses don't lose the race start.
    double pln, plg, rln, rlg;
    int secs = 0;
    int n = sscanf(cmd.c_str(), "race arm %lf %lf %lf %lf %d",
                   &pln, &plg, &rln, &rlg, &secs);
    if (n != 5) {
      tprintln("usage: race arm <pin_lat> <pin_lon> <rc_lat> <rc_lon> <secs_from_now>");
      tprintln("       example: race arm 42.3601 -71.0589 42.3604 -71.0578 300");
    } else {
      uint32_t start_ms = millis() + (uint32_t)(secs * 1000);
      ocsArm(pln, plg, rln, rlg, start_ms);
      bool sent = meshBroadcastRaceArmed(pln, plg, rln, rlg, secs, 0, 30);
      tprintf("race armed locally: PIN(%.5f,%.5f) RC(%.5f,%.5f) T+0 in %d s\n",
              pln, plg, rln, rlg, secs);
      tprintf("mesh broadcast: %s (3x for reliability)\n", sent ? "OK" : "FAILED");
    }

  } else if (cmd == "race disarm" || cmd == "race off") {
    ocsDisarm();
    tprintln("race: disarmed locally (no mesh disarm message yet)");

  } else if (cmd == "mesh") {
    // ESP-NOW peer mesh status (Stage 2)
    if (!g_mesh_enabled) {
      tprintln("mesh: DISABLED");
    } else {
      tprintf("mesh: enabled, sender_id=0x%08lx, peers=%d/%d\n",
              (unsigned long)g_mesh_local_sender_id,
              g_mesh_peer_count, MESH_PEER_MAX);
      tprintf("  tx=%lu (fail %lu), rx=%lu (bad %lu)\n",
              (unsigned long)g_mesh_tx_count,
              (unsigned long)g_mesh_tx_fail_count,
              (unsigned long)g_mesh_rx_count,
              (unsigned long)g_mesh_rx_dropped_bad_magic);
      unsigned long now = millis();
      for (int i = 0; i < g_mesh_peer_count; i++) {
        const MeshPeerState& p = g_mesh_peers[i];
        tprintf("  peer 0x%08lx role=%u age=%lus msgs=%lu lat=%.7f lon=%.7f sog=%.1fkt cog=%d hdg.heel=%d\n",
                (unsigned long)p.sender_id,
                (unsigned)p.unit_role,
                (now - p.last_seen_ms) / 1000,
                (unsigned long)p.msg_count,
                p.last_lat_e7 / 1e7,
                p.last_lon_e7 / 1e7,
                p.last_sog_cm_s / 51.4444,
                p.last_cog_deg10 / 10,
                p.last_heel_deg);
      }
    }

  } else if (cmd == "fleet") {
    // Stage 5 — RC unit's fleet OCS view.
    // Shows per-peer distance from start line + RC-side OCS state.
    // RC-only because boats don't compute fleet-wide OCS.
    if (g_role != ROLE_RC_SIGNAL) {
      tprintf("fleet: only meaningful when role=rc_signal (current role=%d)\n",
              (int)g_role);
    } else if (!g_ocs.armed) {
      tprintln("fleet: OCS not armed (no race armed; use 'race arm ...')");
    } else {
      unsigned long now = millis();
      int32_t time_to_start = (int32_t)(g_ocs.start_time_ms - now);
      tprintf("fleet (RC view): %d peers, T%+ds, line %.6f,%.6f -> %.6f,%.6f\n",
              g_mesh_peer_count, time_to_start / 1000,
              g_ocs.pin_lat, g_ocs.pin_lon, g_ocs.rc_lat, g_ocs.rc_lon);
      for (int i = 0; i < g_mesh_peer_count; i++) {
        const MeshPeerState& p = g_mesh_peers[i];
        const char* ocs_state =
            p.rc_ocs_called ? "OCS"
                            : (p.rc_distance_m < 0 ? "over" : "ok ");
        tprintf("  0x%08lx role=%u fix=%u sat=%2u sog=%.1fkt hdg=%4.0f bow=%.2fm d=%+6.2fm %s%s\n",
                (unsigned long)p.sender_id,
                (unsigned)p.unit_role,
                (unsigned)p.fix_quality,
                (unsigned)p.sat_count,
                p.last_sog_cm_s / 51.4444,
                p.last_heading_deg10 / 10.0,
                bowOffsetForSender(p.sender_id),
                p.rc_distance_m,
                ocs_state,
                p.rc_ocs_called ? "*" : "");
      }
    }

  } else if (cmd == "classes") {
    // Stage 5.5 — dump per-class bow_offset registry loaded from
    // /sf/classes.csv. RC-only (boats use OCS_BOW_OFFSET_M directly).
    if (g_class_registry_count == 0) {
      tprintf("classes: registry empty (default bow=%.2fm applied to all peers)\n",
              OCS_BOW_OFFSET_M);
    } else {
      tprintf("classes: %d entries loaded from /sf/classes.csv\n",
              g_class_registry_count);
      for (int i = 0; i < g_class_registry_count; i++) {
        const ClassRegistryEntry& e = g_class_registry[i];
        tprintf("  %-12s (0x%08lx) class=%-12s bow=%.2fm\n",
                e.boat_id,
                (unsigned long)e.sender_id,
                e.class_name,
                e.bow_offset_m);
      }
    }

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
    tprintln("  pres       - Pressure/temperature sensor");
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
    tprintln("  telneton   - Enable telnet listener (off by default)");
    tprintln("  telnetoff  - Disable telnet listener");
    tprintln("  wifi       - Connect to WiFi");
    tprintln("  disconnect - Disconnect WiFi");
    tprintln("  update     - OTA pull from S3 manifest (manual)");
    tprintln("  reboot     - Restart device");
    tprintln("  hwid       - Show detected hardware platform (E1/B1)");
    tprintln("  role       - Show unit role + radio mode");
    tprintln("  configver  - Show config version + boat_id + firmware");
    tprintln("  flags      - Show v2.0.0 feature flag state");
    tprintln("  radiomode  - Show current radio mode");
    tprintln("  mesh       - ESP-NOW peer mesh status + peers seen");
    tprintln("  fleet      - RC view of fleet OCS (RC-only)");
    tprintln("  classes    - Show /sf/classes.csv bow_offset registry (RC-only)");
    tprintln("  statusup   - Upload fleet health snapshot to S3 now");
    tprintln("  configsync - Fetch cloud config from S3 (observe-only)");
    tprintln("  ocs        - Show OCS state (Stage 4)");
    tprintln("  race arm <pin_lat> <pin_lon> <rc_lat> <rc_lon> <secs>");
    tprintln("             - Arm OCS state machine for a race start");
    tprintln("  race disarm - Clear OCS arming");
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

  // Use config values. Stop-threshold fields stay in the config struct
  // for backwards compatibility with existing /sf/config.txt files but
  // are no longer consumed — recording stops only on a clean operator
  // action (SPDT power-off or `stoprec` serial/telnet command).
  float startThresh = config.start_speed_knots;
  unsigned long startDelay = config.start_delay_sec * 1000UL;

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
      } else if (uploading) {
        // Don't start recording while upload is in progress - SD card conflict
        Serial.println("[REC] Upload in progress, waiting to start recording...");
        // Stay in ARMED state, will retry next cycle
      } else if (now - armStartTime >= startDelay) {
        // Sustained speed — start recording
        sessionCount++;
        if (xSemaphoreTake(sdMutex, pdMS_TO_TICKS(2000))) {
          startLogging();
          xSemaphoreGive(sdMutex);
          recState = REC_RECORDING;
          Serial.printf("[REC] Recording STARTED — session %d\n", sessionCount);
        } else {
          // Mutex timeout - upload may be holding it, stay in ARMED
          Serial.println("[REC] SD busy, retrying...");
        }
      }
      break;

    case REC_RECORDING:
      // No auto-stop. Recording continues until the operator either
      // powers the device off via the SPDT slide switch or sends the
      // `stoprec` serial/telnet command. Speed-triggered stop was
      // removed because operators routinely sit at low speed (tactics
      // before start, between starts in a series, motoring back) and
      // false-stops were chopping sessions mid-race. The stationary-
      // upload path in uploadTaskFunc only fires while `!logging`, so
      // pending files upload at next boot after a clean power-cycle.
      (void)speed; (void)now;   // unused now — silence -Wunused
      break;

    case REC_STOPPING:
      // Unreachable since REC_RECORDING no longer transitions here.
      // Kept defensively: if something forces this state, finish the
      // stop cleanly so we don't sit in a zombie state with files
      // open and `logging` still true.
      if (xSemaphoreTake(sdMutex, pdMS_TO_TICKS(1000))) {
        navFile.flush(); navFile.close();
        imuFile.flush(); imuFile.close();
        if (windFile) { windFile.flush(); windFile.close(); }
        if (presFile) { presFile.flush(); presFile.close(); }
        xSemaphoreGive(sdMutex);
      }
      logging = false;
      recState = REC_IDLE;
      triggerUpload = true;
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
// COUNT PENDING UPLOADS
// ============================================================
void countPendingUploads() {
  // IMPORTANT: Skip counting while logging to avoid SD card conflicts
  // The logging task on Core 1 owns the SD card during recording
  if (!sdOK || logging) {
    // Don't change pendingUploads - keep last known value
    return;
  }

  // Try to get mutex, but don't block - skip if busy
  if (xSemaphoreTake(sdMutex, pdMS_TO_TICKS(100)) != pdTRUE) {
    return;  // SD busy, try again later
  }

  int navCount = 0;   // N: sessions with files still pending
  File root = SD.open("/sf");
  if (!root) {
    xSemaphoreGive(sdMutex);
    pendingUploads = 0;
    return;
  }

  // Walk each session, classifying unuploaded files. Stops at the first
  // pending file per session (the per-file flag flips and we move on).
  File session = root.openNextFile();
  while (session) {
    yield();  // Prevent watchdog timeout
    if (session.isDirectory()) {
      String sessName = session.name();
      if (!sessName.startsWith(".")) {
        bool hasPending = false;
        File f = session.openNextFile();
        while (f) {
          if (hasPending) {
            f.close();
            f = session.openNextFile();
            continue;
          }
          String fname = f.name();
          if (!fname.endsWith(".uploaded") && !fname.startsWith(".")) {
            String markerPath = String("/sf/") + sessName + "/" + fname + ".uploaded";
            if (!SD.exists(markerPath.c_str())) {
              hasPending = true;
            }
          }
          f.close();
          f = session.openNextFile();
        }
        if (hasPending) navCount++;
      }
    }
    session.close();
    session = root.openNextFile();
  }
  root.close();
  xSemaphoreGive(sdMutex);
  pendingUploads = navCount;
}

// ============================================================
// UPLOAD TASK (RUNS ON CORE 0)
// ============================================================
// Independent FreeRTOS task that prints Core 1's last-known section every 5s.
// If loopTask hangs, this task keeps running and the last printed [DIAG] line
// names the section Core 1 was inside. Replaces guesswork with evidence.
void diagnosticsTask(void* param) {
  esp_task_wdt_add(NULL);
  Serial.println("[DIAG] task started");
  uint32_t lastIter = 0;
  // Core 1 loop watchdog state: we let g_loopIter run for one diag tick
  // before we start the timer, so a fresh boot's first slow setup() pass
  // doesn't trip the watchdog.
  uint32_t loopWdLastIter = 0;
  unsigned long loopWdLastChangeMs = millis();

  while (true) {
    esp_task_wdt_reset();
    unsigned long now = millis();
    uint32_t iter = g_loopIter;
    long delta = (long)(iter - lastIter);
    Serial.printf("[DIAG] uptime=%lus heap=%u sect=%s iter=%lu (+%ld)\n",
                  now / 1000, ESP.getFreeHeap(),
                  (const char*)g_loopSection,
                  (unsigned long)iter, delta);
    lastIter = iter;

    // ---------- OTA hard deadline ----------
    // performOTAUpdate arms g_otaDeadlineMs at start and clears it at
    // every exit. If we see a non-zero deadline that's already past,
    // OTA is wedged (the 2026-05-05 16:10-EDT 3-of-6 hang signature).
    // Force a restart so the device doesn't sit indefinitely with
    // wifiBusy/uploading flags stuck.
    if (g_otaDeadlineMs && (long)(now - g_otaDeadlineMs) > 0) {
      char line[96];
      snprintf(line, sizeof(line),
               "ota watchdog: deadline exceeded at sect=%s — restart",
               (const char*)g_loopSection);
      appendBootLog(line);
      Serial.println(line);
      Serial.flush();
      delay(50);
      esp_restart();
    }

    // ---------- Core 1 loop watchdog ----------
    // If g_loopIter hasn't moved in LOOP_HANG_MS, Core 1 is wedged
    // somewhere. Log the last-known section and force restart so the
    // hang becomes a recoverable `reset=SW` next boot instead of a
    // permanent black-screen brick. Skipped while OTA is intentionally
    // running — flash writes can pause Core 1 for many seconds.
    if (iter != loopWdLastIter) {
      loopWdLastIter = iter;
      loopWdLastChangeMs = now;
    } else if (g_otaDeadlineMs == 0 && now - loopWdLastChangeMs > LOOP_HANG_MS) {
      char line[128];
      snprintf(line, sizeof(line),
               "loop watchdog: Core 1 stuck at sect=%s for %lums — restart",
               (const char*)g_loopSection,
               (unsigned long)(now - loopWdLastChangeMs));
      appendBootLog(line);
      Serial.println(line);
      Serial.flush();
      delay(50);
      esp_restart();
    }

    // Every 5 minutes, append an "alive" line to /boot.log with wall-clock
    // + battery + heap. The last such line before the next boot is the
    // device's last known good moment — that gap is how we tell crash /
    // battery-died / clean-power-off apart.
    if (g_bootSessionLogged && now - g_lastAliveLog >= 5UL * 60UL * 1000UL) {
      char iso[24];
      if (formatGpsIso(iso, sizeof(iso))) {
        char line[96];
        snprintf(line, sizeof(line), "alive t=%s batt=%.2fV %d%% heap=%u",
                 iso, battery.voltage, battery.percent, ESP.getFreeHeap());
        appendBootLog(line);
        g_lastAliveLog = now;
      }
    }

    vTaskDelay(pdMS_TO_TICKS(5000));
  }
}

void uploadTaskFunc(void* param) {
  Serial.println("[UPLOAD] Task started on Core 0");

  // Subscribe to the task watchdog so a hang here produces a backtrace
  // instead of a silent freeze. Reset at the top of every iteration.
  esp_err_t wdt_err = esp_task_wdt_add(NULL);
  if (wdt_err != ESP_OK) {
    Serial.printf("[WDT] Failed to subscribe uploadTask: %d\n", wdt_err);
  } else {
    Serial.println("[WDT] uploadTask subscribed");
  }

  unsigned long stationaryStart = 0;  // track how long boat has been still
  unsigned long lastPendingCount = 0;  // Last time we counted pending uploads

  // Count pending uploads immediately on boot (don't wait 30 seconds)
  countPendingUploads();
  Serial.printf("[UPLOAD] Initial pending: N=%d\n", pendingUploads);

  while (true) {
    esp_task_wdt_reset();
    unsigned long now = millis();
    bool shouldUpload = false;

    // Count pending uploads every 30 seconds (for display)
    if (now - lastPendingCount >= 30000) {
      lastPendingCount = now;
      countPendingUploads();
    }

    // Check various upload triggers
    if (triggerUpload && !logging) {
      // Recording just stopped - attempt upload (but respect recent WiFi failures)
      triggerUpload = false;

      // Force recount now that logging stopped (count was skipped during recording)
      countPendingUploads();
      Serial.printf("[UPLOAD] Recording stopped: N=%d pending\n", pendingUploads);

      if (pendingUploads == 0) {
        Serial.println("[UPLOAD] Nothing to upload");
      } else if (uploadRetryCount >= MAX_UPLOAD_RETRIES && now - lastUploadAttempt < UPLOAD_RETRY_DELAY_MS) {
        Serial.println("[UPLOAD] Recording stopped but WiFi backing off — will retry later");
      } else {
        shouldUpload = true;
        uploadRetryCount = 0;  // Reset retry counter for new session
        Serial.println("[UPLOAD] Triggered: recording stopped");
      }
    }
    else if (!logging && !uploading) {
      // Skip if no WiFi configured
      if (config.wifi_count == 0 || !strlen(config.upload_url)) {
        // No WiFi configured, skip
      }
      // Nothing pending to upload — but we still need to wake WiFi
      // periodically to check for new firmware. Without this branch a
      // boat that's fully caught up never connects, never checks the
      // OTA manifest, and never updates. Diagnosed 2026-05-16 after
      // the fleet missed multiple firmware pushes despite booting at
      // home on Home-IOT. The check runs at most once per boot
      // (g_otaCheckedThisBoot, enforced inside performOTAUpdate); the
      // stationary + interval gates avoid waking the radio while the
      // boat is on the water about to record.
      else if (pendingUploads == 0) {
        if (!g_otaCheckedThisBoot) {
          // Track stationary time the same way the upload branch does.
          if (gps.valid && gps.speed_kts >= 0.5) {
            stationaryStart = 0;  // boat moving — skip
          } else {
            if (stationaryStart == 0) stationaryStart = now;
            // Wait 30 s of stationary uptime before competing for the
            // radio (lets GPS get a fix, BNO settle, BLE wind connect).
            if (now - stationaryStart >= 30000 &&
                now - lastUploadCheck >= UPLOAD_CHECK_INTERVAL_MS) {
              lastUploadCheck = now;
              Serial.println("[OTA] No pending uploads — running OTA-only check");
              wifiBusy = true;
              if (!wifiConnected) connectWiFi();
              if (wifiConnected) {
                performOTAUpdate(false);   // body's SSID + version gates apply
                // Stage 3: piggyback fleet health snapshot on the same
                // WiFi window. Once per boot — boats that idle on
                // Home-IOT for hours don't need to spam status PUTs.
                if (!g_statusCheckedThisBoot) {
                  if (uploadStatusSnapshot()) g_statusCheckedThisBoot = true;
                }
                // Stage 3.5: cloud config sync (observe-only MVP).
                if (!g_configSyncCheckedThisBoot) {
                  if (performConfigSync()) g_configSyncCheckedThisBoot = true;
                }
                // Release the radio whether OTA happened or not.
                wifiTeardownRequested = true;
                // Clear wifiBusy so (a) the Core 1 teardown block can
                // proceed (it gates on !wifiBusy) and (b) meshTick can
                // resume broadcasting. Previously left stuck true after
                // "Already up to date" returns since OTA didn't restart
                // — observed on .14 as tx=N frozen on the canary boat.
                wifiBusy = false;
              } else {
                Serial.println("[OTA] WiFi connect failed for OTA-only check");
                wifiBusy = false;
              }
            }
          }
        }
      }
      // Only upload when stationary (speed < 0.5 kt) or no GPS fix
      // If no GPS fix, assume stationary (allow upload)
      else if (gps.valid && gps.speed_kts >= 0.5) {
        stationaryStart = 0;  // reset — boat is moving
      }
      else {
        if (stationaryStart == 0) stationaryStart = now;
        // No delay - connect immediately when stationary
        if (true) {
          // Stationary long enough — check periodic interval
          if (now - lastUploadCheck >= UPLOAD_CHECK_INTERVAL_MS) {
            lastUploadCheck = now;

            // Check retry backoff
            if (uploadRetryCount > 0 && now - lastUploadAttempt < UPLOAD_RETRY_DELAY_MS) {
              // Still in retry backoff period - skip
            } else if (uploadRetryCount >= MAX_UPLOAD_RETRIES) {
              // Max retries reached - wait longer before next attempt
              if (now - lastUploadAttempt >= UPLOAD_RETRY_DELAY_MS * 5) {
                uploadRetryCount = 0;  // Reset after extended wait
                shouldUpload = true;
                Serial.println("[UPLOAD] Triggered: retry reset");
              }
            } else {
              shouldUpload = true;
              Serial.printf("[UPLOAD] Triggered: periodic check (N=%d)\n",
                            pendingUploads);
            }
          }
        }
      }
    }

    // Perform upload if triggered
    if (shouldUpload) {
      lastUploadAttempt = now;

      // Mark WiFi as busy for the entire connect-+-upload window so Core 1
      // skips any LWIP-touching code paths (handleTelnet, telnetServer
      // calls). Without this guard, Core 1 deadlocks inside handleTelnet
      // on LWIP mutex contention during heavy uploads (see 2026.05.03.03
      // diag log: iter frozen at handleTelnet for entire upload phase).
      wifiBusy = true;

      // Try to connect to WiFi if not connected
      if (!wifiConnected) {
        connectWiFi();
      }

      if (wifiConnected) {
        // Set uploading=true to show UPLOADING screen
        uploading = true;
        uploadCount = 0;
        uploadSuccess = 0;
        uploadFailed = 0;
        uploadCurrentFile[0] = '\0';

        vTaskDelay(pdMS_TO_TICKS(100));

        Serial.printf("[UPLOAD] Starting (heap: %u, maxBlock: %u)\n",
                      ESP.getFreeHeap(), heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));

        // Test connectivity before starting uploads (skip after repeated failures)
        bool connOK = true;
        if (uploadRetryCount < 2) {
          connOK = testS3Connection();
          if (!connOK) {
            Serial.println("[UPLOAD] Connectivity test failed");
          }
        } else {
          Serial.printf("[UPLOAD] Skipping conn test (retry %d), trying upload directly\n", uploadRetryCount);
        }

        if (!connOK && uploadRetryCount < 2) {
          uploading = false;
          uploadRetryCount++;
        } else {
          if (xSemaphoreTake(sdMutex, pdMS_TO_TICKS(5000))) {
            // Count files for progress display
            uploadTotal = countFilesToUpload("/sf");
            Serial.printf("[UPLOAD] Found %d files to upload\n", uploadTotal);

            if (uploadTotal > 0) {
              uploadDirectory("/sf");
            }
            xSemaphoreGive(sdMutex);
          }

          uploading = false;
          uploadRetryCount = 0;  // Reset on successful cycle
          Serial.println("[UPLOAD] Cycle complete");

          // After upload cycle, recount pending to get accurate number.
          // MUST hold sdMutex — countFilesToUpload walks the SD tree and races
          // with Core 1's logging/recording start otherwise.
          int remaining = -1;
          if (xSemaphoreTake(sdMutex, pdMS_TO_TICKS(2000))) {
            remaining = countFilesToUpload("/sf");
            xSemaphoreGive(sdMutex);
          } else {
            Serial.println("[UPLOAD] Could not lock SD for recount — assuming 0");
            remaining = 0;
          }
          pendingUploads = (remaining > 0) ? remaining : 0;

          if (remaining <= 0) {
            // Auto-OTA: at most ONCE per boot. The first clean upload
            // cycle on Home-IOT WiFi triggers an OTA manifest check;
            // every subsequent clean cycle is a no-op (the function's
            // g_otaCheckedThisBoot flag flips on first entry). Keeps
            // the day-of-racing behaviour simple — the OTA either
            // happened at boot or it doesn't happen, no surprise
            // mid-day reboots if a new build gets published. Operator
            // can force a re-check via the serial 'update' command.
            Serial.println("[UPLOAD] Cycle clean — checking OTA manifest (one-shot per boot)");
            performOTAUpdate(false);

            // Stage 3 status snapshot upload — once per boot. Piggybacks
            // on the same WiFi window used for OTA + session uploads.
            if (!g_statusCheckedThisBoot) {
              if (uploadStatusSnapshot()) g_statusCheckedThisBoot = true;
            }
            // Stage 3.5 cloud config sync (observe-only MVP).
            if (!g_configSyncCheckedThisBoot) {
              if (performConfigSync()) g_configSyncCheckedThisBoot = true;
            }

            // All done — request WiFi teardown. We do NOT tear down here on
            // Core 0: ArduinoOTA.handle()/handleTelnet() run on Core 1 in the
            // main loop; tearing down WiFi from Core 0 races against them
            // (caused the 2026.05.01.4 post-upload crashes). The main loop
            // sees this flag, gates on !uploading && !triggerUpload, and
            // performs the teardown safely on Core 1.
            // Releasing WiFi is also required so the iPhone hotspot frees
            // a client slot for any boat that hasn't uploaded yet (only ~5
            // simultaneous clients allowed).
            Serial.println("[UPLOAD] All files uploaded — requesting WiFi teardown on Core 1");
            wifiTeardownRequested = true;
          } else {
            Serial.printf("[UPLOAD] %d files remaining — will retry\n", remaining);
          }
        }
      } else {
        Serial.println("[UPLOAD] WiFi connect failed");
        uploadRetryCount++;
        if (uploadRetryCount >= MAX_UPLOAD_RETRIES) {
          Serial.println("[UPLOAD] Max WiFi retries — backing off 25 min");
        }
      }

      // Reset stationary timer to avoid rapid retries
      stationaryStart = 0;
      // After WiFi failure, force backoff by updating lastUploadAttempt
      lastUploadAttempt = now;

      // WiFi work for this trigger is done — let Core 1 service telnet again.
      wifiBusy = false;
    }

    vTaskDelay(pdMS_TO_TICKS(5000));  // Check every 5 seconds
  }
}

// ============================================================
// MAIN LOOP
// ============================================================
void loop() {
  esp_task_wdt_reset();  // feed wdt — without this a stuck loop iteration
                         // panics in 300s with a backtrace pointing at the
                         // call that hung (firmware 2026.05.03.01 hard hang)
  g_loopIter++;
  g_loopSection = "top";
  unsigned long now = millis();

  // ----------------------------------------------------------
  // WiFi state management — MUST run before any handler call.
  //
  // (1) Sync wifiConnected with reality. WiFi.setAutoReconnect(false) means
  //     if the iPhone hotspot kicks an idle device, WiFi.status() flips to
  //     WL_DISCONNECTED but our flag stays stale. Calling ArduinoOTA.handle()
  //     or handleTelnet() against a dead stack panics the device.
  //
  // (2) Honor teardown requests from the upload task. Core 0 sets the flag
  //     after a successful upload cycle; we tear down here on Core 1 because
  //     we own the handlers. Gated on !uploading && !triggerUpload to avoid
  //     racing a new upload cycle that just kicked off.
  // ----------------------------------------------------------
  // Helper: disconnect from AP without powering down the radio.
  // WiFi.disconnect(true) (wifioff=true) reconfigures the radio, which
  // races with the BLE wind-sensor scanner on the shared ESP32 radio
  // (CLAUDE.md known issues #11/#12) and panicked Core 1 in firmware
  // 2026.05.02.03. WiFi.disconnect(false, false) just leaves the AP —
  // the iPhone hotspot slot is freed, which was the goal — while the
  // radio stays in STA mode so BLE coexistence is undisturbed.
  g_loopSection = "wifi-state-sync";
  // Skip while Core 0 is mid-WiFi-work — these calls go through LWIP and
  // deadlock under contention.
  if (!wifiBusy && wifiConnected && WiFi.status() != WL_CONNECTED) {
    Serial.printf("[WIFI] Lost connection (status=%d) — clearing stale flag\n", WiFi.status());
    Serial.flush();
    g_loopSection = "wifi-state-sync.telnet-stop";
    if (telnetClient && telnetClient.connected()) telnetClient.stop();
    g_loopSection = "wifi-state-sync.server-end";
    telnetServer.end();
    g_loopSection = "wifi-state-sync.wifi-disconnect";
    WiFi.disconnect(false, false);
    connectedSSID[0] = '\0';
    wifiConnected = false;
    wifiTeardownRequested = false;  // Already torn down
  }

  g_loopSection = "wifi-teardown-check";
  if (wifiTeardownRequested && !uploading && !triggerUpload && !wifiBusy && wifiConnected) {
    Serial.println("[WIFI] Honoring teardown request (Core 1)");
    Serial.flush();
    g_loopSection = "teardown.telnet-stop";
    if (telnetClient && telnetClient.connected()) telnetClient.stop();
    g_loopSection = "teardown.server-end";
    telnetServer.end();
    g_loopSection = "teardown.wifi-disconnect";
    WiFi.disconnect(false, false);
    connectedSSID[0] = '\0';
    wifiConnected = false;
    wifiTeardownRequested = false;
    Serial.println("[WIFI] Teardown complete");
    Serial.flush();
  }

  // Handle OTA updates (highest priority) — gated behind ENABLE_ARDUINO_OTA
  // because mDNS init + NimBLE active crashes the ESP32 (see top of file).
  // Telnet is also skipped while wifiBusy: handleTelnet's WiFiServer/WiFiClient
  // calls share LWIP locks with Core 0's HTTP uploads and deadlock under
  // sustained contention (firmware 2026.05.03.03 hang).
  if (wifiConnected && !wifiBusy) {
#if ENABLE_ARDUINO_OTA
    ArduinoOTA.handle();
    if (otaInProgress) return;  // Don't do anything else during OTA
#endif
    g_loopSection = "telnet";
    handleTelnet();
  }

  g_loopSection = "mesh";
  meshTick();

  g_loopSection = "ocs";
  ocsTick();

  g_loopSection = "rc-ocs";
  rcComputeFleetOCS();

  g_loopSection = "serial-cmd";
  handleSerialCommand();

  g_loopSection = "gps";
  readGPS();

  // Once per boot: when GPS time first becomes valid, stamp boot.log with
  // wall-clock + battery so we can correlate this session with the previous
  // "alive" tail and tell battery-died from clean-power-off.
  if (!g_bootSessionLogged) {
    char iso[24];
    if (formatGpsIso(iso, sizeof(iso))) {
      char line[80];
      snprintf(line, sizeof(line), "session t=%s batt=%.2fV %d%%",
               iso, battery.voltage, battery.percent);
      appendBootLog(line);
      g_bootSessionLogged = true;
    }
  }

  g_loopSection = "rec-state";
  updateRecordingState();

  // Sensor reads are I2C on Core 1, upload runs on Core 0 — no conflict.
  // SD logging (logIMU/logPressure) is guarded by `logging` which is always
  // false during upload (task checks !logging before starting).
  if (now - lastIMU >= IMU_INTERVAL_MS) {
    g_loopSection = "imu";
    readIMU();
    if (logging) { g_loopSection = "imu.log"; logIMU(); }
    lastIMU = now;
  }

  // Pressure sensor (0.1 Hz - weather trends only, not gust detection)
  static unsigned long lastPres = 0;
  if (presOK && now - lastPres >= PRES_INTERVAL_MS) {
    g_loopSection = "pres";
    readPressure();
    if (logging) { g_loopSection = "pres.log"; logPressure(); }
    lastPres = now;
  }

  // Reset pressure min/max every 10 seconds for fresh gust window
  static unsigned long lastPresReset = 0;
  if (presOK && now - lastPresReset >= 10000) {
    g_loopSection = "pres-reset";
    resetPressureMinMax();
    lastPresReset = now;
  }

  if (logging && gps.newGGA) {
    g_loopSection = "nav.log";
    logNav();
    gps.newGGA = false;
  }

#if ENABLE_WIND
  // Handle wind sensor
  if (config.wind_enabled) {
    g_loopSection = "wind-check";
    checkWindConnection();

    // Log wind data at configured interval
    if (logging && now - lastWind >= WIND_INTERVAL_MS) {
      g_loopSection = "wind.log";
      logWind();
      lastWind = now;
    }
  }
#endif

  if (now - lastDisp >= DISPLAY_UPDATE_MS) {
    g_loopSection = "display";
    updateDisplay();
    lastDisp = now;
  }

  if (logging && now - lastFlush >= FLUSH_INTERVAL_MS) {
    navFile.flush();
    if (imuFile) imuFile.flush();
#if ENABLE_WIND
    if (windFile) windFile.flush();
#endif
    if (presFile) presFile.flush();
    lastFlush = now;
  }

  // WiFi upload is handled entirely by uploadTaskFunc on Core 0
  // (removed checkWiFiUpload from main loop — was causing race condition crashes)

  // Battery monitoring (every 10 seconds)
  static unsigned long lastBattCheck = 0;
  if (now - lastBattCheck >= 10000) {
    g_loopSection = "battery";
    updateBattery();
    handleLowBattery();  // Will warn and halt if critical
    lastBattCheck = now;
  }
  g_loopSection = "loop-end";

  // RTCM3 debug output (every 30 seconds) - helps diagnose PPK data logging
  static unsigned long lastRtcmDebug = 0;
  if (now - lastRtcmDebug >= 30000) {
    // RTCM3 stats tracked but not printed (use 'status' command to see)
    lastRtcmDebug = now;
  }
}
