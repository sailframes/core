#!/usr/bin/env python3
"""
fleet_wind_proxy.py  --  fleet-derived TWD / gradient proxy  [SKELETON / TODO]
=============================================================================
Estimate true wind direction (TWD) and the cross-course gradient from SailFrames
fleet GPS tracks (position/SOG/COG per boat over time). This is the in-area wind
"truth" proxy until boats carry wind sensors -- low absolute accuracy, but a good
SHIFT-TIMING and CROSS-COURSE-GRADIENT detector, which is the tactical signal.

Method (to implement):
  1. Ingest fleet tracks (boat_id, time, lat, lon, sog, cog) from the SailFrames
     store (S3 Parquet / DuckDB).
  2. Detect UPWIND legs: cluster COG into port/starboard beat headings (bimodal),
     gated on SOG in the beat range for the boat class (J/80, Sonar).
  3. TWD estimate: for a matched port/starboard heading pair on the same boat/
     time window, TWD ~= angular midpoint of the two beat headings. Aggregate
     across boats for a fleet TWD(t).
  4. Cross-course gradient: bin boats by position across the sound; the spatial
     variation of the per-bin TWD/heading gives the gradient (seaward-vs-inshore
     shift), and the TIME derivative gives shift/front-passage timing.
  5. Output: TWD(t) series + a coarse spatial gradient field, as the proxy truth
     consumed by validation/benchmark_protocol.md.

CRITICAL -- current contamination:
  COG/SOG are over-ground. Converting boat motion to wind implicitly folds tidal
  current into the estimate. Either (a) restrict to slack +/- 1 h, or (b) subtract
  a tidal-current vector (harmonic prediction / NCOM-HYCOM) from SOG before
  inferring headings. Salem Sound channels run ~0.5-1 kt -- non-trivial vs a
  light sea breeze. Flag windows where |current| is large.

Deps: numpy, pandas (+ pyarrow/duckdb for the store). Reuses SailFrames polar/
interp conventions (STW not SOG).
"""
import argparse

BEAT_SOG = {"j80": (3.5, 6.5), "sonar": (2.5, 5.5)}   # kt gate per class (tune)
TACK_ANGLE_DEG = {"j80": 45.0, "sonar": 45.0}         # half-angle bow-to-wind


def load_tracks(source):
    """TODO: -> DataFrame[boat_id, time, lat, lon, sog, cog]."""
    raise NotImplementedError


def detect_upwind(df, boat_class):
    """TODO: flag beating fixes (bimodal COG + SOG gate)."""
    raise NotImplementedError


def estimate_twd(df_upwind):
    """TODO: port/starboard heading-pair midpoint -> fleet TWD(t)."""
    raise NotImplementedError


def cross_course_gradient(df_upwind):
    """TODO: spatial variation of per-bin TWD -> gradient + shift timing."""
    raise NotImplementedError


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="fleet track source (parquet/duckdb/path)")
    ap.add_argument("--class", dest="boat_class", default="j80", choices=list(BEAT_SOG))
    ap.add_argument("--slack-only", action="store_true",
                    help="restrict to slack water +/- 1 h (current mitigation)")
    args = ap.parse_args()
    print("SKELETON -- implement load_tracks/detect_upwind/estimate_twd/"
          "cross_course_gradient. Mind the current-contamination note in the docstring.")


if __name__ == "__main__":
    main()
