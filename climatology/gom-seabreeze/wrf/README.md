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

## Static geography detail (`--geog-detail`)

The land-**water** mask sets where the coast sits at grid scale — the #1
geography lever for the sea breeze. Topography is secondary here (Cape Ann /
Marblehead relief is <30 m; the "Marblehead Neck shadow" is coastline
*geometry*, not height). At d03 (1 km) the default 30s (~900 m) mask can misplace
Salem Sound / the harbor islands by nearly a whole cell.

The driver renders `geog_data_res` (namelist.wps) + `num_land_cat` (namelist.input)
from one flag:

| `--geog-detail` | `geog_data_res` | `num_land_cat` | notes |
|---|---|---|---|
| `modis` (default) | `30s` ×3 | 21 | MODIS 21-cat; byte-identical to the validated 2024 run |
| `nlcd` | `nlcd2011_9s+default` ×3 | 40 | NLCD 2011 9s (~250 m), NLCD40; sharp coast + Boston urban class |

NLCD is applied to **all three** domains (not just d03): `num_land_cat` is a single
global, so `MMINLU` must be consistent across the nest. `+default` fills the
non-CONUS parts of d01/d02 (offshore, Canada) from the MODIS default, remapped
into NLCD40 space.

### Running it
```bash
# cheap: geogrid + landmask QC only (minutes, no GFS/real/wrf) — DO THIS FIRST
AWS_PROFILE=sailframes GOM_GEOG_DETAIL=nlcd GOM_GEOGRID_ONLY=1 GOM_TERMINATE=1 \
  run/aws/launch.sh 2024-07-31 forecast
#   -> s3://…/gom/2024-07-31/geoqc/nlcd/{geo_em.d0*.nc, geo_d03_nlcd_{LANDMASK,LU_INDEX}.png}
# eyeball the PNGs (or A/B vs a modis geo_em):
#   python validation/plot_geo_landmask.py --geo-dir ./geo_nlcd --compare ./geo_modis \
#     --domain 3 --field LANDMASK -o coast.png

# full run once the coastline looks right
AWS_PROFILE=sailframes GOM_GEOG_DETAIL=nlcd run/aws/launch.sh 2024-07-31 forecast
```

`run_case.sh` fetches `nlcd2011_ll_9s` into WPS_GEOG (cached to S3) and, before it
commits to the run, greps the container's `GEOGRID.TBL` for the `nlcd2011_9s`
entry and `VEGPARM.TBL` for an `NLCD40` section. If either is missing it aborts
with instructions rather than crashing deep in `real.exe`.

### Fallback: post-geogrid LANDMASK surgery
If the DTC 4.3 image lacks NLCD table support (VEGPARM.TBL has no NLCD40 section),
keep MODIS-21 + RUC untouched and override only `LANDMASK` / `LU_INDEX` /
`LANDUSEF` on d03 from a high-res GSHHG coastline at the met_em stage (mirror
`sst/patch_met_em_sst.py`). Zero LSM-compat risk, hits the same lever, loses the
urban bonus. See `sst/gom_coldest_pixel_sst_spec.md` §coastline — and the SST
composite grid must agree with the new mask on the coastline (SST only applies
where mask=water). Not yet implemented; the NLCD path is primary.
