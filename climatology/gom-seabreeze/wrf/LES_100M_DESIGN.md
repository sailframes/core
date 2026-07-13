# LES ~100 m Nest Design — gom-seabreeze gust/oscillation forecasting

**Status:** design only. Nothing launched. WRF-ARW 4.8.0 (DTC-derived container).
**Goal:** resolve sea-breeze **gusts** and **wind oscillations / rolls** over the EYC
race course (~42.46 N, −70.77 W, off Marblehead) for **2026-07-04, 17:00–19:00 UTC**
(13:00–15:00 ET).

This adds two one-way-nested LES domains (d04, d05) below the existing 1 km d03.
The parent chain (d01 9 km → d02 3 km → d03 1 km) is **unchanged** and keeps its
mesoscale MYNN-PBL recipe. Only d04/d05 run LES-mode physics.

> **Doctrine reminder (CLAUDE.md #3):** 1 km ⇒ ~6–7 km effective resolution → d03 is
> the *front*, not gusts. Sub-km is legitimate ONLY with LES-mode physics (3D TKE,
> PBL scheme off). That is exactly what d04/d05 do. This design does not relitigate
> the 1 km recipe; it hangs an LES tile beneath it.

All option **ranks** (per-domain array vs global scalar) and several LES-specific
options are flagged **VERIFY@runtime** against `Registry.EM_COMMON` and the WRF 4.8
User's Guide in the container. Reason below is from WRF registry/doc knowledge; do
not trust a value I've marked VERIFY without checking the source in
`/comsoftware/wrf/WRF-4.8.0`.

---

## 1. Nesting plan (1 km → ~100 m)

Chosen ladder: **1 km → 333 m → 111 m**, two 3:1 nests. Rationale over 1 km→200 m→100 m:

- 3:1 is WRF's sweet spot (≤5:1 is allowed but 3:1 is the well-tested, low-noise ratio,
  same as the existing d01→d02→d03 chain).
- A single 1 km→200 m→100 m uses a 5:1 then 2:1; the 2:1 is unusual and the 5:1 puts
  more spectral energy across one interface. 3:1/3:1 keeps interfaces gentle.
- 111 m is close enough to the "~100 m" target; nobody resolves a gust better at 100
  vs 111 m — the binding constraint is geography and BL depth (§6), not 11 m of Δx.

### Hard WRF grid constraint

Every nest dimension must be **`parent_grid_ratio·n + 1`** (whole parent cells).
For 3:1 nests: `e_we`, `e_sn` ∈ {…, 31, 34, 37, …, 3n+1}. All values below satisfy this.

### Grid geometry

| Dom | Role | ratio | dx | e_we × e_sn | span (km) | PBL |
|-----|------|-------|------|-------------|-----------|-----|
| d01 | GoM synoptic | 1 | 9000 | 100 × 100 | ~900 × 900 | MYNN (5) |
| d02 | New England | 3 | 3000 | 136 × 136 | ~405 × 405 | MYNN (5) |
| d03 | Mass Bay / front | 3 | 1000 | 82 × 76 | ~81 × 75 | MYNN (5) |
| **d04** | **LES coarse** | **3** | **333.3** | **112 × 112** | **~37 × 37** | **LES (0)** |
| **d05** | **LES fine** | **3** | **111.1** | **136 × 136** | **~15 × 15** | **LES (0)** |

- **d04** (333 m, 112×112 = 111 parent-free cells → ~37 km box): a generous buffer
  tile so the LES has fetch to spin up turbulence upstream of the course, and so d05
  sits well inside it (away from d04's relaxation zone).
- **d05** (111 m, 136×136 → ~15 km box): the deliverable domain, centered on the
  course. 15 km covers the whole EYC race area (start box + windward/leeward marks +
  the upwind fetch that sets the oscillation) with margin to the relaxation zone.

> ⚠️ **d04-as-specced (37 km N–S) will NOT fit inside the current d03** — this is not
> conditional. d03 spans lat 41.91–42.60 (~77 km N–S per the wps comment); the course
> at 42.46 N sits only ~15 km from d03's **north** edge. A 37 km d04 centered on the
> course needs ~18.5 km northward and overruns d03's boundary by ~3.5 km *before* the
> 5-cell relax buffer. **Two escapes (pick before rendering):**
> - **(i) Extend d03 north** (~+15 km on d03 `e_sn`, adjust its `j_parent_start`) —
>   **preferred**, keeps d04 big enough to give d05 fetch. This changes d03's grid, so
>   re-run geogrid and re-verify the 1 km front against KBOX if you care about that
>   product (northward extension is over water, low risk).
> - **(ii) Shrink d04 to ~24 km** (73×73, since 72 = 3×24) — fits inside today's d03
>   but leaves only a tight margin around d05 and less spin-up fetch. Fallback only.

Sizes are chosen so the **cell counts stay modest** (d04 ≈ 12.5k columns, d05 ≈ 18.5k
columns) — the LES cost is dominated by the tiny time step (§3), not the horizontal
extent, so a small box is the right call.

### Placement — METHOD, not fabricated indices

⚠️ **Do NOT hard-code the `i/j_parent_start` below without plotgrids.exe.** The current
d03 was centered on the **Mark1→Mark2 corridor**, not the EYC course. The EYC course
center (42.46 N, −70.77 W) lands in the **northern part of d03** (near Mark1, SE of
Marblehead Neck), roughly i≈17, j≈60 of the 82×76 grid — **arithmetic to be confirmed
with plotgrids.exe.** Because that is near d03's north edge, the LES box must be
checked to **clear d03's relaxation zone** (`relax_zone=4` + `spec_zone=1` = 5 cells
= ~5 km buffer) plus a few-Δx working margin. If it crowds the d03 boundary, extend
d03 north (bump `e_sn`, move `j_parent_start` of d03) rather than jam the LES against
the boundary.

