# TACTICS_CLIMATOLOGY_SPEC.md — v0.2 (2026-07-05)

> v0.2: folded in Phase 0 spike findings (`climatology/PHASE0_FINDINGS.md`) —
> hrrrzarr cost gate resolved (cheap, no Herbie/EC2), grid window/projection
> validated, CO-OPS + NDBC inventory, open questions #1 & #3 answered.

Pre-race tactics & navigation section for sailframes.com. Historical wind/tide archive + sea-breeze day classifier + 2D field replay + analog-day finder for the Gloucester → Provincetown corridor.

---

## 1. Purpose & success criteria

Turn 10 years of Mass Bay data into venue-specific tactical knowledge: when the sea breeze fills, from where, how it interacts with gradient and tide, and what today most resembles.

Success:
- Any day 2016+ replayable (wind field animation + obs traces + tide) with first frame < 2 s on broadband.
- P(sea breeze | morning gradient, ΔT, cloud) statistics from ≥ 8 warm seasons.
- Race-morning briefing (analog set + outcome distribution + latest HRRR run) generated < 1 min.
- Classifier ≥ 90% agreement with manual labels on frontal-flip days.

## 2. Scope

- **Bbox:** 41.95–42.75°N, 71.10–69.85°W (Gloucester, Marblehead, Boston approaches, Stellwagen, Race Point, Provincetown). Maps to HRRR grid window **j=761–799 × i=1610–1651 = 39 × 42 ≈ 1,640 cells** (2 hrrrzarr chunks, cy=5/cx=10–11; validated Phase 0). Parametrize bbox from day one (future venues: Buzzards Bay, Newport).
- **Temporal:** fields archive **2016-08-23 → present** (hrrrzarr start; clean 24-cycle days from 2017; cost trivial, useful for deliveries/frostbite); classifier labels Apr 15–Oct 15 only. **2017–2025 = 9 full warm seasons** (≥ the 8 the success criteria need; 2016 usable Aug 23→Oct 15 only).
- **Timezone:** storage UTC everywhere; classifier and all displayed times America/New_York (DST-aware). Onset times reported local.
- **Units:** storage SI (m/s, K, Pa, W/m²); display kt / °T (meteorological "from") / °C for ΔT.

## 3. Data sources

| Source | Role | Depth | Access | Notes |
|---|---|---|---|---|
| HRRR F00 analyses | 2D wind/T fields, "what happened" | 2016-08-23→ (24 hrly cyc from 2017) | **`s3://hrrrzarr` (us-west-1, anon) — sole source, Phase 0 confirmed** | All vars native in `_anl` incl. **valid F00 DSWRF** (873 W/m² midday); + free LAND mask, HPBL. Herbie/EC2 path NOT needed |
| ASCAT Metop-B/C 12.5 km coastal | Gradient-wind truth over water | target 2016→ (PO.DAAC to 2007 later) | NOAA CoastWatch ERDDAP | Pass times ~09:30–10:30 & ~20:30–21:30 LT — misses peak sea breeze by design. Overlay/verification only |
| NDBC 44013 | Mid-bay sentinel: wind, SST | 1980s→ | NDBC stdmet annual files | Annual files present all 2010–2025 (Phase 0). Fine-grained winter-outage/adrift + SST-continuity audit still Phase 1 |
| BHBM3, CSIM3 | Harbor/coastal obs (already dashboard sources) | verify | NDBC | |
| ASOS KBOS, KBVY, KPVC, KBED | Coastal flip detection (KBOS/KBVY/KPVC); inland Tmax for ΔT (KBED) | decades | IEM ASOS archive | KLWM alternate inland |
| CO-OPS water level + predictions | Tide curves | decades (Boston) | CO-OPS API | Boston 8443970 + Provincetown 8446121 confirmed. **No Cape Ann real-time gauge** — Gloucester via subordinate tide-prediction station (check `type=tidepredictions`) or Boston offset |
| CO-OPS current predictions | North Shore / Boston Harbor / Race Point / CCB | predictions any date | CO-OPS API | `BOS11xx` family spans the whole venue (Phase 0): **North Shore/Salem Sound** — BOS1101 Little Misery Is (42.54,−70.80), BOS1105 Marblehead Channel; **Boston Harbor** BOS1106–1127; **Stellwagen/Race Pt** BOS1130–1135 (BOS1131 = 16nm N of Race Pt) + ~40 CCB harbor stns. Point predictions suffice for v1 |
| NECOFS (FVCOM) / GoMOFS | 2D current fields | hindcast depth TBC | THREDDS/OPeNDAP; `nos-ofs` on AWS | Phase 3 |
| NREL NOW-23 | Bulk wind stats baseline (2 km, 2000–2020) | fixed | AWS open data | Phase 3, optional |
| ERA5 | Synoptic regime tagging only | 1940→ | CDS | Never for sea-breeze detection (too coarse) |
| GOES ABI vis | Qualitative sea-breeze front | link-out only (SLIDER archive) | — | No ingestion |

