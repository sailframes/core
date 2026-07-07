#!/usr/bin/env python3
"""
make_obs_stations.py — emit obs_stations.json: the observation stations shown on
the /tactics replay map for model validation. Small, static (stations are fixed);
the UI fetches it once and reads each station's obs/{id}/{year}.parquet per day.

Coords are the published station locations. type: buoy (on-water) | airport (land).
"""
import argparse
import json

# id -> (name, lat, lon, type, source)  — Massachusetts Bay venue set
STATIONS = [
    ("44013", "NDBC 44013 (mid Mass Bay)", 42.346, -70.651, "buoy", "ndbc"),
    ("A01", "NERACOOS A01 (N Mass Bay)", 42.5231, -70.5663, "buoy", "neracoos"),
    ("BOS", "KBOS Logan (coastal)", 42.3606, -71.0097, "airport", "asos"),
    ("BVY", "KBVY Beverly (coastal N)", 42.5842, -70.9161, "airport", "asos"),
    ("BED", "KBED Bedford (inland)", 42.4699, -71.2890, "airport", "asos"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="climatology/obs_stations.json")
    a = ap.parse_args()
    out = [{"id": i, "name": n, "lat": la, "lon": lo, "type": t, "source": s}
           for (i, n, la, lo, t, s) in STATIONS]
    with open(a.out, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote {a.out}: {len(out)} stations "
          f"({sum(x['type']=='buoy' for x in out)} buoy, {sum(x['type']=='airport' for x in out)} airport)")


if __name__ == "__main__":
    main()
