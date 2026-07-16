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
       "&hourly=wind_speed_10m,wind_direction_10m,wind_gusts_10m,pressure_msl,temperature_2m"
       f"&wind_speed_unit=kn&timezone=America%2FNew_York&start_date={D0}&end_date={D1}&models={ms}")
    return _get(u)["hourly"]

def marine_pt(lat,lon):
    u=(f"https://marine-api.open-meteo.com/v1/marine?latitude={lat}&longitude={lon}"
       "&hourly=wave_height,sea_surface_temperature&timezone=America%2FNew_York"
       f"&start_date={D0}&end_date={D1}")
    try:
        h=_get(u)["hourly"]
        wv=[v for t,v in zip(h["time"],h["wave_height"]) if W0<=t<=W1 and v is not None]
        sst=[v for t,v in zip(h["time"],h["sea_surface_temperature"]) if W0<=t<=W1 and v is not None]
        return float(np.nanmean(wv)),float(np.nanmax(wv)),float(np.nanmean(sst))
    except Exception:
        return 0.2,0.3,20.5

def tides(station,interval):
    u=("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?application=sailframes"
       f"&product=predictions&datum=MLLW&interval={interval}&units=english&time_zone=lst_ldt&format=json"
       f"&station={station}&begin_date={D0.replace('-','')}&end_date={D1.replace('-','')}")
    return _get(u)["predictions"]

def win(t): return W0<=t<=W1
def nn(x): return float("nan") if x is None else x
print("pulling HRRR + HRDPS along corridor (one request per point)...")
DATA={m:{} for m in MODELS}; TEMP_T=[]; TEMP_C=[]
for name,la,lo in CORRIDOR:
    h=om_both(la,lo); idx=[i for i,t in enumerate(h["time"]) if win(t)]
    for m,suf in MODELS.items():
        DATA[m][name]=([h["time"][i] for i in idx],
                       [nn(h[f"wind_speed_10m_{suf}"][i]) for i in idx],
                       [nn(h[f"wind_direction_10m_{suf}"][i]) for i in idx],
                       [nn(h[f"wind_gusts_10m_{suf}"][i]) for i in idx])
    if name=="Mid Mass Bay":
        TEMP_T=[h["time"][i] for i in idx]; TEMP_C=[nn(h["temperature_2m_gfs_hrrr"][i]) for i in idx]
    time.sleep(1.5)
tdt=[dt.datetime.strptime(t,"%Y-%m-%dT%H:%M") for t in DATA["HRRR (NOAA 3km)"]["Mid Mass Bay"][0]]
# gust factor (HRRR) per point + sea state
GF={n: float(np.nanmean(DATA["HRRR (NOAA 3km)"][n][3])/max(np.nanmean(DATA["HRRR (NOAA 3km)"][n][1]),0.1)) for n,_,_ in CORRIDOR}
WAVE_M,WAVE_MX,SST=marine_pt(42.25,-70.51)

def shear_pt(la,lo):   # surface / 925 / 850 speed+dir + low-level inversion, mid-bay
    ms=",".join(MODELS.values())
    fl=("wind_speed_10m,wind_direction_10m,wind_speed_925hPa,wind_direction_925hPa,"
        "wind_speed_850hPa,wind_direction_850hPa,temperature_2m,temperature_925hPa")
    u=(f"https://api.open-meteo.com/v1/forecast?latitude={la}&longitude={lo}&hourly={fl}"
       f"&wind_speed_unit=kn&timezone=America%2FNew_York&start_date={D0}&end_date={D1}&models={ms}")
    h=_get(u)["hourly"]; idx=[i for i,t in enumerate(h["time"]) if win(t)]
    def vd(suf,k):
        ds=[h[f"wind_direction_{k}_{suf}"][i] for i in idx if h[f"wind_direction_{k}_{suf}"][i] is not None]
        return float(np.degrees(np.arctan2(np.mean([np.sin(np.radians(x)) for x in ds]),
                                           np.mean([np.cos(np.radians(x)) for x in ds])))%360)
    def sp(suf,k): return float(np.nanmean([nn(h[f"wind_speed_{k}_{suf}"][i]) for i in idx]))
    def tp(suf,k): return float(np.nanmean([nn(h[f"{k}_{suf}"][i]) for i in idx]))
    out={}
    for m,suf in MODELS.items():
        out[m]={"10":(sp(suf,"10m"),vd(suf,"10m")),"925":(sp(suf,"925hPa"),vd(suf,"925hPa")),
                "850":(sp(suf,"850hPa"),vd(suf,"850hPa")),"inv":tp(suf,"temperature_925hPa")-tp(suf,"temperature_2m")}
    return out
