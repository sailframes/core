#!/usr/bin/env bash
# run_holdout_analysis.sh -- boot a small box, run compare_holdout_44013.py across the three
# WRF runs (nudged / freerun-ysu / nudged-val) reading wrfout from S3, upload the MAE table +
# plot, self-terminate. Cheap (c7a.xlarge, ~15 min).
#   AWS_PROFILE=sailframes ./run_holdout_analysis.sh [YYYY-MM-DD]
set -euo pipefail
DATE="${1:-2026-07-04}"
REGION="${AWS_REGION:-us-east-1}"
S3="${GOM_S3:-s3://sailframes-data-prod/gom}"
PROFILE_NAME="${GOM_INSTANCE_PROFILE:-gom-seabreeze-ec2}"
AMI=$(aws ssm get-parameter --region "$REGION" \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query Parameter.Value --output text)

UD=$(sed -e "s|@@DATE@@|$DATE|g" -e "s|@@S3@@|$S3|g" -e "s|@@REGION@@|$REGION|g" <<'USERDATA' | base64
#!/usr/bin/env bash
set -xeuo pipefail
exec > >(tee -a /var/log/holdout.log) 2>&1
DATE="@@DATE@@"; S3="@@S3@@"
dnf install -y python3.11 python3.11-pip tar gzip >/dev/null
python3.11 -m venv /root/venv
/root/venv/bin/pip -q install --upgrade pip
/root/venv/bin/pip -q install numpy xarray netCDF4 matplotlib
aws s3 cp "$S3/code/gom-seabreeze.tar.gz" /root/gom.tar.gz
tar xzf /root/gom.tar.gz -C /root
SCRIPT=$(find /root -name compare_holdout_44013.py | head -1)
set +e
/root/venv/bin/python "$SCRIPT" --date "$DATE" \
  --run "freerun-ysu=$S3/$DATE/freerun-ysu" \
  --run "nudged-val=$S3/$DATE/nudged-val" \
  --run "nudged=$S3/$DATE/nudged" \
  --scratch /mnt/val --out /root/holdout_44013.png > /root/holdout_result.txt 2>&1
RC=$?
set -e
cat /root/holdout_result.txt
aws s3 cp /root/holdout_result.txt "$S3/$DATE/validation/holdout_44013_mae.txt" || true
aws s3 cp /root/holdout_44013.png "$S3/$DATE/validation/holdout_44013.png" || true
aws s3 cp /var/log/holdout.log "$S3/$DATE/validation/holdout.log" || true
echo "$RC" | aws s3 cp - "$S3/$DATE/validation/done.txt" || true
IID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
aws ec2 terminate-instances --instance-ids "$IID" --region @@REGION@@ || shutdown -h now
USERDATA
)

IID=$(aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "${GOM_ITYPE:-c7a.xlarge}" \
  --instance-market-options 'MarketType=spot' \
  --iam-instance-profile "Name=$PROFILE_NAME" \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=100,VolumeType=gp3}' \
  --instance-initiated-shutdown-behavior terminate \
  --user-data "$UD" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=gom-holdout-analysis}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "launched $IID -> $S3/$DATE/validation/ (holdout_44013_mae.txt + .png; done marker: done.txt)"
