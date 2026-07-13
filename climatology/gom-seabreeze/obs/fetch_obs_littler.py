#!/usr/bin/env python3
"""fetch_obs_littler.py -- pull race-zone surface obs and write little_r for OBSGRID.

Feeds the gom-seabreeze obs-nudging chain (little_r -> obsgrid.exe -> OBS_DOMAIN101 ->
WRF &fdda obs_nudge). Stations are the Mass Bay race-zone set (memory:
project_gom_obs_nudging_stations):

  NDBC buoys : 44013 (Boston 16NM, in-corridor), 44029 (Mass Bay A01, N)
  CO-OPS met : 8444069 Castle Island (inner harbor, W)
  ASOS/AWOS  : BOS Logan (W), BVY Beverly (N-shore), PVC Provincetown (Cape tip = Mark2, E)

Design notes verified against the WRF Obs-Nudging Guide (Reen 2016) + OBSGRID docs:
  * Surface reports -> is_sound=.FALSE.
  * Wind carried as SPEED+DIRECTION and moisture as DEWPOINT (obsgrid ignores u/v/RH).
  * Winds are earth-relative; obsgrid rotates to grid-relative in OBS_DOMAIN (don't pre-rotate).
  * Pressure optional: if absent, obsgrid derives it from station height + first guess.
  * One little_r file; obsgrid makes OBS_DOMAIN101, copied to 201/301 (WRF drops out-of-domain obs).

Hold-out: --exclude 44013 drops the offshore in-corridor buoy so it can be an INDEPENDENT
verification point (nudge d01/d02 only -> check d03@44013 vs the free run). See advisor design.

Usage:
  fetch_obs_littler.py --date 2026-07-04 --run-hours 36 --out /path/obs.littler [--exclude 44013]
"""
import argparse
import csv
import datetime as dt
import io
import json
import sys
import time
import urllib.error
import urllib.request

MISS = -888888.0      # little_r missing value
END = -777777.0       # little_r end-of-data flag
QC_OK = 0
KT2MS = 0.514444
UA = {"User-Agent": "sailframes-gom-seabreeze/1.0 (avillach@gmail.com)"}

# station registry: id -> (lat, lon, elev_m, name, platform, source, fetcher)
STATIONS = {
    "44013":   (42.346,    -70.651,   0.0, "NDBC 44013 Boston 16NM", "FM-13 SHIP", "NDBC", "ndbc"),
    "44029":   (42.523,    -70.566,   0.0, "NDBC 44029 Mass Bay A01", "FM-13 SHIP", "NDBC", "ndbc"),
    "8444069": (42.340862, -71.01234, 0.0, "Castle Island CSIM3",     "FM-12 SYNOP", "COOPS", "coops"),
    "BOS":     (42.3606,   -71.0097,  6.0, "Boston Logan KBOS",       "FM-15 METAR", "ASOS", "asos"),
    "BVY":     (42.5841,   -70.9161, 33.0, "Beverly KBVY",            "FM-15 METAR", "ASOS", "asos"),
    "PVC":     (42.0719,   -70.2214,  3.0, "Provincetown KPVC",       "FM-15 METAR", "ASOS", "asos_ma"),
}


