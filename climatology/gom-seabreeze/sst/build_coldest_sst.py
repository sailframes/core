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
# ACSPO L3S lives on the NOAA CoastWatch central ERDDAP; MUR L4 (jplMURSST41)
# lives on the ERD/PFEG node -- different hosts, so each pull names its base.
ERDDAP     = "https://coastwatch.noaa.gov/erddap/griddap"
MUR_ERDDAP = "https://coastwatch.pfeg.noaa.gov/erddap/griddap"

# Primary SST: ACSPO L3S-LEO super-collated (VIIRS+AVHRR, multi-platform),
# ~0.02 deg (~2 km), daily. NESDIS already collates the best-quality clear-sky
# pixels across sensors, so we can composite daily grids directly.
#
# TWO streams, because coverage differs (verified against the CoastWatch ERDDAP
# catalog 2026-07-12):
#   ARCHIVE  noaacwLEOACSPOSSTL3SCDaily  -- science-quality REANALYSIS, day+night
#            super-collated, 2012 -> ~2-month latency (e.g. ended 2026-05-08).
#            Use for HINDCAST / climatology and any date older than ~2 months.
#   NRT      noaacwLEOACSPOSSTL3SnrtPMNight -- near-real-time, only a ~16-day
#            rolling window (e.g. 2026-06-26..07-11). PM/Night = coldest-friendly.
#            Use for FORECAST (race-morning) dates inside that window.
# NOTE the reprocessing GAP between the two (~2 mo ago .. ~16 days ago) is covered
# by neither; a date there can't build a composite. Both carry `quality_level`.
# The old default (nrtAMDay) silently returned ZERO pixels for 2024 dates -- its
# ~16-day window doesn't reach historical dates. That is the bug this fixes.
SST_DATASET_ARCHIVE = "noaacwLEOACSPOSSTL3SCDaily"
SST_DATASET_NRT     = "noaacwLEOACSPOSSTL3SnrtPMNight"
SST_DATASET = SST_DATASET_ARCHIVE              # default; override via --sst-source / --dataset
SST_VAR     = "sea_surface_temperature"
# Composite is only worth its cost if it carries REAL ACSPO cold structure, not
# MUR L4 gap-fill (== the coarse global SST we exist to beat). Warn below this
# fraction of ACSPO-derived cells over water.
MIN_ACSPO_FRAC_WARN = 0.20
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


def open_and_subset(dataset, var, t0, t1, erddap=ERDDAP):
    """Open an ERDDAP griddap dataset over OPeNDAP and subset to the GoM box
    and [t0,t1]. Returns an xarray.DataArray with dims (time, lat, lon) and
    ascending lat/lon, or None on failure."""
    url = f"{erddap}/{dataset}"
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


def build(end_date, tlat, tlon, clim_path, anchor, dataset=SST_DATASET):
    t1 = datetime.strptime(end_date, "%Y-%m-%d")
    t0 = t1 - timedelta(days=WINDOW_DAYS - 1)
    print(f"window: {t0:%Y-%m-%d} .. {t1:%Y-%m-%d}  (coldest of {WINDOW_DAYS} days)")
    print(f"ACSPO dataset: {dataset}")

    res = open_and_subset(dataset, SST_VAR,
                          t0.strftime("%Y-%m-%d"),
                          (t1 + timedelta(days=1)).strftime("%Y-%m-%d"))
    if res is None:
        print(f"FATAL: no SST pulled from {dataset}. Check dataset id / date coverage "
              f"(archive ends ~2 months back; NRT is a ~16-day window) / connectivity.")
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
    acspo_mask = np.isfinite(cold)        # cells with a REAL ACSPO clear-sky obs
    n_acspo = int(acspo_mask.sum())
    print(f"coldest-clear composite: {n_acspo} / {cold.size} cells from ACSPO obs")

    # ---- gap-fill with MUR L4 ----
    mgrid = None
    mres = open_and_subset(MUR_DATASET, MUR_VAR,
                           t1.strftime("%Y-%m-%d"),
                           (t1 + timedelta(days=1)).strftime("%Y-%m-%d"),
                           erddap=MUR_ERDDAP)
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

    # ---- PROVENANCE: is this a real coldest-pixel field or mostly MUR L4? ----
    # A gap-free composite can still be ~100% MUR (coarse global SST = the thing we
    # exist to beat). Report the ACSPO-derived fraction over the *valid* domain.
    n_valid = int(np.isfinite(cold).sum())
    acspo_frac = (n_acspo / n_valid) if n_valid else 0.0
    print(f"PROVENANCE: {acspo_frac*100:.1f}% of valid cells are ACSPO coldest-pixel "
          f"({n_acspo}), the rest MUR L4 gap-fill ({n_valid - n_acspo})")
    if acspo_frac < MIN_ACSPO_FRAC_WARN:
        print(f"  !! WARNING acspo_frac<{MIN_ACSPO_FRAC_WARN:.2f}: composite is mostly MUR "
              f"L4 background -- little real coldest-pixel structure. The 3-day window was "
              f"likely cloud-covered; try a clearer date, a longer WINDOW_DAYS, or the "
              f"night product. Injecting this ~= driver SST.")

    # ---- buoy anchor (domain-mean offset) ----
    if anchor:
        # fetch_buoy_wtmp pulls NDBC realtime2 (~45-day window, latest value). For a
        # date older than that it anchors to TODAY's temp, not the date's -> wrong.
        if (datetime.utcnow() - t1).days > 40:
            print("  ! --anchor SKIPPED: date is >40 days old; NDBC realtime2 only holds "
                  "~45 days and would anchor to today's SST. Use the NDBC stdmet archive "
                  "for hindcast anchoring (not yet wired).")
            anchor = False
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
    return cold, dict(acspo_mask=acspo_mask, acspo_frac=acspo_frac,
                      mur=mgrid, dataset=dataset)


