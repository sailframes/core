# SailFrames — GNSS/IMU Fusion and Race Analytics Spec v1

**Status:** Draft for Claude Code implementation
**Scope:** Cloud-side fusion of raw GNSS + IMU data into a race analytics dataset, plus first three debrief views
**Target consumer:** `lambda/process_upload` and `processing/` modules in `sailframes/core`

---

## Purpose

Today, SailFrames logs raw GNSS observations (PPK-capable) and raw BNO085 IMU data from both S1 (Pi 5 + ZED-F9P) and E1 (ESP32 + LG290P). Post-race processing exists for per-sensor CSVs but does not *fuse* these streams into boat-frame quantities that require both.

This spec adds a fusion step that produces three new derived quantities — **true heading**, **leeway**, and **maneuver events** — and three debrief views that depend on them. These are the analytics that distinguish SailFrames from GPS-only fleet trackers.

This spec does **not** cover: tight-coupled GNSS/INS (v2), true wind motion correction (v2, requires S1 Calypso data), LoRa real-time telemetry, sail shape analysis.

---

## Reading guide for Claude Code

Before writing code, read:
1. `CLAUDE.md` — project context, especially S1/E1 hardware and data flow sections
2. `processing/models.py` — existing data schemas; extend, do not replace
3. `processing/maneuvers.py` — existing maneuver detection; this spec may supersede or wrap it
4. `lambda/process_upload/` — existing Lambda; fusion must integrate into this, not run separately
5. `infrastructure/aws/` — existing S3 bucket policies, IAM, CDK/Terraform

Key constraints to respect:
- E1 uploads via HTTP (not HTTPS) to `s3://sailframes-fleet-data-prod/raw/E1/{date}/`
- S1 uploads to `s3://sailframes-fleet-data-prod/raw/{device_id}/{date}/{sensor_type}/`
- BNO085 runs in `GAME_ROTATION_VECTOR` mode — **no magnetometer**. Magnetometer data does not exist in the pipeline.
- PPK post-processing uses RTKLIB + NOAA CORS base station data (`geodesy.noaa.gov/UFCORS`)
- No competitor brand names in code, comments, or documentation

---

## Architecture

### Trigger

S3 `s3:ObjectCreated:*` event on `raw/{device_id}/**/*.rtcm3` (E1) or `raw/{device_id}/**/*.ubx` (S1) fires the fusion Lambda.

**Session boundary detection** (no manifest file exists):
- Session ID = the GPS datetime prefix already embedded in filenames: `E1_20260405_225030_*` → session `20260405_225030` for device `E1`.
- The `.rtcm3` / `.ubx` file is the largest and usually last to upload per session. Triggering on its arrival is a reliable "session complete" signal for 95% of cases.
- Idempotency: Lambda checks `s3://sailframes-fleet-data-prod/processed/{device_id}/{session_id}/fused.parquet` exists with matching input checksums before reprocessing. Reprocessing is always safe (pure function).

### Fusion Lambda: `sailframes-fuse`

**Inputs** (fetched from S3 by session_id + device_id):
- Raw GNSS: `.rtcm3` (E1) or `.ubx` (S1)
- Parsed NMEA: `_nav.csv` (E1) or `gps/track_*.csv` (S1)
- IMU: `_imu.csv` (E1) or `imu/imu_*.csv` (S1)
- Wind (E1 only): `wind/wind_*.csv`
- NOAA CORS RINEX (nearest station to Boston Harbor, typically MAMI) for the session time window ±1 hour

**Steps:**
1. **PPK solution.** Run RTKLIB `convbin` on raw GNSS → RINEX. Download CORS RINEX. Run `rnx2rtkp` (kinematic, combined forward/backward). Output `.pos` file with 10 Hz lat/lon/ellipsoid_h + Q factor + σ.
2. **IMU resample.** Load IMU CSV, resample to 10 Hz aligned with PPK timestamps. Preserve quaternion and linear acceleration. Compute heel and pitch from quaternion (gravity-aligned).
3. **Heading fusion** (see algorithm below).
4. **Leeway calculation.** `leeway = heading_true − cog`, small-angle unwrap, null when `sog < 1.5 kt`.
5. **Leeway calibration** (see procedure below).
6. **Maneuver detection** (see algorithm below).
7. **Write outputs** (see schema below).

**Runtime budget.** Typical 90-minute race: RTKLIB ~20s, fusion ~3s, maneuvers ~1s. Well under 15-minute Lambda limit. Memory: 2 GB sufficient. Package RTKLIB binary as a Lambda layer compiled for Amazon Linux 2023.