Procedure to fix the indices:

1. Add d04/d05 to `namelist.wps` with placeholder starts, run `geogrid`/`plotgrids.exe`.
2. Confirm d05 is centered on 42.46 N/−70.77 W and its edges clear d04's relaxation
   zone by ≥5 cells; confirm d04 clears d03's relaxation zone by ≥5 cells.
3. Read the resulting `i/j_parent_start` off plotgrids and write them into **both**
   namelists (must match exactly).

**Placeholder starts** (illustrative — REPLACE after plotgrids):

```
                    d03      d04(in d03)   d05(in d04)
i_parent_start  =    1,  28,  55,   10,   40,
j_parent_start  =    1,  30,  37,   45,   40,
```

The d04 start (i=10,j=45 in d03) puts a ~37 km tile over northern Mass Bay/Salem
Sound; d05 start (i=40,j=40 in d04) centers the 15 km fine tile on the course.
**These are guesses to be replaced by plotgrids output.**

One-way nesting: `feedback = 0` (already set), `smooth_option = 0`.

---

## 2. LES physics (per-domain)

Parents (d01–d03) keep the mesoscale recipe **unchanged**: MYNN PBL (`bl_pbl_physics=5`),
MYNN surface layer (`sf_sfclay_physics=5`), `km_opt=4` (2D horizontal Smagorinsky /
1D-column vertical mixing handled by the PBL scheme). d04/d05 switch to LES.

### Core LES switches

| Option | d01,d02,d03 | d04,d05 | rank | Notes |
|--------|-------------|---------|------|-------|
| `bl_pbl_physics` | 5,5,5 | **0,0** | array | **PBL OFF on LES** — turbulence is resolved + SFS-modeled, not parameterized. |
| `sf_sfclay_physics` | 5,5,5 | **1,1** | array | LES needs a surface layer for fluxes. **MM5 MOST (=1)** is the standard LES pairing. (MYNN sfclay=5 assumes a PBL scheme; do not carry it into LES. — VERIFY 5 is not required-with-something on LES.) |
| `sf_surface_physics` | (RUC 3 or Noah 2) | same as parent | array | LSM unchanged; LES still needs land fluxes. Keep whatever the geog mode set. |
| `bl_mynn_tkeadvect`, `bl_mynn_edmf`, `icloud_bl` | on | inert on d04/d05 | — | MYNN-specific; ignored where PBL=0. Leave the arrays alone (values on d04/d05 are unused). |
| `diff_opt` | 2,2,2 | **2,2** | array | Full 3D diffusion in physical space (required for LES). |
| `km_opt` | 4,4,4 | **2,2** | array | **2 = 3D 1.5-order TKE (prognostic SFS TKE)** — the recommended LES closure. (`km_opt=3` = 3D Smagorinsky is the simpler alternative; TKE=2 preferred for the marine BL where Smagorinsky over-damps. — see gray-zone note.) |
| `sfs_opt` | 0,0,0 | **1,1** | array | **Nonlinear Backscatter and Anisotropy (NBA)** SFS stress. Improves resolved-scale energy in LES. `1`=NBA1, `2`=NBA2. VERIFY sfs_opt is an array and NBA is compiled in 4.8. |
| `isfflx` | 1 | 1 | **scalar** | Use surface fluxes from the sfclay/LSM. **Global** — do NOT arrayize. |
| `mix_isotropic` | 0 | **1** | VERIFY rank | Isotropic (3D) mixing for LES vs the default anisotropic mesoscale mixing. VERIFY whether this is a scalar or array in 4.8 registry. |
| `m_opt` | 0 | **1** | VERIFY rank | Adds the Mij (deformation) diagnostic output; optional but useful to inspect resolved stress. VERIFY rank. |
| `mix_full_fields` | .true. | .true. | scalar | Diffuse full fields (not perturbation) — appropriate for LES with real topography. Global scalar. |
| `c_s` / `c_k` | (default) | (default) | VERIFY rank | Smagorinsky / TKE constants. Defaults (~0.18 / ~0.10) are fine; only touch if diagnosing over/under-mixing. VERIFY these are per-domain arrays if you set them. |
| `non_hydrostatic` | .true.×3 | .true.,.true. | array | Already true; mandatory for LES. |
| `diff_6th_opt` | 0,0,0 | 0,0 (or 2,2) | array | Optional 6th-order hyperdiffusion to kill 2Δx noise. Consider `2` on d05 with a small factor (0.12) if the LES shows grid-scale checkerboarding; start at 0. |
| `epssm` | 0.2,0.2,0.5 | **0.5,0.5** | array | Off-centering for vertical acoustic stability on the fine, tall-CFL nests (as d03 already uses 0.5). |

