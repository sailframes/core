#!/usr/bin/env python3
"""Beringer Bowl — Man of War (MOW) competitor sheet.
Overnight JAM fleet (Fleet F = MOW's class + Fleet G), sorted by PHRF Time-on-Time factor,
with time owed vs MOW. Source: club entry sheet (downloaded 2026-07-17). Landscape 1-page PDF.
"""
import datetime as dt
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

ACCENT="#0b3d66"; MOWHL="#ffe08a"
MOW_TCF=1.016
T_EST=8*3600          # ~8 h expected elapsed for this ~40 nm light-air race
# (boat, sail, tcf, type, owner, yc)  — Overnight JAM entrants
FLEET_F=[("BORA","52496",1.020,"X-37","Api Rudich","Eastern YC"),
         ("GOLDENEYE","40180",1.020,"Jeanneau Sel. 37","Peter Engel","—"),
         ("MAN OF WAR","32559",1.016,"C&C 41","Chris Lubeck","Eastern YC"),
         ("ARES","30078",1.002,"C&C 40-2 TR","Linda Hosking","Boston YC"),
         ("KAILOA","100",1.002,"Hanse 458","Scott McMillan","Boston YC"),
         ("CELEBRATION","31910",0.937,"Sabre 38","Tom Wanderer","Jubilee YC"),
         ("CHINGONA","40151",0.929,"Jeanneau SO 37","Ryan Fitzgerald","Savin Hill YC")]
FLEET_G=[("ADEUX","073",0.921,"Tartan 3400","Bill Sullivan","Swampscott YC"),
         ("REGULUS","61848",0.917,"Tartan 372","Chris","Swampscott"),
         ("INVICTUS","799",0.890,"Pearson 32","Edward Ryan","—"),
         ("SEA SEÑORA","1001",0.866,"Pearson 30","Joe","Swampscott YC"),
         ("TRUANT","51743",0.849,"Pearson 303","Matt Bachman","Swampscott")]

def owed(tcf):            # signed seconds vs MOW over T_EST  (+ = boat owes MOW; − = MOW owes boat)
    return (tcf-MOW_TCF)*T_EST
def fmt(sec):
    if abs(sec)<1: return "— scratch"
    sign="+" if sec>0 else "−"; a=int(round(abs(sec)))
    h,m,s=a//3600,(a%3600)//60,a%60
    return (f"{sign}{h}:{m:02d}:{s:02d}" if h else f"{sign}{m}:{s:02d}")
def perhr(tcf):
    v=(tcf-MOW_TCF)*3600
    if abs(v)<0.5: return "0"
    return ("+" if v>0 else "−")+str(int(round(abs(v))))

gen=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
pdf=PdfPages("docs/reports/BERINGER_BOWL_MOW_COMPETITORS_2026-07-17.pdf")
fig=plt.figure(figsize=(11,8.5)); fig.patch.set_facecolor("white")
fig.text(0.5,0.95,"Beringer Bowl — Man of War (MOW) Competitors",ha="center",fontsize=18,fontweight="bold",color=ACCENT)
fig.text(0.5,0.917,"Overnight JAM course (Marblehead → Provincetown) · MOW = sail 32559, C&C 41, Chris Lubeck, Eastern YC · PHRF rating 1.016 (Fleet F)",
         ha="center",fontsize=10.5,color="#333")
fig.text(0.5,0.892,"Sorted by handicap (Time-on-Time factor, high = faster-rated). MOW is the 3rd-highest rating of 12 — it OWES time to 9 of its 11 rivals (only Bora & Goldeneye owe MOW).",
         ha="center",fontsize=9,color="#666",style="italic")

cols=["Sail","Boat","Type","Owner","Yacht club","TCF","vs MOW /hr","vs MOW (~8 h)"]
cw =[0.06,0.155,0.17,0.16,0.155,0.06,0.085,0.105]

def table(ax,rows,title):
    ax.axis("off"); ax.set_title(title,fontsize=12,fontweight="bold",color=ACCENT,loc="left",pad=6)
    cells=[]
    for (boat,sail,tcf,typ,owner,yc) in rows:
        cells.append([sail,boat,typ,owner,yc,f"{tcf:.3f}",perhr(tcf),fmt(owed(tcf))])
    t=ax.table(cellText=cells,colLabels=cols,colWidths=cw,loc="center",cellLoc="left")
    t.auto_set_font_size(False); t.set_fontsize(8.7); t.scale(1,1.5)
    for (r,c),cl in t.get_celld().items():
        cl.set_edgecolor("#ccc")
        if r==0: cl.set_facecolor(ACCENT); cl.set_text_props(color="w",fontweight="bold",fontsize=8.4)
        elif rows[r-1][0]=="MAN OF WAR": cl.set_facecolor(MOWHL); cl.set_text_props(fontweight="bold")
        if r>0 and c in (5,6,7): cl.set_text_props(ha="center") if False else cl.get_text().set_ha("center")
    return t

axF=fig.add_axes([0.04,0.55,0.92,0.28]); table(axF,FLEET_F,"Fleet F — MOW's scoring class (7 boats)")
axG=fig.add_axes([0.04,0.24,0.92,0.21]); table(axG,FLEET_G,"Fleet G — same Overnight course, other class (5 boats)")

note=("HANDICAP = Time-on-Time factor: corrected time = elapsed × factor (lower corrected wins; higher factor = faster-rated boat, so it must "
 "sail faster to compensate).   “vs MOW” = (their factor − MOW 1.016) × elapsed.   + = the boat OWES MOW (must beat MOW across the line by that "
 "much to tie on corrected).   − = MOW OWES the boat (MOW must beat them across the line).   “/hr” = seconds per hour of elapsed; the “~8 h” column "
 "applies it to this race’s ~8 h expected elapsed (light air, ~40 nm) — scale it to your actual finish time using the /hr value.")
fig.text(0.04,0.16,note,fontsize=8.6,color="#222",va="top",wrap=True)
fig.text(0.04,0.03,f"Source: Beringer Bowl entry sheet (downloaded 2026-07-17) · generated {gen} · handicap math per Time-on-Time; verify against the official NoR/scoring.",
         fontsize=7,color="#999")
pdf.savefig(fig); plt.close(fig); pdf.close()
print("wrote docs/reports/BERINGER_BOWL_MOW_COMPETITORS_2026-07-17.pdf")
