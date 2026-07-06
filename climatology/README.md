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
- `backfill_ndbc.py` → `obs/{stn}/{YYYY}.parquet` (44013 stdmet). Verified 2024.
- `backfill_coops.py` → `coops/{stn}/{hilo,pred}_{YYYY}.parquet` (tide). Verified 2024.

Not yet built (remaining Phase 1+): `backfill_asos.py` (KBED Tmax for ΔT),
`label_days.py` (classifier §6), the `/tactics` UI, and the GH Actions workflows.

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