## 4. Storage & serving

All-Parquet, static, no server:

- Canonical + serving format: **Parquet (zstd)** on **`s3://sailframes-data-prod/climatology/`** — ⚠️ **corrected**: this is the CloudFront **S3-Data** origin (REST endpoint, OAC `EP6E4P08OAATN`, already serves `/hls/*`). NOT `sailframes-fleet-data-prod` (the E1-upload bucket, which is *not* a CloudFront origin). Serving-smoke-test verified 2026-07-05.
- Browser queries: **DuckDB-WASM** over HTTP range requests (labels, obs, fields). One engine for everything.
- Cache TTLs: historical partitions immutable/long TTL; `labels.parquet` + `today/*` short TTL + invalidation on daily job.
- CloudFront behavior `/climatology/*` → S3-Data (added 2026-07-05, cloned from `/hls/*`: Managed-CachingOptimized). **Range verified live (GET → 206 + Content-Range).** ⚠️ Range works only via the **REST** origin — the S3-*website* origin (default behavior) returns 200 and must not back this prefix. CORS: same-origin today (page + data both on sailframes.com) so not exercised; add a CORS ORP/response-headers-policy on this behavior before any cross-origin use.
- **Security:** climatology/ prefix read-only via CloudFront. Verify the unauthenticated-PUT bucket policy remains scoped strictly to `raw/E1/*` — add explicit deny for writes elsewhere if not already.

### Layout & schemas

```
climatology/
  grid.json                    # {nx, ny, lats[], lons[], land_mask[]} from HRRR LAND field
  fields/year=YYYY/month=MM/DD.parquet
  ascat/year=YYYY/MM.parquet
  obs/{station}/YYYY.parquet
  coops/{station}/wl_YYYY.parquet | pred_YYYY.parquet
  currents/{station}/pred_YYYY.parquet
  labels.parquet
  today/latest.parquet         # in-season hourly overwrite
```

- `fields`: valid_time_utc, gi (uint16 grid index), u10, v10, gust, t2, td2, mslp, tcdc, dswrf (f32). Sorted (valid_time, gi). ~24 × 1,000 rows/day ≈ 300–400 KB → one GET per replayed day.
- `ascat`: time_utc, sat, lat, lon, wspd, wdir, qc.
- `obs`: time_utc, wspd, wdir, gust, t_air, t_water, slp (as available per station).
- `labels`: date, type, onset_lt_{44013,MHD,GLO,RPT}, dir_onset, dir_12/14/16/18lt (veer trajectory), spd_peak, dt_c, grad_u, grad_v, grad_spd, grad_dir, tcdc_am, dswrf_am, sst, tide_phase_hw_h, synoptic_tag (Phase 2), qc_flags. ~3,800 rows total — loads instantly.

## 5. Pipeline & scheduling

`climatology/` (new monorepo dir, Python):

- `backfill_hrrr.py` — hrrrzarr chunk reads (boto3 + numcodecs, no s3fs/xarray needed — see `climatology/probe_hrrrzarr.py`), crop bbox window (2 chunks), write daily Parquet. Extract LAND once (time-invariant → `grid.json`) + optionally HPBL. Missing cycles → `gaps.parquet`.
- `backfill_ascat.py`, `backfill_ndbc.py`, `backfill_asos.py` (IEM), `backfill_coops.py`, `backfill_necofs.py` (Phase 3).
- `label_days.py` — classifier (§6) → `labels.parquet`.
- `make_grid.py` — grid.json + land mask, one-time.

