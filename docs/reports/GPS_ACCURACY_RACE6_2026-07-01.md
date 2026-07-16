# GPS precision & accuracy — CYC Wed Spring Race 6 (2026‑07‑01)

GPS-quality review for every boat with a track in Race 6 — the **three SailFrames
units** plus **six owner-supplied tracks** (Vakaros, phone apps, GPS loggers).
Race 003bef01 · `sailframes.com/race.html?race=003bef01`.

| Boat | Source | Device / module | Rate | Data |
|---|---|---|---|---|
| Katü | SailFrames **B2** | Quectel LC29HEA | 10 Hz | ✅ 190,889 fixes |
| Wizard | SailFrames **E3** | Waveshare LG290P | 10 Hz | ✅ 88,764 fixes |
| Doc Buck | SailFrames **E4** | Waveshare LG290P | 10 Hz | ✅ 103,898 fixes |
| Amigo | SailFrames **B1** | Quectel LC29HEA | — | ❌ **no data — did not record** |
| RockIt 2.0 | Vakaros | **Atlas** race processor | 5 Hz | ✅ 42,891 pts |
| Uproarious | GPX logger | (reports sats) | 1 Hz | ✅ 12,418 pts |
| MASHNEE | GPX logger | — | 1 Hz | ✅ 10,063 pts |
| Eagle (McLean) | GPX logger | — | 0.1 Hz | ✅ 582 pts (sparse) |
| Agora | phone | Sensor Logger app | 1 Hz | ✅ 4,454 pts |
| Never Settle | phone | Sensor Logger app | 1 Hz | ✅ 9,812 pts |

⚠️ **These are heterogeneous devices, antennas, mounts and log rates** — not a
controlled A/B. The SailFrames units ran **RTK disabled** (plain GPS/SBAS, fix
1–2, no cm-level RTK). Treat cross-device numbers as indicative, not a ranking of
silicon.

## TL;DR
- **Logging quality is excellent on all three that recorded**: solid 10 Hz, no
  gaps > 1 s, ≥ 30 sats.
- **The LG290P (E3/E4) has the stronger front end** — ~34 sats vs ~30 and much
  lower, steadier HDOP (~0.30 vs ~0.45) than the LC29HEA (B2).
- **Position precision** (scatter while sitting still): **E3 ≈ 0.9 m CEP50 is
  the cleanest result**; B2 and E4 land ~2 m, but E4's number is inflated by
  mooring swing, not receiver noise.
- **"Accuracy" (closeness to truth) is not measured here** — there is no survey
  ground-truth, and the receivers' own `hacc` is self-reported (and, on the
  LG290P, essentially a fixed 1.30 m floor). Don't rank the receivers on `hacc`.
- **B1/Amigo captured nothing** (a single fix at 42.8 m / 4 sats). It didn't
  record the race — a fleet-readiness failure, not a GPS-quality data point.
- **Across all sources, log rate spans 100×** (10 Hz → 0.1 Hz). Only the
  SailFrames units (10 Hz) and RockIt's Vakaros Atlas (5 Hz) are fine-grained
  enough for per-tack / start-line tactical work; the 1 Hz phone/GPX logs are
  coarse and Eagle's 0.1 Hz is a bare track.
- **Vakaros Atlas (RockIt) shows the tightest scatter (~0.1 m)** — but that's a
  filtered/smoothed race-processor output, not raw GNSS, so it isn't comparable
  to the raw receivers.
- **Phone GPS (Sensor Logger) is the least accurate** — 2–8 m self-reported and
  spiky (p95 to 14 m); fine for a rough track, not for close-quarters.

## All 9 sources at a glance
Precision = position scatter over each boat's **stillest window** (longest run
< 0.5 kt). "n/a" = no clean still window in that log (device never sat still, or
the log has no speed field to detect it). Self-`hacc` only where the device
reports it.

