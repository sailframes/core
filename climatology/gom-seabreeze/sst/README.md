# sst/ — coldest-dark-pixel SST pipeline

The #1 lever. See `gom_coldest_pixel_sst_spec.md` for the full method + design
tensions.

- `build_coldest_sst.py` — pull ACSPO L3S (VIIRS+AVHRR, ~2 km) from CoastWatch
  ERDDAP → QL≥5 clear → despeckle → COLDEST of N days → MUR gap-fill → buoy
  anchor → `sst_YYYY-MM-DD.nc` (K). `--plot` for the eyeball check.
- `patch_met_em_sst.py` — regrid composite to each WRF domain, overwrite `SST`
  over water in met_em (netCDF4 r+, `.bak` backups), with nearest + driver-SST
  fallback so no water cell is NaN, plus plausibility QC. `--plot` per domain.

## Verify
- ERDDAP dataset IDs are versioned — confirm at
  coastwatch.noaa.gov/erddap/griddap/index.html if a pull is empty.
- `QL_MIN=5` (confidently clear). Relax to 4 for coverage at cloud-leak risk.
- Default is AM/day L3S; add the PM/night dataset for a cooler, cleaner coldest
  baseline (night is preferable — but see the coldest-vs-diurnal tension).
- Both scripts are syntax-checked, not runtime-tested — shake down on one date.
