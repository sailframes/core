#!/usr/bin/env python3
"""
Add Race 6 (2026-07-01) to the 2026 Constitution YC Wednesday Evening
Spring Series (regatta bcfc077b). Data from the Preliminary Results
sheet (Race 6 7/1); rating type is RANDOM LEG - Medium (this week's
column — different from earlier weeks' W50/L50).

GPS trackers this race: Katu->B2, Amigo->B1, Doc Buck->E4, Wizard->E3.

Idempotent: matches the existing "Race 6" on the regatta+date and
PATCHes it, else POSTs a new one. Boat catalog ids are taken from the
live catalog (no re-create); per-race skipper rides on team_name.

HARD GATE: every finisher's computed corrected time (elapsed x rating,
rounded to the second) must equal the sheet's corrected time, or the
script aborts before writing anything.
"""
import json, sys, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

API = "https://rnngzx7flk.execute-api.us-east-1.amazonaws.com"
REGATTA_ID = "bcfc077b"
RACE_NAME = "Race 6"
RACE_DATE = "2026-07-01"           # EDT (UTC-4)
LOCAL_TO_UTC = timedelta(hours=4)
RATING_SYSTEM = "ORR-EZ"
RATING_TYPE = "RANDOM LEG - Medium"
RACE_LEN_NM = 6.00
CONDITIONS = "SSW 12 kts"
COURSE = "S/F > M > S/F"

CLASS_START = {"A": "18:51:00", "B": "18:45:00"}
FIRST_GUN = "18:40:00"

# GPS track linking: device_id -> processed session_path (processed/<dev>/<session>/gps.json).
# The dashboard needs BOTH device_id and session_path to render a track; device_id alone
# is not enough. E3's two sessions were merged into 214256, which spans the whole race.
# B1/Amigo has no usable track (recorded 1 fix), so no session_path.
SESSION = {"B2": "2026-07-01-213151", "E4": "2026-07-01-214156", "E3": "2026-07-01-214256"}

BOAT_LOA = {"J/92":9.14,"J/80":8.00,"J/30":9.14,"J/99":9.99,"J/109":10.81,
    "J/46 DK":14.02,"Arcona 430":13.10,"Beneteau 36.7":11.20,
    "Columbia 30-2 Sport":9.14,"Frers 38":11.58,"Buzzards Bay 30":9.14,
    "Pearson 33-2":10.06,"Jeanneau Sun Odyssey 410":12.45,"Sabre 34 MK1":10.36,
    "Najad 380":11.94}

def local_iso(hms):
    h,m,s = map(int, hms.split(":"))
    return (datetime(2026,7,1,h,m,s)+LOCAL_TO_UTC).replace(tzinfo=timezone.utc)\
        .isoformat().replace("+00:00","Z")

# cls, team, yacht, boat_id, sail, type, club, rating, finish, status, device_id, corr_sheet
ROWS = [
 # Class A — start 18:51:00
 ("A","Ryley, Lance","RockIt 2.0","b2cc7e9b","52816","Columbia 30-2 Sport","Constitution YC",0.968,"19:46:12","FIN",None,"00:53:26"),
 ("A","Powers, David and Tom / Crimmins, Joe","Agora","a464086e","52475","Beneteau 36.7","New York YC / Constitution YC",0.920,"19:49:14","FIN",None,"00:53:34"),
 ("A","Isaacson, Peter","Uproarious","52a2cf69","USA 78","J/109","Constitution YC",0.930,"19:50:26","FIN",None,"00:55:16"),
 ("A","Rudser, Jim","Riot","0938a2df","USA 40","J/99","Constitution YC",0.938,"19:50:18","FIN",None,"00:55:37"),
 ("A","Pogue, Robert","Never Settle","52c1b5a1","USA 14","J/92","Constitution YC",0.910,"19:55:07","FIN",None,"00:58:21"),
 ("A","McLean, Allan","Eagle","079eaee4","42359","Frers 38","Constitution YC",0.936,"20:04:08","FIN",None,"01:08:27"),
 ("A","Jacobson, William","VANISH","29784095","51613","J/46 DK","Constitution YC",1.008,None,"DNC",None,None),
 ("A","Alexander, Dave","Pressure Drop","365f3fca","61430","Arcona 430","Constitution YC",1.007,None,"DNC",None,None),
 ("A","Barmmer, Brian","Saorsa","145f23ab","USA 1111","J/109","Boston YC",1.008,None,"DNC",None,None),
 # Class B — start 18:45:00
 ("B","Moresco, Micky & Marques, Joaquim","Amigo","dd2e70b8","82","J/80","",0.873,"19:53:19","FIN","B1","00:59:38"),
 ("B","De Souter, Marissa & Wafler, Garrett","Special Sauce","d3c4c77c","470","J/30","",0.843,"19:55:47","FIN",None,"00:59:40"),
 ("B","Conway, Ryan","MASHNEE","08117c80","7","Buzzards Bay 30","MIT Nautical Assoc.",0.870,"19:53:42","FIN",None,"00:59:46"),
 ("B","Courageous Sailing","Wizard","215faf2e","811","J/80","",0.873,"19:53:44","FIN","E3","01:00:00"),
 ("B","Long, III, James Gardner & Wagner, Ryan","Badger","82862687","220","Sabre 34 MK1","Constitution YC",0.780,"20:01:56","FIN",None,"01:00:00"),
 ("B","Tubman, Richard","Charisma","74aeb687","4396","Jeanneau Sun Odyssey 410","Constitution YC",0.889,"19:56:56","FIN",None,"01:03:57"),
 ("B","Courageous Sailing","Doc Buck","47f91f61","88","J/80","Courageous SC",0.873,"20:00:41","FIN","E4","01:06:04"),
 ("B","Paul Avillach & Kathryn Commons","Katü","83e911ed","484","J/80","Courageous SC",0.873,"20:03:44","FIN","B2","01:08:44"),
 ("B","Phelps, Isaac","Seabiscuit","d786bfac","110","Pearson 33-2","Constitution YC",0.840,"20:08:52","FIN",None,"01:10:27"),
 ("B","Myrto, Vivjan","Eagle","ff6f3c1f","203","Najad 380","",0.815,None,"DNC",None,None),
]

