#!/usr/bin/env python3
"""fetch_madis_acars.py -- pull MADIS aircraft VERTICAL PROFILES near KBOS and write
little_r soundings for OBSGRID, filling the upper-air gap in the gom-seabreeze
obs-nudging chain.

Companion to fetch_obs_littler.py (surface obs). That writer emits single-level
SURFACE reports (is_sound=.FALSE.). This writer emits MULTI-LEVEL PROFILE reports
(is_sound=.TRUE.) from aircraft ascents/descents at Boston Logan, which obsgrid
QCs like radiosondes. It REUSES fetch_obs_littler.py's _pair/_data/_end_data/_tail
helpers (same exact fixed widths) and provides its own is_sound=.TRUE. header.

DATASET (verified 2026-07-13 by ncdump of a real archive file, see NOTES below):
  MADIS archive, HTTPS, hourly gz'd netCDF:
    https://madis-data.ncep.noaa.gov/madisPublic1/data/archive/
      YYYY/MM/DD/point/acarsProfiles/netcdf/YYYYMMDD_HHMM.gz
  acarsProfiles = airport ASCENT/DESCENT soundings (what we want for KBOS upper air).
  acars        = individual en-route (cruise) reports -> single-level, needs FM-97
                 AIREP + gets QC-rejected off analysis pressure levels. NOT used here
                 (optional, secondary; see --enroute stub).

acarsProfiles netCDF schema (CONFIRMED via ncdump -h on 20210520_1800):
  dims : recNum(UNLIMITED)=one per profile, maxLevels=200
  per-profile (recNum):
    profileTime   double  seconds since 1970-1-1  (land/takeoff time)
    profileAirport char(recNum,6)                  (e.g. "KBOS")
    latitude/longitude float                        (AIRPORT location, deg N / deg E)
    nLevels       int                               (VALID level count for this profile)
    profileType   int      -1=descending, 1=ascending
    elevation     float    meter (airport elevation)
  per-level (recNum,maxLevels):
    altitude   float  "meter (pressure altitude, msl)"   <- HEIGHT for little_r
    temperature float "Kelvin"
    dewpoint   float  "Kelvin"
    windSpeed  float  "meter/sec"
    windDir    float  "degree_true"                       (earth-relative; obsgrid rotates)
    trackLat/trackLon float                               (per-level a/c position)
    <var>DD    char   QC summary: Z,C,S,V (good) / X,Q,B (bad); see DD_value_* globals
  _FillValue = 99999.f on the float physical vars.

  !! NO per-level PRESSURE variable in acarsProfiles. Only pressure-ALTITUDE (meter).
     -> little_r carries HEIGHT in the z slot, pressure=MISS. obsgrid derives pressure
        from height + first guess (same path the surface writer already relies on for
        stations that report no pressure). This is correct and intended.

UNITS: temperature/dewpoint already KELVIN, windSpeed already M/S, altitude already
METERS. NO conversion (unlike the surface fetchers which convert degC/degF/kt). Do NOT
copy their +273.15 / KT2MS / *100.

Usage (mirrors fetch_obs_littler.py; typically run right AFTER it, appending):
  fetch_madis_acars.py --date 2026-07-04 --run-hours 36 \
      --out /path/wpsprd/obs --split-interval 10800 --append \
      [--airport KBOS] [--bbox 41.8 42.8 -71.3 -70.0]
"""
import argparse
import datetime as dt
import gzip
import io
import os
import sys
import time
import urllib.error
import urllib.request

# Reuse the EXACT little_r column writers from the surface module (same fixed widths).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_obs_littler import (  # noqa: E402
    MISS, END, QC_OK, _pair, _data, _end_data, _tail,
)

ARCHIVE = "https://madis-data.ncep.noaa.gov/madisPublic1/data/archive"
UA = {"User-Agent": "sailframes-gom-seabreeze/1.0 (avillach@gmail.com)"}

# MADIS acarsProfiles netCDF variable names -- CONFIRMED against ncdump of a real file,
# but keep them here as an editable map so a schema drift is a one-line fix, not a hunt.
V = dict(
    time="profileTime",      # double, sec since 1970 (per profile)
    airport="profileAirport",  # char(recNum,6)
    lat="latitude",          # float (airport loc)
    lon="longitude",         # float (airport loc)
    nlev="nLevels",          # int (valid levels)
    ptype="profileType",     # int -1 desc / 1 asc
    elev="elevation",        # float m
    z="altitude",            # float m (pressure altitude msl) -> little_r HEIGHT
    t="temperature",         # float K
    td="dewpoint",           # float K
    ws="windSpeed",          # float m/s
    wd="windDir",            # float deg true
    tlat="trackLat",         # float per-level lat
    tlon="trackLon",         # float per-level lon
    z_dd="altitudeDD", t_dd="temperatureDD", td_dd="dewpointDD",
    ws_dd="windSpeedDD", wd_dd="windDirDD",
)
FILL = 99999.0               # netCDF _FillValue on the float physical vars
FILL_LIM = 90000.0           # treat >= this as missing (guards fill + junk)
GOOD_DD = set("ZCSVGI")      # DD summary values we accept (Z=no QC, C/S/V=passed stages,
                             # I=interpolated, G=accept-list). Reject X/Q/B.


