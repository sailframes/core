"""Upwind/downwind/reaching leg segmentation and statistics.

Identifies straight-line sailing segments between maneuvers,
classifies them by point of sail, and computes performance stats.
"""

import math

import numpy as np

from .models import GpsPoint, ImuReading, LegType, Maneuver, StraightLineLeg

# Minimum duration for a leg to be considered (seconds)
MIN_LEG_DURATION_SEC = 15
# Minimum points in a leg
MIN_LEG_POINTS = 5
# Max heading deviation for "straight line" (degrees STD)
MAX_HEADING_STD_DEG = 15.0


def segment_legs(
    gps: list[GpsPoint],
    maneuvers: list[Maneuver],
    true_wind: list[dict] | None = None,
    imu: list[ImuReading] | None = None,
) -> list[StraightLineLeg]:
    """Segment GPS track into straight-line legs between maneuvers.

    Args:
        gps: GPS track points.
        maneuvers: Detected maneuvers (tacks/gybes).
        true_wind: True wind series for TWA classification.
        imu: IMU data for heel angle stats.

    Returns:
        List of StraightLineLeg segments.
    """
    if len(gps) < MIN_LEG_POINTS:
        return []

    gps_times = np.array([p.timestamp for p in gps])

    # Build segment boundaries from maneuver times
    boundaries = [gps_times[0]]
    for m in sorted(maneuvers, key=lambda x: x.start_time):
        boundaries.append(m.start_time)
        boundaries.append(m.end_time)
    boundaries.append(gps_times[-1])

    # True wind for classification
    tw_times = None
    tw_twa = None
    tw_tws = None
    if true_wind:
        tw_times = np.array([tw["timestamp"] for tw in true_wind])
        tw_twa = np.array([tw["twa_deg"] for tw in true_wind])
        tw_tws = np.array([tw["tws_kts"] for tw in true_wind])

    # IMU for heel
    imu_times = None
    imu_heels = None
    if imu:
        imu_times = np.array([r.timestamp for r in imu])
        imu_heels = np.array([r.heel_deg for r in imu])

    legs = []
    for i in range(0, len(boundaries) - 1, 2):
        t_start = boundaries[i]
        t_end = boundaries[i + 1] if i + 1 < len(boundaries) else gps_times[-1]

        # Get GPS points in this segment
        mask = (gps_times >= t_start) & (gps_times <= t_end)
        if mask.sum() < MIN_LEG_POINTS:
            continue

        seg_gps = [gps[j] for j in range(len(gps)) if mask[j]]
        duration = t_end - t_start
        if duration < MIN_LEG_DURATION_SEC:
            continue

        # Compute leg stats
        speeds = np.array([p.speed_kts for p in seg_gps])
        headings = np.array([p.heading_deg for p in seg_gps])

        # Heading stability check
        heading_std = _circular_std(headings)
        if heading_std > MAX_HEADING_STD_DEG:
            continue

        # Distance (sum of segments in nautical miles)
        distance = _compute_distance_nm(seg_gps)

        # VMG from true wind
        avg_vmg = 0.0
        avg_twa = None
        leg_type = LegType.REACH  # default

        if tw_times is not None:
            tw_mask = (tw_times >= t_start) & (tw_times <= t_end)
            if tw_mask.sum() > 0:
                seg_twa = tw_twa[tw_mask]
                seg_tws = tw_tws[tw_mask]
                avg_twa = float(np.mean(np.abs(seg_twa)))

                # Classify leg type by average TWA
                if avg_twa < 70:
                    leg_type = LegType.UPWIND
                elif avg_twa > 120:
                    leg_type = LegType.DOWNWIND
                else:
                    leg_type = LegType.REACH

                # VMG calculation
                avg_speed = np.mean(speeds)
                avg_vmg = float(avg_speed * abs(math.cos(math.radians(avg_twa))))

        # Heel from IMU
        avg_heel = None
        if imu_times is not None:
            imu_mask = (imu_times >= t_start) & (imu_times <= t_end)
            if imu_mask.sum() > 0:
                avg_heel = float(np.mean(np.abs(imu_heels[imu_mask])))

        legs.append(StraightLineLeg(
            leg_type=leg_type,
            start_time=t_start,
            end_time=t_end,
            duration_sec=round(duration, 1),
            distance_nm=round(distance, 3),
            avg_speed_kts=round(float(np.mean(speeds)), 2),
            max_speed_kts=round(float(np.max(speeds)), 2),
            avg_vmg_kts=round(avg_vmg, 2),
            avg_heel_deg=round(avg_heel, 1) if avg_heel is not None else None,
            avg_twa_deg=round(avg_twa, 1) if avg_twa is not None else None,
            std_heading_deg=round(heading_std, 1),
            num_points=len(seg_gps),
            start_lat=seg_gps[0].lat,
            start_lon=seg_gps[0].lon,
            end_lat=seg_gps[-1].lat,
            end_lon=seg_gps[-1].lon,
        ))

    return legs


def _circular_std(angles_deg: np.ndarray) -> float:
    """Compute circular standard deviation of angles in degrees."""
    angles_rad = np.radians(angles_deg)
    sin_mean = np.mean(np.sin(angles_rad))
    cos_mean = np.mean(np.cos(angles_rad))
    r = math.sqrt(sin_mean**2 + cos_mean**2)
    if r > 1.0:
        r = 1.0
    if r < 1e-10:
        return 180.0
    return math.degrees(math.sqrt(-2 * math.log(r)))


def _compute_distance_nm(points: list[GpsPoint]) -> float:
    """Sum great-circle distances between consecutive GPS points."""
    total = 0.0
    for i in range(1, len(points)):
        total += _haversine_nm(
            points[i - 1].lat, points[i - 1].lon,
            points[i].lat, points[i].lon,
        )
    return total


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in nautical miles."""
    r = 3440.065  # Earth radius in nautical miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def leg_comparison(legs: list[StraightLineLeg]) -> dict:
    """Compare performance across legs by type."""
    by_type = {}
    for leg_type in LegType:
        typed_legs = [l for l in legs if l.leg_type == leg_type]
        if not typed_legs:
            continue
        speeds = [l.avg_speed_kts for l in typed_legs]
        vmgs = [l.avg_vmg_kts for l in typed_legs]
        by_type[leg_type.value] = {
            "count": len(typed_legs),
            "avg_speed_kts": round(np.mean(speeds), 2),
            "max_speed_kts": round(max(l.max_speed_kts for l in typed_legs), 2),
            "avg_vmg_kts": round(np.mean(vmgs), 2),
            "total_distance_nm": round(sum(l.distance_nm for l in typed_legs), 3),
        }
    return by_type
