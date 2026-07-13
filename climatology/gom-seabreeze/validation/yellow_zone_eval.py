#!/usr/bin/env python3
"""yellow_zone_eval.py -- did the WRF run capture the July-4 cross-course sea-breeze asymmetry?

On 2026-07-04 ~13:10 ET (17:10 UTC) a sea breeze filled the ESE side of the EYC race course
(the "yellow zone", ~42.46N,-70.77W) while the NW side stayed light -- the on-water truth this
whole effort is evaluated against. This extracts d03 10 m wind across the course through the
fill window and asks, per run: does the yellow zone fill (veer to sea-breeze + strengthen)
before the NW side, and when?

Outputs: (a) multi-panel wind maps (rows=runs, cols=times) with the yellow zone circled, and
(b) a yellow-zone-vs-NW wind-speed/direction time series per run. Run on a box with
xarray+netCDF4+matplotlib and S3 access.
"""
import argparse
import datetime as dt
import subprocess
from pathlib import Path

import numpy as np

# course geometry (estimated from the race screenshot; --yz/--bbox to override)
YZ = dict(lat=42.46, lon=-70.77, r_km=2.5)          # yellow zone (sea breeze filled here first)
NW = dict(lat=42.50, lon=-70.84, r_km=2.5)          # NW side (stayed light) -- Marblehead Neck approaches
BBOX = dict(lat0=42.40, lat1=42.54, lon0=-70.90, lon1=-70.70)


def sh(c):
    subprocess.run(c, shell=True, check=True)


def frames_in(prefix, hours_utc):
    out = subprocess.run(f"aws s3 ls {prefix}/", shell=True, capture_output=True, text=True).stdout
    want = {f"wrfout_d03_2026-07-04_{h:02d}:{m:02d}:00" for h in hours_utc for m in (0, 15, 30, 45)}
    return sorted(l.split()[-1] for l in out.splitlines() if l.split()[-1] in want)


def load(prefix, scratch, label, hours_utc):
    import xarray as xr
    d = Path(scratch) / label; d.mkdir(parents=True, exist_ok=True)
    recs = {}
    for fn in frames_in(prefix, hours_utc):
        lp = d / fn
        if not lp.exists():
            sh(f"aws s3 cp --quiet {prefix}/{fn} {lp}")
        ds = xr.open_dataset(lp)
        t = dt.datetime.strptime("".join(ds["Times"].isel(Time=0).values.astype(str)),
                                 "%Y-%m-%d_%H:%M:%S")
        la = ds["XLAT"].isel(Time=0).values; lo = ds["XLONG"].isel(Time=0).values
        ug = ds["U10"].isel(Time=0).values; vg = ds["V10"].isel(Time=0).values
        # U10/V10 are GRID-relative -> rotate to earth-relative (else dir is off by map rotation)
        sa = ds["SINALPHA"].isel(Time=0).values; ca = ds["COSALPHA"].isel(Time=0).values
        u = ug * ca - vg * sa; v = vg * ca + ug * sa
        recs[t] = dict(la=la, lo=lo, u=u, v=v)
        ds.close()
    return recs


def disc_mask(la, lo, c):
    dkm = np.hypot((la - c["lat"]) * 111, (lo - c["lon"]) * 111 * np.cos(np.radians(la)))
    return dkm <= c["r_km"]


