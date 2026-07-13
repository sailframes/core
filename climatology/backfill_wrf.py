#!/usr/bin/env python3
"""
backfill_wrf.py — extract the SailFrames 1 km WRF (gom-seabreeze) wrfout_d03
frames into the SAME web schema the /tactics + race weather overlay already
reads (grid.json + daily fields parquet), so sf-weather-overlay.js can replay
the run driven by a clock with no new rendering code.

Outputs (mirrors backfill_hrrr.py so sf-weather-overlay.js is source-agnostic):
  <out>/grid.json                              static domain (nx,ny,lats,lons,
                                               land_mask,cell_km,seaward_deg,
                                               coast_dist_nm) — one WRF d03 grid
  <out>/fields/year=YYYY/month=MM/DD.parquet   one row per (valid_time,gi):
                                               valid_time_utc, gi (uint16 row-major
                                               j*nx+i), u10 v10 (float32, EARTH-
                                               relative m/s), t2 (float32 K)

WRF wrfout U10/V10 are GRID-relative (Lambert x/y); we rotate to earth-relative
(true N) with SINALPHA/COSALPHA — the overlay expects true-N winds:
  u_e = U10*COSALPHA - V10*SINALPHA ;  v_e = V10*COSALPHA + U10*SINALPHA

Run:
  # from a local dir of wrfout_d03_* frames
  python3 climatology/backfill_wrf.py --date 2026-07-04 --src /tmp/wrfout --out-dir climatology/_local/wrf
  # or pull the frames straight from S3 first
  python3 climatology/backfill_wrf.py --date 2026-07-04 \
      --s3 s3://sailframes-data-prod/gom/2026-07-04/forecast --out-dir climatology/_local/wrf
  # then publish (grid.json is static; fields are per-date):
  aws s3 cp climatology/_local/wrf/ s3://sailframes-data-prod/climatology/wrf/ --recursive
Units are SI as WRF emits them (m/s, K); display conversion is client-side.
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import tempfile

import numpy as np
from scipy.ndimage import distance_transform_edt, gaussian_filter
import xarray as xr
import pyarrow as pa
import pyarrow.parquet as pq

DOMAIN = "d03"                      # the 1 km race nest
KM_PER_NM = 1.0 / 0.539957
FRAME_RE = re.compile(r"wrfout_" + DOMAIN + r"_(\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2})")
SCHEMA = pa.schema([
    ("valid_time_utc", pa.timestamp("s", tz="UTC")),
    ("gi", pa.uint16()),
    ("u10", pa.float32()), ("v10", pa.float32()), ("t2", pa.float32()),
])


def _stage_from_s3(s3_prefix, date, cache):
    """Download wrfout_<DOMAIN>_* for `date` from an s3:// prefix into `cache`. boto3."""
    import boto3
    m = re.match(r"s3://([^/]+)/(.*)", s3_prefix.rstrip("/"))
    if not m:
        raise SystemExit(f"bad --s3 prefix: {s3_prefix}")
    bucket, key = m.group(1), m.group(2)
    s3 = boto3.client("s3")
    os.makedirs(cache, exist_ok=True)
    got = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{key}/wrfout_{DOMAIN}_"):
        for obj in page.get("Contents", []):
            name = os.path.basename(obj["Key"])
            dest = os.path.join(cache, name)
            if not os.path.exists(dest) or os.path.getsize(dest) != obj["Size"]:
                s3.download_file(bucket, obj["Key"], dest)
            got.append(dest)
    print(f"  staged {len(got)} {DOMAIN} frames from {s3_prefix} -> {cache}")
    return cache


def _frames_for_date(src, date):
    """Sorted list of (valid_time_utc, path) for wrfout_<DOMAIN>_* files matching `date`."""
    out = []
    for p in glob.glob(os.path.join(src, f"wrfout_{DOMAIN}_*")):
        m = FRAME_RE.search(os.path.basename(p))
        if not m:
            continue
        t = dt.datetime.strptime(m.group(1), "%Y-%m-%d_%H:%M:%S").replace(tzinfo=dt.timezone.utc)
        if t.strftime("%Y-%m-%d") == date:                 # keep only this UTC day
            out.append((t, p))
    return sorted(out)


