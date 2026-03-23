"""Tack and gybe detection with per-maneuver performance metrics.

Detects maneuvers by finding rapid heading changes while the boat
is moving, then computes speed loss, recovery time, and other metrics.
"""

import math

import numpy as np

from .models import GpsPoint, ImuReading, Maneuver, ManeuverType


# Detection thresholds
MIN_HEADING_CHANGE_DEG = 60  # minimum heading change to qualify
MAX_MANEUVER_DURATION_SEC = 30  # max time for heading change
MIN_BOAT_SPEED_KTS = 1.5  # ignore turns while nearly stopped
SPEED_RECOVERY_THRESHOLD = 0.9  # 90% of entry speed
MAX_RECOVERY_WINDOW_SEC = 60


def detect_maneuvers(
    gps: list[GpsPoint],
    imu: list[ImuReading] | None = None,
    twd_deg: float | None = None,
) -> list[Maneuver]:
    """Detect tacks and gybes from heading changes.

    Uses IMU heading if available (50Hz), falls back to GPS heading (10Hz).
    Classifies as tack vs gybe using true wind direction if provided.
    """
    if len(gps) < 20:
        return []

    # Build heading time series (prefer IMU for higher resolution)
    if imu and len(imu) > 50:
        times = np.array([r.timestamp for r in imu])
        headings = np.array([r.heading_deg for r in imu])
    else:
        times = np.array([p.timestamp for p in gps])
        headings = np.array([p.heading_deg for p in gps])

    # GPS speed series for speed loss calculation
    gps_times = np.array([p.timestamp for p in gps])
    gps_speeds = np.array([p.speed_kts for p in gps])
    gps_lats = np.array([p.lat for p in gps])
    gps_lons = np.array([p.lon for p in gps])

    # Compute heading rate of change (degrees/sec)
    dt = np.diff(times)
    dt[dt == 0] = 0.001  # avoid division by zero
    dh = _angular_diff(headings[1:], headings[:-1])
    heading_rate = dh / dt

    # Find regions of rapid heading change
    rapid_turn = np.abs(heading_rate) > 5.0  # >5 deg/sec
    maneuvers = []

    i = 0
    while i < len(rapid_turn):
        if not rapid_turn[i]:
            i += 1
            continue

        # Found start of a turn - find the extent
        start_idx = i
        while i < len(rapid_turn) and rapid_turn[i]:
            i += 1
        end_idx = i

        t_start = times[start_idx]
        t_end = times[min(end_idx, len(times) - 1)]
        duration = t_end - t_start

        if duration > MAX_MANEUVER_DURATION_SEC:
            continue

        # Total heading change
        heading_before = headings[start_idx]
        heading_after = headings[min(end_idx, len(headings) - 1)]
        heading_change = _angular_diff(heading_after, heading_before)

        if abs(heading_change) < MIN_HEADING_CHANGE_DEG:
            continue

        # Speed metrics (interpolate GPS speed at maneuver boundaries)
        speed_before = float(np.interp(t_start, gps_times, gps_speeds))
        if speed_before < MIN_BOAT_SPEED_KTS:
            continue

        # Find minimum speed during maneuver
        mask = (gps_times >= t_start) & (gps_times <= t_end)
        if mask.sum() > 0:
            speed_min = float(gps_speeds[mask].min())
        else:
            speed_min = float(np.interp((t_start + t_end) / 2, gps_times, gps_speeds))

        # Find speed after and recovery time
        speed_after, recovery_time = _compute_recovery(
            gps_times, gps_speeds, t_end, speed_before
        )

        # Classify as tack or gybe
        maneuver_type = _classify_maneuver(
            heading_before, heading_after, twd_deg
        )

        # Heel during maneuver (from IMU)
        max_heel = None
        if imu:
            imu_times_arr = np.array([r.timestamp for r in imu])
            imu_heels = np.array([r.heel_deg for r in imu])
            mask_imu = (imu_times_arr >= t_start) & (imu_times_arr <= t_end)
            if mask_imu.sum() > 0:
                max_heel = float(np.max(np.abs(imu_heels[mask_imu])))

        # Start position
        start_lat = float(np.interp(t_start, gps_times, gps_lats))
        start_lon = float(np.interp(t_start, gps_times, gps_lons))

        maneuvers.append(Maneuver(
            maneuver_type=maneuver_type,
            start_time=t_start,
            end_time=t_end,
            duration_sec=round(duration, 1),
            speed_loss_kts=round(speed_before - speed_min, 2),
            speed_before_kts=round(speed_before, 2),
            speed_min_kts=round(speed_min, 2),
            speed_after_kts=round(speed_after, 2),
            recovery_time_sec=round(recovery_time, 1),
            heading_change_deg=round(heading_change, 1),
            max_heel_deg=round(max_heel, 1) if max_heel else None,
            start_lat=start_lat,
            start_lon=start_lon,
        ))

    return maneuvers


