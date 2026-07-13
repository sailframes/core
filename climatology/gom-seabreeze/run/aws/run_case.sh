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
# In-container WRF/WPS build dir versions. dtcenter/wps_wrf:latest = 4.3/4.3; our
# sailframes-wrf:4.8 image = WRF-4.8.0 + WPS-4.7.0 (+ obsgrid.exe). The DTC run_*.ksh
# scripts build paths as WRF-${WRF_VERSION} / WPS-${WPS_VERSION}, so switching images is
# just: GOM_IMAGE=<ecr>:4.8 GOM_WRF_VER=4.8.0 GOM_WPS_VER=4.7.0.
WRF_VER="${GOM_WRF_VER:-4.3}"
WPS_VER="${GOM_WPS_VER:-4.3}"
REPO="${GOM_REPO:-$HOME/sailframes/climatology/gom-seabreeze}"
WORK="${GOM_WORK:-$HOME/gom-work/$DATE}"
GEOG="${GOM_GEOG:-$HOME/WPS_GEOG}"
VENV="${GOM_VENV:-$HOME/gom-venv}"
S3="${GOM_S3:-s3://sailframes-data-prod/gom}"
NPROCS="${GOM_NPROCS:-$(nproc)}"; [ "$NPROCS" -gt 32 ] && NPROCS=32
GEOG_DETAIL="${GOM_GEOG_DETAIL:-modis}"       # modis (30s) | nlcd (9s ~250m + urban)
GEOGRID_ONLY="${GOM_GEOGRID_ONLY:-0}"         # 1 = geogrid + landmask QC plot only, then exit (cheap)
NLCD_URL="${GOM_NLCD_URL:-https://www2.mmm.ucar.edu/wrf/src/wps_files/nlcd2011_ll_9s.tar.bz2}"
CASE=gom
DTC_RAW="https://raw.githubusercontent.com/NCAR/container-dtc-nwp/main/components/scripts/common"

mkdir -p "$WORK"/scripts/{case,common} "$WORK"/{model_data/$CASE,wpsprd,wrfprd,sst,geoqc}

echo "### 1. render namelists (geog_data_path -> container mount, geog_detail=$GEOG_DETAIL)"
GEOGROOT=/data/geog; GEOGROOT_HOST="$GEOG"
[ -d "$GEOG/WPS_GEOG" ] && { GEOGROOT=/data/geog/WPS_GEOG; GEOGROOT_HOST="$GEOG/WPS_GEOG"; }
GOM_WPS="$WORK/scripts/case" GOM_WRFRUN="$WORK/scripts/case" GOM_WPSGEOG="$GEOGROOT" \
  "$VENV/bin/python" "$REPO/run/run_gom_seabreeze.py" --date "$DATE" --mode "$MODE" \
  --run-hours "$RUN_HOURS" --geog-detail "$GEOG_DETAIL" --render-only

# WRF needs each decomposed patch >= 10 cells; the SMALLEST nest binds. WRF's auto
# decomposition of an awkward rank count (32 -> 4x8) can drop d03's y-patch below 10
# (race-focused d03 is only 76 cells N-S). Force a PERFECT-SQUARE rank count k*k with
# k <= (min d03 dim - 1)/10 so WRF picks k x k and every patch stays >= 10 cells.
d3we=$(grep -E '^\s*e_we\s*=' "$WORK/scripts/case/namelist.input" | grep -oE '[0-9]+' | sed -n '3p')
d3sn=$(grep -E '^\s*e_sn\s*=' "$WORK/scripts/case/namelist.input" | grep -oE '[0-9]+' | sed -n '3p')
if [ -n "$d3we" ] && [ -n "$d3sn" ]; then
  d3min=$(( d3we < d3sn ? d3we : d3sn )); kmax=$(( (d3min - 1) / 10 )); k=1
  for kk in $(seq 1 "$kmax"); do [ $((kk*kk)) -le "$NPROCS" ] && k=$kk; done
  new=$(( k * k )); [ "$new" -lt 1 ] && new=1
  [ "$new" -ne "$NPROCS" ] && echo "  MPI ranks $NPROCS -> $new (${k}x${k}) for d03 ${d3we}x${d3sn} (patch>=10)"
  NPROCS=$new
fi

