#!/usr/bin/env python3
"""
run_gom_seabreeze.py  --  end-to-end driver
============================================
Chain a full run for one date + mode:

    render namelists (dates / num_metgrid_levels / grid_fdda)
      -> geogrid (once, cached)
      -> stage_driver: link GFS|ERA5 GRIB + Vtable -> ungrib
      -> metgrid                                    -> met_em.d0{1,2,3}.*
      -> sst/build_coldest_sst.py                   -> sst_<date>.nc
      -> sst/patch_met_em_sst.py                    -> met_em w/ cold SST
      -> real.exe
      -> wrf.exe                                    -> wrfout_d03

Mode switch (README "Run modes"):
    forecast : GFS 0.25 ICs/LBCs, num_metgrid_levels=34, &fdda OFF
    hindcast : ERA5 ICs/LBCs,     num_metgrid_levels=38, &fdda grid-nudge d01 ON

Orchestration only -- the science lives in sst/ and the namelists. Renders the
date/mode fields into copies under WPS_DIR / WRF_RUN and leaves the annotated
template namelists in wrf/ untouched. WPS/real/wrf are external binaries; each
step fails loudly and does not fall through.

Machine paths via env (or edit defaults): GOM_WPS, GOM_WRFRUN, GOM_WPSGEOG,
GOM_VTABLES (WPS ungrib/Variable_Tables), GOM_MPIRUN, GOM_PYTHON.

Examples:
    ./run_gom_seabreeze.py --date 2024-07-31 --mode forecast --grib-dir ~/gfs --fetch-gfs
    ./run_gom_seabreeze.py --date 2024-07-31 --mode hindcast --grib-dir ~/era5
    ./run_gom_seabreeze.py --date 2024-07-31 --dry          # print steps, render only
"""
import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                  # gom-seabreeze/
TEMPLATES = ROOT / "wrf"                             # annotated namelist templates
WPS_DIR = Path(os.environ.get("GOM_WPS", "./WPS")).resolve()
WRF_RUN = Path(os.environ.get("GOM_WRFRUN", "./run")).resolve()
SST_DIR = Path(os.environ.get("GOM_SST", "./sst_out")).resolve()
WPSGEOG = os.environ.get("GOM_WPSGEOG", "/path/to/WPS_GEOG")
VTABLES = os.environ.get("GOM_VTABLES", str(WPS_DIR / "ungrib" / "Variable_Tables"))
MPIRUN = os.environ.get("GOM_MPIRUN", "mpirun -np 4")
PY = os.environ.get("GOM_PYTHON", sys.executable)

# Per-mode driver config. NOTE (verify against the WPS registry before a real run):
#   ERA5 pressure-level = 38 metgrid levels (37 pl + sfc); Vtable.ERA5 is the
#   correct table -- Vtable.ERA-interim.pl substitutes but its soil/land entries
#   differ. GFS 0.25 = 34 levels, Vtable.GFS.
MODE_CFG = {
    "forecast": dict(num_metgrid_levels=34, num_metgrid_soil_levels=4, interval_seconds=10800, fdda=False, vtable="Vtable.GFS"),
    "hindcast": dict(num_metgrid_levels=38, num_metgrid_soil_levels=4, interval_seconds=3600,  fdda=True,  vtable="Vtable.ERA5"),
    # HRRR-driven: obs-anchored mean (HRRR assimilates radar/surface/buoy). 40 isobaric
    # levels + surface = 41; HRRR carries RUC 9-level soil (real.exe interpolates to Noah's
    # 4). Hourly F00 analyses (interval 3600). Vtable.raphrrr ships with WPS.
    "hrrr":     dict(num_metgrid_levels=41, num_metgrid_soil_levels=9, interval_seconds=3600,  fdda=False, vtable="Vtable.raphrrr"),
}

