/*
 * SailFrames E1 Diagnostic Sketch
 * Tests: I2C bus, OLED, SD card, IMU
 */

#include <Wire.h>
#include <SPI.h>
#include <SD.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#define SDA_PIN       21
#define SCL_PIN       22
#define SD_CS_PIN     5
#define SCREEN_WIDTH  128
#define SCREEN_HEIGHT 64
#define OLED_ADDR     0x3C

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);
bool bnoFound = false;
bool sdOK = false;

void setup() {
  Serial.begin(115200);
  delay(2000);

  Serial.println("\n========================================");
  Serial.println("  SailFrames E1 Diagnostic v2");
  Serial.println("========================================\n");

  // I2C init
  Serial.println("=== I2C BUS SCAN ===");
  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);  // 100kHz standard
  delay(100);

  Serial.println("Scanning I2C (SDA=21, SCL=22)...");
  int found = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("  0x%02X - ", addr);
      if (addr == 0x3C) Serial.println("OLED");
      else if (addr == 0x4A) { Serial.println("BNO085"); bnoFound = true; }
      else if (addr == 0x4B) { Serial.println("BNO085 alt"); bnoFound = true; }
      else if (addr == 0x68) Serial.println("MPU6050/DS3231");
      else Serial.println("Unknown");
      found++;
    }
  }
  Serial.printf("Found %d device(s)\n\n", found);

  // OLED init
  Serial.println("=== OLED TEST ===");
  bool oledOK = display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR);
  if (oledOK) {
    Serial.println("  OLED OK");

    // Stop scrolling
    display.ssd1306_command(SSD1306_DEACTIVATE_SCROLL);
    display.ssd1306_command(SSD1306_SETSTARTLINE | 0x00);

    display.clearDisplay();
    display.display();
    delay(50);
  } else {
    Serial.println("  OLED FAILED");
  }
  Serial.println();

  // SD card test
  Serial.println("=== SD CARD TEST ===");
  Serial.println("Pins: CS=5, MOSI=23, MISO=19, CLK=18");
  SPI.begin(18, 19, 23, SD_CS_PIN);
  delay(100);

  sdOK = SD.begin(SD_CS_PIN, SPI, 4000000);
  if (sdOK) {
    Serial.println("  SD OK");
    Serial.printf("  Size: %llu MB\n", SD.cardSize() / (1024*1024));
  } else {
    Serial.println("  SD FAILED - check wiring and FAT32 format");
  }
  Serial.println();

  // Show results on display
  if (oledOK) {
    display.clearDisplay();
    display.setTextSize(2);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0, 0);
    display.println("E1 DIAG");

    display.setTextSize(1);
    display.setCursor(0, 24);
    display.print("OLED: OK");

    display.setCursor(0, 36);
    display.print("IMU:  ");
    display.print(bnoFound ? "OK" : "NONE");

    display.setCursor(0, 48);
    display.print("SD:   ");
    display.print(sdOK ? "OK" : "FAIL");

    display.display();
  }

  Serial.println("=== DONE ===");
  Serial.println(bnoFound ? "IMU: OK" : "IMU: NOT FOUND - check wiring!");
  Serial.println(sdOK ? "SD: OK" : "SD: FAILED - check wiring!");
}

void loop() {
  delay(10000);
}
