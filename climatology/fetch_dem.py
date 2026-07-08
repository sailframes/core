#!/usr/bin/env python3
"""
fetch_dem.py — USGS 3DEP (10 m) terrain for the venue -> shaded-relief PNG +
vector coastline (GeoJSON) + per-HRRR-cell coast height. Feeds the /tactics
Ch.5 topography analysis + the relief basemap.

Source: USGS 3DEPElevation ImageServer (public, no key). We request a float32
elevation GeoTIFF for the bbox at ~30 m sampling (3DEP is 10 m native; 30 m is
plenty over a 100 km venue and keeps one request), read it with tifffile,
render a coloured hillshade (water transparent), contour the shoreline at ~0 m,
and sample elevation onto the HRRR grid.

Outputs to s3://sailframes-data-prod/climatology/:
  relief.png          coloured shaded relief (land), transparent water
  relief.json         {bounds:[[latS,lonW],[latN,lonE]], max_elev_m, ...}
  coastline.geojson   shoreline (0 m contour, long segments only)
  coast_heights.json  {gi: elevation_m} per land-ish HRRR cell + coast stats

Run: AWS_PROFILE=sailframes python3.11 climatology/fetch_dem.py
"""
import io
import json
import os

import boto3
import numpy as np
import requests
import tifffile
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LightSource, LinearSegmentedColormap

import hrrr_grid as hg

IMG = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
BUCKET = os.environ.get("CLIMO_BUCKET", "sailframes-data-prod")
PFX = "climatology"
# Relief/coastline extent is GENEROUS (whole sailing region: Boston → Cape Ann →
# Cape Cod) so the basemap overlay covers the map view at any zoom — larger than the
# HRRR analysis bbox (hg.BBOX), which stays the small racing venue.
B = dict(lat_min=41.5, lat_max=42.95, lon_min=-71.35, lon_max=-69.7)
_s3 = boto3.client("s3")


def fetch_dem(size_w=2200, size_h=2000):
    """Float32 elevation array for the bbox + its geographic transform."""
    # aspect: keep ~square pixels
    dlon = (B["lon_max"] - B["lon_min"]) * np.cos(np.radians((B["lat_min"] + B["lat_max"]) / 2))
    dlat = (B["lat_max"] - B["lat_min"])
    size_h = int(round(size_w * dlat / dlon))
    p = dict(bbox="%f,%f,%f,%f" % (B["lon_min"], B["lat_min"], B["lon_max"], B["lat_max"]),
             bboxSR=4326, imageSR=4326, size="%d,%d" % (size_w, size_h),
             format="tiff", pixelType="F32", f="image",
             interpolation="RSP_BilinearInterpolation", nodata=-9999)
    r = requests.get(IMG, params=p, timeout=180)
    r.raise_for_status()
    a = tifffile.imread(io.BytesIO(r.content)).astype("f4")   # row0 = north
    a = np.where(a < -1000, np.nan, a)                         # mask nodata (-9999)
    return a


def latlon_of(a):
    ny, nx = a.shape
    lat = np.linspace(B["lat_max"], B["lat_min"], ny)   # row0 = north
    lon = np.linspace(B["lon_min"], B["lon_max"], nx)
    return lat, lon


def make_relief(a, out="/tmp/relief.png"):
    """Coloured hillshade PNG (RGBA): land shaded green->tan->grey by elevation,
    water fully transparent so the basemap shows through."""
    elev = np.nan_to_num(a, nan=0.0)
    water = (a <= 0.3) | np.isnan(a)
    ls = LightSource(azdeg=315, altdeg=45)
    cmap = LinearSegmentedColormap.from_list("terr", ["#cfe6c2", "#e8dca8", "#c9a87a", "#9a8f88", "#f2f2f2"])
    vmax = max(30.0, float(np.nanpercentile(a[~water], 98)) if (~water).any() else 30.0)
    norm = np.clip(elev / vmax, 0, 1)
    rgb = ls.shade(norm, cmap=cmap, blend_mode="soft", vert_exag=25,
                   dx=30, dy=30, fraction=1.1)[:, :, :3]
    rgba = np.dstack([rgb, np.where(water, 0.0, 0.85)])      # alpha: water transparent
    plt.imsave(out, np.clip(rgba, 0, 1))
    return vmax


