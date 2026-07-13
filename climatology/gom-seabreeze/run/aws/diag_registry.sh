#!/usr/bin/env bash
# diag_registry.sh -- resolve LES namelist option RANKS (scalar vs max_dom array) against the
# :4.8 image's WRF Registry, so the LES render can't produce a namelist real.exe rejects.
# Cheap on-demand box, self-terminates.  AWS_PROFILE=sailframes ./diag_registry.sh
set -euo pipefail
REGION="${AWS_REGION:-us-east-1}"
ACCT=$(aws sts get-caller-identity --query Account --output text)
ECR="${ACCT}.dkr.ecr.${REGION}.amazonaws.com/sailframes-wrf"
PROFILE_NAME="${GOM_INSTANCE_PROFILE:-gom-seabreeze-ec2}"
S3="${GOM_S3:-s3://sailframes-data-prod/gom}"
AMI=$(aws ssm get-parameter --region "$REGION" --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value --output text)

UD=$(sed -e "s|@@ECR@@|$ECR|g" -e "s|@@REGION@@|$REGION|g" -e "s|@@S3@@|$S3|g" <<'USERDATA' | base64
#!/usr/bin/env bash
set -xeuo pipefail
exec > >(tee -a /var/log/reg.log) 2>&1
dnf install -y docker >/dev/null; systemctl enable --now docker
aws ecr get-login-password --region @@REGION@@ | docker login --username AWS --password-stdin @@ECR@@
docker pull @@ECR@@:4.8
cat > /root/reg.sh <<'INNER'
set -x
REG=$(find /comsoftware/wrf/WRF-4.8.0 -name Registry.EM_COMMON 2>/dev/null | head -1)
echo "REGISTRY=$REG"
# print the rank column for each LES option: 'namelist,<type>,<section>,<default>' -- the
# section field 'max_domains' => per-domain ARRAY, '1' => SCALAR.
for opt in bl_pbl_physics sf_sfclay_physics km_opt diff_opt sfs_opt mix_isotropic m_opt isfflx \
           c_s c_k epssm non_hydrostatic parent_time_step_ratio use_adaptive_time_step \
           mix_full_fields sfs_const; do
  line=$(grep -iE "^[[:space:]]*rconfig[[:space:]]+.*[[:space:]]${opt}[[:space:]]" "$REG" 2>/dev/null | head -1)
  [ -z "$line" ] && line=$(grep -iwE "$opt" "$REG" 2>/dev/null | grep -i rconfig | head -1)
  echo "OPT $opt :: $line"
done
INNER
docker run --rm -i --user 0:0 --entrypoint bash @@ECR@@:4.8 -ls < /root/reg.sh > /root/reg_out.txt 2>&1 || true
cat /root/reg_out.txt
aws s3 cp /root/reg_out.txt @@S3@@/les/registry_ranks.txt || true
echo DONE | aws s3 cp - @@S3@@/les/reg_done.txt || true
IID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
aws ec2 terminate-instances --instance-ids "$IID" --region @@REGION@@ || shutdown -h now
USERDATA
)
IID=$(aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "${GOM_ITYPE:-c7a.xlarge}" \
  --iam-instance-profile "Name=$PROFILE_NAME" \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=60,VolumeType=gp3}' \
  --instance-initiated-shutdown-behavior terminate --user-data "$UD" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=gom-registry-diag}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "launched (on-demand) $IID -> $S3/les/registry_ranks.txt (done: reg_done.txt)"
