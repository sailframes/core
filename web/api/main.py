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
S3_BUCKET = os.environ.get("SAILFRAMES_BUCKET", "sailframes-fleet-data-prod")
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


def _list_objects_with_metadata(prefix: str) -> list[dict]:
    """List S3 objects with size and last modified metadata."""
    if LOCAL_DATA_DIR:
        base = Path(LOCAL_DATA_DIR) / prefix
        if not base.exists():
            return []
        results = []
        for p in base.rglob("*"):
            if p.is_file():
                stat = p.stat()
                from datetime import datetime
                results.append({
                    "key": str(p.relative_to(Path(LOCAL_DATA_DIR))),
                    "size": stat.st_size,
                    "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat() + "Z",
                })
        return results
    results = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            results.append({
                "key": obj["Key"],
                "size": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
            })
    return results


def _generate_presigned_url(key: str, expiry: int = 3600) -> str:
    """Generate presigned GET URL for S3 object."""
    if LOCAL_DATA_DIR:
        return f"/api/e1/download/{key}"
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=expiry,
    )


def _detect_file_type(filename: str) -> str:
    """Detect E1 file type from filename pattern."""
    if "_nav.csv" in filename:
        return "nav"
    if "_imu.csv" in filename:
        return "imu"
    if "_wind.csv" in filename:
        return "wind"
    if ".rtcm3" in filename:
        return "rtcm3"
    if filename.endswith(".json"):
        return "processed"
    return "unknown"


