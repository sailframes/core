"""
SailFrames API - Race and Regatta endpoints.
Handles CRUD operations for races/regattas, multi-boat data loading,
and GPX track uploads as a GPS source for boats without E1 devices.
"""

import base64
import gzip
import json
import logging
import math
import os
import re
import struct
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
DATA_BUCKET = os.environ.get('DATA_BUCKET', 'sailframes-fleet-data-prod')

# S3 paths for race data
RACES_INDEX_KEY = "races/races.json"
REGATTAS_INDEX_KEY = "regattas/regattas.json"
RACEDAYS_INDEX_KEY = "racedays/racedays.json"
BOATS_INDEX_KEY = "boats/boats.json"

CORS_HEADERS = {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, PATCH, DELETE, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type'
}


def lambda_handler(event, context):
    """Handle race API requests."""
    http_method = event.get('requestContext', {}).get('http', {}).get('method') or event.get('httpMethod', 'GET')
    path = event.get('rawPath', '') or event.get('path', '')
    path_params = event.get('pathParameters', {}) or {}

    logger.info(f"Request: {http_method} {path} params={path_params}")

    # Handle CORS preflight
    if http_method == 'OPTIONS':
        return {'statusCode': 200, 'headers': CORS_HEADERS, 'body': '{}'}

    try:
        # Route to appropriate handler
        # Regattas
        if '/api/regattas' in path:
            if http_method == 'GET' and not path_params.get('regatta_id'):
                return list_regattas()
            elif http_method == 'GET':
                return get_regatta(path_params['regatta_id'])
            elif http_method == 'POST':
                return create_regatta(json.loads(event.get('body', '{}')))
            elif http_method == 'PATCH':
                return update_regatta(path_params['regatta_id'], json.loads(event.get('body', '{}')))
            elif http_method == 'DELETE':
                return delete_regatta(path_params['regatta_id'])

        # Race Days — MUST be checked before /api/races because the
        # string '/api/races' is a substring of '/api/racedays', so the
        # race branch would otherwise swallow these requests.
        elif '/api/racedays' in path:
            raceday_id = path_params.get('raceday_id')
            if http_method == 'GET' and not raceday_id:
                qs = event.get('queryStringParameters', {}) or {}
                return list_racedays(qs.get('regatta_id'), qs.get('date'))
            elif http_method == 'GET':
                return get_raceday(raceday_id)
            elif http_method == 'POST':
                return create_raceday(json.loads(event.get('body', '{}')))
            elif http_method == 'PATCH':
                return update_raceday(raceday_id, json.loads(event.get('body', '{}')))
            elif http_method == 'DELETE':
                return delete_raceday(raceday_id)

        # Boats catalog — first-class entity for cross-race history,
        # per-boat metadata (LOA, photos, links, skipper).
        elif '/api/boats' in path:
            boat_id = path_params.get('boat_id')
            # If API Gateway didn't supply the path-param mapping (the
            # `{boat_id}` proxy route is configured per-method), pluck
            # the id straight from the URL — same fallback the GPX
            # endpoint uses for device_id.
            if not boat_id:
                m = re.search(r'/api/boats/([^/]+)', path)
                if m:
                    boat_id = m.group(1)

            # Photo upload: POST /api/boats/{boat_id}/photo/{slot}
            if boat_id and '/photo/' in path and http_method == 'POST':
                slot = path_params.get('slot')
                if not slot:
                    m = re.search(r'/photo/([^/]+)', path)
                    slot = m.group(1) if m else None
                return upload_boat_photo(boat_id, slot, event)

            # Race history: GET /api/boats/{boat_id}/races
            if boat_id and '/races' in path and http_method == 'GET':
                return get_boat_race_history(boat_id)

            # CRUD
            if http_method == 'GET' and not boat_id:
                qs = event.get('queryStringParameters', {}) or {}
                return list_boats(qs.get('q'), qs.get('sail_number'))
            elif http_method == 'GET':
                return get_boat(boat_id)
            elif http_method == 'POST':
                return create_boat(json.loads(event.get('body', '{}')))
            elif http_method == 'PATCH':
                return update_boat(boat_id, json.loads(event.get('body', '{}')))
            elif http_method == 'DELETE':
                return delete_boat(boat_id)

        # Races
        elif '/api/races' in path:
            race_id = path_params.get('race_id')

            # GPX upload by boat_id (no fleet device required):
            # POST /api/races/{race_id}/boats-by-id/{boat_id}/gpx
            # Used for handicap-fleet boats that bring their own GPS
            # data (phone app, Sailmon, Garmin, RaceQs, etc.) and are
            # not on one of the SailFrames E1..E6 trackers.
            if race_id and '/boats-by-id/' in path and '/gpx' in path and http_method == 'POST':
                m = re.search(r'/boats-by-id/([^/]+)/gpx', path)
                boat_id = path_params.get('boat_id') or (m.group(1) if m else None)
                if not boat_id:
                    return response(400, {'error': 'boat_id required'})
                return upload_boat_gpx_by_id(race_id, boat_id, event)

            # VKX (Vakaros Atlas binary) upload by boat_id:
            # POST /api/races/{race_id}/boats-by-id/{boat_id}/vkx
            # Parser converts telemetry records to the same shape as
            # the GPX upload path and stores at the same by-boat-id
            # location, so the rest of the dashboard treats it
            # identically.
            if race_id and '/boats-by-id/' in path and '/vkx' in path and http_method == 'POST':
                m = re.search(r'/boats-by-id/([^/]+)/vkx', path)
                boat_id = path_params.get('boat_id') or (m.group(1) if m else None)
                if not boat_id:
                    return response(400, {'error': 'boat_id required'})
                return upload_boat_vkx_by_id(race_id, boat_id, event)

            # GPX upload: POST /api/races/{race_id}/boats/{device_id}/gpx
            if race_id and '/gpx' in path and http_method == 'POST':
                device_id = path_params.get('device_id')
                if not device_id:
                    m = re.search(r'/boats/([^/]+)/gpx', path)
                    device_id = m.group(1) if m else None
                if not device_id:
                    return response(400, {'error': 'device_id required'})
                return upload_boat_gpx(race_id, device_id, event)

            # Match sessions endpoint
            if race_id and '/match-sessions' in path and http_method == 'POST':
                return match_sessions(race_id)

            # Course auto-suggest endpoints
            if race_id and '/auto-start-line' in path and http_method == 'POST':
                return auto_start_line(race_id)
            if race_id and '/suggest-marks' in path and http_method == 'POST':
                return suggest_marks(race_id)

            # GPX status/debug endpoint
            if race_id and '/gpx-status' in path and http_method == 'GET':
                return get_gpx_status(race_id)

            # Race data endpoint
            if race_id and '/data' in path and http_method == 'GET':
                qs = event.get('queryStringParameters', {}) or {}
                sensors = qs.get('sensors', 'gps,imu,wind')
                pad_start = int(qs.get('pad_start', '0') or 0)
                pad_end = int(qs.get('pad_end', '0') or 0)
                return get_race_data(race_id, sensors, pad_start, pad_end)

            # AIS traffic (non-participant vessels) endpoint. Kept separate
            # from /data per-boat sensors so it can NEVER enter the
            # leaderboard/charts. Optional ?start=&end= clip the per-vessel
            # tracks to a sub-window.
            if race_id and path.endswith('/ais') and http_method == 'GET':
                qs = event.get('queryStringParameters', {}) or {}
                return get_race_ais(race_id, qs.get('start'), qs.get('end'))

            # CRUD operations
            if http_method == 'GET' and not race_id:
                qs = event.get('queryStringParameters', {}) or {}
                return list_races(qs.get('regatta_id'), qs.get('date'))
            elif http_method == 'GET':
                return get_race(race_id)
            elif http_method == 'POST':
                return create_race(json.loads(event.get('body', '{}')))
            elif http_method == 'PATCH':
                return update_race(race_id, json.loads(event.get('body', '{}')))
            elif http_method == 'DELETE':
                return delete_race(race_id)

        return response(404, {'error': 'Not found'})

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return response(500, {'error': str(e)})


# API Gateway / Lambda enforces a hard 6 MB synchronous response body
# limit. The /races/{id}/data endpoint returns GPS+IMU+wind for all
# boats in the race window — for a J/80 fleet of 6 over an 80-minute
# race that's ~8 MB of raw JSON. Anything past 6 MB causes the runtime
# to exit with `Runtime.ExitError` *before* lambda_handler's exception
# handler runs, so the only visible symptom is the API Gateway's
# generic 500 "Internal Server Error" with nothing logged.
#
# We gzip any response body larger than ~256 KB. JSON compresses
# 5–10× → the wire payload comfortably fits under the cap and the
# browser auto-decompresses via Content-Encoding: gzip. Below 256 KB
# we skip the compression overhead.
_GZIP_THRESHOLD_BYTES = 256 * 1024


def response(status_code, body):
    body_str = json.dumps(body)
    if len(body_str) <= _GZIP_THRESHOLD_BYTES:
        return {
            'statusCode': status_code,
            'headers': CORS_HEADERS,
            'body': body_str,
        }
    compressed = gzip.compress(body_str.encode('utf-8'), compresslevel=6)
    return {
        'statusCode': status_code,
        'headers': {**CORS_HEADERS, 'Content-Encoding': 'gzip'},
        'body': base64.b64encode(compressed).decode('ascii'),
        'isBase64Encoded': True,
    }


def now_iso():
    return datetime.utcnow().isoformat() + "Z"


def load_json(key):
    try:
        resp = s3.get_object(Bucket=DATA_BUCKET, Key=key)
        return json.loads(resp['Body'].read())
    except s3.exceptions.NoSuchKey:
        return {}
    except Exception:
        return {}


def save_json(key, data):
    s3.put_object(
        Bucket=DATA_BUCKET,
        Key=key,
        Body=json.dumps(data, indent=2),
        ContentType='application/json'
    )


def delete_json(key):
    try:
        s3.delete_object(Bucket=DATA_BUCKET, Key=key)
    except Exception:
        pass


# --- Regattas ---

def list_regattas():
    data = load_json(REGATTAS_INDEX_KEY)
    return response(200, {'regattas': data.get('regattas', [])})


def get_regatta(regatta_id):
    data = load_json(REGATTAS_INDEX_KEY)
    for regatta in data.get('regattas', []):
        if regatta['regatta_id'] == regatta_id:
            races_data = load_json(RACES_INDEX_KEY)
            races = [r for r in races_data.get('races', []) if r.get('regatta_id') == regatta_id]
            return response(200, {**regatta, 'races': races})
    return response(404, {'error': f'Regatta not found: {regatta_id}'})


