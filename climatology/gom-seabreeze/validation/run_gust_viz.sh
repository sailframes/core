#!/usr/bin/env bash
# run_gust_viz.sh -- ON-DEMAND box: run gust_viz.py on the LES d05 output -> pressure movie +
# gustiness map + point trace + Step-5 turbulence verdict, upload to <s3>/<date>/gust/.
#   AWS_PROFILE=sailframes ./run_gust_viz.sh [YYYY-MM-DD] [s3-prefix-with-wrfout_d05]
set -euo pipefail
DATE="${1:-2026-07-04}"; REGION="${AWS_REGION:-us-east-1}"
S3="${GOM_S3:-s3://sailframes-data-prod/gom}"
PREFIX="${2:-$S3/$DATE/forecast}"          # dir holding wrfout_d05_*
PROFILE_NAME="${GOM_INSTANCE_PROFILE:-gom-seabreeze-ec2}"
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/.." && pwd)"
AMI=$(aws ssm get-parameter --region "$REGION" --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value --output text)
echo "### upload current code"; tar czf /tmp/gom-seabreeze.tar.gz -C "$(dirname "$ROOT")" "$(basename "$ROOT")"
aws s3 cp /tmp/gom-seabreeze.tar.gz "$S3/code/gom-seabreeze.tar.gz"

UD=$(sed -e "s|@@DATE@@|$DATE|g" -e "s|@@S3@@|$S3|g" -e "s|@@PREFIX@@|$PREFIX|g" -e "s|@@REGION@@|$REGION|g" <<'USERDATA' | base64
#!/usr/bin/env bash
set -xeuo pipefail
exec > >(tee -a /var/log/gust.log) 2>&1
DATE="@@DATE@@"; S3="@@S3@@"; PREFIX="@@PREFIX@@"
dnf install -y python3.11 python3.11-pip tar gzip ffmpeg >/dev/null 2>&1 || dnf install -y python3.11 python3.11-pip tar gzip >/dev/null
python3.11 -m venv /root/venv; /root/venv/bin/pip -q install --upgrade pip
/root/venv/bin/pip -q install numpy xarray netCDF4 matplotlib pillow
aws s3 cp "$S3/code/gom-seabreeze.tar.gz" /root/gom.tar.gz; tar xzf /root/gom.tar.gz -C /root
SC=$(find /root -name gust_viz.py | head -1)
set +e
/root/venv/bin/python "$SC" --prefix "$PREFIX" --scratch /mnt/g --outdir /root/gust > /root/gust_out.txt 2>&1
RC=$?
set -e
cat /root/gust_out.txt
aws s3 cp /root/gust_out.txt "$S3/$DATE/gust/gust_log.txt" || true
aws s3 cp /root/gust/ "$S3/$DATE/gust/" --recursive || true
echo "$RC" | aws s3 cp - "$S3/$DATE/gust/done.txt" || true
IID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
aws ec2 terminate-instances --instance-ids "$IID" --region @@REGION@@ || shutdown -h now
USERDATA
)
IID=$(aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "${GOM_ITYPE:-c7a.2xlarge}" \
  --iam-instance-profile "Name=$PROFILE_NAME" \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=80,VolumeType=gp3}' \
  --instance-initiated-shutdown-behavior terminate --user-data "$UD" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=gom-gust-viz}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "launched (on-demand) $IID -> $S3/$DATE/gust/ (pressure_movie + gustiness + point_trace + step5; done.txt)"
