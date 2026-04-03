# E1 Battery & Power System Upgrade — Claude Code Reference

## Hardware Changes

### Old Setup (REMOVED)
- 18650 battery shield with spring contacts
- Samsung 30Q cells in spring-loaded holders
- Problem: vibration-induced shutdowns during sailing (spring contacts losing contact momentarily, causing ESP32 brownout resets)

### New Setup
- **Battery**: PKCELL ICR18650 6600mAh 3.7V pack (3x 18650 cells spot-welded in parallel, shrink-wrapped, JST PH2.0 connector) — no spring contacts
- **Power board**: Adafruit PowerBoost 1000C (product 2465)
  - 1A LiPo charger (MCP73871)
  - 5V boost converter (TPS61090)
  - Load-sharing: charges battery + powers ESP32 simultaneously when USB connected
  - Seamless switchover to battery when USB removed
  - Micro-USB input for charging
  - JST PH2.0 socket for battery
  - ~7 hours to fully charge the 6600mAh pack
- **All connections soldered with stranded wire** — no header pins (vibration risk)
- **Hot glue** on solder joints and JST connector for strain relief

### Wiring

```
PowerBoost 1000C          ESP32 DevKit V1
─────────────────         ───────────────
5V  ──────────────────────── VIN
G   ──────────────────────── GND
Bat ───┬─────────────────── (not directly to ESP32)
       │
       ├── 200Ω resistor ──┬── 200Ω resistor ── GND
       │                   │
       │                   └── GPIO34 (ADC, battery voltage)
       │
LBO ──────────────────────── GPIO35 (digital, low battery alert)

Battery JST ──── PowerBoost JST socket
Micro-USB ────── PowerBoost USB port (for charging)
```

### Voltage Divider (GPIO34)
- Two 200Ω resistors in series between Bat and GND (prototype values)
- Junction point wired to GPIO34
- Divides battery voltage by 2 so 4.2V max becomes 2.1V (safe for ESP32 3.3V ADC)
- TODO for production: replace with 100kΩ + 100kΩ to reduce idle current draw from ~10.5mA to ~21μA

### Low Battery Output (GPIO35)
- PowerBoost LBO pin goes LOW when battery drops below ~3.2V
- Wire to GPIO35 with INPUT_PULLUP
- Use to trigger OLED warning or graceful shutdown (flush SD buffers before power dies)

---

## Firmware Changes Required (sailframes_e1.ino)

### 1. New Pin Definitions
```cpp
const int BATT_VOLTAGE_PIN = 34;  // ADC pin, reads voltage divider
const int LOW_BATT_PIN = 35;       // Digital pin, LOW = battery critical
const float DIVIDER_RATIO = 2.0;   // Two equal resistors
```

### 2. Battery Voltage Reading Function
```cpp
float getBatteryVoltage() {
  int raw = analogRead(BATT_VOLTAGE_PIN);
  float voltage = (raw / 4095.0) * 3.3 * DIVIDER_RATIO;
  return voltage;
}

int getBatteryPercent(float voltage) {
  // LiPo discharge curve (approximate linear mapping)
  // 4.2V = 100%, 3.7V = ~50%, 3.0V = 0%
  if (voltage >= 4.2) return 100;
  if (voltage <= 3.0) return 0;
  return (int)((voltage - 3.0) / 1.2 * 100);
}
```

### 3. Low Battery Detection
```cpp
void setupBattery() {
  pinMode(LOW_BATT_PIN, INPUT_PULLUP);
  analogReadResolution(12);  // 12-bit ADC (0-4095)
}

bool isBatteryCritical() {
  return digitalRead(LOW_BATT_PIN) == LOW;
}
```

### 4. Graceful Shutdown on Low Battery
```cpp
void handleLowBattery() {
  if (isBatteryCritical()) {
    // Flush SD card buffers
    dataFile.flush();
    dataFile.close();

    // Display warning on OLED
    display.clearDisplay();
    display.setTextSize(2);
    display.setCursor(0, 20);
    display.println("LOW BATTERY");
    display.println("SHUTTING DOWN");
    display.display();

    delay(3000);

    // Enter deep sleep to preserve remaining power
    esp_deep_sleep_start();
  }
}
```

### 5. OLED Battery Display
Add a battery indicator to the existing OLED status screen:
```cpp
void drawBatteryIcon(float voltage, int percent) {
  // Draw in top-right corner of 128x64 OLED
  int x = 100, y = 0;
  int barWidth = map(percent, 0, 100, 0, 20);

  display.drawRect(x, y, 24, 10, SSD1306_WHITE);       // Battery outline
  display.fillRect(x + 24, y + 3, 3, 4, SSD1306_WHITE); // Battery tip
  display.fillRect(x + 2, y + 2, barWidth, 6, SSD1306_WHITE); // Fill level

  // Show percentage below icon
  display.setTextSize(1);
  display.setCursor(x, y + 12);
  display.print(percent);
  display.print("%");

  // Show warning if critical
  if (isBatteryCritical()) {
    display.setCursor(x - 20, y + 12);
    display.print("LOW!");
  }
}
```

### 6. Integration into Main Loop
```cpp
void loop() {
  // ... existing sensor reading, GPS, IMU code ...

  // Battery monitoring (read every 10 seconds to avoid ADC noise)
  static unsigned long lastBattCheck = 0;
  if (millis() - lastBattCheck > 10000) {
    float battVoltage = getBatteryVoltage();
    int battPercent = getBatteryPercent(battVoltage);
    drawBatteryIcon(battVoltage, battPercent);

    // Log battery voltage to CSV
    // Add battVoltage as additional column in CSV row

    // Check for critical low battery
    handleLowBattery();

    lastBattCheck = millis();
  }

  // ... existing OLED update, SD write code ...
}
```

### 7. CSV Logging Update
Add battery voltage as a new column in the CSV output:
```
timestamp, lat, lon, speed, heading, roll, pitch, yaw, battV, battPct
```

---

## Power Budget (Updated)

| Component | Current Draw |
|---|---|
| ESP32 (Wi-Fi off) | ~50 mA |
| ESP32 (Wi-Fi on, upload) | ~150-200 mA |
| Waveshare LG290P GNSS | ~200 mA |
| BNO085 IMU | ~10 mA |
| SSD1309 OLED 2.42" | ~20 mA |
| SD card (writing) | ~30-80 mA |
| Voltage divider (200Ω) | ~10.5 mA |
| **Total sailing (Wi-Fi off)** | **~320-370 mA** |
| **Total upload (Wi-Fi on)** | **~410-510 mA** |

### Runtime Estimate
- 6600 mAh battery / 370 mA = ~17-18 hours sailing
- Charge time at 1A = ~7 hours

---

## Diagnostic Notes

### Investigating Previous Shutdowns
The old 18650 spring-contact battery holder likely caused the shutdowns during sailing. To confirm, check SD card logs for:
1. Number of separate log files (each boot creates new file) = number of restarts
2. Truncated/corrupted last line before gap = sudden power loss
3. GNSS fix state after restart (cold start TTFF = full power loss vs warm start = ESP32 reset only)
4. Timing pattern of shutdowns (correlated with choppy water / vibration)

With the new soldered PKCELL pack + PowerBoost, vibration-induced power loss should be eliminated.
