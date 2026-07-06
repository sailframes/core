#!/usr/bin/env python3
"""
make_labels_sample.py — synthetic labels.parquet for the Phase-1 serving
smoke test (DuckDB-WASM + CloudFront Range/CORS).

This is NOT the real classifier output — it is a schema-faithful, plausibly-
valued stand-in (spec §4 `labels` schema) sized like the real thing (~one
row per day across the archive span) so we can prove the browser can range-
query a Parquet served from CloudFront before Phase 1 builds label_days.py.

Deterministic (seeded) so re-runs are byte-stable. Requires pyarrow.
Run with the interpreter that has pyarrow (homebrew 3.11 on this box):
  /opt/homebrew/opt/python@3.11/bin/python3.11 climatology/smoke/make_labels_sample.py
"""
import datetime as dt
import math
import random

import pyarrow as pa
import pyarrow.parquet as pq

OUT = "climatology/smoke/labels.parquet"
START = dt.date(2016, 8, 23)   # hrrrzarr archive start (Phase 0)
END = dt.date(2025, 10, 15)
TYPES = ["F", "R", "P", "G", "N", "U"]           # spec §6 day types
TYPE_W = [0.18, 0.22, 0.06, 0.20, 0.30, 0.04]    # rough plausibility weights

rng = random.Random(20260705)


def vmean_dir(base, spread):
    return (base + rng.uniform(-spread, spread)) % 360.0


def build():
    rows = []
    d = START
    while d <= END:
        doy = d.timetuple().tm_yday
        in_season = dt.date(d.year, 4, 15) <= d <= dt.date(d.year, 10, 15)
        # seasonal sea-breeze propensity peaks mid-summer
        summer = math.cos((doy - 200) / 60.0)
        t = "U" if not in_season and rng.random() < 0.15 else rng.choices(TYPES, TYPE_W)[0]
        onset_base = rng.uniform(11.5, 15.0)            # local time of fill
        dir_on = vmean_dir(120, 40)                     # onshore-ish
        grad_spd = max(0.0, rng.gauss(7 + 4 * (1 - summer), 3))
        grad_dir = vmean_dir(230, 70)
        gu = -grad_spd * math.sin(math.radians(grad_dir))
        gv = -grad_spd * math.cos(math.radians(grad_dir))
        rows.append(dict(
            date=d.isoformat(),
            type=t,
            onset_lt_44013=round(onset_base, 2),
            onset_lt_MHD=round(onset_base - rng.uniform(0, 0.8), 2),
            onset_lt_GLO=round(onset_base - rng.uniform(0, 0.6), 2),
            onset_lt_RPT=round(onset_base + rng.uniform(0, 1.0), 2),
            dir_onset=round(dir_on, 1),
            dir_12lt=round(vmean_dir(dir_on - 20, 20), 1),
            dir_14lt=round(vmean_dir(dir_on, 15), 1),
            dir_16lt=round(vmean_dir(dir_on + 12, 15), 1),
            dir_18lt=round(vmean_dir(dir_on + 20, 20), 1),
            spd_peak=round(max(0.0, rng.gauss(11, 3)), 1),
            dt_c=round(rng.gauss(6 * summer + 2, 3), 1),           # KBED Tmax - SST
            grad_u=round(gu, 2),
            grad_v=round(gv, 2),
            grad_spd=round(grad_spd, 2),
            grad_dir=round(grad_dir, 1),
            tcdc_am=round(min(100, max(0, rng.gauss(45, 30))), 0),
            dswrf_am=round(max(0, rng.gauss(600 * (0.6 + 0.4 * summer), 180)), 0),
            sst=round(rng.gauss(12 + 8 * summer, 2), 1),
            tide_phase_hw_h=round(rng.uniform(-6.2, 6.2), 2),      # hrs rel Boston HW
            synoptic_tag="",                                       # Phase 2
            qc_flags=0,
        ))
        d += dt.timedelta(days=1)
    return rows


def main():
    rows = build()
    cols = {k: [r[k] for r in rows] for k in rows[0]}
    schema = pa.schema([
        ("date", pa.string()), ("type", pa.string()),
        ("onset_lt_44013", pa.float32()), ("onset_lt_MHD", pa.float32()),
        ("onset_lt_GLO", pa.float32()), ("onset_lt_RPT", pa.float32()),
        ("dir_onset", pa.float32()),
        ("dir_12lt", pa.float32()), ("dir_14lt", pa.float32()),
        ("dir_16lt", pa.float32()), ("dir_18lt", pa.float32()),
        ("spd_peak", pa.float32()), ("dt_c", pa.float32()),
        ("grad_u", pa.float32()), ("grad_v", pa.float32()),
        ("grad_spd", pa.float32()), ("grad_dir", pa.float32()),
        ("tcdc_am", pa.float32()), ("dswrf_am", pa.float32()),
        ("sst", pa.float32()), ("tide_phase_hw_h", pa.float32()),
        ("synoptic_tag", pa.string()), ("qc_flags", pa.int16()),
    ])
    tbl = pa.table(cols, schema=schema)
    pq.write_table(tbl, OUT, compression="zstd", row_group_size=2048)
    print(f"wrote {OUT}: {tbl.num_rows} rows, {len(schema)} cols")


if __name__ == "__main__":
    main()
