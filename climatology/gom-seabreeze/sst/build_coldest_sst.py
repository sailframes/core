#!/usr/bin/env python3
"""
build_coldest_sst.py
--------------------
Build a daily, gap-free, ~2 km coldest-dark-pixel SST composite over the
Gulf of Maine from NOAA CoastWatch ACSPO satellite SST, gap-filled with MUR L4
and (optionally) anchored to NDBC buoy water temperature.

Method (see gom_coldest_pixel_sst_spec.md):
  clear-sky selection (ACSPO quality) -> regional despeckle -> COLDEST over a
  rolling N-day window -> gap-fill with MUR -> buoy anchor -> write sst_<date>.nc

Output feeds: patch_met_em_sst.py  ->  real.exe

Requires: numpy, scipy, xarray + an OPeNDAP-capable backend (netCDF4 or pydap),
          requests (only for the optional NDBC buoy anchor); matplotlib if --plot

NOTE ON DATASET IDs: ERDDAP dataset IDs are versioned and change. The defaults
below were valid on the NOAA CoastWatch ERDDAP at time of writing; confirm the
current ID at  https://coastwatch.noaa.gov/erddap/griddap/index.html  if a pull
returns nothing.
"""
import argparse
import sys
from datetime import datetime, timedelta

import numpy as np
from scipy.ndimage import median_filter, gaussian_filter
import xarray as xr

# --------------------------- CONFIG ---------------------------------------
ERDDAP = "https://coastwatch.noaa.gov/erddap/griddap"

# Primary SST: ACSPO L3S-LEO super-collated (VIIRS+AVHRR, multi-platform),
# ~0.02 deg (~2 km), daily, near-real-time. NESDIS has already collated the
# best-quality clear-sky pixels across sensors, which is why we can composite
# daily grids directly. AM/day shown; add the PM/night dataset for a cooler,
# diurnally-cleaner baseline (night is preferable for the coldest field).
SST_DATASET = "noaacwLEOACSPOSSTL3SnrtAMDay"   # id: ACSPO-L3S-LEO-AM
SST_VAR     = "sea_surface_temperature"
# Optional single-sensor L3U (0.75 km, per-granule quality_level) is higher-res
# but far more work/volume; use L3S above unless you need the resolution.

# L4 gap-fill background: MUR 1 km.
MUR_DATASET = "jplMURSST41"
MUR_VAR     = "analysed_sst"

# Gulf of Maine bbox (covers the WRF d01 water footprint).
LAT_MIN, LAT_MAX = 40.0, 45.5
LON_MIN, LON_MAX = -71.5, -65.0

TARGET_RES_DEG     = 0.02   # ~2 km composite grid
WINDOW_DAYS        = 3      # coldest-over-window length
QL_MIN             = 5      # ACSPO 'confidently clear' == GDS2 quality_level 5
DESPECKLE_DELTA_C  = 3.0    # reject pixel > this much colder than local 5x5 median
CLIM_FLOOR_DELTA_C = 4.0    # reject pixel > this below climatology (if --clim given)

# NDBC buoys for anchoring (id: lat, lon).
BUOYS = {"44013": (42.346, -70.651),
         "44018": (42.206, -70.143),
         "44098": (42.798, -70.170)}
# --------------------------------------------------------------------------


def to_kelvin(arr, units):
    u = (units or "").strip().lower()
    if u in ("k", "kelvin", "degk", "degrees_kelvin"):
        return arr
    if u in ("c", "degc", "celsius", "degrees_c", "degree_c", "degrees celsius"):
        return arr + 273.15
    return arr + 273.15 if np.nanmedian(arr) < 100.0 else arr


def _coord(ds, names):
    for n in names:
        if n in ds.coords or n in ds.variables:
            return n
    raise KeyError(f"none of {names} in dataset (have {list(ds.coords)})")


