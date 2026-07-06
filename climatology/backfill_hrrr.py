#!/usr/bin/env python3
"""
backfill_hrrr.py — extract the bbox HRRR field archive from hrrrzarr to daily
Parquet (spec §4 `fields`): one row per (valid_time, grid-cell).

Schema: valid_time_utc (timestamp[s], UTC), gi (uint16 row-major grid index,
matching grid.json), u10 v10 gust t2 td2 mslp tcdc dswrf (float32). Sorted
(valid_time, gi). Output: fields/year=YYYY/month=MM/DD.parquet.

Per day: up to 24 F00 analysis cycles x 1638 cells = ~39k rows, ~300-400 KB.
Missing cycles are logged and appended to a gaps list (--gaps-out).

Run:
  python3 climatology/backfill_hrrr.py --date 20250701 --out-dir climatology/_local
  python3 climatology/backfill_hrrr.py --start 20250701 --end 20250703 --out-dir ...
Units are stored SI as-emitted by HRRR (m/s, K, Pa, %, W/m^2); display
conversion happens client-side.
"""
import argparse
import datetime as dt
import os

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

import hrrr_grid as hg

FIELDS = ["u10", "v10", "gust", "t2", "td2", "mslp", "tcdc", "dswrf"]
SCHEMA = pa.schema(
    [("valid_time_utc", pa.timestamp("s", tz="UTC")), ("gi", pa.uint16())]
    + [(f, pa.float32()) for f in FIELDS]
)


def _valid_time(date, cycle):
    return dt.datetime(int(date[:4]), int(date[4:6]), int(date[6:8]),
                       int(cycle[:2]), tzinfo=dt.timezone.utc)


def extract_day(date, window):
    """Return (pyarrow.Table, missing_cycles). window = (j0,j1,i0,i1)."""
    j0, j1, i0, i1 = window
    ncell = (j1 - j0 + 1) * (i1 - i0 + 1)
    gi = np.arange(ncell, dtype="uint16")
    cycles = hg.list_anl_cycles(date)
    present = {f"{h:02d}z" for h in range(24)} & set(cycles)
    missing = sorted({f"{h:02d}z" for h in range(24)} - present)

    times, gicol = [], []
    cols = {f: [] for f in FIELDS}
    for cyc in sorted(present):
        store = hg.store_anl(date, cyc)
        vt = _valid_time(date, cyc)
        # read all 8 fields for this cycle over the window
        arrs = {f: hg.read_window(store, hg.REQUIRED[f], j0, j1, i0, i1).ravel() for f in FIELDS}
        times.append(np.full(ncell, np.datetime64(vt.replace(tzinfo=None), "s")))
        gicol.append(gi)
        for f in FIELDS:
            cols[f].append(arrs[f])
    if not times:
        return None, missing
    tbl = pa.table(
        {"valid_time_utc": pa.array(np.concatenate(times), type=pa.timestamp("s", tz="UTC")),
         "gi": pa.array(np.concatenate(gicol)),
         **{f: pa.array(np.concatenate(cols[f]), type=pa.float32()) for f in FIELDS}},
        schema=SCHEMA,
    )
    # sorted (valid_time, gi) already by construction (cycles ascending, gi ascending)
    return tbl, missing


def daterange(start, end):
    d = dt.datetime.strptime(start, "%Y%m%d").date()
    e = dt.datetime.strptime(end, "%Y%m%d").date()
    while d <= e:
        yield d.strftime("%Y%m%d")
        d += dt.timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--out-dir", default="climatology/_local")
    ap.add_argument("--gaps-out", default=None)
    a = ap.parse_args()
    dates = [a.date] if a.date else list(daterange(a.start, a.end))

    # window is constant across dates — resolve once from the first available store
    ref = hg.store_anl(dates[0], (hg.list_anl_cycles(dates[0]) or ["18z"])[-1])
    window = hg.bbox_window(ref)

    all_gaps = []
    for date in dates:
        tbl, missing = extract_day(date, window)
        if tbl is None:
            print(f"{date}: NO cycles available")
            all_gaps.append({"date": date, "missing": "ALL"})
            continue
        out = os.path.join(a.out_dir, f"year={date[:4]}", f"month={date[4:6]}", f"{date[6:8]}.parquet")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        pq.write_table(tbl, out, compression="zstd", row_group_size=8192)
        kb = os.path.getsize(out) / 1024
        print(f"{date}: {tbl.num_rows} rows, {kb:.0f} KB -> {out}"
              + (f"  [missing {len(missing)} cyc: {','.join(missing)}]" if missing else "  [24/24]"))
        if missing:
            all_gaps.append({"date": date, "missing": ",".join(missing)})
    if a.gaps_out and all_gaps:
        import json
        with open(a.gaps_out, "w") as f:
            json.dump(all_gaps, f, indent=0)
        print(f"gaps -> {a.gaps_out} ({len(all_gaps)} days with missing cycles)")


if __name__ == "__main__":
    main()