| Boat | Source | Rate | Stillest window | CEP50 | DRMS | Self-`hacc` (med / p95) |
|---|---|---|---|---|---|---|
| Wizard (E3) | SailFrames LG290P | 10 Hz | 585 s | **0.88 m** | 1.29 m | 1.3 m (floor) |
| Katü (B2) | SailFrames LC29HEA | 10 Hz | 6428 s | 2.09 m | 2.36 m | 2.2 / 3.6 m |
| Doc Buck (E4) | SailFrames LG290P | 10 Hz | 318 s | 2.07 m† | 2.36 m† | 1.3 m (floor) |
| RockIt 2.0 | Vakaros Atlas | 5 Hz | 49 s | 0.06 m\* | 0.10 m\* | — |
| Uproarious | GPX logger | 1 Hz | 985 s | 0.82 m | 3.91 m | — |
| Never Settle | phone (Sensor Logger) | 1 Hz | 4005 s | 1.05 m | 5.62 m | 2.1 / 14.2 m |
| Agora | phone (Sensor Logger) | 1 Hz | n/a | — | — | 4.8 / 8.1 m |
| MASHNEE | GPX logger | 1 Hz | n/a | — | — | — |
| Eagle (McLean) | GPX logger | 0.1 Hz | n/a | — | — | — |

\* **Vakaros Atlas ~0.1 m is filtered/smoothed** race-processor output, not raw
GNSS noise — do not compare it directly to the raw receivers. † E4's number is
inflated by mooring swing (see below), not receiver noise.

**What this says about picking a tracker:**
- For **tactical analysis** (starts, per-tack VMG, close-quarters), you need
  ≥ 5 Hz: only the **SailFrames (10 Hz)** and **Vakaros (5 Hz)** qualify. The
  1 Hz phone/GPX logs and Eagle's 0.1 Hz miss the fast dynamics.
- Among the **raw** receivers, the **LG290P (E3)** gives the best
  precision/rate balance (~0.9 m at 10 Hz); the LC29HEA (B2) is ~2 m.
- **Phone GPS is the weakest** — usable for a coarse track and replay, spiky at
  the metres level, not for boat-length decisions.

## What "precision" and "accuracy" mean here
- **Precision (repeatability)** — how much the reported position wanders while
  the antenna is physically fixed. We measure it from each boat's **stillest
  window** (longest run under 0.5 kt) as position standard deviation, DRMS, and
  CEP50/CEP95. This is a real, measurable number — but it's an **upper bound**:
  it also captures real antenna motion (mooring swing, float movement), so the
  true GNSS noise is ≤ these figures.
- **Accuracy (closeness to true position)** — **not measurable in this dataset.**
  We have no surveyed reference point, and the tracks are of moving boats. The
  only accuracy proxy is each receiver's self-reported `hacc`, which is an
  estimate, not truth — and the two chips compute it from **different sentences**
  (LG290P from NMEA `GST`, LC29HEA from `PQTMEPE`), so their `hacc` values are
  **not directly comparable**.

## Results

### Logging continuity (10 Hz target)
| | Rate | Late/missed fix (dt>0.15 s) | Gaps > 1 s | Max gap |
|---|---|---|---|---|
| B2 (LC29HEA) | 10.0 Hz | 1247 (0.65 %) | 0 | 0.3 s |
| E3 (LG290P) | 10.0 Hz | 239 (0.27 %) | 0 | 0.4 s |
| E4 (LG290P) | 10.0 Hz | **1 (0.00 %)** | 0 | 0.3 s |

All three hold a true 10 Hz with no dropouts. E4 was essentially flawless; B2
had the most late fixes but still 99.35 % on-cadence.

### Signal quality
| | Sats (med / min–max) | HDOP (med / p95 / max) | Fix quality |
|---|---|---|---|
| B2 (LC29HEA) | 30 / 17–39 | 0.45 / 1.21 / 1.91 | 99.8 % SBAS, 0.2 % GPS |
| E3 (LG290P) | 34 / 26–37 | 0.31 / 0.35 / 0.69 | 93.4 % SBAS, 6.6 % GPS |
| E4 (LG290P) | 34 / 28–38 | 0.30 / 0.34 / 0.55 | 99.2 % SBAS, 0.8 % GPS |

The **LG290P tracks ~4 more satellites and holds a markedly lower and more
stable HDOP** (better sky geometry). The LC29HEA's HDOP is fine but wanders
higher (up to ~1.9), consistent with its lower sat count.

### Self-reported horizontal accuracy (`hacc`) — read with care
| | median | mean | p95 | max | varies? |
|---|---|---|---|---|---|
| B2 (LC29HEA) | 2.17 m | 2.32 m | 3.63 m | 10.6 m | **yes** — 3788 distinct, σ=0.77 |
| E3 (LG290P) | 1.30 m | 1.31 m | 1.41 m | 2.07 m | **barely** — pinned near 1.30 m, σ=0.05 |
| E4 (LG290P) | 1.30 m | 1.31 m | 1.36 m | 1.88 m | **barely** — pinned near 1.30 m, σ=0.04 |

