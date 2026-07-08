#!/usr/bin/env python3
"""
hourly_update.py — the climatology-hourly.yml job body (spec §5, in-season).

Builds the race-morning briefing input from the latest HRRR run:
  today/latest.parquet  — F00-F18 forecast fields over the bbox (same schema as
                          the daily fields, so /tactics renders it as "today's
                          forecast" in the replay map).
  today/briefing.json   — today's feature vector (morning gradient from obs-so-
                          far + forecast dT/cloud/SST + DOY) for the client-side
                          analog finder (spec §7) to match against labels.parquet.

F00 = latest _anl (analysis / current state); F01-F18 = latest _fcst. Grid.json
(land mask) is downloaded from S3. AWS creds/region from the environment.
Run from repo root:  python climatology/hourly_update.py
"""
import datetime as dt
import json
import math
import os
import sys
from zoneinfo import ZoneInfo

import boto3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests

import hrrr_grid as hg
from backfill_hrrr import FIELDS, SCHEMA
from backfill_ndbc import parse_block, REALTIME_URL

TZ = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
KT = 1.943844
BUCKET = os.environ.get("CLIMO_BUCKET", "sailframes-data-prod")
PFX = "climatology"
DIST_ID = os.environ.get("CLIMO_DIST_ID", "EFO342DVGM3QS")
WORK = os.environ.get("CLIMO_WORK", "_work")

s3 = boto3.client("s3")
cf = boto3.client("cloudfront")


def log(m):
    print(f"[hourly] {m}", flush=True)


def latest_cycle_with_fcst(date):
    both = sorted(set(hg.list_anl_cycles(date)) & set(hg.list_fcst_cycles(date)))
    return both[-1] if both else None


def cycle_dt(date, cyc):
    return dt.datetime(int(date[:4]), int(date[4:6]), int(date[6:8]), int(cyc[:2]), tzinfo=UTC)


def latest_rtma(max_back=6):
    """Newest available RTMA analysis cycle (walk back from now, ~1 h latency)."""
    from backfill_rtma import BUCKET as RB, KEYFMT as RK, _s3 as rs3
    now = dt.datetime.now(UTC)
    for back in range(max_back):
        t = now - dt.timedelta(hours=back)
        d, hh = t.strftime("%Y%m%d"), t.strftime("%H")
        try:
            rs3.head_object(Bucket=RB, Key=RK.format(date=d, hh=hh) + ".idx")
            return d, hh
        except Exception:
            continue
    return None, None


def build_forecast_table(date, cyc, window):
    """F00 (analysis) + F01..F18 (forecast) -> fields Table."""
    j0, j1, i0, i1 = window
    ncell = (j1 - j0 + 1) * (i1 - i0 + 1)
    gi = np.arange(ncell, dtype="uint16")
    base = cycle_dt(date, cyc)
    times, gicol = [], []
    cols = {f: [] for f in FIELDS}

    anl = hg.store_anl(date, cyc)
    a0 = {f: hg.read_window(anl, hg.REQUIRED[f], j0, j1, i0, i1).ravel() for f in FIELDS}
    times.append(np.full(ncell, np.datetime64(base.replace(tzinfo=None), "s"))); gicol.append(gi)
    for f in FIELDS:
        cols[f].append(a0[f])

    fc = hg.store_fcst(date, cyc)
    cube = {f: hg.read_fcst_window(fc, hg.REQUIRED[f], j0, j1, i0, i1, nlead=18) for f in FIELDS}
    nlead = cube[FIELDS[0]].shape[0]
    for k in range(nlead):
        vt = base + dt.timedelta(hours=k + 1)
        times.append(np.full(ncell, np.datetime64(vt.replace(tzinfo=None), "s"))); gicol.append(gi)
        for f in FIELDS:
            cols[f].append(cube[f][k].ravel())

    return pa.table(
        {"valid_time_utc": pa.array(np.concatenate(times), type=pa.timestamp("s", tz="UTC")),
         "gi": pa.array(np.concatenate(gicol)),
         **{f: pa.array(np.concatenate(cols[f]), type=pa.float32()) for f in FIELDS}},
        schema=SCHEMA), base, nlead


