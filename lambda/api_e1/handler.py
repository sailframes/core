"""Lambda handler for E1 fleet data API endpoints.

Handles:
- GET /api/e1/devices - List all E1 devices
- GET /api/e1/devices/{device_id}/uploads - List uploads by device
- GET /api/e1/files/{device_id}/{date}/{filename} - Get file download URL
"""

import json
import os
import re
from datetime import datetime, timezone

import boto3

s3 = boto3.client("s3")
DATA_BUCKET = os.environ.get("DATA_BUCKET", "sailframes-fleet-data-prod")


def lambda_handler(event, context):
    """Route requests to appropriate handler."""
    path = event.get("rawPath", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")

    # Parse path parameters
    path_params = event.get("pathParameters", {}) or {}
    query_params = event.get("queryStringParameters", {}) or {}

    try:
        if path == "/api/e1/devices":
            return list_devices()
        elif re.match(r"/api/e1/devices/[^/]+/uploads", path):
            device_id = path_params.get("device_id") or path.split("/")[4]
            return list_uploads(device_id, query_params)
        elif re.match(r"/api/e1/files/", path):
            # Extract device_id, date, filename from path
            parts = path.split("/")
            device_id = parts[4] if len(parts) > 4 else path_params.get("device_id")
            date = parts[5] if len(parts) > 5 else path_params.get("date")
            filename = "/".join(parts[6:]) if len(parts) > 6 else path_params.get("filename")
            return get_file(device_id, date, filename)
        else:
            return response(404, {"error": f"Not found: {path}"})
    except Exception as e:
        print(f"Error: {e}")
        return response(500, {"error": str(e)})


def response(status_code, body):
    """Create API Gateway response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
        },
        "body": json.dumps(body),
    }


def format_bytes(size):
    """Format byte size as human-readable string."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def detect_file_type(filename):
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


def list_objects_with_metadata(prefix):
    """List S3 objects with size and last modified metadata."""
    results = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=DATA_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            results.append({
                "key": obj["Key"],
                "size": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
            })
    return results


def list_devices():
    """List all E1 devices with upload statistics."""
    objects = list_objects_with_metadata("raw/")

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
            "total_size_formatted": format_bytes(device["total_size_bytes"]),
        })

    return response(200, {"devices": sorted(result, key=lambda d: d["device_id"])})


def list_uploads(device_id, query_params):
    """List all uploads for a specific E1 device grouped by date."""
    start_date = query_params.get("start_date")
    end_date = query_params.get("end_date")

    raw_objects = list_objects_with_metadata(f"raw/{device_id}/")
    processed_objects = list_objects_with_metadata(f"processed/{device_id}/")

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
            "file_type": detect_file_type(filename),
            "size_bytes": obj["size"],
            "size_formatted": format_bytes(obj["size"]),
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
            "size_formatted": format_bytes(obj["size"]),
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
            "total_size_formatted": format_bytes(data["total_size_bytes"]),
            "has_manifest": any(f["filename"] == "manifest.json" for f in data["processed_files"]),
        })

    return response(200, {
        "device_id": device_id,
        "uploads": sorted(uploads, key=lambda u: u["date"], reverse=True),
    })


def get_file(device_id, date, filename):
    """Get file metadata and presigned download URL."""
    key = f"raw/{device_id}/{date}/{filename}"

    # Try raw first, then processed
    try:
        try:
            resp = s3.head_object(Bucket=DATA_BUCKET, Key=key)
        except s3.exceptions.ClientError:
            key = f"processed/{device_id}/{date}/{filename}"
            resp = s3.head_object(Bucket=DATA_BUCKET, Key=key)

        # Generate presigned URL
        download_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": DATA_BUCKET, "Key": key},
            ExpiresIn=3600,
        )

        return response(200, {
            "key": key,
            "filename": filename,
            "file_type": detect_file_type(filename),
            "size_bytes": resp["ContentLength"],
            "size_formatted": format_bytes(resp["ContentLength"]),
            "last_modified": resp["LastModified"].isoformat(),
            "content_type": resp.get("ContentType", "application/octet-stream"),
            "download_url": download_url,
            "download_url_expires_in": 3600,
        })
    except Exception as e:
        return response(404, {"error": f"File not found: {filename}"})
