#!/usr/bin/env python3
"""
backfill_asos.py — IEM ASOS archive -> Parquet (spec §4 obs-family).

Roles (spec §3/§6):
  KBED (inland) -> Tmax for the ΔT sea-breeze term.
  KBOS / KBVY / KPVC (coastal) -> onshore-flip detection (type P: inshore fills,
  44013 doesn't). KLWM alternate inland.

Source: IEM `asos.py` (station uses the 3-4 char id, no K prefix: KBED->BED).
Vars: tmpc (air C), dwpc, drct (wind dir), sknt (wind kt), mslp. Missing 'M'->null.
Output: asos/{ID}/{YYYY}.parquet  (time_utc, tmpc, dwpc, drct, sknt, mslp).

Run:  python3 climatology/backfill_asos.py --station BED --start-year 2017 --end-year 2025 --out-dir climatology/_local
"""
import argparse
import datetime as dt
import io
import os

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests

BASE = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
VARS = ["tmpc", "dwpc", "drct", "sknt", "mslp"]
SCHEMA = pa.schema([("time_utc", pa.timestamp("s", tz="UTC"))]
                   + [(v, pa.float32()) for v in VARS])


def fetch_year(station, yr):
    p_list = [("station", station), ("tz", "Etc/UTC"), ("format", "onlycomma"),
              ("latlon", "no"), ("missing", "M"), ("trace", "null"),
              ("year1", yr), ("month1", 1), ("day1", 1),
              ("year2", yr), ("month2", 12), ("day2", 31)]
    p_list += [("data", v) for v in VARS]
    r = requests.get(BASE, params=p_list, timeout=120)
    r.raise_for_status()
    lines = r.text.splitlines()
    if len(lines) < 2:
        return None
    header = lines[0].split(",")
    idx = {name: k for k, name in enumerate(header)}
    times, cols = [], {v: [] for v in VARS}
    for ln in lines[1:]:
        row = ln.split(",")
        if len(row) < len(header):
            continue
        try:
            t = dt.datetime.strptime(row[idx["valid"]], "%Y-%m-%d %H:%M")
        except Exception:
            continue
        times.append(np.datetime64(t, "s"))
        for v in VARS:
            val = row[idx[v]] if v in idx else "M"
            try:
                cols[v].append(float(val))
            except Exception:
                cols[v].append(None)
    if not times:
        return None
    return pa.table({"time_utc": pa.array(np.array(times, dtype="datetime64[s]"), type=pa.timestamp("s", tz="UTC")),
                     **{v: pa.array(cols[v], type=pa.float32()) for v in VARS}},
                    schema=SCHEMA)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", default="BED", help="IEM id, no K prefix (KBED->BED)")
    ap.add_argument("--start-year", type=int, default=2017)
    ap.add_argument("--end-year", type=int, default=2025)
    ap.add_argument("--out-dir", default="climatology/_local")
    a = ap.parse_args()
    for yr in range(a.start_year, a.end_year + 1):
        tbl = fetch_year(a.station, yr)
        if tbl is None:
            print(f"{a.station} {yr}: unavailable"); continue
        out = os.path.join(a.out_dir, "asos", a.station, f"{yr}.parquet")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        pq.write_table(tbl, out, compression="zstd")
        print(f"{a.station} {yr}: {tbl.num_rows} obs, {os.path.getsize(out)//1024} KB -> {out}")


if __name__ == "__main__":
    main()
