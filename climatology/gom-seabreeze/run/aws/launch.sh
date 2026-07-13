#!/usr/bin/env bash
# launch.sh -- provision a Spot EC2 that runs one gom-seabreeze WRF case end-to-end
# (userdata.sh -> run_case.sh) and pushes wrfout_d03 to S3, then self-terminates.
#
#   AWS_PROFILE=sailframes ./launch.sh 2024-07-31 forecast
#
# Prereqs (one-time): an IAM instance profile ($GOM_INSTANCE_PROFILE) whose role can
# read/write s3://sailframes-data-prod/gom/* and self-terminate (ec2:TerminateInstances).
set -euo pipefail
DATE="${1:?usage: launch.sh YYYY-MM-DD [forecast|hindcast]}"; MODE="${2:-forecast}"
REGION="${AWS_REGION:-us-east-1}"
ITYPE="${GOM_ITYPE:-c7a.8xlarge}"          # 32 vCPU AMD; small domain runs 36-48h in ~1-2h
RUN_HOURS="${GOM_RUN_HOURS:-36}"
S3="${GOM_S3:-s3://sailframes-data-prod/gom}"
PROFILE_NAME="${GOM_INSTANCE_PROFILE:-gom-seabreeze-ec2}"
TERMINATE="${GOM_TERMINATE:-1}"
GEOG_DETAIL="${GOM_GEOG_DETAIL:-modis}"     # modis (30s) | nlcd (9s ~250m land-water mask + urban)
GEOGRID_ONLY="${GOM_GEOGRID_ONLY:-0}"       # 1 = cheap geogrid+landmask-QC pass, no GFS/real/wrf
# Container image + in-container WRF/WPS build-dir versions. Defaults = DTC 4.3 image.
# Modern WRF: GOM_IMAGE=<acct>.dkr.ecr.us-east-1.amazonaws.com/sailframes-wrf:4.8
#            GOM_WRF_VER=4.8.0 GOM_WPS_VER=4.7.0  (ECR refs get an auto docker-login)
IMAGE="${GOM_IMAGE:-dtcenter/wps_wrf:latest}"
WRF_VER="${GOM_WRF_VER:-4.3}"; WPS_VER="${GOM_WPS_VER:-4.3}"
OBS_NUDGE="${GOM_OBS_NUDGE:-0}"             # 1 = obs (station) nudging (needs :4.8 image w/ obsgrid.exe)
OBSGRID_ONLY="${GOM_OBSGRID_ONLY:-0}"       # 1 = stop after obsgrid (cheap obs-nudging gate)
OBS_EXCLUDE="${GOM_OBS_EXCLUDE:-}"          # station ids to hold out (e.g. 44013 for validation)
OBS_NUDGE_DOMS="${GOM_OBS_NUDGE_DOMS:-1,1,1}"  # 1,1,1 product | 1,1,0 d01/d02-only validation | 0,0,0 YSU free baseline
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/../.." && pwd)"

echo "### upload the gom-seabreeze code so the instance can pull it (no git auth needed)"
tar czf /tmp/gom-seabreeze.tar.gz -C "$(dirname "$ROOT")" "$(basename "$ROOT")"
aws s3 cp /tmp/gom-seabreeze.tar.gz "$S3/code/gom-seabreeze.tar.gz"

echo "### latest AL2023 x86_64 AMI"
AMI=$(aws ssm get-parameter --region "$REGION" \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query Parameter.Value --output text)
echo "  AMI=$AMI  type=$ITYPE"

echo "### render userdata"
UD=$(sed -e "s|@@DATE@@|$DATE|g" -e "s|@@MODE@@|$MODE|g" -e "s|@@RUN_HOURS@@|$RUN_HOURS|g" \
        -e "s|@@S3@@|$S3|g" -e "s|@@TERMINATE@@|$TERMINATE|g" \
        -e "s|@@GEOG_DETAIL@@|$GEOG_DETAIL|g" -e "s|@@GEOGRID_ONLY@@|$GEOGRID_ONLY|g" \
        -e "s|@@IMAGE@@|$IMAGE|g" -e "s|@@WRF_VER@@|$WRF_VER|g" -e "s|@@WPS_VER@@|$WPS_VER|g" \
        -e "s|@@OBS_NUDGE@@|$OBS_NUDGE|g" -e "s|@@OBSGRID_ONLY@@|$OBSGRID_ONLY|g" -e "s|@@OBS_EXCLUDE@@|$OBS_EXCLUDE|g" \
        -e "s|@@OBS_NUDGE_DOMS@@|$OBS_NUDGE_DOMS|g" \
        "$HERE/userdata.sh" | base64)

echo "### launch (Spot, terminate-on-shutdown, 150 GB gp3 for geog+work)"
IID=$(aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "$ITYPE" \
  --instance-market-options 'MarketType=spot' \
  --iam-instance-profile "Name=$PROFILE_NAME" \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=150,VolumeType=gp3}' \
  --instance-initiated-shutdown-behavior terminate \
  --user-data "$UD" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=gom-$DATE-$MODE}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "launched $IID  (image=$IMAGE  wrf=$WRF_VER/wps=$WPS_VER  geog=$GEOG_DETAIL  geogrid_only=$GEOGRID_ONLY)"
echo "watch:  aws ssm start-session --target $IID     # then: tail -f /var/log/gom-userdata.log"
if [ "$GEOGRID_ONLY" = 1 ]; then
  echo "output: $S3/$DATE/geoqc/$GEOG_DETAIL/   (geo_em.d0*.nc + LANDMASK/LU_INDEX PNGs)"
else
  echo "output: $S3/$DATE/$MODE/   (wrfout_d03*, userdata.log, exit_code.txt)"
fi