def open_and_subset(dataset, var, t0, t1):
    """Open an ERDDAP griddap dataset over OPeNDAP and subset to the GoM box
    and [t0,t1]. Returns an xarray.DataArray with dims (time, lat, lon) and
    ascending lat/lon, or None on failure."""
    url = f"{ERDDAP}/{dataset}"
    try:
        ds = xr.open_dataset(url)
    except Exception as e:
        print(f"  ! could not open {url}\n    {e}")
        return None

    latn = _coord(ds, ["latitude", "lat"])
    lonn = _coord(ds, ["longitude", "lon"])
    timen = _coord(ds, ["time"])

    lat = ds[latn].values
    lon = ds[lonn].values

    # longitude convention (0..360 vs -180..180)
    lo_min, lo_max = LON_MIN, LON_MAX
    if np.nanmax(lon) > 180.0:
        lo_min = lo_min % 360.0
        lo_max = lo_max % 360.0

    lat_sl = slice(LAT_MAX, LAT_MIN) if lat[0] > lat[-1] else slice(LAT_MIN, LAT_MAX)
    lon_sl = slice(lo_max, lo_min) if lon[0] > lon[-1] else slice(lo_min, lo_max)

    try:
        sub = ds[var].sel({latn: lat_sl, lonn: lon_sl,
                           timen: slice(t0, t1)})
    except Exception as e:
        print(f"  ! subset failed for {dataset}: {e}")
        ds.close()
        return None

    sub = sub.rename({latn: "lat", lonn: "lon", timen: "time"})
    # ascending
    if sub.lat.values[0] > sub.lat.values[-1]:
        sub = sub.isel(lat=slice(None, None, -1))
    if sub.lon.values[0] > sub.lon.values[-1]:
        sub = sub.isel(lon=slice(None, None, -1))
    # wrap 0..360 lon back to negative for our grid
    if np.nanmax(sub.lon.values) > 180.0:
        sub = sub.assign_coords(lon=(((sub.lon + 180) % 360) - 180)).sortby("lon")

    sub.attrs["units"] = ds[var].attrs.get("units", "")
    ql = ds["quality_level"] if "quality_level" in ds.variables else None
    return sub, ql, ds


def regrid(lat, lon, arr, tlat, tlon):
    """Bilinear regrid a regular (lat,lon) field to target (tlat,tlon)."""
    from scipy.interpolate import RegularGridInterpolator
    rgi = RegularGridInterpolator((lat, lon), arr, method="linear",
                                  bounds_error=False, fill_value=np.nan)
    TLON, TLAT = np.meshgrid(tlon, tlat)
    return rgi(np.column_stack([TLAT.ravel(), TLON.ravel()])).reshape(TLAT.shape)


def despeckle(field, delta_c):
    """Reject pixels > delta_c colder than their 5x5 median (cloud leakage)."""
    med = median_filter(np.where(np.isnan(field), np.nanmedian(field), field),
                        size=5, mode="nearest")
    bad = (med - field) > delta_c
    out = field.copy()
    out[bad] = np.nan
    return out


def load_climatology(path, tlat, tlon):
    ds = xr.open_dataset(path)
    sname = None
    for c in ("sst", "SST", "sea_surface_temperature", "analysed_sst"):
        if c in ds:
            sname = c; break
    latn = _coord(ds, ["lat", "latitude"]); lonn = _coord(ds, ["lon", "longitude"])
    da = ds[sname].squeeze()
    arr = to_kelvin(np.asarray(da.values, float), da.attrs.get("units", ""))
    lat = ds[latn].values; lon = ds[lonn].values
    if lat[0] > lat[-1]:
        lat = lat[::-1]; arr = arr[::-1, :]
    if lon[0] > lon[-1]:
        lon = lon[::-1]; arr = arr[:, ::-1]
    ds.close()
    return regrid(lat, lon, arr, tlat, tlon)


def fetch_buoy_wtmp(bid):
    """Latest valid water temp (K) from NDBC realtime2, or None."""
    import requests
    try:
        txt = requests.get(f"https://www.ndbc.noaa.gov/data/realtime2/{bid}.txt",
                           timeout=20).text
    except Exception:
        return None
    for line in txt.splitlines():
        if line.startswith("#"):
            continue
        p = line.split()
        if len(p) < 15:
            continue
        wt = p[14]  # WTMP column
        if wt not in ("MM", "999.0", "99.0"):
            try:
                return float(wt) + 273.15
            except ValueError:
                continue
    return None


