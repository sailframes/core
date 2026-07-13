#!/usr/bin/env bash
# run_yellow_zone.sh -- ON-DEMAND box (bypasses the spot quota held by a running WRF job) that
# runs yellow_zone_eval.py across the nudged + free-YSU runs and uploads the maps + fill table.
#   AWS_PROFILE=sailframes ./run_yellow_zone.sh [YYYY-MM-DD]
set -euo pipefail
DATE="${1:-2026-07-04}"; REGION="${AWS_REGION:-us-east-1}"
S3="${GOM_S3:-s3://sailframes-data-prod/gom}"; PROFILE_NAME="${GOM_INSTANCE_PROFILE:-gom-seabreeze-ec2}"
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/.." && pwd)"
AMI=$(aws ssm get-parameter --region "$REGION" --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value --output text)

echo "### upload current code (has yellow_zone_eval.py)"
tar czf /tmp/gom-seabreeze.tar.gz -C "$(dirname "$ROOT")" "$(basename "$ROOT")"
aws s3 cp /tmp/gom-seabreeze.tar.gz "$S3/code/gom-seabreeze.tar.gz"

UD=$(sed -e "s|@@DATE@@|$DATE|g" -e "s|@@S3@@|$S3|g" -e "s|@@REGION@@|$REGION|g" <<'USERDATA' | base64
#!/usr/bin/env bash
set -xeuo pipefail
exec > >(tee -a /var/log/yz.log) 2>&1
DATE="@@DATE@@"; S3="@@S3@@"
dnf install -y python3.11 python3.11-pip tar gzip >/dev/null
python3.11 -m venv /root/venv; /root/venv/bin/pip -q install --upgrade pip
/root/venv/bin/pip -q install numpy xarray netCDF4 matplotlib
aws s3 cp "$S3/code/gom-seabreeze.tar.gz" /root/gom.tar.gz; tar xzf /root/gom.tar.gz -C /root
SC=$(find /root -name yellow_zone_eval.py | head -1)
set +e
/root/venv/bin/python "$SC" \
  --run "nudged=$S3/$DATE/nudged" --run "free-YSU=$S3/$DATE/freerun-ysu" \
  --scratch /mnt/yz --outdir /root/yz > /root/yz_result.txt 2>&1
RC=$?
set -e
cat /root/yz_result.txt
aws s3 cp /root/yz_result.txt "$S3/$DATE/validation/yellow_zone_fill.txt" || true
aws s3 cp /root/yz/yellow_zone_maps.png "$S3/$DATE/validation/yellow_zone_maps.png" || true
aws s3 cp /var/log/yz.log "$S3/$DATE/validation/yz.log" || true
echo "$RC" | aws s3 cp - "$S3/$DATE/validation/yz_done.txt" || true
IID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
aws ec2 terminate-instances --instance-ids "$IID" --region @@REGION@@ || shutdown -h now
USERDATA
)
# mount instance store / extra gp3 for scratch; c7a.xlarge has none so add a 60GB gp3 at /mnt
IID=$(aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "${GOM_ITYPE:-c7a.xlarge}" \
  --iam-instance-profile "Name=$PROFILE_NAME" \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=80,VolumeType=gp3}' \
  --instance-initiated-shutdown-behavior terminate \
  --user-data "$UD" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=gom-yellow-zone}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "launched (on-demand) $IID -> $S3/$DATE/validation/yellow_zone_maps.png (+ fill table; done: yz_done.txt)"