### Gray-zone decision (explicit — this is a deliverable)

d04 at **333 m** sits in the turbulence gray zone (Wyngaard 2004 "terra incognita"):
too fine for a mesoscale PBL scheme, too coarse for clean LES (the energy-containing
eddies are only marginally resolved, especially over the cool water where the BL is
shallow). Two defensible choices:

- **(a) Treat d04 as LES** (`bl_pbl_physics=0`, `km_opt=2`), accepting under-resolved
  large eddies on d04 but giving d05 a turbulent (not smooth-PBL) parent to inherit
  from. **← recommended**, for turbulence continuity into d05 and simplicity.
- **(b) Scale-aware PBL on d04** (`bl_pbl_physics=11`, Shin–Hong), LES only on d05.
  More defensible *for d04's own fields*, but hands d05 a smooth parent → d05 must
  regrow all its turbulence from scratch (longer spin-up, §6).

This design uses **(a)**: `bl_pbl_physics = 5,5,5,0,0`, `km_opt = 4,4,4,2,2`. Tradeoff
stated: d04's 333 m fields are themselves gray-zone and should be read as a
turbulence-generating buffer, **not** a trustworthy product. Only **d05 (111 m)** is
the deliverable.

---

## 3. Time step (LES CFL)

LES at 111 m needs dt ~0.5–1 s (advective CFL with 10–15 m/s gust cores + resolved
vertical velocities in convective rolls). We refine dt **more aggressively than the
grid ratio** on the inner nests — WRF allows `parent_time_step_ratio ≠ parent_grid_ratio`.

Keep the parent top-level `time_step = 45` s (unchanged; 9 km CFL headroom). Then:

```
parent_time_step_ratio = 1, 3, 3, 3, 5,
```

Resulting effective time steps:

| Dom | dx | dt | CFL check (u≈15 m/s) |
|-----|------|--------------|----------------------|
| d01 | 9000 | 45 s | fine |
| d02 | 3000 | 15 s | fine |
| d03 | 1000 | 5 s | fine |
| **d04** | 333 | **1.667 s** | 15·1.667/333 ≈ 0.075 — comfortable |
| **d05** | 111 | **0.333 s** | 15·0.333/111 ≈ 0.045 — comfortable, and headroom for strong resolved w in rolls |

d05 at 0.333 s is conservative (could likely run 0.5 s = ratio 4 on the last leg);
starting conservative avoids CFL blow-ups during turbulence spin-up when transient
w can spike. If stable, relax d05 to `parent_time_step_ratio=…,4` (dt=0.417 s) to
save ~20% of the dominant cost.

**No fractional top-level dt needed** — 45 s divides cleanly down the ratio chain, so
`time_step_fract_num/den` stay 0/1. (If you ever change the parent dt to a
non-divisible value, use the fraction fields.)

**Adaptive time step (`use_adaptive_time_step`) is NOT recommended here:** it interacts
awkwardly with `history_interval` alignment and with late-started nests, and the LES
CFL is already comfortable at fixed dt. Keep fixed dt.

---

## 4. Run only the LES window (not 36 h)

The mesoscale sea breeze must be established (morning land heating → afternoon breeze)
before the LES is meaningful, but the LES nests only need to exist for **spin-up +
the race window**. Run d01–d03 for the full window; start d04/d05 **late** and end
them at 19:00 UTC.

### Stagger the two LES starts (so d04 gives d05 turbulent inflow)

Choice (a) in §2 justifies d04 as an LES *buffer* that hands d05 already-turbulent
inflow. That only works if **d04 is turbulent when d05 initializes** — so d04 must
start **earlier** than d05, not simultaneously. If both start at 15:30Z, d04 is itself
laminar (fresh parent interpolation) at 15:30 and d05's inflow is smooth until d04
develops, defeating the buffer.

- **d04 start = 14:30Z** (2.5 h before the 17:00 window) — spins up first.
- **d05 start = 15:30Z** (1.5 h before) — initializes into an already-turbulent d04.

So `start_hour = 06, 06, 06, 14, 15` (+ `start_minute` for the :30). Both end 19:00Z.
If d05 still looks laminar at 17:00, push both earlier by an hour.

### Mechanism: online concurrent nest with per-domain start/end times

