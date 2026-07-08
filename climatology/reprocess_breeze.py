#!/usr/bin/env python3
"""
reprocess_breeze.py — re-run breeze_day for every analysed day and re-upload
breeze/<date>.json. Used after the wind-frame (grid→earth) rotation fix so the
850/quadrant/onshore directions in the stored reports are corrected.

Requires the fields/*.parquet to already be earth-relative (run migrate_wind_frame.py
first) since breeze_day reads 10 m wind from the parquet.

Run:  AWS_PROFILE=sailframes python3.11 climatology/reprocess_breeze.py [--limit N] [--workers 5]
"""
import argparse, io, json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

import boto3

BUCKET = os.environ.get("CLIMO_BUCKET", "sailframes-data-prod")
PFX = "climatology"
PY = sys.executable
OUT = "/tmp/breeze_reproc"
s3 = boto3.client("s3")


def days():
    idx = json.loads(s3.get_object(Bucket=BUCKET, Key=f"{PFX}/breeze_index.json")["Body"].read())
    return idx


def one(d):
    try:
        r = subprocess.run([PY, "climatology/breeze_day.py", "--date", d, "--out-dir", OUT],
                           capture_output=True, text=True, timeout=180)
        p = os.path.join(OUT, f"{d}.json")
        if r.returncode != 0 or not os.path.exists(p):
            return (d, "FAIL " + (r.stderr or r.stdout or "")[-200:])
        s3.put_object(Bucket=BUCKET, Key=f"{PFX}/breeze/{d}.json",
                      Body=open(p, "rb").read(), ContentType="application/json", CacheControl="max-age=300")
        return (d, "ok")
    except Exception as e:
        return (d, "ERR " + str(e)[:200])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=5)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    ds = days()
    if a.limit:
        ds = ds[:a.limit]
    print(f"reprocessing {len(ds)} breeze reports, {a.workers} workers")
    ok = fail = done = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for d, st in ex.map(one, ds):
            done += 1
            if st == "ok":
                ok += 1
            else:
                fail += 1
                print(f"  {d}: {st}")
            if done % 50 == 0:
                print(f"  {done}/{len(ds)}  ok={ok} fail={fail}")
    print(f"done: ok={ok} fail={fail}")


if __name__ == "__main__":
    main()
