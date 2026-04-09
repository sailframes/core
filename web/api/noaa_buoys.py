"""
NOAA NDBC Buoy Data Service for SailFrames

Fetches real-time and historical data from NOAA buoys around Boston Harbor.
Data is cached to minimize API calls.

Buoys:
- 44013: Boston (16 NM East) - Primary offshore buoy
- BHBM3: Boston Harbor - In-harbor pressure/temp
- CSIM3: Castle Island - Wind data
- 44029: Massachusetts Bay (Buoy A01)
- 44090: Cape Cod Bay - Waves
"""

import requests
from datetime import datetime, timedelta
from typing import Optional
import json
import os
from functools import lru_cache

# Boston Harbor area NOAA NDBC buoys and C-MAN stations
# Note: WeatherFlow stations (Courageous, Boston Sailing Center, Deer Island, etc.)
# require WeatherFlow API access and are not included here.
BOSTON_BUOYS = {
    # Primary Boston Harbor stations
    "44013": {
        "name": "Boston 16NM",
        "lat": 42.346,
        "lon": -70.651,
        "type": "offshore",
        "data": ["wind", "waves", "pressure", "air_temp", "water_temp"],
        "color": "#e0245e",
    },
    "CSIM3": {
        "name": "Castle Island",
        "lat": 42.341,
        "lon": -71.012,
        "type": "shore",
        "data": ["wind", "air_temp"],
        "color": "#17bf63",
    },
    "44029": {
        "name": "Mass Bay A01",
        "lat": 42.523,
        "lon": -70.566,
        "type": "offshore",
        "data": ["wind", "waves", "air_temp", "water_temp"],
        "color": "#ffad1f",
    },
    # Regional stations for context
    "BUZM3": {
        "name": "Buzzards Bay",
        "lat": 41.397,
        "lon": -71.033,
        "type": "shore",
        "data": ["wind", "pressure"],
        "color": "#8b5cf6",
    },
    "NTKM3": {
        "name": "Nantucket",
        "lat": 41.281,
        "lon": -70.092,
        "type": "shore",
        "data": ["wind"],
        "color": "#f97316",
    },
}

NDBC_REALTIME_URL = "https://www.ndbc.noaa.gov/data/realtime2/{station_id}.txt"
NDBC_HISTORICAL_URL = "https://www.ndbc.noaa.gov/data/stdmet/{month_str}/{station_id}.txt"

# Cache directory
CACHE_DIR = "/tmp/sailframes_noaa_cache"
os.makedirs(CACHE_DIR, exist_ok=True)


def parse_ndbc_line(header: list, line: str) -> Optional[dict]:
    """Parse a single NDBC data line into a dictionary."""
    parts = line.split()
    if len(parts) < 5:
        return None

    try:
        # Date/time fields
        year = int(parts[0])
        month = int(parts[1])
        day = int(parts[2])
        hour = int(parts[3])
        minute = int(parts[4])

        timestamp = datetime(year, month, day, hour, minute)

        data = {
            "timestamp": timestamp.isoformat() + "Z",
            "unix_ts": int(timestamp.timestamp()),
        }

        # Map remaining columns based on header
        col_map = {
            "WDIR": "wind_dir",
            "WSPD": "wind_speed_mps",
            "GST": "wind_gust_mps",
            "WVHT": "wave_height_m",
            "DPD": "wave_period_sec",
            "APD": "wave_avg_period_sec",
            "MWD": "wave_dir",
            "PRES": "pressure_hpa",
            "ATMP": "air_temp_c",
            "WTMP": "water_temp_c",
            "DEWP": "dew_point_c",
            "VIS": "visibility_nm",
            "PTDY": "pressure_tendency",
            "TIDE": "tide_ft",
        }

        for i, col in enumerate(header[5:], start=5):
            if col in col_map and i < len(parts):
                val = parts[i]
                if val != "MM":  # MM = missing
                    try:
                        data[col_map[col]] = float(val)
                    except ValueError:
                        pass

        # Convert wind speed to knots
        if "wind_speed_mps" in data:
            data["wind_speed_kts"] = round(data["wind_speed_mps"] * 1.94384, 1)
        if "wind_gust_mps" in data:
            data["wind_gust_kts"] = round(data["wind_gust_mps"] * 1.94384, 1)

        return data
    except (ValueError, IndexError):
        return None


def fetch_buoy_data(station_id: str, hours_back: int = 24) -> list:
    """
    Fetch buoy data from NDBC for the last N hours.
    Uses realtime2 data which has ~45 days of history.
    """
    cache_file = os.path.join(CACHE_DIR, f"{station_id}_{hours_back}h.json")
    cache_max_age = 600  # 10 minutes

    # Check cache
    if os.path.exists(cache_file):
        age = datetime.now().timestamp() - os.path.getmtime(cache_file)
        if age < cache_max_age:
            with open(cache_file) as f:
                return json.load(f)

    url = NDBC_REALTIME_URL.format(station_id=station_id)

    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[NOAA] Error fetching {station_id}: {e}")
        return []

    lines = resp.text.strip().split("\n")
    if len(lines) < 2:
        return []

    # Parse header (first line starts with #)
    header = lines[0].replace("#", "").split()

    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    data = []

    for line in lines[1:]:
        if line.startswith("#"):
            continue
        parsed = parse_ndbc_line(header, line)
        if parsed:
            ts = datetime.fromisoformat(parsed["timestamp"].replace("Z", ""))
            if ts >= cutoff:
                data.append(parsed)

    # Sort by time ascending
    data.sort(key=lambda x: x["unix_ts"])

    # Cache results
    with open(cache_file, "w") as f:
        json.dump(data, f)

    return data


