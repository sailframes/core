#!/usr/bin/env python3
"""
backfill_currents.py — NOAA tidal-current predictions for the venue stations on one
day → climatology/currents/<date>.json for the /tactics "Current" map layer.

For each station in current_stations.json we fetch 30-min current predictions and
resolve a compass SET (flood dir when Velocity_Major≥0, else ebb dir) + DRIFT (|kt|),
so the map can show a time-synced tidal-current arrow. Times are local (LST/LDT =
the same LT the replay scrubber uses).

Run: AWS_PROFILE=sailframes python3.11 climatology/backfill_currents.py --date 2026-07-04
"""
import argparse
import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
import requests

BUCKET = os.environ.get("CLIMO_BUCKET", "sailframes-data-prod")
PFX = "climatology"
API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
_s3 = boto3.client("s3")


def fetch_station(st, ymd):
    p = dict(product="currents_predictions", application="sailframes",
             begin_date=ymd, end_date=ymd, station=st["id"], bin="1",
             time_zone="lst_ldt", interval="30", units="english", format="json")
    recs = None
    for attempt in range(4):                              # NOAA throttles bursts — retry with backoff
        try:
            j = requests.get(API, params=p, timeout=45).json()
            recs = j.get("current_predictions", {}).get("cp", [])
            if recs:
                break
        except Exception:
            pass
        time.sleep(0.6 * (attempt + 1))
    try:
        if not recs:
            return None
        series = []
        for r in recs:
            t = r["Time"]                                 # 'YYYY-MM-DD HH:MM' local
            h = int(t[11:13]) + int(t[14:16]) / 60.0
            series.append([round(h, 2), round(float(r["Velocity_Major"]), 2)])   # h_lt, signed kt (+flood/-ebb)
        return {"id": st["id"], "name": st["name"], "lat": st["lat"], "lon": st["lon"],
                "flood": float(recs[0]["meanFloodDir"]), "ebb": float(recs[0]["meanEbbDir"]),
                "series": series}
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)      # YYYY-MM-DD
    ap.add_argument("--out-dir", default=None)
    a = ap.parse_args()
    ymd = a.date.replace("-", "")
    stations = json.load(io.BytesIO(_s3.get_object(Bucket=BUCKET, Key=f"{PFX}/current_stations.json")["Body"].read()))
    with ThreadPoolExecutor(max_workers=4) as ex:
        got = [r for r in ex.map(lambda s: fetch_station(s, ymd), stations) if r]
    out = {"date": a.date, "n_stations": len(got), "units": "set=°toward, drift=kt", "stations": got}
    body = json.dumps(out).encode()
    if a.out_dir:
        os.makedirs(a.out_dir, exist_ok=True)
        open(os.path.join(a.out_dir, f"{a.date}.json"), "wb").write(body)
    else:
        _s3.put_object(Bucket=BUCKET, Key=f"{PFX}/currents/{a.date}.json", Body=body,
                       ContentType="application/json", CacheControl="max-age=86400")
    peak = max((max((abs(s2[1]) for s2 in s["series"]), default=0) for s in got), default=0)
    print("currents %s: %d/%d stations, peak drift %.1f kt" % (a.date, len(got), len(stations), peak))


if __name__ == "__main__":
    main()
