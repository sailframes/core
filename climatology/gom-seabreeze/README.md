# gom-seabreeze

High-resolution sea-breeze prediction for Salem Sound / Massachusetts Bay racing.
A RU-WRF-style 1 km WRF downscale driven by a **coldest-dark-pixel SST** lower
boundary, run in two modes off one config: **forecast** (race morning) and
**hindcast** (to build the Tactics-Climatology analog record). Plus the
observation-side tooling to validate it and to benchmark it against the
commercial models (PredictWind PWG, Expedition WRF, HRRR).

This is the modeling backend for the SailFrames **Tactics Climatology** feature
(the "retrospective high-res geometry layer" over the Gloucester–Provincetown
corridor). It can live standalone or under `processing/` in `sailframes/core`.

---

## Why this exists

Every weekend the sea breeze makes or breaks races in Salem Sound, and the
local knowledge that predicts it is exactly the near-shore structure that 3 km
operational models (HRRR, RRFS) blur. The single highest-leverage fix — proven
by Rutgers' **RU-WRF** and validated by NREL to beat HRRR on the Mid-Atlantic
coast — is not finer grid, it's a **near-shore SST field that preserves coastal
upwelling and tidal-mixing fronts**. This project ports that recipe (coldest-
pixel SST + tuned MYNN PBL + 1 km nest) to the Gulf of Maine.

## Pipeline

```
                     ┌─ forecast mode: GFS or HRRR ICs/LBCs
   driver (WPS) ─────┤
                     └─ hindcast mode: ERA5 ICs/LBCs  ──►  &fdda nudging ON (climatology)
         │
         ▼
   geogrid / ungrib / metgrid  ──►  met_em.d0{1,2,3}.*.nc
         │
         │     sst/build_coldest_sst.py
         │       (ACSPO L3S  →  QL≥5 clear  →  despeckle  →  COLDEST of N days
         │        →  MUR gap-fill  →  buoy anchor)
         │             │
         │             ▼
         │        sst_YYYY-MM-DD.nc   (~2 km, Kelvin)
         │             │
         ▼             ▼
   sst/patch_met_em_sst.py  ──►  met_em w/ coldest-pixel SST as lower boundary
         │
         ▼
   real.exe  ──►  wrf.exe  ──►  wrfout_d03   (1 km Salem Sound / Mass Bay)
         │
         ▼
   validation/  (KBOX fine-line front timing; 44013 / KBVY point wind)
   benchmark/   (score PWG / Expedition / HRRR vs truth, by sea-breeze type)
```

## Layout

```
gom-seabreeze/
├── README.md                 ← you are here
├── CLAUDE.md                 ← context + hard constraints for Claude Code
├── requirements.txt
├── .gitignore
├── wrf/
│   ├── namelist.wps          ← WPS: 9/3/1 km Gulf of Maine domains          [BUILT]
│   ├── namelist.input        ← WRF: MYNN + RUC + Thompson + sst_update       [BUILT]
│   └── README.md
├── sst/
│   ├── build_coldest_sst.py  ← ERDDAP pull + coldest-clear compositing       [BUILT]
│   ├── patch_met_em_sst.py   ← inject composite into met_em SST + QC          [BUILT]
│   ├── gom_coldest_pixel_sst_spec.md                                          [BUILT]
│   └── README.md
├── run/
│   └── run_gom_seabreeze.py  ← end-to-end driver (WPS→SST→patch→real→wrf)     [BUILT]
├── validation/
│   ├── kbox_fine_line.py     ← KBOX radar sea-breeze front extractor          [SKELETON/TODO]
│   └── benchmark_protocol.md ← model-vs-truth scoring protocol                [SPEC]
└── fleet/
    └── fleet_wind_proxy.py   ← upwind-leg TWD / cross-course gradient proxy   [SKELETON/TODO]
```

## Quickstart (once WRF/WPS are built and geog installed)

```bash
pip install -r requirements.txt

# 1. build the SST lower boundary for a date
python sst/build_coldest_sst.py --date 2024-07-31 --anchor --plot

# 2. run WPS (geogrid/ungrib/metgrid) with wrf/namelist.wps, then inject SST
python sst/patch_met_em_sst.py --met-dir ./WPS --composite ./sst_out --domains 1 2 3 --plot

# 3. real.exe && wrf.exe    (wrf/namelist.input)
```

The driver in `run/` will chain all of the above for a date + mode once implemented.

## Run modes

Same namelists; only the WPS driver + a couple of switches change:

| | forecast | hindcast (climatology) |
|---|---|---|
| ICs/LBCs | GFS 0.25° (or HRRR) | ERA5 |
| `num_metgrid_levels` | 34 (GFS) | 38 (ERA5) |
| `&fdda` nudging on d01 | off | **on** (keep synoptic tied to ERA5) |
| purpose | race-morning field | build the analog record over past sea-breeze days |

## Dependencies

Python: xarray + an OPeNDAP backend (netCDF4 or pydap), scipy, numpy, requests,
matplotlib. Radar validation additionally needs nexradaws + Py-ART (or metpy).
External: WRF-ARW v4.x + WPS, WPS_GEOG (high-res tables), a driving model.

