#!/usr/bin/env python3
"""
migrate_wind_frame.py — one-time migration: rotate stored fields/*.parquet u10/v10
from GRID-relative to EARTH-relative (true north).

HRRR GRIB winds are grid-relative; the pipeline never rotated them, so every stored
wind direction was ~16° CCW of truth over Mass Bay (LCC grid convergence). The read
side (backfill_hrrr / merge_subh) now rotates at ingestion; this backfills the
already-written history in place. Idempotent: parquet files carry schema metadata
`wind_frame=earth` once rotated, and are skipped on re-run.

Run:  AWS_PROFILE=sailframes python3.11 climatology/migrate_wind_frame.py [--limit N] [--dry]
"""
import argparse, io, os, sys
from concurrent.futures import ThreadPoolExecutor

import boto3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hrrr_grid as hg

BUCKET = os.environ.get("CLIMO_BUCKET", "sailframes-data-prod")
PFX = "climatology"
s3 = boto3.client("s3")

# static per-cell convergence angle over the window, indexed by local gi (0..ncell-1)
_w = hg.bbox_window(hg.store_anl("20260704", "18z"))
ANG = hg.convergence_window(hg.store_anl("20260704", "18z"), *_w)


def list_keys():
    keys, tok = [], None
    while True:
        kw = dict(Bucket=BUCKET, Prefix=f"{PFX}/fields/")
        if tok:
            kw["ContinuationToken"] = tok
        r = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in r.get("Contents", []) if o["Key"].endswith(".parquet")]
        if r.get("IsTruncated"):
            tok = r["NextContinuationToken"]
        else:
            break
    return sorted(keys)


def migrate(key, dry=False):
    t = pq.read_table(io.BytesIO(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()))
    md = dict(t.schema.metadata or {})
    if md.get(b"wind_frame") == b"earth":
        return "skip"
    gi = t.column("gi").combine_chunks().to_numpy(zero_copy_only=False).astype(int)
    u = t.column("u10").combine_chunks().to_numpy(zero_copy_only=False)
    v = t.column("v10").combine_chunks().to_numpy(zero_copy_only=False)
    ue, ve = hg.to_earth(u, v, ANG[gi])
    iu, iv = t.schema.get_field_index("u10"), t.schema.get_field_index("v10")
    t = t.set_column(iu, "u10", pa.array(ue.astype("f4")))
    t = t.set_column(iv, "v10", pa.array(ve.astype("f4")))
    md[b"wind_frame"] = b"earth"
    t = t.replace_schema_metadata(md)
    if dry:
        return "would-rotate"
    buf = io.BytesIO(); pq.write_table(t, buf, compression="zstd")
    s3.put_object(Bucket=BUCKET, Key=key, Body=buf.getvalue(),
                  ContentType="application/octet-stream", CacheControl="max-age=86400")
    return "rotated"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--workers", type=int, default=12)
    a = ap.parse_args()
    keys = list_keys()
    if a.limit:
        keys = keys[:a.limit]
    print(f"convergence over window: {np.degrees(ANG.min()):.2f}–{np.degrees(ANG.max()):.2f}°  |  {len(keys)} parquet files")
    n = {"rotated": 0, "skip": 0, "would-rotate": 0, "err": 0}
    done = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for st in ex.map(lambda k: _safe(k, a.dry), keys):
            n[st] = n.get(st, 0) + 1
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(keys)}  {n}")
    print("done:", n)


def _safe(k, dry):
    try:
        return migrate(k, dry)
    except Exception as e:
        print("ERR", k, e)
        return "err"


if __name__ == "__main__":
    main()