def build(end_date, tlat, tlon, clim_path, anchor):
    t1 = datetime.strptime(end_date, "%Y-%m-%d")
    t0 = t1 - timedelta(days=WINDOW_DAYS - 1)
    print(f"window: {t0:%Y-%m-%d} .. {t1:%Y-%m-%d}  (coldest of {WINDOW_DAYS} days)")

    res = open_and_subset(SST_DATASET, SST_VAR,
                          t0.strftime("%Y-%m-%d"),
                          (t1 + timedelta(days=1)).strftime("%Y-%m-%d"))
    if res is None:
        print("FATAL: no SST pulled. Check dataset id / connectivity.")
        sys.exit(2)
    da, ql, ds = res

    stack = []
    for i in range(da.sizes["time"]):
        day = da.isel(time=i)
        arr = to_kelvin(np.asarray(day.values, float), da.attrs.get("units", ""))
        if ql is not None:
            try:
                qday = np.asarray(ql.isel(time=i).sel(
                    lat=day.lat, lon=day.lon, method="nearest").values)
                arr = np.where(qday >= QL_MIN, arr, np.nan)
            except Exception:
                pass  # L3S may not expose per-pixel QL; already clear-sky collated
        g = regrid(day.lat.values, day.lon.values, arr, tlat, tlon)
        g = despeckle(g, DESPECKLE_DELTA_C)
        stack.append(g)
        print(f"  {str(day.time.values)[:10]}: valid px = {np.isfinite(g).sum()}")

    stack = np.array(stack)
    if clim_path:
        clim = load_climatology(clim_path, tlat, tlon)   # K
        floor = clim - CLIM_FLOOR_DELTA_C
        stack = np.where(stack < floor[None, :, :], np.nan, stack)

    with np.errstate(all="ignore"):
        cold = np.nanmin(stack, axis=0)   # COLDEST clear obs per cell
    print(f"coldest-clear composite: {np.isfinite(cold).sum()} / {cold.size} cells filled")

    # ---- gap-fill with MUR L4 ----
    mres = open_and_subset(MUR_DATASET, MUR_VAR,
                           t1.strftime("%Y-%m-%d"),
                           (t1 + timedelta(days=1)).strftime("%Y-%m-%d"))
    if mres is not None:
        mda, _, mds = mres
        m = mda.isel(time=0) if "time" in mda.dims else mda
        mgrid = regrid(m.lat.values, m.lon.values,
                       to_kelvin(np.asarray(m.values, float),
                                 mda.attrs.get("units", "")), tlat, tlon)
        gaps = np.isnan(cold)
        cold_filled = cold.copy()
        cold_filled[gaps] = mgrid[gaps]
        # light feather across the fill boundary so injected structure doesn't
        # read as a spurious front
        if np.isfinite(cold_filled).all():
            band = gaussian_filter(gaps.astype(float), 2) > 0.05
            sm = gaussian_filter(cold_filled, 1.0)
            cold_filled = np.where(band, sm, cold_filled)
        cold = cold_filled
        mds.close()
    else:
        print("  ! MUR gap-fill unavailable; leaving gaps as NaN "
              "(patch_met_em_sst.py will fall back to driver SST there)")

    # ---- buoy anchor (domain-mean offset) ----
    if anchor:
        offs = []
        for bid, (blat, blon) in BUOYS.items():
            obs = fetch_buoy_wtmp(bid)
            if obs is None:
                continue
            j = int(np.abs(tlat - blat).argmin())
            k = int(np.abs(tlon - blon).argmin())
            mod = cold[j, k]
            if np.isfinite(mod):
                offs.append(obs - mod)
                print(f"  buoy {bid}: obs={obs-273.15:5.2f}C model={mod-273.15:5.2f}C "
                      f"d={obs-mod:+.2f}")
        if offs:
            off = float(np.median(offs))
            cold = cold + off
            print(f"  applied buoy offset {off:+.2f} K (median of {len(offs)})")

    ds.close()
    return cold


def write_out(cold, tlat, tlon, end_date, outdir, do_plot):
    import os
    os.makedirs(outdir, exist_ok=True)
    out = xr.Dataset(
        {"sst": (("lat", "lon"), cold.astype("float32"),
                 {"units": "K", "long_name": "coldest_dark_pixel_SST_composite"})},
        coords={"lat": ("lat", tlat.astype("float32")),
                "lon": ("lon", tlon.astype("float32"))},
        attrs={"title": "Gulf of Maine coldest-dark-pixel SST composite",
               "method": f"coldest of {WINDOW_DAYS}-day ACSPO L3S, QL>={QL_MIN}, "
                         f"MUR gap-fill, buoy-anchored",
               "source_sst": SST_DATASET, "source_l4": MUR_DATASET,
               "date": end_date, "created": datetime.utcnow().isoformat()})
    path = os.path.join(outdir, f"sst_{end_date}.nc")
    out.to_netcdf(path)
    print(f"wrote {path}")
    if do_plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(7, 6))
        pc = ax.pcolormesh(tlon, tlat, cold - 273.15, shading="auto", cmap="turbo")
        ax.set_title(f"coldest-pixel SST {end_date}")
        plt.colorbar(pc, ax=ax, label="SST (degC)")
        fig.tight_layout(); fig.savefig(path.replace(".nc", ".png"), dpi=140)
        print(f"wrote {path.replace('.nc', '.png')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="composite end date YYYY-MM-DD")
    ap.add_argument("--outdir", default="./sst_out")
    ap.add_argument("--clim", default=None,
                    help="optional climatology .nc for the cold floor screen")
    ap.add_argument("--anchor", action="store_true", help="anchor to NDBC buoys")
    ap.add_argument("--plot", action="store_true")
    args = ap.parse_args()

    tlat = np.arange(LAT_MIN, LAT_MAX + TARGET_RES_DEG, TARGET_RES_DEG)
    tlon = np.arange(LON_MIN, LON_MAX + TARGET_RES_DEG, TARGET_RES_DEG)

    cold = build(args.date, tlat, tlon, args.clim, args.anchor)
    write_out(cold, tlat, tlon, args.date, args.outdir, args.plot)


if __name__ == "__main__":
    main()
