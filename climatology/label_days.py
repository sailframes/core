#!/usr/bin/env python3
"""
label_days.py — sea-breeze day classifier (spec §6) -> labels.parquet.

FIRST-CUT thresholds. §6 says validate against 60 manually-labelled days and
tune the confusion matrix; that validation set does not exist yet, so treat
the F/R/P/G/N/U output as mechanism-correct but not threshold-final.

Per day (season Apr 15 - Oct 15), from the local backfill Parquets:
  - morning gradient: vector-mean 06-10 LT wind at NDBC 44013 (primary truth)
    + HRRR over-water bbox mean (fields).
  - ΔT = KBED Tmax(10-17 LT) - SST 44013(10-14 LT mean).
  - insolation: HRRR over-water mean DSWRF + TCDC 09-14 LT.
  - afternoon trajectory: 44013 dir @ 12/14/16/18 LT; onset = first sustained
    onshore fill; peak speed.
  - tide_phase_hw_h: hours from nearest Boston (8443970) high water to onset.
  - type P needs a coastal ASOS (KBOS) flipping while 44013 doesn't.

Directions are meteorological "from" degrees. Onshore sector = 060-170 T.

Run:  python3 climatology/label_days.py --start 20250701 --end 20250703 \
        --local climatology/_local --grid climatology/grid.json --out climatology/_local/labels.parquet
"""
import argparse
import datetime as dt
import json
import math
import os
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import duckdb

TZ = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
ONSHORE = (60.0, 170.0)          # mid-bay onshore sector (tunable per venue)
OFFSHORE_LO, OFFSHORE_HI = 190.0, 50.0   # offshore wraps through 0
KT = 1.943844                    # m/s -> kt

TYPES_SCHEMA = pa.schema([
    ("date", pa.string()), ("type", pa.string()),
    ("onset_lt_44013", pa.float32()), ("dir_onset", pa.float32()),
    ("dir_12lt", pa.float32()), ("dir_14lt", pa.float32()),
    ("dir_16lt", pa.float32()), ("dir_18lt", pa.float32()),
    ("spd_peak_kt", pa.float32()), ("dt_c", pa.float32()),
    ("grad_u", pa.float32()), ("grad_v", pa.float32()),
    ("grad_spd_kt", pa.float32()), ("grad_dir", pa.float32()),
    ("tcdc_am", pa.float32()), ("dswrf_am", pa.float32()),
    ("sst", pa.float32()), ("tide_phase_hw_h", pa.float32()),
    ("qc_flags", pa.int16()),
])


def _in_onshore(d):
    return d is not None and ONSHORE[0] <= d <= ONSHORE[1]


def _is_offshore(d):
    return d is not None and (d >= OFFSHORE_LO or d <= OFFSHORE_HI)


def _vec_mean(dirs, spds):
    """meteorological vector mean -> (dir_from, speed)."""
    u = v = 0.0
    n = 0
    for d, s in zip(dirs, spds):
        if d is None or s is None:
            continue
        # wind FROM d: components pointing to
        u += -s * math.sin(math.radians(d))
        v += -s * math.cos(math.radians(d))
        n += 1
    if n == 0:
        return None, None
    u /= n; v /= n
    spd = math.hypot(u, v)
    frm = (math.degrees(math.atan2(-u, -v))) % 360
    return frm, spd


def _lt_bounds(date, h0, h1):
    """UTC datetimes for local-time window [h0,h1) on `date` (YYYYMMDD)."""
    d = dt.date(int(date[:4]), int(date[4:6]), int(date[6:8]))
    a = dt.datetime(d.year, d.month, d.day, h0, tzinfo=TZ).astimezone(UTC)
    b = dt.datetime(d.year, d.month, d.day, h1, tzinfo=TZ).astimezone(UTC) if h1 < 24 \
        else dt.datetime(d.year, d.month, d.day, 23, 59, tzinfo=TZ).astimezone(UTC)
    return a.replace(tzinfo=None), b.replace(tzinfo=None)


