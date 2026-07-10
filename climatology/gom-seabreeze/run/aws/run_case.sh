#!/usr/bin/env bash
# run_case.sh -- run one gom-seabreeze forecast inside the DTC wps_wrf container.
#
# Drives the container the way its own scripts expect (verified against
# NCAR/container-dtc-nwp: run_wps.ksh / run_wrf.ksh):
#   mounts  /home/scripts/case  (namelists + set_env.ksh)
#           /data/geog          (WPS_GEOG)
#           /data/model_data/<case>  (driver GRIB for link_grib)
#           /home/wpsprd /home/wrfprd  (WPS/WRF work dirs -> outputs land on host)
#   exec    run_wps.ksh -> geogrid/ungrib/metgrid -> met_em in wpsprd
#           [HOST] build_coldest_sst.py + patch_met_em_sst.py on wpsprd/met_em*
#                  (wpsprd is bind-mounted, so the patch is seen by the container)
#           run_wrf.ksh -> real + mpirun wrf.exe -> wrfout_d03 in wrfprd
#   push    wrfout_d03* -> s3
#
# Image = dtcenter/wps_wrf:latest  (WRF/WPS 4.3, OpenMPI dmpar, amd64).
# NOTE: first run is a shakedown -- verify set_env.ksh vars against the DTC
# tutorial and that run_wrf.ksh links met_em from wpsprd (add a link if not).
set -euo pipefail

DATE="${1:?usage: run_case.sh YYYY-MM-DD [forecast|hindcast]}"
MODE="${2:-forecast}"
RUN_HOURS="${GOM_RUN_HOURS:-36}"
IMAGE="${GOM_IMAGE:-dtcenter/wps_wrf:latest}"
REPO="${GOM_REPO:-$HOME/sailframes/climatology/gom-seabreeze}"
WORK="${GOM_WORK:-$HOME/gom-work/$DATE}"
GEOG="${GOM_GEOG:-$HOME/WPS_GEOG}"
VENV="${GOM_VENV:-$HOME/gom-venv}"
S3="${GOM_S3:-s3://sailframes-data-prod/gom}"
NPROCS="${GOM_NPROCS:-$(nproc)}"; [ "$NPROCS" -gt 32 ] && NPROCS=32   # small domain: don't over-decompose
CASE=gom

mkdir -p "$WORK"/{case,model_data/$CASE,wpsprd,wrfprd}

echo "### 1. render namelists (geog_data_path -> container /data/geog)"
GOM_WPS="$WORK/case" GOM_WRFRUN="$WORK/case" GOM_WPSGEOG=/data/geog \
  "$VENV/bin/python" "$REPO/run/run_gom_seabreeze.py" --date "$DATE" --mode "$MODE" \
  --run-hours "$RUN_HOURS" --render-only

echo "### 2. set_env.ksh for the DTC run scripts"
cat > "$WORK/case/set_env.ksh" <<EOF
export WPS_VERSION=4.3
export WRF_VERSION=4.3
export input_data=$( [ "$MODE" = hindcast ] && echo ERA5 || echo GFS )
export case_name=$CASE
export num_procs=$NPROCS
EOF

echo "### 3. stage driver GRIB (GFS via NOMADS) -> model_data/$CASE"
if [ "$MODE" = forecast ]; then
  GOM_PYTHON="$VENV/bin/python" "$VENV/bin/python" - "$DATE" "$RUN_HOURS" "$WORK/model_data/$CASE" <<'PY'
import sys, urllib.request, pathlib
date, rh, out = sys.argv[1], int(sys.argv[2]), pathlib.Path(sys.argv[3])
ymd = date.replace("-", ""); base = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
for fh in range(0, rh + 1, 3):
    q = f"?dir=%2Fgfs.{ymd}%2F00%2Fatmos&file=gfs.t00z.pgrb2.0p25.f{fh:03d}&all_lev=on&all_var=on"
    dest = out / f"gfs.t00z.f{fh:03d}.grib2"
    if not dest.exists():
        print("  GFS f%03d" % fh); urllib.request.urlretrieve(base + q, dest)
PY
else
  echo "  hindcast: stage ERA5 GRIB into $WORK/model_data/$CASE yourself (cdsapi)"; [ -n "$(ls -A "$WORK/model_data/$CASE")" ] || { echo "  no ERA5 GRIB -> abort"; exit 2; }
fi

echo "### 4. start container with the DTC mounts"
docker rm -f gomwrf 2>/dev/null || true
docker run -d --name gomwrf \
  -v "$WORK/case":/home/scripts/case \
  -v "$GEOG":/data/geog \
  -v "$WORK/model_data":/data/model_data \
  -v "$WORK/wpsprd":/home/wpsprd \
  -v "$WORK/wrfprd":/home/wrfprd \
  "$IMAGE" sleep infinity

echo "### 5. WPS (geogrid/ungrib/metgrid) -> met_em in wpsprd"
docker exec gomwrf /home/scripts/common/run_wps.ksh
ls -la "$WORK/wpsprd/"met_em.d0*.nc

echo "### 6. SST lower boundary + inject into met_em (HOST venv; wpsprd is bind-mounted)"
"$VENV/bin/python" "$REPO/sst/build_coldest_sst.py" --date "$DATE" --outdir "$WORK/sst" --anchor
"$VENV/bin/python" "$REPO/sst/patch_met_em_sst.py" --met-dir "$WORK/wpsprd" --composite "$WORK/sst" --domains 1 2 3

echo "### 7. WRF (real + mpirun wrf.exe) -> wrfout_d03 in wrfprd"
docker exec gomwrf /home/scripts/common/run_wrf.ksh

echo "### 8. push wrfout_d03 -> S3"
aws s3 cp "$WORK/wrfprd/" "$S3/$DATE/$MODE/" --recursive --exclude "*" --include "wrfout_d0*"
echo "=== done: $S3/$DATE/$MODE/ ==="
