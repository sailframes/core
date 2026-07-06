# climatology/

Pre-race tactics & climatology data pipeline for `sailframes.com/tactics`.
Design: **`TACTICS_CLIMATOLOGY_SPEC.md`** (repo root).

Status: **Phase 0 spike done** — see **[`PHASE0_FINDINGS.md`](PHASE0_FINDINGS.md)**.
Headline: the HRRR field archive comes from **hrrrzarr** (pure, no Herbie,
no EC2); all classifier inputs incl. valid F00 DSWRF are present; the
Gloucester→P'town bbox is a 39×42 grid window in 2 chunks (~50 GB total
backfill, laptop-viable). Next: prove **DuckDB-WASM + CloudFront Range/CORS**
before any UI.

## Contents

- `PHASE0_FINDINGS.md` — spike results, decisions, spec corrections, open risks.
- `probe_hrrrzarr.py` — Phase-0 spike reader (standalone; superseded by `hrrr_grid.py`).
- `requirements.txt` — pinned pipeline env (use a venv; system python3 is 3.14 w/o these).

### Phase 1 data-ingest pipeline (built + validated on real data)

- `hrrr_grid.py` — shared hrrrzarr access: bbox→grid window, chunk stitch,
  lat/lon via pyproj. Validated in Phase 0.
- `make_grid.py` → `grid.json` (nx/ny, per-cell lat/lon, land mask, window). One-time.
- `backfill_hrrr.py` → `fields/year=/month=/DD.parquet` — 8 fields × 24 F00
  cycles × 1638 cells (~39k rows, ~450 KB/day). Verified: schema, size, physical
  values (18Z wind 6.9 m/s, DSWRF 799, etc.), 24/24 cycles, no gaps.
- `backfill_ndbc.py` → `obs/{stn}/{YYYY}.parquet` (44013 stdmet). Verified 2024/25.
- `backfill_coops.py` → `coops/{stn}/{hilo,pred}_{YYYY}.parquet` (tide). Verified 2024.
- `backfill_asos.py` → `asos/{ID}/{YYYY}.parquet` (IEM; KBED Tmax, KBOS/KBVY flip).
  Verified: BED 2025 = 103k obs, daily Tmax 29-33 °C.
- `label_days.py` → `labels.parquet` — the §6 classifier (F/R/P/G/N/U + onset,
  ΔT, gradient, insolation, tide-phase). **First-cut thresholds**: mechanism +
  features validated on real aligned days (ΔT, DSWRF/cloud coherent); the type
  thresholds (onshore sector width, R-window) need the 60-day manual validation
  set §6 mandates before they're final — do not trust the type split yet.

### Daily update (GitHub Actions)

`daily_update.py` + `.github/workflows/climatology-daily.yml` keep the archive
current (spec §5). Daily at **06:10 UTC** (+ manual `workflow_dispatch`):

1. Backfill **yesterday's** HRRR fields (year-round) → upload one daily Parquet
   (the replay archive grows a day at a time — no bulk backfill needed).
2. **In season (Apr 15–Oct 15):** refresh the current year's obs (44013 via the
   monthly+realtime current-year path, BED, tide), relabel the season, splice
   into `labels.parquet` (prior years are immutable), upload.
3. CloudFront invalidation for the touched paths.

**One-time setup (needs repo admin — I can't create these):**
- GitHub secrets `AWS_ACCESS_KEY_ID_CLIMATOLOGY` / `AWS_SECRET_ACCESS_KEY_CLIMATOLOGY`
  for an IAM user scoped to:
  - `s3:PutObject` + `s3:GetObject` on `arn:aws:s3:::sailframes-data-prod/climatology/*`
  - `cloudfront:CreateInvalidation` on distribution `EFO342DVGM3QS`
- Then trigger once via the Actions tab (`workflow_dispatch`) to smoke-test.

Verified locally against prod infra via `AWS_PROFILE=sailframes python
climatology/daily_update.py` (idempotent).

Not yet built (remaining Phase 1+): the in-season **hourly** `today/latest.parquet`
feed (§5), obs/tide sync-strips + analog finder (§7) on the UI.

Backfill output goes to `_local/` (gitignored); the real archive uploads to
`s3://sailframes-data-prod/climatology/` (Phase 0 serving tier proven).

## Running the probe

hrrrzarr chunks are blosc-compressed, so `numcodecs` is required. On this
box it lives in homebrew python3.11, not the default `python3` (3.14):

```sh
/opt/homebrew/opt/python@3.11/bin/python3.11 climatology/probe_hrrrzarr.py --date 20250701 --cycle 18z
```

Phase 1 should pin a proper env (`requirements.txt`: boto3, numcodecs,
numpy, pyarrow, duckdb, pyproj). `s3fs`/`xarray`/`zarr` are not needed —
the probe reads zarr chunks directly with boto3 + numcodecs.