echo "### 1b. NLCD static geog (9s land-use / land-water mask), only when detail=nlcd"
if [ "$GEOG_DETAIL" = nlcd ] && [ ! -d "$GEOGROOT_HOST/nlcd2011_ll_9s" ]; then
  if aws s3 ls "$S3/geog/nlcd2011_ll_9s.tar.gz" >/dev/null 2>&1; then
    echo "  NLCD: from S3 cache"; aws s3 cp "$S3/geog/nlcd2011_ll_9s.tar.gz" - | tar xz -C "$GEOGROOT_HOST"
  else
    echo "  NLCD: fetch from UCAR + cache to S3 (~1 GB)"
    curl -SL "$NLCD_URL" | tar xj -C "$GEOGROOT_HOST"
    tar cz -C "$GEOGROOT_HOST" nlcd2011_ll_9s | aws s3 cp - "$S3/geog/nlcd2011_ll_9s.tar.gz"
  fi
  [ -d "$GEOGROOT_HOST/nlcd2011_ll_9s" ] || { echo "  FATAL: NLCD extract produced no nlcd2011_ll_9s/ dir"; exit 3; }
fi

echo "### 2. set_env.ksh + DTC run scripts"
cat > "$WORK/scripts/case/set_env.ksh" <<EOF
export WPS_VERSION=$WPS_VER
export WRF_VERSION=$WRF_VER
export input_data=$(case "$MODE" in hindcast) echo ERA5;; hrrr) echo raphrrr;; *) echo GFS;; esac)
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
if [ "$GEOGRID_ONLY" = 1 ]; then
  echo "  geogrid-only: skipping driver GRIB (geogrid needs no meteorology)"