def create_regatta(body):
    regatta_id = str(uuid.uuid4())[:8]
    now = now_iso()

    new_regatta = {
        'regatta_id': regatta_id,
        'name': body.get('name', ''),
        'venue': body.get('venue', ''),
        # boat_class may arrive as a legacy free-text string ("J/80") or
        # the structured {id, name, loa_m, bow_offset_m} object — stored
        # opaque so existing records keep working.
        'boat_class': body.get('boat_class', ''),
        # Handicap rating system used by this regatta — "ORR-EZ", "PHRF",
        # "One-Design", or other. Defaults each race's classes[*].rating_system
        # at read time via _hydrate_rating_system(); a class with an explicit
        # value overrides the regatta default.
        'rating_system': body.get('rating_system') or 'ORR-EZ',
        # Pre-start signal sequence length in minutes. 5 = RRS 26 (keelboat
        # standard: warning at −5:00, prep at −4:00, prep down at −1:00,
        # gun at 0). 3 = RRS Appendix S (dinghy / one-design rabbit-style).
        # Hydrated into race.start_sequence_minutes when not set per-race;
        # drives the start-review countdown anchor + horn pattern.
        'start_sequence_minutes': body.get('start_sequence_minutes') or 5,
        'start_date': body.get('start_date', ''),
        'end_date': body.get('end_date', ''),
        # Optional public-facing docs the race dashboard surfaces as
        # click-through links (open in a new tab).
        'nor_url': body.get('nor_url'),
        'si_url': body.get('si_url'),
        'website_url': body.get('website_url'),
        # Visibility gate. 'admin' = hidden from non-admins in the events
        # browser + race.html regatta picker and site-wide auto-load. This
        # is frontend-only obscurity (the API itself stays open — a direct
        # regatta/race id is still fetchable). Anything else / absent = public.
        'visibility': body.get('visibility') or 'public',
        'race_ids': [],
        'created_at': now,
        'updated_at': now,
    }

    data = load_json(REGATTAS_INDEX_KEY)
    if not data:
        data = {'regattas': []}
    data['regattas'].append(new_regatta)
    save_json(REGATTAS_INDEX_KEY, data)

    return response(201, new_regatta)


def update_regatta(regatta_id, body):
    data = load_json(REGATTAS_INDEX_KEY)
    for i, regatta in enumerate(data.get('regattas', [])):
        if regatta['regatta_id'] == regatta_id:
            # boat_class is special-cased: it's allowed to be set to an
            # empty object or `null`, so we let it through with a
            # presence check (not the truthy `is not None`).
            for key in ['name', 'venue', 'boat_class', 'rating_system',
                        'start_sequence_minutes',
                        'start_date', 'end_date',
                        'nor_url', 'si_url', 'website_url', 'visibility']:
                if key in body:
                    regatta[key] = body[key]
            regatta['updated_at'] = now_iso()
            data['regattas'][i] = regatta
            save_json(REGATTAS_INDEX_KEY, data)
            return response(200, regatta)
    return response(404, {'error': f'Regatta not found: {regatta_id}'})


def delete_regatta(regatta_id):
    data = load_json(REGATTAS_INDEX_KEY)
    original_len = len(data.get('regattas', []))
    data['regattas'] = [r for r in data.get('regattas', []) if r['regatta_id'] != regatta_id]
    if len(data['regattas']) == original_len:
        return response(404, {'error': f'Regatta not found: {regatta_id}'})
    save_json(REGATTAS_INDEX_KEY, data)
    return response(200, {'deleted': regatta_id})


# --- Boats catalog ---
#
# Boats are first-class entities that live independently of any race.
# A boat doc holds identity + photos + links + per-boat properties
# (LOA, default skipper). Race entries reference boats by boat_id;
# per-race state (rating, device, finish_time, session_path) stays
# on the race entry. Legacy races without boat_id keep working through
# embedded fields on race.boats[].

_BOAT_FIELDS = [
    'name', 'type', 'sail_number', 'club', 'loa_m',
    # skippers (new) is the authoritative list [{name, photo}, …].
    # skipper (legacy) is a flattened display string kept in sync for
    # back-compat with old race code that reads boat.skipper directly.
    'skippers', 'skipper',
    'photos', 'links', 'notes',
    # ORR-EZ certificate page (regattaman.com/cert_form.php?sku=…) —
    # one per boat. Dedicated field so the UI can render a single
    # "🏷 Cert" button rather than digging through links[].
    'cert_url',
    # MBSA Boat Finder page
    # (members.massbaysailing.org/members/boatfinder/UUID) — the MBSA
    # club directory page for this boat. Same dedicated-field pattern
    # as cert_url.
    'mbsa_url',
    # ORR-EZ polar speed table — populated by the scraper from the
    # cert URL. Schema: {twa_values, tws_values, spin: {twa,tws: s_per_nm},
    # nospin: {...}, opt_beat, opt_run, source_url, scraped_at}. The
    # race-page polar overlay reads this directly.
    'polar',
]


def _normalize_boat_skippers(boat):
    """Ensure both the new skippers[] array and the legacy `skipper`
    string are set and consistent. Mutates the dict in place. Called
    on every read and write so old + new clients see the same shape."""
    skippers = boat.get('skippers')
    if not isinstance(skippers, list):
        skippers = []
    skippers = [s for s in skippers if isinstance(s, dict)]
    # Trim to 2 — UI assumes at most two co-skippers.
    skippers = skippers[:2]

    # If skippers[] is empty but the legacy `skipper` string is set,
    # synthesize entries from it (split on "&" for "A & B" form).
    if not skippers and boat.get('skipper'):
        parts = [p.strip() for p in re.split(r'\s*&\s*', boat['skipper']) if p.strip()]
        skippers = [{'name': p, 'photo': None} for p in parts[:2]]
        # Pull a legacy single-skipper photo into the first entry.
        legacy_photo = (boat.get('photos') or {}).get('skipper')
        if skippers and legacy_photo:
            skippers[0]['photo'] = legacy_photo

    # Pull per-slot photos from the photos dict into the array. Also
    # handle the legacy `skipper` slot — old uploads went there before
    # we split into skipper1/skipper2, so we migrate it to skipper1 on
    # the first read so the new UI sees it.
    photos = boat.get('photos') or {}
    if photos.get('skipper') and not photos.get('skipper1'):
        photos['skipper1'] = photos['skipper']
    for i, s in enumerate(skippers):
        slot_key = f'skipper{i+1}'
        if s.get('photo') is None and photos.get(slot_key):
            s['photo'] = photos[slot_key]

    boat['skippers'] = skippers
    # Mirror back into the legacy fields so old readers still work.
    boat['skipper'] = ' & '.join(s.get('name', '') for s in skippers if s.get('name'))
    if skippers:
        photos.setdefault('skipper1', skippers[0].get('photo'))
        if len(skippers) >= 2:
            photos.setdefault('skipper2', skippers[1].get('photo'))
        # Legacy `skipper` photo = the first skipper's photo.
        photos.setdefault('skipper', skippers[0].get('photo'))
    boat['photos'] = photos
    return boat


def list_boats(q=None, sail_number=None):
    data = load_json(BOATS_INDEX_KEY)
    boats = data.get('boats', [])
    if sail_number:
        boats = [b for b in boats if str(b.get('sail_number', '')).strip() == sail_number.strip()]
    if q:
        ql = q.lower()
        boats = [b for b in boats
                 if ql in (b.get('name', '') or '').lower()
                 or ql in (b.get('type', '') or '').lower()
                 or ql in str(b.get('sail_number', '')).lower()
                 or ql in (b.get('club', '') or '').lower()]
    boats = sorted(boats, key=lambda b: (b.get('name') or '').lower())
    return response(200, {'boats': boats})


def get_boat(boat_id):
    boat = load_json(f'boats/{boat_id}.json')
    if not boat:
        return response(404, {'error': f'Boat not found: {boat_id}'})
    return response(200, _normalize_boat_skippers(boat))


def create_boat(body):
    boat_id = str(uuid.uuid4())[:8]
    now = now_iso()
    boat = {
        'boat_id': boat_id,
        'name': body.get('name', ''),
        'type': body.get('type', ''),
        'sail_number': body.get('sail_number', ''),
        'club': body.get('club', ''),
        'loa_m': body.get('loa_m'),
        'skippers': body.get('skippers') or [],
        'skipper': body.get('skipper', ''),
        # Photos are URLs (or null). Uploaded via the photo endpoint
        # which writes to boats/<id>/photos/<slot>.<ext> and rewrites
        # the URL into this dict. Slots: boat, skipper1, skipper2
        # (plus a legacy `skipper` alias = skipper1).
        'photos': body.get('photos') or {'boat': None, 'skipper1': None, 'skipper2': None},
        'cert_url': body.get('cert_url', ''),
        'mbsa_url': body.get('mbsa_url', ''),
        'links': body.get('links') or [],
        'notes': body.get('notes', ''),
        'created_at': now,
        'updated_at': now,
    }
    _normalize_boat_skippers(boat)
    save_json(f'boats/{boat_id}.json', boat)
    _upsert_boat_index(boat)
    return response(201, boat)


def update_boat(boat_id, body):
    boat = load_json(f'boats/{boat_id}.json')
    if not boat:
        return response(404, {'error': f'Boat not found: {boat_id}'})
    for k in _BOAT_FIELDS:
        if k in body:
            boat[k] = body[k]
    _normalize_boat_skippers(boat)
    boat['updated_at'] = now_iso()
    save_json(f'boats/{boat_id}.json', boat)
    _upsert_boat_index(boat)
    return response(200, boat)


def delete_boat(boat_id):
    boat = load_json(f'boats/{boat_id}.json')
    if not boat:
        return response(404, {'error': f'Boat not found: {boat_id}'})
    delete_json(f'boats/{boat_id}.json')
    # Best-effort photo cleanup — boats/{id}/photos/* objects. List
    # then delete; failures here don't block the catalog deletion.
    try:
        resp = s3.list_objects_v2(Bucket=DATA_BUCKET, Prefix=f'boats/{boat_id}/photos/')
        for obj in resp.get('Contents', []):
            s3.delete_object(Bucket=DATA_BUCKET, Key=obj['Key'])
    except Exception as e:
        logger.warning(f"Boat photo cleanup failed for {boat_id}: {e}")
    # Remove from index
    data = load_json(BOATS_INDEX_KEY)
    data['boats'] = [b for b in data.get('boats', []) if b.get('boat_id') != boat_id]
    save_json(BOATS_INDEX_KEY, data)
    return response(200, {'deleted': boat_id})


def _upsert_boat_index(boat):
    """Keep boats/boats.json in sync. Stores summary fields only."""
    data = load_json(BOATS_INDEX_KEY) or {'boats': []}
    summary = {
        'boat_id': boat['boat_id'],
        'name': boat.get('name', ''),
        'type': boat.get('type', ''),
        'sail_number': boat.get('sail_number', ''),
        'club': boat.get('club', ''),
        'loa_m': boat.get('loa_m'),
        'skipper': boat.get('skipper', ''),
        'skippers': boat.get('skippers') or [],
        'cert_url': boat.get('cert_url') or '',
        'mbsa_url': boat.get('mbsa_url') or '',
        'polar': boat.get('polar') or None,
        'photos': boat.get('photos') or {},
    }
    boats = data.get('boats', [])
    for i, b in enumerate(boats):
        if b.get('boat_id') == boat['boat_id']:
            boats[i] = summary
            break
    else:
        boats.append(summary)
    data['boats'] = boats
    save_json(BOATS_INDEX_KEY, data)


def _hydrate_start_sequence(race_data):
    """Fill in race.start_sequence_minutes from the parent regatta.
    Per-race override wins; missing field on both = caller defaults
    to 5 (RRS 26). Same lookup pattern as _hydrate_rating_system."""
    if race_data.get('start_sequence_minutes'):
        return race_data
    regatta_id = race_data.get('regatta_id')
    if not regatta_id:
        return race_data
    index = load_json(REGATTAS_INDEX_KEY) or {}
    default = next(
        (r.get('start_sequence_minutes')
         for r in index.get('regattas', [])
         if r.get('regatta_id') == regatta_id and r.get('start_sequence_minutes')),
        None,
    )
    if default:
        race_data['start_sequence_minutes'] = default
    return race_data


