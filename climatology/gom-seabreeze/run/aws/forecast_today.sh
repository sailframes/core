#!/usr/bin/env bash
# forecast_today.sh -- DAY-OF operational HRRR-driven forecast (minimal first step of
# OPERATIONAL_DAYOF.md: retrospective -> forecast). Picks today's LATEST HRRR cycle that already
# has the needed forecast hours, runs the HRRR-driven WRF-SailFrames (mode=hrrr) forward, and
# (optionally) backfills the d02 1km field to a "today" dashboard source.
#
# This is the manual first step; schedule it (EventBridge ~07:00 ET) once trustworthy.
#   AWS_PROFILE=sailframes ./forecast_today.sh [YYYY-MM-DD]   (default: today UTC)
#
# NOTE: gated on the HRRR-driven ingest working end-to-end (wrfnat + Vtable.raphrrr, num_metgrid
# _levels=51) -- validate with one mode=hrrr run first (see run_case.sh HRRR staging).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DATE="${1:-$(date -u +%Y-%m-%d)}"; ymd=${DATE//-/}
RUN_HOURS="${GOM_RUN_HOURS:-18}"; last=$(printf '%02d' "$RUN_HOURS")
IMG="${GOM_IMAGE:-581790374840.dkr.ecr.us-east-1.amazonaws.com/sailframes-wrf:4.8}"

echo "### find latest HRRR cycle for $DATE with F${last} available"
CYCLE=""
for c in $(seq 23 -1 0); do
  cc=$(printf '%02d' "$c")
  if aws s3 ls --no-sign-request "s3://noaa-hrrr-bdp-pds/hrrr.${ymd}/conus/hrrr.t${cc}z.wrfnatf${last}.grib2" >/dev/null 2>&1; then
    CYCLE="$cc"; break
  fi
done
[ -z "$CYCLE" ] && { echo "FATAL: no HRRR cycle for $DATE has F${last} yet (too early? try a shorter GOM_RUN_HOURS)"; exit 1; }
echo "  -> using HRRR ${CYCLE}z cycle (F00..F${last})"

echo "### launch HRRR-driven day-of forecast"
GOM_IMAGE="$IMG" GOM_WRF_VER=4.8.0 GOM_WPS_VER=4.7.0 \
GOM_HRRR_CYCLE="$CYCLE" GOM_RUN_HOURS="$RUN_HOURS" GOM_GEOG_DETAIL=modis \
  bash "$HERE/launch.sh" "$DATE" hrrr

echo "### done. When the run finishes (gom/$DATE/hrrr/), backfill the 1km d02 to a 'today' source:"
echo "    run_backfill_les.sh style on gom/$DATE/hrrr --domain d02 -> climatology/wrf-today/"
echo "    then the dashboards read climatology/wrf-today/ as the DAY-OF wind source."
