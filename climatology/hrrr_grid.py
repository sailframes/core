"""
hrrr_grid.py — shared hrrrzarr access + bbox grid window for the climatology
pipeline. Extracted from the Phase-0 `probe_hrrrzarr.py` (all logic validated
there: projection SW-corner exact, LAND coastline correct, values physical).

Reads zarr chunks directly with boto3 + numcodecs (no s3fs/xarray/zarr).
"""
import functools
import json
import math

import boto3
import numpy as np
from botocore import UNSIGNED
from botocore.config import Config
from numcodecs import Blosc

BUCKET = "hrrrzarr"
REGION = "us-west-1"
CHUNK = 150  # hrrrzarr sfc chunk edge (150x150 over the 1059x1799 CONUS grid)

# bbox — TACTICS_CLIMATOLOGY_SPEC §2 (Gloucester -> Provincetown)
BBOX = dict(lat_min=41.95, lat_max=42.75, lon_min=-71.10, lon_max=-69.85)

# HRRR Lambert Conformal (sphere). lat_1==lat_2==lat_0.
R_EARTH, LAT0, LON0 = 6371229.0, 38.5, -97.5
PROJ4 = f"+proj=lcc +lat_0={LAT0} +lon_0={LON0} +lat_1={LAT0} +lat_2={LAT0} +R={R_EARTH} +units=m +no_defs"
_D2R = math.pi / 180.0

# spec-field -> hrrrzarr <level>/<var> group
REQUIRED = {
    "u10": "10m_above_ground/UGRD", "v10": "10m_above_ground/VGRD",
    "gust": "surface/GUST", "t2": "2m_above_ground/TMP",
    "td2": "2m_above_ground/DPT", "mslp": "mean_sea_level/MSLMA",
    "tcdc": "entire_atmosphere/TCDC", "dswrf": "surface/DSWRF",
}
# time-invariant / optional extras
LAND_GROUP = "surface/LAND"
BONUS = {"hpbl": "surface/HPBL"}

_s3 = boto3.client("s3", region_name=REGION,
                   config=Config(signature_version=UNSIGNED, max_pool_connections=64,
                                 retries={"max_attempts": 3, "mode": "adaptive"}))
_blosc = Blosc()


def s3():
    return _s3


def store_anl(date, cycle):
    """date 'YYYYMMDD', cycle like '18z' -> analysis (F00) zarr store prefix."""
    return f"sfc/{date}/{date}_{cycle}_anl.zarr"


def list_anl_cycles(date):
    """Sorted list of available F00 analysis cycles ('00z'...'23z') for a date."""
    r = _s3.list_objects_v2(Bucket=BUCKET, Prefix=f"sfc/{date}/", Delimiter="/")
    cyc = [p["Prefix"].split("/")[-2] for p in r.get("CommonPrefixes", [])]
    return sorted({c.split("_")[1] for c in cyc if c.endswith("_anl.zarr")})


def apath(group):
    """<level>/<var> -> array path inside the store (var nested under level twice)."""
    level, var = group.split("/")
    return f"{level}/{var}/{level}/{var}"


@functools.lru_cache(maxsize=512)
def zmeta(store):
    """Consolidated zarr metadata for a store. Cached per store — otherwise every
    chunk read re-fetched AND re-parsed this (large) JSON, serializing on the GIL
    and doubling S3 GETs. lru_cache is thread-safe in CPython."""
    return json.loads(_s3.get_object(Bucket=BUCKET, Key=f"{store}/.zmetadata")["Body"].read())["metadata"]