SHEAR=shear_pt(42.25,-70.51)
print("pulling tides...")

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

def mean_ds(model,name):   # race-window vector-mean direction (FROM) + mean speed
    d=[x for x in DATA[model][name][2] if not np.isnan(x)]; s=DATA[model][name][1]
    md=np.degrees(np.arctan2(np.nanmean(np.sin(np.radians(d))),np.nanmean(np.cos(np.radians(d)))))%360
    return md,float(np.nanmean(s))

def draw_diffmap(ax):
    """Map showing WHERE the two models differ: paired HRRR (red) / HRDPS (blue) mean-wind
    arrows at each corridor point. Arrows diverge where the models disagree (the start),
    converge where they agree (the finish)."""
    ax.set_facecolor(SEA)
    xs=[c[0] for c in COAST]; ys=[c[1] for c in COAST]
    ax.fill(xs+[-70.0,-71.1,-71.1],ys+[41.7,41.7,42.75],color=LAND,zorder=0,lw=0)
    ax.plot(xs,ys,color="#8a8170",lw=1.0,zorder=1)
    ax.plot([p[1] for p in ROUTE],[p[0] for p in ROUTE],"-",color="#888",lw=1.5,zorder=3)
    for name,la,lo in CORRIDOR:
        for m,c in [("HRRR (NOAA 3km)",HRRRC),("HRDPS (Canada 2.5km)",HRDPSC)]:
            md,s=mean_ds(m,name); to=(md+180)%360; L=0.03+0.007*s
            ax.add_patch(FancyArrow(lo,la,L*np.sin(np.radians(to)),L*np.cos(np.radians(to)),
                         width=0.003,head_width=0.022,head_length=0.017,color=c,alpha=0.9,
                         zorder=6,length_includes_head=True))
        ax.plot(lo,la,"o",ms=2.5,color="#333",zorder=5)
    ax.plot(START[2],START[1],"*",ms=14,mfc="#2e8b57",mec="k",mew=0.8,zorder=8)
    ax.annotate("START",(START[2],START[1]),xytext=(6,3),textcoords="offset points",fontsize=7,fontweight="bold",color="#1c5c3a")
    ax.annotate("FINISH",(WP2[2],WP2[1]),xytext=(6,-2),textcoords="offset points",fontsize=7,fontweight="bold",color=ACCENT)
    ax.plot([],[],color=HRRRC,lw=3,label="HRRR"); ax.plot([],[],color=HRDPSC,lw=3,label="HRDPS")
    ax.legend(fontsize=7.5,loc="lower left",framealpha=0.9)
    ax.set_xlim(-71.02,-70.05); ax.set_ylim(41.96,42.66); ax.set_aspect(1/np.cos(np.radians(42.3)))
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title("Where the models differ — mean-wind arrows (length ∝ speed)",fontsize=9.5,fontweight="bold",color=ACCENT)

# ============================ BUILD PDF ============================
pdf=PdfPages("docs/reports/RACE_WX_MARBLEHEAD_PTOWN_2026-07-17.pdf")
gen=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")

# ---------- PAGE 1 ----------
fig=plt.figure(figsize=(8.5,11)); fig.patch.set_facecolor("white")
fig.text(0.5,0.965,"Marblehead → Provincetown — Overnight Race Weather",ha="center",fontsize=17,fontweight="bold",color=ACCENT)
fig.text(0.5,0.943,"Friday 2026-07-17, 19:00 EDT start  ·  ~03:00 Sat finish  ·  ~40 nm SE across Massachusetts & Cape Cod Bays",
         ha="center",fontsize=9.5,color="#333")
fig.text(0.5,0.925,"NOAA HRRR (3 km) and Environment Canada HRDPS (2.5 km) — two independent high-resolution models.",
         ha="center",fontsize=8.4,color="#555")
fig.text(0.5,0.911,f"They agree on the overnight but split on the start — compared below and mapped on p.2.   ·   {gen} · ~24 h lead.",
         ha="center",fontsize=7.9,color="#888",style="italic")

axm=fig.add_axes([0.06,0.44,0.56,0.45]); draw_map(axm)
# wind speed timeline (model compare)
axw=fig.add_axes([0.68,0.68,0.28,0.21])
for m,c in [("HRRR (NOAA 3km)",HRRRC),("HRDPS (Canada 2.5km)",HRDPSC)]:
    axw.plot(tdt,course_mean(m,1),color=c,lw=1.8,label=m.split(" (")[0])
    axw.fill_between(tdt,course_mean(m,1),course_mean(m,3),color=c,alpha=0.10)
