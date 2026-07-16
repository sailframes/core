#!/usr/bin/env bash
# promote_gust.sh -- publish a finished gust render from the EC2-writable staging area
# (gom/<date>/gust*) to the DASHBOARD-SERVED location (climatology/gust*/<date>) + bust
# the CloudFront cache.
#
# WHY THIS EXISTS: the gom-seabreeze-ec2 render role can write gom/* but NOT climatology/*
# (see run_backfill_both.sh), so the render can't publish itself. Rumble II 2026-07-15
# is the cautionary tale: a good afternoon render (mean 13.7 / peak 19.3 kt, matching the
# obs) sat in gom/ while the dashboard kept serving the light morning render (9.4 / 12.2)
# through the whole race, because promotion was manual and never ran.
#
# Credentials: honors the AWS CLI's own AWS_PROFILE env var (local) or ambient role (CI).
#   AWS_PROFILE=sailframes ./promote_gust.sh 2026-07-15             # promote LES gust now
#   AWS_PROFILE=sailframes ./promote_gust.sh 2026-07-15 -hrrr       # the HRRR-scale compare view
#   AWS_PROFILE=sailframes ./promote_gust.sh 2026-07-15 '' --wait   # block on the render's done.txt, then promote
#   ./promote_gust.sh auto                                          # CI: promote any FRESH render (today+tomorrow ET, both views)
set -uo pipefail
DATE="${1:?usage: promote_gust.sh <YYYY-MM-DD|auto> [suffix] [--wait]}"
SUF="${2:-}"; WAIT="${3:-}"
DIST="${CF_DIST:-EFO342DVGM3QS}"
BKT="s3://sailframes-data-prod"

_lastmod() {  # echo LastModified ISO of an s3 object, or empty if absent
  aws s3api head-object --bucket sailframes-data-prod --key "$1" --query LastModified --output text 2>/dev/null
}

promote_one() {  # <date> <suffix> -- copy only if the gom render is newer than what's served
  local d="$1" s="$2"
  local src="$BKT/gom/$d/gust${s}" dst="$BKT/climatology/gust${s}/$d"
  local srckey="gom/$d/gust${s}/gustiness.png" dstkey="climatology/gust${s}/$d/gustiness.png"
  local slm; slm=$(_lastmod "$srckey")
  if [ -z "$slm" ]; then return 0; fi                       # no render staged for this date/view
  local dlm; dlm=$(_lastmod "$dstkey")
  if [ -n "$dlm" ] && [[ "$slm" < "$dlm" || "$slm" == "$dlm" ]]; then
    echo "  up to date: gust${s} $d (served $dlm >= render $slm)"; return 0
  fi
  echo "promote  $src  ->  $dst   (render $slm > served ${dlm:-none})"
  if ! aws s3 cp "$src/" "$dst/" --recursive --cache-control max-age=60 --exclude "*.txt"; then
    echo "  WARN: copy failed (perms?) — skipping $d$s" >&2; return 0
  fi
  aws cloudfront create-invalidation --distribution-id "$DIST" \
    --paths "/climatology/gust${s}/$d/*" --query 'Invalidation.Id' --output text 2>/dev/null \
    || echo "  (invalidation skipped — max-age=60 will refresh within a minute)"
}

if [ "$DATE" = "auto" ]; then
  # candidate dates: yesterday..tomorrow in Boston ET (post-midnight, race-day, day-ahead), both views
  for off in -1 0 1; do
    D=$(python3 -c "import datetime as t;print((t.datetime.now(t.timezone.utc).replace(tzinfo=None)-t.timedelta(hours=4)+t.timedelta(days=$off)).date())")
    for S in "" "-hrrr"; do promote_one "$D" "$S"; done
  done
  echo "auto-promote done."
  exit 0
fi

if [ "$WAIT" = "--wait" ]; then
  echo "waiting for $BKT/gom/$DATE/gust${SUF}/done.txt ..."
  until aws s3 ls "$BKT/gom/$DATE/gust${SUF}/done.txt" >/dev/null 2>&1; do sleep 30; done
  echo "render done."
fi
if [ -z "$(_lastmod "gom/$DATE/gust${SUF}/gustiness.png")" ]; then
  echo "ERROR: no render at $BKT/gom/$DATE/gust${SUF} (gustiness.png missing)" >&2; exit 1
fi
promote_one "$DATE" "$SUF"
echo "promoted.  Live: https://sailframes.com/gust.html  (date $DATE)"