WRF supports per-domain start/end and the flag `input_from_file` per domain. The LES
nests are initialized by **interpolation from their parent** at their start time (no
met_em, no real.exe boundary file for d04/d05) — but **geo_em IS required** for them
(static fields), so geogrid must run with `max_dom=5`.

Key namelist pieces (see §5 for the full block):

```
&domains
 max_dom = 5,
&time_control
 ! parents start morning (e.g. 00Z prior day per current template) and run the window;
 ! d04/d05 start 15:30Z and end 19:00Z on race day.
 start_hour = 06, 06, 06, 14, 15,          ! d04 14:30Z, d05 15:30Z (staggered) + start_minute
 start_minute = 0, 0, 0, 30, 30,
 end_hour   = 19, 19, 19, 19, 19,
 end_minute = 0, 0, 0, 0, 0,
 input_from_file = .true., .true., .true., .false., .false.,
```

- **Staggered LES starts** (see above): d04 **14:30 UTC** (2.5 h), d05 **15:30 UTC**
  (1.5 h) before the 17:00 window. 1.5 h for d05 is a floor; if the LES turbulence
  looks laminar at 17:00, push both earlier. Budget for this.
- `input_from_file=.false.` on d04/d05 → initialized from parent, not from a met_em
  file. **VERIFY** in the WRF User's Guide (Nesting → "starting a nest at a later
  time") that real.exe + wrf.exe handle late-started nests initialized this way in
  4.8; this is the one mechanism most worth confirming in-container.
- Parents keep their existing start (do NOT shorten below the morning heating cycle —
  the breeze needs the full diurnal ramp). For a **race-morning forecast** you can
  trim the parent run to, say, 06Z→19Z race day (13 h) instead of the 36 h validation
  run; the sea breeze that day only needs the current-day heating cycle plus a few
  hours of synoptic spin-up. Keep ≥6–8 h of parent lead before the LES starts.

### Offline alternative (ndown) — mentioned, not chosen

`ndown.exe` can drive one nest offline from a coarser wrfout. It's **one level at a
time** (d03→d04, then d04→d05: two ndown passes, two wrf runs), and re-introduces
boundary files. Clunkier than online concurrent for a two-nest LES; use only if the
online late-start proves problematic. Online concurrent is the recommendation.

### Runtime + cost (method + arithmetic, not a hero number)

