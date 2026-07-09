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
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daily_update as du


def main():
    dates = sys.argv[1:]
    if not dates:
        print("usage: backfill_race_days.py YYYY-MM-DD [YYYY-MM-DD ...]"); return
    os.makedirs(du.WORK, exist_ok=True)
    inval, ok_fields, ok_breeze = [], 0, 0
    for ds in dates:
        y = dt.date.fromisoformat(ds); ymd = ds.replace("-", "")
        du.log(f"=== {ds} ===")
        try:
            du.run([du.PY, "climatology/backfill_hrrr.py", "--date", ymd, "--out-dir", du.WORK])
            fpath = os.path.join(du.WORK, f"year={y:%Y}", f"month={y:%m}", f"{y:%d}.parquet")
            fkey = f"fields/year={y:%Y}/month={y:%m}/{y:%d}.parquet"
            if not os.path.exists(fpath):
                du.log(f"WARN no field parquet produced for {ds}"); continue
            du.put(fpath, fkey, "application/octet-stream", "max-age=86400")
            inval.append(f"/{du.PFX}/{fkey}"); ok_fields += 1
            try:
                du.run([du.PY, "climatology/merge_subh_day.py", "--date", ds])
            except Exception as e:
                du.log(f"subh 15-min unavailable for {ds} ({e}) — hourly")
            du.run([du.PY, "climatology/breeze_day.py", "--date", ds, "--out-dir", os.path.join(du.WORK, "breeze")])
            bpath = os.path.join(du.WORK, "breeze", f"{ds}.json")
            if os.path.exists(bpath):
                du.put(bpath, f"breeze/{ds}.json", "application/json", "max-age=300")
                inval.append(f"/{du.PFX}/breeze/{ds}.json"); ok_breeze += 1
        except Exception as e:
            du.log(f"FAIL {ds}: {e}")
    du.rebuild_field_index(); du.rebuild_breeze_index()
    inval += [f"/{du.PFX}/fields_index.json", f"/{du.PFX}/breeze_index.json"]
    r = du.cf.create_invalidation(DistributionId=du.DIST_ID,
        InvalidationBatch={"Paths": {"Quantity": len(inval), "Items": inval},
                           "CallerReference": f"racedays-{dates[0]}-{len(dates)}"})
    du.log(f"fields={ok_fields} breeze={ok_breeze}  invalidation {r['Invalidation']['Id']}")


if __name__ == "__main__":
    main()