**NOAA CORS caching.** Cache CORS hourly files in `s3://sailframes-fleet-data-prod/cors-cache/{station}/{YYYY}/{DOY}/{hour}.zip`. 6 boats per race × 4 races per day would otherwise fetch the same CORS file 24 times.

**Reprocessability discipline.** Fusion logic lives in `processing/fusion.py` as a **pure function**: `(raw_files_dict, cors_rinex, config) → (fused_parquet, events_list)`. The Lambda handler is a thin wrapper. A separate script `scripts/reprocess_season.py` iterates over `raw/` and invokes the same pure function. This is the single most important discipline — when the fusion algorithm improves, we reprocess historical data in one command.

### Outputs

Written to `s3://sailframes-fleet-data-prod/processed/{device_id}/{session_id}/`:

- `fused.parquet` — 10 Hz timeseries (schema below)
- `events.parquet` — one row per maneuver
- `metadata.json` — session metadata (device_id, session_id, start/stop UTC, PPK fix rate, CORS station used, fw_version, config snapshot, fusion algorithm version)

Emits EventBridge event `sailframes.session.processed` with `{device_id, session_id, s3_uri}`. A second Lambda (`sailframes-fleet-align`, not in scope here but noted for architecture) listens and groups same-race sessions into a `fleet.parquet`.

---

## Algorithms

### Heading fusion (complementary filter)

**Inputs per 10 Hz sample:**
- `yaw_rate_gyro` — from BNO085 quaternion derivative, rad/s, body frame z-axis
- `cog_gps` — from PPK velocity, rad, unwrapped
- `sog_gps` — kt

**State:** `heading_fused` (rad, unwrapped), initialized to first valid COG when `sog > 2.0 kt`.

**Update rule:**

```
alpha = dt / (tau + dt)                    # tau = 15s, dt = 0.1s → alpha ≈ 0.0066
heading_predicted = heading_fused + yaw_rate_gyro * dt
if sog_gps >= SOG_GATE:                    # SOG_GATE = 2.0 kt
    # Unwrap cog_gps to be within ±π of heading_predicted
    cog_unwrapped = unwrap_to(cog_gps, reference=heading_predicted)
    heading_fused = (1 - alpha) * heading_predicted + alpha * cog_unwrapped
else:
    heading_fused = heading_predicted       # trust gyro only at low speed
```

**Config parameters** (in `config/fusion.yaml`):
- `tau_seconds: 15.0` — crossover time constant
- `sog_gate_knots: 2.0` — below this, COG is meaningless; trust gyro only
- `initial_heading_min_sog_knots: 2.0` — wait for this before trusting first COG

**Calibration note.** BNO085 yaw in GAME_ROTATION_VECTOR is relative to the sensor frame at startup, not true north. The filter lets GPS COG anchor the absolute reference. After `sog` exceeds `sog_gate` for ~3τ (~45s of sailing), `heading_fused` converges to true-referenced heading ± leeway offset.

**Drift budget.** BNO085 gyro drift in GAME_ROTATION_VECTOR: ~1-3°/min. With τ=15s and COG updates at 10 Hz above 2 kt, steady-state error < 0.5°. This is sufficient for leeway measurement (typical leeway 3-7°).

**Future: adaptive τ (v2).** Vary τ with yaw_rate:
- `|yaw_rate| > 10°/s` → `tau = 45s` (trust gyro during maneuvers)
- `|yaw_rate| < 2°/s` → `tau = 10s` (trust COG in straight lines)
Interpolate linearly. Defer until v1 data shows τ=15s is insufficient.

### Leeway calculation and calibration

**Raw leeway** per sample: `leeway_apparent = wrap_to_pi(heading_fused − cog_gps)`. Null when `sog < 1.5 kt`.

**Calibration offset** — automatic DDW detection:

A "downwind calibration run" is a contiguous window meeting all of:
- `sog > 3.0 kt` (boat is sailing, not drifting)
- For S1 sessions with wind data: `|TWA - 180°| < 10°` (dead downwind)
- For E1 sessions without wind data: use fleet-derived TWA estimate from upwind/downwind leg segmentation in `processing/straight_lines.py` (if available) — otherwise fall back to apparent-leeway-only mode
- Window duration ≥ 30s
- `|yaw_rate|` low throughout (`< 2°/s`) — boat is steady, not gybing

Within the calibration window: `leeway_offset = median(leeway_apparent)`. Applied to all samples in the session: `leeway = leeway_apparent − leeway_offset`.

