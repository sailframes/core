#!/usr/bin/env python3
"""
daily_update.py — the climatology-daily.yml job body (spec §5).

Once a day: extract YESTERDAY's HRRR fields, refresh the in-progress season's
obs, relabel it, merge into labels.parquet, upload, and invalidate CloudFront.

Design:
  - Fields: always backfill yesterday (year-round, per spec §2) -> upload one
    daily Parquet; the replay archive grows a day at a time (no bulk backfill).
  - Labels: only when in season (Apr 15-Oct 15). History (prior years) is
    immutable, so we relabel ONLY the current year and splice it into the
    existing labels.parquet (drop old current-year rows, append fresh ones).
    Uses the current-year obs path (backfill_ndbc merges monthly+realtime).

Idempotent: safe to re-run; each run recomputes yesterday + current season.
AWS creds + region come from the environment (configure-aws-credentials).
Run from the repo root:  python climatology/daily_update.py
"""
import datetime as dt
import json
import os
import subprocess
import sys

import boto3
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

BUCKET = os.environ.get("CLIMO_BUCKET", "sailframes-data-prod")
PFX = "climatology"
DIST_ID = os.environ.get("CLIMO_DIST_ID", "EFO342DVGM3QS")
WORK = os.environ.get("CLIMO_WORK", "_work")
PY = sys.executable

s3 = boto3.client("s3")
cf = boto3.client("cloudfront")


def log(m):
    print(f"[daily] {m}", flush=True)


