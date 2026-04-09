"""
SailFrames API - NOAA Weather Data
Fetches real-time and historical data from NOAA NDBC buoys and NWS airport stations.
"""

import json
import os
import logging
from datetime import datetime, timedelta
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Boston Harbor area NOAA NDBC buoys and C-MAN stations
BOSTON_BUOYS = {
    # Primary Boston Harbor stations
    "44013": {
        "name": "Boston 16NM",
        "lat": 42.346,
        "lon": -70.651,
        "type": "offshore",
        "source": "ndbc",
        "data": ["wind", "waves", "pressure", "air_temp", "water_temp"],
        "color": "#e0245e",
    },
    "CSIM3": {
        "name": "Castle Island",
        "lat": 42.341,
        "lon": -71.012,
        "type": "shore",
        "source": "ndbc",
        "data": ["wind", "air_temp"],
        "color": "#17bf63",
    },
    "44029": {
        "name": "Mass Bay A01",
        "lat": 42.523,
        "lon": -70.566,
        "type": "offshore",
        "source": "ndbc",
        "data": ["wind", "waves", "air_temp", "water_temp"],
        "color": "#ffad1f",
    },
    # NWS Airport station - uses weather.gov API
    "KBOS": {
        "name": "Logan Airport",
        "lat": 42.36,
        "lon": -71.01,
        "type": "airport",
        "source": "nws",
        "data": ["wind", "pressure", "air_temp"],
        "color": "#06b6d4",
    },
}

NDBC_REALTIME_URL = "https://www.ndbc.noaa.gov/data/realtime2/{station_id}.txt"

# In-memory cache (persists across Lambda invocations in same container)
_cache = {}
CACHE_TTL_SECONDS = 600  # 10 minutes


def lambda_handler(event, context):
    """Handle buoy API requests."""
    http_method = event.get('requestContext', {}).get('http', {}).get('method') or event.get('httpMethod', 'GET')
    path = event.get('rawPath', '') or event.get('path', '')
    query_params = event.get('queryStringParameters') or {}

    logger.info(f"Request: {http_method} {path}")

    headers = {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type'
    }

    if http_method == 'OPTIONS':
        return {'statusCode': 200, 'headers': headers, 'body': '{}'}

    try:
        # GET /api/buoys - list all buoys
        if path == '/api/buoys' or path.endswith('/buoys'):
            buoys = []
            for station_id, meta in BOSTON_BUOYS.items():
                buoys.append({
                    "station_id": station_id,
                    "name": meta["name"],
                    "lat": meta["lat"],
                    "lon": meta["lon"],
                    "type": meta["type"],
                    "data_types": meta["data"],
                    "color": meta["color"],
                })
            return {
                'statusCode': 200,
                'headers': headers,
                'body': json.dumps({'buoys': buoys})
            }

        # GET /api/buoys/data?start_ts=&end_ts= - get all buoy data
        if '/buoys/data' in path:
            start_ts = float(query_params.get('start_ts', 0))
            end_ts = float(query_params.get('end_ts', 0))

            if not start_ts or not end_ts:
                return {
                    'statusCode': 400,
                    'headers': headers,
                    'body': json.dumps({'error': 'start_ts and end_ts are required'})
                }

            data = get_all_buoys_data(start_ts, end_ts)
            result = {}
            for station_id, buoy in data.items():
                result[station_id] = {
                    "station_id": station_id,
                    "name": buoy["name"],
                    "lat": buoy["lat"],
                    "lon": buoy["lon"],
                    "color": buoy["color"],
                    "type": buoy["type"],
                    "has_data": buoy["has_data"],
                    "data_points": buoy["data_points"],
                }

            return {
                'statusCode': 200,
                'headers': headers,
                'body': json.dumps({'buoys': result})
            }

        # GET /api/buoys/snapshot?timestamp= - get interpolated values
        if '/buoys/snapshot' in path:
            timestamp = float(query_params.get('timestamp', 0))
            if not timestamp:
                return {
                    'statusCode': 400,
                    'headers': headers,
                    'body': json.dumps({'error': 'timestamp is required'})
                }

            start_ts = float(query_params.get('start_ts', timestamp - 4 * 3600))
            end_ts = float(query_params.get('end_ts', timestamp + 4 * 3600))

            buoys_data = get_all_buoys_data(start_ts, end_ts)
            snapshot = get_buoy_snapshot(buoys_data, timestamp)

            return {
                'statusCode': 200,
                'headers': headers,
                'body': json.dumps({'timestamp': timestamp, 'buoys': snapshot})
            }

        return {
            'statusCode': 404,
            'headers': headers,
            'body': json.dumps({'error': 'Not found'})
        }

    except Exception as e:
        logger.error(f"Error: {e}")
        return {
            'statusCode': 500,
            'headers': headers,
            'body': json.dumps({'error': str(e)})
        }