⚠️ **The LG290P's `hacc` is effectively a fixed ~1.30 m floor**, not a live,
condition-tracking estimate — so "E3 1.3 m beats B2 2.2 m" is **not** a valid
accuracy comparison. The LC29HEA's `hacc` genuinely varies with conditions and
is the more informative (and more conservative) self-estimate.

### Position precision — stillest-window scatter (the real measurement)
| | Window | Mean SOG | σ‑E / σ‑N | DRMS | CEP50 | CEP95 | `hacc` in window |
|---|---|---|---|---|---|---|---|
| B2 (LC29HEA) | 6428 s | 0.01 kt | 1.77 / 1.57 m | 2.36 m | 2.09 m | 4.22 m | 3.20 m |
| E3 (LG290P) | 585 s | 0.03 kt | 0.92 / 0.90 m | **1.29 m** | **0.88 m** | 3.00 m | 1.31 m |
| E4 (LG290P) | 318 s | 0.03 kt | 0.21 / **2.35 m** | 2.36 m | 2.07 m | 4.60 m | 1.30 m |

- **E3 (LG290P) is the cleanest**: ~0.9 m median radial scatter, symmetric (σ‑E ≈
  σ‑N), over a long still window — that's a genuine sub-metre precision result.
- **B2 (LC29HEA)** sat still the longest (107 min at 0.01 kt) and shows isotropic
  ~2 m scatter — a trustworthy ~2 m precision figure.
- **E4 (LG290P)** looks worse (2.4 m DRMS) **only because its scatter is
  wildly asymmetric** (σ‑N 2.35 m vs σ‑E 0.21 m) — that's a boat swinging N–S on a
  mooring, not receiver noise. Same chip as E3, so its true precision is very
  likely ~E3's; the number here is dominated by real motion. **Discount it.**
- In every case the measured scatter is **tighter than the receiver's own
  `hacc`**, i.e. both chips report conservatively.

> **On sample size:** there is exactly **one clean precision result per module
> type** (E3 for the LG290P, B2 for the LC29HEA), on **different boats with
> different antenna mounts and sky**. So these numbers characterise *this
> install of each module*, not the silicon in isolation — read "the LG290P
> unit on Wizard" rather than "the LG290P chip." Satellite/HDOP counts (from
> both E3 and E4) are firmer since they agree across the two LG290P units.

## Conclusions
1. **Stronger standalone GNSS front end on the LG290P installs (E-series).**
   Both LG290P units (E3, E4) tracked more satellites (~34 vs ~30) and held a
   lower, steadier HDOP (~0.30 vs ~0.45) than the LC29HEA (B2). Where precision
   was cleanly measurable — E3 — it reached sub-metre (~0.9 m CEP50); the
   LC29HEA (B2) install shows a solid ~2 m.
2. **Do not compare the two on `hacc`** — the LG290P pins it at a 1.30 m floor;
   the LC29HEA reports a live estimate. Different statistic, different sentence.
3. **Absolute accuracy was not measured** (no ground truth). To claim accuracy,
   do a known-point test (park each unit over a surveyed mark and compare) or
   enable **RTK**, which is the real accuracy lever — the RTK path targets
   ~cm and would dwarf these standalone differences.
4. **B1/Amigo is a reliability failure, not a data point** — it recorded a single
   fix and missed the race. Root-cause its logging (SD / power / config) before
   the next event.

## Method / reproducibility
- **SailFrames units:** `scripts/gps_accuracy_race6.py` over
  `raw/<device>/2026-07-01/*_nav.csv`
  (`ms,utc,lat,lon,alt,sog,cog,sat,hdop,fix,gps_date,hacc`).
- **Owner-supplied tracks:** `scripts/gps_accuracy_race6_external.py` over the
  GPX files, the Vakaros `.vkx` (parsed with the handler's `parse_vkx_full`), and
  the Sensor Logger `Location.csv` exports (`horizontalAccuracy` → `hacc`).
- Common method: **stillest window** = longest contiguous run with SOG < 0.5 kt
  (≥ 30 s); scatter (σ‑E/σ‑N, DRMS, CEP50/95) computed in a local E/N metric
  plane about the window mean. UTC unwrapped across the 00:00 rollover.

All nine tracks are linked into race `003bef01` and visible on the replay
(SailFrames via `session_path`, owner tracks via `gpx_path`).
