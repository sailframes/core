#!/usr/bin/env python3
"""
merge_subh_day.py — turn a historical day's HOURLY field parquet into a 15-MIN one
by merging HRRR sub-hourly (wrfsubhf) 10 m wind. For each hour HH we read that
cycle's wrfsubhf01 (gives HH:15 / :30 / :45), append them as wind-only rows (other
fields null), and rewrite fields/year=/month=/DD.parquet. The /tactics replay groups
by valid_time, so a 15-min parquet automatically yields a 15-min scrubber.

subh GRIB2 is only retained a few days in noaa-hrrr-bdp-pds, so this works for
recent days (the daily job runs on yesterday — in range).

Run: AWS_PROFILE=sailframes AWS_DEFAULT_REGION=us-east-1 \
     python3.11 climatology/merge_subh_day.py --date 2026-07-04
"""
import argparse
import io
import json
import os

import boto3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.compute as pc

import hrrr_grid as hg
from backfill_hrrr import FIELDS, SCHEMA
from backfill_subh import read_subh

BUCKET = os.environ.get("CLIMO_BUCKET", "sailframes-data-prod")
PFX = "climatology"
_s3 = boto3.client("s3")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)      # YYYY-MM-DD
    a = ap.parse_args()
    ymd = a.date.replace("-", "")
    key = f"{PFX}/fields/year={ymd[:4]}/month={ymd[4:6]}/{ymd[6:8]}.parquet"
    try:
        hourly = pq.read_table(io.BytesIO(_s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()))
    except Exception as e:
        print("no hourly parquet for", a.date, e); return
    # already 15-min? (distinct minutes beyond :00)
    mins = set(int(str(t)[14:16]) for t in pc.unique(hourly.column("valid_time_utc")).to_pylist())
    if mins - {0}:
        print(a.date, "already has sub-hourly steps — skip"); return

    window = hg.bbox_window(hg.store_anl(ymd, sorted(hg.list_anl_cycles(ymd))[0]))
    cycles = sorted(hg.list_anl_cycles(ymd))
    parts = []
    for c in cycles:
        sub = read_subh(ymd, c.replace("z", ""), window, nlead=1)   # f01 -> HH:15/:30/:45
        if sub is not None and sub.num_rows:
            parts.append(sub)
    if not parts:
        print(a.date, "no subh available (retention?) — left hourly"); return
    allsub = pa.concat_tables(parts)
    n = allsub.num_rows
    cols = {"valid_time_utc": allsub.column("valid_time_utc"), "gi": allsub.column("gi"),
            "u10": allsub.column("u10"), "v10": allsub.column("v10")}
    for f in FIELDS:
        if f not in ("u10", "v10"):
            cols[f] = pa.array(np.full(n, None), type=pa.float32())
    subtab = pa.table(cols, schema=SCHEMA).cast(hourly.schema)   # match ms/s timestamp unit
    merged = pa.concat_tables([hourly, subtab]).sort_by([("valid_time_utc", "ascending"), ("gi", "ascending")])
    buf = io.BytesIO(); pq.write_table(merged, buf, compression="zstd"); buf.seek(0)
    _s3.put_object(Bucket=BUCKET, Key=key, Body=buf.getvalue(),
                   ContentType="application/octet-stream", CacheControl="max-age=86400")
    nsteps = len(set(t.value for t in pc.unique(merged.column("valid_time_utc"))))
    print("%s: merged %d subh rows -> %d steps (15-min) in %s" % (a.date, n, nsteps, key))


if __name__ == "__main__":
    main()
