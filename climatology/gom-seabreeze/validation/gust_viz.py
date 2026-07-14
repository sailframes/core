#!/usr/bin/env python3
"""gust_viz.py -- turn the LES d05 (111 m) output into gust visuals + the Step-5 turbulence check.

Produces:
  1. pressure_movie.mp4  -- d05 10 m wind SPEED field animated 1-min through the race window
                            (the "puffs" = dark cells drifting across the course)
  2. gustiness.png       -- gust factor (peak/mean over the window) per cell = which side is puffy
  3. point_trace.png     -- speed + direction time series at the course center (oscillation pattern)
  4. STEP-5 CHECK (stdout + json): resolved wind-speed std + gust factor + spatial variance.
     LES SUCCESS = d05 developed structure (std/gust-factor well above the smooth parent), not
     just exit-0. If laminar (flat field), the LES didn't spin up -> fetch/spin-up problem.

Reads d05 wrfout straight from S3. Run on a box with xarray+netCDF4+matplotlib(+ffmpeg).
"""
import argparse, datetime as dt, json, subprocess
from pathlib import Path
import numpy as np

CEN = dict(lat=42.46, lon=-70.77)   # course center

def sh(c): subprocess.run(c, shell=True, check=True)

def load(prefix, scratch, hh0, hh1):
    import xarray as xr
    out = subprocess.run(f"aws s3 ls {prefix}/", shell=True, capture_output=True, text=True).stdout
    frames = sorted(l.split()[-1] for l in out.splitlines()
                    if "wrfout_d05_2026-07-04_" in l and hh0 <= int(l.split("_")[-1][:2]) < hh1)
    d = Path(scratch); d.mkdir(parents=True, exist_ok=True)
    recs = []
    for fn in frames:
        lp = d/fn
        if not lp.exists(): sh(f"aws s3 cp --quiet {prefix}/{fn} {lp}")
        ds = xr.open_dataset(lp)
        la = ds["XLAT"].isel(Time=0).values; lo = ds["XLONG"].isel(Time=0).values
        ug = ds["U10"].isel(Time=0).values; vg = ds["V10"].isel(Time=0).values
        sa = ds["SINALPHA"].isel(Time=0).values; ca = ds["COSALPHA"].isel(Time=0).values
        u = ug*ca - vg*sa; v = vg*ca + ug*sa                    # earth-relative
        raw = ds["Times"].isel(Time=0).values
        ts = "".join(c.decode() if isinstance(c, bytes) else str(c) for c in np.atleast_1d(raw).ravel())
        recs.append(dict(t=dt.datetime.strptime(ts, "%Y-%m-%d_%H:%M:%S"),
                         la=la, lo=lo, spd=np.hypot(u, v)*1.94384,
                         dr=(270-np.degrees(np.arctan2(v, u)))%360, u=u, v=v))
        ds.close()
    return recs

def nearest(la, lo, c):
    d2 = (la-c["lat"])**2 + (lo-c["lon"])**2
    return np.unravel_index(np.argmin(d2), d2.shape)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True, help="s3 dir with wrfout_d05_*")
    ap.add_argument("--scratch", default="/mnt/g"); ap.add_argument("--outdir", default="/root/gust")
    ap.add_argument("--hh0", type=int, default=13); ap.add_argument("--hh1", type=int, default=18)
    a = ap.parse_args()
    recs = load(a.prefix, a.scratch, a.hh0, a.hh1)
    Path(a.outdir).mkdir(parents=True, exist_ok=True)
    if not recs:
        print("NO d05 frames"); return
    la, lo = recs[0]["la"], recs[0]["lo"]; jj, ii = nearest(la, lo, CEN)
    S = np.stack([r["spd"] for r in recs])                       # (t, y, x) kt
    mean = S.mean(0); peak = S.max(0); gf = peak/np.maximum(mean, 0.1)
    # STEP-5 turbulence check: spatial std of the instantaneous field (avg over time) +
    # temporal gust factor at the course. Laminar parent-interp ~ tiny std; real LES ~ large.
    spatial_std = float(np.mean([r["spd"].std() for r in recs]))
    pt = S[:, jj, ii]
    gust_factor_pt = float(pt.max()/max(pt.mean(), .1))
    verdict = dict(frames=len(recs), spatial_std_kt=round(spatial_std, 3),
                   course_mean_kt=round(float(pt.mean()), 2), course_peak_kt=round(float(pt.max()), 2),
                   course_gust_factor=round(gust_factor_pt, 3),
                   turbulence="RESOLVED" if spatial_std > 0.6 and gust_factor_pt > 1.15 else "LAMINAR?(check spin-up)")
    print("STEP-5:", json.dumps(verdict))
    json.dump(verdict, open(f"{a.outdir}/step5_turbulence.json", "w"))
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.animation import FuncAnimation
        from matplotlib.patches import Circle
        # (2) gustiness map
        fig, ax = plt.subplots(figsize=(7, 7))
        pc = ax.pcolormesh(lo, la, gf, cmap="magma", vmin=1, vmax=1.6, shading="auto")
        ax.add_patch(Circle((CEN["lon"], CEN["lat"]), 0.02, fill=False, ec="cyan", lw=2))
        ax.set_title("Gustiness = gust factor (peak/mean), d05 111m"); fig.colorbar(pc, label="peak/mean")
        fig.savefig(f"{a.outdir}/gustiness.png", dpi=110, bbox_inches="tight"); plt.close(fig)
        # (3) point trace
        fig, ax = plt.subplots(2, 1, figsize=(11, 5), sharex=True)
        t = [r["t"] for r in recs]
        ax[0].plot(t, pt, "b-", lw=.8); ax[0].axhline(pt.mean(), color="k", ls="--", lw=.6)
        ax[0].set_ylabel("speed (kt)"); ax[0].set_title(f"Course-center wind (d05 1-min) — gust factor {gust_factor_pt:.2f}")
        ax[1].plot(t, [r["dr"][jj, ii] for r in recs], "g-", lw=.8); ax[1].set_ylabel("dir (deg)")
        fig.savefig(f"{a.outdir}/point_trace.png", dpi=110, bbox_inches="tight"); plt.close(fig)
        # (1) pressure movie
        fig, ax = plt.subplots(figsize=(7, 7))
        q = ax.pcolormesh(lo, la, S[0], cmap="viridis", vmin=0, vmax=max(14, peak.max()), shading="auto")
        ax.add_patch(Circle((CEN["lon"], CEN["lat"]), 0.02, fill=False, ec="red", lw=2))
        ttl = ax.set_title("")
        fig.colorbar(q, label="wind speed (kt)")
        def upd(k):
            q.set_array(S[k].ravel())
            ttl.set_text(f"d05 10m wind — {(recs[k]['t']-dt.timedelta(hours=4)):%H:%M} ET (expected gust structure)")
            return q, ttl
        anim = FuncAnimation(fig, upd, frames=len(recs), interval=120, blit=False)
        try:
            anim.save(f"{a.outdir}/pressure_movie.mp4", dpi=90, writer="ffmpeg")
        except Exception:
            anim.save(f"{a.outdir}/pressure_movie.gif", dpi=70, writer="pillow")
        plt.close(fig)
        print(f"wrote gustiness.png, point_trace.png, pressure_movie.* -> {a.outdir}")
    except Exception as e:
        print(f"viz skipped: {e}")

if __name__ == "__main__":
    main()
