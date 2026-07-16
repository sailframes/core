#!/usr/bin/env python3
"""
kbox_fine_line.py  --  KBOX sea-breeze front (fine-line) viz over the race area
==============================================================================
Show the sea-breeze front from Boston NEXRAD (KBOX) as a low-level reflectivity
"fine line" moving onshore across the race area, cropped to Salem Sound with the
race-course markers. Reflectivity time-lapse (GIF) + the event-moment PNG.

Why viz-first (not scoring): KBOX is ~64 km from the race area, so the 0.5deg
beam grazes the TOP of a shallow sea-breeze fine line -- the feature is real but
MARGINAL and broken at this range (confirmed on 2026-07-04). A single snapshot is
ambiguous scatter; the moving line across the afternoon is what reads as a front.
Range gaps are filled by binning the lowest sweeps onto a regular lat/lon grid.

Method:
  1. unidata-nexrad-level2 (unsigned S3, allows LIST; noaa-nexrad-level2 does not)
     -> KBOX _V06 volumes in the LT window.
  2. Per volume: lowest N sweeps' gates -> mean reflectivity binned to a regular
     lat/lon grid over the race bbox (fills range gaps, smooths the broken line).
  3. Plot cropped to the race area + YZ/NW course markers + KBOX; timestamp in ET.
  4. Assemble a GIF (front motion) + drop the event-moment PNG.

TODO (follow-up, the validation gold): velocity-convergence confirmation and
model_front_from_wrfout() -> onset(min)+position(km) error vs d03. Deliverable
here is the obs plot; scoring is separate (benchmark_protocol.md).

Deps: arm-pyart, numpy, matplotlib, boto3, pillow.
  env -u AWS_PROFILE python kbox_fine_line.py --date 2026-07-04 --event 17:10
"""
import argparse, os, io, tempfile, datetime as dt
import numpy as np
import boto3, botocore
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import Normalize
import pyart

BUCKET = "unidata-nexrad-level2"     # public LIST-able (noaa-nexrad-level2 denies anon list)
STATIONS = {"KBOX": (41.9558, -71.1369), "KGYX": (43.8913, -70.2565)}
# race-area crop + course markers (from the 2026-07-04 yellow-zone screenshot)
BBOX = dict(lo0=-71.05, lo1=-70.45, la0=42.20, la1=42.75)
MARKERS = {"YZ": (42.46, -70.77, "#d4c800"), "NW": (42.50, -70.84, "#22d3ee")}
UTC_OFFSET = -4                       # EDT in July


def s3c():
    return boto3.client("s3", config=botocore.config.Config(
        signature_version=botocore.UNSIGNED, region_name="us-east-1"))


def list_volumes(s3, station, date, t0_lt, t1_lt):
    """Return [(utc_seconds, key)] for _V06 volumes in the LT window."""
    ymd = date.replace("-", "/")
    pfx = f"{ymd}/{station}/"
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=pfx):
        for o in page.get("Contents", []):
            k = o["Key"]
            if not k.endswith("_V06"):
                continue
            b = os.path.basename(k)                # KBOX20260704_171003_V06
            hh, mm, ss = int(b[13:15]), int(b[15:17]), int(b[17:19])
            usec = hh * 3600 + mm * 60 + ss
            lt = usec + UTC_OFFSET * 3600
            if t0_lt * 3600 <= lt <= t1_lt * 3600:
                out.append((usec, k))
    return sorted(out)


def grid_reflectivity(radar, nsweeps=2, res=0.004):
    """Bin lowest nsweeps' reflectivity gates -> regular lat/lon mean field."""
    los, las, vals = [], [], []
    for sw in range(min(nsweeps, radar.nsweeps)):
        s, e = radar.get_start_end(sw)
        ref = radar.fields["reflectivity"]["data"][s:e + 1]
        lo = radar.gate_longitude["data"][s:e + 1]
        la = radar.gate_latitude["data"][s:e + 1]
        m = (~np.ma.getmaskarray(ref)) & (lo >= BBOX["lo0"]) & (lo <= BBOX["lo1"]) \
            & (la >= BBOX["la0"]) & (la <= BBOX["la1"])
        los.append(np.asarray(lo[m])); las.append(np.asarray(la[m]))
        vals.append(np.asarray(ref[m]))
    lo = np.concatenate(los); la = np.concatenate(las); v = np.concatenate(vals)
    xe = np.arange(BBOX["lo0"], BBOX["lo1"] + res, res)
    ye = np.arange(BBOX["la0"], BBOX["la1"] + res, res)
    if v.size == 0:
        return xe, ye, np.full((ye.size - 1, xe.size - 1), np.nan)
    ssum, _, _ = np.histogram2d(la, lo, bins=[ye, xe], weights=v)
    scnt, _, _ = np.histogram2d(la, lo, bins=[ye, xe])
    with np.errstate(invalid="ignore"):
        fld = np.where(scnt > 0, ssum / scnt, np.nan)
    return xe, ye, fld


