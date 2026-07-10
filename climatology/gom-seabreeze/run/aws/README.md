# Run gom-seabreeze on AWS

Single Spot EC2 → DTC `dtcenter/wps_wrf` container (WRF/WPS **4.3**, OpenMPI dmpar,
amd64) → `wrfout_d03` to S3. The domain is small (~2.2M grid points) so one
instance is plenty — no ParallelCluster.

```
launch.sh ──► EC2 (Spot, c7a.8xlarge) ──► userdata.sh
                                             ├ pull dtcenter/wps_wrf:latest
                                             ├ stage WPS_GEOG (S3 cache, else UCAR)
                                             ├ venv: requirements.txt (SST scripts)
                                             └ run_case.sh
                                                  ├ render namelists (driver --render-only)
                                                  ├ fetch GFS 0.25 (NOMADS)
                                                  ├ docker exec run_wps.ksh  → met_em
                                                  ├ HOST: build_coldest_sst + patch_met_em_sst
                                                  ├ docker exec run_wrf.ksh  → wrfout_d03
                                                  └ aws s3 cp → s3://…/gom/<date>/<mode>/
```

## One-time prereq — IAM instance profile
`launch.sh` attaches instance profile `gom-seabreeze-ec2`; its role needs:
`s3:GetObject/PutObject/ListBucket` on `sailframes-data-prod/gom/*` and
`ec2:TerminateInstances` (self-terminate). Create it once (or let me create it at
launch). Without it the instance can't read the code tarball or write output.

## Usage
```bash
AWS_PROFILE=sailframes ./launch.sh 2024-07-31 forecast
# watch:  aws ssm start-session --target <iid> ; tail -f /var/log/gom-userdata.log
# output: s3://sailframes-data-prod/gom/2024-07-31/forecast/ (wrfout_d0*, userdata.log, exit_code.txt)
```

## Cost (us-east-1, Spot)
- **First run ~$3–6**: c7a.8xlarge Spot (~$0.6/hr) × (geog download+cache ~0.5–1 h + WRF ~1–2 h). Geog is cached to S3, so it's one-time.
- **Later runs ~$1–3** each (geog pull from S3 ~5–10 min + WRF ~1–2 h).
- S3: ~30 GB geog cache (~$0.7/mo) + wrfout per day (a few GB at 15-min d03 output).

## VALIDATED end-to-end 2026-07-10 (2024-07-31 forecast)
Full chain ran on a c7a.8xlarge Spot in ~4 h (~$3, incl. one-time geog + shakedown):
provision → container → geog → GFS(archive) → WPS → real → wrf (32 ranks, no CFL) →
145 `wrfout_d03` 15-min frames → `s3://…/gom/2024-07-31/forecast/wrfout/`. The 9 shakedown
fixes are baked into `run_case.sh` + `wrf/namelist.input`; a rerun should reproduce it.

## Two remaining items — SCIENCE, not harness (this run used raw driver SST)
1. **Coldest-pixel SST** — `build_coldest_sst.py` pulls ACSPO from the **NRT** ERDDAP
   dataset, which lacks historical dates (2024 → zero-size). Find an archived /
   science-quality ACSPO L3S dataset id for hindcast/old dates. Until then `run_case.sh`
   runs on raw driver SST (SST step is non-fatal).
2. **`sst_update` block** — WRF 4.3's registry rejects an auxinput4 var, so `run_case.sh`
   strips the `sst_update`/auxinput4 block (static SST). Identify the offending var to
   restore time-varying SST. (Coldest-pixel injection into the static met_em still works
   once #1 is fixed — that's independent of `sst_update`.)

Once #1 is fixed, wrap `launch.sh` in a loop over dates for the hindcast climatology
(or move to AWS Batch + Spot for hundreds of days).
