#!/usr/bin/env python3
"""
Seed the 2026 Eastern Yacht Club EYC Annual Regatta (Marblehead) as a
HIDDEN (admin-only) regatta, with a single boat — Man of War (Class F) —
and Paul's iPhone GPS track for playback.

Only one boat is loaded on purpose: for this event we only have one
GPS track (Man of War, recorded on an iPhone with the Sensor Logger
app). No other entrants / official results are seeded.

The regatta is created with visibility="admin", so events.html and
race.html hide it from non-admins (frontend obscurity — the API itself
stays open). Requires the api_race Lambda redeployed with `visibility`
support, otherwise the field silently drops and the regatta shows to
everyone.

Race facts (from regattaman.com race_id=35, sailed Sat 2026-07-04;
the "July 06" on the results page is the results-posting date):
  - Class F start 13:00:00 EDT
  - Man of War: sail 32559, C&C 41, ORR-EZ rating 1.0156,
    finish 14:01:06 EDT, elapsed 01:01:06, corrected 01:02:03, 7th.

GPS track: Sensor Logger export folder with Location.csv
(time in ns UTC, latitude, longitude, altitudeAboveMeanSeaLevel, speed
in m/s). Converted to GPX and uploaded to the race by boat_id.

Idempotent: re-running reuses the regatta (matched on name), the boat
(matched on sail_number), and the race (matched on regatta_id + date +
name), and re-uploads the track (overwrites the same S3 key).

Usage:
    python3 scripts/seed_eyc_annual_2026.py [TRACK_DIR]
"""

import csv
import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

API_BASE = "https://rnngzx7flk.execute-api.us-east-1.amazonaws.com"

# 2026-07-04 is EDT = UTC-4.
LOCAL_TO_UTC = timedelta(hours=4)
RACE_DATE = "2026-07-04"

DEFAULT_TRACK_DIR = "/Users/paul2/Downloads/Gulf_of_Maine-2026-07-04_16-17-54"

RATING_SYSTEM = "ORR-EZ"          # Class F rating is Time-on-Time (corrected = elapsed × rating)
RATING_TYPE = "W50/L50"


def local_iso(hms: str) -> str:
    """'13:00:00' local EDT on RACE_DATE → '2026-07-04T17:00:00Z' UTC."""
    y, m, d = map(int, RACE_DATE.split("-"))
    h, mi, s = map(int, hms.split(":"))
    dt = datetime(y, m, d, h, mi, s) + LOCAL_TO_UTC
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


REGATTA = {
    "name": "2026 Eastern Yacht Club EYC Annual Regatta — 156th Anniversary",
    "venue": "Marblehead — Eastern Yacht Club",
    "rating_system": RATING_SYSTEM,
    "start_date": RACE_DATE,
    "end_date": RACE_DATE,
    "website_url": "https://www.regattaman.com/results.php?yr=2026&race_id=35&eid=35",
    "visibility": "admin",        # hidden from non-admins
}

CLASSES = [
    {
        "id": "F",
        "name": "Class F",
        "start_time": local_iso("13:00:00"),
        "rating_system": RATING_SYSTEM,
        "rating_type": RATING_TYPE,
    },
]

# The one boat we have a track for.
BOAT = {
    "class": "F",
    "rating": 1.0156,
    "team_name": "Lubeck, Christopher",
    "boat_name": "Man of War",
    "sail_number": "32559",
    "boat_type": "C&C 41",
    "club": "Eastern Yacht Club",
    "loa_m": 12.55,               # C&C 41 LOA
    "finish_time": local_iso("14:01:06"),
    "finish_status": "FIN",
    "device_id": None,            # iPhone track, not a fleet device
    "session_path": None,
    "gpx_path": None,             # set by the GPX upload below
}


# ---------- HTTP helpers ----------

def _request(method, path, body=None):
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", "ignore")
        print(f"  HTTP {e.code} on {method} {url}: {body_txt}", file=sys.stderr)
        raise


def _multipart_upload(path, filename, file_bytes):
    """POST a single file as multipart/form-data with field name 'file'."""
    boundary = f"----sfboundary{uuid.uuid4().hex}"
    pre = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/gpx+xml\r\n\r\n"
    ).encode()
    post = f"\r\n--{boundary}--\r\n".encode()
    body = pre + file_bytes + post
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", "ignore")
        print(f"  HTTP {e.code} on POST {url}: {body_txt}", file=sys.stderr)
        raise


# ---------- Sensor Logger CSV → GPX ----------

