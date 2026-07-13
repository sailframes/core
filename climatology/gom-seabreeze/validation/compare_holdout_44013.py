#!/usr/bin/env python3
"""compare_holdout_44013.py -- held-out obs-nudging validation at buoy 44013.

44013 is EXCLUDED from the nudged-validation run's little_r, so its wind is an independent
truth. This extracts d03 10 m wind at 44013 from each WRF run, scores against the 44013 buoy,
and answers: does assimilating the station network (d01/d02, inherited by d03 via one-way
nesting) pull the offshore point toward reality vs the physics-matched free run?

Runs to compare (all YSU physics, WRF 4.8, 2026-07-04):
  freerun-ysu  obs_nudge_opt=0,0,0  (control)
  nudged-val   obs_nudge_opt=1,1,0, 44013 held out  (d01/d02 nudged, d03 inherits)
  [nudged      obs_nudge_opt=1,1,1, all stations -- shown for reference; 44013 IS nudged so not independent]

Reads wrfout_d03 straight from S3 (downloads to a scratch dir), finds the nearest d03 grid
cell to 44013, builds 10 m wind speed/direction time series, fetches the buoy, prints MAE
and writes a comparison plot. Run on a box with xarray+netCDF4 (the gom venv) + AWS creds.

Usage:
  compare_holdout_44013.py --date 2026-07-04 \
    --run freerun-ysu=s3://.../gom/2026-07-04/freerun-ysu \
    --run nudged-val=s3://.../gom/2026-07-04/nudged-val \
    --run nudged=s3://.../gom/2026-07-04/nudged \
    --scratch /tmp/val --out /tmp/val/holdout_44013.png
"""
import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path

import numpy as np

LAT44013, LON44013 = 42.346, -70.651


def sh(cmd):
    subprocess.run(cmd, shell=True, check=True)


def s3_ls_d03(prefix):
    out = subprocess.run(f"aws s3 ls {prefix}/", shell=True, capture_output=True, text=True).stdout
    return sorted(l.split()[-1] for l in out.splitlines() if "wrfout_d03" in l)


def extract_point_series(prefix, scratch, label):
    """Download this run's wrfout_d03 frames, extract 10m wind at the cell nearest 44013."""
    import xarray as xr
    d = Path(scratch) / label
    d.mkdir(parents=True, exist_ok=True)
    frames = s3_ls_d03(prefix)
    if not frames:
        print(f"  {label}: no wrfout_d03 frames at {prefix}", file=sys.stderr)
        return None
    times, spd, drc = [], [], []
    jj = ii = None
    for fn in frames:
        lp = d / fn
        if not lp.exists():
            sh(f"aws s3 cp --quiet {prefix}/{fn} {lp}")
        ds = xr.open_dataset(lp)
        if jj is None:  # nearest grid cell to 44013 (constant across frames)
            la = ds["XLAT"].isel(Time=0).values; lo = ds["XLONG"].isel(Time=0).values
            dist2 = (la - LAT44013) ** 2 + (lo - LON44013) ** 2
            jj, ii = np.unravel_index(np.argmin(dist2), dist2.shape)
            print(f"  {label}: nearest d03 cell (j={jj},i={ii}) at {la[jj,ii]:.3f},{lo[jj,ii]:.3f}")
        ug = float(ds["U10"].isel(Time=0).values[jj, ii])
        vg = float(ds["V10"].isel(Time=0).values[jj, ii])
        # U10/V10 are GRID-relative -> rotate to earth-relative (SINALPHA/COSALPHA) for direction
        sa = float(ds["SINALPHA"].isel(Time=0).values[jj, ii])
        ca = float(ds["COSALPHA"].isel(Time=0).values[jj, ii])
        u = ug * ca - vg * sa; v = vg * ca + ug * sa
        raw = ds["Times"].isel(Time=0).values  # char |S1 array or 0-d bytestring
        ts = "".join(c.decode() if isinstance(c, bytes) else str(c) for c in np.atleast_1d(raw).ravel())
        times.append(dt.datetime.strptime(ts, "%Y-%m-%d_%H:%M:%S"))
        spd.append((u * u + v * v) ** 0.5)
        drc.append((270.0 - np.degrees(np.arctan2(v, u))) % 360.0)  # meteorological dir
        ds.close()
    return dict(t=times, spd=np.array(spd), dir=np.array(drc))


