#!/usr/bin/env python3
"""corridor_forecast.py -- point-corridor race forecast for a passage that the WRF/LES
sea-breeze stack is the wrong tool for (overnight / open-water / long corridor). Reads a
zone file (zones/*.json: waypoints + race window + tide stations + marine zones) and pulls
independent public guidance:

  * Open-Meteo (HRRR/GFS blend) 10 m wind + gusts + MSLP at each waypoint, hourly, in kt/ET
  * NOAA CO-OPS hi/lo tide predictions at the zone's tide stations
  * NWS coastal-waters zone forecast text (fog / seas / wind narrative)

Prints a corridor table + saves briefing JSON. Re-run as fresh cycles land — the short-lead
run on race morning is the one to trust. No infra, no AWS, stdlib + Open-Meteo only.

  python3 corridor_forecast.py climatology/zones/marblehead_provincetown.json [--out brief.json]
"""
import argparse, json, sys, urllib.request, datetime as dt

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "sailframes-corridor"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")

def om_wind(lat, lon, d0, d1):
    u = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
         "&hourly=wind_speed_10m,wind_direction_10m,wind_gusts_10m,pressure_msl"
         f"&wind_speed_unit=kn&timezone=America%2FNew_York&start_date={d0}&end_date={d1}")
    return json.loads(_get(u))["hourly"]

def tides(station, d0, d1):
    u = ("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?application=sailframes"
         "&product=predictions&datum=MLLW&interval=hilo&units=english&time_zone=lst_ldt&format=json"
         f"&station={station}&begin_date={d0.replace('-','')}&end_date={d1.replace('-','')}")
    try:
        return [(p["t"], p["type"], float(p["v"])) for p in json.loads(_get(u)).get("predictions", [])]
    except Exception as e:
        return []

def marine(zone):
    try:
        txt = _get(f"https://tgftp.nws.noaa.gov/data/forecasts/marine/coastal/an/{zone.lower()}.txt")
        # keep the dated period lines (.THIS... .TONIGHT... .FRI...)
        out = []
        for ln in txt.splitlines():
            if ln.startswith("."):
                out.append(ln.strip())
        return out
    except Exception:
        return []

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zone")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    Z = json.load(open(a.zone))
    w0, w1 = Z["race"]["window_local"]
    d0, d1 = Z["race"]["date"], (dt.date.fromisoformat(Z["race"]["date"]) + dt.timedelta(days=1)).isoformat()
    inwin = lambda t: w0 <= t <= w1

    brief = {"zone": Z["name"], "race": Z["race"], "generated_local": None, "corridor": {}, "tides": {}, "marine": {}}
    print(f"# {Z['name']}\n# window {w0} .. {w1} ET   (rhumb {Z.get('rhumb_bearing_deg')}° / {Z.get('distance_nm')} nm)\n")
    for wp in Z["waypoints"]:
        h = om_wind(wp["lat"], wp["lon"], d0, d1)
        rows = []
        print(f"### {wp['name']}  ({wp['lat']},{wp['lon']})")
        print("  time(ET)       from  kt  gust  MSLP")
        for i, t in enumerate(h["time"]):
            if not inwin(t):
                continue
            r = dict(t=t, dir=round(h["wind_direction_10m"][i]), kt=round(h["wind_speed_10m"][i], 1),
                     gust=round(h["wind_gusts_10m"][i], 1), mslp=round(h["pressure_msl"][i]))
            rows.append(r)
            print(f"  {t[5:16]}  {r['dir']:4d}  {r['kt']:4.1f} {r['gust']:5.1f}  {r['mslp']}")
        brief["corridor"][wp["name"]] = rows
        print()
    for k, st in Z.get("tide_stations", {}).items():
        tl = tides(st, d0, d1)
        brief["tides"][k] = tl
        print(f"## tide {k} ({st}):", ", ".join(f"{t[5:16]} {ty} {v:.1f}ft" for (t, ty, v) in tl if w0[:10] <= t[:10] <= w1[:10]))
    for k, z in Z.get("marine_zones", {}).items():
        m = marine(z)
        brief["marine"][k] = m
        fri = [x for x in m if "FRI" in x.upper()]
        print(f"## marine {k} ({z}) Fri:", " ".join(fri[:2])[:180])
    if a.out:
        json.dump(brief, open(a.out, "w"), indent=1)
        print("\nwrote", a.out)

if __name__ == "__main__":
    main()