## Status

Built: WRF namelists, SST compositing + injection, end-to-end driver.
TODO (skeletons/specs present): KBOX front extractor, fleet wind proxy,
and the benchmark implementation. See CLAUDE.md for task order.

---

## Design decisions & dead-ends (resolved in chat — do NOT relitigate)

- **RU-WRF is the template, not a from-scratch design.** NREL-validated to match/
  beat HRRR on the US East coast via coldest-pixel SST + tuned MYNN + 1 km nest.
  Config = stock WRF; the value is in the SST + PBL, not novel dynamics.
- **SST is the #1 lever.** The land–sea ΔT drives the breeze; near-shore SST is
  the biggest coastal error source. Everything else is secondary.
- **Coldest-DARK-pixel, never warmest-pixel.** Standard warmest-pixel declouding
  deletes cold upwelled/mixed water as "cloud" and erases the ΔT gradient. Rely on
  the ACSPO clear-sky mask (QL≥5) to define *clear*, THEN take the coldest.
- **Coldest-vs-diurnal is a known tension.** The coldest composite suppresses the
  afternoon warm side of ΔT. Recover it via GOES-19 diurnal augmentation,
  `sst_skin=1`, or ocean coupling — NOT by switching to a mean/warmest composite
  (that reintroduces the upwelling-deletion bug).
- **1 km grid ⇒ ~6–7 km effective resolution.** d03 resolves front position, the
  Marblehead Neck shadow, and the Cape Ann convergence — NOT sub-km turbulence or
  a gust map. Sub-km would require LES-mode physics (3D TKE); out of scope.
- **One-way nesting** (`feedback=0`) to avoid feedback noise at 1 km.
- **You inherit the parent's synoptic errors.** Downscaling adds terrain/coastline
  detail, not new large-scale skill. On days the driver blows the synoptic or the
  offshore gradient, the nest inherits it (Giannaros' explicit finding).
- **Static SST first; couple later.** Full WRF↔FVCOM/ROMS coupling is the frontier
  fix for diurnal/tidal SST but a large build. Only pursue if the benchmark shows
  diurnal/tidal SST error is the limiting term.

### On truth (drives validation + benchmark)

- **No gridded product of pure wind observations exists.** Anything on a grid is a
  model + DA. NOAA's RTMA/URMA (2.5 km hourly GRIB2) is the closest gridded
  surface analysis, but it's background-dominated over water → useful *context*,
  not an independent scorer. HRRR analysis as truth for HRRR-derived forecasts is
  circular.
- **Real truth = point obs + radar + SAR:** NDBC 44013 / 44018 / 44098 (wind+SST),
  ASOS KBVY / KBOS (ISD/MADIS), KBOX radar fine-line (front position/timing),
  Sentinel-1 SAR (~1 km ocean wind, sporadic). Buoy SST is the SST truth.
- **No boat wind sensors currently** (future). Until then, in-area wind truth =
  44013 + KBVY + the fleet-heading proxy (`fleet/fleet_wind_proxy.py`).
- **Current contamination:** wind from a moving boat needs velocity *through water*
  (STW), not GPS SOG, or tidal current folds into the "true wind." Restrict to
  slack ±1 h or apply a tidal correction. Salem Sound channels ~0.5–1 kt.

### Commercial-model landscape (for the benchmark's build/buy decision)

- **PredictWind PWG/PWE** = 1 km **CCAM** downscale (NOT WRF) + PWAi AI model.
- **SailFlow/WeatherFlow** = TRRM ML blend + personal-weather-station obs (US).
- **Windy** = visualizer of others' models (no own model).
- **Weather4D** = widest raw-GRIB catalog; uniquely strong on Météo-France
  (AROME 1.3 km) + ocean/current models (Copernicus, NCOM/HYCOM) — the latter
  useful here for the SOG→STW tidal-current correction.
- **AI models** (AIFS, PWAi, GraphCast) are synoptic tools; ERA5-trained (~31 km),
  they underplay the sea breeze. Never the tactical scorer.
- **Decision rule:** build the custom WRF only if PWG/Expedition demonstrably miss
  transition-day onset/gradient (likely, because they use coarse global SST).
  Measure the gap first (see `validation/benchmark_protocol.md`).

## References

- Giannaros et al. 2018, *Meteorol. Appl.* 25:1672 — AEOLUS-RIO2016 (SRTM +
  GlobCover swaps improved wind skill; ICs were the limiter on bad days).
- Golding et al. 2014, *BAMS* 95:883 — London 2012 (333 m Weymouth Bay UM).
- Optis et al. 2020, **NREL/TP-5000-75209** — RU-WRF validation (coldest-pixel
  SST + MYNN; matches/beats HRRR).
- Enoshima climatology, *Climate* 2021, 9(5):80 — WRF+CALMET 10-yr race-area
  wind reconstruction (the climatology analog).
- ACSPO SST (NOAA/NESDIS/OSPO); GHRSST MUR L4 (JPL/PODAAC); CoastWatch ERDDAP.
