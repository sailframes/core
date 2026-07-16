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

def load(prefix, scratch, hh0, hh1, domain="d05"):
    import xarray as xr
    out = subprocess.run(f"aws s3 ls {prefix}/", shell=True, capture_output=True, text=True).stdout
    def _inwin(l):                      # hh1>24 spans past midnight (evening race): e.g. hh0=21 hh1=25 -> 21,22,23,00
        h = int(l.split("_")[-1][:2])
        return (hh0 <= h < min(hh1, 24)) or (hh1 > 24 and h < hh1 - 24)
    frames = sorted(l.split()[-1] for l in out.splitlines()
                    if f"wrfout_{domain}_" in l and _inwin(l))
    d = Path(scratch); d.mkdir(parents=True, exist_ok=True)
    recs = []; lm = None
    for fn in frames:
        lp = d/fn
        if not lp.exists(): sh(f"aws s3 cp --quiet {prefix}/{fn} {lp}")
        ds = xr.open_dataset(lp)
        la = ds["XLAT"].isel(Time=0).values; lo = ds["XLONG"].isel(Time=0).values
        if lm is None and "LANDMASK" in ds:
            lm = ds["LANDMASK"].isel(Time=0).values          # 1=land 0=water -> coastline contour
        ug = ds["U10"].isel(Time=0).values; vg = ds["V10"].isel(Time=0).values
        sa = ds["SINALPHA"].isel(Time=0).values; ca = ds["COSALPHA"].isel(Time=0).values
        u = ug*ca - vg*sa; v = vg*ca + ug*sa                    # earth-relative
        raw = ds["Times"].isel(Time=0).values
        ts = "".join(c.decode() if isinstance(c, bytes) else str(c) for c in np.atleast_1d(raw).ravel())
        recs.append(dict(t=dt.datetime.strptime(ts, "%Y-%m-%d_%H:%M:%S"),
                         la=la, lo=lo, spd=np.hypot(u, v)*1.94384,
                         dr=(270-np.degrees(np.arctan2(v, u)))%360, u=u, v=v))
        ds.close()
    return recs, lm

def nearest(la, lo, c):
    d2 = (la-c["lat"])**2 + (lo-c["lon"])**2
    return np.unravel_index(np.argmin(d2), d2.shape)

