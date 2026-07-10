#!/usr/bin/env python3
"""
patch_met_em_sst.py
-------------------
Overwrite the SST field in WRF `met_em` files with a daily coldest-pixel SST
composite (regridded to each WRF domain), then run sanity checks so you can
confirm the cold near-shore structure is actually in the model before launching.

Pipeline position:
    build_coldest_sst.py  ->  sst_YYYY-MM-DD.nc  ->  [THIS]  ->  real.exe

Why netCDF4 (not xarray) for the write: met_em has a strict structure WRF's
real.exe expects. We open in 'r+' and overwrite only SST[:] in place, leaving
every other variable / attribute / fill-value untouched.

Requires: numpy, scipy, netCDF4, xarray  (matplotlib only if --plot)

Example:
    python patch_met_em_sst.py --met-dir ./WPS --composite ./sst_out \
        --domains 1 2 3 --plot
"""
import argparse
import glob
import os
import re
import shutil
import sys
from datetime import datetime

import numpy as np
from scipy.interpolate import RegularGridInterpolator
from scipy.spatial import cKDTree
import netCDF4 as nc
import xarray as xr

MET_RE = re.compile(r"met_em\.d(\d\d)\.(\d{4}-\d{2}-\d{2})_")

# Physical plausibility window for GoM SST (K). Outside this => flagged.
SST_MIN_K, SST_MAX_K = 265.0, 305.0


def to_kelvin(arr, units):
    u = (units or "").strip().lower()
    if u in ("k", "kelvin", "degk", "degrees_kelvin", "degree_kelvin"):
        return arr
    if u in ("c", "degc", "celsius", "degrees_c", "degree_c", "degrees celsius",
             "deg_c", "degree celsius"):
        return arr + 273.15
    # Unknown units: infer from magnitude.
    return arr + 273.15 if np.nanmedian(arr) < 100.0 else arr


def _find(ds, names):
    for n in names:
        if n in ds.variables or n in getattr(ds, "coords", {}):
            return n
    return None


def load_composite(path):
    """Return (lat_1d_asc, lon_1d_asc, sst2d_K) from a composite NetCDF."""
    ds = xr.open_dataset(path)
    sname = _find(ds, ["sst", "SST", "analysed_sst", "sea_surface_temperature"])
    latn = _find(ds, ["lat", "latitude", "Latitude"])
    lonn = _find(ds, ["lon", "longitude", "Longitude"])
    if sname is None or latn is None or lonn is None:
        raise ValueError(f"{path}: could not find sst/lat/lon variables "
                         f"(have {list(ds.variables)})")
    da = ds[sname].squeeze()
    units = da.attrs.get("units", "")
    arr = to_kelvin(np.asarray(da.values, dtype=float), units)
    lat = np.asarray(ds[latn].values, dtype=float)
    lon = np.asarray(ds[lonn].values, dtype=float)
    # enforce ascending
    if lat[0] > lat[-1]:
        lat = lat[::-1]
        arr = arr[::-1, :]
    if lon[0] > lon[-1]:
        lon = lon[::-1]
        arr = arr[:, ::-1]
    ds.close()
    return lat, lon, arr


def composite_for_date(comp_path, date_str):
    """comp_path may be a single .nc file or a directory of sst_<date>.nc."""
    if os.path.isdir(comp_path):
        cand = os.path.join(comp_path, f"sst_{date_str}.nc")
        if not os.path.exists(cand):
            # fall back to any file whose name contains the date
            hits = glob.glob(os.path.join(comp_path, f"*{date_str}*.nc"))
            if not hits:
                return None
            cand = hits[0]
        return cand
    return comp_path  # single file applied to all times


def regrid_and_fill(clat, clon, carr, xlat, xlong, landmask, orig_sst):
    """Bilinear interp of composite to WRF (curvilinear) grid, water only,
    with nearest-neighbour + original-SST fallback so no water cell is NaN."""
    rgi = RegularGridInterpolator((clat, clon), carr, method="linear",
                                  bounds_error=False, fill_value=np.nan)
    pts = np.column_stack([xlat.ravel(), xlong.ravel()])
    vals = rgi(pts).reshape(xlat.shape)

    water = (landmask == 0)
    need = np.isnan(vals) & water
    if need.any():
        LON, LAT = np.meshgrid(clon, clat)
        good = ~np.isnan(carr)
        tree = cKDTree(np.column_stack([LAT[good], LON[good]]))
        srcv = carr[good]
        _, idx = tree.query(np.column_stack([xlat[need], xlong[need]]))
        vals[need] = srcv[idx]

    # any residual NaN over water -> keep the driver's original SST there
    residual = np.isnan(vals) & water
    n_resid = int(residual.sum())
    vals[residual] = orig_sst[residual]

    new_sst = orig_sst.copy()
    new_sst[water] = vals[water]
    return new_sst, water, n_resid