def _hydrate_rating_system(race_data):
    """Fill in classes[*].rating_system from the parent regatta's
    default. Per-class values (when explicitly set) always win. Lets
    the regatta editor pick "ORR-EZ" / "PHRF" / etc. once and have
    every race inherit it without per-class re-entry, while still
    allowing a class to override when a regatta mixes systems."""
    classes = race_data.get('classes') or []
    if not classes:
        return race_data
    if all(c.get('rating_system') for c in classes):
        return race_data
    regatta_id = race_data.get('regatta_id')
    if not regatta_id:
        return race_data
    index = load_json(REGATTAS_INDEX_KEY) or {}
    default = next(
        (r.get('rating_system') for r in index.get('regattas', [])
         if r.get('regatta_id') == regatta_id and r.get('rating_system')),
        None,
    )
    if not default:
        return race_data
    for c in classes:
        if not c.get('rating_system'):
            c['rating_system'] = default
    return race_data


def _hydrate_polars(race_data):
    """Fill in boats[*].polar from the boats catalog index. Per-race
    boat entries reference catalog rows by boat_id and don't bake in
    the polar — that way a re-scrape of the cert (which updates the
    catalog) shows up immediately on old races too. Mutates in place
    and returns race_data. Race-level polars (if ever set) win over
    the catalog version."""
    boats = race_data.get('boats') or []
    needed = {b.get('boat_id') for b in boats
              if b.get('boat_id') and not b.get('polar')}
    if not needed:
        return race_data
    index = load_json(BOATS_INDEX_KEY) or {}
    polar_by_id = {
        b.get('boat_id'): b.get('polar')
        for b in index.get('boats', [])
        if b.get('boat_id') in needed and b.get('polar')
    }
    if not polar_by_id:
        return race_data
    for b in boats:
        bid = b.get('boat_id')
        if bid and not b.get('polar') and polar_by_id.get(bid):
            b['polar'] = polar_by_id[bid]
    return race_data


def upload_boat_photo(boat_id, slot, event):
    """Multipart upload → boats/<id>/photos/<slot>.<ext> with public-read.
    Slots: boat, skipper1, skipper2 (skipper kept as a legacy alias for
    skipper1 so old clients don't break)."""
    if slot == 'skipper':
        slot = 'skipper1'
    if slot not in ('boat', 'skipper1', 'skipper2'):
        return response(400, {'error': "slot must be 'boat', 'skipper1', or 'skipper2'"})
    boat = load_json(f'boats/{boat_id}.json')
    if not boat:
        return response(404, {'error': f'Boat not found: {boat_id}'})

    file_bytes, filename, content_type = _extract_multipart_file_with_meta(event)
    if not file_bytes:
        return response(400, {'error': 'No file received — POST multipart/form-data with field "file"'})

    # Pick extension from filename, fall back to .jpg
    ext = 'jpg'
    if filename and '.' in filename:
        ext = filename.rsplit('.', 1)[-1].lower()
        if ext not in ('jpg', 'jpeg', 'png', 'webp', 'heic'):
            ext = 'jpg'

    key = f'boats/{boat_id}/photos/{slot}.{ext}'
    ct = content_type or {
        'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
        'webp': 'image/webp', 'heic': 'image/heic',
    }.get(ext, 'image/jpeg')

    s3.put_object(
        Bucket=DATA_BUCKET, Key=key, Body=file_bytes, ContentType=ct,
        CacheControl='public, max-age=300',
    )

    url = f'https://{DATA_BUCKET}.s3.us-east-1.amazonaws.com/{key}?t={int(datetime.utcnow().timestamp())}'
    boat.setdefault('photos', {})[slot] = url
    # For skipper slots, also push the URL into the matching skippers[]
    # entry so the array (authoritative for the new UI) stays in sync
    # with the photos dict (read by legacy code).
    if slot in ('skipper1', 'skipper2'):
        i = 0 if slot == 'skipper1' else 1
        skippers = boat.get('skippers') or []
        # Grow the array if needed — uploading a skipper2 photo before
        # a skipper2 name exists shouldn't crash; the entry stays
        # name-less and the user can fill it in later via PATCH.
        while len(skippers) <= i:
            skippers.append({'name': '', 'photo': None})
        skippers[i]['photo'] = url
        boat['skippers'] = skippers
    _normalize_boat_skippers(boat)
    boat['updated_at'] = now_iso()
    save_json(f'boats/{boat_id}.json', boat)
    _upsert_boat_index(boat)
    return response(200, {'boat_id': boat_id, 'slot': slot, 'url': url})


def _extract_multipart_file_with_meta(event):
    """Same as _extract_multipart_file but also returns filename + content-type."""
    headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
    content_type = headers.get('content-type', '')

    body_raw = event.get('body', '') or ''
    if event.get('isBase64Encoded'):
        body_bytes = base64.b64decode(body_raw)
    else:
        body_bytes = body_raw.encode('latin-1')

    boundary = None
    for part in content_type.split(';'):
        part = part.strip()
        if part.lower().startswith('boundary='):
            boundary = part[9:].strip('"\'')
            break
    if not boundary:
        return (body_bytes if body_bytes else None, None, None)

    delimiter = ('--' + boundary).encode('latin-1')
    parts = body_bytes.split(delimiter)

    for part in parts[1:]:
        if part in (b'--', b'--\r\n', b''):
            continue
        sep = b'\r\n\r\n'
        if sep not in part:
            continue
        headers_blob, file_body = part.split(sep, 1)
        file_body = file_body.rstrip(b'\r\n')
        if not file_body:
            continue
        # Parse Content-Disposition: form-data; name="file"; filename="x.jpg"
        filename = None
        ct = None
        for h in headers_blob.split(b'\r\n'):
            hl = h.decode('latin-1', 'ignore').lower()
            if hl.startswith('content-disposition'):
                m = re.search(r'filename="([^"]+)"', h.decode('latin-1', 'ignore'))
                if m:
                    filename = m.group(1)
            elif hl.startswith('content-type:'):
                ct = h.decode('latin-1', 'ignore').split(':', 1)[1].strip()
        return (file_body, filename, ct)
    return (None, None, None)


def get_boat_race_history(boat_id):
    """Walk every race in the index, look for boats[*].boat_id == boat_id.
    Returns [{race_id, name, date, regatta_id, class, rating, finish_time,
              finish_status, place}] sorted newest first.

    OK at ~50 races. If this gets large we'll add a denormalized
    boat_id → race_ids index updated at race save time."""
    index = load_json(RACES_INDEX_KEY)
    races = index.get('races', [])
    out = []
    for r in races:
        race_id = r.get('race_id')
        race = load_json(f'races/{race_id}/race.json')
        if not race:
            continue
        for b in race.get('boats', []):
            if b.get('boat_id') != boat_id:
                continue
            # Determine handicap place within class if we can. Otherwise
            # leave place=None and let the UI render '—'.
            place = _compute_handicap_place(race, b)
            out.append({
                'race_id': race_id,
                'name': race.get('name'),
                'date': race.get('date'),
                'regatta_id': race.get('regatta_id'),
                'class': b.get('class'),
                'rating': b.get('rating'),
                'finish_time': b.get('finish_time'),
                'finish_status': b.get('finish_status'),
                'device_id': b.get('device_id'),
                'place': place,
            })
            break
    out.sort(key=lambda x: (x.get('date') or '', x.get('name') or ''), reverse=True)
    return response(200, {'boat_id': boat_id, 'races': out})


def _compute_handicap_place(race, target_boat):
    """Mirror the frontend's handicap (TOT) ranking so the history row
    can show '3rd in B'. Same elapsed×rating math whether the regatta
    is scored ORR-EZ, PHRF, or anything else with a TOT multiplier.
    Returns 1-based int or None."""
    if target_boat.get('finish_status') != 'FIN':
        return None
    if not target_boat.get('finish_time'):
        return None
    cls = target_boat.get('class')
    if not cls:
        return None
    classes = race.get('classes') or []
    start_iso = next((c.get('start_time') for c in classes if c.get('id') == cls), None)
    if not start_iso:
        return None
    try:
        start_dt = datetime.fromisoformat(start_iso.replace('Z', '+00:00'))
    except Exception:
        return None

    rows = []
    for b in race.get('boats', []):
        if b.get('class') != cls or b.get('finish_status') != 'FIN' or not b.get('finish_time'):
            continue
        try:
            fdt = datetime.fromisoformat(b['finish_time'].replace('Z', '+00:00'))
        except Exception:
            continue
        elapsed = (fdt - start_dt).total_seconds()
        rating = b.get('rating')
        if not isinstance(rating, (int, float)):
            continue
        rows.append((b, elapsed * rating))
    rows.sort(key=lambda x: x[1])
    for i, (b, _) in enumerate(rows, start=1):
        if b is target_boat or (
            b.get('sail_number') == target_boat.get('sail_number')
            and b.get('boat_name') == target_boat.get('boat_name')
        ):
            return i
    return None


# --- Race Days ---
#
# A race day is an explicit "this is the calendar day on which we plan
# to race" entity that groups one or more races. Most days are
# auto-synthesized from race dates by events.html when no explicit
# raceday row exists, but admins can also create named days
# ("Day 1 — Race Day") in advance, before any race exists for them.

def list_racedays(regatta_id=None, date=None):
    data = load_json(RACEDAYS_INDEX_KEY)
    days = data.get('race_days', [])
    if regatta_id:
        days = [d for d in days if d.get('regatta_id') == regatta_id]
    if date:
        days = [d for d in days if d.get('date') == date]
    days = sorted(days, key=lambda d: (d.get('date') or '', d.get('created_at') or ''))
    return response(200, {'race_days': days})


def get_raceday(raceday_id):
    data = load_json(RACEDAYS_INDEX_KEY)
    for d in data.get('race_days', []):
        if d.get('raceday_id') == raceday_id:
            return response(200, d)
    return response(404, {'error': f'Race day not found: {raceday_id}'})


def create_raceday(body):
    if not body.get('date'):
        return response(400, {'error': 'date is required'})

    raceday_id = str(uuid.uuid4())[:8]
    now = now_iso()
    new_day = {
        'raceday_id': raceday_id,
        'date': body.get('date', ''),
        'type': body.get('type') or 'race_day',
        'name': body.get('name') or None,
        'regatta_id': body.get('regatta_id') or None,
        'race_ids': body.get('race_ids', []) or [],
        'created_at': now,
        'updated_at': now,
    }

    data = load_json(RACEDAYS_INDEX_KEY)
    if not data:
        data = {'race_days': []}
    data.setdefault('race_days', []).append(new_day)
    save_json(RACEDAYS_INDEX_KEY, data)
    return response(201, new_day)


def update_raceday(raceday_id, body):
    if not raceday_id:
        return response(400, {'error': 'raceday_id is required'})
    data = load_json(RACEDAYS_INDEX_KEY)
    days = data.get('race_days', [])
    for i, d in enumerate(days):
        if d.get('raceday_id') == raceday_id:
            # `regatta_id` is allowed to be set to null (un-link from
            # a regatta), so use presence rather than truthiness.
            for key in ['date', 'type', 'name', 'regatta_id', 'race_ids']:
                if key in body:
                    d[key] = body[key]
            d['updated_at'] = now_iso()
            days[i] = d
            data['race_days'] = days
            save_json(RACEDAYS_INDEX_KEY, data)
            return response(200, d)
    return response(404, {'error': f'Race day not found: {raceday_id}'})