def fetch_buoy_data_for_timerange(
    station_id: str,
    start_ts: float,
    end_ts: float
) -> list:
    """
    Fetch buoy data for a specific time range.
    Returns data points within the range.
    """
    # Fetch extra buffer for interpolation
    buffer_hours = 2
    start_dt = datetime.utcfromtimestamp(start_ts) - timedelta(hours=buffer_hours)
    end_dt = datetime.utcfromtimestamp(end_ts) + timedelta(hours=buffer_hours)

    hours_back = int((datetime.utcnow() - start_dt).total_seconds() / 3600) + 1
    hours_back = min(hours_back, 45 * 24)  # Max 45 days

    all_data = fetch_buoy_data(station_id, hours_back)

    # Filter to time range
    filtered = [
        d for d in all_data
        if start_ts <= d["unix_ts"] <= end_ts
    ]

    return filtered


def get_all_buoys_data(start_ts: float, end_ts: float) -> dict:
    """
    Fetch data from all Boston Harbor buoys for a time range.
    Returns dict with buoy metadata and time-series data.
    """
    result = {}

    for station_id, meta in BOSTON_BUOYS.items():
        data = fetch_buoy_data_for_timerange(station_id, start_ts, end_ts)
        result[station_id] = {
            **meta,
            "station_id": station_id,
            "data_points": data,
            "has_data": len(data) > 0,
        }

    return result


def interpolate_buoy_value(data_points: list, target_ts: float, field: str) -> Optional[float]:
    """
    Interpolate a buoy value at a specific timestamp.
    Uses linear interpolation between surrounding points.
    """
    if not data_points:
        return None

    # Find surrounding points
    before = None
    after = None

    for point in data_points:
        if field not in point:
            continue
        if point["unix_ts"] <= target_ts:
            before = point
        elif point["unix_ts"] > target_ts and after is None:
            after = point
            break

    if before is None and after is None:
        return None
    if before is None:
        return after.get(field) if after else None
    if after is None:
        return before.get(field)

    # Linear interpolation
    t1, v1 = before["unix_ts"], before[field]
    t2, v2 = after["unix_ts"], after[field]

    if t2 == t1:
        return v1

    ratio = (target_ts - t1) / (t2 - t1)
    return round(v1 + ratio * (v2 - v1), 2)


def get_buoy_snapshot(buoys_data: dict, target_ts: float) -> dict:
    """
    Get interpolated buoy values at a specific timestamp.
    Returns a dict with current values for each buoy.
    """
    snapshot = {}

    for station_id, buoy in buoys_data.items():
        data_points = buoy.get("data_points", [])

        snapshot[station_id] = {
            "station_id": station_id,
            "name": buoy["name"],
            "lat": buoy["lat"],
            "lon": buoy["lon"],
            "color": buoy["color"],
            "wind_dir": interpolate_buoy_value(data_points, target_ts, "wind_dir"),
            "wind_speed_kts": interpolate_buoy_value(data_points, target_ts, "wind_speed_kts"),
            "wind_gust_kts": interpolate_buoy_value(data_points, target_ts, "wind_gust_kts"),
            "wave_height_m": interpolate_buoy_value(data_points, target_ts, "wave_height_m"),
            "wave_period_sec": interpolate_buoy_value(data_points, target_ts, "wave_period_sec"),
            "pressure_hpa": interpolate_buoy_value(data_points, target_ts, "pressure_hpa"),
            "air_temp_c": interpolate_buoy_value(data_points, target_ts, "air_temp_c"),
            "water_temp_c": interpolate_buoy_value(data_points, target_ts, "water_temp_c"),
        }

        # Remove None values
        snapshot[station_id] = {
            k: v for k, v in snapshot[station_id].items() if v is not None
        }

    return snapshot


if __name__ == "__main__":
    # Test: Fetch data for April 7, 2026 session
    start = datetime(2026, 4, 7, 17, 0).timestamp()
    end = datetime(2026, 4, 7, 21, 0).timestamp()

    print("Fetching NOAA buoy data for Boston Harbor...")
    data = get_all_buoys_data(start, end)

    for station_id, buoy in data.items():
        print(f"\n{station_id} - {buoy['name']}:")
        print(f"  Location: {buoy['lat']}, {buoy['lon']}")
        print(f"  Data points: {len(buoy['data_points'])}")
        if buoy['data_points']:
            print(f"  First: {buoy['data_points'][0]}")
            print(f"  Last: {buoy['data_points'][-1]}")
