#!/usr/bin/env python3
"""Delivery plan — Eagle (Najad 380, dual-handed): Boston -> Marion -> Edgartown.
The hero is CURRENT TIMING: Cape Cod Canal (westbound = ride the WSW ebb) + Woods Hole
(transit at slack). Wind/fog supporting. Data: NOAA CO-OPS currents (COD0904, COD0911),
Open-Meteo wind, NWS marine zones. 2-page PDF, race-report visual style."""
import datetime as dt, math
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Circle

ACCENT="#0b3d66"; SEA="#eef4f8"; LAND="#e7e1d4"; GATE="#c0392b"; ROUTEC="#0b3d66"
gen=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")

# ---- waypoints (lon,lat) ----
WP=[("Boston",-70.98,42.35),("Canal E\n(Sandwich)",-70.50,41.775),("Canal W",-70.63,41.74),
    ("Marion",-70.765,41.70),("Woods Hole",-70.683,41.519),("Edgartown",-70.51,41.39)]
ROUTE=[(-70.98,42.35),(-70.50,41.775),(-70.63,41.74),(-70.765,41.70),(-70.683,41.519),(-70.51,41.39)]
GATES=[("⚡ CAPE COD CANAL",-70.565,41.758),("⚡ WOODS HOLE",-70.683,41.519)]

# ---- coarse land (schematic, for orientation) ----
MAINLAND=[(-71.15,42.45),(-70.98,42.35),(-70.90,42.27),(-70.78,42.10),(-70.66,41.96),(-70.60,41.86),
 (-70.53,41.79),(-70.50,41.775),(-70.545,41.74),(-70.64,41.74),(-70.70,41.745),(-70.75,41.72),
 (-70.78,41.70),(-70.80,41.685),(-70.88,41.64),(-70.96,41.61),(-71.05,41.58),(-71.15,41.55)]
CAPECOD=[(-70.50,41.775),(-70.38,41.76),(-70.25,41.74),(-70.12,41.75),(-70.02,41.83),(-70.00,41.95),
 (-70.05,42.05),(-70.10,42.07),(-70.06,41.98),(-70.10,41.72),(-70.30,41.62),(-70.50,41.55),
 (-70.55,41.55),(-70.60,41.58),(-70.62,41.65),(-70.60,41.70),(-70.545,41.74)]
ELIZ=[(-70.69,41.515),(-70.76,41.48),(-70.83,41.45),(-70.90,41.43),(-70.95,41.42)]   # island chain (Woods Hole->Cuttyhunk)
MV=[(-70.62,41.47),(-70.51,41.40),(-70.47,41.35),(-70.55,41.30),(-70.76,41.31),(-70.84,41.35),(-70.75,41.44),(-70.62,41.47)]

def draw_map(ax):
    ax.set_facecolor(SEA)
    for poly,lw in [(MAINLAND,0),(CAPECOD,0),(MV,0)]:
        ax.fill([p[0] for p in poly],[p[1] for p in poly],color=LAND,zorder=1,lw=0)
    ax.plot([p[0] for p in ELIZ],[p[1] for p in ELIZ],color="#b3a98e",lw=5,solid_capstyle="round",zorder=1)
    # route
    ax.plot([p[0] for p in ROUTE],[p[1] for p in ROUTE],"-",color=ROUTEC,lw=2.4,zorder=5,solid_capstyle="round")
    for name,lo,la in WP:
        ax.plot(lo,la,"o",ms=6,mfc="w",mec=ROUTEC,mew=1.8,zorder=6)
        dx,dy=(6,4)
        if name in("Marion","Woods Hole"): dx,dy=(-8,-14)
        if name=="Edgartown": dx,dy=(6,-10)
        ax.annotate(name,(lo,la),xytext=(dx,dy),textcoords="offset points",fontsize=8,fontweight="bold",color=ROUTEC)
    for lab,lo,la in GATES:
        ax.plot(lo,la,marker="*",ms=17,mfc=GATE,mec="k",mew=0.8,zorder=7)
        ax.annotate(lab,(lo,la),xytext=(9,-2),textcoords="offset points",fontsize=8,fontweight="bold",color=GATE)
    for lab,lo,la,c in [("Mass Bay",-70.72,42.12,"#5b6b7a"),("Cape Cod Bay",-70.35,41.86,"#5b6b7a"),
        ("BUZZARDS BAY",-70.85,41.70,"#487a9a"),("VINEYARD SOUND",-70.62,41.44,"#487a9a"),
        ("CAPE COD",-70.18,41.70,"#7a6a4a"),("MARTHA'S\nVINEYARD",-70.66,41.36,"#7a6a4a"),("Elizabeth Is.",-70.86,41.43,"#7a6a4a")]:
        ax.annotate(lab,(lo,la),fontsize=6.8,color=c,style="italic",ha="center")
    ax.set_xlim(-71.12,-69.98); ax.set_ylim(41.28,42.45); ax.set_aspect(1/math.cos(math.radians(41.8)))
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title("Route — Boston → Cape Cod Canal → Marion → Woods Hole → Edgartown",fontsize=9.5,fontweight="bold",color=ACCENT)

