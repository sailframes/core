#!/usr/bin/env python3
"""
backfill_race_days.py — generate the HRRR field parquet + Bernot breeze report for
specific dates (the fleet's race days) so race-breeze.html can match every race day
to a sea-breeze analysis, and so the race-page weather overlay has field data.

Reuses the daily-job helpers (put / rebuild indexes / invalidate). Winds are stored
earth-relative (backfill_hrrr/breeze_day rotate). subh 15-min is best-effort (the
wrfsubhf source is only retained a few days, so old dates stay hourly).

Run:  AWS_PROFILE=sailframes python3.11 climatology/backfill_race_days.py 2026-06-03 2026-06-10 ...
"""
import datetime as dt
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daily_update as du


def _run_t(cmd, timeout, tries=3):
    """Run a subprocess with a timeout + retries (hrrrzarr sockets occasionally
    stall with no timeout, hanging the whole batch)."""
    for i in range(tries):
        try:
            subprocess.run(cmd, check=True, timeout=timeout); return True
        except subprocess.TimeoutExpired:
            du.log(f"  timeout ({timeout}s) try {i+1}/{tries}: {' '.join(cmd[-3:])}")
        except subprocess.CalledProcessError as e:
            du.log(f"  error try {i+1}/{tries}: {e}")
    return False


def _one(ds):
    y = dt.date.fromisoformat(ds); ymd = ds.replace("-", "")
    du.log(f"=== {ds} ===")
    fpath = os.path.join(du.WORK, f"year={y:%Y}", f"month={y:%m}", f"{y:%d}.parquet")
    fkey = f"fields/year={y:%Y}/month={y:%m}/{y:%d}.parquet"
    got_f = got_b = False
    if not _run_t([du.PY, "climatology/backfill_hrrr.py", "--date", ymd, "--out-dir", du.WORK], 240):
        du.log(f"FAIL field {ds}"); return (ds, None, None)
    if os.path.exists(fpath):
        du.put(fpath, fkey, "application/octet-stream", "max-age=86400"); got_f = f"/{du.PFX}/{fkey}"
        _run_t([du.PY, "climatology/merge_subh_day.py", "--date", ds], 120, tries=1)   # best-effort 15-min
        if _run_t([du.PY, "climatology/breeze_day.py", "--date", ds, "--out-dir", os.path.join(du.WORK, "breeze")], 150):
            bpath = os.path.join(du.WORK, "breeze", f"{ds}.json")
            if os.path.exists(bpath):
                du.put(bpath, f"breeze/{ds}.json", "application/json", "max-age=300"); got_b = f"/{du.PFX}/breeze/{ds}.json"
    return (ds, got_f, got_b)


def main():
    dates = sys.argv[1:]
    if not dates:
        print("usage: backfill_race_days.py YYYY-MM-DD [YYYY-MM-DD ...]"); return
    os.makedirs(du.WORK, exist_ok=True)
    inval, ok_fields, ok_breeze = [], 0, 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for ds, gf, gb in ex.map(_one, dates):
            if gf: inval.append(gf); ok_fields += 1
            if gb: inval.append(gb); ok_breeze += 1
    du.rebuild_field_index(); du.rebuild_breeze_index()
    inval += [f"/{du.PFX}/fields_index.json", f"/{du.PFX}/breeze_index.json"]
    r = du.cf.create_invalidation(DistributionId=du.DIST_ID,
        InvalidationBatch={"Paths": {"Quantity": len(inval), "Items": inval},
                           "CallerReference": f"racedays-{dates[0]}-{len(dates)}"})
    du.log(f"fields={ok_fields} breeze={ok_breeze}  invalidation {r['Invalidation']['Id']}")


if __name__ == "__main__":
    main()
