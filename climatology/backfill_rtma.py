#!/usr/bin/env python3
"""
backfill_rtma.py — NOAA RTMA/URMA 2.5 km surface analysis, 10 m wind over the bbox.

RTMA is the obs-anchored surface analysis (URMA = the NWS "Analysis of Record"),
a stronger truth than HRRR F00 for validating the model — it blends far more
surface observations. This reads just the 10 m U/V GRIB2 messages via the .idx
byte-range (≈11 MB, not the 81 MB full CONUS file), crops to the bbox, and writes
a per-cycle Parquet (rtma/{date}/{HH}.parquet): lat, lon, u10, v10, wspd, wdir.

Needs eccodes (pip install eccodes cfgrib). AWS anon (noaa-rtma-pds, us-east-1).
Run:  python3 climatology/backfill_rtma.py --date 20260706 --cycle 12z --out-dir climatology/_local
"""
import argparse
import math
import os
import tempfile

import boto3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from botocore import UNSIGNED
from botocore.config import Config

import hrrr_grid as hg  # reuse BBOX

BUCKET = "noaa-rtma-pds"
KEYFMT = "rtma2p5.{date}/rtma2p5.t{hh}z.2dvaranl_ndfd.grb2_wexp"
KT = 1.943844
_s3 = boto3.client("s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED))


def _byte_ranges(idx_text, wanted):
    """From the GRIB .idx, byte [start,end] for each (var, level) in `wanted`."""
    lines = idx_text.strip().split("\n")
    out = {}
    for i, l in enumerate(lines):
        f = l.split(":")
        key = (f[3], f[4])
        if key in wanted:
            start = int(f[1])
            end = (int(lines[i + 1].split(":")[1]) - 1) if i + 1 < len(lines) else None
            out[key] = (start, end)
    return out


def read_cycle(date, hh):
    key = KEYFMT.format(date=date, hh=hh)
    idx = _s3.get_object(Bucket=BUCKET, Key=key + ".idx")["Body"].read().decode()
    rng = _byte_ranges(idx, {("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")})
    if len(rng) < 2:
        return None
    buf = b""
    for k in (("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")):
        a, b = rng[k]
        r = f"bytes={a}-{b}" if b is not None else f"bytes={a}-"
        buf += _s3.get_object(Bucket=BUCKET, Key=key, Range=r)["Body"].read()
    import eccodes as ec
    msg = {}
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as tf:
        tf.write(buf); path = tf.name
    try:
        with open(path, "rb") as f:
            while True:
                gid = ec.codes_grib_new_from_file(f)
                if gid is None:
                    break
                msg[ec.codes_get(gid, "shortName")] = {
                    "v": ec.codes_get_values(gid),
                    "lat": ec.codes_get_array(gid, "latitudes"),
                    "lon": ec.codes_get_array(gid, "longitudes")}
                ec.codes_release(gid)
    finally:
        os.unlink(path)
    u, v = msg.get("10u"), msg.get("10v")
    if u is None or v is None:
        return None
    lat = u["lat"]; lon = np.where(u["lon"] > 180, u["lon"] - 360, u["lon"])
    inb = ((lat >= hg.BBOX["lat_min"]) & (lat <= hg.BBOX["lat_max"]) &
           (lon >= hg.BBOX["lon_min"]) & (lon <= hg.BBOX["lon_max"]))
    idx_in = np.where(inb)[0]
    uu = u["v"][idx_in]; vv = v["v"][idx_in]
    spd = np.hypot(uu, vv) * KT
    wdir = (np.degrees(np.arctan2(-uu, -vv)) + 360) % 360
    return pa.table({
        "lat": pa.array(lat[idx_in], type=pa.float32()),
        "lon": pa.array(lon[idx_in], type=pa.float32()),
        "u10": pa.array(uu, type=pa.float32()), "v10": pa.array(vv, type=pa.float32()),
        "wspd_kt": pa.array(spd, type=pa.float32()), "wdir": pa.array(wdir, type=pa.float32()),
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--cycle", default="12z")
    ap.add_argument("--out-dir", default="climatology/_local")
    a = ap.parse_args()
    hh = a.cycle.replace("z", "")
    tbl = read_cycle(a.date, hh)
    if tbl is None:
        print(f"RTMA {a.date} {a.cycle}: unavailable"); return
    out = os.path.join(a.out_dir, "rtma", a.date, f"{hh}.parquet")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    pq.write_table(tbl, out, compression="zstd")
    print(f"RTMA {a.date} {a.cycle}: {tbl.num_rows} bbox pts (2.5km) -> {out} "
          f"[mean {np.mean(tbl.column('wspd_kt').to_numpy()):.1f} kt]")


if __name__ == "__main__":
    main()