def morning_gradient(date):
    """Vector-mean 44013 wind 06-10 LT today (obs-so-far). Falls back to the
    latest few hours if it's still before 10 LT. Returns (u,v,spd_kt,dir,sst)."""
    r = requests.get(REALTIME_URL.format(stn="44013"), timeout=30)
    byt = parse_block(r.text) if r.status_code == 200 else {}
    today = dt.datetime.now(TZ).date()
    rows = []
    for ep, v in byt.items():
        lt = dt.datetime.fromtimestamp(ep, UTC).astimezone(TZ)
        if lt.date() == today and v.get("wdir") is not None and v.get("wspd") is not None:
            rows.append((lt.hour + lt.minute / 60, v["wdir"], v["wspd"], v.get("t_water")))
    rows.sort()
    morn = [(d, s) for h, d, s, _ in rows if 6 <= h < 10] or [(d, s) for _, d, s, _ in rows[-4:]]
    u = v = 0.0
    for d, s in morn:
        u += -s * math.sin(math.radians(d)); v += -s * math.cos(math.radians(d))
    n = max(1, len(morn))
    u /= n; v /= n
    spd = math.hypot(u, v)
    frm = math.degrees(math.atan2(-u, -v)) % 360 if spd > 0 else None
    ssts = [t for _, _, _, t in rows if t is not None]
    return u, v, spd * KT, frm, (ssts[-1] if ssts else None)


def briefing_features(table, base, grid, sst):
    """Forecast-side features: dT (max land T2 - SST), cloud/dswrf morning means."""
    land = np.array(grid["land_mask"])
    water_gi = set(np.where(land == 0)[0].tolist())
    land_gi = set(np.where(land == 1)[0].tolist())
    df = table.to_pydict()
    gi = np.array(df["gi"]); t2 = np.array(df["t2"]); tcdc = np.array(df["tcdc"]); dswrf = np.array(df["dswrf"])
    vt = df["valid_time_utc"]                     # tz-aware datetimes from pyarrow
    is_land = np.array([g in land_gi for g in gi.tolist()])
    is_water = np.array([g in water_gi for g in gi.tolist()])
    # forecast max 2 m T over land (proxy for daytime Tmax) -> dT
    tmax_land = np.nanmax(t2[is_land]) if is_land.any() else np.nan
    dt_c = (float(tmax_land) - 273.15 - sst) if (sst is not None and np.isfinite(tmax_land)) else None
    # morning cloud/insolation over water (09-14 LT of forecast frames)
    morning = np.array([9 <= x.astimezone(TZ).hour <= 14 for x in vt]) & is_water
    cloud = float(np.nanmean(tcdc[morning])) if morning.any() else None
    dswrf_am = float(np.nanmean(dswrf[morning])) if morning.any() else None
    return dt_c, cloud, dswrf_am