def delete_raceday(raceday_id):
    if not raceday_id:
        return response(400, {'error': 'raceday_id is required'})
    data = load_json(RACEDAYS_INDEX_KEY)
    original_len = len(data.get('race_days', []))
    data['race_days'] = [d for d in data.get('race_days', []) if d.get('raceday_id') != raceday_id]
    if len(data['race_days']) == original_len:
        return response(404, {'error': f'Race day not found: {raceday_id}'})
    save_json(RACEDAYS_INDEX_KEY, data)
    return response(200, {'deleted': raceday_id})


# --- Races ---

def list_races(regatta_id=None, date=None):
    data = load_json(RACES_INDEX_KEY)
    races = data.get('races', [])

    if regatta_id:
        races = [r for r in races if r.get('regatta_id') == regatta_id]
    if date:
        races = [r for r in races if r.get('date') == date]

    races = sorted(races, key=lambda r: (r.get('date', ''), r.get('start_time', '')))
    return response(200, {'races': races})


def get_race(race_id):
    race_data = load_json(f'races/{race_id}/race.json')
    if not race_data:
        return response(404, {'error': f'Race not found: {race_id}'})
    _hydrate_rating_system(race_data)
    _hydrate_start_sequence(race_data)
    _hydrate_polars(race_data)
    return response(200, race_data)


def create_race(body):
    race_id = str(uuid.uuid4())[:8]
    now = now_iso()

    boats = body.get('boats', [])
    if isinstance(boats, list):
        boats = [b if isinstance(b, dict) else {} for b in boats]

    new_race = {
        'race_id': race_id,
        'name': body.get('name', ''),
        'date': body.get('date', ''),
        'start_time': body.get('start_time', ''),
        'end_time': body.get('end_time', ''),
        'regatta_id': body.get('regatta_id'),
        'boats': boats,
        # Boat class: {id, name, loa_m}. Drives the RRS 18 zone radius
        # (3 × LOA) drawn around marks on the race page. Optional; the
        # race page defaults to J/80 (24 m) when absent.
        'boat_class': body.get('boat_class'),
        # Multi-class handicap races: per-class start times + rating
        # type, used to compute elapsed/corrected. When absent the
        # race is single-start and the leaderboard ranks by GPS course
        # progress as before.
        'classes': body.get('classes', []),
        'race_conditions': body.get('race_conditions', ''),
        'start_line': body.get('start_line'),
        'finish_line': body.get('finish_line'),
        'marks': body.get('marks', []),
        'course': body.get('course', []),
        'finish_order': body.get('finish_order', []),
        'results': None,
        'created_at': now,
        'updated_at': now,
    }

    # Save race definition
    save_json(f'races/{race_id}/race.json', new_race)

    # Update races index
    data = load_json(RACES_INDEX_KEY)
    if not data:
        data = {'races': []}
    data['races'].append({
        'race_id': race_id,
        'name': new_race['name'],
        'date': new_race['date'],
        'start_time': new_race['start_time'],
        'end_time': new_race['end_time'],
        'regatta_id': new_race['regatta_id'],
        'boat_count': len(boats),
    })
    save_json(RACES_INDEX_KEY, data)

    # Update regatta if linked
    if new_race['regatta_id']:
        regattas_data = load_json(REGATTAS_INDEX_KEY)
        for regatta in regattas_data.get('regattas', []):
            if regatta['regatta_id'] == new_race['regatta_id']:
                if race_id not in regatta.get('race_ids', []):
                    regatta.setdefault('race_ids', []).append(race_id)
                    regatta['updated_at'] = now
                break
        save_json(REGATTAS_INDEX_KEY, regattas_data)

    return response(201, new_race)


def update_race(race_id, body):
    race_data = load_json(f'races/{race_id}/race.json')
    if not race_data:
        return response(404, {'error': f'Race not found: {race_id}'})

    # Sticky own-GPS tracks (server-side guard). An uploaded by-boat-id
    # GPX/VKX track is authoritative — the upload created the file and set
    # the link. The race editor has NO affordance to clear a by-boat-id
    # track, so any payload that drops the link is ALWAYS accidental (the
    # classic case: a stale editor build nulls gpx_path for boats with no
    # E-device — see getFormData's device-clear path). Restore it here so
    # editing a race can never strip uploaded tracks, regardless of the
    # client's JS version.
    #
    # We only restore when the incoming boat has NO GPS source at all
    # (gpx_path + session_path + device_id all empty): that's the
    # accidental-wipe signature. If the user intentionally switched the
    # boat to a fleet session/device, those fields are set and we leave
    # the payload untouched.
    if isinstance(body.get('boats'), list):
        prev_gpx = {
            b.get('boat_id'): b.get('gpx_path')
            for b in race_data.get('boats', [])
            if b.get('boat_id') and '/by-boat-id/' in (b.get('gpx_path') or '')
        }
        for nb in body['boats']:
            if not isinstance(nb, dict):
                continue
            bid = nb.get('boat_id')
            if (bid and prev_gpx.get(bid)
                    and not nb.get('gpx_path')
                    and not nb.get('session_path')
                    and not nb.get('device_id')):
                nb['gpx_path'] = prev_gpx[bid]

    # NOTE: 'date' MUST stay in this allowlist. The dashboard groups
    # races by `race.date` for the day-picker dropdown, so a race whose
    # date is stuck at the wrong day silently sticks to the wrong group
    # forever — exactly the failure mode that prompted this fix.
    #
    # `boat_class` is the only field that legitimately wants a `null`
    # update — handicap races have no overall class, and the editor
    # writes null to clear it. The other fields take the
    # default-incomplete-payload-safe `is not None` guard so a partial
    # PATCH can't accidentally wipe them.
    _ALLOW_NULL = {'boat_class'}
    for key in ['name', 'date', 'start_time', 'end_time', 'boats', 'boat_class', 'classes', 'race_conditions', 'start_line', 'finish_line', 'marks', 'course', 'finish_order']:
        if key not in body:
            continue
        if body[key] is None and key not in _ALLOW_NULL:
            continue
        race_data[key] = body[key]

    race_data['updated_at'] = now_iso()
    save_json(f'races/{race_id}/race.json', race_data)

    # Update index — date is mirrored here for the day-grouping dropdown,
    # so it must stay in sync with the per-race JSON above.
    data = load_json(RACES_INDEX_KEY)
    for i, r in enumerate(data.get('races', [])):
        if r['race_id'] == race_id:
            data['races'][i]['name'] = race_data['name']
            data['races'][i]['date'] = race_data['date']
            data['races'][i]['start_time'] = race_data['start_time']
            data['races'][i]['end_time'] = race_data['end_time']
            data['races'][i]['boat_count'] = len(race_data.get('boats', []))
            break
    save_json(RACES_INDEX_KEY, data)

    return response(200, race_data)


def delete_race(race_id):
    race_data = load_json(f'races/{race_id}/race.json')
    if not race_data:
        return response(404, {'error': f'Race not found: {race_id}'})

    delete_json(f'races/{race_id}/race.json')
    delete_json(f'races/{race_id}/results.json')

    # Update index
    data = load_json(RACES_INDEX_KEY)
    data['races'] = [r for r in data.get('races', []) if r['race_id'] != race_id]
    save_json(RACES_INDEX_KEY, data)

    # Update regatta if linked
    if race_data.get('regatta_id'):
        regattas_data = load_json(REGATTAS_INDEX_KEY)
        for regatta in regattas_data.get('regattas', []):
            if regatta['regatta_id'] == race_data['regatta_id']:
                regatta['race_ids'] = [rid for rid in regatta.get('race_ids', []) if rid != race_id]
                regatta['updated_at'] = now_iso()
                break
        save_json(REGATTAS_INDEX_KEY, regattas_data)

    return response(200, {'deleted': race_id})


# --- Race Data ---

def get_race_data(race_id, sensors_str, pad_start_sec=0, pad_end_sec=0):
    race_data = load_json(f'races/{race_id}/race.json')
    if not race_data:
        return response(404, {'error': f'Race not found: {race_id}'})

    start_time = race_data['start_time']
    end_time = race_data['end_time']

    # Optional widening for start-review / post-race overrun analysis.
    # Caller passes pad_start_sec/pad_end_sec in seconds; we shift the
    # filter window outward only — the official race window in the
    # response stays as recorded.
    filter_start = start_time
    filter_end = end_time
    if pad_start_sec > 0 or pad_end_sec > 0:
        try:
            s_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
            e_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
            from datetime import timedelta
            filter_start = (s_dt - timedelta(seconds=max(0, pad_start_sec))).isoformat().replace('+00:00', 'Z')
            filter_end = (e_dt + timedelta(seconds=max(0, pad_end_sec))).isoformat().replace('+00:00', 'Z')
        except Exception as e:
            logger.warning(f'pad parse failed, using race window: {e}')

    requested_sensors = [s.strip() for s in sensors_str.split(',')]

    boats_data = {}

    for boat in race_data.get('boats', []):
        device_id = boat.get('device_id')
        boat_id = boat.get('boat_id')
        session_path = boat.get('session_path')
        gpx_path = boat.get('gpx_path')

        # Pick the response key: device_id when this boat is on a
        # fleet tracker, else boat_id (catalog FK). Boats with no
        # tracker AND no GPX nor sail data are skipped — the frontend
        # pulls their metadata from race.boats[] directly.
        track_key = device_id or boat_id
        if not track_key:
            continue
        # No GPS source at all → don't synthesize an empty entry. The
        # handicap leaderboard renders these from race.boats[] regardless.
        if not session_path and not gpx_path:
            continue

        boat_sensors = {}
        for sensor in requested_sensors:
            # GPX upload serves as the GPS source for this boat.
            # No window filter — user explicitly assigned this file to the race.
            if sensor == 'gps' and gpx_path:
                try:
                    data = load_json(gpx_path)
                    boat_sensors[sensor] = data if isinstance(data, list) else []
                except Exception as e:
                    boat_sensors[sensor] = {'error': str(e)}
                continue

            if not session_path or not device_id:
                # No SailFrames session to load IMU / wind from.
                boat_sensors[sensor] = []
                continue

            try:
                sensor_key = f"processed/{device_id}/{session_path}/{sensor}.json"
                data = load_json(sensor_key)
                if isinstance(data, list):
                    filtered = [d for d in data if _in_window(d.get('t', ''), filter_start, filter_end)]
                    boat_sensors[sensor] = filtered
                else:
                    boat_sensors[sensor] = data
            except Exception as e:
                boat_sensors[sensor] = {'error': str(e)}

        boats_data[track_key] = {
            'boat': boat,
            'sensors': boat_sensors,
        }

    return response(200, {
        'race': {
            'race_id': race_id,
            'name': race_data['name'],
            'date': race_data['date'],
            'start_time': start_time,
            'end_time': end_time,
        },
        'boats': boats_data,
        'time_bounds': {'start': start_time, 'end': end_time},
    })