elif [ "$MODE" = hrrr ]; then
  # HRRR-driven: hourly F00 ANALYSES (obs-assimilated each hour) as ICs/LBCs.
  # valid time = DATE 00Z + hh; pull hrrr.t<HH>z.wrfprsf00 from that day's dir.
  for hh in $(seq 0 "$RUN_HOURS"); do
    vt=$(date -u -d "$DATE 00:00 UTC +${hh} hours" +%Y%m%d_%H)
    vymd=${vt%_*}; vhh=${vt#*_}; dest="$WORK/model_data/$CASE/hrrr.${vt}.f00.grib2"
    [ -s "$dest" ] || { echo "  HRRR analysis +${hh}h (${vymd} t${vhh}z)"; aws s3 cp --no-sign-request --quiet \
      "s3://noaa-hrrr-bdp-pds/hrrr.${vymd}/conus/hrrr.t${vhh}z.wrfprsf00.grib2" "$dest"; }
  done
elif [ "$MODE" = forecast ]; then
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

echo "### 4b. discriminator: image must support NLCD before we lean on it"
if [ "$GEOG_DETAIL" = nlcd ]; then
  # geogrid needs the nlcd2011_9s resolution entry in GEOGRID.TBL...
  docker exec gomwrf bash -lc "grep -qs nlcd2011_9s /comsoftware/wrf/WPS-${WPS_VER}/geogrid/GEOGRID.TBL*" \
    || { echo "FATAL: GEOGRID.TBL has no nlcd2011_9s entry in this image -> use GOM_GEOG_DETAIL=modis, or add the entry + LANDMASK-surgery fallback"; exit 3; }
  # ...and real/wrf (RUC LSM) needs an NLCD40 section in VEGPARM.TBL (skip check for geogrid-only)
  if [ "$GEOGRID_ONLY" != 1 ]; then
    docker exec gomwrf bash -lc "grep -qsi '^NLCD40' /comsoftware/wrf/WRF-${WRF_VER}/run/VEGPARM.TBL" \
      || { echo "FATAL: VEGPARM.TBL has no NLCD40 section -> real.exe/RUC would abort. Use GOM_GEOG_DETAIL=modis or the post-geogrid LANDMASK-surgery fallback (see wrf/README geography)"; exit 3; }
  fi
  echo "  NLCD support confirmed in image tables"
fi

echo "### 4c. patch VEGPARM.TBL NLCD40 LCZ block (WRF 4.3 bug fix, nlcd only)"
# WRF 4.3 ships the NLCD40 section with only LCZ_1..LCZ_3 (values 24/26/99) while the
# Noah reader expects LCZ_1..LCZ_11 (like every other section) -> it reads past LCZ_3
# into the next section's "Vegetation Parameters" header -> Fortran "Bad integer for
# item 1 in list input" (module_sf_noahdrv.f90). Fixed upstream in WRF master by
# extending the NLCD40 LCZ block to 11 entries (51..61). Apply that here at runtime
# (no recompile): pull the container's table, extend ONLY the NLCD40 LCZ block, copy back.
if [ "$GEOG_DETAIL" = nlcd ] && [ "$GEOGRID_ONLY" != 1 ]; then
  VEG=/comsoftware/wrf/WRF-${WRF_VER}/run/VEGPARM.TBL
  n11=$(docker exec gomwrf grep -c '^LCZ_11' "$VEG" || echo 0)
  if [ "${n11:-0}" -lt 3 ]; then          # <3 => NLCD40 section is the truncated one
    docker exec gomwrf cat "$VEG" > "$WORK/VEGPARM.TBL.orig"
    awk '
      /^NLCD40/ { innlcd=1 }
      innlcd && /^Vegetation Parameters/ { innlcd=0 }
      innlcd && $0=="LCZ_1" && !done {
        printf "LCZ_1\n51\nLCZ_2\n52\nLCZ_3\n53\nLCZ_4\n54\nLCZ_5\n55\nLCZ_6\n56\nLCZ_7\n57\nLCZ_8\n58\nLCZ_9\n59\nLCZ_10\n60\nLCZ_11\n61\n"
        done=1; for (i=0;i<5;i++) getline; next    # drop old 24, LCZ_2, 26, LCZ_3, 99
      }
      { print }
    ' "$WORK/VEGPARM.TBL.orig" > "$WORK/VEGPARM.TBL.fixed"
    if [ "$(grep -c '^LCZ_11' "$WORK/VEGPARM.TBL.fixed")" -eq 3 ]; then
      docker cp "$WORK/VEGPARM.TBL.fixed" gomwrf:"$VEG"
      echo "  VEGPARM.TBL NLCD40 LCZ block extended 3 -> 11 entries"
    else
      echo "  WARN: VEGPARM.TBL patch produced unexpected LCZ_11 count -> left original (wrf.exe may still FATAL)"
    fi
  else
    echo "  VEGPARM.TBL NLCD40 already has full LCZ block -> no patch needed"
  fi
fi

echo "### 5. WPS (geogrid/ungrib/metgrid) -> met_em in wpsprd"
# run_wps links only *.exe into wpsprd; geogrid/metgrid look for GEOGRID.TBL /
# METGRID.TBL under ./geogrid ./metgrid -> symlink those table dirs in first
docker exec gomwrf bash -lc "cd /home/wpsprd && ln -sf /comsoftware/wrf/WPS-${WPS_VER}/geogrid . && ln -sf /comsoftware/wrf/WPS-${WPS_VER}/metgrid ."

if [ "$GEOGRID_ONLY" = 1 ]; then
  echo "### 5-only. geogrid.exe -> geo_em + landmask QC (no GFS/real/wrf)"
  # render a detail's namelist.wps into scripts/<sub>, geogrid into wpsprd/<sub>.
  # geogrid.exe reads namelist.wps from cwd + writes geo_em there.
  geogrid_detail() {  # $1=detail  $2=subdir
    local det="$1" sub="$2"
    mkdir -p "$WORK/scripts/$sub" "$WORK/wpsprd/$sub"
    GOM_WPS="$WORK/scripts/$sub" GOM_WRFRUN="$WORK/scripts/$sub" GOM_WPSGEOG="$GEOGROOT" \
      "$VENV/bin/python" "$REPO/run/run_gom_seabreeze.py" --date "$DATE" --mode "$MODE" \
      --run-hours "$RUN_HOURS" --geog-detail "$det" --render-only
    sed -i -e 's/!.*//' -e '/^[[:space:]]*$/d' "$WORK/scripts/$sub/namelist.wps"
    docker exec gomwrf bash -lc "cd /home/wpsprd/$sub && ln -sf /comsoftware/wrf/WPS-${WPS_VER}/geogrid.exe . && ln -sf /comsoftware/wrf/WPS-${WPS_VER}/geogrid . && cp /home/scripts/$sub/namelist.wps . && ./geogrid.exe"
    ls "$WORK/wpsprd/$sub/"geo_em.d0*.nc
  }
  geogrid_detail "$GEOG_DETAIL" primary
  # A/B against the MODIS baseline -- the compare IS the "more detailed geography" proof
  CMP=""
  if [ "$GEOG_DETAIL" = nlcd ]; then geogrid_detail modis baseline; CMP="--compare $WORK/wpsprd/baseline"; fi
  # d03 = race area (default extent); d01/d02 = full domain (confirm the Gulf of Maine
  # stays WATER -- the risky part of the all-domains NLCD '+default' ocean remap)
  for dom in 1 2 3; do
    ext=""; [ "$dom" != 3 ] && ext="--full-extent"
    for fld in LANDMASK LU_INDEX; do
      "$VENV/bin/python" "$REPO/validation/plot_geo_landmask.py" --geo-dir "$WORK/wpsprd/primary" \
        $CMP --domain "$dom" --field "$fld" $ext -o "$WORK/geoqc/geo_d0${dom}_${fld}.png" || true
    done
  done
  # geogrid leaves broken symlinks (geogrid.exe / geogrid -> container paths) in the
  # work dirs; `aws s3 cp --recursive` returns exit 2 skipping them, which under set -e
  # would abort BEFORE the PNGs upload. Remove them, PNGs first (the deliverable), and
  # make the copies non-fatal.
  find "$WORK/wpsprd" -maxdepth 2 -xtype l -delete 2>/dev/null || true
  aws s3 cp "$WORK/geoqc/" "$S3/$DATE/geoqc/$GEOG_DETAIL/" --recursive --exclude "*" --include "*.png" || true
  aws s3 cp "$WORK/wpsprd/primary/"  "$S3/$DATE/geoqc/$GEOG_DETAIL/"      --recursive --exclude "*" --include "geo_em.d0*.nc" || true
  [ -d "$WORK/wpsprd/baseline" ] && aws s3 cp "$WORK/wpsprd/baseline/" "$S3/$DATE/geoqc/modis/" --recursive --exclude "*" --include "geo_em.d0*.nc" || true
  echo "=== geogrid-only done: $S3/$DATE/geoqc/$GEOG_DETAIL/ (geo_em + d01/d02/d03 PNGs) ==="
  echo "    CHECK: d01/d02 LANDMASK ocean still water; d03 coast sharper than MODIS baseline"
  exit 0
fi

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

# On real/wrf failure the real diagnosis is in rsl.error.0000 (+ run_*.log), which
# run_*.ksh only echoes the MPI_ABORT wrapper of -> capture them to S3 before the
# box self-terminates. Without this a failure is undiagnosable post-mortem.
push_debug() {  # $1 = stage label
  echo "  $1 FAILED -> uploading rsl.* + logs to $S3/$DATE/$MODE/debug/ for diagnosis"
  aws s3 cp "$WORK/wrfprd/" "$S3/$DATE/$MODE/debug/" --recursive --exclude "*" \
    --include "rsl.error.*" --include "rsl.out.0000" --include "run_real.log" \
    --include "run_wrf.log" --include "namelist.input" || true
}

echo "### 7. real.exe (links patched wpsprd/met_em -> wrfinput/wrfbdy)"
if ! docker exec gomwrf /home/scripts/common/run_real.ksh; then push_debug "real.exe"; exit 44; fi
ls "$WORK/wrfprd/"wrfinput_d01 "$WORK/wrfprd/"wrfbdy_d01

echo "### 8. wrf.exe (mpirun -np $NPROCS) -> wrfout_d03"
# container execs as root -> OpenMPI needs the run-as-root override
if ! docker exec -e OMPI_ALLOW_RUN_AS_ROOT=1 -e OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1 \
     gomwrf /home/scripts/common/run_wrf.ksh -np "$NPROCS"; then push_debug "wrf.exe"; exit 45; fi

echo "### 9. push wrfout -> S3"
# aws s3 cp --recursive can return 2 on a transient skip even when every file uploaded
# (seen on the :4.8 validation: rc=2 yet all 219 frames present). Don't let that spurious
# code kill the run under set -e; gate success on the actual d03 object count instead.
aws s3 cp --quiet "$WORK/wrfprd/" "$S3/$DATE/$MODE/" --recursive --exclude "*" --include "wrfout_d0*" \
  || echo "  WARN: aws s3 cp exited non-zero (transient); verifying by count"
localn=$(ls "$WORK/wrfprd/"wrfout_d03_* 2>/dev/null | wc -l | tr -d ' ')
s3n=$(aws s3 ls "$S3/$DATE/$MODE/" | grep -c wrfout_d03 || true)
echo "  wrfout_d03: local=$localn  s3=$s3n"
{ [ "${localn:-0}" -gt 0 ] && [ "${s3n:-0}" -ge "$localn" ]; } \
  || { echo "FATAL: wrfout_d03 upload incomplete (s3=$s3n < local=$localn)"; exit 9; }
echo "=== done: $S3/$DATE/$MODE/ ==="
