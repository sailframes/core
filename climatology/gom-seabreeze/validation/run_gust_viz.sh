#!/usr/bin/env bash
# run_gust_viz.sh -- ON-DEMAND box: run gust_viz.py on the LES d05 output -> pressure movie +
# gustiness map + point trace + Step-5 turbulence verdict, upload to <s3>/<date>/gust/.
#   AWS_PROFILE=sailframes ./run_gust_viz.sh [YYYY-MM-DD] [s3-prefix-with-wrfout_d05]
set -euo pipefail
DATE="${1:-2026-07-04}"; REGION="${AWS_REGION:-us-east-1}"
S3="${GOM_S3:-s3://sailframes-data-prod/gom}"
PREFIX="${2:-$S3/$DATE/forecast}"          # dir holding wrfout_d0N_*
PROFILE_NAME="${GOM_INSTANCE_PROFILE:-gom-seabreeze-ec2}"
# LES nest + course center + window: d05/Marblehead/afternoon default; d04/Hospital-Shoals/evening = HRRR-LES race
DOMAIN="${GOM_DOMAIN:-d05}"; CEN_LAT="${GOM_CEN_LAT:-42.46}"; CEN_LON="${GOM_CEN_LON:--70.77}"
HH0="${GOM_HH0:-13}"; HH1="${GOM_HH1:-18}"
SUFFIX="${GOM_OUT_SUFFIX:-}"     # e.g. -hrrr -> uploads to <date>/gust-hrrr/ (HRRR-scale compare view)
BBOX="${GOM_BBOX:-}"             # lo0,lo1,la0,la1 -> crop plots (match LES extent when comparing domains)
# gust ceiling inputs (winds aloft mix down in gusts) + surface-vs-gradient veer + movie bounds
W925="${GOM_W925:-}"; W850="${GOM_W850:-}"; CAPE="${GOM_CAPE:-}"; GRADDIR="${GOM_GRADIENT_DIR:-}"
NOMOVIE="${GOM_NO_MOVIE:-}"; MAXFR="${GOM_MAX_MOVIE_FRAMES:-180}"
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/.." && pwd)"
AMI=$(aws ssm get-parameter --region "$REGION" --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value --output text)
echo "### upload current code"; tar czf /tmp/gom-seabreeze.tar.gz -C "$(dirname "$ROOT")" "$(basename "$ROOT")"
aws s3 cp /tmp/gom-seabreeze.tar.gz "$S3/code/gom-seabreeze.tar.gz"

