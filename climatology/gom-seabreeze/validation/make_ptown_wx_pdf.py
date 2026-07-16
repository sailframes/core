#!/usr/bin/env python3
"""2-page weather + tactics PDF for the Marblehead->Provincetown overnight race.
HRRR (gfs_hrrr) vs Canadian HRDPS (gem_hrdps_continental) via Open-Meteo, NOAA tides,
GPX route + LNG avoid zones. matplotlib PdfPages."""
import json, urllib.request, datetime as dt, time
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Circle, FancyArrow
import matplotlib.dates as mdates

ACCENT="#0b3d66"; HRRRC="#d1495b"; HRDPSC="#1f6fb2"; LAND="#e7e1d4"; SEA="#eef4f8"
W0,W1="2026-07-17T18:00","2026-07-18T06:00"
D0,D1="2026-07-17","2026-07-18"

# --- route (from GPX) + sampled corridor points ---
START=("Tinkers Rock\n(start)",42.481873,-70.814438)
WP1=("Wp1",42.006088,-70.192657); WP2=("Wp2 (P-town)",42.034050,-70.161224)
ROUTE=[(42.481873,-70.814438),(42.006088,-70.192657),(42.034050,-70.161224)]
AVOID=[("LNG NE Gateway B",42.398889,-70.616667),("LNG NE Gateway A",42.393889,-70.591944)]
CORRIDOR=[("Start (Tinkers Rk)",42.48,-70.81),("Off Nahant",42.37,-70.66),
          ("Mid Mass Bay",42.25,-70.51),("Stellwagen edge",42.13,-70.36),("Race Pt / P-town",42.02,-70.20)]

def _get(url,tries=5):
    last=None
    for i in range(tries):
        try:
            with urllib.request.urlopen(url,timeout=45) as r: return json.loads(r.read())
        except Exception as e:
            last=e; time.sleep(2.5+3*i)
    raise last

MODELS={"HRRR (NOAA 3km)":"gfs_hrrr","HRDPS (Canada 2.5km)":"gem_hrdps_continental"}
def om_both(lat,lon):
    ms=",".join(MODELS.values())
    u=(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
       "&hourly=wind_speed_10m,wind_direction_10m,wind_gusts_10m,pressure_msl"
       f"&wind_speed_unit=kn&timezone=America%2FNew_York&start_date={D0}&end_date={D1}&models={ms}")
    return _get(u)["hourly"]

def tides(station,interval):
    u=("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?application=sailframes"
       f"&product=predictions&datum=MLLW&interval={interval}&units=english&time_zone=lst_ldt&format=json"
       f"&station={station}&begin_date={D0.replace('-','')}&end_date={D1.replace('-','')}")
    return _get(u)["predictions"]

def win(t): return W0<=t<=W1
def nn(x): return float("nan") if x is None else x
print("pulling HRRR + HRDPS along corridor (one request per point)...")
DATA={m:{} for m in MODELS}
for name,la,lo in CORRIDOR:
    h=om_both(la,lo); idx=[i for i,t in enumerate(h["time"]) if win(t)]
    for m,suf in MODELS.items():
        DATA[m][name]=([h["time"][i] for i in idx],
                       [nn(h[f"wind_speed_10m_{suf}"][i]) for i in idx],
                       [nn(h[f"wind_direction_10m_{suf}"][i]) for i in idx],
                       [nn(h[f"wind_gusts_10m_{suf}"][i]) for i in idx])
    time.sleep(1.5)
tdt=[dt.datetime.strptime(t,"%Y-%m-%dT%H:%M") for t in DATA["HRRR (NOAA 3km)"]["Mid Mass Bay"][0]]

# course-mean speed per model over time
def course_mean(model,which):  # which: 1 spd, 3 gust
    arrs=[DATA[model][n][which] for n,_,_ in CORRIDOR]
    return np.nanmean(np.array(arrs),axis=0)

print("pulling tides...")
tide_curve=tides("8443970","30")   # Boston, 30-min for the curve
tide_hilo=[p for p in tides("8443970","hilo") if D0<=p["t"][:10]<=D1]

# coarse MA coastline for orientation (lon,lat), Cape Ann -> Boston -> Plymouth -> Cape Cod Bay -> P-town
COAST=[(-70.62,42.68),(-70.66,42.61),(-70.73,42.56),(-70.80,42.52),(-70.86,42.50),(-70.93,42.44),
 (-70.99,42.39),(-71.03,42.34),(-70.97,42.30),(-70.90,42.28),(-70.84,42.25),(-70.78,42.22),
 (-70.72,42.17),(-70.68,42.08),(-70.66,41.99),(-70.60,41.91),(-70.53,41.84),(-70.45,41.79),
 (-70.35,41.75),(-70.24,41.73),(-70.13,41.74),(-70.04,41.80),(-70.01,41.90),(-70.03,41.98),
 (-70.06,42.03),(-70.11,42.06),(-70.18,42.08),(-70.235,42.063),(-70.20,42.03),(-70.15,42.02)]