def parse_ndbc_line(header: list, line: str) -> Optional[dict]:
    """Parse a single NDBC data line into a dictionary."""
    parts = line.split()
    if len(parts) < 5:
        return None

    try:
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
                if val != "MM":
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
    """Fetch buoy data from NDBC for the last N hours."""
    cache_key = f"{station_id}_{hours_back}h"

    # Check cache
    if cache_key in _cache:
        cached_time, cached_data = _cache[cache_key]
        if datetime.utcnow().timestamp() - cached_time < CACHE_TTL_SECONDS:
            return cached_data

    url = NDBC_REALTIME_URL.format(station_id=station_id)

    try:
        with urlopen(url, timeout=10) as response:
            text = response.read().decode('utf-8')
    except URLError as e:
        logger.warning(f"Error fetching {station_id}: {e}")
        return []

    lines = text.strip().split("\n")
    if len(lines) < 2:
        return []

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

    data.sort(key=lambda x: x["unix_ts"])

    # Cache result
    _cache[cache_key] = (datetime.utcnow().timestamp(), data)

    return data


def fetch_buoy_data_for_timerange(station_id: str, start_ts: float, end_ts: float) -> list:
    """Fetch buoy data for a specific time range."""
    buffer_hours = 2
    start_dt = datetime.utcfromtimestamp(start_ts) - timedelta(hours=buffer_hours)

    hours_back = int((datetime.utcnow() - start_dt).total_seconds() / 3600) + 1
    hours_back = min(hours_back, 45 * 24)  # Max 45 days

    all_data = fetch_buoy_data(station_id, hours_back)

    filtered = [d for d in all_data if start_ts <= d["unix_ts"] <= end_ts]

    return filtered