def build_grid(sample_path):
    """Static grid.json dict from one wrfout frame (mass grid, row-major j*nx+i)."""
    ds = xr.open_dataset(sample_path)
    lat = np.asarray(ds["XLAT"].isel(Time=0).values, float)      # (ny, nx)
    lon = np.asarray(ds["XLONG"].isel(Time=0).values, float)
    land = np.asarray(ds["LANDMASK"].isel(Time=0).values).astype(np.uint8)
    ny, nx = lat.shape
    cell_km = float(ds.attrs.get("DX", 1000.0)) / 1000.0

    water = (land == 0)
    # distance from each cell to the nearest LAND cell -> offshore "dist-to-coast" (NM)
    coast_dist_nm = (distance_transform_edt(water) * cell_km * KM_PER_NM).astype(np.float32)
    # seaward bearing: compass direction toward increasing (smoothed) water fraction.
    # row axis = south_north (north+), col axis = west_east (east+); bearing = atan2(E,N).
    wf = gaussian_filter(water.astype(float), 3.0)
    g_north, g_east = np.gradient(wf)
    seaward_deg = (np.degrees(np.arctan2(g_east, g_north)) % 360).astype(np.float32)

    ds.close()
    return {
        "source": "SailFrames WRF-ARW 1km (gom-seabreeze d03)",
        "nx": nx, "ny": ny, "cell_km": cell_km,
        "lats": [round(float(v), 5) for v in lat.ravel()],
        "lons": [round(float(v), 5) for v in lon.ravel()],
        "land_mask": [int(v) for v in land.ravel()],
        "seaward_deg": [round(float(v), 1) for v in seaward_deg.ravel()],
        "coast_dist_nm": [round(float(v), 2) for v in coast_dist_nm.ravel()],
    }


def extract_frame(path):
    """(valid_time_utc, u10_earth, v10_earth, t2) flattened row-major, all float32."""
    ds = xr.open_dataset(path)
    t = dt.datetime.strptime(bytes(ds["Times"].values[0]).decode(), "%Y-%m-%d_%H:%M:%S")
    ug = np.asarray(ds["U10"].isel(Time=0).values, float)
    vg = np.asarray(ds["V10"].isel(Time=0).values, float)
    sa = np.asarray(ds["SINALPHA"].isel(Time=0).values, float)
    ca = np.asarray(ds["COSALPHA"].isel(Time=0).values, float)
    ue = (ug * ca - vg * sa)                     # grid-relative -> earth-relative (true N)
    ve = (vg * ca + ug * sa)
    t2 = np.asarray(ds["T2"].isel(Time=0).values, float)
    ds.close()
    return t, ue.ravel().astype("f4"), ve.ravel().astype("f4"), t2.ravel().astype("f4")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="UTC day YYYY-MM-DD")
    ap.add_argument("--src", help="local dir of wrfout_d03_* frames")
    ap.add_argument("--s3", help="s3://bucket/prefix holding wrfout_d03_* (downloaded first)")
    ap.add_argument("--out-dir", default="climatology/_local/wrf")
    ap.add_argument("--no-grid", action="store_true", help="skip rewriting grid.json (static)")
    a = ap.parse_args()

    src = a.src
    if a.s3:
        src = _stage_from_s3(a.s3, a.date, a.src or tempfile.mkdtemp(prefix="wrfout_"))
    if not src:
        raise SystemExit("provide --src (local dir) or --s3 (prefix)")

    frames = _frames_for_date(src, a.date)
    if not frames:
        raise SystemExit(f"no wrfout_{DOMAIN}_* frames for {a.date} in {src}")

    if not a.no_grid:
        grid = build_grid(frames[0][1])
        os.makedirs(a.out_dir, exist_ok=True)
        with open(os.path.join(a.out_dir, "grid.json"), "w") as f:
            json.dump(grid, f, separators=(",", ":"))
        print(f"  grid.json: {grid['nx']}x{grid['ny']} @ {grid['cell_km']}km "
              f"({grid['nx']*grid['ny']} cells)")

    ncell = None
    times, gicol = [], []
    cols = {"u10": [], "v10": [], "t2": []}
    for t, ue, ve, t2 in (extract_frame(p) for _, p in frames):
        if ncell is None:
            ncell = ue.size
            gi = np.arange(ncell, dtype="uint16")
        times.append(np.full(ncell, np.datetime64(t, "s")))
        gicol.append(gi)
        cols["u10"].append(ue); cols["v10"].append(ve); cols["t2"].append(t2)

    tbl = pa.table({
        "valid_time_utc": pa.array(np.concatenate(times), type=pa.timestamp("s", tz="UTC")),
        "gi": pa.array(np.concatenate(gicol)),
        "u10": pa.array(np.concatenate(cols["u10"]), type=pa.float32()),
        "v10": pa.array(np.concatenate(cols["v10"]), type=pa.float32()),
        "t2": pa.array(np.concatenate(cols["t2"]), type=pa.float32()),
    }, schema=SCHEMA).replace_schema_metadata({b"wind_frame": b"earth", b"model": b"wrf-1km"})

    y, m, d = a.date.split("-")
    out = os.path.join(a.out_dir, f"fields/year={y}", f"month={m}", f"{d}.parquet")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    pq.write_table(tbl, out, compression="zstd", row_group_size=8192)
    kb = os.path.getsize(out) / 1024
    print(f"{a.date}: {tbl.num_rows} rows ({len(frames)} frames x {ncell} cells), "
          f"{kb:.0f} KB -> {out}")


if __name__ == "__main__":
    main()