def draw_map(ax):
    ax.set_facecolor(SEA)
    xs=[c[0] for c in COAST]; ys=[c[1] for c in COAST]
    ax.fill(xs+[-70.0,-71.1,-71.1],ys+[41.7,41.7,42.75],color=LAND,zorder=0,lw=0)  # crude land fill west/south
    ax.plot(xs,ys,color="#8a8170",lw=1.2,zorder=1)
    # route
    rla=[p[0] for p in ROUTE]; rlo=[p[1] for p in ROUTE]
    ax.plot(rlo,rla,"-",color=ACCENT,lw=2.4,zorder=5,solid_capstyle="round")
    ax.plot(rlo,rla,"o",color=ACCENT,ms=4,zorder=6)
    ax.plot(START[2],START[1],"*",ms=20,mfc="#2e8b57",mec="k",mew=1,zorder=7)
    ax.annotate(START[0],(START[2],START[1]),xytext=(6,4),textcoords="offset points",fontsize=8,fontweight="bold",color="#1c5c3a")
    ax.annotate("FINISH\nProvincetown",(WP2[2],WP2[1]),xytext=(8,-2),textcoords="offset points",fontsize=8,fontweight="bold",color=ACCENT)
    # LNG avoid
    for nm,la,lo in AVOID:
        ax.add_patch(Circle((lo,la),0.017,color="#d1495b",alpha=0.18,zorder=3))  # ~1nm no-anchor
        ax.plot(lo,la,"x",color="#b3243b",ms=7,mew=2,zorder=4)
    ax.annotate("⚠ LNG NE Gateway\nDeepwater Port\n(no entry 500 m)",(-70.604,42.396),
                xytext=(10,14),textcoords="offset points",fontsize=7.5,fontweight="bold",color="#b3243b",
                arrowprops=dict(arrowstyle="->",color="#b3243b",lw=1))
    # mean-wind arrows (HRRR) at corridor pts -> shows reaching angle vs course
    for n,la,lo in CORRIDOR:
        d=np.array(DATA["HRRR (NOAA 3km)"][n][2]); s=np.nanmean(DATA["HRRR (NOAA 3km)"][n][1])
        md=np.degrees(np.arctan2(np.nanmean(np.sin(np.radians(d))),np.nanmean(np.cos(np.radians(d)))))%360
        # blow-TO direction = from+180; dx east, dy north
        to=(md+180)%360; L=0.05
        dx=L*np.sin(np.radians(to)); dy=L*np.cos(np.radians(to))
        ax.add_patch(FancyArrow(lo,la,dx,dy,width=0.004,head_width=0.016,head_length=0.014,
                     color="#5566aa",alpha=0.85,zorder=6,length_includes_head=True))
    for nm,la,lo,dx,dy in [("Marblehead",42.50,-70.86,-30,2),("Boston",42.34,-71.02,-28,-2),
        ("Cape Ann",42.63,-70.66,2,2),("Plymouth",41.96,-70.66,-38,0),("Cape Cod",41.74,-70.25,-6,-12),("Provincetown",42.06,-70.24,-64,8)]:
        ax.annotate(nm,(lo,la),xytext=(dx,dy),textcoords="offset points",fontsize=7,color="#5b5344",style="italic")
    ax.set_xlim(-71.08,-70.02); ax.set_ylim(41.90,42.72)
    ax.set_aspect(1/np.cos(np.radians(42.3)))
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title("Route & overnight wind (HRRR mean arrows)",fontsize=9.5,fontweight="bold",color=ACCENT)
    ax.text(0.02,0.03,"~40 nm · start 19:00 EDT · finish ~03:00",transform=ax.transAxes,fontsize=7.5,color="#5b5344")

# ============================ BUILD PDF ============================
pdf=PdfPages("docs/reports/RACE_WX_MARBLEHEAD_PTOWN_2026-07-17.pdf")
gen=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")

# ---------- PAGE 1 ----------
fig=plt.figure(figsize=(8.5,11)); fig.patch.set_facecolor("white")
fig.text(0.5,0.965,"Marblehead → Provincetown — Overnight Race Weather",ha="center",fontsize=17,fontweight="bold",color=ACCENT)
fig.text(0.5,0.943,"Friday 2026-07-17, 19:00 EDT start  ·  ~03:00 Sat finish  ·  ~40 nm SE across Massachusetts & Cape Cod Bays",
         ha="center",fontsize=9.5,color="#333")
