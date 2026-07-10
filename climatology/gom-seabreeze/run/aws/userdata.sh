#!/usr/bin/env bash
# userdata.sh -- EC2 bootstrap for a gom-seabreeze WRF run (Amazon Linux 2023, amd64).
# Substituted by launch.sh: @@DATE@@ @@MODE@@ @@RUN_HOURS@@ @@S3@@ @@TERMINATE@@.
# Logs stream to /var/log/gom-userdata.log and are pushed to S3 at the end.
set -xeuo pipefail
exec > >(tee -a /var/log/gom-userdata.log) 2>&1

DATE="@@DATE@@"; MODE="@@MODE@@"; RUN_HOURS="@@RUN_HOURS@@"; S3="@@S3@@"; TERMINATE="@@TERMINATE@@"
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

echo "### docker image (WRF/WPS 4.3)"
docker pull dtcenter/wps_wrf:latest

echo "### run the case"
export GOM_REPO="$CODE" GOM_GEOG="$GEOG" GOM_VENV="$VENV" GOM_S3="$S3" GOM_RUN_HOURS="$RUN_HOURS"
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
