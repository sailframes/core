# HRRR vs WRF-SailFrames — detailed comparison + improvement roadmap

*Context: 2026-07 gom-seabreeze effort. WRF-SailFrames = the 9/3/1 km WRF-ARW 4.8 sea-breeze
downscale over Massachusetts Bay (GFS-driven, surface obs-nudged, coldest-pixel SST, NLCD
geography). HRRR = NOAA's operational 3 km convection-allowing model. Both are **WRF-ARW** — the
difference is configuration + the data-assimilation/operational machine around the core.*

---

## STATUS UPDATE (2026-07-14) — what changed, what remains

Since the original comparison, several gaps CLOSED and one flipped in SailFrames' favor:

| Weakness | Was | Now |
|---|---|---|
| W1 HRRR-driven | GFS-driven, d01 overran CONUS | **CONFIRMED end-to-end (2026-07-14):** domain fits CONUS; wrfnat native atmos (51 lvl) + **dual-sourced wrfprs soil** (9 lvl) → real.exe writes valid IC/BC. Ingest done. |
| W2/W3 obs / gradient | surface-only, none | **obs-nudging BUILT + VALIDATED** — held-out 44013 MAE 2.80 vs free 3.22 kt; MADIS aircraft drafted (not run) |
| W5 gusts | parameterized (≈HRRR) | **100 m LES BUILT + VALIDATED — RESOLVED gusts (gust factor 1.28); SailFrames now EXCEEDS HRRR here** (HRRR never runs LES) |
| output | thin | LES 111 m gust source on the dashboard; gust_viz (movie/gustiness/trace) |

**Current head-to-head:** SailFrames now **wins** resolution (1 km + **111 m LES**), SST, geography, **and resolved gusts**; HRRR still **wins** assimilation (radar/aircraft/satellite, **hourly cycling**) + operational robustness.

**Still remaining (in leverage order):** (1) ~~confirm the HRRR-driven ingest~~ **DONE 2026-07-14** → HRRR-driving now inherits HRRR's hourly DA; (2) **operational day-of** — near-done (`forecast_today.sh` + `OPERATIONAL_DAYOF.md`), just needed the ingest that now works; (3) **MADIS aircraft + mesonet** obs (drafted) for upper-air + in-gradient anchoring — its own sub-project; (4) **cycling** (HRRR re-init cadence first, LES warm-start later — designed in OPERATIONAL_DAYOF.md). The core science (resolution, SST, gusts, obs-nudging) is done; what remains is **assimilation depth + making it operational.**

---

## 0. TL;DR

- **Same engine, different machine.** HRRR *is* a WRF-ARW configuration. So this is not "which
  model core is better" — it's **HRRR's hourly data-assimilation + operational robustness** vs
  **SailFrames' finer resolution + sea-breeze-tuned lower boundary**.
- **HRRR wins** on: assimilation (radar/aircraft/satellite/mesonet, hourly-cycled), operational
  reliability, richer output, verification maturity.
- **SailFrames wins** on: ~3× finer *effective* resolution (1 km→~7 km eff vs 3 km→~20 km eff),
  high-res coldest-pixel SST tuned for the land-sea thermal contrast that drives the sea breeze,
  250 m NLCD coastline/land-use, and full customizability (LES, obs choice).
- **The single highest-leverage upgrade: drive WRF-SailFrames FROM HRRR instead of GFS.** That
  inherits HRRR's entire assimilated state (radar, aircraft, satellite, mesonet) as the large
  scale, then adds 1 km resolution + high-res SST on top = best of both. (We attempted this; it
  failed on domain-fit — d01 poked outside the HRRR CONUS grid. The fix is a domain redesign.)

---

## 1. Side-by-side parameters

