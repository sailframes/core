#!/usr/bin/env python3
"""
Seed the 2026 Constitution YC Wednesday Evening SUMMER Series — Race 3
(2026-07-22). Reuses the existing summer regatta (8954995d) + boats (by
sail_number, from Races 1/2). ORR-EZ ratings, "Lt/Med" column this week
(S to SW 8 kt). Entries + finish/corrected transcribed from the CYC
preliminary results sheet. Two GPS tracks (Sensor Logger) attached:

  - Agora (52475)     Sensor Logger → Location.csv → GPX
  - Charisma (4396)   Sensor Logger → Location.csv → GPX

Idempotent: reuses regatta/boats; creates or PATCHes the race
(regatta_id + date + name "Race 3"); re-uploads tracks (same S3 key);
preserves any device_id/session/gpx already on a boat.

Usage:  python3 scripts/seed_cyc_wed_summer_race3.py [TRACK_DIR]
"""

import csv, glob, io, json, re, sys
import urllib.error, urllib.parse, urllib.request, uuid
from datetime import datetime, timedelta, timezone

API_BASE = "https://rnngzx7flk.execute-api.us-east-1.amazonaws.com"

LOCAL_TO_UTC = timedelta(hours=4)          # 2026-07-22 is EDT = UTC-4
RACE_DATE = "2026-07-22"
RACE_NAME = "Race 3"
RATING_SYSTEM = "ORR-EZ"
RATING_TYPE = "W50/L50 - Lt/Med"

DEFAULT_TRACK_DIR = "/Users/paul2/Library/CloudStorage/Dropbox-Personal/Documents/Sports/Sail/26-07-22"


def local_iso(hms: str) -> str:
    y, m, d = map(int, RACE_DATE.split("-"))
    h, mi, s = map(int, hms.split(":"))
    dt = datetime(y, m, d, h, mi, s) + LOCAL_TO_UTC
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


REGATTA = {
    "name": "2026 Constitution YC Wednesday Evening Summer Series",
    "venue": "Boston Harbor — Constitution Yacht Club",
    "rating_system": RATING_SYSTEM,
    "start_date": "2026-07-08",
    "end_date": "2026-08-12",
}

# First gun 18:30; Class B starts 18:35, Class A 18:41. Both Course 3, 4.20 nm.
CLASSES = [
    {"id": "A", "name": "Class A", "start_time": local_iso("18:41:00"),
     "rating_system": RATING_SYSTEM, "rating_type": RATING_TYPE, "race_len_nm": 4.20},
    {"id": "B", "name": "Class B", "start_time": local_iso("18:35:00"),
     "rating_system": RATING_SYSTEM, "rating_type": RATING_TYPE, "race_len_nm": 4.20},
]

# (class, team, yacht, club, sail, boat_type, rating, finish_hms|None, status)
ROWS = [
    # Class A — start 18:41:00, Course 3, 4.20 nm
    ("A", "Jacobson, William", "VANISH", "Constitution YC", "51613", "J/46 DK", 0.771, "19:28:10", "FIN"),
    ("A", "Isaacson, Peter", "Uproarious", "Constitution YC", "USA 78", "J/109", 0.739, "19:32:10", "FIN"),
    ("A", "Rudser, Jim", "Riot", "Constitution YC", "USA 40", "J/99", 0.748, "19:33:44", "FIN"),
    ("A", "Ryley, Lance", "RockIt 2.0", "", "52816", "Columbia 30-2 Sport", 0.788, "19:31:20", "FIN"),
    ("A", "Powers, David and Tom / Crimmins, Joe", "Agora", "New York YC / Constitution YC", "52475", "Beneteau 36.7", 0.734, "19:37:03", "FIN"),
    ("A", "Pogue, Robert", "Never Settle", "Constitution YC", "USA 14", "J/92", 0.743, "19:37:42", "FIN"),
    ("A", "McLean, Allan", "Eagle", "Constitution YC", "42359", "Frers 38", 0.751, "19:47:21", "FIN"),
    ("A", "Alexander, Dave", "Pressure Drop", "Constitution YC", "61430", "Arcona 430", 0.773, None, "DNC"),
    ("A", "Barmmer, Brian", "Saorsa", "Boston YC", "USA 1111", "J/109", 0.737, None, "DNC"),
    # Class B — start 18:35:00, Course 3, 4.20 nm
    ("B", "Tubman, Richard", "Charisma", "Constitution YC", "4396", "Jeanneau Sun Odyssey 410", 0.667, "19:37:59", "FIN"),
    ("B", "Conway, Ryan", "MASHNEE", "MIT Nautical Assoc.", "7", "Buzzards Bay 30 MOD", 0.671, "19:39:58", "FIN"),
    ("B", "De Souter, Marissa & Wafler, Garrett", "Special Sauce", "", "470", "J/30", 0.673, "19:40:03", "FIN"),
    ("B", "DiLorenzo, Dave & Sailing, Courageous", "Doc Buck", "Courageous SC", "88", "J/80", 0.708, "19:37:20", "FIN"),
    ("B", "Dave DiLorenzo", "Wizard", "", "811", "J/80", 0.708, "19:37:27", "FIN"),
    ("B", "Dave DiLorenzo", "Amigo", "", "82", "J/80", 0.708, "19:37:37", "FIN"),
    ("B", "Phelps, Isaac", "Seabiscuit", "Constitution YC", "110", "Pearson 33-2", 0.630, "19:47:54", "FIN"),
    ("B", "Gordon Parris", "Katü", "Courageous SC", "484", "J/80", 0.708, None, "DNC"),
    ("B", "Long, III, James Gardner & Wagner, Ryan", "Badger", "Constitution YC", "220", "Sabre 34 MK1", 0.599, None, "DNC"),
]