def write_out(cold, tlat, tlon, end_date, outdir, do_plot, diag=None):
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
               "source_sst": (diag or {}).get("dataset", SST_DATASET),
               "source_l4": MUR_DATASET,
               "acspo_pixel_fraction": round(float((diag or {}).get("acspo_frac", float("nan"))), 4),
               "date": end_date, "created": datetime.utcnow().isoformat()})
    path = os.path.join(outdir, f"sst_{end_date}.nc")
    out.to_netcdf(path)
    print(f"wrote {path}")
    if do_plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        mur = (diag or {}).get("mur")
        # panel 1: the composite; panel 2: composite - MUR (where does the coldest-
        # pixel field actually differ from the coarse L4 background? that difference
        # IS the near-shore upwelling/mixing structure we injected).
        ncol = 2 if mur is not None else 1
        fig, axes = plt.subplots(1, ncol, figsize=(7 * ncol, 6), squeeze=False)
        pc = axes[0][0].pcolormesh(tlon, tlat, cold - 273.15, shading="auto", cmap="turbo")
        af = (diag or {}).get("acspo_frac")
        sub = f"  (ACSPO {af*100:.0f}%)" if af is not None else ""
        axes[0][0].set_title(f"coldest-pixel SST {end_date}{sub}")
        fig.colorbar(pc, ax=axes[0][0], label="SST (degC)")
        if mur is not None:
            d = cold - mur
            vmax = float(np.nanpercentile(np.abs(d), 99)) or 1.0
            pd = axes[0][1].pcolormesh(tlon, tlat, d, shading="auto", cmap="RdBu_r",
                                       vmin=-vmax, vmax=vmax)
            axes[0][1].set_title("composite - MUR L4 (K)  [near-shore cold structure]")
            fig.colorbar(pd, ax=axes[0][1], label="K")
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
    ap.add_argument("--sst-source", choices=["auto", "archive", "nrt"], default="auto",
                    help="auto (default: nrt if date within ~18 days else archive), "
                         "archive (SCDaily reanalysis, hindcast/old dates), or "
                         "nrt (PMNight, recent ~16-day window / forecast)")
    ap.add_argument("--dataset", default=None,
                    help="raw ERDDAP dataset id override (beats --sst-source)")
    ap.add_argument("--plot", action="store_true")
    args = ap.parse_args()

    source = args.sst_source
    if source == "auto":
        age = (datetime.utcnow() - datetime.strptime(args.date, "%Y-%m-%d")).days
        source = "nrt" if 0 <= age <= 18 else "archive"
        print(f"sst-source auto -> {source} (date is {age} days old)")
    dataset = args.dataset or (SST_DATASET_NRT if source == "nrt"
                               else SST_DATASET_ARCHIVE)

    tlat = np.arange(LAT_MIN, LAT_MAX + TARGET_RES_DEG, TARGET_RES_DEG)
    tlon = np.arange(LON_MIN, LON_MAX + TARGET_RES_DEG, TARGET_RES_DEG)

    cold, diag = build(args.date, tlat, tlon, args.clim, args.anchor, dataset)
    write_out(cold, tlat, tlon, args.date, args.outdir, args.plot, diag)


if __name__ == "__main__":
    main()