def _buoy_series(con, obs_path, date):
    """Return list of (lt_hour_float, wdir, wspd_ms) for the local day."""
    d = dt.date(int(date[:4]), int(date[4:6]), int(date[6:8]))
    a = dt.datetime(d.year, d.month, d.day, 0, tzinfo=TZ).astimezone(UTC).replace(tzinfo=None)
    b = dt.datetime(d.year, d.month, d.day, 23, 59, tzinfo=TZ).astimezone(UTC).replace(tzinfo=None)
    rows = con.execute(
        f"SELECT time_utc, wdir, wspd FROM read_parquet('{obs_path}') "
        f"WHERE time_utc BETWEEN '{a}' AND '{b}' AND wdir IS NOT NULL ORDER BY time_utc"
    ).fetchall()
    out = []
    for t, wd, ws in rows:
        tu = t.replace(tzinfo=UTC) if t.tzinfo is None else t
        lt = tu.astimezone(TZ)
        out.append((lt.hour + lt.minute / 60.0, float(wd), None if ws is None else float(ws)))
    return out


def _nearest_at(series, lt_hour):
    best = None
    for h, wd, ws in series:
        if best is None or abs(h - lt_hour) < abs(best[0] - lt_hour):
            best = (h, wd, ws)
    return best


def classify(date, local, grid, con):
    obs = os.path.join(local, "obs", "44013", f"{date[:4]}.parquet")
    bed = os.path.join(local, "asos", "BED", f"{date[:4]}.parquet")
    fields = os.path.join(local, f"year={date[:4]}", f"month={date[4:6]}", f"{date[6:8]}.parquet")
    if not (os.path.exists(obs) and os.path.exists(fields)):
        return dict(date=date, type="U", qc_flags=1)

    series = _buoy_series(con, obs, date)
    qc = 0

    # --- morning gradient (06-10 LT) at 44013 ---
    morn = [(wd, ws) for h, wd, ws in series if 6 <= h < 10]
    gdir, gspd = _vec_mean([d for d, _ in morn], [s for _, s in morn])
    gu = None if gspd is None else -gspd * math.sin(math.radians(gdir))
    gv = None if gspd is None else -gspd * math.cos(math.radians(gdir))

    # --- afternoon trajectory + onset ---
    dirs = {h: (_nearest_at(series, h) or (None, None, None))[1] for h in (12, 14, 16, 18)}
    onset_lt, onset_dir = None, None
    # first time >=10 LT with onshore sustained >=5kt for >=2 consecutive samples
    aft = [(h, wd, ws) for h, wd, ws in series if 10 <= h <= 18]
    for i in range(len(aft) - 1):
        h, wd, ws = aft[i]
        h2, wd2, ws2 = aft[i + 1]
        if _in_onshore(wd) and _in_onshore(wd2) and (ws or 0) * KT >= 5 and (ws2 or 0) * KT >= 5:
            onset_lt, onset_dir = h, wd
            break
    spd_peak = max([(ws or 0) for _, _, ws in aft], default=0.0) * KT
    day_mean_kt = (np.nanmean([ws for _, _, ws in series if ws is not None]) * KT
                   if any(ws is not None for _, _, ws in series) else 0.0)

    # --- ΔT ---
    ta, tb = _lt_bounds(date, 10, 17)
    tmax = con.execute(f"SELECT max(tmpc) FROM read_parquet('{bed}') WHERE time_utc BETWEEN '{ta}' AND '{tb}'").fetchone()[0] if os.path.exists(bed) else None
    sa, sb = _lt_bounds(date, 10, 14)
    sst = con.execute(f"SELECT avg(t_water) FROM read_parquet('{obs}') WHERE time_utc BETWEEN '{sa}' AND '{sb}'").fetchone()[0]
    dt_c = (tmax - sst) if (tmax is not None and sst is not None) else None

    # --- insolation (HRRR over-water, 09-14 LT) ---
    water = [i for i, m in enumerate(grid["land_mask"]) if m == 0]
    ia, ib = _lt_bounds(date, 9, 14)
    wlist = ",".join(map(str, water))
    dswrf, tcdc = con.execute(
        f"SELECT avg(dswrf), avg(tcdc) FROM read_parquet('{fields}') "
        f"WHERE valid_time_utc BETWEEN '{ia}' AND '{ib}' AND gi IN ({wlist})"
    ).fetchone()

    # --- tide phase at onset (hrs from nearest Boston HW) ---
    tide_phase = None
    hilo = os.path.join(local, "coops", "8443970", f"hilo_{date[:4]}.parquet")
    if onset_lt is not None and os.path.exists(hilo):
        d = dt.date(int(date[:4]), int(date[4:6]), int(date[6:8]))
        onset_utc = dt.datetime(d.year, d.month, d.day, int(onset_lt),
                                int((onset_lt % 1) * 60), tzinfo=TZ).astimezone(UTC).replace(tzinfo=None)
        hw = con.execute(f"SELECT time_utc FROM read_parquet('{hilo}') WHERE type='H' "
                         f"ORDER BY abs(epoch(time_utc) - epoch(TIMESTAMP '{onset_utc}')) LIMIT 1").fetchone()
        if hw:
            hwt = hw[0].replace(tzinfo=UTC) if hw[0].tzinfo is None else hw[0]
            tide_phase = (onset_utc.replace(tzinfo=UTC) - hwt).total_seconds() / 3600.0

    # --- typing (§6) ---
    if gspd is None:
        typ = "U"; qc = 1
    elif day_mean_kt >= 15 and onset_lt is None:
        typ = "G"
    elif _is_offshore(gdir) and onset_lt is not None and abs(((onset_dir - gdir + 180) % 360) - 180) >= 90:
        typ = "F"                                   # frontal flip
    elif gdir is not None and (gdir >= 180 or gdir <= 240) and gspd * KT <= 12 and onset_lt is not None \
            and 150 <= (dirs.get(16) or -1) <= 200:
        typ = "R"                                   # reinforcement
    elif onset_lt is None:
        typ = "N"
    else:
        typ = "F" if _is_offshore(gdir) else "R"    # fell through but did fill

    return dict(
        date=f"{date[:4]}-{date[4:6]}-{date[6:8]}", type=typ,
        onset_lt_44013=onset_lt, dir_onset=onset_dir,
        dir_12lt=dirs.get(12), dir_14lt=dirs.get(14), dir_16lt=dirs.get(16), dir_18lt=dirs.get(18),
        spd_peak_kt=round(spd_peak, 1), dt_c=None if dt_c is None else round(dt_c, 1),
        grad_u=None if gu is None else round(gu, 2), grad_v=None if gv is None else round(gv, 2),
        grad_spd_kt=None if gspd is None else round(gspd * KT, 1),
        grad_dir=None if gdir is None else round(gdir, 0),
        tcdc_am=None if tcdc is None else round(tcdc, 0),
        dswrf_am=None if dswrf is None else round(dswrf, 0),
        sst=None if sst is None else round(sst, 1),
        tide_phase_hw_h=None if tide_phase is None else round(tide_phase, 2),
        qc_flags=qc,
    )


