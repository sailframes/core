"""Statistical analysis: violin plot data, STD, correlations.

Provides data for violin plots comparing maneuver performance,
statistical summaries, and correlation analysis between variables.
"""

import math
from collections import defaultdict

import numpy as np

from .models import (
    AnalysisResult,
    GpsPoint,
    ImuReading,
    Maneuver,
    ManeuverType,
    StraightLineLeg,
    WindReading,
)


def violin_plot_data(maneuvers: list[Maneuver]) -> dict:
    """Generate data for violin plots comparing tacks vs gybes.

    Returns distributions for speed loss, recovery time, duration,
    grouped by maneuver type.
    """
    groups = defaultdict(lambda: defaultdict(list))

    for m in maneuvers:
        key = m.maneuver_type.value
        groups[key]["speed_loss_kts"].append(m.speed_loss_kts)
        groups[key]["recovery_time_sec"].append(m.recovery_time_sec)
        groups[key]["duration_sec"].append(m.duration_sec)
        if m.max_heel_deg is not None:
            groups[key]["max_heel_deg"].append(m.max_heel_deg)
        groups[key]["heading_change_deg"].append(abs(m.heading_change_deg))

    result = {}
    for mtype, metrics in groups.items():
        result[mtype] = {}
        for metric, values in metrics.items():
            arr = np.array(values)
            result[mtype][metric] = {
                "values": [round(v, 2) for v in values],
                "mean": round(float(np.mean(arr)), 2),
                "median": round(float(np.median(arr)), 2),
                "std": round(float(np.std(arr)), 2),
                "min": round(float(np.min(arr)), 2),
                "max": round(float(np.max(arr)), 2),
                "q25": round(float(np.percentile(arr, 25)), 2),
                "q75": round(float(np.percentile(arr, 75)), 2),
            }

    return result


def session_statistics(
    gps: list[GpsPoint],
    wind: list[WindReading],
    imu: list[ImuReading] | None = None,
) -> dict:
    """Compute overall session statistics."""
    stats = {}

    if gps:
        speeds = [p.speed_kts for p in gps]
        stats["speed"] = {
            "mean": round(np.mean(speeds), 2),
            "max": round(max(speeds), 2),
            "std": round(np.std(speeds), 2),
            "median": round(np.median(speeds), 2),
        }

    if wind:
        aws = [w.apparent_speed_kts for w in wind]
        stats["apparent_wind_speed"] = {
            "mean": round(np.mean(aws), 2),
            "max": round(max(aws), 2),
            "std": round(np.std(aws), 2),
        }

    if imu:
        heels = [abs(r.heel_deg) for r in imu]
        pitches = [r.pitch_deg for r in imu]
        stats["heel"] = {
            "mean": round(np.mean(heels), 1),
            "max": round(max(heels), 1),
            "std": round(np.std(heels), 1),
        }
        stats["pitch"] = {
            "mean": round(np.mean(pitches), 1),
            "max": round(max(np.abs(pitches)), 1),
            "std": round(np.std(pitches), 1),
        }

    return stats


def correlation_matrix(
    gps: list[GpsPoint],
    true_wind: list[dict],
    imu: list[ImuReading] | None = None,
) -> dict:
    """Compute correlations between key sailing variables.

    Builds a time-aligned dataset and computes Pearson correlations
    between: boat speed, TWS, TWA, heel, VMG.
    """
    if not true_wind or not gps:
        return {}

    gps_times = np.array([p.timestamp for p in gps])
    gps_speeds = np.array([p.speed_kts for p in gps])

    # Build aligned arrays at true_wind timestamps
    timestamps = []
    boat_speed = []
    tws = []
    twa = []
    vmg = []

    for tw in true_wind:
        t = tw["timestamp"]
        if t < gps_times[0] or t > gps_times[-1]:
            continue
        spd = float(np.interp(t, gps_times, gps_speeds))
        timestamps.append(t)
        boat_speed.append(spd)
        tws.append(tw["tws_kts"])
        twa.append(abs(tw["twa_deg"]))
        vmg.append(spd * abs(math.cos(math.radians(tw["twa_deg"]))))

    variables = {
        "boat_speed": np.array(boat_speed),
        "tws": np.array(tws),
        "twa": np.array(twa),
        "vmg": np.array(vmg),
    }

    # Add heel if IMU available
    if imu and len(imu) > 10:
        imu_times = np.array([r.timestamp for r in imu])
        imu_heels = np.array([abs(r.heel_deg) for r in imu])
        heel_interp = np.interp(timestamps, imu_times, imu_heels)
        variables["heel"] = heel_interp

    # Compute correlation matrix
    var_names = list(variables.keys())
    n = len(var_names)
    matrix = {}

    for i in range(n):
        row = {}
        for j in range(n):
            if len(variables[var_names[i]]) > 2:
                corr = float(np.corrcoef(
                    variables[var_names[i]],
                    variables[var_names[j]]
                )[0, 1])
                row[var_names[j]] = round(corr, 3)
            else:
                row[var_names[j]] = 0.0
        matrix[var_names[i]] = row

    return {"variables": var_names, "matrix": matrix}


def leg_performance_ranking(legs: list[StraightLineLeg]) -> list[dict]:
    """Rank legs by VMG performance for leaderboard display."""
    ranked = []
    for i, leg in enumerate(legs):
        ranked.append({
            "leg_index": i,
            "leg_type": leg.leg_type.value,
            "avg_speed_kts": leg.avg_speed_kts,
            "avg_vmg_kts": leg.avg_vmg_kts,
            "duration_sec": leg.duration_sec,
            "distance_nm": leg.distance_nm,
            "start_time": leg.start_time,
        })

    return sorted(ranked, key=lambda x: x["avg_vmg_kts"], reverse=True)