fig.text(0.5,0.927,f"Models: NOAA HRRR 3 km + Environment Canada HRDPS 2.5 km (Open-Meteo)  ·  generated {gen}  ·  ~24 h lead — refine race morning",
         ha="center",fontsize=8,color="#777",style="italic")

axm=fig.add_axes([0.06,0.44,0.56,0.45]); draw_map(axm)
# wind speed timeline (model compare)
axw=fig.add_axes([0.68,0.68,0.28,0.21])
for m,c in [("HRRR (NOAA 3km)",HRRRC),("HRDPS (Canada 2.5km)",HRDPSC)]:
    axw.plot(tdt,course_mean(m,1),color=c,lw=1.8,label=m.split(" (")[0])
    axw.fill_between(tdt,course_mean(m,1),course_mean(m,3),color=c,alpha=0.10)
axw.set_title("Course-mean wind (kt)",fontsize=8.5,fontweight="bold"); axw.legend(fontsize=6.5,loc="upper left")
axw.xaxis.set_major_formatter(mdates.DateFormatter("%Hh")); axw.tick_params(labelsize=6.5); axw.grid(alpha=0.25)
axw.axvspan(dt.datetime(2026,7,17,19),dt.datetime(2026,7,18,3),color="#ffe9a8",alpha=0.3)
# direction timeline
axd=fig.add_axes([0.68,0.44,0.28,0.18])
for m,c in [("HRRR (NOAA 3km)",HRRRC),("HRDPS (Canada 2.5km)",HRDPSC)]:
    dmid=DATA[m]["Mid Mass Bay"][2]; axd.plot(tdt,dmid,".",color=c,ms=3)
axd.set_title("Wind dir (° from, mid-bay)",fontsize=8.5,fontweight="bold"); axd.set_ylim(60,240)
axd.axhspan(157.5,202.5,color="#cfe6cf",alpha=0.5); axd.text(tdt[0],205,"S",fontsize=6,color="#487a48")
axd.xaxis.set_major_formatter(mdates.DateFormatter("%Hh")); axd.tick_params(labelsize=6.5); axd.grid(alpha=0.25)

# synopsis
hr0=np.nanmean([DATA["HRRR (NOAA 3km)"][n][1][1] for n,_,_ in CORRIDOR])
syn=("Light, weak-gradient southerly night (flat ~1016 mb, no front). The models AGREE on the overnight: the wind fills "
 "S/SW 8-12 kt offshore, a starboard-tack reach that gradually frees toward Race Point, seas ≤2 ft. They SHARPLY DISAGREE "
 "on the start: NOAA HRRR has SE ~10 kt (a reach/beat down the rhumb) while Canada HRDPS shows a NW land-breeze ~7 kt "
 "(offshore), dying to near-calm by ~01:00 before the southerly fills — opposite first-leg angles. HRDPS has verified "
 "better locally this season, but at a post-sunset transition neither is reliable: sail the breeze that's actually on the "
 "water, then get offshore into the steadier southerly. Hour-by-hour, tactics, tide and hazards overleaf.")
fig.text(0.06,0.37,"Overnight synopsis",fontsize=12,fontweight="bold",color=ACCENT)
fig.text(0.06,0.35,syn,fontsize=9.2,color="#222",wrap=True,va="top",ha="left")
fig.text(0.06,0.02,"SailFrames race weather · HRRR + HRDPS via Open-Meteo · NOT for navigation — verify with official forecasts",fontsize=6.5,color="#999")
pdf.savefig(fig); plt.close(fig)

# ---------- PAGE 2 ----------
fig=plt.figure(figsize=(8.5,11)); fig.patch.set_facecolor("white")
fig.text(0.5,0.965,"Tactics, Strategy & Timing",ha="center",fontsize=16,fontweight="bold",color=ACCENT)
fig.text(0.5,0.945,"Marblehead → Provincetown · Fri 2026-07-17 overnight",ha="center",fontsize=9.5,color="#333")

# corridor table (HRRR / HRDPS dir/spd at key times)
keyt=["19:00","21:00","23:00","01:00","03:00","05:00"]
def cell(m,n,tt):
    T=DATA[m][n][0]
    for i,x in enumerate(T):
        if x[11:16]==tt: return f"{round(DATA[m][n][2][i]):03d}/{DATA[m][n][1][i]:.0f}"
    return "—"
axt=fig.add_axes([0.06,0.60,0.88,0.30]); axt.axis("off")
cols=["Time ET"]+[n for n,_,_ in CORRIDOR]
rows=[]
for tt in keyt:
    rows.append([tt+"  H\n       C"]+[f"{cell('HRRR (NOAA 3km)',n,tt)}\n{cell('HRDPS (Canada 2.5km)',n,tt)}" for n,_,_ in CORRIDOR])
