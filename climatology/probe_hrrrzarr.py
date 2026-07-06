#!/usr/bin/env python3
"""
probe_hrrrzarr.py — Phase 0 spike tool for TACTICS_CLIMATOLOGY_SPEC.

Confirms the hrrrzarr archive can supply the fields the classifier needs,
resolves the bbox -> HRRR grid-index window (via Lambert Conformal
projection), and value-checks the fields that matter (esp. F00 DSWRF,
which can be degenerate in other HRRR products but is NOT in hrrrzarr).

hrrrzarr layout (public, anonymous, us-west-1):
    s3://hrrrzarr/sfc/YYYYMMDD/YYYYMMDD_HHz_anl.zarr/<level>/<var>/<level>/<var>/<cy>.<cx>
  - _anl store per hour = F00 analysis. 24 hourly cycles/day from 2017-01
    onward (2016 begins ragged on 2016-08-23 with 4 cycles).
  - CONUS grid (ny=1059, nx=1799), chunked 150x150, blosc-compressed f4.
  - _fcst stores also exist (F01+, forecast_period axis) — the spec's
    radiation fallback, but UNNEEDED: F00 DSWRF is valid (see findings).

Requires numcodecs + numpy + boto3. On this box numcodecs is only in the
homebrew python3.11, NOT the default python3 (3.14):
    /opt/homebrew/opt/python@3.11/bin/python3.11 climatology/probe_hrrrzarr.py

This is a spike artifact and the seed for climatology/backfill_hrrr.py.
"""
import argparse
import json
import math

import boto3
import numpy as np
from botocore import UNSIGNED
from botocore.config import Config
from numcodecs import Blosc

BUCKET = "hrrrzarr"
REGION = "us-west-1"

# --- bbox from TACTICS_CLIMATOLOGY_SPEC §2 (Gloucester -> Provincetown) ---
BBOX = dict(lat_min=41.95, lat_max=42.75, lon_min=-71.10, lon_max=-69.85)

# --- HRRR Lambert Conformal Conic projection (sphere) ---
R_EARTH = 6371229.0
LAT0 = 38.5   # lat_1 == lat_2 == lat_0 for HRRR CONUS
LON0 = -97.5
D2R = math.pi / 180.0

# The 8 fields the classifier/replay need (spec §3 / §4 fields schema),
# keyed to their hrrrzarr <level>/<var> group.
REQUIRED = {
    "u10":  "10m_above_ground/UGRD",
    "v10":  "10m_above_ground/VGRD",
    "gust": "surface/GUST",
    "t2":   "2m_above_ground/TMP",
    "td2":  "2m_above_ground/DPT",
    "mslp": "mean_sea_level/MSLMA",
    "tcdc": "entire_atmosphere/TCDC",
    "dswrf": "surface/DSWRF",
}
# useful bonuses hrrrzarr carries for free (land mask, sea-breeze depth, cloud split)
BONUS = {
    "land": "surface/LAND",
    "hpbl": "surface/HPBL",
    "lcdc": "low_cloud_layer/LCDC",
}

_s3 = boto3.client("s3", region_name=REGION, config=Config(signature_version=UNSIGNED))
_blosc = Blosc()


def _apath(group):
    """<level>/<var> -> the array path inside the zarr store (var nested under level)."""
    level, var = group.split("/")
    return f"{level}/{var}/{level}/{var}"


def zmeta(store):
    return json.loads(_s3.get_object(Bucket=BUCKET, Key=f"{store}/.zmetadata")["Body"].read())["metadata"]


