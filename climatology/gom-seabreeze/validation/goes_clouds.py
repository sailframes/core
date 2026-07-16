#!/usr/bin/env python3
"""
goes_clouds.py  --  GOES-19 ABI cloud imagery over the race area
================================================================
Show cloud formations (sea-breeze cumulus line / clear marine slot) over Mass Bay
from GOES-19 (GOES-East) ABI, cropped to the race area with the course markers.
Visible (C02, 0.64um) reads the daytime cumulus; clean-IR (C13, 10.3um) reads
cloud tops. This is the "what did the sky look like" companion to the KBOX radar
fine line -- a sea-breeze front usually has a cumulus line on the seaward-air side
and a clear/subsident slot behind it.

Source: noaa-goes19 ABI-L2-MCMIPC (Cloud & Moisture Imagery, CONUS, all 16 bands
at 2 km, 5-min), unsigned S3. Geostationary fixed-grid -> lat/lon via the GOES-R
PUG inverse projection (no goes2go / cartopy dep).

  env -u AWS_PROFILE python goes_clouds.py --date 2026-07-04 --event 13:10 --band C02
"""
import argparse, os, io, datetime as dt
import numpy as np
import boto3, botocore
import xarray as xr
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

BUCKET = "noaa-goes19"; PRODUCT = "ABI-L2-MCMIPC"
BBOX = dict(lo0=-71.7, lo1=-69.6, la0=41.4, la1=43.5)     # regional cloud context
MARKERS = {"YZ": (42.46, -70.77, "#d4c800"), "NW": (42.50, -70.84, "#22d3ee")}
UTC_OFFSET = -4


def s3c():
    return boto3.client("s3", config=botocore.config.Config(
        signature_version=botocore.UNSIGNED, region_name="us-east-1"))


def list_scenes(s3, date, hours):
    ymd = dt.datetime.strptime(date, "%Y-%m-%d"); doy = ymd.timetuple().tm_yday
    out = []
    for h in hours:
        pfx = f"{PRODUCT}/{ymd:%Y}/{doy:03d}/{h:02d}/"
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=pfx):
            for o in page.get("Contents", []):
                k = o["Key"]
                if not k.endswith(".nc"):
                    continue
                # ..._sYYYYJJJHHMMSSs_...  scan start
                s = os.path.basename(k).split("_s")[1]
                usec = int(s[7:9]) * 3600 + int(s[9:11]) * 60 + int(s[11:13])
                out.append((usec, k))
    return sorted(out)


def latlon_grid(ds):
    """GOES fixed-grid scan angles -> lat/lon (GOES-R PUG L2 vol3)."""
    p = ds["goes_imager_projection"]
    lon0 = np.radians(float(p.longitude_of_projection_origin))
    H = float(p.perspective_point_height) + float(p.semi_major_axis)
    r_eq = float(p.semi_major_axis); r_pol = float(p.semi_minor_axis)
    x = ds["x"].values * float(ds["x"].scale_factor) + float(ds["x"].add_offset) \
        if "scale_factor" in ds["x"].attrs else ds["x"].values
    y = ds["y"].values * float(ds["y"].scale_factor) + float(ds["y"].add_offset) \
        if "scale_factor" in ds["y"].attrs else ds["y"].values
    X, Y = np.meshgrid(x, y)
    a = np.sin(X)**2 + np.cos(X)**2 * (np.cos(Y)**2 + (r_eq**2 / r_pol**2) * np.sin(Y)**2)
    b = -2 * H * np.cos(X) * np.cos(Y)
    c = H**2 - r_eq**2
    with np.errstate(invalid="ignore"):
        rs = (-b - np.sqrt(b**2 - 4 * a * c)) / (2 * a)
        sx = rs * np.cos(X) * np.cos(Y); sy = -rs * np.sin(X); sz = rs * np.cos(X) * np.sin(Y)
        lat = np.degrees(np.arctan((r_eq**2 / r_pol**2) * sz / np.sqrt((H - sx)**2 + sy**2)))
        lon = np.degrees(lon0 - np.arctan(sy / (H - sx)))
    return lat, lon


