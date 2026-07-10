# MADIS obs puller — EC2 setup

`madis_pull.py` mirrors Boston-area MADIS surface obs (netCDF) to
`s3://sailframes-data-prod/madis/raw/…`. It must run on the **EC2 that owns the
registered Elastic IP `100.60.174.47`** so that egress matches the MADIS
allowlist (required for restricted data; public data works from anywhere).

## 0. Confirm egress is the EIP
On the instance:
```bash
curl -s https://checkip.amazonaws.com   # MUST print 100.60.174.47
```
If it prints anything else, the EIP isn't associated with this instance (or the
box egresses via a NAT that isn't the EIP) and MADIS restricted access will fail.

## 1. Install
```bash
sudo dnf install -y python3-pip            # Amazon Linux 2023 (or: apt install python3-pip)
sudo python3 -m pip install requests boto3
sudo install -D -m0755 madis_pull.py /opt/sailframes/madis_pull.py
```

## 2. IAM (instance role)
Attach this policy to the instance's role (no static keys on the box):
```json
{ "Version": "2012-10-17", "Statement": [
  { "Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"],
    "Resource": "arn:aws:s3:::sailframes-data-prod/madis/*" } ] }
```
(`GetObject` covers the HEAD dedup check; no `ListBucket` needed.)

## 3. Schedule (systemd timer, every 10 min)
`/etc/systemd/system/madis-pull.service`:
```ini
[Unit]
Description=MADIS Boston obs puller
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
EnvironmentFile=/etc/sailframes/madis.env
ExecStart=/usr/bin/python3 /opt/sailframes/madis_pull.py
User=ec2-user
```
`/etc/systemd/system/madis-pull.timer`:
```ini
[Unit]
Description=Run MADIS puller every 10 min
[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
```
`/etc/sailframes/madis.env` (chmod 600 — holds creds later):
```bash
MADIS_BUCKET=sailframes-data-prod
MADIS_LOOKBACK_MIN=180
# MADIS_DATASETS=point/metar,LDAD/hfmetar          # default (small, ~1.5 MB/hr)
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now madis-pull.timer
sudo systemctl start madis-pull.service   # run once now
journalctl -u madis-pull.service -n 20    # check output
```
(cron alt: `*/10 * * * * /usr/bin/env $(cat /etc/sailframes/madis.env|xargs) python3 /opt/sailframes/madis_pull.py >> /var/log/madis.log 2>&1`)

## 4. When the restricted account arrives
No code change — just add to `madis.env` and restart the timer:
```bash
MADIS_BASE=<restricted base URL from MADIS support>   # if different from madisPublic1
MADIS_USER=<login>
MADIS_PASS=<password>
MADIS_DATASETS=point/metar,LDAD/hfmetar,LDAD/mesonet,<restricted sets>
```
The puller sends HTTP basic auth when `MADIS_USER` is set and tries both netCDF-case
dirs. Confirm the restricted paths with `--dry` first.

## Datasets & storage
| Dataset | Content | Size | Notes |
|---|---|---|---|
| `point/metar` | hourly METAR (KBOS, KBVY, …) | ~0.6 MB/hr | default |
| `LDAD/hfmetar` | 5-min ASOS (KBOS high-freq wind) | ~0.6–1.2 MB/hr | default; best for onset timing |
| `LDAD/mesonet` | surrounding surface mesonet | **~34 MB/hr CONUS** | opt-in — see below |
| `point/maritime` | buoys | — | redundant with existing NDBC pull |

**Mesonet is CONUS-wide (~800 MB/day raw).** If you add it, either set an **S3
lifecycle** to expire `madis/raw/LDAD/mesonet/` after a few days, or (better) add a
parse step that subsets to a Boston bbox and drops the raw file.

## Next step (not built yet): parse → Boston obs
The raw files are gzipped netCDF (MADIS point format). A parser (netCDF4/xarray)
should subset to the Boston bbox (~lat 42.0–42.8, lon −71.3 to −70.4), pull
`windSpeed`/`windDir`/`temperature` with the QC flags (`windSpeedDD` etc.), and
write per-station parquet into the existing `climatology/obs/<id>/<year>.parquet`
shape so KBOS 5-min wind feeds the obs-validation + `/tactics` sea-breeze analysis.