BOAT_TYPE_LOA = {
    "J/92": 9.14, "J/80": 8.00, "J/30": 9.14, "J/99": 9.99, "J/109": 10.81,
    "J/46 DK": 14.02, "Arcona 430": 13.10, "Beneteau 36.7": 11.20,
    "Columbia 30-2 Sport": 9.14, "Frers 38": 11.58, "Buzzards Bay 30 MOD": 9.14,
    "Pearson 33-2": 10.06, "Jeanneau Sun Odyssey 410": 12.45, "Sabre 34 MK1": 10.36,
}

TRACKS = {
    "52475": ("sl",  "Agora*"),                        # Sensor Logger
    "4396":  ("sl",  "Charisma*"),                     # Sensor Logger
    "470":   ("gpx", "Special Sauce*Navionics*.gpx"),  # Navionics export
    "52816": ("vkx", "RockIt*.vkx"),                   # Vakaros (3.1 MB, under the 6 MB multipart limit)
}


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


def _multipart_upload(path, filename, file_bytes, part_ctype="application/gpx+xml"):
    boundary = f"----sfboundary{uuid.uuid4().hex}"
    pre = (f"--{boundary}\r\n"
           f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
           f"Content-Type: {part_ctype}\r\n\r\n").encode()
    body = pre + file_bytes + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{API_BASE}{path}", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.status, json.loads(resp.read())


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
    out.write('<gpx version="1.1" creator="sailframes seed_cyc_wed_summer_race3" '
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
        if r.get("name") == RACE_NAME:
            return r["race_id"]
    return None


def resolve_track(track_dir, glob_pat):
    hits = glob.glob(f"{track_dir.rstrip('/')}/{glob_pat}")
    return hits[0] if hits else None


def main():
    track_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TRACK_DIR
    print(f"Seeding CYC Wednesday Evening SUMMER Series — {RACE_NAME} ({RACE_DATE})")

    regatta_id = find_or_create_regatta()

    boats = []
    for row in ROWS:
        cls, team, yacht, club, sail, btype, rating, finish, status = row
        bid = find_or_create_boat(row)
        boats.append({
            "boat_id": bid, "device_id": None, "class": cls, "rating": rating,
            "team_name": team, "boat_name": yacht, "sail_number": sail.strip(),
            "boat_type": btype, "club": club, "loa_m": BOAT_TYPE_LOA.get(btype),
            "finish_time": local_iso(finish) if finish else None,
            "finish_status": status, "session_path": None, "gpx_path": None,
        })

    race_payload = {
        "name": RACE_NAME, "date": RACE_DATE,
        "start_time": local_iso("18:30:00"),   # first gun
        "end_time": local_iso("20:15:00"),
        "regatta_id": regatta_id, "classes": CLASSES,
        "race_conditions": "S to SW 8 kt", "boats": boats,
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

    by_sail = {b["sail_number"]: b["boat_id"] for b in boats}
    for sail, (kind, pat) in TRACKS.items():
        boat_id = by_sail.get(sail)
        src = resolve_track(track_dir, pat)
        if not boat_id or not src:
            print(f"  ! track for sail {sail}: boat_id={boat_id} src={src} — skipped", file=sys.stderr)
            continue
        endpoint, ctype = "gpx", "application/gpx+xml"
        if kind == "gpx":
            with open(src, "rb") as f:
                file_bytes = f.read()
            fname = src.split("/")[-1]
        elif kind == "vkx":                                    # Vakaros — raw .vkx to the /vkx endpoint
            with open(src, "rb") as f:
                file_bytes = f.read()
            fname, endpoint, ctype = f"{sail}.vkx", "vkx", "application/octet-stream"
        else:                                                  # Sensor Logger folder -> GPX
            file_bytes, n = sensorlogger_to_gpx(src, sail)
            fname = f"{sail}.gpx"
        _, res = _multipart_upload(
            f"/api/races/{race_id}/boats-by-id/{boat_id}/{endpoint}", fname, file_bytes, ctype)
        print(f"  track {sail}: {res.get('points')} pts "
              f"({res.get('start_time')} → {res.get('end_time')})")

    fin = sum(1 for r in ROWS if r[8] == "FIN")
    print(f"\n✓ Regatta {regatta_id} · Race {race_id}")
    print(f"  https://sailframes.com/race.html?race={race_id}")
    print(f"  {len(ROWS)} boats ({fin} finishers) · {len(TRACKS)} GPS tracks attached")


if __name__ == "__main__":
    main()