def coastline(a, out="/tmp/coastline.geojson"):
    """0.3 m contour -> GeoJSON MultiLineString, long segments only (drop ponds)."""
    lat, lon = latlon_of(a)
    LON, LAT = np.meshgrid(lon, lat)
    fig = plt.figure()
    cs = plt.contour(LON, LAT, np.nan_to_num(a, nan=-5.0), levels=[0.3])
    feats = []
    for seg in cs.allsegs[0]:
        if len(seg) >= 25:                                   # skip tiny ponds/specks
            feats.append([[round(float(x), 5), round(float(y), 5)] for x, y in seg[::2]])
    plt.close(fig)
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "MultiLineString", "coordinates": feats}}]}
    json.dump(gj, open(out, "w"))
    return sum(len(f) for f in feats), len(feats)


def cell_heights(a):
    """Sample DEM elevation onto each HRRR grid cell + a coastal 'height inland'
    proxy (max elevation within ~3 km inland of the coast)."""
    grid = json.load(io.BytesIO(_s3.get_object(Bucket=BUCKET, Key=f"{PFX}/grid.json")["Body"].read()))
    lat, lon = latlon_of(a)
    lats = np.array(grid["lats"]); lons = np.array(grid["lons"]); land = np.array(grid["land_mask"])
    ny, nx = a.shape
    heights = {}
    for k in range(len(lats)):
        ri = int(np.clip(round((B["lat_max"] - lats[k]) / (B["lat_max"] - B["lat_min"]) * (ny - 1)), 0, ny - 1))
        ci = int(np.clip(round((lons[k] - B["lon_min"]) / (B["lon_max"] - B["lon_min"]) * (nx - 1)), 0, nx - 1))
        v = a[ri, ci]
        heights[k] = None if (v != v) else round(float(v), 1)
    land_h = [heights[k] for k in range(len(lats)) if land[k] and heights[k] is not None]
    stats = {"max_land_m": round(max(land_h), 0) if land_h else None,
             "mean_land_m": round(float(np.mean(land_h)), 0) if land_h else None,
             "note": ("Low-relief venue — topography a minor term" if (land_h and np.mean(land_h) < 40)
                      else "Notable coastal relief (e.g. Cape Ann) — topography matters")}
    return heights, stats


def put(local, key, ctype):
    _s3.upload_file(local, BUCKET, f"{PFX}/{key}", ExtraArgs={"ContentType": ctype, "CacheControl": "max-age=86400"})
    print("uploaded", key)


def main():
    print("fetching 3DEP elevation for the venue…")
    a = fetch_dem()
    print("  dem", a.shape, "min %.1f max %.1f m" % (np.nanmin(a), np.nanmax(a)))
    vmax = make_relief(a)
    npts, nseg = coastline(a)
    heights, stats = cell_heights(a)
    print("  relief vmax=%.0f m; coastline %d pts / %d segments; %s" % (vmax, npts, nseg, stats["note"]))
    relief_meta = {"bounds": [[B["lat_min"], B["lon_min"]], [B["lat_max"], B["lon_max"]]],
                   "max_elev_m": round(float(np.nanmax(a)), 0), **stats,
                   "source": "USGS 3DEP 10 m (sampled ~30 m)"}
    json.dump(relief_meta, open("/tmp/relief.json", "w"))
    json.dump({"heights": heights, **stats}, open("/tmp/coast_heights.json", "w"))
    put("/tmp/relief.png", "relief.png", "image/png")
    put("/tmp/relief.json", "relief.json", "application/json")
    put("/tmp/coastline.geojson", "coastline.geojson", "application/geo+json")
    put("/tmp/coast_heights.json", "coast_heights.json", "application/json")
    print("done:", relief_meta)


if __name__ == "__main__":
    main()
