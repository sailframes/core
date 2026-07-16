#!/usr/bin/env python3
import csv, math, json, statistics as st, xml.etree.ElementTree as ET
from datetime import datetime

DIR="/Users/paul2/Library/CloudStorage/Dropbox-Personal/Documents/Sports/Sail/CYC 2026 Spring series/07-01-26"

def iso_s(t):
    t=t.replace("Z","+00:00")
    return datetime.fromisoformat(t).timestamp()

def load_gpx(path):
    root=ET.fromstring(open(path,"rb").read())
    ns=""
    m=root.tag
    if m.startswith("{"): ns="{"+m[1:].split("}")[0]+"}"
    r=[]
    for tp in root.iter(f"{ns}trkpt"):
        te=tp.find(f"{ns}time")
        if te is None or not te.text: continue
        lat=float(tp.get("lat")); lon=float(tp.get("lon"))
        sp=None
        for e in tp.iter():
            if e.tag.split("}")[-1]=="speed" and e.text: sp=float(e.text)*1.94384
        r.append(dict(t=iso_s(te.text), lat=lat, lon=lon, sog=sp, hacc=None))
    return r

def load_sensorlog(path):
    r=[]
    for row in csv.DictReader(open(path)):
        try:
            t=int(row["time"])/1e9; lat=float(row["latitude"]); lon=float(row["longitude"])
            sog=max(0.0,float(row["speed"]))*1.94384; hacc=float(row["horizontalAccuracy"])
        except (ValueError,KeyError): continue
        if lat==0 or lon==0: continue
        r.append(dict(t=t,lat=lat,lon=lon,sog=sog,hacc=hacc))
    r.sort(key=lambda x:x["t"]); return r

def load_json_track(path):
    d=json.load(open(path)); r=[]
    for p in d:
        r.append(dict(t=iso_s(p["t"]), lat=p["lat"], lon=p["lon"], sog=p.get("speed_kn"), hacc=None))
    return r

def pct(xs,p):
    xs=sorted(xs); k=(len(xs)-1)*p/100; f=int(k)
    return xs[f] if f+1>=len(xs) else xs[f]+(xs[f+1]-xs[f])*(k-f)

def analyze(name, dev, r):
    r=[x for x in r if x["lat"]]
    n=len(r); dur=r[-1]["t"]-r[0]["t"]
    dts=[r[i]["t"]-r[i-1]["t"] for i in range(1,n) if 0<r[i]["t"]-r[i-1]["t"]<60]
    med_dt=st.median(dts) if dts else 0
    have_sog = any(x["sog"] is not None for x in r)
    # stillest window (sog<0.5 if available else near-zero movement)
    best=None; i=0
    def still(x):
        return (x["sog"] is not None and x["sog"]<0.5)
    if have_sog:
        while i<n:
            if still(r[i]):
                j=i
                while j<n and still(r[j]): j+=1
                if not best or (r[j-1]["t"]-r[i]["t"])>(best[1]-best[0]): best=(r[i]["t"],r[j-1]["t"],i,j)
                i=j
            else: i+=1
    prec=None
    if best and best[1]-best[0]>=30:
        seg=r[best[2]:best[3]]; mlat=st.mean(x["lat"] for x in seg); mlon=st.mean(x["lon"] for x in seg)
        c=math.cos(math.radians(mlat))
        E=[(x["lon"]-mlon)*111320*c for x in seg]; N=[(x["lat"]-mlat)*111320 for x in seg]
        rad=[math.hypot(e,nn) for e,nn in zip(E,N)]
        prec=dict(secs=best[1]-best[0], drms=math.sqrt(st.pvariance(E)+st.pvariance(N)),
                  cep50=pct(rad,50), cep95=pct(rad,95), sog=st.mean(x["sog"] for x in seg))
    hacc=[x["hacc"] for x in r if x["hacc"] is not None]
    print(f"\n== {name}  [{dev}] ==")
    print(f"  {n} pts, {dur/60:.0f} min, median dt {med_dt*1000:.0f} ms ({1/med_dt:.1f} Hz)" if med_dt else f"  {n} pts")
    if hacc: print(f"  hacc(m): med {st.median(hacc):.1f}  p95 {pct(hacc,95):.1f}")
    if prec: print(f"  stillest {prec['secs']:.0f}s @ {prec['sog']:.2f}kt: DRMS {prec['drms']:.2f}m  CEP50 {prec['cep50']:.2f}m  CEP95 {prec['cep95']:.2f}m")
    else: print("  no clean stationary window (or no speed field) — scatter precision N/A")

analyze("RockIt 2.0","Vakaros Atlas", load_json_track("/tmp/rockit_track.json"))
analyze("Uproarious","GPX logger (w/ sat)", load_gpx(f"{DIR}/Uproarious - 20260701-173127 - Uproarious 7_1_26.gpx"))
analyze("MASHNEE","GPX logger", load_gpx(f"{DIR}/26-7-1-Mashnee_gpx.gpx"))
analyze("Eagle (McLean)","GPX logger", load_gpx(f"{DIR}/Eagle_Track_(Allan)7_1_26_1.gpx"))
analyze("Agora","phone (Sensor Logger)", load_sensorlog(f"{DIR}/Agora - Boston_Main_Channel-2026-07-01_22-40-00/Location.csv"))
analyze("Never Settle","phone (Sensor Logger)", load_sensorlog(f"{DIR}/Never Settle - Boston_Main_Channel-2026-07-01_22-37-28/Location.csv"))
