#!/usr/bin/env python3
"""Test Calypso Mini BLE wind sensor - scan and read data."""

import asyncio

print("=== SailFrames Wind Sensor Test ===\n")

try:
    from bleak import BleakScanner, BleakClient
except ImportError:
    print("✗ Missing library: bleak")
    print("  Run: pip3 install bleak --break-system-packages")
    exit(1)


CALYPSO_WIND_CHAR_UUID = "0000a001-0000-1000-8000-00805f9b34fb"


async def scan():
    print("Scanning for BLE devices (30 seconds)...\n")
    devices = await BleakScanner.discover(timeout=30)

    calypso_found = None
    print(f"Found {len(devices)} BLE devices:\n")

    for d in devices:
        name = d.name or "Unknown"
        is_calypso = 'calypso' in name.lower() or 'ultrasonic' in name.lower()
        marker = " ← WIND SENSOR" if is_calypso else ""
        rssi = getattr(d, 'rssi', None) or '?'
        print(f"  {d.address}  RSSI={rssi}  {name}{marker}")
        if is_calypso:
            calypso_found = d

    if calypso_found:
        print(f"\n✓ Calypso found: {calypso_found.name} at {calypso_found.address}")
        print(f"\n  Set this in config: ble_mac_address: \"{calypso_found.address}\"")

        # Try to connect and read
        print(f"\n  Attempting to connect...")
        try:
            async with BleakClient(calypso_found.address, timeout=20) as client:
                print(f"  ✓ Connected!")

                # List services
                print(f"\n  Available services:")
                for service in client.services:
                    print(f"    {service.uuid}: {service.description}")
                    for char in service.characteristics:
                        props = ','.join(char.properties)
                        print(f"      {char.uuid} [{props}]")

                # Find all notify-capable characteristics
                notify_chars = []
                for service in client.services:
                    for char in service.characteristics:
                        if 'notify' in char.properties:
                            notify_chars.append(char)

                print(f"\n  Found {len(notify_chars)} notify-capable characteristic(s)")

                # Try to read wind data from each notify characteristic
                for char in notify_chars:
                    print(f"\n  Trying {char.uuid}...")
                    data_received = []

                    def callback(sender, data):
                        data_received.append(data)
                        print(f"    Received: {data.hex()} ({len(data)} bytes)")

                    try:
                        await client.start_notify(char.uuid, callback)
                        await asyncio.sleep(5)
                        await client.stop_notify(char.uuid)

                        if data_received:
                            print(f"    ✓ Got {len(data_received)} packets from {char.uuid}")
                        else:
                            print(f"    No data from {char.uuid}")
                    except Exception as e:
                        print(f"    Error: {e}")

        except Exception as e:
            print(f"  ✗ Connection failed: {e}")
    else:
        print("\n✗ No Calypso device found.")
        print("  Make sure the Calypso Mini is:")
        print("  - Powered on (blue LED)")
        print("  - Not connected to another device (phone app, etc.)")
        print("  - Within 30m range")


asyncio.run(scan())
