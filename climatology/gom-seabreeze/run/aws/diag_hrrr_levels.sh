#!/usr/bin/env bash
# diag_hrrr_levels.sh -- pinpoint why HRRR metgrid made met_em with 1 level. Stages one HRRR
# wrfprs grib, g2print's it (does the grib HAVE isobaric 3D?), ungribs with Vtable.raphrrr,
# rd_intermediate's the FILE (did the isobaric levels survive ungrib?). Cheap, self-terminates.
set -euo pipefail
REGION="${AWS_REGION:-us-east-1}"; ACCT=$(aws sts get-caller-identity --query Account --output text)
ECR="${ACCT}.dkr.ecr.${REGION}.amazonaws.com/sailframes-wrf"; S3="${GOM_S3:-s3://sailframes-data-prod/gom}"
PROFILE_NAME="${GOM_INSTANCE_PROFILE:-gom-seabreeze-ec2}"
AMI=$(aws ssm get-parameter --region "$REGION" --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value --output text)
UD=$(sed -e "s|@@ECR@@|$ECR|g" -e "s|@@REGION@@|$REGION|g" -e "s|@@S3@@|$S3|g" <<'USERDATA' | base64
#!/usr/bin/env bash
set -xeuo pipefail
exec > >(tee -a /var/log/hl.log) 2>&1
dnf install -y docker >/dev/null; systemctl enable --now docker
aws ecr get-login-password --region @@REGION@@ | docker login --username AWS --password-stdin @@ECR@@
docker pull @@ECR@@:4.8
aws s3 cp --no-sign-request --quiet s3://noaa-hrrr-bdp-pds/hrrr.20260704/conus/hrrr.t00z.wrfprsf00.grib2 /root/hrrr.grib2
cat > /root/hl.sh <<'INNER'
set -x; cd /tmp
WPS=$(ls -d /comsoftware/wrf/WPS-4.7.0 2>/dev/null || ls -d /comsoftware/wrf/WPS-* | head -1)
echo "WPS=$WPS"
echo "===== g2print: does HRRR wrfprs HAVE isobaric 3D? (count ISBL / mb levels) ====="
"$WPS/util/g2print.exe" /root/hrrr.grib2 2>&1 | grep -iE 'TMP|UGRD|VGRD|HGT|isbl|mb|Total' | head -40
echo "----- isobaric level count -----"; "$WPS/util/g2print.exe" /root/hrrr.grib2 2>&1 | grep -icE '[0-9]+ mb|isobaric'
echo "===== does Vtable.raphrrr have isobaric (level type 100) entries? ====="
grep -cE '^\s*[0-9]+\s*\|\s*100\s*\|' "$WPS/ungrib/Variable_Tables/Vtable.raphrrr" 2>/dev/null || echo "no lvl-100 lines"
head -30 "$WPS/ungrib/Variable_Tables/Vtable.raphrrr"
echo "===== ungrib the grib + rd_intermediate the FILE (isobaric levels in intermediate?) ====="
cp "$WPS/ungrib/Variable_Tables/Vtable.raphrrr" Vtable
"$WPS/link_grib.csh" /root/hrrr.grib2
cat > namelist.wps <<NL
&share
 wrf_core='ARW', max_dom=1, start_date='2026-07-04_00:00:00', end_date='2026-07-04_00:00:00', interval_seconds=3600,
/
&ungrib
 out_format='WPS', prefix='HL',
/
NL
"$WPS/ungrib/src/ungrib.exe" > ungrib.log 2>&1 || "$WPS/ungrib.exe" > ungrib.log 2>&1 || true
ls -la HL:* 2>/dev/null
"$WPS/util/rd_intermediate.exe" HL:2026-07-04_00 2>&1 | grep -iE 'TT|UU|VV|level|xlvl|field' | head -50
echo "----- distinct pressure levels in intermediate -----"
"$WPS/util/rd_intermediate.exe" HL:2026-07-04_00 2>&1 | grep -oE 'xlvl *= *[0-9.]+' | sort -u | wc -l
INNER
docker run --rm -i --user 0:0 --entrypoint bash -v /root:/host @@ECR@@:4.8 -lc 'cp /host/hrrr.grib2 /root/hrrr.grib2; bash -s' < /root/hl.sh > /root/hl_out.txt 2>&1 || true
cat /root/hl_out.txt
aws s3 cp /root/hl_out.txt @@S3@@/hrrr-diag/levels.txt || true
echo DONE | aws s3 cp - @@S3@@/hrrr-diag/done.txt || true
IID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id); aws ec2 terminate-instances --instance-ids "$IID" --region @@REGION@@ || shutdown -h now
USERDATA
)
IID=$(aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "${GOM_ITYPE:-c7a.xlarge}" \
  --iam-instance-profile "Name=$PROFILE_NAME" --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=60,VolumeType=gp3}' \
  --instance-initiated-shutdown-behavior terminate --user-data "$UD" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=gom-hrrr-diag}]" --query 'Instances[0].InstanceId' --output text)
echo "launched (on-demand) $IID -> $S3/hrrr-diag/levels.txt (done: done.txt)"