def main():
    os.makedirs(WORK, exist_ok=True)
    today = dt.datetime.now(UTC).date()
    if not ((4, 15) <= (today.month, today.day) <= (10, 15)):
        log(f"{today} out of season (Apr15-Oct15) — hourly briefing feed skipped"); return
    ymd = today.strftime("%Y%m%d")
    cyc = latest_cycle_with_fcst(ymd)
    if cyc is None:  # early UTC: fall back to yesterday's latest run
        ymd = (today - dt.timedelta(days=1)).strftime("%Y%m%d")
        cyc = latest_cycle_with_fcst(ymd)
    if cyc is None:
        log("no cycle with forecast available; abort"); return
    log(f"latest run {ymd} {cyc}")

    window = hg.bbox_window(hg.store_anl(ymd, cyc))
    table, base, nlead = build_forecast_table(ymd, cyc, window)   # hourly, all 8 fields
    # add 15-min sub-hourly wind from HRRR subh (GRIB2) — non-fatal, wind-only
    merged = table
    try:
        from backfill_subh import read_subh
        subh = read_subh(ymd, cyc.replace("z", ""), window, nlead)
        if subh is not None and subh.num_rows:
            n = subh.num_rows
            cols = {"valid_time_utc": subh.column("valid_time_utc"), "gi": subh.column("gi"),
                    "u10": subh.column("u10"), "v10": subh.column("v10")}
            for f in ("gust", "t2", "td2", "mslp", "tcdc", "dswrf"):
                cols[f] = pa.array(np.full(n, None), type=pa.float32())
            merged = pa.concat_tables([table, pa.table(cols, schema=SCHEMA)]) \
                .sort_by([("valid_time_utc", "ascending"), ("gi", "ascending")])
            log(f"added {n} sub-hourly (15-min) wind rows")
    except Exception as e:
        log(f"WARN subh 15-min merge failed ({e}); hourly-only feed")
    out = os.path.join(WORK, "latest.parquet")
    pq.write_table(merged, out, compression="zstd")
    s3.upload_file(out, BUCKET, f"{PFX}/today/latest.parquet",
                   ExtraArgs={"ContentType": "application/octet-stream", "CacheControl": "max-age=120"})
    log(f"today/latest.parquet: F00-F{nlead} ({merged.num_rows} rows) from {base:%Y-%m-%dT%H}Z")

    # briefing feature vector for the analog finder
    s3.download_file(BUCKET, f"{PFX}/grid.json", os.path.join(WORK, "grid.json"))
    grid = json.load(open(os.path.join(WORK, "grid.json")))
    gu, gv, gspd_kt, gdir, sst = morning_gradient(ymd)
    dt_c, cloud, dswrf_am = briefing_features(table, base, grid, sst)
    doy = today.timetuple().tm_yday
    brief = {
        "date": today.isoformat(), "run": f"{base.isoformat()}", "cycle": cyc,
        "grad_u": round(gu, 2), "grad_v": round(gv, 2),
        "grad_spd_kt": round(gspd_kt, 1), "grad_dir": None if gdir is None else round(gdir, 0),
        "dt_c": None if dt_c is None else round(dt_c, 1),
        "tcdc_am": None if cloud is None else round(cloud, 0),
        "dswrf_am": None if dswrf_am is None else round(dswrf_am, 0),
        "sst": None if sst is None else round(sst, 1),
        "doy": doy, "sin_doy": round(math.sin(2 * math.pi * doy / 365), 4),
        "cos_doy": round(math.cos(2 * math.pi * doy / 365), 4),
    }
    bpath = os.path.join(WORK, "briefing.json")
    json.dump(brief, open(bpath, "w"))
    s3.upload_file(bpath, BUCKET, f"{PFX}/today/briefing.json",
                   ExtraArgs={"ContentType": "application/json", "CacheControl": "max-age=120"})
    log(f"today/briefing.json: {brief}")

    # live RTMA truth overlay — newest available obs-anchored 2.5 km analysis over
    # the bbox (model-confirmation layer for the today's-forecast map). Non-fatal.
    rtma_ok = False
    try:
        from backfill_rtma import read_cycle
        rd, rh = latest_rtma()
        if rd:
            rt = read_cycle(rd, rh)
            if rt is not None and rt.num_rows:
                rmeta = {"cycle": f"{rd}T{rh}Z", "n": rt.num_rows,
                         "mean_kt": round(float(np.mean(rt.column("wspd_kt").to_numpy())), 1)}
                rpath = os.path.join(WORK, "rtma.parquet")
                pq.write_table(rt, rpath, compression="zstd")
                s3.upload_file(rpath, BUCKET, f"{PFX}/today/rtma.parquet",
                               ExtraArgs={"ContentType": "application/octet-stream", "CacheControl": "max-age=120"})
                s3.put_object(Bucket=BUCKET, Key=f"{PFX}/today/rtma.json",
                              Body=json.dumps(rmeta).encode(), ContentType="application/json", CacheControl="max-age=120")
                rtma_ok = True
                log(f"today/rtma.parquet: {rmeta}")
    except Exception as e:
        log(f"WARN RTMA overlay failed ({e}); forecast feed unaffected")

    paths = [f"/{PFX}/today/latest.parquet", f"/{PFX}/today/briefing.json"]
    if rtma_ok:
        paths += [f"/{PFX}/today/rtma.parquet", f"/{PFX}/today/rtma.json"]
    r = cf.create_invalidation(DistributionId=DIST_ID,
                               InvalidationBatch={"Paths": {"Quantity": len(paths), "Items": paths},
                                                  "CallerReference": f"climo-hourly-{base:%Y%m%d%H}"})
    log(f"invalidation {r['Invalidation']['Id']}")
    log("done")


if __name__ == "__main__":
    main()
