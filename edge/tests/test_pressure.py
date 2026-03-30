#!/usr/bin/env python3
"""Test DPS310 pressure sensor - verify I2C connection and readings."""

import time

print("=== SailFrames Pressure Test ===\n")

try:
    import board
    import busio
    import adafruit_dps310
except ImportError as e:
    print(f"✗ Missing library: {e}")
    print("  Run: pip3 install adafruit-circuitpython-dps310 --break-system-packages")
    exit(1)

try:
    i2c = busio.I2C(board.SCL, board.SDA, frequency=400000)
    print("✓ I2C bus initialized")
except Exception as e:
    print(f"✗ I2C bus error: {e}")
    exit(1)

try:
    dps = adafruit_dps310.DPS310(i2c, address=0x77)
    print("✓ DPS310 connected at address 0x77")
except Exception as e:
    print(f"✗ DPS310 not found: {e}")
    print("  Check I2C daisy chain from BNO085 to DPS310")
    print("  Run: i2cdetect -y 1  (should show 77)")
    exit(1)

# Configure for highest precision
dps.pressure_rate = adafruit_dps310.Rate.RATE_1_HZ
dps.pressure_oversample_count = adafruit_dps310.SampleCount.COUNT_128
dps.temperature_rate = adafruit_dps310.Rate.RATE_1_HZ
dps.temperature_oversample_count = adafruit_dps310.SampleCount.COUNT_128
dps.mode = adafruit_dps310.Mode.CONT_PRESTEMP
print("✓ Configured for high-precision mode\n")

print("Reading 10 samples...\n")
print(f"{'Pressure':>12}  {'Temp':>7}  {'Sea Level':>12}")
print("-" * 36)

for _ in range(10):
    time.sleep(1)
    pressure = dps.pressure
    temperature = dps.temperature

    # Sea level correction (2m deck height)
    sea_level = pressure * (
        1 - (0.0065 * 2.0) / (temperature + 273.15 + 0.0065 * 2.0)
    ) ** -5.257

    print(f"{pressure:12.4f} hPa  {temperature:5.1f}°C  {sea_level:10.3f} hPa")

print(f"\n✓ DPS310 working! Precision: ±0.002 hPa")
