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
   **NOTE:** the HRRR-driven run currently fails at real.exe (met_em 1 level = ungrib/Vtable
   level-count issue) — fix that first (Vtable.raphrrr level mapping / num_metgrid_levels).
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

## Minimal first step

Before full automation: a **one-command "forecast today"** — `GOM_MODE=hrrr ... launch.sh $(today)`
that runs the HRRR-driven forecast for the current date off the latest cycle and backfills to a
"today" dashboard source. Manual on race morning first; schedule it once it's trustworthy.
