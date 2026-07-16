#!/usr/bin/env bash
# userdata.sh -- EC2 bootstrap for a gom-seabreeze WRF run (Amazon Linux 2023, amd64).
# Substituted by launch.sh: @@DATE@@ @@MODE@@ @@RUN_HOURS@@ @@S3@@ @@TERMINATE@@.
# Logs stream to /var/log/gom-userdata.log and are pushed to S3 at the end.
set -xeuo pipefail
exec > >(tee -a /var/log/gom-userdata.log) 2>&1

DATE="@@DATE@@"; MODE="@@MODE@@"; RUN_HOURS="@@RUN_HOURS@@"; S3="@@S3@@"; TERMINATE="@@TERMINATE@@"
GEOG_DETAIL="@@GEOG_DETAIL@@"; GEOGRID_ONLY="@@GEOGRID_ONLY@@"
IMAGE="@@IMAGE@@"; WRF_VER="@@WRF_VER@@"; WPS_VER="@@WPS_VER@@"
OBS_NUDGE="@@OBS_NUDGE@@"; OBSGRID_ONLY="@@OBSGRID_ONLY@@"; OBS_EXCLUDE="@@OBS_EXCLUDE@@"
OBS_NUDGE_DOMS="@@OBS_NUDGE_DOMS@@"
LES="@@LES@@"; LES_D04_HOUR="@@LES_D04_HOUR@@"; LES_D05_HOUR="@@LES_D05_HOUR@@"
START_HOUR="@@START_HOUR@@"; HRRR_CYCLE="@@HRRR_CYCLE@@"; HRRR_CYCLE_DATE="@@HRRR_CYCLE_DATE@@"; HRRR_START_FH="@@HRRR_START_FH@@"
export HOME=/root
GEOG=/mnt/WPS_GEOG; VENV=/root/gom-venv; CODE=/root/gom-seabreeze

dnf install -y docker git python3.11 python3.11-pip tar gzip >/dev/null
systemctl enable --now docker

echo "### code: pull the gom-seabreeze dir tarball from S3 (uploaded by launch.sh)"
aws s3 cp "$S3/code/gom-seabreeze.tar.gz" /root/gom.tar.gz
mkdir -p "$CODE" && tar xzf /root/gom.tar.gz -C /root

echo "### python venv for the SST scripts"
python3.11 -m venv "$VENV"
"$VENV/bin/pip" -q install --upgrade pip
"$VENV/bin/pip" -q install -r "$CODE/requirements.txt"

echo "### WPS_GEOG: reuse the S3 cache if present, else fetch from UCAR + cache it"
mkdir -p "$GEOG"
if aws s3 ls "$S3/geog/WPS_GEOG.tar.gz" >/dev/null 2>&1; then
  aws s3 cp "$S3/geog/WPS_GEOG.tar.gz" - | tar xz -C "$GEOG"
else
  # WPS high-res mandatory geog (~2.8 GB gz -> ~10 GB); one-time, then cached to S3.
  curl -SL https://www2.mmm.ucar.edu/wrf/src/wps_files/geog_high_res_mandatory.tar.gz | tar xz -C "$GEOG"
  tar cz -C "$GEOG" . | aws s3 cp - "$S3/geog/WPS_GEOG.tar.gz"
fi
# geog fields may sit in a subdir (e.g. $GEOG/WPS_GEOG). If geogrid can't find them,
# the shakedown adjusts geog_data_path (mount stays /data/geog).
ls "$GEOG" | head

echo "### docker image ($IMAGE  wrf=$WRF_VER wps=$WPS_VER)"
case "$IMAGE" in
  *.dkr.ecr.*.amazonaws.com/*)   # private ECR ref -> authenticate first
    REG="${IMAGE%%/*}"
    aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "$REG"
    ;;
esac
docker pull "$IMAGE"

echo "### docker image tar bzip2 for optional NLCD geog"
dnf install -y bzip2 >/dev/null 2>&1 || true

echo "### run the case"
export GOM_REPO="$CODE" GOM_GEOG="$GEOG" GOM_VENV="$VENV" GOM_S3="$S3" GOM_RUN_HOURS="$RUN_HOURS"
export GOM_GEOG_DETAIL="$GEOG_DETAIL" GOM_GEOGRID_ONLY="$GEOGRID_ONLY"
export GOM_IMAGE="$IMAGE" GOM_WRF_VER="$WRF_VER" GOM_WPS_VER="$WPS_VER"
export GOM_OBS_NUDGE="$OBS_NUDGE" GOM_OBSGRID_ONLY="$OBSGRID_ONLY" GOM_OBS_EXCLUDE="$OBS_EXCLUDE"
export GOM_OBS_NUDGE_DOMS="$OBS_NUDGE_DOMS"
export GOM_LES="$LES" GOM_LES_D04_HOUR="$LES_D04_HOUR" GOM_LES_D05_HOUR="$LES_D05_HOUR"
# F-offset HRRR init (empty GOM_HRRR_CYCLE -> run_case.sh analysis branch; set -> forecast/F-offset)
export GOM_START_HOUR="$START_HOUR" GOM_HRRR_CYCLE="$HRRR_CYCLE" GOM_HRRR_CYCLE_DATE="$HRRR_CYCLE_DATE" GOM_HRRR_START_FH="$HRRR_START_FH"
set +e
bash "$CODE/run/aws/run_case.sh" "$DATE" "$MODE"; RC=$?
set -e

echo "### push logs (rc=$RC)"
aws s3 cp /var/log/gom-userdata.log "$S3/$DATE/$MODE/userdata.log" || true
echo "$RC" | aws s3 cp - "$S3/$DATE/$MODE/exit_code.txt" || true

if [ "$TERMINATE" = "1" ]; then
  IID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
  aws ec2 terminate-instances --instance-ids "$IID" --region us-east-1 || shutdown -h now
fi
