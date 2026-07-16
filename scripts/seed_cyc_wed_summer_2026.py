#!/usr/bin/env python3
"""
Seed the 2026 Constitution YC Wednesday Evening SUMMER Series — Race 1
(2026-07-08). Public regatta (visibility defaults to public).

Multi-class handicap race, ORR-EZ ratings (W50/L50 - Light column this
week). Entries + finish/corrected times transcribed from the CYC
preliminary results sheet (regattaman-style). Four boats also have GPS
tracks recorded on phone apps; those are converted/uploaded and attached
by boat_id so they play back on race.html:

  - Pressure Drop (61430)  Open GPX Tracker  → GPX file
  - Agora (52475)          Sensor Logger     → Location.csv → GPX
  - Badger (220)           Waterspeed        → GPX file
  - Charisma (4396)        Sensor Logger     → Location.csv → GPX

Boats are matched to the catalog by sail_number and REUSED without
patching (they already exist from the Spring series — we don't clobber
their catalog skipper/LOA with a one-race substitute skipper). Per-race
skipper/rating/finish live on the race entry.

Idempotent: reuses the regatta (by name), boats (by sail_number), and
race (by regatta_id + date + name "Race 1"); re-uploads tracks (same S3
key). Preserves any device_id/session/gpx already set on a boat.

Requires the api_race Lambda deployed with classes/race_conditions
support (already live).

Usage:
    python3 scripts/seed_cyc_wed_summer_2026.py [TRACK_DIR]
"""

import csv
import glob
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

API_BASE = "https://rnngzx7flk.execute-api.us-east-1.amazonaws.com"

LOCAL_TO_UTC = timedelta(hours=4)          # 2026-07-08 is EDT = UTC-4
RACE_DATE = "2026-07-08"
RATING_SYSTEM = "ORR-EZ"
RATING_TYPE = "W50/L50 - Light"

DEFAULT_TRACK_DIR = "/Users/paul2/Library/CloudStorage/Dropbox-Personal/Documents/Sports/Sail/26-07-08"


def local_iso(hms: str) -> str:
    """'18:46:00' local EDT on RACE_DATE → UTC ISO 'Z'. Handles finish
    times after local midnight is not needed here (all same evening)."""
    y, m, d = map(int, RACE_DATE.split("-"))
    h, mi, s = map(int, hms.split(":"))
    dt = datetime(y, m, d, h, mi, s) + LOCAL_TO_UTC
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


REGATTA = {
    "name": "2026 Constitution YC Wednesday Evening Summer Series",
    "venue": "Boston Harbor — Constitution Yacht Club",
    "rating_system": RATING_SYSTEM,
    "start_date": RACE_DATE,
    "end_date": "2026-08-12",       # Race 6 tab date
}

CLASSES = [
    {"id": "A", "name": "Class A", "start_time": local_iso("18:46:00"),
     "rating_system": RATING_SYSTEM, "rating_type": RATING_TYPE, "race_len_nm": 4.20},
    {"id": "B", "name": "Class B", "start_time": local_iso("18:40:00"),
     "rating_system": RATING_SYSTEM, "rating_type": RATING_TYPE, "race_len_nm": 2.10},
]

# (class, team, yacht, club, sail, boat_type, rating, finish_hms|None, status)
ROWS = [
    # Class A — start 18:46:00, Course 3 · 2 laps, 4.20 nm
    ("A", "Alexander, Dave", "Pressure Drop", "Constitution YC", "61430", "Arcona 430", 0.589, "20:16:14", "FIN"),
    ("A", "Powers, David and Tom / Crimmins, Joe", "Agora", "New York YC / Constitution YC", "52475", "Beneteau 36.7", 0.566, "20:21:09", "FIN"),
    ("A", "Jacobson, William", "VANISH", "Constitution YC", "51613", "J/46 DK", 0.591, "20:23:42", "FIN"),
    ("A", "Pogue, Robert", "Never Settle", "Constitution YC", "USA 14", "J/92", 0.586, "20:30:41", "FIN"),
    ("A", "Isaacson, Peter", "Uproarious", "Constitution YC", "USA 78", "J/109", 0.574, "20:33:53", "FIN"),
    ("A", "Ryley, Lance", "RockIt 2.0", "", "52816", "Columbia 30-2 Sport", 0.621, "20:28:31", "FIN"),
    ("A", "Rudser, Jim", "Riot", "Constitution YC", "USA 40", "J/99", 0.582, "20:47:08", "FIN"),
    ("A", "Barmmer, Brian", "Saorsa", "Boston YC", "USA 1111", "J/109", 0.572, None, "DNC"),
    ("A", "McLean, Allan", "Eagle", "Constitution YC", "42359", "Frers 38", 0.581, None, "RET"),
    # Class B — start 18:40:00, Course 3 · 1 lap, 2.10 nm
    ("B", "De Souter, Marissa & Wafler, Garrett", "Special Sauce", "", "470", "J/30", 0.523, "19:24:05", "FIN"),
    ("B", "Long, III, James Gardner & Wagner, Ryan", "Badger", "Constitution YC", "220", "Sabre 34 MK1", 0.457, "19:43:55", "FIN"),
    ("B", "Dave DiLorenzo", "Amigo", "", "82", "J/80", 0.558, "19:34:01", "FIN"),
    ("B", "Dave DiLorenzo", "Wizard", "", "811", "J/80", 0.558, "19:35:00", "FIN"),
    ("B", "Phelps, Isaac", "Seabiscuit", "Constitution YC", "110", "Pearson 33-2", 0.473, "19:45:58", "FIN"),
    ("B", "Tubman, Richard", "Charisma", "Constitution YC", "4396", "Jeanneau Sun Odyssey 410", 0.514, "19:44:05", "FIN"),
    ("B", "Gordon Parris", "Katü", "Courageous SC", "484", "J/80", 0.558, None, "DNC"),
    ("B", "Conway, Ryan", "MASHNEE", "MIT Nautical Assoc.", "7", "Buzzards Bay 30", 0.547, None, "DNC"),
    ("B", "DiLorenzo, Dave & Sailing, Courageous", "Doc Buck", "Courageous SC", "88", "J/80", 0.558, None, "DNC"),
]