def get_gpx_status(race_id):
    """Return per-boat GPX import summary for debugging."""
    race_data = load_json(f'races/{race_id}/race.json')
    if not race_data:
        return response(404, {'error': f'Race not found: {race_id}'})

    start_time = race_data.get('start_time', '')
    end_time = race_data.get('end_time', '')
    status = []

    for boat in race_data.get('boats', []):
        device_id = boat.get('device_id')
        if not device_id:
            continue  # non-GPS handicap entry
        gpx_path = boat.get('gpx_path')
        entry = {'device_id': device_id, 'gpx_path': gpx_path}

        if not gpx_path:
            entry['status'] = 'no_gpx'
        else:
            data = load_json(gpx_path)
            if not isinstance(data, list) or not data:
                entry['status'] = 'empty'
            else:
                in_window = [d for d in data if _in_window(d.get('t', ''), start_time, end_time)]
                entry.update({
                    'status': 'ok',
                    'total_points': len(data),
                    'points_in_window': len(in_window),
                    'track_start': data[0].get('t'),
                    'track_end': data[-1].get('t'),
                    'race_window_start': start_time,
                    'race_window_end': end_time,
                })
        status.append(entry)

    return response(200, {'race_id': race_id, 'boats': status})


def _in_window(t, start, end):
    """Check if timestamp t falls within [start, end] using normalized ISO comparison."""
    # Normalize: strip trailing Z and milliseconds for consistent comparison
    def norm(s):
        return s.replace('Z', '').split('.')[0] if s else ''
    tn, s, e = norm(t), norm(start), norm(end)
    return s <= tn <= e


def get_race_ais(race_id, start=None, end=None):
    """Return non-participant AIS vessel tracks for a race's map overlay.

    Stored at races/{race_id}/ais/vessels.json (written by
    scripts/fetch_ais_datalastic.py). Optional start/end ISO clip each
    vessel's position array to a sub-window. Missing file -> empty list,
    so the frontend simply hides the AIS toggle.

    This data is deliberately a separate path from raceData.boats: AIS
    vessels render on the map only and never feed the leaderboard,
    rankings, or charts.
    """
    doc = load_json(f'races/{race_id}/ais/vessels.json')
    if not doc or not doc.get('vessels'):
        return response(200, {'vessels': [], 'source': None})

    vessels = doc.get('vessels', [])
    if start and end:
        clipped = []
        for v in vessels:
            pts = [p for p in v.get('positions', []) if _in_window(p.get('t', ''), start, end)]
            if pts:
                vv = {k: val for k, val in v.items() if k != 'positions'}
                vv['positions'] = pts
                clipped.append(vv)
        vessels = clipped

    return response(200, {
        'vessels': vessels,
        'source': doc.get('source'),
        'center': doc.get('center'),
        'radius_nm': doc.get('radius_nm'),
        'window': doc.get('window'),
        'generated_at': doc.get('generated_at'),
    })


# --- GPX Upload ---

def upload_boat_gpx(race_id, device_id, event):
    """Parse a GPX file upload and store it as the GPS source for a boat."""
    race_data = load_json(f'races/{race_id}/race.json')
    if not race_data:
        return response(404, {'error': f'Race not found: {race_id}'})

    boat = next((b for b in race_data.get('boats', []) if b.get('device_id') == device_id), None)
    if boat is None:
        return response(404, {'error': f'Boat {device_id} not found in race {race_id}'})

    gpx_bytes = _extract_multipart_file(event)
    if not gpx_bytes:
        return response(400, {'error': 'No file received — send as multipart/form-data with field name "file"'})

    try:
        track_points = _parse_gpx(gpx_bytes)
    except Exception as e:
        logger.error(f"GPX parse error: {e}", exc_info=True)
        return response(400, {'error': f'Failed to parse GPX: {e}'})

    if not track_points:
        return response(400, {'error': 'GPX file contains no track points with timestamps'})

    gpx_key = f'races/{race_id}/gpx/{device_id}.json'
    save_json(gpx_key, track_points)

    boat['gpx_path'] = gpx_key
    boat['session_path'] = None  # GPX replaces E1 session
    race_data['updated_at'] = now_iso()
    save_json(f'races/{race_id}/race.json', race_data)

    logger.info(f"GPX uploaded for {device_id} in race {race_id}: {len(track_points)} points")

    return response(200, {
        'device_id': device_id,
        'gpx_path': gpx_key,
        'points': len(track_points),
        'start_time': track_points[0]['t'],
        'end_time': track_points[-1]['t'],
    })


# --- VKX (Vakaros Atlas) parser + boat-owner report ---
#
# Scan parser for Vakaros .vkx binary logs. Extracts every record type
# the spec documents (telemetry, race timer, line positions, wind,
# depth, temperature, speed-through-water, declination, shift angles,
# load cells, config) and produces a structured summary + an HTML
# report intended to be shared with the boat owner — so they can see
# what was recorded and act on firmware / config / sensor gaps.
#
# Spec reference: github.com/vakaros/vkx/wiki/Vakaros-VXK-Format
# Msg type is at byte 7 of the 8-byte key; the page-header type is
# 0xFD (newer firmware) or 0xFF (older firmware — observed at the
# head of the user's RockIt 2.0 file).
_VKX_MESSAGE_SIZES = {
    0x01: 32, 0x02: 44, 0x03: 20, 0x04: 17, 0x05: 13, 0x06: 18,
    0x07: 12, 0x08: 16, 0x09: 16, 0x0A: 12, 0x0E: 16, 0x10: 12,
    0x11: 16, 0x20: 13, 0xFD: 7, 0xFE: 2, 0xFF: 7,
}

_VKX_TIMER_EVENTS = {0: 'reset', 1: 'start', 2: 'sync',
                     3: 'race_start', 4: 'race_end'}


def _vkx_quat_roll(qw, qx, qy, qz):
    """Heel angle (deg, signed) from a Vakaros quaternion."""
    return math.degrees(math.atan2(2 * (qw * qx + qy * qz),
                                    1 - 2 * (qx * qx + qy * qy)))


def _vkx_iso(ts_ms):
    return datetime.utcfromtimestamp(ts_ms / 1000.0).isoformat() + 'Z'


def _vkx_local_hms(ts_ms, tz_offset_h=-4):
    """ts_ms → 'HH:MM:SS' in EDT (UTC-4). The fleet only races in
    Boston Harbor and the report is for one boat at a time, so a
    fixed offset is enough for now."""
    dt = datetime.utcfromtimestamp(ts_ms / 1000.0)
    from datetime import timedelta
    local = dt + timedelta(hours=tz_offset_h)
    return local.strftime('%H:%M:%S')


def parse_vkx_full(data):
    """Full VKX scan. Returns a dict of every record type extracted +
    the GPS-points list (in the GPX-compatible shape) + a `summary`
    block with stats and data-quality flags."""
    n = len(data)
    pos = 0

    telem = []
    timer = []
    lines = []
    declination = []
    wind = []
    stw = []
    depth = []
    temp = []
    load = []
    shifts = []
    config = []
    page_headers = []
    page_terminators = 0
    internal_counts = {}    # type → count for 0x01/0x07/0x0E

    # Special case: if the file starts with 0xFF, treat it as a page
    # header at byte 0 (the wiki spec shows page-header type byte at
    # offset 0, payload directly after — different from the message-
    # type-at-byte-7 convention for telemetry records).
    if n >= 8 and data[0] == 0xFF:
        page_headers.append({'pos': 0, 'format_version': data[1]})

    while pos + 8 <= n:
        msg_type = data[pos + 7]
        size = _VKX_MESSAGE_SIZES.get(msg_type)
        if size is None:
            pos += 1
            continue
        if pos + 8 + size > n:
            break
        payload = data[pos + 8: pos + 8 + size]
        # `consumed` = did this position yield a real record? If not we
        # slide one byte instead of advancing the full key+payload — a
        # false positive (e.g. a 0x02 byte at offset 7 by coincidence)
        # would otherwise skip past real telemetry that starts a few
        # bytes later.
        consumed = False
        try:
            if msg_type == 0x02:
                (ts_ms, lat_i, lon_i, sog, cog, alt,
                 qw, qx, qy, qz) = struct.unpack('<Qiifffffff', payload)
                if 1577836800000 < ts_ms < 1893456000000:
                    telem.append({
                        't_ms': ts_ms,
                        'lat': lat_i / 1e7, 'lon': lon_i / 1e7,
                        'sog_mps': sog, 'cog_rad': cog, 'alt_m': alt,
                        'qw': qw, 'qx': qx, 'qy': qy, 'qz': qz,
                    })
                    consumed = True
            elif msg_type == 0x03:
                ts_ms, d, lat_i, lon_i = struct.unpack('<Qfii', payload)
                if 1577836800000 < ts_ms < 1893456000000:
                    declination.append({'t_ms': ts_ms, 'decl_deg': math.degrees(d)})
                    consumed = True
            elif msg_type == 0x04:
                ts_ms, end, lat, lon = struct.unpack('<QBff', payload)
                if 1577836800000 < ts_ms < 1893456000000:
                    lines.append({'t_ms': ts_ms,
                                  'end': 'pin' if end == 0 else 'boat',
                                  'lat': lat, 'lon': lon})
                    consumed = True
            elif msg_type == 0x05:
                ts_ms, ev, val = struct.unpack('<QBi', payload)
                if 1577836800000 < ts_ms < 1893456000000:
                    timer.append({'t_ms': ts_ms, 'event': ev,
                                  'event_name': _VKX_TIMER_EVENTS.get(ev, f'unk{ev}'),
                                  'value_sec': val})
                    consumed = True
            elif msg_type == 0x06:
                ts_ms, tack, manual, hdg, sog = struct.unpack('<QBBff', payload)
                if 1577836800000 < ts_ms < 1893456000000:
                    shifts.append({'t_ms': ts_ms,
                                   'tack': 'port' if tack else 'starboard',
                                   'manual': bool(manual),
                                   'heading_deg': hdg, 'sog_kts': sog})
                    consumed = True
            elif msg_type == 0x08:
                ts_ms, wd, ws = struct.unpack('<Qff', payload)
                if 1577836800000 < ts_ms < 1893456000000:
                    wind.append({'t_ms': ts_ms, 'dir_deg': wd, 'spd_mps': ws})
                    consumed = True
            elif msg_type == 0x09:
                ts_ms, fwd, lat = struct.unpack('<Qff', payload)
                if 1577836800000 < ts_ms < 1893456000000:
                    stw.append({'t_ms': ts_ms, 'fwd_mps': fwd, 'lat_mps': lat})
                    consumed = True
            elif msg_type == 0x0A:
                ts_ms, d = struct.unpack('<Qf', payload)
                if 1577836800000 < ts_ms < 1893456000000:
                    depth.append({'t_ms': ts_ms, 'm': d})
                    consumed = True
            elif msg_type == 0x10:
                ts_ms, c = struct.unpack('<Qf', payload)
                if 1577836800000 < ts_ms < 1893456000000:
                    temp.append({'t_ms': ts_ms, 'c': c})
                    consumed = True
            elif msg_type == 0x11:
                ts_ms = struct.unpack('<Q', payload[:8])[0]
                if 1577836800000 < ts_ms < 1893456000000:
                    name = payload[8:12].decode('ascii', 'replace').rstrip('\x00')
                    val = struct.unpack('<f', payload[12:16])[0]
                    load.append({'t_ms': ts_ms, 'sensor': name, 'load': val})
                    consumed = True
            elif msg_type == 0x20:
                flags, rate = struct.unpack('<IB', payload[8:13])
                config.append({'flags': flags, 'rate_hz': rate,
                               'fixed_to_body': bool(flags & 1)})
                consumed = True
            elif msg_type in (0xFD, 0xFF):
                page_headers.append({'pos': pos, 'format_version': payload[0]})
                consumed = True
            elif msg_type == 0xFE:
                page_terminators += 1
                consumed = True
            else:
                internal_counts[msg_type] = internal_counts.get(msg_type, 0) + 1
                consumed = True
        except struct.error:
            consumed = False
        pos += (8 + size) if consumed else 1

    telem.sort(key=lambda r: r['t_ms'])
    summary = _vkx_summary(data, telem, timer, lines, declination, wind, stw,
                           depth, temp, load, shifts, config, page_headers,
                           page_terminators, internal_counts)
    return {
        'telem': telem, 'timer': timer, 'lines': lines,
        'declination': declination, 'wind': wind,
        'stw': stw, 'depth': depth, 'temp': temp, 'load': load,
        'shifts': shifts, 'config': config, 'page_headers': page_headers,
        'summary': summary,
    }


