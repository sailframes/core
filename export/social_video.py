"""Social media video export with data overlay.

Creates vertical (9:16) videos with speed, wind, and heading
data overlaid on race footage using FFmpeg.
"""

import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent


def generate_social_video(
    video_path: str,
    analysis_path: str,
    output_path: str | None = None,
    duration_sec: int = 60,
    start_offset_sec: float = 0,
    resolution: str = "1080x1920",
):
    """Generate a vertical social media video with data overlay.

    Args:
        video_path: Source race video file.
        analysis_path: Path to analysis.json.
        output_path: Output video path.
        duration_sec: Clip duration in seconds.
        start_offset_sec: Start offset into the video.
        resolution: Output resolution (WxH).
    """
    video_path = Path(video_path)
    analysis_path = Path(analysis_path)

    if output_path is None:
        output_path = video_path.parent / f"{video_path.stem}_social.mp4"

    data = json.loads(analysis_path.read_text())
    stats = data.get("session_stats", {})
    summary = data.get("maneuver_summary", {})

    width, height = resolution.split("x")

    # Build FFmpeg drawtext filter chain for data overlay
    overlay_lines = _build_overlay_text(stats, summary)

    filter_complex = _build_filter(
        int(width), int(height), overlay_lines
    )

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_offset_sec),
        "-i", str(video_path),
        "-t", str(duration_sec),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    print(f"Generating social video: {output_path}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"FFmpeg error: {result.stderr}", file=sys.stderr)
        return None

    print(f"Social video saved to {output_path}")
    return str(output_path)


def _build_overlay_text(stats: dict, summary: dict) -> list[str]:
    """Build overlay text lines from analysis stats."""
    lines = ["SailFrames"]

    if stats.get("speed"):
        lines.append(f"Max Speed: {stats['speed']['max']} kts")
        lines.append(f"Avg Speed: {stats['speed']['mean']} kts")

    if stats.get("apparent_wind_speed"):
        lines.append(f"Wind: {stats['apparent_wind_speed']['mean']} kts")

    if summary.get("tacks", {}).get("count"):
        lines.append(f"Tacks: {summary['tacks']['count']}")

    if stats.get("heel"):
        lines.append(f"Max Heel: {stats['heel']['max']}°")

    return lines


def _build_filter(width: int, height: int, text_lines: list[str]) -> str:
    """Build FFmpeg filter_complex string for vertical video with overlay."""
    # Crop and scale source to vertical
    filters = [
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}[scaled]"
    ]

    # Semi-transparent overlay bar at bottom
    filters.append(
        f"[scaled]drawbox=x=0:y={height-200}:w={width}:h=200:"
        f"color=black@0.6:t=fill[bar]"
    )

    # Add text lines
    prev = "bar"
    for i, line in enumerate(text_lines):
        label = f"txt{i}"
        y_pos = height - 180 + i * 28
        font_size = 22 if i == 0 else 18
        escaped = line.replace(":", "\\:")
        filters.append(
            f"[{prev}]drawtext=text='{escaped}':"
            f"fontsize={font_size}:fontcolor=white:"
            f"x=40:y={y_pos}:fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            f"[{label}]"
        )
        prev = label

    filters.append(f"[{prev}]copy[out]")

    return ";".join(filters)


def main():
    if len(sys.argv) < 3:
        print("Usage: python social_video.py <video.mp4> <analysis.json> [output.mp4]")
        sys.exit(1)

    video = sys.argv[1]
    analysis = sys.argv[2]
    output = sys.argv[3] if len(sys.argv) > 3 else None

    generate_social_video(video, analysis, output)


if __name__ == "__main__":
    main()
