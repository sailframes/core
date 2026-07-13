#!/usr/bin/env bash
# build_wrf48_image.sh -- build a modern WRF 4.8 + WPS 4.7 + OBSGRID container image
# on an EC2 box and push it to ECR, then self-terminate. The gom-seabreeze harness
# then runs on it via GOM_IMAGE=<ecr>/sailframes-wrf:4.8.
#
# Reuses the DTC image (dtcenter/wps_wrf:latest) ONLY as a dependency base -- it already
# carries the finicky netCDF/HDF5/OpenMPI/jasper stack + env vars. We compile the current
# WRF/WPS/OBSGRID on top, which drops every WRF-4.3 workaround (VEGPARM NLCD40 LCZ patch,
# namelist comment-strip, sst_update auxinput4 quirk) and adds obsgrid.exe for obs-nudging.
#
#   AWS_PROFILE=sailframes ./build_wrf48_image.sh
set -euo pipefail
REGION="${AWS_REGION:-us-east-1}"
ACCT=$(aws sts get-caller-identity --query Account --output text)
ECR="${ACCT}.dkr.ecr.${REGION}.amazonaws.com/sailframes-wrf"
TAG="${WRF_TAG:-4.8}"
ITYPE="${BUILD_ITYPE:-c7a.8xlarge}"          # 32 vCPU -> fast -j compile
PROFILE_NAME="${GOM_INSTANCE_PROFILE:-gom-seabreeze-ec2}"
S3="${GOM_S3:-s3://sailframes-data-prod/gom}"

AMI=$(aws ssm get-parameter --region "$REGION" \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query Parameter.Value --output text)

# Quoted heredoc: nothing expands here; only @@VARS@@ are sed-substituted below.
UD=$(sed -e "s|@@ECR@@|$ECR|g" -e "s|@@TAG@@|$TAG|g" -e "s|@@REGION@@|$REGION|g" -e "s|@@S3@@|$S3|g" <<'USERDATA' | base64
#!/usr/bin/env bash
set -xeuo pipefail
exec > >(tee -a /var/log/wrf-build.log) 2>&1
dnf install -y docker >/dev/null; systemctl enable --now docker
mkdir -p /build && cd /build
cat > Dockerfile <<'DOCKER'
# WRF 4.8.0 + WPS 4.7.0 + OBSGRID on the DTC dependency stack.
FROM dtcenter/wps_wrf:latest
USER root
SHELL ["/bin/bash", "-lc"]
ARG WRF_VER=4.8.0
ARG WPS_VER=4.7.0
RUN (command -v git) || (yum install -y git || (apt-get update && apt-get install -y git)) || true
WORKDIR /comsoftware/wrf
# WRF 4.8 -- GNU dmpar (x86_64 gfortran/gcc = option 34), nesting basic (1). Tee the
# configure menu so a wrong option number is visible in the build log if this fails.
RUN git clone --depth 1 -b v${WRF_VER} https://github.com/wrf-model/WRF.git WRF-${WRF_VER} && \
    cd WRF-${WRF_VER} && printf '34\n1\n' | ./configure 2>&1 | tee /tmp/wrf_conf.log && \
    ./compile -j $(nproc) em_real > /tmp/wrf_compile.log 2>&1; tail -40 /tmp/wrf_compile.log && \
    test -x main/wrf.exe && test -x main/real.exe
# WPS 4.7 -- serial + grib2 (option 1; needs JASPER env from base + WRF_DIR).
ENV WRF_DIR=/comsoftware/wrf/WRF-${WRF_VER}
RUN git clone --depth 1 -b v${WPS_VER} https://github.com/wrf-model/WPS.git WPS-${WPS_VER} && \
    cd WPS-${WPS_VER} && printf '1\n' | ./configure 2>&1 | tee /tmp/wps_conf.log && \
    ./compile > /tmp/wps_compile.log 2>&1; tail -30 /tmp/wps_compile.log && \
    test -x geogrid/src/geogrid.exe && test -x ungrib/src/ungrib.exe && test -x metgrid/src/metgrid.exe
# OBSGRID (obs-nudging OBS_DOMAIN generator; unmaintained but functional). Option 2 =
# gfortran (1 is PGI/pgf90, not installed). Its configure.oa FFLAGS omit
# -fallow-argument-mismatch, which gfortran 10+ requires for this 2016 code (WRF/WPS
# configure add it automatically; OBSGRID's doesn't) -- inject it. The plot utils also
# need NCAR Graphics (-lncarg) and fail-but-ignored; we only need obsgrid.exe, so guard
# on it. On failure, surface the real obsgrid.exe errors (not just the plot-util tail).
RUN git clone --depth 1 https://github.com/wrf-model/OBSGRID.git OBSGRID && \
    cd OBSGRID && printf '2\n' | ./configure 2>&1 | tee /tmp/obsgrid_conf.log && \
    sed -i -E 's/^(FFLAGS[[:space:]]*=.*)/\1 -fallow-argument-mismatch -fallow-invalid-boz/; s/^(F77FLAGS[[:space:]]*=.*)/\1 -fallow-argument-mismatch -fallow-invalid-boz/' configure.oa && \
    grep -E '^(FFLAGS|F77FLAGS)' configure.oa && \
    (./compile > /tmp/obsgrid_compile.log 2>&1 || true); \
    echo '=== obsgrid errors (non-ignored) ==='; grep -iE 'error|obsgrid\.exe' /tmp/obsgrid_compile.log | grep -vi ignored | tail -40; \
    echo '=== last 30 lines ==='; tail -30 /tmp/obsgrid_compile.log && \
    test -x obsgrid.exe
DOCKER
set +e
docker build -t @@ECR@@:@@TAG@@ /build; BRC=$?
set -e
echo "docker build rc=$BRC"
if [ "$BRC" = 0 ]; then
  aws ecr get-login-password --region @@REGION@@ | docker login --username AWS --password-stdin @@ECR@@
  docker push @@ECR@@:@@TAG@@ && echo "PUSHED @@ECR@@:@@TAG@@"
fi
aws s3 cp /var/log/wrf-build.log @@S3@@/wrf48-build/build.log || true
echo "$BRC" | aws s3 cp - @@S3@@/wrf48-build/exit_code.txt || true
IID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
aws ec2 terminate-instances --instance-ids "$IID" --region @@REGION@@ || shutdown -h now
USERDATA
)

echo "### launch build box ($ITYPE, 150GB gp3) -> $ECR:$TAG"
IID=$(aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "$ITYPE" \
  --instance-market-options 'MarketType=spot' \
  --iam-instance-profile "Name=$PROFILE_NAME" \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=150,VolumeType=gp3}' \
  --instance-initiated-shutdown-behavior terminate \
  --user-data "$UD" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=gom-wrf48-build}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "launched $IID -> pushes $ECR:$TAG"
echo "log: $S3/wrf48-build/build.log   exit: $S3/wrf48-build/exit_code.txt"
