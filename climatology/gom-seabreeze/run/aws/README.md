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

## First run is a SHAKEDOWN — verify these live (unrunnable off-AWS)
1. **`set_env.ksh` vars** — `run_case.sh` sets `WPS_VERSION/WRF_VERSION/input_data/
   case_name/num_procs`. Reconcile against the DTC tutorial's `set_env.ksh`; add any
   other vars `run_wps.ksh`/`run_wrf.ksh` reference.
2. **met_em hand-off** — confirm `run_wrf.ksh` links `met_em` from `/home/wpsprd`
   (bind-mounted, so the host SST patch is visible). If it expects them in `wrfprd`,
   add a link step between WPS and WRF.
3. **geog fields** — `geog_data_res='30s'` needs the 30 s MODIS landuse + topo in the
   mandatory geog; if geogrid errors on a missing field, pull the specific
   `geog_<field>` tarball.
4. **GFS levels** — namelist `num_metgrid_levels=34` must match the GFS pgrb2 0p25
   level count metgrid reports; adjust if metgrid complains.
5. **Nest placement** — `plotgrids`/first geo_em: confirm d03 covers Salem Sound →
   Cape Cod Bay before trusting a run.

Once the shakedown passes, wrap `launch.sh` in a loop over dates for the hindcast
climatology (or move to AWS Batch + Spot if you run hundreds of days).