UD=$(sed -e "s|@@DATE@@|$DATE|g" -e "s|@@S3@@|$S3|g" -e "s|@@PREFIX@@|$PREFIX|g" -e "s|@@REGION@@|$REGION|g" \
        -e "s|@@DOMAIN@@|$DOMAIN|g" -e "s|@@CEN_LAT@@|$CEN_LAT|g" -e "s|@@CEN_LON@@|$CEN_LON|g" \
        -e "s|@@HH0@@|$HH0|g" -e "s|@@HH1@@|$HH1|g" -e "s|@@SUFFIX@@|$SUFFIX|g" -e "s|@@BBOX@@|$BBOX|g" \
        -e "s|@@W925@@|$W925|g" -e "s|@@W850@@|$W850|g" -e "s|@@CAPE@@|$CAPE|g" -e "s|@@GRADDIR@@|$GRADDIR|g" \
        -e "s|@@NOMOVIE@@|$NOMOVIE|g" -e "s|@@MAXFR@@|$MAXFR|g" <<'USERDATA' | base64
#!/usr/bin/env bash
set -xeuo pipefail
exec > >(tee -a /var/log/gust.log) 2>&1
DATE="@@DATE@@"; S3="@@S3@@"; PREFIX="@@PREFIX@@"
DOMAIN="@@DOMAIN@@"; CEN_LAT="@@CEN_LAT@@"; CEN_LON="@@CEN_LON@@"; HH0="@@HH0@@"; HH1="@@HH1@@"
SUFFIX="@@SUFFIX@@"; BBOX="@@BBOX@@"; OUT="$S3/$DATE/gust${SUFFIX}"
W925="@@W925@@"; W850="@@W850@@"; CAPE="@@CAPE@@"; GRADDIR="@@GRADDIR@@"; NOMOVIE="@@NOMOVIE@@"; MAXFR="@@MAXFR@@"
dnf install -y python3.11 python3.11-pip tar gzip ffmpeg >/dev/null 2>&1 || dnf install -y python3.11 python3.11-pip tar gzip >/dev/null
python3.11 -m venv /root/venv; /root/venv/bin/pip -q install --upgrade pip
/root/venv/bin/pip -q install numpy xarray netCDF4 matplotlib pillow pyshp
# static ffmpeg (AL2023 has no ffmpeg pkg) -> enables mp4 = native play/pause/scrub in <video>; non-fatal
{ curl -sL --max-time 120 https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz -o /tmp/ff.tar.xz && \
  tar xJf /tmp/ff.tar.xz -C /tmp && cp /tmp/ffmpeg-*-static/ffmpeg /usr/local/bin/ && chmod +x /usr/local/bin/ffmpeg && \
  /usr/local/bin/ffmpeg -version | head -1; } || echo "NO ffmpeg -> gif only"
# GSHHG full-res coastline (detailed harbor islands) -> --coast-shp; non-fatal (model LANDMASK fallback)
COAST=""
if curl -sL --max-time 180 http://www.soest.hawaii.edu/pwessel/gshhg/gshhg-shp-2.3.7.zip -o /root/gshhg.zip && \
   mkdir -p /root/gshhg && python3.11 -c "import zipfile;zipfile.ZipFile('/root/gshhg.zip').extractall('/root/gshhg')"; then
  COAST="--coast-shp=/root/gshhg"; echo "GSHHG coast ready"
else echo "NO GSHHG -> LANDMASK coast"; fi
aws s3 cp "$S3/code/gom-seabreeze.tar.gz" /root/gom.tar.gz; tar xzf /root/gom.tar.gz -C /root
mkdir -p /root/gust
# (#4) independent obs reality check FIRST — cheap, no deps, ships before the LES render so the
# dashboard always shows what actually blew even if the render fails/times out.
OC=$(find /root -name obs_gust_check.py | head -1)
/root/venv/bin/python "$OC" --date "$DATE" --hh0 "$HH0" --hh1 "$HH1" \
  --out /root/gust/obs_check.json --upload-prefix "$OUT" || echo "obs check failed (non-fatal)"
SC=$(find /root -name gust_viz.py | head -1)
set +e
# --upload-prefix ships gustiness/point_trace/step5 the instant they're written (before the slow movie);
# gust ceiling from winds aloft (@@W925@@/@@W850@@ + CAPE) + surface-vs-gradient veer; movie bounded.
/root/venv/bin/python "$SC" --prefix "$PREFIX" --domain "$DOMAIN" --cen-lat "$CEN_LAT" --cen-lon "$CEN_LON" \
  --hh0 "$HH0" --hh1 "$HH1" ${BBOX:+--bbox="$BBOX"} $COAST --scratch /mnt/g --outdir /root/gust \
  --upload-prefix "$OUT" --max-movie-frames "$MAXFR" ${NOMOVIE:+--no-movie} \
  ${W925:+--w925-kt="$W925"} ${W850:+--w850-kt="$W850"} ${CAPE:+--cape="$CAPE"} \
  ${GRADDIR:+--gradient-dir="$GRADDIR"} > /root/gust_out.txt 2>&1
RC=$?
set -e
cat /root/gust_out.txt
aws s3 cp /root/gust_out.txt "$OUT/gust_log.txt" || true
aws s3 cp /root/gust/ "$OUT/" --recursive || true
echo "$RC" | aws s3 cp - "$OUT/done.txt" || true
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
