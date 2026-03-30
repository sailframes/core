#!/usr/bin/env python3
"""Test BNO085 IMU connection - verify I2C communication and sensor readings."""

import time
import math

print("=== SailFrames IMU Test ===\n")

try:
    import board
    import busio
    import adafruit_bno08x
    from adafruit_bno08x.i2c import BNO08X_I2C
except ImportError as e:
    print(f"✗ Missing library: {e}")
    print("  Run: pip3 install adafruit-circuitpython-bno08x adafruit-blinka --break-system-packages")
    exit(1)

# Check I2C bus
try:
    i2c = busio.I2C(board.SCL, board.SDA, frequency=400000)
    print("✓ I2C bus initialized at 400kHz")
except Exception as e:
    print(f"✗ I2C bus error: {e}")
    print("  Check: sudo raspi-config -> Interface Options -> I2C -> Enable")
    exit(1)

# Connect to BNO085
try:
    bno = BNO08X_I2C(i2c, address=0x4A)
    print("✓ BNO085 connected at address 0x4A")
except Exception as e:
    print(f"✗ BNO085 not found: {e}")
    print("  Check wiring: VIN→3.3V, GND→GND, SDA→GPIO2, SCL→GPIO3")
    print("  Run: i2cdetect -y 1  (should show 4a)")
    exit(1)

# Enable rotation vector
bno.enable_feature(adafruit_bno08x.BNO_REPORT_ROTATION_VECTOR)
bno.enable_feature(adafruit_bno08x.BNO_REPORT_LINEAR_ACCELERATION)
print("✓ Sensor reports enabled\n")

print("Reading 10 samples (hold the sensor steady)...\n")
print(f"{'Heading':>8}  {'Pitch':>7}  {'Heel':>7}  {'AccelX':>8}  {'AccelY':>8}  {'AccelZ':>8}")
print("-" * 58)

for _ in range(10):
    time.sleep(0.2)
    quat = bno.quaternion
    accel = bno.linear_acceleration

    if quat and all(q is not None for q in quat):
        i, j, k, real = quat

        # Euler conversion
        sinr = 2.0 * (real * i + j * k)
        cosr = 1.0 - 2.0 * (i * i + j * j)
        heel = math.degrees(math.atan2(sinr, cosr))

        sinp = 2.0 * (real * j - k * i)
        pitch = math.degrees(math.asin(max(-1, min(1, sinp))))

        siny = 2.0 * (real * k + i * j)
        cosy = 1.0 - 2.0 * (j * j + k * k)
        heading = math.degrees(math.atan2(siny, cosy))
        if heading < 0:
            heading += 360

        ax, ay, az = accel if accel else (0, 0, 0)
        print(f"{heading:8.1f}° {pitch:7.1f}° {heel:7.1f}° {ax:8.3f} {ay:8.3f} {az:8.3f}")

print(f"\n✓ BNO085 working! Tilt the sensor to see heel/pitch change.")
