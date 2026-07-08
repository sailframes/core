#!/usr/bin/env python3
"""
backfill_subh.py — HRRR sub-hourly (15-min) 10 m wind over the bbox.

The hourly HRRR archive we use is zarr; the 15-min sub-hourly output ("subh" /
wrfsubhf) is GRIB2 only. Each wrfsubhfFF holds the FF-th forecast hour's 15/30/45-min
steps (f01 -> run+0:15/0:30/0:45, f02 -> +1:15/…). The subh field is on the IDENTICAL
HRRR 3km grid as our zarr fields (verified: window corners match grid.json), so we
extract the same (j0..j1, i0..i1) window row-major -> same `gi`. Wind-only (subh has
no MSLP/TCDC), which is all the replay needs for 15-min frames.

Combine with the hourly feed to get a 15-min "today's forecast".
Needs eccodes. AWS anon (noaa-hrrr-bdp-pds, us-east-1).
Run:  python3 climatology/backfill_subh.py --date 20260706 --cycle 12z --grid climatology/grid.json --out climatology/_local/subh.parquet
"""
import argparse
import datetime as dt
import json
import os
import tempfile

import boto3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from botocore import UNSIGNED
from botocore.config import Config

BUCKET = "noaa-hrrr-bdp-pds"
KEYFMT = "hrrr.{date}/conus/hrrr.t{hh}z.wrfsubhf{ff:02d}.grib2"
_s3 = boto3.client("s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED))


def _ranges(idx_text, var):
    """Byte ranges for INSTANTANEOUS 10 m `var` forecasts (skip averages/accums,
    whose labels contain 'ave'/'acc'/'-'). The exact minute is read from the GRIB."""
    lines = idx_text.strip().split("\n")
    out = []
    for i, l in enumerate(lines):
        f = l.split(":")
        if f[3] == var and f[4] == "10 m above ground" and not any(x in f[5] for x in ("ave", "acc", "-")):
            start = int(f[1])
            end = (int(lines[i + 1].split(":")[1]) - 1) if i + 1 < len(lines) else None
            out.append((start, end))
    return out


def _read_msgs(buf):
    """Decode all GRIB messages in buf -> list of (validity_iso, values_1d)."""
    import eccodes as ec
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as tf:
        tf.write(buf); path = tf.name
    out = []
    try:
        with open(path, "rb") as f:
            while True:
                gid = ec.codes_grib_new_from_file(f)
                if gid is None:
                    break
                vdate = ec.codes_get(gid, "validityDate"); vtime = ec.codes_get(gid, "validityTime")
                out.append((vdate, vtime, ec.codes_get_values(gid)))
                ec.codes_release(gid)
    finally:
        os.unlink(path)
    return out


def read_subh(date, hh, window, nlead=18):
    j0, j1, i0, i1 = window
    Ni = 1799
    rows_t, rows_gi, rows_u, rows_v = [], [], [], []
    ncell = (j1 - j0 + 1) * (i1 - i0 + 1)
    gi = np.arange(ncell, dtype="uint16")
    for ff in range(1, nlead + 1):
        key = KEYFMT.format(date=date, hh=hh, ff=ff)
        try:
            idx = _s3.get_object(Bucket=BUCKET, Key=key + ".idx")["Body"].read().decode()
        except Exception:
            continue
        urs = _ranges(idx, "UGRD"); vrs = _ranges(idx, "VGRD")
        for (ua, ub), (va, vb) in zip(urs, vrs):
            um = _read_msgs(_s3.get_object(Bucket=BUCKET, Key=key, Range=f"bytes={ua}-{ub}")["Body"].read())[0]
            vm = _read_msgs(_s3.get_object(Bucket=BUCKET, Key=key, Range=f"bytes={va}-{vb}")["Body"].read())[0]
            vd, vt = um[0], um[1]
            if vt % 100 not in (15, 30, 45):     # sub-hour steps only (hours come from the hourly feed)
                continue
            valid = dt.datetime(vd // 10000, (vd // 100) % 100, vd % 100, vt // 100, vt % 100)
            u2d = um[2].reshape(-1, Ni)[j0:j1 + 1, i0:i1 + 1].ravel()
            v2d = vm[2].reshape(-1, Ni)[j0:j1 + 1, i0:i1 + 1].ravel()
            rows_t.append(np.full(ncell, np.datetime64(valid, "s")))
            rows_gi.append(gi); rows_u.append(u2d.astype("f4")); rows_v.append(v2d.astype("f4"))
    if not rows_t:
        return None
    return pa.table({
        "valid_time_utc": pa.array(np.concatenate(rows_t), type=pa.timestamp("s", tz="UTC")),
        "gi": pa.array(np.concatenate(rows_gi)),
        "u10": pa.array(np.concatenate(rows_u), type=pa.float32()),
        "v10": pa.array(np.concatenate(rows_v), type=pa.float32()),
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--cycle", default="12z")
    ap.add_argument("--grid", default="climatology/grid.json")
    ap.add_argument("--out", default="climatology/_local/subh.parquet")
    ap.add_argument("--nlead", type=int, default=18)
    a = ap.parse_args()
    w = json.load(open(a.grid))["window"]
    window = (w["j0"], w["j1"], w["i0"], w["i1"])
    tbl = read_subh(a.date, a.cycle.replace("z", ""), window, a.nlead)
    if tbl is None:
        print(f"subh {a.date} {a.cycle}: unavailable"); return
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    pq.write_table(tbl, a.out, compression="zstd")
    nsteps = len(set(x.value for x in tbl.column("valid_time_utc")))
    print(f"subh {a.date} {a.cycle}: {nsteps} sub-hourly (15-min) steps, {tbl.num_rows} rows -> {a.out}")


if __name__ == "__main__":
    main()