Scheduling — **GitHub Actions only** (pattern: firmware-e1.yml):
- `climatology-daily.yml` @ 06:10 UTC: extract yesterday (all sources), append, relabel, CloudFront invalidation for labels.
- `climatology-hourly.yml` @ :50, Apr 15–Oct 15: `today/latest.parquet` = obs-so-far + latest HRRR F00–F18 bbox subset (briefing input).

Backfill economics — **decided Phase 0: hrrrzarr laptop path.**
- hrrrzarr path (chosen): 8 core vars × 24 hr × 2 chunks × ~40 KB ≈ **~15 MB/day** transfer → ~**50 GB** total over 2016-08→present, extracting ~300–400 KB/day Parquet. Laptop-viable in ~1–2 days wall-clock; no EC2.
- ~~Herbie/EC2 path (0.6–0.9 TB, us-east-1 spot)~~ — **not needed for v1** (kept only as a note should hrrrzarr ever go dark).
- Final archive size: ~1.5 GB fields + obs/tide noise. Storage cost negligible.

## 6. Day classifier

Season Apr 15–Oct 15. Reference points: 44013 (primary), plus coastal grid cells off Marblehead, Gloucester, Race Point.

Inputs per day:
- Morning gradient: vector-mean 06–10 LT at 44013 ∧ HRRR over-water bbox mean.
- ΔT = KBED Tmax(10–17 LT) − SST44013(10–14 LT mean).
- Insolation: mean DSWRF and TCDC 09–14 LT (store both raw).
- Onshore sector at mid-bay: **060°–170°T** (tunable constant per venue).

Types:
- **F (frontal flip):** morning offshore (190°–050°), afternoon rotation into onshore sector sustained ≥ 2 h @ ≥ 5 kt between 10–18 LT, |Δdir| ≥ 90°. The Marblehead classic.
- **R (reinforcement):** morning S–SW ≤ 12 kt, afternoon settles 150°–200° with ≥ 3 kt build.
- **P (pinned inshore):** KBOS or KBVY flips onshore ≥ 2 h but 44013 never does. Tactically gold — inshore-only fill.
- **G (gradient-dominated):** daily mean ≥ 15 kt, no onshore rotation.
- **N:** none of the above. **U:** insufficient data.

Also log: onset time per reference point, fill direction at onset, veer trajectory (dir @ 12/14/16/18 LT — quantifies the afternoon right shift), peak speed, tide phase at onset (hours rel. Boston HW).

Validation: manually label 60 stratified days; report confusion matrix; tune thresholds. Obs (buoy/ASOS) are primary truth; HRRR analyses are fields, not truth, pre-2016 especially.

## 7. Analog finder

Feature vector: [grad_u, grad_v, ΔT (forecast for today / analysis for history), cloud index, sin/cos DOY, SST]. Standardize; k = 30 nearest (Euclidean) within ±45 days-of-year across all years.

Output: P(type F/R/P), onset-time quantiles, fill-direction rose, veer path, matched date list → links to day replays. Runs client-side in DuckDB-WASM/JS on labels.parquet — no backend.

Race-morning mode: auto-load `today/latest.parquet` (or manual inputs offline), render briefing; print-CSS one-pager (no server PDF).

## 8. UI — `web/src/tactics/`, route `/tactics`

1. **Calendar heatmap** — day type per season/year; click → day replay.
2. **Day replay** — MapLibre GL basemap; HRRR 10 m vector field as canvas arrow layer, 24 frames, scrub + play (10 fps sufficient); ASCAT swath toggle with pass times; synchronized strips: buoy/ASOS traces, tide curve + current predictions, DSWRF/cloud. Single timeline scrubber drives all.
3. **Climatology stats** — flip probability vs gradient speed × direction heatmap (the money chart); onset-time distributions by month; wind roses morning vs afternoon by type; tide-phase-at-onset distribution.
4. **Briefing / analog finder** — §7.
5. **Venue notes** — per-sub-area markdown (Gloucester, Marblehead line, Stellwagen, Race Point) in repo, rendered; accumulates tribal knowledge alongside the data.

