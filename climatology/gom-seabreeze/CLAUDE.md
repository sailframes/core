# CLAUDE.md — context for Claude Code

## What this is
Modeling backend for SailFrames' sea-breeze / Tactics-Climatology work: a
RU-WRF-style 1 km WRF downscale over Massachusetts Bay driven by a coldest-pixel
SST lower boundary, plus the observation tooling to validate and benchmark it.
Read `README.md` first — it has the pipeline, layout, and the full design
rationale.

## Hard constraints (do not relitigate — these were resolved in chat)
1. **SST is the #1 lever.** Coldest-DARK-pixel compositing, never warmest-pixel
   (warmest-pixel deletes cold upwelled water as cloud). Use ACSPO QL≥5 to define
   clear, then take coldest.
2. **Coldest-vs-diurnal tension is known.** Recover the afternoon ΔT peak via
   GOES-19 augmentation / `sst_skin=1` / ocean coupling — never by switching to a
   mean/warmest composite.
3. **1 km ⇒ ~6–7 km effective resolution.** d03 = front position + geometry, not a
   gust map. Do NOT propose sub-km without LES-mode physics (out of scope).
4. **One-way nesting** (`feedback=0`).
5. **Truth = point obs + radar + SAR**, not gridded model analyses. No boat wind
   sensors yet → in-area truth is 44013 + KBVY + fleet proxy. Handle tidal-current
   contamination (STW not SOG; slack ±1 h or correct).
6. **Static SST first.** WRF↔ocean coupling only if the benchmark proves diurnal/
   tidal SST is the limiting error.
7. **PredictWind PWG is CCAM, not WRF.** (Correct any note that says otherwise.)

## Coding conventions (Paul)
- Terse, technically precise. No preamble.
- **Verify against source, not notes** — read the actual namelist / file / repo,
  don't assert from a session summary.
- Document dead-ends explicitly as named constraints (see README).
- Pinned/verified over convenient: confirm ERDDAP dataset IDs and WRF option
  numbers against the real catalog/registry before trusting them.

## Task order (the TODO skeletons)
1. `run/run_gom_seabreeze.py` — the end-to-end driver. Chain WPS → build_coldest_sst
   → patch_met_em_sst → real → wrf, with the forecast/hindcast mode switch
   (driver source, `num_metgrid_levels`, `&fdda` nudging).
2. `validation/kbox_fine_line.py` — extract the KBOX sea-breeze fine-line/
   convergence front timing+position; compare to the d03 wind-convergence front.
3. `fleet/fleet_wind_proxy.py` — upwind-leg TWD + cross-course gradient proxy from
   SailFrames fleet GPS (the in-area wind truth until sensors exist).
4. Implement `validation/benchmark_protocol.md` — freeze PWG/Expedition/HRRR,
   match to truth, score by sea-breeze type, apply the build/buy decision rule.

## First shakedown (validate the chain on one case before automating)
Pick one logged transition-day sea-breeze date → `build_coldest_sst.py --plot`
(eyeball Cape Ann/Mass Bay cold structure + buoy offset) → run WPS →
`patch_met_em_sst.py --plot` (confirm d03 coastline + cold water) → real/wrf →
compare d03 front timing to KBOX and 44013.
