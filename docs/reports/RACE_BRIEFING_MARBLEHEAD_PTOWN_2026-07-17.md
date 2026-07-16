# Race Briefing — Marblehead → Provincetown (overnight)

**Start:** Fri 2026-07-17 **19:00 EDT** off Marblehead · **Finish:** Provincetown, expected ~**03:00 Sat**
**Course:** ~**38 nm** rhumb line, bearing **~133° (SE)** across Massachusetts Bay into Cape Cod Bay
**Lead time:** first-look at **~27 h** — refine Friday with short-lead HRRR + the actual breeze at the dock.
*Sources: Open-Meteo (HRRR/GFS blend) corridor, NWS BOX marine zones ANZ231/233, NOAA CO-OPS tides. Regenerate: `python3 climatology/corridor_forecast.py climatology/zones/marblehead_provincetown.json`.*

---

## Headline

A **light, weak-gradient southerly night** (MSLP flat ~1016 mb, no front). The reliable signal: the wind
**fills from the S/SW and holds 8–12 kt offshore** once you're clear of the coast, giving a **starboard-tack
reach** that gradually frees across the bay to Race Point. **Calm seas (≤2 ft).** The **start is the wildcard**
— light and directionally uncertain in the post-sunset transition. Get offshore into pressure early; the
middle of the bay has real breeze while the Marblehead shore goes soft.

## Hour-by-hour along the corridor (Open-Meteo, kt, ET)

| Leg (ET) | Marblehead start | Mass Bay mid | CC Bay approach | Race Pt / P-town |
|---|---|---|---|---|
| 19:00 (start) | SE ~127°/**4–7** | SE 134°/**10** | ESE 122°/8 | SSE 159°/8 |
| 21:00 | var 75–140°/**2–3** | SE 140°/8 | SSE 151°/8 | SSE 155°/8 |
| 23:00 | S 153°/3 | S 153°/9 | S 165°/**11** g15 | S 171°/8 g15 |
| 01:00 Sat | SW 214°/**1–2** | S 158°/8 | S 181°/9 | S 181°/10 g15 |
| 03:00 (finish) | SE 129°/3 | S 178°/**10** g14 | S 182°/**11** g15 | S 185°/9 g14 |
| 05:00 | SSW 197°/6 | SSW 193°/10 g14 | SSW 193°/11 g15 | SSW 186°/10 g14 |

**Read it as a gradient, not one number:** the **inshore Marblehead corner is the light spot** (2–6 kt, shifty
through the sunset transition); **mid-bay and south hold ~8–11 kt** all night, building slightly toward dawn
with **gusts 13–16 kt** near Race Point. Wind direction **veers SE → S → SSW** through the night (clocks right).

## The start (19:00–21:00) — the one genuinely uncertain part

Sources split on the *direction* here, which flips the start's character:
- **Open-Meteo:** dying sea-breeze **SE 4–7 kt** → that's roughly **close-hauled** on the 133° rhumb.
- **NWS BOX:** NW/N daytime **becoming SW at night** → a coastal **NW land-breeze** would be a **downwind** start.

Either way it is **light (≤7 kt) and shifty** right at 19:00 (sunset ~20:15, so you start into the evening
transition). **Do not commit to a beat-vs-run plan from the forecast — sail the breeze that's actually on the
water at the gun.** Highest-value input Friday evening: the wind at the Marblehead dock/committee boat at
start time. Robust call regardless: **it fills S/SW within the first ~1–2 hours as you get offshore**, so
**prioritize getting off the line cleanly and reaching pressure to the south/offshore** over defending a side.

## Middle bay → Race Point (21:00–02:30)

- Once established, **S–SSW 8–12 kt** = a **starboard-tack close/beam reach** on the SE rhumb that **frees as
  the wind veers** — later it's a broad reach into the P-town approach.
- **Pressure lives offshore/south:** mid-bay ~10 kt vs the Marblehead shore's 2–6 kt. Sagging toward the
  Cape Ann shore = parked. **Commit south into the steadier band.**
- The **right shift is persistent** (veering all night) — on a reach that generally means staying on the
  header/pressure side and not over-standing north.

## Tides & current

Boston/P-town tides (near-identical): **Low ~20:25 Fri**, **High ~02:45 Sat**.
- **Start ≈ low-water slack**, then the **flood builds all night** to a **high right at the ~03:00 finish → ≈ high-water slack.**
- **Current at the P-town finish is minor** (near slack, whichever way you arrive).
- **Mass Bay open-water current is weak (<0.5 kt)** — negligible mid-passage.
- **Race Point is the one current gate:** boats rounding **before ~02:45** carry the last of the **flood into
  Cape Cod Bay** (favorable); boats **later than ~03:00** start meeting the **building ebb** on the approach.
  (Couldn't pin an exact Race Point current station at this lead — treat as directional, not a number.)

## Night hazards / watch-items

- **Dark night:** new moon was ~07-14, so only a thin crescent that sets early → **little moonlight.** Nav by
  instruments; lights/watches set. Wildfire **smoke** aloft (noted in the marine text) may dim stars/horizon.
- **Fog watch:** July S/SW flow over the cooler bay is a classic sea-fog setup. **NWS is NOT flagging fog for
  Friday night** (and seas ≤2 ft), but it's the thing most likely to change the game — **recheck Friday PM.**
- **No Small Craft Advisory expected Friday night** (the current SCA expires Thursday). Benign wind/seas.

## Confidence & refresh

Moderate on the **overnight S/SW reach + calm seas + tide timing** (sources agree). **Low on the exact start
direction/strength** (transition + 27 h lead). **Re-run Friday midday and again pre-start** — the short-lead
HRRR and dock obs are what to trust. Optional extra if you want it: a WRF micro-run for just the **Marblehead
land-breeze corner** and **Race Point channeling** (the only places a downscale would beat the gradient models).