Match existing web/ chart + build stack; no new framework.

## 9. Infrastructure changes

- CloudFront: ✅ behavior `/climatology/*` → S3-Data added 2026-07-05 (imperative `update-distribution`, cloned `/hls/*`; dist `EFO342DVGM3QS`). Range verified 206. **CloudFront is NOT in CFN/Terraform** (managed imperatively) — this behavior + any CORS ORP/RHP to add later are imperative changes; document them so they aren't lost on a stack redeploy (cf. `/ais` route drift). TTL split per §4 still TODO.
- S3: prefix policy audit (no public write), lifecycle none (keep everything).
- GitHub Actions: two workflows §5. Secrets: AWS deploy role only.
- ~~One-time EC2 spot backfill runbook (only if Herbie path)~~ — not needed (Phase 0: hrrrzarr laptop path).

## 10. Phasing

- **Phase 0 (spike):** ✅ **DONE 2026-07-05** — see `climatology/PHASE0_FINDINGS.md` + `probe_hrrrzarr.py`. ✅ hrrrzarr carries all vars incl. valid F00 DSWRF (no Herbie hybrid); ✅ bbox→grid window + projection validated; ✅ CO-OPS/Race Point currents + NDBC 44013 inventoried. Deferred: ASCAT ERDDAP dataset IDs (Phase 2 overlay), manual-label 60 days (with Phase 1 classifier). ✅ **Serving smoke test done 2026-07-05** (`climatology/smoke/`): `/climatology/*` CloudFront behavior + `labels.SAMPLE.parquet` (marked synthetic) + `sailframes.com/tactics-smoke.html`. **Serving tier fully proven**: Range → 206, DuckDB httpfs range-query over the live CloudFront URL, and **duckdb-wasm confirmed in a real browser** (headless Chrome: instantiate + range-query + render). Nothing left to de-risk. Learned: serve from the REST origin (website origin returns 200 on Range); climatology/ bucket corrected to `sailframes-data-prod` (§4).
- **Phase 1:** HRRR + obs + CO-OPS backfill; classifier + labels; calendar, day replay (fields + traces + tide), stats page; daily workflow.
- **Phase 2:** ASCAT overlay; analog finder + briefing + hourly today-feed; synoptic tagging.
- **Phase 3:** NECOFS/GoMOFS current fields; NOW-23 baseline; second venue via bbox parametrization.

## 11. Dead ends — do not relitigate

- **No API Gateway** in the data path (29 s timeout lesson from E1). Static Parquet + CloudFront.
- **No server-side query tier.** DuckDB-WASM client-side; data volumes don't justify a backend.
- **No raw GRIB2 retention.** Extraction is deterministic/re-runnable; store subsets only.
- **No Zarr-in-browser** for v1 (reader maturity, second dependency). Zarr acceptable as *read source* (hrrrzarr) only.
- **ERA5 never used for sea-breeze detection** — resolution smooths the circulation out; synoptic tags only.
- **ASCAT is not sea-breeze truth** — pass timing brackets, doesn't sample, the afternoon fill.
- **No scraping SailFlow/Windy/PredictWind** — licensing; primary NOAA/EUMETSAT sources only.
- **No Lambda+eccodes container** for scheduled jobs in v1 — GH Actions has clean pip; revisit only if latency demands.
- **No raster tile pyramid** for wind fields — 39 × 42 grid draws as client-side vectors trivially.

## 12. Open questions

1. ~~hrrrzarr variable coverage (Phase 0 gate — decides backfill cost by ~40×).~~ **RESOLVED Phase 0: full native coverage incl. valid F00 DSWRF → cheap hrrrzarr laptop path (~50 GB), no Herbie/EC2.**
2. Second venue priority + whether venue notes ship in Phase 1 (cheap) or 2.
3. NECOFS hindcast depth and whether point current predictions suffice for Race Point tactics — **Phase 0: Race Point/Stellwagen current-prediction stations (BOS1130/1131/1132) exist → provisionally yes for v1; NECOFS fields stay Phase 3.**
