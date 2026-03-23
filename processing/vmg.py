"""Velocity Made Good (VMG) computation.

Calculates VMG relative to true wind direction and optionally
relative to a mark/waypoint.
"""

import math

import numpy as np

from .models import GpsPoint, VmgResult


def compute_vmg(
    boat_speed_kts: float,
    heading_deg: float,
    twd_deg: float,
) -> float:
    """Compute VMG toward the wind (upwind positive, downwind positive).

    VMG = boat_speed * cos(heading - twd)
    Returns absolute value for both upwind and downwind.
    """
    angle = math.radians(heading_deg - twd_deg)
    return abs(boat_speed_kts * math.cos(angle))


def compute_vmg_to_mark(
    boat_speed_kts: float,
    heading_deg: float,
    bearing_to_mark_deg: float,
) -> float:
    """Compute VMG toward a specific mark/waypoint."""
    angle = math.radians(heading_deg - bearing_to_mark_deg)
    return boat_speed_kts * math.cos(angle)


def compute_vmg_series(
    gps: list[GpsPoint],
    true_wind: list[dict],
) -> list[VmgResult]:
    """Compute VMG time series from GPS and true wind data.

    Args:
        gps: GPS track points.
        true_wind: Output from wind.compute_true_wind_series().

    Returns:
        List of VmgResult for each true wind point.
    """
    if not true_wind:
        return []

    gps_times = np.array([p.timestamp for p in gps])
    gps_speeds = np.array([p.speed_kts for p in gps])
    gps_headings = np.array([p.heading_deg for p in gps])

    results = []
    for tw in true_wind:
        t = tw["timestamp"]
        if t < gps_times[0] or t > gps_times[-1]:
            continue

        speed = float(np.interp(t, gps_times, gps_speeds))
        heading = float(np.interp(t, gps_times, gps_headings))
        twd = tw["twd_deg"]
        twa = tw["twa_deg"]

        vmg = compute_vmg(speed, heading, twd)

        results.append(VmgResult(
            timestamp=t,
            vmg_kts=round(vmg, 2),
            twa_deg=round(twa, 1),
            boat_speed_kts=round(speed, 2),
            tws_kts=tw.get("tws_kts"),
        ))

    return results


def compute_optimal_vmg_angles(
    polar_points: list[dict],
) -> dict[float, tuple[float, float]]:
    """From polar data, find optimal VMG angles for each wind speed.

    Returns dict mapping tws_bucket -> (optimal_upwind_twa, optimal_downwind_twa).
    """
    from collections import defaultdict

    by_tws = defaultdict(list)
    for p in polar_points:
        by_tws[p["tws_kts"]].append(p)

    optimal = {}
    for tws, points in by_tws.items():
        best_upwind_vmg = 0
        best_upwind_twa = 0
        best_downwind_vmg = 0
        best_downwind_twa = 0

        for p in points:
            twa = abs(p["twa_deg"])
            speed = p["boat_speed_kts"]
            vmg = speed * math.cos(math.radians(twa))

            if twa < 90 and vmg > best_upwind_vmg:
                best_upwind_vmg = vmg
                best_upwind_twa = twa
            elif twa >= 90 and abs(vmg) > best_downwind_vmg:
                best_downwind_vmg = abs(vmg)
                best_downwind_twa = twa

        optimal[tws] = (best_upwind_twa, best_downwind_twa)

    return optimal
