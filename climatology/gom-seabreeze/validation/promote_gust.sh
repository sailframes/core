#!/usr/bin/env bash
# promote_gust.sh -- publish a finished gust render from the EC2-writable staging area
# (gom/<date>/gust*) to the DASHBOARD-SERVED location (climatology/gust*/<date>) + bust
# the CloudFront cache.
#
# WHY THIS EXISTS: the gom-seabreeze-ec2 render role can write gom/* but NOT climatology/*
# (see run_backfill_both.sh), so the render can't publish itself. Rumble II 2026-07-15
# is the cautionary tale: a good afternoon render (mean 13.7 / peak 19.3 kt, matching the
# obs) sat in gom/ while the dashboard kept serving the light morning render (9.4 / 12.2)
# through the whole race, because this promote step was manual and never ran. Run this
# after every race-day render (or point CI at it) so the freshest render is what people see.
#
#   AWS_PROFILE=sailframes ./promote_gust.sh 2026-07-15            # promote LES gust now
#   AWS_PROFILE=sailframes ./promote_gust.sh 2026-07-15 -hrrr     # the HRRR-scale compare view
#   AWS_PROFILE=sailframes ./promote_gust.sh 2026-07-15 '' --wait # block until the render's done.txt lands, then promote
set -euo pipefail
DATE="${1:?usage: promote_gust.sh YYYY-MM-DD [suffix] [--wait]}"
SUF="${2:-}"; WAIT="${3:-}"
PROFILE="${AWS_PROFILE:-sailframes}"; DIST="${CF_DIST:-EFO342DVGM3QS}"
SRC="s3://sailframes-data-prod/gom/$DATE/gust${SUF}"
DST="s3://sailframes-data-prod/climatology/gust${SUF}/$DATE"

if [ "$WAIT" = "--wait" ]; then
  echo "waiting for $SRC/done.txt ..."
  until aws s3 ls "$SRC/done.txt" --profile "$PROFILE" >/dev/null 2>&1; do sleep 30; done
  echo "render done."
fi
if ! aws s3 ls "$SRC/gustiness.png" --profile "$PROFILE" >/dev/null 2>&1; then
  echo "ERROR: no render at $SRC (gustiness.png missing) — nothing to promote" >&2; exit 1
fi
echo "promote  $SRC  ->  $DST"
# copy the served artifacts; skip logs. short max-age so a re-promote shows within a minute.
aws s3 cp "$SRC/" "$DST/" --recursive --profile "$PROFILE" \
  --cache-control max-age=60 --exclude "*.txt"
INV=$(aws cloudfront create-invalidation --distribution-id "$DIST" \
  --paths "/climatology/gust${SUF}/$DATE/*" --profile "$PROFILE" \
  --query 'Invalidation.Id' --output text)
echo "promoted + invalidated ($INV).  Live: https://sailframes.com/gust.html  (date $DATE)"