def draw(xe, ye, fld, when_utc, station, path, event=False):
    fig, ax = plt.subplots(figsize=(7.2, 7.6))
    pm = ax.pcolormesh(xe, ye, np.ma.masked_invalid(fld), cmap="ChaseSpectral",
                       norm=Normalize(-8, 22), shading="flat")
    cb = plt.colorbar(pm, ax=ax, fraction=0.046, pad=0.02)
    cb.set_label("0.5deg reflectivity dBZ  (clear-air fine-line range)")
    for nm, (la, lo, c) in MARKERS.items():
        ax.plot(lo, la, "o", mfc="none", mec=c, mew=2.4, ms=16)
        ax.text(lo + .012, la, nm, color=c, fontsize=12, fontweight="bold")
    slat, slon = STATIONS[station]
    if BBOX["lo0"] <= slon <= BBOX["lo1"] and BBOX["la0"] <= slat <= BBOX["la1"]:
        ax.plot(slon, slat, "k^", ms=10); ax.text(slon, slat + .01, station, fontsize=9)
    lt = when_utc + dt.timedelta(hours=UTC_OFFSET)
    ax.set_xlim(BBOX["lo0"], BBOX["lo1"]); ax.set_ylim(BBOX["la0"], BBOX["la1"])
    ax.set_aspect(1 / np.cos(np.radians(42.5)))
    ax.set_xlabel("lon"); ax.set_ylabel("lat")
    title = f"{station} low-level reflectivity  {lt:%H:%M} ET  ({when_utc:%H:%M}Z)"
    if event:
        title = "EVENT MOMENT  " + title
    ax.set_title(title + "\nsea-breeze fine line = enhanced clear-air echo drifting onshore",
                 fontsize=10)
    fig.savefig(path, dpi=110, bbox_inches="tight"); plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True)
    ap.add_argument("--station", default="KBOX", choices=list(STATIONS))
    ap.add_argument("--start", type=float, default=13.0, help="LT window start (hours)")
    ap.add_argument("--end", type=float, default=18.0, help="LT window end (hours)")
    ap.add_argument("--event", default="17:10", help="event moment ET HH:MM for the still")
    ap.add_argument("--stride", type=int, default=2, help="use every Nth volume for the GIF")
    ap.add_argument("--sweeps", type=int, default=2)
    ap.add_argument("--outdir", default=None)
    args = ap.parse_args()
    outdir = args.outdir or f"/tmp/radar_{args.date}"
    os.makedirs(outdir, exist_ok=True)
    frames_dir = os.path.join(outdir, "frames"); os.makedirs(frames_dir, exist_ok=True)

    s3 = s3c()
    vols = list_volumes(s3, args.station, args.date, args.start, args.end)
    if not vols:
        print("no volumes in window"); return
    print(f"{len(vols)} volumes in {args.start:.0f}-{args.end:.0f} LT; stride {args.stride}")
    eh, em = map(int, args.event.split(":"))
    ev_usec = (eh - UTC_OFFSET) * 3600 + em * 60          # event LT -> UTC seconds
    ymd = dt.datetime.strptime(args.date, "%Y-%m-%d")

    frames = []
    for i, (usec, key) in enumerate(vols):
        is_event = abs(usec - ev_usec) < 200
        if i % args.stride and not is_event:
            continue
        try:
            buf = io.BytesIO(); s3.download_fileobj(BUCKET, key, buf); buf.seek(0)
            with tempfile.NamedTemporaryFile(suffix="_V06", delete=False) as tf:
                tf.write(buf.read()); tmp = tf.name
            radar = pyart.io.read_nexrad_archive(tmp); os.unlink(tmp)
        except Exception as ex:
            print("  skip", os.path.basename(key), ex); continue
        xe, ye, fld = grid_reflectivity(radar, args.sweeps)
        when = ymd + dt.timedelta(seconds=usec)
        fp = os.path.join(frames_dir, f"f{i:03d}.png")
        draw(xe, ye, fld, when, args.station, fp)
        frames.append(fp)
        if is_event:
            draw(xe, ye, fld, when, args.station,
                 os.path.join(outdir, "event_moment.png"), event=True)
            print("  * event moment:", os.path.basename(key))
    print(f"rendered {len(frames)} frames")

    # GIF via pillow
    try:
        from PIL import Image
        imgs = [Image.open(f) for f in frames]
        if imgs:
            imgs[0].save(os.path.join(outdir, "fine_line.gif"), save_all=True,
                         append_images=imgs[1:], duration=350, loop=0)
            print("saved", os.path.join(outdir, "fine_line.gif"))
    except Exception as ex:
        print("gif failed:", ex)


if __name__ == "__main__":
    main()