**Fallback behavior** when no calibration window is found (rare — upwind-finish format races):
- Output field `leeway` is **null**
- Output field `leeway_apparent` is populated with uncalibrated values
- `metadata.json` flags `leeway_calibrated: false`
- Debrief view #1 displays "Uncalibrated — results show heading-minus-COG only" banner

**Why this works.** On a well-set-up boat dead downwind with a symmetric spinnaker or no heel, leeway is physically near zero. The difference between heading and COG at that moment *is* the mounting/sensor alignment offset. Subtracting it gives true hydrodynamic leeway for all other points of sail.

**Known limitation.** Calibration assumes no current. In Boston Harbor, tidal currents of 0.5-1.5 kt are common. V2 should ingest NOAA station 8443970 current predictions and remove the current-drift component before calibration. For v1, document this as a known bias.

### Maneuver detection

**Event types:** `tack`, `gybe`, `bear_away`, `head_up` (v1 focuses on tack + gybe).

**Trigger (OR logic):**
- `|yaw_rate| >= 15°/s` sustained for ≥ 1.0s, OR
- COG change ≥ 60° within any 8s rolling window

Whichever fires first creates a candidate event at time `t_start`.

**Classification:** Look at COG change from `t_start − 5s` to `t_start + 15s`:
- If TWA (or approximate TWA from fleet-derived wind) crosses 0° → tack
- If TWA crosses 180° → gybe
- For E1 sessions without wind: classify by COG change magnitude and direction alone, flag as `maneuver` generically if ambiguous

**Phase decomposition** — for each event, emit these timestamps and metrics:
- `t_entry` — yaw_rate first exceeds 5°/s
- `t_head_to_wind` (tacks only) — COG crosses estimated wind axis
- `t_exit` — yaw_rate returns below 5°/s
- `t_speed_rebuild` — SOG returns to 90% of 10s pre-entry average
- `sog_entry` — SOG at `t_entry`
- `sog_min` — minimum SOG between entry and rebuild
- `time_to_rebuild_seconds` — `t_speed_rebuild − t_entry`
- `vmg_loss_estimate_meters` — `integral((sog_steady − sog_actual) * cos(twa)) dt` from entry to rebuild. Requires TWA estimate; null if not available.
- `heel_peak_deg` — max `|heel|` during window
- `yaw_rate_peak_deg_s` — max `|yaw_rate|` during window

**Config parameters** (in `config/fusion.yaml`):
- `yaw_rate_trigger_deg_s: 15.0`
- `yaw_rate_sustain_seconds: 1.0`
- `cog_change_trigger_deg: 60.0`
- `cog_change_window_seconds: 8.0`
- `event_window_pre_seconds: 5.0`
- `event_window_post_seconds: 15.0`
- `speed_rebuild_fraction: 0.9`

---

## Data schemas

### `fused.parquet` — 10 Hz timeseries

| Column | Type | Units | Notes |
|---|---|---|---|
| `t_utc` | timestamp[ns, UTC] | — | Authoritative clock, from GNSS |
| `device_id` | string | — | `E1-01`, `sailframes-01`, etc. |
| `session_id` | string | — | `YYYYMMDD_HHMMSS` |
| `lat` | float64 | deg | PPK solution |
| `lon` | float64 | deg | PPK solution |
| `ellipsoid_h` | float32 | m | PPK solution |
| `pos_q` | uint8 | — | RTKLIB quality: 1=fix, 2=float, 5=single |
| `pos_sigma_h` | float32 | m | Horizontal standard deviation |
| `pos_sigma_v` | float32 | m | Vertical standard deviation |
| `sog` | float32 | kt | From PPK velocity |
| `cog` | float32 | deg, 0-360 | From PPK velocity, null if sog < 0.5 kt |
| `heading_true` | float32 | deg, 0-360 | Fused, null before initial convergence |
| `heel` | float32 | deg, signed | + = starboard heel |
| `pitch` | float32 | deg, signed | + = bow up |
| `yaw_rate` | float32 | deg/s, signed | Body frame z |
| `pitch_rate` | float32 | deg/s, signed | Body frame y |
| `roll_rate` | float32 | deg/s, signed | Body frame x |
| `leeway` | float32 | deg, signed | Calibrated; null if uncalibrated or sog < 1.5 kt |
| `leeway_apparent` | float32 | deg, signed | Uncalibrated heading−cog |
| `accel_lat` | float32 | m/s² | Boat frame, linear accel (gravity removed) |
| `accel_long` | float32 | m/s² | Boat frame |
| `accel_vert` | float32 | m/s² | Boat frame |
| `sat_count_gps` | uint8 | — | From NMEA GSA |
| `sat_count_glo` | uint8 | — | |
| `sat_count_gal` | uint8 | — | |
| `sat_count_bds` | uint8 | — | |
| `pdop` | float32 | — | From NMEA GSA |
| `cycle_slip` | bool | — | From RTKLIB |
| *(S1 only)* `aws` | float32 | kt | Apparent wind speed |
| *(S1 only)* `awa` | float32 | deg | Apparent wind angle, 0=bow, 180-corrected |
| *(S1 only)* `baro_hpa` | float32 | hPa | |