def hms_to_s(hms):
    h,m,s = map(int, hms.split(":")); return h*3600+m*60+s
def s_to_hms(s):
    s=int(round(s)); return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"

def build_and_verify():
    boats=[]; print(f"{'Boat':14} {'Cls':3} {'Rating':6} {'Elapsed':9} {'Corr(calc)':10} {'Corr(sheet)':11} OK")
    ok=True
    for (cls,team,yacht,bid,sail,btype,club,rating,fin,status,dev,corr_sheet) in ROWS:
        entry={"device_id":dev,"boat_id":bid,"class":cls,"rating":rating,
            "team_name":team,"boat_name":yacht,"sail_number":sail,"boat_type":btype,
            "club":club,"finish_time":local_iso(fin) if fin else None,
            "finish_status":status,"session_path":SESSION.get(dev),"gpx_path":None,
            "loa_m":BOAT_LOA.get(btype)}
        boats.append(entry)
        if status=="FIN":
            elapsed=hms_to_s(fin)-hms_to_s(CLASS_START[cls])
            calc=s_to_hms(elapsed*rating)
            match = (calc==corr_sheet)
            ok = ok and match
            print(f"{yacht:14} {cls:3} {rating:<6} {s_to_hms(elapsed):9} {calc:10} {corr_sheet:11} {'OK' if match else 'MISMATCH!!'}")
        else:
            print(f"{yacht:14} {cls:3} {rating:<6} {'--':9} {'--':10} {status:11}")
    return boats, ok

def req(method, path, body=None):
    data=json.dumps(body).encode() if body is not None else None
    r=urllib.request.Request(API+path, data=data, method=method,
        headers={"Content-Type":"application/json"} if body else {})
    with urllib.request.urlopen(r, timeout=30) as resp:
        return resp.status, json.loads(resp.read())

def main():
    do_post = "--post" in sys.argv
    boats, ok = build_and_verify()
    print(f"\nGATE: elapsed x rating == sheet corrected for all finishers: {'PASS' if ok else 'FAIL'}")
    if not ok:
        print("ABORT — transcription mismatch, not writing."); sys.exit(1)

    classes=[{"id":c,"name":f"Class {c}","start_time":local_iso(CLASS_START[c]),
        "rating_system":RATING_SYSTEM,"rating_type":RATING_TYPE,"race_len_nm":RACE_LEN_NM}
        for c in ("A","B")]
    payload={"name":RACE_NAME,"date":RACE_DATE,"start_time":local_iso(FIRST_GUN),
        "end_time":local_iso("20:20:00"),"regatta_id":REGATTA_ID,"classes":classes,
        "race_conditions":CONDITIONS,"course":COURSE,"boats":boats}

    devs={b["boat_name"]:b["device_id"] for b in boats if b["device_id"]}
    print(f"\nGPS device assignments: {devs}")
    print(f"Race: {RACE_NAME} {RACE_DATE} | {len(boats)} boats | conditions {CONDITIONS}")

    if not do_post:
        print("\n[DRY RUN] re-run with --post to write to the live regatta.")
        return

    _, data = req("GET", f"/api/races?regatta_id={REGATTA_ID}&date={RACE_DATE}")
    existing=next((r["race_id"] for r in data.get("races",[]) if r.get("name")==RACE_NAME), None)
    if existing:
        # preserve any user-set device/session/gpx by sail number
        _, prior = req("GET", f"/api/races/{existing}")
        by_sail={(b.get('sail_number') or '').strip():b for b in prior.get('boats',[])}
        for b in payload["boats"]:
            old=by_sail.get((b.get('sail_number') or '').strip())
            if old:
                if not b.get("device_id") and old.get("device_id"): b["device_id"]=old["device_id"]
                if old.get("session_path"): b["session_path"]=old["session_path"]
                if old.get("gpx_path"): b["gpx_path"]=old["gpx_path"]
        print(f"\nPATCH existing race {existing}")
        _, race = req("PATCH", f"/api/races/{existing}", payload)
    else:
        print("\nPOST new race")
        _, race = req("POST", "/api/races", payload)
    print(f"✓ race_id {race['race_id']}")
    print(f"  https://sailframes.com/race.html?race={race['race_id']}")

if __name__=="__main__":
    main()