Cost ∝ Σ_domains( n_columns × n_vertical × n_timesteps-over-that-domain's-window ).
On c7a.8xlarge (32 vCPU) the existing 36 h **1 km** run is ~2 h wall / ~$1.50 Spot.

Relative per-domain step counts (window × steps/s):

| Dom | columns | window | dt | timesteps | columns×steps (rel.) |
|-----|---------|--------|------|-----------|----------------------|
| d03 | 6.2k | full run | 5 s | (baseline) | — |
| d04 | 12.5k | 3.5 h | 1.667 s | ~7,560 | 12.5k × 7,560 ≈ 9.5e7 |
| d05 | 18.5k | 3.5 h | 0.333 s | ~37,800 | 18.5k × 37,800 ≈ 7.0e8 |

d05 dominates: ~18.5k columns × ~37.8k steps over a 3.5 h window. That's a large but
bounded workload on 32 cores — the tile is small (18.5k columns is a fraction of
d03's 6.2k×full-run integrated load, but at 7.5× the step rate). **Order-of-magnitude
estimate: the LES nests add roughly a few hours of wall time** on top of the parent
run, i.e. total run in the **~3–6 h wall / ~$3–8 Spot** range on c7a.8xlarge — **an
estimate, to be measured on the first real run**, not a guaranteed figure. Levers if
it's too slow: relax d05 dt to ratio 4 (−~20%), shrink d05 to 100×100 (−~45% columns),
or shorten spin-up to 1 h.

---

## 5. Namelist deltas

### 5a. `namelist.wps` — extend to 5 domains

```diff
 &share
- max_dom          = 3,
+ max_dom          = 5,
- start_date       = '…', '…', '…',
+ start_date       = '<d01>', '<d02>', '<d03>', '<race>_14:30:00', '<race>_15:30:00',   ! staggered LES starts
- end_date         = '…', '…', '…',
+ end_date         = '<d01end>', '…', '…', '<race>_19:00:00', '<race>_19:00:00',
 /
 &geogrid
- parent_id         =   1,   1,   2,
+ parent_id         =   1,   1,   2,   3,   4,
- parent_grid_ratio =   1,   3,   3,
+ parent_grid_ratio =   1,   3,   3,   3,   3,
- i_parent_start    =   1,  28,  55,
+ i_parent_start    =   1,  28,  55,  10,  40,   ! d04,d05 = PLACEHOLDER — set from plotgrids
- j_parent_start    =   1,  30,  37,
+ j_parent_start    =   1,  30,  37,  45,  40,   ! d04,d05 = PLACEHOLDER — set from plotgrids
- e_we              = 100, 136,  82,
+ e_we              = 100, 136,  82, 112, 136,   ! d04 3n+1, d05 3n+1
- e_sn              = 100, 136,  76,
+ e_sn              = 100, 136,  76, 112, 136,
- geog_data_res     = '30s', '30s', '30s',
+ geog_data_res     = '30s','30s','30s','nlcd2011_9s+default','nlcd2011_9s+default',
 /
```

- **geog_data_res on d04/d05:** use the **finest available** (NLCD 9s ≈ 250 m landmask;
  topo `3s` if the SRTM 3s tile is installed — else `30s`). See §6 — the 250 m landmask
  is the real resolution limit of the "100 m" coastline. If the container can't do NLCD,
  fall back to `'30s'` on d04/d05 and plan GSHHG LANDMASK surgery on d05 (README
  "coastline" path).
- **dx/dy** in `&geogrid` stay 9000 (d01) — nest dx derives from `parent_grid_ratio`.

### 5b. `namelist.input` — extend arrays to 5, add LES block

```diff
 &time_control
+ ! parents run the morning→window; LES nests d04/d05 run race-day 15:30→19:00Z
  start_year  = 2026, 2026, 2026, 2026, 2026,
  start_month = 07,   07,   07,   07,   07,
  start_day   = 04,   04,   04,   04,   04,      ! all race day for a forecast run
  start_hour  = 06,   06,   06,   14,   15,      ! parents 06Z; d04 14:30Z, d05 15:30Z (staggered)
+ start_minute = 0,    0,    0,   30,   30,
  end_hour    = 19,   19,   19,   19,   19,
+ end_minute  = 0,    0,    0,    0,    0,
  input_from_file = .true., .true., .true., .false., .false.,
  history_interval = 60, 60, 15,                 ! parents unchanged
+ history_interval_s = 0, 0, 0, 20, 20,          ! 20-s output on LES (see output-cadence note)
  auxinput4_interval = 1440, 1440, 1440, 1440, 1440,
 /
```

> ⚠️ **`sst_update=1` vs parent-initialized LES nests — likely runtime FATAL.**
> `sst_update` is a global scalar; with it on, WRF expects `wrflowinp_d04` /
> `wrflowinp_d05`. Those are produced by metgrid/real, which we deliberately do **not**
> run for d04/d05 (they're parent-interpolated). You cannot disable `sst_update`
> per-domain. Over a 3.5 h window SST barely changes, so physically the LES should just
> inherit parent SST at init. **VERIFY in-container (top-priority check):** does WRF
> tolerate `sst_update=1` with missing `wrflowinp_d04/d05` on parent-initialized nests,
> or must you generate stub `wrflowinp_d04/d05` (e.g. copy/interpolate the d03 low-input
> file onto the LES grids)? Resolve this before the first `wrf.exe`.
```

 &domains
- max_dom = 3,
+ max_dom = 5,
- e_we  = 100, 136,  82,
+ e_we  = 100, 136,  82, 112, 136,
- e_sn  = 100, 136,  76,
+ e_sn  = 100, 136,  76, 112, 136,
- e_vert = 60, 60, 60,
+ e_vert = 60, 60, 60, 60, 60,                   ! shared vertical (see §6 caveat)
- dx = 9000, 3000, 1000,
+ dx = 9000, 3000, 1000, 333.333, 111.111,
- dy = 9000, 3000, 1000,
+ dy = 9000, 3000, 1000, 333.333, 111.111,
- grid_id = 1, 2, 3,
+ grid_id = 1, 2, 3, 4, 5,
- parent_id = 1, 1, 2,
+ parent_id = 1, 1, 2, 3, 4,
- i_parent_start = 1, 28, 55,
+ i_parent_start = 1, 28, 55, 10, 40,            ! d04,d05 PLACEHOLDER (match wps)
- j_parent_start = 1, 30, 37,
+ j_parent_start = 1, 30, 37, 45, 40,
- parent_grid_ratio = 1, 3, 3,
+ parent_grid_ratio = 1, 3, 3, 3, 3,
- parent_time_step_ratio = 1, 3, 3,
+ parent_time_step_ratio = 1, 3, 3, 3, 5,        ! dt: 45/15/5/1.667/0.333 s
 /

 &physics
- mp_physics        = 8, 8, 8,
+ mp_physics        = 8, 8, 8, 8, 8,
- ra_lw_physics     = 4, 4, 4,
+ ra_lw_physics     = 4, 4, 4, 4, 4,
- ra_sw_physics     = 4, 4, 4,
+ ra_sw_physics     = 4, 4, 4, 4, 4,
- radt              = 10, 10, 10,
+ radt              = 10, 10, 10, 10, 10,
- sf_sfclay_physics = 5, 5, 5,
+ sf_sfclay_physics = 5, 5, 5, 1, 1,             ! MM5 MOST on LES
- sf_surface_physics = 3, 3, 3,
+ sf_surface_physics = 3, 3, 3, 3, 3,            ! (2,2 if geog=nlcd/Noah)
- bl_pbl_physics    = 5, 5, 5,
+ bl_pbl_physics    = 5, 5, 5, 0, 0,             ! PBL OFF = LES
- bl_mynn_edmf      = 1, 1, 1,
+ bl_mynn_edmf      = 1, 1, 1, 0, 0,             ! inert where PBL=0
- bldt              = 0, 0, 0,
+ bldt              = 0, 0, 0, 0, 0,
- cu_physics        = 3, 0, 0,
+ cu_physics        = 3, 0, 0, 0, 0,
- sf_urban_physics  = 0, 0, 0,
+ sf_urban_physics  = 0, 0, 0, 0, 0,
+ isfflx            = 1,                          ! GLOBAL scalar — surface fluxes on
 /

 &dynamics
- diff_opt   = 2, 2, 2,
+ diff_opt   = 2, 2, 2, 2, 2,
- km_opt     = 4, 4, 4,
+ km_opt     = 4, 4, 4, 2, 2,                    ! 3D TKE (1.5-order) on LES
- diff_6th_opt   = 0, 0, 0,
+ diff_6th_opt   = 0, 0, 0, 0, 0,                ! ->2,2 on LES only if 2dx noise appears
- diff_6th_factor = 0.12, 0.12, 0.12,
+ diff_6th_factor = 0.12, 0.12, 0.12, 0.12, 0.12,
- zdamp   = 5000., 5000., 5000.,
+ zdamp   = 5000., 5000., 5000., 5000., 5000.,
- dampcoef = 0.2, 0.2, 0.2,
+ dampcoef = 0.2, 0.2, 0.2, 0.2, 0.2,
- khdif   = 0, 0, 0,
+ khdif   = 0, 0, 0, 0, 0,
- kvdif   = 0, 0, 0,
+ kvdif   = 0, 0, 0, 0, 0,
- non_hydrostatic = .true., .true., .true.,
+ non_hydrostatic = .true., .true., .true., .true., .true.,
- moist_adv_opt   = 1, 1, 1,
+ moist_adv_opt   = 1, 1, 1, 1, 1,
- scalar_adv_opt  = 1, 1, 1,
+ scalar_adv_opt  = 1, 1, 1, 1, 1,
- gwd_opt = 1, 0, 0,
+ gwd_opt = 1, 0, 0, 0, 0,
- epssm   = 0.2, 0.2, 0.5,
+ epssm   = 0.2, 0.2, 0.5, 0.5, 0.5,
+ ! --- LES-only additions (VERIFY rank in Registry.EM_COMMON) ---
+ sfs_opt        = 0, 0, 0, 1, 1,                ! NBA1 SFS stress on LES   [array? VERIFY]
+ mix_isotropic  = 0, 0, 0, 1, 1,               ! isotropic 3D mixing LES  [rank VERIFY]
+ m_opt          = 0, 0, 0, 1, 1,               ! Mij deformation diag     [rank VERIFY]
+ mix_full_fields = .true.,                      ! GLOBAL scalar
 /

 &bdy_control
- nested = .false., .true., .true.,
+ nested = .false., .true., .true., .true., .true.,
 /
```

**Also verify per driver:** the `&fdda`, `sst_update`/`auxinput4_interval`,
`obs_nudge_opt` arrays all need extension to 5 entries if used (the driver renders
those with `triple()` today — see §7). Obs-nudging on the LES nests makes no physical
sense (you don't nudge resolved turbulence); set `obs_nudge_opt = …, 0, 0` on d04/d05.

---

## 6. Honesty — what this LES CAN and CANNOT deliver

### CAN

- **Gust magnitude / gust factor.** A physically plausible realization of the peak
  10 m wind and the gust-to-mean ratio in the afternoon sea breeze (resolved rolls +
  SFS TKE). `WSPD10MAX` (already on via `nwp_diagnostics=1`) is the gust proxy —
  **note its semantics:** it is a running max of the 10 m wind **reset every history
  interval**, so at a 20-s interval it reports the per-20-s peak gust. That is the
  right quantity for **gust magnitude / gust factor**, but it is *not* a wind time
  series and cannot give oscillation cadence (below).
- **Roll / convective-cell spacing.** The horizontal wavelength of the convective rolls
  / cells (typically ~2–5× BL depth) — a *statistical* property that 111 m resolves
  where 1 km cannot.
- **Oscillation period + amplitude.** The characteristic timescale and swing of the
  wind-direction/speed oscillation a boat would feel (roll passage cadence), as a
  distribution, not a schedule. **This needs dense output:** roll-passage periods over
  the shallow marine BL can be **~2–5 min**, so 1-min history is only marginally above
  Nyquist for the swing a boat feels. Hence `history_interval_s = 20` on d04/d05 (§5b)
  — instantaneous `U10/V10` sampled every 20 s to characterize the oscillation, kept
  separate from the `WSPD10MAX` gust-envelope diagnostic. 20 s output over a 3.5 h
  window is ~630 frames/nest; storage is modest but non-trivial — a race-window subset
  (16:30–19:00Z) can be sliced post-run.
- **Spatial structure over the course:** where the breeze is gustier vs steadier, the
  Marblehead Neck lee, convergence banding — geometry, one realization.

### CANNOT

- **Deterministic per-gust timing.** LES produces *a* turbulent realization consistent
  with the mean state, not *the* actual sequence of gusts. "A 12 kt puff at 13:47 on
  the committee-boat end" is not a claim this can make. Report statistics
  (gust factor, period, amplitude, spacing), never a timeline of individual puffs.
- **Skill beyond the parent's synoptic/SST error.** LES adds turbulence structure, not
  large-scale skill. If d01–d03 blow the sea-breeze onset or the offshore ΔT, the LES
  faithfully renders the wrong breeze in high resolution (CLAUDE.md: "you inherit the
  parent's synoptic errors").

### Binding physical caveats (must be read alongside the CAN list)

1. **Geography is the real resolution limit, not Δx.** The sea breeze is
   coastline-driven; the landmask is only as sharp as its source — **NLCD 9s ≈ 250 m**
   (topo 30s ≈ 900 m; 3s ≈ 90 m if installed). So the "100 m" coastline is effectively
   ~250 m. Cape Ann/Marblehead relief is <30 m so **topo matters little here**, but the
   **landmask is everything** — plan GSHHG LANDMASK surgery on d05 if NLCD isn't sharp
   enough. Without a crisp coast, the 111 m grid resolves turbulence over the wrong
   land/sea boundary.

2. **Turbulence spin-up from a smooth parent.** A late-started LES inherits laminar
   (PBL-parameterized) parent fields with **zero resolved eddies**; turbulence must
   grow over fetch and time. Budget **≥1.5 h spin-up** (§4), possibly more over the
   shallow marine BL, and **discard the spin-up period** from any product. The
   literature accelerant is the **Cell Perturbation Method** (Muñoz-Esparza 2014) —
   **likely NOT in stock WRF 4.8; VERIFY.** If absent, spin-up time is the only lever;
   d04's LES buffer (choice (a), §2) exists partly to shorten d05's spin-up by handing
   it already-turbulent inflow.

3. **Shallow marine BL makes 111 m marginal LES — and this is consistent with our own
   doctrine.** Over the cool water the BL depth zᵢ may be only ~200–500 m, so the
   energy-containing eddies are that small, and applying our own "1 km ⇒ 6–7×" rule one
   level down, 111 m gives ~500–700 m effective resolution — **barely** resolving the
   marine rolls. Over the heated land zᵢ is deeper and 111 m is genuinely good. State
   plainly: **on the water the LES is at the edge of resolving the rolls; 50 m would be
   cleaner but costs ~8×** (½ Δx in x,y and ½ dt) and is out of budget. The honest read:
   trust the LES structure over/near the coast and heated land more than in the middle
   of the cold offshore patch.

4. **Vertical resolution is shared and marginal.** `e_vert=60` with a 15 m first layer
   is fine for the mesoscale but **marginal for LES** (LES ideally wants isotropic-ish
   cells near the surface, i.e. Δz comparable to Δx≈111 m in the mixed layer, and many
   levels in the shallow marine BL). WRF's `e_vert` is **shared across all domains** in
   the standard build; per-nest vertical refinement exists (`vert_refine_method=1`) but
   is finicky — **VERIFY / treat as optional.** Bumping e_vert changes *all* domains and
   raises cost everywhere. Recommendation: **keep 60**, and caveat that the LES vertical
   structure (especially the surface-layer eddies) is under-resolved. If a future run
   targets vertical roll structure specifically, revisit with `vert_refine_method`.

---

## 7. Changes to `run_gom_seabreeze.py`

Add an **LES mode** that renders the 5-domain namelists for the race window. The
current renderer assumes 3 domains and uses `triple()` (3-entry arrays) everywhere —
LES mode needs 5-entry arrays and per-domain start/end times. Concrete changes:

1. **New CLI flag** `--les` (or `--profile les`) that switches the whole render path
   to 5 domains. Keep the existing 3-domain path byte-identical when `--les` is off
   (validation runs must not change).

2. **`quint()` helper** alongside `triple()`:
   ```python
   quint = lambda v: f"{v}, {v}, {v}, {v}, {v},"
   ```
   Use it (or explicit 5-entry strings) for every array the renderer touches:
   `start_*`, `end_*`, `e_vert`, `num_*` (metgrid arrays stay 3-wide? — LES nests have
   no met_em, but real still reads a 5-entry array; **VERIFY** whether metgrid-level
   counts must be 5-wide or stay parent-length). Set `e_we/e_sn/dx/dy/parent_*` from an
   `LES_GRID` dict rather than `set_nml` on single lines.

3. **`LES_GRID` config dict** (parallels `MODE_CFG`/`GEOG_CFG`):
   ```python
   LES_GRID = dict(
       max_dom=5,
       e_we="100, 136, 82, 112, 136,",
       e_sn="100, 136, 76, 112, 136,",
       dx="9000, 3000, 1000, 333.333, 111.111,",
       dy="9000, 3000, 1000, 333.333, 111.111,",
       parent_id="1, 1, 2, 3, 4,",
       parent_grid_ratio="1, 3, 3, 3, 3,",
       parent_time_step_ratio="1, 3, 3, 3, 5,",
       i_parent_start="1, 28, 55, 10, 40,",   # PLACEHOLDER — from plotgrids
       j_parent_start="1, 30, 37, 45, 40,",   # PLACEHOLDER
       feedback=0,
   )
   LES_PHYS = dict(                            # &physics + &dynamics LES overrides
       bl_pbl_physics="5, 5, 5, 0, 0,",
       sf_sfclay_physics="5, 5, 5, 1, 1,",
       km_opt="4, 4, 4, 2, 2,",
       diff_opt="2, 2, 2, 2, 2,",
       epssm="0.2, 0.2, 0.5, 0.5, 0.5,",
       # sfs_opt / mix_isotropic / m_opt injected as new lines
   )
   ```

4. **Per-domain LES start/end times.** Add `--les-window-start`/`--les-window-end`
   (default 15:30 / 19:00 UTC on `--date`) and render `start_hour/minute` and
   `end_hour/minute` with the parents at the run start and d04/d05 at the LES window.
   Introduce `start_minute/end_minute` lines into the template (currently absent — the
   3-domain path can render them as `0,0,0`).

5. **`input_from_file`** → `.true., .true., .true., .false., .false.` in LES mode.

6. **`inject_before_group_end`** the three new `&dynamics` lines (`sfs_opt`,
   `mix_isotropic`, `m_opt`) and the `&physics` `isfflx=1` scalar — they don't exist in
   the template, so `set_nml` would fail (it errors on missing keys). Add them as an
   injected block guarded by `--les`.

7. **Extend the existing renders** (`sf_surface_physics`, `num_soil_layers`,
   `auxinput4_interval`, `grid_fdda`, obs-nudge arrays) to 5 entries when `--les`.
   Guard the obs-nudge path: `obs_nudge_opt` must be `…, 0, 0` on the LES nests.

8. **geogrid must run with max_dom=5** — the driver's geogrid cache check
   (`geo_em.d01.nc` exists) must be invalidated when switching to LES (check for
   `geo_em.d05.nc`), or force `--skip-geogrid=false` on the first LES render.

9. **`num_metgrid_levels` unchanged** (LES nests have no met_em; they're
   parent-interpolated). Ungrib/metgrid stay 3-domain. **VERIFY** metgrid/real behavior
   with `max_dom=5` but only 3 met_em levels present — real.exe should build d04/d05
   from parent; this is the second thing to confirm in-container (with the late-start
   mechanism, §4).

Keep everything **behind `--les`** so the validated 3-domain forecast/hindcast paths
are untouched.

---

## 8. Pre-flight checklist (in-container, before any run)

Ordered, cheap-first:

1. `plotgrids.exe` → fix d04/d05 `i/j_parent_start`; confirm d05 centered on
   42.46 N/−70.77 W and both LES nests clear their parent's relaxation zone by ≥5 cells.
   Extend d03 north if the course crowds its boundary.
2. **VERIFY option ranks** in `/comsoftware/wrf/WRF-4.8.0/Registry/Registry.EM_COMMON`:
   `sfs_opt`, `mix_isotropic`, `m_opt`, `c_s`, `c_k`, `vert_refine_method`,
   `mix_full_fields`, `isfflx` (scalar), and that `bl_pbl_physics=0` + `km_opt=2` +
   `diff_opt=2` is a supported LES combination in 4.8.
3. **VERIFY late-nest-start** handling (User's Guide, Nesting): `input_from_file=.false.`
   on d04/d05 with per-domain **staggered** `start_*` (d04 14:30Z, d05 15:30Z) after the
   parent start, and real.exe with `max_dom=5` given only 3 met_em levels.
3a. **VERIFY `sst_update=1` + missing `wrflowinp_d04/d05`** (see §5b box) — does WRF
   tolerate it on parent-initialized nests, or must you stub the low-input files?
   Top-priority runtime check.
4. **VERIFY** Cell Perturbation Method availability (`≈ perturb_...` namelist keys) —
   if present, consider enabling on d04 inflow to cut spin-up; if absent, rely on the
   ≥1.5 h spin-up + d04 turbulent buffer.
5. Run `real.exe` (`max_dom=5`) → check `rsl.error.0000` for grid-dimension /
   rank / LSM-compat FATALs before committing to the full `wrf.exe`.
6. Short smoke run (30 min sim) to confirm CFL stability on d05 at dt=0.333 s before
   the full window.

---

*Design only. No AWS launch. All VERIFY@runtime items must be checked against
`/comsoftware/wrf/WRF-4.8.0` source + WRF 4.8 User's Guide before the first LES run.*