def _format_bytes(size: int) -> str:
    """Format byte size as human-readable string."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


# --- E1 Fleet Data ---

@app.get("/api/e1/devices")
def list_e1_devices():
    """List all E1 devices with upload statistics."""
    objects = _list_objects_with_metadata("raw/")

    # Group by device_id
    devices = {}
    for obj in objects:
        parts = obj["key"].split("/")
        if len(parts) < 3:
            continue
        device_id = parts[1]
        date = parts[2]

        if device_id not in devices:
            devices[device_id] = {
                "device_id": device_id,
                "dates": set(),
                "total_files": 0,
                "total_size_bytes": 0,
            }

        devices[device_id]["dates"].add(date)
        devices[device_id]["total_files"] += 1
        devices[device_id]["total_size_bytes"] += obj["size"]

    # Convert to list with computed fields
    result = []
    for device in devices.values():
        dates = sorted(device["dates"])
        result.append({
            "device_id": device["device_id"],
            "first_upload": dates[0] if dates else None,
            "last_upload": dates[-1] if dates else None,
            "total_sessions": len(dates),
            "total_files": device["total_files"],
            "total_size_bytes": device["total_size_bytes"],
            "total_size_formatted": _format_bytes(device["total_size_bytes"]),
        })

    return {"devices": sorted(result, key=lambda d: d["device_id"])}


@app.get("/api/e1/devices/{device_id}/uploads")
def list_e1_uploads(
    device_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
):
    """List all uploads for a specific E1 device grouped by date."""
    raw_objects = _list_objects_with_metadata(f"raw/{device_id}/")
    processed_objects = _list_objects_with_metadata(f"processed/{device_id}/")

    # Group raw files by date
    uploads_by_date = {}
    for obj in raw_objects:
        parts = obj["key"].split("/")
        if len(parts) < 4:
            continue
        date = parts[2]
        filename = parts[3]

        # Apply date filters
        if start_date and date < start_date:
            continue
        if end_date and date > end_date:
            continue

        if date not in uploads_by_date:
            uploads_by_date[date] = {
                "date": date,
                "raw_files": [],
                "processed_files": [],
                "total_size_bytes": 0,
            }

        uploads_by_date[date]["raw_files"].append({
            "key": obj["key"],
            "filename": filename,
            "file_type": _detect_file_type(filename),
            "size_bytes": obj["size"],
            "size_formatted": _format_bytes(obj["size"]),
            "last_modified": obj["last_modified"],
        })
        uploads_by_date[date]["total_size_bytes"] += obj["size"]

    # Add processed files
    for obj in processed_objects:
        parts = obj["key"].split("/")
        if len(parts) < 4:
            continue
        date = parts[2]
        filename = parts[3]

        if date not in uploads_by_date:
            continue

        uploads_by_date[date]["processed_files"].append({
            "key": obj["key"],
            "filename": filename,
            "sensor_type": filename.replace(".json", ""),
            "size_bytes": obj["size"],
            "size_formatted": _format_bytes(obj["size"]),
        })

    # Compute summary stats per date
    uploads = []
    for date, data in uploads_by_date.items():
        file_types = {}
        for f in data["raw_files"]:
            ft = f["file_type"]
            file_types[ft] = file_types.get(ft, 0) + 1

        uploads.append({
            **data,
            "file_type_counts": file_types,
            "total_size_formatted": _format_bytes(data["total_size_bytes"]),
            "has_manifest": any(f["filename"] == "manifest.json" for f in data["processed_files"]),
        })

    return {
        "device_id": device_id,
        "uploads": sorted(uploads, key=lambda u: u["date"], reverse=True),
    }


@app.get("/api/e1/files/{device_id}/{date}/{filename:path}")
def get_e1_file(device_id: str, date: str, filename: str):
    """Get file metadata and presigned download URL."""
    key = f"raw/{device_id}/{date}/{filename}"

    # Try raw first, then processed
    try:
        if LOCAL_DATA_DIR:
            path = Path(LOCAL_DATA_DIR) / key
            if not path.exists():
                key = f"processed/{device_id}/{date}/{filename}"
                path = Path(LOCAL_DATA_DIR) / key
            if not path.exists():
                raise HTTPException(404, f"File not found: {filename}")
            stat = path.stat()
            from datetime import datetime
            metadata = {
                "size_bytes": stat.st_size,
                "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat() + "Z",
                "content_type": "text/csv" if filename.endswith(".csv") else "application/octet-stream",
            }
        else:
            try:
                resp = s3.head_object(Bucket=S3_BUCKET, Key=key)
            except Exception:
                key = f"processed/{device_id}/{date}/{filename}"
                resp = s3.head_object(Bucket=S3_BUCKET, Key=key)
            metadata = {
                "size_bytes": resp["ContentLength"],
                "last_modified": resp["LastModified"].isoformat(),
                "content_type": resp.get("ContentType", "application/octet-stream"),
            }
    except Exception as e:
        raise HTTPException(404, f"File not found: {filename}")

    return {
        "key": key,
        "filename": filename,
        "file_type": _detect_file_type(filename),
        "size_bytes": metadata["size_bytes"],
        "size_formatted": _format_bytes(metadata["size_bytes"]),
        "last_modified": metadata["last_modified"],
        "content_type": metadata["content_type"],
        "download_url": _generate_presigned_url(key),
        "download_url_expires_in": 3600,
    }


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
    """Get sensor time-series data for a session, merged by timestamp.

    Returns data in merged format expected by frontend:
    {
        data: [{t, gps: {...}, imu: {...}, pressure: {...}, wind: {...}}, ...],
        start_time: "...",
        end_time: "..."
    }
    """
    # Load each sensor's data
    sensor_data = {}
    for sensor in sensors.split(","):
        sensor = sensor.strip()
        key = f"{DATA_PREFIX}/{device_id}/{date}/{sensor}.json"
        try:
            data = _load_json(key)
            records = data if isinstance(data, list) else data.get("data", [])
            sensor_data[sensor] = records
        except HTTPException:
            sensor_data[sensor] = []

    # Merge by timestamp - collect all unique timestamps
    merged = {}  # timestamp -> {t, gps: {...}, imu: {...}, ...}

    for sensor, records in sensor_data.items():
        for record in records:
            t = record.get("t")
            if not t:
                continue
            if t not in merged:
                merged[t] = {"t": t}
            # Nest sensor data under sensor key (exclude 't' from nested object)
            sensor_record = {k: v for k, v in record.items() if k != "t"}
            merged[t][sensor] = sensor_record

    # Sort by timestamp and convert to list
    data = [merged[t] for t in sorted(merged.keys())]

    # Time filtering using ISO string comparison (works for chronological order)
    # Note: start/end params are currently unused as frontend filters via timeController
    # TODO: Support ISO string time bounds if needed

    # Downsample
    if resolution > 1:
        data = data[::resolution]

    # Calculate time bounds
    start_time = data[0]["t"] if data else None
    end_time = data[-1]["t"] if data else None

    return {
        "data": data,
        "start_time": start_time,
        "end_time": end_time,
    }


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


# --- Session Management ---

def _delete_s3_prefix(prefix: str) -> int:
    """Delete all objects under an S3 prefix. Returns count of deleted objects."""
    if LOCAL_DATA_DIR:
        base = Path(LOCAL_DATA_DIR) / prefix
        count = 0
        if base.exists():
            import shutil
            for p in base.rglob("*"):
                if p.is_file():
                    p.unlink()
                    count += 1
            # Remove empty directories
            shutil.rmtree(base, ignore_errors=True)
        return count

    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        objects = page.get("Contents", [])
        if objects:
            s3.delete_objects(
                Bucket=S3_BUCKET,
                Delete={"Objects": [{"Key": obj["Key"]} for obj in objects]}
            )
            deleted += len(objects)
    return deleted


@app.delete("/api/sessions/{device_id}/{date}")
def delete_session(device_id: str, date: str):
    """Delete a session and all its data (processed folder)."""
    prefix = f"{DATA_PREFIX}/{device_id}/{date}/"
    deleted_count = _delete_s3_prefix(prefix)

    if deleted_count == 0:
        raise HTTPException(404, f"Session not found: {device_id}/{date}")

    return {
        "status": "deleted",
        "device_id": device_id,
        "date": date,
        "files_deleted": deleted_count,
    }


# --- NOAA Buoy Data ---

from .noaa_buoys import (
    BOSTON_BUOYS,
    get_all_buoys_data,
    get_buoy_snapshot,
    fetch_buoy_data_for_timerange,
)


@app.get("/api/buoys")
def list_buoys():
    """List all Boston Harbor area buoys with their metadata."""
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
    return {"buoys": buoys}


@app.get("/api/buoys/data")
def get_buoys_data(
    start_ts: float = Query(..., description="Start timestamp (Unix)"),
    end_ts: float = Query(..., description="End timestamp (Unix)"),
):
    """
    Get NOAA buoy data for all Boston Harbor buoys within a time range.
    Returns metadata and time-series data for each buoy.
    """
    data = get_all_buoys_data(start_ts, end_ts)

    # Format response
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

    return {"buoys": result}


@app.get("/api/buoys/snapshot")
def get_buoys_snapshot(
    timestamp: float = Query(..., description="Target timestamp (Unix)"),
    start_ts: float = Query(None, description="Session start (for caching)"),
    end_ts: float = Query(None, description="Session end (for caching)"),
):
    """
    Get interpolated buoy values at a specific timestamp.
    Useful for real-time display during timeline scrubbing.
    """
    # Use provided range or default to +/- 4 hours
    if start_ts is None:
        start_ts = timestamp - 4 * 3600
    if end_ts is None:
        end_ts = timestamp + 4 * 3600

    buoys_data = get_all_buoys_data(start_ts, end_ts)
    snapshot = get_buoy_snapshot(buoys_data, timestamp)

    return {"timestamp": timestamp, "buoys": snapshot}


@app.get("/api/buoys/{station_id}/data")
def get_single_buoy_data(
    station_id: str,
    start_ts: float = Query(..., description="Start timestamp (Unix)"),
    end_ts: float = Query(..., description="End timestamp (Unix)"),
):
    """Get data for a specific buoy within a time range."""
    if station_id not in BOSTON_BUOYS:
        raise HTTPException(404, f"Unknown buoy: {station_id}")

    meta = BOSTON_BUOYS[station_id]
    data_points = fetch_buoy_data_for_timerange(station_id, start_ts, end_ts)

    return {
        "station_id": station_id,
        "name": meta["name"],
        "lat": meta["lat"],
        "lon": meta["lon"],
        "color": meta["color"],
        "data_points": data_points,
    }


# --- Static files (frontend) ---

frontend_dir = Path(__file__).parent.parent / "frontend" / "dist"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
