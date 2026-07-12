#!/usr/bin/env python3
"""
plot_geo_landmask.py  --  eyeball the static geography from geogrid
=================================================================
Quicklook of the WPS static fields that drive the sea breeze -- the land-WATER
mask and land-use index (and topography) -- straight out of geo_em.d0N.nc. Run
this AFTER a geogrid-only pass (run_case.sh GOM_GEOGRID_ONLY=1) to confirm the
coastline before spending the ~$3 / 4 h full WPS->real->wrf chain.

The land-water mask sets WHERE the coast sits at grid scale; at d03 (1 km) a
default 30s (~900 m) mask can misplace Salem Sound / the harbor islands /
Marblehead Neck by a whole cell. NLCD 9s (~250 m) sharpens it -- use --compare
to put the two side by side over the race area.

    # single field
    python validation/plot_geo_landmask.py --geo-dir ./wpsprd --domain 3 --field LANDMASK -o geo_d03.png

    # A/B the coastline: MODIS-30s vs NLCD-9s geogrid outputs
    python validation/plot_geo_landmask.py --geo-dir ./geo_nlcd --compare ./geo_modis \
        --domain 3 --field LANDMASK -o coast_nlcd_vs_modis.png

geo_em carries its own lon/lat (XLONG_M / XLAT_M) and MMINLU, so no external
coastline is needed -- the mask *is* the model's coastline. A reference coast
(GSHHG via cartopy) is overlaid if cartopy is importable, purely for the eye.
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import xarray as xr
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm

# race-area default extent (lon_w, lon_e, lat_s, lat_n): Cape Ann -> Boston,
# the Salem Sound / Mass Bay geometry d03 exists to resolve.
RACE_EXTENT = (-71.10, -70.35, 42.25, 42.80)


def _open(geo_dir, dom):
    p = Path(geo_dir) / f"geo_em.d{dom:02d}.nc"
    if not p.exists():
        sys.exit(f"not found: {p} (run geogrid first)")
    return xr.open_dataset(p)


def _field(ds, name):
    """Return (lon2d, lat2d, values2d, mminlu) for a geo_em field, first time slice."""
    lon = ds["XLONG_M"].isel(Time=0).values
    lat = ds["XLAT_M"].isel(Time=0).values
    if name not in ds:
        sys.exit(f"field {name} not in geo_em (have: {', '.join(list(ds.data_vars)[:20])} ...)")
    v = ds[name].isel(Time=0).values
    mminlu = ds.attrs.get("MMINLU", "?")
    return lon, lat, v, mminlu


def _draw(ax, lon, lat, v, field, extent):
    if field == "LANDMASK":
        cmap = plt.get_cmap("Blues_r")
        norm = BoundaryNorm([-0.5, 0.5, 1.5], cmap.N)
        m = ax.pcolormesh(lon, lat, v, cmap=cmap, norm=norm, shading="auto")
    elif field in ("HGT_M", "HGT"):
        m = ax.pcolormesh(lon, lat, np.where(v > 0, v, np.nan),
                          cmap="terrain", shading="auto")
    else:  # LU_INDEX or any categorical
        m = ax.pcolormesh(lon, lat, v, cmap="tab20", shading="auto")
    # water/land contour from the mask, if present alongside
    ax.set_xlim(extent[0], extent[1]); ax.set_ylim(extent[2], extent[3])
    ax.set_aspect(1.0 / np.cos(np.deg2rad(np.mean(extent[2:]))))
    ax.set_xlabel("lon"); ax.set_ylabel("lat")
    return m


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--geo-dir", required=True, help="dir holding geo_em.d0N.nc")
    ap.add_argument("--compare", help="second geo_em dir to A/B against (e.g. the MODIS run)")
    ap.add_argument("--domain", type=int, default=3)
    ap.add_argument("--field", default="LANDMASK",
                    help="LANDMASK (default) | LU_INDEX | HGT_M")
    ap.add_argument("--extent", type=float, nargs=4, metavar=("W", "E", "S", "N"),
                    default=RACE_EXTENT, help="lon_w lon_e lat_s lat_n (default = race area)")
    ap.add_argument("--full-extent", action="store_true",
                    help="use the domain's own bounds instead of --extent (check d01/d02 ocean is water)")
    ap.add_argument("-o", "--out", default="geo_landmask.png")
    args = ap.parse_args()

    extent = args.extent
    if args.full_extent:
        ds0 = _open(args.geo_dir, args.domain)
        lo, la, _, _ = _field(ds0, "LANDMASK")
        extent = (float(lo.min()), float(lo.max()), float(la.min()), float(la.max()))
        ds0.close()

    dirs = [("this", args.geo_dir)]
    if args.compare:
        dirs.append(("compare", args.compare))

    fig, axes = plt.subplots(1, len(dirs), figsize=(7.5 * len(dirs), 7.0), squeeze=False)
    for ax, (tag, d) in zip(axes[0], dirs):
        ds = _open(d, args.domain)
        lon, lat, v, mminlu = _field(ds, args.field)
        dx = float(ds.attrs.get("DX", 0)) / 1000.0
        m = _draw(ax, lon, lat, v, args.field, extent)
        # count water cells inside the extent -> a crude "how much coast moved" metric
        w, e, s, n = extent
        inbox = (lon >= w) & (lon <= e) & (lat >= s) & (lat <= n)
        if args.field == "LANDMASK":
            frac_water = 100.0 * np.mean(v[inbox] < 0.5) if inbox.any() else float("nan")
            sub = f"  water={frac_water:.1f}% in box"
        else:
            sub = ""
        ax.set_title(f"d{args.domain:02d} {args.field}  ({dx:.0f} km, {mminlu}){sub}",
                     fontsize=11)
        fig.colorbar(m, ax=ax, shrink=0.7, pad=0.02)
        ds.close()

    fig.suptitle(f"geogrid static geography — {args.field}", fontsize=13)
    fig.tight_layout()
    fig.savefig(args.out, dpi=130)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
