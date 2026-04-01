"""FastAPI backend for SailFrames analysis dashboard.

Serves processed analysis data and manages sessions, boats,
and leaderboard endpoints. Designed to run locally or behind
API Gateway in AWS.
"""

import json
import os
from pathlib import Path

import boto3
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

app = FastAPI(
    title="SailFrames Analysis API",
    version="1.0.0",
    description="Sailboat racing analysis and replay dashboard",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
S3_BUCKET = os.environ.get("SAILFRAMES_BUCKET", "sailframes-data-prod")
DATA_PREFIX = os.environ.get("SAILFRAMES_DATA_PREFIX", "processed")
LOCAL_DATA_DIR = os.environ.get("SAILFRAMES_LOCAL_DATA", None)

s3 = boto3.client("s3") if not LOCAL_DATA_DIR else None


def _load_json(key: str) -> dict:
    """Load JSON from S3 or local filesystem."""
    if LOCAL_DATA_DIR:
        path = Path(LOCAL_DATA_DIR) / key
        if not path.exists():
            raise HTTPException(404, f"Data not found: {key}")
        return json.loads(path.read_text())
    resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return json.loads(resp["Body"].read())


def _list_keys(prefix: str) -> list[str]:
    """List S3 keys or local files under prefix."""
    if LOCAL_DATA_DIR:
        base = Path(LOCAL_DATA_DIR) / prefix
        if not base.exists():
            return []
        return [str(p.relative_to(Path(LOCAL_DATA_DIR))) for p in base.rglob("*") if p.is_file()]
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


# --- Sessions ---

@app.get("/api/sessions")
def list_sessions():
    """List all available race sessions."""
    keys = _list_keys(f"{DATA_PREFIX}/")
    manifests = [k for k in keys if k.endswith("manifest.json")]

    sessions = []
    for key in manifests:
        try:
            manifest = _load_json(key)
            parts = key.split("/")
            device_id = parts[1] if len(parts) > 2 else "unknown"
            date = parts[2] if len(parts) > 2 else "unknown"
            sessions.append({
                "device_id": device_id,
                "date": date,
                "start_time": manifest.get("start_time"),
                "end_time": manifest.get("end_time"),
                "duration_sec": manifest.get("duration_sec"),
                "sensors": manifest.get("sensors", []),
                "has_video": manifest.get("has_video", False),
                "has_analysis": manifest.get("has_analysis", False),
            })
        except Exception:
            continue

    return {"sessions": sorted(sessions, key=lambda s: s["date"], reverse=True)}


@app.get("/api/sessions/{device_id}/{date}")
def get_session(device_id: str, date: str):
    """Get session metadata and manifest."""
    key = f"{DATA_PREFIX}/{device_id}/{date}/manifest.json"
    return _load_json(key)


# --- Sensor Data ---

@app.get("/api/data/{device_id}/{date}")
def get_sensor_data(
    device_id: str,
    date: str,
    sensors: str = Query("gps,imu,wind,pressure", description="Comma-separated sensor list"),
    start: float | None = None,
    end: float | None = None,
    resolution: int = Query(1, description="Downsample factor"),
):
    """Get sensor time-series data for a session."""
    result = {}
    for sensor in sensors.split(","):
        sensor = sensor.strip()
        key = f"{DATA_PREFIX}/{device_id}/{date}/{sensor}.json"
        try:
            data = _load_json(key)
            records = data if isinstance(data, list) else data.get("data", [])

            # Time filtering
            if start is not None:
                records = [r for r in records if r.get("timestamp", 0) >= start]
            if end is not None:
                records = [r for r in records if r.get("timestamp", 0) <= end]

            # Downsample
            if resolution > 1:
                records = records[::resolution]

            result[sensor] = records
        except HTTPException:
            result[sensor] = []

    return result


# --- Analysis ---

@app.get("/api/analysis/{device_id}/{date}")
def get_analysis(device_id: str, date: str):
    """Get full analysis results for a session."""
    key = f"{DATA_PREFIX}/{device_id}/{date}/analysis.json"
    return _load_json(key)


@app.get("/api/analysis/{device_id}/{date}/maneuvers")
def get_maneuvers(device_id: str, date: str):
    """Get maneuver detection results."""
    analysis = get_analysis(device_id, date)
    return {
        "maneuvers": analysis.get("maneuvers", []),
        "summary": analysis.get("maneuver_summary", {}),
    }


@app.get("/api/analysis/{device_id}/{date}/legs")
def get_legs(device_id: str, date: str):
    """Get straight-line leg analysis."""
    analysis = get_analysis(device_id, date)
    return {
        "legs": analysis.get("legs", []),
        "comparison": analysis.get("leg_comparison", {}),
    }


@app.get("/api/analysis/{device_id}/{date}/polar")
def get_polar(device_id: str, date: str):
    """Get polar diagram data."""
    analysis = get_analysis(device_id, date)
    return {"polar": analysis.get("polar", {})}


@app.get("/api/analysis/{device_id}/{date}/stats")
def get_stats(device_id: str, date: str):
    """Get statistical analysis (violin, correlations)."""
    analysis = get_analysis(device_id, date)
    return {
        "violin": analysis.get("violin", {}),
        "correlations": analysis.get("correlations", {}),
        "session_stats": analysis.get("session_stats", {}),
        "leg_ranking": analysis.get("leg_ranking", []),
    }


# --- Boats ---

@app.get("/api/boats")
def list_boats():
    """List all boat profiles."""
    key = f"{DATA_PREFIX}/boats.json"
    try:
        return _load_json(key)
    except HTTPException:
        return {"boats": []}


@app.get("/api/boats/{boat_id}")
def get_boat(boat_id: str):
    """Get a specific boat profile."""
    boats = list_boats()
    for boat in boats.get("boats", []):
        if boat.get("boat_id") == boat_id:
            return boat
    raise HTTPException(404, f"Boat not found: {boat_id}")


# --- Leaderboard ---

@app.get("/api/leaderboard")
def get_leaderboard(
    metric: str = Query("max_speed", description="Ranking metric"),
    boat_class: str | None = None,
    limit: int = 20,
):
    """Get leaderboard rankings across sessions."""
    key = f"{DATA_PREFIX}/leaderboard.json"
    try:
        data = _load_json(key)
    except HTTPException:
        return {"entries": [], "metric": metric}

    entries = data.get("entries", [])

    if boat_class:
        entries = [e for e in entries if e.get("boat_class") == boat_class]

    # Sort by metric
    entries.sort(key=lambda e: e.get(metric, 0), reverse=True)

    return {"entries": entries[:limit], "metric": metric}


# --- Video ---

@app.get("/api/video/{device_id}/{date}")
def get_video(device_id: str, date: str):
    """Get video stream URLs for a session."""
    key = f"{DATA_PREFIX}/{device_id}/{date}/manifest.json"
    manifest = _load_json(key)

    cameras = {}
    for cam in manifest.get("cameras", []):
        cam_name = cam.get("name", "default")
        cameras[cam_name] = {
            "playlist_url": cam.get("playlist_url"),
            "start_time": cam.get("start_time"),
            "end_time": cam.get("end_time"),
            "duration_sec": cam.get("duration_sec"),
        }

    return {"cameras": cameras}


# --- Static files (frontend) ---

frontend_dir = Path(__file__).parent.parent / "frontend" / "dist"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