| Parameter | HRRR (operational) | WRF-SailFrames (gom-seabreeze) | Edge |
|---|---|---|---|
| Dynamical core | WRF-ARW ~v3.9+ | **WRF-ARW 4.8.0** (newer) | ~tie (SF newer) |
| Horizontal grid | 3 km CONUS+Alaska | 9/3/**1 km** nests, Mass Bay | **SF** (1 km) |
| Effective resolution | ~15–20 km (≈7Δx) | **~7 km** (≈7Δx at 1 km) | **SF** (~3× finer) |
| Domain | CONUS (1799×1059) | tiny race tile (~80×75 @1 km) | HRRR (coverage) / SF (focus) |
| Vertical levels | 50, top ~15 km/20 hPa | ~60, top 100 hPa | ~tie |
| Driving / LBCs | RAP 13 km (itself assimilated), hourly | **GFS 0.25°, 3-hourly** (coarser, staler) | **HRRR** |
| Data assimilation | **GSI hybrid 3D-EnVar, 36-mem 3 km ensemble (HRRRDAS)** | **obs-nudging (FDDA), surface stns only** | **HRRR** (huge) |
| — radar | reflectivity DA + latent-heat spin-up | **none** | **HRRR** |
| — aircraft | ACARS/AMDAR/Mode-S assimilated | none yet (MADIS draft ready) | **HRRR** (SF closing) |
| — satellite | GOES clear-sky radiances + GLM lightning | none | **HRRR** |
| — surface/mesonet | MADIS mesonet assimilated | 6 stations nudged (→MADIS mesonet) | HRRR (SF closing) |
| Cycling | **hourly** (fresh analysis every hour) | **single daily cold-start** | **HRRR** (huge for nowcast) |
| SST lower boundary | coarse SST analysis (RTG/GFS-class) | **coldest-pixel ACSPO/MUR high-res** | **SF** (sea-breeze critical) |
| Land surface / geog | RUC LSM, ~standard geography | Noah LSM, **NLCD 250 m** land-use | **SF** (finer coast/urban) |
| PBL / sfc layer | MYNN-EDMF | MYNN (free) / YSU+MM5-MOST (nudged) | HRRR (EDMF, tuned) |
| Microphysics | Thompson-Eidhammer aerosol-aware | Thompson (template) | HRRR (tuned) |
| Gusts / turbulence | PBL-parameterized (WSPD10MAX≈sustained) | **same** (parameterized) | tie (both need LES) |
| Output fields | rich GRIB2 (refl, CAPE, gust, 3D, …) | wind + T2 → parquet (minimal) | **HRRR** |
| Update cadence | operational 24/7, ~50 min latency | on-demand EC2, ~2 h/run | **HRRR** |
| Verification | extensive, operational | just started (yellow-zone, holdout MAE) | **HRRR** |
| Cost | national infra | ~$1.5/run Spot (credits) | SF (cheap/flexible) |

---

## 2. Where WRF-SailFrames already beats HRRR

1. **Effective resolution ~3× finer.** HRRR's 3 km grid → ~20 km effective; it *smooths* the
   Mass Bay coastline, Cape Ann, and — proven this session — the **~5 kt cross-course sea-breeze
   gradient** (the July-4 yellow-zone event). SailFrames' 1 km (~7 km eff) resolves it. For a 2 nm
   race course, that difference is the whole tactical game.
2. **Sea-breeze-tuned SST.** The coldest-pixel ACSPO/MUR composite gives a sharper, colder
   coastal SST band than HRRR's coarse SST analysis. Since the sea breeze is *driven* by the
   land-sea thermal contrast, a better SST directly sharpens onset timing + strength.
3. **250 m NLCD land-use + coastline** vs HRRR's coarser geography → better differential heating
   (Boston→Salem→Marblehead urban gradient) at the scale the breeze responds to.
4. **Newer core (4.8) + full control** — LES, obs selection, physics all tunable for *this* problem.

Empirically: an earlier point comparison had HRRR ~2.9 kt vs free WRF ~3.6 kt at buoy 44013
(HRRR's assimilation winning on average) — but **WRF won the sea-breeze onset timing** (resolution).
After adding obs-nudging this session, the held-out WRF-nudged MAE fell to **2.80 kt** — i.e.
nudging closed most of the gap to HRRR while keeping the resolution edge.

---

## 3. Where WRF-SailFrames is WEAK — and how to fix each (vs HRRR)

Ranked by leverage.

### W1 — No real data assimilation / not HRRR-driven  ⭐ highest leverage
**Weakness:** driven by GFS 0.25° (3-hourly, coarse, *un*-assimilated at the mesoscale) with only
surface obs-nudging bolted on. HRRR's advantage is 90% its hourly GSI machine.
**Fix (biggest win): drive WRF-SailFrames FROM HRRR, not GFS.** One-way nest the 1 km run inside
HRRR → inherit HRRR's *entire* assimilated state (radar, aircraft, satellite, mesonet) as the
large scale, then add 1 km + high-res SST + NLCD on top. This is the RU-WRF-style pattern and is
*the* move: HRRR's accuracy × SailFrames' resolution. **Blocker (known):** our d01 pokes outside
the HRRR CONUS grid → metgrid fails. **Do:** redesign so the outer domain fits inside HRRR CONUS
(shrink d01 / reposition), Vtable.raphrrr, 3-hourly→hourly LBCs. Fallback stays GFS.

### W2 — Surface-only obs; no upper air  ⭐ high
**Weakness:** obsgrid reported "0 upper-air obs"; nudging can't shape the vertical structure
(inversion height, breeze depth, return flow) that HRRR gets from aircraft.
**Fix:** the **MADIS ACARS aircraft profiles** already drafted (`fetch_madis_acars.py`) — KBOS
ascent/descent soundings = the *same* GDC-ABO data HRRR assimilates. Wire it in + add **restricted
MADIS mesonet** (denser surface net). This also fixes W3.

### W3 — Sparse obs smear the gradient  ⭐ high (tactical)
**Weakness:** this session's finding — nudging *improves the point* (44013 MAE 3.22→2.80) but
*smooths the cross-course gradient* (yellow-zone 5 kt→2 kt) because no station sits in the zone.
HRRR sidesteps this by assimilating a dense network + radar.
**Fix:** obs *inside* the gradient — MADIS mesonet (CWOP/DOT) across the course + eventually fleet
GPS wind proxy. Or, where obs are sparse, trust the *free* run's gradient (which the dashboard
toggle now lets you compare).

### W4 — No cycling; single cold-start
**Weakness:** HRRR refreshes hourly with new obs; SailFrames is one daily cold-start with a
spin-up transient. For nowcasting "where's the front now," HRRR wins outright.
**Fix:** implement **hourly/3-hourly cycling** — warm-start each run from the previous + nudge the
latest obs (partial-cycling). Cheaper than full DA, captures most of the nowcast value.

### W5 — Gusts/oscillations unresolved
**Weakness:** *both* models parameterize turbulence (WSPD10MAX≈sustained). Neither gives resolved
puffs. (Not a HRRR advantage — a shared limit.)
**Fix:** the **100 m LES nest** already designed (`LES_100M_DESIGN.md`) — resolves rolls/thermals
→ gust magnitude, roll spacing, oscillation period. This is where SailFrames can *exceed* HRRR
(HRRR will never run LES operationally).

### W6 — No radar → blind to fronts/convection in the analysis
**Weakness:** HRRR assimilates NEXRAD reflectivity; SailFrames can't (nudging can't ingest radar).
**Fix:** two paths — (a) W1 (HRRR-driven) inherits the radar-informed state for free; (b) use
KBOX radar **fine-line** as *validation/nowcast* truth (script exists: `kbox_fine_line.py`), not DA.
Full radar DA (WRFDA/GSI) is a large separate build — low priority given (a).

### W7 — Thin output; no operational robustness
**Weakness:** backfill emits only 10 m wind + T2; runs are manual/on-demand and fragile.
**Fix:** expand `backfill_wrf.py` to emit gust/CAPE/reflectivity-proxy/3D; **automate** (scheduled
run, health checks, retries) so it's a dependable daily product, not a hand-launched experiment.

### W8 — Physics less tuned than HRRR's operational suite
**Weakness:** HRRR's Thompson-Eidhammer + MYNN-EDMF + RUC LSM are years-tuned for CONUS convection.
**Fix:** adopt HRRR's physics suite where compatible (MYNN-EDMF PBL, Thompson-Eidhammer MP) for
consistency; keep Noah/NLCD for the coastal detail. (Note: obs-nudging forces YSU+MM5-MOST — a
constraint to revisit if we move to HRRR-driven + less nudging.)

---

## 4. Prioritized roadmap (what to build, in order)

1. **HRRR-driven WRF-SailFrames** (W1) — redesign domain to fit inside HRRR CONUS, drive from HRRR
   hourly. *Single biggest accuracy gain; inherits radar+aircraft+satellite for free.*
2. **MADIS aircraft + mesonet obs** (W2/W3) — draft ready; fixes upper-air + the gradient smear.
3. **100 m LES nest** (W5) — the gust/oscillation forecast; where SailFrames *beats* HRRR.
4. **Cycling** (W4) — hourly/3-hourly warm-start for nowcast parity.
5. **Output + automation** (W7) — make it a dependable product.

**Bottom line:** HRRR's edge is its *assimilation + operations*, not its core or its resolution.
The fastest way to "beat HRRR for coastal racing" is to **stand on HRRR** (drive from it) and add
the two things HRRR structurally can't: **1 km resolution with a sea-breeze-tuned SST**, and
eventually **LES-resolved gusts**.
</content>
