# SailFrames E1 Firmware - Complete Annotated Guide

This document explains every part of the `sailframes_e1.ino` firmware in detail.
Written for someone new to ESP32 and Arduino programming.

---

## Table of Contents

1. [What is Arduino/ESP32 Programming?](#1-what-is-arduinoesp32-programming)
2. [File Structure Overview](#2-file-structure-overview)
3. [Include Statements (Libraries)](#3-include-statements-libraries)
4. [Preprocessor Directives](#4-preprocessor-directives)
5. [Pin Definitions](#5-pin-definitions)
6. [Configuration Constants](#6-configuration-constants)
7. [Data Structures (structs)](#7-data-structures-structs)
8. [Global Variables](#8-global-variables)
9. [The setup() Function](#9-the-setup-function)
10. [The loop() Function](#10-the-loop-function)
11. [Helper Functions](#11-helper-functions)
12. [Common Patterns](#12-common-patterns)
13. [Memory Management](#13-memory-management)
14. [Dual-Core Programming](#14-dual-core-programming)
15. [Deep Sleep and Power Management](#15-deep-sleep-and-power-management)

---

## 1. What is Arduino/ESP32 Programming?

### The Arduino Framework

Arduino is a simplified C++ framework for programming microcontrollers. It hides
much of the complexity of embedded programming behind easy-to-use functions.

Every Arduino program has two required functions:

```cpp
void setup() {
    // Runs ONCE when the device powers on or resets
    // Use this for initialization: setting up pins, starting serial, etc.
}

void loop() {
    // Runs REPEATEDLY forever after setup() completes
    // This is where your main program logic goes
    // When loop() finishes, it immediately starts again
}
```

### The ESP32 Microcontroller

The ESP32 is a powerful microcontroller with:
- **Dual-core CPU** at 240 MHz (can run two tasks simultaneously)
- **520 KB RAM** (for variables and program execution)
- **4 MB Flash** (for storing your program and files)
- **Built-in WiFi and Bluetooth**
- **Many GPIO pins** (General Purpose Input/Output)
- **Multiple communication interfaces**: I2C, SPI, UART

### How Code Executes

1. You write code on your computer
2. Arduino IDE compiles it to machine code
3. Machine code is uploaded to ESP32's flash memory
4. On power-on, ESP32 runs your code starting with `setup()`
5. After `setup()`, it runs `loop()` forever

---

## 2. File Structure Overview

The firmware is organized into logical sections:

```
1. FILE HEADER (comments describing the project)
2. #include STATEMENTS (import libraries)
3. #define STATEMENTS (constants and configuration)
4. STRUCT DEFINITIONS (custom data types)
5. GLOBAL VARIABLES (data shared across functions)
6. HELPER FUNCTIONS (reusable code blocks)
7. setup() FUNCTION (initialization)
8. loop() FUNCTION (main program)
```

---

## 3. Include Statements (Libraries)

Libraries are pre-written code packages that provide functionality. Including
a library gives you access to its functions.

```cpp
#include <Wire.h>
```
**Wire.h** - I2C communication library
- I2C is a two-wire protocol (SDA for data, SCL for clock)
- Used to communicate with: OLED display, IMU sensor
- Functions: `Wire.begin()`, `Wire.write()`, `Wire.read()`

```cpp
#include <SPI.h>
```
**SPI.h** - SPI communication library
- SPI is a four-wire protocol (MOSI, MISO, CLK, CS)
- Faster than I2C, used for: SD card
- Functions: `SPI.begin()`, `SPI.transfer()`

```cpp
#include <SD.h>
```
**SD.h** - SD card file system library
- Read/write files on SD cards
- Functions: `SD.begin()`, `SD.open()`, `file.write()`, `file.close()`

```cpp
#include <WiFi.h>
```
**WiFi.h** - WiFi connectivity library
- Connect to WiFi networks, create access points
- Functions: `WiFi.begin()`, `WiFi.status()`, `WiFi.localIP()`

```cpp
#include <WiFiClient.h>
#include <WiFiClientSecure.h>
```
**WiFiClient.h** - Basic TCP client (unencrypted)
**WiFiClientSecure.h** - HTTPS/TLS encrypted client
- Used for uploading data to AWS over HTTPS

```cpp
#include <ArduinoOTA.h>
```
**ArduinoOTA.h** - Over-The-Air update library
- Update firmware via WiFi instead of USB cable
- Functions: `ArduinoOTA.begin()`, `ArduinoOTA.handle()`

```cpp
#include <HTTPClient.h>
```
**HTTPClient.h** - HTTP request library
- Make GET/POST/PUT requests to web servers
- Functions: `http.begin()`, `http.GET()`, `http.POST()`

```cpp
#include <U8g2lib.h>
```
**U8g2lib.h** - Universal graphics library for displays
- Draw text, shapes, images on OLED/LCD displays
- Functions: `u8g2.drawStr()`, `u8g2.drawLine()`, `u8g2.sendBuffer()`

```cpp
#include <Adafruit_BNO08x.h>
```
**Adafruit_BNO08x.h** - BNO085 IMU sensor library
- Read orientation, acceleration, gyroscope data
- Functions: `bno.begin()`, `bno.getSensorEvent()`

```cpp
#include <NimBLEDevice.h>
```
**NimBLEDevice.h** - Bluetooth Low Energy library
- Connect to BLE devices (wind sensor)
- Lighter than the default BLE library

```cpp
#include <esp_sleep.h>
```
**esp_sleep.h** - ESP32 deep sleep library
- Put ESP32 into ultra-low-power sleep mode
- Wake up on button press, timer, or other triggers

---

## 4. Preprocessor Directives

Preprocessor directives start with `#` and are processed BEFORE compilation.

### #define - Create Constants

```cpp
#define GPS_BAUD 460800
```
This creates a constant named `GPS_BAUD` with value `460800`.
Everywhere you write `GPS_BAUD` in code, the compiler replaces it with `460800`.

**Why use #define instead of variables?**
- Constants use NO RAM (replaced at compile time)
- Can be used in places variables can't (like array sizes)
- Makes code more readable

### #if / #endif - Conditional Compilation

```cpp
#define ENABLE_WIND true

#if ENABLE_WIND
    // This code is ONLY compiled if ENABLE_WIND is true
    initWindSensor();
#endif
```

This lets you enable/disable features at compile time. Disabled code doesn't
take up any space in the final program.

---

## 5. Pin Definitions

GPIO = General Purpose Input/Output pins. These are the physical pins on the
ESP32 that you connect wires to.

```cpp
#define GPS_RX_PIN    16
#define GPS_TX_PIN    17
```
**GPS UART pins** - Serial communication with GPS module
- RX (receive) = ESP32 receives data FROM GPS
- TX (transmit) = ESP32 sends data TO GPS
- UART is asynchronous serial (no clock wire needed)

```cpp
#define SD_CS_PIN     5
```
**SD Card Chip Select** - Tells SD card "I'm talking to you"
- SPI can have multiple devices; CS selects which one is active
- LOW = selected, HIGH = not selected

```cpp
#define SDA_PIN       21
#define SCL_PIN       22
```
**I2C pins** - Two-wire communication bus
- SDA = Serial Data (bidirectional data line)
- SCL = Serial Clock (clock signal from master)
- Multiple devices can share same pins (each has unique address)

```cpp
#define LED_PIN       2
```
**Built-in LED** - Blue LED on ESP32 DevKit board
- GPIO2 is connected to the onboard LED
- Used to indicate recording status (blinks when logging)

```cpp
#define POWER_BTN_PIN 13
```
**Power button** - Momentary pushbutton for shutdown
- Connected between GPIO13 and GND
- Uses internal pullup resistor (no external resistor needed)

```cpp
#define BATT_VOLTAGE_PIN 34
```
**Battery voltage ADC** - Analog-to-Digital Converter input
- Reads voltage through a voltage divider
- GPIO 34-39 are input-only on ESP32

---

## 6. Configuration Constants

```cpp
#define GPS_BAUD      460800
```
Baud rate = bits per second for serial communication.
460800 is very fast; standard values are 9600, 115200.

```cpp
#define SERIAL_BAUD   115200
```
Baud rate for USB serial monitor (debugging output).

```cpp
#define SCREEN_WIDTH  128
#define SCREEN_HEIGHT 64
```
OLED display dimensions in pixels.

```cpp
#define OLED_ADDR     0x3C
```
I2C address of the OLED display.
- I2C addresses are 7-bit (0x00 to 0x7F)
- Each device has a unique address
- Use `i2cdetect` or I2C scanner to find addresses

```cpp
#define GPS_FIX_TIMEOUT_MS  300000
```
Timeout in milliseconds (300000 ms = 5 minutes).
If GPS doesn't get a fix in 5 minutes, give up waiting.

```cpp
#define DISPLAY_UPDATE_MS   1000
```
Update display every 1000ms (1 second).
Updating too frequently wastes CPU and can cause flicker.

---

## 7. Data Structures (structs)

A `struct` groups related variables together into a custom data type.

### GPSData Structure

```cpp
struct GPSData {
    float lat = 0, lon = 0, alt = 0;
    float speed_kts = 0, course = 0, hdop = 99.9;
    int satellites = 0, fix_quality = 0;
    char utc_time[12] = "000000.00";
    char date[8] = "010100";
    bool valid = false;
    bool newGGA = false;
};
```

This creates a new data type called `GPSData` containing:
- `float lat, lon, alt` - Position (latitude, longitude, altitude)
- `float speed_kts` - Speed in knots
- `float course` - Direction of travel in degrees
- `float hdop` - Horizontal precision (lower = better, <2 is good)
- `int satellites` - Number of satellites being used
- `int fix_quality` - 0=no fix, 1=GPS, 2=DGPS, 4=RTK
- `char utc_time[12]` - Time string "HHMMSS.ss"
- `char date[8]` - Date string "DDMMYY"
- `bool valid` - Do we have a valid GPS fix?
- `bool newGGA` - Has new position data arrived?

**Default values** (`= 0`) initialize the fields when struct is created.

**Usage:**
```cpp
GPSData gps;          // Create a GPSData variable
gps.lat = 42.3601;    // Set latitude
gps.valid = true;     // Mark as valid
```

### IMUData Structure

```cpp
struct IMUData {
    float accel_x, accel_y, accel_z;    // Acceleration (g-force)
    float gyro_x, gyro_y, gyro_z;       // Rotation rate (deg/sec)
    float heel = 0, pitch = 0;          // Boat angles
    float heading = 0;                   // Compass heading
};
```

**Heel** = side-to-side tilt (rolling)
**Pitch** = front-to-back tilt (bow up/down)
**Heading** = compass direction boat is pointing

### WindData Structure

```cpp
struct WindData {
    float speed_mps = 0;        // Wind speed in meters/second
    float speed_kts = 0;        // Wind speed in knots
    int angle_deg = 0;          // Wind angle in degrees
    bool connected = false;     // Is sensor connected?
    bool newData = false;       // New reading available?
    unsigned long lastUpdate;   // When last data received
};
```

### Config Structure

```cpp
struct Config {
    WiFiNetwork wifi[MAX_WIFI_NETWORKS];  // Array of WiFi networks
    int wifi_count = 0;                    // How many configured
    char upload_url[256] = "https://...";  // AWS endpoint
    char boat_id[16] = "E1";               // Device identifier
    int gps_rate_hz = 10;                  // GPS update rate
    float start_speed_knots = 1.5;         // Auto-record start speed
    float stop_speed_knots = 0.5;          // Auto-record stop speed
    int start_delay_sec = 10;              // Seconds before start
    int stop_delay_sec = 180;              // Seconds before stop
};
```

This struct holds all configuration loaded from `config.txt` on SD card.

---

## 8. Global Variables

Global variables are declared outside any function and can be accessed
from anywhere in the program.

```cpp
GPSData gps;
IMUData imu;
WindData wind;
Config config;
```
These create instances of our struct types.

```cpp
File navFile, imuFile, rawFile, windFile;
```
File handles for SD card files. `File` is a type from the SD library.

```cpp
bool sdOK = false, imuOK = false, oledOK = false;
```
Status flags - track which hardware initialized successfully.

```cpp
bool logging = false;
```
Are we currently recording data to SD card?

```cpp
bool wifiConnected = false;
```
Are we connected to a WiFi network?

```cpp
unsigned long lastDisp = 0, lastFlush = 0, lastIMU = 0;
```
Timestamps for timing operations. `unsigned long` holds milliseconds
since boot (up to ~49 days before overflow).

### Recording State Machine Variables

```cpp
enum RecordState { REC_IDLE, REC_ARMED, REC_RECORDING, REC_STOPPING };
RecordState recState = REC_IDLE;
```

**enum** creates a list of named constants. `recState` can only be one of
these four values. This is clearer than using magic numbers (0, 1, 2, 3).

```cpp
unsigned long armStartTime = 0;
unsigned long stopStartTime = 0;
```
Track when we entered armed/stopping states (for delay timing).

```cpp
int sessionCount = 0;
```
Counts recording sessions (increments each time recording starts).

### FreeRTOS Variables

```cpp
SemaphoreHandle_t sdMutex = NULL;
TaskHandle_t uploadTaskHandle = NULL;
volatile bool triggerUpload = false;
```

**SemaphoreHandle_t** - A mutex (mutual exclusion lock) for SD card access.
Prevents two tasks from accessing SD card simultaneously.

**TaskHandle_t** - Reference to a FreeRTOS task (for the upload task).

**volatile** - Tells compiler this variable can change unexpectedly
(from another task or interrupt). Prevents optimization bugs.

---

## 9. The setup() Function

`setup()` runs once on power-on. It initializes all hardware.

### Serial Communication

```cpp
Serial.begin(SERIAL_BAUD);
delay(500);
Serial.println("SailFrames E1 v2.1");
```

`Serial.begin(115200)` starts USB serial at 115200 baud.
`delay(500)` waits 500ms for serial to stabilize.
`Serial.println()` sends text to USB (viewable in Serial Monitor).

### Check Wake Reason

```cpp
esp_sleep_wakeup_cause_t wakeReason = esp_sleep_get_wakeup_cause();
if (wakeReason == ESP_SLEEP_WAKEUP_EXT0) {
    Serial.println("Woke from deep sleep via button");
}
```

After deep sleep, ESP32 reboots. This checks WHY it woke up:
- `ESP_SLEEP_WAKEUP_EXT0` = external pin (button)
- `ESP_SLEEP_WAKEUP_TIMER` = timer expired
- Other = normal power-on

### Initialize Mutex

```cpp
sdMutex = xSemaphoreCreateMutex();
```

Creates a mutex for SD card access. A mutex is like a lock:
- Only one task can hold it at a time
- Other tasks wait until it's released
- Prevents data corruption from simultaneous access

### GPIO Setup

```cpp
pinMode(POWER_BTN_PIN, INPUT_PULLUP);
```

`pinMode()` configures a GPIO pin:
- `INPUT` = read external signals
- `OUTPUT` = control external devices
- `INPUT_PULLUP` = input with internal pull-up resistor

**Pull-up resistor**: When button is NOT pressed, pin reads HIGH (1).
When button IS pressed (connects to GND), pin reads LOW (0).

```cpp
pinMode(LED_PIN, OUTPUT);
digitalWrite(LED_PIN, LOW);
```

`digitalWrite()` sets an output pin HIGH (3.3V) or LOW (0V).

### I2C Initialization

```cpp
Wire.begin(SDA_PIN, SCL_PIN);
Wire.setClock(100000);
```

`Wire.begin()` starts I2C as master on specified pins.
`Wire.setClock(100000)` sets I2C speed to 100kHz (standard mode).

### Device Detection

```cpp
Wire.beginTransmission(OLED_ADDR);
bool oledFound = (Wire.endTransmission() == 0);
```

I2C device detection:
1. `beginTransmission()` starts talking to address
2. `endTransmission()` returns 0 if device acknowledged, non-zero if no response

### OLED Initialization

```cpp
u8g2.begin();
u8g2.clearBuffer();
u8g2.setFont(u8g2_font_helvB14_tr);
u8g2.drawStr(10, 20, "SAIL");
u8g2.sendBuffer();
```

U8g2 uses a framebuffer pattern:
1. `clearBuffer()` - clear the memory buffer
2. `drawStr()`, `drawLine()`, etc. - draw to buffer
3. `sendBuffer()` - send buffer to display

### SD Card Initialization

```cpp
SPI.begin(18, 19, 23, SD_CS_PIN);
sdOK = SD.begin(SD_CS_PIN, SPI, 4000000);
```

`SPI.begin(CLK, MISO, MOSI, CS)` starts SPI on specified pins.
`SD.begin()` initializes SD card at 4MHz. Returns true if successful.

### Load Configuration

```cpp
loadConfig();
```

Reads `config.txt` from SD card and parses settings into `config` struct.

### GPS Initialization

```cpp
Serial2.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
```

ESP32 has three hardware serial ports:
- `Serial` - USB (for debugging)
- `Serial1` - UART1
- `Serial2` - UART2 (used for GPS)

`SERIAL_8N1` = 8 data bits, No parity, 1 stop bit (standard).

### WiFi Connection

```cpp
WiFi.mode(WIFI_STA);
WiFi.disconnect(true);
connectWiFi();
```

`WIFI_STA` = Station mode (connect to existing network).
`WIFI_AP` = Access Point mode (create a network).

### Create Upload Task

```cpp
xTaskCreatePinnedToCore(
    uploadTaskFunc,     // Function to run
    "uploadTask",       // Task name (for debugging)
    8192,               // Stack size in bytes
    NULL,               // Parameters (none)
    1,                  // Priority (1 = low)
    &uploadTaskHandle,  // Handle to reference later
    0                   // Core to run on (0 or 1)
);
```

This creates a FreeRTOS task that runs on Core 0.
Core 1 runs `loop()`. Now we have true parallel execution.

---

## 10. The loop() Function

`loop()` runs continuously after `setup()` completes.

### Timing with millis()

```cpp
unsigned long now = millis();
```

`millis()` returns milliseconds since boot. Used for non-blocking timing:

```cpp
if (now - lastDisp >= DISPLAY_UPDATE_MS) {
    updateDisplay();
    lastDisp = now;
}
```

This runs `updateDisplay()` every 1000ms WITHOUT blocking other code.

**Why not use delay()?**

```cpp
// BAD - blocks everything for 1 second
delay(1000);
updateDisplay();

// GOOD - checks time, doesn't block
if (now - lastDisplay >= 1000) {
    updateDisplay();
    lastDisplay = now;
}
```

With `delay()`, nothing else can happen during the wait.
With `millis()`, other code keeps running.

### Reading Sensors

```cpp
readGPS();
```

Reads any available data from GPS serial port. Parses NMEA sentences
and updates the `gps` struct.

```cpp
if (now - lastIMU >= IMU_INTERVAL_MS) {
    readIMU();
    lastIMU = now;
}
```

Reads IMU at a fixed interval (every 50ms = 20Hz).

### Logging Data

```cpp
if (logging && gps.newGGA) {
    logNav();
    gps.newGGA = false;
}
```

When logging is active AND new GPS data arrived, write to log file.
`gps.newGGA` is set by the GPS parser when new position received.

### The State Machine

```cpp
updateRecordingState();
```

Checks GPS speed and updates recording state:
- **IDLE** → **ARMED** when speed > 1.5kt
- **ARMED** → **RECORDING** after 10 seconds above threshold
- **RECORDING** → **STOPPING** when speed < 0.5kt
- **STOPPING** → **IDLE** after 3 minutes below threshold

### Checking for Commands

```cpp
handleTelnet();
handleSerialCommand();
```

Checks for incoming commands via telnet (WiFi) or USB serial.

### Flushing Files

```cpp
if (logging && now - lastFlush >= FLUSH_INTERVAL_MS) {
    navFile.flush();
    lastFlush = now;
}
```

`flush()` writes buffered data to SD card. If power is lost before
flush, unflushed data is lost. We flush every 10 seconds.

---

## 11. Helper Functions

### Reading GPS (NMEA Parsing)

```cpp
void readGPS() {
    while (Serial2.available()) {
        char c = Serial2.read();
        // Parse NMEA sentences...
    }
}
```

`Serial2.available()` returns number of bytes waiting to be read.
`Serial2.read()` reads one byte.

NMEA sentences look like:
```
$GNRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A
```

The code parses these character by character, extracting fields.

### The Shutdown Function

```cpp
void shutdownCleanly() {
    // Close files
    navFile.flush();
    navFile.close();

    // Show message on display
    u8g2.clearBuffer();
    u8g2.drawStr(15, 35, "SHUTDOWN");
    u8g2.sendBuffer();

    // Configure wake source and sleep
    esp_sleep_enable_ext0_wakeup(GPIO_NUM_13, 0);
    esp_deep_sleep_start();
}
```

1. Flush and close all files (prevent data loss)
2. Show "SHUTDOWN" on display
3. Configure GPIO13 as wake source (0 = wake on LOW)
4. Enter deep sleep (~10µA power consumption)

### WiFi Connection

```cpp
bool connectWiFi() {
    WiFi.begin(ssid, password);

    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts++ < 30) {
        delay(500);
    }

    return WiFi.status() == WL_CONNECTED;
}
```

`WiFi.begin()` starts connection attempt (non-blocking).
`WiFi.status()` returns connection state:
- `WL_CONNECTED` = connected
- `WL_DISCONNECTED` = not connected
- `WL_CONNECT_FAILED` = wrong password, etc.

### File Operations

```cpp
File f = SD.open("/config.txt", FILE_READ);
if (f) {
    while (f.available()) {
        String line = f.readStringUntil('\n');
        // Process line...
    }
    f.close();
}
```

`SD.open()` opens a file:
- `FILE_READ` = read only
- `FILE_WRITE` = write (creates if doesn't exist)
- `FILE_APPEND` = append to existing

Always check if file opened successfully (`if (f)`).
Always close files when done (`f.close()`).

---

## 12. Common Patterns

### Non-Blocking Delays

```cpp
static unsigned long lastAction = 0;
if (millis() - lastAction >= 1000) {
    doSomething();
    lastAction = millis();
}
```

`static` makes the variable persist between function calls.

### State Machines

```cpp
enum State { STATE_A, STATE_B, STATE_C };
State currentState = STATE_A;

void update() {
    switch (currentState) {
        case STATE_A:
            if (conditionToB) currentState = STATE_B;
            break;
        case STATE_B:
            if (conditionToC) currentState = STATE_C;
            break;
        case STATE_C:
            if (conditionToA) currentState = STATE_A;
            break;
    }
}
```

State machines make complex logic easier to understand and debug.

### Circular Buffers

```cpp
char buffer[256];
int bufferIndex = 0;

void addChar(char c) {
    buffer[bufferIndex++] = c;
    if (bufferIndex >= sizeof(buffer)) {
        bufferIndex = 0;  // Wrap around
    }
}
```

### Debouncing Buttons

```cpp
if (digitalRead(BUTTON) == LOW) {
    unsigned long pressStart = millis();
    while (digitalRead(BUTTON) == LOW) {
        if (millis() - pressStart > 2000) {
            // Button held for 2 seconds
            doLongPressAction();
        }
    }
}
```

---

## 13. Memory Management

### Stack vs Heap

**Stack**: Local variables, function calls. Fast but limited (~8KB per task).
**Heap**: Dynamic allocation (`new`, `malloc`). Larger but slower.

```cpp
void function() {
    int localVar;           // Stack - automatic cleanup
    char buffer[100];       // Stack - fixed size

    String s = "hello";     // Heap - dynamic size
    char* p = new char[n];  // Heap - manual cleanup needed
}
```

### Checking Memory

```cpp
Serial.printf("Free heap: %u bytes\n", ESP.getFreeHeap());
Serial.printf("Stack remaining: %u bytes\n", uxTaskGetStackHighWaterMark(NULL));
```

### Avoiding Memory Issues

1. **Avoid String class in loops** - creates heap fragmentation
2. **Use fixed-size buffers** - `char buf[64]` instead of `String`
3. **Check heap before large allocations** (SSL needs ~45KB)
4. **Close files and connections** when done

---

## 14. Dual-Core Programming

ESP32 has two cores. By default:
- **Core 1**: Runs Arduino `setup()` and `loop()`
- **Core 0**: Runs WiFi/Bluetooth stack

We can run our own tasks on Core 0:

```cpp
void myTask(void* param) {
    while (true) {
        // Do something
        vTaskDelay(pdMS_TO_TICKS(1000));  // Wait 1 second
    }
}

// In setup():
xTaskCreatePinnedToCore(myTask, "name", 8192, NULL, 1, &handle, 0);
```

### Task Parameters

- **Function**: Must have signature `void func(void* param)`
- **Name**: For debugging (visible in task list)
- **Stack size**: How much stack memory (8192 = 8KB)
- **Parameter**: Passed to function (NULL if not needed)
- **Priority**: Higher = more CPU time (1-24, WiFi uses 23)
- **Handle**: Reference for later (can be NULL)
- **Core**: 0 or 1

### Synchronization (Mutex)

When two tasks access shared data, you need synchronization:

```cpp
SemaphoreHandle_t mutex = xSemaphoreCreateMutex();

// In task 1:
if (xSemaphoreTake(mutex, pdMS_TO_TICKS(1000))) {  // Wait up to 1 sec
    // Access shared resource
    xSemaphoreGive(mutex);  // Release
}

// In task 2:
if (xSemaphoreTake(mutex, pdMS_TO_TICKS(1000))) {
    // Access shared resource
    xSemaphoreGive(mutex);
}
```

---

## 15. Deep Sleep and Power Management

### Power Modes

| Mode | Current | Wake Time | Use Case |
|------|---------|-----------|----------|
| Active | 80-260mA | - | Running |
| Light Sleep | 0.8mA | <1ms | Brief pauses |
| Deep Sleep | 10µA | ~250ms | Long idle |

### Entering Deep Sleep

```cpp
// Configure wake source
esp_sleep_enable_ext0_wakeup(GPIO_NUM_13, 0);  // Wake on LOW

// OR wake on timer
esp_sleep_enable_timer_wakeup(10 * 1000000);  // 10 seconds (in µs)

// Enter sleep (doesn't return)
esp_deep_sleep_start();
```

### What Survives Deep Sleep?

- **Lost**: RAM contents, running tasks, WiFi connection
- **Preserved**: RTC memory (8KB), GPIO state (with hold)

### RTC Memory

```cpp
RTC_DATA_ATTR int bootCount = 0;  // Survives deep sleep

void setup() {
    bootCount++;
    Serial.printf("Boot #%d\n", bootCount);
}
```

`RTC_DATA_ATTR` stores variable in RTC memory (preserved during sleep).

---

## Quick Reference

### Serial Functions
```cpp
Serial.begin(baud)          // Start serial
Serial.print(x)             // Print without newline
Serial.println(x)           // Print with newline
Serial.printf("x=%d", x)    // Formatted print
Serial.available()          // Bytes waiting to read
Serial.read()               // Read one byte
Serial.readStringUntil(c)   // Read until character
```

### GPIO Functions
```cpp
pinMode(pin, mode)          // Configure pin
digitalWrite(pin, val)      // Set output HIGH/LOW
digitalRead(pin)            // Read input state
analogRead(pin)             // Read ADC (0-4095)
```

### Timing Functions
```cpp
millis()                    // Milliseconds since boot
micros()                    // Microseconds since boot
delay(ms)                   // Blocking delay
delayMicroseconds(us)       // Blocking microsecond delay
```

### SD Card Functions
```cpp
SD.begin(CS_PIN)            // Initialize
SD.exists(path)             // Check if file exists
SD.mkdir(path)              // Create directory
SD.open(path, mode)         // Open file
file.read()                 // Read byte
file.write(data)            // Write data
file.flush()                // Force write to card
file.close()                // Close file
```

### WiFi Functions
```cpp
WiFi.mode(mode)             // STA, AP, or both
WiFi.begin(ssid, pass)      // Connect to network
WiFi.status()               // Connection status
WiFi.localIP()              // Our IP address
WiFi.disconnect()           // Disconnect
WiFi.scanNetworks()         // Find networks
```

---

## Conclusion

This firmware combines many embedded programming concepts:
- Multiple communication protocols (I2C, SPI, UART, WiFi, BLE)
- File system operations (SD card logging)
- Real-time sensor reading (GPS, IMU)
- State machines (recording control)
- Multi-tasking (dual-core upload)
- Power management (deep sleep)

Each concept builds on the basics. Start with simple examples
(blink LED, read button, print to serial) and gradually add complexity.

**Resources for Learning:**
- [ESP32 Arduino Core Documentation](https://docs.espressif.com/projects/arduino-esp32/)
- [Arduino Reference](https://www.arduino.cc/reference/en/)
- [Random Nerd Tutorials (ESP32)](https://randomnerdtutorials.com/esp32/)
- [FreeRTOS Documentation](https://www.freertos.org/Documentation/)