Partitioning: by `device_id`, then `session_id`. Typical size: ~1-2 MB per boat-race.

### `events.parquet` — one row per maneuver

| Column | Type | Notes |
|---|---|---|
| `event_id` | string | UUID |
| `device_id` | string | |
| `session_id` | string | |
| `event_type` | string | `tack`, `gybe`, `maneuver` |
| `t_entry` | timestamp[ns, UTC] | |
| `t_head_to_wind` | timestamp[ns, UTC] | tacks only, null otherwise |
| `t_exit` | timestamp[ns, UTC] | |
| `t_speed_rebuild` | timestamp[ns, UTC] | null if didn't rebuild |
| `sog_entry` | float32 | kt |
| `sog_min` | float32 | kt |
| `time_to_rebuild_seconds` | float32 | |
| `vmg_loss_estimate_meters` | float32 | null if TWA unavailable |
| `heel_peak_deg` | float32 | |
| `yaw_rate_peak_deg_s` | float32 | |
| `trigger_source` | string | `yaw_rate`, `cog_change`, or `both` |

### `metadata.json`

```json
{
  "device_id": "E1-01",
  "session_id": "20260405_225030",
  "t_start_utc": "2026-04-05T22:50:30Z",
  "t_end_utc": "2026-04-06T00:35:12Z",
  "fw_version": "e1-v1.3.2",
  "fusion_version": "v1.0.0",
  "fusion_config": { "tau_seconds": 15.0, "sog_gate_knots": 2.0, "..." : "..." },
  "ppk_cors_station": "MAMI",
  "ppk_fix_rate_percent": 94.3,
  "leeway_calibrated": true,
  "leeway_offset_deg": -2.4,
  "leeway_calibration_window": ["2026-04-05T23:12:10Z", "2026-04-05T23:12:45Z"],
  "event_count": { "tack": 7, "gybe": 4, "maneuver": 1 }
}
```

---

## Debrief views (v1 scope — three views only)

These live in `web/frontend/` and read from the processed Parquet files via `web/api/`. This spec specifies behavior only; component structure is Claude Code's call based on the existing frontend patterns.

### View 1: Leeway & VMG scatter

**Data source:** `fused.parquet`, filtered to upwind legs (TWA ∈ [30°, 60°] port or starboard, requires leg segmentation from `processing/straight_lines.py`).

**Plot:** Scatter of `leeway` (x) vs `sog` (y), points colored by `|heel|`. Overlay: fleet median line if multiple boats in same race. Side panel shows each boat's median upwind leeway, median upwind SOG, median heel, and optimal-heel band (heel bucket with highest median VMG).

**Empty state:** If `leeway_calibrated == false`, show banner: "Leeway calibration not available for this session (no sustained downwind leg detected). Showing apparent leeway — results include mounting offset and should be compared within this session only."

### View 2: Maneuver decomposition timeline

**Data source:** `events.parquet` (list) + `fused.parquet` (strip charts on demand).

**List panel:** Table of all maneuvers, sortable by `time_to_rebuild_seconds` or `vmg_loss_estimate_meters` descending. Columns: time, type, entry SOG, min SOG, rebuild time, VMG loss, heel peak.

**Detail panel:** When a maneuver is selected, 20-second strip chart (t_entry − 5s to t_entry + 15s) with stacked traces:
- SOG (with 90%-of-pre-entry line overlaid)
- Heading (with head-to-wind marker for tacks)
- Heel
- Yaw rate

Event markers vertical lines at t_entry, t_head_to_wind, t_exit, t_speed_rebuild.

**Comparison mode (if fleet data available):** overlay same-race tacks from other boats at matched time offsets from t_entry.

### View 3: Track quality / PPK diagnostics

**Data source:** `fused.parquet` + `metadata.json`.