def daterange(a, b):
    d = dt.datetime.strptime(a, "%Y%m%d").date()
    e = dt.datetime.strptime(b, "%Y%m%d").date()
    while d <= e:
        yield d.strftime("%Y%m%d")
        d += dt.timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--local", default="climatology/_local")
    ap.add_argument("--grid", default="climatology/grid.json")
    ap.add_argument("--out", default="climatology/_local/labels.parquet")
    a = ap.parse_args()
    grid = json.load(open(a.grid))
    con = duckdb.connect(); con.execute("SET TimeZone='UTC';")
    rows = [classify(d, a.local, grid, con) for d in daterange(a.start, a.end)]
    # normalize to schema (fill missing keys)
    keys = [f.name for f in TYPES_SCHEMA]
    cols = {k: [r.get(k) for r in rows] for k in keys}
    tbl = pa.table(cols, schema=TYPES_SCHEMA)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    pq.write_table(tbl, a.out, compression="zstd")
    print(f"wrote {a.out}: {tbl.num_rows} day-labels")
    for r in rows:
        print(f"  {r['date']} type={r['type']:2s} grad={r.get('grad_dir')}@{r.get('grad_spd_kt')}kt "
              f"dT={r.get('dt_c')} onset={r.get('onset_lt_44013')} dswrf={r.get('dswrf_am')} "
              f"tide_hw={r.get('tide_phase_hw_h')}")


if __name__ == "__main__":
    main()
