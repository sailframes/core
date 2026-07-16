#!/usr/bin/env python3
"""GPS precision analysis for CYC Race 6 (2026-07-01).
Inputs: raw/<dev>/2026-07-01/*_nav.csv from s3://sailframes-fleet-data-prod
(download B2/E3/E4/B1 to B2.csv/E3.csv/E4.csv and run in that dir).
Output backs docs/reports/GPS_ACCURACY_RACE6_2026-07-01.md."""
import csv, math, statistics as st

FILES = {"B2 (Katu, LC29HEA)":"B2.csv", "E3 (Wizard, LG290P)":"E3.csv",
         "E4 (Doc Buck, LG290P)":"E4.csv"}

def utc_s(u):  # HHMMSS.sss -> seconds of day
    u=float(u); h=int(u//10000); m=int((u%10000)//100); s=u%100
    return h*3600+m*60+s

def load(path):
    rows=[]
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                rows.append(dict(t=utc_s(r["utc"]), lat=float(r["lat"]), lon=float(r["lon"]),
                    sog=float(r["sog"]), sat=int(r["sat"]), hdop=float(r["hdop"]),
                    fix=int(r["fix"]), hacc=float(r["hacc"])))
            except (ValueError, KeyError): pass
    # unwrap midnight
    for i in range(1,len(rows)):
        while rows[i]["t"] < rows[i-1]["t"] - 43200: rows[i]["t"] += 86400
    return rows

def pct(xs,p):
    xs=sorted(xs); k=(len(xs)-1)*p/100; f=int(k)
    return xs[f] if f+1>=len(xs) else xs[f]+(xs[f+1]-xs[f])*(k-f)

def analyze(name, path):
    r=load(path)
    n=len(r); dur=r[-1]["t"]-r[0]["t"]
    dts=[r[i]["t"]-r[i-1]["t"] for i in range(1,n)]
    dts=[d for d in dts if d>0]
    med_dt=st.median(dts)
    miss=sum(1 for d in dts if d>0.15)          # missed/late fix (>1.5x nominal)
    biggap=sum(1 for d in dts if d>1.0)
    maxgap=max(dts)
    hacc=[x["hacc"] for x in r]; hdop=[x["hdop"] for x in r]; sat=[x["sat"] for x in r]
    fixc={};
    for x in r: fixc[x["fix"]]=fixc.get(x["fix"],0)+1
    # stillest window: longest run with sog<0.5kt, >=30s
    best=None; i=0
    while i<n:
        if r[i]["sog"]<0.5:
            j=i
            while j<n and r[j]["sog"]<0.5: j+=1
            if r[j-1]["t"]-r[i]["t"]>= (best[1]-best[0] if best else 0):
                best=(r[i]["t"], r[j-1]["t"], i, j)
            i=j
        else: i+=1
    prec=None
    if best and best[1]-best[0]>=30:
        seg=r[best[2]:best[3]]
        mlat=st.mean(x["lat"] for x in seg); mlon=st.mean(x["lon"] for x in seg)
        mlatr=math.radians(mlat)
        E=[(x["lon"]-mlon)*111320*math.cos(mlatr) for x in seg]
        N=[(x["lat"]-mlat)*111320 for x in seg]
        rad=[math.hypot(e,nn) for e,nn in zip(E,N)]
        prec=dict(secs=best[1]-best[0], n=len(seg),
            sog=st.mean(x["sog"] for x in seg),
            sdE=st.pstdev(E), sdN=st.pstdev(N),
            drms=math.sqrt(st.pvariance(E)+st.pvariance(N)),
            cep50=pct(rad,50), cep95=pct(rad,95),
            hacc_seg=st.mean(x["hacc"] for x in seg))
    print(f"\n===== {name} =====")
    print(f"  fixes={n}  duration={dur/60:.1f} min  median dt={med_dt*1000:.0f} ms  (rate={1/med_dt:.1f} Hz)")
    print(f"  late/missed fixes (dt>0.15s): {miss} ({100*miss/len(dts):.2f}%)   gaps>1s: {biggap}   max gap: {maxgap:.1f}s")
    print(f"  sat:  med {int(st.median(sat))}  min {min(sat)}  max {max(sat)}")
    print(f"  HDOP: med {st.median(hdop):.2f}  p95 {pct(hdop,95):.2f}  max {max(hdop):.2f}")
    print(f"  fix-quality counts: {dict(sorted(fixc.items()))}  (0=none 1=GPS 2=DGPS/SBAS 4=RTKfix 5=RTKflt)")
    uniq=len(set(round(h,3) for h in hacc))
    print(f"  hacc(m): med {st.median(hacc):.2f}  mean {st.mean(hacc):.2f}  p95 {pct(hacc,95):.2f}  max {max(hacc):.2f}  min {min(hacc):.2f}")
    print(f"  hacc VARIES? unique values={uniq}, stdev={st.pstdev(hacc):.3f}  {'(flat/quantized!)' if uniq<=3 else ''}")
    if prec:
        print(f"  STILLEST WINDOW: {prec['secs']:.0f}s, {prec['n']} fixes, mean SOG {prec['sog']:.2f} kt")
        print(f"    position scatter: stdE={prec['sdE']:.2f}m stdN={prec['sdN']:.2f}m  DRMS={prec['drms']:.2f}m")
        print(f"    CEP50={prec['cep50']:.2f}m  CEP95={prec['cep95']:.2f}m   (receiver hacc in window: {prec['hacc_seg']:.2f}m)")
    else:
        print("  no >=30s window under 0.5 kt (boat never sat still) — precision from scatter not available")

for name,path in FILES.items(): analyze(name,path)
print("\n[B1 (Amigo, LC29HEA)]: 1 data fix only (42.8 m hacc, 4 sats) — did not record the race.")