# ================= PAGE 1 =================
pdf=PdfPages("docs/reports/DELIVERY_EAGLE_BOSTON_EDGARTOWN_2026-07-21.pdf")
fig=plt.figure(figsize=(8.5,11)); fig.patch.set_facecolor("white")
fig.text(0.5,0.967,"Delivery Plan — Eagle (Najad 380, dual-handed)",ha="center",fontsize=16.5,fontweight="bold",color=ACCENT)
fig.text(0.5,0.945,"Boston → Marion → Edgartown, MA  ·  depart Tue 2026-07-21 08:00 EDT  ·  Leg 1 ~55 nm · Leg 2 ~22 nm  ·  assumed ~6 kt",ha="center",fontsize=9.5,color="#333")
fig.text(0.5,0.927,"TWO CURRENT GATES decide this trip — Cape Cod Canal (ride the ebb) + Woods Hole (transit at slack); both run 3–5 kt.",ha="center",fontsize=8.4,color=GATE,fontweight="bold")
fig.text(0.5,0.912,f"generated {gen} · currents NOAA CO-OPS (COD0904 Canal RR-bridge, COD0911 Woods Hole) · wind Open-Meteo · ~1 day lead, re-check race morning.",ha="center",fontsize=7.4,color="#999",style="italic")

axm=fig.add_axes([0.05,0.52,0.90,0.36]); draw_map(axm)

# --- Current-gate table (the hero) ---
fig.text(0.06,0.475,"⚡ Current gates — get these right",fontsize=12.5,fontweight="bold",color=GATE)
axg=fig.add_axes([0.06,0.285,0.88,0.175]); axg.axis("off")
rows=[
 ["CAPE COD CANAL\n(westbound →\nride the EBB)","Tue\n07-21",
  "EBB sets WSW 248° toward Buzzards Bay\n= FAIR. Fair: 04:00–09:50 (too early) &\n16:20–22:20 (max ebb 18:53, −4.2 kt).",
  "AVOID max FLOOD 13:51 (+3.8 kt, nose).\nMotor only (no sailing); Canal Control\nVHF 13; obey signal lights; 10 mph limit."],
 ["WOODS HOLE\n(transit at\nSLACK)","Wed\n07-22",
  "SLACK 05:38 · 11:42 · 18:04.\nTarget 11:42 — through in 20–30 min.",
  "Set (NOAA): flood 077°/ENE · ebb 261°/WSW\n— cross-check Eldridge + local knowledge.\nRocks + eddies; bail to Quicks Hole in 20+ kt."],
]
t=axg.table(cellText=rows,colLabels=["Gate","Day","Timing (EDT)","Notes / cautions"],colWidths=[0.18,0.075,0.375,0.37],loc="center",cellLoc="left")
t.auto_set_font_size(False); t.set_fontsize(7.5); t.scale(1,4.0)
for (r,c),cl in t.get_celld().items():
    cl.set_edgecolor("#ccc"); cl.set_text_props(va="center")
    if r==0: cl.set_facecolor(GATE); cl.set_text_props(color="w",fontweight="bold",fontsize=7.7,va="center")

