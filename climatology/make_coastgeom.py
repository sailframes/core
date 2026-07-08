#!/usr/bin/env python3
"""
make_coastgeom.py — add per-cell coast geometry to grid.json:
  coast_dist_nm[]  distance from coast (nearest land) in nautical miles, per cell
  seaward_deg[]    the bearing a sea breeze blows FROM at that cell (direction of
                   open water = away from the nearest land), per cell
  venue_seaward    scalar mean seaward over the racing region (fallback)

Powers the /tactics sea-breeze ZONE + FRONT + distance-from-coast overlays.
Downloads the current grid.json from S3, augments it, re-uploads (backward
compatible — consumers ignore unknown keys).

Run: AWS_PROFILE=sailframes python3.11 climatology/make_coastgeom.py
"""
import io
import json
import math
import os

import boto3
import numpy as np
from scipy import ndimage

BUCKET = os.environ.get("CLIMO_BUCKET", "sailframes-data-prod")
PFX = "climatology"
_s3 = boto3.client("s3")


def main():
    grid = json.load(io.BytesIO(_s3.get_object(Bucket=BUCKET, Key=f"{PFX}/grid.json")["Body"].read()))
    nx, ny = grid["nx"], grid["ny"]
    lats = np.array(grid["lats"]).reshape(ny, nx)
    lons = np.array(grid["lons"]).reshape(ny, nx)
    land = np.array(grid["land_mask"]).reshape(ny, nx).astype(float)
    water = land < 0.5

    # cell spacing (km) from adjacent-cell haversine — the HRRR grid is ~3 km
    def hav(la1, lo1, la2, lo2):
        r = 6371.0
        dla = math.radians(la2 - la1); dlo = math.radians(lo2 - lo1)
        a = math.sin(dla / 2) ** 2 + math.cos(math.radians(la1)) * math.cos(math.radians(la2)) * math.sin(dlo / 2) ** 2
        return 2 * r * math.asin(math.sqrt(a))
    cell_km = hav(lats[ny // 2, nx // 2], lons[ny // 2, nx // 2], lats[ny // 2, nx // 2 + 1], lons[ny // 2, nx // 2 + 1])

    # distance from coast (water cells): EDT to nearest land, in cells -> km -> NM
    dist_cells = ndimage.distance_transform_edt(water)   # 0 on land, grows into water
    coast_dist_nm = (dist_cells * cell_km / 1.852)
    coast_dist_nm = np.where(water, coast_dist_nm, 0.0)

    # per-cell seaward bearing = direction away from the nearest land.
    # Use the gradient of a smoothed land fraction (points toward land); seaward = -grad.
    S = ndimage.gaussian_filter(land, sigma=1.2)
    gy, gx = np.gradient(S)                               # index-space gradient
    dlat_drow = np.gradient(lats, axis=0); dlat_dcol = np.gradient(lats, axis=1)
    dlon_drow = np.gradient(lons, axis=0); dlon_dcol = np.gradient(lons, axis=1)
    det = dlat_drow * dlon_dcol - dlat_dcol * dlon_drow
    det = np.where(np.abs(det) < 1e-12, 1e-12, det)
    dL_dlat = (dlon_dcol * gy - dlon_drow * gx) / det
    dL_dlon = (-dlat_dcol * gy + dlat_drow * gx) / det
    e = -dL_dlon * np.cos(np.radians(lats))              # seaward east comp
    n = -dL_dlat                                          # seaward north comp
    mag = np.hypot(e, n)
    seaward = (np.degrees(np.arctan2(e, n))) % 360.0
    # weak-gradient (deep offshore) cells -> fill with the racing-region mean seaward
    region = (lats >= 42.2) & (lats <= 42.6) & (lons >= -71.0) & (lons <= -70.4) & (mag > 1e-9)
    reg = seaward[region]
    ve = np.mean(np.sin(np.radians(reg))); vn = np.mean(np.cos(np.radians(reg)))
    venue_seaward = math.degrees(math.atan2(ve, vn)) % 360.0
    seaward = np.where(mag > 1e-4, seaward, venue_seaward)

    grid["coast_dist_nm"] = [round(float(v), 2) for v in coast_dist_nm.ravel()]
    grid["seaward_deg"] = [round(float(v), 0) for v in seaward.ravel()]
    grid["venue_seaward"] = round(venue_seaward, 0)
    grid["cell_km"] = round(cell_km, 2)

    body = json.dumps(grid).encode()
    _s3.put_object(Bucket=BUCKET, Key=f"{PFX}/grid.json", Body=body,
                   ContentType="application/json", CacheControl="max-age=300")
    print("grid.json v2: cell=%.2f km, venue_seaward=%.0f°, coast_dist max %.1f NM"
          % (cell_km, venue_seaward, coast_dist_nm.max()))


if __name__ == "__main__":
    main()
