#!/usr/bin/env python3
"""
breeze.py — Jean-Yves Bernot's sea-breeze decision method, as computable functions.

Faithful to NOTES-Meteo-Brise-Thermique-Bernot.md (the spec). Pure functions over
scalars/series so the per-day driver (breeze_day.py) can produce, for each step of
the decision process, BOTH a prediction and a real-observation validation.

Angle convention: all directions are meteorological "from" bearings in degrees
(0=N, 90=E), unless a name ends in `_to` (direction the wind blows toward).
Speeds in knots unless suffixed. Coast "facing" = seaward-normal bearing (the way
you look when you look out to sea).
"""
import math

KT = 1.943844
ONSHORE_LO, ONSHORE_HI = None, None   # set per-venue by the driver from coast facing


# ----------------------------- angle helpers --------------------------------
def norm360(a):
    return a % 360.0


def ang_diff(a, b):
    """Signed smallest a-b in (-180, 180]."""
    return (a - b + 180.0) % 360.0 - 180.0


def to_uv(from_deg, spd):
    """Meteorological 'from' bearing + speed -> (u east, v north) of the wind vector
    (the direction it blows TOWARD)."""
    to = math.radians(from_deg + 180.0)
    return spd * math.sin(to), spd * math.cos(to)


def from_uv(u, v):
    """(u,v) blow-toward vector -> (from_deg, spd)."""
    spd = math.hypot(u, v)
    if spd < 1e-9:
        return None, 0.0
    to = math.degrees(math.atan2(u, v)) % 360.0
    return (to + 180.0) % 360.0, spd


def vec_mean(dirs, spds):
    """Meteorological vector-mean of (from_dir, spd) pairs -> (from_dir, spd)."""
    u = v = 0.0
    n = 0
    for d, s in zip(dirs, spds):
        if d is None or s is None:
            continue
        uu, vv = to_uv(d, s)
        u += uu; v += vv; n += 1
    if not n:
        return None, None
    fd, sp = from_uv(u / n, v / n)
    return fd, sp


# --------------------------- coast geometry ---------------------------------
def coast_facing(land_2d, lats_2d, lons_2d, region=None, smooth=2):
    """Seaward-normal bearing (deg) averaged over coastal cells: which way the coast
    faces. `land_2d` is 1=land/0=water (ny,nx); lats/lons same shape. `region` is an
    optional boolean mask selecting the racing area. We smooth the land fraction and
    take -grad (toward water) as the seaward normal; average its bearing over cells
    that straddle the coast (0.15<landfrac<0.85)."""
    import numpy as np
    L = np.asarray(land_2d, dtype="f8")
    ny, nx = L.shape
    # box-smooth the land fraction
    S = L.copy()
    for _ in range(smooth):
        P = np.pad(S, 1, mode="edge")
        S = (P[:-2, 1:-1] + P[2:, 1:-1] + P[1:-1, :-2] + P[1:-1, 2:] + P[1:-1, 1:-1]) / 5.0
    gy, gx = np.gradient(S)                 # grad in index space (row=+north? see below)
    # convert index gradient to geographic east/north using per-cell lat/lon spacing
    lat = np.asarray(lats_2d, "f8"); lon = np.asarray(lons_2d, "f8")
    dlat_drow = np.gradient(lat, axis=0); dlat_dcol = np.gradient(lat, axis=1)
    dlon_drow = np.gradient(lon, axis=0); dlon_dcol = np.gradient(lon, axis=1)
    # d(land)/d(north) and d(land)/d(east) via chain rule (invert the 2x2 jacobian)
    # [gy;gx] = J^T [dL/dlat; dL/dlon] with J = [[dlat_drow,dlat_dcol],[dlon_drow,dlon_dcol]]
    det = dlat_drow * dlon_dcol - dlat_dcol * dlon_drow
    det = np.where(np.abs(det) < 1e-12, 1e-12, det)
    dL_dlat = (dlon_dcol * gy - dlon_drow * gx) / det
    dL_dlon = (-dlat_dcol * gy + dlat_drow * gx) / det
    # geographic gradient of land toward increasing land; seaward = -grad
    e = -dL_dlon        # east component (per deg lon) -> scale by cos(lat) for meters, but bearing only needs ratio
    n = -dL_dlat
    e = e * np.cos(np.radians(lat))
    mag = np.hypot(e, n)
    coastal = (S > 0.15) & (S < 0.85) & (mag > 1e-9)
    if region is not None:
        coastal = coastal & np.asarray(region, dtype=bool)
    if not coastal.any():
        return None
    eu = np.sum((e / np.where(mag == 0, 1, mag))[coastal])
    nu = np.sum((n / np.where(mag == 0, 1, mag))[coastal])
    return (math.degrees(math.atan2(eu, nu))) % 360.0