# Static-geography detail (land-use / land-WATER mask). The mask sets WHERE the
# coast sits at grid scale -- the #1 geography lever for the sea breeze (topo is
# secondary: Cape Ann/Marblehead relief is <30 m). See wrf/README.md "geography".
#   modis  default 30s MODIS (21 cats) -- byte-identical to the validated 2024 run.
#   nlcd   NLCD 2011 9s (~250 m, 40 cats) on ALL domains via '+default' fallback
#          for a sharp Salem Sound / harbor-island coastline + Boston urban class.
# num_land_cat is a single global -> MMINLU must be consistent across the nest, so
# NLCD is applied to every domain (d01 offshore/Canada falls back to MODIS-default,
# remapped into NLCD40 space). Requires nlcd2011_ll_9s in WPS_GEOG + an NLCD40
# section in the image's VEGPARM.TBL (run_case.sh checks both before the full run).
# LSM COUPLING: WRF hard-forbids NLCD40 land use with RUC LSM (sf_surface_physics=3,
# the RU-WRF default) -- real.exe FATALs "NLCD40 data may not be used with RUCLSMSCHEME"
# (verified 2026-07-12 via rsl.error.0000). NLCD40 is only allowed with SLAB/Noah(2)/
# Pleim-Xiu(7) (Noah-MP=4 is also forbidden). So the nlcd path also switches RUC->Noah
# (sf_surface_physics=2, num_soil_layers 9->4; Noah is lower-friction with GFS's 4 soil
# levels and pairs fine with the MYNN sfclay/PBL). modis keeps the validated RUC recipe.
GEOG_CFG = {
    "modis": dict(geog_data_res="'30s', '30s', '30s',", num_land_cat=21,
                  sf_surface_physics="3, 3, 3,", num_soil_layers=9),   # RUC (RU-WRF)
    "nlcd":  dict(geog_data_res="'nlcd2011_9s+default', 'nlcd2011_9s+default', 'nlcd2011_9s+default',",
                  num_land_cat=40,
                  sf_surface_physics="2, 2, 2,", num_soil_layers=4),   # Noah (NLCD40-compatible)
}
DRY = False


def sh(cmd, cwd=None):
    print(f"$ {cmd}   (cwd={cwd or os.getcwd()})")
    if DRY:
        return
    r = subprocess.run(cmd, shell=True, cwd=cwd)
    if r.returncode != 0:
        sys.exit(f"FAILED ({r.returncode}): {cmd}")


# ---- namelist rendering (targeted; preserves the templates' comments) ----------
def set_nml(text, key, value):
    """Replace the value of `key = ...` on its line, keeping any trailing !comment."""
    pat = re.compile(rf"^(?P<pre>\s*{re.escape(key)}\s*=[ \t]*)(?P<val>[^!\n]*?)(?P<pad>[ \t]*)(?P<cmt>!.*)?$", re.M)
    def repl(m):
        cmt = m.group("cmt")
        return f"{m.group('pre')}{value}" + (f"   {cmt}" if cmt else "")
    out, n = pat.subn(repl, text)
    if n == 0:
        sys.exit(f"namelist key not found: {key}")
    return out


def uncomment_keys(text, keys):
    """Strip a leading '! ' from lines assigning any of `keys` (enable &fdda params)."""
    for k in keys:
        text = re.sub(rf"^(\s*)!\s?(\s*{re.escape(k)}\s*=)", r"\1 \2", text, flags=re.M)
    return text


def inject_before_group_end(text, group, block):
    """Insert `block` just before the closing '/' of the &group namelist group.
    Anchor the header at line-start so a comment mentioning '&group' can't false-match."""
    gm = re.search(rf"^&{group}\b", text, re.M)
    if not gm:
        sys.exit(f"namelist group not found: &{group}")
    m = re.search(r"^\s*/\s*$", text[gm.start():], re.M)
    if not m:
        sys.exit(f"no closing / for &{group}")
    pos = gm.start() + m.start()
    return text[:pos] + block + text[pos:]