**Main view:** Session track plotted on map (Leaflet/MapLibre — follow existing frontend choice), colored by `pos_q` (green=fix, yellow=float, red=single). Line thickness modulated by `pos_sigma_h`.

**Side panel:**
- PPK fix rate percentage (from metadata)
- CORS station used
- Timeline chart: `sat_count_total`, `pdop`, `cycle_slip` flags over session duration
- Table of cycle slip events with timestamps

**Purpose:** Explains anomalies in the other two views. If a leeway outlier corresponds to a PPK float-solution stretch, it's noise, not performance.

---

## Implementation plan (for Claude Code)

Order matters because later steps depend on earlier outputs:

1. **Pure fusion function.** `processing/fusion.py::fuse_session(raw_files, cors_rinex, config) -> (fused_df, events_df, metadata)`. Unit tests against a synthetic dataset where ground truth heading and maneuvers are known.
2. **RTKLIB integration.** `processing/ppk.py::run_ppk(rtcm3_or_ubx_path, cors_rinex_path, config) -> pos_df`. Wrapper around `convbin` and `rnx2rtkp`. Error handling for missing CORS data (retry with exponential backoff; write partial result with PPK-skipped flag).
3. **Schema extensions.** Add `FusedTimeseries`, `ManeuverEvent`, and `SessionMetadata` to `processing/models.py`. Pydantic models. Must not break existing schemas.
4. **Lambda integration.** Extend `lambda/process_upload/` (or add `lambda/fuse_session/` — whichever aligns better with existing patterns — Claude Code decides after reading the code) to call the pure function and write outputs. S3 event filter on `.rtcm3` and `.ubx` suffixes.
5. **CORS caching.** `processing/cors.py::fetch_cors(station, time_window)` with S3 cache. Target station for Boston Harbor: `MAMI` (Leica GR50, L1/L2/L5).
6. **Reprocess script.** `scripts/reprocess_session.py --device E1-01 --session 20260405_225030` and `scripts/reprocess_season.py --since 2026-03-01`.
7. **API endpoints.** `web/api/` adds `GET /sessions/{session_id}/fused`, `GET /sessions/{session_id}/events`, `GET /sessions/{session_id}/metadata`. Parquet served as Arrow IPC to frontend, or as JSON for small event lists.
8. **Frontend views.** Three views above in `web/frontend/`. Follow existing component conventions.

---

## Testing

**Synthetic data.** `tests/fixtures/synthetic_session.py` generates a race with known ground-truth heading, known leeway, and known tack/gybe events. Fusion output must match within tolerances:
- Heading within ±2° (steady-state)
- Leeway within ±1° (when calibrated)
- All tacks detected, no false positives above yaw_rate_trigger
- Maneuver timing within ±0.5s

**Real-world regression.** After first weekend with v1 deployed, pick one session with obviously good data and one with known issues (float solution patches, etc.). Store as regression fixtures. Any fusion algorithm change must reproduce these within tolerance or explain why.

**Integration test.** End-to-end Lambda invocation with a small sample session, asserting all three output files written to S3 with expected schema.

---

## What this spec deliberately defers

- **Tight-coupled GNSS/INS (Kalman/EKF).** Loose complementary filter is sufficient for v1 views. Revisit if view 3 shows cycle slips the loose filter can't explain, or if view 1 leeway has unexplained noise.
- **True wind motion correction.** Needs S1 apparent wind + fused motion. Defer to v2 when polar view is built.
- **Fleet alignment Lambda.** Cross-boat joins on t_utc. Belongs to a separate `sailframes-fleet-align` spec.
- **Current correction for leeway calibration.** NOAA station 8443970 integration. V2.
- **Adaptive τ.** V2 if v1 data shows the need.
- **Starboard/port leeway asymmetry.** Boats often leeway differently on each tack. V2 reports this separately; v1 aggregates.

---

## Open questions for Claude Code to resolve by reading the repo

- Is there an existing `device_id` column convention, or does fusion need to define it?
- Does `processing/maneuvers.py` already have a detection implementation that should be preserved/wrapped, or is it a stub?
- Does `lambda/process_upload` already run per-file or per-session? If per-file, does it have idempotency?
- What's the existing PostgreSQL schema for sessions, and does fusion write there or only to S3 Parquet?
- What frontend charting library is in use? (Affects view implementation but not data.)

These should be resolved by reading the code, not by guessing.

---

*Spec version: v1.0.0-draft — Generated for review before handoff to Claude Code.*