tab=axt.table(cellText=rows,colLabels=cols,loc="center",cellLoc="center")
tab.auto_set_font_size(False); tab.set_fontsize(7.2); tab.scale(1,2.1)
for (r,c),cl in tab.get_celld().items():
    cl.set_edgecolor("#ccc")
    if r==0: cl.set_facecolor(ACCENT); cl.set_text_props(color="w",fontweight="bold")
axt.set_title("Corridor wind — dir°/kt, H=HRRR (top) / C=HRDPS (bottom)",fontsize=9.5,fontweight="bold",color=ACCENT,pad=2)

# tide curve
axtide=fig.add_axes([0.06,0.43,0.88,0.13])
tt=[dt.datetime.strptime(p["t"],"%Y-%m-%d %H:%M") for p in tide_curve]
tv=[float(p["v"]) for p in tide_curve]
sel=[(x,v) for x,v in zip(tt,tv) if dt.datetime(2026,7,17,17)<=x<=dt.datetime(2026,7,18,7)]
axtide.plot([x for x,_ in sel],[v for _,v in sel],color=HRDPSC,lw=1.8)
axtide.axvspan(dt.datetime(2026,7,17,19),dt.datetime(2026,7,18,3),color="#ffe9a8",alpha=0.35,label="race window")
for p in tide_hilo:
    x=dt.datetime.strptime(p["t"],"%Y-%m-%d %H:%M")
    if dt.datetime(2026,7,17,17)<=x<=dt.datetime(2026,7,18,7):
        axtide.annotate(f"{p['type']} {x:%H:%M}",(x,float(p["v"])),fontsize=7,ha="center",
                        color="#b3243b" if p['type']=='H' else "#1c5c3a",fontweight="bold",
                        xytext=(0,6 if p['type']=='H' else -12),textcoords="offset points")
axtide.set_title("Tide at Boston (proxy) — start ≈ low slack, finish ≈ high slack",fontsize=9,fontweight="bold",color=ACCENT)
axtide.xaxis.set_major_formatter(mdates.DateFormatter("%Hh")); axtide.tick_params(labelsize=7); axtide.grid(alpha=0.25)
axtide.set_ylabel("ft",fontsize=7)

tac=[
("① Start (19:00–21:00) — models disagree, so read the water","HRRR: SE ~10 kt (reach/close-hauled). HRDPS: NW land-breeze ~7 kt (offshore), dying to calm ~01:00. Opposite angles — don't pre-commit. Sail the actual dock breeze; whatever it is it's LIGHT (≤8 kt). Get clear air off the line, then work OFFSHORE — the Marblehead shore is the light trap."),
("② The crossing (21:00–01:00) — reach in the filling southerly","Both models fill S/SW 8-12 kt offshore — a starboard reach that frees as the wind veers right. Pressure lives offshore/south (mid-bay ~10 kt vs shore 2-6). Commit south; stay on the pressure side, don't over-stand north toward Cape Ann."),
("③ ⚠ LNG Northeast Gateway — hard avoid","Two STL buoys at ~42.40 N (−70.60/−70.59) sit just N of the direct line. NO ENTRY within 500 m, no anchoring within ~1 nm. The rhumb passes ~5 nm south — staying on/south of the line is both faster (more breeze) and legal."),
("④ Race Point & finish (01:00–03:00) — the tide gate","S/SSW 8-11 kt, gusts to 15 into Cape Cod Bay. Flood builds to Boston high 02:45 ≈ finish. Round Race Point BEFORE ~02:45 to carry the flood in; later boats meet the building ebb. Finish current itself is minor (near high slack)."),
("⑤ Night & hazards","Dark — thin crescent sets early, so nav by instruments, watches + lights set. Fog watch: July S-flow over cool water; NWS not flagging Fri night but recheck AM. Seas ≤2 ft, no Small Craft Advisory expected."),
]
y=0.335
fig.text(0.06,0.365,"Tactics & strategy",fontsize=12,fontweight="bold",color=ACCENT)
for head,body in tac:
    fig.text(0.06,y,head,fontsize=9.3,fontweight="bold",color="#123")
    fig.text(0.06,y-0.015,body,fontsize=8.1,color="#222",wrap=True,va="top")
    y-=0.062
fig.text(0.06,0.018,"Confidence: moderate on the overnight reach + tide timing (models agree); low on the exact start (transition + lead). Re-run race morning + pre-start.",
         fontsize=7.2,color="#777",style="italic")
pdf.savefig(fig); plt.close(fig)
pdf.close()
print("wrote docs/reports/RACE_WX_MARBLEHEAD_PTOWN_2026-07-17.pdf")
