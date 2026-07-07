#!/usr/bin/env python3
"""
backfill_neracoos.py — NERACOOS/IOOS buoy met (wind) -> obs Parquet.

For the model-validation overlay: independent on-water wind observations to
compare against the HRRR field. NERACOOS buoy A01 sits IN Massachusetts Bay
(42.52 N, -70.57 W), inside the venue bbox — a second on-water truth station
alongside NDBC 44013.

Source: NERACOOS ERDDAP tabledap (https://data.neracoos.org/erddap). Wind is
hourly (10-min slots are NaN placeholders); we keep the on-the-hour values.
Output: obs/{station}/{YYYY}.parquet — same schema as backfill_ndbc.

Run:  python3 climatology/backfill_neracoos.py --dataset A01_met --station A01 --start-year 2017 --end-year 2026 --out-dir climatology/_local
"""
import argparse
import datetime as dt
import io
import os

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests

ERDDAP = "https://data.neracoos.org/erddap/tabledap/{ds}.csv"
# ERDDAP var -> our obs field
MAP = {"wind_speed": "wspd", "wind_direction": "wdir", "wind_gust": "gust",
       "air_temperature": "t_air", "barometric_pressure": "slp"}
OUT_FIELDS = ["wspd", "wdir", "gust", "t_air", "t_water", "slp"]
SCHEMA = pa.schema([("time_utc", pa.timestamp("s", tz="UTC"))]
                   + [(f, pa.float32()) for f in OUT_FIELDS])


def fetch_year(dataset, yr):
    vars_ = ["time"] + list(MAP)
    url = (ERDDAP.format(ds=dataset) + "?" + ",".join(vars_)
           + f"&time%3E={yr}-01-01T00:00:00Z&time%3C={yr}-12-31T23:59:59Z")
    r = requests.get(url, timeout=120)
    if r.status_code != 200 or not r.text.strip():
        return None, None
    rows = list(io.StringIO(r.text))
    if len(rows) < 3:
        return None, None
    header = rows[0].strip().split(",")           # names
    idx = {n: k for k, n in enumerate(header)}
    times, cols = [], {f: [] for f in OUT_FIELDS}
    latlon = None
    for line in rows[2:]:                          # row[1] = units line
        c = line.rstrip("\n").split(",")
        if len(c) < len(header):
            continue
        try:
            t = dt.datetime.strptime(c[idx["time"]], "%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            continue
        if t.minute != 0:                          # keep hourly only
            continue
        vals = {f: None for f in OUT_FIELDS}
        any_wind = False
        for ev, field in MAP.items():
            try:
                v = float(c[idx[ev]])
                if v == v:                         # not NaN
                    vals[field] = v
                    if field in ("wspd", "wdir"):
                        any_wind = True
            except Exception:
                pass
        if not any_wind:
            continue
        times.append(np.datetime64(t, "s"))
        for f in OUT_FIELDS:
            cols[f].append(vals[f])
    if not times:
        return None, None
    tbl = pa.table({"time_utc": pa.array(np.array(times, dtype="datetime64[s]"), type=pa.timestamp("s", tz="UTC")),
                    **{f: pa.array(cols[f], type=pa.float32()) for f in OUT_FIELDS}}, schema=SCHEMA)
    return tbl, latlon


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="A01_met")
    ap.add_argument("--station", default="A01")
    ap.add_argument("--start-year", type=int, default=2017)
    ap.add_argument("--end-year", type=int, default=2026)
    ap.add_argument("--out-dir", default="climatology/_local")
    a = ap.parse_args()
    for yr in range(a.start_year, a.end_year + 1):
        tbl, _ = fetch_year(a.dataset, yr)
        if tbl is None:
            print(f"{a.station} {yr}: unavailable"); continue
        out = os.path.join(a.out_dir, "obs", a.station, f"{yr}.parquet")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        pq.write_table(tbl, out, compression="zstd")
        print(f"{a.station} {yr}: {tbl.num_rows} hourly obs -> {out}")


if __name__ == "__main__":
    main()