def _get_bytes(url, timeout=60, retries=4):
    """GET raw bytes with backoff. Returns None on 404 (that hour simply absent)."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(5 * (attempt + 1)); continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1)); continue
            raise
    return None


def _open_hour(t):
    """Fetch + gunzip one hourly acarsProfiles file -> netCDF4.Dataset (in-memory), or None."""
    from netCDF4 import Dataset  # imported lazily: only the aircraft path needs it
    url = (f"{ARCHIVE}/{t:%Y/%m/%d}/point/acarsProfiles/netcdf/"
           f"{t:%Y%m%d_%H}00.gz")
    raw = _get_bytes(url)
    if raw is None:
        return None, url
    nc = gzip.decompress(raw)
    # netCDF4 can open an in-memory bytes buffer via memory= (needs libnetcdf >= 4.4)
    return Dataset("inmem", mode="r", memory=nc), url


def _cstr(char_row):
    """Join a netCDF char array row -> stripped python str."""
    try:
        return b"".join(bytes([c]) if isinstance(c, int) else bytes(c)
                        for c in char_row.tobytes()).decode("ascii", "ignore").strip()
    except Exception:
        # netCDF4 usually returns a masked char array; fall back to chartostring
        from netCDF4 import chartostring
        return str(chartostring(char_row)).strip()


def _val(x):
    """netCDF float -> python float or None (fill/masked/out-of-range -> None)."""
    try:
        import numpy as np
        if x is np.ma.masked or (hasattr(x, "mask") and x.mask):
            return None
        f = float(x)
    except Exception:
        return None
    if f is None or f >= FILL_LIM or f <= -FILL_LIM:
        return None
    return f


def fetch_acars_profiles(t0, t1, airport=None, bbox=None):
    """Pull acarsProfiles for every hour in [t0,t1], keep profiles matching airport or bbox.

    Returns list of profile dicts:
      dict(t=datetime, lat, lon, elev, airport, ptype, name,
           levels=[dict(z,tempK,dewK,wspd,wdir), ...  bottom->top])
    """
    import numpy as np  # noqa: F401  (used indirectly by _val / netCDF4 masking)
    profiles = []
    hour = t0.replace(minute=0, second=0, microsecond=0)
    while hour <= t1:
        try:
            ds, url = _open_hour(hour)
        except Exception as e:  # noqa: BLE001 - one bad hour shouldn't kill the batch
            print(f"  {hour:%Y-%m-%d_%H}Z acarsProfiles ERROR: {e}", file=sys.stderr)
            ds = None
        if ds is None:
            hour += dt.timedelta(hours=1); continue
        try:
            n = ds.dimensions["recNum"].size
            lat = ds.variables[V["lat"]][:]
            lon = ds.variables[V["lon"]][:]
            nlev = ds.variables[V["nlev"]][:]
            ptime = ds.variables[V["time"]][:]
            ptype = ds.variables[V["ptype"]][:]
            elev = ds.variables[V["elev"]][:]
            apt = ds.variables[V["airport"]][:]
            zL = ds.variables[V["z"]][:]     # (recNum, maxLevels)
            tL = ds.variables[V["t"]][:]
            tdL = ds.variables[V["td"]][:]
            wsL = ds.variables[V["ws"]][:]
            wdL = ds.variables[V["wd"]][:]
            # QC summary chars (optional -- absent-tolerant)
            def ddvar(k):
                return ds.variables[V[k]][:] if V[k] in ds.variables else None
            zDD, tDD, tdDD, wsDD, wdDD = (ddvar("z_dd"), ddvar("t_dd"),
                                          ddvar("td_dd"), ddvar("ws_dd"), ddvar("wd_dd"))
            for i in range(n):
                pa = _cstr(apt[i]) if apt.ndim == 2 else ""
                la, lo = _val(lat[i]), _val(lon[i])
                if la is None or lo is None:
                    continue
                keep = False
                if airport and pa.upper().lstrip("K") == airport.upper().lstrip("K"):
                    keep = True
                if bbox and (bbox[0] <= la <= bbox[1] and bbox[2] <= lo <= bbox[3]):
                    keep = True
                if not keep:
                    continue
                nk = int(nlev[i]) if _val(nlev[i]) is not None else zL.shape[1]
                nk = min(nk, zL.shape[1])
                levels = []
                for k in range(nk):
                    def dd_ok(ddarr):
                        if ddarr is None:
                            return True
                        c = _cstr(ddarr[i, k:k + 1]) if ddarr.ndim == 2 else ""
                        return (not c) or (c in GOOD_DD)
                    z = _val(zL[i, k])
                    T = _val(tL[i, k]) if dd_ok(tDD) else None
                    Td = _val(tdL[i, k]) if dd_ok(tdDD) else None
                    ws = _val(wsL[i, k]) if dd_ok(wsDD) else None
                    wd = _val(wdL[i, k]) if dd_ok(wdDD) else None
                    if not (dd_ok(zDD)):
                        z = None
                    # a level needs a height AND at least one measured quantity
                    if z is None or all(v is None for v in (T, Td, ws, wd)):
                        continue
                    levels.append(dict(z=z, tempK=T, dewK=Td, wspd=ws, wdir=wd))
                if len(levels) < 2:          # a "profile" needs >= 2 levels to be a sounding
                    continue
                levels.sort(key=lambda L: L["z"])   # bottom -> top
                tt = dt.datetime.utcfromtimestamp(float(ptime[i]))
                if not (t0 <= tt <= t1):
                    continue
                pt = int(ptype[i]) if _val(ptype[i]) is not None else 0
                profiles.append(dict(
                    t=tt, lat=la, lon=lo, elev=(_val(elev[i]) or 0.0),
                    airport=pa or (airport or "ACARSP"),
                    ptype=("descent" if pt < 0 else "ascent"),
                    name=f"ACARSP {pa or airport} {'desc' if pt<0 else 'asc'} {tt:%H%MZ}",
                    levels=levels))
        finally:
            ds.close()
        hour += dt.timedelta(hours=1)
    return profiles


# ---- little_r PROFILE (sounding) writer -------------------------------------------------
# Differs from fetch_obs_littler.py's surface writer ONLY in the header: is_sound=.TRUE.
# and we emit N data lines (one per level) before the single _end_data. Column layout of
# every line is byte-identical to the surface writer (we reuse _data/_end_data/_tail/_pair).

def _header_sound(lat, lon, sid, name, platform, source, elev, date14, nvld, seq):
    """little_r header with is_sound=.TRUE. -- profile counterpart of _header().

    Same 2f20.5 / 2a40 / 2a40 / f20.5 / 5i10 / 3l10 / 2i10 / a20 / 13(f13.5,i7) layout as
    the surface writer, with the ONLY change being the 3l10 logical block: is_sound = T.
    numvld = total valid (level x field) values. slp/press slots stay MISS (aircraft carry
    no SLP; per-level pressure is derived by obsgrid from height).
    """
    p = []
    p.append(f"{lat:20.5f}{lon:20.5f}")                       # 2f20.5 airport lat/lon
    p.append(f"{sid:<40.40s}{name:<40.40s}")                  # 2a40 id + name
    p.append(f"{platform:<40.40s}{source:<40.40s}")          # 2a40 FM-97 AIREP + source
    p.append(f"{elev:20.5f}")                                 # f20.5 airport elevation
    p.append(f"{nvld:10d}{0:10d}{0:10d}{seq:10d}{0:10d}")     # 5i10 numvld,err,warn,seq,dups
    p.append(f"{'T':>10s}{'F':>10s}{'F':>10s}")               # 3l10 is_sound=T, bogus, discard
    p.append(f"{0:10d}{0:10d}")                               # 2i10 sut, julian
    p.append(f"{date14:>20.20s}")                             # a20 YYYYMMDDHHMMSS
    p.append("".join(_pair(v) for v in [MISS] * 13))          # 13(f13.5,i7): slp + 12 miss
    return "".join(p)


def write_littler_profiles(profiles, path, mode="w", seq_start=1):
    """Write profile reports as little_r soundings. Returns (n_reports, next_seq).

    mode="a" + seq_start let this APPEND after the surface writer into the same
    obs:<date> file with a continuous seq_num (see integration note in run_case.sh).
    """
    n, seq = 0, seq_start
    with open(path, mode) as fh:
        for pr in sorted(profiles, key=lambda x: x["t"]):
            date14 = pr["t"].strftime("%Y%m%d%H%M%S")
            nvld = sum(1 for L in pr["levels"] for v in
                       (L["tempK"], L["dewK"], L["wspd"], L["wdir"]) if v is not None)
            fh.write(_header_sound(pr["lat"], pr["lon"], pr["airport"], pr["name"],
                                   "FM-97 AIREP", "MADIS ACARSP", pr["elev"],
                                   date14, nvld, seq) + "\n")
            for L in pr["levels"]:
                # data cols: p, z, T, Td, spd, dir, u, v, rh, thickness. pressure=MISS
                # (no per-level P in acarsProfiles) -> obsgrid derives it from z.
                fh.write(_data(MISS, L["z"], L["tempK"], L["dewK"],
                               L["wspd"], L["wdir"]) + "\n")
            fh.write(_end_data() + "\n")
            fh.write(_tail(nvld) + "\n")
            n += 1; seq += 1
    return n, seq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD (run init at 00Z)")
    ap.add_argument("--run-hours", type=int, default=36)
    ap.add_argument("--pre-hours", type=int, default=2)
    ap.add_argument("--out", required=True,
                    help="output path; with --split-interval this is the PREFIX (obs:<date>)")
    ap.add_argument("--split-interval", type=int, default=0,
                    help="seconds; bin each profile to nearest analysis period -> <out>:YYYY-MM-DD_HH")
    ap.add_argument("--airport", default="KBOS", help="airport code filter (matches profileAirport)")
    ap.add_argument("--bbox", nargs=4, type=float, default=[41.8, 42.8, -71.3, -70.0],
                    metavar=("LATMIN", "LATMAX", "LONMIN", "LONMAX"),
                    help="also keep any profile whose airport-loc falls in this box")
    ap.add_argument("--append", action="store_true",
                    help="APPEND to existing obs:<date> files (after the surface writer) "
                         "instead of overwriting; keeps a continuous seq_num within each file")
    a = ap.parse_args()

    d0 = dt.datetime.strptime(a.date, "%Y-%m-%d")
    t0 = d0 - dt.timedelta(hours=a.pre_hours)
    t1 = d0 + dt.timedelta(hours=a.run_hours)

    profiles = fetch_acars_profiles(t0, t1, airport=a.airport, bbox=tuple(a.bbox))
    print(f"acarsProfiles window {t0:%Y-%m-%d %H}Z .. {t1:%Y-%m-%d %H}Z "
          f"airport={a.airport} bbox={a.bbox}")
    print(f"  {len(profiles)} profiles kept "
          f"({sum(len(p['levels']) for p in profiles)} total levels)")
    for p in profiles[:20]:
        print(f"    {p['t']:%Y-%m-%d %H:%MZ} {p['airport']:5s} {p['ptype']:7s} "
              f"{len(p['levels']):3d} lvls  z {p['levels'][0]['z']:.0f}->{p['levels'][-1]['z']:.0f} m")
    if not profiles:
        print("no aircraft profiles in window/area (aircraft obs are OPTIONAL upper-air "
              "augmentation -- surface nudging still runs). Exit 0.")
        return

    if a.split_interval:
        step = dt.timedelta(seconds=a.split_interval)
        nper = int(a.run_hours * 3600 / a.split_interval)
        periods = [d0 + i * step for i in range(nper + 1)]
        bins = {}
        for p in profiles:
            per = min(periods, key=lambda pp: abs((p["t"] - pp).total_seconds()))
            bins.setdefault(per, []).append(p)
        total = 0
        for per, prs in sorted(bins.items()):
            path = f"{a.out}:{per:%Y-%m-%d_%H}"
            # when appending, continue seq_num after however many surface reports exist
            mode = "a" if (a.append and os.path.exists(path)) else "w"
            seq0 = _count_reports(path) + 1 if mode == "a" else 1
            m, _ = write_littler_profiles(prs, path, mode=mode, seq_start=seq0)
            total += m
        print(f"WROTE {total} profile reports across {len(bins)} period files "
              f"-> {a.out}:<date> (mode={'append' if a.append else 'write'})")
    else:
        mode = "a" if (a.append and os.path.exists(a.out)) else "w"
        seq0 = _count_reports(a.out) + 1 if mode == "a" else 1
        n, _ = write_littler_profiles(profiles, a.out, mode=mode, seq_start=seq0)
        print(f"WROTE {n} profile reports -> {a.out} (mode={'append' if a.append else 'write'})")


def _count_reports(path):
    """Count existing little_r reports in a file (tail lines) so append seq_num continues.
    A report's tail line is the 3(i7) line; the end-data line just before it has the -777777
    sentinel. Simplest robust count: number of END sentinels in the data stream."""
    try:
        with open(path) as fh:
            return sum(1 for ln in fh if ln.lstrip().startswith("-777777"))
    except OSError:
        return 0


if __name__ == "__main__":
    main()
