#!/usr/bin/env bash
# run_backfill_both.sh -- ON-DEMAND box: backfill_wrf on BOTH the nudged and free-YSU runs into
# the web replay schema (grid.json + fields parquet) and publish to two bases under
# s3://sailframes-data-prod/climatology/ so the dashboard can toggle wrf-nudged vs wrf-free.
#   AWS_PROFILE=sailframes ./run_backfill_both.sh [YYYY-MM-DD]
set -euo pipefail
DATE="${1:-2026-07-04}"; REGION="${AWS_REGION:-us-east-1}"
S3="${GOM_S3:-s3://sailframes-data-prod/gom}"; WEB="s3://sailframes-data-prod/climatology"
PROFILE_NAME="${GOM_INSTANCE_PROFILE:-gom-seabreeze-ec2}"
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/.." && pwd)"
AMI=$(aws ssm get-parameter --region "$REGION" --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value --output text)

echo "### upload current code (backfill_wrf.py lives in climatology/, copy it into the tarball dir)"
cp "$ROOT/../backfill_wrf.py" "$ROOT/backfill_wrf.py"
tar czf /tmp/gom-seabreeze.tar.gz -C "$(dirname "$ROOT")" "$(basename "$ROOT")"
rm -f "$ROOT/backfill_wrf.py"
aws s3 cp /tmp/gom-seabreeze.tar.gz "$S3/code/gom-seabreeze.tar.gz"

UD=$(sed -e "s|@@DATE@@|$DATE|g" -e "s|@@S3@@|$S3|g" -e "s|@@WEB@@|$WEB|g" -e "s|@@REGION@@|$REGION|g" <<'USERDATA' | base64
#!/usr/bin/env bash
set -xeuo pipefail
exec > >(tee -a /var/log/bf.log) 2>&1
DATE="@@DATE@@"; S3="@@S3@@"; WEB="@@WEB@@"
dnf install -y python3.11 python3.11-pip tar gzip >/dev/null
python3.11 -m venv /root/venv; /root/venv/bin/pip -q install --upgrade pip
/root/venv/bin/pip -q install numpy scipy xarray netCDF4 pyarrow boto3
aws s3 cp "$S3/code/gom-seabreeze.tar.gz" /root/gom.tar.gz; tar xzf /root/gom.tar.gz -C /root
BF=$(find /root -name backfill_wrf.py | head -1)
set +e; RC=0
for pair in "nudged:wrf-nudged" "freerun-ysu:wrf-free"; do
  run="${pair%%:*}"; dest="${pair##*:}"
  /root/venv/bin/python "$BF" --date "$DATE" --s3 "$S3/$DATE/$run" --out-dir "/mnt/$dest" || RC=$?
  aws s3 cp "/mnt/$dest/" "$WEB/$dest/" --recursive || RC=$?
  echo "PUBLISHED $run -> $WEB/$dest/"
done
set -e
aws s3 cp /var/log/bf.log "$S3/$DATE/validation/backfill.log" || true
echo "$RC" | aws s3 cp - "$S3/$DATE/validation/backfill_done.txt" || true
IID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
aws ec2 terminate-instances --instance-ids "$IID" --region @@REGION@@ || shutdown -h now
USERDATA
)
IID=$(aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "${GOM_ITYPE:-c7a.xlarge}" \
  --iam-instance-profile "Name=$PROFILE_NAME" \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=100,VolumeType=gp3}' \
  --instance-initiated-shutdown-behavior terminate --user-data "$UD" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=gom-backfill-both}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "launched (on-demand) $IID -> $WEB/{wrf-nudged,wrf-free}/  (done: $S3/$DATE/validation/backfill_done.txt)"