def _get(url, timeout=40, retries=4):
    """GET with backoff on 429/5xx (Iowa Mesonet rate-limits bursty callers)."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(5 * (attempt + 1)); continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1)); continue
            raise


def fetch_ndbc(sid, t0, t1):
    """NDBC realtime2: '#YY MM DD hh mm WDIR WSPD GST ... PRES ATMP WTMP DEWP ...'; MM=missing."""
    txt = _get(f"https://www.ndbc.noaa.gov/data/realtime2/{sid}.txt")
    obs = []
    for line in txt.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        c = line.split()
        if len(c) < 15:
            continue
        try:
            t = dt.datetime(int(c[0]), int(c[1]), int(c[2]), int(c[3]), int(c[4]))
        except ValueError:
            continue
        if not (t0 <= t <= t1):
            continue
        def val(i, conv=float):
            return None if c[i] == "MM" else conv(c[i])
        # cols: 0-4 YY MM DD hh mm | 5 WDIR 6 WSPD 7 GST 8 WVHT 9 DPD 10 APD 11 MWD |
        #       12 PRES 13 ATMP 14 WTMP 15 DEWP 16 VIS ...
        wdir = val(5); wspd = val(6)                    # degT, m/s
        pres = val(12); atmp = val(13); dewp = val(15)  # hPa, degC (air), degC
        obs.append(dict(t=t, wspd=wspd, wdir=wdir,
                        tempK=(atmp + 273.15) if atmp is not None else None,
                        dewK=(dewp + 273.15) if dewp is not None else None,
                        presPa=(pres * 100.0) if pres is not None else None))
    return obs


def fetch_coops(sid, t0, t1):
    """CO-OPS datagetter wind product: metric m/s + degrees. No temp/pres on the wind product."""
    b, e = t0.strftime("%Y%m%d"), t1.strftime("%Y%m%d")
    url = ("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?product=wind"
           f"&station={sid}&begin_date={b}&end_date={e}&time_zone=gmt&units=metric&format=json")
    d = json.loads(_get(url))
    obs = []
    for row in d.get("data", []):
        try:
            t = dt.datetime.strptime(row["t"], "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            continue
        if not (t0 <= t <= t1):
            continue
        try:
            wspd = float(row["s"]); wdir = float(row["d"])
        except (KeyError, ValueError):
            continue
        obs.append(dict(t=t, wspd=wspd, wdir=wdir, tempK=None, dewK=None, presPa=None))
    return obs


def fetch_asos(sid, t0, t1, network=None):
    """Iowa Mesonet ASOS: drct/sknt/tmpf/dwpf (+ latlon). network= for smaller MA fields (PVC)."""
    net = f"&network={network}" if network else ""
    url = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?"
           f"station={sid}{net}&data=drct&data=sknt&data=tmpf&data=dwpf"
           f"&year1={t0.year}&month1={t0.month}&day1={t0.day}"
           f"&year2={t1.year}&month2={t1.month}&day2={t1.day}"
           "&tz=UTC&format=onlycomma&missing=M&latlon=yes")
    obs = []
    for row in csv.DictReader(io.StringIO(_get(url))):
        try:
            t = dt.datetime.strptime(row["valid"], "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            continue
        if not (t0 <= t <= t1):
            continue
        def f(k):
            v = row.get(k, "M")
            return None if v in ("M", "", None) else float(v)
        drct = f("drct"); sknt = f("sknt"); tmpf = f("tmpf"); dwpf = f("dwpf")
        if drct is None and sknt is None:
            continue
        obs.append(dict(t=t,
                        wspd=(sknt * KT2MS) if sknt is not None else None,
                        wdir=drct,
                        tempK=((tmpf - 32) * 5 / 9 + 273.15) if tmpf is not None else None,
                        dewK=((dwpf - 32) * 5 / 9 + 273.15) if dwpf is not None else None,
                        presPa=None))
    return obs


FETCHERS = {
    "ndbc": lambda sid, t0, t1: fetch_ndbc(sid, t0, t1),
    "coops": lambda sid, t0, t1: fetch_coops(sid, t0, t1),
    "asos": lambda sid, t0, t1: fetch_asos(sid, t0, t1),
    "asos_ma": lambda sid, t0, t1: fetch_asos(sid, t0, t1, network="MA_ASOS"),
}


# ---- little_r writers (exact Fortran-equivalent fixed widths) --------------------------
def _pair(v, qc=QC_OK):
    return f"{(v if v is not None else MISS):13.5f}{qc:7d}"


def _header(lat, lon, sid, name, platform, source, elev, date14, nvld, seq, slp):
    p = []
    p.append(f"{lat:20.5f}{lon:20.5f}")                       # 2f20.5
    p.append(f"{sid:<40.40s}{name:<40.40s}")                  # 2a40
    p.append(f"{platform:<40.40s}{source:<40.40s}")          # 2a40
    p.append(f"{elev:20.5f}")                                 # 1f20.5
    p.append(f"{nvld:10d}{0:10d}{0:10d}{seq:10d}{0:10d}")     # 5i10 numvld,err,warn,seq,dups
    p.append(f"{'F':>10s}{'F':>10s}{'F':>10s}")               # 3l10 is_sound,bogus,discard
    p.append(f"{0:10d}{0:10d}")                               # 2i10 sut,julian
    p.append(f"{date14:>20.20s}")                             # a20 date YYYYMMDDHHMMSS
    p.append("".join(_pair(v) for v in [slp] + [MISS] * 12))  # 13(f13.5,i7): slp + 12 miss
    return "".join(p)


def _data(presPa, z, tempK, dewK, wspd, wdir):
    # p, z, T, Td, spd, dir, u, v, rh, thickness  (u/v/rh/thick left missing per guide)
    vals = [presPa, z, tempK, dewK, wspd, wdir, MISS, MISS, MISS, MISS]
    return "".join(_pair(v) for v in vals)


def _end_data():
    vals = [END, END] + [MISS] * 8
    return "".join(_pair(v) for v in vals)


def _tail(nvld):
    return f"{nvld:7d}{0:7d}{0:7d}"


def write_littler(records, path):
    """records: list of dict(sid, meta, t, wspd, wdir, tempK, dewK, presPa). Sorted by time."""
    n = 0
    with open(path, "w") as fh:
        for seq, r in enumerate(sorted(records, key=lambda x: x["t"]), start=1):
            lat, lon, elev, name, platform, source, _ = r["meta"]
            date14 = r["t"].strftime("%Y%m%d%H%M%S")
            nvld = sum(1 for v in (r["presPa"], r["tempK"], r["dewK"], r["wspd"], r["wdir"])
                       if v is not None)
            slp = r["presPa"] if r["presPa"] is not None else MISS
            fh.write(_header(lat, lon, r["sid"], name, platform, source, elev, date14,
                             nvld, seq, slp) + "\n")
            fh.write(_data(r["presPa"], elev, r["tempK"], r["dewK"], r["wspd"], r["wdir"]) + "\n")
            fh.write(_end_data() + "\n")
            fh.write(_tail(nvld) + "\n")
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD (run init at 00Z)")
    ap.add_argument("--run-hours", type=int, default=36)
    ap.add_argument("--pre-hours", type=int, default=2, help="include obs this many h before init (obs_twindo)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--exclude", nargs="*", default=[], help="station ids to hold out (e.g. 44013)")
    a = ap.parse_args()

    d0 = dt.datetime.strptime(a.date, "%Y-%m-%d")
    t0 = d0 - dt.timedelta(hours=a.pre_hours)
    t1 = d0 + dt.timedelta(hours=a.run_hours)

    records, summary = [], []
    for sid, meta in STATIONS.items():
        if sid in a.exclude:
            summary.append(f"  {sid:8s} HELD OUT"); continue
        fetcher = FETCHERS[meta[6]]
        try:
            obs = fetcher(sid, t0, t1)
        except Exception as e:  # noqa: BLE001 - one dead source shouldn't kill the batch
            summary.append(f"  {sid:8s} FETCH ERROR: {e}"); continue
        for o in obs:
            o.update(sid=sid, meta=meta)
        records.extend(obs)
        summary.append(f"  {sid:8s} {len(obs):5d} reports  ({meta[3]})")

    n = write_littler(records, a.out)
    print(f"little_r window {t0:%Y-%m-%d %H}Z .. {t1:%Y-%m-%d %H}Z, exclude={a.exclude or 'none'}")
    print("\n".join(summary))
    print(f"WROTE {n} reports -> {a.out}")
    if n == 0:
        print("FATAL: no obs written", file=sys.stderr); sys.exit(2)


if __name__ == "__main__":
    main()
