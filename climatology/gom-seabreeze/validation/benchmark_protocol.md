# Benchmark Protocol — commercial models vs. Salem Sound truth

**Goal.** Quantify how much skill PredictWind PWG (1 km) and Expedition WRF add
over an HRRR baseline for Salem Sound race-day wind, so the build/buy decision on
the custom `gom-seabreeze` WRF run is a **number, not a hunch**.

**Decision variable.** Skill improvement of each candidate over HRRR and over
persistence, **stratified by sea-breeze type**, on the tactically-relevant
metrics. The decision lives in the *transition-day* bin.

---

## 1. Truth sources (no gridded pure-obs wind exists)

Point / feature truth, in priority:
1. **NDBC 44013** (Mass Bay) — calibrated wind + SST; the anchor. Offshore of the
   mark field, so necessary but not sufficient.
2. **Wind-instrumented boat** (Calypso + Vakaros) — *when it exists*. TWD/TWS
   along a track inside the race area = highest-value verification. Apply the
   Calypso 180° AWA fix. **Not available today** → omit for now.
3. **KBVY** (Beverly ASOS, N edge) — sea-breeze onset sentinel (shore timing).
4. **Fleet wind proxy** (`fleet/fleet_wind_proxy.py`) — upwind-leg TWD +
   cross-course gradient from fleet GPS. Low absolute accuracy; use as a
   **shift-timing + gradient detector**, not a speed truth.
5. **Sentinel-1 SAR** (~1 km ocean wind) — sporadic; grab overpasses that line up
   with a race.

Gridded **context** (not scorers): RTMA/URMA (2.5 km hourly) is background-
dominated over water; HRRR analysis as truth for HRRR forecasts is circular.

**Current contamination:** boat-derived wind needs STW, not GPS SOG. Restrict
wind-truth windows to slack ±1 h, or correct SOG with a tidal-current model
(Weather4D NCOM/HYCOM, or harmonic prediction at the nearest station).

## 2. Freeze the forecasts (as issued pre-race, not hindcast)

For each race day, archive the run valid over the race window (e.g. 10:00–16:00
LT) at a **fixed forecast lead**:
- **PWG** — GRIB via PredictWind download/API for the Salem Sound tile.
- **Expedition WRF** — on-demand Salem Sound GRIB.
- **HRRR** — from AWS via hrrrzarr (same valid time + lead).
Score all three at the same lead so it's apples-to-apples.

## 3. Match

Bilinear-interpolate each model's 10 m (u,v) to each truth location and valid
time (reuse the J/80-polar interp machinery). Resample to 10-min means to remove
gust/motion noise the models can't resolve.

## 4. Metrics — separate raw from tactical

Raw (per location):
- Speed: bias, MAE, RMSE (kt).
- Direction: u/v RMSE **and Mean Vector Error (MVE)** — MVE matches Giannaros'
  metric, so compare directly to their **1.5–2.0 m/s** in-area bar.

Tactical (weight higher — these decide races):
- **Onset timing error** (min): model TWD entering SE–S + building vs.
  KBVY/44013/fleet actual.
- **Cross-course gradient**: does the model reproduce seaward-stronger? Score sign
  + magnitude vs. the fleet proxy.
- **Peak shift magnitude & clock-time** of the transition.
- **Which-side-favored** reduced to a binary call → hit rate.

Skill baselines (the decision variable): report each candidate as improvement
over **persistence** (dawn obs held) and over **raw HRRR**.

## 5. Stratify by the 5 sea-breeze types (non-negotiable)

Aggregate scores lie. Bin every race day by the Salem-Sound classifier
(Reinforced / Frontal-transition / Pure-thermal / Suppressed / Backdoor-NE) and
report per-type. **Money bin = Frontal/transition** — the hardest and most
decisive. Likely finding: PWG nails gradient days but misses onset/gradient on
transition days (coarse global SST) — that miss, quantified, is the ROI case for
the custom run with local coldest-pixel SST.

## 6. Sample

Log every race day this season (~10–15 days → thin but directional per-type
signal; two seasons for stable stats). Per day: freeze 3 forecasts + collect
44013 + KBVY + fleet proxy (+ SAR if available) over the race window + label the
type + log what actually happened on the water.

## 7. Decision rule

- PWG MVE ≈ 1.5–2 m/s **and** it beats HRRR on transition-day onset/gradient →
  buy PWG, skip the custom WRF, spend effort on the analog/climatology layer.
- PWG/Expedition systematically miss near-shore gradient or onset on transition
  days → measured case for the custom `gom-seabreeze` run with coldest-pixel SST.