def read_1d(store, path):
    za = zmeta(store)[f"{path}/.zarray"]
    n, cs, dt = za["shape"][0], za["chunks"][0], za["dtype"]
    out = np.empty(n, dtype=dt)
    for ci in range((n + cs - 1) // cs):
        raw = _s3.get_object(Bucket=BUCKET, Key=f"{store}/{path}/{ci}")["Body"].read()
        seg = np.frombuffer(_blosc.decode(raw), dtype=dt)
        out[ci * cs: ci * cs + min(cs, n - ci * cs)] = seg[: min(cs, n - ci * cs)]
    return out


def _lcc_fwd(lat, lon):
    n = math.sin(LAT0 * _D2R)
    F = math.cos(LAT0 * _D2R) * (math.tan(math.pi / 4 + LAT0 * _D2R / 2) ** n) / n
    rho = R_EARTH * F / (math.tan(math.pi / 4 + lat * _D2R / 2) ** n)
    rho0 = R_EARTH * F / (math.tan(math.pi / 4 + LAT0 * _D2R / 2) ** n)
    th = n * (lon - LON0) * _D2R
    return rho * math.sin(th), rho0 - rho * math.cos(th)


def bbox_window(store):
    """(j0, j1, i0, i1) inclusive grid-index window covering BBOX. Constant across
    dates (static grid), so callers may cache it."""
    g = REQUIRED["u10"]
    xs = read_1d(store, f"{g}/projection_x_coordinate")
    ys = read_1d(store, f"{g}/projection_y_coordinate")
    js, is_ = [], []
    for la in (BBOX["lat_min"], BBOX["lat_max"]):
        for lo in (BBOX["lon_min"], BBOX["lon_max"]):
            x, y = _lcc_fwd(la, lo)
            is_.append(int(np.argmin(np.abs(xs - x))))
            js.append(int(np.argmin(np.abs(ys - y))))
    return min(js), max(js), min(is_), max(is_)


def _chunk(store, group, cy, cx):
    ap = apath(group)
    za = zmeta(store)[f"{ap}/.zarray"]
    try:
        raw = _s3.get_object(Bucket=BUCKET, Key=f"{store}/{ap}/{cy}.{cx}")["Body"].read()
    except _s3.exceptions.NoSuchKey:
        return None
    return np.frombuffer(_blosc.decode(raw), dtype=za["dtype"]).reshape(za["chunks"]).copy()


def read_window(store, group, j0, j1, i0, i1):
    """Stitch the [j0..j1] x [i0..i1] grid window across the chunks it spans (float32)."""
    out = np.full((j1 - j0 + 1, i1 - i0 + 1), np.nan, dtype="f4")
    for cy in range(j0 // CHUNK, j1 // CHUNK + 1):
        for cx in range(i0 // CHUNK, i1 // CHUNK + 1):
            a = _chunk(store, group, cy, cx)
            if a is None:
                continue
            gj0, gi0 = cy * CHUNK, cx * CHUNK
            sj0, sj1 = max(j0, gj0), min(j1, gj0 + CHUNK - 1)
            si0, si1 = max(i0, gi0), min(i1, gi0 + CHUNK - 1)
            out[sj0 - j0: sj1 - j0 + 1, si0 - i0: si1 - i0 + 1] = \
                a[sj0 - gj0: sj1 - gj0 + 1, si0 - gi0: si1 - gi0 + 1]
    return out


def store_fcst(date, cycle):
    """Forecast (F01..F48) zarr store for a cycle."""
    return f"sfc/{date}/{date}_{cycle}_fcst.zarr"


def list_fcst_cycles(date):
    r = _s3.list_objects_v2(Bucket=BUCKET, Prefix=f"sfc/{date}/", Delimiter="/")
    cyc = [p["Prefix"].split("/")[-2] for p in r.get("CommonPrefixes", [])]
    return sorted({c.split("_")[1] for c in cyc if c.endswith("_fcst.zarr")})


def read_fcst_window(store, group, j0, j1, i0, i1, nlead=18):
    """[nlead, ny_win, nx_win] forecast cube over the bbox window. The _fcst array
    is [48(F01..F48), ny, nx] chunked [48,150,150] — time not chunked, so one
    spatial-chunk read yields all lead times."""
    ap = apath(group)
    za = zmeta(store)[f"{ap}/.zarray"]
    nt = za["shape"][0]
    nlead = min(nlead, nt)
    out = np.full((nlead, j1 - j0 + 1, i1 - i0 + 1), np.nan, dtype="f4")
    for cy in range(j0 // CHUNK, j1 // CHUNK + 1):
        for cx in range(i0 // CHUNK, i1 // CHUNK + 1):
            try:
                raw = _s3.get_object(Bucket=BUCKET, Key=f"{store}/{ap}/0.{cy}.{cx}")["Body"].read()
            except _s3.exceptions.NoSuchKey:
                continue
            a = np.frombuffer(_blosc.decode(raw), dtype=za["dtype"]).reshape(za["chunks"])  # [48,150,150]
            gj0, gi0 = cy * CHUNK, cx * CHUNK
            sj0, sj1 = max(j0, gj0), min(j1, gj0 + CHUNK - 1)
            si0, si1 = max(i0, gi0), min(i1, gi0 + CHUNK - 1)
            out[:, sj0 - j0: sj1 - j0 + 1, si0 - i0: si1 - i0 + 1] = \
                a[:nlead, sj0 - gj0: sj1 - gj0 + 1, si0 - gi0: si1 - gi0 + 1]
    return out


def latlon_grid(store, j0, j1, i0, i1):
    """2D (lat, lon) arrays for the window, via pyproj inverse of the store's own
    projection x/y coordinate arrays. Shapes (ny_win, nx_win)."""
    from pyproj import Transformer
    g = REQUIRED["u10"]
    xs = read_1d(store, f"{g}/projection_x_coordinate")[i0:i1 + 1]
    ys = read_1d(store, f"{g}/projection_y_coordinate")[j0:j1 + 1]
    X, Y = np.meshgrid(xs, ys)
    tr = Transformer.from_crs(PROJ4, "EPSG:4326", always_xy=True)
    lon, lat = tr.transform(X, Y)
    return lat.astype("f4"), lon.astype("f4")