# Observation (station) nudging -- injected into &fdda when --obs-nudge is set. OBS_DOMAIN101
# is produced by obsgrid.exe from little_r (obs/fetch_obs_littler.py) and copied to d02/d03
# (WRF ignores out-of-domain obs). Params per WRF Obs-Nudging Guide (Reen 2016): dynamic
# ANALYSIS across the whole run (obs_idynin=0, fdda_end past run end), radius of influence
# shrinks on the finer nests, surface-obs earth-relative winds rotated by obsgrid.
FDDA_OBS_BLOCK = """\
 ! --- observation (station) nudging: buoys 44013/44029 + Castle Is + BOS/BVY/PVC ---
 obs_nudge_opt            = {doms},
 max_obs                  = 100000,
 fdda_start               = 0., 0., 0.,
 fdda_end                 = {end_min}., {end_min}., {end_min}.,
 obs_nudge_wind           = 1, 1, 1,
 obs_coef_wind            = 6.e-4, 6.e-4, 6.e-4,
 obs_nudge_temp           = 1, 1, 1,
 obs_coef_temp            = 6.e-4, 6.e-4, 6.e-4,
 obs_nudge_mois           = 1, 1, 1,
 obs_coef_mois            = 6.e-4, 6.e-4, 6.e-4,
 obs_nudge_pstr           = 0, 0, 0,
 ! surface-obs vertical spreading: 0 = Reen (2013), needed because MYNN sfclay (option 5)
 ! doesn't set the MM5 stability 'regime' the original scheme (=1, default) requires ->
 ! else fddaobs_driver compute_VIH aborts "Unknown regime type 0.0"
 obs_sfc_scheme_vert      = 0,
 obs_rinxy                = 60., 40., 20.,
 obs_rinsig               = 0.1,
 obs_twindo               = 0.6666667,
 obs_npfi                 = 10,
 obs_ionf                 = 2, 2, 2,
 obs_idynin               = 0,
 obs_dtramp               = 40.,
 obs_prt_freq             = 10, 10, 10,
 obs_ipf_errob            = .true.,
 obs_ipf_nudob            = .true.,
 obs_ipf_in4dob           = .true.,
 obs_ipf_init             = .true.,
"""
# WRF must re-check OBS_DOMAIN each step -> auxinput11_interval=1 (guide warns off the _s form).
AUX11_BLOCK = """\
 ! --- obs-nudging input stream (check for OBS_DOMAIN each step) ---
 auxinput11_interval      = 1, 1, 1,
 auxinput11_end_h         = {end_h}, {end_h}, {end_h},
"""