def _read_1d(store, apath):
    m = zmeta(store)
    za = m[f"{apath}/.zarray"]
    n, cs, dt = za["shape"][0], za["chunks"][0], za["dtype"]
    out = np.empty(n, dtype=dt)
    for ci in range((n + cs - 1) // cs):
        raw = _s3.get_object(Bucket=BUCKET, Key=f"{store}/{apath}/{ci}")["Body"].read()
        seg = np.frombuffer(_blosc.decode(raw), dtype=dt)
        out[ci * cs: ci * cs + min(cs, n - ci * cs)] = seg[: min(cs, n - ci * cs)]
    return out


def lcc(lat, lon):
    n = math.sin(LAT0 * D2R)
    F = math.cos(LAT0 * D2R) * (math.tan(math.pi / 4 + LAT0 * D2R / 2) ** n) / n
    rho = R_EARTH * F / (math.tan(math.pi / 4 + lat * D2R / 2) ** n)
    rho0 = R_EARTH * F / (math.tan(math.pi / 4 + LAT0 * D2R / 2) ** n)
    th = n * (lon - LON0) * D2R
    return rho * math.sin(th), rho0 - rho * math.cos(th)


def bbox_window(store):
    """Return (j0, j1, i0, i1) grid-index window covering BBOX, plus the chunk it lives in."""
    group = REQUIRED["u10"]  # coord arrays live at <level>/<var>/projection_[xy]_coordinate
    xs = _read_1d(store, f"{group}/projection_x_coordinate")
    ys = _read_1d(store, f"{group}/projection_y_coordinate")
    js, is_ = [], []
    for la in (BBOX["lat_min"], BBOX["lat_max"]):
        for lo in (BBOX["lon_min"], BBOX["lon_max"]):
            x, y = lcc(la, lo)
            is_.append(int(np.argmin(np.abs(xs - x))))
            js.append(int(np.argmin(np.abs(ys - y))))
    j0, j1, i0, i1 = min(js), max(js), min(is_), max(is_)
    return (j0, j1, i0, i1), (j0 // 150, i0 // 150), (j1 // 150, i1 // 150)


def _chunk(store, group, cy, cx):
    ap = _apath(group)
    za = zmeta(store)[f"{ap}/.zarray"]
    try:
        raw = _s3.get_object(Bucket=BUCKET, Key=f"{store}/{ap}/{cy}.{cx}")["Body"].read()
    except _s3.exceptions.NoSuchKey:
        return None, za
    a = np.frombuffer(_blosc.decode(raw), dtype=za["dtype"]).reshape(za["chunks"]).copy()
    return a, za


def read_window(store, group, j0, j1, i0, i1):
    """Stitch the [j0..j1] x [i0..i1] grid window across however many 150x150 chunks it spans.
    This is the core read that backfill_hrrr.py performs per field per hour."""
    ch, cw = 150, 150
    out = np.full((j1 - j0 + 1, i1 - i0 + 1), np.nan, dtype="f4")
    for cy in range(j0 // ch, j1 // ch + 1):
        for cx in range(i0 // cw, i1 // cw + 1):
            a, _ = _chunk(store, group, cy, cx)
            if a is None:
                continue
            # overlap of this chunk's global extent with the requested window
            gj0, gi0 = cy * ch, cx * cw
            sj0, sj1 = max(j0, gj0), min(j1, gj0 + ch - 1)
            si0, si1 = max(i0, gi0), min(i1, gi0 + cw - 1)
            out[sj0 - j0: sj1 - j0 + 1, si0 - i0: si1 - i0 + 1] = \
                a[sj0 - gj0: sj1 - gj0 + 1, si0 - gi0: si1 - gi0 + 1]
    return out


def audit(date="20250701", cycle="18z"):
    store = f"sfc/{date}/{date}_{cycle}_anl.zarr"
    (j0, j1, i0, i1), c_lo, c_hi = bbox_window(store)
    nch = (c_hi[0] - c_lo[0] + 1) * (c_hi[1] - c_lo[1] + 1)
    print(f"store: {store}")
    print(f"bbox grid window: j={j0}..{j1} ({j1 - j0 + 1} rows)  i={i0}..{i1} ({i1 - i0 + 1} cols)")
    print(f"chunks spanned: {c_lo}..{c_hi}  = {nch} chunk(s)/field/hour")
    jm, im = (j0 + j1) // 2, (i0 + i1) // 2
    print("\nfield          bbox-cell   window[min/mean/max]   status")
    for name, group in {**REQUIRED, **BONUS}.items():
        win = read_window(store, group, j0, j1, i0, i1)
        fin = win[np.isfinite(win)]
        if fin.size == 0:
            print(f"  {name:10s}  MISSING"); continue
        cell = win[jm - j0, im - i0]
        print(f"  {name:10s}  {cell:9.1f}   {fin.min():7.1f}/{fin.mean():6.1f}/{fin.max():6.1f}   ok ({fin.size} cells)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="20250701")
    ap.add_argument("--cycle", default="18z", help="e.g. 18z (=14 EDT midday)")
    a = ap.parse_args()
    audit(a.date, a.cycle)
