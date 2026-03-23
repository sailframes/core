"""Polar diagram generation from sailing data.

Builds polar performance curves by bucketing boat speed data
by true wind angle and true wind speed.
"""

import math
from collections import defaultdict

import numpy as np

from .models import GpsPoint, PolarPoint


# Bucket sizes for polar diagram
TWA_BUCKET_SIZE = 5  # degrees
TWS_BUCKET_SIZE = 2  # knots
MIN_SAMPLES_PER_BUCKET = 3


def generate_polar(
    gps: list[GpsPoint],
    true_wind: list[dict],
    use_max: bool = False,
) -> list[PolarPoint]:
    """Generate polar diagram data points.

    Args:
        gps: GPS track data.
        true_wind: True wind series from wind.compute_true_wind_series().
        use_max: If True, use max speed per bucket (target polar).
                 If False, use average speed (actual performance).

    Returns:
        List of PolarPoint for each TWA/TWS bucket.
    """
    if not true_wind:
        return []

    gps_times = np.array([p.timestamp for p in gps])
    gps_speeds = np.array([p.speed_kts for p in gps])

    # Collect data into TWA/TWS buckets
    buckets: dict[tuple[float, float], list[tuple[float, float]]] = defaultdict(list)

    for tw in true_wind:
        t = tw["timestamp"]
        if t < gps_times[0] or t > gps_times[-1]:
            continue

        twa = abs(tw["twa_deg"])  # symmetric polar
        tws = tw["tws_kts"]
        speed = float(np.interp(t, gps_times, gps_speeds))

        if speed < 0.5 or tws < 1.0:
            continue

        twa_bucket = round(twa / TWA_BUCKET_SIZE) * TWA_BUCKET_SIZE
        tws_bucket = round(tws / TWS_BUCKET_SIZE) * TWS_BUCKET_SIZE

        vmg = speed * abs(math.cos(math.radians(twa)))
        buckets[(twa_bucket, tws_bucket)].append((speed, vmg))

    # Build polar points
    points = []
    for (twa_b, tws_b), samples in buckets.items():
        if len(samples) < MIN_SAMPLES_PER_BUCKET:
            continue

        speeds, vmgs = zip(*samples)
        if use_max:
            boat_speed = max(speeds)
            vmg_val = max(vmgs)
        else:
            boat_speed = np.mean(speeds)
            vmg_val = np.mean(vmgs)

        points.append(PolarPoint(
            twa_deg=twa_b,
            tws_kts=tws_b,
            boat_speed_kts=round(float(boat_speed), 2),
            vmg_kts=round(float(vmg_val), 2),
            sample_count=len(samples),
        ))

    return sorted(points, key=lambda p: (p.tws_kts, p.twa_deg))


def polar_to_chart_data(points: list[PolarPoint]) -> dict:
    """Convert polar points to format suitable for frontend rendering.

    Returns dict with TWS values as keys, each containing
    arrays of (angle, speed) pairs for plotting.
    """
    by_tws = defaultdict(list)
    for p in points:
        by_tws[p.tws_kts].append({
            "angle": p.twa_deg,
            "speed": p.boat_speed_kts,
            "vmg": p.vmg_kts,
            "samples": p.sample_count,
        })

    chart_data = {}
    for tws in sorted(by_tws.keys()):
        data = sorted(by_tws[tws], key=lambda x: x["angle"])
        chart_data[str(tws)] = data

    return chart_data


def merge_polars(
    sessions_polars: list[list[PolarPoint]],
) -> list[PolarPoint]:
    """Merge polar data from multiple sessions into a composite polar."""
    all_buckets: dict[tuple[float, float], list[tuple[float, float, int]]] = defaultdict(list)

    for polar in sessions_polars:
        for p in polar:
            all_buckets[(p.twa_deg, p.tws_kts)].append(
                (p.boat_speed_kts, p.vmg_kts, p.sample_count)
            )

    merged = []
    for (twa, tws), entries in all_buckets.items():
        total_samples = sum(e[2] for e in entries)
        # Weighted average by sample count
        w_speed = sum(e[0] * e[2] for e in entries) / total_samples
        w_vmg = sum(e[1] * e[2] for e in entries) / total_samples

        merged.append(PolarPoint(
            twa_deg=twa,
            tws_kts=tws,
            boat_speed_kts=round(w_speed, 2),
            vmg_kts=round(w_vmg, 2),
            sample_count=total_samples,
        ))

    return sorted(merged, key=lambda p: (p.tws_kts, p.twa_deg))