def render_namelists(date, mode, start_hour, run_hours, geog_detail="modis", obs_nudge=False,
                     obs_nudge_doms="1, 1, 1", les=False, les_d04_hour=14, les_d05_hour=15):
    cfg = MODE_CFG[mode]
    gcfg = GEOG_CFG[geog_detail]
    y, m, d = map(int, date.split("-"))
    start = dt.datetime(y, m, d, start_hour)
    end = start + dt.timedelta(hours=run_hours)
    triple = lambda v: f"{v}, {v}, {v},"
    if les:
        # 5-domain LES render (namelist.*.les): parents run [start,end]; LES nests d04/d05
        # start later (staggered late-start) and end with the parents. Uses the template's
        # RUC/modis physics defaults (30s geog); nlcd on 5 LES domains is a later refinement.
        quint = lambda v: f"{v}, {v}, {v}, {v}, {v},"
        wps = (TEMPLATES / "namelist.wps.les").read_text()
        wps = set_nml(wps, "start_date", quint(f"'{start:%Y-%m-%d_%H:%M:%S}'"))
        wps = set_nml(wps, "end_date", quint(f"'{end:%Y-%m-%d_%H:%M:%S}'"))
        wps = set_nml(wps, "interval_seconds", f"{cfg['interval_seconds']},")
        wps = set_nml(wps, "geog_data_path", f"'{WPSGEOG}'")
        WPS_DIR.mkdir(parents=True, exist_ok=True)
        (WPS_DIR / "namelist.wps").write_text(wps)

        inp = (TEMPLATES / "namelist.input.les").read_text()
        inp = set_nml(inp, "run_hours", f"{run_hours},")
        inp = set_nml(inp, "start_year", quint(f"{start:%Y}"))
        inp = set_nml(inp, "start_month", quint(f"{start:%m}"))
        inp = set_nml(inp, "start_day", quint(f"{start:%d}"))
        inp = set_nml(inp, "start_hour",
                      f"{start:%H}, {start:%H}, {start:%H}, {les_d04_hour:02d}, {les_d05_hour:02d},")
        inp = set_nml(inp, "end_year", quint(f"{end:%Y}"))
        inp = set_nml(inp, "end_month", quint(f"{end:%m}"))
        inp = set_nml(inp, "end_day", quint(f"{end:%d}"))
        inp = set_nml(inp, "end_hour", quint(f"{end:%H}"))
        inp = set_nml(inp, "interval_seconds", f"{cfg['interval_seconds']},")
        inp = set_nml(inp, "num_metgrid_levels", f"{cfg['num_metgrid_levels']},")
        inp = set_nml(inp, "num_metgrid_soil_levels", f"{cfg['num_metgrid_soil_levels']},")
        inp = set_nml(inp, "auxinput4_end_h", f"{run_hours},")
        WRF_RUN.mkdir(parents=True, exist_ok=True)
        (WRF_RUN / "namelist.input").write_text(inp)
        print(f"  rendered LES namelists: {start:%Y-%m-%d_%H}Z +{run_hours}h  "
              f"d04@{les_d04_hour:02d}Z d05@{les_d05_hour:02d}Z  levels={cfg['num_metgrid_levels']}  "
              f"5 domains (9/3/1km/333m/111m)")
        return

    # --- namelist.wps ---
    wps = (TEMPLATES / "namelist.wps").read_text()
    wps = set_nml(wps, "start_date", triple(f"'{start:%Y-%m-%d_%H:%M:%S}'"))
    wps = set_nml(wps, "end_date",   triple(f"'{end:%Y-%m-%d_%H:%M:%S}'"))
    wps = set_nml(wps, "interval_seconds", f"{cfg['interval_seconds']},")
    wps = set_nml(wps, "geog_data_res", gcfg["geog_data_res"])
    wps = set_nml(wps, "geog_data_path", f"'{WPSGEOG}'")
    WPS_DIR.mkdir(parents=True, exist_ok=True)
    (WPS_DIR / "namelist.wps").write_text(wps)

    # --- namelist.input ---
    inp = (TEMPLATES / "namelist.input").read_text()
    inp = set_nml(inp, "run_hours", f"{run_hours},")
    inp = set_nml(inp, "start_year", triple(f"{start:%Y}"))
    inp = set_nml(inp, "start_month", triple(f"{start:%m}"))
    inp = set_nml(inp, "start_day", triple(f"{start:%d}"))
    inp = set_nml(inp, "start_hour", triple(f"{start:%H}"))
    inp = set_nml(inp, "end_year", triple(f"{end:%Y}"))
    inp = set_nml(inp, "end_month", triple(f"{end:%m}"))
    inp = set_nml(inp, "end_day", triple(f"{end:%d}"))
    inp = set_nml(inp, "end_hour", triple(f"{end:%H}"))
    inp = set_nml(inp, "interval_seconds", f"{cfg['interval_seconds']},")
    inp = set_nml(inp, "num_metgrid_levels", f"{cfg['num_metgrid_levels']},")
    inp = set_nml(inp, "num_metgrid_soil_levels", f"{cfg['num_metgrid_soil_levels']},")
    inp = set_nml(inp, "num_land_cat", f"{gcfg['num_land_cat']},")
    inp = set_nml(inp, "sf_surface_physics", gcfg["sf_surface_physics"])
    inp = set_nml(inp, "num_soil_layers", f"{gcfg['num_soil_layers']},")
    inp = set_nml(inp, "auxinput4_end_h", f"{run_hours},")
    if cfg["fdda"]:
        inp = set_nml(inp, "grid_fdda", "1, 0, 0,")
        inp = uncomment_keys(inp, ["gfdda_inname", "grid_sfdda", "fgdt",
                                   "if_no_pbl_nudging_uv", "if_no_pbl_nudging_t", "if_no_pbl_nudging_q",
                                   "k_zfac_uv", "k_zfac_t", "k_zfac_q"])
        print("  NOTE hindcast: grid (analysis) nudging on d01 requires wrffdda_d01 staged from ERA5 "
              "(obsgrid/real) + gfdda_interval_m/gfdda_end_h set in the template — verify before running.")
    else:
        inp = set_nml(inp, "grid_fdda", "0, 0, 0,")
    if obs_nudge:
        # obs-nudging's compute_VIH needs the MM5 stability 'regime', set ONLY by the revised
        # MM5 MOST surface layer (sf_sfclay_physics=1) + its paired YSU PBL (bl_pbl_physics=1).
        # The default MYNN (sfclay=5/pbl=5) never sets it -> "Unknown regime type 0.0" abort.
        # Swap to YSU+MOST for nudged runs (standard convective/sea-breeze combo); MYNN stays
        # the default for free runs. NOTE: a free-vs-nudged comparison must use YSU on BOTH.
        inp = set_nml(inp, "bl_pbl_physics", "1, 1, 1,")
        inp = set_nml(inp, "sf_sfclay_physics", "1, 1, 1,")
        inp = inject_before_group_end(inp, "time_control", AUX11_BLOCK.format(end_h=run_hours))
        inp = inject_before_group_end(inp, "fdda",
                                      FDDA_OBS_BLOCK.format(end_min=run_hours * 60, doms=obs_nudge_doms))
    WRF_RUN.mkdir(parents=True, exist_ok=True)
    (WRF_RUN / "namelist.input").write_text(inp)
    print(f"  rendered namelists: {start:%Y-%m-%d_%H} +{run_hours}h  "
          f"levels={cfg['num_metgrid_levels']}  fdda={'on' if cfg['fdda'] else 'off'}  "
          f"obs_nudge={'on' if obs_nudge else 'off'}  "
          f"geog={geog_detail}(num_land_cat={gcfg['num_land_cat']})")


