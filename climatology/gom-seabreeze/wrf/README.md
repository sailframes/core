# wrf/ — WRF-ARW namelists (Gulf of Maine 9/3/1 km)

RU-WRF-style config: MYNN PBL + MYNN sfc layer + RUC LSM + Thompson + RRTMG,
coldest-pixel SST as lower boundary via `sst_update`. Domains: d01 9 km
(Gulf of Maine + offshore), d02 3 km (New England coast), d03 1 km
(Salem Sound / Mass Bay). One-way nesting.

## Must verify / set before running
- `num_metgrid_levels` — 34 (GFS 0.25°) / 38 (ERA5) / HRRR's count. Wrong value
  = real.exe crash.
- Nest placement — run `plotgrids.exe`, nudge `i_/j_parent_start` so d03 sits
  over Salem Sound → Cape Ann → Cape Cod Bay. Geometry must match between the
  two namelists.
- `geog_data_path`, dates.
- SST injection — `sst_update=1` is wired, but WRF uses the *driver's* SST unless
  you patch met_em first (see ../sst/). Confirm cold structure in wrfinput.

## Known knobs
- RUC LSM (`sf_surface_physics=3`) matches RU-WRF; Noah-MP (`=4`, `num_soil_layers=4`)
  is lower-friction with GFS soil.
- `sst_skin=1` gives partial diurnal-SST recovery (see coldest-vs-diurnal tension).
- `&fdda` spectral nudging on d01 — ON for hindcast/climatology, OFF for forecast.