# --------------------------- quadrant theory --------------------------------
def quadrant(syn_from_deg, coast_face_deg):
    """Bernot theory of quadrants (Ch.3), relative to the COAST not compass.
    coast_face_deg = seaward-normal bearing. Returns dict with quadrant Q1..Q4,
    from_land flag, land_on_right flag, and the angles used."""
    if syn_from_deg is None or coast_face_deg is None:
        return None
    wind_to = norm360(syn_from_deg + 180.0)          # direction wind blows toward
    seaward = coast_face_deg                          # toward sea
    landward = norm360(coast_face_deg + 180.0)        # toward land
    # offshore (from land) if the wind blows toward sea: |wind_to - seaward| < 90
    from_land = abs(ang_diff(wind_to, seaward)) < 90.0
    # coast on the right (of the wind, looking downwind) if landward is +90..+180 from wind_to
    land_on_right = ang_diff(landward, wind_to) < 0.0   # landward is clockwise (right) of wind_to
    if from_land:
        q = "Q1" if land_on_right else "Q2"
    else:
        q = "Q3" if land_on_right else "Q4"
    strat = {
        "Q1": ("Best. Breeze establishes early & likely strong, builds coast->offshore.", "Go to the coast."),
        "Q2": ("Combat douteux — breeze fights the synoptic. Confused; if it comes it's late & offshore-first; failed attempts likely.", "Play the old wind; only trust the breeze once the horizon clears."),
        "Q3": ("Breeze seems to reinforce the synoptic; wind does a there-and-back (backs left then returns right).", "Manage the transition zone."),
        "Q4": ("Worst. Breeze usually won't lift despite the heat; wind may die then return as synoptic.", "Expect no help."),
    }[q]
    return {"quadrant": q, "from_land": from_land, "land_on_right": land_on_right,
            "wind_to": round(wind_to, 1), "seaward": round(seaward, 1), "landward": round(landward, 1),
            "behaviour": strat[0], "strategy": strat[1]}


def divergence_sign(syn_from_deg, coast_face_deg):
    """Ch.3 §1.2: coast on the right of the wind -> divergence -> subsidence -> favours
    the breeze; coast on the left -> convergence -> hinders. Returns +1/-1 and a label."""
    q = quadrant(syn_from_deg, coast_face_deg)
    if q is None:
        return None
    if q["land_on_right"]:
        return {"sign": +1, "label": "divergence (favours the breeze)"}
    return {"sign": -1, "label": "convergence (hinders the breeze)"}


# --------------------------- Wisdorff force grid ----------------------------
# Table 2: total score -> max afternoon breeze (kt)
WISDORFF_SPEED = {8: 25, 7: 20, 6: 18, 5: 16, 4: 12, 3: 9, 2: 6, 1: 0}


