# climatology/smoke/ — serving-tier smoke test

De-risks the whole `/tactics` serving architecture (spec §4) **before** any UI:
static Parquet + CloudFront + HTTP Range + DuckDB-WASM. Deployed & verified
2026-07-05, then **torn down** (synthetic data not left on the public domain).

## Status: COMPLETE — full serving tier proven, artifacts torn down

Deployed 2026-07-05 (sample marked synthetic: `.SAMPLE.` filename + S3 metadata
`synthetic=true` + red page banner), verified end-to-end, then **torn down** —
no synthetic data left on the public domain. The `/climatology/*` CloudFront
behavior is **retained** for Phase 1. To reproduce, re-`aws s3 cp` the two files
from this dir (URLs below) and invalidate.

| URL (during test) | Backing |
|---|---|
| `sailframes.com/climatology/labels.SAMPLE.parquet` | `s3://sailframes-data-prod/climatology/labels.SAMPLE.parquet` via CloudFront `/climatology/*` |
| `sailframes.com/tactics-smoke.html` | `s3://sailframes-web-prod/tactics-smoke.html` via default behavior |

**Proven:**
- ✅ HTTP **Range → 206** (`curl` GET, `Content-Range` correct).
- ✅ **DuckDB httpfs range-query** over the live CloudFront URL (native duckdb,
  same range-read path as duckdb-wasm): `read_parquet()` count / group-by /
  filtered query all succeed.
- ✅ **duckdb-wasm in a real browser** — confirmed 2026-07-05 in headless Chrome:
  page instantiates duckdb-wasm, range-queries the Parquet (rows=3341,
  by-type table, filtered analog query = 119 days), prints "range-queried the
  Parquet end-to-end." **Full serving tier proven; nothing left to de-risk.**

Teardown when done (see block below) — don't leave the sample up indefinitely.

## Result

- ✅ **HTTP Range proven** — `GET` with `Range: bytes=0-99` → **`206 Partial Content`**,
  `Content-Range: bytes 0-99/163084`, 100 bytes transferred.
  ```
  curl -s -o /dev/null -D - -H 'Range: bytes=0-99' https://sailframes.com/climatology/labels.parquet
  ```
  (Note: test with **GET**, not `curl -I`/HEAD — Range on HEAD returns 200 and is misleading.)
- ⏳ **DuckDB-WASM browser query** — open `https://sailframes.com/tactics-smoke.html`
  in a browser; it shows the raw Range fetch (206) + a real DuckDB-WASM
  `read_parquet()` range query (row count, group-by, filtered analog-style query).
  Not headless-verifiable here.

## Key learnings (folded into spec §4/§9)

1. **Serve from the REST origin, not the S3-website origin.** The default
   behavior points at `sailframes-web-prod` via its **S3 *website* endpoint**,
   which returns **200 (full object)** to Range requests. Range 206 requires
   the **REST** origin — `sailframes-data-prod` (S3-Data, OAC), which already
   streams `/hls/*`. So `climatology/` lives on **`sailframes-data-prod`**, not
   the spec's original `sailframes-fleet-data-prod` (not a CloudFront origin).
2. **CloudFront is not in CFN/Terraform** — the `/climatology/*` behavior was
   added imperatively (cloned `/hls/*`, Managed-CachingOptimized, no ORP/RHP).
   Record imperative changes so a future stack redeploy doesn't drop them.
3. **CORS not yet exercised** — page + data are same-origin (both sailframes.com).
   Add a CORS ORP/response-headers-policy on `/climatology/*` before any
   cross-origin use.

## Files

- `make_labels_sample.py` — regenerates `labels.parquet` (3,341 rows, schema-faithful
  to spec §4 `labels`, seeded/deterministic). Needs pyarrow (homebrew python3.11).
- `labels.parquet` — the sample (synthetic, NOT real classifier output).
- `tactics-smoke.html` — DuckDB-WASM + Range/CORS test page.

## Teardown

```sh
P="--profile sailframes"
aws s3 rm s3://sailframes-data-prod/climatology/labels.SAMPLE.parquet $P
aws s3 rm s3://sailframes-web-prod/tactics-smoke.html $P
# remove the /climatology/* CloudFront behavior only if NOT proceeding to Phase 1
# (Phase 1 reuses it). To remove: get-distribution-config, drop the behavior,
# update-distribution --if-match <ETag>.
aws cloudfront create-invalidation --distribution-id EFO342DVGM3QS --paths '/climatology/*' '/tactics-smoke.html' $P
```