def _angular_diff(a: float | np.ndarray, b: float | np.ndarray) -> float | np.ndarray:
    """Signed angular difference a - b, result in [-180, 180]."""
    d = a - b
    if isinstance(d, np.ndarray):
        d = (d + 180) % 360 - 180
    else:
        d = (d + 180) % 360 - 180
    return d


def _classify_maneuver(
    heading_before: float,
    heading_after: float,
    twd_deg: float | None,
) -> ManeuverType:
    """Classify as tack (head through wind) or gybe (stern through wind)."""
    if twd_deg is None:
        # Without wind data, large heading changes >90° across likely wind axis
        # Default to tack for heading changes in typical upwind range
        change = abs(_angular_diff(heading_after, heading_before))
        return ManeuverType.TACK if change < 120 else ManeuverType.GYBE

    # Relative to wind: tack if bow crosses wind, gybe if stern crosses
    rel_before = _angular_diff(heading_before, twd_deg)
    rel_after = _angular_diff(heading_after, twd_deg)

    # Tack: both headings within ~90° of wind (upwind), different sides
    if abs(rel_before) < 100 and abs(rel_after) < 100:
        if (rel_before > 0) != (rel_after > 0):
            return ManeuverType.TACK

    return ManeuverType.GYBE


def _compute_recovery(
    gps_times: np.ndarray,
    gps_speeds: np.ndarray,
    maneuver_end: float,
    speed_before: float,
) -> tuple[float, float]:
    """Find speed after maneuver and time to recover to 90% entry speed."""
    target = speed_before * SPEED_RECOVERY_THRESHOLD
    mask = gps_times > maneuver_end
    future_times = gps_times[mask]
    future_speeds = gps_speeds[mask]

    if len(future_times) == 0:
        return speed_before, 0.0

    # Speed 5 seconds after maneuver
    t_after = maneuver_end + 5.0
    speed_after = float(np.interp(t_after, gps_times, gps_speeds))

    # Recovery time
    window_mask = (future_times - maneuver_end) < MAX_RECOVERY_WINDOW_SEC
    window_speeds = future_speeds[window_mask]
    window_times = future_times[window_mask]

    recovered = window_speeds >= target
    if recovered.any():
        first_idx = np.argmax(recovered)
        recovery_time = window_times[first_idx] - maneuver_end
    else:
        recovery_time = MAX_RECOVERY_WINDOW_SEC

    return speed_after, recovery_time


def maneuver_summary(maneuvers: list[Maneuver]) -> dict:
    """Compute summary statistics for all maneuvers."""
    tacks = [m for m in maneuvers if m.maneuver_type == ManeuverType.TACK]
    gybes = [m for m in maneuvers if m.maneuver_type == ManeuverType.GYBE]

    def _stats(group: list[Maneuver]) -> dict:
        if not group:
            return {"count": 0}
        speeds_lost = [m.speed_loss_kts for m in group]
        recovery_times = [m.recovery_time_sec for m in group]
        durations = [m.duration_sec for m in group]
        return {
            "count": len(group),
            "avg_speed_loss_kts": round(np.mean(speeds_lost), 2),
            "avg_recovery_sec": round(np.mean(recovery_times), 1),
            "avg_duration_sec": round(np.mean(durations), 1),
            "best_speed_loss_kts": round(min(speeds_lost), 2),
            "worst_speed_loss_kts": round(max(speeds_lost), 2),
        }

    return {
        "tacks": _stats(tacks),
        "gybes": _stats(gybes),
        "total": len(maneuvers),
    }
