#!/usr/bin/env bash
# run_backfill_les.sh -- backfill the LES d05 (111m) wrfout into the web replay schema so the
# dashboard can show the resolved-gust field as a "WRF LES 111m" wind source. Publishes to
# gom/<date>/web/wrf-les/ (role-writable); a local step copies it to climatology/wrf-les/.
#   AWS_PROFILE=sailframes ./run_backfill_les.sh [YYYY-MM-DD]
set -euo pipefail
DATE="${1:-2026-07-04}"; REGION="${AWS_REGION:-us-east-1}"
S3="${GOM_S3:-s3://sailframes-data-prod/gom}"; PROFILE_NAME="${GOM_INSTANCE_PROFILE:-gom-seabreeze-ec2}"
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/.." && pwd)"
AMI=$(aws ssm get-parameter --region "$REGION" --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value --output text)
echo "### upload code (backfill_wrf.py lives in climatology/, copy into the tarball dir)"
cp "$ROOT/../backfill_wrf.py" "$ROOT/backfill_wrf.py"; tar czf /tmp/gom-seabreeze.tar.gz -C "$(dirname "$ROOT")" "$(basename "$ROOT")"; rm -f "$ROOT/backfill_wrf.py"
aws s3 cp /tmp/gom-seabreeze.tar.gz "$S3/code/gom-seabreeze.tar.gz"

UD=$(sed -e "s|@@DATE@@|$DATE|g" -e "s|@@S3@@|$S3|g" -e "s|@@REGION@@|$REGION|g" <<'USERDATA' | base64
#!/usr/bin/env bash
set -xeuo pipefail
exec > >(tee -a /var/log/bfles.log) 2>&1
DATE="@@DATE@@"; S3="@@S3@@"
dnf install -y python3.11 python3.11-pip tar gzip >/dev/null
python3.11 -m venv /root/venv; /root/venv/bin/pip -q install --upgrade pip
/root/venv/bin/pip -q install numpy scipy xarray netCDF4 pyarrow boto3
aws s3 cp "$S3/code/gom-seabreeze.tar.gz" /root/gom.tar.gz; tar xzf /root/gom.tar.gz -C /root
BF=$(find /root -name backfill_wrf.py | head -1)
set +e; RC=0
/root/venv/bin/python "$BF" --date "$DATE" --s3 "$S3/$DATE/forecast" --domain d05 --out-dir /mnt/wrf-les || RC=$?
aws s3 cp "/mnt/wrf-les/" "$S3/$DATE/web/wrf-les/" --recursive || RC=$?
set -e
echo "PUBLISHED d05 -> $S3/$DATE/web/wrf-les/"
aws s3 cp /var/log/bfles.log "$S3/$DATE/validation/backfill_les.log" || true
echo "$RC" | aws s3 cp - "$S3/$DATE/validation/backfill_les_done.txt" || true
IID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
aws ec2 terminate-instances --instance-ids "$IID" --region @@REGION@@ || shutdown -h now
USERDATA
)
IID=$(aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "${GOM_ITYPE:-c7a.xlarge}" \
  --iam-instance-profile "Name=$PROFILE_NAME" \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=100,VolumeType=gp3}' \
  --instance-initiated-shutdown-behavior terminate --user-data "$UD" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=gom-backfill-les}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "launched (on-demand) $IID -> $S3/$DATE/web/wrf-les/  (done: $S3/$DATE/validation/backfill_les_done.txt)"
