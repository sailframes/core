"""Session analysis runner.

Loads raw sensor data and runs the full processing pipeline,
saving results alongside the processed data.
"""

import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _to_timestamp(t) -> float:
    """Convert ISO string or datetime to Unix timestamp."""
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, str):
        t = t.replace("Z", "+00:00")
        return datetime.fromisoformat(t).timestamp()
    if isinstance(t, datetime):
        return t.timestamp()
    return 0.0

from processing.maneuvers import detect_maneuvers, maneuver_summary
from processing.models import GpsPoint, ImuReading, SessionMetadata, WindReading
from processing.polar import generate_polar, polar_to_chart_data
from processing.stats import (
    correlation_matrix,
    leg_performance_ranking,
    session_statistics,
    violin_plot_data,
)
from processing.straight_lines import leg_comparison, segment_legs
from processing.vmg import compute_vmg_series
from processing.wind import compute_true_wind_series, estimate_wind_from_gps


def load_sensor_json(path: Path) -> list[dict]:
    """Load sensor JSON file."""
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return data if isinstance(data, list) else data.get("data", [])


def parse_gps(records: list[dict]) -> list[GpsPoint]:
    return [GpsPoint(
        timestamp=_to_timestamp(r.get("timestamp", r.get("t", ""))),
        lat=r.get("lat", r.get("latitude", 0)),
        lon=r.get("lon", r.get("longitude", 0)),
        speed_kts=r.get("speed_kts", r.get("speed_kn", r.get("speed", 0))),
        heading_deg=r.get("heading_deg", r.get("course", r.get("heading", 0))),
        fix_quality=r.get("fix_quality", r.get("fix", 0)),
    ) for r in records if "timestamp" in r or "t" in r]


def parse_imu(records: list[dict]) -> list[ImuReading]:
    return [ImuReading(
        timestamp=_to_timestamp(r.get("timestamp", r.get("t", ""))),
        heading_deg=r.get("heading_deg", r.get("heading", 0)),
        pitch_deg=r.get("pitch_deg", r.get("pitch", 0)),
        heel_deg=r.get("heel_deg", r.get("heel", 0)),
        accel_x=r.get("accel_x", 0),
        accel_y=r.get("accel_y", 0),
        accel_z=r.get("accel_z", 0),
    ) for r in records if "timestamp" in r or "t" in r]


def parse_wind(records: list[dict]) -> list[WindReading]:
    return [WindReading(
        timestamp=_to_timestamp(r.get("timestamp", r.get("t", ""))),
        apparent_speed_kts=r.get("apparent_speed_kts", r.get("aws_kn", r.get("speed_kts", 0))),
        apparent_angle_deg=r.get("apparent_angle_deg", r.get("awa", r.get("angle_deg", 0))),
    ) for r in records if "timestamp" in r or "t" in r]


def analyze_session(data_dir: Path) -> dict:
    """Run full analysis pipeline on a session directory.

    Expects directory structure:
        data_dir/
            gps.json
            imu.json
            wind.json
            pressure.json
            manifest.json
    """
    gps = parse_gps(load_sensor_json(data_dir / "gps.json"))
    imu = parse_imu(load_sensor_json(data_dir / "imu.json"))
    wind = parse_wind(load_sensor_json(data_dir / "wind.json"))

    if not gps:
        return {"error": "No GPS data found"}

    # True wind calculation
    true_wind = compute_true_wind_series(gps, wind, imu)

    # Estimate wind direction if no wind sensor data
    twd_estimate = None
    if not true_wind:
        est = estimate_wind_from_gps(gps)
        if est:
            twd_estimate = est[0]

    avg_twd = None
    if true_wind:
        avg_twd = float(np.mean([tw["twd_deg"] for tw in true_wind]))

    # Maneuver detection
    maneuvers = detect_maneuvers(gps, imu, avg_twd or twd_estimate)
    m_summary = maneuver_summary(maneuvers)

    # Leg segmentation
    legs = segment_legs(gps, maneuvers, true_wind, imu)
    l_comparison = leg_comparison(legs)

    # Polar diagram
    polar_points = generate_polar(gps, true_wind)
    polar_chart = polar_to_chart_data(polar_points)

    # VMG series
    vmg_series = compute_vmg_series(gps, true_wind)

    # Statistics
    sess_stats = session_statistics(gps, wind, imu)
    violin = violin_plot_data(maneuvers)
    correlations = correlation_matrix(gps, true_wind, imu)
    leg_ranking = leg_performance_ranking(legs)

    # Build result
    result = {
        "maneuvers": [asdict(m) for m in maneuvers],
        "maneuver_summary": m_summary,
        "legs": [asdict(l) for l in legs],
        "leg_comparison": l_comparison,
        "polar": polar_chart,
        "vmg_series": [asdict(v) for v in vmg_series],
        "true_wind": true_wind,
        "session_stats": sess_stats,
        "violin": violin,
        "correlations": correlations,
        "leg_ranking": leg_ranking,
    }

    return result


def main():
    """CLI entry point: analyze a session directory."""
    if len(sys.argv) < 2:
        print("Usage: python analyzer.py <session_data_dir>")
        sys.exit(1)

    data_dir = Path(sys.argv[1])
    if not data_dir.exists():
        print(f"Directory not found: {data_dir}")
        sys.exit(1)

    result = analyze_session(data_dir)

    output_path = data_dir / "analysis.json"
    output_path.write_text(json.dumps(result, indent=2))
    print(f"Analysis written to {output_path}")


if __name__ == "__main__":
    main()
