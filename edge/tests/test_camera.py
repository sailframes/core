#!/usr/bin/env python3
"""Test Pi Camera Module 3 Wide - capture a test image and short video."""

import time
from pathlib import Path

print("=== SailFrames Camera Test ===\n")

try:
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder
    from picamera2.outputs import FfmpegOutput
except ImportError as e:
    print(f"✗ Missing library: {e}")
    print("  picamera2 should be pre-installed on Raspberry Pi OS")
    print("  Run: sudo apt install python3-picamera2")
    exit(1)

try:
    picam2 = Picamera2()
    print("✓ Camera detected")
except Exception as e:
    print(f"✗ Camera not found: {e}")
    print("  Check: CSI ribbon cable connected, camera enabled in raspi-config")
    exit(1)

# Capture a test image
print("\nCapturing test image...")
picam2.configure(picam2.create_still_configuration())
picam2.start()
time.sleep(2)  # Let auto-exposure settle

test_img = Path('/tmp/sailframes_test.jpg')
picam2.capture_file(str(test_img))
size_kb = test_img.stat().st_size / 1024
print(f"✓ Image saved: {test_img} ({size_kb:.0f}KB)")

# Get camera properties
metadata = picam2.capture_metadata()
print(f"  Exposure: {metadata.get('ExposureTime', '?')}µs")
print(f"  Gain: {metadata.get('AnalogueGain', '?')}")
print(f"  Focus: {metadata.get('FocusFoM', '?')}")

picam2.stop()

# Capture a short test video
print("\nRecording 5-second test video at 1080p/30fps...")
video_config = picam2.create_video_configuration(
    main={"size": (1920, 1080), "format": "RGB888"},
    controls={"FrameRate": 30}
)
picam2.configure(video_config)
picam2.start()
time.sleep(1)

test_vid = Path('/tmp/sailframes_test.mp4')
encoder = H264Encoder(bitrate=8_000_000)
output = FfmpegOutput(str(test_vid))
picam2.start_recording(encoder, output)
time.sleep(5)
picam2.stop_recording()
picam2.stop()

size_mb = test_vid.stat().st_size / (1024 * 1024)
print(f"✓ Video saved: {test_vid} ({size_mb:.1f}MB)")
print(f"  Expected rate: ~{size_mb / 5 * 3600:.0f}MB/hour")

print(f"\n✓ Camera working! Test files in /tmp/")
print(f"  View image: scp pi@$(hostname -I | awk '{{print $1}}'):/tmp/sailframes_test.jpg .")
