#!/usr/bin/env python3
"""
Correct AWA (Apparent Wind Angle) in all wind data files in S3.

The Calypso wind sensor was reporting wind direction 180° inverted.
This script applies the correction: corrected_awa = (raw_awa + 180) % 360

Processes both:
- Raw CSV files: raw/*/wind/*.csv
- Processed JSON files: processed/*/wind.json

Usage:
    python scripts/correct_wind_awa.py --dry-run    # Preview changes
    python scripts/correct_wind_awa.py              # Apply corrections
    python scripts/correct_wind_awa.py --session 2026-04-07-175325  # Single session
"""

import os
import sys
import json
import csv
import argparse
import io
from datetime import datetime

import boto3

BUCKET = 'sailframes-fleet-data-prod'
REGION = 'us-east-2'


def correct_awa(value):
    """Apply 180° correction to AWA value."""
    if value is None:
        return None
    try:
        val = float(value)
        return int((val + 180) % 360)
    except (ValueError, TypeError):
        return value


def process_raw_csv(s3, bucket, key, dry_run=False):
    """Correct AWA in raw wind CSV file."""
    print(f"  Processing raw CSV: {key}")

    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        content = response['Body'].read().decode('utf-8')
    except Exception as e:
        print(f"    Error reading: {e}")
        return False

    lines = content.strip().split('\n')
    if len(lines) < 2:
        print(f"    Skipping: empty file")
        return False

    # Parse header
    reader = csv.DictReader(io.StringIO(content))
    headers = reader.fieldnames

    if not headers:
        print(f"    Skipping: no headers")
        return False

    # Find AWA column
    awa_col = None
    for col in ['awa_deg', 'apparent_wind_angle_deg']:
        if col in headers:
            awa_col = col
            break

    if not awa_col:
        print(f"    Skipping: no AWA column found (headers: {headers})")
        return False

    # Process rows
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()

    corrected_count = 0
    for row in reader:
        if row.get(awa_col):
            original = row[awa_col]
            row[awa_col] = str(correct_awa(original))
            if row[awa_col] != original:
                corrected_count += 1
        writer.writerow(row)

    if corrected_count == 0:
        print(f"    No corrections needed")
        return False

    print(f"    Corrected {corrected_count} rows")

    if not dry_run:
        corrected_content = output.getvalue()
        s3.put_object(Bucket=bucket, Key=key, Body=corrected_content.encode('utf-8'))
        print(f"    Saved")

    return True


def process_wind_json(s3, bucket, key, dry_run=False):
    """Correct AWA in processed wind.json file."""
    print(f"  Processing wind.json: {key}")

    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        content = response['Body'].read().decode('utf-8')
        data = json.loads(content)
    except Exception as e:
        print(f"    Error reading: {e}")
        return False

    if not isinstance(data, list):
        print(f"    Skipping: not a list")
        return False

    corrected_count = 0
    for record in data:
        if 'awa' in record and record['awa'] is not None:
            original = record['awa']
            record['awa'] = correct_awa(original)
            if record['awa'] != original:
                corrected_count += 1

    if corrected_count == 0:
        print(f"    No corrections needed")
        return False

    print(f"    Corrected {corrected_count} records")

    if not dry_run:
        s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(data).encode('utf-8'))
        print(f"    Saved")

    return True


def list_wind_files(s3, bucket, session_filter=None):
    """List all wind data files in S3."""
    raw_files = []
    json_files = []

    # List raw CSV files
    paginator = s3.get_paginator('list_objects_v2')

    for prefix in ['raw/S1/', 'raw/E1/', 'raw/sailframes-01/']:
        try:
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    if session_filter and session_filter not in key:
                        continue
                    if 'wind' in key.lower() and key.endswith('.csv'):
                        raw_files.append(key)
        except Exception:
            pass

    # List processed wind.json files
    for prefix in ['processed/S1/', 'processed/E1/', 'processed/sailframes-01/']:
        try:
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    if session_filter and session_filter not in key:
                        continue
                    if key.endswith('/wind.json'):
                        json_files.append(key)
        except Exception:
            pass

    return raw_files, json_files


def main():
    parser = argparse.ArgumentParser(description='Correct AWA in S3 wind data')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without writing')
    parser.add_argument('--session', type=str, help='Process only specific session (e.g., 2026-04-07-175325)')
    parser.add_argument('--profile', type=str, default='sailframes', help='AWS profile')
    args = parser.parse_args()

    print(f"AWA Correction Script")
    print(f"Bucket: {BUCKET}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    if args.session:
        print(f"Session filter: {args.session}")
    print()

    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    s3 = session.client('s3')

    print("Scanning for wind data files...")
    raw_files, json_files = list_wind_files(s3, BUCKET, args.session)

    print(f"Found {len(raw_files)} raw CSV files")
    print(f"Found {len(json_files)} processed JSON files")
    print()

    if not raw_files and not json_files:
        print("No wind data files found")
        return

    # Process raw CSV files
    if raw_files:
        print("Processing raw CSV files:")
        raw_corrected = 0
        for key in raw_files:
            if process_raw_csv(s3, BUCKET, key, args.dry_run):
                raw_corrected += 1
        print(f"Raw CSV files corrected: {raw_corrected}/{len(raw_files)}")
        print()

    # Process wind.json files
    if json_files:
        print("Processing wind.json files:")
        json_corrected = 0
        for key in json_files:
            if process_wind_json(s3, BUCKET, key, args.dry_run):
                json_corrected += 1
        print(f"JSON files corrected: {json_corrected}/{len(json_files)}")
        print()

    if args.dry_run:
        print("DRY RUN complete. Run without --dry-run to apply changes.")
    else:
        print("Correction complete!")


if __name__ == '__main__':
    main()
