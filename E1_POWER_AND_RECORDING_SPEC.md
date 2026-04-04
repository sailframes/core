# E1 Firmware Update: Power Button + GPS-Triggered Recording

## Overview

Two firmware changes to `sailframes_e1.ino`:

1. **Single momentary pushbutton** for power on/off using ESP32 deep sleep
2. **GPS speed-based auto-recording** — no manual start/stop needed

These replace the current always-on, always-recording behavior. The goal is zero-interaction recording for 6 fleet boats operated by different sailors.

---

## 1. Power Button (Deep Sleep)

### Hardware

- 6mm tactile momentary pushbutton wired between **GPIO13** and **GND**
- No external resistor needed — use ESP32 internal pullup
- Button is inside the IP67 YETLEBOX enclosure; pressed before sealing lid

### Behavior

| Action | Result |
|---|---|
| Press button (wake) | ESP32 boots from deep sleep, runs `setup()`, starts normal operation |
| Hold button > 2 seconds | Clean shutdown: flush + close SD files, show "SHUTDOWN" on OLED, enter deep sleep (~10µA draw) |
| Power glitch during racing | ESP32 auto-reboots, opens new log files, resumes operation — no data loss for prior files |

### Implementation

```cpp
#define POWER_BTN 13

void setup() {
  pinMode(POWER_BTN, INPUT_PULLUP);

  // Check wake reason (informational — behaves the same either way)
  esp_sleep_wakeup_cause_t reason = esp_sleep_get_wakeup_cause();

  // Normal init: OLED, SD, GPS, IMU...
  initSensors();
}

void loop() {
  // Check for shutdown: hold button > 2 seconds
  if (digitalRead(POWER_BTN) == LOW) {
    unsigned long pressStart = millis();
    while (digitalRead(POWER_BTN) == LOW) {
      if (millis() - pressStart > 2000) {
        shutdownCleanly();
      }
    }
  }

  // ... rest of loop
}

void shutdownCleanly() {
  // Close any open log files
  if (logging) {
    navFile.flush();
    navFile.close();
    imuFile.flush();
    imuFile.close();
    rawFile.flush();
    rawFile.close();
    logging = false;
  }

  // Visual confirmation
  display.clearDisplay();
  display.setTextSize(2);
  display.setCursor(10, 20);
  display.println("SHUTDOWN");
  display.display();
  delay(1500);

  display.clearDisplay();
  display.display();  // turn off OLED

  // Configure wake on same button (LOW = pressed)
  esp_sleep_enable_ext0_wakeup(GPIO_NUM_13, 0);
  esp_deep_sleep_start();
}
```

### Notes

- Deep sleep draws ~10µA — the LiPo holds charge for months between uses
- On wake, `setup()` runs fresh. No prior state needed — always open new log files
- The button does NOT start/stop recording — it only controls power. Recording is handled by GPS speed (see below)
- GPIO13 is safe to use — not conflicting with GPS UART (16/17) or I2C (21/22) or SPI SD (5/18/19/23)

---

## 2. GPS Speed-Triggered Recording

### Concept

Instead of recording continuously from power-on, use GPS speed to detect when the boat is actually sailing vs. sitting at dock or drifting between races. Each period of sailing becomes its own log file automatically.

### Thresholds

| Condition | Action |
|---|---|
| GPS speed > 1.5 knots sustained for 10 seconds | Start recording → open new timestamped log files |
| GPS speed < 0.5 knots sustained for 3 minutes | Stop recording → flush and close log files |
| Speed picks up again after stop | Start new recording session → new set of files |

### Why these values

- **1.5 knots start**: Well below any sailing speed for Sonar 23 or J/80, but above mooring drift and GPS noise at rest (~0.2-0.5 knots)
- **0.5 knots stop**: Covers anchored/moored/drifting between races
- **10 second start delay**: Prevents false triggers from a single GPS glitch
- **3 minute stop delay**: Covers brief lulls during racing (tacks, mark roundings, light air) without prematurely ending a recording

### State Machine

