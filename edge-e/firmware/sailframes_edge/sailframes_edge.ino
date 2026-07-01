/*
 * SailFrames Edge — Fleet Tracker Firmware (unified E + B devices)
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
#include "rtk_relay.h" // RTK Phase-2: RTCM3 framer + reassembler (Gate A proven)
#include "freertos/stream_buffer.h"  // SPSC ring: recv-cb -> loop, RTCM bytes to GNSS

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

// Adaptive backlight (2026.05.26.04). Backlight is the dominant load
// in the power budget (~40% of total system draw on E-series).
// Dimming when not recording recovers ~30% of backlight current
// during the "between sails / pre-start" idle periods, gaining
// ~10-15% of total system runtime. Levels chosen per operator
// preference: 80% gives plenty of daylight readability while
// shaving 20% off the recording-mode backlight current; 50% is
// still legible in shade and saves half the idle backlight current.
//
// Uses the ESP32 Arduino Core 3.x ledcAttach API (pin-addressed),
// NOT the legacy 2.x ledcSetup/ledcAttachPin channel-addressed API
// which doesn't exist in Core 3.3.7 (built failed on 2026.05.26.04).
#define TFT_BL_PWM_FREQ     5000   // 5 kHz — well above flicker perception
#define TFT_BL_PWM_RES      8      // 8-bit duty (0-255)
#define TFT_BL_DUTY_RECORDING 204  // ~80%
#define TFT_BL_DUTY_IDLE      128  // ~50%

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

#ifdef BUILD_B1
// ---- B1 v0.13 Qi-pad self-power latch (edge-b/hardware/B1_V013_QI_POWER.md) ----
// B1 has NO hardware power switch. The ESP32 holds its own power: PWR_HOLD
// (GPIO19) drives /LATCH_Q through R28(0 Ohm) -> MT3608 boost EN + Q_PWR gate.
// HIGH = latched on (survives lift off the Qi pad); LOW = release (off, once
// off the pad — on the pad the D7 rail keeps the MCU alive until lifted).
// QI_PRESENT (GPIO15) reads the V_QI divider: HIGH = on the pad. Both pins are
// free on B (TFT_BL is GPIO25 here); on E this block is compiled out and GPIO19
// is the E backlight (TFT_BL in User_Setup.h) — never drive it there.
#define PWR_HOLD_PIN        19
#define QI_PRESENT_PIN      15
#define B1_BATT_CUTOFF_V    3.30f       // off-pad auto-power-off threshold (LiPo overdischarge guard)
#define B1_BATT_CUTOFF_MS   20000       // ...sustained this long (ride out GNSS/TFT sag transients)
#define B1_CHARGED_V        4.15f       // battery-plateau "charged" inference (no TP4056 STDBY pin wired)
#define B1_IDLE_OFF_MS      1800000UL   // 30 min charged+uploaded+idle on pad -> store-and-forget off
// QI_PRESENT debounce, asymmetric (hysteresis): quick to latch ON (docking
// stops recording promptly) but slow to release OFF, so a brief Qi-link wobble
// from imperfect pad coupling doesn't flip the display or reset the on-pad
// timers. A real lift holds QI low continuously, so OFF still fires in ~2.5 s.
#define B1_QI_ON_MS         200
#define B1_QI_OFF_MS        2500
#endif

// ============================================================
// CONFIGURATION
// ============================================================
// Firmware version: YYYY.MM.DD.N (date + daily build number)
// Platform-specific so a B-only change doesn't re-rev (and needlessly re-OTA)
// the E fleet, and vice versa. Bump the line for the platform you changed; CI
// builds + publishes each variant at its own version.
#ifdef BUILD_B1
#define FW_VERSION    "2026.06.29.04"   // B (LC29HEA)
#else
#define FW_VERSION    "2026.06.29.02"   // E (LG290P)
#endif
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
#ifdef BUILD_B1
#define SCREEN_WIDTH  240     // B1: 2.8" ILI9341 portrait width
#define SCREEN_HEIGHT 320     // B1: 2.8" ILI9341 portrait height
#else
#define SCREEN_WIDTH  320     // E: 3.5" ST7796 portrait width
#define SCREEN_HEIGHT 480     // E: 3.5" ST7796 portrait height
#endif
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
  // lat/lon are DOUBLE, not float: a float32 near 42° has ~0.4 m resolution
  // (worse at the atof parse step) — it silently quantizes away the cm RTK fix
  // before the value is ever stored, capping OCS at ~0.5 m. Double preserves it.
  double lat = 0, lon = 0;
  float alt = 0;
  float speed_kts = 0, course = 0, hdop = 99.9;
  int satellites = 0, fix_quality = 0;
  char utc_time[12] = "000000.00";
  char date[8] = "010100";
  bool valid = false;
  bool newGGA = false;
  // GST 1-sigma position error std-devs in metres (LG290P; 0 until GST parses).
  float lat_std = 0, lon_std = 0, alt_std = 0;
  // Unified horizontal 1-sigma accuracy (m), 0 = no data. Set from GST
  // (LG290P, = sqrt(lat_std^2+lon_std^2)) OR from $PQTMEPE EPE_2D (LC29HEA,
  // which supports neither GST nor float GST). The whole system reads this.
  float hacc_m = 0;
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
#ifdef BUILD_B1
HardwarePlatform g_hw = HW_B1;   // B1 build default — a no-SD boot still uses the LC29H path
#else
HardwarePlatform g_hw = HW_E1;
#endif
UnitRole         g_role = ROLE_RACING_BOAT;
RadioMode        g_radio_mode = MODE_BOOT;

// RTK Phase-2 relay state (docs/RTK_PHASE2_DESIGN.md). All inert unless
// config.rtk_enabled. The RC base (rc_signal) PRODUCES; everyone else CONSUMES.
RtcmFramer           g_rtcmTx;             // RC base: Serial2 RTCM frames (loop ctx only)
RtcmReassembler      g_rtcmRx;             // rover: ESP-NOW frags (recv-cb ctx only)
StreamBufferHandle_t g_rtcmRing = nullptr; // recv-cb -> loop: reassembled RTCM bytes to GNSS
uint8_t              g_rtcmTxMsgId = 0;    // rolling msg_id for fragmentation (loop ctx)
static inline bool roleIsBase()  { return g_role == ROLE_RC_SIGNAL; }
static inline bool roleIsRover() { return g_role != ROLE_RC_SIGNAL; }

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
    uint8_t  hdop_x10;             // RTK Phase-2: HDOP*10 from peer (0 = no data)
    uint8_t  hacc_mm;             // RTK Phase-2: GST horiz 1-sigma mm from peer (0 = no data)
    uint32_t last_seen_ms;
    int8_t   last_rssi;            // ESP-NOW RX RSSI (dBm) of last packet — link-budget/range debug (0 = no data)
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
  char boat_id[16] = "UNCFG";  // Non-colliding sentinel. If config.txt is missing/blank (SD reads OK
                               // but no boat_id), the device joins the mesh as "UNCFG" rather than
                               // impersonating a real boat. A default of a real ID ("E1") would put a
                               // duplicate FNV-1a sender_id on the mesh, corrupting peers/OCS/registry.
                               // (SD-unreadable is handled separately by the boot-time SD fault gate.)
  char wind_mac[20] = "";  // Calypso Mini MAC (loaded from /wind_mac.txt if present)
  bool wind_enabled = false;  // Auto-enabled if /wind_mac.txt exists on SD
  int wind_offset = 0;  // Heading offset in degrees (added to raw AWA for sensor mounting correction)
  // Recording thresholds
  float start_speed_knots = 1.5;
  float stop_speed_knots = 0.5;
  int start_delay_sec = 10;
  int stop_delay_sec = 180;

  // v2.0.0 foundation (SF_FIRMWARE_V2_SPEC.md Stage 1)
#ifdef BUILD_B1
  char hardware_platform[8] = "b1";       // "e1" or "b1" (B1 build default)
#else
  char hardware_platform[8] = "e1";       // "e1" or "b1"
#endif
  char unit_role[24]        = "racing_boat";
  int  config_version       = 0;          // bumped by cloud config sync (Stage 3)
  // RTK Phase-2 (docs/RTK_PHASE2_DESIGN.md). SD-config ONLY — deliberately NOT
  // cloud-allow-listed: flipping it reconfigures the GNSS (base/rover RTK) and
  // is a physical bring-up act, not a remote push. Default off ⇒ byte-identical
  // to pre-RTK behavior, so an OTA that ships this code changes nothing until set.
  bool rtk_enabled          = false;
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

#ifdef BUILD_B1
// B1 v0.13 Qi-pad self-power latch state (see PWR_HOLD/QI_PRESENT defines).
// "Parked" is a non-blocking loop STATE, not a halt: loop() early-returns each
// iteration after g_loopIter++/wdt-feed, so the Core-1 loop watchdog (gotcha
// #22) never fires an esp_restart() that would re-run setup() and re-latch
// PWR_HOLD HIGH (powering the unit back on). It dies when lifted off the pad.
bool g_b1Parked            = false;  // latch released; loop() is in the parked early-return
bool g_b1QiPresent         = false;  // debounced: device is on the Qi pad
bool g_b1ParkScreenDrawn   = false;  // parked screen painted once
bool g_b1ChargeShown       = false;  // on-pad charging screen painted once
bool g_b1ChargeScreenActive = false; // charging screen currently owns the display
unsigned long g_b1LowBattSinceMs = 0;  // 0 = not currently below cutoff while off-pad
unsigned long g_b1IdleSinceMs    = 0;  // 0 = idle-on-pad (store-and-forget) timer not running
#endif

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

// Where Core 0's upload task currently is. Complements g_loopSection so
// the [DIAG] heartbeat names the stuck section on either core when a task
// wdt fires. Added 2026-05-26 after the .25 wdt-during-upload saga where
// the wdt panicked with "uploadTask hung" but the boot-log + DIAG couldn't
// tell us WHERE inside the upload pipeline it was — connect, scan,
// sendRequest body, response drain? Set at every major checkpoint in
// uploadTaskFunc, uploadFile, connectWiFi, performOTAUpdate, performConfigSync,
// uploadStatusSnapshot. Value "idle" means uploadTask is sleeping between
// iterations (vTaskDelay 5 s) — not stuck.
volatile const char* g_uploadSection = "idle";

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
  // ESP-NOW RX RSSI (dBm) for link-budget / range debugging. Captured
  // here, surfaced per-peer in the `mesh` command. Guard the pointer
  // chain in case a core build doesn't populate rx_ctrl.
  int8_t rx_rssi = (info && info->rx_ctrl) ? (int8_t)info->rx_ctrl->rssi : 0;
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
    g_mesh_peers[idx].hdop_x10          = p->hdop_x10;   // 0 = no data
    g_mesh_peers[idx].hacc_mm           = p->hacc_mm;    // 0 = no data
    g_mesh_peers[idx].last_seen_ms      = millis();
    g_mesh_peers[idx].last_rssi         = rx_rssi;
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
  else if (h->msg_type == MSG_RTCM_FRAG) {
    // RTK Phase-2 — rover ingests RC base corrections. This callback is the
    // ONLY context that touches g_rtcmRx; completed frames go to the ring via
    // its onFrame (rtcmRingPush). Gated off during uploads (RF contention,
    // gotchas #21/#22). Inert unless rtk_enabled — old-firmware boats and
    // non-RTK boats simply fall through here, ignoring the new msg_type.
    if (config.rtk_enabled && roleIsRover() && !wifiBusy && !uploading) {
      g_rtcmRx.onPacket(data, len);
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

  // ESP-NOW range fixes (2026.06.08):
  // 1) Disable modem power-save. Default STA power-save duty-cycles the
  //    receiver, so it sleeps through broadcasts — at the link margin
  //    this looks like a sharp short-range cliff (peers vanish past a
  //    few metres). WIFI_PS_NONE keeps the RX always listening.
  // 2) Pin max TX power for the ALWAYS-ON mesh. setTxPower was only
  //    applied inside connectWiFi() (the upload window), so mesh-only
  //    operation ran at the post-mode default. Make it explicit here.
  WiFi.setSleep(false);                    // esp_wifi_set_ps(WIFI_PS_NONE)
  WiFi.setTxPower(WIFI_POWER_19_5dBm);

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
  // Per-boat quality for the RC pre-race panel (former reserved[2], same 20 B
  // wire). 0 = "no data" (no fix / no GST) so the RC renders "--", never 0.
  // Clamp: hdop is 99.9 with no fix (×10 overflows u8), hacc could exceed 255 mm.
  if (gps.valid && gps.hdop > 0.1f && gps.hdop < 25.5f) {
    p->hdop_x10 = (uint8_t)lroundf(gps.hdop * 10.0f);
  } else {
    p->hdop_x10 = 0;   // no data
  }
  float hacc_mm = gps.hacc_m * 1000.0f;   // unified: GST (LG290P) or PQTMEPE (LC29HEA)
  p->hacc_mm = (hacc_mm > 0.5f) ? (uint8_t)fminf(255.0f, lroundf(hacc_mm)) : 0;  // 0 = no data

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

// ============================================================
// RTK Phase-2 — RTCM relay glue (docs/RTK_PHASE2_DESIGN.md)
// ============================================================
// Rover, ESP-NOW recv-callback context: a completed RTCM frame from the
// reassembler. Push into the ring for the loop to drain to the GNSS UART —
// the ring is the ONLY object that crosses the callback↔loop boundary.
// Drop silently if the ring is full (RTCM is loss-tolerant; rover rides diff-age).
void rtcmRingPush(const uint8_t* frame, int len) {
  if (g_rtcmRing) xStreamBufferSend(g_rtcmRing, frame, (size_t)len, 0);
}

// RC base, loop context (readGPSBase): fragment a complete RTCM frame and
// broadcast it 2× over ESP-NOW. Gated off during uploads to avoid the WiFi/RF
// contention that caused past fleet hangs (gotchas #21/#22) — racing never
// overlaps an upload, so nothing is lost.
void rtcmBroadcastFrame(const uint8_t* frame, int len) {
  if (!g_mesh_enabled || wifiBusy || uploading) return;
  uint8_t msg_id = g_rtcmTxMsgId++;
  int fc = (len + RTCM_FRAG_MAX - 1) / RTCM_FRAG_MAX;
  if (fc > RTK_MAX_FRAGS) return;   // shouldn't happen (≤1029 B), defensive
  uint8_t buf[sizeof(MeshHeader) + 4 + RTCM_FRAG_MAX];
  for (int rep = 0; rep < 2; rep++) {                 // 2×-tx for loss margin
    for (int fi = 0; fi < fc; fi++) {
      int off = fi * RTCM_FRAG_MAX;
      int flen = len - off; if (flen > RTCM_FRAG_MAX) flen = RTCM_FRAG_MAX;
      MeshHeader* h = (MeshHeader*)buf;
      h->magic[0] = MESH_MAGIC_0; h->magic[1] = MESH_MAGIC_1; h->version = MESH_VERSION;
      h->msg_type = MSG_RTCM_FRAG; h->seq = g_mesh_seq++; h->ttl = 0; h->reserved = 0;
      h->sender_id = g_mesh_local_sender_id; h->gps_time_ms = 0;
      RtcmFragPayload* p = (RtcmFragPayload*)(buf + sizeof(MeshHeader));
      p->msg_id = msg_id; p->frag_index = (uint8_t)fi; p->frag_count = (uint8_t)fc;
      p->frag_len = (uint8_t)flen;
      memcpy(p->data, frame + off, flen);
      esp_now_send(MESH_BROADCAST_ADDR, buf, sizeof(MeshHeader) + 4 + flen);
      delayMicroseconds(250);   // light pacing for peers' recv; small so readGPSBase RX doesn't overflow
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
  // A re-arm is a new start sequence — a clean slate. Clear any prior RC-side
  // OCS latches on every peer so boats recalled in a previous start don't stay
  // flagged OCS into the new one. (rc_ocs_called is RC-only state; clearing it
  // on a racing boat is harmless.)
  for (int i = 0; i < g_mesh_peer_count; i++) {
    g_mesh_peers[i].rc_ocs_called = false;
    g_mesh_peers[i].rc_ocs_called_at_ms = 0;
  }
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

// ============================================================
// fleetwatch — live RC fleet dashboard (Serial)
// ============================================================
// Reverse FNV-1a lookup so the dashboard shows "E3" instead of a raw
// 0x05cbe9c9. Fleet is small + fixed; just hash the known ids and match.
const char* boatNameForSender(uint32_t id) {
  static const char* names[] = {"E1","E2","E3","E4","E5","E6","B1","F1"};
  for (unsigned i = 0; i < sizeof(names)/sizeof(names[0]); i++) {
    if (boatIdHash(names[i]) == id) return names[i];
  }
  return "??";
}

// Non-blocking live dashboard: `fleetwatch` toggles g_fleetWatch; this tick
// runs from the main loop and re-paints the RC fleet table ~2 Hz using ANSI
// cursor-home so it updates in place. Blocking here would freeze mesh/OCS and
// trip the loop watchdog, so it MUST be loop-driven, not a while() in the cmd
// handler. Needs a VT100 terminal (screen/picocom/minicom/PuTTY) — the
// Arduino IDE Serial Monitor doesn't render the escapes.
bool g_fleetWatch = false;
unsigned long g_fleetWatchLast = 0;

void fleetWatchTick() {
  if (!g_fleetWatch) return;
  unsigned long now = millis();
  if (now - g_fleetWatchLast < 500) return;  // ~2 Hz
  g_fleetWatchLast = now;
  static uint32_t refresh = 0;
  refresh++;

  Serial.print("\033[H");  // cursor home (overwrite in place)
  if (g_role != ROLE_RC_SIGNAL) {
    Serial.printf("fleetwatch: role is not rc_signal (role=%d) — no fleet OCS here\033[K\r\n", (int)g_role);
    Serial.print("\033[J");
    return;
  }
  if (!g_ocs.armed) {
    Serial.print("fleetwatch: OCS not armed — use 'race arm <pinLat> <pinLon> <rcLat> <rcLon> <secs>'\033[K\r\n");
    Serial.print("\033[J");
    return;
  }
  int32_t tts = (int32_t)(g_ocs.start_time_ms - now);
  Serial.printf("RC FLEET LIVE  T%+ds  peers=%d  bow=%.2fm  #%lu   (type 'fleetwatch' to stop)\033[K\r\n",
                tts / 1000, g_mesh_peer_count, OCS_BOW_OFFSET_M, (unsigned long)refresh);
  Serial.printf("line %.6f,%.6f -> %.6f,%.6f\033[K\r\n",
                g_ocs.pin_lat, g_ocs.pin_lon, g_ocs.rc_lat, g_ocs.rc_lon);
  Serial.print("NAME ID          FIX SAT  SOG   HDG    d(m)    STATE age\033[K\r\n");
  for (int i = 0; i < g_mesh_peer_count; i++) {
    const MeshPeerState& p = g_mesh_peers[i];
    const char* st = p.rc_ocs_called ? "OCS*" : (p.rc_distance_m < 0 ? "over" : "ok");
    Serial.printf("%-4s 0x%08lx  %u  %2u  %4.1f  %4.0f  %+7.2f  %-4s  %lus\033[K\r\n",
                  boatNameForSender(p.sender_id), (unsigned long)p.sender_id,
                  (unsigned)p.fix_quality, (unsigned)p.sat_count,
                  p.last_sog_cm_s / 51.4444, p.last_heading_deg10 / 10.0,
                  p.rc_distance_m, st, (now - p.last_seen_ms) / 1000);
  }
  Serial.print("\033[J");  // erase anything below the table
}

// ============================================================
// RC fleet OCS panel — live on the TFT (rc_signal + armed)
// ============================================================
// Shows each mesh peer by name with its distance-to-line and a colour-coded
// OCS state on E6's screen. Partial redraw: the static layout is painted once
// and only rows whose distance/state changed are repainted, so it updates
// live without full-screen flicker. Replaces the nav display while the RC is
// armed. Refresh cadence is sped up by the loop's dispGate while armed.
bool g_rcPanelShown = false;

// --- RC panel chrome: top-of-screen clock + bottom FW/battery footer ----
// Added 2026-06-08 per request: first row = time-of-day (HH:MM:SS), last
// row = firmware version + battery %. Shared by both RC panels (armed
// fleet view + pre-race roster); each repaints only on change so they
// don't fight the panels' partial-redraw flicker control.
#define RC_FOOTER_H            28
// Venue-local clock offset from UTC, in minutes. Boston EDT (summer) = -240.
// Manual knob — no automatic DST: Boston EST (winter) = -300, 0 = raw UTC.
#define RC_CLOCK_TZ_OFFSET_MIN (-240)

static void drawRcClock(bool force) {
  static int prevSec = -1;
  int sod = -1;  // local seconds-of-day (-1 = no valid GPS time yet)
  if (gps.valid && strlen(gps.utc_time) >= 6) {
    int hh = (gps.utc_time[0]-'0')*10 + (gps.utc_time[1]-'0');
    int mm = (gps.utc_time[2]-'0')*10 + (gps.utc_time[3]-'0');
    int ss = (gps.utc_time[4]-'0')*10 + (gps.utc_time[5]-'0');
    sod = (((hh*3600 + mm*60 + ss) + RC_CLOCK_TZ_OFFSET_MIN*60) % 86400 + 86400) % 86400;
  }
  if (!force && sod == prevSec) return;
  prevSec = sod;
  tft.fillRect(0, 0, 150, 34, TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextDatum(TL_DATUM);
  char tb[16];
  if (sod >= 0) snprintf(tb, sizeof(tb), "%02d:%02d:%02d", sod/3600, (sod/60)%60, sod%60);
  else          strcpy(tb, "--:--:--");
  tft.drawString(tb, 6, 8, 4);
}

static void drawRcFooter(bool force) {
  static int prevBatt = -999;
  if (!force && battery.percent == prevBatt) return;
  prevBatt = battery.percent;
  const int FY = SCREEN_HEIGHT - RC_FOOTER_H;
  tft.fillRect(0, FY, SCREEN_WIDTH, RC_FOOTER_H, TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  char fb[40];
  snprintf(fb, sizeof(fb), "FW %s", FW_VERSION);
  tft.setTextDatum(TL_DATUM);
  tft.drawString(fb, 6, FY + 6, 2);
  char bb[12];
  snprintf(bb, sizeof(bb), "BAT %d%%", battery.percent);
  tft.setTextDatum(TR_DATUM);
  tft.drawString(bb, SCREEN_WIDTH - 6, FY + 6, 2);
}

void drawRcFleetPanel() {
  static int      prevCount = -1;
  static uint32_t prevSender[MESH_PEER_MAX];
  static int      prevDm[MESH_PEER_MAX];
  static int8_t   prevSt[MESH_PEER_MAX];
  static int      prevTsec = -99999;

  unsigned long now = millis();
  int tsec = (int)((int32_t)(g_ocs.start_time_ms - now) / 1000);

  bool full = (!g_rcPanelShown || g_mesh_peer_count != prevCount);
  if (full) {
    g_rcPanelShown = true;
    prevCount = g_mesh_peer_count;
    prevTsec = -99999;
    for (int i = 0; i < MESH_PEER_MAX; i++) { prevSender[i] = 0; prevDm[i] = -1000000; prevSt[i] = -2; }
    tft.fillScreen(COLOR_BG);
    tft.fillRect(0, 0, SCREEN_WIDTH, 34, TFT_BLACK);
    // Top-left "RC FLEET" label dropped — the time-of-day clock now lives
    // there (drawRcClock); the countdown stays on the right of the bar.
    tft.setTextColor(COLOR_LABEL, COLOR_BG);
    tft.drawString("BOAT", 8, 42, 2);
    tft.drawString("DIST", 95, 42, 2);
    tft.drawString("ST", 238, 42, 2);
    tft.drawFastHLine(0, 62, SCREEN_WIDTH, COLOR_DIVIDER);
  }

  // Countdown in the title bar (redraw only when the whole second changes).
  if (tsec != prevTsec) {
    prevTsec = tsec;
    tft.fillRect(180, 0, SCREEN_WIDTH - 180, 34, TFT_BLACK);
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    tft.setTextDatum(TR_DATUM);
    char tb[16]; snprintf(tb, sizeof(tb), "T%+ds", tsec);
    tft.drawString(tb, SCREEN_WIDTH - 6, 8, 4);
  }

  const int rowH = 64, y0 = 70, maxRows = (SCREEN_HEIGHT - RC_FOOTER_H - y0) / rowH;
  for (int i = 0; i < g_mesh_peer_count && i < maxRows; i++) {
    const MeshPeerState& p = g_mesh_peers[i];
    int dm = (int)lroundf(p.rc_distance_m * 10.0f);
    int8_t st = p.rc_ocs_called ? 2 : (p.rc_distance_m < 0 ? 1 : 0);
    if (p.sender_id == prevSender[i] && dm == prevDm[i] && st == prevSt[i]) continue;
    prevSender[i] = p.sender_id; prevDm[i] = dm; prevSt[i] = st;
    int y = y0 + i * rowH;
    tft.fillRect(0, y, SCREEN_WIDTH, rowH - 4, COLOR_BG);
    tft.setTextDatum(TL_DATUM);
    tft.setTextSize(2);
    tft.setTextColor(COLOR_TEXT, COLOR_BG);
    tft.drawString(boatNameForSender(p.sender_id), 8, y + 6, 4);
    char db[16]; snprintf(db, sizeof(db), "%+.1f", p.rc_distance_m);
    tft.drawString(db, 95, y + 6, 4);
    uint16_t sc = (st == 2) ? COLOR_ERROR : (st == 1) ? COLOR_WARN : COLOR_GOOD;
    const char* ss = (st == 2) ? "OCS" : (st == 1) ? "OVR" : "OK";
    tft.setTextColor(sc, COLOR_BG);
    tft.drawString(ss, 238, y + 6, 4);
    tft.setTextSize(1);
  }

  drawRcClock(full);
  drawRcFooter(full);
}

// ============================================================
// RC pre-race panel — shown on the RC (rc_signal) when NOT armed
// ============================================================
// The RC's job before the start is to confirm every boat is connected and has
// a good fix — so the RC never shows the nav (COG/SOG) display. Instead this
// fleet-connection roster: per-boat fix quality + sat count + link freshness,
// plus this base's own status and a "<fixed>/<connected> FIX" readiness gauge.
// Partial per-row redraw (on change) avoids flicker; link is OK/STALE rather
// than a ticking age (which would force a 1 Hz full repaint).
bool g_rcPrePanelShown = false;

void drawRcPreRacePanel() {
  // Sunlight-readable: BIG font-4 values, ALL BLACK (no color washes out on the
  // white-background TFT). One line per boat: name · FIX · ACC · HDOP · SAT,
  // under column headers. Partial per-row redraw on change. Fix state is read
  // from the text (FIX/FLT/---), not color.
  static uint32_t prevSender[MESH_PEER_MAX];
  static int8_t   prevQ[MESH_PEER_MAX];
  static int      prevSat[MESH_PEER_MAX], prevHdop[MESH_PEER_MAX], prevHacc[MESH_PEER_MAX];
  static int      prevConn = -1, prevFixed = -1;
  static int      prevBaseSat = -1, prevBaseHdop = -1, prevBaseHacc = -1;
  static int8_t   prevBaseReady = -1;

  const int CX_NAME = 8, CX_FIX = 76, CX_ACC = 146, CX_HDOP = 234, CX_SAT = 288;
  const int HDR_Y = 46, DIV_Y = 64, ROW0 = 70, rowH = 48;

  int conn = g_mesh_peer_count, fixed = 0;
  for (int i = 0; i < g_mesh_peer_count; i++)
    if (g_mesh_peers[i].fix_quality == 4) fixed++;
  int8_t baseReady = (gps.valid && (gps.lat != 0 || gps.lon != 0)) ? 1 : 0;

  // Static layout — repaint on first show or when the peer COUNT changes.
  bool full = (!g_rcPrePanelShown || conn != prevConn);
  if (full) {
    g_rcPrePanelShown = true;
    prevConn = -999; prevFixed = -999;
    prevBaseSat = -1; prevBaseHdop = -1; prevBaseHacc = -1; prevBaseReady = -1;
    for (int i = 0; i < MESH_PEER_MAX; i++) {
      prevSender[i]=0; prevQ[i]=-2; prevSat[i]=-1; prevHdop[i]=-1; prevHacc[i]=-1;
    }
    tft.fillScreen(COLOR_BG);
    tft.fillRect(0, 0, SCREEN_WIDTH, 34, TFT_BLACK);
    // Top-left "RC PRE-RACE" label dropped — the time-of-day clock now
    // occupies the top bar (drawRcClock); FIX gauge stays on the right.
    tft.setTextColor(COLOR_TEXT, COLOR_BG); tft.setTextDatum(TL_DATUM);
    tft.drawString("BOAT", CX_NAME, HDR_Y, 2);
    tft.drawString("ST",   CX_FIX,  HDR_Y, 2);
    tft.drawString("ACC",  CX_ACC,  HDR_Y, 2);
    tft.drawString("HDOP", CX_HDOP, HDR_Y, 2);
    tft.drawString("SAT",  CX_SAT,  HDR_Y, 2);
    tft.drawFastHLine(0, DIV_Y, SCREEN_WIDTH, COLOR_DIVIDER);
  }

  // Header right: "<fixed>/<connected> FIX" readiness gauge (white on black bar).
  if (conn != prevConn || fixed != prevFixed) {
    prevConn = conn; prevFixed = fixed;
    tft.fillRect(168, 0, SCREEN_WIDTH - 168, 34, TFT_BLACK);
    tft.setTextColor(TFT_WHITE, TFT_BLACK); tft.setTextDatum(TR_DATUM);
    char hb[20]; snprintf(hb, sizeof(hb), "%d/%d FIX", fixed, conn);
    tft.drawString(hb, SCREEN_WIDTH - 6, 8, 4);
  }

  // BASE row (row 0) — this boat's own status in the same columns: ST=RDY/SVY,
  // ACC (gps.hacc_m), HDOP (gps.hdop), SAT (gps.satellites).
  int bHd = (gps.valid && gps.hdop > 0.1f && gps.hdop < 25.5f) ? (int)lroundf(gps.hdop * 10) : 0;
  int bHa = (gps.hacc_m > 0.0005f) ? (int)fminf(255.0f, lroundf(gps.hacc_m * 1000)) : 0;
  if (baseReady != prevBaseReady || gps.satellites != prevBaseSat ||
      bHd != prevBaseHdop || bHa != prevBaseHacc) {
    prevBaseReady = baseReady; prevBaseSat = gps.satellites; prevBaseHdop = bHd; prevBaseHacc = bHa;
    int y = ROW0;
    tft.fillRect(0, y, SCREEN_WIDTH, rowH - 4, COLOR_BG);
    tft.setTextSize(1); tft.setTextDatum(TL_DATUM); tft.setTextColor(COLOR_TEXT, COLOR_BG);
    tft.drawString("BASE", CX_NAME, y + 4, 4);
    tft.drawString(baseReady ? "RDY" : "SVY", CX_FIX, y + 4, 4);
    char a[12], h[8], s[6];
    if (bHa > 0) snprintf(a, sizeof(a), "%.1fcm", bHa / 10.0); else strcpy(a, "--");
    if (bHd > 0) snprintf(h, sizeof(h), "%.1f", bHd / 10.0);   else strcpy(h, "--");
    snprintf(s, sizeof(s), "%d", gps.satellites);
    tft.drawString(a, CX_ACC,  y + 4, 4);
    tft.drawString(h, CX_HDOP, y + 4, 4);
    tft.drawString(s, CX_SAT,  y + 4, 4);
  }

  // Peer rows (below the BASE row) — name · FIX · ACC · HDOP · SAT, big + black.
  int maxRows = (SCREEN_HEIGHT - RC_FOOTER_H - (ROW0 + rowH)) / rowH;
  for (int i = 0; i < g_mesh_peer_count && i < maxRows; i++) {
    const MeshPeerState& p = g_mesh_peers[i];
    int q = p.fix_quality, sat = p.sat_count, hd = p.hdop_x10, ha = p.hacc_mm;
    if (p.sender_id == prevSender[i] && q == prevQ[i] && sat == prevSat[i] &&
        hd == prevHdop[i] && ha == prevHacc[i]) continue;
    prevSender[i]=p.sender_id; prevQ[i]=q; prevSat[i]=sat; prevHdop[i]=hd; prevHacc[i]=ha;
    int y = ROW0 + (i + 1) * rowH;
    tft.fillRect(0, y, SCREEN_WIDTH, rowH - 4, COLOR_BG);
    tft.setTextSize(1); tft.setTextDatum(TL_DATUM); tft.setTextColor(COLOR_TEXT, COLOR_BG);
    tft.drawString(boatNameForSender(p.sender_id), CX_NAME, y + 4, 4);
    tft.drawString(q==4?"FIX":q==5?"FLT":q==2?"DGP":q==1?"GPS":"---", CX_FIX, y + 4, 4);
    char a[12], h[8], s[6];
    if (ha > 0) snprintf(a, sizeof(a), "%.1fcm", ha / 10.0); else strcpy(a, "--");
    if (hd > 0) snprintf(h, sizeof(h), "%.1f", hd / 10.0);   else strcpy(h, "--");
    snprintf(s, sizeof(s), "%d", sat);
    tft.drawString(a, CX_ACC,  y + 4, 4);
    tft.drawString(h, CX_HDOP, y + 4, 4);
    tft.drawString(s, CX_SAT,  y + 4, 4);
  }

  drawRcClock(full);
  drawRcFooter(full);
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
#ifdef BUILD_B1
  // FIRST GPIO op (before any slow init): latch our own power ON. A B1 always
  // cold-boots on the Qi pad via the D7 rail; driving PWR_HOLD HIGH now makes
  // the MT3608 boost self-sustaining so the unit survives being lifted off the
  // pad. GPIO15=QI_PRESENT as INPUT (no pull) so the off-pad read is a solid
  // LOW (the V_QI divider drives it HIGH on the pad). See B1_V013_QI_POWER.md.
  pinMode(PWR_HOLD_PIN, OUTPUT);
  digitalWrite(PWR_HOLD_PIN, HIGH);
  pinMode(QI_PRESENT_PIN, INPUT);
#endif
  Serial.begin(SERIAL_BAUD);
  delay(500);

  // Capture WHY we last rebooted before any other init runs. The fleet
  // had a simultaneous-reboot event on 2026-05-03 with no serial captured;
  // this prints the cause at the top of the next boot log AND appends it
  // to /boot.log on SD so we can read it later if no USB was connected.
  esp_reset_reason_t resetReason = esp_reset_reason();

  Serial.println("\n=================================");
  Serial.printf("  SailFrames Edge %s\n", FW_VERSION);
  Serial.println("  Hardware Power Switch Edition");
  Serial.printf("  Reset reason: %s (%d)\n", resetReasonStr(resetReason), (int)resetReason);
  Serial.printf("  Free heap: %u, min ever: %u\n",
                ESP.getFreeHeap(),
                (unsigned)esp_get_minimum_free_heap_size());
  Serial.println("=================================");
#ifdef BUILD_B1
  Serial.println("[B1PWR] PWR_HOLD(GPIO19)=HIGH — self-power latched. QI_PRESENT=GPIO15.");
#endif

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
#ifdef BUILD_B1
  Serial.println("[TFT] Initializing ILI9341V (B1 2.8\" 240x320)...");
#else
  Serial.println("[TFT] Initializing ST7796U...");
#endif
  // Backlight via PWM so we can dim during idle. Init at IDLE level —
  // updateBacklight() in the loop pushes to RECORDING when logging starts.
  // Core 3.x ledcAttach: one call attaches a pin with freq + resolution,
  // and ledcWrite addresses the PIN (not a channel) thereafter.
  ledcAttach(TFT_BL_PIN, TFT_BL_PWM_FREQ, TFT_BL_PWM_RES);
  ledcWrite(TFT_BL_PIN, TFT_BL_DUTY_IDLE);
  tft.init();
  tft.setRotation(2);  // Portrait orientation (180° from rotation 0)
#ifdef BUILD_B1
  tft.invertDisplay(true);   // ILI9341 IPS on B1 — bench-confirmed colors need inversion
#else
  tft.invertDisplay(true);  // Required for correct colors on this ST7796 panel
#endif
  tft.fillScreen(COLOR_BG);
  oledOK = true;
#ifdef BUILD_B1
  Serial.println("[TFT] ILI9341V initialized (240x320 portrait)");
#else
  Serial.println("[TFT] ST7796U initialized (320x480 portrait)");
#endif

  // SD-card fault gate. If the card never came up, loadConfig() never ran
  // and config.boat_id is still its compile-time default ("E1"). Booting on
  // would put a SECOND "E1" on the mesh — duplicate FNV-1a sender_id —
  // corrupting peer state, OCS, and the class registry (this is exactly how
  // an E6 with a half-seated card silently impersonated E1). Refuse to boot:
  // show a persistent fault screen and stop here, BEFORE meshInit() ever
  // broadcasts a bogus identity. We are past tft.init() but before
  // esp_task_wdt_add(NULL), so the task WDT is not yet armed; the delay()
  // in the loop below still yields to the IDF idle task, keeping its
  // watchdog fed. Recovery is operator action: reseat the card + power-cycle.
  if (!sdOK) {
#ifdef BUILD_B1
    // B1.0 hardware has a dead microSD (J5 net→pad bug, fixed in v0.13). For
    // bring-up, continue in DEGRADED mode instead of halting: no logging, the
    // sentinel boat_id "UNCFG" (non-colliding), but TFT + GNSS + power latch all
    // come up. The identity-impersonation risk the E gate below guards against
    // does NOT apply — UNCFG never collides with a real boat. E build keeps the
    // halt (this whole branch is compiled out without -DBUILD_B1).
    Serial.println("[SD] B1 build: SD unavailable — continuing in DEGRADED mode "
                   "(no logging, boat_id=UNCFG). v0.13 hardware fixes the SD.");
#else
    tft.fillScreen(COLOR_BG);
    tft.setTextDatum(MC_DATUM);
    tft.setTextColor(COLOR_ERROR, COLOR_BG);
    tft.setTextSize(2);
    tft.drawString("SD CARD", SCREEN_WIDTH/2, 70, 4);
    tft.drawString("FAILURE", SCREEN_WIDTH/2, 125, 4);
    tft.setTextColor(COLOR_TEXT, COLOR_BG);
    tft.setTextSize(1);
    tft.drawString("Contact", SCREEN_WIDTH/2, 215, 4);
    tft.drawString("Paul Avillach", SCREEN_WIDTH/2, 260, 4);
    tft.setTextSize(2);
    tft.drawString("857 891 0512", SCREEN_WIDTH/2, 325, 2);
    tft.setTextSize(1);
    Serial.println("[SD] FATAL: SD unreadable at boot — refusing to start "
                   "(would impersonate default boat_id). Reseat card + power-cycle.");
    while (true) {
      Serial.println("[SD] HALTED: SD card failure — see TFT for contact info.");
      delay(5000);  // yields to idle task so the IDF idle WDT stays fed
    }
#endif
  }

  // Splash screen - show device ID, domain, and firmware version
  tft.fillScreen(COLOR_BG);
  tft.setTextDatum(MC_DATUM);

  // Draw device ID in HUGE font (fill most of the screen)
  tft.setTextColor(COLOR_TEXT, COLOR_BG);
#ifdef BUILD_B1
  tft.setTextSize(3);   // 2.8" 240px wide: a 5-char id ("UNCFG") must fit
#else
  tft.setTextSize(8);
#endif
  tft.drawString(config.boat_id, SCREEN_WIDTH/2, SCREEN_HEIGHT/2 - 60, 4);

  // "Sailframes.com" - black, medium size
  tft.setTextColor(TFT_BLACK, COLOR_BG);
  tft.setTextSize(1);
  tft.drawString("Sailframes.com", SCREEN_WIDTH/2, SCREEN_HEIGHT/2 + 80, 4);

  // Firmware version at bottom - large enough to read across the cabin
  tft.setTextColor(COLOR_LABEL, COLOR_BG);
#ifdef BUILD_B1
  tft.setTextSize(1);   // "2026.06.08.03" at size 2 font 4 overflows 240px
#else
  tft.setTextSize(2);
#endif
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
  // Enlarge the RX FIFO (default 256 B ≈ 5.5 ms at 460800). The RTK base path
  // (readGPSBase) can spend a few ms broadcasting RTCM mid-read; a 2 KB buffer
  // (~44 ms) absorbs that so outgoing base RTCM isn't dropped on RX overflow.
  Serial2.setRxBufferSize(2048);   // must precede begin()
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
  gnssConfigure();   // RTK off ⇒ exactly configureLG290P(); on ⇒ base/rover per role+chip

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
  rtkRelayInit();   // RTK Phase-2: arm relay callbacks + rover ring (inert unless rtk_enabled)
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
// RTK Phase-2 — GNSS config driver (docs/RTK_PHASE2_DESIGN.md §3/§8)
// ============================================================
// Command sets empirically pinned 2026-06-03/04: E rover (LG290P) FIXED via
// PQTMCFGRTK; E base from PPK archive (08cdadfe); B base/rover (LC29HEA) bench-
// verified. configureLG290P() above is left UNTOUCHED so the rtk_enabled==false
// path is byte-identical to pre-RTK firmware.

// E rover: standard 10 Hz NMEA config + RTK relative mode. PQTMCFGRTK is a
// runtime setting (no restart needed); saved so a reboot keeps it.
void lg290pConfigRover() {
  configureLG290P();                       // unchanged: rover, 10 Hz, NMEA on (+save+restart)
  Serial.println("[GPS] Enabling RTK rover (PQTMCFGRTK,W,1,2,120) + GST accuracy...");
  sendPQTM("PQTMCFGRTK,W,1,2,120");        // DiffMode=Auto, RelMode=relative, 120 s diff-age
  // Accuracy output — enable BOTH so the SAME rover config works on either chip
  // (this config also runs on LC29HEA boats left as hardware_platform=e1):
  //   LG290P  -> GST (2-param form); LC29HEA NAKs it.
  //   LC29HEA -> PQTMEPE (3-param form); LG290P NAKs it.
  sendPQTM("PQTMCFGMSGRATE,W,GST,1");       // LG290P GST -> gps.hacc_m
  sendPQTM("PQTMCFGMSGRATE,W,PQTMEPE,1,2"); // LC29HEA $PQTMEPE EPE_2D -> gps.hacc_m
  sendPQTM("PQTMSAVEPAR");
  delay(300);
}

// E base (rc_signal): Base mode (locks 1 Hz) + survey-in, then after restart
// (re)issue the non-persistent RTCM3-out + re-enable NMEA. Args from PPK archive.
void lg290pConfigBase() {
  Serial.println("[GPS] Configuring LG290P as RTK BASE (1 Hz, MSM7 out)...");
  sendPQTM("PQTMCFGRCVRMODE,W,2");         // base mode (locks 1 Hz)
  delay(200);
  sendPQTM("PQTMCFGSVIN,W,1,60,0,0,0,0");  // survey-in: short/loose (base err is common-mode)
  delay(200);
  sendPQTM("PQTMSAVEPAR");
  delay(400);
  sendPQTM("PQTMSRR");                      // restart to apply base mode
  delay(6000);
  while (Serial2.available()) Serial2.read();
  // post-restart: RTCM out (non-persistent → re-issue each boot) + re-enable NMEA
  sendPQTM("PQTMCFGRTCM,W,7,0,-90,07,06,1,0");  // MSM7 1077/1087/1097/1127 + eph + 1006
  delay(200);
  sendPQTM("PQTMCFGMSGRATE,W,GGA,1");       // base mode auto-disables NMEA; RC still needs GGA
  sendPQTM("PQTMCFGMSGRATE,W,RMC,1");
  sendPQTM("PQTMCFGMSGRATE,W,GSA,1");        // GSA -> base hdop for the RC panel
  sendPQTM("PQTMCFGMSGRATE,W,GST,1");        // LG290P base accuracy -> gps.hacc_m
  sendPQTM("PQTMCFGMSGRATE,W,PQTMEPE,1,2");  // (if base is ever LC29HEA) -> gps.hacc_m
  Serial.println("[GPS] LG290P base: MSM7 + 1006 + ephemeris @ 1 Hz");
}

// B rover (LC29HEA): rover + 10 Hz + GGA/RMC. RTK engages from rover mode +
// inbound corrections (Phase-1 proven 2026-06-03, no explicit RTK-enable needed).
void lc29hConfigRover() {
  Serial.println("[GPS] Configuring LC29HEA as RTK rover (10 Hz)...");
  sendPQTM("PQTMCFGRCVRMODE,W,1");          // rover (Quectel cmd, shared)
  delay(200);
  sendPQTM("PAIR050,100");                  // 10 Hz position output
  delay(200);
  sendPQTM("PAIR062,0,01");                 // enable GGA (sentence 0)
  delay(200);
  sendPQTM("PQTMCFGMSGRATE,W,PQTMEPE,1,2"); // accuracy: LC29HEA $PQTMEPE EPE_2D -> gps.hacc_m
  delay(200);                               // (LC29HEA supports neither GST nor float-GST)
  sendPQTM("PQTMSAVEPAR");
  delay(300);
  // NOTE: RMC ($PAIR062 sentence id) for the B rover still needs bench
  // confirmation — GGA (fix quality) is sufficient for OCS; COG falls back to IMU.
}

// B base (LC29HEA): bench-verified 2026-06-04. Base mode + survey-in + reboot,
// then non-persistent RTCM enables ($PAIR432 MSM7 / 434 1005 / 436 eph / 062 GGA).
void lc29hConfigBase() {
  Serial.println("[GPS] Configuring LC29HEA as RTK BASE (1 Hz)...");
  sendPQTM("PQTMCFGRCVRMODE,W,2");          // base mode
  delay(250);
  sendPQTM("PQTMCFGSVIN,W,1,60,0.0,0,0,0"); // survey-in: short/loose
  delay(250);
  sendPQTM("PQTMSAVEPAR");
  delay(400);
  sendPQTM("PAIR023");                       // reboot to apply base mode
  delay(3000);
  while (Serial2.available()) Serial2.read();
  sendPQTM("PAIR432,1"); delay(150);         // MSM7 observations
  sendPQTM("PAIR434,1"); delay(150);         // 1005 station position
  sendPQTM("PAIR436,1"); delay(150);         // ephemeris
  sendPQTM("PAIR062,0,01"); delay(150);      // GGA (base disables NMEA)
  sendPQTM("PAIR062,2,01"); delay(150);      // GSA -> base hdop for the RC panel
  sendPQTM("PQTMCFGMSGRATE,W,PQTMEPE,1,2"); delay(150);  // LC29HEA base accuracy -> gps.hacc_m
  Serial.println("[GPS] LC29HEA base: MSM7 + 1005 + ephemeris @ 1 Hz");
}

// Single entry point used at boot + on the `gps` reconfig command. With RTK
// off this is EXACTLY configureLG290P() (byte-identical legacy path).
void gnssConfigure() {
  if (!config.rtk_enabled) {
    configureLG290P();
#ifdef BUILD_B1
    // B1/LC29HEA: enable EPE (estimated position error) so gps.hacc_m gives a
    // live horizontal-accuracy readout on the TFT even with RTK off. Sent after
    // configureLG290P()'s save+restart → RAM-only, re-applied every boot. Kept
    // out of configureLG290P() so the E (LG290P) build stays byte-identical.
    sendPQTM("PQTMCFGMSGRATE,W,PQTMEPE,1,2");
    delay(150);
#endif
    return;
  }
  bool base = roleIsBase();
  if (g_hw == HW_B1) { if (base) lc29hConfigBase();  else lc29hConfigRover();  }
  else               { if (base) lg290pConfigBase(); else lg290pConfigRover(); }
}

// RTK relay init — set callbacks + alloc the rover ring. Inert unless enabled.
void rtkRelayInit() {
  if (!config.rtk_enabled) return;
  if (roleIsBase()) {
    g_rtcmTx.onFrame = rtcmBroadcastFrame;
    Serial.println("[RTK] relay PRODUCER (RC base) armed");
  } else {
    g_rtcmRx.onFrame = rtcmRingPush;
    g_rtcmRing = xStreamBufferCreate(4096, 1);   // SPSC byte ring, trigger level 1
    Serial.printf("[RTK] relay CONSUMER (rover) armed, ring=%s\n", g_rtcmRing ? "ok" : "ALLOC FAIL");
  }
  appendBootLog(roleIsBase() ? "rtk relay base" : "rtk relay rover");
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

// RTK base read path: the LG290P/LC29HEA in base mode emits RTCM3 (binary)
// interleaved with 1 Hz NMEA on Serial2. Demux is LENGTH-driven via the
// RtcmFramer (never '$'-keyed — a 0x24 inside a binary payload must not flip
// us into NMEA mode). Complete CRC-valid frames fire g_rtcmTx.onFrame
// (rtcmBroadcastFrame); non-RTCM bytes feed the normal NMEA line parser.
// Used only when rtk_enabled && role==rc_signal; otherwise readGPS() runs.
void readGPSBase() {
  while (Serial2.available()) {
    uint8_t c = Serial2.read();
    if (g_rtcmTx.feed(c)) continue;          // consumed as part of an RTCM frame
    if (c == '$') {
      nmeaIdx = 0; nmeaBuf[nmeaIdx++] = c;
    } else if (c == '\n' || c == '\r') {
      if (nmeaIdx > 5) { nmeaBuf[nmeaIdx] = '\0'; parseNMEA(nmeaBuf); nmeaIdx = 0; }
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
      double raw = atof(f);              // double: preserve ddmm.mmmmmm to cm
      int deg = (int)(raw / 100);
      gps.lat = deg + (raw - deg * 100) / 60.0;
      char ns[4];
      if (getField(s, 3, ns, sizeof(ns)) && ns[0] == 'S') gps.lat = -gps.lat;
    }
    if (getField(s, 4, f, sizeof(f))) {
      double raw = atof(f);              // double: preserve ddmm.mmmmmm to cm
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
  } else if (strstr(s, "GST")) {
    // GST — position error statistics (RTK Phase-2 accuracy readout). Fields:
    // 6 = latitude σ (m), 7 = longitude σ (m), 8 = altitude σ (m). Horizontal
    // 1σ ≈ √(latσ²+lonσ²); ~1-2 cm at RTK FIXED, ~decimetres-metre at FLOAT.
    // Enabled only on the RTK rover path (PQTMCFGMSGRATE,W,GST,1).
    char f[32];
    if (getField(s, 6, f, sizeof(f))) { float v = atof(f); if (v >= 0 && v < 1000) gps.lat_std = v; }
    if (getField(s, 7, f, sizeof(f))) { float v = atof(f); if (v >= 0 && v < 1000) gps.lon_std = v; }
    if (getField(s, 8, f, sizeof(f))) { float v = atof(f); if (v >= 0 && v < 1000) gps.alt_std = v; }
    gps.hacc_m = sqrtf(gps.lat_std * gps.lat_std + gps.lon_std * gps.lon_std);
  } else if (strstr(s, "PQTMEPE")) {
    // LC29HEA accuracy: $PQTMEPE,<ver>,<N>,<E>,<D>,<2D>,<3D>. Field 5 = horizontal
    // (2D) error in metres. The LC29HEA supports neither GST nor float-GST, so
    // this is its accuracy source (enabled via PQTMCFGMSGRATE,W,PQTMEPE,1,2).
    char f[32];
    if (getField(s, 5, f, sizeof(f))) { float v = atof(f); if (v >= 0 && v < 1000) gps.hacc_m = v; }
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
// E only. B1 uses the factory-calibrated analogReadMilliVolts path with a true
// 2.0 divider ratio (see readBatteryVoltage) — the raw-analogRead + single ratio
// fudge (2.09) under-read near full and capped the B1 gauge ~90% on a full pack.
const float BATT_DIVIDER_RATIO = 2.25;
const int BATT_SAMPLES = 16;  // Average multiple readings for stability

void setupBattery() {
  // GPIO34 is input-only, no internal pull-up (ideal for ADC)
  analogReadResolution(12);  // 12-bit ADC (0-4095)
  analogSetAttenuation(ADC_11db);  // Full 0-3.3V range
}

float readBatteryVoltage() {
  uint32_t sum = 0;
#ifdef BUILD_B1
  // B1: factory-calibrated ADC path. Raw analogRead*(3.3/4095) is nonlinear near
  // the top of the range and under-read ~0.03V at full (a rested-full 4.16V pack
  // read ~4.13V -> 90%). analogReadMilliVolts applies the ESP32 eFuse ADC
  // calibration for accurate mV. Divider R1/R2 = 100k/100k -> x2.0
  // (B1_V013_AUDIT.md). Verified against a 4.159V multimeter reading.
  for (int i = 0; i < BATT_SAMPLES; i++) {
    sum += analogReadMilliVolts(BATT_VOLTAGE_PIN);
    delayMicroseconds(100);
  }
  return (sum / (float)BATT_SAMPLES) / 1000.0f * 2.0f;
#else
  for (int i = 0; i < BATT_SAMPLES; i++) {
    sum += analogRead(BATT_VOLTAGE_PIN);
    delayMicroseconds(100);
  }
  float raw = (float)sum / BATT_SAMPLES;
  float adcVoltage = (raw / 4095.0) * 3.3;  // Voltage at ADC pin
  return adcVoltage * BATT_DIVIDER_RATIO;    // Actual battery voltage
#endif
}

int getBatteryPercent(float voltage) {
  // Voltage -> state-of-charge lookup (LiPo discharge curve, flat in the middle).
#ifdef BUILD_B1
  // B1 rested-LiPo curve: a LiPo rests at ~4.15V when full (it only sits at
  // 4.20V while actively being charged), so 4.15V maps to 100% here — otherwise
  // a fully-charged pack caps ~95% forever. 3.30V = 0% (overdischarge cutoff).
  static const float voltageTable[] = {
    4.15, 4.10, 4.05, 4.00, 3.90, 3.85, 3.80, 3.75, 3.70, 3.60, 3.50, 3.40, 3.30
  };
  static const int percentTable[] = {
    100,   92,   85,   76,   60,   52,   44,   35,   26,   14,    6,    2,    0
  };
#else
  static const float voltageTable[] = {
    4.20, 4.15, 4.10, 4.05, 4.00, 3.90, 3.80, 3.70, 3.60, 3.50, 3.40, 3.30
  };
  static const int percentTable[] = {
    100,   95,   85,   75,   65,   50,   35,   20,   12,    6,    2,    0
  };
#endif
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
  float v = readBatteryVoltage();
#ifdef BUILD_B1
  // B1's GPIO34 sense is flaky — it logs 0.00 V and 4.48 V for a pack that is
  // really ~4.1 V (see /boot.log). Reject physically-impossible reads (a LiPo
  // lives in ~2.5–4.35 V) and hold the last good value. This stops the gauge
  // from flapping AND stops the on-pad charging screen from redrawing the big
  // %% on every garbage sample (an avoidable SPI/CPU load spike near the lift).
  if (v < 2.5f || v > 4.35f) {
    if (battery.valid) { battery.lastRead = millis(); return; }
  }
  // EMA-smooth the flaky B1 sense so the gauge doesn't bounce read-to-read
  // (e.g. the 34/45/38% chatter seen on a poorly-coupled pad). Slow enough to
  // steady the display, fast enough to track real charge/discharge over minutes.
  if (battery.valid) v = 0.75f * battery.voltage + 0.25f * v;
#endif
  battery.voltage = v;
  battery.percent = getBatteryPercent(v);
  battery.critical = isBatteryCritical();
  battery.valid = true;
  battery.lastRead = millis();
}

void handleLowBattery() {
#ifdef BUILD_B1
  // B1 has no power switch and must never halt: low-battery power-off is owned
  // by b1PowerTick() (off-pad + sustained -> b1EnterParked, a non-blocking loop
  // state). A spin-halt here would freeze g_loopIter and the Core-1 loop
  // watchdog (gotcha #22) would esp_restart() + re-latch PWR_HOLD back ON.
  return;
#else
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
#endif
}

#ifdef BUILD_B1
// ============================================================
// B1 v0.13 Qi-pad self-power management (B1_V013_QI_POWER.md §6)
// ============================================================
// Read QI_PRESENT with a simple debounce so coil-seating bounce / contact
// chatter doesn't toggle the on-pad state. Returns the debounced level.
static bool b1ReadQiPresent() {
  static unsigned long highSinceMs = 0;  // when raw last went HIGH (0 = currently low)
  static unsigned long lowSinceMs  = 0;  // when raw last went LOW  (0 = currently high)
  static bool state = false;             // debounced on-pad state
  int raw = digitalRead(QI_PRESENT_PIN);
  unsigned long now = millis();
  if (raw == HIGH) {
    lowSinceMs = 0;
    if (highSinceMs == 0) highSinceMs = now;
    if (!state && now - highSinceMs >= B1_QI_ON_MS) state = true;    // quick latch ON
  } else {
    highSinceMs = 0;
    if (lowSinceMs == 0) lowSinceMs = now;
    if (state && now - lowSinceMs >= B1_QI_OFF_MS) state = false;    // slow release OFF (hysteresis)
  }
  return state;
}

// Release the self-power latch. Flush + close any open files and record the
// cause to /boot.log BEFORE dropping PWR_HOLD — off the pad that drop is
// instant power loss. On the pad the MCU stays alive on the D7 rail and loop()
// parks (non-blocking) until the unit is lifted, at which point it dies.
void b1EnterParked(const char* reason) {
  if (g_b1Parked) return;
  Serial.printf("[B1PWR] POWER OFF (%s) — releasing latch.\n", reason);
  if (logging) {
    if (navFile)  { navFile.flush();  navFile.close(); }
    if (imuFile)  { imuFile.flush();  imuFile.close(); }
    if (windFile) { windFile.flush(); windFile.close(); }
    if (presFile) { presFile.flush(); presFile.close(); }
    logging = false;
  }
  char line[96];
  snprintf(line, sizeof(line), "poweroff reason=%s batt=%.2fV %d%%",
           reason, battery.voltage, battery.percent);
  appendBootLog(line);
  Serial.flush();
  g_b1Parked = true;
  g_b1ParkScreenDrawn = false;
  digitalWrite(PWR_HOLD_PIN, LOW);
}

// Non-blocking parked state — run every loop() iteration (after the wdt feed)
// once g_b1Parked is set. Holds PWR_HOLD LOW, paints the parked screen once,
// and idles briefly so g_loopIter keeps advancing (watchdogs stay fed, no
// auto-restart). The unit powers down for real when lifted off the pad.
void b1ParkedLoop() {
  digitalWrite(PWR_HOLD_PIN, LOW);
  if (oledOK && !g_b1ParkScreenDrawn) {
    tft.fillScreen(TFT_BLACK);
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    tft.setTextDatum(MC_DATUM);
    tft.drawString("POWERED OFF", SCREEN_WIDTH / 2, SCREEN_HEIGHT / 2 - 20, 4);
    tft.drawString("lift off pad to finish", SCREEN_WIDTH / 2, SCREEN_HEIGHT / 2 + 20, 2);
    tft.setTextDatum(TL_DATUM);
    g_b1ParkScreenDrawn = true;
  }
  delay(20);
}

// Power-management tick — evaluates the auto power-off triggers once per loop()
// in the normal (non-parked) path. Deliberate off is the `poweroff` serial/
// telnet command; there is intentionally NO lift-and-replace gesture (a coil-
// alignment nudge on the pad would false-trigger it).
void b1PowerTick() {
  unsigned long now = millis();
  g_b1QiPresent = b1ReadQiPresent();

  // On the pad while recording = the sail is over and you've docked it. Stop
  // the session (same as `stoprec`) so the stationary-upload path can ship it
  // and the unit charges. Auto-start is speed-gated, so a stationary on-pad
  // unit won't re-start. This is what makes "set it on the pad" mean
  // stop+upload+charge for the sealed, button-less B unit.
  if (g_b1QiPresent && logging) {
    Serial.println("[B1PWR] on pad while recording — stopping session for upload.");
    if (xSemaphoreTake(sdMutex, pdMS_TO_TICKS(1000))) {
      if (navFile)  { navFile.flush();  navFile.close(); }
      if (imuFile)  { imuFile.flush();  imuFile.close(); }
      if (windFile) { windFile.flush(); windFile.close(); }
      if (presFile) { presFile.flush(); presFile.close(); }
      xSemaphoreGive(sdMutex);
    }
    logging = false;
    recState = REC_IDLE;
  }

  // Trigger 1 — low battery while OFF the pad, sustained (ride out the sag from
  // a GNSS fix burst / TFT redraw). On the pad we're charging: cutoff ignored.
  bool battKnown = battery.valid && battery.voltage > 0.5f;
  if (!g_b1QiPresent && battKnown && battery.voltage < B1_BATT_CUTOFF_V) {
    if (g_b1LowBattSinceMs == 0) g_b1LowBattSinceMs = now;
    else if (now - g_b1LowBattSinceMs >= B1_BATT_CUTOFF_MS) { b1EnterParked("low-batt"); return; }
  } else {
    g_b1LowBattSinceMs = 0;
  }

  // Trigger 3 — store-and-forget: charged + nothing left to upload + idle,
  // sitting on the pad continuously for 30 min. Charge-complete itself NEVER
  // drops PWR_HOLD (a charged unit must still race when lifted — §6 foot-gun);
  // only this compound + timer does.
  bool idleReady = g_b1QiPresent && battKnown && battery.voltage >= B1_CHARGED_V &&
                   pendingUploads == 0 && !uploading && !triggerUpload && !logging;
  if (idleReady) {
    if (g_b1IdleSinceMs == 0) g_b1IdleSinceMs = now;
    else if (now - g_b1IdleSinceMs >= B1_IDLE_OFF_MS) { b1EnterParked("idle-on-pad"); return; }
  } else {
    g_b1IdleSinceMs = 0;
  }
}

// On-pad charging screen — paint-once + update-on-change (like the OCS screen)
// to avoid per-tick flicker. Shows big battery %, and "CHARGED / LIFT to RACE"
// once the pack plateaus.
void drawB1ChargingScreen() {
  if (!oledOK) return;
  static int  prevPct = -1;
  static bool prevFull = false;
  bool full = battery.voltage >= B1_CHARGED_V;
  if (!g_b1ChargeShown) {
    tft.fillScreen(TFT_BLACK);
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    tft.setTextDatum(MC_DATUM);
    tft.drawString("ON CHARGE", SCREEN_WIDTH / 2, 70, 4);
    tft.setTextDatum(TL_DATUM);
    g_b1ChargeShown = true;
    prevPct = -1;
    prevFull = !full;
  }
  if (battery.percent != prevPct) {
    char buf[16];
    snprintf(buf, sizeof(buf), "%d%%", battery.percent);
    tft.fillRect(0, SCREEN_HEIGHT / 2 - 45, SCREEN_WIDTH, 95, TFT_BLACK);
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    tft.setTextDatum(MC_DATUM);
    tft.setTextSize(2);  // font 4 x2 (~52px) — full charset, so '%' renders (fonts 6/7/8 are numeric-only)
    tft.drawString(buf, SCREEN_WIDTH / 2, SCREEN_HEIGHT / 2, 4);
    tft.setTextSize(1);
    tft.setTextDatum(TL_DATUM);
    prevPct = battery.percent;
  }
  if (full != prevFull) {
    tft.fillRect(0, SCREEN_HEIGHT - 80, SCREEN_WIDTH, 60, TFT_BLACK);
    tft.setTextDatum(MC_DATUM);
    if (full) {
      tft.setTextColor(TFT_GREEN, TFT_BLACK);
      tft.drawString("CHARGED", SCREEN_WIDTH / 2, SCREEN_HEIGHT - 58, 4);
      tft.setTextColor(TFT_WHITE, TFT_BLACK);
      tft.drawString("LIFT to RACE", SCREEN_WIDTH / 2, SCREEN_HEIGHT - 28, 2);
    } else {
      tft.setTextColor(TFT_WHITE, TFT_BLACK);
      tft.drawString("charging...", SCREEN_WIDTH / 2, SCREEN_HEIGHT - 48, 2);
    }
    tft.setTextDatum(TL_DATUM);
    prevFull = full;
  }
}
#endif // BUILD_B1

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
    else if (k == "rtk_enabled")       config.rtk_enabled = (v == "1" || v.equalsIgnoreCase("true"));
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
      telnetClient.printf("  SailFrames Edge %s\n", FW_VERSION);
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
    navFile.println("ms,utc,lat,lon,alt,sog,cog,sat,hdop,fix,gps_date,hacc");
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
  // hacc = horizontal 1-sigma (m); GST (LG290P) or PQTMEPE (LC29HEA); 0 = none.
  float hacc = gps.hacc_m;
  navFile.printf("%lu,%s,%.10f,%.10f,%.3f,%.3f,%.2f,%d,%.2f,%d,%s,%.3f\n",
    e, gps.utc_time, gps.lat, gps.lon, gps.alt,
    gps.speed_kts, gps.course, gps.satellites, gps.hdop, gps.fix_quality, gps.date, hacc);
  totalBytes += 98;
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

// ---- D2 layout geometry ----------------------------------------------------
// E (3.5" 320x480): the original constants — the #else values below are byte-
// identical to the literals this layout shipped with, so the E build is
// unchanged. B1 (2.8" 240x320): the big fonts shrink to Font 8 x1 (75px) and
// everything repacks into 320 px tall / 240 px wide; the HDOP cell is dropped
// from the top bar (no room at 240 px).
#ifdef BUILD_B1
  #define D2_TOPBAR_H      22
  #define D2_TB_TXT_Y       4
  #define D2_TB_REC_X       3
  #define D2_TB_FIX_X      66
  #define D2_TB_SAT_X     108
  #define D2_TB_SATN_X    150
  #define D2_LBL_X          3
  #define D2_LBL_FONT       2
  #define D2_COG_LBL_Y     24
  #define D2_COG_CLR_Y     42
  #define D2_COG_CLR_H     80
  #define D2_COG_VAL_Y     84
  #define D2_DIV_Y        145
  #define D2_SOG_LBL_Y    148
  #define D2_SOG_CLR_Y    166
  #define D2_SOG_CLR_H     80
  #define D2_SOG_VAL_Y    210
  #define D2_BIG_SZ         1
  #define D2_BAR_CLR_Y    270
  #define D2_BAR_CLR_H     50
  #define D2_BAR_FILL_Y   272
  #define D2_BAR_FILL_H    48
  #define D2_ROW1_Y       278
  #define D2_ROW2_Y       300
#else
  #define D2_TOPBAR_H      30
  #define D2_TB_TXT_Y       7
  #define D2_TB_REC_X       5
  #define D2_TB_FIX_X     100
  #define D2_TB_SAT_X     145
  #define D2_TB_SATN_X    180
  #define D2_TB_HDOP_X    210
  #define D2_TB_HDOPV_X   250
  #define D2_LBL_X          5
  #define D2_LBL_FONT       4
  #define D2_COG_LBL_Y     35
  #define D2_COG_CLR_Y     60
  #define D2_COG_CLR_H    155
  #define D2_COG_VAL_Y    130
  #define D2_DIV_Y        220
  #define D2_SOG_LBL_Y    225
  #define D2_SOG_CLR_Y    250
  #define D2_SOG_CLR_H    155
  #define D2_SOG_VAL_Y    320
  #define D2_BIG_SZ         2
  #define D2_BAR_CLR_Y    430
  #define D2_BAR_CLR_H     50
  #define D2_BAR_FILL_Y   440
  #define D2_BAR_FILL_H    40
  #define D2_ROW1_Y       440
  #define D2_ROW2_Y       456
#endif

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
    tft.fillRect(0, 0, SCREEN_WIDTH, D2_TOPBAR_H, TFT_BLACK);
    tft.fillRect(0, D2_BAR_FILL_Y, SCREEN_WIDTH, D2_BAR_FILL_H, TFT_BLACK);

    // Divider between COG and SOG
    uint16_t lineColor = tft.color565(180, 180, 180);
    tft.drawFastHLine(0, D2_DIV_Y, SCREEN_WIDTH, lineColor);

    // Labels for COG and SOG - LARGE (Font 4 = 26px on E, Font 2 on B1)
    uint16_t labelColor = tft.color565(100, 100, 100);
    tft.setTextColor(labelColor, COLOR_BG);
    tft.setTextDatum(TL_DATUM);
    tft.drawString("COG", D2_LBL_X, D2_COG_LBL_Y, D2_LBL_FONT);
    tft.drawString("SOG", D2_LBL_X, D2_SOG_LBL_Y, D2_LBL_FONT);

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
    tft.fillRect(0, 0, SCREEN_WIDTH, D2_TOPBAR_H, TFT_BLACK);

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
    tft.drawString(recStr, D2_TB_REC_X, D2_TB_TXT_Y, 2);

    // Fix type (0=none, 1=GPS, 2=DGPS/SBAS, 4=RTK fix, 5=RTK float)
    const char* fixStr;
    switch (gps.fix_quality) {
      case 1: fixStr = "GPS"; break;
      case 2: fixStr = "SBAS"; break;
      case 4: fixStr = "RTK"; break;
      case 5: fixStr = "FLT"; break;
      default: fixStr = "---"; break;
    }
    tft.drawString(fixStr, D2_TB_FIX_X, D2_TB_TXT_Y, 2);

    // SAT count
    tft.drawString("SAT", D2_TB_SAT_X, D2_TB_TXT_Y, 2);
    snprintf(buf, sizeof(buf), "%2d", dispSats);
    tft.drawString(buf, D2_TB_SATN_X, D2_TB_TXT_Y, 2);

#ifndef BUILD_B1
    // HDOP (dropped on B1 — no room at 240 px wide)
    tft.drawString("HDOP", D2_TB_HDOP_X, D2_TB_TXT_Y, 2);
    snprintf(buf, sizeof(buf), "%.1f", gps.hdop);
    tft.drawString(buf, D2_TB_HDOPV_X, D2_TB_TXT_Y, 2);
#endif
    // WiFi status removed from top bar - shown on bottom bar instead
  }

  // COG - Font 8 x2 = 150px
  // COG area: 30-220 (190px), center at 130 (moved down to not overlap label)
  if (abs(gps.course - prevCOG) > 0.5) {
    prevCOG = gps.course;
    tft.fillRect(0, D2_COG_CLR_Y, SCREEN_WIDTH, D2_COG_CLR_H, COLOR_BG);
    tft.setTextColor(TFT_BLACK, COLOR_BG);
    tft.setTextDatum(MC_DATUM);
    tft.setTextSize(D2_BIG_SZ);
    snprintf(buf, sizeof(buf), "%03d", (int)gps.course);
    tft.drawString(buf, SCREEN_WIDTH/2, D2_COG_VAL_Y, 8);
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
    tft.fillRect(0, D2_SOG_CLR_Y, SCREEN_WIDTH, D2_SOG_CLR_H, COLOR_BG);
    tft.setTextColor(TFT_BLACK, COLOR_BG);
    tft.setTextDatum(MC_DATUM);
    tft.setTextSize(D2_BIG_SZ);
    tft.drawString(newSogBuf, SCREEN_WIDTH/2, D2_SOG_VAL_Y, 8);
    tft.setTextSize(1);
  }

#ifdef BUILD_B1
  // B1: live GNSS quality in the COG/SOG label rows (top-right). HDOP next to
  // COG; horizontal 1-sigma accuracy (gps.hacc_m from $PQTMEPE) next to SOG.
  // These rows sit above the big-number clear regions, so value redraws don't
  // wipe them. Updated only on change.
  static float prevHDOP2 = -999, prevHacc = -999;
  if (fabsf(gps.hdop - prevHDOP2) > 0.1f) {
    prevHDOP2 = gps.hdop;
    tft.fillRect(80, D2_COG_LBL_Y, SCREEN_WIDTH - 80, 18, COLOR_BG);
    tft.setTextColor(tft.color565(100, 100, 100), COLOR_BG);
    tft.setTextDatum(TR_DATUM);
    char hb[16]; snprintf(hb, sizeof(hb), "HDOP %.1f", gps.hdop);
    tft.drawString(hb, SCREEN_WIDTH - 3, D2_COG_LBL_Y, 2);
    tft.setTextDatum(TL_DATUM);
  }
  if (fabsf(gps.hacc_m - prevHacc) > 0.005f) {
    prevHacc = gps.hacc_m;
    tft.fillRect(80, D2_SOG_LBL_Y, SCREEN_WIDTH - 80, 18, COLOR_BG);
    tft.setTextColor(tft.color565(100, 100, 100), COLOR_BG);
    tft.setTextDatum(TR_DATUM);
    char ab[16];
    if      (gps.hacc_m <= 0.0f) snprintf(ab, sizeof(ab), "ACC --");
    else if (gps.hacc_m < 1.0f)  snprintf(ab, sizeof(ab), "ACC %dcm", (int)lroundf(gps.hacc_m * 100));
    else                         snprintf(ab, sizeof(ab), "ACC %.1fm", gps.hacc_m);
    tft.drawString(ab, SCREEN_WIDTH - 3, D2_SOG_LBL_Y, 2);
    tft.setTextDatum(TL_DATUM);
  }
#endif

  // Bottom status bar - two rows on BLACK background
  static bool prevWindConnected = true;
  static bool prevWifiConnected = false;
  static unsigned long lastStatusUpdate = 0;

  static int prevPendingN = -1;
  static float prevLineDist = -99999;
  static bool prevArmed = false;
  bool statusChanged = (wind.connected != prevWindConnected) ||
                       (wifiConnected != prevWifiConnected) ||
                       (abs(imu.heel - prevHeel) > 0.5) ||
                       (abs(imu.pitch - prevPitch) > 0.5) ||
                       (abs(aws - prevAWS2) > 0.3) ||
                       (abs(awa - prevAWA2) > 1) ||
                       (battery.percent != prevBattery) ||
                       (pendingUploads != prevPendingN) ||
                       (g_ocs.armed != prevArmed) ||
                       (g_ocs.armed && abs(g_ocs.distance_to_line_m - prevLineDist) > 0.1) ||
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
    prevLineDist = g_ocs.distance_to_line_m;
    prevArmed = g_ocs.armed;

    // BOTTOM BAR: Two rows - WHITE on BLACK
    // Clear entire bottom bar first
    tft.fillRect(0, D2_BAR_CLR_Y, SCREEN_WIDTH, D2_BAR_CLR_H, TFT_BLACK);
    tft.setTextColor(TFT_WHITE, TFT_BLACK);

    // Row 1 (y=440): Heel + Pitch always; AWS + AWA appended when wind connected.
    // When a race is armed, append signed distance-to-line in small print
    // ("L+5.2m" / "L-4.3m") so the crew sees their line position at a glance.
    // Single row keeps heel/pitch visible even with the wind sensor active.
    char row1[64];
    char lineTag[16] = "";
    if (g_ocs.armed) snprintf(lineTag, sizeof(lineTag), " L%+.1fm", g_ocs.distance_to_line_m);
    if (config.wind_enabled && wind.connected) {
      snprintf(row1, sizeof(row1), "H%+.0f P%+.0f AWS%.1f%s",
               imu.heel, imu.pitch, aws, lineTag);
    } else {
      snprintf(row1, sizeof(row1), "H %+.0f  P %+.0f%s", imu.heel, imu.pitch, lineTag);
    }
    tft.setTextDatum(MC_DATUM);
    tft.drawString(row1, SCREEN_WIDTH/2, D2_ROW1_Y, 2);

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
    tft.drawString(left, D2_LBL_X, D2_ROW2_Y, 2);

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
    tft.drawString(right, SCREEN_WIDTH - 5, D2_ROW2_Y, 2);
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

#ifdef BUILD_B1
  // On the Qi pad and not recording: dedicated charging screen (takes priority
  // over the nav/RC views — on the pad you're charging regardless of role).
  if (g_b1QiPresent && !logging && !g_b1Parked) {
    drawB1ChargingScreen();
    g_b1ChargeScreenActive = true;
    return;
  }
  if (g_b1ChargeScreenActive) {     // just lifted off the pad — force a nav repaint
    g_b1ChargeScreenActive = false;
    g_b1ChargeShown = false;
    d2LayoutDrawn = false;
    d3LayoutDrawn = false;
    lastFullRedraw = 0;
  }
#endif

  // RC fleet panel takes over the screen while this unit is the armed Race
  // Committee — a live, colour-coded table of every peer's distance-to-line
  // and OCS state. (Checked before the boat-local OCS alarm: a stationary
  // committee boat "over" its own line isn't meaningful; it monitors the fleet.)
  if (g_role == ROLE_RC_SIGNAL && g_ocs.armed) {
    g_rcPrePanelShown = false;   // force pre-race panel repaint when we later disarm
    drawRcFleetPanel();
    return;
  }
  // RC, not armed: pre-race fleet-connection roster instead of the nav (COG/SOG)
  // display. The committee boat confirms every boat is connected + fixed here.
  if (g_role == ROLE_RC_SIGNAL) {
    g_rcPanelShown = false;      // OCS panel not shown; force its repaint on next arm
    drawRcPreRacePanel();
    return;
  }
  if (g_rcPanelShown) {  // just left the RC panel — repaint the nav display
    g_rcPanelShown = false;
    d2LayoutDrawn = false;
    d3LayoutDrawn = false;
    lastFullRedraw = 0;
  }

  // OCS alarm takes over the whole screen while this boat is over the line —
  // whether it computed that itself (ocsTick) or was forced by an RC
  // individual recall (ocsForceOver). When you're OCS the only thing that
  // matters is "you're over, come back," so the nav display is hidden and the
  // distance shows how far over you are (how far to dip back). Painted ONCE on
  // entry; only the distance value region is redrawn, on change — no per-tick
  // fillScreen (that would flicker). Big text uses font 4 scaled (fonts 6/7/8
  // are digits-only, can't render "OCS"/"RETURN").
  static bool ocsAlarmShown = false;
  static bool ocsLastInv = false;
  static int  ocsAlarmPrevDm = -1000000;  // last drawn distance, decimetres
  if (g_ocs.armed && g_ocs.over_line) {
    unsigned long now = millis();
    // Blink at ~2 Hz: invert the whole screen every 250 ms — alternate
    // white-on-black and black-on-white (black background, no red). The
    // full-screen repaint only happens on a blink-phase flip (~4×/s) or a
    // distance change, so it's the intended blink, not runaway flicker.
    bool inv = ((now / 250) % 2) == 0;
    uint16_t bg = inv ? TFT_BLACK : TFT_WHITE;
    uint16_t fg = inv ? TFT_WHITE : TFT_BLACK;
    int dm = (int)lroundf(g_ocs.distance_to_line_m * 10.0f);
    bool full = !ocsAlarmShown || (inv != ocsLastInv);  // first entry or blink flip
    if (full || dm != ocsAlarmPrevDm) {
      ocsAlarmShown = true;
      ocsLastInv = inv;
      ocsAlarmPrevDm = dm;
      char buf[24];
      snprintf(buf, sizeof(buf), "%+.1f m", g_ocs.distance_to_line_m);
      tft.setTextColor(fg, bg);
      tft.setTextDatum(MC_DATUM);
      if (full) {
        tft.fillScreen(bg);
        tft.setTextSize(5);
        tft.drawString("OCS", SCREEN_WIDTH/2, 110, 4);
        tft.setTextSize(3);
        tft.drawString("RETURN", SCREEN_WIDTH/2, 245, 4);
        tft.drawString(buf, SCREEN_WIDTH/2, 360, 4);
        tft.setTextSize(1);
      } else {
        // distance changed within the same blink phase — redraw just it
        tft.fillRect(0, 315, SCREEN_WIDTH, 100, bg);
        tft.setTextSize(3);
        tft.drawString(buf, SCREEN_WIDTH/2, 360, 4);
        tft.setTextSize(1);
      }
    }
    return;
  }
  // Just left the alarm — force the nav display to fully repaint over the red.
  if (ocsAlarmShown) {
    ocsAlarmShown = false;
    d2LayoutDrawn = false;
    d3LayoutDrawn = false;
    lastFullRedraw = 0;  // D1's force-redraw lever
  }

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
  g_uploadSection = "uploadFile.open";
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

  // Determine content type
  String contentType = "application/octet-stream";
  if (filename.endsWith(".csv")) {
    contentType = "text/csv";
  } else if (filename.endsWith(".rtcm3")) {
    contentType = "application/octet-stream";
  }

  // Manual chunked PUT (replaces HTTPClient::sendRequest). HTTPClient
  // blocks the entire body transmission with no esp_task_wdt_reset()
  // calls inside. For multi-MB files at typical link speeds (50-100
  // KB/s) a single PUT can take 3+ minutes; the 300 s task wdt fires
  // mid-upload (2026-05-25 event: 19.8 MB IMU CSV repeatedly tripped
  // wdt at ~300 s into the PUT, even though bytes were flowing).
  // setTimeout on HTTPClient is per-read, not total — useless against
  // genuinely-slow-but-progressing transfers.
  //
  // Doing the write loop ourselves lets us:
  //   - feed the task wdt every chunk (~4 KB)
  //   - run a no-progress stall watchdog independent of total elapsed
  //   - enforce a hard ceiling (10 min/file) so a truly stuck PUT
  //     bails without burning the task wdt
  String s3Host = String(config.s3_bucket) + ".s3." + String(config.s3_region) + ".amazonaws.com";
  String s3Path = "/raw/" + String(config.boat_id) + "/" + dateFolder + "/" + filename;

  WiFiClient client;
  g_uploadSection = "uploadFile.connect";
  if (!client.connect(s3Host.c_str(), 80, 10000)) {
    Serial.printf("[UPLOAD] TCP connect failed: %s\n", s3Host.c_str());
    file.close();
    uploadFailed++;
    return false;
  }

  // Headers
  g_uploadSection = "uploadFile.headers";
  client.printf("PUT %s HTTP/1.1\r\n", s3Path.c_str());
  client.printf("Host: %s\r\n", s3Host.c_str());
  client.printf("Content-Type: %s\r\n", contentType.c_str());
  client.printf("Content-Length: %u\r\n", (unsigned)fileSize);
  client.printf("x-amz-meta-boat-id: %s\r\n", config.boat_id);
  client.printf("x-amz-meta-original-path: %s\r\n", filepath);
  client.print("Connection: close\r\n\r\n");

  yield();
  esp_task_wdt_reset();

  // Body — chunked send with per-chunk wdt feed.
  // Static buf keeps 4 KB off the upload task's stack (only ~9 KB).
  g_uploadSection = "uploadFile.body";
  const size_t CHUNK = 4096;
  static uint8_t buf[CHUNK];
  unsigned long startTime = millis();
  unsigned long lastProgress = startTime;
  size_t sent = 0;
  bool aborted = false;
  const char* abortReason = "";

  while (sent < fileSize) {
    esp_task_wdt_reset();
    yield();

    unsigned long now = millis();
    if (now - lastProgress > 30000) {
      aborted = true; abortReason = "STALL_30S"; break;
    }
    if (now - startTime > 600000) {
      aborted = true; abortReason = "CEILING_10MIN"; break;
    }
    if (!client.connected()) {
      aborted = true; abortReason = "PEER_CLOSED"; break;
    }

    size_t want = (fileSize - sent < CHUNK) ? (fileSize - sent) : CHUNK;
    int r = file.read(buf, want);
    if (r <= 0) {
      aborted = true; abortReason = "SD_READ_FAILED"; break;
    }

    size_t w = client.write(buf, (size_t)r);
    if (w == 0) {
      aborted = true; abortReason = "SOCKET_WRITE_0"; break;
    }
    sent += w;
    lastProgress = millis();
  }

  unsigned long elapsed = (millis() - startTime) / 1000;
  file.close();

  if (aborted) {
    Serial.printf("[UPLOAD] Aborted: %s (%s) at %u/%u bytes after %lus\n",
                  filepath, abortReason, (unsigned)sent, (unsigned)fileSize, elapsed);
    client.stop();
    uploadFailed++;
    return false;
  }

  esp_task_wdt_reset();

  // Response — wait up to 60 s for S3 to start replying, then parse
  // status line and drain. 60 s is generous because S3 can take a
  // few seconds to acknowledge a large PUT.
  g_uploadSection = "uploadFile.response";
  int httpCode = -1;
  String response;
  unsigned long respDeadline = millis() + 60000;
  while (client.connected() && !client.available() && millis() < respDeadline) {
    esp_task_wdt_reset();
    yield();
    delay(10);
  }

  if (client.available()) {
    String statusLine = client.readStringUntil('\n');
    int sp1 = statusLine.indexOf(' ');
    int sp2 = statusLine.indexOf(' ', sp1 + 1);
    if (sp1 > 0 && sp2 > sp1) {
      httpCode = statusLine.substring(sp1 + 1, sp2).toInt();
    }
    // Drain remainder so connection closes cleanly + body is logged on errors.
    unsigned long drainDeadline = millis() + 5000;
    while (client.connected() && millis() < drainDeadline) {
      if (client.available()) {
        char c = client.read();
        if (response.length() < 500) response += c;
      } else {
        esp_task_wdt_reset();
        yield();
        delay(1);
      }
    }
  } else {
    Serial.println("[UPLOAD] No response from S3 within 60 s after upload");
  }

  client.stop();
  yield();
  delay(50);

  if (httpCode == 200 || httpCode == 201 || httpCode == 204) {
    Serial.printf("[UPLOAD] Success: %s (HTTP %d, %lus, %u bytes)\n",
                  filepath, httpCode, elapsed, (unsigned)fileSize);
    uploadSuccess++;
    return true;
  } else {
    const char* errMsg =
      (httpCode == -1)  ? "NO_RESPONSE" :
      (httpCode == 403) ? "FORBIDDEN (check bucket policy)" :
      (httpCode == 400) ? "BAD_REQUEST" : "";
    Serial.printf("[UPLOAD] Failed: %s (HTTP %d %s, %lus)\n",
                  filepath, httpCode, errMsg, elapsed);
    if (response.length() > 0 && response.length() < 500) {
      Serial.printf("[UPLOAD] Response: %s\n", response.c_str());
    }
    uploadFailed++;
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
  g_uploadSection = "wifi.scan";
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
    g_uploadSection = "wifi.associate";

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
    WiFi.setSleep(false);    // keep RX live for the always-on ESP-NOW mesh once the upload window ends
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

  // OTA runs on ANY connected WiFi (SSID gate removed 2026-06-06 per request) —
  // the fleet should pick up firmware wherever it gets online (yacht club,
  // phone hotspot, home). Note: the ~1.5 MB pull will use hotspot data, and a
  // boat associating with an unfamiliar AP may update there. The stall + 180 s
  // deadline watchdogs (gotcha #22) still bound a bad download.
  Serial.printf("[OTA] on %s — proceeding (any-WiFi OTA)\n", connectedSSID);

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

// Scan /sf/ for the lexically largest folder name. The session folder
// naming convention is YYYYMMDD_HHMMSS so lex-max == chronological-max.
// Falls back to session_NNN fallback names if no GPS-timed folders
// exist. Returns "" if /sf/ is empty.
static void findLatestSessionFolder(char* out, size_t outlen) {
  out[0] = '\0';
  if (outlen == 0) return;
  File root = SD.open("/sf");
  if (!root || !root.isDirectory()) {
    if (root) root.close();
    return;
  }
  File f = root.openNextFile();
  while (f) {
    if (f.isDirectory()) {
      const char* name = f.name();
      // Skip the SD root walker's leading slash if present.
      const char* base = strrchr(name, '/');
      base = base ? base + 1 : name;
      if (base[0] != '\0' && base[0] != '.' && strcmp(base, out) > 0) {
        strncpy(out, base, outlen - 1);
        out[outlen - 1] = '\0';
      }
    }
    f = root.openNextFile();
    yield();
  }
  root.close();
}

bool uploadStatusSnapshot() {
  if (!wifiConnected) return false;

  // Build JSON in a stack-local buffer. Keep under 1 KB.
  char body[1024];
  char ts[24] = "";
  formatGpsIso(ts, sizeof(ts));   // empty string if GPS time not yet valid

  const char* fixStr =
    gps.fix_quality == 2 ? "dgps" :
    gps.fix_quality == 1 ? "gps"  :
    "none";

  // SD free space — totalBytes/usedBytes return uint64_t, convert to MB.
  uint64_t sdTotal = SD.totalBytes();
  uint64_t sdUsed  = SD.usedBytes();
  uint32_t sdFreeMb = (sdTotal > sdUsed) ? (uint32_t)((sdTotal - sdUsed) / (1024ULL * 1024ULL)) : 0;

  // Latest /sf/<session>/ folder name. Empty if no sessions yet.
  char lastSail[32] = "";
  findLatestSessionFolder(lastSail, sizeof(lastSail));

  int written = snprintf(body, sizeof(body),
    "{"
    "\"version\":\"%s\","
    "\"boat_id\":\"%s\","
    "\"ts_iso\":\"%s\","
    "\"gps_fix\":\"%s\","
    "\"sats\":%d,"
    "\"hdop\":%.1f,"
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
    "\"sd_ok\":%s,"
    "\"pending_uploads\":%d,"
    "\"sd_free_mb\":%lu,"
    "\"last_sail_folder\":\"%s\""
    "}",
    FW_VERSION,
    config.boat_id,
    ts,
    fixStr,
    gps.satellites,
    gps.hdop,
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
    sdOK ? "true" : "false",
    pendingUploads,
    (unsigned long)sdFreeMb,
    lastSail);
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
// /boot.log → S3 upload (post-status, once per boot)
// ============================================================
// The diag/wdt boot log lives at /boot.log (SD root), so the
// recursive /sf walker in uploadDirectory never picks it up.
// This function PUTs the entire current /boot.log to
//   raw/<boat>/_boot.log
// once per boot, after the status snapshot. The file is
// append-only on the boat side — every upload contains the
// complete history, so post-flash boats give us their whole
// life history (back to 2026-05-05 when the file was first
// written) on first upload, then incremental tails as new
// alive/boot lines accrue. Used by the web battery dashboard.
static bool g_bootLogUploadedThisBoot = false;

bool uploadBootLogSnapshot() {
  if (!wifiConnected) return false;
  g_uploadSection = "bootlog.open";

  File file = SD.open("/boot.log", FILE_READ);
  if (!file) {
    Serial.println("[BOOTLOG] /boot.log not on SD — nothing to upload");
    return true;  // not an error per se
  }
  size_t fileSize = file.size();
  if (fileSize == 0) {
    Serial.println("[BOOTLOG] /boot.log empty");
    file.close();
    return true;
  }

  Serial.printf("[BOOTLOG] Uploading /boot.log (%u bytes, heap %u, rssi %d)\n",
                (unsigned)fileSize, ESP.getFreeHeap(), WiFi.RSSI());

  String s3Host = String(config.s3_bucket) + ".s3." + String(config.s3_region) + ".amazonaws.com";
  String s3Path = "/raw/" + String(config.boat_id) + "/_boot.log";

  WiFiClient client;
  g_uploadSection = "bootlog.connect";
  if (!client.connect(s3Host.c_str(), 80, 10000)) {
    Serial.printf("[BOOTLOG] TCP connect failed: %s\n", s3Host.c_str());
    file.close();
    return false;
  }

  g_uploadSection = "bootlog.headers";
  client.printf("PUT %s HTTP/1.1\r\n", s3Path.c_str());
  client.printf("Host: %s\r\n", s3Host.c_str());
  client.print("Content-Type: text/plain\r\n");
  client.printf("Content-Length: %u\r\n", (unsigned)fileSize);
  client.print("Connection: close\r\n\r\n");

  yield();
  esp_task_wdt_reset();

  // Body — same chunked-PUT pattern as uploadFile (per-chunk wdt feed,
  // stall watchdog, hard ceiling). boot.log is small (~100 KB after
  // ~3 weeks) so the 2-min ceiling is plenty even on weak signal.
  g_uploadSection = "bootlog.body";
  const size_t CHUNK = 4096;
  static uint8_t buf[CHUNK];
  unsigned long startTime = millis();
  unsigned long lastProgress = startTime;
  size_t sent = 0;
  bool aborted = false;
  const char* abortReason = "";

  while (sent < fileSize) {
    esp_task_wdt_reset();
    yield();
    unsigned long now = millis();
    if (now - lastProgress > 30000) { aborted = true; abortReason = "STALL_30S"; break; }
    if (now - startTime > 120000)   { aborted = true; abortReason = "CEILING_2MIN"; break; }
    if (!client.connected())        { aborted = true; abortReason = "PEER_CLOSED"; break; }

    size_t want = (fileSize - sent < CHUNK) ? (fileSize - sent) : CHUNK;
    int r = file.read(buf, want);
    if (r <= 0) { aborted = true; abortReason = "SD_READ_FAILED"; break; }
    size_t w = client.write(buf, (size_t)r);
    if (w == 0) { aborted = true; abortReason = "SOCKET_WRITE_0"; break; }
    sent += w;
    lastProgress = millis();
  }

  unsigned long elapsed = (millis() - startTime) / 1000;
  file.close();

  if (aborted) {
    Serial.printf("[BOOTLOG] Aborted: %s at %u/%u bytes after %lus\n",
                  abortReason, (unsigned)sent, (unsigned)fileSize, elapsed);
    client.stop();
    return false;
  }

  g_uploadSection = "bootlog.response";
  int httpCode = -1;
  unsigned long respDeadline = millis() + 30000;
  while (client.connected() && !client.available() && millis() < respDeadline) {
    esp_task_wdt_reset();
    yield();
    delay(10);
  }
  if (client.available()) {
    String statusLine = client.readStringUntil('\n');
    int sp1 = statusLine.indexOf(' ');
    int sp2 = statusLine.indexOf(' ', sp1 + 1);
    if (sp1 > 0 && sp2 > sp1) {
      httpCode = statusLine.substring(sp1 + 1, sp2).toInt();
    }
    unsigned long drainDeadline = millis() + 3000;
    while (client.connected() && millis() < drainDeadline) {
      if (client.available()) client.read();
      else { esp_task_wdt_reset(); yield(); delay(1); }
    }
  }
  client.stop();

  if (httpCode >= 200 && httpCode < 300) {
    Serial.printf("[BOOTLOG] Uploaded OK (%u bytes, %lus, HTTP %d)\n",
                  (unsigned)fileSize, elapsed, httpCode);
    return true;
  }
  Serial.printf("[BOOTLOG] Failed: HTTP %d (%lus)\n", httpCode, elapsed);
  return false;
}

// ============================================================
// v2.0.0 Stage 3.6 — cloud config sync + apply
// ============================================================
// Cloud-config flow mirrors firmware OTA:
//   /config/<boat_id>/latest.json   = { version, url, sha256, applied_at }
//   /config/<boat_id>/vN.txt        = raw text body, same key=value
//                                     format as /sf/config.txt
//
// Manifest-points-at-text was chosen over JSON-with-embedded-body
// because (a) the simple otaExtractJsonString helper does not
// unescape \n, and (b) sha256 verification mirrors the OTA path
// for free.
//
// Apply path is one-shot per boot (g_configSyncCheckedThisBoot)
// and gated by:
//   - g_ocs.armed       — don't disturb a race-start window
//   - logging           — don't reboot mid-recording
//
// Allow-list — keys that cloud config may override on local
// /sf/config.txt. Anything outside the list in the cloud body is
// silently dropped. Identity & connectivity keys (boat_id, wifi*,
// wind_mac, s3_*) are deliberately excluded — a bad push must not
// be able to lock a boat off the network or change its sender_id
// hash (which would break mesh peers + class registry mappings).
static const char* CLOUD_CONFIG_ALLOW_KEYS[] = {
    "wind_enabled",
    "wind_offset",
    "start_speed_knots",
    "stop_speed_knots",
    "start_delay_sec",
    "stop_delay_sec",
    "unit_role",
    "rtk_enabled",   // RTK operating mode — settable via cloud so the fleet's
                     // base/rover assignment is OTA-managed (not identity/conn,
                     // so consistent with the allow-list philosophy, gotcha #27).
                     // Applying it reconfigures the GNSS + reboots (gated on
                     // !armed && !logging by the cloud-apply path).
    nullptr
};

static bool isAllowedConfigKey(const String& key) {
    for (int i = 0; CLOUD_CONFIG_ALLOW_KEYS[i] != nullptr; i++) {
        if (key.equalsIgnoreCase(CLOUD_CONFIG_ALLOW_KEYS[i])) return true;
    }
    return false;
}

static bool g_configSyncCheckedThisBoot = false;
static int  g_cloud_config_version = -1;   // -1 = unknown / not fetched
static bool g_configRebootPending = false;
static uint32_t g_configRebootAtMs = 0;

// Compute sha256 of an Arduino String, return lowercase hex.
static String sha256OfString(const String& s) {
  uint8_t digest[32];
  mbedtls_sha256_context ctx;
  mbedtls_sha256_init(&ctx);
  mbedtls_sha256_starts(&ctx, 0);
  mbedtls_sha256_update(&ctx, (const uint8_t*)s.c_str(), s.length());
  mbedtls_sha256_finish(&ctx, digest);
  mbedtls_sha256_free(&ctx);
  return otaHexDigest(digest, 32);
}

// Rewrite /sf/config.txt with cloud key=value overrides merged in.
// Strategy: load current file into memory, replace allow-listed
// keys' lines with new values, append new keys not already present,
// force config_version=<cloudVersion>, then atomic rename through
// .tmp with .prev as one-deep backup.
static bool applyCloudConfigBody(const String& cloudBody, int cloudVersion) {
  // Load current config.txt into memory (line-by-line)
  String existing = "";
  {
    File f = SD.open("/config.txt", FILE_READ);
    if (f) {
      while (f.available()) existing += (char)f.read();
      f.close();
    }
  }
  if (existing.length() == 0) {
    Serial.println("[CFGSYNC] WARNING: local /config.txt empty/missing — bailing out for safety");
    return false;
  }

  // Parse cloud body into (key, val) pairs, allow-listed only
  struct KV { String k; String v; };
  static const int MAX_KV = 16;
  KV kv[MAX_KV];
  int kvCount = 0;
  int dropped = 0;
  int p = 0;
  while (p < (int)cloudBody.length() && kvCount < MAX_KV) {
    int nl = cloudBody.indexOf('\n', p);
    String line = (nl < 0) ? cloudBody.substring(p) : cloudBody.substring(p, nl);
    p = (nl < 0) ? cloudBody.length() : nl + 1;
    line.trim();
    if (line.length() == 0 || line.startsWith("#")) continue;
    int eq = line.indexOf('=');
    if (eq < 0) continue;
    String k = line.substring(0, eq); k.trim();
    String v = line.substring(eq + 1); v.trim();
    if (k.equalsIgnoreCase("config_version")) continue;  // forced from manifest
    if (!isAllowedConfigKey(k)) { dropped++; continue; }
    kv[kvCount].k = k;
    kv[kvCount].v = v;
    kvCount++;
  }
  Serial.printf("[CFGSYNC] parsed cloud body: %d allowed, %d dropped\n", kvCount, dropped);

  // Build merged output: walk existing line-by-line, replace matching keys
  String out = "";
  bool kvUsed[MAX_KV] = {false};
  int existingP = 0;
  bool sawConfigVersion = false;
  while (existingP < (int)existing.length()) {
    int nl = existing.indexOf('\n', existingP);
    String line = (nl < 0) ? existing.substring(existingP) : existing.substring(existingP, nl);
    int origLen = (nl < 0) ? line.length() : nl - existingP + 1;
    existingP += origLen;

    String trimmed = line; trimmed.trim();
    if (trimmed.length() == 0 || trimmed.startsWith("#")) {
      out += line;
      if (nl >= 0) out += "\n";
      continue;
    }
    int eq = trimmed.indexOf('=');
    if (eq < 0) {
      out += line;
      if (nl >= 0) out += "\n";
      continue;
    }
    String k = trimmed.substring(0, eq); k.trim();

    if (k.equalsIgnoreCase("config_version")) {
      out += "config_version=" + String(cloudVersion) + "\n";
      sawConfigVersion = true;
      continue;
    }

    bool replaced = false;
    for (int i = 0; i < kvCount; i++) {
      if (k.equalsIgnoreCase(kv[i].k) && !kvUsed[i]) {
        out += kv[i].k + "=" + kv[i].v + "\n";
        kvUsed[i] = true;
        replaced = true;
        break;
      }
    }
    if (!replaced) {
      out += line;
      if (nl >= 0) out += "\n";
    }
  }
  if (!out.endsWith("\n")) out += "\n";
  // Append any cloud keys not already present
  for (int i = 0; i < kvCount; i++) {
    if (!kvUsed[i]) out += kv[i].k + "=" + kv[i].v + "\n";
  }
  if (!sawConfigVersion) out += "config_version=" + String(cloudVersion) + "\n";

  // Atomic rewrite: tmp → rename. Keep .prev as one-deep backup.
  if (SD.exists("/config.txt.tmp")) SD.remove("/config.txt.tmp");
  File tf = SD.open("/config.txt.tmp", FILE_WRITE);
  if (!tf) {
    Serial.println("[CFGSYNC] cannot open /config.txt.tmp");
    return false;
  }
  size_t wrote = tf.print(out);
  tf.flush();
  tf.close();
  if (wrote != out.length()) {
    Serial.printf("[CFGSYNC] short write: %u of %u\n",
                  (unsigned)wrote, (unsigned)out.length());
    SD.remove("/config.txt.tmp");
    return false;
  }
  // Verify the tmp file read back matches
  {
    File rf = SD.open("/config.txt.tmp", FILE_READ);
    if (!rf || (int)rf.size() != (int)out.length()) {
      Serial.println("[CFGSYNC] tmp read-back size mismatch");
      if (rf) rf.close();
      SD.remove("/config.txt.tmp");
      return false;
    }
    rf.close();
  }
  if (SD.exists("/config.txt.prev")) SD.remove("/config.txt.prev");
  if (!SD.rename("/config.txt", "/config.txt.prev")) {
    Serial.println("[CFGSYNC] rename .txt -> .prev failed");
    SD.remove("/config.txt.tmp");
    return false;
  }
  if (!SD.rename("/config.txt.tmp", "/config.txt")) {
    Serial.println("[CFGSYNC] rename .tmp -> .txt failed — restoring .prev");
    SD.rename("/config.txt.prev", "/config.txt");
    return false;
  }
  Serial.printf("[CFGSYNC] /config.txt rewritten (%u bytes, v%d)\n",
                (unsigned)out.length(), cloudVersion);
  return true;
}

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
  String manifest = http.getString();
  http.end();

  long cloudVersion = otaExtractJsonNumber(manifest, "version");
  String bodyUrl    = otaExtractJsonString(manifest, "url");
  String sha256Hex  = otaExtractJsonString(manifest, "sha256");
  if (cloudVersion < 0) {
    Serial.println("[CFGSYNC] manifest missing 'version' — skipping");
    return false;
  }
  g_cloud_config_version = (int)cloudVersion;
  int localVersion = config.config_version;
  Serial.printf("[CFGSYNC] cloud v%d, local v%d\n",
                g_cloud_config_version, localVersion);

  if (g_cloud_config_version == localVersion) {
    Serial.printf("[CFGSYNC] up to date (v%d)\n", g_cloud_config_version);
    return true;
  }
  if (g_cloud_config_version < localVersion) {
    Serial.printf("[CFGSYNC] cloud older than local (v%d < v%d) — ignoring\n",
                  g_cloud_config_version, localVersion);
    return true;
  }

  // Stage 3.6 safety gates — defer apply, keep flag false so a later
  // post-race boot picks it up. We still mark checkedThisBoot=true
  // via the caller so we don't re-fetch the manifest 10x this boot,
  // but the defer path returns true to indicate "fetch OK, no apply".
  if (g_ocs.armed) {
    Serial.printf("[CFGSYNC] DEFER: OCS armed — won't rewrite config mid-race\n");
    appendBootLog("cfgsync defer=ocs-armed");
    return true;
  }
  if (logging) {
    Serial.printf("[CFGSYNC] DEFER: recording active — won't reboot mid-session\n");
    appendBootLog("cfgsync defer=logging");
    return true;
  }
  if (bodyUrl.length() == 0) {
    Serial.println("[CFGSYNC] manifest missing 'url' — cannot fetch body");
    return false;
  }

  Serial.printf("[CFGSYNC] cloud NEWER (v%d > v%d) — fetching body %s\n",
                g_cloud_config_version, localVersion, bodyUrl.c_str());

  WiFiClient client2;
  HTTPClient http2;
  http2.setConnectTimeout(8000);
  http2.setTimeout(10000);
  http2.setReuse(false);
  http2.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  if (!http2.begin(client2, bodyUrl)) {
    Serial.println("[CFGSYNC] body http.begin failed");
    return false;
  }
  int code2 = http2.GET();
  if (code2 != 200) {
    Serial.printf("[CFGSYNC] body HTTP %d\n", code2);
    http2.end();
    return false;
  }
  String body = http2.getString();
  http2.end();
  if (body.length() == 0 || body.length() > 4096) {
    Serial.printf("[CFGSYNC] body length %u out of bounds\n", (unsigned)body.length());
    return false;
  }

  if (sha256Hex.length() == 64) {
    String got = sha256OfString(body);
    if (!got.equalsIgnoreCase(sha256Hex)) {
      Serial.printf("[CFGSYNC] sha256 mismatch: got %s, want %s — aborting\n",
                    got.c_str(), sha256Hex.c_str());
      appendBootLog("cfgsync abort=sha256-mismatch");
      return false;
    }
    Serial.println("[CFGSYNC] sha256 OK");
  } else {
    Serial.println("[CFGSYNC] manifest has no sha256 — skipping integrity check");
  }

  Serial.printf("[CFGSYNC] cloud body (%u bytes):\n%s\n",
                (unsigned)body.length(), body.c_str());

  if (!applyCloudConfigBody(body, g_cloud_config_version)) {
    Serial.println("[CFGSYNC] apply failed");
    appendBootLog("cfgsync apply=failed");
    return false;
  }
  char line[80];
  snprintf(line, sizeof(line), "cfgsync applied cloud=v%d (was v%d) reboot=3s",
           g_cloud_config_version, localVersion);
  appendBootLog(line);
  Serial.println("[CFGSYNC] Apply OK. Rebooting in 3s.");

  // Schedule reboot — main loop drains diag + flushes any in-flight
  // serial before the actual restart. We don't restart here directly
  // because we're inside the upload task; an immediate esp_restart
  // would race with the diag heartbeat + watchdog deinit.
  g_configRebootPending = true;
  g_configRebootAtMs = millis() + 3000;
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
    gnssConfigure();   // RTK off ⇒ configureLG290P(); on ⇒ base/rover per role+chip
    tprintln("GPS reconfigured");

  } else if (cmd == "rtk") {
    // RTK Phase-2 relay status (bench verification).
    tprintf("rtk_enabled=%d role=%s (%s) hw=%s\n", config.rtk_enabled,
            roleName(g_role), roleIsBase() ? "base/produce" : "rover/consume", hwName(g_hw));
    tprintf("gps fix_quality=%d (4=RTK-FIXED 5=float 2=DGPS 1=GPS) sat=%d hdop=%.1f\n",
            gps.fix_quality, gps.satellites, gps.hdop);
    tprintf("accuracy: h=%.3f m (1sigma; GST=LG290P / PQTMEPE=LC29HEA)%s\n",
            gps.hacc_m, (gps.hacc_m == 0) ? "  (no data yet)" : "");
    if (roleIsBase()) {
      tprintf("base: tx_msg_id=%u (frames fragmented+broadcast 2x)\n", (unsigned)g_rtcmTxMsgId);
    } else {
      tprintf("rover: pkts=%lu complete=%lu crc_fail=%lu dropped=%lu dup=%lu bad=%lu ring=%u\n",
              g_rtcmRx.s_pkts, g_rtcmRx.s_complete, g_rtcmRx.s_crc_fail, g_rtcmRx.s_dropped,
              g_rtcmRx.s_dup, g_rtcmRx.s_bad,
              g_rtcmRing ? (unsigned)xStreamBufferBytesAvailable(g_rtcmRing) : 0);
    }

  } else if (cmd.startsWith("setcfg ")) {
    // Bench helper: append a key=value to /config.txt so config can be set over
    // USB/telnet without pulling the SD. APPEND-only ⇒ never rewrites existing
    // identity/wifi lines (no corruption risk); loadConfig() takes the LAST
    // occurrence of a key, so the appended value wins. Reboot to apply.
    String kv = cmd.substring(7); kv.trim();
    int eq = kv.indexOf('=');
    if (eq < 1 || eq >= (int)kv.length() - 1) {
      tprintln("usage: setcfg key=value   (e.g. setcfg rtk_enabled=1)");
    } else {
      File f = SD.open("/config.txt", FILE_APPEND);
      if (!f) {
        tprintln("setcfg: cannot open /config.txt");
      } else {
        f.print("\n"); f.print(kv); f.print("\n"); f.close();
        tprintf("setcfg: appended '%s' — power-cycle/reset to apply (config is read at boot)\n",
                kv.c_str());
      }
    }

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

#ifdef BUILD_B1
  } else if (cmd == "power" || cmd == "qi") {
    // B1 Qi-pad self-power latch status
    tprintln("=== B1 Power (Qi latch) ===");
    tprintf("PWR_HOLD(GPIO19): %s\n", digitalRead(PWR_HOLD_PIN) ? "HIGH (on)" : "LOW (released)");
    tprintf("QI_PRESENT(GPIO15): %s\n", g_b1QiPresent ? "ON PAD" : "off pad");
    tprintf("Battery: %.2fV %d%% %s\n", battery.voltage, battery.percent,
            battery.voltage >= B1_CHARGED_V ? "(charged)" : "");
    tprintf("Parked: %s\n", g_b1Parked ? "YES" : "no");
  } else if (cmd == "poweroff" || cmd == "pwroff") {
    // Deliberate power-off (sealed unit, no button). On the pad the unit stays
    // alive on D7 and parks until lifted; off the pad it powers down now.
    tprintln("[B1PWR] poweroff requested — releasing latch.");
    b1EnterParked("cmd");
#endif

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

  } else if (cmd.startsWith("race armrtk")) {
    // Increment 2 — capture the start line in the RTK frame, so cm-accurate
    // boats are measured against a cm-accurate line (not a ±2 m typed line).
    //   RC end  = own position (RC base = committee end = RTK frame origin)
    //   PIN end = the rc_pin peer's latest RTK-FIXED position over the mesh
    // Then the existing ocsArm + MSG_RACE_ARMED fleet path, with cm coords.
    int secs = -1;
    if (sscanf(cmd.c_str(), "race armrtk %d", &secs) != 1 || secs < 0) {
      tprintln("usage: race armrtk <secs_from_now>   (RC-only; captures line from base + rc_pin RTK)");
    } else if (!config.rtk_enabled) {
      tprintln("race armrtk: rtk_enabled is OFF (SD config). Use `race arm <coords>` for the manual line.");
    } else if (g_role != ROLE_RC_SIGNAL) {
      tprintln("race armrtk: RC-only — this boat is not unit_role=rc_signal (the base).");
    } else if (!gps.valid || (gps.lat == 0 && gps.lon == 0)) {
      tprintln("race armrtk: RC base has no position yet (survey-in not complete?).");
    } else {
      uint32_t now = millis();
      int pin = -1;
      for (int i = 0; i < g_mesh_peer_count; i++) {
        if (g_mesh_peers[i].unit_role == ROLE_RC_PIN &&
            (now - g_mesh_peers[i].last_seen_ms) < 5000) { pin = i; break; }
      }
      if (pin < 0) {
        tprintln("race armrtk: no rc_pin peer in last 5s — is the pin boat on (unit_role=rc_pin) + in the mesh?");
      } else if (g_mesh_peers[pin].fix_quality != 4) {
        tprintf("race armrtk: rc_pin peer NOT RTK FIXED (q=%d) — wait for q=4 so the pin end is cm-accurate.\n",
                g_mesh_peers[pin].fix_quality);
      } else {
        double pln = g_mesh_peers[pin].last_lat_e7 / 1e7;
        double plg = g_mesh_peers[pin].last_lon_e7 / 1e7;
        double rln = gps.lat, rlg = gps.lon;
        // line-length sanity (equirectangular)
        double refLat = ((pln + rln) / 2.0) * PI / 180.0;
        double dx = (plg - rlg) * 111320.0 * cos(refLat);
        double dy = (pln - rln) * 111320.0;
        double lineLen = sqrt(dx * dx + dy * dy);
        if (lineLen < 10.0 || lineLen > 1000.0) {
          tprintf("race armrtk: line length %.1f m out of sane range (10-1000 m) — check positions. NOT armed.\n",
                  lineLen);
        } else {
          uint32_t start_ms = now + (uint32_t)(secs * 1000);
          ocsArm(pln, plg, rln, rlg, start_ms);
          bool sent = meshBroadcastRaceArmed(pln, plg, rln, rlg, secs, 0, 30);
          tprintf("race ARMED (RTK frame): PIN(%.7f,%.7f q=4)  RC(%.7f,%.7f base)  len=%.1f m  T+0 in %d s\n",
                  pln, plg, rln, rlg, lineLen, secs);
          tprintf("mesh broadcast: %s (3x). NOTE: RC end taken from base GGA — verify it equals the surveyed ARP (1005).\n",
                  sent ? "OK" : "FAILED");
        }
      }
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
        tprintf("  %-3s 0x%08lx role=%u age=%lus rssi=%ddBm msgs=%lu lat=%.7f lon=%.7f sog=%.1fkt cog=%d hdg.heel=%d\n",
                boatNameForSender(p.sender_id),
                (unsigned long)p.sender_id,
                (unsigned)p.unit_role,
                (now - p.last_seen_ms) / 1000,
                (int)p.last_rssi,
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

  } else if (cmd == "fleetwatch") {
    // Toggle the live RC fleet dashboard (refreshes from fleetWatchTick()
    // in the main loop — non-blocking). VT100 terminal required.
    g_fleetWatch = !g_fleetWatch;
    if (g_fleetWatch) {
      g_fleetWatchLast = 0;          // paint on the next tick immediately
      Serial.print("\033[2J");       // clear screen on start
      tprintln("fleetwatch: ON (live ~2 Hz; type 'fleetwatch' again to stop)");
    } else {
      tprintln("fleetwatch: OFF");
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
    tprintln("  fleetwatch - live RC fleet OCS dashboard, ~2 Hz (RC-only; VT100 term)");
    tprintln("  classes    - Show /sf/classes.csv bow_offset registry (RC-only)");
    tprintln("  statusup   - Upload fleet health snapshot to S3 now");
    tprintln("  configsync - Fetch + apply cloud config from S3 (reboots if newer)");
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
    // Suppress the heartbeat print while the live fleet dashboard is up, so
    // its 5 s lines don't corrupt the ANSI table. Watchdog logic below still runs.
    if (!g_fleetWatch)
      Serial.printf("[DIAG] uptime=%lus heap=%u sect=%s up=%s iter=%lu (+%ld)\n",
                    now / 1000, ESP.getFreeHeap(),
                    (const char*)g_loopSection,
                    (const char*)g_uploadSection,
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
  g_uploadSection = "count-pending-initial";
  countPendingUploads();
  Serial.printf("[UPLOAD] Initial pending: N=%d\n", pendingUploads);

  while (true) {
    g_uploadSection = "idle";
    esp_task_wdt_reset();
    unsigned long now = millis();
    bool shouldUpload = false;

    // Count pending uploads every 30 seconds (for display)
    if (now - lastPendingCount >= 30000) {
      g_uploadSection = "count-pending-periodic";
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
              if (!wifiConnected) { g_uploadSection = "wifi-connect.ota-only"; connectWiFi(); }
              if (wifiConnected) {
                g_uploadSection = "ota-only";
                performOTAUpdate(false);   // version gate only (any-WiFi OTA)
                // Stage 3: piggyback fleet health snapshot on the same
                // WiFi window. Once per boot — boats that idle on
                // Home-IOT for hours don't need to spam status PUTs.
                if (!g_statusCheckedThisBoot) {
                  g_uploadSection = "status-upload.ota-only";
                  if (uploadStatusSnapshot()) g_statusCheckedThisBoot = true;
                }
                if (!g_bootLogUploadedThisBoot) {
                  g_uploadSection = "bootlog-upload.ota-only";
                  if (uploadBootLogSnapshot()) g_bootLogUploadedThisBoot = true;
                }
                // Stage 3.5: cloud config sync (observe-only MVP).
                if (!g_configSyncCheckedThisBoot) {
                  g_uploadSection = "cfgsync.ota-only";
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
        g_uploadSection = "wifi-connect";
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
          g_uploadSection = "s3-conn-test";
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
            g_uploadSection = "count-files";
            uploadTotal = countFilesToUpload("/sf");
            Serial.printf("[UPLOAD] Found %d files to upload\n", uploadTotal);

            if (uploadTotal > 0) {
              g_uploadSection = "upload-dir";
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
            g_uploadSection = "count-files.post";
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
            g_uploadSection = "ota-check";
            performOTAUpdate(false);

            // Stage 3 status snapshot upload — once per boot. Piggybacks
            // on the same WiFi window used for OTA + session uploads.
            if (!g_statusCheckedThisBoot) {
              g_uploadSection = "status-upload";
              if (uploadStatusSnapshot()) g_statusCheckedThisBoot = true;
            }
            // /boot.log upload — once per boot, after status. Surfaces
            // alive-heartbeat history to the web battery dashboard.
            if (!g_bootLogUploadedThisBoot) {
              g_uploadSection = "bootlog-upload";
              if (uploadBootLogSnapshot()) g_bootLogUploadedThisBoot = true;
            }
            // Stage 3.5 cloud config sync (observe-only MVP).
            if (!g_configSyncCheckedThisBoot) {
              g_uploadSection = "cfgsync";
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

#ifdef BUILD_B1
  // B1 self-power latch released: stay in a non-blocking parked state. This is
  // AFTER g_loopIter++/wdt-reset so both watchdogs keep being fed — a halt here
  // would trip the Core-1 loop watchdog into a restart that re-latches power.
  if (g_b1Parked) { g_loopSection = "b1-parked"; b1ParkedLoop(); return; }
#endif

  // Stage 3.6 — cloud config apply rebooted-in-3s mechanism. The
  // upload task sets g_configRebootPending after a successful
  // config rewrite; we restart here on Core 1 so the upload task
  // unwinds cleanly first. Gate on !uploading so an in-flight
  // upload of another file isn't truncated by the restart.
  if (g_configRebootPending && (int32_t)(now - g_configRebootAtMs) >= 0
      && !uploading && !triggerUpload) {
    Serial.println("[CFGSYNC] Rebooting now for cloud config apply.");
    delay(50);
    esp_restart();
  }

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

  g_loopSection = "fleetwatch";
  fleetWatchTick();

  g_loopSection = "serial-cmd";
  handleSerialCommand();

#ifdef BUILD_B1
  // B1 Qi-pad power management: QI sense + the auto power-off triggers.
  g_loopSection = "b1-power";
  b1PowerTick();
  if (g_b1Parked) return;   // tripped a trigger this iteration — out before logging
#endif

  g_loopSection = "gps";
  if (config.rtk_enabled && roleIsBase()) readGPSBase();   // demux RTCM-out + 1 Hz NMEA
  else                                    readGPS();        // unchanged NMEA-only path

  // RTK Phase-2 — rover: drain reassembled RTCM from the ring (filled in the
  // ESP-NOW recv callback) to the GNSS UART. Bounded + non-blocking: write only
  // what fits the UART TX buffer this iteration, never flush a backlog.
  if (config.rtk_enabled && roleIsRover() && g_rtcmRing) {
    g_loopSection = "rtcm-drain";
    uint8_t tmp[256];
    for (int budget = 4; budget > 0; budget--) {
      int canWrite = Serial2.availableForWrite();
      if (canWrite <= 0) break;
      if (canWrite > (int)sizeof(tmp)) canWrite = sizeof(tmp);
      size_t n = xStreamBufferReceive(g_rtcmRing, tmp, (size_t)canWrite, 0);
      if (n == 0) break;
      Serial2.write(tmp, n);
    }
  }

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

  // The OCS alarm blinks at ~2 Hz (inverts every 250 ms); the normal 500 ms
  // display cadence is too slow to render that (and aliases to a static
  // frame), so tick the display ~every 120 ms while the alarm is up. The RC
  // fleet panel also wants a faster, live refresh.
  bool fastDisp = (g_ocs.armed && g_ocs.over_line) ||
                  (g_role == ROLE_RC_SIGNAL && g_ocs.armed);
  unsigned long dispGate = fastDisp ? 120 : DISPLAY_UPDATE_MS;
  if (now - lastDisp >= dispGate) {
    g_loopSection = "display";
    updateDisplay();
    // Adaptive backlight — recheck at every display tick, only write
    // PWM register when target changes (effectively at logging
    // start/stop). Saves ~30% of backlight current during idle.
    static uint8_t bl_current = TFT_BL_DUTY_IDLE;
    uint8_t bl_target = logging ? TFT_BL_DUTY_RECORDING : TFT_BL_DUTY_IDLE;
    if (bl_target != bl_current) {
      ledcWrite(TFT_BL_PIN, bl_target);
      bl_current = bl_target;
    }
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
