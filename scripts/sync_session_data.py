#!/usr/bin/env python3
"""
Sync and index GoPro videos with E1 sensor data.

Creates a session manifest JSON that maps video timestamps to sensor data files,
enabling synchronized playback and analysis.

Usage:
    python sync_session_data.py 2026-04-02 --upload
    python sync_session_data.py 2026-04-03 --upload
"""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class VideoSegment:
    filename: str
    start_time: datetime  # UTC
    duration_sec: float
    end_time: datetime  # UTC
    s3_path: Optional[str] = None
    local_path: Optional[str] = None


@dataclass
class SensorFile:
    filename: str
    sensor_type: str  # nav, imu, wind, rtcm3
    boot_num: int
    start_time: datetime  # UTC (from filename)
    s3_path: str
    file_size: int


@dataclass
class SessionManifest:
    date: str
    device_id: str
    videos: list[dict]
    sensors: list[dict]
    timeline_start: str  # ISO format
    timeline_end: str  # ISO format
    video_coverage_start: Optional[str] = None
    video_coverage_end: Optional[str] = None
    sync_created: str = ""


def parse_gopro_metadata(video_path: Path) -> Optional[VideoSegment]:
    """Extract creation time and duration from GoPro video."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration:format_tags=creation_time",
                "-of", "json", str(video_path)
            ],
            capture_output=True, text=True, check=True
        )
        data = json.loads(result.stdout)
        fmt = data.get("format", {})

        creation_str = fmt.get("tags", {}).get("creation_time")
        duration_str = fmt.get("duration")

        if not creation_str or not duration_str:
            return None

        # Parse creation time (format: 2026-04-03T14:39:49.000000Z)
        start_time = datetime.fromisoformat(creation_str.replace("Z", "+00:00"))
        duration = float(duration_str)
        end_time = datetime.fromtimestamp(start_time.timestamp() + duration, tz=timezone.utc)

        return VideoSegment(
            filename=video_path.name,
            start_time=start_time,
            duration_sec=duration,
            end_time=end_time,
            local_path=str(video_path)
        )
    except Exception as e:
        print(f"Warning: Could not parse {video_path.name}: {e}", file=sys.stderr)
        return None


def parse_sensor_filename(filename: str, date_str: str) -> Optional[SensorFile]:
    """Parse E1 sensor filename to extract metadata."""
    # Pattern: E1_boot{N}_{HHMMSS}_{type}.{ext}
    # or: E1_boot{N}_{HHMMSS}.rtcm3

    patterns = [
        (r"E1_boot(\d+)_(\d{6})_(nav|imu|wind)\.csv", None),
        (r"E1_boot(\d+)_(\d{6})\.(rtcm3)", "rtcm3"),
    ]

    for pattern, fixed_type in patterns:
        m = re.match(pattern, filename)
        if m:
            boot_num = int(m.group(1))
            time_str = m.group(2)
            sensor_type = fixed_type if fixed_type else m.group(3)

            # Parse time from filename
            h, m_val, s = int(time_str[0:2]), int(time_str[2:4]), int(time_str[4:6])

            # Create datetime (assume UTC, use date from argument)
            date_parts = date_str.split("-")
            start_time = datetime(
                int(date_parts[0]), int(date_parts[1]), int(date_parts[2]),
                h, m_val, s, tzinfo=timezone.utc
            )

            return SensorFile(
                filename=filename,
                sensor_type=sensor_type,
                boot_num=boot_num,
                start_time=start_time,
                s3_path="",  # Will be filled in
                file_size=0
            )

    return None


def list_s3_sensor_files(date_str: str, profile: str, bucket: str) -> list[SensorFile]:
    """List E1 sensor files from S3."""
    prefix = f"raw/E1/{date_str}/"

    try:
        result = subprocess.run(
            ["aws", "s3", "ls", f"s3://{bucket}/{prefix}",
             "--profile", profile],
            capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError:
        return []

    sensors = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        # Format: 2026-04-02 10:57:36     149834 raw/E1/2026-04-02/filename
        parts = line.split()
        if len(parts) >= 4:
            size = int(parts[2])
            filename = parts[3]

            sensor = parse_sensor_filename(filename, date_str)
            if sensor:
                sensor.s3_path = f"s3://{bucket}/{prefix}{filename}"
                sensor.file_size = size
                sensors.append(sensor)

    return sensors


def list_s3_gopro_videos(date_str: str, profile: str, bucket: str) -> list[str]:
    """List GoPro videos in S3."""
    prefix = f"raw/gopro/{date_str}/video/"

    try:
        result = subprocess.run(
            ["aws", "s3", "ls", f"s3://{bucket}/{prefix}",
             "--profile", profile],
            capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError:
        return []

    videos = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) >= 4 and parts[3].upper().endswith(".MP4"):
            videos.append(parts[3])

    return videos


def find_overlapping_sensors(
    videos: list[VideoSegment],
    sensors: list[SensorFile]
) -> list[SensorFile]:
    """Find sensor files that overlap with video time range."""
    if not videos:
        return sensors

    video_start = min(v.start_time for v in videos)
    video_end = max(v.end_time for v in videos)

    # Add buffer (30 min before, 30 min after)
    from datetime import timedelta
    buffer = timedelta(minutes=30)
    range_start = video_start - buffer
    range_end = video_end + buffer

    overlapping = []
    for s in sensors:
        # Sensor start time within extended range
        if range_start <= s.start_time <= range_end:
            overlapping.append(s)

    return overlapping


def create_manifest(
    date_str: str,
    videos: list[VideoSegment],
    sensors: list[SensorFile],
    device_id: str = "E1"
) -> SessionManifest:
    """Create a session manifest combining video and sensor data."""

    # Determine timeline bounds
    all_times = [s.start_time for s in sensors]
    if videos:
        all_times.extend([v.start_time for v in videos])
        all_times.extend([v.end_time for v in videos])

    timeline_start = min(all_times) if all_times else datetime.now(timezone.utc)
    timeline_end = max(all_times) if all_times else datetime.now(timezone.utc)

    video_dicts = []
    for v in videos:
        video_dicts.append({
            "filename": v.filename,
            "start_time": v.start_time.isoformat(),
            "end_time": v.end_time.isoformat(),
            "duration_sec": v.duration_sec,
            "s3_path": v.s3_path,
        })

    sensor_dicts = []
    for s in sensors:
        sensor_dicts.append({
            "filename": s.filename,
            "sensor_type": s.sensor_type,
            "boot_num": s.boot_num,
            "start_time": s.start_time.isoformat(),
            "s3_path": s.s3_path,
            "file_size": s.file_size,
        })

    # Sort sensors by start time
    sensor_dicts.sort(key=lambda x: x["start_time"])
    video_dicts.sort(key=lambda x: x["start_time"])

    manifest = SessionManifest(
        date=date_str,
        device_id=device_id,
        videos=video_dicts,
        sensors=sensor_dicts,
        timeline_start=timeline_start.isoformat(),
        timeline_end=timeline_end.isoformat(),
        video_coverage_start=videos[0].start_time.isoformat() if videos else None,
        video_coverage_end=videos[-1].end_time.isoformat() if videos else None,
        sync_created=datetime.now(timezone.utc).isoformat(),
    )

    return manifest


def main():
    parser = argparse.ArgumentParser(description="Sync GoPro videos with E1 sensor data")
    parser.add_argument("date", help="Session date (YYYY-MM-DD)")
    parser.add_argument("--upload", action="store_true", help="Upload manifest to S3")
    parser.add_argument("--profile", default="sailframes", help="AWS profile")
    parser.add_argument("--bucket", default="sailframes-fleet-data-prod", help="S3 bucket")
    parser.add_argument("--data-dir", default="/Users/paul2/sailframes/data",
                        help="Local data directory")
    parser.add_argument("--output", help="Output manifest file (default: stdout or S3)")
    args = parser.parse_args()

    date_str = args.date

    print(f"Syncing session data for {date_str}", file=sys.stderr)
    print("=" * 50, file=sys.stderr)

    # 1. Find local GoPro videos and extract metadata
    gopro_dir = Path(args.data_dir) / f"sail_{date_str}" / "GoPro" / "DCIM" / "100GOPRO"
    videos = []

    if gopro_dir.exists():
        print(f"\nScanning local GoPro videos: {gopro_dir}", file=sys.stderr)
        for mp4_file in sorted(gopro_dir.glob("*.MP4")):
            segment = parse_gopro_metadata(mp4_file)
            if segment:
                videos.append(segment)
                print(f"  {segment.filename}: {segment.start_time.strftime('%H:%M:%S')} - "
                      f"{segment.end_time.strftime('%H:%M:%S')} ({segment.duration_sec:.1f}s)",
                      file=sys.stderr)
    else:
        print(f"\nNo local GoPro directory found: {gopro_dir}", file=sys.stderr)

    # 2. Check S3 for GoPro videos
    s3_videos = list_s3_gopro_videos(date_str, args.profile, args.bucket)
    if s3_videos:
        print(f"\nGoPro videos in S3: {len(s3_videos)}", file=sys.stderr)
        for v in s3_videos:
            # Update s3_path for matching videos
            for video in videos:
                if video.filename.upper() == v.upper():
                    video.s3_path = f"s3://{args.bucket}/raw/gopro/{date_str}/video/{v}"
            print(f"  {v}", file=sys.stderr)
    else:
        print(f"\nNo GoPro videos found in S3 for {date_str}", file=sys.stderr)

    # 3. List E1 sensor files from S3
    print(f"\nFetching E1 sensor files from S3...", file=sys.stderr)
    sensors = list_s3_sensor_files(date_str, args.profile, args.bucket)
    print(f"Found {len(sensors)} sensor files", file=sys.stderr)

    # Group by sensor type
    by_type = {}
    for s in sensors:
        by_type.setdefault(s.sensor_type, []).append(s)
    for stype, files in sorted(by_type.items()):
        print(f"  {stype}: {len(files)} files", file=sys.stderr)

    # 4. Find sensors that overlap with video timeline
    if videos:
        overlapping = find_overlapping_sensors(videos, sensors)
        print(f"\nSensor files overlapping with video: {len(overlapping)}", file=sys.stderr)
    else:
        overlapping = sensors

    # 5. Create manifest
    print(f"\nCreating session manifest...", file=sys.stderr)
    manifest = create_manifest(date_str, videos, overlapping)

    manifest_json = json.dumps(asdict(manifest), indent=2)

    # 6. Output or upload
    if args.output:
        output_path = Path(args.output)
        output_path.write_text(manifest_json)
        print(f"\nManifest written to: {output_path}", file=sys.stderr)

    if args.upload:
        s3_manifest_path = f"s3://{args.bucket}/processed/{date_str}/session_manifest.json"
        print(f"\nUploading manifest to: {s3_manifest_path}", file=sys.stderr)

        # Write to temp file and upload
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write(manifest_json)
            temp_path = f.name

        try:
            subprocess.run(
                ["aws", "s3", "cp", temp_path, s3_manifest_path,
                 "--profile", args.profile],
                check=True
            )
            print(f"Uploaded successfully!", file=sys.stderr)
        finally:
            os.unlink(temp_path)

    if not args.output and not args.upload:
        # Print to stdout
        print(manifest_json)

    # Print summary
    print(f"\n" + "=" * 50, file=sys.stderr)
    print(f"Session Summary for {date_str}:", file=sys.stderr)
    print(f"  Videos: {len(manifest.videos)}", file=sys.stderr)
    print(f"  Sensor files: {len(manifest.sensors)}", file=sys.stderr)
    if manifest.video_coverage_start:
        print(f"  Video coverage: {manifest.video_coverage_start} - {manifest.video_coverage_end}",
              file=sys.stderr)
    print(f"  Timeline: {manifest.timeline_start} - {manifest.timeline_end}", file=sys.stderr)


if __name__ == "__main__":
    main()