```
                    ┌──────────────┐
         power on → │   IDLE       │ ← speed < 0.5kt for 3 min
                    │  (not recording) │
                    └──────┬───────┘
                           │ speed > 1.5kt for 10 sec
                           ▼
                    ┌──────────────┐
                    │  RECORDING   │
                    │  (logging to SD) │
                    └──────┬───────┘
                           │ speed < 0.5kt for 3 min
                           ▼
                    ┌──────────────┐
                    │   IDLE       │ → upload previous files via Wi-Fi
                    │  (not recording) │
                    └──────────────┘
```

### Implementation

```cpp
// Recording states
enum RecordState { REC_IDLE, REC_ARMED, REC_RECORDING, REC_STOPPING };
RecordState recState = REC_IDLE;

unsigned long armStartTime = 0;      // when speed first exceeded start threshold
unsigned long stopStartTime = 0;     // when speed first dropped below stop threshold
int sessionCount = 0;                // increments each recording session

// Thresholds (configurable via config.txt on SD card)
float startSpeedKnots = 1.5;
float stopSpeedKnots = 0.5;
unsigned long startDelayMs = 10000;  // 10 seconds
unsigned long stopDelayMs = 180000;  // 3 minutes

void updateRecordingState() {
  float speed = gps.speedKnots;
  unsigned long now = millis();

  switch (recState) {
    case REC_IDLE:
      if (speed > startSpeedKnots) {
        recState = REC_ARMED;
        armStartTime = now;
      }
      break;

    case REC_ARMED:
      if (speed <= startSpeedKnots) {
        // Speed dropped, reset
        recState = REC_IDLE;
      } else if (now - armStartTime >= startDelayMs) {
        // Sustained speed — start recording
        sessionCount++;
        openNewLogFiles(sessionCount);
        logging = true;
        recState = REC_RECORDING;
        Serial.println("[REC] Recording started — session " + String(sessionCount));
      }
      break;

    case REC_RECORDING:
      if (speed < stopSpeedKnots) {
        recState = REC_STOPPING;
        stopStartTime = now;
      }
      break;

    case REC_STOPPING:
      if (speed >= stopSpeedKnots) {
        // Speed picked up, keep recording
        recState = REC_RECORDING;
      } else if (now - stopStartTime >= stopDelayMs) {
        // Sustained slow — stop recording
        navFile.flush(); navFile.close();
        imuFile.flush(); imuFile.close();
        rawFile.flush(); rawFile.close();
        logging = false;
        recState = REC_IDLE;
        Serial.println("[REC] Recording stopped — session " + String(sessionCount));

        // Trigger Wi-Fi upload of completed files
        // (on Core 0, non-blocking — see Wi-Fi section below)
        triggerUpload = true;
      }
      break;
  }
}
```

### Call from loop()

```cpp
void loop() {
  // ... power button check
  readGPS();
  readIMU();

  updateRecordingState();

  if (logging) {
    logNav();
    logIMU();
  }

  // ... OLED update, flush, etc.
}
```

### OLED Display Updates

Show recording state on the OLED so sailors have visual confirmation:

| State | OLED shows |
|---|---|
| IDLE (no fix) | `GPS: SEARCHING...` |
| IDLE (with fix) | `READY  SPD:0.3kt` |
| ARMED | `ARMING... SPD:2.1kt` |
| RECORDING | `REC ● S03 SPD:4.8kt` (session number, blinking dot) |
| STOPPING | `STOPPING... SPD:0.2kt` |

---

## 3. Wi-Fi Upload Between Races (Dual Core)

### Concept

When recording stops (boat slows between races), the idle period is the natural window to upload the just-completed session files. This runs on Core 0 so it never blocks sensor reads on Core 1.

### Networks

The firmware tries two SSIDs in order:
1. Yacht club Wi-Fi
2. Paul's iPhone hotspot (SSID: `SailFrames`, configured in config.txt)

### Implementation

