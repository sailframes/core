#!/usr/bin/env python3
"""
breeze_day.py — run Bernot's sea-breeze decision walkthrough for ONE day and emit
climatology/breeze/<date>.json: for each decision step, the prediction AND the
real-observation validation (✓/✗), plus the data series the web report draws.

Data: existing daily fields parquet (u10/v10/t2/td2/tcdc/dswrf) on S3 + extra
hrrrzarr fields pulled live (CAPE, 925mb wind/T, VIS, HGT static) + obs
(44013/A01/BOS/BVY) + Boston tide. Grid + coast facing from grid.json.

Run: AWS_PROFILE=sailframes python3.11 climatology/breeze_day.py --date 2026-07-04
"""
import argparse
import datetime as dt
import io
import json
import math
import os
from zoneinfo import ZoneInfo

import boto3
import numpy as np
import pyarrow.parquet as pq
import pyarrow.compute as pc

import hrrr_grid as hg
import breeze as B

TZ = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
KT = 1.943844
BUCKET = os.environ.get("CLIMO_BUCKET", "sailframes-data-prod")
PFX = "climatology"
_s3 = boto3.client("s3")
STATIONS = {"44013": "buoy", "A01": "buoy", "BOS": "airport", "BVY": "airport"}
INSHORE = ("BOS", "BVY")     # coastal — where the sea breeze shows most
OFFSHORE = ("44013", "A01")  # out in the bay


def _load(key):
    try:
        return pq.read_table(io.BytesIO(_s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()))
    except Exception:
        return None


def _lt(tu):
    d = tu if isinstance(tu, dt.datetime) else dt.datetime.fromisoformat(str(tu))
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(TZ)


def obs_series(stn, ymd):
    """Hourly-ish (h_lt_float, from_dir, spd_kt, t_water) for the date at a station."""
    t = _load(f"{PFX}/obs/{stn}/{ymd[:4]}.parquet")
    if t is None:
        return []
    out = []
    for r in t.to_pylist():
        if str(r.get("time_utc"))[:10] != f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}":
            continue
        L = _lt(r["time_utc"])
        ws = r.get("wspd"); wd = r.get("wdir")
        out.append((L.hour + L.minute / 60.0, wd, None if ws is None else ws * KT, r.get("t_water")))
    out.sort()
    return out


def hourly_pick(series):
    """Reduce a dense series to one sample per LT hour (nearest :00)."""
    by = {}
    for h, d, s, tw in series:
        hh = int(round(h))
        if hh not in by or abs(h - hh) < abs(by[hh][0] - hh):
            by[hh] = (h, d, s, tw)
    return [by[k] for k in sorted(by)]


# --------------------------- HRRR field helpers -----------------------------
def read_field_hourly(ymd, group, cycles):
    """{cyc: 1d array over window} for a group across given cycles (analysis F00)."""
    j0, j1, i0, i1 = hg.bbox_window(hg.store_anl(ymd, cycles[0]))
    out = {}
    for c in cycles:
        try:
            out[c] = hg.read_window(hg.store_anl(ymd, c), group, j0, j1, i0, i1).ravel()
        except Exception:
            pass
    return out, (j0, j1, i0, i1)


def cycles_for(ymd):
    return sorted(hg.list_anl_cycles(ymd))


