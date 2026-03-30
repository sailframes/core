#!/usr/bin/env python3
"""Test GPS connection - run this to verify ZED-F9P is working."""

import serial
import time

PORTS = ['/dev/ttyACM0', '/dev/ttyACM1', '/dev/ttyUSB0']

print("=== SailFrames GPS Test ===\n")

for port in PORTS:
    try:
        ser = serial.Serial(port, 115200, timeout=2)
        print(f"✓ Found device on {port}")
        print("  Waiting for NMEA sentences...\n")

        count = 0
        start = time.time()
        while count < 10 and time.time() - start < 10:
            line = ser.readline().decode('ascii', errors='replace').strip()
            if line.startswith('$'):
                print(f"  {line}")
                count += 1

        ser.close()
        if count > 0:
            print(f"\n✓ GPS working! Received {count} NMEA sentences.")
        else:
            print("\n✗ GPS connected but no NMEA data. Check firmware/configuration.")
        break
    except Exception as e:
        continue
else:
    print("✗ No GPS device found on any port.")
    print("  Check USB connection and run: ls /dev/ttyACM* /dev/ttyUSB*")