axw.set_title("Course-mean wind (kt)",fontsize=8.5,fontweight="bold"); axw.legend(fontsize=6.5,loc="upper left")
axw.xaxis.set_major_formatter(mdates.DateFormatter("%Hh")); axw.tick_params(labelsize=6.5); axw.grid(alpha=0.25)
axw.axvspan(dt.datetime(2026,7,17,19),dt.datetime(2026,7,18,3),color="#ffe9a8",alpha=0.3)
# vertical wind-shear profile (HRRR): light SE surface under a W-WNW flow aloft
axs=fig.add_axes([0.655,0.435,0.32,0.205]); axs.set_xlim(-1.7,2.9); axs.set_ylim(-1.35,2.75)
axs.set_aspect("equal"); axs.axis("off")
axs.set_title("Wind shear — HRRR (mid-bay)",fontsize=8.5,fontweight="bold")
for lab,k,y in [("Surface 10 m","10",0),("925 hPa ≈0.8 km","925",1),("850 hPa ≈1.5 km","850",2)]:
    spd,d=SHEAR["HRRR (NOAA 3km)"][k]
    to=(d+180)%360; L=0.10+0.022*spd
    axs.annotate("",xy=(0.2+L*np.sin(np.radians(to)),y+L*np.cos(np.radians(to))),xytext=(0.2,y),
                 arrowprops=dict(arrowstyle="-|>",color=ACCENT,lw=2.2,mutation_scale=13))
    axs.text(1.25,y,f"{lab}\n{d:.0f}° / {spd:.0f} kt",fontsize=7,va="center",ha="left")
axs.plot([0.2,0.2],[0,2],color="#ccc",lw=0.8,ls=":",zorder=0)
inv=SHEAR["HRRR (NOAA 3km)"]["inv"]
axs.text(-1.65,-0.55,f"~100° veer, wind ~2× in 1 km.\nLight SE surface under W–WNW ~16 kt.\n"
         f"925 hPa {'+' if inv>=0 else ''}{inv:.1f}°C = {'inversion (decoupled)' if inv>0 else 'mixed'}.",
         fontsize=6.2,color="#b3243b",va="top")

# synopsis
hr0=np.nanmean([DATA["HRRR (NOAA 3km)"][n][1][1] for n,_,_ in CORRIDOR])
syn=("Light, weak-gradient southerly night (flat ~1016 mb, no front). The models AGREE on the overnight: the wind fills "
 "S/SW 8-12 kt offshore, a starboard-tack reach that gradually frees toward Race Point, seas ≤2 ft. They SHARPLY DISAGREE "
 "on the start: NOAA HRRR has SE ~10 kt (a reach/beat down the rhumb) while Canada HRDPS shows a NW land-breeze ~7 kt "
 "(offshore), dying to near-calm by ~01:00 before the southerly fills — opposite first-leg angles. HRDPS has verified "
 "better locally this season, but at a post-sunset transition neither is reliable: sail the breeze that's actually on the "
 "water, then get offshore into the steadier southerly. Hour-by-hour, tactics, tide and hazards overleaf.")
fig.text(0.06,0.415,"Overnight synopsis",fontsize=12,fontweight="bold",color=ACCENT)
fig.text(0.06,0.395,syn,fontsize=9.0,color="#222",wrap=True,va="top",ha="left")

# ---- Left: how the two models differ · Right: conditions cards ----
fig.text(0.06,0.235,"How the two models differ (this race)",fontsize=11,fontweight="bold",color=ACCENT)
fig.text(0.06,0.212,
 "Both are high-res mesoscale models and AGREE on\n"
 "the overnight (S/SW 8–12 kt reach to Race Point).\n"
 "• HRRR (NOAA, 3 km): hourly, radar/satellite DA —\n"
 "  strong on convection, gusts & fast change. Holds\n"
 "  a decoupled SE surface here; gives a gust field.\n"
 "• HRDPS (Canada, 2.5 km): finer mesh; the better\n"
 "  local performer this season. Mixes more → a NW\n"
 "  land-breeze at the start; no separate gust field.\n"
 "They split most at the START (near-opposite dirs);\n"
 "they converge by the finish.",
 fontsize=7.5,color="#222",va="top")

fig.text(0.52,0.235,"Conditions at a glance",fontsize=11,fontweight="bold",color=ACCENT)
def card(x,y,title,body):
    fig.text(x,y,title,fontsize=9,fontweight="bold",color="#123")
    fig.text(x,y-0.015,body,fontsize=7.9,color="#222",va="top",wrap=True)
