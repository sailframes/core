#!/usr/bin/env python3
"""
madis_pull.py — mirror Boston-area MADIS surface obs (netCDF) to S3.

Real-time (default): walk each dataset's netCDF directory listing and copy any file
within the lookback window that S3 doesn't already have (deduped by size, so an
updating current-hour file is re-pulled while completed hours are skipped).
Backfill:  --date YYYY-MM-DD  mirrors that day from the MADIS archive tree.

Runs on the EC2 that owns the registered Elastic IP (100.60.174.47) so egress
matches the MADIS allowlist. The public datasets below work with NO credentials
today; set MADIS_USER/MADIS_PASS (+ MADIS_BASE for the restricted endpoint) once
the restricted account is issued — no code change needed.

Verified server quirks (2026-07-10): the netCDF subdir case differs by family —
LDAD/* use "netCDF", point/* use "netcdf" — so we try both. TLS cert is valid
(no -k needed). Files are hourly `yyyymmdd_HHMM.gz`; hfmetar holds the 5-min ASOS.

Env:
  MADIS_BASE          default https://madis-data.ncep.noaa.gov/madisPublic1
  MADIS_BUCKET        default sailframes-data-prod
  MADIS_PREFIX        default madis/raw
  MADIS_DATASETS      default point/metar,LDAD/hfmetar,LDAD/mesonet   (comma sep)
  MADIS_LOOKBACK_MIN  default 180
  MADIS_USER/PASS     optional HTTP basic auth (restricted account)
  MADIS_VERIFY        set 0 to skip TLS verify (leave on; cert is valid)

Usage:  python3 madis_pull.py [--dry] [--date YYYY-MM-DD]
"""
import datetime as dt
import os
import re
import sys

import boto3
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = os.environ.get("MADIS_BASE", "https://madis-data.ncep.noaa.gov/madisPublic1").rstrip("/")
BUCKET = os.environ.get("MADIS_BUCKET", "sailframes-data-prod")
PREFIX = os.environ.get("MADIS_PREFIX", "madis/raw").strip("/")
# default = the two small wind datasets (~0.6-1.2 MB/hr each: metar = KBOS/KBVY hourly,
# hfmetar = KBOS 5-min ASOS). LDAD/mesonet adds surrounding surface stations but is
# ~34 MB/hr CONUS-wide — add it explicitly + set an S3 lifecycle / subset it (see docs).
DATASETS = [d.strip() for d in os.environ.get("MADIS_DATASETS", "point/metar,LDAD/hfmetar").split(",") if d.strip()]
LOOKBACK = int(os.environ.get("MADIS_LOOKBACK_MIN", "180"))
VERIFY = os.environ.get("MADIS_VERIFY", "1") != "0"
AUTH = (os.environ["MADIS_USER"], os.environ["MADIS_PASS"]) if os.environ.get("MADIS_USER") else None

FNAME = re.compile(r"(\d{8})_(\d{4})\.gz")
s3 = boto3.client("s3")
sess = requests.Session()
if AUTH:
    sess.auth = AUTH
sess.mount("https://", HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1.0, status_forcelist=[500, 502, 503, 504])))


def _dir(ds, day):
    """Return (base_url, listing_html) for a dataset, trying both netCDF-case dirs."""
    root = f"{BASE}/data" + (f"/archive/{day[:4]}/{day[4:6]}/{day[6:8]}" if day else "")
    for nc in ("netCDF", "netcdf"):
        url = f"{root}/{ds}/{nc}/"
        try:
            r = sess.get(url, timeout=30, verify=VERIFY)
            if r.ok and FNAME.search(r.text):
                return url, r.text
        except requests.RequestException:
            pass
    return None, None


def _remote_size(url):
    try:
        r = sess.head(url, timeout=30, verify=VERIFY, allow_redirects=True)
        return int(r.headers.get("Content-Length", 0)) if r.ok else None
    except requests.RequestException:
        return None


def _have(key, size):
    try:
        h = s3.head_object(Bucket=BUCKET, Key=key)
        return bool(size) and h["ContentLength"] == size
    except Exception:
        return False


def pull(ds, day=None, dry=False):
    base_url, html = _dir(ds, day)
    if not base_url:
        print(f"[madis] {ds}: no listing (path/case/auth?)"); return
    now = dt.datetime.now(dt.timezone.utc)
    files = sorted(set(FNAME.findall(html)))
    got = skip = 0
    for ymd, hm in files:
        ts = dt.datetime.strptime(ymd + hm, "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc)
        if not day and (now - ts).total_seconds() > LOOKBACK * 60:
            continue
        fname = f"{ymd}_{hm}.gz"
        url = base_url + fname
        key = f"{PREFIX}/{ds}/{ymd[:4]}/{ymd[4:6]}/{ymd[6:8]}/{fname}"
        size = _remote_size(url)
        if _have(key, size):
            skip += 1; continue
        if dry:
            print(f"[madis]   would fetch {url}  ({size} B) -> s3://{BUCKET}/{key}")
            got += 1; continue
        try:
            r = sess.get(url, timeout=180, verify=VERIFY)
            if not r.ok:
                continue
            s3.put_object(Bucket=BUCKET, Key=key, Body=r.content, ContentType="application/gzip",
                          Metadata={"madis-src": url, "madis-valid": f"{ymd}T{hm}Z"})
            got += 1
        except requests.RequestException as e:
            print(f"[madis]   {fname}: {e}")
    print(f"[madis] {ds}{('@' + day) if day else ''}: {got} {'would-fetch' if dry else 'new'}, {skip} up-to-date, {len(files)} listed")


def main():
    args = sys.argv[1:]
    dry = "--dry" in args
    day = None
    if "--date" in args:
        day = args[args.index("--date") + 1].replace("-", "")
    for ds in DATASETS:
        pull(ds, day, dry)


if __name__ == "__main__":
    main()