# --- Leg-by-leg timing ---
fig.text(0.06,0.263,"Leg-by-leg timing (from 08:00 start, ~6 kt)",fontsize=12,fontweight="bold",color=ACCENT)
leg1=("LEG 1 · Tue 07-21 · Boston → Marion · ~55 nm (via the Canal)",
 "08:00 depart Boston; motor-sail SE across Mass/Cape Cod Bay.  ~15:00 reach the Canal E entrance (Sandwich) — the canal is "
 "FLOODING against you (easing). DECISION: (A) ease to ~5.5 kt / hold off Sandwich to the 16:20 slack, then ride the building WSW "
 "ebb (up to +4 kt) — through by ~17:00; or (B) push on at ~15:00 and punch the easing flood (~2–2.5 kt, SOG ~3.5 kt), slack "
 "mid-canal ~16:20, ebb helps the 2nd half — through by ~16:15.  Exit W end ~16:30 → ~9 nm up Buzzards Bay → Marion ~18:00–18:30 "
 "(sunset 20:10, daylight).")
leg2=("LEG 2 · Wed 07-22 · Marion → Edgartown · ~22 nm (via Woods Hole)",
 "Target the 11:42 Woods Hole slack. Depart Marion ~09:40; ~11 nm down Buzzards Bay (building downwind SW chop).  ~11:40 Woods "
 "Hole — transit AT slack, through in 20–30 min.  ~12:10 into Vineyard Sound → ~11 nm → Edgartown ~14:00 (daylight).  ⚠ WIND: Wed "
 "builds a strong SW sea-breeze (15–20 kt, GUSTS 26–35 in Buzzards Bay) through the afternoon — REEF before leaving Marion; the "
 "morning schedule front-loads the leg before the worst.")
y=0.235
for head,body in (leg1,leg2):
    fig.text(0.06,y,head,fontsize=9.2,fontweight="bold",color="#123")
    fig.text(0.06,y-0.016,body,fontsize=8.1,color="#222",va="top",wrap=True)
    y-=0.11
fig.text(0.06,0.02,"SailFrames delivery plan · NOT for navigation — verify against Eldridge, NOAA charts, Canal Control, and current conditions.",fontsize=6.5,color="#999")
pdf.savefig(fig); plt.close(fig)

# ================= PAGE 2 =================
fig=plt.figure(figsize=(8.5,11)); fig.patch.set_facecolor("white")
fig.text(0.5,0.965,"Weather, Hazards & Full Current Tables",ha="center",fontsize=15,fontweight="bold",color=ACCENT)
fig.text(0.5,0.945,"Eagle · Boston → Marion → Edgartown · 2026-07-21/22",ha="center",fontsize=9.5,color="#333")

# wind table
fig.text(0.06,0.915,"Wind & visibility (Open-Meteo, kt)",fontsize=12,fontweight="bold",color=ACCENT)
wrows=[
 ["Tue 07-21 AM (Leg 1)","S/SE 8–10","S 8–10","—","good (13–18 km)"],
 ["Tue 07-21 PM","SE 12–15 G18–23","S ~9","—","good"],
 ["Wed 07-22 (Leg 2)","—","S/SW 15–20 G26–35","S 14–16 G30","good (13–22 km)"],
]
axw=fig.add_axes([0.06,0.80,0.88,0.085]); axw.axis("off")
tw=axw.table(cellText=wrows,colLabels=["When","Mass/Cape Cod Bay","Buzzards Bay","Vineyard Sound","Fog / visibility"],
             colWidths=[0.19,0.20,0.20,0.19,0.20],loc="center",cellLoc="left")
tw.auto_set_font_size(False); tw.set_fontsize(7.7); tw.scale(1,1.7)
for (r,c),cl in tw.get_celld().items():
    cl.set_edgecolor("#ccc")
    if r==0: cl.set_facecolor(ACCENT); cl.set_text_props(color="w",fontweight="bold",fontsize=7.5)
