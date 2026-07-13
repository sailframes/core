#!/usr/bin/env bash
# diag_obsgrid.sh -- one-shot diagnostic: from the pushed :4.8 image, configure+compile
# OBSGRID and capture EVERYTHING (gfortran version, assembled configure.oa, the .F.o rule,
# full compile log, and a verbose preprocess+compile of the first failing module) to S3 so
# the real "Unclassifiable statement" cause is legible. Cheap box, self-terminates.
#   AWS_PROFILE=sailframes ./diag_obsgrid.sh
set -euo pipefail
REGION="${AWS_REGION:-us-east-1}"
ACCT=$(aws sts get-caller-identity --query Account --output text)
ECR="${ACCT}.dkr.ecr.${REGION}.amazonaws.com/sailframes-wrf"
PROFILE_NAME="${GOM_INSTANCE_PROFILE:-gom-seabreeze-ec2}"
S3="${GOM_S3:-s3://sailframes-data-prod/gom}"
AMI=$(aws ssm get-parameter --region "$REGION" \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query Parameter.Value --output text)

UD=$(sed -e "s|@@ECR@@|$ECR|g" -e "s|@@REGION@@|$REGION|g" -e "s|@@S3@@|$S3|g" <<'USERDATA' | base64
#!/usr/bin/env bash
set -xeuo pipefail
exec > >(tee -a /var/log/obsgrid-diag.log) 2>&1
dnf install -y docker >/dev/null; systemctl enable --now docker
aws ecr get-login-password --region @@REGION@@ | docker login --username AWS --password-stdin @@ECR@@
cat > /root/diag.sh <<'INNER'
set -x
gfortran --version | head -1
cd /comsoftware/wrf
rm -rf OBSGRID; git clone --depth 1 https://github.com/wrf-model/OBSGRID.git OBSGRID
cd OBSGRID && printf '2\n' | ./configure >/tmp/conf.log 2>&1
# ROOT CAUSE: cpp -C keeps comments; Red Hat gcc 8 auto-includes stdc-predef.h, whose
# /* GNU C Library */ license block lands in the preprocessed Fortran -> gfortran chokes.
# Drop -C so the injected C comment is stripped.
sed -i 's#^CPP\s*=.*#CPP = /usr/bin/cpp -P -traditional#' configure.oa
echo "===== CPP line after fix ====="; grep '^CPP' configure.oa
echo "===== compile obsgrid (target arg -> skips fixed-form plot utils) ====="
./compile obsgrid > /tmp/obs_compile.log 2>&1 || true
echo "----- head 40 -----"; sed -n '1,40p' /tmp/obs_compile.log
echo "----- errors (non-ignored) -----"; grep -iE 'error|undefined|Unclassifiable|cannot' /tmp/obs_compile.log | grep -vi ignored | head -30
echo "----- tail 20 -----"; tail -20 /tmp/obs_compile.log
echo "OBSGRID_EXE:"; ls -la obsgrid.exe src/obsgrid.exe 2>/dev/null || echo "(none)"
INNER
# DTC entrypoint drops to UID 9999 (can't read root's mount / write /comsoftware) -> override
# entrypoint to bash, run as root, feed the script via stdin (bash -ls) so no file perms needed.
docker run --rm -i --user 0:0 --entrypoint bash @@ECR@@:4.8 -ls < /root/diag.sh > /root/obsgrid_diag.txt 2>&1 || true
aws s3 cp /root/obsgrid_diag.txt @@S3@@/wrf48-build/obsgrid_diag.txt || true
echo DONE | aws s3 cp - @@S3@@/wrf48-build/diag_done.txt || true
IID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
aws ec2 terminate-instances --instance-ids "$IID" --region @@REGION@@ || shutdown -h now
USERDATA
)

IID=$(aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "${BUILD_ITYPE:-c7a.xlarge}" \
  --instance-market-options 'MarketType=spot' \
  --iam-instance-profile "Name=$PROFILE_NAME" \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=60,VolumeType=gp3}' \
  --instance-initiated-shutdown-behavior terminate \
  --user-data "$UD" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=gom-obsgrid-diag}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "launched $IID -> $S3/wrf48-build/obsgrid_diag.txt (done marker: diag_done.txt)"