# ---- driver staging (ungrib) ---------------------------------------------------
def _alpha(n):                                       # 0->AAA, 1->AAB, ...  (link_grib suffixes)
    a = ""
    for _ in range(3):
        a = chr(ord("A") + n % 26) + a; n //= 26
    return a


def link_grib(paths):
    for f in WPS_DIR.glob("GRIBFILE.*"):
        f.unlink()
    for i, p in enumerate(sorted(paths)):
        link = WPS_DIR / f"GRIBFILE.{_alpha(i)}"
        print(f"  link GRIBFILE.{_alpha(i)} -> {p}")
        if not DRY:
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(Path(p).resolve())
    return len(paths)


def fetch_gfs(date, cycle, fhours, out):
    """Pull GFS 0.25 f000..fNNN for one cycle from NOMADS into `out` (forecast mode)."""
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    ymd = date.replace("-", "")
    base = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
    got = []
    for fh in fhours:
        q = (f"?dir=%2Fgfs.{ymd}%2F{cycle:02d}%2Fatmos&file=gfs.t{cycle:02d}z.pgrb2.0p25.f{fh:03d}"
             "&all_lev=on&all_var=on")
        dest = out / f"gfs.t{cycle:02d}z.f{fh:03d}.grib2"
        print(f"  fetch GFS f{fh:03d} -> {dest}")
        if DRY:
            got.append(dest); continue
        try:
            urllib.request.urlretrieve(base + q, dest)
            got.append(dest)
        except Exception as e:
            print(f"    ! GFS f{fh:03d} failed: {e}")
    return got


