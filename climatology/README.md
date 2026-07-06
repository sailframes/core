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
- `probe_hrrrzarr.py` — working hrrrzarr reader: resolves bbox→grid window
  via Lambert Conformal, stitches chunks, value-checks fields. Seed for the
  future `backfill_hrrr.py`.

## Running the probe

hrrrzarr chunks are blosc-compressed, so `numcodecs` is required. On this
box it lives in homebrew python3.11, not the default `python3` (3.14):

```sh
/opt/homebrew/opt/python@3.11/bin/python3.11 climatology/probe_hrrrzarr.py --date 20250701 --cycle 18z
```

Phase 1 should pin a proper env (`requirements.txt`: boto3, numcodecs,
numpy, pyarrow, duckdb, pyproj). `s3fs`/`xarray`/`zarr` are not needed —
the probe reads zarr chunks directly with boto3 + numcodecs.