def fetch_buoy_44013(t0, t1):
    """NDBC 44013 realtime2 wind (m/s, degT) over [t0,t1]."""
    import urllib.request
    req = urllib.request.Request("https://www.ndbc.noaa.gov/data/realtime2/44013.txt",
                                 headers={"User-Agent": "sailframes-gom/1.0"})
    txt = urllib.request.urlopen(req, timeout=40).read().decode()
    t, spd, drc = [], [], []
    for line in txt.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        c = line.split()
        try:
            tt = dt.datetime(int(c[0]), int(c[1]), int(c[2]), int(c[3]), int(c[4]))
        except (ValueError, IndexError):
            continue
        if not (t0 <= tt <= t1) or c[5] == "MM" or c[6] == "MM":
            continue
        t.append(tt); drc.append(float(c[5])); spd.append(float(c[6]))
    order = np.argsort(t)
    return dict(t=[t[i] for i in order], spd=np.array(spd)[order], dir=np.array(drc)[order])


def ang_err(a, b):
    d = np.abs((a - b + 180) % 360 - 180)
    return d


def interp_to(buoy_t, run):
    """nearest-time run value at each buoy time (within 8 min)."""
    rt = np.array([x.timestamp() for x in run["t"]])
    out_s, out_d = [], []
    for bt in buoy_t:
        k = int(np.argmin(np.abs(rt - bt.timestamp())))
        if abs(rt[k] - bt.timestamp()) <= 480:
            out_s.append(run["spd"][k]); out_d.append(run["dir"][k])
        else:
            out_s.append(np.nan); out_d.append(np.nan)
    return np.array(out_s), np.array(out_d)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--run", action="append", required=True, help="label=s3prefix (repeatable)")
    ap.add_argument("--run-hours", type=int, default=36)
    ap.add_argument("--scratch", default="/tmp/val")
    ap.add_argument("--out", default="/tmp/val/holdout_44013.png")
    a = ap.parse_args()
    d0 = dt.datetime.strptime(a.date, "%Y-%m-%d"); t1 = d0 + dt.timedelta(hours=a.run_hours)

    runs = {}
    for spec in a.run:
        label, prefix = spec.split("=", 1)
        runs[label] = extract_point_series(prefix, a.scratch, label)
    buoy = fetch_buoy_44013(d0, t1)
    print(f"\nbuoy 44013: {len(buoy['t'])} obs {d0:%m-%d %H}Z..{t1:%m-%d %H}Z")

    print(f"\n{'run':14} {'MAE wspd (kt)':>14} {'MAE wdir (deg)':>15}  (vs held-out 44013)")
    print("-" * 48)
    for label, r in runs.items():
        if r is None:
            continue
        rs, rd = interp_to(buoy["t"], r)
        m = ~np.isnan(rs)
        mae_s = np.nanmean(np.abs(rs[m] - buoy["spd"][m])) * 1.94384  # m/s -> kt
        mae_d = np.nanmean(ang_err(rd[m], buoy["dir"][m]))
        print(f"{label:14} {mae_s:14.2f} {mae_d:15.1f}")

    # plot
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(2, 1, figsize=(13, 8), sharex=True)
        ax[0].plot(buoy["t"], buoy["spd"] * 1.94384, "k.-", lw=1.5, label="44013 buoy (truth)")
        ax[1].plot(buoy["t"], buoy["dir"], "k.", ms=4, label="44013 buoy (truth)")
        for label, r in runs.items():
            if r is None:
                continue
            ax[0].plot(r["t"], r["spd"] * 1.94384, "-", lw=1.2, label=label)
            ax[1].plot(r["t"], r["dir"], "-", lw=1.2, label=label)
        ax[0].set_ylabel("wind speed (kt)"); ax[1].set_ylabel("wind dir (deg)")
        ax[0].set_title(f"Held-out validation at 44013 (d03 10m wind) — {a.date}")
        ax[0].legend(loc="upper left"); ax[0].grid(alpha=.3); ax[1].grid(alpha=.3)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        fig.tight_layout(); fig.savefig(a.out, dpi=110)
        print(f"\nplot -> {a.out}")
    except Exception as e:  # noqa: BLE001
        print(f"plot skipped: {e}")


if __name__ == "__main__":
    main()
