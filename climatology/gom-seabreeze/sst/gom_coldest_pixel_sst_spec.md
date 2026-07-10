# Gulf of Maine — Coldest-Dark-Pixel SST Compositing Pipeline

**Purpose.** Produce a daily, gap-free, ~1–2 km SST field over the WRF d01 footprint (Gulf of Maine) that *preserves* the cold near-shore structure — coastal upwelling, tidal-mixing fronts, the Merrimack plume — because that structure sets the cold side of the land–sea temperature contrast that drives the Salem Sound sea breeze. This is the single highest-leverage input to the downscale.

**Why not just use MUR / OSTIA / RTG.** Standard L4 SST products declould using warmest-pixel–style compositing, which mistakes genuinely cold, clear water (upwelling/mixing) for cloud and deletes it — rendering the coast a uniform warm blob and erasing the ΔT gradient. RUCOOL's fix (NREL-validated to beat HRRR in the Mid-Atlantic Bight) is a **coldest-dark-pixel** composite: among pixels *confirmed clear by a proper cloud mask*, keep the coldest. This spec ports that method to the Gulf of Maine.

---

## 1. Target grid

- Regular grid at **1–2 km** covering the d01 extent (~38.5–46.6°N, 76–65°W). 1 km if you want to force d03 crisply; 2 km matches RUCOOL and is cheaper. WRF interpolates to each nest, so one field covers all three domains.
- Output as WPS intermediate (for `metgrid`) **and** NetCDF (for QC / injection). See §7.

## 2. Inputs (satellite SST for the Gulf of Maine)

Primary — polar IR, per-pixel quality, ~750 m–1 km:
- **VIIRS ACSPO L2P/L3U** — SNPP + NOAA-20 + NOAA-21 (~750 m). Primary source.
- **AVHRR ACSPO** — Metop-B/C (~1 km). Temporal fill.
- **MODIS L2P** — Aqua + Terra (~1 km). Fill.
- *(optional)* **Sentinel-3 SLSTR L2P** (~1 km). More looks against cloud gaps.

Diurnal (optional augmentation, §6):
- **GOES-19 ABI L2 SST** — geostationary, ~2 km, hourly. The only source that sees the daytime shallow-water warming.

Background for gap-fill (§5):
- **GHRSST L4 MUR** (1 km, gap-free daily) or **OSTIA** (~5 km). Used only where no clear obs exist in the window.

Access:
- **NOAA CoastWatch ERDDAP** (coastwatch.pfeg.noaa.gov / OSPO) — VIIRS/AVHRR ACSPO, GOES ABI.
- **NASA PODAAC** — GHRSST L2P/L3U (VIIRS/MODIS) and MUR L4.
- Anchor obs (§6): **NDBC 44013, 44018, 44098, 44005** SST; nearshore stations (BHBM3).

## 3. Clear-sky selection (the crux — do this BEFORE any coldest operation)

1. Ingest L2/L3 SST **with the per-pixel quality level** (ACSPO `quality_level`, 0–5).
2. Keep only **QL ≥ 4** (prefer QL = 5). This is the whole game: rely on the sensor's clear-sky mask to establish "clear," *then* take coldest among clear. Do **not** derive cloudiness from temperature — that's the warmest-pixel trap that eats upwelling.
3. Drop pixels flagged for sea ice, sun glint, or high satellite zenith (> ~55°).

## 4. Regional screening + coldest-clear compositing

Protect against cloud leakage (imperfect masks let cold cloud edges through), then composite:

1. **Climatological floor.** Reject any QL-passed pixel colder than a per-cell, per-week SST climatology (e.g., from the multi-year MUR/CDR record) by more than a tunable Δ (start **Δ = 4 °C**). Catches residual cloud without removing real upwelling (upwelling in the GoM is typically 2–4 °C below surroundings — set Δ above that).
2. **Spatial coherence / despeckle.** Reject isolated cold pixels inconsistent with their neighborhood (e.g., a pixel > 2–3 °C colder than a 5×5 median). Removes speckle; keeps coherent upwelling filaments.
3. **Coldest-clear over a rolling window.** For each grid cell, over a **3-day** trailing window (RUCOOL's choice), take the **minimum** QL-passed, screened SST. Rationale: within a few days the coldest confirmed-clear observation best represents the true cool surface state (upwelling/mixing), and it will not have been deleted by warmest-pixel logic.
   - Window length is a tradeoff (see §8). 3 days is the default; shorten toward 1–2 days in rapidly evolving conditions if you have enough clear looks.

## 5. Gap-fill to a continuous field

After coldest-clear compositing, persistently cloudy cells are still empty. Fill in this order:
1. Where the window has ≥1 valid coldest-clear value → use it.
2. Where empty → fill from the **L4 background** (MUR), **feathering** the seam (distance-weighted blend over a few km) so the injected upwelling doesn't create a step the model reads as a spurious front.
3. Optional: OI/kriging across small gaps from surrounding valid composite pixels instead of pure L4 fill.

Result: gap-free ~1–2 km field with real near-shore cold structure where observed, smoothly reverting to L4 offshore/under-cloud.

## 6. Buoy anchoring + (optional) diurnal augmentation

- **Anchor.** Bias-correct the composite toward in-situ SST at 44013/44018/44098 (small-radius nudge or a domain-mean offset). Buoy SST is your SST truth.
- **Diurnal (optional).** The coldest composite deliberately suppresses daytime warming — great for upwelling, but it can under-represent the **afternoon ΔT peak** that strengthens the sea breeze in shallow Mass Bay. Two ways to recover it:
  - Superimpose a diurnal warming curve from **GOES-19 hourly ABI** on the coldest baseline (add the ABI hourly anomaly relative to that day's ABI minimum).
  - Or let WRF do it: `sst_skin = 1` (already in namelist.input) applies a diurnal skin-SST parameterization over water. Simplest; partial.
  - Cleanest long-term: couple WRF↔FVCOM/ROMS (NECOFS) so SST evolves with tides/fluxes. Bigger build (see §8 dead-ends).

## 7. WRF integration (getting the field into the model)

`sst_update = 1` in namelist.input makes WRF read a new SST from `wrflowinp_d0N` at `auxinput4_interval` (daily). To feed **your** composite instead of the driver's SST:

- **Path A (recommended): patch `met_em`.** Run WPS normally (driver SST included), then overwrite the `SST` variable in each `met_em.d0N.<date>` file with your composite regridded to that domain, before `real.exe`. `real.exe` then builds `wrfinput_d0N` and `wrflowinp_d0N` from the patched SST. Simplest to script (xarray/NetCDF); no WPS internals.
- **Path B: separate ungrib stream.** Write the composite to WPS intermediate format and add it as a second `fg_name` in `&metgrid` so `metgrid` writes it into `met_em` directly.

Either way, confirm `SST` in `wrfinput`/`wrflowinp` shows your cold structure before launching WRF (quick `ncview`/`ncdump` check).

**Coastline note.** SST only matters where the land-water mask says "water." If d03's default mask is too coarse to separate Marblehead Neck / harbor islands, replace `LANDMASK`/`LU_INDEX` on d03 (post-geogrid) with a high-res mask from **GSHHG** or **NLCD**, and make sure your SST grid and that mask agree on the coastline.

## 8. Design tensions / dead-ends (documented, don't relitigate)

- **Coldest vs diurnal peak.** Coldest-clear compositing captures upwelling but suppresses the afternoon warm side of the ΔT. Accept for upwelling fidelity; recover the diurnal signal via GOES augmentation, `sst_skin`, or ocean coupling — not by switching to a mean/warmest composite (that reintroduces the upwelling-deletion bug).
- **Window length.** Longer window = fewer gaps but staler SST (misses fast upwelling onset/relaxation); shorter = fresher but gappier. 3 days is the validated default; tune with clear-look density.
- **Static SST vs coupling.** A daily static composite (even a good one) can't evolve through the run. Full WRF↔FVCOM/ROMS coupling is the frontier fix but a large build — out of scope for v1; static composite first, couple later only if benchmark shows the diurnal/tidal SST error is the limiting term.
- **Near-shore pixel contamination.** IR SST within ~1 pixel of land is unreliable (land leakage). Mask the first coastal pixel row and let gap-fill/feathering bridge to shore rather than trusting contaminated near-shore retrievals.

## 9. Cadence / automation

- Run daily, pre-dawn, so the composite is ready before the race-morning WRF launch.
- Pull last 3 days of VIIRS/AVHRR/MODIS ACSPO from CoastWatch ERDDAP → screen → coldest-clear → gap-fill (MUR) → anchor (buoys) → regrid to d01 → patch `met_em` → `real.exe` → WRF.
- For the **climatology** build, run the same pipeline over each catalogued past sea-breeze day (ERA5-driven WRF), producing the historical high-res field the analog engine indexes.

## References
- Optis et al. 2020, *Validation of RU-WRF* — NREL/TP-5000-75209 (nrel.gov/docs/fy20osti/75209.pdf): config, PBL, coldest-pixel SST evaluation.
- RUCOOL coldest-dark-pixel method: AVHRR/VIIRS + MAB-tuned declouding, 2 km, 3-day composite, used as WRF lower boundary.
- ACSPO SST (NOAA NESDIS/OSPO); GHRSST MUR L4 (JPL/PODAAC).