def sanity(new_sst, water, domain, n_resid):
    w = new_sst[water]
    lo, hi, mean = np.nanmin(w), np.nanmax(w), np.nanmean(w)
    n_nan = int(np.isnan(w).sum())
    flag = "" if (lo >= SST_MIN_K and hi <= SST_MAX_K and n_nan == 0) else "  <-- CHECK"
    print(f"  d{domain}: water cells={w.size:>7d}  "
          f"SST[K] min={lo:6.2f} max={hi:6.2f} mean={mean:6.2f}  "
          f"NaN={n_nan}  filled_from_driver={n_resid}{flag}")
    if n_nan:
        print(f"  d{domain}: ERROR {n_nan} NaN over water AFTER fill -- WRF will crash. "
              f"Aborting write for this file.")
        return False
    if lo < SST_MIN_K or hi > SST_MAX_K:
        print(f"  d{domain}: WARNING SST outside [{SST_MIN_K},{SST_MAX_K}] K "
              f"(unit bug? bad composite?). Written anyway -- inspect the plot.")
    return True


def quicklook(path_png, xlat, xlong, new_sst, landmask):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    masked = np.ma.masked_where(landmask == 1, new_sst - 273.15)
    fig, ax = plt.subplots(figsize=(7, 6))
    pc = ax.pcolormesh(xlong, xlat, masked, shading="auto", cmap="turbo")
    ax.set_title(os.path.basename(path_png).replace(".png", ""))
    ax.set_xlabel("lon"); ax.set_ylabel("lat")
    cb = plt.colorbar(pc, ax=ax); cb.set_label("SST (degC)")
    fig.tight_layout(); fig.savefig(path_png, dpi=140); plt.close(fig)
    print(f"  wrote {path_png}")


def process_file(fpath, domain, date_str, comp_path, do_plot, backup, sst_var):
    comp_file = composite_for_date(comp_path, date_str)
    if comp_file is None:
        print(f"  d{domain} {date_str}: no composite found -- skipped")
        return
    clat, clon, carr = load_composite(comp_file)

    if backup:
        bak = fpath + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(fpath, bak)

    with nc.Dataset(fpath, "r+") as ncf:
        xlat = np.asarray(ncf.variables["XLAT_M"][0], dtype=float)
        xlong = np.asarray(ncf.variables["XLONG_M"][0], dtype=float)
        landmask = np.asarray(ncf.variables["LANDMASK"][0], dtype=float)
        sstv = ncf.variables[sst_var]
        orig_sst = np.asarray(sstv[0], dtype=float)

        new_sst, water, n_resid = regrid_and_fill(
            clat, clon, carr, xlat, xlong, landmask, orig_sst)

        if not sanity(new_sst, water, domain, n_resid):
            return  # leave file untouched

        sstv[0, :, :] = new_sst

    if do_plot:
        quicklook(fpath.replace(".nc", "") + f"_SST.png",
                  xlat, xlong, new_sst, landmask)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--met-dir", required=True, help="dir containing met_em.d0N.*.nc")
    ap.add_argument("--composite", required=True,
                    help="composite .nc file OR dir of sst_YYYY-MM-DD.nc")
    ap.add_argument("--domains", nargs="+", type=int, default=[1, 2, 3])
    ap.add_argument("--sst-var", default="SST", help="SST var name in met_em (default SST)")
    ap.add_argument("--plot", action="store_true", help="save a PNG per file")
    ap.add_argument("--no-backup", action="store_true", help="do NOT write .bak backups")
    args = ap.parse_args()

    total = 0
    for d in args.domains:
        files = sorted(glob.glob(os.path.join(args.met_dir, f"met_em.d{d:02d}.*.nc")))
        if not files:
            print(f"d{d}: no met_em files in {args.met_dir}")
            continue
        print(f"d{d}: {len(files)} met_em file(s)")
        for f in files:
            m = MET_RE.search(os.path.basename(f))
            if not m:
                print(f"  skip (unparsable name): {f}")
                continue
            date_str = m.group(2)
            process_file(f, d, date_str, args.composite,
                         args.plot, not args.no_backup, args.sst_var)
            total += 1
    print(f"done: processed {total} file(s).")
    if total == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
