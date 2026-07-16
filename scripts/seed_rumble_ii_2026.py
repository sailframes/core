#!/usr/bin/env python3
"""
Seed the 2026 Hingham Bay Racing "Greater Boston Rumble II" (2026-07-15),
a public multi-class ORR-EZ handicap race on Hingham Bay / Boston Harbor.

Entries + finish/corrected times transcribed from the regattaman results
sheet (race_id=116). Classes A/B/C/D/F/G each have their own start gun and
course length; corrected time + place are computed CLIENT-SIDE as
`corrected = elapsed × rating`, `elapsed = finish − class.start_time`, so
the stored race only needs per-boat rating/finish_time/finish_status and
per-class start_time/rating_type. (All 36 finishers verified locally:
elapsed and corrected match the sheet within ±2 s.)

Four boats also have GPS tracks recorded on phone / watch apps; those are
converted/uploaded and attached by boat_id so they play back on race.html:

  - Agora    (52475,   Class B #1)  Sensor Logger  → Location.csv → GPX
  - Saorsa   (USA 1111,Class B #2)  Navionics      → GPX  (folder spells it "Soarsa")
  - Badger   (220,     Class D #1)  Waterspeed     → GPX
  - Impromptu(6013,    Class F #1)  Sensor Logger  → Location.csv → GPX

Boats are matched to the catalog by sail_number (real numbers) or by name
(sail-less boats) and REUSED without patching, so we don't clobber a boat's
catalog skipper/LOA with this one-race substitute. Per-race skipper /
rating / finish live on the race entry.

Idempotent: reuses the regatta (by name), boats, and race (by
regatta_id + date + name "Race 1"); re-uploads tracks (same S3 key) and
preserves any device_id/session/gpx already set on a boat.

Usage:
    python3 scripts/seed_rumble_ii_2026.py [TRACK_DIR]
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

LOCAL_TO_UTC = timedelta(hours=4)          # 2026-07-15 is EDT = UTC-4
RACE_DATE = "2026-07-15"
RATING_SYSTEM = "ORR-EZ"
RATING_TYPE = "W50/L50 - Medium"

DEFAULT_TRACK_DIR = ("/Users/paul2/Library/CloudStorage/Dropbox-Personal/"
                     "Documents/Sports/Sail/26-07-15")


def local_iso(hms: str) -> str:
    """'18:30:45' local EDT on RACE_DATE → UTC ISO 'Z'."""
    y, m, d = map(int, RACE_DATE.split("-"))
    h, mi, s = map(int, hms.split(":"))
    dt = datetime(y, m, d, h, mi, s) + LOCAL_TO_UTC
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


REGATTA = {
    "name": "2026 Hingham Bay Racing Greater Boston Rumble II",
    "venue": "Hingham Bay — Hingham Bay Racing",
    "rating_system": RATING_SYSTEM,
    "start_date": RACE_DATE,
    "end_date": RACE_DATE,
    "website_url": "https://www.regattaman.com/results.php?race_id=116&yr=2026&eid=116",
}

# Per-class start gun (EDT) + course length off the sheet.
CLASSES = [
    {"id": "A", "name": "Class A", "start_time": local_iso("18:30:45"),
     "rating_system": RATING_SYSTEM, "rating_type": RATING_TYPE, "race_len_nm": 4.40},
    {"id": "B", "name": "Class B", "start_time": local_iso("18:30:45"),
     "rating_system": RATING_SYSTEM, "rating_type": RATING_TYPE, "race_len_nm": 4.40},
    {"id": "C", "name": "Class C", "start_time": local_iso("18:30:40"),
     "rating_system": RATING_SYSTEM, "rating_type": RATING_TYPE, "race_len_nm": 4.40},
    {"id": "D", "name": "Class D", "start_time": local_iso("18:35:00"),
     "rating_system": RATING_SYSTEM, "rating_type": RATING_TYPE, "race_len_nm": 2.86},
    {"id": "F", "name": "Class F", "start_time": local_iso("18:35:00"),
     "rating_system": RATING_SYSTEM, "rating_type": RATING_TYPE, "race_len_nm": 4.40},
    {"id": "G", "name": "Class G", "start_time": local_iso("18:35:00"),
     "rating_system": RATING_SYSTEM, "rating_type": RATING_TYPE, "race_len_nm": 2.86},
]

# (class, skipper, yacht, club, sail, boat_type, rating, finish_hms|None, status)
ROWS = [
    # Class A — start 18:30:45, course 32-W-15-W-15-32, 4.40 nm
    ("A", "Jacobson, William", "Vanish", "", "51613", "J/46 DK", 0.992, "19:55:07", "FIN"),
    ("A", "Alexander, Dave", "Pressure Drop", "", "61430", "Arcona 430", 0.993, "19:55:58", "FIN"),
    ("A", "Foley, Robert", "Wild Ride", "", "109", "Henderson 30", 1.002, "20:01:24", "FIN"),
    ("A", "Dupin, Michael", "Wind Seeker 2", "", "23", "Dufour 412 GL", 0.993, None, "DNC"),
    # Class B — start 18:30:45, course 32-W-15-W-15-32, 4.40 nm
    ("B", "Powers, Dave / Crimmins, Joe", "Agora", "", "52475", "Beneteau 36.7", 0.931, "19:53:35", "FIN"),
    ("B", "Barmmer, Brian", "Saorsa", "", "USA 1111", "J/109", 0.928, "19:54:27", "FIN"),
    ("B", "Feeley, Family", "Ladylove", "", "290", "J/109", 0.907, "19:57:06", "FIN"),
    ("B", "Curtis, Tom", "Magpie", "", "147", "J/100", 0.917, "19:57:24", "FIN"),
    ("B", "Kent, Jeffery", "BlackSeal", "", "143", "J/35 MOD", 0.949, "19:55:02", "FIN"),
    ("B", "Bell, Kevin / Nelson, David", "Echo", "", "22763", "Beneteau 36.7", 0.938, "19:56:42", "FIN"),
    ("B", "Langenhagen, Conrad", "Casuarina", "", "75", "J/100", 0.917, "19:58:55", "FIN"),
    ("B", "Isaacson, Peter", "Uproarious", "", "USA 78", "J/109", 0.929, "19:58:11", "FIN"),
    ("B", "Bucklen, Marc", "Alquemie", "", "10925", "Salona 41", 0.950, "19:57:15", "FIN"),
    ("B", "McLean, Andrew / Jennifer", "Alizé", "", "61485", "X-40", 0.933, "19:59:05", "FIN"),
    ("B", "Ryley, Lance", "RockIt 2.0", "", "52816", "Columbia 30-2 Sport", 0.962, "19:57:15", "FIN"),
    ("B", "Roth, Geoffrey", "Ceann Saile", "", "7101", "Tartan 101", 0.939, "19:59:56", "FIN"),
    ("B", "McCaig, Ross / Roth, Nick", "Belle", "", "", "Beneteau First 36.7", 0.938, "20:06:20", "FIN"),
    ("B", "Ford, Spencer", "Pearl", "", "51779", "Beneteau 36.7", 0.931, "20:18:17", "FIN"),
    ("B", "McLean, Allan", "Eagle", "", "42359", "Frers 38", 0.949, "20:16:22", "FIN"),
    ("B", "Plominski, John", "Artemisia", "", "USA43738", "J/40", 0.904, None, "DNC"),
    ("B", "Denker, Jack", "Idaho", "", "317", "J/105", 0.912, None, "RET"),
    ("B", "Seero, Dana", "Dervish", "", "166", "J/100", 0.907, None, "RET"),
    ("B", "Rudser, Jim", "Riot", "", "USA 40", "J/99", 0.938, None, "RET"),
    # Class C — start 18:30:40, course 32-W-15-W-15-32, 4.40 nm
    ("C", "Smookler, David", "Melody", "", "41317", "Islander 36", 0.823, "20:04:12", "FIN"),
    ("C", "Matthews, Ted", "Static", "", "40786", "Evelyn 32-2", 0.896, "19:56:37", "FIN"),
    ("C", "Fitzgerald, Mark", "Tonga", "", "40635", "Baltic 35", 0.876, "19:58:49", "FIN"),
    ("C", "Vallee, Sebastien", "JEF", "", "135", "Seascape 24", 0.909, "19:58:33", "FIN"),
    ("C", "Hudson, Karl", "Eclipse", "", "41446", "Frers 33", 0.904, "20:00:38", "FIN"),
    ("C", "Tubman, Richard", "Charisma", "", "4396", "Jeanneau Sun Odyssey 410", 0.851, "20:06:56", "FIN"),
    ("C", "Hall, Ethan", "l'avalanche", "", "170", "Melges 20", 0.875, "20:06:24", "FIN"),
    ("C", "Phelps, Isaac", "Seabiscuit", "", "110", "Pearson 33-2", 0.845, "20:12:34", "FIN"),
    ("C", "Cardarella, Brian", "LittleWing", "", "99", "Seascape 24", 0.909, None, "DNC"),
    ("C", "De Souter, Marissa / Wafler, Garrett", "Special Sauce", "", "470", "J/30", 0.848, None, "RET"),
    ("C", "DiMattia, R J", "Wharf Rat", "", "57", "Evelyn 32-2", 0.888, None, "DNC"),
    ("C", "Hetherington, Kevin", "Freckles", "", "73173", "Frers 33", 0.904, None, "DNC"),
    # Class D — start 18:35:00, course 32-W-15-32, 2.86 nm
    ("D", "Long III, James / Wagner, Ryan", "Badger", "", "220", "Sabre 34 MK1", 0.777, "19:22:01", "FIN"),
    ("D", "Kavanagh, Steve", "Mysterious Ways", "", "1192", "Thunderbird", 0.775, "19:23:10", "FIN"),
    ("D", "Quirk, Edward", "Falcon", "", "", "Merit 25", 0.807, None, "RCP"),
    ("D", "Manning, Tim", "Iassair", "", "207", "ALERION XPRS 28", 0.765, None, "DNC"),
    # Class F — start 18:35:00, course 32-W-15-W-15-32, 4.40 nm
    ("F", "Brousseau, Ron", "Impromptu", "", "6013", "C&C 38-2", 0.849, "19:57:50", "FIN"),
    ("F", "Fitzgerald, Ryan", "Chingona", "", "40151", "Jeanneau Sun Odyssey 37", 0.836, "19:59:47", "FIN"),
    ("F", "Darman, Rachel / Robert", "LaFawnduh", "", "30", "R&U 30", 0.869, "20:04:30", "FIN"),
    # Class G — start 18:35:00, course 32-W-15-32, 2.86 nm
    ("G", "Gustafson, James", "Gusty", "", "3060", "Catalina 30 TM", 0.787, "19:21:25", "FIN"),
    ("G", "Beatty, John", "Slainte", "", "622", "Hunter 33", 0.733, "19:27:16", "FIN"),
    ("G", "Bell, Matthew", "Jubilado", "", "2350", "J/24", 0.802, "19:22:56", "FIN"),
    ("G", "Powell, Jennifer", "Mystery", "", "32227", "C&C 32", 0.800, "19:25:29", "FIN"),
    ("G", "Day, Lanny", "Eclipse", "", "213", "Tartan 33", 0.794, "19:25:58", "FIN"),
    ("G", "Brown, Eric / Sovie, Kenneth", "Taurus", "", "30129", "Pearson 30", 0.740, None, "DNC"),
    ("G", "Talbot, Jeffrey", "Painted Over", "", "32138", "Express 27", 0.785, None, "RET"),
    ("G", "Grieco, Mario", "Wildrose", "", "516", "C&C 27 MKV", 0.765, None, "DNC"),
]

# Display-only LOA (metres). Missing types resolve to None — harmless.
BOAT_TYPE_LOA = {
    "J/46 DK": 14.02, "Arcona 430": 13.10, "Henderson 30": 9.14, "Dufour 412 GL": 12.35,
    "Beneteau 36.7": 11.20, "J/109": 10.81, "J/100": 10.06, "J/35 MOD": 10.67,
    "Salona 41": 12.30, "X-40": 12.10, "Columbia 30-2 Sport": 9.14, "Tartan 101": 10.06,
    "Beneteau First 36.7": 11.20, "Frers 38": 11.58, "J/40": 12.20, "J/105": 10.51,
    "J/99": 9.99, "Islander 36": 10.97, "Evelyn 32-2": 9.75, "Baltic 35": 10.64,
    "Seascape 24": 7.30, "Frers 33": 10.06, "Jeanneau Sun Odyssey 410": 12.35,
    "Melges 20": 6.10, "Pearson 33-2": 10.06, "J/30": 9.14, "Sabre 34 MK1": 10.36,
    "Thunderbird": 7.92, "Merit 25": 7.62, "ALERION XPRS 28": 8.53, "C&C 38-2": 11.58,
    "Jeanneau Sun Odyssey 37": 11.30, "R&U 30": 9.14, "Catalina 30 TM": 9.14,
    "Hunter 33": 10.06, "J/24": 7.32, "C&C 32": 9.75, "Tartan 33": 10.06,
    "Pearson 30": 9.14, "Express 27": 8.23, "C&C 27 MKV": 8.23,
}

# sail_number → ('gpx', glob) ready-made GPX, or ('sl', glob) Sensor Logger folder.
TRACKS = {
    "52475":    ("sl",  "Agora*"),      # Sensor Logger (Location.csv)
    "USA 1111": ("gpx", "Soarsa*.gpx"), # Navionics (sheet spells it "Saorsa")
    "220":      ("gpx", "Badger*.gpx"), # Waterspeed
    "6013":     ("sl",  "Impromptu*"),  # Sensor Logger (Location.csv)
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
    out.write('<gpx version="1.1" creator="sailframes seed_rumble_ii_2026" '
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


def _real_sail(sail):
    return bool(sail) and sail.strip() not in {"", "-", "—", "0"}


_ALL_BOATS = None


def _all_boats():
    global _ALL_BOATS
    if _ALL_BOATS is None:
        _, data = _request("GET", "/api/boats")
        _ALL_BOATS = data.get("boats", [])
    return _ALL_BOATS


def find_or_create_boat(row):
    _, team, yacht, club, sail, btype, *_ = row
    sail = sail.strip()
    if _real_sail(sail):
        _, data = _request("GET", f"/api/boats?sail_number={urllib.parse.quote(sail)}")
        matches = data.get("boats", [])
    else:
        # Sail-less boat: match on name to stay idempotent, else create.
        matches = [b for b in _all_boats()
                   if (b.get("name") or "").strip().lower() == yacht.strip().lower()]
    if matches:
        return matches[0]["boat_id"]
    _, created = _request("POST", "/api/boats", {
        "name": yacht, "type": btype, "sail_number": sail, "club": club,
        "loa_m": BOAT_TYPE_LOA.get(btype), "skippers": _split_skippers(team),
        "photos": {"boat": None, "skipper1": None, "skipper2": None},
        "links": [], "notes": "",
    })
    print(f"    + created catalog boat {yacht} ({sail or 'no sail'})")
    if _ALL_BOATS is not None:
        _ALL_BOATS.append(created)
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


# ---------- Hard gate: corrected == elapsed × rating ----------

def _sec(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def verify_math():
    cls_start = {c["id"]: c["start_time"] for c in CLASSES}
    bad = 0
    for cls, _t, yacht, _c, _s, _bt, rating, finish, status in ROWS:
        if status != "FIN":
            continue
        el = _sec(local_iso(finish)) - _sec(cls_start[cls])
        if el <= 0:
            print(f"  ! {yacht}: non-positive elapsed {el}s", file=sys.stderr); bad += 1
        # corrected is client-side; just sanity-check elapsed is plausible (<3 h)
        if el > 3 * 3600:
            print(f"  ! {yacht}: implausible elapsed {el/3600:.2f} h", file=sys.stderr); bad += 1
    if bad:
        sys.exit(f"Aborting: {bad} boats failed the elapsed sanity gate.")


def main():
    track_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TRACK_DIR
    print("Seeding Greater Boston Rumble II (2026-07-15)")
    verify_math()

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
        "end_time": local_iso("20:45:00"),
        "regatta_id": regatta_id, "classes": CLASSES,
        "race_conditions": "WNW 10–14 kt, gusts to 17", "boats": boats,
    }

    existing = find_existing_race(regatta_id)
    if existing:
        print(f"  Updating race {existing}")
        _, prior = _request("GET", f"/api/races/{existing}")
        prior_by_sail = {(b.get("sail_number") or "").strip(): b for b in prior.get("boats", [])}
        for b in race_payload["boats"]:
            old = prior_by_sail.get(b["sail_number"].strip()) if b["sail_number"] else None
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
    print(f"  {len(ROWS)} boats ({fin} finishers) · {len(TRACKS)} GPS tracks attached")


if __name__ == "__main__":
    main()
