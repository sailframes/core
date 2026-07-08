#!/usr/bin/env python3
"""
compute_validation.py — aggregate model-vs-obs skill over the whole field archive.

For every backfilled HRRR field day, sample the model at each obs station's nearest
grid cell and compare to the observed wind (matched within ±30 min). Emit a small
validation.json the /tactics UI shows as a venue "model skill" card:
  per station + overall: speed bias (HRRR−obs), speed RMSE, mean |dir error|, N.

Reads local Parquet only (no network). Run:
  python3 climatology/compute_validation.py --local climatology/_local --grid climatology/grid.json --stations climatology/obs_stations.json --out climatology/_local/validation.json
"""
import argparse
import datetime as dt
import glob
import json
import math
import os

import numpy as np
import pyarrow.parquet as pq

KT = 1.943844


def nearest_gi(grid, lat, lon):
    lats = np.array(grid["lats"]); lons = np.array(grid["lons"])
    cw = math.cos(lat * math.pi / 180)
    d = (lats - lat) ** 2 + ((lons - lon) * cw) ** 2
    return int(np.argmin(d))


def load_obs_year(local, station, yr):
    f = os.path.join(local, "obs", station, f"{yr}.parquet")
    if not os.path.exists(f):
        return None
    t = pq.read_table(f, columns=["time_utc", "wspd", "wdir"])
    ep = np.array([int(x.timestamp()) for x in t.column("time_utc").to_pylist()])
    return {"ep": ep, "wspd": t.column("wspd").to_numpy(zero_copy_only=False),
            "wdir": t.column("wdir").to_numpy(zero_copy_only=False)}


def ang_diff(a, b):
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", default="climatology/_local")
    ap.add_argument("--grid", default="climatology/grid.json")
    ap.add_argument("--stations", default="climatology/obs_stations.json")
    ap.add_argument("--out", default="climatology/_local/validation.json")
    a = ap.parse_args()
    grid = json.load(open(a.grid))
    stations = json.load(open(a.stations))
    for s in stations:
        s["gi"] = nearest_gi(grid, s["lat"], s["lon"])

    # accumulators per station
    acc = {s["id"]: {"n": 0, "sum_bias": 0.0, "sum_sq": 0.0, "sum_dir": 0.0} for s in stations}
    obs_cache = {}
    field_files = sorted(glob.glob(os.path.join(a.local, "year=*/month=*/*.parquet")))
    days = 0
    for ff in field_files:
        # parse date from path year=YYYY/month=MM/DD.parquet
        parts = ff.replace("\\", "/").split("/")
        yr = parts[-3].split("=")[1]; mo = parts[-2].split("=")[1]; dd = parts[-1][:2]
        gilist = sorted({s["gi"] for s in stations})
        t = pq.read_table(ff, columns=["valid_time_utc", "gi", "u10", "v10"])
        gi = t.column("gi").to_numpy(zero_copy_only=False)
        mask = np.isin(gi, gilist)
        if not mask.any():
            continue
        vt = np.array([int(x.timestamp()) for x in t.column("valid_time_utc").to_pylist()])
        u = t.column("u10").to_numpy(zero_copy_only=False)
        v = t.column("v10").to_numpy(zero_copy_only=False)
        # index model by (gi, epoch)
        model = {}
        for i in np.where(mask)[0]:
            model[(int(gi[i]), int(vt[i]))] = (float(u[i]), float(v[i]))
        for s in stations:
            ok = obs_cache.get((s["id"], yr), "miss")
            if ok == "miss":
                ok = load_obs_year(a.local, s["id"], yr); obs_cache[(s["id"], yr)] = ok
            if ok is None:
                continue
            for (g, ep), (mu, mv) in model.items():
                if g != s["gi"]:
                    continue
                if not (np.isfinite(mu) and np.isfinite(mv)):
                    continue
                # nearest obs within 30 min
                k = int(np.argmin(np.abs(ok["ep"] - ep)))
                if abs(ok["ep"][k] - ep) > 1800:
                    continue
                ows, owd = ok["wspd"][k], ok["wdir"][k]
                if not (np.isfinite(ows) and np.isfinite(owd)):
                    continue
                ows, owd = float(ows), float(owd)
                mspd = math.hypot(mu, mv) * KT
                mdir = (math.degrees(math.atan2(-mu, -mv)) + 360) % 360
                A = acc[s["id"]]
                A["n"] += 1
                A["sum_bias"] += mspd - ows * KT
                A["sum_sq"] += (mspd - ows * KT) ** 2
                A["sum_dir"] += ang_diff(mdir, owd)
        days += 1

    out = {"generated_days": days, "stations": []}
    tot = {"n": 0, "sum_bias": 0.0, "sum_sq": 0.0, "sum_dir": 0.0}
    by_id = {s["id"]: s for s in stations}
    for sid, A in acc.items():
        for k in tot:
            tot[k] += A[k]
        if A["n"]:
            out["stations"].append({
                "id": sid, "name": by_id[sid]["name"], "type": by_id[sid]["type"], "n": A["n"],
                "bias_kt": round(A["sum_bias"] / A["n"], 2),
                "rmse_kt": round(math.sqrt(A["sum_sq"] / A["n"]), 2),
                "dir_err": round(A["sum_dir"] / A["n"], 1),
            })
    out["stations"].sort(key=lambda x: x["id"])
    if tot["n"]:
        out["overall"] = {"n": tot["n"], "bias_kt": round(tot["sum_bias"] / tot["n"], 2),
                          "rmse_kt": round(math.sqrt(tot["sum_sq"] / tot["n"]), 2),
                          "dir_err": round(tot["sum_dir"] / tot["n"], 1)}
    json.dump(out, open(a.out, "w"), indent=0)
    print(f"wrote {a.out}: {len(out['stations'])} stations, {out.get('overall', {}).get('n', 0)} station·hours over {days} days")
    for s in out["stations"]:
        print(f"  {s['id']:6s} n={s['n']:5d}  bias {s['bias_kt']:+.1f} kt  RMSE {s['rmse_kt']:.1f} kt  dir {s['dir_err']:.0f}°")


if __name__ == "__main__":
    main()
