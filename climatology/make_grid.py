#!/usr/bin/env python3
"""
make_grid.py — one-time. Emit climatology/grid.json (spec §4):
  {nx, ny, lats[], lons[], land_mask[], window, bbox, source}

- nx/ny: window dimensions (39 x 42 for the Gloucester->P'town bbox).
- lats/lons: per-cell centers, row-major (ny*nx), from HRRR LCC via pyproj.
- land_mask: HRRR surface/LAND (0=water,1=land), row-major, uint8. Time-
  invariant, so read once here rather than per-day in backfill.
- window: the constant grid-index window (j0,j1,i0,i1) backfill_hrrr.py reuses.

Row-major means index k = row*nx + col, row 0 = SOUTH (j0), col 0 = WEST (i0)
— same orientation as the fields Parquet grid index `gi`.

Run:  python3 climatology/make_grid.py [--ref-date YYYYMMDD --ref-cycle 18z] [--out PATH]
"""
import argparse
import json

import numpy as np

import hrrr_grid as hg


def build(ref_date, ref_cycle):
    store = hg.store_anl(ref_date, ref_cycle)
    j0, j1, i0, i1 = hg.bbox_window(store)
    ny, nx = j1 - j0 + 1, i1 - i0 + 1
    lat, lon = hg.latlon_grid(store, j0, j1, i0, i1)      # (ny, nx), row 0 = south
    land = hg.read_window(store, hg.LAND_GROUP, j0, j1, i0, i1)
    land_mask = (np.nan_to_num(land) > 0.5).astype("uint8")
    return {
        "nx": nx, "ny": ny,
        "window": {"j0": j0, "j1": j1, "i0": i0, "i1": i1},
        "bbox": hg.BBOX,
        "proj4": hg.PROJ4,
        "order": "row-major, row0=south(j0), col0=west(i0); k = row*nx + col",
        "source": f"hrrrzarr {store}",
        "lats": [round(v, 5) for v in lat.ravel().tolist()],
        "lons": [round(v, 5) for v in lon.ravel().tolist()],
        "land_mask": land_mask.ravel().tolist(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref-date", default="20250701")
    ap.add_argument("--ref-cycle", default="18z")
    ap.add_argument("--out", default="climatology/grid.json")
    a = ap.parse_args()
    grid = build(a.ref_date, a.ref_cycle)
    with open(a.out, "w") as f:
        json.dump(grid, f, separators=(",", ":"))
    n = grid["nx"] * grid["ny"]
    land_pct = 100 * sum(grid["land_mask"]) / n
    print(f"wrote {a.out}: {grid['nx']}x{grid['ny']} = {n} cells, "
          f"land {land_pct:.0f}%, window {grid['window']}")
    print(f"  lat {min(grid['lats']):.3f}..{max(grid['lats']):.3f}  "
          f"lon {min(grid['lons']):.3f}..{max(grid['lons']):.3f}")


if __name__ == "__main__":
    main()