def gshhg_coast(bbox, shp_dir):
    """GSHHG full-res L1 land rings overlapping bbox -> [(lons,lats)] for ax.plot.
    Far more detailed than the ~900 m model LANDMASK (resolves the harbor islands)."""
    import glob, shapefile                                          # pyshp
    lo0, lo1, la0, la1 = bbox; segs = []
    files = glob.glob(f"{shp_dir}/**/GSHHS_f_L1.shp", recursive=True)
    if not files: return segs
    for sh in shapefile.Reader(files[0]).shapes():
        bx = sh.bbox                                                # xmin,ymin,xmax,ymax
        if bx[2] < lo0 or bx[0] > lo1 or bx[3] < la0 or bx[1] > la1:
            continue                                                # polygon doesn't touch bbox
        pts = sh.points; parts = list(sh.parts) + [len(pts)]
        for i in range(len(parts) - 1):
            ring = pts[parts[i]:parts[i + 1]]
            segs.append(([p[0] for p in ring], [p[1] for p in ring]))
    return segs

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True, help="s3 dir with wrfout_<domain>_*")
    ap.add_argument("--domain", default="d05", help="LES nest: d05 (5-dom GFS-LES) or d04 (4-dom HRRR-LES)")
    ap.add_argument("--cen-lat", type=float, default=42.46, help="course center lat (Marblehead default; Hospital Shoals=42.31)")
    ap.add_argument("--cen-lon", type=float, default=-70.77, help="course center lon (Marblehead default; Hospital Shoals=-70.95)")
    ap.add_argument("--scratch", default="/mnt/g"); ap.add_argument("--outdir", default="/root/gust")
    ap.add_argument("--hh0", type=int, default=13); ap.add_argument("--hh1", type=int, default=18)
    ap.add_argument("--bbox", default=None, help="crop plots to lo0,lo1,la0,la1 (match LES extent for HRRR compare)")
    ap.add_argument("--coast-shp", default=None, help="dir with GSHHG GSHHS_f_L1.shp (detailed coast; else model LANDMASK)")
    a = ap.parse_args()
    global CEN; CEN = dict(lat=a.cen_lat, lon=a.cen_lon)
    recs, LM = load(a.prefix, a.scratch, a.hh0, a.hh1, a.domain)
    Path(a.outdir).mkdir(parents=True, exist_ok=True)
    if not recs:
        print(f"NO {a.domain} frames"); return
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
        latm = float(np.mean(la)); aspect = 1/np.cos(np.radians(latm))
        um = np.mean([r["u"] for r in recs], 0); vm = np.mean([r["v"] for r in recs], 0)   # time-mean flow
        st = max(1, la.shape[0]//16)                                                        # quiver stride
        loq, laq = lo[::st, ::st], la[::st, ::st]
        mean_dir = (270 - np.degrees(np.arctan2(vm.mean(), um.mean()))) % 360               # dir wind FROM
        bb = [float(x) for x in a.bbox.split(",")] if a.bbox else None                      # lo0,lo1,la0,la1
        coast = None
        if a.coast_shp:
            cb = bb or [float(lo.min()), float(lo.max()), float(la.min()), float(la.max())]
            try:
                coast = gshhg_coast(cb, a.coast_shp); print(f"  GSHHG coast: {len(coast)} rings")
            except Exception as e:
                print("  GSHHG coast failed, fall back to LANDMASK:", e)
        def decorate(ax, coast_c="0.15", star_c="cyan", quiver_c=None):
            if coast:                                                                       # detailed GSHHG coast + islands
                for xs, ys in coast:
                    ax.plot(xs, ys, "-", color=coast_c, lw=1.0, solid_capstyle="round")
            elif LM is not None:
                ax.contour(lo, la, LM, levels=[0.5], colors=coast_c, linewidths=0.9)        # fallback: model land mask
            qh = None
            if quiver_c is not None:                                                        # STATIC mean-flow arrows (stills)
                qh = ax.quiver(loq, laq, um[::st, ::st], vm[::st, ::st], color=quiver_c, alpha=0.8, scale=140, width=0.005)
            ax.plot(CEN["lon"], CEN["lat"], "*", ms=20, mfc=star_c, mec="k", mew=1.2)        # START marker
            ax.annotate("START\nHospital Shoals", (CEN["lon"], CEN["lat"]), textcoords="offset points",
                        xytext=(8, 8), color=star_c, fontsize=9, fontweight="bold")
            ax.set_aspect(aspect); ax.set_xlabel("lon"); ax.set_ylabel("lat")
            if bb: ax.set_xlim(bb[0], bb[1]); ax.set_ylim(bb[2], bb[3])
            return qh
        def mean_wind_ref(ax):
            # CONSTANT (steady) wind = big magenta reference arrow + speed/dir label, distinct from
            # the thin instantaneous (gust) arrows. Anchored top-left of the frame.
            umn, vmn = float(um.mean()), float(vm.mean()); ms = float(S.mean())
            if bb: cx = bb[0] + 0.17 * (bb[1] - bb[0]); cy = bb[2] + 0.88 * (bb[3] - bb[2])
            else:  cx = float(lo.mean()); cy = float(la.max())
            ax.quiver([cx], [cy], [umn], [vmn], color="magenta", scale=40, width=0.013,
                      headwidth=4, headlength=5, pivot="mid", zorder=7)
            ax.annotate(f"MEAN WIND (period avg)\n{ms:.1f} kt from {mean_dir:.0f}°", (cx, cy),
                        textcoords="offset points", xytext=(10, 12), color="magenta", fontsize=9.5,
                        fontweight="bold", zorder=7,
                        bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="magenta", alpha=0.85))
        # (2) gustiness map -- adaptive scale (this evening's gust factor >> the old 1.6 default)
        gvmax = float(min(3.0, max(1.4, np.nanpercentile(gf, 97))))
        fig, ax = plt.subplots(figsize=(7.4, 7.6))
        pc = ax.pcolormesh(lo, la, gf, cmap="magma", vmin=1, vmax=gvmax, shading="auto")
        qh = decorate(ax, coast_c="cyan", star_c="lime", quiver_c="white")
        if qh is not None:
            ax.quiverkey(qh, 0.83, 0.055, 10, "10 kt", labelpos="E", coordinates="axes",
                         color="white", labelcolor="white", fontproperties={"weight": "bold", "size": 10})
        mean_wind_ref(ax)
        ax.set_title(f"Gustiness (gust factor peak/mean) — {a.domain}\ncyan=coast · white=mean flow (→10kt key) · magenta=CONSTANT wind kt/dir")
        fig.colorbar(pc, label="peak/mean", fraction=0.046, pad=0.02)
        fig.savefig(f"{a.outdir}/gustiness.png", dpi=110, bbox_inches="tight"); plt.close(fig)
        # (3) point trace
        fig, ax = plt.subplots(2, 1, figsize=(11, 5), sharex=True)
        t = [r["t"] for r in recs]
        ax[0].plot(t, pt, "b-", lw=.8); ax[0].axhline(pt.mean(), color="k", ls="--", lw=.6)
        ax[0].set_ylabel("speed (kt)"); ax[0].set_title(f"Course-center wind ({a.domain} 1-min) — gust factor {gust_factor_pt:.2f}")
        ax[1].plot(t, [r["dr"][jj, ii] for r in recs], "g.", ms=2); ax[1].set_ylabel("dir (from, deg)"); ax[1].set_ylim(0, 360)
        import matplotlib.dates as mdates
        def _tfmt(x, _):
            d = mdates.num2date(x).replace(tzinfo=None)
            return f"{(d - dt.timedelta(hours=4)):%H:%M} ET\n{d:%H:%M} Z"
        ax[1].xaxis.set_major_locator(mdates.HourLocator())
        ax[1].xaxis.set_major_formatter(plt.FuncFormatter(_tfmt))
        ax[1].set_xlabel("time (Boston ET / UTC Z)")
        fig.savefig(f"{a.outdir}/point_trace.png", dpi=110, bbox_inches="tight"); plt.close(fig)
        # (1) pressure movie -- arrows ANIMATE with the instantaneous flow each frame
        fig, ax = plt.subplots(figsize=(7.4, 7.6))
        svmax = float(min(np.nanpercentile(S, 99.5), max(10, float(peak.max()))))
        q = ax.pcolormesh(lo, la, S[0], cmap="turbo", vmin=0, vmax=svmax, shading="auto")
        decorate(ax, coast_c="white", star_c="red", quiver_c=None)                           # coast+start only; flow below animates
        # (#4) animated WIND LINES (streamlines) — reveal eddies/shadows/shifts far better than arrows.
        # WRF grid is ~uniform -> linspace grid for streamplot; try/except falls back to arrows.
        _sx = np.linspace(float(lo[0, :].min()), float(lo[0, :].max()), lo.shape[1])
        _sy = np.linspace(float(la[:, 0].min()), float(la[:, 0].max()), la.shape[0])
        _stream = [None]; _Qa = [None]
        def _draw_flow(k):
            if _stream[0] is not None:
                try: _stream[0].lines.remove(); _stream[0].arrows.set_visible(False)
                except Exception: pass
            try:
                _stream[0] = ax.streamplot(_sx, _sy, recs[k]["u"], recs[k]["v"], color="w",
                                           density=1.5, linewidth=0.7, arrowsize=0.6)
            except Exception:                                                                # fallback: animated arrows
                if _Qa[0] is None:
                    _Qa[0] = ax.quiver(loq, laq, recs[k]["u"][::st, ::st], recs[k]["v"][::st, ::st],
                                       color="black", alpha=0.9, scale=140, width=0.005)
                    ax.quiverkey(_Qa[0], 0.83, 0.055, 10, "10 kt", labelpos="E", coordinates="axes",
                                 color="yellow", labelcolor="yellow", fontproperties={"weight": "bold", "size": 10})
                else:
                    _Qa[0].set_UVC(recs[k]["u"][::st, ::st], recs[k]["v"][::st, ::st])
        _draw_flow(0)
        # MEAN/background wind ANIMATES per frame (domain-mean this instant, not the whole-period avg)
        def _fmean(k):
            return float(np.mean(recs[k]["u"])), float(np.mean(recs[k]["v"]))
        _mx = (bb[0] + 0.17 * (bb[1] - bb[0])) if bb else float(lo.mean())
        _my = (bb[2] + 0.88 * (bb[3] - bb[2])) if bb else float(la.max())
        _u0, _v0 = _fmean(0)
        Qm = ax.quiver([_mx], [_my], [_u0], [_v0], color="magenta", scale=40, width=0.013,
                       headwidth=4, headlength=5, pivot="mid", zorder=8)
        Mtx = ax.annotate("", (_mx, _my), textcoords="offset points", xytext=(10, 12), color="magenta",
                          fontsize=9.5, fontweight="bold", zorder=8,
                          bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="magenta", alpha=0.85))
        ttl = ax.set_title("")
        fig.colorbar(q, label="wind speed (kt)", fraction=0.046, pad=0.02)
        def upd(k):
            q.set_array(S[k].ravel())
            _draw_flow(k)                                                                     # streamlines (or arrows) this frame
            uk, vk = _fmean(k)                                                                # this-frame background wind
            Qm.set_UVC([uk], [vk])
            sp = float(np.hypot(uk, vk)) * 1.94384; fdr = float((270 - np.degrees(np.arctan2(vk, uk))) % 360)
            Mtx.set_text(f"MEAN WIND\n{sp:.1f} kt from {fdr:.0f}°")
            et = recs[k]["t"] - dt.timedelta(hours=4)
            ttl.set_text(f"{a.domain} 10m wind + flow — {et:%H:%M} ET / {recs[k]['t']:%H:%M} Z")
            return q, Qm, Mtx, ttl
        anim = FuncAnimation(fig, upd, frames=len(recs), interval=120, blit=False)
        try:                                                          # mp4 = native play/pause/scrub in <video>
            anim.save(f"{a.outdir}/pressure_movie.mp4", dpi=90, writer="ffmpeg", fps=8)
            print("  wrote pressure_movie.mp4")
        except Exception as e:
            print("  mp4 failed (ffmpeg missing?):", e)
        try:                                                          # gif fallback
            anim.save(f"{a.outdir}/pressure_movie.gif", dpi=70, writer="pillow")
        except Exception as e:
            print("  gif failed:", e)
        plt.close(fig)
        print(f"wrote gustiness.png, point_trace.png, pressure_movie.* -> {a.outdir}  mean_dir={mean_dir:.0f}")
    except Exception as e:
        import traceback; print(f"viz skipped: {e}"); traceback.print_exc()

if __name__ == "__main__":
    main()