fig.text(0.06,0.775,"FOG: none in the NWS forecast for either day (zones ANZ235/236/237 clean; vis 12–22 km) — good news for Woods Hole. Still July: recheck each morning.",
         fontsize=8.2,color="#222",va="top",wrap=True)

# hazards / procedures
fig.text(0.06,0.735,"Hazards & procedures",fontsize=12,fontweight="bold",color=ACCENT)
haz=[
 ("Cape Cod Canal","Motor ONLY (sailing prohibited). Monitor Canal Control VHF 13; obey the traffic-signal lights at each end; 10 mph (≈8.7 kt over ground) limit. Land cut ~7 nm; strong wakes from traffic. Ride the ebb (§ page 1)."),
 ("Woods Hole","Transit at/near SLACK (11:42 Wed) only — rock-strewn, strong cross-set + eddies, heavy ferry traffic. Follow the marked channel (green-to-starboard inbound to Buzzards Bay = reverse for us). Cross-check Eldridge current diagram. Bail-out: Quicks Hole (wider, far less current) or wait for the next slack."),
 ("Wednesday SW blow","Buzzards Bay's afternoon 'smoky sou'wester' builds to 15–20 G26–35. Reef at the dock in Marion; the Marion→Woods Hole run is a building downwind chop. If Woods Hole/Vineyard Sound look bad in 20+ kt, delay or use Quicks Hole."),
 ("Daylight","Sunrise ~05:26 / sunset ~20:10 both days. Every transit (Canal ~16:30, Marion arr ~18:00, Woods Hole 11:42, Edgartown ~14:00) is in daylight — no night pilotage required if on schedule."),
 ("Dual-handed","Long Leg-1 day (~10 h). Set watches, autopilot, jacklines/tethers for the open-water legs; pre-stage the canal + Woods Hole waypoints and current times before departure."),
]
y=0.71
for h,b in haz:
    fig.text(0.07,y,"• "+h,fontsize=9.1,fontweight="bold",color="#123")
    fig.text(0.09,y-0.015,b,fontsize=8.0,color="#222",va="top",wrap=True)
    nlines=max(2,(len(b)+128)//129)   # variable row height so a long bullet won't collide
    y-=0.021+0.0135*nlines

# full current tables
fig.text(0.06,0.40,"Full current predictions (NOAA CO-OPS, EDT)",fontsize=11,fontweight="bold",color=ACCENT)
ccc=("CAPE COD CANAL (COD0904, RR bridge) — flood 66°→Cape Cod Bay / ebb 248°→Buzzards Bay\n"
 "  Tue 07-21:  01:27 flood +3.6 · 03:52 SLACK · 06:17 EBB −4.1 · 09:53 SLACK · 13:51 flood +3.8 · 16:20 SLACK · 18:53 EBB −4.2 · 22:30 SLACK\n"
 "  (westbound wants the EBB → the 16:20→22:20 window is your gate.)")
wh=("WOODS HOLE (COD0911, The Strait) — flood 077° / ebb 261°; max ~2.4–3.2 kt\n"
 "  Wed 07-22:  03:28 flood +2.9 · 05:38 SLACK · 07:36 ebb −2.4 · 11:42 SLACK · 15:50 flood +3.0 · 18:04 SLACK · 21:49 ebb −2.5\n"
 "  (transit at the 11:42 SLACK.)")
fig.text(0.06,0.385,ccc,fontsize=7.6,color="#222",va="top",family="monospace")
fig.text(0.06,0.315,wh,fontsize=7.6,color="#222",va="top",family="monospace")
fig.text(0.06,0.02,f"SailFrames delivery plan · generated {gen} · NOT for navigation — verify against Eldridge, NOAA charts + Canal Control.",fontsize=6.5,color="#999")
pdf.savefig(fig); plt.close(fig); pdf.close()
print("wrote docs/reports/DELIVERY_EAGLE_BOSTON_EDGARTOWN_2026-07-21.pdf")
