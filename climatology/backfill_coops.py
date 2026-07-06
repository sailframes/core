#!/usr/bin/env python3
"""
backfill_coops.py — NOAA CO-OPS tide predictions -> Parquet (spec §4 `coops`).

Water-level predictions (6-min curve for replay + hilo for HW/LW times the
classifier's tide_phase_hw_h needs). 6-min predictions are capped at 31 days
per API call, so we chunk monthly. Output:
  coops/{station}/pred_{YYYY}.parquet      (time_utc, pred_m)          6-min
  coops/{station}/hilo_{YYYY}.parquet      (time_utc, type, pred_m)    H/L only

Phase 0 confirmed Boston 8443970 + Provincetown 8446121. Datum MLLW, metric.

Run:  python3 climatology/backfill_coops.py --station 8443970 --start-year 2017 --end-year 2025 --out-dir climatology/_local
"""
import argparse
import datetime as dt
import os

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests

API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"


def _get(station, begin, end, interval):
    p = dict(product="predictions", application="sailframes-climatology",
             begin_date=begin, end_date=end, datum="MLLW", station=station,
             time_zone="gmt", units="metric", format="json")
    if interval == "hilo":
        p["interval"] = "hilo"
    else:
        p["interval"] = "6"
    r = requests.get(API, params=p, timeout=60)
    r.raise_for_status()
    return r.json().get("predictions", [])


def _months(yr):
    for m in range(1, 13):
        b = dt.date(yr, m, 1)
        e = (dt.date(yr, m + 1, 1) - dt.timedelta(days=1)) if m < 12 else dt.date(yr, 12, 31)
        yield b.strftime("%Y%m%d"), e.strftime("%Y%m%d")


def _parse_t(s):
    return np.datetime64(dt.datetime.strptime(s, "%Y-%m-%d %H:%M"), "s")


def year_curve(station, yr):
    times, vals = [], []
    for b, e in _months(yr):
        for row in _get(station, b, e, "6"):
            times.append(_parse_t(row["t"])); vals.append(float(row["v"]))
    if not times:
        return None
    return pa.table({"time_utc": pa.array(np.array(times, dtype="datetime64[s]"), type=pa.timestamp("s", tz="UTC")),
                     "pred_m": pa.array(vals, type=pa.float32())})


def year_hilo(station, yr):
    times, typ, vals = [], [], []
    b, e = dt.date(yr, 1, 1).strftime("%Y%m%d"), dt.date(yr, 12, 31).strftime("%Y%m%d")
    for row in _get(station, b, e, "hilo"):
        times.append(_parse_t(row["t"])); typ.append(row.get("type", "")); vals.append(float(row["v"]))
    if not times:
        return None
    return pa.table({"time_utc": pa.array(np.array(times, dtype="datetime64[s]"), type=pa.timestamp("s", tz="UTC")),
                     "type": pa.array(typ), "pred_m": pa.array(vals, type=pa.float32())})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", default="8443970")   # Boston
    ap.add_argument("--start-year", type=int, default=2017)
    ap.add_argument("--end-year", type=int, default=2025)
    ap.add_argument("--out-dir", default="climatology/_local")
    ap.add_argument("--curve", action="store_true", help="also fetch the 6-min curve (slow, monthly)")
    a = ap.parse_args()
    base = os.path.join(a.out_dir, "coops", a.station)
    os.makedirs(base, exist_ok=True)
    for yr in range(a.start_year, a.end_year + 1):
        hilo = year_hilo(a.station, yr)
        if hilo is not None:
            f = os.path.join(base, f"hilo_{yr}.parquet")
            pq.write_table(hilo, f, compression="zstd")
            print(f"{a.station} {yr} hilo: {hilo.num_rows} rows -> {f}")
        if a.curve:
            cur = year_curve(a.station, yr)
            if cur is not None:
                f = os.path.join(base, f"pred_{yr}.parquet")
                pq.write_table(cur, f, compression="zstd")
                print(f"{a.station} {yr} 6-min: {cur.num_rows} rows, {os.path.getsize(f)//1024} KB -> {f}")


if __name__ == "__main__":
    main()
