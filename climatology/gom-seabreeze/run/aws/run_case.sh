#!/usr/bin/env bash
# run_case.sh -- run one gom-seabreeze forecast inside the DTC wps_wrf container.
#
# Drives the container with DTC's own scripts (run_wps/run_real/run_wrf), which are
# NOT baked into the image -- they're mounted at /home/scripts/common. Layout:
#   /home/scripts/case   namelists + set_env.ksh
#   /home/scripts/common DTC *.ksh (downloaded from NCAR/container-dtc-nwp)
#   /data/geog           WPS_GEOG (fields under WPS_GEOG/)
#   /data/model_data/<c> driver GRIB (GFS from noaa-gfs-bdp-pds)
#   /home/wpsprd /home/wrfprd  WPS/WRF work dirs (outputs land on host)
# Flow: run_wps -> met_em(wpsprd) -> [HOST] build/patch SST on wpsprd/met_em ->
#       run_real (links wpsprd/met_em -> wrfinput/wrfbdy, cold SST baked in) ->
#       run_wrf -np N -> wrfout_d03 -> S3.
# Image = dtcenter/wps_wrf:latest (WRF/WPS 4.3, OpenMPI dmpar, amd64).
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
NPROCS="${GOM_NPROCS:-$(nproc)}"; [ "$NPROCS" -gt 32 ] && NPROCS=32
CASE=gom
DTC_RAW="https://raw.githubusercontent.com/NCAR/container-dtc-nwp/main/components/scripts/common"

mkdir -p "$WORK"/scripts/{case,common} "$WORK"/{model_data/$CASE,wpsprd,wrfprd,sst}

echo "### 1. render namelists (geog_data_path -> container mount)"
GEOGROOT=/data/geog; [ -d "$GEOG/WPS_GEOG" ] && GEOGROOT=/data/geog/WPS_GEOG
GOM_WPS="$WORK/scripts/case" GOM_WRFRUN="$WORK/scripts/case" GOM_WPSGEOG="$GEOGROOT" \
  "$VENV/bin/python" "$REPO/run/run_gom_seabreeze.py" --date "$DATE" --mode "$MODE" \
  --run-hours "$RUN_HOURS" --render-only

echo "### 2. set_env.ksh + DTC run scripts"
cat > "$WORK/scripts/case/set_env.ksh" <<EOF
export WPS_VERSION=4.3
export WRF_VERSION=4.3
export input_data=$( [ "$MODE" = hindcast ] && echo ERA5 || echo GFS )
export case_name=$CASE
export file_date=${DATE}_00
export num_procs=$NPROCS
EOF
for k in run_wps.ksh run_real.ksh run_wrf.ksh; do
  curl -sSL "$DTC_RAW/$k" -o "$WORK/scripts/common/$k"; chmod +x "$WORK/scripts/common/$k"
done
# container OpenMPI (old) blocks running as root and only honors the flag, not the env var
sed -i 's/mpirun -np/mpirun --allow-run-as-root -np/' "$WORK/scripts/common/run_wrf.ksh"
# WRF 4.3's namelist reader rejects in-group comments (WPS tolerates them) -> strip
for f in namelist.wps namelist.input; do
  sed -i -e 's/!.*//' -e '/^[[:space:]]*$/d' "$WORK/scripts/case/$f"
done
# TEMP: WRF 4.3's registry rejects an auxinput4 var in the sst_update block, and the
# coldest-pixel SST is injected into the static met_em (not via wrflowinp) anyway ->
# drop the block. Restore once the offending 4.3 var is identified. See README/memory.
sed -i -e '/sst_update/d' -e '/io_form_auxinput4/d' -e '/auxinput4_/d' "$WORK/scripts/case/namelist.input"

echo "### 3. stage GFS 0.25 from the AWS Open Data archive (public, in-region)"
if [ "$MODE" = forecast ]; then
  ymd=${DATE//-/}
  for fh in $(seq 0 3 "$RUN_HOURS"); do
    fh3=$(printf '%03d' "$fh"); dest="$WORK/model_data/$CASE/gfs.t00z.f${fh3}.grib2"
    [ -s "$dest" ] || { echo "  GFS f${fh3}"; aws s3 cp --no-sign-request --quiet \
      "s3://noaa-gfs-bdp-pds/gfs.${ymd}/00/atmos/gfs.t00z.pgrb2.0p25.f${fh3}" "$dest"; }
  done
else
  echo "  hindcast: stage ERA5 GRIB into $WORK/model_data/$CASE (cdsapi)"; [ -n "$(ls -A "$WORK/model_data/$CASE")" ] || { echo "  no ERA5 -> abort"; exit 2; }
fi

echo "### 4. (re)start container with the DTC mounts"
docker rm -f gomwrf 2>/dev/null || true
docker run -d --name gomwrf \
  -v "$WORK/scripts":/home/scripts \
  -v "$GEOG":/data/geog \
  -v "$WORK/model_data":/data/model_data \
  -v "$WORK/wpsprd":/home/wpsprd \
  -v "$WORK/wrfprd":/home/wrfprd \
  "$IMAGE" sleep infinity

echo "### 5. WPS (geogrid/ungrib/metgrid) -> met_em in wpsprd"
# run_wps links only *.exe into wpsprd; geogrid/metgrid look for GEOGRID.TBL /
# METGRID.TBL under ./geogrid ./metgrid -> symlink those table dirs in first
docker exec gomwrf bash -lc 'cd /home/wpsprd && ln -sf /comsoftware/wrf/WPS-4.3/geogrid . && ln -sf /comsoftware/wrf/WPS-4.3/metgrid .'
docker exec gomwrf /home/scripts/common/run_wps.ksh
ls "$WORK/wpsprd/"met_em.d0*.nc

echo "### 6. SST lower boundary + inject into met_em (HOST venv; wpsprd bind-mounted)"
# non-fatal: ACSPO NRT ERDDAP lacks historical dates -> if the composite fails, run on
# the raw driver SST (still a valid run; the coldest-pixel value needs an archive dataset)
if "$VENV/bin/python" "$REPO/sst/build_coldest_sst.py" --date "$DATE" --outdir "$WORK/sst" --anchor \
   && "$VENV/bin/python" "$REPO/sst/patch_met_em_sst.py" --met-dir "$WORK/wpsprd" --composite "$WORK/sst" --domains 1 2 3; then
  echo "  coldest-pixel SST injected"
else
  echo "  WARN: SST composite failed -> proceeding with raw driver SST in met_em"
fi

echo "### 7. real.exe (links patched wpsprd/met_em -> wrfinput/wrfbdy)"
docker exec gomwrf /home/scripts/common/run_real.ksh
ls "$WORK/wrfprd/"wrfinput_d01 "$WORK/wrfprd/"wrfbdy_d01

echo "### 8. wrf.exe (mpirun -np $NPROCS) -> wrfout_d03"
# container execs as root -> OpenMPI needs the run-as-root override
docker exec -e OMPI_ALLOW_RUN_AS_ROOT=1 -e OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1 \
  gomwrf /home/scripts/common/run_wrf.ksh -np "$NPROCS"

echo "### 9. push wrfout -> S3"
aws s3 cp --quiet "$WORK/wrfprd/" "$S3/$DATE/$MODE/" --recursive --exclude "*" --include "wrfout_d0*"
echo "=== done: $S3/$DATE/$MODE/ ==="