def draw(lat, lon, data, band, when_utc, path, event=False):
    if band == "C02":
        cmap, vmin, vmax, lab = "Greys_r", 0.0, 1.0, "C02 0.64um reflectance (cumulus bright)"
        data = np.sqrt(np.clip(data, 0, 1))                # gamma stretch
    else:
        cmap, vmin, vmax, lab = "Greys", 190, 300, "C13 10.3um brightness temp (K, cold=high cloud)"
    fig, ax = plt.subplots(figsize=(7.6, 7.4))
    pm = ax.pcolormesh(lon, lat, np.ma.masked_invalid(data), cmap=cmap,
                       vmin=vmin, vmax=vmax, shading="auto")
    plt.colorbar(pm, ax=ax, fraction=0.046, pad=0.02).set_label(lab)
    for nm, (la, lo, c) in MARKERS.items():
        ax.plot(lo, la, "o", mfc="none", mec=c, mew=2.4, ms=15)
        ax.text(lo + .03, la, nm, color=c, fontsize=12, fontweight="bold")
    ax.set_xlim(BBOX["lo0"], BBOX["lo1"]); ax.set_ylim(BBOX["la0"], BBOX["la1"])
    ax.set_aspect(1 / np.cos(np.radians(42.5)))
    ax.set_xlabel("lon"); ax.set_ylabel("lat")
    lt = when_utc + dt.timedelta(hours=UTC_OFFSET)
    t = f"GOES-19 {band}  {lt:%H:%M} ET  ({when_utc:%H:%M}Z)"
    ax.set_title(("EVENT MOMENT  " if event else "") + t +
                 "\nsea-breeze: cumulus line on the seaward-air side, clear slot behind", fontsize=10)
    fig.savefig(path, dpi=110, bbox_inches="tight"); plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True)
    ap.add_argument("--band", default="C02", choices=["C02", "C13"])
    ap.add_argument("--event", default="13:10", help="event ET HH:MM")
    ap.add_argument("--start", type=int, default=13, help="LT start hour")
    ap.add_argument("--end", type=int, default=18, help="LT end hour")
    ap.add_argument("--stride", type=int, default=3)
    ap.add_argument("--outdir", default=None)
    args = ap.parse_args()
    outdir = args.outdir or f"/tmp/goes_{args.date}"
    os.makedirs(outdir, exist_ok=True)
    frames_dir = os.path.join(outdir, "frames"); os.makedirs(frames_dir, exist_ok=True)

    utc_hours = list(range(args.start - UTC_OFFSET, args.end - UTC_OFFSET + 1))
    s3 = s3c()
    scenes = list_scenes(s3, args.date, utc_hours)
    if not scenes:
        print("no scenes"); return
    eh, em = map(int, args.event.split(":"))
    ev = (eh - UTC_OFFSET) * 3600 + em * 60
    ymd = dt.datetime.strptime(args.date, "%Y-%m-%d")
    print(f"{len(scenes)} scenes; band {args.band}")

    LAT = LON = idx = None
    frames = []
    for i, (usec, key) in enumerate(scenes):
        is_event = abs(usec - ev) < 300
        if i % args.stride and not is_event:
            continue
        buf = io.BytesIO(); s3.download_fileobj(BUCKET, key, buf); buf.seek(0)
        ds = xr.open_dataset(buf, engine="h5netcdf")
        if LAT is None:
            LAT, LON = latlon_grid(ds)
            inb = (LAT >= BBOX["la0"]) & (LAT <= BBOX["la1"]) & \
                  (LON >= BBOX["lo0"]) & (LON <= BBOX["lo1"])
            rows = np.where(inb.any(1))[0]; cols = np.where(inb.any(0))[0]
            idx = (slice(rows.min(), rows.max() + 1), slice(cols.min(), cols.max() + 1))
            LAT = LAT[idx]; LON = LON[idx]
        cmi = ds[f"CMI_{args.band}"].values[idx]
        when = ymd + dt.timedelta(seconds=usec)
        fp = os.path.join(frames_dir, f"f{i:03d}.png")
        draw(LAT, LON, cmi, args.band, when, fp); frames.append(fp)
        if is_event:
            draw(LAT, LON, cmi, args.band, when,
                 os.path.join(outdir, f"event_moment_{args.band}.png"), event=True)
            print("  * event scene:", os.path.basename(key))
        ds.close()
    print(f"rendered {len(frames)} frames")
    try:
        from PIL import Image
        imgs = [Image.open(f) for f in frames]
        if imgs:
            gif = os.path.join(outdir, f"clouds_{args.band}.gif")
            imgs[0].save(gif, save_all=True, append_images=imgs[1:], duration=350, loop=0)
            print("saved", gif)
    except Exception as ex:
        print("gif failed:", ex)


if __name__ == "__main__":
    main()
