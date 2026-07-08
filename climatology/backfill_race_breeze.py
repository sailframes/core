#!/usr/bin/env python3
"""
backfill_race_breeze.py — the racing-area breeze climatology for the wind-rose-by-coast.

The Stats "breeze rotation rose" was buoy-based (44013 sits offshore in the SW corner
and misses the racing-area sea breeze). This computes, for every FILL day in the
archive, the racing-area (central Mass Bay) HRRR 10 m wind by solar hour (10–19 LT),
and aggregates a vector-mean per hour → race_rose.json for the client rose. Also
writes race_breeze.parquet (per day·hour) for reuse.

Fill days = labels.parquet type in {F,R,P}. LT→UTC = +4 (warm season = EDT).
hrrrzarr F00 analysis, race cells = 42.30–42.52 N, −70.95…−70.55 W (water).

Run: AWS_PROFILE=sailframes AWS_DEFAULT_REGION=us-east-1 \
     python3.11 climatology/backfill_race_breeze.py [--limit N]
"""
import argparse
import io
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor

import boto3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

import hrrr_grid as hg

BUCKET = os.environ.get("CLIMO_BUCKET", "sailframes-data-prod")
PFX = "climatology"
KT = 1.943844
WORKERS = 16
_s3 = boto3.client("s3")


def race_gi(grid):
    lats = np.array(grid["lats"]); lons = np.array(grid["lons"]); land = np.array(grid["land_mask"])
    race = (lats >= 42.30) & (lats <= 42.52) & (lons >= -70.95) & (lons <= -70.55) & (land == 0)
    return np.where(race)[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    grid = json.load(io.BytesIO(_s3.get_object(Bucket=BUCKET, Key=f"{PFX}/grid.json")["Body"].read()))
    rgi = race_gi(grid)
    lab = pq.read_table(io.BytesIO(_s3.get_object(Bucket=BUCKET, Key=f"{PFX}/labels.parquet")["Body"].read()))
    dates = [r["date"] for r in lab.to_pylist() if r["type"] in ("F", "R", "P")]
    dates = sorted(set(dates))
    if a.limit:
        dates = dates[:: max(1, len(dates) // a.limit)][:a.limit]
    print(f"fill days: {len(dates)}")

    HOURS = list(range(10, 20))                     # 10–19 LT
    tasks = []                                       # (date, h_lt, cyc)
    for d in dates:
        ymd = d.replace("-", "")
        for h in HOURS:
            tasks.append((ymd, d, h, "%02dz" % ((h + 4) % 24)))   # EDT -> UTC

    # window is constant across all dates (same projection grid) — compute once
    j0, j1, i0, i1 = hg.bbox_window(hg.store_anl(tasks[0][0], tasks[0][3]))

    def read_one(t):
        ymd, d, h, cyc = t
        try:
            store = hg.store_anl(ymd, cyc)
            u = hg.read_window(store, hg.REQUIRED["u10"], j0, j1, i0, i1).ravel()[rgi]
            v = hg.read_window(store, hg.REQUIRED["v10"], j0, j1, i0, i1).ravel()[rgi]
            um = float(np.nanmean(u)); vm = float(np.nanmean(v))
            if um != um:
                return None
            return (d, h, um, vm)
        except Exception:
            return None

    rows = []
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for res in ex.map(read_one, tasks):
            done += 1
            if done % 1000 == 0:
                print(f"  {done}/{len(tasks)}", flush=True)
            if res:
                rows.append(res)
    print(f"got {len(rows)} day·hour samples")

    # per-day·hour parquet
    tbl = pa.table({
        "date": pa.array([r[0] for r in rows]),
        "h_lt": pa.array([r[1] for r in rows], type=pa.int16()),
        "from_deg": pa.array([round((math.degrees(math.atan2(r[2], r[3])) + 180) % 360, 0) for r in rows], type=pa.float32()),
        "spd_kt": pa.array([round(math.hypot(r[2], r[3]) * KT, 1) for r in rows], type=pa.float32()),
    })
    buf = io.BytesIO(); pq.write_table(tbl, buf, compression="zstd"); buf.seek(0)
    _s3.put_object(Bucket=BUCKET, Key=f"{PFX}/race_breeze.parquet", Body=buf.getvalue(),
                   ContentType="application/octet-stream", CacheControl="max-age=3600")

    # aggregate rose: vector-mean per hour over all fill days
    byh = {}
    for d, h, um, vm in rows:
        acc = byh.setdefault(h, [0.0, 0.0, 0.0, 0])
        acc[0] += um; acc[1] += vm; acc[2] += math.hypot(um, vm); acc[3] += 1
    hours = []
    for h in HOURS:
        if h not in byh:
            continue
        su, sv, ss, n = byh[h]
        mu, mv = su / n, sv / n
        frm = (math.degrees(math.atan2(mu, mv)) + 180) % 360
        meanvec = math.hypot(mu, mv); meanspd = ss / n
        hours.append({"h": h, "from": round(frm, 0),
                      "consistency": round(meanvec / meanspd if meanspd else 0, 3),
                      "mean_kt": round(meanspd * KT, 1), "n": n})
    rose = {"hours": hours, "n_days": len(dates), "source": "racing-area HRRR field (fill days F/R/P)"}
    _s3.put_object(Bucket=BUCKET, Key=f"{PFX}/race_rose.json", Body=json.dumps(rose).encode(),
                   ContentType="application/json", CacheControl="max-age=3600")
    print("wrote race_rose.json:", json.dumps(rose))


if __name__ == "__main__":
    main()