BOAT_TYPE_LOA = {
    "J/92": 9.14, "J/80": 8.00, "J/30": 9.14, "J/99": 9.99, "J/109": 10.81,
    "J/46 DK": 14.02, "Arcona 430": 13.10, "Beneteau 36.7": 11.20,
    "Columbia 30-2 Sport": 9.14, "Frers 38": 11.58, "Buzzards Bay 30": 9.14,
    "Pearson 33-2": 10.06, "Jeanneau Sun Odyssey 410": 12.45, "Sabre 34 MK1": 10.36,
}

# sail_number → ('gpx', glob) for ready-made GPX, or ('sl', glob) for a
# Sensor Logger folder (Location.csv). Globs are resolved under TRACK_DIR.
TRACKS = {
    "61430": ("gpx", "Presure drop*.gpx"),          # Open GPX Tracker
    "52475": ("sl",  "Agora*"),                      # Sensor Logger
    "220":   ("gpx", "Badger*.gpx"),                 # Waterspeed
    "4396":  ("sl",  "Charisma*"),                   # Sensor Logger
    "82":    ("gpx", "Amigo*Navionics*.gpx"),        # Navionics
    "470":   ("gpx", "Special Sauce*Navionics*.gpx"),  # Navionics
}


# ---------- HTTP ----------

def _request(method, path, body=None):
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} on {method} {url}: {e.read().decode('utf-8','ignore')}", file=sys.stderr)
        raise


def _multipart_upload(path, filename, file_bytes):
    boundary = f"----sfboundary{uuid.uuid4().hex}"
    pre = (f"--{boundary}\r\n"
           f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
           f"Content-Type: application/gpx+xml\r\n\r\n").encode()
    body = pre + file_bytes + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{API_BASE}{path}", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} on POST {path}: {e.read().decode('utf-8','ignore')}", file=sys.stderr)
        raise


# ---------- Sensor Logger CSV → GPX ----------