def wisdorff_score(*, air_warmer, synoptic_kt, quad, unstable, wind800_aids,
                   air_temp_class, syn_shifts_to_breeze, sunshine_class,
                   afternoon_high_tide, shear_aloft):
    """Bernot/Wisdorff force grid (Ch.4 Tables 1-2). Each criterion -> weight; total
    -> predicted max afternoon breeze speed. Returns (components, total, pred_kt).
    Gate criteria 1 & 2: if air not warmer than sea OR synoptic >= 18 kt -> no breeze.
    Any argument may be None (unknown) -> that criterion contributes 0 and is flagged."""
    comps = []

    def add(name, detail, weight, known=True):
        comps.append({"name": name, "detail": detail, "weight": weight, "known": known})

    # 1 & 2 are gates
    gate_fail = False
    if air_warmer is False:
        add("1. Air warmer than sea?", "No — no breeze", 0); gate_fail = True
    elif air_warmer is True:
        add("1. Air warmer than sea?", "Yes — breeze possible", 0)
    else:
        add("1. Air warmer than sea?", "unknown", 0, False)
    if synoptic_kt is None:
        add("2. Synoptic < 18 kt?", "unknown", 0, False)
    elif synoptic_kt >= 18:
        add("2. Synoptic < 18 kt?", "No (%.0f kt) — no breeze" % synoptic_kt, 0); gate_fail = True
    else:
        add("2. Synoptic < 18 kt?", "Yes (%.0f kt)" % synoptic_kt, 0)

    qw = {"Q1": 2, "Q2": 1, "Q3": 0, "Q4": -1}.get(quad)
    add("3. Synoptic direction (quadrant)", "%s" % (quad or "?"), qw if qw is not None else 0, qw is not None)
    add("4. Air-mass stability", "Unstable" if unstable else ("Stable" if unstable is False else "unknown"),
        (2 if unstable else -1) if unstable is not None else 0, unstable is not None)
    add("5. 800 m wind aids return current", "Yes" if wind800_aids else ("No" if wind800_aids is False else "unknown"),
        (1 if wind800_aids else -1) if wind800_aids is not None else 0, wind800_aids is not None)
    tw = {"cool": 1, "mild": 0, "hot": -1}.get(air_temp_class)
    add("6. Air-mass temperature", air_temp_class or "unknown", tw if tw is not None else 0, tw is not None)
    add("7. Synoptic shifts toward breeze dir", "Yes" if syn_shifts_to_breeze else ("No" if syn_shifts_to_breeze is False else "unknown"),
        (1 if syn_shifts_to_breeze else -1) if syn_shifts_to_breeze is not None else 0, syn_shifts_to_breeze is not None)
    sw = {"good": 1, "medium": 0, "poor": -1}.get(sunshine_class)
    add("8. Sunshine", sunshine_class or "unknown", sw if sw is not None else 0, sw is not None)
    add("9. Afternoon high tide", "Yes" if afternoon_high_tide else ("No" if afternoon_high_tide is False else "unknown"),
        (0 if afternoon_high_tide else -1) if afternoon_high_tide is not None else 0, afternoon_high_tide is not None)
    add("10. Vertical shear aloft", "Yes" if shear_aloft else ("No" if shear_aloft is False else "unknown"),
        (-1 if shear_aloft else 1) if shear_aloft is not None else 0, shear_aloft is not None)

    total = sum(c["weight"] for c in comps)
    if gate_fail:
        pred = 0
    else:
        # Table 2 lookup, clamped
        t = max(1, min(8, total))
        pred = WISDORFF_SPEED[t]
    return {"components": comps, "total": total, "pred_kt": pred, "gate_fail": gate_fail}


# --------------------------- loop / cross-section ---------------------------
def loop_params(*, dt_c, hpbl_m, wind850_kt, surface_breeze_kt):
    """Vertical-loop parameters for the cross-section drawing (Ch.2 Fig.1 numbers)."""
    return {
        "dt_c": dt_c,
        "depth_m": hpbl_m,                     # HPBL ~ breeze depth (200-500 m expected)
        "return_kt": wind850_kt,               # ~1500 m return current ~ 850 hPa wind
        "surface_kt": surface_breeze_kt,       # surface inflow 8-10 kt expected
        "front_offshore_M": 3.0,               # forms ~3 M off (Bernot)
        "front_speed_kt": 2.5,                 # advances 2-3 kt
    }


# --------------------------- onset from obs ---------------------------------
def onshore_sector(coast_face_deg, half=55.0):
    """Onshore = wind FROM within +/-half of the coast facing (seaward normal)."""
    return coast_face_deg, half


def detect_onset(series, coast_face_deg, half=55.0, min_kt=5.0):
    """series: list of (hour_lt_float, from_dir, spd_kt). First time >=10 LT with
    onshore (from within +/-half of coast facing) sustained >= min_kt for 2 samples."""
    aft = [(h, d, s) for (h, d, s) in series if h is not None and 10 <= h <= 20 and d is not None]
    for i in range(len(aft) - 1):
        h, d, s = aft[i]; h2, d2, s2 = aft[i + 1]
        if (abs(ang_diff(d, coast_face_deg)) <= half and abs(ang_diff(d2, coast_face_deg)) <= half
                and (s or 0) >= min_kt and (s2 or 0) >= min_kt):
            return {"onset_lt": h, "onset_dir": d}
    return {"onset_lt": None, "onset_dir": None}


if __name__ == "__main__":
    # self-test: 2026-07-04 Mass Bay — morning synoptic W (261 deg), coast faces ~ESE.
    for cf in (100, 110, 120):
        q = quadrant(261.0, cf)
        print("coast_face=%d -> %s (from_land=%s land_on_right=%s)  %s"
              % (cf, q["quadrant"], q["from_land"], q["land_on_right"], q["strategy"]))
    w = wisdorff_score(air_warmer=True, synoptic_kt=12, quad="Q2", unstable=True,
                       wind800_aids=True, air_temp_class="hot", syn_shifts_to_breeze=False,
                       sunshine_class="good", afternoon_high_tide=False, shear_aloft=False)
    print("Wisdorff demo: total=%d pred=%d kt" % (w["total"], w["pred_kt"]))
