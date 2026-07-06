#!/usr/bin/env python3
"""
backfill_ndbc.py — NDBC standard-meteorological annual archives -> obs Parquet
(spec §4 `obs`): time_utc, wspd, wdir, gust, t_air, t_water, slp.

Source: https://www.ndbc.noaa.gov/data/historical/stdmet/{stn}h{YYYY}.txt.gz
Whitespace-delimited; 2 header rows (#names, #units). Missing sentinels
(99/999/9999/MM) -> null. Winds m/s, temps degC, pressure hPa (as archived).
Output: obs/{stn}/{YYYY}.parquet.

Phase 0 confirmed 44013 present all 2010-2025.

Run:  python3 climatology/backfill_ndbc.py --station 44013 --start-year 2017 --end-year 2025 --out-dir climatology/_local
"""
import argparse
import gzip
import io
import os

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests

URL = "https://www.ndbc.noaa.gov/data/historical/stdmet/{stn}h{yr}.txt.gz"
# NDBC column name -> (our field, missing sentinels)
MAP = {
    "WDIR": ("wdir", (999,)), "WSPD": ("wspd", (99.0,)), "GST": ("gust", (99.0,)),
    "ATMP": ("t_air", (999.0,)), "WTMP": ("t_water", (999.0,)),
    "PRES": ("slp", (9999.0,)), "BAR": ("slp", (9999.0,)),
}
OUT_FIELDS = ["wspd", "wdir", "gust", "t_air", "t_water", "slp"]
SCHEMA = pa.schema([("time_utc", pa.timestamp("s", tz="UTC"))]
                   + [(f, pa.float32()) for f in OUT_FIELDS])


import datetime as dt
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTHLY_URL = "https://www.ndbc.noaa.gov/data/stdmet/{mo}/{stn}.txt"
REALTIME_URL = "https://www.ndbc.noaa.gov/data/realtime2/{stn}.txt"


def parse_block(text, year=None):
    """Parse an NDBC stdmet text block (annual / monthly / realtime — same format).
    Returns {epoch_sec: valsdict}. `year` filters (realtime/monthly may span years)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {}
    header = lines[0].lstrip("#").split()
    idx = {name: k for k, name in enumerate(header)}
    out = {}
    for ln in lines:
        if ln.startswith("#"):
            continue
        row = ln.split()
        if len(row) < len(header):
            continue
        try:
            t = dt.datetime(int(row[0]), int(row[1]), int(row[2]), int(row[3]), int(row[4]),
                            tzinfo=dt.timezone.utc)
        except Exception:
            continue
        if year and t.year != year:
            continue
        vals = {f: None for f in OUT_FIELDS}
        for ncol, (field, miss) in MAP.items():
            if ncol in idx:
                try:
                    v = float(row[idx[ncol]])
                except Exception:
                    continue
                if v in miss or v >= 999:
                    continue
                vals[field] = v
        out[int(t.timestamp())] = vals
    return out


def _table_from(byt):
    if not byt:
        return None
    times = sorted(byt)
    return pa.table(
        {"time_utc": pa.array(np.array([np.datetime64(t, "s") for t in times]), type=pa.timestamp("s", tz="UTC")),
         **{f: pa.array([byt[t][f] for t in times], type=pa.float32()) for f in OUT_FIELDS}},
        schema=SCHEMA)


def fetch_year(stn, yr):
    # 1) annual historical archive (published after year-end)
    r = requests.get(URL.format(stn=stn, yr=yr), timeout=60)
    if r.status_code == 200:
        return _table_from(parse_block(gzip.decompress(r.content).decode("latin-1")))
    # 2) current year: merge available monthly files + the ~45-day realtime feed
    byt = {}
    now_year = dt.datetime.now(dt.timezone.utc).year
    if yr == now_year:
        for mo in MONTHS:
            rr = requests.get(MONTHLY_URL.format(mo=mo, stn=stn), timeout=30)
            if rr.status_code == 200:
                byt.update(parse_block(rr.text, year=yr))
        rt = requests.get(REALTIME_URL.format(stn=stn), timeout=30)
        if rt.status_code == 200:
            byt.update(parse_block(rt.text, year=yr))   # realtime wins on overlap (freshest)
    return _table_from(byt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", default="44013")
    ap.add_argument("--start-year", type=int, default=2017)
    ap.add_argument("--end-year", type=int, default=2025)
    ap.add_argument("--out-dir", default="climatology/_local")
    a = ap.parse_args()
    for yr in range(a.start_year, a.end_year + 1):
        tbl = fetch_year(a.station, yr)
        if tbl is None:
            print(f"{a.station} {yr}: unavailable"); continue
        out = os.path.join(a.out_dir, "obs", a.station, f"{yr}.parquet")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        pq.write_table(tbl, out, compression="zstd")
        kb = os.path.getsize(out) / 1024
        print(f"{a.station} {yr}: {tbl.num_rows} obs, {kb:.0f} KB -> {out}")


if __name__ == "__main__":
    main()