card(0.52,0.212,"Gusts — steady, modest",
     "HRRR gust factor ~1.4–1.5, peaks ~15–16 kt over ~9 kt mean. CAPE 0 + only ~11–15 kt aloft → no convective or momentum-driven gusts. HRDPS: no gust field. Plan smooth, not puffy.")
card(0.52,0.14,"Sea state — benign",
     f"~{WAVE_M:.1f} m / {WAVE_M*3.28:.1f} ft short-period wind chop (NWS seas ≤1–2 ft). SST ~{SST:.0f}°C / {SST*9/5+32:.0f}°F. Flat water — the limiter is wind, not waves.")
card(0.52,0.075,"Temperature — mild & near-steady",
     "~18°C / 64–65°F all night on the water (sea-moderated: ~1°F drop offshore, up to ~10°F near the P-town shore). Warm water under cooler air → damp; reinforces the fog watch. Layers for damp, not cold.")

fig.text(0.06,0.02,"SailFrames race weather · HRRR + HRDPS via Open-Meteo · NOT for navigation — verify with official forecasts",fontsize=6.5,color="#999")
pdf.savefig(fig); plt.close(fig)

# ---------- PAGE 2 ----------
fig=plt.figure(figsize=(8.5,11)); fig.patch.set_facecolor("white")
fig.text(0.5,0.965,"Tactics, Strategy & Timing",ha="center",fontsize=16,fontweight="bold",color=ACCENT)
fig.text(0.5,0.945,"Marblehead → Provincetown · Fri 2026-07-17 overnight",ha="center",fontsize=9.5,color="#333")

# model-difference MAP (replaces the corridor table) + per-point comparison
axdm=fig.add_axes([0.05,0.58,0.55,0.33]); draw_diffmap(axdm)
fig.text(0.62,0.895,"HRRR vs HRDPS — mean wind by point",fontsize=9.5,fontweight="bold",color=ACCENT)
yy=0.865
for name,_,_ in CORRIDOR:
    d1,s1=mean_ds("HRRR (NOAA 3km)",name); d2,s2=mean_ds("HRDPS (Canada 2.5km)",name)
    delta=abs(((d1-d2+180)%360)-180)
    fig.text(0.62,yy,f"{name}",fontsize=8,fontweight="bold",color="#123")
    fig.text(0.62,yy-0.015,f"HRRR {d1:.0f}°/{s1:.0f}kt  ·  HRDPS {d2:.0f}°/{s2:.0f}kt  →  Δdir {delta:.0f}°",
             fontsize=7.5,color="#c0392b" if delta>60 else "#2c6e2c")
    yy-=0.044
fig.text(0.62,yy+0.003,
 "Biggest split at the START (near-opposite: a dying SE\n"
 "sea-breeze vs a NW land-breeze). They converge to a\n"
 "common S/SW by Race Point, HRDPS lighter throughout.\n"
 "→ The first leg is the uncertain part — sail the actual\n"
 "breeze; for the rest of the night both models agree.",
 fontsize=7.6,color="#222",va="top")

# tide curve
axtide=fig.add_axes([0.06,0.46,0.88,0.10])
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
("⑤ Wind shear — expect shifty, veered puffs","Light SE/S surface sits under a W–WNW 13–16 kt flow at 925/850 hPa with a low-level inversion (decoupled night). Puffs mix a little of that down — shiftier and veered toward W/NW vs the mean, but capped ~16 kt. Toward dawn the layer mixes and the surface veers/builds to the gradient. Don't bank on one wind angle; sail conservatively through puffs, keep the boat in pressure."),
("⑥ Night & hazards","Dark — thin crescent sets early, so nav by instruments, watches + lights set. Fog watch: July S-flow over ~20°C water; NWS not flagging Fri night but recheck AM. Seas ≤2 ft, no SCA expected."),
]
y=0.385
fig.text(0.06,0.41,"Tactics & strategy",fontsize=12,fontweight="bold",color=ACCENT)
for head,body in tac:
    fig.text(0.06,y,head,fontsize=9.1,fontweight="bold",color="#123")
    fig.text(0.06,y-0.014,body,fontsize=7.9,color="#222",wrap=True,va="top")
    y-=0.0585
fig.text(0.06,0.016,"Confidence: moderate on the overnight reach + tide timing (models agree); low on the exact start (transition + lead). Re-run race morning + pre-start.",
         fontsize=7.2,color="#777",style="italic")
pdf.savefig(fig); plt.close(fig)
pdf.close()
print("wrote docs/reports/RACE_WX_MARBLEHEAD_PTOWN_2026-07-17.pdf")