```cpp
volatile bool triggerUpload = false;

// Spawned once in setup(), runs on Core 0
void uploadTask(void* param) {
  while (true) {
    if (triggerUpload && !logging) {
      triggerUpload = false;

      // Try each configured network
      const char* networks[][2] = {
        {"YachtClub_WiFi", "clubpass"},
        {"SailFrames", "hotspotpass"}
      };

      for (int i = 0; i < 2; i++) {
        WiFi.mode(WIFI_STA);
        WiFi.begin(networks[i][0], networks[i][1]);

        int attempts = 0;
        while (WiFi.status() != WL_CONNECTED && attempts < 30) {
          vTaskDelay(100 / portTICK_PERIOD_MS);
          attempts++;
        }

        if (WiFi.status() == WL_CONNECTED) {
          uploadCompletedFiles();  // upload only closed files
          WiFi.disconnect(true);
          WiFi.mode(WIFI_OFF);
          break;
        }
        WiFi.disconnect(true);
        WiFi.mode(WIFI_OFF);
      }
    }
    vTaskDelay(5000 / portTICK_PERIOD_MS);
  }
}

// In setup():
xTaskCreatePinnedToCore(uploadTask, "upload", 8192, NULL, 1, NULL, 0);
```

### SD Card Access Safety

Both cores may access SD — Core 1 for logging, Core 0 for reading files to upload. Use a mutex:

```cpp
SemaphoreHandle_t sdMutex = xSemaphoreCreateMutex();

// Wrap all SD operations:
if (xSemaphoreTake(sdMutex, pdMS_TO_TICKS(100))) {
  // SD read or write
  xSemaphoreGive(sdMutex);
}
```

Upload only reads closed files from previous sessions, never the active log files.

---

## 4. File Naming

Each session gets its own set of files with a session counter:

```
/sailframes/2026-04-05/
  E1_20260405_141523_S01_nav.csv
  E1_20260405_141523_S01_imu.csv
  E1_20260405_141523_S01_raw.rtcm3
  E1_20260405_143812_S02_nav.csv
  E1_20260405_143812_S02_imu.csv
  E1_20260405_143812_S02_raw.rtcm3
  ...
```

Format: `{boat_id}_{date}_{time}_{session}_{type}.{ext}`

This means each race automatically becomes its own file set, making post-race processing straightforward.

---

## 5. Config.txt Additions

Add these to the existing `config.txt` on SD card:

```
# Recording thresholds
start_speed_knots=1.5
stop_speed_knots=0.5
start_delay_sec=10
stop_delay_sec=180

# Wi-Fi networks (tried in order)
wifi_ssid_1=YachtClubWiFi
wifi_pass_1=clubpass123
wifi_ssid_2=SailFrames
wifi_pass_2=hotspotpass

# Upload endpoint
upload_url=https://sailframes-upload.s3.amazonaws.com
api_key=xxxxxxxxxxxx

# Power button GPIO
power_btn_pin=13
```

---

## 6. GPIO Pin Summary (E1 Complete)

| GPIO | Function | Notes |
|---|---|---|
| 5 | SD card CS | SPI |
| 13 | Power button | Internal pullup, ext0 wake source |
| 16 | GPS UART RX | ESP32 RX ← LG290P TX |
| 17 | GPS UART TX | ESP32 TX → LG290P RX |
| 18 | SD card CLK | SPI |
| 19 | SD card MISO | SPI |
| 21 | I2C SDA | BNO085 + SSD1309 OLED |
| 22 | I2C SCL | BNO085 + SSD1309 OLED |
| 23 | SD card MOSI | SPI |
| 2 | Onboard LED | Blink while recording |

---

## 7. Acceptance Criteria

- [ ] Button press wakes ESP32 from deep sleep, OLED shows splash screen
- [ ] 2-second button hold triggers clean SD flush/close and enters deep sleep
- [ ] Deep sleep current < 50µA (verify with multimeter)
- [ ] Device boots clean after power glitch (no state dependency)
- [ ] Recording starts automatically when GPS speed > 1.5kt for 10s
- [ ] Recording stops when GPS speed < 0.5kt for 3 minutes
- [ ] Each sailing session produces a separate set of log files
- [ ] OLED displays current state (READY / ARMING / REC / STOPPING)
- [ ] Wi-Fi upload triggers on Core 0 when recording stops
- [ ] Logging on Core 1 is never interrupted during upload
- [ ] SD mutex prevents simultaneous access from both cores
- [ ] Config.txt thresholds are loaded at boot (with sane defaults if missing)
