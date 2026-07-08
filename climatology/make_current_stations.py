#!/usr/bin/env python3
"""
make_current_stations.py — pick a representative set of NOAA tidal-current stations
for the venue and publish current_stations.json.

The venue has ~130 current-prediction stations, densely clustered in Boston Harbour's
island channels. We dedup by id, prefer the BOS harmonic-reference stations over the
ACT subordinate ones, and greedily thin to ≥MIN_KM spacing so the map isn't a blob —
keeping the open-bay / North Shore / Stellwagen stations that matter for racing.

Run: AWS_PROFILE=sailframes python3.11 climatology/make_current_stations.py
"""
import io
import json
import math
import os

import boto3
import requests

BUCKET = os.environ.get("CLIMO_BUCKET", "sailframes-data-prod")
PFX = "climatology"
BBOX = (42.15, 42.72, -71.15, -70.30)      # lat0, lat1, lon0, lon1
MIN_KM = 2.6
_s3 = boto3.client("s3")


def hav(a, b, c, d):
    r = 6371.0
    dla, dlo = math.radians(c - a), math.radians(d - b)
    h = math.sin(dla / 2) ** 2 + math.cos(math.radians(a)) * math.cos(math.radians(c)) * math.sin(dlo / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def main():
    js = requests.get("https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json?type=currentpredictions", timeout=60).json()
    la0, la1, lo0, lo1 = BBOX
    uniq = {}
    for s in js["stations"]:
        la, lo = s.get("lat", 0), s.get("lng", 0)
        if la0 <= la <= la1 and lo0 <= lo <= lo1:
            uniq.setdefault(s["id"], s)
    cand = list(uniq.values())
    # prefer BOS (harmonic reference) so they win the thinning ties
    cand.sort(key=lambda s: (0 if s["id"].startswith("BOS") else 1, s["id"]))
    kept = []
    for s in cand:
        if all(hav(s["lat"], s["lng"], k["lat"], k["lng"]) >= MIN_KM for k in kept):
            kept.append(s)
    out = [{"id": s["id"], "name": s.get("name", ""), "lat": round(s["lat"], 4), "lon": round(s["lng"], 4)} for s in kept]
    _s3.put_object(Bucket=BUCKET, Key=f"{PFX}/current_stations.json",
                   Body=json.dumps(out).encode(), ContentType="application/json", CacheControl="max-age=86400")
    print("current_stations.json: %d of %d unique stations (≥%.1f km spacing)" % (len(out), len(uniq), MIN_KM))
    for s in out:
        print("  %-9s %.3f,%.3f  %s" % (s["id"], s["lat"], s["lon"], s["name"][:40]))


if __name__ == "__main__":
    main()
