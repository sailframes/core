#!/usr/bin/env python3
"""
kbox_fine_line.py  --  KBOX sea-breeze front extractor  [SKELETON / TODO]
=========================================================================
Extract the sea-breeze front position + timing from KBOX (Boston NEXRAD) and
compare it to the WRF d03 front, to validate the model's onset/penetration.

The sea-breeze front shows on radar as:
  * a clear-air REFLECTIVITY "fine line" (insect/aerosol convergence), and
  * a VELOCITY convergence couplet (onshore behind, offshore/calm ahead).

Method (to implement):
  1. Pull KBOX Level II for the race window (nexradaws from the NOAA AWS bucket
     'noaa-nexrad-level2', or ERDDAP/THREDDS), lowest elevation sweeps.
  2. Read with Py-ART. Threshold clear-air reflectivity (~ -5..15 dBZ) to a
     candidate fine-line; use radial velocity convergence to confirm the front.
  3. Trace the front line each volume -> (time, front position across the sound).
  4. Derive onset time (front first enters Salem Sound) and inland penetration.
  5. Compare to WRF d03: compute the model wind-convergence line from wrfout_d03
     10 m (u,v) (divergence minimum / SE-sector wind boundary) -> model front
     position+timing. Report onset error (min) and position error (km).

Deps: nexradaws, arm-pyart (Py-ART), numpy, xarray; optional metpy, cartopy.

Ties to: validation/benchmark_protocol.md (KBOX is the front-timing truth) and
the first-shakedown check in CLAUDE.md.
"""
import argparse

KBOX = dict(lat=41.9558, lon=-71.1369, name="KBOX")   # Boston/Taunton NEXRAD
# Salem Sound race-area box (front-crossing detection window)
SOUND_BBOX = dict(lat_min=42.3, lat_max=42.7, lon_min=-70.95, lon_max=-70.55)


def fetch_kbox_level2(date, t0, t1, outdir):
    """TODO: nexradaws -> download KBOX volumes in [t0,t1] to outdir."""
    raise NotImplementedError


def extract_fine_line(radar):
    """TODO: Py-ART radar object -> front polyline from clear-air reflectivity
    fine-line confirmed by velocity convergence."""
    raise NotImplementedError


def model_front_from_wrfout(wrfout_d03):
    """TODO: wrfout_d03 -> 10 m wind convergence line (front) per output time."""
    raise NotImplementedError


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True)
    ap.add_argument("--start", default="13:00", help="race window start LT")
    ap.add_argument("--end", default="18:00", help="race window end LT")
    ap.add_argument("--wrfout", default=None, help="wrfout_d03 for model comparison")
    args = ap.parse_args()
    print("SKELETON -- implement fetch_kbox_level2 / extract_fine_line / "
          "model_front_from_wrfout, then report onset(min) + position(km) errors.")


if __name__ == "__main__":
    main()