def _ns_to_iso(ns_str):
    ts = int(ns_str) / 1e9
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def build_gpx(track_dir):
    """Read Sensor Logger Location.csv → GPX string. Points are sorted by
    time; rows without a valid fix (lat/lon 0 or blank) are skipped."""
    loc_path = f"{track_dir.rstrip('/')}/Location.csv"
    with open(loc_path, newline="") as f:
        rows = list(csv.DictReader(f))

    pts = []
    for r in rows:
        try:
            lat = float(r["latitude"])
            lon = float(r["longitude"])
        except (KeyError, ValueError):
            continue
        if lat == 0.0 and lon == 0.0:
            continue
        t = _ns_to_iso(r["time"])
        ele = r.get("altitudeAboveMeanSeaLevel") or r.get("altitude") or ""
        speed = r.get("speed") or ""
        pts.append((r["time"], t, lat, lon, ele, speed))

    pts.sort(key=lambda p: int(p[0]))

    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.write('<gpx version="1.1" creator="sailframes seed_eyc_annual_2026" '
              'xmlns="http://www.topografix.com/GPX/1/1">\n')
    out.write('<trk><name>Man of War — EYC Annual 2026</name><trkseg>\n')
    for _, t, lat, lon, ele, speed in pts:
        out.write(f'<trkpt lat="{lat:.7f}" lon="{lon:.7f}">')
        if ele != "":
            try:
                out.write(f"<ele>{float(ele):.2f}</ele>")
            except ValueError:
                pass
        out.write(f"<time>{t}</time>")
        if speed != "":
            try:
                out.write(f"<speed>{float(speed):.3f}</speed>")
            except ValueError:
                pass
        out.write("</trkpt>\n")
    out.write("</trkseg></trk></gpx>\n")
    return out.getvalue(), len(pts)


# ---------- Upsert helpers ----------

def find_or_create_regatta():
    _, data = _request("GET", "/api/regattas")
    for r in data.get("regattas", []):
        if r["name"] == REGATTA["name"]:
            print(f"  Reusing regatta {r['regatta_id']} — {r['name']}")
            # Re-assert visibility in case it was created before Lambda support.
            if r.get("visibility") != "admin":
                _request("PATCH", f"/api/regattas/{r['regatta_id']}", {"visibility": "admin"})
                print("    (patched visibility → admin)")
            return r["regatta_id"]
    print(f"  Creating hidden regatta: {REGATTA['name']}")
    _, created = _request("POST", "/api/regattas", REGATTA)
    if created.get("visibility") != "admin":
        print("    WARNING: Lambda dropped `visibility` — redeploy api_race, "
              "then re-run to hide this regatta.", file=sys.stderr)
    return created["regatta_id"]


def find_or_create_boat():
    sail = BOAT["sail_number"]
    _, data = _request("GET", f"/api/boats?sail_number={urllib.parse.quote(sail)}")
    matches = data.get("boats", [])
    skippers = [{"name": BOAT["team_name"], "photo": None}]
    if matches:
        boat_id = matches[0]["boat_id"]
        _request("PATCH", f"/api/boats/{boat_id}", {
            "name": BOAT["boat_name"], "type": BOAT["boat_type"],
            "loa_m": BOAT["loa_m"], "club": BOAT["club"], "skippers": skippers,
        })
        print(f"  Reusing boat {boat_id} — {BOAT['boat_name']} ({sail})")
        return boat_id
    _, created = _request("POST", "/api/boats", {
        "name": BOAT["boat_name"], "type": BOAT["boat_type"],
        "sail_number": sail, "club": BOAT["club"], "loa_m": BOAT["loa_m"],
        "skippers": skippers,
        "photos": {"boat": None, "skipper1": None, "skipper2": None},
        "links": [], "notes": "",
    })
    print(f"  Created boat {created['boat_id']} — {BOAT['boat_name']} ({sail})")
    return created["boat_id"]


def find_existing_race(regatta_id):
    _, data = _request("GET", f"/api/races?regatta_id={regatta_id}&date={RACE_DATE}")
    for r in data.get("races", []):
        if r.get("name") == "Race 1":
            return r["race_id"]
    return None


def main():
    track_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TRACK_DIR
    print("Seeding EYC Annual Regatta 2026 (HIDDEN) — Man of War only")

    regatta_id = find_or_create_regatta()
    boat_id = find_or_create_boat()

    entry = dict(BOAT)
    entry["boat_id"] = boat_id

    race_payload = {
        "name": "Race 1",
        "date": RACE_DATE,
        "start_time": local_iso("12:30:00"),   # first gun
        "end_time": local_iso("14:15:00"),
        "regatta_id": regatta_id,
        "classes": CLASSES,
        "race_conditions": "Sunny, hot, 5–8 kt",
        "boats": [entry],
    }

    existing = find_existing_race(regatta_id)
    if existing:
        print(f"  Updating race {existing}")
        _, prior = _request("GET", f"/api/races/{existing}")
        for b in prior.get("boats", []):
            if (b.get("sail_number") or "").strip() == BOAT["sail_number"]:
                if b.get("gpx_path"):
                    entry["gpx_path"] = b["gpx_path"]
        _, race = _request("PATCH", f"/api/races/{existing}", race_payload)
    else:
        print("  Creating race")
        _, race = _request("POST", "/api/races", race_payload)
    race_id = race["race_id"]

    print(f"  Building GPX from {track_dir} …")
    gpx_str, n = build_gpx(track_dir)
    print(f"  {n} track points → uploading …")
    status, res = _multipart_upload(
        f"/api/races/{race_id}/boats-by-id/{boat_id}/gpx",
        "man_of_war_2026-07-04.gpx",
        gpx_str.encode(),
    )
    print(f"  Track uploaded: {res.get('points')} points "
          f"({res.get('start_time')} → {res.get('end_time')})")

    print()
    print(f"✓ Hidden regatta seeded: {regatta_id}")
    print(f"  Race: {race_id}")
    print(f"  Dashboard (admins): https://sailframes.com/race.html?race={race_id}")


if __name__ == "__main__":
    main()
