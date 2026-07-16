#!/usr/bin/env python3
"""obs_gust_check.py -- the reality check the gust dashboard was missing.

Pulls independent surface obs over the race window and reports SUSTAINED + GUST +
gust-factor per station, so the LES gust product can be scored against what actually
blew. Born from Rumble II (2026-07-15): the LES said gust factor 1.30 / peak 12 kt
while Logan (KBOS) and Castle Island (CSIM3) gusted 17-23 kt. Had this run pre-race
and shown next to the model, the miss would have been obvious.

Server-side (run on the render box or anywhere with outbound HTTP) -> writes
obs_check.json for gust.html to overlay. No CORS problem, no browser deps.

  python3 obs_gust_check.py --date 2026-07-15 --hh0 22 --hh1 25 \
      --out /root/gust/obs_check.json --upload-prefix s3://.../2026-07-15/gust

Stations default to the Boston Harbor set; --stations overrides. NDBC buoys/CMAN via
realtime2 (m/s -> kt); airports (K...) via the Iowa State ASOS archive (already kt).
Both are the SAME independent-obs sources the /tactics obs overlay uses.
"""
import argparse, datetime as dt, json, subprocess, urllib.request

MPS_KT = 1.94384
# name + type; NDBC = realtime2 buoy/CMAN, ASOS = airport METAR (Iowa State archive)
STATIONS = {
    "KBOS":  {"name": "Logan (city downwind)", "src": "asos"},
    "CSIM3": {"name": "Castle Island (in harbor)", "src": "ndbc"},
    "44013": {"name": "Boston 16 NM (offshore)", "src": "ndbc"},
}

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "sailframes-gust-check"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")

def _win(date, hh0, hh1):
    d0 = dt.datetime.strptime(date, "%Y-%m-%d")
    return d0 + dt.timedelta(hours=hh0), d0 + dt.timedelta(hours=hh1)   # hh1 may exceed 24 (evening race)

def ndbc(stn, t0, t1):
    """realtime2: cols  #YY MM DD hh mm WDIR WSPD GST ... (WSPD/GST in m/s)."""
    rows = []
    txt = _get(f"https://www.ndbc.noaa.gov/data/realtime2/{stn}.txt")
    for ln in txt.splitlines():
        if ln.startswith("#") or not ln.strip():
            continue
        f = ln.split()
        try:
            ts = dt.datetime(int(f[0]), int(f[1]), int(f[2]), int(f[3]), int(f[4]))
        except (ValueError, IndexError):
            continue
        if not (t0 <= ts <= t1):
            continue
        def num(x):
            return None if x in ("MM", "999", "99.0") else float(x)
        wd, ws, gs = num(f[5]), num(f[6]), num(f[7])
        rows.append((ts, wd, None if ws is None else ws * MPS_KT, None if gs is None else gs * MPS_KT))
    return rows

def asos(stn, t0, t1):
    """Iowa State ASOS archive: station,valid,drct,sknt,gust (already knots)."""
    site = stn[1:] if stn.startswith("K") and len(stn) == 4 else stn
    url = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?"
           f"station={site}&data=drct&data=sknt&data=gust&missing=M&trace=T&"
           f"year1={t0.year}&month1={t0.month}&day1={t0.day}&"
           f"year2={t1.year}&month2={t1.month}&day2={t1.day}&"
           "tz=Etc/UTC&format=onlycomma&latlon=no")
    rows = []
    for ln in _get(url).splitlines()[1:]:
        p = ln.split(",")
        if len(p) < 5:
            continue
        try:
            ts = dt.datetime.strptime(p[1], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if not (t0 <= ts <= t1):
            continue
        def num(x):
            return None if x in ("M", "T", "") else float(x)
        rows.append((ts, num(p[2]), num(p[3]), num(p[4])))
    return rows

def summarize(rows):
    sus = [r[2] for r in rows if r[2] is not None]
    gus = [r[3] for r in rows if r[3] is not None]
    drs = [r[1] for r in rows if r[1] is not None]
    if not sus:
        return None
    import math
    # vector-mean direction (handles 0/360 wrap)
    dmean = None
    if drs:
        sx = sum(math.sin(math.radians(d)) for d in drs); cy = sum(math.cos(math.radians(d)) for d in drs)
        dmean = round(math.degrees(math.atan2(sx, cy)) % 360)
    gmax = max(gus) if gus else max(sus)
    smean = sum(sus) / len(sus)
    return dict(n=len(rows), sustained_mean_kt=round(smean, 1), sustained_max_kt=round(max(sus), 1),
                gust_max_kt=round(gmax, 1), gust_factor=round(gmax / max(smean, 0.1), 2),
                dir_mean_deg=dmean)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--hh0", type=int, default=22); ap.add_argument("--hh1", type=int, default=25)
    ap.add_argument("--out", default="/root/gust/obs_check.json")
    ap.add_argument("--stations", default=None, help="comma list to override defaults")
    ap.add_argument("--upload-prefix", default=None, help="s3 dir to cp obs_check.json to")
    a = ap.parse_args()
    t0, t1 = _win(a.date, a.hh0, a.hh1)
    stns = a.stations.split(",") if a.stations else list(STATIONS)
    out = {"date": a.date, "window_utc": [a.hh0, a.hh1],
           "window_et": [a.hh0 - 4, a.hh1 - 4], "stations": {}}
    for s in stns:
        meta = STATIONS.get(s, {"name": s, "src": "ndbc"})
        try:
            rows = (asos if meta["src"] == "asos" else ndbc)(s, t0, t1)
            summ = summarize(rows)
            if summ:
                summ["name"] = meta["name"]; out["stations"][s] = summ
                print(f"  {s:6s} {meta['name']:26s} sust {summ['sustained_mean_kt']:.1f} kt  "
                      f"gust {summ['gust_max_kt']:.1f} kt  GF {summ['gust_factor']}  dir {summ['dir_mean_deg']}")
            else:
                print(f"  {s:6s} no data in window")
        except Exception as e:
            print(f"  {s:6s} FAILED: {e}")
    # headline: the strongest observed gust + gust-factor across stations = the truth to beat
    gs = [v for v in out["stations"].values()]
    if gs:
        out["obs_gust_max_kt"] = round(max(v["gust_max_kt"] for v in gs), 1)
        out["obs_gust_factor_max"] = round(max(v["gust_factor"] for v in gs), 2)
    json.dump(out, open(a.out, "w"))
    print("wrote", a.out, "->", json.dumps(out.get("stations", {}), default=str)[:200])
    if a.upload_prefix:
        subprocess.run(f"aws s3 cp --quiet {a.out} {a.upload_prefix}/obs_check.json", shell=True, check=False)

if __name__ == "__main__":
    main()