def stage_driver(date, mode, grib_dir, fetch, run_hours, cycle):
    cfg = MODE_CFG[mode]
    if fetch:
        if mode != "forecast":
            sys.exit("--fetch-gfs is forecast-mode only; stage ERA5 into --grib-dir (cdsapi).")
        fhours = list(range(0, run_hours + 1, cfg["interval_seconds"] // 3600))
        paths = fetch_gfs(date, cycle, fhours, grib_dir)
    else:
        if not grib_dir:
            sys.exit("provide --grib-dir with the staged driver GRIB (or --fetch-gfs for GFS).")
        paths = sorted(Path(grib_dir).glob("*"))
        if not paths:
            sys.exit(f"no GRIB files in {grib_dir}")
    n = link_grib(paths)
    vtab = Path(VTABLES) / cfg["vtable"]
    sh(f"ln -sf {vtab} Vtable", cwd=WPS_DIR)
    print(f"  Vtable -> {vtab}  ({n} GRIB linked)")
    sh("./ungrib.exe", cwd=WPS_DIR)


def main():
    global DRY
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--mode", choices=list(MODE_CFG), default="forecast")
    ap.add_argument("--geog-detail", choices=list(GEOG_CFG), default="modis",
                    help="static land-use/coastline detail: modis (30s, default) or nlcd (9s ~250m + urban)")
    ap.add_argument("--start-hour", type=int, default=0, help="run start hour UTC (default 00)")
    ap.add_argument("--run-hours", type=int, default=36, help="forecast length (default 36)")
    ap.add_argument("--grib-dir", help="dir of staged driver GRIB (GFS or ERA5)")
    ap.add_argument("--fetch-gfs", action="store_true", help="download GFS 0.25 from NOMADS (forecast)")
    ap.add_argument("--gfs-cycle", type=int, default=0, help="GFS cycle hour for --fetch-gfs (default 00z)")
    ap.add_argument("--anchor", action="store_true", help="pass --anchor to build_coldest_sst")
    ap.add_argument("--skip-geogrid", action="store_true", help="reuse cached geo_em (static domains)")
    ap.add_argument("--dry", action="store_true", help="render namelists + print steps, run nothing external")
    ap.add_argument("--render-only", action="store_true", help="render namelists into WPS/WRF dirs then exit (for the AWS container runner)")
    ap.add_argument("--obs-nudge", action="store_true", help="enable observation (station) nudging &fdda block (needs OBS_DOMAIN from obsgrid)")
    ap.add_argument("--obs-nudge-doms", default="1, 1, 1",
                    help="per-domain obs_nudge_opt (default '1, 1, 1'; '1, 1, 0' = d01/d02 only for "
                         "held-out validation; '0, 0, 0' = YSU physics-matched free baseline)")
    ap.add_argument("--les", action="store_true",
                    help="5-domain LES mode: add d04 333m + d05 111m nests (namelist.*.les) for gusts")
    ap.add_argument("--les-d04-hour", type=int, default=14, help="LES d04 start hour UTC (default 14)")
    ap.add_argument("--les-d05-hour", type=int, default=15, help="LES d05 start hour UTC (default 15)")
    args = ap.parse_args()
    DRY = args.dry

    print(f"=== gom-seabreeze: {args.date}  mode={args.mode}  {args.run_hours}h{' LES' if args.les else ''} ===")
    render_namelists(args.date, args.mode, args.start_hour, args.run_hours, args.geog_detail,
                     args.obs_nudge, args.obs_nudge_doms, args.les, args.les_d04_hour, args.les_d05_hour)
    if args.render_only:
        return

    geo_ok = (WPS_DIR / "geo_em.d01.nc").exists()
    if args.skip_geogrid or geo_ok:
        print(f"  geogrid: {'skipped (--skip-geogrid)' if args.skip_geogrid else 'cached geo_em present'}")
    else:
        sh("./geogrid.exe", cwd=WPS_DIR)

    stage_driver(args.date, args.mode, args.grib_dir, args.fetch_gfs, args.run_hours, args.gfs_cycle)
    sh("./metgrid.exe", cwd=WPS_DIR)

    # SST lower boundary (coldest-pixel composite) + injection into met_em
    a = " --anchor" if args.anchor else ""
    sh(f"{PY} {ROOT}/sst/build_coldest_sst.py --date {args.date} --outdir {SST_DIR}{a} --plot")
    sh(f"{PY} {ROOT}/sst/patch_met_em_sst.py --met-dir {WPS_DIR} --composite {SST_DIR} --domains 1 2 3 --plot")

    # real + wrf (real must see the patched SST in met_em)
    sh("cp namelist.input real_namelist.bak 2>/dev/null; true", cwd=WRF_RUN)
    sh("./real.exe", cwd=WRF_RUN)
    sh(f"{MPIRUN} ./wrf.exe", cwd=WRF_RUN)
    print(f"=== done: wrfout_d03 in {WRF_RUN} ===")


if __name__ == "__main__":
    main()