def _ns_to_iso(ns_str):
    dt = datetime.fromtimestamp(int(ns_str) / 1e9, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def sensorlogger_to_gpx(track_dir, name):
    with open(f"{track_dir.rstrip('/')}/Location.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    pts = []
    for r in rows:
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except (KeyError, ValueError):
            continue
        if lat == 0.0 and lon == 0.0:
            continue
        pts.append((int(r["time"]), _ns_to_iso(r["time"]), lat, lon,
                    r.get("altitudeAboveMeanSeaLevel") or r.get("altitude") or "",
                    r.get("speed") or ""))
    pts.sort(key=lambda p: p[0])
    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.write('<gpx version="1.1" creator="sailframes seed_cyc_wed_summer_2026" '
              'xmlns="http://www.topografix.com/GPX/1/1">\n')
    out.write(f'<trk><name>{name}</name><trkseg>\n')
    for _, t, lat, lon, ele, speed in pts:
        out.write(f'<trkpt lat="{lat:.7f}" lon="{lon:.7f}">')
        if ele != "":
            try: out.write(f"<ele>{float(ele):.2f}</ele>")
            except ValueError: pass
        out.write(f"<time>{t}</time>")
        if speed != "":
            try: out.write(f"<speed>{float(speed):.3f}</speed>")
            except ValueError: pass
        out.write("</trkpt>\n")
    out.write("</trkseg></trk></gpx>\n")
    return out.getvalue().encode(), len(pts)


# ---------- Upsert ----------

def find_or_create_regatta():
    _, data = _request("GET", "/api/regattas")
    for r in data.get("regattas", []):
        if r["name"] == REGATTA["name"]:
            print(f"  Reusing regatta {r['regatta_id']}")
            return r["regatta_id"]
    print(f"  Creating regatta: {REGATTA['name']}")
    _, created = _request("POST", "/api/regattas", REGATTA)
    return created["regatta_id"]


def _split_skippers(team):
    parts = [p.strip() for p in re.split(r"\s*&\s*|\s+and\s+|\s*/\s*", team) if p.strip()]
    return [{"name": p, "photo": None} for p in parts[:2]]


def find_or_create_boat(row):
    _, team, yacht, club, sail, btype, *_ = row
    sail = sail.strip()
    _, data = _request("GET", f"/api/boats?sail_number={urllib.parse.quote(sail)}")
    matches = data.get("boats", [])
    if matches:
        # Reuse without clobbering existing catalog identity.
        return matches[0]["boat_id"]
    _, created = _request("POST", "/api/boats", {
        "name": yacht, "type": btype, "sail_number": sail, "club": club,
        "loa_m": BOAT_TYPE_LOA.get(btype), "skippers": _split_skippers(team),
        "photos": {"boat": None, "skipper1": None, "skipper2": None},
        "links": [], "notes": "",
    })
    print(f"    + created catalog boat {yacht} ({sail})")
    return created["boat_id"]


def find_existing_race(regatta_id):
    _, data = _request("GET", f"/api/races?regatta_id={regatta_id}&date={RACE_DATE}")
    for r in data.get("races", []):
        if r.get("name") == "Race 1":
            return r["race_id"]
    return None


def resolve_track(track_dir, glob_pat):
    hits = glob.glob(f"{track_dir.rstrip('/')}/{glob_pat}")
    return hits[0] if hits else None


def main():
    track_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TRACK_DIR
    print("Seeding CYC Wednesday Evening SUMMER Series — Race 1 (2026-07-08)")

    regatta_id = find_or_create_regatta()

    boats = []
    for row in ROWS:
        cls, team, yacht, club, sail, btype, rating, finish, status = row
        bid = find_or_create_boat(row)
        boats.append({
            "boat_id": bid, "device_id": None, "class": cls, "rating": rating,
            "team_name": team, "boat_name": yacht, "sail_number": sail,
            "boat_type": btype, "club": club, "loa_m": BOAT_TYPE_LOA.get(btype),
            "finish_time": local_iso(finish) if finish else None,
            "finish_status": status, "session_path": None, "gpx_path": None,
        })

    race_payload = {
        "name": "Race 1", "date": RACE_DATE,
        "start_time": local_iso("18:30:00"),   # first gun
        "end_time": local_iso("21:15:00"),
        "regatta_id": regatta_id, "classes": CLASSES,
        "race_conditions": "4–8 kt SE", "boats": boats,
    }

    existing = find_existing_race(regatta_id)
    if existing:
        print(f"  Updating race {existing}")
        _, prior = _request("GET", f"/api/races/{existing}")
        prior_by_sail = {(b.get("sail_number") or "").strip(): b for b in prior.get("boats", [])}
        for b in race_payload["boats"]:
            old = prior_by_sail.get(b["sail_number"])
            if not old:
                continue
            if old.get("device_id"):    b["device_id"] = old["device_id"]
            if old.get("session_path"): b["session_path"] = old["session_path"]
            if old.get("gpx_path"):     b["gpx_path"] = old["gpx_path"]
        _, race = _request("PATCH", f"/api/races/{existing}", race_payload)
    else:
        print("  Creating race")
        _, race = _request("POST", "/api/races", race_payload)
    race_id = race["race_id"]

    # Attach GPS tracks
    by_sail = {b["sail_number"]: b["boat_id"] for b in boats}
    for sail, (kind, pat) in TRACKS.items():
        boat_id = by_sail.get(sail)
        src = resolve_track(track_dir, pat)
        if not boat_id or not src:
            print(f"  ! track for sail {sail}: boat_id={boat_id} src={src} — skipped", file=sys.stderr)
            continue
        if kind == "gpx":
            with open(src, "rb") as f:
                gpx_bytes = f.read()
            fname = src.split("/")[-1]
        else:
            gpx_bytes, n = sensorlogger_to_gpx(src, sail)
            fname = f"{sail}.gpx"
        _, res = _multipart_upload(
            f"/api/races/{race_id}/boats-by-id/{boat_id}/gpx", fname, gpx_bytes)
        print(f"  track {sail}: {res.get('points')} pts "
              f"({res.get('start_time')} → {res.get('end_time')})")

    fin = sum(1 for r in ROWS if r[8] == "FIN")
    print(f"\n✓ Regatta {regatta_id} · Race {race_id}")
    print(f"  https://sailframes.com/race.html?race={race_id}")
    print(f"  {len(ROWS)} boats ({fin} finishers) · 4 GPS tracks attached")


if __name__ == "__main__":
    main()