def zone_stats(rec, c):
    m = disc_mask(rec["la"], rec["lo"], c)
    u = rec["u"][m].mean(); v = rec["v"][m].mean()
    spd = np.hypot(u, v) * 1.94384                       # kt
    drc = (270 - np.degrees(np.arctan2(v, u))) % 360     # met dir
    return spd, drc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", required=True, help="label=s3prefix")
    ap.add_argument("--hours", default="14,15,16,17,18,19", help="UTC hours to load")
    ap.add_argument("--panel-times", default="16:30,17:00,17:15,17:45,18:30",
                    help="UTC HH:MM columns for the map panels")
    ap.add_argument("--scratch", default="/mnt/yz")
    ap.add_argument("--outdir", default="/root/yz")
    a = ap.parse_args()
    hours = [int(x) for x in a.hours.split(",")]
    runs = {}
    for spec in a.run:
        label, prefix = spec.split("=", 1)
        runs[label] = load(prefix, a.scratch, label, hours)
    Path(a.outdir).mkdir(parents=True, exist_ok=True)

    # (b) time series: yellow zone vs NW, per run
    print(f"\n{'time ET':8} " + " ".join(f"{lb:>22}" for lb in runs))
    print("         " + " ".join(f"{'YZ kt/dir  NW kt/dir':>22}" for _ in runs))
    all_t = sorted(next(iter(runs.values())).keys())
    series = {lb: dict(t=[], yz=[], nw=[], dyz=[], dnw=[]) for lb in runs}
    for t in all_t:
        et = (t - dt.timedelta(hours=4)).strftime("%H:%M")
        row = []
        for lb, recs in runs.items():
            if t not in recs:
                row.append(" " * 22); continue
            ys, yd = zone_stats(recs[t], YZ); ns, nd = zone_stats(recs[t], NW)
            series[lb]["t"].append(t); series[lb]["yz"].append(ys); series[lb]["nw"].append(ns)
            series[lb]["dyz"].append(yd); series[lb]["dnw"].append(nd)
            row.append(f"{ys:4.1f}/{yd:03.0f}  {ns:4.1f}/{nd:03.0f}".rjust(22))
        print(f"{et:8} " + " ".join(row))

    # onset = first time YZ exceeds NW by >=3 kt AND YZ dir is onshore (sea breeze ~ SE, 90-200)
    print("\nsea-breeze fill in the YELLOW ZONE (YZ-NW >=3kt & YZ dir 90-200):")
    for lb, s in series.items():
        onset = None
        for i, t in enumerate(s["t"]):
            if s["yz"][i] - s["nw"][i] >= 3 and 90 <= s["dyz"][i] <= 200:
                onset = t; break
        et = (onset - dt.timedelta(hours=4)).strftime("%H:%M ET") if onset else "not captured"
        print(f"  {lb:16} onset = {et}")

    # (a) maps
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Circle
        pts = [dt.datetime.strptime("2026-07-04 " + p, "%Y-%m-%d %H:%M") for p in a.panel_times.split(",")]
        nr, nc = len(runs), len(pts)
        fig, ax = plt.subplots(nr, nc, figsize=(3.4 * nc, 3.4 * nr), squeeze=False)
        for ri, (lb, recs) in enumerate(runs.items()):
            for ci, t in enumerate(pts):
                A = ax[ri][ci]
                if t not in recs:
                    A.set_axis_off(); continue
                r = recs[t]; spd = np.hypot(r["u"], r["v"]) * 1.94384
                inb = ((r["la"] >= BBOX["lat0"]) & (r["la"] <= BBOX["lat1"]) &
                       (r["lo"] >= BBOX["lon0"]) & (r["lo"] <= BBOX["lon1"]))
                pc = A.pcolormesh(r["lo"], r["la"], np.ma.masked_where(~inb, spd),
                                  cmap="viridis", vmin=0, vmax=14, shading="auto")
                st = 3
                A.quiver(r["lo"][::st, ::st], r["la"][::st, ::st], r["u"][::st, ::st], r["v"][::st, ::st],
                         scale=200, width=.004, color="white")
                A.add_patch(Circle((YZ["lon"], YZ["lat"]), YZ["r_km"] / 111, fill=False, ec="yellow", lw=2.5))
                A.add_patch(Circle((NW["lon"], NW["lat"]), NW["r_km"] / 111, fill=False, ec="red", lw=1.5, ls="--"))
                A.set_xlim(BBOX["lon0"], BBOX["lon1"]); A.set_ylim(BBOX["lat0"], BBOX["lat1"])
                A.set_xticks([]); A.set_yticks([])
                if ri == 0:
                    A.set_title((t - dt.timedelta(hours=4)).strftime("%H:%M ET"))
                if ci == 0:
                    A.set_ylabel(lb, fontsize=11)
        fig.suptitle("d03 10 m wind — July-4 cross-course sea-breeze (yellow=fill zone, red=NW side)", y=1.0)
        fig.colorbar(pc, ax=ax, shrink=.6, label="wind speed (kt)")
        fig.savefig(f"{a.outdir}/yellow_zone_maps.png", dpi=110, bbox_inches="tight")
        print(f"\nmaps -> {a.outdir}/yellow_zone_maps.png")
    except Exception as e:  # noqa: BLE001
        print(f"maps skipped: {e}")


if __name__ == "__main__":
    main()
