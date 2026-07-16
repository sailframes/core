# Operational day-of forecast pipeline — retrospective → race-morning forecast

**The gap (Paul, 2026-07):** the /tactics + sea-breeze dashboards are **retrospective** — they
replay *past* days (HRRR archive, reanalysis). Every WRF run so far reforecasts a **past date**
(2026-07-04) too — great for *validation* (we know the truth), useless *before* a race. To be
raceable, the forecast must run **forward, initialized from race-morning conditions.**

This is the difference between **"studying sea breezes"** (what usually / did happen) and
**"forecasting *your* race"** (what's happening today → what it becomes this afternoon).

---

## Architecture

```
  ~07:00 ET race morning
        |
   [EventBridge schedule]  --(fires the run for today's date)-->
        |
   detect LATEST available HRRR cycle (e.g. today 06Z or 09Z, F00..F12)
        |
   HRRR-DRIVEN WRF-SailFrames  (mode=hrrr: d01 3km + d02 1km, fits CONUS)
     + coldest-pixel SST (today's ACSPO/MUR)
     + obs-nudge today's MADIS surface + aircraft (anchor to THIS morning)
     + [race window] LES d03/d04 111m nests for gusts
        |
   backfill_wrf.py  -->  grid.json + fields parquet  -->  climatology/wrf-today/
        |
   dashboards read climatology/wrf-today/ as the DAY-OF source:
     - /tactics sea-breeze  (today's fill timing/position, not climatology)
     - Gust/Pressure panel  (today's expected gust structure)
     - race map wind overlay ("WRF today")
```

## Components (what to build, on top of what exists)

1. **Scheduler** — EventBridge rule (like the retired MAMI CORS rule) firing a Lambda/launch
   at ~07:00 ET on race days (or daily). Passes `date = today`, `mode = hrrr`.
2. **Latest-HRRR-cycle detection** — instead of a fixed date, pick the newest HRRR cycle on
   `noaa-hrrr-bdp-pds` with F00..F(race_hour+2) available. run_case.sh's HRRR staging already
   pulls hourly F00; generalize it to "latest cycle" + forecast hours (F00..FNN of ONE cycle,
   not F00 of many) so it's a true forecast, not an analysis stitch.
   **STATUS (2026-07-14): ingest CONFIRMED.** wrfnat native atmosphere (Vtable.raphrrr level-type
   109, num_metgrid_levels=51) + **dual-sourced wrfprs soil** (wrfnat has no TSOIL / only 2 SOILW
   layers → num_metgrid_soil_levels=9 from wrfprs) → real.exe writes valid wrfinput_d01/wrfbdy_d01.
   Also fixed: innermost-domain MPI decomposition (HRRR d02=76 cells needs k×k ranks, patch≥10).
3. **Anchor to today** — obs-nudge the morning's MADIS surface + aircraft (project_madis_ingest)
   so the run is tied to *this morning's* observed state, and (later) cycling (warm-start off the
   previous hour) for continuity.
4. **Forecast, not hindcast** — the run integrates from the morning HRRR init forward through the
   race window; the LES nests cover 1pm-3pm for gusts.
5. **Publish + dashboards** — `backfill_wrf.py` -> `climatology/wrf-today/{grid.json,fields}`; the
   dashboards get a "today" wind source + a **Gust/Pressure panel** (gust_viz.py outputs:
   pressure movie, gustiness map, point trace) shown for the *upcoming* race, not a past one.

## Timing (1 pm race example)

| Time (ET) | Step |
|---|---|
| 07:00 | schedule fires; pick latest HRRR cycle (e.g. 06Z=02ET, or wait for 09Z=05ET) |
| 07:05 | stage HRRR FNN + geog + today's SST + morning obs |
| 07:10 | WPS -> real -> WRF forward to ~19Z; LES nests over 17-19Z |
| ~09:00 | run done (2-3h incl LES); backfill -> dashboards |
| 09:00-12:30 | sailor reads today's sea-breeze fill + gust structure pre-race |

## Honesty / limits

- The forecast is only as good as the morning HRRR + our downscale; **update it** as newer HRRR
  cycles arrive (re-run at 09Z, 12Z) — freshness beats a single early run.
- LES gusts remain **expected character**, not a per-puff clock (see gust_viz / dashboard framing).
- This is the **W4 (cycling) + W7 (productionize)** items from HRRR_vs_WRF_SAILFRAMES.md, plus the
  HRRR-driven fit (W1, done) and MADIS obs (W2/W3) as the anchoring inputs.

## Cycling (W4) — design, not yet built

"Cycling" = keep the forecast fresh through the morning instead of one early cold run. Two flavors,
cheapest-useful first:

**A. HRRR re-init cadence (partial cycling — DO THIS FIRST, ~free).** Re-run the HRRR-driven
downscale from each new HRRR cycle as it lands (06Z→02ET, 09Z→05ET, 12Z→08ET). Each run is a fresh
cold start off HRRR — but **HRRR is itself cycled hourly** (its own GSI 3D-EnVar assimilates
radar/aircraft/mesonet every hour), so we inherit HRRR's DA freshness for zero extra machinery. This
is the "freshness beats a single early run" note above, made concrete: the scheduler fires at 05:00
and 08:00 ET, latest-cycle detection picks the newest HRRR, dashboards show the most recent run.
Storage: overwrite `climatology/wrf-today/` each cycle (keep a `wrf-today-<cycle>Z/` archive for diff).

**B. WRF warm-start (true continuity — LATER, only if A isn't enough).** Carry our *downscale's*
spun-up state (sea-breeze circulation, SST-forced gradients, PBL/LES turbulence) across cycles via
`wrfrst` restart files, so each cycle doesn't re-spin from HRRR's coarse init:
- emit restarts: `restart_interval` = 60 min; retain last ~3 `wrfrst_d0N` (EBS work vol or S3
  `gom/<date>/rst/`).
- **The value is the LES nests.** d04/d05 turbulence needs ~30–60 min to develop; a cold LES each
  cycle wastes that spin-up. Warm-start ONLY the LES nests from the prior cycle's `wrfrst`, keep
  parents cold-from-fresh-HRRR — fresh synoptic + SST forcing *and* pre-spun turbulence for gusts.
- **Caveat (why B is "later"):** blending a prior `wrfrst` atmosphere with a new HRRR init is a
  mini-DA problem (interior/boundary mismatch → gravity-wave shock). Flavor A sidesteps it entirely
  by leaning on HRRR's cycling. Don't build B until a benchmark shows A's per-cycle cold-start error
  (esp. LES gust onset) actually limits the product.

Ties to HRRR_vs_WRF_SAILFRAMES.md W4. Depends on the HRRR-driven ingest (#2 above) being confirmed
first — a cold HRRR re-init is worthless until the HRRR ingest works end-to-end.

## Minimal first step

Before full automation: a **one-command "forecast today"** — `GOM_MODE=hrrr ... launch.sh $(today)`
that runs the HRRR-driven forecast for the current date off the latest cycle and backfills to a
"today" dashboard source. Manual on race morning first; schedule it once it's trustworthy.