def _vkx_summary(data, telem, timer, lines, declination, wind, stw, depth,
                 temp, load, shifts, config, page_headers,
                 page_terminators, internal_counts):
    """Distill the parsed records into stats + data-quality flags
    the report generator and the boat owner actually care about."""
    n = len(data)
    s = {
        'file_size_bytes': n,
        'record_counts': {
            'Telemetry (GPS + IMU)':   len(telem),
            'Page Headers':            len(page_headers),
            'Page Terminators':        page_terminators,
            'Configuration':           len(config),
            'Declination':             len(declination),
            'Race Timer Events':       len(timer),
            'Line Positions':          len(lines),
            'Tack Shifts':             len(shifts),
            'Calypso Wind':            len(wind),
            'Speed Through Water':     len(stw),
            'Depth':                   len(depth),
            'Temperature':             len(temp),
            'Load Cells':              len(load),
        },
        'internal_record_counts': {f'0x{k:02X}': v for k, v in internal_counts.items()},
        'format_version': page_headers[0]['format_version'] if page_headers else None,
    }

    # Telemetry stats
    if telem:
        t0_ms, t1_ms = telem[0]['t_ms'], telem[-1]['t_ms']
        duration_s = (t1_ms - t0_ms) / 1000.0
        sog_kts = [t['sog_mps'] * 1.94384 for t in telem]
        heels = [_vkx_quat_roll(t['qw'], t['qx'], t['qy'], t['qz']) for t in telem]
        absh = sorted(abs(h) for h in heels)
        gaps = sorted((telem[i+1]['t_ms'] - telem[i]['t_ms']) for i in range(len(telem)-1)) if len(telem) > 1 else [0]
        median_gap_ms = gaps[len(gaps)//2] if gaps else 0
        p95_gap_ms = gaps[int(len(gaps)*0.95)] if gaps else 0
        sample_hz = (1000.0 / median_gap_ms) if median_gap_ms > 0 else 0
        # Coverage: telemetry seconds / wall-clock seconds. <0.9 means
        # gaps; <0.5 means significant dropouts.
        coverage = len(telem) / max(1, duration_s) / max(sample_hz, 0.1) if sample_hz else 0
        s['telemetry'] = {
            'start_iso': _vkx_iso(t0_ms),
            'end_iso': _vkx_iso(t1_ms),
            'start_local_hms': _vkx_local_hms(t0_ms),
            'end_local_hms': _vkx_local_hms(t1_ms),
            'duration_s': round(duration_s, 1),
            'duration_min': round(duration_s / 60.0, 1),
            'point_count': len(telem),
            'sample_rate_hz': round(sample_hz, 2),
            'median_gap_ms': median_gap_ms,
            'p95_gap_ms': p95_gap_ms,
            'coverage_pct': round(min(1.0, coverage) * 100, 1),
            'max_speed_kts': round(max(sog_kts), 2),
            'avg_speed_kts': round(sum(sog_kts) / len(sog_kts), 2),
            'max_heel_deg': round(max(absh), 1),
            'p95_heel_deg': round(absh[int(len(absh) * 0.95)], 1),
            'avg_heel_deg': round(sum(absh) / len(absh), 1),
            'lat_min': round(min(t['lat'] for t in telem), 6),
            'lat_max': round(max(t['lat'] for t in telem), 6),
            'lon_min': round(min(t['lon'] for t in telem), 6),
            'lon_max': round(max(t['lon'] for t in telem), 6),
        }

    # Race-event quality flags
    s['line_positions_valid'] = sum(1 for l in lines
                                     if abs(l['lat']) > 0.01 and abs(l['lon']) > 0.01)
    s['line_positions_invalid'] = len(lines) - s['line_positions_valid']
    s['has_gun_event'] = any(t['event'] == 3 for t in timer)

    # Recommendations
    recs = []
    tele = s.get('telemetry', {})
    rate = tele.get('sample_rate_hz', 0)
    if rate and rate < 4.5:
        recs.append({
            'level': 'high',
            'title': f'Telemetry sample rate is {rate:.1f} Hz — too low for tack-by-tack analysis',
            'body': "For modern motion analysis (tack timing, OCS detection, "
                    "leg-by-leg speed) you want at least 5 Hz and preferably "
                    "10 Hz. Open the Vakaros app, go to Device → Settings, and "
                    "raise the telemetry rate. If the option isn't there, you "
                    "may be on an older firmware — update via the app's "
                    "Firmware tab."
        })
    if not s['has_gun_event']:
        recs.append({
            'level': 'medium',
            'title': 'No race start gun was recorded',
            'body': "The race-timer feature on the Atlas writes a 'race_start' "
                    "event when the timer hits zero. None was found in this "
                    "file, which means the timer wasn't started before the "
                    "race. Use Race Mode in the Vakaros app to start the timer "
                    "and unlock OCS, elapsed-time, and start-line bias analysis."
        })
    if s['line_positions_invalid'] > 0 and s['line_positions_valid'] == 0:
        recs.append({
            'level': 'medium',
            'title': 'Start line was not set (default 0,0 coordinate found)',
            'body': "A line-position record exists but its coordinates are 0,0 — "
                    "the default placeholder. To get start-line-bias analytics "
                    "next week, use the Vakaros app to ping the pin and "
                    "committee-boat ends before the start sequence."
        })
    if not wind and not stw and not depth and not temp and not load:
        recs.append({
            'level': 'low',
            'title': 'No external sensors connected',
            'body': "No wind, depth, temperature, speed-through-water, or load "
                    "cell data was found. The Atlas integrates with Calypso "
                    "Ultrasonic wind sensors via Bluetooth and Cyclops load "
                    "cells via the Atlas Hub — adding these expands what's "
                    "recoverable from each race."
        })
    if tele.get('coverage_pct', 100) < 90:
        recs.append({
            'level': 'medium',
            'title': f"Telemetry coverage is {tele['coverage_pct']:.0f}% — gaps detected",
            'body': f"The p95 gap between telemetry samples is "
                    f"{tele.get('p95_gap_ms', 0)} ms — a healthy file at "
                    f"{rate:.0f} Hz should be ≤200% of the median. Gaps usually "
                    "mean GPS-fix loss (try mounting the Atlas higher and away "
                    "from rigging) or a corrupt page (often resolved by a "
                    "firmware update + factory format of the device SD card "
                    "if applicable)."
        })
    fv = s['format_version']
    if fv is not None and fv < 0x05:
        recs.append({
            'level': 'medium',
            'title': f'VKX format version 0x{fv:02X} is older than what Vakaros currently ships',
            'body': "The Atlas writes the format version into every page "
                    "header. Yours is below 0x05 — newer firmware writes "
                    "more sensor types and improves IMU calibration. Check "
                    "vakaros.com/support for the latest firmware."
        })
    s['recommendations'] = recs
    return s


def parse_vkx_to_gps_points(parsed):
    """Convert the telemetry list from parse_vkx_full into the
    {t, lat, lon, speed_kn, course} shape the GPX path expects."""
    out = []
    for t in parsed.get('telem', []):
        cog_deg = math.degrees(t['cog_rad']) % 360
        out.append({
            't': _vkx_iso(t['t_ms']),
            'lat': round(t['lat'], 7),
            'lon': round(t['lon'], 7),
            'speed_kn': round(t['sog_mps'] * 1.94384, 2),
            'course': round(cog_deg, 1),
        })
    return out


def _vkx_report_html(boat, race, summary, report_url):
    """Self-contained HTML report. Inline CSS, no JS dependencies — opens
    cleanly in any browser, sharable by URL."""
    tele = summary.get('telemetry', {})
    fv = summary.get('format_version')
    fv_str = f'0x{fv:02X}' if fv is not None else '—'
    boat_name = boat.get('boat_name') or boat.get('team_name') or 'Boat'
    sail = boat.get('sail_number') or ''
    boat_type = boat.get('boat_type') or ''
    skipper = boat.get('skipper') or boat.get('team_name') or ''
    race_name = race.get('name', 'Race')
    race_date = race.get('date', '')

    def row(label, value, sub=''):
        sub_html = f' <span class="sub">{_h(sub)}</span>' if sub else ''
        return f'<div class="stat"><div class="lbl">{_h(label)}</div>' \
               f'<div class="val">{_h(value)}{sub_html}</div></div>'

    # Record counts table
    rc_rows = []
    for name, count in summary['record_counts'].items():
        if count == 0:
            cls = 'rc-zero'
        elif count < 5:
            cls = 'rc-low'
        else:
            cls = 'rc-ok'
        rc_rows.append(f'<tr class="{cls}"><td>{_h(name)}</td>'
                       f'<td class="num">{count:,}</td></tr>')
    if summary.get('internal_record_counts'):
        for t, c in summary['internal_record_counts'].items():
            rc_rows.append(f'<tr class="rc-internal"><td>Internal {t}</td>'
                           f'<td class="num">{c:,}</td></tr>')

    # Recommendations
    rec_html = ''
    for r in summary.get('recommendations', []):
        rec_html += (f'<div class="rec rec-{_h(r["level"])}">'
                     f'<div class="rec-title">{_h(r["title"])}</div>'
                     f'<div class="rec-body">{_h(r["body"])}</div></div>')
    if not rec_html:
        rec_html = ('<div class="rec rec-low">'
                    '<div class="rec-title">Everything looks good</div>'
                    '<div class="rec-body">No firmware or configuration '
                    'changes are required based on this file. Keep doing '
                    'what you\'re doing.</div></div>')

    # Telemetry stats card grid
    if tele:
        stats_html = '\n'.join([
            row('Duration', f'{tele["duration_min"]:.1f} min'),
            row('Sample rate', f'{tele["sample_rate_hz"]:.2f} Hz',
                f'(median gap {tele["median_gap_ms"]} ms)'),
            row('Telemetry points', f'{tele["point_count"]:,}'),
            row('Coverage', f'{tele["coverage_pct"]:.0f}%'),
            row('Max speed', f'{tele["max_speed_kts"]:.1f} kn'),
            row('Avg speed', f'{tele["avg_speed_kts"]:.1f} kn'),
            row('Max heel', f'{tele["max_heel_deg"]:.0f}°',
                f'(p95 {tele["p95_heel_deg"]:.0f}°)'),
            row('Avg heel', f'{tele["avg_heel_deg"]:.0f}°'),
            row('Start (local)', tele['start_local_hms']),
            row('End (local)', tele['end_local_hms']),
            row('File size', f'{summary["file_size_bytes"]:,} bytes'),
            row('VKX format', fv_str),
        ])
    else:
        stats_html = ('<div class="empty">No telemetry records were found '
                      'in this file — it may be corrupt or empty.</div>')

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VKX Report · {_h(boat_name)} · {_h(race_name)}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #0a0a0a; color: #e8e8e8; margin: 0; padding: 0;
         line-height: 1.5; }}
  .wrap {{ max-width: 880px; margin: 0 auto; padding: 40px 24px 80px; }}
  header {{ border-bottom: 1px solid #2a2a2a; padding-bottom: 18px;
           margin-bottom: 28px; }}
  .brand {{ color: #22d3ee; font-weight: 700; letter-spacing: 0.05em;
           font-size: 0.85rem; text-transform: uppercase; }}
  h1 {{ margin: 6px 0 4px; font-size: 1.75rem; }}
  .subtitle {{ color: #888; font-size: 0.95rem; }}
  h2 {{ margin: 32px 0 12px; font-size: 1.1rem; color: #22d3ee;
       text-transform: uppercase; letter-spacing: 0.04em;
       border-bottom: 1px solid #2a2a2a; padding-bottom: 6px; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fill,
            minmax(180px, 1fr)); gap: 10px; }}
  .stat {{ background: #131313; padding: 12px 14px; border-radius: 6px;
          border: 1px solid #1f1f1f; }}
  .lbl {{ font-size: 0.7rem; color: #888; text-transform: uppercase;
         letter-spacing: 0.05em; margin-bottom: 4px; }}
  .val {{ font-size: 1.15rem; font-weight: 600; color: #fff;
         font-variant-numeric: tabular-nums; }}
  .val .sub {{ font-size: 0.72rem; color: #888; font-weight: 400; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th, td {{ text-align: left; padding: 7px 10px; border-bottom: 1px solid #1f1f1f; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums;
           color: #ccc; }}
  tr.rc-zero td {{ color: #555; }}
  tr.rc-low td {{ color: #eab308; }}
  tr.rc-internal td {{ color: #555; font-style: italic; }}
  .rec {{ background: #131313; border-left: 4px solid #22d3ee;
         padding: 12px 16px; margin-bottom: 12px; border-radius: 0 6px 6px 0; }}
  .rec-high {{ border-left-color: #f4212e; }}
  .rec-medium {{ border-left-color: #f59e0b; }}
  .rec-low {{ border-left-color: #22d3ee; }}
  .rec-title {{ font-weight: 700; margin-bottom: 6px; color: #fff; }}
  .rec-body {{ color: #ccc; font-size: 0.92rem; }}
  .empty {{ color: #888; padding: 16px; font-style: italic; }}
  footer {{ margin-top: 50px; padding-top: 18px; border-top: 1px solid #2a2a2a;
           color: #666; font-size: 0.8rem; }}
  a {{ color: #22d3ee; }}
</style></head><body>
<div class="wrap">
  <header>
    <div class="brand">SailFrames · Vakaros Atlas Report</div>
    <h1>{_h(boat_name)}{f' · #{_h(sail)}' if sail else ''}</h1>
    <div class="subtitle">
      {_h(race_name)} · {_h(race_date)}
      {f' · {_h(boat_type)}' if boat_type else ''}
      {f' · {_h(skipper)}' if skipper else ''}
    </div>
  </header>

  <h2>Overview</h2>
  <div class="stats">
    {stats_html}
  </div>

  <h2>Recommendations</h2>
  {rec_html}

  <h2>Record counts</h2>
  <table>
    <thead><tr><th>Type</th><th class="num">Count</th></tr></thead>
    <tbody>{''.join(rc_rows)}</tbody>
  </table>

  <footer>
    Auto-generated by sailframes.com from the uploaded Vakaros .vkx
    binary log. Format spec:
    <a href="https://github.com/vakaros/vkx/wiki/Vakaros-VXK-Format-Specification">vakaros/vkx wiki</a>.
    Share this report: <a href="{report_url}">{report_url}</a>
  </footer>
</div></body></html>
"""


def _h(s):
    """HTML-escape (lightweight)."""
    if s is None:
        return ''
    s = str(s)
    return (s.replace('&', '&amp;').replace('<', '&lt;')
             .replace('>', '&gt;').replace('"', '&quot;'))


def upload_boat_vkx_by_id(race_id, boat_id, event):
    """Vakaros .vkx upload for a boat without a fleet device.
    Parses every record type, writes GPS points to the by-boat-id
    GPX path (so the dashboard reads them through the unchanged
    gpx_path plumbing), AND generates an HTML report intended for
    the boat owner — saved at races/{id}/vkx-reports/{boat_id}.html
    (public-readable) and linked from the boat drawer."""
    race_data = load_json(f'races/{race_id}/race.json')
    if not race_data:
        return response(404, {'error': f'Race not found: {race_id}'})

    boat = next((b for b in race_data.get('boats', []) if b.get('boat_id') == boat_id), None)
    if boat is None:
        return response(404, {'error': f'Boat {boat_id} not found in race {race_id}'})

    vkx_bytes = _extract_multipart_file(event)
    if not vkx_bytes:
        return response(400, {'error': 'No file received — send as multipart/form-data with field name "file"'})

    try:
        parsed = parse_vkx_full(vkx_bytes)
    except Exception as e:
        logger.error(f"VKX parse error: {e}", exc_info=True)
        return response(400, {'error': f'Failed to parse VKX: {e}'})

    track_points = parse_vkx_to_gps_points(parsed)
    if not track_points:
        return response(400, {'error': 'VKX file contains no telemetry records'})

    # GPS track for the dashboard
    gpx_key = f'races/{race_id}/gpx/by-boat-id/{boat_id}.json'
    save_json(gpx_key, track_points)
    boat['gpx_path'] = gpx_key
    boat['session_path'] = None

    # Report HTML for the boat owner. Stored at a deterministic path
    # so re-uploads overwrite cleanly. Public-readable via the bucket
    # policy entry for vkx-reports/*.
    report_key = f'races/{race_id}/vkx-reports/{boat_id}.html'
    report_url = f'https://{DATA_BUCKET}.s3.us-east-1.amazonaws.com/{report_key}'
    html = _vkx_report_html(boat, race_data, parsed['summary'], report_url)
    s3.put_object(
        Bucket=DATA_BUCKET, Key=report_key,
        Body=html.encode('utf-8'),
        ContentType='text/html; charset=utf-8',
        CacheControl='public, max-age=120',
    )
    boat['vkx_report_url'] = report_url

    race_data['updated_at'] = now_iso()
    save_json(f'races/{race_id}/race.json', race_data)

    logger.info(f"VKX uploaded for boat_id={boat_id}: {len(track_points)} pts, "
                f"report → {report_url}")

    return response(200, {
        'boat_id': boat_id,
        'gpx_path': gpx_key,
        'points': len(track_points),
        'source': 'vkx',
        'start_time': track_points[0]['t'],
        'end_time': track_points[-1]['t'],
        'report_url': report_url,
        'summary': parsed['summary'],
    })


def upload_boat_gpx_by_id(race_id, boat_id, event):
    """GPX upload for a boat that has no fleet device_id assigned.
    Same as upload_boat_gpx but matches on boat_id (catalog FK).
    Storage key uses a /by-boat-id/ prefix so it can't collide with
    device-keyed paths."""
    race_data = load_json(f'races/{race_id}/race.json')
    if not race_data:
        return response(404, {'error': f'Race not found: {race_id}'})

    boat = next((b for b in race_data.get('boats', []) if b.get('boat_id') == boat_id), None)
    if boat is None:
        return response(404, {'error': f'Boat {boat_id} not found in race {race_id}'})

    gpx_bytes = _extract_multipart_file(event)
    if not gpx_bytes:
        return response(400, {'error': 'No file received — send as multipart/form-data with field name "file"'})

    try:
        track_points = _parse_gpx(gpx_bytes)
    except Exception as e:
        logger.error(f"GPX parse error: {e}", exc_info=True)
        return response(400, {'error': f'Failed to parse GPX: {e}'})

    if not track_points:
        return response(400, {'error': 'GPX file contains no track points with timestamps'})

    gpx_key = f'races/{race_id}/gpx/by-boat-id/{boat_id}.json'
    save_json(gpx_key, track_points)

    boat['gpx_path'] = gpx_key
    boat['session_path'] = None
    race_data['updated_at'] = now_iso()
    save_json(f'races/{race_id}/race.json', race_data)

    logger.info(f"GPX uploaded for boat_id={boat_id} in race {race_id}: {len(track_points)} points")

    return response(200, {
        'boat_id': boat_id,
        'gpx_path': gpx_key,
        'points': len(track_points),
        'start_time': track_points[0]['t'],
        'end_time': track_points[-1]['t'],
    })


def _extract_multipart_file(event):
    """Extract the raw file bytes from a multipart/form-data Lambda event."""
    headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
    content_type = headers.get('content-type', '')

    body_raw = event.get('body', '') or ''
    if event.get('isBase64Encoded'):
        body_bytes = base64.b64decode(body_raw)
    else:
        body_bytes = body_raw.encode('latin-1')

    # Extract boundary from Content-Type header
    boundary = None
    for part in content_type.split(';'):
        part = part.strip()
        if part.lower().startswith('boundary='):
            boundary = part[9:].strip('"\'')
            break

    if not boundary:
        # No multipart — treat the raw body as the file
        return body_bytes if body_bytes else None

    delimiter = ('--' + boundary).encode('latin-1')
    parts = body_bytes.split(delimiter)

    for part in parts[1:]:  # skip preamble
        if part in (b'--', b'--\r\n', b''):
            continue
        # Split headers from body
        sep = b'\r\n\r\n'
        if sep not in part:
            continue
        _, file_body = part.split(sep, 1)
        file_body = file_body.rstrip(b'\r\n')
        if file_body:
            return file_body

    return None


# --- GPX Parsing ---

_GPX_TS_FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%b %d, %Y %I:%M:%S %p",
    "%b %d, %Y %I:%M %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
]


def _normalize_gpx_ts(t: str) -> str:
    """Normalize any GPX timestamp to ISO 8601 UTC string."""
    t = t.strip()
    # Already standard ISO — fast path
    if re.match(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', t):
        return t.replace('+0000', 'Z').rstrip('+0000') if '+0000' in t else t
    for fmt in _GPX_TS_FORMATS:
        try:
            dt = datetime.strptime(t, fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return t  # Return as-is if unrecognized


def _parse_gpx(content: bytes) -> list:
    """Parse GPX XML into GPS track points matching processed gps.json format."""
    root = ET.fromstring(content)
    ns_match = re.match(r'\{([^}]+)\}', root.tag)
    ns = f"{{{ns_match.group(1)}}}" if ns_match else ""

    raw = []
    for seg in root.iter(f"{ns}trkseg"):
        for trkpt in seg.iter(f"{ns}trkpt"):
            lat = float(trkpt.get('lat', 0))
            lon = float(trkpt.get('lon', 0))
            time_el = trkpt.find(f"{ns}time")
            if time_el is None or not time_el.text:
                continue
            t = _normalize_gpx_ts(time_el.text)

            speed_ms = None
            for el in trkpt.iter():
                local = el.tag.split('}')[-1] if '}' in el.tag else el.tag
                if local == 'speed' and el.text:
                    try:
                        speed_ms = float(el.text)
                    except ValueError:
                        pass
                    break

            raw.append({'lat': lat, 'lon': lon, 't': t, '_speed_ms': speed_ms})

    result = []
    for i, pt in enumerate(raw):
        sog = 0.0
        cog = 0.0

        if pt['_speed_ms'] is not None:
            sog = pt['_speed_ms'] * 1.94384  # m/s → knots
        elif i > 0:
            prev = raw[i - 1]
            try:
                dt = iso_diff_seconds(pt['t'], prev['t'])
                if dt > 0:
                    dist_m = _haversine_m(prev['lat'], prev['lon'], pt['lat'], pt['lon'])
                    sog = (dist_m / dt) * 1.94384
            except Exception:
                pass

        if i > 0:
            prev = raw[i - 1]
            cog = _bearing(prev['lat'], prev['lon'], pt['lat'], pt['lon'])
        elif i < len(raw) - 1:
            nxt = raw[i + 1]
            cog = _bearing(pt['lat'], pt['lon'], nxt['lat'], nxt['lon'])

        result.append({
            't': pt['t'],
            'lat': pt['lat'],
            'lon': pt['lon'],
            'speed_kn': round(sog, 2),
            'course': round(cog, 1),
        })

    return result


def _haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _bearing(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


# --- Session Matching ---

def match_sessions(race_id):
    race_data = load_json(f'races/{race_id}/race.json')
    if not race_data:
        return response(404, {'error': f'Race not found: {race_id}'})

    race_start = race_data['start_time']
    race_end = race_data['end_time']
    race_date = race_data['date']

    matched = []
    for boat in race_data.get('boats', []):
        device_id = boat.get('device_id')
        if not device_id:
            continue  # non-GPS handicap entry — nothing to match

        try:
            sessions = find_device_sessions(device_id, race_date)
        except Exception:
            sessions = []

        best_session = None
        best_overlap = 0

        for session in sessions:
            session_start = session.get('start_time', '')
            session_end = session.get('end_time', '')

            overlap_start = max(race_start, session_start)
            overlap_end = min(race_end, session_end)

            if overlap_start < overlap_end:
                overlap_duration = iso_diff_seconds(overlap_end, overlap_start)
                if overlap_duration > best_overlap:
                    best_overlap = overlap_duration
                    best_session = session

        if best_session:
            boat['session_path'] = best_session['session_path']
            matched.append({
                'device_id': device_id,
                'session_path': best_session['session_path'],
                'overlap_sec': best_overlap,
            })
        else:
            matched.append({
                'device_id': device_id,
                'session_path': None,
                'error': 'No overlapping session found',
            })

    race_data['updated_at'] = now_iso()
    save_json(f'races/{race_id}/race.json', race_data)

    return response(200, {'race_id': race_id, 'matched': matched})


def find_device_sessions(device_id, date):
    sessions = []
    prefix = f"processed/{device_id}/{date}"

    try:
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=DATA_BUCKET, Prefix=prefix):
            for obj in page.get('Contents', []):
                if obj['Key'].endswith('/manifest.json'):
                    try:
                        resp = s3.get_object(Bucket=DATA_BUCKET, Key=obj['Key'])
                        manifest = json.loads(resp['Body'].read())
                        parts = obj['Key'].split('/')
                        session_folder = parts[-2]
                        sessions.append({
                            # processed sessions are flat:
                            # processed/<device>/<session_folder>/ — the
                            # folder name already encodes the date, so the
                            # session_path is just the folder (NOT
                            # "<date>/<folder>", which pointed the data
                            # endpoint at a non-existent nested path).
                            'session_path': session_folder,
                            'start_time': manifest.get('start_time', ''),
                            'end_time': manifest.get('end_time', ''),
                        })
                    except Exception:
                        pass
    except Exception:
        pass

    return sessions


def iso_diff_seconds(end, start):
    try:
        fmt = "%Y-%m-%dT%H:%M:%S"
        start_clean = start.replace('Z', '').split('.')[0]
        end_clean = end.replace('Z', '').split('.')[0]
        start_dt = datetime.strptime(start_clean, fmt)
        end_dt = datetime.strptime(end_clean, fmt)
        return (end_dt - start_dt).total_seconds()
    except Exception:
        return 0


# --- Course Auto-Suggest ---

def meters_per_deg_lat():
    return 111320.0


def meters_per_deg_lon(lat):
    return 111320.0 * math.cos(math.radians(lat))


def offset_meters(lat, lon, bearing_deg, dist_m):
    dx = dist_m * math.sin(math.radians(bearing_deg))
    dy = dist_m * math.cos(math.radians(bearing_deg))
    dlat = dy / meters_per_deg_lat()
    dlon = dx / meters_per_deg_lon(lat)
    return lat + dlat, lon + dlon


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def mean_angle_deg(angles):
    if not angles:
        return 0.0
    xs = sum(math.cos(math.radians(a)) for a in angles)
    ys = sum(math.sin(math.radians(a)) for a in angles)
    return (math.degrees(math.atan2(ys, xs)) + 360.0) % 360.0


def angle_diff_deg(a, b):
    return (a - b + 180.0) % 360.0 - 180.0


def load_race_gps(race_data):
    """Return {device_id: [gps_points]} filtered to the race window."""
    start_time = race_data['start_time']
    end_time = race_data['end_time']
    out = {}
    for boat in race_data.get('boats', []):
        device_id = boat.get('device_id')
        session_path = boat.get('session_path')
        if not device_id or not session_path:
            continue
        key = f"processed/{device_id}/{session_path}/gps.json"
        data = load_json(key)
        if not isinstance(data, list):
            continue
        filtered = [d for d in data if start_time <= d.get('t', '') <= end_time]
        if filtered:
            out[device_id] = filtered
    return out


def points_near(points, iso_target, window_sec=30.0):
    out = []
    for p in points:
        t = p.get('t', '')
        if not t:
            continue
        if abs(iso_diff_seconds(t, iso_target)) <= window_sec:
            out.append(p)
    return out


def auto_start_line(race_id):
    """Estimate a start line from fleet positions at the gun time."""
    race_data = load_json(f'races/{race_id}/race.json')
    if not race_data:
        return response(404, {'error': f'Race not found: {race_id}'})

    boat_gps = load_race_gps(race_data)
    if not boat_gps:
        return response(400, {'error': 'No boat session data available for this race'})

    start_iso = race_data['start_time']
    positions = []
    headings = []
    for device_id, gps in boat_gps.items():
        near = points_near(gps, start_iso, window_sec=30.0)
        if not near:
            continue
        closest = min(near, key=lambda p: abs(iso_diff_seconds(p.get('t', ''), start_iso)))
        lat, lon = closest.get('lat'), closest.get('lon')
        if lat is None or lon is None:
            continue
        positions.append((lat, lon))
        cog = closest.get('course')
        if cog is not None:
            headings.append(cog)

    if len(positions) < 1:
        return response(400, {'error': 'No boat positions available at start time'})

    clat = sum(p[0] for p in positions) / len(positions)
    clon = sum(p[1] for p in positions) / len(positions)
    mean_heading = mean_angle_deg(headings) if headings else 0.0
    perp = (mean_heading + 90.0) % 360.0

    if len(positions) >= 2:
        projs = []
        for lat, lon in positions:
            dx_m = (lon - clon) * meters_per_deg_lon(clat)
            dy_m = (lat - clat) * meters_per_deg_lat()
            proj = dx_m * math.sin(math.radians(perp)) + dy_m * math.cos(math.radians(perp))
            projs.append(proj)
        half_len = max(abs(min(projs)), abs(max(projs))) + 30.0
    else:
        half_len = 40.0

    pin_lat, pin_lon = offset_meters(clat, clon, perp, half_len)
    boat_lat, boat_lon = offset_meters(clat, clon, (perp + 180.0) % 360.0, half_len)

    return response(200, {
        'start_line': {
            'pin_lat': pin_lat, 'pin_lon': pin_lon,
            'boat_lat': boat_lat, 'boat_lon': boat_lon,
        },
        'mean_heading_deg': mean_heading,
        'boats_used': len(positions),
    })


def suggest_marks(race_id):
    """Detect rounding points across boat tracks and cluster them into candidate marks."""
    race_data = load_json(f'races/{race_id}/race.json')
    if not race_data:
        return response(404, {'error': f'Race not found: {race_id}'})

    boat_gps = load_race_gps(race_data)
    if not boat_gps:
        return response(400, {'error': 'No boat session data available for this race'})

    COURSE_CHANGE_DEG = 60.0
    WINDOW_SEC = 30.0
    CLUSTER_RADIUS_M = 100.0

    roundings = []
    for device_id, gps in boat_gps.items():
        pts = [p for p in gps if p.get('lat') is not None and p.get('course') is not None]
        if len(pts) < 10:
            continue
        i = 0
        while i < len(pts):
            p = pts[i]
            t_i = p.get('t', '')
            cog_i = p['course']
            j = i + 1
            max_diff = 0.0
            max_j = i
            while j < len(pts):
                t_j = pts[j].get('t', '')
                if not t_j or iso_diff_seconds(t_j, t_i) > WINDOW_SEC:
                    break
                diff = abs(angle_diff_deg(pts[j]['course'], cog_i))
                if diff > max_diff:
                    max_diff = diff
                    max_j = j
                j += 1
            if max_diff >= COURSE_CHANGE_DEG:
                mid = pts[(i + max_j) // 2]
                roundings.append({
                    'lat': mid['lat'], 'lon': mid['lon'],
                    't': mid.get('t', ''), 'device_id': device_id,
                })
                i = max_j + 1
            else:
                i += 1

    if not roundings:
        return response(200, {'marks': [], 'roundings_found': 0})

    clusters = []
    for r in roundings:
        placed = False
        for c in clusters:
            d = haversine_m(r['lat'], r['lon'], c['centroid_lat'], c['centroid_lon'])
            if d <= CLUSTER_RADIUS_M:
                c['points'].append(r)
                n = len(c['points'])
                c['centroid_lat'] = sum(pt['lat'] for pt in c['points']) / n
                c['centroid_lon'] = sum(pt['lon'] for pt in c['points']) / n
                placed = True
                break
        if not placed:
            clusters.append({
                'centroid_lat': r['lat'],
                'centroid_lon': r['lon'],
                'points': [r],
            })

    clusters = [c for c in clusters if len(c['points']) >= 2]

    def avg_time(c):
        times = [pt['t'] for pt in c['points'] if pt['t']]
        if not times:
            return ''
        return sorted(times)[len(times) // 2]

    clusters.sort(key=avg_time)

    suggested = []
    for i, c in enumerate(clusters):
        suggested.append({
            'mark_id': f'sug_{i+1}',
            'name': f'Mark {i + 1}',
            'mark_type': 'windward' if i % 2 == 0 else 'leeward',
            'lat': c['centroid_lat'],
            'lon': c['centroid_lon'],
            'rounding_count': len(c['points']),
        })

    return response(200, {
        'marks': suggested,
        'roundings_found': len(roundings),
        'clusters_found': len(clusters),
    })