def fetch_nws_station_data(station_id: str, hours_back: int = 24) -> list:
    """Fetch weather data from NWS weather.gov API for airport stations."""
    cache_key = f"nws_{station_id}_{hours_back}h"

    # Check cache
    if cache_key in _cache:
        cached_time, cached_data = _cache[cache_key]
        if datetime.utcnow().timestamp() - cached_time < CACHE_TTL_SECONDS:
            return cached_data

    url = f"https://api.weather.gov/stations/{station_id}/observations?limit=50"

    try:
        req = Request(url, headers={
            "User-Agent": "SailFrames/1.0 (sailing analytics; contact@sailframes.com)",
            "Accept": "application/geo+json"
        })
        with urlopen(req, timeout=5) as response:
            text = response.read().decode('utf-8')
            response_data = json.loads(text)
    except Exception as e:
        logger.warning(f"Error fetching NWS {station_id}: {e}")
        # Cache empty result briefly to avoid hammering on errors
        _cache[cache_key] = (datetime.utcnow().timestamp(), [])
        return []

    features = response_data.get("features", [])
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    data = []

    for feature in features:
        props = feature.get("properties", {})
        ts_str = props.get("timestamp")
        if not ts_str:
            continue

        try:
            # Parse ISO timestamp
            ts = datetime.fromisoformat(ts_str.replace("+00:00", "").replace("Z", ""))
            if ts < cutoff:
                continue

            point = {
                "timestamp": ts.isoformat() + "Z",
                "unix_ts": int(ts.timestamp()),
            }

            # Wind direction (degrees)
            wind_dir = props.get("windDirection", {}).get("value")
            if wind_dir is not None:
                point["wind_dir"] = wind_dir

            # Wind speed (km/h -> knots)
            wind_speed_kmh = props.get("windSpeed", {}).get("value")
            if wind_speed_kmh is not None:
                point["wind_speed_kts"] = round(wind_speed_kmh * 0.539957, 1)

            # Wind gust (km/h -> knots)
            wind_gust_kmh = props.get("windGust", {}).get("value")
            if wind_gust_kmh is not None:
                point["wind_gust_kts"] = round(wind_gust_kmh * 0.539957, 1)

            # Temperature (C)
            temp = props.get("temperature", {}).get("value")
            if temp is not None:
                point["air_temp_c"] = temp

            # Pressure (Pa -> hPa)
            pressure_pa = props.get("barometricPressure", {}).get("value")
            if pressure_pa is not None:
                point["pressure_hpa"] = round(pressure_pa / 100, 1)

            # Dew point (C)
            dewpoint = props.get("dewpoint", {}).get("value")
            if dewpoint is not None:
                point["dew_point_c"] = dewpoint

            # Only add if we have wind data
            if "wind_dir" in point or "wind_speed_kts" in point:
                data.append(point)

        except (ValueError, TypeError) as e:
            logger.debug(f"Error parsing NWS observation: {e}")
            continue

    data.sort(key=lambda x: x["unix_ts"])

    # Cache result
    _cache[cache_key] = (datetime.utcnow().timestamp(), data)

    return data


def fetch_nws_data_for_timerange(station_id: str, start_ts: float, end_ts: float) -> list:
    """Fetch NWS station data for a specific time range."""
    buffer_hours = 2
    start_dt = datetime.utcfromtimestamp(start_ts) - timedelta(hours=buffer_hours)

    hours_back = int((datetime.utcnow() - start_dt).total_seconds() / 3600) + 1
    hours_back = min(hours_back, 7 * 24)  # NWS API has ~7 days of history

    all_data = fetch_nws_station_data(station_id, hours_back)

    filtered = [d for d in all_data if start_ts <= d["unix_ts"] <= end_ts]

    return filtered


def get_all_buoys_data(start_ts: float, end_ts: float) -> dict:
    """Fetch data from all Boston Harbor weather stations for a time range."""
    result = {}

    for station_id, meta in BOSTON_BUOYS.items():
        source = meta.get("source", "ndbc")

        try:
            if source == "nws":
                data = fetch_nws_data_for_timerange(station_id, start_ts, end_ts)
            else:
                data = fetch_buoy_data_for_timerange(station_id, start_ts, end_ts)
        except Exception as e:
            logger.warning(f"Error fetching {station_id}: {e}")
            data = []

        result[station_id] = {
            **meta,
            "station_id": station_id,
            "data_points": data,
            "has_data": len(data) > 0,
        }

    return result


def interpolate_buoy_value(data_points: list, target_ts: float, field: str) -> Optional[float]:
    """Interpolate a buoy value at a specific timestamp."""
    if not data_points:
        return None

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

    t1, v1 = before["unix_ts"], before[field]
    t2, v2 = after["unix_ts"], after[field]

    if t2 == t1:
        return v1

    ratio = (target_ts - t1) / (t2 - t1)
    return round(v1 + ratio * (v2 - v1), 2)


def get_buoy_snapshot(buoys_data: dict, target_ts: float) -> dict:
    """Get interpolated buoy values at a specific timestamp."""
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
        snapshot[station_id] = {k: v for k, v in snapshot[station_id].items() if v is not None}

    return snapshot
