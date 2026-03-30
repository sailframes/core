#!/usr/bin/env python3
"""
Configure u-blox ZED-F9P for SailFrames
- Enable NMEA GSA sentences (satellite usage per constellation)
- Enable UBX-RXM-RAWX (raw measurements for RTKLib)
- Enable UBX-RXM-SFRBX (navigation data for RTKLib)
- Save configuration to flash
"""

import serial
import struct
import time
import sys

def ubx_checksum(payload):
    """Calculate UBX checksum (8-bit Fletcher)."""
    ck_a, ck_b = 0, 0
    for b in payload:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return bytes([ck_a, ck_b])

def ubx_message(msg_class, msg_id, payload=b''):
    """Build a complete UBX message."""
    header = bytes([0xB5, 0x62])  # Sync chars
    length = struct.pack('<H', len(payload))
    msg_header = bytes([msg_class, msg_id])
    checksum = ubx_checksum(msg_header + length + payload)
    return header + msg_header + length + payload + checksum

def cfg_msg(msg_class, msg_id, rate):
    """Create UBX-CFG-MSG to set message rate."""
    # Rate for current port (USB)
    payload = bytes([msg_class, msg_id, rate])
    return ubx_message(0x06, 0x01, payload)

def cfg_valset(key_id, value, layers=0x01):
    """Create UBX-CFG-VALSET message (ZED-F9P configuration)."""
    # Version 0, layers (1=RAM, 2=BBR, 4=Flash), reserved
    header = bytes([0x00, layers, 0x00, 0x00])
    # Key ID (4 bytes little endian) + value
    key_bytes = struct.pack('<I', key_id)
    if isinstance(value, bool):
        val_bytes = bytes([1 if value else 0])
    elif isinstance(value, int):
        val_bytes = bytes([value])
    else:
        val_bytes = value
    payload = header + key_bytes + val_bytes
    return ubx_message(0x06, 0x8A, payload)

def cfg_save():
    """Create UBX-CFG-CFG to save config to flash."""
    # Save all sections to flash
    clear_mask = struct.pack('<I', 0x00000000)
    save_mask = struct.pack('<I', 0x0000FFFF)  # Save all
    load_mask = struct.pack('<I', 0x00000000)
    device_mask = bytes([0x17])  # All devices
    payload = clear_mask + save_mask + load_mask + device_mask
    return ubx_message(0x06, 0x09, payload)

def send_and_wait(ser, msg, description, wait=0.1):
    """Send UBX message and wait for processing."""
    print(f"  Configuring: {description}...", end=" ", flush=True)
    ser.write(msg)
    time.sleep(wait)
    # Read any response (ACK/NAK)
    response = ser.read(ser.in_waiting or 1)
    if b'\xb5\x62\x05\x01' in response:
        print("OK (ACK)")
        return True
    elif b'\xb5\x62\x05\x00' in response:
        print("FAILED (NAK)")
        return False
    else:
        print("OK (no response)")
        return True

def main():
    device = sys.argv[1] if len(sys.argv) > 1 else '/dev/sailframes-gps'
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else 115200

    print(f"Configuring ZED-F9P on {device} at {baud} baud")
    print("=" * 50)

    try:
        ser = serial.Serial(device, baud, timeout=1)
    except Exception as e:
        print(f"Error opening {device}: {e}")
        sys.exit(1)

    time.sleep(0.5)  # Wait for port to settle
    ser.reset_input_buffer()

    # Configuration items (using CFG-VALSET for ZED-F9P)
    # Key IDs from u-blox ZED-F9P interface description

    configs = [
        # NMEA GSA messages (per constellation)
        ("NMEA-GSA on USB", 0x209100c1, 1),      # CFG-MSGOUT-NMEA_ID_GSA_USB

        # UBX-RXM-RAWX (raw measurements for PPK)
        ("UBX-RXM-RAWX on USB", 0x209102a5, 1),  # CFG-MSGOUT-UBX_RXM_RAWX_USB

        # UBX-RXM-SFRBX (subframe/navigation data)
        ("UBX-RXM-SFRBX on USB", 0x20910232, 1), # CFG-MSGOUT-UBX_RXM_SFRBX_USB

        # Also enable on UART1 for redundancy
        ("NMEA-GSA on UART1", 0x209100bf, 1),    # CFG-MSGOUT-NMEA_ID_GSA_UART1
        ("UBX-RXM-RAWX on UART1", 0x209102a4, 1), # CFG-MSGOUT-UBX_RXM_RAWX_UART1
        ("UBX-RXM-SFRBX on UART1", 0x20910231, 1), # CFG-MSGOUT-UBX_RXM_SFRBX_UART1
    ]

    print("\n1. Enabling messages:")
    success_count = 0
    for desc, key_id, value in configs:
        # Save to RAM + BBR + Flash (layers = 0x07)
        msg = cfg_valset(key_id, value, layers=0x07)
        if send_and_wait(ser, msg, desc):
            success_count += 1

    print(f"\n   {success_count}/{len(configs)} messages configured")

    # Verify by reading a few lines
    print("\n2. Verifying output (5 seconds):")
    ser.reset_input_buffer()

    seen_gsa = False
    seen_rawx = False
    start = time.time()

    while time.time() - start < 5:
        if ser.in_waiting:
            data = ser.read(ser.in_waiting)
            # Check for NMEA GSA
            if b'GSA' in data:
                if not seen_gsa:
                    print("   ✓ NMEA GSA detected")
                    seen_gsa = True
            # Check for UBX-RXM-RAWX (0x02 0x15)
            if b'\xb5\x62\x02\x15' in data:
                if not seen_rawx:
                    print("   ✓ UBX-RXM-RAWX detected")
                    seen_rawx = True
            # Check for UBX-RXM-SFRBX (0x02 0x13)
            if b'\xb5\x62\x02\x13' in data:
                print("   ✓ UBX-RXM-SFRBX detected")
        time.sleep(0.1)

    if not seen_gsa:
        print("   ⚠ NMEA GSA not detected (may need restart)")
    if not seen_rawx:
        print("   ⚠ UBX-RXM-RAWX not detected (may need restart)")

    print("\n3. Configuration saved to flash")
    print("=" * 50)
    print("Done! Restart GPS service to apply changes:")
    print("  sudo systemctl restart sailframes-gps")
    print("\nFor RTKLib post-processing, raw UBX data will be in the")
    print("serial stream. Consider logging to separate .ubx file.")

    ser.close()

if __name__ == '__main__':
    main()