def _summary(q, dt_c, syn_kt, genuine, ambiguous, peak):
    quad = q["quadrant"] if q else "?"
    dtf = "" if dt_c is None else "ΔT %.0f°C" % dt_c
    synf = "" if syn_kt is None else "850 hPa synoptic %.0f kt" % syn_kt
    if genuine:
        extra = " A Q2 'combat douteux' that the breeze won." if quad == "Q2" else ""
        return "A %s day: a thermal sea breeze filled at the coast (peak onshore %.0f kt).%s" % (quad, peak, extra)
    if ambiguous:
        return ("A %s day: the afternoon wind was onshore, but it was already onshore in the morning — this is the sea-origin synoptic, not a distinct thermal breeze (Bernot's Q3/Q4 'breeze indistinct' case)." % quad)
    if quad in ("Q1", "Q3") and syn_kt is not None and syn_kt >= 16:
        return ("A %s day that looked favourable (%s) yet did NOT deliver: the %s sat near the ~18 kt ceiling and held all afternoon, so the sea breeze never established (peak onshore only %.0f kt). Quadrant favourability is overridden when the synoptic is near the ceiling." % (quad, dtf, synf, peak))
    if quad == "Q4":
        return ("A %s day (worst case): the sea breeze did not lift (peak onshore %.0f kt) — as expected." % (quad, peak))
    return ("A %s day: despite %s, the sea breeze did not establish — the synoptic held (peak onshore %.0f kt)." % (quad, dtf, peak))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)      # YYYY-MM-DD
    ap.add_argument("--out-dir", default=None)
    a = ap.parse_args()
    ymd = a.date.replace("-", "")

    # ---- grid + coast facing -------------------------------------------------
    grid = json.load(io.BytesIO(_s3.get_object(Bucket=BUCKET, Key=f"{PFX}/grid.json")["Body"].read()))
    nx, ny = grid["nx"], grid["ny"]
    lats = np.array(grid["lats"]).reshape(ny, nx)
    lons = np.array(grid["lons"]).reshape(ny, nx)
    land = np.array(grid["land_mask"]).reshape(ny, nx)
    # racing region ~ western Mass Bay (Boston harbor approaches to Cape Ann)
    region = (lats >= 42.2) & (lats <= 42.6) & (lons >= -71.0) & (lons <= -70.4)
    coast_face = B.coast_facing(land, lats, lons, region=region)
    coast_face_all = B.coast_facing(land, lats, lons, region=None)
    cf = coast_face if coast_face is not None else coast_face_all

    # ---- obs ----------------------------------------------------------------
    obs = {s: obs_series(s, ymd) for s in STATIONS}
    obs_h = {s: hourly_pick(v) for s, v in obs.items()}

    # morning synoptic gradient (06-10 LT) from 44013
    morn = [(d, s) for (h, d, s, _) in obs["44013"] if 6 <= h < 10 and d is not None and s is not None]
    gdir, gspd = B.vec_mean([d for d, _ in morn], [s for _, s in morn])

    # SST (10-14 LT mean water temp from the buoy)
    sst_vals = [tw for (h, d, s, tw) in obs["44013"] if 10 <= h <= 14 and tw is not None]
    sst = float(np.mean(sst_vals)) if sst_vals else None
    # Tmax over land from HRRR 2 m T (10-17 LT) — aligned, avoids the missing ASOS file
    fields = _load(f"{PFX}/fields/year={ymd[:4]}/month={ymd[4:6]}/{ymd[6:8]}.parquet")
    tmax = None
    if fields is not None:
        landgi = set(np.where(land.ravel().astype(bool))[0].tolist())
        tcol = fields.column("valid_time_utc").to_pylist(); gg = fields.column("gi").to_pylist()
        t2c = fields.column("t2").to_pylist()
        tland = [t2c[i] - 273.15 for i in range(len(t2c))
                 if gg[i] in landgi and t2c[i] is not None and 10 <= _lt(tcol[i]).hour <= 17]
        tmax = max(tland) if tland else None
    dt_c = (tmax - sst) if (tmax is not None and sst is not None) else None

    # ---- HRRR extra fields (hourly) -----------------------------------------
    cyc = cycles_for(ymd)
    cape_h, win = read_field_hourly(ymd, "surface/CAPE", cyc)
    vis_h, _ = read_field_hourly(ymd, "surface/VIS", cyc)
    u925_h, _ = read_field_hourly(ymd, "925mb/UGRD", cyc)
    v925_h, _ = read_field_hourly(ymd, "925mb/VGRD", cyc)
    t925_h, _ = read_field_hourly(ymd, "925mb/TMP", cyc)
    t850_h, _ = read_field_hourly(ymd, "850mb/TMP", cyc)
    u850_h, _ = read_field_hourly(ymd, "850mb/UGRD", cyc)
    v850_h, _ = read_field_hourly(ymd, "850mb/VGRD", cyc)
    j0, j1, i0, i1 = win
    hgt = hg.read_window(hg.store_anl(ymd, cyc[len(cyc) // 2]), "surface/HGT", j0, j1, i0, i1).ravel()
    landflat = land.ravel().astype(bool)
    waterflat = ~landflat

    def cyc_lt_hour(c):
        return _lt(dt.datetime(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]), int(c[:2]), tzinfo=UTC)).hour

    def land_mean(d, c):
        return float(np.nanmean(d[c][landflat])) if c in d else None

    def water_mean(d, c):
        return float(np.nanmean(d[c][waterflat])) if c in d else None

    # midday (12-16 LT) land CAPE -> instability; morning 925 wind -> return current
    midday_cyc = [c for c in cyc if 12 <= cyc_lt_hour(c) <= 16]
    cape_mid = np.nanmean([land_mean(cape_h, c) for c in midday_cyc if land_mean(cape_h, c) is not None]) if midday_cyc else None
    morn_cyc = [c for c in cyc if 8 <= cyc_lt_hour(c) <= 11]
    # 925mb morning wind (return-current level ~750 m) vector mean over the window
    w925 = []
    for c in morn_cyc:
        if c in u925_h and c in v925_h:
            w925.append((float(np.nanmean(u925_h[c])), float(np.nanmean(v925_h[c]))))
    u925m = np.mean([x[0] for x in w925]) if w925 else None
    v925m = np.mean([x[1] for x in w925]) if w925 else None
    dir925, spd925 = (B.from_uv(u925m, v925m) if u925m is not None else (None, None))
    spd925_kt = spd925 * KT if spd925 is not None else None
    # 850 mb morning wind = Bernot's synoptic proxy (direction + the ~18 kt ceiling)
    w850 = [(float(np.nanmean(u850_h[c])), float(np.nanmean(v850_h[c])))
            for c in morn_cyc if c in u850_h and c in v850_h]
    if w850:
        u850m = np.mean([x[0] for x in w850]); v850m = np.mean([x[1] for x in w850])
        syn_dir, syn_spd = B.from_uv(u850m, v850m)
        syn_spd_kt = syn_spd * KT
    else:
        syn_dir, syn_spd_kt = None, None
    # primary synoptic = 850 mb (fall back to surface buoy gradient)
    SYN_DIR = syn_dir if syn_dir is not None else gdir
    SYN_KT = syn_spd_kt if syn_spd_kt is not None else gspd

    # VIS offshore timeline (horizon clearing) — mean over water, by LT hour
    vis_ts = []
    for c in cyc:
        wm = water_mean(vis_h, c)
        if wm is not None:
            vis_ts.append([cyc_lt_hour(c), round(wm / 1000.0, 1)])   # km
    vis_ts.sort()

    # ---- STEP computations ---------------------------------------------------
    steps = []

    # onshore sector from coast facing
    half = 55.0

    def onshore(d):
        return d is not None and abs(B.ang_diff(d, cf)) <= half

    # observed: did a clean onshore sea breeze establish at the coast?
    def onset_at(stn):
        ser = [(h, d, s) for (h, d, s, _) in obs[stn]]
        return B.detect_onset(ser, cf, half=half, min_kt=6.0)

    onset_inshore = {s: onset_at(s) for s in INSHORE}
    onset_offshore = {s: onset_at(s) for s in OFFSHORE}

    def morning_onshore_frac(stn):
        ms = [d for (h, d, s, _) in obs[stn] if 6 <= h < 10 and d is not None]
        if not ms:
            return None
        return sum(1 for d in ms if abs(B.ang_diff(d, cf)) <= half) / len(ms)

    def is_genuine(stn, onset):
        """A real (thermal) sea breeze = an afternoon onshore onset that was NOT
        already onshore in the morning. If the morning was onshore, the afternoon
        onshore wind is the synoptic (Bernot's Q3/Q4 'breeze indistinct')."""
        if onset["onset_lt"] is None:
            return False
        mo = morning_onshore_frac(stn)
        return mo is None or mo < 0.5

    genuine_inshore = any(is_genuine(s, onset_inshore[s]) for s in INSHORE)
    ambiguous_inshore = any(onset_inshore[s]["onset_lt"] is not None and not is_genuine(s, onset_inshore[s]) for s in INSHORE)
    genuine_offshore = any(is_genuine(s, onset_offshore[s]) for s in OFFSHORE)
    any_inshore_fill = genuine_inshore
    any_offshore_fill = genuine_offshore

    # afternoon peak onshore-component speed (the "breeze" strength, not synoptic)
    def peak_onshore_kt(stn):
        vals = []
        for (h, d, s, _) in obs[stn]:
            if 11 <= h <= 19 and d is not None and s is not None:
                comp = s * math.cos(math.radians(B.ang_diff(d, cf)))   # component from seaward
                vals.append(max(0.0, comp))
        return max(vals) if vals else 0.0
    peak_breeze = max(peak_onshore_kt(s) for s in STATIONS)

    # STEP 1 — will it establish?
    gate_dt = (dt_c is not None and dt_c >= 2.0)
    gate_syn = (SYN_KT is not None and SYN_KT < 18.0)
    steps.append({
        "n": 1, "title": "Will the breeze establish?",
        "rule": "Necessary: land air warmer than sea by ≥2–3 °C AND synoptic < 18 kt (850 hPa proxy).",
        "prediction": {
            "dt_c": None if dt_c is None else round(dt_c, 1),
            "synoptic_kt": None if SYN_KT is None else round(SYN_KT, 1),
            "synoptic_dir": None if SYN_DIR is None else round(SYN_DIR, 0),
            "surface_gradient_kt": None if gspd is None else round(gspd, 1),
            "surface_gradient_dir": None if gdir is None else round(gdir, 0),
            "gate_pass": bool(gate_dt and gate_syn),
            "note": ("ΔT %.1f°C (favourable) but the 850 hPa synoptic is %.0f kt — %s. The light %.0f kt surface reading hides the real flow aloft."
                     % ((dt_c or 0), (SYN_KT or 0),
                        "at/over the ~18 kt ceiling, so a breeze is unlikely" if not gate_syn else "under the ceiling",
                        (gspd or 0))),
        },
        "validation": {
            "verdict": "yes" if genuine_inshore else ("info" if ambiguous_inshore else "no"),
            "detail": ("Coastal stations swung from offshore to a sustained onshore breeze — a thermal sea breeze."
                       if genuine_inshore else
                       ("Afternoon wind was onshore, but it was already onshore in the morning — this is the synoptic, not a distinct thermal breeze."
                        if ambiguous_inshore else
                        "Coastal stations (Logan/Beverly) never established a thermal onshore breeze — stayed offshore/synoptic all afternoon.")),
        },
    })

    # STEP 2 — quadrant (using the 850 hPa synoptic direction, Bernot's proxy)
    q = B.quadrant(SYN_DIR, cf)
    div = B.divergence_sign(SYN_DIR, cf)
    # observed behaviour classification for validation
    if genuine_inshore:
        obs_behav = "a thermal sea breeze filled at the coast"
    elif ambiguous_inshore:
        obs_behav = "afternoon wind was onshore but it is the synoptic (already onshore in the morning) — no distinct thermal breeze"
    elif genuine_offshore:
        obs_behav = "only weak offshore attempts — no coastal fill"
    else:
        obs_behav = "no thermal onshore fill anywhere — the synoptic held"
    q_expect = {"Q1": "early coastal fill", "Q2": "offshore-first attempts, may fail (combat douteux)",
                "Q3": "there-and-back, seems to reinforce synoptic", "Q4": "no lift"}.get(q["quadrant"] if q else None, "?")
    q_ok, q_verdict = None, "n/a"
    if q:
        Q = q["quadrant"]
        if Q == "Q1":
            q_ok = genuine_inshore; q_verdict = "yes" if q_ok else "no"
        elif Q == "Q2":
            q_ok = True; q_verdict = "info"          # combat douteux — either outcome is consistent
        elif Q == "Q3":
            q_ok = True; q_verdict = "info"
        else:  # Q4 — expect no thermal lift
            q_ok = (not genuine_inshore); q_verdict = "yes" if q_ok else "no"
    steps.append({
        "n": 2, "title": "Quadrant (synoptic vs coastline)",
        "rule": "Quadrant is defined relative to the COAST, not compass. Q1 best · Q2 combat douteux · Q3 round-trip · Q4 worst.",
        "prediction": {
            "coast_faces_deg": None if cf is None else round(cf, 0),
            "synoptic_from_deg": None if SYN_DIR is None else round(SYN_DIR, 0),
            "quadrant": q["quadrant"] if q else None,
            "from_land": q["from_land"] if q else None,
            "divergence": div["label"] if div else None,
            "behaviour": q["behaviour"] if q else None,
            "strategy": q["strategy"] if q else None,
            "expected": q_expect,
        },
        "validation": {
            "verdict": q_verdict,
            "detail": "Observed: %s." % obs_behav,
        },
    })

    # STEP 4 — Wisdorff force grid
    # instability from CAPE; 800 m return-current aid = 925 mb wind has an offshore
    # (from-land) component that would slide under the synoptic
    unstable = None if cape_mid is None else bool(cape_mid >= 150)
    wind800_aids = None
    if dir925 is not None and cf is not None:
        # aids if the ~750 m wind is from land (offshore) -> feeds the return current
        wind800_aids = bool(abs(B.ang_diff((dir925 + 180) % 360, cf)) < 90)
    air_temp_class = None
    if tmax is not None:
        air_temp_class = "hot" if tmax >= 28 else ("cool" if tmax <= 20 else "mild")
    # sunshine from morning cloud/insolation in the existing fields parquet (loaded above)
    sunshine_class = None
    if fields is not None:
        watergi = set(np.where(waterflat)[0].tolist())
        # 09-14 LT mean tcdc over water
        df = fields
        tcol = df.column("valid_time_utc").to_pylist(); gg = df.column("gi").to_pylist()
        tc = df.column("tcdc").to_pylist(); ds = df.column("dswrf").to_pylist()
        cl = [tc[i] for i in range(len(tc)) if gg[i] in watergi and 9 <= _lt(tcol[i]).hour <= 14 and tc[i] is not None]
        cloud_am = float(np.mean(cl)) if cl else None
        if cloud_am is not None:
            sunshine_class = "good" if cloud_am <= 30 else ("poor" if cloud_am >= 70 else "medium")
    # afternoon high tide?
    tide = _load(f"{PFX}/coops/8443970/hilo_{ymd[:4]}.parquet")
    aft_high = None
    if tide is not None:
        highs = [_lt(r["time_utc"]).hour for r in tide.to_pylist()
                 if str(_lt(r["time_utc"]).date()) == a.date and r.get("type") == "H"]
        aft_high = any(12 <= h <= 18 for h in highs) if highs else False
    w = B.wisdorff_score(
        air_warmer=(None if dt_c is None else dt_c > 0), synoptic_kt=(None if SYN_KT is None else SYN_KT),
        quad=(q["quadrant"] if q else None), unstable=unstable, wind800_aids=wind800_aids,
        air_temp_class=air_temp_class, syn_shifts_to_breeze=None, sunshine_class=sunshine_class,
        afternoon_high_tide=aft_high, shear_aloft=None)
    steps.append({
        "n": 4, "title": "Wisdorff force grid → predicted breeze strength",
        "rule": "Ten criteria (Bernot Table 1) sum to a score; Table 2 maps score→max afternoon breeze (kt).",
        "prediction": {
            "components": w["components"], "total": w["total"], "pred_kt": w["pred_kt"],
            "gate_fail": w["gate_fail"],
        },
        "validation": {
            "verdict": ("yes" if (peak_breeze <= max(3, w["pred_kt"]) + 4 and peak_breeze >= max(0, w["pred_kt"] - 6)) else "no"),
            "observed_peak_onshore_kt": round(peak_breeze, 1),
            "detail": "Observed afternoon peak onshore-component wind = %.1f kt (predicted max %d kt)." % (peak_breeze, w["pred_kt"]),
        },
    })

    # STEP 5 — onset cues (VIS horizon clearing) + observed onset
    vis_clear = None
    if len(vis_ts) >= 3:
        am = np.mean([v for h, v in vis_ts if 8 <= h <= 11]) if any(8 <= h <= 11 for h, _ in vis_ts) else None
        pm = np.mean([v for h, v in vis_ts if 12 <= h <= 16]) if any(12 <= h <= 16 for h, _ in vis_ts) else None
        vis_clear = (am is not None and pm is not None and pm > am + 1.0)
    obs_onset = min([o["onset_lt"] for o in {**onset_inshore, **onset_offshore}.values() if o["onset_lt"] is not None], default=None)
    steps.append({
        "n": 5, "title": "Onset cues (horizon clearing) & observed onset",
        "rule": "Offshore visibility rising = subsidence 'nettoyage de l'horizon' → imminent breeze (Bernot §2.3/§3.2).",
        "prediction": {"vis_offshore_km_by_hour": vis_ts, "horizon_cleared": vis_clear},
        "validation": {
            "verdict": ("yes" if obs_onset is not None else "no"),
            "observed_onset_lt": obs_onset,
            "detail": ("Onshore breeze onset observed ~%02d:%02d LT." % (int(obs_onset), int((obs_onset % 1) * 60))
                       if obs_onset is not None else "No sustained onshore onset observed."),
        },
    })

    # STEP 6 — rotation (only meaningful if it filled)
    dir_by_hour = {s: [[h, d] for (h, d, sp, _) in obs_h[s] if d is not None] for s in STATIONS}
    steps.append({
        "n": 6, "title": "Rotation (~10°/h right, Coriolis)",
        "rule": "Once established, a pure breeze veers right ~10°/hour.",
        "prediction": {"expected_rate_deg_per_h": 10, "applies": bool(obs_onset is not None)},
        "validation": {
            "verdict": "n/a" if obs_onset is None else "info",
            "obs_dir_by_hour": dir_by_hour,
            "detail": ("Breeze never established — rotation not applicable; wind direction shown is synoptic."
                       if obs_onset is None else "Observed hourly wind direction shown; compare the right-rotation."),
        },
    })

    # STEP 8 — offshore vs inshore contrast (transition zone / stronger-at-coast)
    def mean_onshore_comp(stns):
        vals = []
        for s in stns:
            for (h, d, sp, _) in obs[s]:
                if 12 <= h <= 18 and d is not None and sp is not None:
                    vals.append(sp * math.cos(math.radians(B.ang_diff(d, cf))))
        return round(float(np.mean(vals)), 1) if vals else None
    steps.append({
        "n": 8, "title": "Coast vs offshore (transition zone)",
        "rule": "A sea breeze is stronger at the coast; the breeze↔synoptic transition migrates offshore through the afternoon.",
        "prediction": {"note": "If a breeze, expect a stronger onshore component inshore than offshore."},
        "validation": {
            "verdict": "info",
            "inshore_onshore_comp_kt": mean_onshore_comp(INSHORE),
            "offshore_onshore_comp_kt": mean_onshore_comp(OFFSHORE),
            "detail": "Mean 12–18 LT onshore wind component, coast vs offshore. %s" % (
                "Both offshore/negative — no sea-breeze inflow either place." if (mean_onshore_comp(INSHORE) or 0) < 1 and (mean_onshore_comp(OFFSHORE) or 0) < 1
                else "Compare the onshore inflow coast vs offshore."),
        },
    })

    # ---- loop params for the cross-section ----------------------------------
    # HPBL (breeze depth) midday over water
    hpbl_h, _ = read_field_hourly(ymd, "surface/HPBL", midday_cyc or cyc)
    hpbl_mid = np.nanmean([water_mean(hpbl_h, c) for c in (midday_cyc or cyc) if water_mean(hpbl_h, c) is not None]) if hpbl_h else None
    loop = B.loop_params(dt_c=None if dt_c is None else round(dt_c, 1),
                         hpbl_m=None if hpbl_mid is None else round(float(hpbl_mid), 0),
                         wind850_kt=None if spd925_kt is None else round(spd925_kt, 1),
                         surface_breeze_kt=round(peak_breeze, 1))

    out = {
        "date": a.date,
        "venue": "Massachusetts Bay (Boston → Cape Ann → Cape Cod)",
        "coast_faces_deg": None if cf is None else round(cf, 0),
        "morning_synoptic": {"from_deg": None if SYN_DIR is None else round(SYN_DIR, 0),
                             "speed_kt": None if SYN_KT is None else round(SYN_KT, 1),
                             "source": "850 hPa (Bernot proxy)"},
        "surface_gradient": {"from_deg": None if gdir is None else round(gdir, 0),
                             "speed_kt": None if gspd is None else round(gspd, 1),
                             "source": "44013 buoy 06–10 LT"},
        "wind_925mb": {"from_deg": None if dir925 is None else round(dir925, 0),
                       "speed_kt": None if spd925_kt is None else round(spd925_kt, 1)},
        "dt_c": None if dt_c is None else round(dt_c, 1), "sst_c": sst, "tmax_c": tmax,
        "cape_midday": None if cape_mid is None else round(float(cape_mid), 0),
        "quadrant": q["quadrant"] if q else None,
        "wisdorff": {"total": w["total"], "pred_kt": w["pred_kt"]},
        "loop": loop,
        "steps": steps,
        "obs_hourly": {s: [[round(h, 2), d, None if sp is None else round(sp, 1)] for (h, d, sp, _) in obs_h[s]] for s in STATIONS},
        "stations_meta": STATIONS,
        "established": bool(genuine_inshore),
        "summary_verdict": _summary(q, dt_c, SYN_KT, genuine_inshore, ambiguous_inshore, peak_breeze),
    }

    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, (np.floating, float)):
            f = float(o)
            return None if math.isnan(f) else f
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.bool_,)):
            return bool(o)
        if isinstance(o, np.ndarray):
            return clean(o.tolist())
        return o

    out = clean(out)
    out_dir = a.out_dir or "climatology/_local/breeze"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{a.date}.json")
    json.dump(out, open(path, "w"), allow_nan=False)
    print("wrote %s" % path)
    print("  coast_faces=%s  synoptic=%s@%s  quadrant=%s  Wisdorff total=%s pred=%s kt  peak_onshore_obs=%.1f kt"
          % (out["coast_faces_deg"], out["morning_synoptic"]["from_deg"], out["morning_synoptic"]["speed_kt"],
             out["quadrant"], w["total"], w["pred_kt"], peak_breeze))
    print("  " + out["summary_verdict"])
    return out


if __name__ == "__main__":
    main()