def run(cmd):
    log("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def in_season(d):
    return (4, 15) <= (d.month, d.day) <= (10, 15)


def rebuild_field_index():
    """List climatology/fields/ and publish fields_index.json (which days are
    replayable) so the /tactics UI can mark analog days accurately."""
    import re
    dates, tok = [], None
    while True:
        kw = dict(Bucket=BUCKET, Prefix=f"{PFX}/fields/")
        if tok:
            kw["ContinuationToken"] = tok
        r = s3.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            m = re.search(r"year=(\d{4})/month=(\d{2})/(\d{2})\.parquet$", o["Key"])
            if m:
                dates.append(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
        if r.get("IsTruncated"):
            tok = r["NextContinuationToken"]
        else:
            break
    body = json.dumps(sorted(set(dates)))
    s3.put_object(Bucket=BUCKET, Key=f"{PFX}/fields_index.json", Body=body.encode(),
                  ContentType="application/json", CacheControl="max-age=300")
    log(f"fields_index.json: {len(set(dates))} replayable days")


def rebuild_breeze_index():
    """List climatology/breeze/*.json and publish breeze_index.json (days with a
    Bernot sea-breeze analysis)."""
    import re
    dates, tok = [], None
    while True:
        kw = dict(Bucket=BUCKET, Prefix=f"{PFX}/breeze/")
        if tok:
            kw["ContinuationToken"] = tok
        r = s3.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            m = re.search(r"breeze/(\d{4}-\d{2}-\d{2})\.json$", o["Key"])
            if m:
                dates.append(m.group(1))
        if r.get("IsTruncated"):
            tok = r["NextContinuationToken"]
        else:
            break
    s3.put_object(Bucket=BUCKET, Key=f"{PFX}/breeze_index.json", Body=json.dumps(sorted(set(dates))).encode(),
                  ContentType="application/json", CacheControl="max-age=300")
    log(f"breeze_index.json: {len(set(dates))} analysed days")


def put(local, key, ctype, cache):
    s3.upload_file(local, BUCKET, f"{PFX}/{key}",
                   ExtraArgs={"ContentType": ctype, "CacheControl": cache})
    log(f"uploaded s3://{BUCKET}/{PFX}/{key}")


def main():
    os.makedirs(WORK, exist_ok=True)
    y = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    ymd = y.strftime("%Y%m%d")
    yr = y.year
    invalidate = []

    # 1) yesterday's HRRR fields (year-round) -> upload. Non-fatal if not yet published.
    try:
        run([PY, "climatology/backfill_hrrr.py", "--date", ymd, "--out-dir", WORK])
        fkey = f"fields/year={y:%Y}/month={y:%m}/{y:%d}.parquet"
        fpath = os.path.join(WORK, f"year={y:%Y}", f"month={y:%m}", f"{y:%d}.parquet")
        if os.path.exists(fpath):
            put(fpath, fkey, "application/octet-stream", "max-age=86400")
            invalidate.append(f"/{PFX}/{fkey}")
            rebuild_field_index()   # refresh replayable-days list for the UI
            invalidate.append(f"/{PFX}/fields_index.json")
            # 15-min: merge HRRR sub-hourly wind into the day parquet (replay scrubber)
            try:
                run([PY, "climatology/merge_subh_day.py", "--date", y.isoformat()])
            except Exception as e:
                log(f"WARN subh 15-min merge failed ({e}); day stays hourly")
    except Exception as e:
        log(f"WARN fields backfill failed ({e}); continuing to labels")

    # 1b) Bernot sea-breeze analysis for yesterday -> breeze/<date>.json (+ index)
    if in_season(y):
        try:
            run([PY, "climatology/breeze_day.py", "--date", y.isoformat(),
                 "--out-dir", os.path.join(WORK, "breeze")])
            bpath = os.path.join(WORK, "breeze", f"{y.isoformat()}.json")
            if os.path.exists(bpath):
                put(bpath, f"breeze/{y.isoformat()}.json", "application/json", "max-age=300")
                invalidate.append(f"/{PFX}/breeze/{y.isoformat()}.json")
                rebuild_breeze_index()
                invalidate.append(f"/{PFX}/breeze_index.json")
        except Exception as e:
            log(f"WARN breeze analysis failed ({e})")

    # 2) labels — in-season only; relabel current year and splice into history
    if in_season(y):
        s3.download_file(BUCKET, f"{PFX}/grid.json", os.path.join(WORK, "grid.json"))
        run([PY, "climatology/backfill_ndbc.py", "--station", "44013",
             "--start-year", str(yr), "--end-year", str(yr), "--out-dir", WORK])
        run([PY, "climatology/backfill_asos.py", "--station", "BED",
             "--start-year", str(yr), "--end-year", str(yr), "--out-dir", WORK])
        run([PY, "climatology/backfill_coops.py", "--station", "8443970",
             "--start-year", str(yr), "--end-year", str(yr), "--out-dir", WORK])
        run([PY, "climatology/label_days.py", "--start", f"{yr}0101", "--end", ymd,
             "--local", WORK, "--grid", os.path.join(WORK, "grid.json"),
             "--out", os.path.join(WORK, "cur_labels.parquet")])

        s3.download_file(BUCKET, f"{PFX}/labels.parquet", os.path.join(WORK, "labels_old.parquet"))
        old = pq.read_table(os.path.join(WORK, "labels_old.parquet"))
        cur = pq.read_table(os.path.join(WORK, "cur_labels.parquet"))
        keep = old.filter(pc.invert(pc.starts_with(old["date"], str(yr))))   # immutable history
        merged = pa.concat_tables([keep, cur]).sort_by("date")
        out = os.path.join(WORK, "labels.parquet")
        pq.write_table(merged, out, compression="zstd")
        put(out, "labels.parquet", "application/octet-stream", "max-age=300")
        invalidate.append(f"/{PFX}/labels.parquet")
        log(f"labels: {old.num_rows} -> {merged.num_rows} rows (current year {yr} refreshed)")
    else:
        log(f"{y} out of season (Apr15-Oct15) — fields only, no relabel")

    # 3) CloudFront invalidation
    if invalidate:
        r = cf.create_invalidation(
            DistributionId=DIST_ID,
            InvalidationBatch={"Paths": {"Quantity": len(invalidate), "Items": invalidate},
                               "CallerReference": f"climo-daily-{ymd}"})
        log(f"invalidation {r['Invalidation']['Id']} for {invalidate}")
    log("done")


if __name__ == "__main__":
    main()
