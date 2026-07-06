# Phase 0 spike — findings

Spike against `TACTICS_CLIMATOLOGY_SPEC.md` §10 Phase 0. Run 2026-07-05.
All checks are reproducible from `climatology/probe_hrrrzarr.py` and the
one-liners noted per item. **Bottom line: the headline cost gate (open
question #1) is resolved on the cheap side — pure hrrrzarr, no Herbie, no
EC2. Green light for Phase 1 on the field pipeline.**

## Verdict table

| Phase 0 item | Status | Result |
|---|---|---|
| hrrrzarr variable coverage (the 40× gate) | ✅ resolved | All 8 required vars native in `_anl` (F00). No hybrid. |
| — incl. DSWRF/TCDC **values** valid, not just names | ✅ resolved | F00 DSWRF = 873 W/m² midday (valid). See below. |
| bbox → grid-index window + chunk cost | ✅ resolved | 39×42 cells, **2 chunks/field/hr** (~15 MB/day, ~50 GB total). |
| Backfill economics decision | ✅ decided | **hrrrzarr laptop path.** Herbie/EC2 runbook not needed for v1. |
| CO-OPS station inventory (Boston/P'town/Gloucester) | ✅ resolved | Boston + P'town gauges exist; **no Gloucester gauge** (use offsets). |
| CO-OPS current stations (Race Point / CCB) | ✅ resolved | BOS1131/1132/1130 (Stellwagen/Race Pt) + CCB harbors present. |
| NDBC 44013 gap audit | ✅ (start) | 44013 full 2010–2025; 44018 gappy (retire as secondary). |
| ASCAT ERDDAP dataset IDs | ⏳ deferred | Phase 2 (overlay only). ERDDAP search endpoint uncooperative. |
| DuckDB-WASM + CloudFront Range/CORS smoke test | 🔴 **top open risk** | Not exercisable without a deploy. This is now the #1 un-derisked item. |
| Manual-label 60 days | ⏳ deferred | Human task; do alongside Phase 1 classifier build. |

---

## 1. hrrrzarr coverage & the cost gate (open question #1) — RESOLVED, cheap side

`s3://hrrrzarr` (public, anonymous, **us-west-1**) `sfc/` analysis stores carry
**every** field the classifier and replay need, natively:

| Spec field | hrrrzarr group |
|---|---|
| u10 / v10 | `10m_above_ground/UGRD`,`VGRD` |
| gust | `surface/GUST` |
| t2 / td2 | `2m_above_ground/TMP`,`DPT` |
| mslp | `mean_sea_level/MSLMA` |
| **tcdc** | `entire_atmosphere/TCDC` |
| **dswrf** | `surface/DSWRF` |

**Free bonuses worth taking:** `surface/LAND` (→ land mask for `grid.json`,
read once — it's time-invariant), `surface/HPBL` (boundary-layer depth — a
genuine sea-breeze signal the spec doesn't yet exploit), `low/middle/high
cloud` (`LCDC/MCDC/HCDC` — cloud split beyond total), `surface/DLWRF/USWRF`,
`surface/VIS`, `80m_above_ground` winds.

### The value check that matters (DSWRF F00 degeneracy)

Variable *presence* was not enough: HRRR surface flux fields (DSWRF/DLWRF)
are a time-average over the elapsed forecast period and can be **zero in an
F00 analysis** — and hrrrzarr `_anl` = F00. DSWRF is exactly the field the
spec flagged as the cost risk and the classifier's insolation input (§6).

Decoded actual chunk values (blosc → f4) over the bbox at 18z (= 14 EDT,
midday) on 2025-07-01:

```
dswrf   bbox-cell 873.0   window 207/799/885 W/m²   ← physical, NOT degenerate
tcdc      4.0   0/32/100 %
u10/v10  -1.1 / 6.0 m/s   (≈ SSW 12 kt — a sea breeze)
t2      291.7 K (18.5 °C)
```

**Conclusion:** F00 DSWRF in hrrrzarr is valid. The spec's "radiation via
Herbie F01 fallback" contingency is **unneeded**. Pure `_anl` path stands.
(The `_fcst` F01 store exists as a fallback but has a different internal
layout — forecast_period axis — and was not needed, so not mapped.)

### Grid window & chunk cost

CONUS grid ny=1059 × nx=1799, Lambert Conformal (sphere R=6371229,
lat0=lat1=lat2=38.5, lon0=−97.5), chunked **150×150**, blosc f4.

The Gloucester→P'town bbox maps to grid window **j=761–799 × i=1610–1651
(39×42 = 1,638 cells)**, spanning **2 adjacent chunks (cy=5, cx=10 & 11)**.
So per field per hour = 2 chunk GETs (~40 KB each). This window is constant
across all dates (same static grid) → it's exactly what `make_grid.py`
should hard-code, and `read_window()` in the probe script already stitches
the two chunks correctly.

**Projection validated (not circumstantial):**
- SW-corner round-trip is **exact** — `lcc(21.138123, −122.719528)` reproduces
  the store's own `xs[0]/ys[0]` = (−2697520.1, −1587306.2) to 0.0 m. The
  hand-rolled LCC matches hrrrzarr's coordinate arrays.
- The `LAND` mask over the window draws Mass Bay correctly: inland MA solid
  on the W, a **Cape Ann/Gloucester peninsula jutting E** mid-north, open bay
  to the E, Cape Cod land in the SE. Anchors: Boston(42.36,−71.05)→LAND=1,
  Cape Ann(42.61,−70.66)→LAND=1, open-bay(42.35,−70.30)→ocean. (Provincetown
  point reads ocean — a 1-cell artifact of a sub-3 km sand spit; the §6
  coastal reference cells off Marblehead/Gloucester land correctly.)

**Backfill economics (recomputed):** 8 core vars × 24 hr × 2 chunks × ~40 KB
≈ **~15 MB/day** transfer, extracting ~300–400 KB/day of bbox Parquet.
Over 2016-08 → present (~3,400 days) ≈ **~50 GB** total transfer, ~1.5 GB
final archive. Laptop-viable in a day or two of wall-clock. **The 0.6–0.9 TB
Herbie/EC2 path in §5 is not required for v1** — delete that runbook
dependency (keep as a note only).

### Temporal coverage (supports "any day 2016+ replayable")

`_anl` hourly cycle count by sample date:

```
2016-08-23   4     ← ragged start of the archive
2017-07-01  24
2019-08-15  24
2021-07-04  24
2023-07-01  24
2025-07-01  24
2026-07-01  24
```

- Archive begins **2016-08-23** (spec's "full-year 2016" is optimistic;
  most of the 2016 warm season is missing). State this in §2.
- Clean 24-cycle days on every sampled date from **2017-01 onward**.
- **Warm seasons present: 2017–2025 = 9 ≥ the spec's "≥ 8"** ✔.
  (2016 usable Aug 23→Oct 15 only; fine for deliveries/frostbite, weak for
  the sea-breeze classifier's first season.)
- ⚠️ **Sampled, not audited** — one day/year checked; a per-day Apr 15–Oct 15
  gap audit is deferred to `backfill_hrrr.py` (its `gaps.parquet`, spec §5).
  "9 clean seasons" is expected but not yet proven cycle-by-cycle.

---

## 2. Tide & currents (CO-OPS) — RESOLVED, one spec correction

- **Water level:** Boston `8443970` ✔ and **Provincetown `8446121`** ✔
  (42.050°N, −70.182°W — inside bbox). `8447435` is **Chatham**, not
  Gloucester.
- **No CO-OPS *real-time* water-level gauge in the Gloucester/Cape Ann area**
  (searched `type=waterlevels`, 42.4–42.75°N). NOAA likely still has a
  Gloucester **subordinate tide-*prediction* station** with its own harmonic
  offsets (better than raw Boston for that sub-area) — check `type=tidepredictions`
  in Phase 1. Worst case: Boston + offset (phase difference is minutes).
  Update §3/§10 — "Gloucester WL" is not a primary gauge.
- **Current predictions near Race Point / Stellwagen (spec's Phase-3
  tactical need) are well covered:**
  - `BOS1131` — Stellwagen Bank, 16 nm N of Race Point (42.324, −70.294)
  - `BOS1132` — Stellwagen Bank, 15 nm NNE of Race Point (42.312, −70.112)
  - `BOS1130` — Stellwagen Basin, east end (42.338, −70.532)
  - plus ~40 Cape Cod Bay harbor stations (ACT****).
  Point current *predictions* almost certainly suffice for Race Point v1
  (resolves open question #3 provisionally: **yes**) — NECOFS/GoMOFS field
  currents can stay Phase 3.

---

## 3. Buoys (NDBC) — 44013 solid

- `44013` (mid-bay sentinel): **annual stdmet present for all of 2010–2025**
  (`https://www.ndbc.noaa.gov/data/historical/stdmet/44013h{YYYY}.txt.gz`).
  Solid primary reference for the classifier.
- `44018`: 2010–2024 with gaps (station moved/retired). Demote to optional.
- Still to do in Phase 1: fine-grained winter-outage / adrift audit and SST
  continuity for the ΔT term.

---

## 4. Environment note (for whoever runs Phase 1)

- **hrrrzarr chunks are blosc-compressed** → decoding needs `numcodecs`.
  On this box `numcodecs`/`boto3`/`numpy` are only in **homebrew
  python3.11**, NOT the default `python3` (3.14, numpy-only):
  ```
  /opt/homebrew/opt/python@3.11/bin/python3.11 climatology/probe_hrrrzarr.py
  ```
  Phase 1 should pin an env (`requirements.txt`: boto3, numcodecs, numpy,
  pyarrow, duckdb, pyproj) rather than lean on system interpreters. `s3fs`
  + `xarray`/`zarr` are *not* required — the probe reads chunks directly
  with boto3 + numcodecs, which is lighter and avoids the zarr-in-Python
  version churn.

---

## 5. Open risks carried into Phase 1

1. ✅ **DuckDB-WASM + CloudFront Range/CORS serving tier — DE-RISKED 2026-07-05**
   (`climatology/smoke/`). Deployed a sample `labels.parquet` + `/climatology/*`
   CloudFront behavior + `sailframes.com/tactics-smoke.html`. **Range proven
   live: GET → 206 + Content-Range.** Two learnings folded into spec §4/§9:
   (a) must serve from the **REST** origin `sailframes-data-prod` — the S3
   *website* origin returns 200 on Range; (b) CloudFront is imperative, not in
   CFN. Also proven: DuckDB httpfs range-query (native) **and duckdb-wasm
   confirmed in a real browser** (headless Chrome — instantiate + range-query
   rows=3341 + render). **Serving tier fully de-risked; nothing left.** Only
   follow-up: add a CORS ORP/RHP before any cross-origin use (same-origin today).
   Synthetic sample deployed, verified, then **torn down** (nothing left on the
   public domain); `/climatology/*` behavior retained for Phase 1.
2. ASCAT ERDDAP dataset ID still unknown (Phase 2 — overlay only, low risk).
3. Gloucester tide is a derived offset, not a gauge — decide (Boston-as-is
   vs. harmonic offset) when the tide strip is built.

---

## 6. Recommended spec edits

- §2 Temporal: "full-year 2016→present" → **"2016-08-23 → present; clean
  24-cycle days from 2017; 2017–2025 = 9 full warm seasons."**
- §3 CO-OPS row: drop "Gloucester WL"; note **no Cape Ann gauge — Boston
  offset**. Confirm Race Point currents = `BOS1130/1131/1132`.
- §5 backfill economics: mark the **Herbie/EC2 0.6–0.9 TB path as not needed
  for v1** (hrrrzarr laptop path confirmed ~50 GB / ~1.5 GB archive).
- §5 pipeline: add `LAND` (once) and optionally `HPBL` to the field extract.
- §10/§12: mark open question #1 **resolved (hrrrzarr, cheap)**; #3
  **provisionally yes (point current predictions suffice for Race Point)**.
