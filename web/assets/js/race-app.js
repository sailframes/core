/**
 * Race Dashboard Application
 *
 * Main controller for the multi-boat race dashboard.
 * Handles race selection, data loading, map visualization,
 * and playback controls.
 */

import {
    DEFAULT_BOAT_LOA_M,
    DEFAULT_BOAT_BEAM_M,
    DEFAULT_BOW_OFFSET_M,
    populateBoatClassDropdown as bcPopulateDropdown,
    setBoatClassInForm as bcSetInForm,
    getBoatClassFromForm as bcGetFromForm,
} from './boat-classes.js?v=2';

// Check if user is authenticated via Cloudflare Access
function isAdmin() {
    return document.cookie.includes('CF_Authorization');
}
const IS_ADMIN = isAdmin();

// Configuration
const API_BASE = window.SAILFRAMES_API_URL || window.location.origin;
const BOAT_COLORS = {
    'E1': '#1d9bf0',  // Blue
    'E2': '#f59e0b',  // Orange
    'E3': '#00ba7c',  // Green
    'E4': '#f4212e',  // Red
    'E5': '#a855f7',  // Purple
    'E6': '#22d3ee',  // Cyan
    'B1': '#ec4899',  // Pink
    'B2': '#84cc16',  // Lime
    'B3': '#d946ef',  // Fuchsia
    'B4': '#eab308',  // Gold
    'B5': '#14b8a6',  // Teal
    'B6': '#f43f5e',  // Rose
};

// Single source of truth for "what color is this boat?" — keyed by
// device id so the polyline, marker, label, leaderboard swatch, and
// chart line/toggle are guaranteed to render the same color for the
// same boat. Use this everywhere instead of indexing BOAT_COLORS
// directly so any future fallback policy stays consistent.
//
// For non-fleet track keys (handicap boats whose GPS came from an
// external GPX upload, keyed by boat_id), derive a deterministic
// HSL colour from the string hash so each boat keeps a stable
// identifying colour across reloads and across the leaderboard /
// map / charts.
function colorFor(trackKey) {
    if (!trackKey) return '#888888';
    if (BOAT_COLORS[trackKey]) return BOAT_COLORS[trackKey];
    return _hashHslFor(trackKey);
}

function _hashHslFor(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) {
        h = (h * 31 + s.charCodeAt(i)) | 0;
    }
    // Hue: 0–359. Skip a band near the most-used fleet hues
    // (red/orange/blue) to keep contrast with E1..E6 markers.
    const hue = Math.abs(h) % 360;
    return `hsl(${hue}, 65%, 58%)`;
}

// Fleet configuration - COURAGEOUS J80 Spring Racing Series 2026
const FLEET_BOATS = ['Wizard', 'Fins', 'Doc Buck', 'Katu', 'Bliss & Ella', 'Amigo'];
const FLEET_TEAMS = ['Vela Veloce', 'Seadogs', 'Mystic Mutiny', 'Rooster Alumni Club', 'Anchor Management', 'Team 6'];

// Persisted catalog of team / boat names this browser has seen.
// Auto-populated from every race load (so anyone who's browsed a
// regatta gets those teams in their autocomplete) and after every
// save. Lives in localStorage so it survives page reloads.
const TEAM_STORE_KEY = 'sf-known-teams';
const BOAT_STORE_KEY = 'sf-known-boats';

function _readLocalSet(key) {
    try {
        const raw = localStorage.getItem(key);
        if (!raw) return new Set();
        const arr = JSON.parse(raw);
        return new Set(Array.isArray(arr) ? arr.filter(s => typeof s === 'string' && s.trim()) : []);
    } catch { return new Set(); }
}
function _writeLocalSet(key, set) {
    try { localStorage.setItem(key, JSON.stringify(Array.from(set))); } catch {}
}
function _attrEsc(s) {
    return String(s).replace(/[&"<>]/g, c =>
        ({'&':'&amp;','"':'&quot;','<':'&lt;','>':'&gt;'}[c]));
}

// Merge the built-in fleet list with anything stored locally and
// anything already on the currently-loaded race. Returns a sorted,
// de-duped array.
function knownTeamNames() {
    const s = new Set([...FLEET_TEAMS, ..._readLocalSet(TEAM_STORE_KEY)]);
    if (currentRace?.boats) for (const b of currentRace.boats) {
        if (b?.team_name) s.add(String(b.team_name).trim());
    }
    s.delete('');
    return Array.from(s).sort((a, b) => a.localeCompare(b));
}
function knownBoatNames() {
    const s = new Set([...FLEET_BOATS, ..._readLocalSet(BOAT_STORE_KEY)]);
    if (currentRace?.boats) for (const b of currentRace.boats) {
        if (b?.boat_name) s.add(String(b.boat_name).trim());
    }
    s.delete('');
    return Array.from(s).sort((a, b) => a.localeCompare(b));
}

// Harvest team / boat names from any race object (just-saved race
// or the race that loadRaceData() returned) and append them to the
// local store so they appear in future autocomplete dropdowns.
function rememberRaceNames(race) {
    if (!race?.boats) return;
    const teams = _readLocalSet(TEAM_STORE_KEY);
    const boats = _readLocalSet(BOAT_STORE_KEY);
    let tDirty = false, bDirty = false;
    for (const b of race.boats) {
        const t = (b?.team_name || '').trim();
        const n = (b?.boat_name || '').trim();
        if (t && !FLEET_TEAMS.includes(t) && !teams.has(t)) { teams.add(t); tDirty = true; }
        if (n && !FLEET_BOATS.includes(n) && !boats.has(n)) { boats.add(n); bDirty = true; }
    }
    if (tDirty) _writeLocalSet(TEAM_STORE_KEY, teams);
    if (bDirty) _writeLocalSet(BOAT_STORE_KEY, boats);
}

// State
let regattas = [];
// regatta_ids flagged visibility==='admin' — hidden from the picker and the
// site-wide "latest race" auto-load for non-admins (frontend obscurity).
let hiddenRegattaIds = new Set();
let raceDays = [];  // Race days for selected regatta
let races = [];     // Races for selected race day
let currentRaceDay = null;
let currentRace = null;
let raceData = null;
let map = null;
let boatLayers = {};  // device_id -> { track, marker }
// AIS traffic overlay (non-participant vessels). Deliberately a separate
// structure from boatLayers/raceData.boats: these render on the map and
// animate with the scrubber but NEVER enter the leaderboard or charts.
let aisData = null;          // raw API doc
let aisMarkers = {};         // mmsi -> { marker, vessel, times, positions }
let aisLayerGroup = null;    // L.layerGroup holding the vessel markers
let aisControl = null;       // Leaflet toggle control
let aisVisible = false;      // default OFF (data too sparse to be useful); persisted in localStorage 'sf_ais_on'
let sfwInited = false;       // SFWeather overlay init-once guard
let weatherObsHidden = false;// Weather "Obs buoys" toggle hides the NOAA wind-rose markers
let isPlaying = false;
let playbackSpeed = 10;  // default 10× — 25-min race plays in ~2.5 min
let currentTime = 0;  // seconds from race start
let raceDuration = 0;
let playbackInterval = null;
let speedChart = null;
let heelChart = null;
let windChart = null;
let playCursorSeconds = 0;

// Wind data from nearby NOAA stations (Castle Island CSIM3, Logan KBOS,
// Boston 16NM 44013). One boat has an onboard Calypso; the rest don't.
// CSIM3 is ~3 km from Boston Harbor sailing waters with marine
// instrumentation, so it's the right authoritative TWD/TWS source for
// fleet-wide tactical metrics (laylines, TWA, wind badge).
let raceBuoyData = {};            // {stationId: {data_points, lat, lon, name, color, ...}}
let weatherWindSamples = [];       // sorted [{tMs, twd, tws}] from primary source
let weatherWindSource = null;      // "Castle Island" / "Logan" / null
let raceAvgTWD = null;             // average TWD across the race window, for laylines
let laylineLayer = null;           // Leaflet layer group holding rendered laylines
let windMarker = null;             // legacy single-marker (kept for compatibility)
let windMarkers = {};              // stationId → Leaflet marker (multi-station rendering)
let windStationStats = {};         // stationId → { meanTWD, stdTWD, minTWS, maxTWS, avgTWS, sampleCount }
let _windDropdownOutsideListener = null;  // ref-tracked so re-renders don't leak handlers

// Set of station IDs to show in dropdown + on map. Computed every picker
// render to dedupe stations that point at the same physical sensor through
// multiple aggregator paths (e.g. KBOS direct vs SYN_KBOS via Synoptic).
let visibleStationIds = new Set();
// Preferred-first ordering for the auto-pick. Synoptic SYN_* stations
// are added dynamically below if/when they appear in the API response.
// Order matters: the auto-pick at race load walks this list and takes
// the first station with usable samples.
//
// Castle Island (CSIM3) is the operational default for the Boston
// Harbor fleet — it's right at the start area, so its TWD reflects
// what the boats actually see. Boston 16NM (NDBC 44013, ocean buoy)
// and Logan Airport (KBOS, METAR) stay as fallbacks for the rare
// race window where CSIM3 has no usable samples.
const PRIMARY_WIND_STATIONS = ['CSIM3', '44013', 'KBOS'];
let selectedWindStationId = null;  // user-selected wind source (null = auto-pick)
// Race-level wind-station override set by a coach via the wind picker's
// "Set as default" button. `{race_id, station_id, set_by, set_at}` once
// fetched from the coach Lambda's public GET endpoint; null if no override.
let windDefaultOverride = null;

// User-controlled visibility toggles. Defaults are ON so the dashboard
// shows everything on first load; per-user overrides persist through
// a race session but reset when the page reloads (intentional — no
// cross-session state).
let laylinesVisible = true;

// Multi-class handicap races: which class(es) to show in the
// leaderboard + on the map. 'all' = both classes; a specific class id
// (e.g. 'A') = filter to that class only. Ignored when the race has
// no classes[] defined. Persisted to URL (?class=A) and localStorage
// so coaches can deep-link / refresh without losing the filter.
let classFilter = 'all';
// Trail window: how much past track to render as a polyline. Default 1 min
// keeps the map readable; "All" (Infinity) restores the historical full-race
// behavior. Changing this re-renders every boat's trail at the current
// playback time.
let trailWindowMs = 60_000;
// Index into currentRace.course of the next mark the leader has yet to round.
// Drives "next windward only" layline rendering — when the leader rounds the
// current target, laylines shift to the next mark (and disappear if that
// mark is downwind). 0 = pre-start, course.length = finished.
let activeLeg = 0;
// TWD used to draw the currently rendered laylines. Updated each playback
// tick from windAt(targetTime) so the laylines pivot when the wind shifts.
// null = haven't sampled yet → renderLaylines falls back to raceAvgTWD.
let lastLaylineTWD = null;

// Per-marker overlay toggles, controlled by the top-right Leaflet legend.
// Each item adds (or removes) one piece of information from every boat's
// map cursor / track in real time. Defaults match the most-used readout.
//   trail — the colored polyline behind the boat (still trimmed by the
//           top-right trail-window picker for duration)
//   speed — current SOG in knots, beside the arrow
//   heel  — heel angle from the IMU, with Sd/Pt prefix
//   twa   — true wind angle (signed; P/S prefix). Per-boat — derived from
//           NOAA TWD and the boat's COG, so it works for every boat
//           regardless of whether they have an onboard wind sensor. This
//           is the most actionable wind metric on a boat marker: AWA is
//           nice but only one boat in the fleet has the Calypso, and TWD
//           is fleet-global (already on the wind picker).
// Polar-ghost mode: 'targetSpeed' (your own line at polar target speed) or
// 'optimal' (ideal VMG route to the course marks). Persisted; see updateGhost.
let ghostMode = 'targetSpeed';
let markerOverlays = {
    trail:    true,    // only the trail is on by default — keeps the map
    speed:    false,   //   uncluttered until the user opts in to extra
    heel:     false,   //   readouts via the SHOW legend
    twa:      false,
    vmg:      false,
    polarPct: false,
    rank:     false,
    // Polar "ghost" / target-boat overlay for the SELECTED boat — see
    // updateGhost(). Sails your own line at polar target speed.
    polarGhost: false,
    // Sail number next to the boat's initials. Off by default — keeps the
    // map cleaner; toggle on via the SHOW legend.
    sail:     false,
    // GNSS accuracy from the per-sample HDOP, surfaced as
    // ±<N>cm next to the boat label. HDOP is unit-less but
    // multiplying by ~1 m UERE (LG290P standard mode) gives a
    // practical horizontal-uncertainty estimate in metres; we
    // render it in centimetres for granularity at sailing scales.
    hdop: false,
    // Distance lines between adjacent boats by leaderboard rank, with
    // small athwartships tick marks at each boat (perpendicular to its
    // COG) and a centre label in metres. Useful for cross-tack /
    // pre-rounding tactical separation; off by default to keep the
    // map clean.
    distances: false,
    // Trail is rendered as N short coloured segments per boat instead
    // of one solid polyline. Hue stays the boat's team colour; per-
    // segment lightness/opacity/width encode speed within that
    // boat's recent track (same encoding as the tack-analysis modal,
    // slow = bright/opaque/thick, fast = dark/faded/thin). Falls
    // back to the original solid polyline when off (default — keeps
    // the map quiet until the user opts in).
    trailSpeedColor: false,
};

// Auto-follow: keep the map framed on the current race leader and the
// mark they're sailing toward. Re-fits bounds at most every 700 ms to
// avoid jitter during slider scrubbing or fast playback. Toggle in the
// topright FOLLOW control; persisted to localStorage.
let followLeader = true;
let lastFollowPanMs = 0;
let polarOverlayVisible = true;

// J/80 typical upwind tack angle (degrees off true wind). Used for laylines.
const J80_UPWIND_TACK_ANGLE = 42;

// J/80 polar table (Seapilot format, 2018 publication).
// Columns: TWS in knots. Rows: target boat speed in knots at each (TWA, TWS).
// Source: https://www.seapilot.com/wp-content/uploads/2018/05/J80.txt
const J80_TWS = [6, 8, 10, 12, 14, 16, 20];
const J80_TWA_TABLE = [52.5, 60, 75, 90, 105, 120, 135, 150, 165, 180];
const J80_BSPEED_TABLE = [
    [5.514, 6.369, 6.818, 7.039, 7.178, 7.261, 7.311],  // 52.5°
    [5.787, 6.677, 7.125, 7.344, 7.492, 7.575, 7.670],  // 60°
    [6.235, 7.013, 7.473, 7.830, 8.024, 8.164, 8.345],  // 75°
    [6.733, 7.380, 7.726, 7.957, 8.303, 8.613, 8.923],  // 90°
    [6.775, 7.537, 8.031, 8.326, 8.582, 8.812, 9.197],  // 105°
    [6.381, 7.326, 8.021, 8.573, 8.999, 9.273, 9.753],  // 120°
    [5.605, 6.634, 7.317, 7.881, 8.439, 8.935, 9.813],  // 135°
    [3.928, 5.127, 5.953, 6.638, 7.190, 7.658, 8.557],  // 150°
    [2.996, 3.970, 4.897, 5.706, 6.375, 6.934, 7.763],  // 165°
    [2.799, 3.752, 4.617, 5.450, 6.115, 6.727, 7.632],  // 180°
];
const J80_BEAT_ANGLE = [45.2, 41.4, 40.4, 40.0, 39.8, 39.9, 39.9];
const J80_BEAT_VMG   = [3.550, 4.224, 4.561, 4.765, 4.880, 4.951, 4.960];

// Bilinear-interp target boat speed for given TWA (signed) and TWS.
// The lookup table is hard-coded for the J/80 — when the race uses any
// other class, return null so every consumer (chart polar overlay,
// %polar in the leaderboard / marker label / drawer / briefing / legs
// table) short-circuits and the polar metric is hidden rather than
// shown with bogus J/80-derived numbers.
function polarTargetSpeed(twaSigned, tws) {
    if (!polarSupportedForCurrentRace()) return null;
    if (twaSigned == null || tws == null || !Number.isFinite(tws) || tws <= 0) return null;
    // Polar is symmetric port/starboard
    let a = Math.abs(twaSigned);
    if (a > 180) a = 360 - a;

    // Clamp TWS to polar range
    const twsArr = J80_TWS;
    const tt = Math.max(twsArr[0], Math.min(twsArr[twsArr.length - 1], tws));

    // TWS bracket
    let iLo = 0;
    while (iLo < twsArr.length - 1 && twsArr[iLo + 1] <= tt) iLo++;
    const iHi = Math.min(iLo + 1, twsArr.length - 1);
    const twsF = (iHi === iLo) ? 0 : (tt - twsArr[iLo]) / (twsArr[iHi] - twsArr[iLo]);

    // Per-TWS speed at TWA `a`. Synth a row at the optimal beat angle so
    // 40°-52.5° has a slope and TWAs below the beat angle return a graceful
    // pinching estimate (linear from 0 to beatSpeed).
    const speedAtCol = (col) => {
        const beatA = J80_BEAT_ANGLE[col];
        const beatSpd = J80_BEAT_VMG[col] / Math.cos(beatA * Math.PI / 180);
        if (a <= beatA) {
            // Below optimal — pinching. Crude linear from (0,0) to (beatA,beatSpd).
            return Math.max(0, (a / beatA) * beatSpd);
        }
        const angles = [beatA, ...J80_TWA_TABLE];
        const speeds = [beatSpd, ...J80_BSPEED_TABLE.map(row => row[col])];
        let aLo = 0;
        while (aLo < angles.length - 1 && angles[aLo + 1] < a) aLo++;
        const aHi = Math.min(aLo + 1, angles.length - 1);
        const aF = (aHi === aLo) ? 0 : (a - angles[aLo]) / (angles[aHi] - angles[aLo]);
        return speeds[aLo] * (1 - aF) + speeds[aHi] * aF;
    };

    const sLo = speedAtCol(iLo);
    const sHi = speedAtCol(iHi);
    return sLo * (1 - twsF) + sHi * twsF;
}

// True when the loaded race uses the J/80 — the only class for which
// we ship a polar table. Any other class would pull bogus targets
// from the J/80 lookup, so all polar-derived metrics (target speed,
// %polar, on-chart polar overlay) are short-circuited downstream.
// Legacy races with no boat_class set are treated as J/80 (the
// historical default), preserving every old dashboard view.
function polarSupportedForCurrentRace() {
    const cls = currentRace?.boat_class;
    if (cls == null) return true;
    if (typeof cls === 'string') {
        const s = cls.trim();
        return !s || s === 'J/80';
    }
    return cls.id === 'j80';
}

function polarPercent(sog, twaSigned, tws) {
    // Class gate is enforced inside polarTargetSpeed — calling it
    // here returns null for non-J/80 races, which cascades through
    // and skips the %polar display everywhere it's used.
    const target = polarTargetSpeed(twaSigned, tws);
    if (!target || target <= 0.1) return null;
    return (sog / target) * 100;
}
let availableSessions = {};  // device_id -> [session paths]
let pendingGpxFiles = {};   // device_id -> File (staged GPX uploads)
// Parallel store for boats with NO fleet device — staged GPX files
// keyed by boat_id. Uploaded via the by-boat-id endpoint on save.
let pendingGpxFilesByBoatId = {};
// Vakaros .vkx binary uploads keyed by boat_id. Server-side parser
// converts to the same GPS-points shape as GPX, stored under the
// same by-boat-id path — so the dashboard treats both identically.
let pendingVkxFilesByBoatId = {};

// Pre-race display: show 3 minutes before start
const PRE_RACE_SECONDS = 180;

// Initialize
document.addEventListener('DOMContentLoaded', init);

async function init() {
    console.log('[Race] Initializing race dashboard...');

    // Hide admin controls for non-authenticated users. Race-mutating
    // actions (create / edit / duplicate / course-copy) all live inside
    // the Race-management dropdown now — hiding the wrapper hides them
    // all in one shot. Guests still see read-only controls (regatta
    // picker, charts, legs/maneuvers, tactics).
    if (!IS_ADMIN) {
        const rm = document.getElementById('race-mgmt-dropdown');
        if (rm) rm.style.display = 'none';
    }

    // Initialize map
    initMap();
    addLaylinesMapControl();
    addFollowLeaderMapControl();
    addMarkerOverlaysMapControl();   // includes the trail-window dropdown
    addMapWindPicker();
    setupChartsOverlay();

    // Initialize chart
    initSpeedChart();

    // Load regattas (race days and races loaded on selection)
    await loadRegattas();

    // Setup event listeners
    setupEventListeners();

    // Drawer close
    const drawerClose = document.getElementById('drawer-close');
    if (drawerClose) drawerClose.addEventListener('click', closeBoatDrawer);
    // Esc key closes the drawer / open modals
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            if (drawerDeviceId) closeBoatDrawer();
            for (const id of ['leg-modal', 'maneuver-modal', 'tack-analysis-modal', 'roll-tacking-modal', 'orrez-modal']) {
                const m = document.getElementById(id);
                if (m && m.style.display !== 'none') m.style.display = 'none';
            }
        }
    });

    // Modal close buttons
    for (const el of document.querySelectorAll('[data-close-modal]')) {
        el.addEventListener('click', () => {
            const id = el.getAttribute('data-close-modal');
            const m = document.getElementById(id);
            if (m) m.style.display = 'none';
        });
    }

    // Polar overlay toggle
    const polTog = document.getElementById('polar-overlay-toggle');
    if (polTog) polTog.addEventListener('click', () => {
        polarOverlayVisible = !polarOverlayVisible;
        polTog.classList.toggle('active', polarOverlayVisible);
        updateSpeedChart();
    });

    // Legs / Maneuvers modals
    const legBtn = document.getElementById('btn-leg-summary');
    if (legBtn) legBtn.addEventListener('click', openLegModal);
    const manBtn = document.getElementById('btn-maneuvers');
    if (manBtn) manBtn.addEventListener('click', openManeuverModal);
    const taBtn = document.getElementById('btn-tack-analysis');
    if (taBtn) taBtn.addEventListener('click', openTackAnalysisModal);
    const rtBtn = document.getElementById('btn-roll-tacking');
    if (rtBtn) rtBtn.addEventListener('click', openRollTackingModal);
    const ppBtn = document.getElementById('btn-polar-plot');
    if (ppBtn) ppBtn.addEventListener('click', openPolarOverlay);
    const orrezBtn = document.getElementById('btn-orrez-explainer');
    if (orrezBtn) orrezBtn.addEventListener('click', openOrrEzModal);

    // Tactics-discussion drawer
    setupTacticsDrawer();

    // Toolbar dropdowns (Race management, Analytics, Learning, Discuss Tactics with…)
    setupToolbarDropdown('btn-race-mgmt', 'race-mgmt-menu');
    setupToolbarDropdown('btn-analytics', 'analytics-menu');
    setupToolbarDropdown('btn-learning', 'learning-menu');
    setupToolbarDropdown('btn-tactics-dd', 'tactics-menu');

    // Mobile UX (only active when viewport ≤ 900 px — see race.css media query)
    setupMobileNav();

    // Ensure Leaflet has correct dimensions after initial CSS layout settles
    // (on mobile the map panel height depends on flex:1 inside .race-main,
    // which isn't necessarily resolved when initMap() runs).
    setTimeout(() => { if (map) map.invalidateSize(); }, 250);
    window.addEventListener('resize', () => {
        if (map) map.invalidateSize();
    });

    // Deep-link support: ?race=<race_id> loads that specific race instead
    // of the auto-pick. Used by the coach app, which iframes this page,
    // and by shared WhatsApp links + notification emails. ?race_id= is
    // honored as an alias for legacy URLs (older notification emails).
    const params = new URLSearchParams(location.search);
    const raceParam = params.get('race') || params.get('race_id');
    if (raceParam) {
        try {
            const r = await fetch(`${API_BASE}/api/races/${raceParam}`);
            if (r.ok) {
                const race = await r.json();
                const regattaId = race.regatta_id || '__all__';
                const regSel = document.getElementById('regatta-select');
                if (regSel) regSel.value = regattaId;
                await loadRaceDays(regattaId);
                const daySel = document.getElementById('raceday-select');
                if (daySel) daySel.value = race.date;
                loadRacesForDay(race.date);
                const raceSel = document.getElementById('race-select');
                if (raceSel) raceSel.value = raceParam;
                await loadRaceData(raceParam);
                console.log('[Race] Dashboard ready (deep-link to', raceParam, ')');
                return;
            }
            console.warn('[Race] Deep-link race not found, falling back to latest:', raceParam);
        } catch (err) {
            console.warn('[Race] Deep-link load failed, falling back to latest:', err);
        }
    }

    // ?regatta=<id> — landing-page deep link. Pre-set the regatta
    // dropdown and auto-load the latest race within that regatta
    // (instead of the global latest). Falls through to the unfiltered
    // auto-pick if the regatta is unknown or has no races with data.
    const regattaParam = params.get('regatta');
    if (regattaParam) {
        try {
            const reg = (regattas || []).find(r => r.regatta_id === regattaParam);
            if (reg) {
                const regSel = document.getElementById('regatta-select');
                if (regSel) regSel.value = regattaParam;
                await loadRaceDays(regattaParam);
                if (await loadLatestRaceWithData(regattaParam)) {
                    console.log('[Race] Dashboard ready (deep-link to regatta', regattaParam, ')');
                    return;
                }
                console.log('[Race] Regatta', regattaParam, 'has no races with data; showing day picker.');
                return;
            }
            console.warn('[Race] Deep-link regatta not found, falling back to latest:', regattaParam);
        } catch (err) {
            console.warn('[Race] Regatta deep-link failed, falling back to latest:', err);
        }
    }

    // Auto-load the most recent race with boat data
    await loadLatestRaceWithData();

    console.log('[Race] Dashboard ready');
}

// ---------------------------------------------------------------------------
// Leaderboard hide/show — toggle button + URL param + persisted setting.
// Hidden state collapses the right column so the map fills the whole row.
//
// Critical: the body class must be applied BEFORE init() runs so that
// Leaflet sizes the map for the wider (leaderboard-hidden) container at
// creation time. If we wait for DOMContentLoaded the map is already
// sized for the narrow layout and the captured screenshots show the
// boats clustered in a quarter of the canvas with empty area to the
// right. Doing it synchronously at script-parse time fixes that.
// ---------------------------------------------------------------------------
(function applyLayoutFromUrl() {
    try {
        const params = new URLSearchParams(location.search);
        const urlHidden = params.get('leaderboard_hidden') === '1';
        let hidden = urlHidden;
        if (!urlHidden) {
            try { hidden = localStorage.getItem('sf-leaderboard-hidden') === '1'; } catch {}
        }
        if (hidden && document.body) document.body.classList.add('leaderboard-hidden');
    } catch {}
})();

function setLeaderboardHidden(hidden) {
    document.body.classList.toggle('leaderboard-hidden', !!hidden);
    try { localStorage.setItem('sf-leaderboard-hidden', hidden ? '1' : '0'); } catch {}
    // Tell Leaflet the container resized; then refit bounds so the map
    // fills the new area instead of staying zoomed for the old size.
    setTimeout(() => {
        if (typeof map !== 'undefined' && map) {
            map.invalidateSize();
            if (typeof fitMapToBounds === 'function') fitMapToBounds();
        }
    }, 50);
}
document.addEventListener('DOMContentLoaded', () => {
    const hideBtn = document.getElementById('btn-hide-leaderboard');
    const showBtn = document.getElementById('btn-show-leaderboard');
    if (hideBtn) hideBtn.addEventListener('click', () => setLeaderboardHidden(true));
    if (showBtn) showBtn.addEventListener('click', () => setLeaderboardHidden(false));
});

// ---------------------------------------------------------------------------
// Coach-app integration. Same-origin iframes (the coach review page is on
// sailframes.com too) call these to drive the live race page.
//
//   window.seekTo(timeSecondsFromRaceStart)
//     → moves the playback cursor to that moment so capture grabs that view
//
//   window.captureMapPng()
//     → snapshots the Leaflet map as a PNG dataURL using html2canvas.
//
// Note: tile layers must be created with `crossOrigin: 'anonymous'` or the
// canvas gets tainted (and html2canvas with allowTaint:false silently
// produces an empty image). Set as the default for all L.TileLayer instances
// via mergeOptions so every basemap option is screenshot-safe.
// ---------------------------------------------------------------------------
if (typeof L !== 'undefined' && L.TileLayer && L.TileLayer.mergeOptions) {
    L.TileLayer.mergeOptions({ crossOrigin: 'anonymous' });
}

window.seekTo = function (timeSecondsFromRaceStart) {
    if (typeof updateBoatPositions !== 'function') return false;
    if (typeof updatePlayCursor !== 'function') return false;
    const t = Math.max(0, +timeSecondsFromRaceStart || 0);
    updateBoatPositions(t);
    updatePlayCursor(t);
    const slider = document.getElementById('timeline-slider');
    if (slider) slider.value = String(t);
    return true;
};

// Re-fit the map to the race's primary view (start line + first mark, then
// fallback to all boat coordinates). Coach app calls this after seeking to
// make sure the screenshot frames the action tightly instead of inheriting
// a zoom from earlier panning.
window.fitMapToRace = function () {
    if (typeof fitMapToBounds !== 'function') return false;
    if (typeof map !== 'undefined' && map) map.invalidateSize();
    fitMapToBounds();
    return true;
};

// True once the map has tiles, the race data is loaded, and at least one
// boat marker has been placed. The coach app polls this before capturing.
window.captureReady = function () {
    if (typeof map === 'undefined' || !map) return false;
    const el = document.getElementById('race-map');
    if (!el || el.clientWidth === 0 || el.clientHeight === 0) return false;
    if (typeof boatLayers !== 'object' || !Object.keys(boatLayers).length) return false;
    return true;
};

// Capture engine version. Bump when the pipeline changes meaningfully so
// the coach app can auto-replace stale captures stored from older code.
window.CAPTURE_ENGINE_VERSION = 6;

// Wait until every visible Leaflet tile has the .leaflet-tile-loaded class
// (Leaflet adds it once the <img> finishes loading). Polls every 80 ms.
// Times out after 5 s so a stuck CDN tile doesn't hold up the sweep.
async function _waitTilesLoaded(timeoutMs = 5000) {
    const el = document.getElementById('race-map');
    if (!el) return;
    const startedAt = Date.now();
    while (Date.now() - startedAt < timeoutMs) {
        const tiles = el.querySelectorAll('.leaflet-tile-pane img.leaflet-tile');
        if (!tiles.length) return;
        const loaded = el.querySelectorAll('.leaflet-tile-pane img.leaflet-tile-loaded');
        if (loaded.length === tiles.length) {
            // One extra frame so the tile fade-in CSS animation settles.
            await new Promise(r => requestAnimationFrame(r));
            return;
        }
        await new Promise(r => setTimeout(r, 80));
    }
}

// Quick "is this canvas mostly empty?" check — guards against html2canvas
// returning a blank PNG when something went sideways.
function _canvasHasContent(canvas) {
    try {
        const w = Math.min(canvas.width, 100), h = Math.min(canvas.height, 100);
        const ctx = canvas.getContext('2d');
        const data = ctx.getImageData(0, 0, w, h).data;
        // Non-trivial if any pixel deviates from the seed color by ≥ 8/channel.
        const r0 = data[0], g0 = data[1], b0 = data[2];
        for (let i = 4; i < data.length; i += 4) {
            if (Math.abs(data[i] - r0) > 8) return true;
            if (Math.abs(data[i + 1] - g0) > 8) return true;
            if (Math.abs(data[i + 2] - b0) > 8) return true;
        }
        return false;
    } catch { return true; }   // assume content if we can't check
}

window.captureMapPng = async function () {
    const el = document.getElementById('race-map');
    if (!el) throw new Error('Map element not in DOM');
    if (el.clientWidth === 0 || el.clientHeight === 0) throw new Error('Map has zero size');
    if (typeof html2canvas !== 'function') throw new Error('html2canvas not loaded');

    // Always tell Leaflet "your container may have resized" before we snap.
    // Without this, captures inherit a tile layout that was sized for an
    // older container width — and you get tiles in only a quarter of the
    // canvas with empty area filling the rest.
    if (typeof map !== 'undefined' && map) {
        map.invalidateSize({ animate: false, pan: false });
    }
    // Wait for tile fetches in flight to complete (after a recent seek/fit).
    await _waitTilesLoaded();
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));

    // FORCEFULLY remove controls from the DOM during capture. `display:none`
    // wasn't being honored in some cases (the SHOW legend kept appearing
    // in the captures despite the inline style) — physically detaching the
    // nodes and re-attaching them in the `finally` is unambiguous.
    const HIDE_SELECTORS = [
        '.marker-overlays-control',
        '.leaflet-control-layers',
        '.leaflet-control-zoom',
        '.leaflet-control-attribution',
    ];
    const detached = [];
    for (const sel of HIDE_SELECTORS) {
        for (const node of Array.from(el.querySelectorAll(sel))) {
            detached.push({ node, parent: node.parentNode, next: node.nextSibling });
            node.parentNode.removeChild(node);
        }
    }

    // The coach iframe explicitly requests `?basemap=ESRI Ocean` (a natively
    // light, blue-water-themed tile set with no CSS filter dependency). So
    // a single straightforward html2canvas pass is enough — what the coach
    // sees in the iframe is what the screenshot is.
    // scale: 1 keeps captured PNGs under the Lambda 6 MB PUT limit. On
    // retina (devicePixelRatio=2) we'd otherwise produce 4× pixels per
    // capture and a 6-paragraph briefing's attachments would silently
    // fail to save. 1100×720 → ~250 KB JPEG is plenty for the PDF.
    const baseOpts = {
        useCORS: true, allowTaint: false,
        logging: false, scale: 1,
    };
    // Final dataURL format: JPEG at 0.85 keeps the per-capture payload
    // ~5× smaller than PNG. Map screenshots tolerate JPEG fine; the
    // foreground vectors are anti-aliased before composite anyway.
    const toDataUrl = (canvas) => canvas.toDataURL('image/jpeg', 0.85);


    try {
        const canvas = await html2canvas(el, { ...baseOpts, backgroundColor: '#0f1419' });
        return toDataUrl(canvas);
    } finally {
        // Re-attach controls in their original positions.
        for (const { node, parent, next } of detached) {
            try {
                if (next && next.parentNode === parent) parent.insertBefore(node, next);
                else parent.appendChild(node);
            } catch {}
        }
    }
};

// Fit the map to the bounding box of all boat positions in a specific time
// window. Coach app calls this so each per-paragraph capture frames the
// section being discussed (instead of the static race-wide bounds).
window.fitToTimeRange = function (tStartSecondsFromRaceStart, tEndSecondsFromRaceStart) {
    if (typeof map === 'undefined' || !map) return false;
    if (typeof currentRace === 'undefined' || !currentRace || !currentRace.start_time) return false;
    const raceStart = new Date(currentRace.start_time).getTime();
    const tStart = raceStart + Math.max(0, +tStartSecondsFromRaceStart || 0) * 1000;
    const tEnd = raceStart + Math.max(0, +tEndSecondsFromRaceStart || 0) * 1000;
    if (tEnd <= tStart) return false;
    const coords = [];
    if (typeof boatLayers === 'object' && boatLayers) {
        for (const layer of Object.values(boatLayers)) {
            if (!layer || !Array.isArray(layer.data)) continue;
            for (const p of layer.data) {
                if (!p || p.lat == null || p.lon == null || !p.t) continue;
                const t = new Date(p.t).getTime();
                if (t >= tStart && t <= tEnd) coords.push([p.lat, p.lon]);
            }
        }
    }
    if (coords.length < 2) return false;
    map.invalidateSize();
    // Tighter framing for per-section captures: maxZoom 18 lets
    // tightly-clustered boats fill the canvas; padding 20 px gives just
    // enough breathing room without empty space.
    map.fitBounds(L.latLngBounds(coords), { padding: [20, 20], maxZoom: 18 });
    return true;
};

async function loadLatestRaceWithData(regattaFilter = null) {
    try {
        // Fetch races, optionally scoped to a single regatta when called
        // from the ?regatta=<id> deep-link path.
        const url = regattaFilter
            ? `${API_BASE}/api/races?regatta_id=${encodeURIComponent(regattaFilter)}`
            : `${API_BASE}/api/races`;
        const resp = await fetch(url);
        const data = await resp.json();
        const allRaces = data.races || [];

        const now = new Date();

        // Exclude races belonging to admin-only regattas so a hidden regatta
        // never becomes the default race loaded for non-admins.
        const visibleRaces = IS_ADMIN
            ? allRaces
            : allRaces.filter(r => !hiddenRegattaIds.has(r.regatta_id));

        // Find races with boats assigned (boat_count > 0), not in the future, sorted by date descending
        const racesWithBoats = visibleRaces
            .filter(r => r.boat_count > 0 && new Date(r.start_time) <= now)
            .sort((a, b) => {
                // Sort by start_time descending (most recent first)
                return new Date(b.start_time) - new Date(a.start_time);
            });

        if (racesWithBoats.length === 0) {
            console.log('[Race] No past races with boats found' + (regattaFilter ? ` for regatta ${regattaFilter}` : ''));
            return false;
        }

        // Pick the latest race DAY, then pick a race within it.
        //
        // - Unfiltered auto-pick (no regatta): Race 2 by convention —
        //   Race 1 has the most variable course-setup churn, Race 2
        //   is usually the cleanest "real race" to debrief. Falls
        //   back to Race 1 if the day only has one race.
        // - Regatta deep-link (?regatta=<id> from the landing page):
        //   Race 1 instead — when a visitor clicks into a series they
        //   want the natural starting point of that day's racing,
        //   not to land mid-card.
        const latestDate = racesWithBoats[0].date;
        const dayRaces = racesWithBoats
            .filter(r => r.date === latestDate)
            .sort((a, b) => new Date(a.start_time) - new Date(b.start_time));
        const targetRace = regattaFilter ? dayRaces[0] : (dayRaces[1] || dayRaces[0]);
        console.log(`[Race] Auto-loading ${targetRace.name} of ${targetRace.date} (regattaFilter=${regattaFilter || 'none'}):`, targetRace.race_id);

        // Set the regatta dropdown (use __all__ for races without regatta)
        const regattaId = targetRace.regatta_id || '__all__';
        document.getElementById('regatta-select').value = regattaId;
        await loadRaceDays(regattaId);

        // Set the race day dropdown
        document.getElementById('raceday-select').value = targetRace.date;
        loadRacesForDay(targetRace.date);

        // Set the race dropdown
        document.getElementById('race-select').value = targetRace.race_id;

        // Load the race data
        await loadRaceData(targetRace.race_id);
        return true;

    } catch (err) {
        console.error('[Race] Failed to auto-load latest race:', err);
        return false;
    }
}

// --- Map ---

function initMap() {
    map = L.map('race-map', {
        center: [42.36, -71.05],  // Boston Harbor
        zoom: 14,
        zoomControl: true,
        maxZoom: 20,  // Allow deep zoom regardless of tile layer limits
        // Disable Leaflet's built-in arrow-key panning so ←/→ are
        // reserved for the timeline scrubber (1 s nudges, 10 s with
        // Shift). The user can still drag/zoom the map with mouse.
        keyboard: false,
    });

    // Base layers
    const baseLayers = {
        'NOAA Charts': L.tileLayer.wms('https://gis.charttools.noaa.gov/arcgis/rest/services/MCS/NOAAChartDisplay/MapServer/exts/MaritimeChartService/WMSServer', {
            layers: '0,1,2,3,4,5,6,7',
            format: 'image/png',
            transparent: true,
            attribution: '&copy; <a href="https://nauticalcharts.noaa.gov">NOAA</a>',
            maxZoom: 18,
        }),
        'Dark': L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
            attribution: '&copy; OpenStreetMap, &copy; CARTO',
            maxZoom: 19,
        }),
        // Same Carto dark_all tiles as "Dark", but inverted + hue-shifted
        // via CSS so the dark canvas reads as a soft light blue. The
        // .tile-light-blue class is defined in race.css.
        'Light Blue': L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
            attribution: '&copy; OpenStreetMap, &copy; CARTO',
            maxZoom: 19,
            className: 'tile-light-blue',
        }),
        // Carto Voyager (no labels) — natively light, soft blue water,
        // beige land. Tonally close to Light Blue but rendered without
        // a CSS filter, so it captures cleanly via html2canvas. The coach
        // iframe loads with `?basemap=Voyager` so screenshots match the
        // iframe view (Light Blue's CSS invert+hue filter doesn't survive
        // browser-side capture).
        'Voyager': L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager_nolabels/{z}/{x}/{y}{r}.png', {
            attribution: '&copy; OpenStreetMap, &copy; CARTO',
            maxZoom: 19,
        }),
        'OSM': L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; <a href="https://openstreetmap.org">OpenStreetMap</a> contributors',
            maxZoom: 19,
        }),
        'ESRI Ocean': L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}', {
            attribution: '&copy; Esri, GEBCO, NOAA, National Geographic',
            maxNativeZoom: 13,  // Tiles only exist up to zoom 13
            maxZoom: 20,        // Allow overzoom (stretches tiles)
        }),
        'Satellite': L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
            attribution: '&copy; Esri',
            maxZoom: 19,
        }),
    };

    // Overlay layers (nautical marks)
    const overlayLayers = {
        'OpenSeaMap': L.tileLayer('https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png', {
            attribution: '&copy; <a href="https://www.openseamap.org">OpenSeaMap</a>',
            maxZoom: 18,
            opacity: 0.8,
        }),
        'SHOM Bathymetry (FR)': L.tileLayer.wms('https://services.data.shom.fr/INSPIRE/wms/r', {
            layers: 'LITTO3D_GUAD_2016_PYR_3857_WMSR,LITTO3D_MART_2016_PYR_3857_WMSR',
            format: 'image/png',
            transparent: true,
            attribution: '&copy; <a href="https://data.shom.fr">SHOM</a>',
            maxZoom: 18,
            opacity: 0.7,
        }),
    };

    // Default basemap is Light Blue (Carto dark_all + CSS invert filter).
    // The CSS-filter approach ISN'T rasterized by html2canvas, so when the
    // coach app screenshots the iframe the saved PNG shows the raw dark
    // tiles without the blue inversion. The coach iframe overrides via
    // `?basemap=ESRI Ocean` (natively-rendered light blue ocean tiles)
    // so the screenshot matches what the coach sees.
    const requestedBasemap = new URLSearchParams(location.search).get('basemap');
    const initialBasemap = (requestedBasemap && baseLayers[requestedBasemap])
        ? requestedBasemap : 'Light Blue';
    baseLayers[initialBasemap].addTo(map);

    // Add layer control
    L.control.layers(baseLayers, overlayLayers, {
        position: 'topright',
        collapsed: true,
    }).addTo(map);
}

function clearBoatLayers() {
    for (const deviceId of Object.keys(boatLayers)) {
        const L = boatLayers[deviceId];
        if (L.track) map.removeLayer(L.track);
        if (L.marker) map.removeLayer(L.marker);
        if (L.hull) map.removeLayer(L.hull);
        if (L.boom) map.removeLayer(L.boom);
        // Speed-colour segment pool — drop everything before the next
        // race wires up its own pool, otherwise stale boat-coloured
        // segments would linger on the map.
        if (L.segPool) {
            for (const s of L.segPool) {
                if (map.hasLayer(s)) map.removeLayer(s);
            }
        }
    }
    boatLayers = {};
    clearDistanceLines();
    if (laylineLayer) {
        map.removeLayer(laylineLayer);
        laylineLayer = null;
    }
    // Reset leader-leg pointer so prior race's state doesn't bleed into the
    // freshly loaded one (laylines redraw to course[0] on next leaderboard tick).
    activeLeg = 0;
    // Drop the cached layline TWD too — next syncLaylineWind() tick will
    // resample from the new race's wind data.
    lastLaylineTWD = null;
    if (windMarker) {
        map.removeLayer(windMarker);
        windMarker = null;
    }
    for (const sid of Object.keys(windMarkers)) {
        if (windMarkers[sid]) map.removeLayer(windMarkers[sid]);
    }
    windMarkers = {};
    windStationStats = {};
}

// Project a point (lat, lon, bearingDegrees, distMeters) to a destination
// lat/lon. Earth-radius approximation, flat-earth-friendly for ≤10 km legs.
function destinationPoint(lat, lon, bearingDeg, distM) {
    const R = 6371000;
    const φ1 = lat * Math.PI / 180;
    const λ1 = lon * Math.PI / 180;
    const θ = bearingDeg * Math.PI / 180;
    const dR = distM / R;
    const φ2 = Math.asin(Math.sin(φ1) * Math.cos(dR) +
                          Math.cos(φ1) * Math.sin(dR) * Math.cos(θ));
    const λ2 = λ1 + Math.atan2(Math.sin(θ) * Math.sin(dR) * Math.cos(φ1),
                                Math.cos(dR) - Math.sin(φ1) * Math.sin(φ2));
    return [φ2 * 180 / Math.PI, λ2 * 180 / Math.PI];
}

// Draw port + starboard upwind laylines from each upcoming windward mark.
// "Windward" is determined relative to the race-average TWD: if the mark
// is in the upper half-plane relative to the wind (mark bearing within
// ±90° of TWD as seen from the start), it counts. Length is scaled to be
// visible across a typical Boston Harbor course (~3 km).
// ============================================================
// AIS traffic overlay (non-participant vessels)
// ============================================================
// Loaded from GET /api/races/<id>/ais →
//   { vessels: [ { mmsi, name, category, type_raw, length_m, beam_m,
//                  positions: [ {t,lat,lon,sog,cog,hdg,nav} ] } ] }
// Renders muted/colour-coded chevrons that animate with the playback
// scrubber. Hazardous commercial traffic (cargo/tanker/tug) is flagged
// orange, ferries blue, pleasure/sail grey. They are obstacles only —
// never scored, never in the leaderboard.

const AIS_CAT_COLORS = {
    commercial: '#ff8c1a',  // cargo / tanker / tug / dredger — real hazards
    passenger:  '#3aa0ff',  // ferries / passenger
    pleasure:   '#9aa0a6',
    sailing:    '#9aa0a6',
    other:      '#9aa0a6',
};

function _aisColor(cat) { return AIS_CAT_COLORS[cat] || AIS_CAT_COLORS.other; }

// Marker size by vessel size — gross tonnage when known (so a 90k-GT
// container ship dwarfs a 975-GT ferry), else length, else a small
// default. sqrt/linear keeps the 122 .. 90,389 GT range from blowing up.
function aisSizePx(v) {
    let px;
    if (v && v.gross_tonnage) px = 8 + Math.sqrt(v.gross_tonnage) / 12;
    else if (v && v.length_m) px = 8 + v.length_m / 12;
    else px = 9;
    return Math.max(10, Math.min(34, Math.round(px)));
}

function aisIcon(category, cog, sizePx) {
    // Chevron pointing north (0°); rotated to COG via inline transform.
    const color = _aisColor(category);
    const s = sizePx || 15;
    const html = `<div class="ais-chevron" style="transform:rotate(${cog || 0}deg);color:${color}">`
        + `<svg viewBox="0 0 16 16" width="${s}" height="${s}"><path d="M8 1 L14 15 L8 11 L2 15 Z" `
        + `fill="currentColor" stroke="#0a0a0a" stroke-width="0.9"/></svg></div>`;
    return L.divIcon({ html, className: 'ais-divicon', iconSize: [s, s], iconAnchor: [s / 2, s / 2] });
}

function aisPopupHtml(v, p) {
    const dims = v.length_m ? `${Math.round(v.length_m)}×${v.beam_m ? Math.round(v.beam_m) : '?'} m` : '';
    const gt = v.gross_tonnage ? `${Math.round(v.gross_tonnage).toLocaleString()} GT` : '';
    const spd = (p && p.sog != null) ? `${(+p.sog).toFixed(1)} kn` : '';
    const crs = (p && p.cog != null) ? `${Math.round(p.cog)}°` : '';
    const typ = v.type_raw || v.category || '';
    const sizeLine = [dims, gt].filter(Boolean).join(' · ');
    return `<div class="ais-popup"><strong>${_attrEsc(v.name || '(unknown)')}</strong><br>`
        + `${_attrEsc(typ)}${sizeLine ? '<br>' + sizeLine : ''}<br>`
        + `MMSI ${_attrEsc(String(v.mmsi || '?'))}<br>${spd}${spd && crs ? ' · ' : ''}${crs}</div>`;
}

function clearAIS() {
    if (aisLayerGroup) {
        aisLayerGroup.clearLayers();
        if (map && map.hasLayer(aisLayerGroup)) map.removeLayer(aisLayerGroup);
    }
    aisLayerGroup = null;
    aisMarkers = {};
    aisData = null;
    if (aisControl) { try { map.removeControl(aisControl); } catch {} aisControl = null; }
}

async function loadRaceAIS(raceId) {
    clearAIS();  // always clear the previous race's vessels first
    try {
        const saved = localStorage.getItem('sf_ais_on');
        if (saved !== null) aisVisible = saved === '1';
    } catch {}
    let doc;
    try {
        const resp = await fetch(`${API_BASE}/api/races/${raceId}/ais`);
        doc = await resp.json();
    } catch (e) { console.warn('[AIS] fetch failed', e); return; }
    const vessels = (doc && doc.vessels) || [];
    if (!vessels.length) { console.log('[AIS] no vessels for this race'); return; }
    aisData = doc;
    aisLayerGroup = L.layerGroup();
    buildAISMarkers(vessels);
    if (aisVisible) aisLayerGroup.addTo(map);
    addAISToggleControl();
    updateAISPositions(playCursorSeconds || 0);
    console.log(`[AIS] ${vessels.length} vessels loaded (${doc.source || '?'})`);
}

function buildAISMarkers(vessels) {
    for (const v of vessels) {
        const pts = v.positions || [];
        if (!pts.length) continue;
        // One directional logo per vessel, sized by tonnage. No track
        // line and no interpolation — the marker only ever sits ON a real
        // AIS fix (see updateAISPositions), so every shown position is a
        // genuine reported point, not a guess.
        const sizePx = aisSizePx(v);
        const times = new Float64Array(pts.length);
        for (let i = 0; i < pts.length; i++) times[i] = new Date(pts[i].t).getTime();
        const marker = L.marker([pts[0].lat, pts[0].lon], {
            icon: aisIcon(v.category, pts[0].cog, sizePx),
            keyboard: false,
        });
        marker.bindPopup(aisPopupHtml(v, pts[0]));
        marker.addTo(aisLayerGroup);
        aisMarkers[v.mmsi] = { marker, vessel: v, times, positions: pts, sizePx };
    }
}

// Place every AIS vessel at the playback cursor by SNAPPING to its
// nearest real AIS fix in time — no interpolation. The marker therefore
// only ever sits on a genuine reported position. When the nearest fix is
// more than AIS_STALE_MS from the cursor (the vessel wasn't in the area
// near that moment) the marker is hidden. Anchored vessels (SOG < 0.5 kt)
// are dimmed. Writes ONLY marker state — never raceData / the leaderboard.
const AIS_STALE_MS = 25 * 60 * 1000;
function updateAISPositions(timeSeconds) {
    if (!aisLayerGroup || !currentRace) return;
    const startTime = new Date(currentRace.start_time).getTime();
    const targetTime = startTime + timeSeconds * 1000;
    for (const mmsi in aisMarkers) {
        const a = aisMarkers[mmsi];
        const times = a.times, pts = a.positions, n = times.length;
        if (!n) { a.marker.setOpacity(0); continue; }
        let bi = 0, bd = Infinity;
        for (let i = 0; i < n; i++) {
            const d = Math.abs(times[i] - targetTime);
            if (d < bd) { bd = d; bi = i; }
        }
        if (bd > AIS_STALE_MS) { a.marker.setOpacity(0); continue; }
        const p = pts[bi];
        if (!p || p.lat == null || p.lon == null) { a.marker.setOpacity(0); continue; }
        const anchored = (p.sog || 0) < 0.5;
        a.marker.setLatLng([p.lat, p.lon]);
        a.marker.setOpacity(anchored ? 0.45 : 0.95);
        a.current = p;
        const el = a.marker.getElement();
        if (el) {
            const c = el.querySelector('.ais-chevron');
            if (c) c.style.transform = `rotate(${p.cog || 0}deg)`;
        }
        if (a.marker.isPopupOpen && a.marker.isPopupOpen()) {
            a.marker.setPopupContent(aisPopupHtml(a.vessel, p));
        }
    }
}

function addAISToggleControl() {
    if (!map || aisControl) return;
    aisControl = L.control({ position: 'topright' });
    aisControl.onAdd = function () {
        const div = L.DomUtil.create('div', 'leaflet-bar map-toggle-control');
        div.innerHTML = `<a href="#" id="ais-toggle" class="${aisVisible ? 'active' : ''}" title="Show/hide AIS vessel traffic (non-racers)">⚓ AIS</a>`;
        L.DomEvent.disableClickPropagation(div);
        const a = div.querySelector('#ais-toggle');
        a.addEventListener('click', (e) => {
            e.preventDefault();
            aisVisible = !aisVisible;
            a.classList.toggle('active', aisVisible);
            try { localStorage.setItem('sf_ais_on', aisVisible ? '1' : '0'); } catch {}
            if (!aisLayerGroup) return;
            if (aisVisible) { aisLayerGroup.addTo(map); updateAISPositions(playCursorSeconds || 0); }
            else if (map.hasLayer(aisLayerGroup)) map.removeLayer(aisLayerGroup);
        });
        return div;
    };
    aisControl.addTo(map);
}

function addLaylinesMapControl() {
    if (!map) return;
    const ctl = L.control({ position: 'topright' });
    ctl.onAdd = function () {
        const div = L.DomUtil.create('div', 'leaflet-bar map-toggle-control');
        div.innerHTML = `<a href="#" id="layline-toggle" class="${laylinesVisible ? 'active' : ''}" title="Show/hide laylines">⌃ Laylines</a>`;
        L.DomEvent.disableClickPropagation(div);
        const a = div.querySelector('#layline-toggle');
        a.addEventListener('click', (e) => {
            e.preventDefault();
            laylinesVisible = !laylinesVisible;
            a.classList.toggle('active', laylinesVisible);
            // When turning on, snap immediately to the wind at the playback
            // cursor instead of waiting for the next updateBoatPositions tick.
            if (laylinesVisible && currentRace) {
                const targetMs = new Date(currentRace.start_time).getTime() + (playCursorSeconds * 1000);
                lastLaylineTWD = null;  // force re-sample
                syncLaylineWind(targetMs);
            }
            renderLaylines();
        });
        return div;
    };
    ctl.addTo(map);
}

// Auto-follow toggle. When on, the map flies to the current leader and
// the mark they're heading toward, re-framing as the fleet progresses
// through windward / leeward / second beat / finish. When off the user
// has full manual pan/zoom control. Default ON; persisted across races.
function addFollowLeaderMapControl() {
    if (!map) return;
    try {
        const saved = localStorage.getItem('sf-follow-leader');
        if (saved !== null) followLeader = saved === '1';
    } catch {}
    const ctl = L.control({ position: 'topright' });
    ctl.onAdd = function () {
        const div = L.DomUtil.create('div', 'leaflet-bar map-toggle-control');
        div.innerHTML = `<a href="#" id="follow-toggle" class="${followLeader ? 'active' : ''}" title="Click a boat, then auto-pan to keep it centred (pan only, never zooms)">⚐ Follow</a>`;
        L.DomEvent.disableClickPropagation(div);
        const a = div.querySelector('#follow-toggle');
        a.addEventListener('click', (e) => {
            e.preventDefault();
            followLeader = !followLeader;
            a.classList.toggle('active', followLeader);
            try { localStorage.setItem('sf-follow-leader', followLeader ? '1' : '0'); } catch {}
            if (followLeader && currentRace) {
                applyLeaderFollow(true);
            }
        });
        return div;
    };
    ctl.addTo(map);
}

// Camera follow during playback. SELECTED-BOAT ONLY, PAN-ONLY.
//   - No boat selected → the camera never moves on its own. Scrubbing
//     the timeline or playing back does NOT pan or zoom.
//   - A boat is selected (clicked → its drawer is open) → the camera
//     keeps that one boat centred as it moves, panning only. Zoom is
//     NEVER changed; the user's zoom level is preserved.
// `drawerDeviceId` is the boatLayers key of the selected boat (covers
// both fleet device tracks and GPX boat_id tracks; no-track boats open
// via openCatalogDrawer and have no layer to follow).
//
// Throttled to 2.5 s; pixel-space hysteresis suppresses micro-pans when
// the boat is already near centre. animate:false so the trail polyline
// never lags the marker during the pan.
function applyLeaderFollow(force = false) {
    if (!followLeader && !force) return;
    if (!map || !currentRace) return;

    // Only follow when a single boat is selected.
    const selKey = drawerDeviceId;
    if (!selKey) return;
    const layer = boatLayers[selKey];
    if (!layer || !layer.visible || !layer.current) return;

    const now = Date.now();
    if (!force && now - lastFollowPanMs < 2500) return;

    const target = L.latLng(layer.current.lat, layer.current.lon);

    if (!force) {
        const sz = map.getSize();
        const cur = map.latLngToLayerPoint(map.getCenter());
        const tgt = map.latLngToLayerPoint(target);
        const distPx = Math.hypot(tgt.x - cur.x, tgt.y - cur.y);
        if (distPx < 0.2 * Math.min(sz.x, sz.y)) return;
    }
    lastFollowPanMs = now;
    map.panTo(target, { animate: false });
}

// Trail window options shown in the SHOW > Trail dropdown. 30s exists
// for tight tactical replay, "All" restores the historical full-race
// behavior. Note that `Infinity` is encoded as the string "Infinity" in
// the option value because <select> coerces values to strings.
const TRAIL_WINDOW_OPTIONS = [
    { label: '30s', ms: 30_000 },
    { label: '1m',  ms: 60_000 },
    { label: '5m',  ms: 5 * 60_000 },
    { label: '10m', ms: 10 * 60_000 },
    { label: 'All', ms: Infinity },
];

// Top-right legend: per-marker overlay toggles. Each row drives whether
// a particular piece of info is rendered on the boat cursor; the Trail
// row also exposes a duration dropdown that combines what used to be a
// separate TRAIL Leaflet control. State lives in `markerOverlays` and
// `trailWindowMs`, both persisted to localStorage so the user's choices
// stick across races.
function addMarkerOverlaysMapControl() {
    if (!map) return;
    // Hydrate from prior session if present.
    try {
        // v2 of the storage key: new default is trail-only. Old key
        // (sf-marker-overlays) had everything on; ignoring it forces the
        // new default for users who tried the previous build.
        const saved = JSON.parse(localStorage.getItem('sf-marker-overlays-v2') || 'null');
        if (saved && typeof saved === 'object') {
            for (const k of Object.keys(markerOverlays)) {
                if (typeof saved[k] === 'boolean') markerOverlays[k] = saved[k];
            }
        }
        const gm = localStorage.getItem('sf-ghost-mode');
        if (gm === 'optimal' || gm === 'targetSpeed') ghostMode = gm;
        const savedWin = localStorage.getItem('sf-trail-window-ms');
        if (savedWin) {
            const n = (savedWin === 'Infinity') ? Infinity : Number(savedWin);
            if (Number.isFinite(n) || n === Infinity) trailWindowMs = n;
        }
    } catch {}

    const items = [
        { key: 'trail',           label: 'Trail' },
        { key: 'trailSpeedColor', label: '🌈 Speed colour' },
        { key: 'speed',           label: 'Speed' },
        { key: 'heel',            label: 'Heel' },
        { key: 'twa',             label: 'TWA' },
        { key: 'vmg',             label: 'VMG' },
        { key: 'polarPct',        label: '%pol' },
        { key: 'rank',            label: 'Rank' },
        { key: 'sail',            label: 'Sail #' },
        { key: 'distances',       label: '↔ Dist' },
        { key: 'hdop',            label: '±cm GPS' },
        { key: 'polarGhost',      label: '👻 Polar' },
    ];

    // Initial collapsed state: URL param `legend_compact=1` (set by the
    // coach iframe) wins over localStorage; otherwise hydrate from prior
    // session, default expanded.
    const urlCompact = new URLSearchParams(location.search).get('legend_compact') === '1';
    let collapsed = urlCompact;
    try {
        if (!urlCompact) collapsed = localStorage.getItem('sf-marker-legend-collapsed') === '1';
    } catch {}

    const ctl = L.control({ position: 'topright' });
    ctl.onAdd = function () {
        const div = L.DomUtil.create('div', 'leaflet-bar map-toggle-control marker-overlays-control');
        if (collapsed) div.classList.add('collapsed');
        div.title = 'Boat-cursor overlays — toggle each piece of info';
        const optionsHtml = TRAIL_WINDOW_OPTIONS.map(o => {
            const v = (o.ms === Infinity) ? 'Infinity' : String(o.ms);
            const sel = (o.ms === trailWindowMs) ? ' selected' : '';
            return `<option value="${v}"${sel}>${o.label}</option>`;
        }).join('');
        div.innerHTML =
            `<button class="legend-collapse-btn" type="button" title="Collapse / expand"
                     aria-label="Collapse legend">${collapsed ? '▸' : '▾'}</button>` +
            `<span class="trail-window-label">SHOW</span>` +
            `<div class="legend-body">` +
            items.map(it => {
                const cb = `<label><input type="checkbox" data-key="${it.key}" ${markerOverlays[it.key] ? 'checked' : ''}> ${it.label}`;
                if (it.key === 'trail') {
                    return `${cb}<select class="trail-window-select" data-trail-window
                                     title="How much past track to draw">${optionsHtml}</select></label>`;
                }
                if (it.key === 'polarGhost') {
                    return `${cb}<select class="ghost-mode-select" data-ghost-mode title="Ghost mode">`
                        + `<option value="targetSpeed"${ghostMode === 'targetSpeed' ? ' selected' : ''}>line</option>`
                        + `<option value="optimal"${ghostMode === 'optimal' ? ' selected' : ''}>optimal</option>`
                        + `</select></label>`;
                }
                return `${cb}</label>`;
            }).join('') +
            // Keyboard hint row — also reminds the user that arrow
            // keys scrub the timeline (and only the timeline; map
            // arrow-panning is intentionally disabled).
            `<div class="legend-kbd-hint">
                <kbd>←</kbd>/<kbd>→</kbd> ±1 s · <kbd>Shift</kbd>+<kbd>←</kbd>/<kbd>→</kbd> ±10 s · <kbd>Space</kbd> ▶ / ⏸
            </div>` +
            `</div>`;
        L.DomEvent.disableClickPropagation(div);
        L.DomEvent.disableScrollPropagation(div);

        const collapseBtn = div.querySelector('.legend-collapse-btn');
        if (collapseBtn) collapseBtn.addEventListener('click', () => {
            const isCollapsed = div.classList.toggle('collapsed');
            collapseBtn.textContent = isCollapsed ? '▸' : '▾';
            try { localStorage.setItem('sf-marker-legend-collapsed', isCollapsed ? '1' : '0'); } catch {}
        });

        div.querySelectorAll('input[type="checkbox"]').forEach(cb => {
            cb.addEventListener('change', () => {
                markerOverlays[cb.dataset.key] = cb.checked;
                try { localStorage.setItem('sf-marker-overlays-v2', JSON.stringify(markerOverlays)); } catch {}
                if (currentRace) updateBoatPositions(playCursorSeconds);
            });
        });
        const gsel = div.querySelector('.ghost-mode-select');
        if (gsel) {
            gsel.addEventListener('change', () => {
                ghostMode = gsel.value === 'optimal' ? 'optimal' : 'targetSpeed';
                try { localStorage.setItem('sf-ghost-mode', ghostMode); } catch {}
                if (currentRace) updateBoatPositions(playCursorSeconds);
            });
        }
        const sel = div.querySelector('.trail-window-select');
        if (sel) {
            sel.addEventListener('change', () => {
                const v = sel.value;
                trailWindowMs = (v === 'Infinity') ? Infinity : Number(v);
                try { localStorage.setItem('sf-trail-window-ms', v); } catch {}
                refreshAllTrails();
            });
        }
        return div;
    };
    ctl.addTo(map);
}

// Top-left wind widget on the map: a rotating arrow + TWD/TWS readout
// with the station dropdown directly below. Replaces the old leaderboard
// wind badge AND the toolbar wind picker — there's now one wind control
// for the whole dashboard, anchored to the map. The dropdown's content
// is rendered into #wind-source-picker by renderWindSourcePicker(); the
// rest of the picker code is unchanged.
function addMapWindPicker() {
    if (!map) return;
    const ctl = L.control({ position: 'topleft' });
    ctl.onAdd = function () {
        const div = L.DomUtil.create('div', 'leaflet-bar map-wind-picker');
        div.innerHTML = `
            <div class="map-wind-picker-row">
                <div class="map-wind-arrow" id="map-wind-arrow" title="True wind">
                    <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
                        <path d="M12 2 L18 14 L12 11 L6 14 Z"
                              fill="#22d3ee" stroke="#fff" stroke-width="1"/>
                    </svg>
                </div>
                <div class="map-wind-readout">
                    <span class="map-wind-readout-twd" id="map-wind-twd">---°</span>
                    <span class="map-wind-readout-tws" id="map-wind-tws">-- kn</span>
                </div>
            </div>
            <div class="wind-source-picker" id="wind-source-picker" style="display:none"></div>
        `;
        L.DomEvent.disableClickPropagation(div);
        L.DomEvent.disableScrollPropagation(div);
        return div;
    };
    ctl.addTo(map);
}

// Charts overlay open/close. Triggered by the toolbar Charts button,
// closed by the X / backdrop click / Escape. Adds .charts-open to body
// so the rest of the dashboard can react if needed.
function setupChartsOverlay() {
    const overlay = document.getElementById('charts-overlay');
    const trigger = document.getElementById('btn-charts');
    if (!overlay || !trigger) return;

    function open() {
        overlay.hidden = false;
        document.body.classList.add('charts-open');
        // Chart.js cached canvas dimensions when the canvas was hidden
        // (display:none → 0×0). Force a resize now that the overlay is
        // visible so curves fill the new viewport instead of staying
        // collapsed at the top.
        requestAnimationFrame(() => {
            if (speedChart) speedChart.resize();
            if (heelChart)  heelChart.resize();
            if (windChart)  windChart.resize();
        });
    }
    function close() {
        overlay.hidden = true;
        document.body.classList.remove('charts-open');
    }

    trigger.addEventListener('click', () => overlay.hidden ? open() : close());
    overlay.querySelectorAll('[data-close-charts]').forEach(el => {
        el.addEventListener('click', close);
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !overlay.hidden) close();
    });

    // Panel-fade slider — drags the alpha on the chart panel itself so
    // the map behind shows through. The backdrop stays fully transparent
    // (it's only there as a click-to-close target). At 0% the panel is
    // fully opaque (no map bleed); at 100% the panel is fully
    // transparent (map fully visible, charts ghost over it). Persisted
    // across page loads via localStorage.
    const slider = document.getElementById('charts-panel-fade');
    const valEl  = document.getElementById('charts-panel-fade-val');
    const panel  = overlay.querySelector('.charts-overlay-panel');
    function applyPanelFade(pct) {
        const alpha = Math.max(0, Math.min(1, 1 - pct / 100));
        if (panel) panel.style.background = `rgba(15, 23, 42, ${alpha.toFixed(2)})`;
        if (valEl) valEl.textContent = `${pct}%`;
        try { localStorage.setItem('sf-charts-panel-fade', String(pct)); } catch {}
    }
    if (slider) {
        const saved = parseInt(localStorage.getItem('sf-charts-panel-fade') || '30', 10);
        slider.value = String(saved);
        applyPanelFade(saved);
        slider.addEventListener('input', (e) => applyPanelFade(parseInt(e.target.value, 10)));
    }
}

// Trim each boat's polyline to the configured trail window around the
// current playback index. With trailWindowMs === Infinity the full track
// is restored.
function refreshAllTrails() {
    for (const layer of Object.values(boatLayers)) {
        applyTrailWindow(layer);
    }
}

// Hide every polyline in the layer's segment pool — used when we
// flip from speed-colour mode back to plain-trail mode, or when the
// trail toggle goes off.
function _hideAllSegments(layer) {
    if (!layer.segPool) return;
    for (const p of layer.segPool) {
        if (map.hasLayer(p)) map.removeLayer(p);
    }
}

// Per-segment speed-coloured rendering of a trail window. One small
// L.polyline per segment, drawn into a shared canvas renderer (much
// faster than SVG when there are thousands of small features).
// Polylines are reused across frames — we keep a pool per boat and
// only grow it on demand. Speed normalization is per-WINDOW (not the
// whole track) so the visible portion stretches the full slow→fast
// range; that's the change that makes the brightness variation
// actually pop on a 1-min trail with low absolute variance.
function _drawSpeedColoredTrail(layer, startIdx, endIdx) {
    const need = Math.max(0, endIdx - startIdx);  // segment count
    const renderer = _getSpeedSegRenderer();

    // Speed envelope of the visible window only.
    let minSpd = Infinity, maxSpd = -Infinity;
    for (let i = startIdx; i <= endIdx; i++) {
        const s = layer.data[i]?.speed_kn;
        if (s == null || !Number.isFinite(s)) continue;
        if (s < minSpd) minSpd = s;
        if (s > maxSpd) maxSpd = s;
    }
    const span = maxSpd - minSpd;

    // Grow pool on demand (creating a polyline is the slow part —
    // we only do it once and then reuse forever).
    while (layer.segPool.length < need) {
        layer.segPool.push(L.polyline([], {
            renderer,
            weight: 3,
            color: layer.color,
            opacity: 1,
            interactive: false,
            lineCap: 'round',
        }));
    }

    // Update active polylines.
    for (let i = 0; i < need; i++) {
        const k = startIdx + i;
        const a = layer.coords[k], b = layer.coords[k + 1];
        const segSpeed = (layer.data[k].speed_kn + layer.data[k + 1].speed_kn) / 2;
        const n = span > 0.05 ? (segSpeed - minSpd) / span : 1;
        const { stroke, opacity, widthBoost } = boatSpeedAttrs(layer.deviceId, n);
        const w = (1.5 + parseFloat(widthBoost)).toFixed(2);
        const seg = layer.segPool[i];
        seg.setLatLngs([a, b]);
        seg.setStyle({ color: stroke, opacity: parseFloat(opacity), weight: parseFloat(w) });
        if (!map.hasLayer(seg)) seg.addTo(map);
    }
    // Hide unused polylines (don't remove from pool — keep them
    // around for the next frame which will likely need them again).
    for (let i = need; i < layer.segPool.length; i++) {
        if (map.hasLayer(layer.segPool[i])) map.removeLayer(layer.segPool[i]);
    }
}

function applyTrailWindow(layer) {
    if (!layer || !layer.track || !layer.coords || !layer.times) return;

    // Boat hidden via toggleBoatVisibility — don't redraw anything.
    if (layer.visible === false) {
        _hideAllSegments(layer);
        return;
    }

    // SHOW > Trail off → wipe both the plain track and any speed
    // segments. Layers stay registered so toggleBoatVisibility
    // still works.
    if (!markerOverlays.trail) {
        layer.track.setLatLngs([]);
        _hideAllSegments(layer);
        return;
    }

    const n = layer.coords.length;
    if (n === 0) return;
    const endIdx = Math.min(n - 1, layer.currentIdx ?? (n - 1));

    // GPX-backup boats: clamp the trail to race start so the dock
    // departure / delivery sail doesn't show in the standard panel.
    // E1-native boats fall through unchanged.
    let raceStartFloor = -Infinity;
    if (layer.gpxOnly && currentRace?.start_time) {
        raceStartFloor = new Date(currentRace.start_time).getTime();
    }

    // Resolve the start of the visible window.
    let startIdx;
    if (!Number.isFinite(trailWindowMs)) {
        // "All" — full track, still floored at race start for GPX.
        startIdx = 0;
        if (raceStartFloor !== -Infinity) {
            while (startIdx < n && layer.times[startIdx] < raceStartFloor) startIdx++;
        }
    } else {
        const windowFloor = layer.times[endIdx] - trailWindowMs;
        const cutoff = Math.max(windowFloor, raceStartFloor);
        startIdx = endIdx;
        while (startIdx > 0 && layer.times[startIdx - 1] >= cutoff) startIdx--;
    }

    // Branch on speed-colour mode.
    if (markerOverlays.trailSpeedColor) {
        // Hide the plain-trail polyline; render coloured segments instead.
        if (layer.track.getLatLngs().length) layer.track.setLatLngs([]);
        _drawSpeedColoredTrail(layer, startIdx, endIdx);
    } else {
        // Plain mode: reset the polyline + tear down any segments
        // left over from the previous render.
        _hideAllSegments(layer);
        layer.track.setLatLngs(layer.coords.slice(startIdx, endIdx + 1));
    }
}

function renderLaylines() {
    if (laylineLayer) { map.removeLayer(laylineLayer); laylineLayer = null; }
    if (!laylinesVisible) return;
    // Prefer the live TWD set by syncLaylineWind() against playback time;
    // fall back to the race-average if no sample has been seen yet (e.g.
    // initial render before the first updateBoatPositions tick).
    const twd = lastLaylineTWD ?? raceAvgTWD;
    if (twd == null) return;

    // Collect every mark that should get laylines. Two sources:
    //   1. Marks explicitly typed `windward` (or `offset` — the
    //      small mark just downwind of the windward, still part of
    //      the upwind layline read). The user's mark-type assignment
    //      is authoritative; if they typed it windward, draw the cone.
    //   2. Marks reached by an upwind leg in the course sequence —
    //      catches windward marks the user typed as `custom`.
    // Deduped by mark_id so a mark hit by both heuristics renders once.
    const seen = new Set();
    const targets = [];   // [{ mark, label }]

    for (const m of (currentRace?.marks || [])) {
        if (!m || m.lat == null || m.lon == null) continue;
        if (m.mark_type === 'windward' || m.mark_type === 'offset') {
            seen.add(m.mark_id);
            targets.push({ mark: m, label: m.name || m.mark_type });
        }
    }

    // Course-walk fallback: any mark whose approach-leg bearing is a
    // beat (within ±90° of TWD) is a de-facto windward mark even if
    // mark_type isn't set. Same modulo-MAX_LAPS scan as the old code
    // so multi-lap W-L courses still get laylines on lap-N marks.
    const courseSeq = currentRace?.course || [];
    if (courseSeq.length) {
        const marksById = buildMarksById(currentRace);
        const startAnchor = startMidpoint(currentRace);
        const scanLimit = courseSeq.length * MAX_LAPS_TO_DETECT;
        for (let i = 0; i < scanLimit; i++) {
            const markId = courseSeq[i % courseSeq.length];
            const mark = marksById[markId];
            if (!mark || mark.lat == null || mark.lon == null) continue;
            if (seen.has(mark.mark_id)) continue;
            const prevSeqIdx = (i - 1 + courseSeq.length) % courseSeq.length;
            const prev = (i === 0)
                ? startAnchor
                : (marksById[courseSeq[prevSeqIdx]] || startAnchor);
            if (!prev) continue;
            const legBearing = bearingDegrees(prev.lat, prev.lon, mark.lat, mark.lon);
            const offset = ((legBearing - twd + 540) % 360) - 180;
            if (Math.abs(offset) <= 90) {
                seen.add(mark.mark_id);
                targets.push({ mark, label: mark.name || mark.mark_type || `Mark ${i + 1}` });
            }
        }
    }

    if (!targets.length) return;

    laylineLayer = L.featureGroup().addTo(map);
    const LAYLINE_M = 3000;
    const stbBearing = (twd + 180 - J80_UPWIND_TACK_ANGLE + 360) % 360;
    const portBearing = (twd + 180 + J80_UPWIND_TACK_ANGLE) % 360;
    const styleStb = { color: '#22d3ee', weight: 1.5, opacity: 0.55, dashArray: '6,6' };
    const stylePort = { color: '#ef4444', weight: 1.5, opacity: 0.55, dashArray: '6,6' };
    const twdLabel = `TWD ${twd.toFixed(0)}°`;

    for (const { mark, label } of targets) {
        const stbEnd = destinationPoint(mark.lat, mark.lon, stbBearing, LAYLINE_M);
        const portEnd = destinationPoint(mark.lat, mark.lon, portBearing, LAYLINE_M);
        L.polyline([[mark.lat, mark.lon], stbEnd], styleStb)
            .bindTooltip(`Starboard layline → ${label} · ${twdLabel}`, { sticky: true })
            .addTo(laylineLayer);
        L.polyline([[mark.lat, mark.lon], portEnd], stylePort)
            .bindTooltip(`Port layline → ${label} · ${twdLabel}`, { sticky: true })
            .addTo(laylineLayer);
    }
}

// Re-render laylines if the wind at the current playback time has shifted
// enough since the last render (≥1°). Cheap guard: avoids rebuilding the
// two polylines on every frame when interpolation jitter is sub-degree.
// Called from updateBoatPositions; also re-fires on layline toggle so the
// initial display reflects the playback cursor's wind, not race-average.
function syncLaylineWind(targetTimeMs) {
    if (!laylinesVisible) return;
    const sample = windAt(targetTimeMs);
    const twd = sample?.twd;
    if (twd == null) return;
    if (lastLaylineTWD != null) {
        const diff = Math.abs(((twd - lastLaylineTWD + 540) % 360) - 180);
        if (diff < 1) return;
    }
    lastLaylineTWD = twd;
    renderLaylines();
}

// Two- to four-letter team initials, e.g. "Mystic Mutiny" → "MM",
// "Rooster Alumni Club" → "RAC". Single-word team names get an explicit
// override (Seadogs → SD) — otherwise we fall back to the first two
// letters of the name.
const TEAM_INITIALS_OVERRIDES = {
    'Seadogs':  'SD',
    'Vela Veloce':  'VV',
    'Mystic Mutiny':  'MM',
    'Anchor Management':  'AM',
    'Rooster Alumni Club':  'RAC',
    'Always Lost':  'AL',
    "l'avalanche":  "L'A",
};
function teamInitials(name) {
    if (!name) return '';
    if (TEAM_INITIALS_OVERRIDES[name]) return TEAM_INITIALS_OVERRIDES[name];
    const words = name.trim().split(/\s+/).filter(Boolean);
    if (words.length >= 2) {
        return words.map(w => w[0].toUpperCase()).join('').slice(0, 4);
    }
    return name.slice(0, 2).toUpperCase();
}

// Build the divIcon HTML for a boat marker. Includes the directional
// arrow plus a small label (initials · speed · heel · TWA) — each piece
// can be hidden via the top-right SHOW legend (markerOverlays). The
// initials chip stays so boats are always identifiable; only the
// numeric stats can be turned off.
function createBoatIcon(color, rotation = 0, initials = '', stats = {}, sailNumber = '') {
    // The directional arrow used to live here. It's been replaced by
    // a real-scale hull polygon (`hullPolygonLatLngs`) drawn directly
    // on the map per boat — sized to the actual LOA, oriented along
    // COG, with a mainsail boom showing tack. The marker now renders
    // only the label (team initials, sail #, optional stat line) so
    // each boat stays identifiable at every zoom level.
    const ovr = markerOverlays || {};
    const parts = [];
    // Rank (#1..#6) — current leaderboard position.
    if (ovr.rank && stats.rank != null) parts.push(`#${stats.rank}`);
    // Speed in knots.
    if (ovr.speed && Number.isFinite(stats.speedKn)) parts.push(`${stats.speedKn.toFixed(1)}kn`);
    // Heel: signed (port = negative). P/S matches TWA convention.
    if (ovr.heel && Number.isFinite(stats.heelDeg)) {
        const s = stats.heelDeg >= 0 ? 'S' : 'P';
        parts.push(`Heel ${s} ${Math.round(Math.abs(stats.heelDeg))}°`);
    }
    // TWA: signed true wind angle. Per-boat (TWD - COG).
    if (ovr.twa && Number.isFinite(stats.twaSigned)) {
        const s = stats.twaSigned <= 0 ? 'P' : 'S';
        parts.push(`TWA ${s} ${Math.round(Math.abs(stats.twaSigned))}°`);
    }
    // VMG to next mark (kn). Signed: + = closing, - = losing ground.
    if (ovr.vmg && Number.isFinite(stats.vmg)) {
        const sign = stats.vmg >= 0 ? '+' : '';
        parts.push(`VMG ${sign}${stats.vmg.toFixed(1)}`);
    }
    // %polar — performance vs J/80 polar target.
    if (ovr.polarPct && Number.isFinite(stats.polarPct)) {
        parts.push(`${Math.round(stats.polarPct)}%pol`);
    }
    // GNSS accuracy in centimetres, derived from per-sample HDOP.
    // Multiplied by ~1 m UERE (typical for the LG290P running
    // standard L1/L5 without RTK). PPK-processed sessions land in
    // the 1–30 cm range; raw NMEA HDOP typically reads 50–150 cm.
    if (ovr.hdop && Number.isFinite(stats.hdop)) {
        parts.push(`±${Math.round(stats.hdop * 100)}cm`);
    }
    const statsHtml = parts.length ? `<span class="bml-stats">${parts.join(' ')}</span>` : '';
    // Sail number rides next to the team-initials chip in the same
    // dark pill — slightly de-emphasized (lighter weight, dimmer
    // colour) so the initials still scan first.
    const showSail = ovr.sail && sailNumber;
    const sailHtml = showSail ? `<span class="bml-sail">${sailNumber}</span>` : '';
    const label = (initials || showSail || parts.length)
        ? `<span class="boat-marker-label"><span class="bml-init" style="color:${color}">${initials}</span>${sailHtml}${statsHtml ? ' ' + statsHtml : ''}</span>`
        : '';
    return L.divIcon({
        html: `<div class="boat-marker-wrap">${label}</div>`,
        className: 'boat-marker',
        iconSize: null,
        iconAnchor: [0, 0],
    });
}

function haversineMeters(lat1, lon1, lat2, lon2) {
    const R = 6371000;
    const toRad = (d) => d * Math.PI / 180;
    const dLat = toRad(lat2 - lat1);
    const dLon = toRad(lon2 - lon1);
    const a = Math.sin(dLat / 2) ** 2 +
              Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(a));
}

// Bearing from A to B, degrees clockwise from north (0..360).
function bearingDegrees(lat1, lon1, lat2, lon2) {
    const f1 = lat1 * Math.PI / 180, f2 = lat2 * Math.PI / 180;
    const dLam = (lon2 - lon1) * Math.PI / 180;
    const y = Math.sin(dLam) * Math.cos(f2);
    const x = Math.cos(f1) * Math.sin(f2) - Math.sin(f1) * Math.cos(f2) * Math.cos(dLam);
    return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}

// --- Course-aware progress metrics (Tier 2) ---
const MARK_ROUNDING_RADIUS_M = 35;  // meters, generous for J/80 mark roundings

function buildMarksById(race) {
    const out = {};
    for (const m of (race?.marks || [])) {
        if (m.mark_id) out[m.mark_id] = m;
    }
    return out;
}

function startMidpoint(race) {
    const sl = race?.start_line;
    if (sl && sl.pin_lat != null && sl.boat_lat != null) {
        return {
            lat: (sl.pin_lat + sl.boat_lat) / 2,
            lon: (sl.pin_lon + sl.boat_lon) / 2,
        };
    }
    return null;
}

// How many laps to scan for. Most J/80 club races are 1–2 laps; 4 is a
// safe ceiling that covers everything we've ever sailed. Over-scanning
// is harmless — if the boat never re-enters the next slot's radius the
// algorithm just stops.
const MAX_LAPS_TO_DETECT = 4;

// Walk each boat's track once and record the time it passed within
// MARK_ROUNDING_RADIUS_M of each mark in the course sequence — repeated
// up to MAX_LAPS_TO_DETECT times so that races defined as a single lap
// of [W, L] (the common J/80 pattern) automatically pick up the second
// lap's roundings without anyone having to re-enter the marks. After
// the last detected rounding, if a finish_line is defined and the boat
// crosses it, that crossing is appended as the race-end timestamp.
//
// Result stored on layer.roundingTimes — a flat array of epoch ms, one
// entry per detected event. Length up to courseSeq.length * MAX_LAPS + 1
// (the +1 = finish-line crossing).
function precomputeRoundingsForLayer(layer, courseSeq, marksById, finishLine) {
    if (!courseSeq?.length || !layer?.data?.length) {
        layer.roundingTimes = null;
        return;
    }
    const times = [];
    const maxEvents = courseSeq.length * MAX_LAPS_TO_DETECT;
    let lastSampleConsumed = 0;
    for (let i = 0; i < layer.data.length && times.length < maxEvents; i++) {
        const p = layer.data[i];
        if (!p.lat || !p.lon) continue;
        const seqIdx = times.length % courseSeq.length;
        const m = marksById[courseSeq[seqIdx]];
        if (!m) break;  // dangling mark id — give up rather than mis-credit
        const d = haversineMeters(p.lat, p.lon, m.lat, m.lon);
        if (d < MARK_ROUNDING_RADIUS_M) {
            times.push(new Date(p.t).getTime());
            lastSampleConsumed = i;
        }
    }

    // Finish-line crossing: walk forward from the last detected rounding
    // and look for the first GPS segment that intersects the line. If no
    // finish line is defined or the boat never crosses, leave the array
    // as-is (last rounding becomes the de-facto finish).
    if (finishLine && finishLine.pin_lat != null && finishLine.boat_lat != null) {
        const startIdx = times.length ? lastSampleConsumed + 1 : 1;
        let prevP = layer.data[startIdx - 1] || null;
        for (let i = startIdx; i < layer.data.length; i++) {
            const p = layer.data[i];
            if (!p.lat || !p.lon) { prevP = p; continue; }
            if (prevP && prevP.lat && prevP.lon) {
                if (segmentsIntersect(
                    prevP.lat, prevP.lon, p.lat, p.lon,
                    finishLine.pin_lat, finishLine.pin_lon,
                    finishLine.boat_lat, finishLine.boat_lon
                )) {
                    times.push(new Date(p.t).getTime());
                    break;
                }
            }
            prevP = p;
        }
    }

    layer.roundingTimes = times;
}

// 2-D segment intersection. Lat/lon at Boston-Harbor scale is close
// enough to a Cartesian plane (sub-metre error over a few km) for a
// boolean "did the boat's last GPS segment cross the finish line?"
// check. Returns false for collinear or non-overlapping segments.
function segmentsIntersect(ax, ay, bx, by, cx, cy, dx, dy) {
    const d1 = (dx - cx) * (ay - cy) - (dy - cy) * (ax - cx);
    const d2 = (dx - cx) * (by - cy) - (dy - cy) * (bx - cx);
    const d3 = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
    const d4 = (bx - ax) * (dy - ay) - (by - ay) * (dx - ax);
    return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) &&
           ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0));
}

// --- Weather-station wind (Tier 2 wind / TWD / laylines) ---

async function loadRaceWindData(startTime, endTime) {
    weatherWindSamples = [];
    weatherWindSource = null;
    raceAvgTWD = null;
    raceBuoyData = {};
    selectedWindStationId = null;
    // Per-race admin override is per-race-id. Reset on every wind
    // reload so an override set on the previous race doesn't leak
    // forward and make a different race look like it inherited the
    // same default for the whole day.
    windDefaultOverride = null;
    if (!startTime || !endTime) return;
    try {
        const startTs = new Date(startTime).getTime() / 1000;
        const endTs = new Date(endTime).getTime() / 1000;
        const resp = await fetch(`${API_BASE}/api/buoys/data?start_ts=${startTs}&end_ts=${endTs}`);
        if (!resp.ok) {
            console.warn('[Wind] buoys/data HTTP', resp.status);
            return;
        }
        const data = await resp.json();
        raceBuoyData = data.buoys || {};

        // Inject a synthetic FLEET station inferred from the boats'
        // own COG + heel (each close-hauled boat gives a TWD estimate
        // of COG ± tackAngle; heel sign picks the side). Plugs in to
        // raceBuoyData so the picker, the override system, and
        // rebuildWindFromSelected all treat it like any other source.
        _buildFleetWindStation();

        // Auto-pick the first station with usable samples. Try the NDBC
        // primaries (Castle Island, Logan, 16NM) first; if none of them
        // has data for this race window, fall back to any Synoptic
        // station (SYN_*) that does.
        const usableId = (sid) =>
            raceBuoyData[sid]?.data_points?.some(d =>
                d.wind_dir != null && d.wind_speed_kts != null
            );

        // Admin override: a coach can pin a specific wind station as the
        // default for a race via the wind picker's "Set as default"
        // button. The coach Lambda exposes it at
        // GET /race-wind-default/{race_id} (public). If present AND that
        // station has usable samples, honor it before the auto-pick.
        if (currentRace && currentRace.race_id) {
            try {
                const ov = await _fetchWindDefaultOverride(currentRace.race_id);
                if (ov && ov.station_id) {
                    // Remember the override even when its station has
                    // no usable samples for this window — the picker
                    // should still surface the "📌 Default" badge so
                    // the coach knows the intent is persisted. Only
                    // the *active* station selection gates on data.
                    windDefaultOverride = ov;
                    if (usableId(ov.station_id)) {
                        selectedWindStationId = ov.station_id;
                    }
                }
            } catch {}
        }

        if (!selectedWindStationId) {
            for (const sid of PRIMARY_WIND_STATIONS) {
                if (usableId(sid)) { selectedWindStationId = sid; break; }
            }
        }
        if (!selectedWindStationId) {
            for (const sid of Object.keys(raceBuoyData)) {
                if (usableId(sid)) { selectedWindStationId = sid; break; }
            }
        }
        rebuildWindFromSelected();
        renderWindSourcePicker();
    } catch (e) {
        console.error('[Wind] load failed', e);
    }
}

// ---- Admin wind-station override helpers ------------------------------
// The coach app stores per-race wind-station defaults in S3 via the coach
// Lambda. These helpers read/write that store. Read is public (any
// race-page visitor); write requires a valid coach Google ID token from
// localStorage (`sf-coach-id-token` — same key the coach app uses).

const _COACH_API_URL = (window.SAILFRAMES_COACH_API || '').replace(/\/+$/, '');

function _coachToken() {
    try { return localStorage.getItem('sf-coach-id-token') || ''; } catch { return ''; }
}
function _isCoachLoggedIn() { return !!_coachToken(); }

// Decode the `exp` claim from a Google ID token. JWTs are base64url
// JSON; we only need the payload (middle segment) to read exp.
function _coachTokenExpMs() {
    const t = _coachToken();
    if (!t) return 0;
    try {
        const payload = t.split('.')[1];
        const json = atob(payload.replace(/-/g, '+').replace(/_/g, '/'));
        const claims = JSON.parse(decodeURIComponent(escape(json)));
        return claims && claims.exp ? claims.exp * 1000 : 0;
    } catch { return 0; }
}

// True when a token exists AND has >30 s of life left. The 30 s grace
// matches the coach app so the two stay in sync.
function _coachTokenIsValid() {
    const exp = _coachTokenExpMs();
    return !!exp && exp > Date.now() + 30_000;
}

// Sentinel marker on errors so call sites can distinguish "expired"
// from a generic API failure and offer a re-sign-in prompt.
const COACH_SESSION_EXPIRED = 'COACH_SESSION_EXPIRED';

function _makeSessionExpiredError() {
    const err = new Error('Coach session expired. Sign in again to save race defaults.');
    err.code = COACH_SESSION_EXPIRED;
    return err;
}

// Bounce to the coach login page with a `next` param so the browser
// returns to the current race page after sign-in.
function _redirectToCoachLogin() {
    const here = window.location.pathname + window.location.search + window.location.hash;
    const url = `/coach/login.html?next=${encodeURIComponent(here)}`;
    window.location.href = url;
}

// Centralized handler for the wind-default action errors. Shows a
// re-sign-in confirm() on expiry, plain alert() otherwise. Returns
// true if it handled the error (caller may skip further reporting).
function _handleCoachActionError(err) {
    if (err && err.code === COACH_SESSION_EXPIRED) {
        const go = confirm('Your coach sign-in has expired. Sign in again now?');
        if (go) _redirectToCoachLogin();
        return true;
    }
    return false;
}

async function _fetchWindDefaultOverride(raceId) {
    if (!_COACH_API_URL) return null;
    try {
        const r = await fetch(`${_COACH_API_URL}/race-wind-default/${encodeURIComponent(raceId)}`);
        if (!r.ok) return null;
        return await r.json();
    } catch { return null; }
}

async function setRaceWindDefault(raceId, stationId) {
    if (!_COACH_API_URL) throw new Error('Coach API not configured');
    if (!_coachTokenIsValid()) throw _makeSessionExpiredError();
    const r = await fetch(`${_COACH_API_URL}/race-wind-default/${encodeURIComponent(raceId)}`, {
        method: 'PUT',
        headers: { 'Authorization': 'Bearer ' + _coachToken(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ station_id: stationId }),
    });
    if (r.status === 401) throw _makeSessionExpiredError();
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || data.error || `HTTP ${r.status}`);
    return data;
}

async function clearRaceWindDefault(raceId) {
    if (!_COACH_API_URL) throw new Error('Coach API not configured');
    if (!_coachTokenIsValid()) throw _makeSessionExpiredError();
    const r = await fetch(`${_COACH_API_URL}/race-wind-default/${encodeURIComponent(raceId)}`, {
        method: 'DELETE',
        headers: { 'Authorization': 'Bearer ' + _coachToken() },
    });
    if (r.status === 401) throw _makeSessionExpiredError();
    if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        throw new Error(data.detail || data.error || `HTTP ${r.status}`);
    }
    return true;
}

// Rebuild weatherWindSamples/raceAvgTWD/weatherWindSource from the
// currently selected station. Cheap; safe to call whenever the user
// flips between Castle Island and Logan.
function rebuildWindFromSelected() {
    weatherWindSamples = [];
    weatherWindSource = null;
    raceAvgTWD = null;
    if (!selectedWindStationId) return;
    const buoy = raceBuoyData[selectedWindStationId];
    if (!buoy?.data_points?.length) return;
    const samples = [];
    for (const dp of buoy.data_points) {
        if (dp.wind_dir == null || dp.wind_speed_kts == null) continue;
        const tMs = (dp.timestamp ? new Date(dp.timestamp).getTime() : (dp.ts * 1000));
        if (!Number.isFinite(tMs)) continue;
        samples.push({ tMs, twd: dp.wind_dir, tws: dp.wind_speed_kts });
    }
    if (!samples.length) return;
    samples.sort((a, b) => a.tMs - b.tMs);
    weatherWindSamples = samples;
    weatherWindSource = buoy.name || selectedWindStationId;
    let sx = 0, sy = 0;
    for (const s of samples) {
        sx += Math.sin(s.twd * Math.PI / 180);
        sy += Math.cos(s.twd * Math.PI / 180);
    }
    raceAvgTWD = (Math.atan2(sx, sy) * 180 / Math.PI + 360) % 360;
    console.log(`[Wind] using ${weatherWindSource}, ${samples.length} samples, avg TWD ${raceAvgTWD.toFixed(0)}°`);
}

// Wind-source segmented picker (Castle Is / Logan / 44013) next to badge.
function shortStationLabel(stationId, fullName) {
    const overrides = {
        'CSIM3': 'Castle Is',
        'KBOS':  'Logan',
        '44013': '16NM',
        '44029': 'Mass Bay',
        'FLEET': 'Fleet',
    };
    if (overrides[stationId]) return overrides[stationId];
    if (!fullName) return stationId;
    let n = fullName
        .replace(/\b(Sailing Center|Sailing|Airport|Buoy|Station)\b/gi, '')
        .replace(/\s+/g, ' ').trim();
    return n.length > 14 ? n.slice(0, 13) + '…' : n;
}

// ---- Fleet-inferred wind direction -----------------------------------
// When the user can't trust the nearest land station (Logan is across
// the bay, the 16NM ocean buoy is, well, 16 NM offshore), the boats
// themselves are the best in-situ TWD sensor: on a beat they sail at
// a known angle to the wind, so the COG distribution of close-hauled
// boats is bimodal around TWD ± tackAngle and the bisector is the
// wind direction. Heel sign (port/starboard heel) disambiguates which
// side of the bisector each boat is on so we don't need to know the
// answer to compute it.

const _FLEET_TACK_ANGLE_DEG = 42;     // J/80 close-hauled (used for all
                                       // classes — within ±3° for all
                                       // four built-in classes, error
                                       // averages out across boats)

// Estimate TWD from the fleet at one instant. Returns {twd, tws, n}
// or null when no boat in the fleet is currently on the upwind leg.
//
// Two-gate algorithm — both gates must pass per boat:
//
//   (a) Course-aware leg gate: the boat's current target mark must be
//       the one labelled mark_type='windward'. Excludes reaches, runs,
//       gate roundings, and any custom-marked legs. Established last
//       revision.
//
//   (b) Geometry-only tack gate: compute the signed angle from the
//       boat's COG to its bearing-to-windward-mark. The magnitude
//       must fall in the close-hauled band (25°–65°, i.e. ~tackAngle
//       ± 20° tolerance for layline approach and pinch). The SIGN
//       of that angle uniquely identifies tack:
//         diff > 0 → mark is to starboard of the boat's heading
//                  → boat is on STARBOARD tack
//                  → TWD = COG + tackAngle
//         diff < 0 → mark is to port
//                  → boat is on PORT tack
//                  → TWD = COG − tackAngle
//
// Critical improvement over the previous revision: tack identification
// is GPS-only (COG + bearing-to-mark). The IMU heel signal isn't used
// at all. Heel was the load-bearing input before, and its sign
// convention was sometimes inconsistent, which is what made the
// inference drift wildly after a few minutes.
function _inferTWDFromFleetAt(timeMs, tackAngleDeg = _FLEET_TACK_ANGLE_DEG) {
    if (!timeMs || !Number.isFinite(timeMs)) return null;
    if (!currentRace) return null;
    const courseSeq = currentRace.course || [];
    if (!courseSeq.length) return null;
    const marksById = (typeof buildMarksById === 'function') ? buildMarksById(currentRace) : {};
    const totalLegs = currentRace._totalLegs ?? courseSeq.length;

    const ests = [];
    let sumSog = 0;
    for (const layer of Object.values(boatLayers || {})) {
        if (!layer?.data?.length) continue;

        // (a) Course-aware leg gate.
        const legsDone = (typeof legsCompletedAt === 'function')
            ? legsCompletedAt(layer, timeMs) : 0;
        if (legsDone >= totalLegs) continue;
        const targetMarkId = courseSeq[legsDone % courseSeq.length];
        const targetMark = marksById[targetMarkId];
        if (!targetMark || targetMark.lat == null || targetMark.lon == null) continue;
        if (targetMark.mark_type !== 'windward') continue;

        const idx = (typeof gpsIdxAt === 'function') ? gpsIdxAt(layer, timeMs) : null;
        if (idx == null) continue;
        const p = layer.data[idx];
        if (!p || p.lat == null || p.lon == null) continue;
        const sog = p.speed_kn;
        if (!Number.isFinite(sog) || sog < 2 || sog > 9) continue;
        const cog = p.course;
        if (!Number.isFinite(cog)) continue;

        // (b) Geometry-only tack gate. Signed angle (BTM − COG) mapped
        // to [-180, 180]. Positive = mark on starboard, negative = mark
        // on port. Magnitude must look like a close-hauled angle.
        const btm = bearingDegrees(p.lat, p.lon, targetMark.lat, targetMark.lon);
        const diff = ((btm - cog + 540) % 360) - 180;
        const absDiff = Math.abs(diff);
        if (absDiff < 25 || absDiff > 65) continue;

        const tackSign = diff > 0 ? +1 : -1;
        const twdEst = (cog + tackSign * tackAngleDeg + 720) % 360;
        ests.push({ twd: twdEst, sog });
        sumSog += sog;
    }
    if (ests.length < 1) return null;

    // Reject high-disagreement slices: if the per-boat TWD estimates
    // span more than 35° std-dev across ≥ 3 boats, something's off
    // (boats at different points of sail, mark misidentified, etc.).
    // Better to leave a gap in the time series than emit a meaningless
    // average that drifts the displayed TWD.
    // Circular mean.
    let sx = 0, sy = 0;
    for (const e of ests) {
        const r = e.twd * Math.PI / 180;
        sx += Math.sin(r);
        sy += Math.cos(r);
    }
    const twd = (Math.atan2(sx, sy) * 180 / Math.PI + 360) % 360;

    // Disagreement check: circular std-dev of the per-boat estimates.
    // High std with ≥ 3 contributors means the boats themselves
    // disagree, so the circular mean is meaningless — drop the slice.
    if (ests.length >= 3) {
        const meanRad = twd * Math.PI / 180;
        let sq = 0;
        for (const e of ests) {
            const r = e.twd * Math.PI / 180;
            const d = ((r - meanRad + 3 * Math.PI) % (2 * Math.PI)) - Math.PI;
            sq += d * d;
        }
        const stdDeg = Math.sqrt(sq / ests.length) * 180 / Math.PI;
        if (stdDeg > 35) return null;
    }

    // Crude TWS estimate from mean close-hauled SOG. J/80 polar at TWA
    // 42°: SOG ≈ 4 kt @ TWS 8, 5.5 kt @ 12, 6.3 kt @ 16. Linearised:
    // TWS ≈ 1.6 × SOG. Good to ±2 kt — fine for ranking-style use.
    const meanSog = sumSog / ests.length;
    const tws = meanSog * 1.6;
    return { twd, tws, n: ests.length };
}

// Mean lat/lon of the fleet at race start — used to anchor the FLEET
// pseudo-station's map marker somewhere sensible (centroid of the
// boats), and to let recomputeVisibleStations() keep it in the picker
// (which filters out stations with null lat/lon).
function _fleetCentroidAt(timeMs) {
    let lat = 0, lon = 0, n = 0;
    for (const layer of Object.values(boatLayers || {})) {
        if (!layer?.data?.length) continue;
        const idx = (typeof gpsIdxAt === 'function') ? gpsIdxAt(layer, timeMs) : null;
        if (idx == null) continue;
        const p = layer.data[idx];
        if (!p || p.lat == null || p.lon == null) continue;
        lat += p.lat; lon += p.lon; n++;
    }
    if (!n) return null;
    return { lat: lat / n, lon: lon / n };
}

// Sample fleet TWD every 30 s across the race window and register the
// result as a synthetic 'FLEET' entry in raceBuoyData. The rest of the
// wind pipeline (picker, override, rebuildWindFromSelected, layline
// sync, briefing) treats it like any other station — no other code
// path needs to be FLEET-aware.
function _buildFleetWindStation() {
    if (!currentRace?.start_time || !currentRace?.end_time) return;
    const start = new Date(currentRace.start_time).getTime();
    const end   = new Date(currentRace.end_time).getTime();
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return;

    const STEP_MS = 30_000;
    const data_points = [];
    for (let t = start; t <= end; t += STEP_MS) {
        const w = _inferTWDFromFleetAt(t);
        if (!w) continue;
        data_points.push({
            timestamp: new Date(t).toISOString(),
            unix_ts: Math.floor(t / 1000),
            wind_dir: w.twd,
            wind_speed_kts: Math.round(w.tws * 10) / 10,
            _n_boats: w.n,
        });
    }
    if (!data_points.length) {
        // Not enough close-hauled samples in any time slice — don't
        // register the station at all rather than show an empty entry.
        return;
    }

    const centroid = _fleetCentroidAt(start) || _fleetCentroidAt((start + end) / 2);
    raceBuoyData['FLEET'] = {
        station_id: 'FLEET',
        name: 'Fleet inferred',
        lat: centroid?.lat ?? 42.36,
        lon: centroid?.lon ?? -71.05,
        type: 'fleet',
        source: 'fleet',
        color: '#22d3ee',
        data: ['wind'],
        data_points,
        has_data: true,
    };
}

// Compute mean/std/min/max wind direction and speed across the race
// window for one station. TWD uses vector mean (handles 0/360 wrap).
function computeWindStats(buoy, raceStartMs, raceEndMs) {
    if (!buoy?.data_points?.length) return null;
    // ±30 min buffer so short race windows still show stats.
    const startMs = raceStartMs - 30 * 60 * 1000;
    const endMs = raceEndMs + 30 * 60 * 1000;
    const inWindow = [];
    for (const d of buoy.data_points) {
        if (d.wind_dir == null || d.wind_speed_kts == null) continue;
        const t = d.timestamp ? new Date(d.timestamp).getTime() : (d.unix_ts * 1000);
        if (!Number.isFinite(t) || t < startMs || t > endMs) continue;
        inWindow.push(d);
    }
    if (!inWindow.length) return null;
    let sx = 0, sy = 0;
    let minTWS = Infinity, maxTWS = -Infinity, sumTWS = 0;
    for (const d of inWindow) {
        sx += Math.sin(d.wind_dir * Math.PI / 180);
        sy += Math.cos(d.wind_dir * Math.PI / 180);
        const s = d.wind_speed_kts;
        if (s < minTWS) minTWS = s;
        if (s > maxTWS) maxTWS = s;
        sumTWS += s;
    }
    const meanTWD = (Math.atan2(sx, sy) * 180 / Math.PI + 360) % 360;
    const r = Math.sqrt(sx * sx + sy * sy) / inWindow.length;
    // Circular std (Mardia/Jupp formula). Clamp r to avoid log(0).
    const stdTWD = Math.sqrt(-2 * Math.log(Math.max(r, 1e-6))) * 180 / Math.PI;
    return {
        sampleCount: inWindow.length,
        meanTWD, stdTWD,
        minTWS, maxTWS,
        avgTWS: sumTWS / inWindow.length,
    };
}

function computeAllWindStats() {
    windStationStats = {};
    if (!currentRace) return;
    const raceStartMs = new Date(currentRace.start_time).getTime();
    const raceEndMs   = new Date(currentRace.end_time).getTime();
    for (const [sid, buoy] of Object.entries(raceBuoyData)) {
        const s = computeWindStats(buoy, raceStartMs, raceEndMs);
        if (s) windStationStats[sid] = s;
    }
}

function fmtStationStats(stats) {
    const tws = stats.minTWS.toFixed(0) === stats.maxTWS.toFixed(0)
        ? `${stats.minTWS.toFixed(0)} kn`
        : `${stats.minTWS.toFixed(0)}–${stats.maxTWS.toFixed(0)} kn`;
    return `${Math.round(stats.meanTWD).toString().padStart(3, '0')}°±${Math.round(stats.stdTWD)}°  ${tws}`;
}

// Compact "source" label for the dropdown column. NDBC/METAR/NWS render as-is;
// Synoptic stations get the underlying mesonet network when available so the
// user can tell a Tempest from a CWOP from a Logan-via-Synoptic mirror.
function fmtSourceLabel(buoy) {
    const src = (buoy.source || 'ndbc').toLowerCase();
    if (src === 'fleet') return 'Fleet';
    if (src === 'ndbc')  return 'NDBC';
    if (src === 'metar') return 'METAR';
    if (src === 'nws')   return 'NWS';
    if (src === 'synoptic') {
        const net = (buoy.network || '').toUpperCase();
        // Map Synoptic's network short names to a 1-token tag suitable
        // for the dropdown column. Order matters — earliest match wins.
        if (net.includes('TEMPEST') || net.includes('WXFLOW')) return 'Syn·Tempest';
        if (net.includes('CWOP') || net.includes('APRS'))      return 'Syn·CWOP';
        if (net.includes('ASOS') || net.includes('AWOS') ||
            net.includes('METAR') || net.includes('NWS/FAA'))  return 'Syn·METAR';
        if (net.includes('RAWS'))                              return 'Syn·RAWS';
        if (net.includes('WEATHERSTEM'))                       return 'Syn·WxStem';
        if (net.includes('DAVIS') || net.includes('WLINK'))    return 'Syn·Davis';
        if (net === 'NTC' || net.includes('TIDES'))            return 'Syn·NOAA·NTC';
        if (net.includes('BUOY') || net.includes('NDBC') ||
            net.includes('CMAN'))                              return 'Syn·NDBC';
        if (net.includes('SCHOOL'))                            return 'Syn·SchoolNet';
        return net ? `Syn·${net.length > 10 ? net.slice(0, 9) + '…' : net}` : 'Synoptic';
    }
    return src;
}

// Dedupe stations that point at the same physical sensor reachable via
// multiple aggregators (e.g. KBOS direct + SYN_KBOS via Synoptic, or any
// Synoptic mirror of an NDBC buoy). Group by lat/lon proximity (≤200 m)
// and keep the highest-priority source per group:
//   ndbc > metar > nws > synoptic
// Hidden duplicates stay in raceBuoyData (data preserved) but disappear
// from the picker and the map markers so the screen isn't double-marked.
function recomputeVisibleStations() {
    const SOURCE_PRIORITY = { ndbc: 0, metar: 1, nws: 2, synoptic: 3 };
    const stations = Object.entries(raceBuoyData)
        .filter(([sid, _b]) => windStationStats[sid])
        .map(([sid, b]) => ({
            id: sid,
            lat: typeof b.lat === 'number' ? b.lat : null,
            lon: typeof b.lon === 'number' ? b.lon : null,
            source: (b.source || 'ndbc').toLowerCase(),
        }))
        .filter(s => s.lat != null && s.lon != null);

    const groups = [];
    for (const s of stations) {
        let placed = false;
        for (const g of groups) {
            const ref = g[0];
            if (haversineMeters(s.lat, s.lon, ref.lat, ref.lon) <= 200) {
                g.push(s);
                placed = true;
                break;
            }
        }
        if (!placed) groups.push([s]);
    }

    const visible = new Set();
    for (const g of groups) {
        if (g.length === 1) {
            visible.add(g[0].id);
            continue;
        }
        g.sort((a, b) => {
            const pa = SOURCE_PRIORITY[a.source] ?? 99;
            const pb = SOURCE_PRIORITY[b.source] ?? 99;
            return pa - pb;
        });
        visible.add(g[0].id);
        // Note: g.slice(1) are hidden duplicates — their data stays in
        // raceBuoyData, just not surfaced in the UI.
    }
    visibleStationIds = visible;
}

// Wind-source dropdown. Triggered by a single button showing the
// currently selected station + its summary; click to expand the menu
// listing every station with usable wind data, each row showing the
// mean wind direction (with circular std-dev) and speed range during
// the race window.
function renderWindSourcePicker() {
    const host = document.getElementById('wind-source-picker');
    if (!host) return;

    computeAllWindStats();
    recomputeVisibleStations();

    const stations = Object.entries(raceBuoyData)
        .filter(([sid, _b]) => windStationStats[sid] && visibleStationIds.has(sid))
        .map(([sid, b]) => ({
            id: sid,
            name: b.name || sid,
            color: b.color || '#888',
            source: fmtSourceLabel(b),
            stats: windStationStats[sid],
        }));

    // If the auto-pick selected a station that's now hidden as a duplicate,
    // promote to the corresponding visible peer.
    if (selectedWindStationId && !visibleStationIds.has(selectedWindStationId) && stations.length) {
        selectedWindStationId = stations[0].id;
        rebuildWindFromSelected();
    }

    const order = (sid) => {
        const i = PRIMARY_WIND_STATIONS.indexOf(sid);
        return i >= 0 ? i : 100;
    };
    stations.sort((a, b) => {
        const o = order(a.id) - order(b.id);
        if (o !== 0) return o;
        // Then by sample count desc, then by name
        const c = b.stats.sampleCount - a.stats.sampleCount;
        return c !== 0 ? c : a.name.localeCompare(b.name);
    });

    if (stations.length === 0) {
        host.style.display = 'none';
        return;
    }
    host.style.display = 'flex';

    // Make sure selectedWindStationId is set to a station that exists
    if (!stations.find(s => s.id === selectedWindStationId)) {
        selectedWindStationId = stations[0].id;
        rebuildWindFromSelected();
    }
    const selected = stations.find(s => s.id === selectedWindStationId) || stations[0];

    host.innerHTML = `
      <button class="wind-dropdown-trigger" id="wind-dropdown-trigger" aria-haspopup="listbox" aria-expanded="false">
        <span class="wind-dropdown-prefix">WIND</span>
        <span class="wind-dropdown-current">
          <span class="wind-dropdown-current-name">${shortStationLabel(selected.id, selected.name)}</span>
          <span class="wind-dropdown-current-stats">${fmtStationStats(selected.stats)} · ${selected.source}</span>
        </span>
        <span class="wind-dropdown-arrow" aria-hidden="true">▾</span>
      </button>
      <div class="wind-dropdown-menu" id="wind-dropdown-menu" role="listbox" hidden>
        <div class="wind-dropdown-header" role="presentation"
             title="Number of wind samples each station reported during this race window">
            <span></span>
            <span>STATION</span>
            <span>SOURCE</span>
            <span>MEAN DIR · SPEED</span>
            <span class="wind-dropdown-header-n" title="Sample count during race window">N</span>
        </div>
        ${stations.map(s => {
            const isOverride = windDefaultOverride && windDefaultOverride.station_id === s.id;
            const showAdmin = _isCoachLoggedIn();
            // Two affordances per row, both shown only when the row is
            // NOT the active override:
            //   • "📌 Default" badge on the override row itself
            //   • "Set as default" button on the others (admin only)
            const rightSlot = isOverride
                ? '<span class="wind-option-default-badge" title="Race default — every visitor sees this station">📌 Default</span>'
                : (showAdmin
                    ? `<button class="wind-option-set-default" data-set-default="${s.id}" title="Make this the default wind station for this race for all visitors">📌 Set default</button>`
                    : '');
            return `
              <button class="wind-dropdown-option ${s.id === selectedWindStationId ? 'active' : ''} ${isOverride ? 'is-default' : ''}"
                      data-station="${s.id}" role="option"
                      aria-selected="${s.id === selectedWindStationId}"
                      title="${shortStationLabel(s.id, s.name)} · ${s.source} · ${s.stats.sampleCount} samples during race">
                <span class="wind-option-dot" style="background:${s.color}"></span>
                <span class="wind-option-name">${shortStationLabel(s.id, s.name)}</span>
                <span class="wind-option-source">${s.source}</span>
                <span class="wind-option-stats">${fmtStationStats(s.stats)}</span>
                <span class="wind-option-count">${s.stats.sampleCount}</span>
                <span class="wind-option-right">${rightSlot}</span>
              </button>
            `;
        }).join('')}
        ${windDefaultOverride && _isCoachLoggedIn()
            ? `<div class="wind-dropdown-footer">
                 Default set by <strong>${(windDefaultOverride.set_by || '').replace(/[<>"]/g,'')}</strong>
                 <button class="wind-clear-default" id="wind-clear-default" title="Remove the race-level default; revert to auto-pick">Clear default</button>
               </div>`
            : ''}
      </div>
    `;

    const trigger = host.querySelector('#wind-dropdown-trigger');
    const menu = host.querySelector('#wind-dropdown-menu');

    function close() {
        menu.hidden = true;
        trigger.setAttribute('aria-expanded', 'false');
    }
    function toggle(e) {
        if (e) { e.stopPropagation(); e.preventDefault(); }
        const willOpen = menu.hidden;
        menu.hidden = !willOpen;
        trigger.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    }

    trigger.addEventListener('click', toggle);

    // Click outside to close. Detach the previous render's listener so
    // we don't leak handlers across picker rebuilds.
    if (_windDropdownOutsideListener) {
        document.removeEventListener('click', _windDropdownOutsideListener);
    }
    _windDropdownOutsideListener = (e) => { if (!host.contains(e.target)) close(); };
    document.addEventListener('click', _windDropdownOutsideListener);

    for (const opt of menu.querySelectorAll('.wind-dropdown-option')) {
        opt.addEventListener('click', (e) => {
            // If the user clicked the inline "Set default" button, don't
            // also fire the row's "pick this station" handler.
            if (e.target.closest('[data-set-default]')) return;
            e.stopPropagation();
            close();
            setWindStation(opt.dataset.station);
        });
    }
    for (const btn of menu.querySelectorAll('[data-set-default]')) {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            e.preventDefault();
            const sid = btn.getAttribute('data-set-default');
            btn.disabled = true;
            btn.textContent = 'saving…';
            try {
                const saved = await setRaceWindDefault(currentRace.race_id, sid);
                windDefaultOverride = saved;
                // setWindStation re-renders the picker — but it
                // early-returns when sid already matches the active
                // station (the most common case after picking "Set
                // default" on the station you're already viewing).
                // Force a re-render so the 📌 Default badge appears
                // regardless of whether the active station changed.
                setWindStation(sid);
                renderWindSourcePicker();
            } catch (err) {
                btn.disabled = false;
                btn.textContent = '📌 Set default';
                if (!_handleCoachActionError(err)) {
                    alert(`Could not save default: ${err.message || err}`);
                }
            }
        });
    }
    const clearBtn = menu.querySelector('#wind-clear-default');
    if (clearBtn) clearBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        e.preventDefault();
        if (!confirm('Clear the race default wind station? Future visitors will see the auto-pick.')) return;
        clearBtn.disabled = true;
        clearBtn.textContent = 'clearing…';
        try {
            await clearRaceWindDefault(currentRace.race_id);
            windDefaultOverride = null;
            renderWindSourcePicker();
        } catch (err) {
            clearBtn.disabled = false;
            clearBtn.textContent = 'Clear default';
            if (!_handleCoachActionError(err)) {
                alert(`Could not clear default: ${err.message || err}`);
            }
        }
    });
}

function setWindStation(stationId) {
    if (stationId === selectedWindStationId) return;
    if (!raceBuoyData[stationId]?.data_points?.length) return;
    selectedWindStationId = stationId;
    rebuildWindFromSelected();
    renderWindSourcePicker();
    // New station → new wind series. Force the layline TWD to resample
    // against the current playback time before rebuilding the polylines.
    lastLaylineTWD = null;
    if (currentRace) {
        const targetMs = new Date(currentRace.start_time).getTime() + (playCursorSeconds * 1000);
        syncLaylineWind(targetMs);
        updateWindBadge(targetMs);
    }
    renderLaylines();
    updateSpeedChart();           // rebuilds polar overlays under new wind
    renderLeaderboard();          // TWA/%pol depend on wind
    updateBoatDrawer();
}

// Linear interp of TWD/TWS at a given time. TWD interpolated as a vector
// (sin/cos average) so wraparound across 0/360 is handled correctly.
function windAt(timeMs) {
    if (!weatherWindSamples.length) return null;
    if (timeMs <= weatherWindSamples[0].tMs) return weatherWindSamples[0];
    const last = weatherWindSamples[weatherWindSamples.length - 1];
    if (timeMs >= last.tMs) return last;
    // Binary search
    let lo = 0, hi = weatherWindSamples.length - 1;
    while (lo + 1 < hi) {
        const mid = (lo + hi) >> 1;
        if (weatherWindSamples[mid].tMs <= timeMs) lo = mid; else hi = mid;
    }
    const a = weatherWindSamples[lo], b = weatherWindSamples[hi];
    const span = b.tMs - a.tMs;
    if (span <= 0) return a;
    const f = (timeMs - a.tMs) / span;
    const ar = a.twd * Math.PI / 180, br = b.twd * Math.PI / 180;
    const sx = Math.sin(ar) * (1 - f) + Math.sin(br) * f;
    const sy = Math.cos(ar) * (1 - f) + Math.cos(br) * f;
    const twd = (Math.atan2(sx, sy) * 180 / Math.PI + 360) % 360;
    const tws = a.tws * (1 - f) + b.tws * f;
    return { tMs: timeMs, twd, tws };
}

// On-course wind for maneuver analysis. The NOAA buoy (windAt) carries a
// ~15-20° course bias, which warps the tack-geometry "wind-up" frame and
// makes real tacks fail isCleanTack. The fleet-inferred TWD
// (_inferTWDFromFleetAt) is the actual on-course wind — same source the
// laylines/polar use. Fleet inference is only valid for the CURRENT race
// (it reads live boatLayers), so callers processing other races / boats
// pass useFleet=false and fall back to the buoy.
function analysisWindAt(timeMs, useFleet) {
    if (useFleet) {
        const fw = _inferTWDFromFleetAt(timeMs);
        if (fw && fw.twd != null) return fw;
    }
    return windAt(timeMs);
}

function precomputeAllRoundings() {
    if (!currentRace) return;
    const marksById = buildMarksById(currentRace);
    const courseSeq = currentRace.course || [];
    const finishLine = currentRace.finish_line || null;
    for (const layer of Object.values(boatLayers)) {
        precomputeRoundingsForLayer(layer, courseSeq, marksById, finishLine);
    }
    // Race-wide totalLegs = max events any boat reached. Used by the
    // leaderboard to know when a boat has "finished" and by the layline
    // scanner to stop drawing once the fleet is done. With multi-lap
    // detection in precomputeRoundingsForLayer, this lifts the implicit
    // `courseSeq.length` ceiling so race 2 (course=[W,L], 2 laps, with
    // a finish line) shows 4 legs in the summary instead of 2.
    let maxEvents = 0;
    for (const layer of Object.values(boatLayers)) {
        const n = layer?.roundingTimes?.length || 0;
        if (n > maxEvents) maxEvents = n;
    }
    currentRace._totalLegs = maxEvents;
}

function legsCompletedAt(layer, targetTimeMs) {
    if (!layer?.roundingTimes) return 0;
    let count = 0;
    for (const t of layer.roundingTimes) {
        if (t !== undefined && t <= targetTimeMs) count++;
        else break;
    }
    return count;
}

// Along-course meters from start to current playback time. Sums leg
// lengths for completed legs and adds projected progress along the
// active leg (legLength - distToTarget, clamped to [0, legLength]).
// Indexes into courseSeq modulo its length so multi-lap progress (race
// 2: course=[W,L] sailed 2× → legsCompleted up to 4) keeps growing
// instead of capping at the end of lap 1.
function progressMetersAt(point, courseSeq, marksById, legsCompleted, startAnchor) {
    if (!courseSeq?.length || !point) return 0;
    let prev = startAnchor;
    if (!prev) return 0;
    let cum = 0;
    for (let i = 0; i < legsCompleted; i++) {
        const m = marksById[courseSeq[i % courseSeq.length]];
        if (!m) break;
        cum += haversineMeters(prev.lat, prev.lon, m.lat, m.lon);
        prev = m;
    }
    const totalLegs = currentRace?._totalLegs ?? courseSeq.length;
    if (legsCompleted < totalLegs && prev) {
        const target = marksById[courseSeq[legsCompleted % courseSeq.length]];
        if (target) {
            const legLen = haversineMeters(prev.lat, prev.lon, target.lat, target.lon);
            const distToT = haversineMeters(point.lat, point.lon, target.lat, target.lon);
            cum += Math.max(0, Math.min(legLen, legLen - distToT));
        }
    }
    return cum;
}

// Shared canvas renderer for the per-segment speed-coloured trail
// segments — much faster than SVG when each boat may have 60–600
// short coloured polylines updated every playback tick. Lazy-init on
// first use so the map layer doesn't get created before initMap()
// has run.
let _speedSegRenderer = null;
function _getSpeedSegRenderer() {
    if (!_speedSegRenderer && map) {
        _speedSegRenderer = L.canvas({ padding: 0.5 });
    }
    return _speedSegRenderer;
}

function addBoatTrack(deviceId, gpsData, boat, imuData = null) {
    const color = colorFor(deviceId);

    // Create track polyline (initially empty — populated by applyTrailWindow
    // after the first updateBoatPositions call sets currentIdx).
    const coords = gpsData.map(p => [p.lat, p.lon]);
    const track = L.polyline([], {
        color: color,
        weight: 3,
        opacity: 0.8,
    }).addTo(map);

    // Boat label marker — boat initials (NS for Never Settle, PD for
    // Pressure Drop) + sail # + optional stat line. Boat name is more
    // identifying than skipper for tactical map reading — same
    // identity priority as the leaderboard.
    const initials = teamInitials(boat?.boat_name || boat?.team_name || '');
    const sailNumber = (boat?.sail_number || '').toString().trim();
    const initialCourse = gpsData[0]?.course || 0;
    const marker = L.marker([0, 0], {
        icon: createBoatIcon(color, initialCourse, initials, {}, sailNumber),
        rotationOrigin: 'center center',
    }).addTo(map);
    marker.on('click', () => openBoatDrawer(deviceId));

    // Real-scale sailboat hull, sized to the race's boat_class. Drawn
    // in metres so it stays correct at every zoom; visible-as-a-shape
    // only when zoomed in enough to resolve a few metres per pixel,
    // which is exactly the mark-rounding zoom range where it matters.
    const hull = L.polygon([], {
        color: '#ffffff',
        weight: 1,
        opacity: 0.9,
        fillColor: color,
        fillOpacity: 0.55,
        interactive: false,
    }).addTo(map);

    // Mainsail boom — straight line from the mast (≈ antenna position)
    // aft-and-out to the side OPPOSITE the wind. Acts as a tack
    // indicator: boom on port = starboard tack, boom on stbd = port
    // tack. Hidden when no per-boat TWA is available.
    const boom = L.polyline([], {
        color: '#ffffff',
        weight: 2,
        opacity: 0.85,
        interactive: false,
    }).addTo(map);

    // Pre-compute cumulative distance (meters) per GPS sample so the
    // leaderboard can sort by progress rather than instantaneous speed.
    const cumDist = new Float32Array(gpsData.length);
    for (let i = 1; i < gpsData.length; i++) {
        const a = gpsData[i - 1];
        const b = gpsData[i];
        const seg = (a.lat && a.lon && b.lat && b.lon)
            ? haversineMeters(a.lat, a.lon, b.lat, b.lon)
            : 0;
        cumDist[i] = cumDist[i - 1] + seg;
    }

    // Pre-parse timestamps once so the trail window can walk back from the
    // current playback index without re-parsing ISO strings every frame.
    const times = new Float64Array(gpsData.length);
    for (let i = 0; i < gpsData.length; i++) {
        times[i] = new Date(gpsData[i].t).getTime();
    }

    boatLayers[deviceId] = {
        deviceId,
        track,
        marker,
        hull,
        boom,
        data: gpsData,
        coords,
        times,
        cumDist,
        boat,
        color,
        initials,
        sailNumber,
        imu: imuData || [],   // for per-frame heel readout on the marker label
        visible: true,
        // GPX-backup boats (no E1 device on the day → user uploaded a
        // post-race .gpx) typically have a long pre-start tail of the
        // delivery sail / dock departure that adds nothing to the race
        // story but clutters the standard view. The trail window for
        // these boats is also clipped to race start time. E1-native
        // boats are unaffected — auto-recording only kicks on at >2 kt
        // so they don't carry that tail.
        gpxOnly: !!boat?.gpx_path,
        // Pool of L.polyline objects, one per visible trail segment
        // when speed-colour mode is on. We REUSE these across frames
        // (set lat/lngs + style) instead of creating/destroying —
        // creation is the slow part. Pool grows on demand and shrinks
        // by hiding (not removing) extras to keep the next render
        // fast. Lazy-allocated on first speed-colour render.
        segPool: [],
    };
}

function updateBoatPositions(timeSeconds) {
    const startTime = currentRace ? new Date(currentRace.start_time).getTime() : 0;
    const targetTime = startTime + timeSeconds * 1000;
    playCursorSeconds = timeSeconds;  // calculatePositions reads this

    // Pass 1: snap each boat's layer.current/currentIdx to the closest
    // GPS sample for this playback time. This must complete before
    // calculatePositions runs because the leaderboard ranking and the
    // VMG / %pol / progress numbers all read layer.current.
    for (const [deviceId, layer] of Object.entries(boatLayers)) {
        if (!layer.visible || !layer.data.length) continue;
        let closestIdx = 0;
        let minDiff = Infinity;
        for (let i = 0; i < layer.data.length; i++) {
            const pointTime = new Date(layer.data[i].t).getTime();
            const diff = Math.abs(pointTime - targetTime);
            if (diff < minDiff) { minDiff = diff; closestIdx = i; }
        }
        const closest = layer.data[closestIdx];
        if (closest && closest.lat && closest.lon) {
            layer.marker.setLatLng([closest.lat, closest.lon]);
            layer.current = closest;
            layer.currentIdx = closestIdx;
            applyTrailWindow(layer);
        }
    }

    // RRS 18 zone — fade in/out the 3-boat-length circles around each
    // mark based on whether any visible boat is currently inside.
    updateMarkZoneCircles();

    // Compute the leaderboard once so per-boat stats (vmg / polarPct /
    // rank) are available to the marker labels and don't get
    // recomputed downstream.
    const positions = calculatePositions();
    const statsByDevice = new Map();
    positions.forEach((p, idx) => {
        statsByDevice.set(p.deviceId, { ...p, rank: idx + 1 });
    });

    // Inter-boat distance lines (toggle via SHOW › ↔ Dist). Walks the
    // same `positions` array so each line connects boats that are
    // adjacent in the current leaderboard order.
    updateDistanceLines(positions);

    // Pass 2: paint each marker icon with its full stats payload.
    for (const [deviceId, layer] of Object.entries(boatLayers)) {
        if (!layer.visible || !layer.current) continue;
        const closest = layer.current;
        const course = closest.course || 0;
        const imuSample = nearestSampleAt(layer.imu, targetTime);
        const heelDeg = imuSample?.heel ?? null;
        const wSample = windAt(targetTime);
        const twaSigned = wSample ? (((wSample.twd - course + 540) % 360) - 180) : null;
        const lbStats = statsByDevice.get(deviceId);
        layer.marker.setIcon(createBoatIcon(layer.color, course, layer.initials, {
            speedKn: closest.speed_kn ?? null,
            heelDeg,
            twaSigned,
            vmg: lbStats?.vmg ?? null,
            polarPct: lbStats?.polarPct ?? null,
            rank: lbStats?.rank ?? null,
            hdop: closest.hdop ?? null,
        }, layer.sailNumber));

        // Real-scale hull polygon + mainsail boom. Both anchored to
        // the antenna fix; hull rotates with COG, boom rotates with
        // COG and swings to the side opposite the wind. For handicap
        // races the boat-specific LOA (from the catalog) sizes the
        // polygon — an Arcona 430 renders much bigger than a J/80.
        if (layer.hull) {
            const dims = hullDimsForDevice(deviceId);
            layer.hull.setLatLngs(hullPolygonLatLngs(closest.lat, closest.lon, course, dims));
        }
        if (layer.boom) {
            const bm = boomLatLngs(closest.lat, closest.lon, course, twaSigned);
            layer.boom.setLatLngs(bm || []);
        }
    }

    // Refresh leaderboard + chart play cursors + drawer at playback time.
    // renderLeaderboard recomputes positions internally — the small
    // duplication is fine and keeps the flow simple.
    renderLeaderboard();
    updatePlayCursor(timeSeconds);
    updateWindBadge(targetTime);
    // Pivot the laylines to the wind at the current playback time. Cheap
    // — re-renders only when TWD has shifted ≥1° from the last drawn pair.
    syncLaylineWind(targetTime);
    // Refresh the polar plot when it's open — moves each boat's dot
    // to its current (TWA, SOG). No-op when overlay is hidden.
    refreshPolarOverlayIfOpen();
    // Re-frame the map around the leader + their next mark. Throttled
    // internally to one fly per ~700 ms so slider scrubbing or fast
    // playback doesn't ricochet the viewport.
    applyLeaderFollow(false);
    updateGhost(targetTime);
    updateBoatDrawer();
}

// ── Polar ghost (target boat) ───────────────────────────────────────────
// For the SELECTED boat, simulate sailing the SAME geographic line at polar
// TARGET speed (boat's own VPP + NOAA wind), anchored at the class-start
// gun. Renders a ghost marker ahead on your own track, highlights the lead
// it has built, and labels the time/distance you're leaving out there.
// v1 mode = 'targetSpeed'; 'optimal' (ideal angles to the mark) is a planned
// second mode. Same NOAA wind source the drawer %polar uses — so the two
// numbers reconcile (note: that buoy carries a ~15-20° course bias, so this
// is an estimate, not gospel).
let _ghostMarker = null, _ghostLead = null;

function _ensureGhostObjects() {
    if (!_ghostLead) {
        _ghostLead = L.polyline([], { color: '#22d3ee', weight: 4, opacity: 0.7,
            dashArray: '5 6', interactive: false });
    }
    if (!_ghostMarker) {
        _ghostMarker = L.marker([0, 0], { interactive: false, keyboard: false,
            zIndexOffset: 300 });
    }
}

function hideGhost() {
    if (_ghostMarker && map && map.hasLayer(_ghostMarker)) map.removeLayer(_ghostMarker);
    if (_ghostLead && map && map.hasLayer(_ghostLead)) map.removeLayer(_ghostLead);
}

function _boatClassStartMs(boat) {
    const cls = (currentRace?.classes || []).find(c => c.id === boat?.class);
    return cls?.start_time ? new Date(cls.start_time).getTime() : null;
}

function _fmtGap(sec) {
    const s = Math.round(Math.abs(sec));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

// ghostElapsed[i] = seconds from the class-start gun to reach vertex i if
// this line were sailed at polar target speed. Anchored at startIdx (first
// vertex at/after the gun) so the gap is 0 at the gun and grows only from
// in-race losses. Cached + signature-keyed (boat | wind source | mode |
// window) so it only recomputes when something material changes.
function computeGhost(layer) {
    const boat = layer.boat;
    const polar = boat?.polar;
    if (!polar || !(polar.nospin || polar.spin)) return null;
    if (!weatherWindSamples || !weatherWindSamples.length) return null;
    const classStartMs = _boatClassStartMs(boat);
    if (classStartMs == null) return null;

    const times = layer.times, cum = layer.cumDist, data = layer.data;
    const n = data.length;
    const finishMs = boat?.finish_time ? new Date(boat.finish_time).getTime() : times[n - 1];
    const sig = `${boat.boat_id || layer.deviceId}|${weatherWindSource}|${ghostMode}|${classStartMs}|${finishMs}`;
    if (layer._ghost && layer._ghost.sig === sig) return layer._ghost;

    let startIdx = 0;
    while (startIdx < n - 1 && times[startIdx] < classStartMs) startIdx++;
    let finishIdx = startIdx;
    while (finishIdx < n - 1 && times[finishIdx + 1] <= finishMs) finishIdx++;
    if (finishIdx <= startIdx) { layer._ghost = { sig, invalid: true }; return null; }

    const ghostElapsed = new Float64Array(n);
    for (let i = startIdx + 1; i <= finishIdx; i++) {
        const segLen = cum[i] - cum[i - 1];               // metres along the line
        const w = windAt(times[i - 1]);
        let dt;
        if (w && w.tws != null) {
            const twa = ((w.twd - (data[i - 1].course || 0) + 540) % 360) - 180;
            const tgtKn = _polarSpeedKn(polar, twa, w.tws);
            const tgtMps = tgtKn ? tgtKn * 0.514444 : null;
            dt = (tgtMps && tgtMps > 0.05) ? segLen / tgtMps : (times[i] - times[i - 1]) / 1000;
        } else {
            dt = (times[i] - times[i - 1]) / 1000;        // no wind → match actual
        }
        ghostElapsed[i] = ghostElapsed[i - 1] + dt;
    }
    layer._ghost = { sig, startIdx, finishIdx, classStartMs, ghostElapsed };
    return layer._ghost;
}

function updateGhost(targetTimeMs) {
    const sel = drawerDeviceId;
    const layer = sel ? boatLayers[sel] : null;
    if (!markerOverlays.polarGhost || !layer || !layer.visible) { hideGhost(); return; }
    if (ghostMode === 'optimal') { updateGhostOptimal(targetTimeMs, layer); return; }
    const g = computeGhost(layer);
    if (!g || g.invalid) { hideGhost(); return; }
    const { startIdx, finishIdx, classStartMs, ghostElapsed } = g;
    const times = layer.times, cum = layer.cumDist, coords = layer.coords;

    const Treal = (targetTimeMs - classStartMs) / 1000;
    if (Treal <= 0) { hideGhost(); return; }              // pre-start: no ghost

    const realIdx = Math.min(Math.max(layer.currentIdx ?? startIdx, startIdx), finishIdx);

    // Locate the ghost: vertex j with ghostElapsed[j] <= Treal <= [j+1].
    let j, f = 0, terminal = false;
    if (Treal >= ghostElapsed[finishIdx]) {
        j = finishIdx; terminal = true;                   // ghost already finished
    } else {
        j = realIdx;
        if (ghostElapsed[j] > Treal) { while (j > startIdx && ghostElapsed[j] > Treal) j--; }
        else { while (j < finishIdx && ghostElapsed[j + 1] <= Treal) j++; }
        const span = ghostElapsed[j + 1] - ghostElapsed[j];
        f = span > 0 ? (Treal - ghostElapsed[j]) / span : 0;
    }
    const jb = Math.min(j + 1, finishIdx);
    const a = coords[j], b = coords[jb];
    const ghostPos = [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
    const ghostS = cum[j] + (cum[jb] - cum[j]) * f;

    // Headline: how long your polar-self reached your CURRENT spot before you.
    const gapSec = (times[realIdx] - classStartMs) / 1000 - ghostElapsed[realIdx];
    const gapM = ghostS - cum[realIdx];
    const ahead = gapSec < -1;                            // you're beating polar

    _ensureGhostObjects();
    const color = layer.color || '#22d3ee';
    const label = terminal
        ? `🏁 polar finished +${_fmtGap(gapSec)} ahead`
        : (ahead ? `▲ ${_fmtGap(gapSec)} ahead of polar`
                 : `👻 +${_fmtGap(gapSec)}${Math.abs(gapM) >= 10 ? ` · ${Math.round(gapM)}m` : ''}`);
    _ghostMarker.setIcon(L.divIcon({
        className: 'ghost-marker',
        html: `<span class="ghost-dot" style="background:${color}"></span>`
            + `<span class="ghost-label">${label}</span>`,
        iconSize: [0, 0], iconAnchor: [0, 0],
    }));
    _ghostMarker.setLatLng(ghostPos);
    if (!map.hasLayer(_ghostMarker)) _ghostMarker.addTo(map);

    // Lead segment: the slice of YOUR own line between you and the ghost.
    const loI = Math.min(realIdx, j), hiI = Math.max(realIdx, j);
    const lead = coords.slice(loI, hiI + 1).map(c => [c[0], c[1]]);
    if (!terminal) { (realIdx <= j) ? lead.push(ghostPos) : lead.unshift(ghostPos); }
    _ghostLead.setLatLngs(lead);
    _ghostLead.setStyle({ color, opacity: ahead ? 0.4 : 0.7, dashArray: '5 6', weight: 4 });
    if (!map.hasLayer(_ghostLead)) _ghostLead.addTo(map);
}

// ── Optimal-routing ghost (mode 2) ──────────────────────────────────────
// Sails the IDEAL route to each course mark — an actually-sailable path:
// TACKS up the laylines at the polar's best beat angle on legs to windward,
// GYBES down the runs at the best downwind angle, fetches straight on
// reaches. One tack/gybe per leg (no-shift optimum). Times each segment at
// the boat's own polar boatspeed for that angle, with per-leg NOAA wind,
// anchored at the gun. ESTIMATE — sensitive to the buoy wind's ~15-20° bias,
// which moves both the laylines and the time gap.

// Best VMG point of sail from a boat's own polar, CLAMPED to the polar's
// published TWA range (extrapolating below it invents bogus high speeds and
// a false-optimal pinching angle). Returns {twa, vmg(kn toward/away wind), speed}.
function _bestVMG(polar, tws, upwind) {
    const tA = polar?.twa_values || [];
    if (!tA.length) return null;
    const lo = Math.ceil(tA[0]), hi = Math.floor(tA[tA.length - 1]);
    let best = null;
    for (let twa = lo; twa <= hi; twa++) {
        if (upwind ? twa >= 90 : twa < 90) continue;
        const v = _polarSpeedKn(polar, twa, tws);
        if (!v) continue;
        const vmg = upwind ? v * Math.cos(twa * Math.PI / 180)
                           : v * Math.cos((180 - twa) * Math.PI / 180);
        if (vmg > 0 && (!best || vmg > best.vmg)) best = { twa, vmg, speed: v };
    }
    return best;
}

// Local ENU helpers (small-area equirectangular) for layline geometry.
function _llToXY(p, ref) {
    const k = Math.PI / 180;
    return { x: (p.lon - ref.lon) * Math.cos(ref.lat * k) * 111320,
             y: (p.lat - ref.lat) * 110540 };
}
function _xyToLL(xy, ref) {
    const k = Math.PI / 180;
    return { lat: ref.lat + xy.y / 110540,
             lon: ref.lon + xy.x / (Math.cos(ref.lat * k) * 111320) };
}
function _dirVec(bearingDeg) {
    const r = bearingDeg * Math.PI / 180;
    return { x: Math.sin(r), y: Math.cos(r) };   // (East, North)
}
// Fraction (0..1) of P's projection onto segment A→B (along-course position).
function _projFrac(A, B, P) {
    const b = _llToXY(B, A), p = _llToXY(P, A);   // A at origin
    const L2 = b.x * b.x + b.y * b.y;
    if (L2 < 1e-6) return 0;
    return Math.max(0, Math.min(1, (p.x * b.x + p.y * b.y) / L2));
}
// Single tack (downwind=false) / gybe (true) corner from A to B sailing at
// optimal angle `theta` (TWA) given wind-from `twd`: the boat sails one tack
// to the layline, then the other to lay B. Returns the corner {lat,lon} or
// null when no forward two-tack solution exists (treat as a straight fetch).
function _cornerPoint(A, B, twd, theta, downwind) {
    const base = downwind ? (twd + 180) : twd;     // axis we're sailing toward
    const off = downwind ? (180 - theta) : theta;  // angle off that axis
    const Bxy = _llToXY(B, A);                      // A at origin
    let best = null;
    for (const [hA, hB] of [[base - off, base + off], [base + off, base - off]]) {
        const d1 = _dirVec(hA), d2 = _dirVec(hB);
        const det = d1.x * d2.y - d2.x * d1.y;
        if (Math.abs(det) < 1e-9) continue;
        const t1 = (Bxy.x * d2.y - d2.x * Bxy.y) / det;   // A + t1*d1 = corner
        const t2 = (d1.x * Bxy.y - d1.y * Bxy.x) / det;
        if (t1 > 2 && t2 > 2 && (!best || t1 + t2 < best.total)) {  // forward, favoured (shorter)
            best = { total: t1 + t2, xy: { x: t1 * d1.x, y: t1 * d1.y } };
        }
    }
    return best ? _xyToLL(best.xy, A) : null;
}

// Build the optimal route as an actually-sailable path: tack to the layline
// on beats, gybe on runs, fetch straight on reaches. Returns route lat/lngs
// plus cumulative TIME and cumulative along-course (rhumb) distance per
// vertex — the latter so the gap compares against your own rhumb progress.
function _buildOptimalRoute(layer, wps) {
    const polar = layer.boat.polar;
    const classStartMs = _boatClassStartMs(layer.boat);
    const routeLL = [[wps[0].lat, wps[0].lon]];
    const cumTime = [0], cumRhumb = [0];
    const push = (p, segSec, rhumb) => {
        routeLL.push([p.lat, p.lon]);
        cumTime.push(cumTime[cumTime.length - 1] + segSec);
        cumRhumb.push(rhumb);
    };
    for (let i = 1; i < wps.length; i++) {
        const A = wps[i - 1], B = wps[i];
        const L = haversineMeters(A.lat, A.lon, B.lat, B.lon);
        const be = bearingDegrees(A.lat, A.lon, B.lat, B.lon);
        const w = windAt(classStartMs + cumTime[cumTime.length - 1] * 1000) || windAt(classStartMs);
        const S_A = cumRhumb[cumRhumb.length - 1];
        const up = w ? _bestVMG(polar, w.tws, true) : null;
        const dn = w ? _bestVMG(polar, w.tws, false) : null;
        const g = w ? Math.abs(((be - w.twd + 540) % 360) - 180) : 90;
        let corner = null, vKn = null;
        if (w && up && g <= up.twa) { corner = _cornerPoint(A, B, w.twd, up.twa, false); vKn = up.speed; }
        else if (w && dn && g >= dn.twa) { corner = _cornerPoint(A, B, w.twd, dn.twa, true); vKn = dn.speed; }
        if (corner && vKn) {
            const v = vKn * 0.514444;
            const l1 = haversineMeters(A.lat, A.lon, corner.lat, corner.lon);
            const l2 = haversineMeters(corner.lat, corner.lon, B.lat, B.lon);
            push(corner, l1 / v, S_A + _projFrac(A, B, corner) * L);
            push(B, l2 / v, S_A + L);
        } else {
            const v = ((w && _polarSpeedKn(polar, g, w.tws)) || 3) * 0.514444;
            push(B, L / v, S_A + L);
        }
    }
    return { routeLL, cumTime, cumRhumb, classStartMs };
}

// Course waypoints: start-line midpoint → each mark in the course → finish
// midpoint. Returns null if the race has no usable course.
function _courseWaypoints(race) {
    const start = startMidpoint(race);
    const byId = buildMarksById(race);
    const marks = (race?.course || []).map(id => byId[id]).filter(m => m && m.lat != null);
    if (!start || marks.length < 1) return null;
    const wps = [{ lat: start.lat, lon: start.lon }, ...marks.map(m => ({ lat: m.lat, lon: m.lon }))];
    const fl = race?.finish_line;
    if (fl && fl.pin_lat != null && fl.boat_lat != null) {
        wps.push({ lat: (fl.pin_lat + fl.boat_lat) / 2, lon: (fl.pin_lon + fl.boat_lon) / 2 });
    }
    return { wps, nMarks: marks.length };
}

// Your along-course distance over [start, …marks, finish], using rounded
// marks (legsDone) + projection onto the current leg. Mirrors
// progressMetersAt but extends to the finish leg.
function _ghostCourseProgressM(wps, nMarks, point, legsDone) {
    const reached = Math.min(legsDone, nMarks);
    let cum = 0;
    for (let i = 1; i <= reached; i++) {
        cum += haversineMeters(wps[i - 1].lat, wps[i - 1].lon, wps[i].lat, wps[i].lon);
    }
    const nextI = reached + 1;
    if (nextI < wps.length && point) {
        const prev = wps[reached], target = wps[nextI];
        const legLen = haversineMeters(prev.lat, prev.lon, target.lat, target.lon);
        const dToT = haversineMeters(point.lat, point.lon, target.lat, target.lon);
        cum += Math.max(0, Math.min(legLen, legLen - dToT));
    }
    return cum;
}

function _interp(xs, ys, x) {
    const n = xs.length;
    if (n === 0) return 0;
    if (x <= xs[0]) return ys[0];
    if (x >= xs[n - 1]) return ys[n - 1];
    let k = 0;
    while (k < n - 1 && xs[k + 1] < x) k++;
    const span = xs[k + 1] - xs[k];
    const f = span > 0 ? (x - xs[k]) / span : 0;
    return ys[k] + (ys[k + 1] - ys[k]) * f;
}

// Precompute the optimal (tacked/gybed) route + per-vertex time and rhumb
// progress, anchored at the gun. Cached + signature-keyed.
function computeOptimalGhost(layer) {
    const boat = layer.boat, polar = boat?.polar;
    if (!polar || !(polar.nospin || polar.spin)) return null;
    if (!weatherWindSamples || !weatherWindSamples.length) return null;
    if (_boatClassStartMs(boat) == null) return null;
    const cw = _courseWaypoints(currentRace);
    if (!cw) return null;
    const sig = `${boat.boat_id || layer.deviceId}|${weatherWindSource}|opt|${currentRace.race_id}|${cw.wps.length}`;
    if (layer._ghostOpt && layer._ghostOpt.sig === sig) return layer._ghostOpt;
    const route = _buildOptimalRoute(layer, cw.wps);
    layer._ghostOpt = { sig, ...route, wps: cw.wps, nMarks: cw.nMarks };
    return layer._ghostOpt;
}

function updateGhostOptimal(targetTimeMs, layer) {
    const g = computeOptimalGhost(layer);
    if (!g) { hideGhost(); return; }
    const { routeLL, cumTime, cumRhumb, classStartMs, wps, nMarks } = g;
    const T = (targetTimeMs - classStartMs) / 1000;
    if (T <= 0) { hideGhost(); return; }
    const last = cumTime.length - 1;

    let pos, sOpt, terminal = false;
    if (T >= cumTime[last]) {
        pos = routeLL[last]; sOpt = cumRhumb[last]; terminal = true;
    } else {
        let k = 0;
        while (k < last && cumTime[k + 1] <= T) k++;
        const span = cumTime[k + 1] - cumTime[k];
        const f = span > 0 ? (T - cumTime[k]) / span : 0;
        pos = [routeLL[k][0] + (routeLL[k + 1][0] - routeLL[k][0]) * f,
               routeLL[k][1] + (routeLL[k + 1][1] - routeLL[k][1]) * f];
        sOpt = cumRhumb[k] + (cumRhumb[k + 1] - cumRhumb[k]) * f;
    }

    const legsDone = legsCompletedAt(layer, targetTimeMs);
    const sYou = layer.current ? _ghostCourseProgressM(wps, nMarks, layer.current, legsDone) : 0;
    const gapSec = T - _interp(cumRhumb, cumTime, sYou);   // time behind optimal pace
    const gapM = sOpt - sYou;
    const ahead = gapSec < -1;

    _ensureGhostObjects();
    const color = layer.color || '#22d3ee';
    _ghostLead.setLatLngs(routeLL);                          // actual tacked/gybed route
    _ghostLead.setStyle({ color, opacity: 0.55, dashArray: '3 6', weight: 3 });
    if (!map.hasLayer(_ghostLead)) _ghostLead.addTo(map);

    const label = terminal
        ? `🎯 optimal done · you +${_fmtGap(gapSec)}`
        : (ahead ? `▲ ${_fmtGap(gapSec)} ahead of optimal`
                 : `🎯 +${_fmtGap(gapSec)}${Math.abs(gapM) >= 10 ? ` · ${Math.round(gapM)}m` : ''}`);
    _ghostMarker.setIcon(L.divIcon({
        className: 'ghost-marker',
        html: `<span class="ghost-dot" style="background:${color}"></span>`
            + `<span class="ghost-label">${label}</span>`,
        iconSize: [0, 0], iconAnchor: [0, 0],
    }));
    _ghostMarker.setLatLng(pos);
    if (!map.hasLayer(_ghostMarker)) _ghostMarker.addTo(map);
}

// Refresh the map's top-left wind picker (rotating arrow + TWD/TWS
// readout) and the per-station map roses. The leaderboard wind badge
// has been removed — the picker on the map is now the single source.
function updateWindBadge(targetTimeMs) {
    const sample = windAt(targetTimeMs);
    const arrow  = document.getElementById('map-wind-arrow');
    const twdEl  = document.getElementById('map-wind-twd');
    const twsEl  = document.getElementById('map-wind-tws');
    if (arrow && sample) {
        // Wind blows TO (twd + 180); arrow SVG points up (north) at 0°.
        const blowTo = (sample.twd + 180) % 360;
        arrow.style.transform = `rotate(${blowTo}deg)`;
        arrow.title = `True wind from ${weatherWindSource || 'NOAA'} (interpolated)`;
    }
    if (twdEl) twdEl.textContent = sample ? `${sample.twd.toFixed(0).padStart(3, '0')}°` : '---°';
    if (twsEl) twsEl.textContent = sample ? `${sample.tws.toFixed(1)} kn` : '-- kn';
    updateAllWindMarkers(targetTimeMs);
}

// Find the wind sample at a given time for ONE specific station (closest
// data point). Used to drive the multi-station map rose markers.
function stationWindAt(stationId, timeMs) {
    const buoy = raceBuoyData[stationId];
    if (!buoy?.data_points?.length) return null;
    let best = null, bestDiff = Infinity;
    for (const dp of buoy.data_points) {
        if (dp.wind_dir == null || dp.wind_speed_kts == null) continue;
        const t = dp.timestamp ? new Date(dp.timestamp).getTime() : (dp.unix_ts * 1000);
        if (!Number.isFinite(t)) continue;
        const diff = Math.abs(t - timeMs);
        if (diff < bestDiff) {
            bestDiff = diff;
            best = { tMs: t, twd: dp.wind_dir, tws: dp.wind_speed_kts };
        }
    }
    // Reject samples too far from the requested time (>2h, prevents
    // showing wildly stale data from buffer regions).
    return bestDiff < 7200_000 ? best : null;
}

function createWindRoseIcon(twd, tws, opts = {}) {
    const isSel = !!opts.selected;
    const sz = isSel ? 56 : 36;
    const arrow = isSel ? 44 : 30;
    const fontTws = isSel ? '0.85rem' : '0.65rem';
    const fontTwd = isSel ? '0.65rem' : '0.55rem';
    const color = isSel ? '#22d3ee' : (opts.color || '#9ca3af');
    const stroke = isSel ? 3 : 2;
    const opacity = isSel ? 1 : 0.78;
    const blowTo = (twd + 180) % 360;
    const tspeed = (tws ?? 0).toFixed(0);
    const tdir = (twd ?? 0).toFixed(0).padStart(3, '0');
    const html = `
        <div class="wind-rose ${isSel ? 'is-selected' : 'is-secondary'}" style="opacity:${opacity}">
            <div class="wind-rose-arrow" style="transform: rotate(${blowTo}deg); width:${arrow}px; height:${arrow}px">
                <svg width="${arrow}" height="${arrow}" viewBox="0 0 48 48">
                    <line x1="24" y1="40" x2="24" y2="10" stroke="${color}" stroke-width="${stroke}" stroke-linecap="round"/>
                    <polygon points="24,4 17,16 31,16" fill="${color}"/>
                </svg>
            </div>
            <div class="wind-rose-label" style="color:${color}">
                <div class="wind-rose-tws" style="font-size:${fontTws}">${tspeed} kn</div>
                <div class="wind-rose-twd" style="font-size:${fontTwd}">${tdir}°</div>
            </div>
        </div>
    `;
    return L.divIcon({
        html, className: 'wind-rose-marker',
        iconSize: [sz + 36, sz],
        iconAnchor: [arrow / 2, arrow / 2],
    });
}

// Render or update markers for EVERY station with wind data. Selected
// station gets the cyan/large treatment; others render small + muted.
// Each marker rotates and updates with the playback cursor.
function updateAllWindMarkers(targetTimeMs) {
    if (!map) return;
    if (weatherObsHidden) {   // Weather panel hid the obs buoys — clear rose markers
        for (const sid of Object.keys(windMarkers)) { map.removeLayer(windMarkers[sid]); delete windMarkers[sid]; }
        return;
    }
    const present = new Set();
    for (const [sid, buoy] of Object.entries(raceBuoyData)) {
        if (!windStationStats[sid]) continue;
        // Skip stations hidden as duplicates of higher-priority sources.
        if (!visibleStationIds.has(sid)) continue;
        if (buoy.lat == null || buoy.lon == null) continue;
        present.add(sid);
        const sample = stationWindAt(sid, targetTimeMs);
        // Fall back to mean stats if no sample close enough
        const twd = sample?.twd ?? windStationStats[sid].meanTWD;
        const tws = sample?.tws ?? windStationStats[sid].avgTWS;
        const isSelected = sid === selectedWindStationId;
        const icon = createWindRoseIcon(twd, tws, {
            selected: isSelected,
            color: buoy.color || '#9ca3af',
        });
        if (windMarkers[sid]) {
            windMarkers[sid].setIcon(icon);
        } else {
            const m = L.marker([buoy.lat, buoy.lon], {
                icon,
                zIndexOffset: isSelected ? -50 : -150,
                interactive: true,
            });
            m.bindTooltip(buoy.name || sid, { sticky: true });
            m.on('click', () => setWindStation(sid));
            m.addTo(map);
            windMarkers[sid] = m;
        }
        windMarkers[sid].setTooltipContent(
            `${buoy.name || sid}: ${twd.toFixed(0)}° / ${tws.toFixed(1)} kn`
        );
    }
    // Remove any markers for stations no longer present
    for (const sid of Object.keys(windMarkers)) {
        if (!present.has(sid)) {
            map.removeLayer(windMarkers[sid]);
            delete windMarkers[sid];
        }
    }
}

function fitMapToBounds() {
    // Initial framing: start line + first mark in one frame. That's
    // the racing area — pre-start positioning at the bottom, the first
    // beat at the top — and what the user wants to see when they land
    // on a race. Once they press play, follow-mode (which frames the
    // fleet only) takes over.
    const corners = [];
    const sl = currentRace?.start_line;
    if (sl && sl.pin_lat != null && sl.boat_lat != null) {
        corners.push([sl.pin_lat, sl.pin_lon]);
        corners.push([sl.boat_lat, sl.boat_lon]);
    }
    const courseSeq = currentRace?.course || [];
    if (courseSeq.length) {
        const marksById = buildMarksById(currentRace);
        const firstMark = marksById[courseSeq[0]];
        if (firstMark && firstMark.lat != null && firstMark.lon != null) {
            corners.push([firstMark.lat, firstMark.lon]);
        }
    }
    if (corners.length >= 2) {
        const bounds = L.latLngBounds(corners);
        console.log('[Race] Fitting to start-line + first-mark bounds:', bounds.toBBoxString());
        // Padding 60 px gives breathing room around the line endpoints
        // and the windward; maxZoom 17 caps how tight we go for short
        // beats so the windward never fills the screen alone.
        map.fitBounds(bounds, { padding: [60, 60], maxZoom: 17 });
        return;
    }

    // Fallback: full-race bounds (older races without a defined course).
    const allCoords = [];
    for (const layer of Object.values(boatLayers)) {
        if (layer.data) {
            for (const p of layer.data) {
                if (p.lat && p.lon) allCoords.push([p.lat, p.lon]);
            }
        }
    }
    console.log(`[Race] fitMapToBounds fallback: ${allCoords.length} coordinates`);
    if (allCoords.length > 0) {
        map.fitBounds(L.latLngBounds(allCoords), { padding: [50, 50] });
    } else {
        console.warn('[Race] No coordinates to fit map bounds');
    }
}

// --- Boat Legend ---

// Top-left fleet-tracker legend has been replaced by per-marker labels
// (initials + speed + heel) drawn beside the boat arrow. The leaderboard
// on the right is the canonical color/team key. No-op kept so call sites
// don't need editing.
function renderBoatLegend() {}

function updateLegendSpeed(deviceId, speed) {
    const el = document.getElementById(`legend-speed-${deviceId}`);
    if (el) {
        el.textContent = `${speed.toFixed(1)} kn`;
    }
}

function toggleBoatVisibility(deviceId) {
    const layer = boatLayers[deviceId];
    if (!layer) return;

    layer.visible = !layer.visible;

    if (layer.visible) {
        layer.track.addTo(map);
        layer.marker.addTo(map);
        if (layer.hull) layer.hull.addTo(map);
        if (layer.boom) layer.boom.addTo(map);
        // Repaint will rebuild any speed-colour segments via applyTrailWindow.
    } else {
        map.removeLayer(layer.track);
        map.removeLayer(layer.marker);
        if (layer.hull) map.removeLayer(layer.hull);
        if (layer.boom) map.removeLayer(layer.boom);
        // Hide the per-segment polylines too — applyTrailWindow's
        // visible-guard upstream handles this for the toggle path,
        // but the explicit hide here keeps the off state immediate.
        if (layer.segPool) {
            for (const s of layer.segPool) {
                if (map.hasLayer(s)) map.removeLayer(s);
            }
        }
    }
}

// --- Boats catalog hydration ---
//
// New races reference boats by boat_id (catalog FK). Old races keep
// boat metadata embedded. This shim parallel-fetches catalog docs for
// every unique boat_id on the race, then overlays the catalog fields
// onto the matching per-race boat entry — but only fields that aren't
// already set, so per-race overrides (e.g. a guest skipper for one
// night) take precedence over the catalog default.
async function hydrateBoatsFromCatalog(race) {
    if (!race || !Array.isArray(race.boats)) return;
    const ids = Array.from(new Set(race.boats
        .map(b => b.boat_id).filter(Boolean)));
    if (!ids.length) return;
    const docs = await Promise.all(ids.map(id =>
        fetch(`${API_BASE}/api/boats/${id}`)
            .then(r => r.ok ? r.json() : null)
            .catch(() => null)));
    const byId = {};
    for (const d of docs) if (d?.boat_id) byId[d.boat_id] = d;
    for (const b of race.boats) {
        const cat = b.boat_id && byId[b.boat_id];
        if (!cat) continue;
        // Identity: prefer per-race overrides if set; otherwise inherit
        // from catalog.
        if (!b.boat_name) b.boat_name = cat.name;
        if (!b.boat_type) b.boat_type = cat.type;
        if (!b.sail_number) b.sail_number = cat.sail_number;
        if (!b.club) b.club = cat.club;
        // Catalog-only fields — always pulled (no per-race override
        // path today; if we add one later, gate the assignment).
        if (b.loa_m == null) b.loa_m = cat.loa_m;
        // Skippers — prefer the array form. Legacy `skipper` string
        // remains in sync via the lambda's normalizer.
        if (!Array.isArray(b.skippers) || !b.skippers.length) {
            b.skippers = Array.isArray(cat.skippers) ? cat.skippers : [];
        }
        if (!b.skipper) b.skipper = cat.skipper;
        if (!b.team_name && cat.skipper) b.team_name = cat.skipper;
        if (!b.cert_url) b.cert_url = cat.cert_url;
        if (!b.mbsa_url) b.mbsa_url = cat.mbsa_url;
        b.photos = b.photos || cat.photos || {};
        b.links = b.links || cat.links || [];
        b.notes = b.notes || cat.notes || '';
        b._catalog = cat;   // keep raw for drawer / debug
    }
}

// --- Leaderboard ---

// Top-level dispatcher: handicap races (currentRace.classes non-empty)
// render the PHRF-grouped leaderboard; everything else falls through to
// the legacy course-aware GPS-only renderer below.
function renderLeaderboard() {
    if (!currentRace) {
        const c = document.getElementById('leaderboard');
        if (c) c.innerHTML = '<div class="leaderboard-empty">Select a race to view standings</div>';
        return;
    }
    if (Array.isArray(currentRace.classes) && currentRace.classes.length > 0) {
        renderPHRFLeaderboard();
        return;
    }
    renderLegacyLeaderboard();
}

function renderLegacyLeaderboard() {
    const container = document.getElementById('leaderboard');

    if (!currentRace || !raceData) {
        container.innerHTML = '<div class="leaderboard-empty">Select a race to view standings</div>';
        return;
    }

    // Get current positions based on distance or speed
    const positions = calculatePositions();

    // Sync laylines to the *trailing* boat's next mark — laylines stay
    // visible until the LAST boat rounds, so boats still approaching the
    // windward keep their tactical aid even after the leader is past.
    // Shifts to the next target (or hides, if that target is downwind)
    // only when every boat has rounded. Re-renders only on change.
    const newActiveLeg = positions.length
        ? Math.min(...positions.map(p => p.legsCompleted))
        : 0;
    if (newActiveLeg !== activeLeg) {
        activeLeg = newActiveLeg;
        renderLaylines();
    }

    const drawerActive = drawerDeviceId;
    container.innerHTML = positions.map((item, index) => {
        const pos = index + 1;
        const color = colorFor(item.deviceId);
        const posClass = pos <= 3 ? `p${pos}` : '';
        const activeClass = item.deviceId === drawerActive ? ' active' : '';
        const finClass = item.finished ? ' leaderboard-finished' : '';

        // Bottom-line stats. Finished boats show `FIN ✓` plus the time gap
        // to the winner ("+0:25") instead of live VMG/TWA/%pol/distance —
        // those numbers are meaningless once a boat has crossed.
        const subParts = [];
        if (item.finished) {
            subParts.push('FIN ✓');
            if (item.gapSec == null) {
                subParts.push(positions.length > 1 ? 'WIN' : '—');
            } else {
                subParts.push(`+${fmtMMSS(item.gapSec)}`);
            }
        } else {
            if (item.vmg !== null && item.vmg !== undefined) {
                const sign = item.vmg >= 0 ? '+' : '';
                subParts.push(`VMG ${sign}${item.vmg.toFixed(1)}`);
            }
            if (item.twa !== null && item.twa !== undefined) {
                const tack = item.twa < 0 ? 'P' : 'S';
                subParts.push(`${tack} ${Math.abs(item.twa).toFixed(0)}°`);
            }
            if (item.polarPct !== null && item.polarPct !== undefined) {
                subParts.push(`${item.polarPct.toFixed(0)}%pol`);
            }
            if (item.gap === null || item.gap === undefined) {
                subParts.push(positions.length > 1 ? 'LEAD' : '—');
            } else {
                const gap = item.gap;
                subParts.push(gap >= 1000
                    ? `−${(gap / 1000).toFixed(2)} km`
                    : `−${gap.toFixed(0)} m`);
            }
        }

        // Speed cell: live SOG normally; total elapsed M:SS when finished.
        const speedCell = item.finished && item.finishElapsedSec != null
            ? `<div class="leaderboard-speed leaderboard-finish-time" title="Total race time">${fmtMMSS(item.finishElapsedSec)}</div>`
            : `<div class="leaderboard-speed">${item.speed.toFixed(1)} kn</div>`;

        return `
            <div class="leaderboard-item${activeClass}${finClass}" data-device-id="${item.deviceId}">
                <div class="leaderboard-position ${posClass}">${pos}</div>
                <div class="leaderboard-boat-color" style="background: ${color}"></div>
                <div class="leaderboard-boat-info">
                    <div class="leaderboard-boat-name">${item.displayName}</div>
                    <div class="leaderboard-boat-subtitle">${item.subtitle}</div>
                </div>
                <div class="leaderboard-stats">
                    ${speedCell}
                    <div class="leaderboard-delta">${subParts.join(' · ')}</div>
                </div>
            </div>
        `;
    }).join('');

    // Click handler: open the per-boat drawer. Re-bind on every render
    // since innerHTML wiped the old listeners.
    for (const el of container.querySelectorAll('.leaderboard-item')) {
        el.addEventListener('click', () => {
            const id = el.getAttribute('data-device-id');
            if (id) openBoatDrawer(id);
        });
    }
}

// --- PHRF leaderboard (handicap, multi-class, multi-start) ---
//
// Different game from the legacy GPS course-aware leaderboard. The
// roster is currentRace.boats (which includes non-GPS handicap
// entries). Each boat carries: class, rating, finish_time,
// finish_status. We rank within class by corrected time
// (elapsed × rating). Live per-boat speed/heel/%pol still come from
// raceData.boats[device_id] for GPS-equipped boats; the *order* never
// changes during playback — this is a results sheet, not a live race.

const PHRF_STATUSES = new Set(['FIN', 'DNF', 'DNC', 'DSQ', 'RET', 'OCS', 'NSC']);

// Display label for the class header. Prefers an explicit
// rating_system on the class (e.g. "ORR-EZ"), falls back to a generic
// label if only rating_type is set. Result: "ORR-EZ · W50/L50 - Medium"
// (or just "W50/L50 - Medium" if rating_system isn't set yet on a
// legacy race).
function _ratingLabel(cls) {
    if (!cls) return 'ORR-EZ';
    const sys = cls.rating_system || '';
    const t = cls.rating_type || '';
    if (sys && t) return `${sys} · ${t}`;
    return sys || t || 'ORR-EZ';
}

// LOA conversion helper for the race dashboard — same as boats-app.js
// but kept local because race-app is a separate module and there's
// no shared util layer yet.
function _loaFeet(loaM, decimals = 1) {
    if (loaM == null || !Number.isFinite(Number(loaM))) return null;
    return (Number(loaM) * 3.28084).toFixed(decimals);
}

function _parseTimeToMs(t) {
    if (!t) return null;
    const d = new Date(t);
    const ms = d.getTime();
    return Number.isFinite(ms) ? ms : null;
}

// The scored racing window for ONE boat: [class start gun, that boat's
// finish]. All analysis (tacks, maneuvers, leg/polar stats) must clip to
// this so pre-start milling and post-finish sailing-home never count.
// end = official finish_time → else the boat's last mark rounding → else
// the race end. Pass the boat record (has .class / .finish_time) and its
// layer (has .roundingTimes).
function racingWindowMsFor(boatRecord, layer) {
    let startMs = currentRace?.start_time ? new Date(currentRace.start_time).getTime() : 0;
    let endMs = currentRace?.end_time ? new Date(currentRace.end_time).getTime() : Infinity;
    const cls = (currentRace?.classes || []).find(c => c.id === boatRecord?.class);
    const gun = cls ? _parseTimeToMs(cls.start_time) : null;
    if (gun != null) startMs = gun;
    const fin = _parseTimeToMs(boatRecord?.finish_time);
    if (fin != null) {
        endMs = fin;
    } else if (layer?.roundingTimes?.length) {
        const last = layer.roundingTimes[layer.roundingTimes.length - 1];
        if (last != null) endMs = last;
    }
    return { startMs, endMs };
}

// Median GPS sample rate (Hz) of a track, optionally restricted to a
// [startMs, endMs] window. Surfaced in the boat panel + analytics so it's
// obvious WHY a sparse track (e.g. a 0.1 Hz phone GPX = 1 fix / 10 s) can't
// be tack/maneuver-analysed — the detector needs ~1 Hz or finer.
function gpsHzFor(layer, startMs, endMs) {
    const d = layer?.data;
    if (!d || d.length < 5) return null;
    const dts = [];
    let prev = null;
    for (let i = 0; i < d.length; i++) {
        const t = (layer.times && Number.isFinite(layer.times[i])) ? layer.times[i] : new Date(d[i].t).getTime();
        if (!Number.isFinite(t)) continue;
        if (startMs != null && endMs != null && (t < startMs || t > endMs)) continue;
        if (prev != null) { const dt = (t - prev) / 1000; if (dt > 0 && dt < 60) dts.push(dt); }
        prev = t;
    }
    if (dts.length < 3) return null;
    dts.sort((a, b) => a - b);
    const med = dts[dts.length >> 1];
    return med > 0 ? 1 / med : null;
}

function fmtGpsHz(hz) {
    if (hz == null || !Number.isFinite(hz)) return '—';
    if (hz >= 3) return `${Math.round(hz)} Hz`;
    const s = hz.toFixed(1);
    return `${s.endsWith('.0') ? s.slice(0, -2) : s} Hz`;
}

// Analysis-sufficiency bucket: ok ≥~1 Hz, low ~0.3-1 Hz, poor <0.3 Hz.
function gpsHzClass(hz) {
    if (hz == null) return 'gps-hz-unknown';
    if (hz >= 0.9) return 'gps-hz-ok';
    if (hz >= 0.3) return 'gps-hz-low';
    return 'gps-hz-poor';
}

// Returns { class_id → [boats sorted by rank] } where each row carries
// the PHRF computation (elapsed_sec, corrected_sec) plus the live
// state pulled from calculatePositions() (speed_kn, etc.) when the boat
// has a GPS track.
function computePHRFResults() {
    const out = {};
    if (!currentRace || !Array.isArray(currentRace.classes)) return out;

    // GPS live state keyed by deviceId. May be empty if raceData hasn't
    // arrived yet — the PHRF rank still computes from finish_time alone.
    const liveByDevice = {};
    if (raceData) {
        for (const p of calculatePositions()) liveByDevice[p.deviceId] = p;
    }

    // Per-class start time lookup
    const classStartMs = {};
    for (const c of currentRace.classes) {
        classStartMs[c.id] = _parseTimeToMs(c.start_time);
    }

    for (const c of currentRace.classes) out[c.id] = [];

    for (const boat of currentRace.boats || []) {
        const classId = boat.class;
        if (!classId || !(classId in out)) continue;  // boat without a class is hidden
        const startMs = classStartMs[classId];
        const finishMs = _parseTimeToMs(boat.finish_time);
        const rating = Number(boat.rating);
        const status = (boat.finish_status || (finishMs ? 'FIN' : 'DNS')).toUpperCase();

        let elapsedSec = null;
        let correctedSec = null;
        if (status === 'FIN' && startMs != null && finishMs != null) {
            // Pursuit races start each boat at its own staggered gun — elapsed
            // is from that boat's own start, not the class (first) gun.
            const ownStart = boat.pursuit_start ? _parseTimeToMs(boat.pursuit_start) : null;
            elapsedSec = (finishMs - (ownStart != null ? ownStart : startMs)) / 1000;
            // PHRF stores a precomputed corrected time (ToT/ToD/pursuit don't
            // reduce to elapsed×rating, and `rating` holds the real PHRF number).
            // ORR-EZ falls back to elapsed×rating as before.
            if (Number.isFinite(boat.corrected_sec)) {
                correctedSec = boat.corrected_sec;
            } else if (Number.isFinite(rating) && rating > 0) {
                correctedSec = elapsedSec * rating;
            }
        }

        out[classId].push({
            boat,
            deviceId: boat.device_id || null,
            classId,
            rating: Number.isFinite(rating) ? rating : null,
            startMs,
            finishMs,
            elapsedSec,
            correctedSec,
            status,
            live: boat.device_id ? (liveByDevice[boat.device_id] || null) : null,
        });
    }

    // Rank within class: FIN (by corrected ASC, then elapsed ASC), then
    // non-finishers (DNF/DNC/RET/DSQ/OCS) at the bottom in entry order.
    for (const classId of Object.keys(out)) {
        const fin = out[classId].filter(r => r.status === 'FIN' && r.correctedSec != null);
        const finNoRating = out[classId].filter(r => r.status === 'FIN' && r.correctedSec == null);
        const dnf = out[classId].filter(r => r.status !== 'FIN');
        fin.sort((a, b) => a.correctedSec - b.correctedSec);
        finNoRating.sort((a, b) => (a.elapsedSec ?? Infinity) - (b.elapsedSec ?? Infinity));
        out[classId] = [...fin, ...finNoRating, ...dnf];
    }

    return out;
}

// ===== ORR-EZ "Learning" explainer =====
// A teaching panel that explains how the selected race is scored, using
// THIS race's real boats and numbers. Reuses computePHRFResults() (the
// same elapsed × rating math the leaderboard ranks on) so the worked
// example always matches the official results.

function _fmtDur(sec) {
    if (sec == null || !Number.isFinite(sec)) return '—';
    const s = Math.round(sec);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    return h > 0
        ? `${h}:${String(m).padStart(2, '0')}:${String(ss).padStart(2, '0')}`
        : `${m}:${String(ss).padStart(2, '0')}`;
}

function openOrrEzModal() {
    const modal = document.getElementById('orrez-modal');
    const body = document.getElementById('orrez-modal-body');
    if (!modal || !body) return;
    body.innerHTML = renderOrrEzExplainer();
    modal.style.display = 'flex';
}

function renderOrrEzExplainer() {
    if (!currentRace) return '<div class="modal-empty">Load a race first.</div>';
    const classes = currentRace.classes || [];
    if (!classes.length) {
        return '<div class="modal-empty">This race isn’t scored on a handicap rating — boats finish on the line (first-to-finish), so there’s no corrected time to explain.</div>';
    }
    const esc = (typeof _attrEsc === 'function') ? _attrEsc : (s => String(s ?? ''));
    // PHRF regattas use a different handicap model than ORR-EZ — show a
    // PHRF-specific explainer instead of the elapsed×rating one.
    const sys0 = `${classes[0]?.rating_system || ''} ${classes[0]?.rating_type || ''}`.toUpperCase();
    if (sys0.includes('PHRF')) {
        const pursuit = sys0.includes('PURSUIT');
        const tod = !pursuit && (sys0.includes('DISTANCE') || sys0.includes('TOD'));
        if (pursuit) {
            return `
            <div class="orrez">
              <p class="orrez-lede">A <strong>pursuit race</strong> puts the handicap into the <em>start</em>, not a
              post-race correction. <strong>Slower boats (higher PHRF) start first</strong>; faster boats start later
              by their rating. If everyone sailed exactly to handicap they’d all finish together — so the
              <strong>first boat across the line wins</strong>. No corrected time: finish order is the result.</p>
              <div class="orrez-formula">
                <div class="orrez-formula-main">Start&nbsp;delay&nbsp;=&nbsp;(slowest&nbsp;rating&nbsp;−&nbsp;your&nbsp;rating)&nbsp;×&nbsp;course&nbsp;distance</div>
                <div class="orrez-formula-sub">Everyone sails the same course &nbsp;·&nbsp; <strong>first to finish wins</strong></div>
              </div>
              <div class="orrez-grid2">
                <div class="orrez-card"><h4>Reading the table</h4>
                  <p><strong>Place = finish order.</strong> Each boat’s <em>start</em> is its own staggered gun and
                  <em>elapsed</em> is its real time on the course (finish − own start) — a faster elapsed means the
                  boat beat its rating, even if it didn’t cross first.</p></div>
                <div class="orrez-card"><h4>Which way does the rating go?</h4>
                  <p>A <strong>lower PHRF = a faster boat</strong>, so it starts later and has less time to make up
                  the gap. A boat that finishes ahead of its scheduled rivals sailed better than its handicap.</p></div>
              </div>
            </div>`;
        }
        return `
        <div class="orrez">
          <p class="orrez-lede">Boats of different size and speed race together, so first-across-the-line
          wouldn’t be fair. <strong>PHRF</strong> gives each boat a <strong>rating</strong> (seconds-per-mile
          handicap) and scores on <strong>corrected time</strong> — lowest corrected wins.</p>
          <div class="orrez-formula">
            <div class="orrez-formula-main">${tod
                ? 'Corrected&nbsp;=&nbsp;Elapsed&nbsp;−&nbsp;(Rating&nbsp;×&nbsp;Distance)'
                : 'Corrected&nbsp;=&nbsp;Elapsed&nbsp;×&nbsp;TCF,&nbsp;&nbsp;TCF&nbsp;=&nbsp;650&nbsp;/&nbsp;(550&nbsp;+&nbsp;Rating)'}</div>
            <div class="orrez-formula-sub">Elapsed = finish − class start &nbsp;·&nbsp;
              ${tod ? 'Time-on-Distance' : 'Time-on-Time'} &nbsp;·&nbsp; <strong>lowest corrected wins</strong></div>
          </div>
          <div class="orrez-grid2">
            <div class="orrez-card"><h4>Which way does the rating go?</h4>
              <p>A <strong>lower PHRF rating = a faster boat</strong> (it owes time). Slower boats carry a higher
              rating and are given time back, so a smaller boat can beat a bigger one on corrected time.</p></div>
            <div class="orrez-card"><h4>How this regatta was scored</h4>
              <p>${tod
                ? 'A distance race scored <strong>Time-on-Distance</strong>: each boat’s corrected time subtracts its rating times the course distance sailed.'
                : 'Buoy races scored <strong>Time-on-Time</strong>: corrected time multiplies elapsed by a Time-Correction-Factor derived from the boat’s PHRF rating.'}</p></div>
          </div>
        </div>`;
    }
    const results = computePHRFResults();
    const band = (classes[0]?.rating_type || '').split('-').pop().trim() || '—';
    const ratingLabel = _ratingLabel(classes[0]);
    const nm = (b) => (b?.boat_name || b?.team_name || 'Boat');

    // --- Concept + formula (static) ---
    let html = `
    <div class="orrez">
      <p class="orrez-lede">Boats of very different size and speed race together here, so first-across-the-line
      wouldn’t be fair — a big fast boat would win every time. <strong>ORR-EZ</strong> gives each boat a
      <strong>rating</strong> (a handicap number from its measurements) and scores the race on
      <strong>corrected time</strong> instead of finish order.</p>

      <div class="orrez-formula">
        <div class="orrez-formula-main">Corrected&nbsp;time&nbsp;=&nbsp;Elapsed&nbsp;time&nbsp;×&nbsp;Rating</div>
        <div class="orrez-formula-sub">Elapsed = your finish − your class’s start gun &nbsp;·&nbsp; <strong>lowest corrected time wins</strong></div>
      </div>

      <div class="orrez-grid2">
        <div class="orrez-card">
          <h4>Which way does the rating go?</h4>
          <p>A <strong>higher rating = a faster boat</strong>. Because corrected = elapsed × rating, a fast boat’s
          time gets scaled <em>up</em> — it’s expected to finish sooner, so it has to. A slower boat (lower
          rating) gets its time scaled <em>down</em> as compensation. Two equally-rated boats just race straight up.</p>
        </div>
        <div class="orrez-card">
          <h4>Why the wind band matters</h4>
          <p>Boats’ <em>relative</em> speeds change with the breeze, so every boat has different ratings for
          <strong>Light</strong>, <strong>Medium</strong> and <strong>Heavy</strong> air. The Race Committee picks the
          band for the day. This race was scored <strong>${esc(band)}</strong> <span class="orrez-dim">(${esc(ratingLabel)})</span>.</p>
        </div>
        <div class="orrez-card">
          <h4>Spinnaker vs Non-Spinnaker (NS)</h4>
          <p>A boat racing <strong>without a spinnaker</strong> (white sails only) is slower, so it gets a
          separate, more favourable <strong>“NS” rating</strong>. The same hull can enter Spinnaker <em>or</em>
          Non-Spinnaker and is scored on whichever it actually sailed — the RC notes each boat’s configuration.
          So your rating comes from <strong>the boat’s measurements + the course type + the wind band + whether
          you flew a kite</strong>.</p>
        </div>
      </div>`;

    // --- Per-class worked example + table (dynamic) ---
    for (const cls of classes) {
        const rows = (results[cls.id] || []);
        const fin = rows.filter(r => r.status === 'FIN' && r.correctedSec != null);
        html += `<h3 class="orrez-class">${esc(cls.name || cls.id)} <span class="orrez-dim">· ${esc(ratingLabel)}</span></h3>`;

        // Line-honors (lowest elapsed) vs corrected winner (lowest corrected).
        if (fin.length >= 2) {
            const winner = fin[0];  // computePHRFResults sorts FIN by corrected ASC
            const lineHonors = fin.reduce((a, b) => (b.elapsedSec < a.elapsedSec ? b : a));
            if (lineHonors !== winner) {
                const lhName = nm(lineHonors.boat), wName = nm(winner.boat);
                const needElapsed = winner.correctedSec / lineHonors.rating;   // elapsed LH needed to win
                const shortfall = lineHonors.elapsedSec - needElapsed;
                const crossedGap = winner.finishMs - lineHonors.finishMs;       // ms LH crossed ahead by
                const lostBy = winner.correctedSec - lineHonors.correctedSec;   // negative => LH actually won; here LH lost so >0... compute abs
                html += `
                <div class="orrez-callout">
                  <strong>${esc(lhName)} crossed the line first</strong> (elapsed ${_fmtDur(lineHonors.elapsedSec)}) —
                  but <strong>${esc(wName)} won ${esc(cls.name || cls.id)}</strong> on corrected time.
                  ${esc(lhName)} is rated faster (<strong>${lineHonors.rating}</strong> vs <strong>${winner.rating}</strong>),
                  so it owed time. To win it needed a corrected time under ${esc(wName)}’s ${_fmtDur(winner.correctedSec)} —
                  i.e. an elapsed under <strong>${_fmtDur(needElapsed)}</strong>. It sailed ${_fmtDur(lineHonors.elapsedSec)},
                  about <strong>${_fmtDur(Math.abs(shortfall))} too slow</strong>. It beat ${esc(wName)} across the line by only
                  ${_fmtDur(Math.abs(crossedGap) / 1000)}, so on corrected time <strong>${esc(wName)} wins by ${_fmtDur(Math.abs(lostBy))}</strong>.
                </div>`;
            }
        }

        // Full worked table.
        if (!rows.length) { html += '<p class="orrez-dim">No entries.</p>'; continue; }
        const winnerCorrected = fin.length ? fin[0].correctedSec : null;
        html += `<div class="orrez-tablewrap"><table class="orrez-table">
            <thead><tr>
              <th>#</th><th>Boat</th><th class="num">Rating</th><th class="num">Elapsed</th>
              <th class="num orrez-calc">× rating =</th><th class="num">Corrected</th><th class="num">Δ to winner</th>
            </tr></thead><tbody>`;
        rows.forEach((r, i) => {
            const place = r.status === 'FIN' && r.correctedSec != null ? (i + 1) : r.status;
            const delta = (r.correctedSec != null && winnerCorrected != null)
                ? (r.correctedSec === winnerCorrected ? '—' : '+' + _fmtDur(r.correctedSec - winnerCorrected))
                : '';
            const calc = (r.elapsedSec != null && r.rating != null)
                ? `${_fmtDur(r.elapsedSec)} × ${r.rating}`
                : '';
            html += `<tr class="${i === 0 && r.status === 'FIN' ? 'orrez-winner' : ''}">
                <td>${place}</td>
                <td>${esc(nm(r.boat))} <span class="orrez-dim">${esc(r.boat?.boat_type || '')}</span></td>
                <td class="num">${r.rating ?? '—'}</td>
                <td class="num">${_fmtDur(r.elapsedSec)}</td>
                <td class="num orrez-calc">${calc}</td>
                <td class="num"><strong>${_fmtDur(r.correctedSec)}</strong></td>
                <td class="num">${delta}</td>
            </tr>`;
        });
        html += '</tbody></table></div>';
    }

    html += `
      <div class="orrez-takeaways">
        <h4>What it means on the water</h4>
        <ul>
          <li>To win, beat the boats rated near or above you by <strong>more than the rating gap</strong> between you.</li>
          <li>You can finish ahead of a slower-rated boat and still <strong>lose to it on corrected</strong> — that’s the handicap doing its job.</li>
          <li>Check your rating against your rivals’ to know how much time you <strong>owe</strong> (you’re faster) or are <strong>owed</strong> (you’re slower).</li>
        </ul>
      </div>
    </div>`;
    return html;
}

// Format a UTC ISO timestamp as the local wall-clock HH:MM:SS the
// regatta scoring sheet shows ("19:17:32"). Race scoring is always
// reported in local time at the venue; the user's browser timezone
// is the same as the venue for our use case (Boston).
function _fmtLocalHMS(iso) {
    const ms = _parseTimeToMs(iso);
    if (ms == null) return '—';
    const d = new Date(ms);
    return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function _fmtElapsedHMS(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '—';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.round(seconds % 60);
    if (h > 0) return `${h}:${m.toString().padStart(2,'0')}:${s.toString().padStart(2,'0')}`;
    return `${m}:${s.toString().padStart(2,'0')}`;
}

function renderPHRFLeaderboard() {
    const container = document.getElementById('leaderboard');
    if (!container) return;
    const results = computePHRFResults();
    const classes = currentRace.classes || [];
    const visibleClasses = classes.filter(c => classFilter === 'all' || classFilter === c.id);
    const drawerActive = drawerDeviceId;

    // Class filter — "All" first, then one tab per class. Built every
    // render so a class added/renamed in the editor reflects immediately.
    const toggle = `
        <div class="lb-class-filter" role="tablist" aria-label="Filter by class">
            <button type="button" data-class-filter="all"
                    class="${classFilter === 'all' ? 'active' : ''}">All</button>
            ${classes.map(c => `
                <button type="button" data-class-filter="${_attrEsc(c.id)}"
                        class="${classFilter === c.id ? 'active' : ''}">${_attrEsc(c.name || c.id)}</button>
            `).join('')}
        </div>
    `;

    const sections = visibleClasses.map(cls => {
        const rows = results[cls.id] || [];
        const startLocal = _fmtLocalHMS(cls.start_time);
        const header = `
            <div class="lb-class-header">
                <span class="lb-class-name">${_attrEsc(cls.name || cls.id)}</span>
                <span class="lb-class-meta">Start ${startLocal} · ${_attrEsc(_ratingLabel(cls))}</span>
            </div>
        `;
        if (!rows.length) {
            return header + '<div class="leaderboard-empty">No boats in this class</div>';
        }
        return header + rows.map((r, idx) => _renderPHRFRow(r, idx, drawerActive)).join('');
    }).join('');

    container.innerHTML = toggle + (sections || '<div class="leaderboard-empty">No classes defined</div>');

    // Class filter buttons
    for (const btn of container.querySelectorAll('[data-class-filter]')) {
        btn.addEventListener('click', () => {
            const v = btn.getAttribute('data-class-filter');
            setClassFilter(v);
        });
    }
    // Row click → drawer. GPS boats open the full live-data drawer;
    // non-GPS boats open a profile-only view (photos, type, LOA,
    // skipper, links, race history).
    for (const el of container.querySelectorAll('.leaderboard-item[data-device-id]')) {
        el.addEventListener('click', () => {
            const id = el.getAttribute('data-device-id');
            if (id) openBoatDrawer(id);
        });
    }
    for (const el of container.querySelectorAll('.leaderboard-item[data-boat-id]')) {
        el.addEventListener('click', () => {
            const bid = el.getAttribute('data-boat-id');
            if (!bid) return;
            // A handicap boat keyed by boat_id can still have a GPS track
            // (uploaded GPX/VKX). If so, open the full live drawer (motion,
            // track quality, …) like the map marker does — only fall back to
            // the catalog-only view for boats with no track at all.
            if (boatLayers[bid]?.data?.length) openBoatDrawer(bid);
            else openCatalogDrawer(bid);
        });
    }
}

function _renderPHRFRow(r, idx, drawerActive) {
    const pos = idx + 1;
    const posLabel = r.status === 'FIN' ? String(pos) : r.status;
    const posClass = r.status === 'FIN' && pos <= 3 ? `p${pos}` : '';
    const activeClass = r.deviceId && r.deviceId === drawerActive ? ' active' : '';
    const finClass = r.status === 'FIN' ? ' leaderboard-finished' : ' lb-row-dnf';
    const noGpsClass = r.deviceId ? '' : ' lb-no-gps';

    const boat = r.boat;
    const team = (boat?.team_name || '').trim();
    const boatName = (boat?.boat_name || '').trim();
    const sailNumber = (boat?.sail_number != null ? String(boat.sail_number) : '').trim();
    const boatType = (boat?.boat_type || '').trim();

    // Identity priority: boat name first (e.g. "Never Settle"), then
    // skipper, sail#, device id. Boat name is the more identifying
    // anchor for a regatta result sheet — same boat shows up week
    // after week with potentially different skippers.
    let displayName;
    const idBits = [];
    if (boatName) {
        displayName = boatName;
        if (team) idBits.push(team);
    } else if (team) {
        displayName = team;
    } else if (sailNumber) {
        displayName = `#${sailNumber}`;
    } else {
        displayName = r.deviceId || '—';
    }
    if (sailNumber && !displayName.includes(sailNumber)) idBits.push(`#${sailNumber}`);
    if (boatType) idBits.push(boatType);
    const subtitle = idBits.join(' · ');

    // Color swatch only for GPS boats (the only ones that have a
    // matching coloured polyline + map marker). Non-GPS gets a hollow
    // grey dot — visually signals "no track on the map".
    const color = r.deviceId ? colorFor(r.deviceId) : '#3a3a3a';
    const swatchStyle = r.deviceId
        ? `background:${color}`
        : `background:transparent;border:1.5px dashed ${color}`;

    // Right-hand stats cluster: corrected time large, elapsed + rating
    // small below. For DNF/DNC/RET the status itself is the "result".
    let bigCell, smallParts;
    if (r.status === 'FIN' && r.correctedSec != null) {
        bigCell = `<div class="leaderboard-speed lb-corr" title="Corrected time = elapsed × rating">${_fmtElapsedHMS(r.correctedSec)}</div>`;
        smallParts = [];
        if (r.rating != null) smallParts.push(`<span title="ORR-EZ rating (multiplier)">${r.rating.toFixed(3)}</span>`);
        if (r.elapsedSec != null) smallParts.push(`<span title="Elapsed">e ${_fmtElapsedHMS(r.elapsedSec)}</span>`);
        if (r.finishMs != null) smallParts.push(`<span title="Finish wall-clock">${_fmtLocalHMS(boat.finish_time)}</span>`);
    } else if (r.status === 'FIN' && r.elapsedSec != null) {
        bigCell = `<div class="leaderboard-speed lb-corr">${_fmtElapsedHMS(r.elapsedSec)}</div>`;
        smallParts = ['<span>(no rating)</span>'];
    } else {
        bigCell = `<div class="leaderboard-speed lb-status-big">${r.status}</div>`;
        smallParts = [];
        if (r.rating != null) smallParts.push(`<span>${r.rating.toFixed(3)}</span>`);
        if (boatType && !subtitle.includes(boatType)) smallParts.push(`<span>${_attrEsc(boatType)}</span>`);
    }

    // Row identifier: device_id when this boat is GPS-equipped (live
    // data drawer); boat_id when it's a results-only entry (catalog-
    // only drawer). Either way the drawer opens — boats without GPS
    // still have photos, skipper info, race history worth surfacing.
    const rowAttrs = r.deviceId
        ? `data-device-id="${_attrEsc(r.deviceId)}"`
        : (r.boat?.boat_id ? `data-boat-id="${_attrEsc(r.boat.boat_id)}"` : '');

    return `
        <div class="leaderboard-item${activeClass}${finClass}${noGpsClass}" ${rowAttrs}>
            <div class="leaderboard-position ${posClass}">${posLabel}</div>
            <div class="leaderboard-boat-color" style="${swatchStyle}"></div>
            <div class="leaderboard-boat-info">
                <div class="leaderboard-boat-name">${_attrEsc(displayName)}</div>
                <div class="leaderboard-boat-subtitle">${_attrEsc(subtitle)}</div>
            </div>
            <div class="leaderboard-stats">
                ${bigCell}
                <div class="leaderboard-delta">${smallParts.join(' · ')}</div>
            </div>
        </div>
    `;
}

// --- Class filter (handicap multi-class races only) ---

function setClassFilter(v) {
    if (v !== 'all' && !(currentRace?.classes || []).some(c => c.id === v)) return;
    if (classFilter === v) return;
    classFilter = v;
    try { localStorage.setItem('sf-class-filter', v); } catch {}
    // Reflect into URL without polluting history.
    try {
        const u = new URL(location.href);
        if (v === 'all') u.searchParams.delete('class');
        else u.searchParams.set('class', v);
        history.replaceState(null, '', u.toString());
    } catch {}
    applyClassFilterToMap();
    renderLeaderboard();
}

// Show/hide each boat's track + marker + hull + boom + speed segments
// based on the active class filter. No-op for races without classes
// (every layer stays visible). Called whenever the filter changes or
// new boat layers are wired up.
function applyClassFilterToMap() {
    if (!map) return;
    // boatLayers is keyed by trackKey — that's device_id for fleet
    // trackers and boat_id for handicap boats that uploaded their
    // own GPX. Build a single track_key → class lookup so either
    // kind of layer resolves correctly.
    const classByKey = {};
    if (Array.isArray(currentRace?.boats)) {
        for (const b of currentRace.boats) {
            if (b.device_id) classByKey[b.device_id] = b.class || null;
            if (b.boat_id)   classByKey[b.boat_id]   = b.class || null;
        }
    }
    const classesDefined = Array.isArray(currentRace?.classes) && currentRace.classes.length > 0;
    for (const [trackKey, L] of Object.entries(boatLayers)) {
        const cls = classByKey[trackKey];
        const visible = !classesDefined || classFilter === 'all' || classFilter === cls;
        const op = visible ? 1 : 0;
        if (L.track && L.track.setStyle) L.track.setStyle({ opacity: op });
        if (L.marker && L.marker.setOpacity) L.marker.setOpacity(op);
        if (L.hull && L.hull.setStyle) L.hull.setStyle({ opacity: op, fillOpacity: op * 0.7 });
        if (L.boom && L.boom.setStyle) L.boom.setStyle({ opacity: op });
        if (L.segPool) for (const s of L.segPool) {
            if (s.setStyle) s.setStyle({ opacity: op });
        }
    }
}

// Read initial filter from URL ?class= or localStorage. Called once at
// race-load so a deep link / refresh restores the user's view.
function initClassFilterFromPersistence() {
    let v = 'all';
    try {
        const u = new URL(location.href);
        const qp = u.searchParams.get('class');
        if (qp) v = qp;
        else {
            const stored = localStorage.getItem('sf-class-filter');
            if (stored) v = stored;
        }
    } catch {}
    classFilter = v;
}

// Binary-search the GPS index whose timestamp is closest to (and not
// after) targetMs, using the pre-parsed layer.times Float64Array. Used
// to "freeze" the leaderboard at a boat's finish moment — once a boat
// crosses the line, every subsequent leaderboard render reads the GPS
// sample at the finish time, not the live-playback time, so speed /
// COG / TWA / VMG / %pol all snap to the finish-state values.
function gpsIdxAt(layer, targetMs) {
    if (!layer?.times || !layer.times.length) return null;
    const ts = layer.times;
    if (targetMs <= ts[0]) return 0;
    if (targetMs >= ts[ts.length - 1]) return ts.length - 1;
    let lo = 0, hi = ts.length - 1;
    while (lo + 1 < hi) {
        const mid = (lo + hi) >> 1;
        if (ts[mid] <= targetMs) lo = mid; else hi = mid;
    }
    return lo;
}

function calculatePositions() {
    if (!raceData?.boats) return [];

    const positions = [];

    const courseSeq = currentRace?.course || [];
    const courseDefined = courseSeq.length > 0;
    const marksById = buildMarksById(currentRace);
    const startAnchor = startMidpoint(currentRace);
    const startTimeMs = currentRace ? new Date(currentRace.start_time).getTime() : 0;
    const targetTimeMs = startTimeMs + (playCursorSeconds * 1000);

    for (const [deviceId, boatData] of Object.entries(raceData.boats)) {
        if (boatData.error || !boatData.sensors?.gps?.length) continue;

        const layer = boatLayers[deviceId];
        const boat = boatData.boat;

        const gps = boatData.sensors.gps;

        // Race-finish state. legsCompletedAt() filters by playback time,
        // so a boat is "finished" only once the cursor passes its actual
        // finish-line crossing.
        const totalLegs = currentRace?._totalLegs ?? courseSeq.length;
        const liveLegs = layer ? legsCompletedAt(layer, targetTimeMs) : 0;
        const finished = courseDefined && totalLegs > 0 && liveLegs >= totalLegs;
        const finishTimeMs = (finished && layer?.roundingTimes && layer.roundingTimes[totalLegs - 1] != null)
            ? layer.roundingTimes[totalLegs - 1]
            : null;

        // Freeze: when finished, evaluate every per-boat metric at the
        // finish-line moment instead of the playback cursor. The boat's
        // ranking, speed, TWA, %pol, VMG all stop changing the instant
        // it crosses. Map marker keeps moving (the boat physically
        // continues sailing); the leaderboard does not.
        const evalTimeMs = (finished && finishTimeMs != null) ? finishTimeMs : targetTimeMs;
        let idx;
        let point;
        if (finished && finishTimeMs != null && layer?.times) {
            idx = gpsIdxAt(layer, finishTimeMs) ?? (gps.length - 1);
            point = layer.data?.[idx] || gps[idx] || gps[gps.length - 1];
        } else {
            idx = layer?.currentIdx ?? (gps.length - 1);
            point = layer?.current || gps[gps.length - 1];
        }
        const windNow = windAt(evalTimeMs);

        // Distance-only fallback for races without a defined course.
        const cumDistM = (layer?.cumDist && layer.cumDist[idx] !== undefined)
            ? layer.cumDist[idx] : 0;

        // Course-aware metrics (only when a course sequence is defined)
        let legsCompleted = liveLegs;
        let progressM = cumDistM;
        let vmg = null;
        let distToNext = null;
        let nextMarkName = null;
        if (courseDefined && layer && point && point.lat && point.lon) {
            progressM = progressMetersAt(point, courseSeq, marksById, legsCompleted,
                                         startAnchor || { lat: gps[0].lat, lon: gps[0].lon });
            if (legsCompleted < totalLegs) {
                const target = marksById[courseSeq[legsCompleted % courseSeq.length]];
                if (target) {
                    nextMarkName = target.name || target.mark_type || `Mark ${legsCompleted + 1}`;
                    distToNext = haversineMeters(point.lat, point.lon, target.lat, target.lon);
                    const brg = bearingDegrees(point.lat, point.lon, target.lat, target.lon);
                    const sog = point.speed_kn || 0;
                    const cog = point.course || 0;
                    // Signed angle (-180..180) from heading to mark bearing.
                    const angleDiff = ((brg - cog + 540) % 360) - 180;
                    vmg = sog * Math.cos(angleDiff * Math.PI / 180);
                }
            }
        }

        // Identity priority: boat_name → team_name → sail# → device ID.
        // Boat name is the anchor across races (same boat, possibly
        // different skipper); device id only appears when no human-
        // friendly identifier is on file.
        const team = (boat?.team_name || '').trim();
        const boatName = (boat?.boat_name || '').trim();
        const sailNumber = (boat?.sail_number != null ? String(boat.sail_number) : '').trim();

        let displayName;
        const idBits = [];
        if (boatName) {
            displayName = boatName;
            if (team) idBits.push(team);
            if (sailNumber) idBits.push(`#${sailNumber}`);
        } else if (team) {
            displayName = team;
            if (sailNumber) idBits.push(`#${sailNumber}`);
        } else if (sailNumber) {
            displayName = `#${sailNumber}`;
        } else {
            displayName = deviceId;
        }
        const subtitle = idBits.join(' · ');

        // True Wind Angle (signed, port = negative). Requires NOAA wind.
        let twa = null;
        let polarPct = null;
        if (windNow && point) {
            const cog = point.course || 0;
            twa = ((windNow.twd - cog + 540) % 360) - 180;
            polarPct = polarPercent(point.speed_kn || 0, twa, windNow.tws);
        }

        const finishElapsedSec = (finished && finishTimeMs != null)
            ? (finishTimeMs - startTimeMs) / 1000
            : null;

        positions.push({
            deviceId,
            displayName,
            subtitle,
            speed: point?.speed_kn || 0,
            heading: point?.course || 0,
            cumDistM,
            progressM,
            legsCompleted,
            distToNext,
            nextMarkName,
            vmg,
            twa,
            polarPct,
            finished,
            finishTimeMs,
            finishElapsedSec,
            gapSec: null,    // filled in below for finished boats
            gap: null,       // filled in below
        });
    }

    // Rank: course-aware uses (legsCompleted DESC, then finishTime ASC for
    // boats that finished, then distToNext ASC for boats still racing).
    // Fallback (no course) is cumulative distance traveled.
    if (courseDefined) {
        positions.sort((a, b) => {
            if (b.legsCompleted !== a.legsCompleted) return b.legsCompleted - a.legsCompleted;
            if (a.finished && b.finished) {
                const ta = a.finishTimeMs ?? Infinity;
                const tb = b.finishTimeMs ?? Infinity;
                if (ta !== tb) return ta - tb;
            }
            const da = a.distToNext ?? Infinity;
            const db = b.distToNext ?? Infinity;
            return da - db;
        });
    } else {
        positions.sort((a, b) => b.cumDistM - a.cumDistM);
    }

    // Gap: along-course meters behind leader (course defined) or behind in
    // raw distance traveled (fallback). Leader keeps gap=null and the
    // renderer turns that into "LEAD". For finished boats we also fill
    // gapSec relative to the winner's finish moment — that's the
    // canonical sailing scoreboard delta ("+0:25 behind") that the
    // distance gap can't express once both boats have crossed.
    if (positions.length > 0) {
        const leader = positions[0];
        const baseField = courseDefined ? 'progressM' : 'cumDistM';
        const leaderFinishMs = leader.finished ? leader.finishTimeMs : null;
        for (let i = 1; i < positions.length; i++) {
            positions[i].gap = leader[baseField] - positions[i][baseField];
            if (positions[i].finished && leaderFinishMs != null && positions[i].finishTimeMs != null) {
                positions[i].gapSec = (positions[i].finishTimeMs - leaderFinishMs) / 1000;
            }
        }
    }

    return positions;
}

// --- Speed Chart ---

// Vertical play-cursor line drawn over each comparison chart.
// The cursor X is `playCursorSeconds` (seconds from race start), matching
// the X axis of all three charts so a single global value drives all of them.
const playCursorPlugin = {
    id: 'playCursor',
    afterDatasetsDraw: (chart) => {
        const xScale = chart.scales.x;
        const yScale = chart.scales.y;
        if (!xScale || !yScale) return;
        if (playCursorSeconds < xScale.min || playCursorSeconds > xScale.max) return;
        const x = xScale.getPixelForValue(playCursorSeconds);
        const ctx = chart.ctx;
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(x, yScale.top);
        ctx.lineTo(x, yScale.bottom);
        ctx.lineWidth = 1;
        ctx.strokeStyle = 'rgba(255,255,255,0.7)';
        ctx.setLineDash([3, 3]);
        ctx.stroke();
        ctx.restore();
    },
};
if (typeof Chart !== 'undefined') Chart.register(playCursorPlugin);

const COMPARISON_CHART_OPTIONS = (yLabel, ySuggestedMin, ySuggestedMax, yTickFormat) => ({
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { intersect: false, mode: 'index' },
    plugins: {
        legend: { display: false },
        tooltip: { enabled: false },
    },
    scales: {
        x: {
            type: 'linear',
            title: { display: false },
            grid: { color: 'rgba(255,255,255,0.05)' },
            ticks: {
                color: '#666',
                callback: (v) => formatChartTime(v),
                maxTicksLimit: 6,
            },
        },
        y: {
            title: { display: true, text: yLabel, color: '#888', font: { size: 10 } },
            grid: { color: 'rgba(255,255,255,0.08)' },
            ticks: yTickFormat
                ? { color: '#888', callback: yTickFormat }
                : { color: '#888' },
            suggestedMin: ySuggestedMin,
            suggestedMax: ySuggestedMax,
        },
    },
});

// Heel sign convention on the E1 fleet: positive = starboard down,
// negative = port down. Render as "S 12°" / "P 8°" — same P/S acronyms
// as TWA on the map labels, so the user has one mental model for tack
// notation across the dashboard.
const heelTickFormatter = (v) => {
    if (v === 0) return '0°';
    return v > 0 ? `S ${v}°` : `P ${-v}°`;
};

function formatChartTime(seconds) {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
}

function initSpeedChart() {
    const speedCtx = document.getElementById('speed-chart').getContext('2d');
    speedChart = new Chart(speedCtx, {
        type: 'line',
        data: { datasets: [] },
        options: COMPARISON_CHART_OPTIONS('Speed (kn)', 0, 12),
    });

    const heelCtx = document.getElementById('heel-chart').getContext('2d');
    heelChart = new Chart(heelCtx, {
        type: 'line',
        data: { datasets: [] },
        options: COMPARISON_CHART_OPTIONS('Heel', -30, 30, heelTickFormatter),
    });

    // Wind chart uses NOAA TWD instead of per-boat AWS. Single curve.
    const windCtx = document.getElementById('wind-chart').getContext('2d');
    windChart = new Chart(windCtx, {
        type: 'line',
        data: { datasets: [] },
        options: COMPARISON_CHART_OPTIONS('TWD (°)', 0, 360),
    });
}

// Build a downsampled (time-seconds, value) series for one boat's sensor data.
function buildSeries(samples, valueField, raceStartMs, maxPoints = 240) {
    if (!samples?.length) return [];
    const step = Math.max(1, Math.floor(samples.length / maxPoints));
    const out = [];
    for (let i = 0; i < samples.length; i += step) {
        const s = samples[i];
        if (s == null) continue;
        const t = new Date(s.t).getTime();
        const v = s[valueField];
        if (v == null || Number.isNaN(v)) continue;
        out.push({ x: (t - raceStartMs) / 1000, y: v });
    }
    return out;
}

function updateSpeedChart() {
    if (!raceData?.boats || !speedChart) return;
    if (!currentRace) return;
    const raceStartMs = new Date(currentRace.start_time).getTime();

    const speedSets = [];
    const heelSets = [];
    const windSets = [];

    const togglesContainer = document.getElementById('speed-chart-toggles');
    togglesContainer.innerHTML = '';

    // Iterate device ids in lexical order so the chart toggles render in
    // a stable position across page loads — the user can build muscle
    // memory for "purple = E5" regardless of API response order, and the
    // chart-toggle row matches the per-marker label colors on the map.
    const orderedEntries = Object.entries(raceData.boats)
        .sort(([a], [b]) => a.localeCompare(b));
    for (const [deviceId, boatData] of orderedEntries) {
        if (boatData.error || !boatData.sensors?.gps?.length) continue;
        const color = colorFor(deviceId);
        const baseDataset = (data) => ({
            label: deviceId,
            data,
            borderColor: color,
            backgroundColor: color + '20',
            borderWidth: 1.5,
            pointRadius: 0,
            tension: 0.2,
            spanGaps: true,
        });

        speedSets.push(baseDataset(buildSeries(boatData.sensors.gps, 'speed_kn', raceStartMs)));

        // Polar target overlay (dashed, same color, lower opacity). Computed
        // per GPS sample using NOAA wind at that timestamp; downsampled to
        // ~240 points to match the speed line.
        if (polarOverlayVisible && weatherWindSamples.length) {
            const gpsArr = boatData.sensors.gps;
            const step = Math.max(1, Math.floor(gpsArr.length / 240));
            const polarPts = [];
            for (let i = 0; i < gpsArr.length; i += step) {
                const p = gpsArr[i];
                if (!p?.t) continue;
                const t = new Date(p.t).getTime();
                const w = windAt(t);
                if (!w) continue;
                const cog = p.course || 0;
                const twa = ((w.twd - cog + 540) % 360) - 180;
                const target = polarTargetSpeed(twa, w.tws);
                if (target != null && target > 0) {
                    polarPts.push({ x: (t - raceStartMs) / 1000, y: target });
                }
            }
            if (polarPts.length) {
                speedSets.push({
                    label: deviceId + ':polar',
                    data: polarPts,
                    borderColor: color + '88',  // 50% alpha
                    backgroundColor: 'transparent',
                    borderWidth: 1,
                    borderDash: [4, 3],
                    pointRadius: 0,
                    tension: 0.2,
                    spanGaps: false,
                });
            }
        }

        heelSets.push(baseDataset(buildSeries(boatData.sensors.imu, 'heel', raceStartMs)));
        // Wind chart: not per-boat. Filled from NOAA below; per-boat dataset
        // here is just a placeholder so the toggle dot still affects this
        // chart visually (kept hidden). Avoids visual clutter.
        windSets.push({ ...baseDataset([]), hidden: true });

        // One shared toggle controls all three charts for this boat.
        // Now displays the team initials inside the colored chip so a
        // user can map line color → boat without consulting the
        // leaderboard. Hover tooltip still shows the full team / boat name.
        const toggle = document.createElement('button');
        toggle.className = 'chart-toggle';
        toggle.style.borderColor = color;
        toggle.style.background = color;
        const team = boatData.boat?.team_name;
        const boatName = boatData.boat?.boat_name;
        const initials = teamInitials(boatName || team || deviceId);
        toggle.textContent = initials;
        toggle.title = team && boatName
            ? `${team} — ${boatName}`
            : (team || boatName || deviceId);
        toggle.addEventListener('click', () => {
            const sNew = !speedChart.data.datasets.find(d => d.label === deviceId)?.hidden;
            for (const ch of [speedChart, heelChart, windChart]) {
                const ds = ch.data.datasets.find(d => d.label === deviceId);
                if (ds) ds.hidden = sNew;
                ch.update('none');
            }
            toggle.classList.toggle('disabled', sNew);
        });
        togglesContainer.appendChild(toggle);
    }

    speedChart.data.datasets = speedSets;
    heelChart.data.datasets = heelSets;

    // Wind chart: NOAA Castle Island (or fallback) TWD as a single white
    // curve over the race window. Independent of any boat's onboard sensor.
    if (weatherWindSamples.length) {
        const twdSeries = weatherWindSamples
            .filter(s => s.tMs >= raceStartMs)
            .map(s => ({ x: (s.tMs - raceStartMs) / 1000, y: s.twd }));
        windSets.push({
            label: '__noaa_twd__',
            data: twdSeries,
            borderColor: '#e2e8f0',
            backgroundColor: 'transparent',
            borderWidth: 2,
            pointRadius: 0,
            tension: 0.1,
            spanGaps: false,
        });
    }
    windChart.data.datasets = windSets;

    // Update the wind-chart label to reflect the source
    const windHeader = document.querySelector('#analytics-panel .chart-section:nth-child(3) h3');
    if (windHeader) {
        windHeader.textContent = weatherWindSource
            ? `Wind direction (${weatherWindSource})`
            : 'Wind direction (no NOAA data)';
    }

    speedChart.update();
    heelChart.update();
    windChart.update();
}

// --- Mobile UX (tabs + collapsible menu) ---
//
// Activated by CSS media query at <=900px width. JS only needs to:
//   - flip the active tab (CSS handles which panel is visible)
//   - close the slide-down menu on selection
//   - tell Leaflet/Chart.js to re-measure when becoming visible
//     (otherwise they render with the wrong dimensions because they
//      were sized while their parent had display:none).
function setupMobileNav() {
    document.body.dataset.mobileTab = 'map';

    // Tab switching
    const tabs = document.getElementById('mobile-tabs');
    if (tabs) {
        for (const btn of tabs.querySelectorAll('button')) {
            btn.addEventListener('click', () => {
                const t = btn.dataset.mtab;
                document.body.dataset.mobileTab = t;
                for (const b of tabs.querySelectorAll('button')) {
                    b.classList.toggle('active', b === btn);
                }
                // The charts now live inside the desktop overlay container;
                // mirror the open/close state to the overlay's `hidden`
                // attribute so the mobile "Charts" tab keeps working.
                const overlay = document.getElementById('charts-overlay');
                if (overlay) overlay.hidden = (t !== 'charts');
                // Force re-measure so Leaflet/Chart.js fill the now-visible panel
                requestAnimationFrame(() => {
                    if (t === 'map' && map) map.invalidateSize();
                    if (t === 'charts') {
                        for (const ch of [speedChart, heelChart, windChart]) {
                            if (ch) ch.resize();
                        }
                    }
                });
            });
        }
    }

    // Slide-down menu toggle
    const menuBtn = document.getElementById('mobile-menu-btn');
    if (menuBtn) {
        menuBtn.addEventListener('click', () => {
            document.body.classList.toggle('mobile-menu-open');
        });
    }

    // Auto-close the menu on any race-controls click (selectors / buttons),
    // so picking a race or pressing Legs dismisses the overlay naturally.
    const ctrls = document.getElementById('race-controls');
    if (ctrls) {
        ctrls.addEventListener('click', (e) => {
            if (e.target.closest('button, select') && document.body.classList.contains('mobile-menu-open')) {
                document.body.classList.remove('mobile-menu-open');
            }
        });
        ctrls.addEventListener('change', () => {
            if (document.body.classList.contains('mobile-menu-open')) {
                document.body.classList.remove('mobile-menu-open');
            }
        });
    }
}

// --- Per-boat detail drawer ---
let drawerDeviceId = null;
// When opening a non-GPS leaderboard row, we don't have a device_id —
// the drawer falls back to a profile-only view sourced from the
// catalog. drawerBoatId carries the catalog id for that case.
let drawerBoatId = null;

function nearestSampleAt(samples, targetMs) {
    if (!samples?.length) return null;
    let best = null, bestDiff = Infinity;
    for (const s of samples) {
        const t = s.t ? new Date(s.t).getTime() : null;
        if (t == null) continue;
        const d = Math.abs(t - targetMs);
        if (d < bestDiff) { bestDiff = d; best = s; }
    }
    // Reject very stale matches (>30 s away from playback time)
    return bestDiff < 30000 ? best : null;
}

function openBoatDrawer(deviceId) {
    drawerDeviceId = deviceId;
    drawerBoatId = null;
    const el = document.getElementById('boat-drawer');
    if (el) el.classList.add('open');
    updateBoatDrawer();
    renderLeaderboard();  // re-render to highlight active row
    // Reposition follow + polar ghost for the newly selected boat right
    // away (so they appear even while paused).
    if (currentRace) updateBoatPositions(playCursorSeconds);
}

// Open the drawer for a boat that has no GPS track this race. Same
// drawer panel, profile-only content (no live motion / wind / next-
// mark sections). Sourced from currentRace.boats[].
function openCatalogDrawer(boatId) {
    drawerDeviceId = null;
    drawerBoatId = boatId;
    const el = document.getElementById('boat-drawer');
    if (el) el.classList.add('open');
    updateBoatDrawer();
    renderLeaderboard();
}

function closeBoatDrawer() {
    drawerDeviceId = null;
    drawerBoatId = null;
    const el = document.getElementById('boat-drawer');
    if (el) el.classList.remove('open');
    hideGhost();           // ghost is selected-boat only
    renderLeaderboard();
}

function bearingToCardinal(deg) {
    const dirs = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                  'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
    return dirs[Math.round(((deg % 360) / 22.5)) % 16];
}

function fmt(v, digits = 1, suffix = '') {
    if (v == null || !Number.isFinite(v)) return '—';
    return `${v.toFixed(digits)}${suffix}`;
}

function updateBoatDrawer() {
    if (!drawerDeviceId && !drawerBoatId) return;
    const el = document.getElementById('boat-drawer');
    if (!el || !el.classList.contains('open')) return;

    // Catalog-only mode: no GPS this race, render just the profile +
    // race history. Drawer panel is the same; the live-metric
    // sections are simply omitted.
    if (drawerBoatId) {
        _renderCatalogOnlyDrawer(drawerBoatId);
        return;
    }

    const boatData = raceData?.boats?.[drawerDeviceId];
    const layer = boatLayers[drawerDeviceId];
    const point = layer?.current;
    if (!boatData || !point) {
        document.getElementById('drawer-body').innerHTML =
            '<div class="drawer-empty">No data at this time</div>';
        return;
    }

    const boat = boatData.boat;
    const team = (boat?.team_name || '').trim();
    const boatName = (boat?.boat_name || '').trim();
    const sailNumber = (boat?.sail_number != null ? String(boat.sail_number) : '').trim();
    // Identity priority: boat name → skipper → sail# → device ID.
    // Matches the leaderboard inversion — the boat is the more
    // identifying anchor across races.
    let titleMain;
    const titleBits = [];
    if (boatName) {
        titleMain = boatName;
        if (team) titleBits.push(team);
        if (sailNumber) titleBits.push(`#${sailNumber}`);
    } else if (team) {
        titleMain = team;
        if (sailNumber) titleBits.push(`#${sailNumber}`);
    } else if (sailNumber) {
        titleMain = `#${sailNumber}`;
    } else {
        titleMain = drawerDeviceId;
    }
    document.getElementById('drawer-title').innerHTML = `
        <span class="drawer-color-bar" style="background:${BOAT_COLORS[drawerDeviceId] || '#888'}"></span>
        <span class="drawer-team">${titleMain}</span>
        ${titleBits.length ? `<span class="drawer-boat">${titleBits.join(' · ')}</span>` : ''}
        <span class="drawer-device">(${drawerDeviceId})</span>
    `;

    const targetMs = new Date(point.t).getTime();
    const imu = nearestSampleAt(boatData.sensors?.imu, targetMs);
    const ownWind = nearestSampleAt(boatData.sensors?.wind, targetMs);
    const noaa = windAt(targetMs);

    const sog = point.speed_kn || 0;
    const cog = point.course || 0;
    const twa = noaa ? (((noaa.twd - cog + 540) % 360) - 180) : null;
    // Use THIS boat's own loaded VPP polar (from its ORR-EZ cert) — not the
    // generic J/80 table — so Target/%polar reflect the actual boat. When no
    // boat polar is loaded we hide the section entirely rather than show a
    // misleading J/80 default.
    const hasPolar = !!(boat?.polar && (boat.polar.nospin || boat.polar.spin));
    const polarLabel = (boat?.boat_type || '').trim() || 'VPP';
    const polTarget = hasPolar ? _polarSpeedKn(boat.polar, twa, noaa?.tws) : null;
    const polPct = (polTarget && polTarget > 0.1 && Number.isFinite(sog)) ? (sog / polTarget) * 100 : null;
    const tack = twa == null ? '—' : (twa < 0 ? 'Port' : 'Starboard');

    // Course-aware
    const courseSeq = currentRace?.course || [];
    const marksById = buildMarksById(currentRace);
    const startAnchor = startMidpoint(currentRace);
    const legsCompleted = legsCompletedAt(layer, targetMs);
    let nextMarkBlock = '';
    const totalLegsDrawer = currentRace?._totalLegs ?? courseSeq.length;
    if (courseSeq.length && legsCompleted < totalLegsDrawer) {
        const target = marksById[courseSeq[legsCompleted % courseSeq.length]];
        if (target && point.lat && point.lon) {
            const distToNext = haversineMeters(point.lat, point.lon, target.lat, target.lon);
            const brg = bearingDegrees(point.lat, point.lon, target.lat, target.lon);
            const angleDiff = ((brg - cog + 540) % 360) - 180;
            const vmgToMark = sog * Math.cos(angleDiff * Math.PI / 180);
            const ttm = (vmgToMark > 0.1)
                ? `${(distToNext / (vmgToMark * 0.5144)).toFixed(0)} s`
                : '—';
            nextMarkBlock = `
                <div class="drawer-section">
                    <div class="drawer-section-title">Next mark · ${target.name || target.mark_type || ('Mark ' + (legsCompleted + 1))}</div>
                    <div class="drawer-grid">
                        <div class="drawer-stat"><div class="drawer-label">Distance</div><div class="drawer-value">${fmt(distToNext, 0, ' m')}</div></div>
                        <div class="drawer-stat"><div class="drawer-label">Bearing</div><div class="drawer-value">${fmt(brg, 0, '°')} <span class="drawer-sub">${bearingToCardinal(brg)}</span></div></div>
                        <div class="drawer-stat"><div class="drawer-label">VMG</div><div class="drawer-value">${vmgToMark >= 0 ? '+' : ''}${fmt(vmgToMark, 1, ' kn')}</div></div>
                        <div class="drawer-stat"><div class="drawer-label">ETA</div><div class="drawer-value">${ttm}</div></div>
                    </div>
                </div>
            `;
        }
    }

    const ownWindBlock = ownWind ? `
        <div class="drawer-section">
            <div class="drawer-section-title">Onboard wind (Calypso)</div>
            <div class="drawer-grid">
                <div class="drawer-stat"><div class="drawer-label">AWS</div><div class="drawer-value">${fmt(ownWind.aws_kn, 1, ' kn')}</div></div>
                <div class="drawer-stat"><div class="drawer-label">AWA</div><div class="drawer-value">${fmt(ownWind.awa, 0, '°')}</div></div>
            </div>
        </div>
    ` : '';

    // Optional profile block from the boat catalog (photos, type, LOA,
    // skipper, links). Only renders if any catalog data is present —
    // legacy fleet races without boat_id show the live-data drawer
    // exactly as before.
    const raceBoat = (currentRace?.boats || []).find(b => b.device_id === drawerDeviceId);
    const profileBlock = _drawerProfileBlock(raceBoat);

    // Track quality — GPS sample rate over this boat's racing window. Makes
    // it obvious when a sparse track can't support tack/maneuver analysis.
    const dWin = racingWindowMsFor(boat, layer);
    const dHz = gpsHzFor(layer, dWin.startMs, dWin.endMs);
    const dHzNote = (dHz != null && dHz < 0.9)
        ? '⚠ Too coarse for reliable tack / maneuver analysis — needs ≈1 Hz or finer.'
        : '';
    const trackBlock = `
        <div class="drawer-section">
            <div class="drawer-section-title">Track quality</div>
            <div class="drawer-grid">
                <div class="drawer-stat"><div class="drawer-label">GPS rate</div><div class="drawer-value ${gpsHzClass(dHz)}">${fmtGpsHz(dHz)}</div></div>
                <div class="drawer-stat"><div class="drawer-label">Points</div><div class="drawer-value">${(layer?.data?.length || 0).toLocaleString()}</div></div>
            </div>
            ${dHzNote ? `<div class="drawer-track-note">${dHzNote}</div>` : ''}
        </div>`;

    document.getElementById('drawer-body').innerHTML = `
        ${profileBlock}
        <div class="drawer-section">
            <div class="drawer-section-title">Motion</div>
            <div class="drawer-grid">
                <div class="drawer-stat"><div class="drawer-label">SOG</div><div class="drawer-value drawer-strong">${fmt(sog, 1, ' kn')}</div></div>
                <div class="drawer-stat"><div class="drawer-label">COG</div><div class="drawer-value">${fmt(cog, 0, '°')} <span class="drawer-sub">${bearingToCardinal(cog)}</span></div></div>
                <div class="drawer-stat"><div class="drawer-label">Heel</div><div class="drawer-value">${
                    imu?.heel != null && Number.isFinite(imu.heel)
                        ? `${imu.heel >= 0 ? 'S' : 'P'} ${Math.round(Math.abs(imu.heel))}°`
                        : '—'
                }</div></div>
                <div class="drawer-stat"><div class="drawer-label">Pitch</div><div class="drawer-value">${fmt(imu?.pitch, 0, '°')}</div></div>
            </div>
        </div>
        ${trackBlock}
        ${ownWindBlock}
        <div class="drawer-section">
            <div class="drawer-section-title">True wind${weatherWindSource ? ' · ' + weatherWindSource : ''}</div>
            <div class="drawer-grid">
                <div class="drawer-stat"><div class="drawer-label">TWD</div><div class="drawer-value">${fmt(noaa?.twd, 0, '°')} <span class="drawer-sub">${noaa ? bearingToCardinal(noaa.twd) : ''}</span></div></div>
                <div class="drawer-stat"><div class="drawer-label">TWS</div><div class="drawer-value">${fmt(noaa?.tws, 1, ' kn')}</div></div>
                <div class="drawer-stat"><div class="drawer-label">TWA</div><div class="drawer-value">${twa == null ? '—' : `${tack[0]} ${Math.abs(twa).toFixed(0)}°`}</div></div>
                <div class="drawer-stat"><div class="drawer-label">Tack</div><div class="drawer-value">${tack}</div></div>
            </div>
        </div>
        ${hasPolar ? `
        <div class="drawer-section">
            <div class="drawer-section-title">Polar (${_attrEsc(polarLabel)}) · <a href="#" class="cert-link drawer-polar-plot-link">📈 plot</a>${
                boat?.cert_url
                    ? ` · <a href="${_attrEsc(boat.cert_url)}" target="_blank" rel="noopener" class="cert-link">🏷 cert ↗</a>`
                    : ''
            }</div>
            <div class="drawer-grid">
                <div class="drawer-stat"><div class="drawer-label">Target</div><div class="drawer-value">${fmt(polTarget, 1, ' kn')}</div></div>
                <div class="drawer-stat"><div class="drawer-label">% polar</div><div class="drawer-value drawer-strong">${fmt(polPct, 0, '%')}</div></div>
            </div>
        </div>` : ''}
        ${nextMarkBlock}
    `;

    // Wire the in-app "polar plot" link in the Polar header. The cert link
    // is a plain external anchor and needs no JS. Re-queried each render
    // (the drawer re-renders every playback tick).
    const _ppLink = document.querySelector('#drawer-body .drawer-polar-plot-link');
    if (_ppLink) _ppLink.addEventListener('click', (e) => { e.preventDefault(); openPolarOverlay(); });
}

// Catalog-only drawer renderer: opens for boats with no GPS this
// race. Shows the same profile section as the GPS path plus the
// per-race result row (corrected/elapsed/place/status); skips
// motion/wind/polar/next-mark since there's no live data to populate
// them with.
function _renderCatalogOnlyDrawer(boatId) {
    const raceBoat = (currentRace?.boats || []).find(b => b.boat_id === boatId);
    if (!raceBoat) {
        document.getElementById('drawer-body').innerHTML =
            '<div class="drawer-empty">Boat not found in this race.</div>';
        return;
    }

    const boatName = (raceBoat.boat_name || '').trim();
    const team = (raceBoat.team_name || '').trim();
    const sailNumber = (raceBoat.sail_number != null ? String(raceBoat.sail_number) : '').trim();

    let titleMain;
    const titleBits = [];
    if (boatName) {
        titleMain = boatName;
        if (team) titleBits.push(team);
        if (sailNumber) titleBits.push(`#${sailNumber}`);
    } else if (team) {
        titleMain = team;
        if (sailNumber) titleBits.push(`#${sailNumber}`);
    } else {
        titleMain = sailNumber ? `#${sailNumber}` : 'Boat';
    }

    document.getElementById('drawer-title').innerHTML = `
        <span class="drawer-color-bar" style="background:rgba(255,255,255,0.18)"></span>
        <span class="drawer-team">${_attrEsc(titleMain)}</span>
        ${titleBits.length ? `<span class="drawer-boat">${_attrEsc(titleBits.join(' · '))}</span>` : ''}
        <span class="drawer-device">(no GPS this race)</span>
    `;

    const profileBlock = _drawerProfileBlock(raceBoat);

    // Per-race result row sourced straight from the PHRF computation
    // — when we have rating + finish_time, render corrected / elapsed /
    // place. Otherwise just status (DNC, RET, …).
    const resultBlock = _catalogDrawerResultBlock(raceBoat);

    document.getElementById('drawer-body').innerHTML = `
        ${profileBlock || '<div class="drawer-empty">No catalog data for this boat yet — add it on the <a href="/boats.html" target="_blank" rel="noopener">Boats page</a>.</div>'}
        ${resultBlock}
    `;
}

function _catalogDrawerResultBlock(raceBoat) {
    const cls = (currentRace?.classes || []).find(c => c.id === raceBoat.class);
    const startMs = cls ? _parseTimeToMs(cls.start_time) : null;
    const finishMs = _parseTimeToMs(raceBoat.finish_time);
    const rating = Number(raceBoat.rating);
    const status = (raceBoat.finish_status || (finishMs ? 'FIN' : 'DNS')).toUpperCase();

    let elapsedSec = null, correctedSec = null;
    if (status === 'FIN' && startMs != null && finishMs != null) {
        const ownStart = raceBoat.pursuit_start ? _parseTimeToMs(raceBoat.pursuit_start) : null;
        elapsedSec = (finishMs - (ownStart != null ? ownStart : startMs)) / 1000;
        if (Number.isFinite(raceBoat.corrected_sec)) correctedSec = raceBoat.corrected_sec;
        else if (Number.isFinite(rating) && rating > 0) correctedSec = elapsedSec * rating;
    }

    // Place: re-derive within class using the same logic as the
    // PHRF leaderboard so the drawer stays consistent.
    let place = null;
    if (status === 'FIN' && correctedSec != null) {
        const peers = (currentRace?.boats || [])
            .filter(b => b.class === raceBoat.class && b.finish_status === 'FIN' && b.finish_time)
            .map(b => {
                const fM = _parseTimeToMs(b.finish_time);
                const r = Number(b.rating);
                return (fM != null && r > 0 && startMs != null)
                    ? { b, corr: (fM - startMs) / 1000 * r }
                    : null;
            })
            .filter(Boolean);
        peers.sort((a, b) => a.corr - b.corr);
        const idx = peers.findIndex(p => p.b === raceBoat);
        if (idx >= 0) place = idx + 1;
    }

    const rows = [];
    rows.push(['Status', status]);
    if (cls) rows.push(['Class', cls.name || cls.id]);
    if (rating > 0) rows.push(['Rating', rating.toFixed(3)]);
    if (place != null) rows.push(['Place in class', String(place)]);
    if (finishMs != null) rows.push(['Finish', _fmtLocalHMS(raceBoat.finish_time)]);
    if (elapsedSec != null) rows.push(['Elapsed', _fmtElapsedHMS(elapsedSec)]);
    if (correctedSec != null) rows.push(['Corrected', _fmtElapsedHMS(correctedSec)]);

    return `
        <div class="drawer-section">
            <div class="drawer-section-title">This race</div>
            <div class="drawer-grid">
                ${rows.map(([k, v]) => `
                    <div class="drawer-stat">
                        <div class="drawer-label">${_attrEsc(k)}</div>
                        <div class="drawer-value">${_attrEsc(v)}</div>
                    </div>
                `).join('')}
            </div>
        </div>
    `;
}

// Drawer profile block: photos + identity + skippers + LOA + links +
// ORR-EZ cert. Renders only when there's at least one catalog-sourced
// field; for legacy races (no boat_id) this returns '' so the drawer
// keeps its original live-data-only layout.
function _drawerProfileBlock(raceBoat) {
    if (!raceBoat) return '';
    const photos = raceBoat.photos || {};
    const links = raceBoat.links || [];
    // Use the new skippers[] array when present, falling back to the
    // legacy single string for older catalog docs.
    let skippers = Array.isArray(raceBoat.skippers) ? raceBoat.skippers : [];
    if (!skippers.length && raceBoat.skipper) {
        skippers = [{ name: raceBoat.skipper, photo: photos.skipper || photos.skipper1 || null }];
    }

    const hasCatalog = raceBoat.boat_id || photos.boat || skippers.length
        || raceBoat.loa_m != null || links.length || raceBoat.cert_url;
    if (!hasCatalog) return '';

    // Photo strip: boat first, then up to two skipper photos.
    const photoEls = [];
    if (photos.boat) photoEls.push(`<img class="drawer-photo" src="${_attrEsc(photos.boat)}" alt="Boat" title="Boat">`);
    for (const s of skippers) {
        if (s.photo) photoEls.push(`<img class="drawer-photo" src="${_attrEsc(s.photo)}" alt="${_attrEsc(s.name || 'Skipper')}" title="${_attrEsc(s.name || 'Skipper')}">`);
    }
    const photoStrip = photoEls.length ? `<div class="drawer-photos">${photoEls.join('')}</div>` : '';

    const meta = [];
    if (raceBoat.boat_type) meta.push(`<span class="drawer-meta-item">${_attrEsc(raceBoat.boat_type)}</span>`);
    if (raceBoat.loa_m) {
        const ft = _loaFeet(raceBoat.loa_m, 1);
        meta.push(`<span class="drawer-meta-item" title="${raceBoat.loa_m.toFixed(2)} m">${ft} ft LOA</span>`);
    }
    if (raceBoat.club) meta.push(`<span class="drawer-meta-item">${_attrEsc(raceBoat.club)}</span>`);
    const metaRow = meta.length ? `<div class="drawer-meta-row">${meta.join('')}</div>` : '';

    const skipperRow = skippers.length
        ? `<div class="drawer-skipper">${skippers.length > 1 ? 'Skippers' : 'Skipper'}: <strong>${
            skippers.map(s => _attrEsc(s.name || '—')).join(' &amp; ')
          }</strong></div>`
        : '';

    // Links list — combine the structured fields (cert, MBSA, VKX
    // report) with any user-defined links[]. Structured ones come
    // first as pills so they're visually anchored.
    const linkItems = [];
    if (raceBoat.cert_url) {
        linkItems.push(`<a href="${_attrEsc(raceBoat.cert_url)}" target="_blank" rel="noopener" class="cert-link">🏷 ORR-EZ Cert ↗</a>`);
    }
    if (raceBoat.mbsa_url) {
        linkItems.push(`<a href="${_attrEsc(raceBoat.mbsa_url)}" target="_blank" rel="noopener" class="cert-link">⚓ MBSA ↗</a>`);
    }
    if (raceBoat.vkx_report_url) {
        linkItems.push(`<a href="${_attrEsc(raceBoat.vkx_report_url)}" target="_blank" rel="noopener" class="cert-link vkx-report-link">📊 VKX report ↗</a>`);
    }
    for (const l of links) {
        if (l.url) linkItems.push(`<a href="${_attrEsc(l.url)}" target="_blank" rel="noopener">${_attrEsc(l.label || l.url)}</a>`);
    }
    const linksRow = linkItems.length ? `<div class="drawer-links">${linkItems.join(' · ')}</div>` : '';

    const editLink = raceBoat.boat_id
        ? `<a class="drawer-edit-boat" href="/boats.html?boat=${_attrEsc(raceBoat.boat_id)}" target="_blank" rel="noopener" title="Open the boat catalog page">Edit boat ↗</a>`
        : '';

    return `
        <div class="drawer-section drawer-profile">
            ${photoStrip}
            ${metaRow}
            ${skipperRow}
            ${linksRow}
            ${editLink}
        </div>
    `;
}

// --- Polar plot overlay (ORR-EZ live TWA vs SOG vs target) ---
//
// SVG polar diagram. 0° at top = head-to-wind, 180° at bottom =
// running. Speed scale = radius. Each boat with a polar shows a
// curve at the current TWS (interpolated between the two bracketing
// TWS columns in the cert). Every GPS-equipped boat's current
// (TWA, SOG) shows as a coloured dot, signed so port = left half
// of the diagram, starboard = right half. Updates per playback tick.

let polarOverlayOpen = false;
let polarSelectedKey = null;   // boat track key whose curve is drawn
// Trail window for the polar plot, in seconds. 0 = no trail.
// Persisted in localStorage so the coach's preferred setting survives
// reloads. Set via the polar-trail-select dropdown.
let polarTrailSeconds = (() => {
    try { return Number(localStorage.getItem('sf-polar-trail-sec') || 0) || 0; }
    catch { return 0; }
})();
// Constant TWD offset added to the wind source before computing TWA.
// Compensates for buoy-vs-racecourse wind bias, fast wind shifts the
// interpolator can't keep up with, and current-induced COG/heading
// split. Persisted globally; user adjusts via the polar-twd-nudge
// input or the "snap" button (auto-snap to class opt_beat angle).
let polarTWDNudgeDeg = (() => {
    try { return Number(localStorage.getItem('sf-polar-twd-nudge') || 0) || 0; }
    catch { return 0; }
})();

function openPolarOverlay() {
    const el = document.getElementById('polar-overlay');
    if (!el) return;
    el.hidden = false;
    polarOverlayOpen = true;
    _renderPolarFull();
}

function closePolarOverlay() {
    const el = document.getElementById('polar-overlay');
    if (el) el.hidden = true;
    polarOverlayOpen = false;
}

// Linearly interpolate `s_per_nm` between the two TWS columns that
// bracket the target TWS. Returns a speed in s/nm or null if the
// polar can't fulfill the request at that TWA.
function _polarSpeedSPerNm(polar, twaDeg, twsKn, prefer) {
    if (!polar || twsKn == null) return null;
    const twa = Math.min(180, Math.max(0, Math.abs(twaDeg)));
    const twsAxis = polar.tws_values || [];
    const twaAxis = polar.twa_values || [];
    if (!twsAxis.length || !twaAxis.length) return null;

    // Pick polar source: `spin` for TWA >= ~95°, `nospin` for upwind.
    // Fall back to whichever exists.
    const grid =
        (prefer === 'spin' && polar.spin) ? polar.spin :
        (prefer === 'nospin' && polar.nospin) ? polar.nospin :
        (twa >= 95 && polar.spin) ? polar.spin :
        polar.nospin || polar.spin;
    if (!grid) return null;

    // Find bracketing TWA values
    let twaLo = twaAxis[0], twaHi = twaAxis[twaAxis.length - 1];
    for (let i = 0; i < twaAxis.length - 1; i++) {
        if (twaAxis[i] <= twa && twa <= twaAxis[i+1]) { twaLo = twaAxis[i]; twaHi = twaAxis[i+1]; break; }
    }
    if (twa < twaAxis[0]) { twaLo = twaHi = twaAxis[0]; }
    if (twa > twaAxis[twaAxis.length-1]) { twaLo = twaHi = twaAxis[twaAxis.length-1]; }

    // Find bracketing TWS values
    let twsLo = twsAxis[0], twsHi = twsAxis[twsAxis.length - 1];
    for (let i = 0; i < twsAxis.length - 1; i++) {
        if (twsAxis[i] <= twsKn && twsKn <= twsAxis[i+1]) { twsLo = twsAxis[i]; twsHi = twsAxis[i+1]; break; }
    }
    if (twsKn < twsAxis[0]) { twsLo = twsHi = twsAxis[0]; }
    if (twsKn > twsAxis[twsAxis.length-1]) { twsLo = twsHi = twsAxis[twsAxis.length-1]; }

    function cell(twaV, twsV) {
        return grid[`${twaV},${twsV}`] ?? null;
    }
    const v00 = cell(twaLo, twsLo);
    const v01 = cell(twaLo, twsHi);
    const v10 = cell(twaHi, twsLo);
    const v11 = cell(twaHi, twsHi);
    if (v00 == null && v01 == null && v10 == null && v11 == null) return null;
    // Bilinear interpolation — fall back to whichever cells exist.
    const fT = (twaHi === twaLo) ? 0 : (twa - twaLo) / (twaHi - twaLo);
    const fW = (twsHi === twsLo) ? 0 : (twsKn - twsLo) / (twsHi - twsLo);
    const safe = (a, b, f) => (a == null ? b : (b == null ? a : a * (1 - f) + b * f));
    const lo = safe(v00, v10, fT);
    const hi = safe(v01, v11, fT);
    if (lo == null && hi == null) return null;
    return safe(lo, hi, fW);
}

function _polarSpeedKn(polar, twaDeg, twsKn) {
    const s = _polarSpeedSPerNm(polar, twaDeg, twsKn);
    return s ? 3600 / s : null;
}

// Look up the class's optimum close-hauled angle + speed at this TWS.
// polar.opt_beat is keyed by integer TWS strings ("8", "10", …) and
// stores {angle: degrees, vmg: s_per_nm}. Bilinear in TWS between the
// two bracketing keys; returns {angle, sog_kn} or null.
function _polarOptBeatAt(polar, twsKn) {
    const ob = polar?.opt_beat;
    if (!ob) return null;
    const keys = Object.keys(ob).map(Number).filter(Number.isFinite).sort((a,b) => a - b);
    if (!keys.length) return null;
    let lo = keys[0], hi = keys[keys.length - 1];
    for (let i = 0; i < keys.length - 1; i++) {
        if (keys[i] <= twsKn && twsKn <= keys[i+1]) { lo = keys[i]; hi = keys[i+1]; break; }
    }
    if (twsKn < keys[0]) { lo = hi = keys[0]; }
    if (twsKn > keys[keys.length - 1]) { lo = hi = keys[keys.length - 1]; }
    const a = ob[String(lo)], b = ob[String(hi)];
    if (!a && !b) return null;
    const f = (hi === lo) ? 0 : (twsKn - lo) / (hi - lo);
    const safe = (av, bv, ff) => (av == null ? bv : (bv == null ? av : av * (1 - ff) + bv * ff));
    const angle = safe(a?.angle, b?.angle, f);
    const vmg = safe(a?.vmg, b?.vmg, f);
    if (!Number.isFinite(angle) || !Number.isFinite(vmg) || vmg <= 0) return null;
    // VMG cell is the s/nm equivalent of the close-hauled speed component
    // *along the wind axis*. Boat-speed along the boat's heading is
    // VMG / cos(angle).
    const sogVmg = 3600 / vmg;
    const sog = sogVmg / Math.cos(angle * Math.PI / 180);
    return { angle, sog };
}

// Build a sampled polar curve at fixed TWS: array of (twaDeg, sogKn).
function _polarCurveAt(polar, twsKn) {
    if (!polar) return [];
    const out = [];
    for (let twa = 30; twa <= 170; twa += 2) {
        const sog = _polarSpeedKn(polar, twa, twsKn);
        if (sog) out.push({ twa, sog });
    }
    return out;
}

// Returns current (twaSigned, sog, tws) for every boat with GPS data
// at the current playback time. twaSigned ∈ [-180,180].
function _polarCurrentBoatStates() {
    if (!raceData?.boats) return [];
    const out = [];
    for (const [trackKey, boatData] of Object.entries(raceData.boats)) {
        const layer = boatLayers[trackKey];
        if (!layer?.current) continue;
        const p = layer.current;
        if (p.lat == null || p.lon == null) continue;
        const ts = p.t ? new Date(p.t).getTime() : null;
        // Only count a boat while it's actually racing — cursor within its
        // [gun, finish] window. Pre-start and post-finish drift don't belong
        // in the polar cloud.
        if (ts != null) {
            const win = racingWindowMsFor(boatData.boat, layer);
            if (ts < win.startMs || ts > win.endMs) continue;
        }
        const w = windAt(ts);
        if (!w) continue;
        const cog = p.course || 0;
        // Signed TWA: +stb / -port. Same convention as marker labels.
        // Apply the user's TWD nudge so the cluster can be aligned to
        // the class opt_beat reference when the wind source is biased.
        const twdAdj = w.twd + polarTWDNudgeDeg;
        const twa = ((twdAdj - cog + 540) % 360) - 180;
        const sog = p.speed_kn || 0;
        out.push({
            trackKey,
            twa,
            sog,
            tws: w.tws,
            twd: twdAdj,
            twdRaw: w.twd,
            boat: boatData.boat,
        });
    }
    return out;
}

// Build past-position trail for a boat in polar space. Walks the
// boat's GPS samples back `windowSec` seconds from playback time and
// converts each (sample, wind-at-sample-time) into a polar XY pair.
// Returns [{x_norm, y_norm, ageFrac}] where ageFrac=0 is the
// oldest, 1 is current (used for opacity gradient).
function _polarTrailFor(trackKey, windowSec, ptOf) {
    if (!windowSec || windowSec <= 0) return [];
    const boatData = raceData?.boats?.[trackKey];
    const gps = boatData?.sensors?.gps;
    if (!Array.isArray(gps) || !gps.length) return [];
    const layer = boatLayers[trackKey];
    const idxNow = layer?.currentIdx ?? (gps.length - 1);
    const tNow = gps[idxNow]?.t ? new Date(gps[idxNow].t).getTime() : null;
    if (tNow == null) return [];
    const tOldest = tNow - windowSec * 1000;
    const pts = [];
    // Step back through samples until we hit the window edge. Sub-
    // sample to ~120 points max (1 every windowSec/120 seconds) so a
    // 10-min trail renders cheaply.
    const stepSec = Math.max(1, Math.floor(windowSec / 120));
    let lastT = null;
    for (let i = idxNow; i >= 0; i--) {
        const p = gps[i];
        if (!p?.t) continue;
        const t = new Date(p.t).getTime();
        if (t < tOldest) break;
        if (lastT != null && (lastT - t) < stepSec * 1000) continue;
        lastT = t;
        const w = windAt(t);
        if (!w) continue;
        const cog = p.course || 0;
        const twa = (((w.twd + polarTWDNudgeDeg) - cog + 540) % 360) - 180;
        const sog = p.speed_kn || 0;
        const ageFrac = 1 - ((tNow - t) / (windowSec * 1000));
        const [x, y] = ptOf(twa, sog);
        pts.push({ x, y, ageFrac });
    }
    return pts.reverse();
}

function _renderPolarFull() {
    const svg = document.getElementById('polar-svg');
    if (!svg) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    // Centered polar: boat at the canvas center, 0° (head-to-wind)
    // pointing UP, 90° starboard to the RIGHT, 180° run pointing
    // DOWN, 270° = -90° port to the LEFT. Standard sailing convention.
    const W = 640, H = 600;
    const cx = W / 2, cy = H / 2;
    const radius = Math.min(W, H) / 2 - 50;

    const NS = 'http://www.w3.org/2000/svg';
    function mk(tag, attrs) {
        const e = document.createElementNS(NS, tag);
        for (const k in attrs) e.setAttribute(k, attrs[k]);
        return e;
    }

    const states = _polarCurrentBoatStates();
    const twsForScale = states.length
        ? (states.reduce((s, b) => s + (b.tws || 0), 0) / states.length)
        : 10;
    let maxKn = 6;
    for (const b of (currentRace?.boats || [])) {
        if (!b.polar) continue;
        for (let twa = 50; twa <= 170; twa += 10) {
            const v = _polarSpeedKn(b.polar, twa, twsForScale);
            if (v && v > maxKn) maxKn = v;
        }
    }
    for (const s of states) if (s.sog > maxKn) maxKn = s.sog;
    maxKn = Math.ceil(maxKn + 1);

    // SVG y-axis points DOWN. We want 0° TWA = up, so use -cos for y.
    function ptOf(twaSigned, sogKn) {
        const r = radius * Math.min(1, Math.max(0, sogKn) / maxKn);
        const a = twaSigned * Math.PI / 180;
        return [cx + r * Math.sin(a), cy - r * Math.cos(a)];
    }

    // --- Background grid ---
    for (let s = 2; s <= maxKn; s += 2) {
        const r = radius * (s / maxKn);
        svg.appendChild(mk('circle', {
            cx, cy, r, class: 'polar-grid-ring',
        }));
        // Speed labels along the north-south axis, slightly offset
        // so they don't overlap the centerline.
        svg.appendChild(mk('text', {
            x: cx + 4, y: cy - r + 11, class: 'polar-axis-label',
        })).textContent = `${s} kn`;
    }
    // Centerline spokes every 30°, plus extras at the typical
    // upwind / downwind tack angles.
    const SPOKE_TWAS = [-180, -150, -135, -120, -90, -60, -45, -30, 0, 30, 45, 60, 90, 120, 135, 150, 180];
    for (const twa of SPOKE_TWAS) {
        const [x, y] = ptOf(twa, maxKn);
        svg.appendChild(mk('line', {
            x1: cx, y1: cy, x2: x, y2: y, class: 'polar-grid-spoke',
        }));
        const labelText = (twa === 0) ? '0°\nhead-to-wind'
            : (twa === 180 || twa === -180) ? '180°\nrun'
            : `${Math.abs(twa)}°${twa < 0 ? 'P' : 'S'}`;
        const [lx, ly] = ptOf(twa, maxKn * 1.08);
        // Multi-line via tspan
        const t = mk('text', {
            x: lx, y: ly, class: 'polar-axis-label',
            'text-anchor': 'middle', 'dominant-baseline': 'middle',
        });
        const lines = labelText.split('\n');
        for (let i = 0; i < lines.length; i++) {
            const ts = mk('tspan', { x: lx, dy: i === 0 ? 0 : '1.05em' });
            ts.textContent = lines[i];
            t.appendChild(ts);
        }
        svg.appendChild(t);
    }

    // --- Selected boat's polar curve ---
    const selected = (currentRace?.boats || []).find(b => {
        const k = b.device_id || b.boat_id;
        return k === polarSelectedKey;
    }) || (currentRace?.boats || []).find(b => b.polar);
    if (selected && selected.polar) {
        polarSelectedKey = selected.device_id || selected.boat_id;
        const color = colorFor(polarSelectedKey);
        const curve = _polarCurveAt(selected.polar, twsForScale);
        if (curve.length > 1) {
            const stbPts = curve.map(({ twa, sog }) => ptOf(twa, sog));
            const portPts = curve.map(({ twa, sog }) => ptOf(-twa, sog));
            const stbD = 'M ' + stbPts.map(p => p.join(',')).join(' L ');
            const portD = 'M ' + portPts.map(p => p.join(',')).join(' L ');
            svg.appendChild(mk('path', { d: stbD, stroke: color, class: 'polar-curve' }));
            svg.appendChild(mk('path', { d: portD, stroke: color, class: 'polar-curve' }));
        }

        // Class beat-angle reference rays. opt_beat[tws] gives the
        // class's optimum close-hauled TWA + VMG (s/nm) at that TWS.
        // Boats sailing the class properly should sit on the dashed
        // rays; a 15° offset between cluster and rays means the wind
        // source is biased — dial the TWD nudge until they coincide.
        const beat = _polarOptBeatAt(selected.polar, twsForScale);
        if (beat && Number.isFinite(beat.angle) && Number.isFinite(beat.sog)) {
            const [sx, sy] = ptOf(beat.angle, beat.sog);
            const [px, py] = ptOf(-beat.angle, beat.sog);
            svg.appendChild(mk('line', {
                x1: cx, y1: cy, x2: sx, y2: sy,
                class: 'polar-beat-ray',
            }));
            svg.appendChild(mk('line', {
                x1: cx, y1: cy, x2: px, y2: py,
                class: 'polar-beat-ray',
            }));
            // Tiny "opt 42°" tick at each ray's outer end so the user
            // knows exactly what angle the dashed reference encodes.
            svg.appendChild(mk('text', {
                x: sx + 4, y: sy + 4, class: 'polar-beat-label',
            })).textContent = `opt ${beat.angle.toFixed(0)}°`;
            svg.appendChild(mk('text', {
                x: px - 4, y: py + 4, class: 'polar-beat-label',
                'text-anchor': 'end',
            })).textContent = `opt ${beat.angle.toFixed(0)}°`;
        }
    }

    // --- Trails (past polar positions) ---
    if (polarTrailSeconds > 0) {
        for (const s of states) {
            const pts = _polarTrailFor(s.trackKey, polarTrailSeconds, ptOf);
            if (pts.length < 2) continue;
            const color = colorFor(s.trackKey);
            // Render as a series of short segments with opacity
            // ramping from faded (oldest) to bright (newest). One
            // path per segment is heavier than a single polyline
            // but lets us vary opacity along its length.
            for (let i = 1; i < pts.length; i++) {
                const a = pts[i - 1], b = pts[i];
                const op = Math.max(0.05, b.ageFrac * 0.65);
                svg.appendChild(mk('line', {
                    x1: a.x, y1: a.y, x2: b.x, y2: b.y,
                    stroke: color, 'stroke-width': 1.6,
                    'stroke-opacity': op.toFixed(2),
                    'stroke-linecap': 'round',
                    class: 'polar-trail-seg',
                }));
            }
        }
    }

    // --- Live boat dots + heading arrows ---
    for (const s of states) {
        const [x, y] = ptOf(s.twa, s.sog);
        const c = colorFor(s.trackKey);
        const isSel = s.trackKey === polarSelectedKey;

        // Heading arrow — small triangle showing the boat's recent
        // motion in polar space (i.e. is the dot moving outward =
        // accelerating, inward = decelerating, clockwise = bearing
        // off, counter-clockwise = heading up). Direction comes
        // from the last ~3s of trail. If no trail data, point along
        // the radial (outward from origin).
        const recent = _polarTrailFor(s.trackKey, 3, ptOf);
        let dx = 0, dy = 0;
        if (recent.length >= 2) {
            const last = recent[recent.length - 1];
            const past = recent[0];
            dx = last.x - past.x;
            dy = last.y - past.y;
        }
        if (dx === 0 && dy === 0) {
            // Fallback: point radially outward (where the boat is
            // sailing TO on the polar).
            const a = s.twa * Math.PI / 180;
            dx = Math.sin(a);
            dy = -Math.cos(a);
        }
        const mag = Math.hypot(dx, dy) || 1;
        const ux = dx / mag, uy = dy / mag;
        const arrowLen = 14;
        const arrowWid = 6;
        const tipX = x + ux * arrowLen;
        const tipY = y + uy * arrowLen;
        const leftX = x + (-uy) * arrowWid;
        const leftY = y + (ux) * arrowWid;
        const rightX = x - (-uy) * arrowWid;
        const rightY = y - (ux) * arrowWid;
        svg.appendChild(mk('polygon', {
            points: `${tipX},${tipY} ${leftX},${leftY} ${rightX},${rightY}`,
            fill: c, 'fill-opacity': 0.85,
            stroke: '#fff', 'stroke-width': 0.5,
            'stroke-opacity': 0.5,
            class: 'polar-boat-arrow',
        }));

        svg.appendChild(mk('circle', {
            cx: x, cy: y, r: isSel ? 7 : 5,
            fill: c, class: 'polar-boat-dot' + (isSel ? ' selected' : ''),
            'data-track-key': s.trackKey,
        }));
        const initials = teamInitials(s.boat?.boat_name || s.boat?.team_name || '') || s.trackKey;
        svg.appendChild(mk('text', {
            x: x + 9, y: y + 3, class: 'polar-boat-label',
        })).textContent = initials;
    }

    // --- Header readout ---
    // Lists what's actually feeding the math: TWS, mean TWD across the
    // visible boats, wind source name, and the current nudge if any.
    // Without this, a biased buoy silently warps the whole picture.
    const readout = document.getElementById('polar-wind-readout');
    if (readout) {
        if (states.length) {
            const trailStr = polarTrailSeconds > 0
                ? ` · trail ${polarTrailSeconds < 60 ? polarTrailSeconds + 's' : (polarTrailSeconds / 60) + 'm'}`
                : '';
            // Circular mean TWD across boats (already includes nudge).
            let sx = 0, sy = 0;
            for (const s of states) {
                sx += Math.sin(s.twd * Math.PI / 180);
                sy += Math.cos(s.twd * Math.PI / 180);
            }
            const twdMean = (Math.atan2(sx, sy) * 180 / Math.PI + 360) % 360;
            const src = (typeof weatherWindSource === 'string' && weatherWindSource)
                ? weatherWindSource : 'wind';
            const nudgeStr = polarTWDNudgeDeg
                ? ` · nudge ${polarTWDNudgeDeg > 0 ? '+' : ''}${polarTWDNudgeDeg}°`
                : '';
            readout.textContent =
                `TWS ~${twsForScale.toFixed(1)} kn · TWD ${twdMean.toFixed(0)}° · ${src} · ${states.length} boat${states.length === 1 ? '' : 's'}${nudgeStr}${trailStr}`;
        } else {
            readout.textContent = 'No live boat data yet — start playback';
        }
    }

    _polarRebuildBoatSelector();
    _polarRebuildTrailSelector();
    _polarRebuildNudgeControl(states, selected);
    _polarRebuildLegend(states);

    svg.addEventListener('click', _polarHandleClick, { once: true });
}

// Wire the TWD nudge input + "snap" button. Snap computes the delta
// between the median |TWA| of close-hauled-looking boats and the
// selected boat's class opt_beat angle, and applies it to the nudge.
function _polarRebuildNudgeControl(states, selected) {
    const inp = document.getElementById('polar-twd-nudge');
    const snap = document.getElementById('polar-twd-snap');
    if (!inp || !snap) return;
    inp.value = String(polarTWDNudgeDeg);
    if (!inp.dataset.bound) {
        inp.dataset.bound = '1';
        inp.addEventListener('change', () => {
            const v = Math.max(-45, Math.min(45, Math.round(Number(inp.value) || 0)));
            polarTWDNudgeDeg = v;
            inp.value = String(v);
            try { localStorage.setItem('sf-polar-twd-nudge', String(v)); } catch {}
            _renderPolarFull();
        });
    }
    if (!snap.dataset.bound) {
        snap.dataset.bound = '1';
        snap.addEventListener('click', () => {
            const sel = selected || (currentRace?.boats || []).find(b => b.polar);
            const twsForScale = states.length
                ? (states.reduce((s, b) => s + (b.tws || 0), 0) / states.length)
                : 10;
            const beat = sel?.polar ? _polarOptBeatAt(sel.polar, twsForScale) : null;
            if (!beat) { alert('No class opt_beat to snap against.'); return; }
            // Use only boats whose current |TWA| looks close-hauled
            // (between 15° and 70°). Median is robust to one outlier
            // (a boat tacking, or just rounded a windward mark).
            const closeHauled = states
                .map(s => Math.abs(s.twa))
                .filter(a => a >= 15 && a <= 70)
                .sort((a, b) => a - b);
            if (!closeHauled.length) {
                alert('No close-hauled boats in view to snap against.');
                return;
            }
            const mid = closeHauled.length >> 1;
            const median = closeHauled.length % 2
                ? closeHauled[mid]
                : (closeHauled[mid - 1] + closeHauled[mid]) / 2;
            // We want median observed |TWA| to equal beat.angle, by
            // adjusting TWD. If observed |TWA| < beat.angle, TWD is
            // currently too tight to boats' headings → nudge OUTWARD.
            // The sign of the nudge depends on which tack dominates:
            // if the median port boat shows TWA=-25° vs opt -42°, we
            // need TWD lower (nudge negative) for port; on starboard
            // we need TWD higher. The set is mostly one-sided in
            // practice, so we pick the side with more boats.
            const portBoats = states.filter(s => s.twa < 0).length;
            const stbBoats = states.filter(s => s.twa > 0).length;
            const deltaMag = Math.round(beat.angle - median);
            const newNudge = polarTWDNudgeDeg
                + (portBoats >= stbBoats ? -deltaMag : +deltaMag);
            const clamped = Math.max(-45, Math.min(45, newNudge));
            polarTWDNudgeDeg = clamped;
            inp.value = String(clamped);
            try { localStorage.setItem('sf-polar-twd-nudge', String(clamped)); } catch {}
            _renderPolarFull();
        });
    }
}

function _polarRebuildTrailSelector() {
    const sel = document.getElementById('polar-trail-select');
    if (!sel) return;
    sel.value = String(polarTrailSeconds || 0);
    if (sel.dataset.bound) return;
    sel.dataset.bound = '1';
    sel.addEventListener('change', () => {
        polarTrailSeconds = Number(sel.value) || 0;
        try { localStorage.setItem('sf-polar-trail-sec', String(polarTrailSeconds)); } catch {}
        _renderPolarFull();
    });
}

function _polarHandleClick(e) {
    const t = e.target;
    if (!t?.dataset?.trackKey) return;
    polarSelectedKey = t.dataset.trackKey;
    _renderPolarFull();
}

function _polarRebuildBoatSelector() {
    const sel = document.getElementById('polar-boat-select');
    if (!sel) return;
    const boats = (currentRace?.boats || []).filter(b => b.polar);
    if (sel.dataset.populated === String(boats.length) && sel.options.length) {
        // Already populated for this race — just set value
        sel.value = polarSelectedKey || '';
        return;
    }
    sel.innerHTML = boats.map(b => {
        const k = b.device_id || b.boat_id || '';
        const name = b.boat_name || b.team_name || k;
        return `<option value="${_attrEsc(k)}">${_attrEsc(name)} — ${_attrEsc(b.boat_type || '')}</option>`;
    }).join('');
    sel.dataset.populated = String(boats.length);
    sel.value = polarSelectedKey || '';
    sel.onchange = () => {
        polarSelectedKey = sel.value || null;
        _renderPolarFull();
    };
}

function _polarRebuildLegend(states) {
    const el = document.getElementById('polar-legend');
    if (!el) return;
    el.innerHTML = states.map(s => {
        const c = colorFor(s.trackKey);
        const name = s.boat?.boat_name || s.boat?.team_name || s.trackKey;
        const tack = s.twa < 0 ? 'P' : 'S';
        const target = (() => {
            const cat = (currentRace?.boats || []).find(b =>
                (b.device_id || b.boat_id) === s.trackKey);
            const t = cat?.polar ? _polarSpeedKn(cat.polar, s.twa, s.tws) : null;
            return t ? `→ ${t.toFixed(1)} kn target = ${Math.round(s.sog / t * 100)}%` : '';
        })();
        return `<span class="polar-legend-item">
            <span class="polar-legend-swatch" style="background:${c}"></span>
            ${_attrEsc(name)} · ${s.sog.toFixed(1)}kn ${tack}${Math.abs(s.twa).toFixed(0)}° ${target}
        </span>`;
    }).join('');
}

// Per-playback-tick hook. Wired into updateBoatPositions so the
// plot moves while the timeline scrubs.
function refreshPolarOverlayIfOpen() {
    if (!polarOverlayOpen) return;
    _renderPolarFull();
}

// Close handlers
document.addEventListener('click', (e) => {
    if (!polarOverlayOpen) return;
    if (e.target.closest?.('[data-close-polar]')) closePolarOverlay();
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && polarOverlayOpen) closePolarOverlay();
});

// --- Report-table helpers ---

// Resolve a boat's sail number from the currently-loaded race.
// Returned as a printable string, or '' when no number is on file.
// Used by Legs / Maneuvers / Tack-Analysis tables to fill the
// "Sail #" column with a consistent value sourced from one place.
function sailNumberFor(deviceId) {
    if (!deviceId) return '';
    const sn = raceData?.boats?.[deviceId]?.boat?.sail_number;
    if (sn == null) return '';
    return String(sn).trim();
}

function sailNumberCell(deviceId) {
    return sailNumberFor(deviceId) || '—';
}

// --- Leg summary ---

function computeLegSummary() {
    if (!currentRace || !raceData?.boats) return [];
    const courseSeq = currentRace.course || [];
    if (!courseSeq.length) return [];
    const startTimeMs = new Date(currentRace.start_time).getTime();
    const rows = [];

    for (const [deviceId, boatData] of Object.entries(raceData.boats)) {
        const layer = boatLayers[deviceId];
        if (!layer?.data?.length || !layer.roundingTimes) continue;
        const team = boatData.boat?.boat_name || boatData.boat?.team_name || deviceId;

        // Boundaries for completed legs only — first leg starts at THIS
        // boat's class gun (not the generic race start), and we stop at
        // its finish (drop any roundings logged after it finished).
        const win = racingWindowMsFor(boatData.boat, layer);
        const bounds = [win.startMs];
        for (const t of layer.roundingTimes) {
            if (t !== undefined && t <= win.endMs) bounds.push(t); else break;
        }
        if (bounds.length < 2) continue;

        for (let leg = 0; leg < bounds.length - 1; leg++) {
            const tStart = bounds[leg], tEnd = bounds[leg + 1];
            const samples = layer.data.filter(p => {
                const t = new Date(p.t).getTime();
                return t >= tStart && t < tEnd && p.lat && p.lon;
            });
            if (samples.length < 2) continue;

            const sumSog = samples.reduce((s, p) => s + (p.speed_kn || 0), 0);
            const avgSog = sumSog / samples.length;

            let polSum = 0, polN = 0;
            for (const p of samples) {
                const t = new Date(p.t).getTime();
                const w = windAt(t);
                if (!w) continue;
                const twa = ((w.twd - (p.course || 0) + 540) % 360) - 180;
                const pp = polarPercent(p.speed_kn || 0, twa, w.tws);
                if (pp != null) { polSum += pp; polN++; }
            }
            const avgPol = polN > 0 ? polSum / polN : null;

            let dist = 0;
            for (let i = 1; i < samples.length; i++) {
                dist += haversineMeters(samples[i-1].lat, samples[i-1].lon, samples[i].lat, samples[i].lon);
            }

            rows.push({
                deviceId, team,
                leg: leg + 1,
                durationSec: (tEnd - tStart) / 1000,
                avgSog, avgPolPct: avgPol, distM: dist,
            });
        }
    }
    return rows;
}

function fmtMMSS(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '—';
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
}

function openLegModal() {
    const body = document.getElementById('leg-modal-body');
    if (!body) return;
    const rows = computeLegSummary();
    if (!rows.length) {
        body.innerHTML = '<div class="modal-empty">No leg data — race needs a defined course with mark roundings.</div>';
    } else {
        // Group by leg, then sort within each leg by duration ASC
        const byLeg = {};
        for (const r of rows) (byLeg[r.leg] = byLeg[r.leg] || []).push(r);
        const legs = Object.keys(byLeg).map(Number).sort((a, b) => a - b);

        let html = '';
        for (const leg of legs) {
            const group = byLeg[leg].sort((a, b) => a.durationSec - b.durationSec);
            const fastest = group[0]?.durationSec || 0;
            html += `<h3 class="leg-section-title">Leg ${leg}</h3>
                <table class="data-table">
                    <thead>
                        <tr><th>#</th><th>Team</th><th>Sail #</th><th>Time</th><th>Δ leader</th><th>Avg SOG</th><th>Avg %pol</th><th>Distance</th></tr>
                    </thead><tbody>`;
            group.forEach((r, i) => {
                const delta = r.durationSec - fastest;
                html += `<tr class="${i === 0 ? 'leg-leader' : ''}">
                    <td>${i + 1}</td>
                    <td><span class="lb-color" style="background:${BOAT_COLORS[r.deviceId] || '#888'}"></span>${r.team}</td>
                    <td>${sailNumberCell(r.deviceId)}</td>
                    <td>${fmtMMSS(r.durationSec)}</td>
                    <td>${i === 0 ? '—' : '+' + fmtMMSS(delta)}</td>
                    <td>${r.avgSog.toFixed(1)} kn</td>
                    <td>${r.avgPolPct != null ? r.avgPolPct.toFixed(0) + '%' : '—'}</td>
                    <td>${(r.distM / 1000).toFixed(2)} km</td>
                </tr>`;
            });
            html += '</tbody></table>';
        }
        body.innerHTML = html;
    }
    document.getElementById('leg-modal').style.display = 'flex';
}

// --- Maneuver detection (tacks & gybes) ---
//
// Heuristic, but tuned for J/80 racing on flat-ish water:
//   1. Walk each boat's GPS track. At every sample, compute the heading
//      change relative to ~10s earlier.
//   2. When |Δheading| > 60° within a 30s window AND speed in that
//      window dipped below 70% of the pre-window average, classify it
//      as a maneuver.
//   3. Use NOAA TWD at the entry to classify tack vs gybe by absolute
//      TWA before the maneuver (<90° upwind = tack, >90° downwind = gybe).

function detectManeuversForLayer(layer, useFleet = false) {
    const out = [];
    const data = layer.data;
    if (!data || data.length < 30) return out;

    const tMs = data.map(p => new Date(p.t).getTime());
    const speeds = data.map(p => p.speed_kn || 0);
    const cogs = data.map(p => p.course || 0);

    let i = 10;
    while (i < data.length - 30) {
        const before = speeds.slice(i - 10, i);
        const avgBefore = before.reduce((s, v) => s + v, 0) / before.length;
        if (avgBefore < 2) { i++; continue; }

        let maxDh = 0, endIdx = i;
        for (let j = i + 1; j < Math.min(data.length, i + 30); j++) {
            const dh = Math.abs(((cogs[j] - cogs[i] + 540) % 360) - 180);
            if (dh > maxDh) { maxDh = dh; endIdx = j; }
        }
        if (maxDh < 60) { i++; continue; }

        // Speed dip inside [i, endIdx]
        let minSpd = Infinity;
        for (let j = i; j <= endIdx; j++) if (speeds[j] < minSpd) minSpd = speeds[j];
        if (minSpd > avgBefore * 0.7) { i++; continue; }

        const after = speeds.slice(endIdx, Math.min(data.length, endIdx + 10));
        const avgAfter = after.reduce((s, v) => s + v, 0) / Math.max(1, after.length);

        const wEntry = analysisWindAt(tMs[i], useFleet);
        let type = 'rounding', twaBefore = null, twaAfter = null;
        if (wEntry) {
            const a = ((wEntry.twd - cogs[i] + 540) % 360) - 180;
            const b = ((wEntry.twd - cogs[endIdx] + 540) % 360) - 180;
            twaBefore = a; twaAfter = b;
            if (Math.abs(a) < 90 && Math.abs(b) < 90) type = 'tack';
            else if (Math.abs(a) > 90 && Math.abs(b) > 90) type = 'gybe';
            else type = 'rounding';
        }

        out.push({
            tStart: tMs[i], tEnd: tMs[endIdx],
            durationSec: (tMs[endIdx] - tMs[i]) / 1000,
            speedBefore: avgBefore, speedAfter: avgAfter, speedMin: minSpd,
            loss: Math.max(0, avgBefore - avgAfter),
            headingChange: maxDh,
            twaBefore, twaAfter, type,
        });
        i = endIdx + 10;  // skip past this maneuver
    }
    return out;
}

function openManeuverModal() {
    const body = document.getElementById('maneuver-modal-body');
    if (!body) return;
    if (!raceData?.boats) {
        body.innerHTML = '<div class="modal-empty">No race data loaded.</div>';
        document.getElementById('maneuver-modal').style.display = 'flex';
        return;
    }

    // Aggregate by team × type (tack / gybe)
    const summary = [];
    const allManeuvers = [];
    for (const [deviceId, boatData] of Object.entries(raceData.boats)) {
        const layer = boatLayers[deviceId];
        if (!layer?.data?.length) continue;
        const team = boatData.boat?.boat_name || boatData.boat?.team_name || deviceId;
        const win = racingWindowMsFor(boatData.boat, layer);
        const ms = detectManeuversForLayer(layer, true)
            .filter(m => m.tStart >= win.startMs && m.tStart <= win.endMs);
        for (const m of ms) allManeuvers.push({ deviceId, team, ...m });

        const tacks = ms.filter(m => m.type === 'tack');
        const gybes = ms.filter(m => m.type === 'gybe');
        summary.push({
            deviceId, team,
            tacksCount: tacks.length,
            tacksAvgLoss: tacks.length ? tacks.reduce((s, m) => s + m.loss, 0) / tacks.length : null,
            tacksAvgDur:  tacks.length ? tacks.reduce((s, m) => s + m.durationSec, 0) / tacks.length : null,
            gybesCount: gybes.length,
            gybesAvgLoss: gybes.length ? gybes.reduce((s, m) => s + m.loss, 0) / gybes.length : null,
            gybesAvgDur:  gybes.length ? gybes.reduce((s, m) => s + m.durationSec, 0) / gybes.length : null,
        });
    }

    if (!summary.length) {
        body.innerHTML = '<div class="modal-empty">No boat data.</div>';
        document.getElementById('maneuver-modal').style.display = 'flex';
        return;
    }

    summary.sort((a, b) => (a.tacksAvgLoss ?? 99) - (b.tacksAvgLoss ?? 99));

    let html = `<h3 class="maneuver-section-title">Per-team summary (sorted by tack loss)</h3>
        <table class="data-table">
            <thead>
                <tr><th>Team</th><th>Sail #</th>
                    <th>Tacks</th><th>Avg loss</th><th>Avg dur</th>
                    <th>Gybes</th><th>Avg loss</th><th>Avg dur</th></tr>
            </thead><tbody>`;
    for (const s of summary) {
        html += `<tr>
            <td><span class="lb-color" style="background:${BOAT_COLORS[s.deviceId] || '#888'}"></span>${s.team}</td>
            <td>${sailNumberCell(s.deviceId)}</td>
            <td>${s.tacksCount}</td>
            <td>${s.tacksAvgLoss != null ? s.tacksAvgLoss.toFixed(2) + ' kn' : '—'}</td>
            <td>${s.tacksAvgDur != null ? s.tacksAvgDur.toFixed(1) + ' s' : '—'}</td>
            <td>${s.gybesCount}</td>
            <td>${s.gybesAvgLoss != null ? s.gybesAvgLoss.toFixed(2) + ' kn' : '—'}</td>
            <td>${s.gybesAvgDur != null ? s.gybesAvgDur.toFixed(1) + ' s' : '—'}</td>
        </tr>`;
    }
    html += '</tbody></table>';

    // Per-maneuver detail
    allManeuvers.sort((a, b) => a.tStart - b.tStart);
    if (allManeuvers.length) {
        html += `<h3 class="maneuver-section-title" style="margin-top:1.5rem">Every maneuver, in order</h3>
            <table class="data-table">
                <thead>
                    <tr><th>Time</th><th>Team</th><th>Sail #</th><th>Type</th>
                        <th>Δ heading</th><th>TWA in → out</th>
                        <th>SOG before</th><th>min</th><th>after</th>
                        <th>Loss</th><th>Duration</th></tr>
                </thead><tbody>`;
        const raceStart = new Date(currentRace.start_time).getTime();
        for (const m of allManeuvers) {
            const elapsed = (m.tStart - raceStart) / 1000;
            const twaIn = m.twaBefore == null ? '—' : `${m.twaBefore < 0 ? 'P' : 'S'}${Math.abs(m.twaBefore).toFixed(0)}°`;
            const twaOut = m.twaAfter == null ? '—' : `${m.twaAfter < 0 ? 'P' : 'S'}${Math.abs(m.twaAfter).toFixed(0)}°`;
            const typeClass = m.type === 'tack' ? 'mtype-tack' : (m.type === 'gybe' ? 'mtype-gybe' : 'mtype-other');
            html += `<tr>
                <td>${fmtMMSS(elapsed)}</td>
                <td><span class="lb-color" style="background:${BOAT_COLORS[m.deviceId] || '#888'}"></span>${m.team}</td>
                <td>${sailNumberCell(m.deviceId)}</td>
                <td><span class="${typeClass}">${m.type}</span></td>
                <td>${m.headingChange.toFixed(0)}°</td>
                <td>${twaIn} → ${twaOut}</td>
                <td>${m.speedBefore.toFixed(1)}</td>
                <td>${m.speedMin.toFixed(1)}</td>
                <td>${m.speedAfter.toFixed(1)}</td>
                <td>${m.loss.toFixed(2)} kn</td>
                <td>${m.durationSec.toFixed(1)} s</td>
            </tr>`;
        }
        html += '</tbody></table>';
    }

    body.innerHTML = html;
    document.getElementById('maneuver-modal').style.display = 'flex';
}

// --- Tack analysis (wind-up overlay of every tack, color = SOG) ---
//
// For each detected tack we:
//   1. Take the slice of the boat's GPS track from ~15s before the maneuver
//      start through ~15s after the maneuver end (or until SOG recovers to
//      95% of pre-tack speed).
//   2. Find the apex (lowest SOG inside the maneuver window).
//   3. Translate so apex is at (0,0) and rotate so true wind is up.
//      Mirror port-tacks (twaBefore < 0) to the starboard side so all
//      tacks render as turning the same direction.
//   4. Compute "time lost (s)" = Σ max(0, speedBefore - speed) * dt /
//      speedBefore over the whole window.
//
// The lowest time-lost tack across the fleet is the "fleet best" reference;
// each boat also gets a personal-best.
//
// Tacks where windAt() returns null at the apex are dropped (we can't
// align them in a wind-up frame).

const TA_PRE_SEC = 15;          // raw window before tStart (trimmed by distance later)
const TA_POST_SEC = 30;         // raw window after tEnd (trimmed by distance later)
const TA_DIST_CAP_M = 20;       // hard cap on cumulative distance from apex, both sides
const TA_WIDE_TURN_DEG = 95;    // turn angle above this gets a "wide" badge

let tackAnalysisState = null;   // { tacks, selectedDevices: Set, highlightId, fleetBestId, perBoatBestId }

function buildTackTrack(tack, layer, useFleet = false) {
    // Returns { id, deviceId, team, color, points: [{x,y,speed,t}],
    //   timeLostSec, turnAngle, durationSec, speedBefore, speedMin,
    //   isPort, twd, isExcluded } or null if essential data missing.
    const data = layer.data;
    if (!data || data.length < 5) return null;
    const tMs = data.map(p => new Date(p.t).getTime());

    // On-course wind for the wind-up frame (fleet-inferred for the current
    // race, buoy fallback) — using the biased buoy here is what made real
    // tacks fail isCleanTack's geometry.
    const wAtApex = analysisWindAt((tack.tStart + tack.tEnd) / 2, useFleet);
    if (!wAtApex || wAtApex.twd == null) return null;
    const twd = wAtApex.twd;

    // Locate the apex (min speed inside [tStart, tEnd]).
    let apexIdx = -1, apexSpd = Infinity;
    for (let i = 0; i < data.length; i++) {
        if (tMs[i] < tack.tStart || tMs[i] > tack.tEnd) continue;
        const s = data[i].speed_kn ?? 0;
        if (s < apexSpd) { apexSpd = s; apexIdx = i; }
    }
    if (apexIdx < 0) return null;

    // Raw time window — generous; trimmed by cumulative distance later.
    const winStartMs = tack.tStart - TA_PRE_SEC * 1000;
    const winEndMs = tack.tEnd + TA_POST_SEC * 1000;
    const speedBefore = tack.speedBefore || 0;

    // Slice + project to a local east-north meters frame around the apex.
    const apex = data[apexIdx];
    const lat0 = apex.lat, lon0 = apex.lon;
    if (lat0 == null || lon0 == null) return null;
    const R = 6371000;
    const mPerDegLat = (Math.PI / 180) * R;
    const mPerDegLon = (Math.PI / 180) * R * Math.cos(lat0 * Math.PI / 180);

    // Wind-up rotation: rotate by +TWD (CCW) so the "wind from" bearing
    // maps to +y (up on the math frame; we'll flip y for SVG screen later).
    const twdRad = twd * Math.PI / 180;
    const cosT = Math.cos(twdRad), sinT = Math.sin(twdRad);

    // Build raw rotated points first; choose mirror by geometry, not by
    // the detector's twaBefore sign (which depends on the TWD sample that
    // was current at the maneuver — and that's the very thing we don't
    // want to trust here).
    const ptsRaw = [];
    for (let i = 0; i < data.length; i++) {
        if (tMs[i] < winStartMs || tMs[i] > winEndMs) continue;
        const p = data[i];
        if (p.lat == null || p.lon == null) continue;
        const dx_m = (p.lon - lon0) * mPerDegLon;   // east
        const dy_m = (p.lat - lat0) * mPerDegLat;   // north
        // Rotate by twdRad CCW: (dx, dy) -> (dx cosT - dy sinT, dx sinT + dy cosT)
        const x_rot = dx_m * cosT - dy_m * sinT;
        const y_rot = dx_m * sinT + dy_m * cosT;
        ptsRaw.push({ x: x_rot, y: y_rot, speed: p.speed_kn ?? 0, t: tMs[i] });
    }
    if (ptsRaw.length < 4) return null;

    // Geometry-based mirror: if the rotated cluster lives mostly on the
    // negative-x side, flip x. This guarantees every clean tack lands in
    // the +x half-plane, robust to noisy TWD at the apex.
    let xSum = 0;
    for (const p of ptsRaw) xSum += p.x;
    const xSign = xSum < 0 ? -1 : 1;
    const isPort = (tack.twaBefore != null && tack.twaBefore < 0);  // kept for label only

    const ptsAll = ptsRaw.map(p => ({ x: p.x * xSign, y: p.y, speed: p.speed, t: p.t }));

    // Apex inside the rotated+mirrored cluster: closest point to origin
    // (origin = apex by construction of lat0/lon0).
    let apexInAll = 0, apexAllDist = Infinity;
    for (let i = 0; i < ptsAll.length; i++) {
        const d = ptsAll[i].x * ptsAll[i].x + ptsAll[i].y * ptsAll[i].y;
        if (d < apexAllDist) { apexAllDist = d; apexInAll = i; }
    }

    // Distance-cap trim: bound the visible track to ±TA_DIST_CAP_M of
    // cumulative path length from the apex. This keeps the visualization
    // and the time-lost integral consistent across boats with different
    // recovery profiles, instead of letting slow recoverers run a 75 m+ tail.
    let preStart = 0, cumPre = 0;
    for (let i = apexInAll; i > 0; i--) {
        const dx = ptsAll[i].x - ptsAll[i - 1].x;
        const dy = ptsAll[i].y - ptsAll[i - 1].y;
        cumPre += Math.sqrt(dx * dx + dy * dy);
        if (cumPre > TA_DIST_CAP_M) { preStart = i; break; }
    }
    let postEnd = ptsAll.length, cumPost = 0;
    for (let i = apexInAll; i < ptsAll.length - 1; i++) {
        const dx = ptsAll[i + 1].x - ptsAll[i].x;
        const dy = ptsAll[i + 1].y - ptsAll[i].y;
        cumPost += Math.sqrt(dx * dx + dy * dy);
        if (cumPost > TA_DIST_CAP_M) { postEnd = i + 1; break; }
    }
    const pts = ptsAll.slice(preStart, postEnd);

    // Time-lost integral (uses real dt, doesn't assume 1 Hz).
    let deficitSum = 0;
    for (let i = 1; i < pts.length; i++) {
        const dt = (pts[i].t - pts[i - 1].t) / 1000;
        if (dt <= 0 || dt > 10) continue;  // skip gaps
        const s = (pts[i].speed + pts[i - 1].speed) / 2;
        const def = Math.max(0, speedBefore - s);
        deficitSum += def * dt;
    }
    const timeLostSec = speedBefore > 0.5 ? deficitSum / speedBefore : null;

    // Turn angle: averages of ~5s before tStart vs ~5s after tEnd in COG.
    function avgCogNear(targetMs, half = 5000) {
        let sx = 0, sy = 0, n = 0;
        for (let i = 0; i < data.length; i++) {
            if (Math.abs(tMs[i] - targetMs) > half) continue;
            const c = data[i].course;
            if (c == null) continue;
            const r = c * Math.PI / 180;
            sx += Math.sin(r); sy += Math.cos(r); n++;
        }
        if (n === 0) return null;
        return (Math.atan2(sx, sy) * 180 / Math.PI + 360) % 360;
    }
    const cogBefore = avgCogNear(tack.tStart - 2500);
    const cogAfter  = avgCogNear(tack.tEnd + 2500);
    let turnAngle = null;
    if (cogBefore != null && cogAfter != null) {
        turnAngle = Math.abs(((cogAfter - cogBefore + 540) % 360) - 180);
    }

    // Apex index inside the trimmed `pts`.
    const apexInPts = apexInAll - preStart;
    const tApexMs = pts[apexInPts].t;

    // Pre/post mean (x, y) in the wind-up + mirrored frame. A real upwind
    // tack: pre-window y < 0 (boat was downwind of apex), post-window y > 0
    // (boat sailed upwind after). Both windows should have x > 0 (in the +x
    // half-plane after our geometry-based mirror). Combined: catches
    // mark-rounding misclassifications, gybes-as-tacks, and TWD-was-90°-off
    // cases that the y-only check missed in the previous iteration.
    let preXsum = 0, preYsum = 0, preN = 0, postXsum = 0, postYsum = 0, postN = 0;
    let maxAbsX = 0;
    for (let i = 0; i < apexInPts; i++) { preXsum += pts[i].x; preYsum += pts[i].y; preN++; if (Math.abs(pts[i].x) > maxAbsX) maxAbsX = Math.abs(pts[i].x); }
    for (let i = apexInPts + 1; i < pts.length; i++) { postXsum += pts[i].x; postYsum += pts[i].y; postN++; if (Math.abs(pts[i].x) > maxAbsX) maxAbsX = Math.abs(pts[i].x); }
    const preXmean = preN > 0 ? preXsum / preN : null;
    const preYmean = preN > 0 ? preYsum / preN : null;
    const postXmean = postN > 0 ? postXsum / postN : null;
    const postYmean = postN > 0 ? postYsum / postN : null;

    // Symmetry: where the apex sits inside the maneuver window.
    //   0.5 = perfectly centred turn (clean execution)
    //   < 0.5 = apex closer to start (boat stalled entering the tack)
    //   > 0.5 = apex closer to end (boat dragged through exit)
    const tWindow = tack.tEnd - tack.tStart;
    const symmetry = tWindow > 0 ? (tApexMs - tack.tStart) / tWindow : null;

    // Max heel during the maneuver window (IMU). At ~1 Hz on the E-series,
    // a 6–10 s tack window has 6–10 samples — enough for a max. Top crews
    // flatten the boat through head-to-wind and don't carry heel into the
    // new tack, so a low max-heel discriminates execution quality.
    let maxHeel = null;
    if (layer.imu?.length) {
        let mh = 0, found = false;
        for (const s of layer.imu) {
            const t = s?.t ? new Date(s.t).getTime() : null;
            if (t == null) continue;
            if (t < tack.tStart || t > tack.tEnd) continue;
            const h = Math.abs(s.heel ?? 0);
            if (Number.isFinite(h)) { if (h > mh) mh = h; found = true; }
        }
        if (found) maxHeel = mh;
    }

    // Turn-rate σ: standard deviation of dCOG/dt across the maneuver
    // window. Smooth turn = small σ. Jerky steering = larger σ.
    const turnRates = [];
    for (let i = 1; i < data.length; i++) {
        const t1 = tMs[i - 1], t2 = tMs[i];
        if (t1 < tack.tStart || t2 > tack.tEnd) continue;
        const dt = (t2 - t1) / 1000;
        if (dt <= 0 || dt > 5) continue;
        const a = data[i - 1].course, b = data[i].course;
        if (a == null || b == null) continue;
        const dCog = ((b - a + 540) % 360) - 180;
        turnRates.push(dCog / dt);
    }
    let turnRateStd = null;
    if (turnRates.length >= 3) {
        const m = turnRates.reduce((s, v) => s + v, 0) / turnRates.length;
        const v = turnRates.reduce((s, x) => s + (x - m) ** 2, 0) / turnRates.length;
        turnRateStd = Math.sqrt(v);
    }

    // TWA exit-vs-entry: |twaAfter| - |twaBefore|. Positive = exited
    // wider than entered (sailing below close-hauled to build speed).
    // Negative = exited tighter (pinching). Close to 0 = symmetric crossing.
    const twaDelta = (tack.twaBefore != null && tack.twaAfter != null)
        ? (Math.abs(tack.twaAfter) - Math.abs(tack.twaBefore))
        : null;

    return {
        deviceId: tack.deviceId,
        team: tack.team,
        color: tack.color,
        points: pts,
        apexIdx: apexInPts,
        tApexMs,
        timeLostSec,
        turnAngle,
        symmetry,
        maxHeel,
        turnRateStd,
        twaDelta,
        durationSec: tack.durationSec,
        speedBefore,
        speedMin: apexSpd,
        isPort,
        twd,
        tStart: tack.tStart,
        preXmean, preYmean, postXmean, postYmean, maxAbsX,
    };
}

// Quality filter: drop the obviously-not-a-tack maneuvers that survive
// the broad detector (e.g. small course corrections, mark roundings,
// gybes that got misclassified because TWD was off at the time).
const TA_MIN_TURN = 70;     // a real tack should be ≥70° (typical 85-110°)
const TA_MAX_TURN = 150;    // >150° = circling / artifact
const TA_MIN_WINDWARD_GAIN_M = 2;  // post-apex must end this many m more upwind than pre
const TA_MIN_X_EXTENT_M = 3;       // horizontal extent floor (not a vertical line)
function isCleanTack(t) {
    if (t.turnAngle == null) return false;
    if (t.turnAngle < TA_MIN_TURN || t.turnAngle > TA_MAX_TURN) return false;
    if (t.preYmean == null || t.postYmean == null) return false;
    // RELATIVE windward-gain test (tolerant of residual wind-frame rotation).
    // The old version used strict absolute sign gates (preY<0 && postY>0 &&
    // preX>0 && postX>0); with the ~15-20° buoy-wind bias rotating the frame,
    // real tacks' borderline signs flipped and ~57% were wrongly excluded.
    // A real tack always nets WINDWARD through the turn: post-apex the boat
    // is more upwind (+y in the wind-up frame) than pre-apex. A bear-away or
    // leeward rounding does the opposite, so this still rejects non-tacks —
    // and gybes never reach here (classified out before isCleanTack).
    if ((t.postYmean - t.preYmean) < TA_MIN_WINDWARD_GAIN_M) return false;
    if (t.maxAbsX < TA_MIN_X_EXTENT_M) return false;
    return true;
}

function speedToColor(speed, maxSpeed) {
    // Legacy absolute-speed scale (slow = red, fast = blue). Kept
    // for any caller that wants a fleet-wide gauge; the tack-analysis
    // plot now uses boatSpeedColor() instead so each boat keeps its
    // team identity and the line brightness encodes % of entry speed.
    const f = Math.max(0, Math.min(1, speed / Math.max(0.1, maxSpeed)));
    return `hsl(${(f * 240).toFixed(0)}, 80%, 55%)`;
}

// ---------- HSL helpers for per-boat speed-modulated colours ----------
//
// We want each tack track on the analysis canvas to read as ITS BOAT's
// colour (the same one the leaderboard / map markers use), with line
// brightness changing to reflect speed. Hue stays fixed = identity;
// lightness varies = speed signal. Saturation also nudges down at low
// speed so very dim segments don't read as "different boat".
//
// We encode "% of entry speed" rather than absolute kn:
//   ratio = currentSpeed / speedBefore
// so the same plot tells you instantly whether a boat held its speed
// through the tack or hemorrhaged it — independent of whether they
// went in at 4 kn or 7 kn. That's the canonical tack-quality metric.

const _hexHslCache = {};
function hexToHsl(hex) {
    if (!hex) return { h: 0, s: 0, l: 50 };
    if (_hexHslCache[hex]) return _hexHslCache[hex];
    const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
    if (!m) { _hexHslCache[hex] = { h: 0, s: 0, l: 50 }; return _hexHslCache[hex]; }
    const r = parseInt(m[1], 16) / 255;
    const g = parseInt(m[2], 16) / 255;
    const b = parseInt(m[3], 16) / 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    let h, s; const l = (max + min) / 2;
    if (max === min) { h = 0; s = 0; }
    else {
        const d = max - min;
        s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
        switch (max) {
            case r: h = (g - b) / d + (g < b ? 6 : 0); break;
            case g: h = (b - r) / d + 2; break;
            default: h = (r - g) / d + 4;
        }
        h *= 60;
    }
    const result = { h: Math.round(h), s: Math.round(s * 100), l: Math.round(l * 100) };
    _hexHslCache[hex] = result;
    return result;
}

// Render one segment of a boat's track. `n` is a NORMALIZED speed
// position in [0, 1] computed per-track (0 = slowest segment in this
// track, 1 = fastest). Per-track normalization is what makes the
// variance visible — encoding absolute "% of entry speed" had a
// total dynamic range of ~15 L-points on team-avg data because the
// averaging dampens the peak, so the lines all looked the same
// brightness. With per-track stretching, every track uses the full
// range regardless of how narrow the underlying variance.
//
// Inverted ramp: FASTEST = DARKER. Tactically this is the right
// direction — the eye is drawn to the bright end, and the bright
// end now marks the SLOW segments (the problem moments where speed
// was lost). All three visual channels move together so the bright,
// opaque, thick line segments are the ones a coach wants to look at:
//   1. lightness 82 % → 18 %  (slow=bright, fast=dark)
//   2. opacity   1.0  → 0.55  (slow=opaque, fast=faded)
//   3. stroke    +1.8 → +0.0  (slow=thicker, fast=thinner)
function boatSpeedAttrs(deviceId, n) {
    const base = hexToHsl(colorFor(deviceId));
    const t = Math.max(0, Math.min(1, n));
    const L = 82 - t * 64;                              // 82..18 (inverted)
    const S = Math.max(45, base.s - (1 - t) * 25);      // ease saturation at the slow end (consistent with normal hue family)
    const opacity = (1.0 - t * 0.45).toFixed(2);        // 1.0..0.55 (inverted)
    const widthBoost = ((1 - t) * 1.8).toFixed(2);      // 1.8..0 (inverted)
    return {
        stroke: `hsl(${base.h}, ${S.toFixed(0)}%, ${L.toFixed(0)}%)`,
        opacity,
        widthBoost,
    };
}

function fmtTimeOfDay(ms) {
    const d = new Date(ms);
    return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`;
}

function openTackAnalysisModal() {
    const body = document.getElementById('tack-analysis-modal-body');
    const modal = document.getElementById('tack-analysis-modal');
    if (!body || !modal) return;

    if (!raceData?.boats) {
        body.innerHTML = '<div class="modal-empty">No race data loaded.</div>';
        modal.style.display = 'flex';
        return;
    }

    // Detect tacks across every boat, build aligned tracks.
    const raw = [];
    let droppedNoWind = 0;
    for (const [deviceId, boatData] of Object.entries(raceData.boats)) {
        const layer = boatLayers[deviceId];
        if (!layer?.data?.length) continue;
        const team = boatData.boat?.boat_name || boatData.boat?.team_name || deviceId;
        const color = layer.color || '#888';
        // Clip to this boat's racing window (gun → its finish) and use the
        // on-course (fleet-inferred) wind for the current race.
        const win = racingWindowMsFor(boatData.boat, layer);
        const tacks = detectManeuversForLayer(layer, true)
            .filter(m => m.type === 'tack' && m.tStart >= win.startMs && m.tStart <= win.endMs);
        for (const t of tacks) {
            const built1 = buildTackTrack({ ...t, deviceId, team, color }, layer, true);
            if (!built1) { droppedNoWind++; continue; }
            raw.push(built1);
        }
    }

    // Quality filter (turn angle in plausible range + geometric sanity).
    const built = raw.filter(isCleanTack);
    const droppedQuality = raw.length - built.length;

    if (!built.length) {
        body.innerHTML = `<div class="modal-empty">
            No clean tacks found in this race.
            ${droppedNoWind ? `<div style="margin-top:0.5rem;font-size:0.8rem;">${droppedNoWind} excluded · no wind data.</div>` : ''}
            ${droppedQuality ? `<div style="margin-top:0.25rem;font-size:0.8rem;">${droppedQuality} excluded · turn angle or geometry out of range.</div>` : ''}
        </div>`;
        modal.style.display = 'flex';
        return;
    }

    // Assign IDs after filtering so they're contiguous.
    built.forEach((t, i) => t.id = `t${i}`);

    // Best (lowest time-lost) overall and per-boat.
    const valid = built.filter(t => t.timeLostSec != null);
    const fleetBest = valid.length ? valid.reduce((a, b) => (a.timeLostSec <= b.timeLostSec ? a : b)) : null;
    const perBoatBest = {};
    for (const t of valid) {
        if (!perBoatBest[t.deviceId] || t.timeLostSec < perBoatBest[t.deviceId].timeLostSec) {
            perBoatBest[t.deviceId] = t;
        }
    }

    // Roster = EVERY boat with a track this race (keyed by trackKey),
    // identified by BOAT NAME. The table and checkboxes show all of them,
    // not just boats with detected tacks — a boat with 0 tacks still gets
    // a row (N=0) instead of silently vanishing.
    const allBoats = [];
    for (const [deviceId, boatData] of Object.entries(raceData.boats)) {
        const layer = boatLayers[deviceId];
        if (!layer?.data?.length) continue;
        const win0 = racingWindowMsFor(boatData.boat, layer);
        allBoats.push({
            deviceId,
            team: boatData.boat?.boat_name || boatData.boat?.team_name || deviceId,
            color: layer.color || '#888',
            gpsHz: gpsHzFor(layer, win0.startMs, win0.endMs),
        });
    }
    const allDevices = allBoats.map(b => b.deviceId);
    tackAnalysisState = {
        tacksThisRace: built,
        allBoats,
        tacksAcrossDay: null,         // populated lazily when user enters "Today" mode
        crossDayState: 'idle',         // idle | loading | ready | error
        selectedDevices: new Set(allDevices),
        highlightId: null,
        fleetBestId: fleetBest?.id || null,
        perBoatBestIds: new Set(Object.values(perBoatBest).map(t => t.id)),
        droppedNoWind,
        droppedQuality,
        viewMode: 'teamAvgDay',        // default: pool every team's tacks across the day
    };

    body.innerHTML = renderTackAnalysisShell(allDevices);
    wireTackAnalysisInteractions(body);
    // Kick off the cross-day pool as the default landing view.
    ensureCrossDayTacks(body).then(() => redrawTackAnalysis());
    modal.style.display = 'flex';
}

function renderTackAnalysisShell(allDevices) {
    const st = tackAnalysisState;
    // Seed with EVERY boat that has a track (count 0), then add tack
    // counts — so zero-tack boats still get a chip instead of vanishing.
    const boatsBy = {};
    for (const b of (st.allBoats || [])) {
        boatsBy[b.deviceId] = { team: b.team, color: b.color, count: 0 };
    }
    for (const t of st.tacksThisRace) {
        if (!boatsBy[t.deviceId]) boatsBy[t.deviceId] = { team: t.team, color: t.color, count: 0 };
        boatsBy[t.deviceId].count++;
    }
    const boatChips = allDevices.map(dev => {
        const b = boatsBy[dev];
        return `<label data-ta-toggle-boat="${dev}">
            <input type="checkbox" checked>
            <span class="ta-swatch" style="background:${b.color}"></span>${b.team} <span style="opacity:0.6">(${b.count})</span>
        </label>`;
    }).join('');

    const nValid = st.tacksThisRace.length;
    const drops = [];
    if (st.droppedNoWind) drops.push(`${st.droppedNoWind} no wind`);
    if (st.droppedQuality) drops.push(`${st.droppedQuality} bad geometry/angle`);
    const meta = `${nValid} clean tack${nValid === 1 ? '' : 's'}` +
        (drops.length ? ` · <span title="Excluded from the overlay">${drops.join(' · ')} excluded</span>` : '');

    return `
        <div class="ta-toolbar">
            <div class="ta-segctl" role="tablist" aria-label="View mode">
                <button data-ta-mode="tacks"      class="ta-seg"        type="button">Per tack</button>
                <button data-ta-mode="teamAvg"    class="ta-seg"        type="button">Team avg</button>
                <button data-ta-mode="teamAvgDay" class="ta-seg active" type="button">Team avg · today</button>
            </div>
            ${boatChips}
            <label data-ta-toggle="fleet-best"><input type="checkbox" checked> <span class="ta-badge-best">Fleet best</span></label>
            <span class="ta-meta" id="ta-meta">${meta}</span>
        </div>
        <div class="ta-stack">
            <div class="ta-plot-wrap">
                <div class="ta-legend">
                    <span>Each track in its boat colour. Brightness / opacity / thickness scale with speed within that track:</span>
                    <span class="ta-legend-step ta-legend-dim">slowest</span>
                    <span class="ta-legend-bar"></span>
                    <span class="ta-legend-step ta-legend-bright">fastest</span>
                </div>
                <svg class="ta-plot" id="ta-plot" viewBox="0 0 400 700" preserveAspectRatio="xMidYMid meet" aria-label="Tack overlay, wind up"></svg>
            </div>
            <div class="ta-right">
                <div class="ta-list-wrap">
                    <table class="ta-list" id="ta-list">
                        <thead id="ta-list-head"></thead>
                        <tbody></tbody>
                    </table>
                </div>
                <details class="ta-metric-legend" open>
                    <summary>Reading the metrics &mdash; what to look for</summary>
                    <div class="ta-metric-grid">
                        <div><span class="ta-mname">Turn° <span class="ta-arrow">⊙</span></span> Heading change from before to after the tack. <em>~85–110° is typical.</em> Wider = boat wasn't close-hauled before/after.</div>
                        <div><span class="ta-mname">Lost (s) <span class="ta-arrow">↓</span></span> Time-equivalent of speed loss across the tack window. <em>Lower is better.</em></div>
                        <div><span class="ta-mname">Δ kn <span class="ta-arrow">↓</span></span> Biggest instantaneous speed drop (speedBefore − speedMin). <em>Lower is better.</em></div>
                        <div><span class="ta-mname">Heel° <span class="ta-arrow">↓</span></span> Peak heel during the tack (IMU). <em>Lower is better</em> — top crews flatten the boat through head-to-wind.</div>
                        <div><span class="ta-mname">Turn σ <span class="ta-arrow">↓</span></span> Smoothness of dCOG/dt across the turn. <em>Lower is better</em> (constant turn rate); high values = jerky steering.</div>
                        <div><span class="ta-mname">ΔTWA <span class="ta-arrow">⊙</span></span> Exit close-hauled angle minus entry. <em>Near 0 is symmetric.</em> Positive = footing for speed; negative = pinching.</div>
                        <div><span class="ta-mname">Lost (s) ± σ <span class="ta-arrow">↓ ↓</span></span> <em>Team-avg only.</em> Mean ± standard deviation. Two teams with the same mean and different σ are not equal — small σ = consistent crew.</div>
                        <div><span class="ta-mname">GPS</span> Track sample rate. Tack &amp; maneuver detection needs <strong>≈1 Hz or finer</strong>. A <strong>0.1 Hz</strong> track (one fix every 10 s) is too coarse to resolve a tack, so those boats read low or empty (<span class="gps-hz-poor">red</span>) — it's a data-rate limit, not zero tacks.</div>
                    </div>
                </details>
            </div>
        </div>
    `;
}

function wireTackAnalysisInteractions(body) {
    body.addEventListener('change', (e) => {
        const t = e.target;
        if (t.matches('[data-ta-toggle-boat] input')) {
            const dev = t.closest('[data-ta-toggle-boat]').getAttribute('data-ta-toggle-boat');
            if (t.checked) tackAnalysisState.selectedDevices.add(dev);
            else            tackAnalysisState.selectedDevices.delete(dev);
            redrawTackAnalysis();
        } else if (t.matches('[data-ta-toggle="fleet-best"] input')) {
            redrawTackAnalysis();
        }
    });
    body.addEventListener('click', async (e) => {
        const seg = e.target.closest('[data-ta-mode]');
        if (seg) {
            const mode = seg.getAttribute('data-ta-mode');
            if (mode === tackAnalysisState.viewMode) return;
            tackAnalysisState.viewMode = mode;
            tackAnalysisState.highlightId = null;
            for (const b of body.querySelectorAll('[data-ta-mode]')) {
                b.classList.toggle('active', b.getAttribute('data-ta-mode') === mode);
            }
            if (mode === 'teamAvgDay' && tackAnalysisState.crossDayState !== 'ready') {
                await ensureCrossDayTacks(body);
            }
            redrawTackAnalysis();
            return;
        }
        const row = e.target.closest('[data-ta-id]');
        if (!row) return;
        const id = row.getAttribute('data-ta-id');
        tackAnalysisState.highlightId = (tackAnalysisState.highlightId === id) ? null : id;
        applyTackEmphasis();
    });
}

// Lazy-fetch GPS for the other races of the same day, run maneuver
// detection against the wind data already loaded for the current race
// window, and accumulate clean tacks into tacksAcrossDay.
async function ensureCrossDayTacks(body) {
    const st = tackAnalysisState;
    const meta = body.querySelector('#ta-meta');
    if (st.crossDayState === 'loading' || st.crossDayState === 'ready') return;
    if (!currentRaceDay?.races?.length) {
        st.tacksAcrossDay = st.tacksThisRace.slice();
        st.crossDayState = 'ready';
        return;
    }
    st.crossDayState = 'loading';
    if (meta) meta.innerHTML = '<em>Loading other races…</em>';

    const otherRaces = currentRaceDay.races.filter(r => r.race_id !== currentRace?.race_id);
    const pool = st.tacksThisRace.slice();           // start with current race
    let dropNoWind = st.droppedNoWind;
    let dropQuality = st.droppedQuality;

    await Promise.all(otherRaces.map(async (r) => {
        try {
            const resp = await fetch(`${API_BASE}/api/races/${r.race_id}/data?sensors=gps,imu`);
            if (!resp.ok) return;
            const data = await resp.json();
            for (const [deviceId, boatData] of Object.entries(data.boats || {})) {
                const gps = boatData?.sensors?.gps;
                if (!gps?.length) continue;
                const team = boatData.boat?.boat_name || boatData.boat?.team_name || deviceId;
                // Reuse the current-race color if we have it, else fall back to grey.
                const color = boatLayers[deviceId]?.color || '#888';
                const fakeLayer = { data: gps, imu: boatData?.sensors?.imu || [] };
                // Best-effort window for the other race (no per-class gun
                // available cross-day): that race's start → this boat's
                // finish. Buoy wind (fleet inference is current-race only).
                const oStart = data.race?.start_time ? new Date(data.race.start_time).getTime() : -Infinity;
                const oEnd = _parseTimeToMs(boatData.boat?.finish_time)
                    ?? (data.race?.end_time ? new Date(data.race.end_time).getTime() : Infinity);
                const tacks = detectManeuversForLayer(fakeLayer, false)
                    .filter(m => m.type === 'tack' && m.tStart >= oStart && m.tStart <= oEnd);
                for (const t of tacks) {
                    const built = buildTackTrack({ ...t, deviceId, team, color }, fakeLayer, false);
                    if (!built) { dropNoWind++; continue; }
                    if (!isCleanTack(built)) { dropQuality++; continue; }
                    pool.push(built);
                }
            }
        } catch (e) {
            console.warn('[TackAnalysis] cross-day fetch failed for', r.race_id, e);
        }
    }));

    pool.forEach((t, i) => t.id = `td${i}`);  // unique IDs in the day pool
    st.tacksAcrossDay = pool;
    st.droppedNoWindDay = dropNoWind;
    st.droppedQualityDay = dropQuality;
    st.crossDayState = 'ready';
}

// Resample tacks onto a uniform time-from-apex grid, then mean (x, y, speed)
// per team. Returns { deviceId, team, color, points: [{x,y,speed}], count, meanLost, meanTurn }.
//
// Edge handling: with the 20m distance cap, individual tacks span ~±8–13 s
// from the apex (varies with boat speed). At grid edges only a few tacks
// contribute, which produces noisy/jumpy averages. We require ≥75% of the
// team's tacks to contribute at each emitted grid point — points with
// sparse coverage are silently dropped, so the averaged track ends cleanly
// where it has real support instead of waving around at 15–20 m out.
function buildTeamAverages(tacks) {
    const T_MIN = -12, T_MAX = 12, T_STEP = 0.5;  // seconds from apex
    const MIN_COVERAGE = 0.75;                     // fraction of team's tacks
    const grid = [];
    for (let t = T_MIN; t <= T_MAX + 1e-9; t += T_STEP) grid.push(t);

    // Precompute time-from-apex per point for each tack.
    const byTeam = new Map();
    for (const t of tacks) {
        if (!byTeam.has(t.deviceId)) byTeam.set(t.deviceId, { team: t.team, color: t.color, items: [] });
        const series = t.points.map(p => ({
            dt: (p.t - t.tApexMs) / 1000,
            x: p.x, y: p.y, speed: p.speed,
        })).sort((a, b) => a.dt - b.dt);
        byTeam.get(t.deviceId).items.push({ tack: t, series });
    }

    function lerpAt(series, target) {
        if (target < series[0].dt || target > series[series.length - 1].dt) return null;
        // Binary search.
        let lo = 0, hi = series.length - 1;
        while (lo + 1 < hi) {
            const mid = (lo + hi) >> 1;
            if (series[mid].dt <= target) lo = mid; else hi = mid;
        }
        const a = series[lo], b = series[hi];
        const span = b.dt - a.dt;
        if (span <= 0) return a;
        const f = (target - a.dt) / span;
        return {
            x: a.x + (b.x - a.x) * f,
            y: a.y + (b.y - a.y) * f,
            speed: a.speed + (b.speed - a.speed) * f,
        };
    }

    const out = [];
    for (const [deviceId, info] of byTeam.entries()) {
        const minContrib = Math.max(2, Math.ceil(info.items.length * MIN_COVERAGE));
        const avgPoints = [];
        for (const dt of grid) {
            let sx = 0, sy = 0, sv = 0, n = 0;
            for (const it of info.items) {
                const v = lerpAt(it.series, dt);
                if (!v) continue;
                sx += v.x; sy += v.y; sv += v.speed; n++;
            }
            if (n < minContrib) continue;     // sparse coverage at this grid point — skip
            avgPoints.push({ x: sx / n, y: sy / n, speed: sv / n, t: dt * 1000 });
        }
        if (avgPoints.length < 4) continue;
        const losses = info.items.map(i => i.tack.timeLostSec).filter(v => v != null);
        const turns = info.items.map(i => i.tack.turnAngle).filter(v => v != null);
        const drops = info.items
            .map(i => (i.tack.speedBefore != null && i.tack.speedMin != null) ? (i.tack.speedBefore - i.tack.speedMin) : null)
            .filter(v => v != null);
        const syms = info.items.map(i => i.tack.symmetry).filter(v => v != null);
        const heels = info.items.map(i => i.tack.maxHeel).filter(v => v != null);
        const trStds = info.items.map(i => i.tack.turnRateStd).filter(v => v != null);
        const twaDs = info.items.map(i => i.tack.twaDelta).filter(v => v != null);
        const mean = arr => arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null;
        const meanLost = mean(losses);
        // Population std-dev of time-lost. Two teams with the same mean but
        // different std are NOT the same team — std discriminates consistency.
        let stdLost = null;
        if (losses.length > 1 && meanLost != null) {
            const variance = losses.reduce((s, v) => s + (v - meanLost) ** 2, 0) / losses.length;
            stdLost = Math.sqrt(variance);
        }
        const speedBefore = info.items[0].tack.speedBefore;  // representative
        out.push({
            id: `team-${deviceId}`,
            deviceId, team: info.team, color: info.color,
            points: avgPoints,
            count: info.items.length,
            meanLost, stdLost,
            meanTurn: mean(turns),
            meanSpeedDrop: mean(drops),
            meanSymmetry: mean(syms),
            meanHeel: mean(heels),
            meanTurnRateStd: mean(trStds),
            meanTwaDelta: mean(twaDs),
            speedBefore,
        });
    }
    out.sort((a, b) => (a.meanLost ?? 99) - (b.meanLost ?? 99));
    return out;
}

function redrawTackAnalysis() {
    const st = tackAnalysisState;
    if (!st) return;

    let pool;
    if (st.viewMode === 'teamAvgDay' && st.tacksAcrossDay) pool = st.tacksAcrossDay;
    else pool = st.tacksThisRace;

    const filteredTacks = pool.filter(t => st.selectedDevices.has(t.deviceId));

    let tracks;
    let listMode;
    if (st.viewMode === 'tacks') {
        tracks = filteredTacks.map(t => ({ ...t, thick: false }));
        listMode = 'tacks';
    } else {
        // Team avg / team avg + day. Show EVERY selected boat — including
        // boats with too few tacks for an averaged plot line, and zero-tack
        // boats (N=0). buildTeamAverages supplies the averaged track where
        // coverage allows; the per-boat metrics are computed from the
        // boat's own tacks so even a sparse boat still gets numbers.
        const avgByDev = new Map(buildTeamAverages(filteredTacks).map(a => [a.deviceId, a]));
        const tacksByDev = new Map();
        for (const t of filteredTacks) {
            if (!tacksByDev.has(t.deviceId)) tacksByDev.set(t.deviceId, []);
            tacksByDev.get(t.deviceId).push(t);
        }
        const mean = arr => arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null;
        const roster = (st.allBoats || []).filter(b => st.selectedDevices.has(b.deviceId));
        tracks = roster.map(b => {
            const a = avgByDev.get(b.deviceId);
            const ts = tacksByDev.get(b.deviceId) || [];
            const losses = ts.map(t => t.timeLostSec).filter(v => v != null);
            const meanLost = mean(losses);
            let stdLost = null;
            if (losses.length > 1 && meanLost != null) {
                stdLost = Math.sqrt(losses.reduce((s, v) => s + (v - meanLost) ** 2, 0) / losses.length);
            }
            return {
                id: a?.id || `team-${b.deviceId}`,
                deviceId: b.deviceId, team: b.team, color: b.color,
                gpsHz: b.gpsHz,
                points: a?.points || [],          // averaged plot line only where coverage allows
                count: ts.length,
                meanLost, stdLost,
                meanTurn: mean(ts.map(t => t.turnAngle).filter(v => v != null)),
                meanSpeedDrop: mean(ts.map(t => (t.speedBefore != null && t.speedMin != null) ? (t.speedBefore - t.speedMin) : null).filter(v => v != null)),
                meanHeel: mean(ts.map(t => t.maxHeel).filter(v => v != null)),
                meanTurnRateStd: mean(ts.map(t => t.turnRateStd).filter(v => v != null)),
                meanTwaDelta: mean(ts.map(t => t.twaDelta).filter(v => v != null)),
                speedBefore: a?.speedBefore ?? ts[0]?.speedBefore ?? 0,
                thick: true,
            };
        });
        // Boats that tacked first (best tack-loss on top); zero-tack boats last.
        tracks.sort((x, y) => ((x.count ? 0 : 1) - (y.count ? 0 : 1)) || ((x.meanLost ?? 999) - (y.meanLost ?? 999)));
        listMode = 'teams';
    }

    drawTackPlot(tracks);
    drawTackList(filteredTacks, tracks, listMode);
    applyTackEmphasis();

    // Legend max: from speedBefore of underlying per-tack data (consistent across modes).
    const maxSpeed = filteredTacks.reduce((m, t) => Math.max(m, t.speedBefore || 0), 0);
    const lblMax = document.getElementById('ta-legend-max');
    if (lblMax) lblMax.textContent = maxSpeed > 0 ? maxSpeed.toFixed(1) : '—';

    // Meta count.
    const meta = document.getElementById('ta-meta');
    if (meta) {
        const ndrops = (st.viewMode === 'teamAvgDay' && st.crossDayState === 'ready')
            ? `${(st.droppedNoWindDay || 0)} no wind · ${(st.droppedQualityDay || 0)} bad geom`
            : `${st.droppedNoWind} no wind · ${st.droppedQuality} bad geom`;
        const nLabel = st.viewMode === 'tacks'
            ? `${filteredTacks.length} clean tack${filteredTacks.length === 1 ? '' : 's'}`
            : `${tracks.length} team${tracks.length === 1 ? '' : 's'} · ${filteredTacks.length} tacks pooled`;
        const dayTag = st.viewMode === 'teamAvgDay' ? ' · day pool' : '';
        meta.innerHTML = `${nLabel}${dayTag} <span title="Excluded">· ${ndrops} excluded</span>`;
    }
}

function drawTackPlot(visible) {
    const svg = document.getElementById('ta-plot');
    if (!svg) return;
    const W = 400, H = 700;
    const margin = 40;

    // Auto-fit to the bounding box of all visible points (always include the
    // apex at (0,0), and 10% padding so nothing kisses the edge). After the
    // geometry-based mirror, every clean tack lives in the +x half-plane,
    // so the bounding box naturally hugs the data and the apex sits on the
    // left side of the cluster.
    let minX = 0, maxX = 0, minY = 0, maxY = 0;
    for (const t of visible) {
        for (const p of t.points) {
            if (p.x < minX) minX = p.x; if (p.x > maxX) maxX = p.x;
            if (p.y < minY) minY = p.y; if (p.y > maxY) maxY = p.y;
        }
    }
    // Reserve a strip at the top of the canvas for the wind arrow + label
    // so it lives above the data area rather than floating inside it.
    const topReserve = 36;
    const yExt = Math.max(20, maxY - minY);
    const padY = yExt * 0.04;
    // Asymmetric X padding: small fixed gap on both sides — keep apex
    // off the left margin and the rightmost extent off the right margin.
    const padXleft = 1.5;
    const padXright = 1;
    minX -= padXleft; maxX += padXright; minY -= padY; maxY += padY;
    const rangeX = maxX - minX, rangeY = maxY - minY;
    const scale = Math.min((W - 2 * margin) / rangeX, (H - margin - topReserve) / rangeY);
    // Top-left align the bounding box: top of data sits at topReserve so
    // the wind arrow has its own strip above. Apex hugs the left margin.
    const offsetX = margin - minX * scale;
    const offsetY = topReserve + maxY * scale;
    const sx = (x) => (offsetX + x * scale).toFixed(1);
    const sy = (y) => (offsetY - y * scale).toFixed(1);
    const apexSx = +sx(0), apexSy = +sy(0);
    const maxR = Math.max(maxX, maxY, -minX, -minY);

    const parts = [];
    // Background axes (cross at the apex).
    parts.push(`<line class="ta-axis" x1="${margin}" y1="${apexSy}" x2="${W - margin}" y2="${apexSy}"/>`);
    parts.push(`<line class="ta-axis" x1="${apexSx}" y1="${margin}" x2="${apexSx}" y2="${H - margin}"/>`);
    // Range rings every N m.
    const ringStep = maxR < 60 ? 10 : (maxR < 150 ? 25 : 50);
    for (let r = ringStep; r <= maxR; r += ringStep) {
        parts.push(`<circle cx="${apexSx}" cy="${apexSy}" r="${(r * scale).toFixed(1)}" fill="none" class="ta-grid-line"/>`);
        parts.push(`<text class="ta-axis-label" x="${apexSx + 4}" y="${apexSy - r * scale - 2}">${r}m</text>`);
    }
    // Ideal close-hauled V at ±42° from the wind axis (J/80 upwind angle):
    // dashed reference lines from the apex so users can see how their tack
    // shape compares to a perfect 84° turn.
    const idealAngleDeg = 42;
    const refLen = Math.max(maxX, Math.max(-minY, maxY)) * 0.95;
    const dx = Math.sin(idealAngleDeg * Math.PI / 180) * refLen;
    const dyUp = Math.cos(idealAngleDeg * Math.PI / 180) * refLen;
    parts.push(`<line class="ta-ideal" x1="${apexSx}" y1="${apexSy}" x2="${sx(dx)}" y2="${sy(dyUp)}"/>`);
    parts.push(`<line class="ta-ideal" x1="${apexSx}" y1="${apexSy}" x2="${sx(dx)}" y2="${sy(-dyUp)}"/>`);
    // Wind arrow pinned to the top reserved strip — above all data.
    const arrowYTop = 4;
    const arrowYBot = topReserve - 4;
    parts.push(`<g class="ta-wind">
        <line class="ta-wind-arrow" x1="${apexSx}" y1="${arrowYTop}" x2="${apexSx}" y2="${arrowYBot}"/>
        <polyline class="ta-wind-arrow" points="${apexSx - 6},${arrowYBot - 8} ${apexSx},${arrowYBot} ${apexSx + 6},${arrowYBot - 8}"/>
        <text class="ta-wind-label" x="${apexSx + 10}" y="${arrowYBot - 6}">wind</text>
    </g>`);
    // Direction labels.
    parts.push(`<text class="ta-axis-label" x="${W - margin - 30}" y="${apexSy + 14}">exit →</text>`);
    parts.push(`<text class="ta-axis-label" x="${apexSx + 6}" y="${H - margin + 4}">downwind</text>`);
    parts.push(`<text class="ta-axis-label" x="${sx(dx) - 60}" y="${sy(-dyUp) + 12}" style="opacity:0.5">ideal 42°</text>`);
    // Apex marker.
    parts.push(`<circle class="ta-apex-dot" cx="${apexSx}" cy="${apexSy}" r="4"/>`);

    // Each track: split into N short polylines so each segment can carry
    // its own visual encoding. Hue per track = the boat's team colour
    // (overlapping tracks read as separate identities); each segment's
    // brightness + opacity + stroke-width encode its speed POSITION
    // within this track's own min/max range.
    //
    // Per-track normalization (vs absolute % of entry speed) is the
    // change that makes the variance actually visible — averaging
    // damps the peak in Team-avg mode, so a fixed reference compresses
    // every line into the same narrow brightness band. Stretching to
    // fill the full dim→bright range PER TRACK lets each track tell
    // its own "where did I lose speed" story even when the absolute
    // variance is small.
    for (const t of visible) {
        const pts = t.points;
        const baseWidth = t.thick ? 2.6 : 1.5;
        // Per-track speed envelope. Skip degenerate tracks where every
        // point reports the same speed (would divide by zero) — they
        // render at the bright end so they're still legible.
        let minSpd = Infinity, maxSpd = -Infinity;
        for (const p of pts) {
            const s = p.speed;
            if (s == null || !Number.isFinite(s)) continue;
            if (s < minSpd) minSpd = s;
            if (s > maxSpd) maxSpd = s;
        }
        const span = maxSpd - minSpd;
        for (let i = 1; i < pts.length; i++) {
            const a = pts[i - 1], b = pts[i];
            const segSpeed = (a.speed + b.speed) / 2;
            const n = span > 0.05 ? (segSpeed - minSpd) / span : 1;
            const { stroke, opacity, widthBoost } = boatSpeedAttrs(t.deviceId, n);
            const w = (baseWidth + parseFloat(widthBoost)).toFixed(2);
            parts.push(
                `<polyline data-ta-id="${t.id}" class="normal" ` +
                `stroke="${stroke}" stroke-width="${w}" opacity="${opacity}" ` +
                `points="${sx(a.x)},${sy(a.y)} ${sx(b.x)},${sy(b.y)}"/>`
            );
        }
    }

    svg.innerHTML = parts.join('');
}

function drawTackList(filteredTacks, tracks, listMode) {
    const head = document.getElementById('ta-list-head');
    const tbody = document.querySelector('#ta-list tbody');
    if (!head || !tbody) return;
    const st = tackAnalysisState;

    if (listMode === 'teams') {
        head.innerHTML = `<tr>
            <th>Team</th>
            <th>Sail #</th>
            <th class="num" title="GPS sample rate of the track. Tack analysis needs ≈1 Hz or finer; a 0.1 Hz track (1 fix / 10 s) is too coarse to resolve tacks.">GPS</th>
            <th class="num">N</th>
            <th class="num">Turn°</th>
            <th class="num metric" title="Mean ± standard deviation of per-tack time-lost. Same mean with smaller σ = more consistent crew.">Lost&nbsp;(s)</th>
            <th class="num" title="Mean instantaneous speed drop (speedBefore − speedMin) per tack.">Δ&nbsp;kn</th>
            <th class="num" title="Mean max |heel| during the tack window (degrees, IMU). Lower = boat flattened through head-to-wind better.">Heel°</th>
            <th class="num" title="Mean σ of dCOG/dt during the maneuver. Lower = smoother turn-rate profile.">Turn&nbsp;σ</th>
            <th class="num" title="Mean of |TWA_after| − |TWA_before|. Positive = exits wider than entry (footing for speed). Near 0 = symmetric crossing.">ΔTWA</th>
        </tr>`;
        const rows = tracks.map(t => {
            const lost = (t.meanLost != null)
                ? (t.stdLost != null ? `${t.meanLost.toFixed(1)} ± ${t.stdLost.toFixed(1)}` : t.meanLost.toFixed(1))
                : '—';
            return `<tr data-ta-id="${t.id}">
                <td><span class="ta-row-color" style="background:${t.color}"></span>${t.team}</td>
                <td>${sailNumberCell(t.deviceId)}</td>
                <td class="num ${gpsHzClass(t.gpsHz)}" title="GPS sample rate">${fmtGpsHz(t.gpsHz)}</td>
                <td class="num">${t.count}</td>
                <td class="num">${t.meanTurn != null ? t.meanTurn.toFixed(0) + '°' : '—'}</td>
                <td class="num metric">${lost}</td>
                <td class="num">${t.meanSpeedDrop != null ? t.meanSpeedDrop.toFixed(1) : '—'}</td>
                <td class="num">${t.meanHeel != null ? t.meanHeel.toFixed(1) + '°' : '—'}</td>
                <td class="num">${t.meanTurnRateStd != null ? t.meanTurnRateStd.toFixed(1) : '—'}</td>
                <td class="num">${t.meanTwaDelta != null ? (t.meanTwaDelta >= 0 ? '+' : '') + t.meanTwaDelta.toFixed(0) + '°' : '—'}</td>
            </tr>`;
        }).join('');
        tbody.innerHTML = rows || '<tr><td colspan="10" style="text-align:center;color:var(--text-secondary);padding:1rem">No teams in current selection.</td></tr>';
        return;
    }

    // Per-tack mode (default).
    head.innerHTML = `<tr>
        <th>Tack</th>
        <th>Sail #</th>
        <th class="num">Turn°</th>
        <th class="num metric" title="Lower = better. Σ(speed deficit · dt) / speedBefore.">Lost&nbsp;(s)</th>
        <th class="num" title="speedBefore − speedMin · instantaneous worst speed drop.">Δ&nbsp;kn</th>
        <th class="num" title="Max |heel| during the tack window (degrees, IMU). Lower = boat flattened through head-to-wind better.">Heel°</th>
        <th class="num" title="σ of dCOG/dt during the maneuver. Lower = smoother turn rate. Higher = jerky steering.">Turn&nbsp;σ</th>
        <th class="num" title="|TWA_after| − |TWA_before|. Positive = exited wider than entered (footing for speed). Near 0 = symmetric crossing.">ΔTWA</th>
    </tr>`;
    const sorted = filteredTacks.slice().sort((a, b) => {
        if (a.timeLostSec == null) return 1;
        if (b.timeLostSec == null) return -1;
        return a.timeLostSec - b.timeLostSec;
    });
    const rows = sorted.map(t => {
        const isFleetBest = (t.id === st.fleetBestId);
        const isPersonalBest = st.perBoatBestIds.has(t.id);
        const wide = t.turnAngle != null && t.turnAngle > TA_WIDE_TURN_DEG;
        const badges = [
            isFleetBest ? '<span class="ta-badge-best">Fleet</span>' : '',
            (!isFleetBest && isPersonalBest) ? '<span class="ta-badge-best" style="background:rgba(34,211,238,0.15);color:#22d3ee">Boat</span>' : '',
            wide ? '<span class="ta-badge-wide">Wide</span>' : '',
        ].join('');
        const drop = (t.speedBefore != null && t.speedMin != null) ? (t.speedBefore - t.speedMin) : null;
        return `<tr data-ta-id="${t.id}" class="${isFleetBest ? 'best' : ''}">
            <td><span class="ta-row-color" style="background:${t.color}"></span>${t.team}<br><span style="font-size:0.7rem;color:var(--text-secondary)">${fmtTimeOfDay(t.tStart)}${badges}</span></td>
            <td>${sailNumberCell(t.deviceId)}</td>
            <td class="num">${t.turnAngle != null ? t.turnAngle.toFixed(0) + '°' : '—'}</td>
            <td class="num metric">${t.timeLostSec != null ? t.timeLostSec.toFixed(1) : '—'}</td>
            <td class="num">${drop != null ? drop.toFixed(1) : '—'}</td>
            <td class="num">${t.maxHeel != null ? t.maxHeel.toFixed(1) + '°' : '—'}</td>
            <td class="num">${t.turnRateStd != null ? t.turnRateStd.toFixed(1) : '—'}</td>
            <td class="num">${t.twaDelta != null ? (t.twaDelta >= 0 ? '+' : '') + t.twaDelta.toFixed(0) + '°' : '—'}</td>
        </tr>`;
    }).join('');
    tbody.innerHTML = rows || '<tr><td colspan="8" style="text-align:center;color:var(--text-secondary);padding:1rem">No tacks match the current selection.</td></tr>';
}

function applyTackEmphasis() {
    const st = tackAnalysisState;
    if (!st) return;
    const fleetBestToggle = document.querySelector('[data-ta-toggle="fleet-best"] input');
    const showFleetBest = fleetBestToggle ? fleetBestToggle.checked : true;
    const highlight = st.highlightId;

    const polys = document.querySelectorAll('#ta-plot polyline[data-ta-id]');
    for (const el of polys) {
        const id = el.getAttribute('data-ta-id');
        let cls = 'normal';
        if (highlight) {
            cls = (id === highlight) ? 'emphasis' : 'faded';
        } else if (showFleetBest && st.fleetBestId) {
            cls = (id === st.fleetBestId) ? 'emphasis' : 'normal';
        }
        el.setAttribute('class', cls);
    }
    const rows = document.querySelectorAll('#ta-list tbody tr[data-ta-id]');
    for (const tr of rows) {
        tr.classList.toggle('selected', tr.getAttribute('data-ta-id') === highlight);
    }
}

// =====================================================================
// Roll-tacking analysis
//
// Per-tack technique analysis across boats. Compares the five phases of
// a roll tack — Approach, Windward Roll, Head-to-Wind, Flatten, Exit —
// by extracting heel + speed signatures around each detected tack and
// computing phase-specific metrics. Reuses detectManeuversForLayer() for
// tack discovery and windAt() for TWD lookup; the only new state is the
// per-tack "profile" record below.
//
// Profile shape (one per detected tack):
//   {
//     id, deviceId, team, color,
//     t0Ms,            // head-to-wind moment (|TWA| min in [tStart, tEnd])
//     tStartMs,        // tack start (speed dip onset)
//     tEndMs,          // tack end (turn complete)
//     heelSeries,      // [{tRel, heel}] resampled to TR_DT_S grid in [-5, +10]
//     speedSeries,     // [{tRel, speed}] same grid
//     metrics: {
//       approachSpeedKn, exitSpeedKn5, exitSpeedKn10, minSpeedKn,
//       speedLossPct, recoveryTimeS,
//       heelRangeDeg,           // max(heel) - min(heel) in [-2, +3]
//       peakRollExcessDeg,      // overshoot beyond steady-state pre-tack heel
//       peakFlattenRateDegS,    // max |d(heel)/dt| in [-1, +2]
//       timeInIronsS,           // duration |TWA| < 10°
//       wasRolled,              // boolean: peakRollExcessDeg > 4
//     }
//   }
// =====================================================================

const RT_PRE_SEC = 5;       // seconds before t0
const RT_POST_SEC = 10;     // seconds after t0
const RT_DT_S = 0.5;        // resample grid step
const RT_ROLL_THRESHOLD_DEG = 4;   // excess heel beyond steady → "rolled"

// Per-modal state (analogue of tackAnalysisState).
let rollTackState = null;
const _rtCharts = {};       // Chart.js instances per canvas id

function _rtResampleSeries(samples, t0Ms, accessor, tMin = -RT_PRE_SEC, tMax = RT_POST_SEC) {
    // samples: [{t: iso, ...}] with accessor(s) returning a numeric value.
    // Returns [{tRel, value}] on a uniform grid; gaps filled by linear
    // interpolation between flanking samples. Empty array if no data.
    if (!samples?.length) return [];
    const ts = [];
    const vs = [];
    for (const s of samples) {
        const tm = s?.t ? new Date(s.t).getTime() : null;
        if (tm == null) continue;
        const v = accessor(s);
        if (!Number.isFinite(v)) continue;
        ts.push((tm - t0Ms) / 1000);
        vs.push(v);
    }
    if (ts.length < 2) return [];
    const out = [];
    let j = 0;
    for (let tRel = tMin; tRel <= tMax + 1e-6; tRel += RT_DT_S) {
        while (j + 1 < ts.length && ts[j + 1] < tRel) j++;
        if (tRel < ts[0] || tRel > ts[ts.length - 1]) { out.push({ tRel, value: null }); continue; }
        const t1 = ts[j], v1 = vs[j];
        const t2 = ts[Math.min(j + 1, ts.length - 1)], v2 = vs[Math.min(j + 1, vs.length - 1)];
        if (t2 === t1) { out.push({ tRel, value: v1 }); continue; }
        const f = (tRel - t1) / (t2 - t1);
        out.push({ tRel, value: v1 + (v2 - v1) * f });
    }
    return out;
}

// Locate the head-to-wind moment inside the tack window: the sample
// where the absolute TWA is smallest (COG ≈ TWD).
function _rtFindT0(layer, tack) {
    const data = layer.data;
    if (!data?.length) return null;
    let bestT = null, bestAbsTwa = Infinity;
    for (const p of data) {
        const tm = new Date(p.t).getTime();
        if (tm < tack.tStart || tm > tack.tEnd) continue;
        const w = windAt(tm);
        if (!w || w.twd == null || p.course == null) continue;
        const twa = Math.abs(((w.twd - p.course + 540) % 360) - 180);
        if (twa < bestAbsTwa) { bestAbsTwa = twa; bestT = tm; }
    }
    return bestT;
}

// Assess BNO085 health for a full IMU recording. Three failure modes
// we've seen in the field:
//   'dead'    — sensor returns 0.0 for every sample (E2 on 2026-05-12:
//               BNO085 was nearly off its header pins → I²C bus open →
//               driver returned the default zero with no error surfaced).
//   'garbage' — sensor returns physically-impossible heel values
//               (E3 on 2026-05-12: range >300°, intermixed with stuck
//               runs — likely BNO firmware fault or wrong report mode).
//   'no-data' — too few samples to assess (boat skipped, IMU off, etc.).
//   'ok'      — usable.
// A boat flagged dead/garbage is dropped from the heel-derived charts
// (signature chart + roll-vs-loss scatter) and rendered with a warning
// badge in the legend and tables, instead of polluting the analysis
// with sensor failures.
function _rtAssessImuHealth(imuSamples) {
    if (!imuSamples || imuSamples.length < 10) return 'no-data';
    let n = 0, outliers = 0, mn = Infinity, mx = -Infinity;
    for (const s of imuSamples) {
        const h = Number(s?.heel);
        if (!Number.isFinite(h)) continue;
        n++;
        if (h < mn) mn = h;
        if (h > mx) mx = h;
        if (Math.abs(h) > 80) outliers++;
    }
    if (n < 10) return 'no-data';
    const range = mx - mn;
    // Dynamic range under 2° across an entire recording = sensor is
    // stuck (E2's symptom). Threshold is well below any real boat's
    // heel motion even in glassy conditions.
    if (range < 2) return 'dead';
    // More than 0.5% of samples beyond ±80° heel = the sensor is
    // returning something other than tilt (could be yaw, could be
    // quaternion-singularity garbage). E3 hit 0.9%.
    if (outliers / n > 0.005) return 'garbage';
    return 'ok';
}

function _rtBuildProfile(tack, layer, meta, idx) {
    const t0Ms = _rtFindT0(layer, tack);
    if (t0Ms == null) return null;
    const heelSamples = layer.imu || [];
    if (heelSamples.length < 5) return null;

    const heelSeries = _rtResampleSeries(
        heelSamples.filter(s => {
            const tm = s?.t ? new Date(s.t).getTime() : 0;
            return tm >= t0Ms - RT_PRE_SEC * 1000 - 2000 &&
                   tm <= t0Ms + RT_POST_SEC * 1000 + 2000;
        }),
        t0Ms,
        s => Number(s.heel),
    );
    const speedSeries = _rtResampleSeries(
        (layer.data || []).filter(s => {
            const tm = s?.t ? new Date(s.t).getTime() : 0;
            return tm >= t0Ms - RT_PRE_SEC * 1000 - 2000 &&
                   tm <= t0Ms + RT_POST_SEC * 1000 + 2000;
        }),
        t0Ms,
        s => Number(s.speed_kn),
    );

    // Require enough heel coverage in the action window [-2, +3] to be
    // meaningful — otherwise the metrics are unreliable.
    const heelCore = heelSeries.filter(p => p.tRel >= -2 && p.tRel <= 3 && p.value != null);
    if (heelCore.length < 6) return null;

    // Normalize heel sign so positive = "new tack's heeled-to side"
    // (= the side the boat ends up heeling toward after settling).
    // This makes every tack overlay with the same shape: starts
    // negative (old tack heel side), dips MORE negative during the
    // roll, swings sharply positive after the flatten.
    const preHeelAvg = _rtAvg(heelSeries.filter(p => p.tRel >= -5 && p.tRel <= -2).map(p => p.value));
    const sign = (preHeelAvg != null && preHeelAvg > 0) ? -1 : 1;
    const heelNorm = heelSeries.map(p => ({ tRel: p.tRel, value: p.value == null ? null : sign * p.value }));

    // Phase metrics ---------------------------------------------------
    const valsHeelCore = heelCore.map(p => sign * p.value);
    const heelMax = Math.max(...valsHeelCore);
    const heelMin = Math.min(...valsHeelCore);
    const heelRangeDeg = heelMax - heelMin;

    // Steady pre-tack heel in normalized frame (should be negative).
    const steadyPre = _rtAvg(
        heelNorm.filter(p => p.tRel >= -5 && p.tRel <= -2 && p.value != null).map(p => p.value)
    );
    // Peak windward overshoot: how far below steady_pre did heel dip?
    // (More negative = more excess roll = better roll-tack execution.)
    const peakWindwardNorm = Math.min(
        ...heelNorm.filter(p => p.tRel >= -2 && p.tRel <= 1 && p.value != null).map(p => p.value)
    );
    const peakRollExcessDeg = (steadyPre != null && Number.isFinite(peakWindwardNorm))
        ? Math.max(0, steadyPre - peakWindwardNorm) : 0;

    // Peak flatten rate: max |d(heel)/dt| in [-1, +2] window.
    let peakFlattenRateDegS = 0;
    const flatWin = heelNorm.filter(p => p.tRel >= -1 && p.tRel <= 2 && p.value != null);
    for (let i = 1; i < flatWin.length; i++) {
        const dh = flatWin[i].value - flatWin[i - 1].value;
        const dt = flatWin[i].tRel - flatWin[i - 1].tRel;
        if (dt > 0) {
            const r = Math.abs(dh / dt);
            if (r > peakFlattenRateDegS) peakFlattenRateDegS = r;
        }
    }

    // Speed metrics ---------------------------------------------------
    const approachSpeedKn = tack.speedBefore || 0;
    const minSpeedKn = tack.speedMin || 0;
    const speedLossPct = approachSpeedKn > 0
        ? Math.max(0, (approachSpeedKn - minSpeedKn) / approachSpeedKn * 100) : 0;
    const _spdAt = (tRel) => {
        const p = speedSeries.find(x => Math.abs(x.tRel - tRel) < RT_DT_S / 2);
        return p?.value;
    };
    const exitSpeedKn5  = _spdAt(5);
    const exitSpeedKn10 = _spdAt(10);
    // Recovery time: first tRel ≥ 0 where speed crosses back to 95% of approach.
    let recoveryTimeS = null;
    if (approachSpeedKn > 0) {
        const target = 0.95 * approachSpeedKn;
        for (const p of speedSeries) {
            if (p.tRel < 0 || p.value == null) continue;
            if (p.value >= target) { recoveryTimeS = p.tRel; break; }
        }
    }

    // Time in irons: count tRel samples where |TWA| < 10° around t0.
    let timeInIronsS = 0;
    const data = layer.data || [];
    for (const p of data) {
        const tm = new Date(p.t).getTime();
        if (tm < tack.tStart || tm > tack.tEnd) continue;
        const w = windAt(tm);
        if (!w || w.twd == null || p.course == null) continue;
        const twa = Math.abs(((w.twd - p.course + 540) % 360) - 180);
        if (twa < 10) timeInIronsS += 1;   // 1 Hz data ≈ 1 s per sample
    }

    const wasRolled = peakRollExcessDeg >= RT_ROLL_THRESHOLD_DEG;

    return {
        id: `rt_${meta.deviceId}_${idx}`,
        deviceId: meta.deviceId,
        team: meta.team,
        sailNumber: meta.sailNumber || '',
        color: meta.color,
        raceId: meta.raceId,
        raceName: meta.raceName,
        imuHealth: meta.imuHealth || 'ok',  // 'ok' | 'dead' | 'garbage' | 'no-data'
        t0Ms,
        tStartMs: tack.tStart,
        tEndMs: tack.tEnd,
        heelSeries: heelNorm,
        speedSeries,
        metrics: {
            approachSpeedKn, exitSpeedKn5, exitSpeedKn10, minSpeedKn,
            speedLossPct, recoveryTimeS,
            heelRangeDeg, peakRollExcessDeg, peakFlattenRateDegS,
            timeInIronsS, wasRolled,
        },
    };
}

function _rtAvg(arr) {
    const xs = arr.filter(v => v != null && Number.isFinite(v));
    if (!xs.length) return null;
    return xs.reduce((s, v) => s + v, 0) / xs.length;
}

// Boat label = team name + sail # (when present). Matches the
// convention used elsewhere (leaderboard, drawer, leg/maneuver tables).
function _rtBoatLabel(o) {
    const team = o.team || '';
    const sail = (o.sailNumber || '').trim();
    return sail ? `${team} #${sail}` : team;
}

// Gather profiles for every detected tack across the boats in the
// currently-loaded race. Sync.
function _rtGatherProfilesForCurrentRace() {
    const out = [];
    if (!raceData?.boats) return out;
    let idx = 0;
    for (const [deviceId, boatData] of Object.entries(raceData.boats)) {
        const layer = boatLayers[deviceId];
        if (!layer?.data?.length) continue;
        const team = boatData.boat?.boat_name || boatData.boat?.team_name || deviceId;
        const sailNumber = (boatData.boat?.sail_number != null ? String(boatData.boat.sail_number) : '').trim();
        const color = layer.color || '#888';
        // Assess IMU health ONCE per boat-recording (not per tack —
        // sensor failures are recording-wide). The flag propagates
        // through to every profile for this boat.
        const imuHealth = _rtAssessImuHealth(layer.imu);
        const win = racingWindowMsFor(boatData.boat, layer);
        const tacks = detectManeuversForLayer(layer, true)
            .filter(m => m.type === 'tack' && m.tStart >= win.startMs && m.tStart <= win.endMs);
        for (const t of tacks) {
            const meta = {
                deviceId, team, sailNumber, color, imuHealth,
                raceId: currentRace?.race_id, raceName: currentRace?.race_name,
            };
            const p = _rtBuildProfile(t, layer, meta, idx++);
            if (p) out.push(p);
        }
    }
    return out;
}

// Async: pull tacks from every other race on the day and add to the
// pool. Same pattern as ensureCrossDayTacks().
async function _rtGatherProfilesForDay() {
    const pool = _rtGatherProfilesForCurrentRace();
    if (!currentRaceDay?.races?.length) return pool;
    const others = currentRaceDay.races.filter(r => r.race_id !== currentRace?.race_id);
    let idx = pool.length;
    await Promise.all(others.map(async (r) => {
        try {
            const resp = await fetch(`${API_BASE}/api/races/${r.race_id}/data?sensors=gps,imu`);
            if (!resp.ok) return;
            const data = await resp.json();
            for (const [deviceId, boatData] of Object.entries(data.boats || {})) {
                const gps = boatData?.sensors?.gps;
                if (!gps?.length) continue;
                const team = boatData.boat?.boat_name || boatData.boat?.team_name || deviceId;
                const sailNumber = (boatData.boat?.sail_number != null ? String(boatData.boat.sail_number) : '').trim();
                const color = boatLayers[deviceId]?.color || '#888';
                const fakeLayer = { data: gps, imu: boatData?.sensors?.imu || [] };
                const imuHealth = _rtAssessImuHealth(fakeLayer.imu);
                const oStart = data.race?.start_time ? new Date(data.race.start_time).getTime() : -Infinity;
                const oEnd = _parseTimeToMs(boatData.boat?.finish_time)
                    ?? (data.race?.end_time ? new Date(data.race.end_time).getTime() : Infinity);
                const tacks = detectManeuversForLayer(fakeLayer, false)
                    .filter(m => m.type === 'tack' && m.tStart >= oStart && m.tStart <= oEnd);
                for (const t of tacks) {
                    const meta = {
                        deviceId, team, sailNumber, color, imuHealth,
                        raceId: r.race_id, raceName: r.race_name,
                    };
                    const p = _rtBuildProfile(t, fakeLayer, meta, idx++);
                    if (p) pool.push(p);
                }
            }
        } catch (e) {
            console.warn('[RollTacking] cross-day fetch failed for', r.race_id, e);
        }
    }));
    return pool;
}

function openRollTackingModal() {
    const modal = document.getElementById('roll-tacking-modal');
    const body = document.getElementById('roll-tacking-modal-body');
    if (!modal || !body) return;
    if (!raceData?.boats) {
        body.innerHTML = '<div class="rt-empty">Load a race first.</div>';
        modal.style.display = 'flex';
        return;
    }

    rollTackState = {
        scope: 'race',
        profiles: _rtGatherProfilesForCurrentRace(),
        crossDayProfiles: null,
        crossDayState: 'idle',
    };

    _rtRenderShell(body);
    _rtRenderContent(body);
    modal.style.display = 'flex';
}

function _rtRenderShell(body) {
    body.innerHTML = `
        <div class="rt-toolbar">
            <div class="rt-scope" role="tablist">
                <button data-scope="race" class="active" type="button">This race</button>
                <button data-scope="day" type="button">All races today</button>
            </div>
            <span class="rt-meta" id="rt-meta"></span>
            <div class="rt-legend" id="rt-legend"></div>
        </div>
        <div class="rt-phase-bar" title="Roll-tack phases (relative to head-to-wind)">
            <span style="flex:4; background:#475569;">1·Approach</span>
            <span style="flex:1; background:#64748b;">2·Roll</span>
            <span style="flex:0.5; background:#22d3ee;">3·HTW</span>
            <span style="flex:1.5; background:#fb923c;">4·Flatten</span>
            <span style="flex:8.5; background:#22c55e;">5·Exit / Recovery</span>
        </div>
        <div class="rt-charts-grid">
            <div class="rt-chart-card">
                <div class="rt-chart-title">Heel signature</div>
                <div class="rt-chart-sub">Normalized so positive = new-tack heel side. Deeper dip pre-zero = more windward roll. Steeper rise through zero = faster flatten.</div>
                <div class="rt-chart-canvas-wrap"><canvas id="rt-chart-heel"></canvas></div>
            </div>
            <div class="rt-chart-card">
                <div class="rt-chart-title">Speed signature</div>
                <div class="rt-chart-sub">SOG vs time around head-to-wind. Shallower dip + faster recovery = better roll-tack execution.</div>
                <div class="rt-chart-canvas-wrap"><canvas id="rt-chart-speed"></canvas></div>
            </div>
            <div class="rt-chart-card span-2">
                <div class="rt-chart-title">Roll amplitude vs speed loss</div>
                <div class="rt-chart-sub">One dot per detected tack. Bottom-right is best (more roll, less loss). Bottom-left = small flat tacks.</div>
                <div class="rt-chart-canvas-wrap" style="height:280px;"><canvas id="rt-chart-scatter"></canvas></div>
            </div>
        </div>
        <div class="rt-table-wrap">
            <h3>Per-boat aggregate</h3>
            <div id="rt-table-boat"></div>
        </div>
        <div class="rt-table-wrap">
            <h3>Every tack</h3>
            <div id="rt-table-tack"></div>
        </div>
    `;

    for (const btn of body.querySelectorAll('[data-scope]')) {
        btn.addEventListener('click', async () => {
            const scope = btn.getAttribute('data-scope');
            if (rollTackState.scope === scope) return;
            for (const b of body.querySelectorAll('[data-scope]')) b.classList.toggle('active', b === btn);
            rollTackState.scope = scope;
            if (scope === 'day' && !rollTackState.crossDayProfiles) {
                rollTackState.crossDayState = 'loading';
                document.getElementById('rt-meta').textContent = 'Loading other races…';
                rollTackState.crossDayProfiles = await _rtGatherProfilesForDay();
                rollTackState.crossDayState = 'ready';
            }
            _rtRenderContent(body);
        });
    }
}

function _rtRenderContent(body) {
    const profiles = rollTackState.scope === 'day'
        ? (rollTackState.crossDayProfiles || rollTackState.profiles)
        : rollTackState.profiles;

    // Meta line + boat legend
    const meta = document.getElementById('rt-meta');
    if (meta) {
        const nBoats = new Set(profiles.map(p => p.deviceId)).size;
        meta.textContent = `${profiles.length} tack${profiles.length === 1 ? '' : 's'} · ${nBoats} boat${nBoats === 1 ? '' : 's'}`;
    }
    const legendEl = document.getElementById('rt-legend');
    if (legendEl) {
        const byBoat = new Map();
        for (const p of profiles) {
            if (!byBoat.has(p.deviceId)) {
                byBoat.set(p.deviceId, {
                    team: p.team, sailNumber: p.sailNumber, color: p.color,
                    imuHealth: p.imuHealth || 'ok',
                });
            }
        }
        legendEl.innerHTML = [...byBoat.values()].map(b => {
            const badge = _rtImuBadge(b.imuHealth);
            return `<span class="rt-legend-item"><span class="rt-legend-swatch" style="background:${b.color}"></span>${_tdEscape(_rtBoatLabel(b))}${badge}</span>`;
        }).join('');
    }

    if (!profiles.length) {
        document.getElementById('rt-table-boat').innerHTML = '<div class="rt-empty">No tacks detected.</div>';
        document.getElementById('rt-table-tack').innerHTML = '';
        for (const id of ['rt-chart-heel', 'rt-chart-speed', 'rt-chart-scatter']) {
            if (_rtCharts[id]) { _rtCharts[id].destroy(); delete _rtCharts[id]; }
        }
        return;
    }

    // Heel-derived charts: drop boats whose IMU is dead/garbage so we
    // don't pollute the picture. Speed signature stays inclusive —
    // GPS is independent of the IMU failure.
    const heelHealthyProfiles = profiles.filter(p => (p.imuHealth || 'ok') === 'ok');

    _rtDrawHeelChart(heelHealthyProfiles);
    _rtDrawSpeedChart(profiles);
    _rtDrawScatter(heelHealthyProfiles);
    _rtRenderBoatTable(profiles);
    _rtRenderTackTable(profiles);
}

// Inline HTML badge for the per-boat IMU status, surfaced in the
// legend and tables. Empty string when 'ok' so the layout doesn't
// shift for healthy boats.
function _rtImuBadge(status) {
    if (status === 'dead')    return ' <span class="rt-imu-warn" title="BNO085 returned 0.0 for every sample — sensor likely disconnected. Heel chart suppressed.">⚠ IMU dead</span>';
    if (status === 'garbage') return ' <span class="rt-imu-warn" title="BNO085 returned physically impossible values — sensor likely miscalibrated or wrong report mode. Heel chart suppressed.">⚠ IMU garbage</span>';
    if (status === 'no-data') return ' <span class="rt-imu-warn rt-imu-no-data" title="No IMU samples recorded for this boat.">— no IMU</span>';
    return '';
}

// Compute per-boat mean series at each grid x by averaging across that
// boat's tacks. Faded individual lines + bold mean line per boat.
function _rtBuildBoatMeanDatasets(profiles, seriesKey, label) {
    const byBoat = new Map();   // deviceId → { team, sailNumber, color, perTack: [series, ...] }
    for (const p of profiles) {
        if (!byBoat.has(p.deviceId)) {
            byBoat.set(p.deviceId, { team: p.team, sailNumber: p.sailNumber, color: p.color, perTack: [] });
        }
        byBoat.get(p.deviceId).perTack.push(p[seriesKey]);
    }
    const datasets = [];
    for (const [_, b] of byBoat) {
        // Mean per x.
        const gridX = b.perTack[0].map(s => s.tRel);
        const meanY = gridX.map((x, ix) => {
            const vals = b.perTack.map(s => s[ix]?.value).filter(v => v != null && Number.isFinite(v));
            return vals.length ? vals.reduce((s, v) => s + v, 0) / vals.length : null;
        });
        // Faded individual tacks.
        for (const series of b.perTack) {
            datasets.push({
                label: '',
                data: series.map(p => ({ x: p.tRel, y: p.value })),
                borderColor: b.color + '55',
                borderWidth: 1,
                fill: false,
                pointRadius: 0,
                tension: 0.2,
                spanGaps: true,
            });
        }
        // Bold mean.
        datasets.push({
            label: `${_rtBoatLabel(b)} (mean of ${b.perTack.length})`,
            data: gridX.map((x, i) => ({ x, y: meanY[i] })),
            borderColor: b.color,
            borderWidth: 2.5,
            fill: false,
            pointRadius: 0,
            tension: 0.25,
            spanGaps: true,
        });
    }
    return datasets;
}

function _rtMakeOrUpdateChart(canvasId, config) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    if (_rtCharts[canvasId]) { _rtCharts[canvasId].destroy(); }
    _rtCharts[canvasId] = new Chart(canvas.getContext('2d'), config);
}

const _RT_PHASE_BANDS = [
    { from: -5,   to: -1,  color: 'rgba(71, 85, 105, 0.10)'  },   // approach
    { from: -1,   to:  0,  color: 'rgba(100, 116, 139, 0.18)' },  // roll
    { from:  0,   to:  0.4, color: 'rgba(34, 211, 238, 0.18)' },  // HTW
    { from:  0.4, to:  1.8, color: 'rgba(251, 146, 60, 0.16)' },  // flatten
    { from:  1.8, to: 10,   color: 'rgba(34, 197, 94, 0.10)'  },  // exit
];

const _rtPhaseBandPlugin = {
    id: 'rtPhaseBand',
    beforeDraw(chart) {
        const { ctx, chartArea, scales } = chart;
        if (!chartArea) return;
        const x = scales.x;
        ctx.save();
        for (const b of _RT_PHASE_BANDS) {
            const xL = x.getPixelForValue(b.from);
            const xR = x.getPixelForValue(b.to);
            ctx.fillStyle = b.color;
            ctx.fillRect(xL, chartArea.top, xR - xL, chartArea.bottom - chartArea.top);
        }
        // t0 line (head-to-wind).
        ctx.strokeStyle = 'rgba(34, 211, 238, 0.85)';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        const x0 = x.getPixelForValue(0);
        ctx.beginPath();
        ctx.moveTo(x0, chartArea.top);
        ctx.lineTo(x0, chartArea.bottom);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.restore();
    },
};

function _rtDrawHeelChart(profiles) {
    const datasets = _rtBuildBoatMeanDatasets(profiles, 'heelSeries', 'heel');
    _rtMakeOrUpdateChart('rt-chart-heel', {
        type: 'line',
        data: { datasets },
        plugins: [_rtPhaseBandPlugin],
        options: {
            responsive: true, maintainAspectRatio: false,
            animation: false,
            plugins: {
                legend: { display: false },
                tooltip: { enabled: false },
            },
            scales: {
                x: { type: 'linear', min: -RT_PRE_SEC, max: RT_POST_SEC,
                     title: { display: true, text: 'Seconds from head-to-wind' },
                     grid: { color: 'rgba(255,255,255,0.05)' },
                     ticks: { color: '#94a3b8' } },
                y: { title: { display: true, text: 'Heel (deg, +/− = new/old tack side)' },
                     grid: { color: 'rgba(255,255,255,0.05)' },
                     ticks: { color: '#94a3b8' } },
            },
        },
    });
}

function _rtDrawSpeedChart(profiles) {
    const datasets = _rtBuildBoatMeanDatasets(profiles, 'speedSeries', 'speed');
    _rtMakeOrUpdateChart('rt-chart-speed', {
        type: 'line',
        data: { datasets },
        plugins: [_rtPhaseBandPlugin],
        options: {
            responsive: true, maintainAspectRatio: false,
            animation: false,
            plugins: { legend: { display: false }, tooltip: { enabled: false } },
            scales: {
                x: { type: 'linear', min: -RT_PRE_SEC, max: RT_POST_SEC,
                     title: { display: true, text: 'Seconds from head-to-wind' },
                     grid: { color: 'rgba(255,255,255,0.05)' },
                     ticks: { color: '#94a3b8' } },
                y: { title: { display: true, text: 'SOG (kt)' },
                     grid: { color: 'rgba(255,255,255,0.05)' },
                     ticks: { color: '#94a3b8' }, beginAtZero: true },
            },
        },
    });
}

function _rtDrawScatter(profiles) {
    const byBoat = new Map();
    for (const p of profiles) {
        if (!byBoat.has(p.deviceId)) {
            byBoat.set(p.deviceId, { team: p.team, sailNumber: p.sailNumber, color: p.color, pts: [] });
        }
        byBoat.get(p.deviceId).pts.push({
            x: p.metrics.peakRollExcessDeg,
            y: p.metrics.speedLossPct,
        });
    }
    const datasets = [...byBoat.values()].map(b => ({
        label: _rtBoatLabel(b),
        data: b.pts,
        backgroundColor: b.color,
        borderColor: b.color,
        pointRadius: 5,
        pointHoverRadius: 7,
    }));
    _rtMakeOrUpdateChart('rt-chart-scatter', {
        type: 'scatter',
        data: { datasets },
        options: {
            responsive: true, maintainAspectRatio: false,
            animation: false,
            plugins: {
                legend: { display: true, labels: { color: '#94a3b8' } },
                tooltip: { callbacks: {
                    label: (ctx) => `${ctx.dataset.label}: roll ${ctx.parsed.x.toFixed(1)}°, loss ${ctx.parsed.y.toFixed(1)}%`,
                } },
            },
            scales: {
                x: { title: { display: true, text: 'Peak windward roll excess (deg)' },
                     grid: { color: 'rgba(255,255,255,0.05)' },
                     ticks: { color: '#94a3b8' }, beginAtZero: true },
                y: { title: { display: true, text: 'Speed loss (%)' },
                     grid: { color: 'rgba(255,255,255,0.05)' },
                     ticks: { color: '#94a3b8' }, beginAtZero: true },
            },
        },
    });
}

function _rtRenderBoatTable(profiles) {
    const byBoat = new Map();
    for (const p of profiles) {
        if (!byBoat.has(p.deviceId)) {
            byBoat.set(p.deviceId, {
                team: p.team, sailNumber: p.sailNumber, color: p.color,
                imuHealth: p.imuHealth || 'ok', tacks: [],
            });
        }
        byBoat.get(p.deviceId).tacks.push(p);
    }
    const rows = [...byBoat.values()].map(b => {
        const avg = (k) => _rtAvg(b.tacks.map(t => t.metrics[k]));
        const rolledCount = b.tacks.filter(t => t.metrics.wasRolled).length;
        return {
            team: b.team, sailNumber: b.sailNumber, color: b.color,
            imuHealth: b.imuHealth, n: b.tacks.length,
            rolledPct: 100 * rolledCount / b.tacks.length,
            peakRoll: avg('peakRollExcessDeg'),
            heelRange: avg('heelRangeDeg'),
            flatten: avg('peakFlattenRateDegS'),
            loss: avg('speedLossPct'),
            recovery: avg('recoveryTimeS'),
            irons: avg('timeInIronsS'),
        };
    }).sort((a, b) => (a.loss ?? 999) - (b.loss ?? 999));

    const fmt = (v, n = 1) => v == null || !Number.isFinite(v) ? '—' : v.toFixed(n);
    // For boats with bad IMU, blank out heel-derived columns so the
    // reader doesn't compare a real boat's roll to noise. Speed-loss
    // / recovery / in-irons stay populated (GPS-only).
    const fmtHeel = (r, val, n = 1) => (r.imuHealth === 'ok') ? fmt(val, n) : '—';
    const html = `
        <table class="rt-table">
            <thead><tr>
                <th>Boat</th><th>Tacks</th><th>Rolled %</th>
                <th>Peak roll<br>excess (°)</th><th>Heel<br>range (°)</th>
                <th>Flatten<br>rate (°/s)</th>
                <th>Speed<br>loss (%)</th><th>Recovery<br>(s)</th>
                <th>In irons<br>(s)</th>
            </tr></thead>
            <tbody>
                ${rows.map(r => `
                    <tr>
                        <td><span class="rt-boat-swatch" style="background:${r.color}"></span>${_tdEscape(_rtBoatLabel(r))}${_rtImuBadge(r.imuHealth)}</td>
                        <td>${r.n}</td>
                        <td>${(r.imuHealth === 'ok') ? fmt(r.rolledPct, 0) + '%' : '—'}</td>
                        <td>${fmtHeel(r, r.peakRoll)}</td>
                        <td>${fmtHeel(r, r.heelRange)}</td>
                        <td>${fmtHeel(r, r.flatten)}</td>
                        <td>${fmt(r.loss)}</td>
                        <td>${fmt(r.recovery)}</td>
                        <td>${fmt(r.irons, 0)}</td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;
    document.getElementById('rt-table-boat').innerHTML = html;
}

function _rtRenderTackTable(profiles) {
    const rows = profiles.slice().sort((a, b) => (a.t0Ms || 0) - (b.t0Ms || 0));
    const fmt = (v, n = 1) => v == null || !Number.isFinite(v) ? '—' : v.toFixed(n);
    const tt = (ms) => ms ? new Date(ms).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : '—';
    const html = `
        <table class="rt-table">
            <thead><tr>
                <th>Boat</th><th>Time</th>
                <th>Status</th>
                <th>Peak roll<br>excess (°)</th>
                <th>Heel<br>range (°)</th>
                <th>Flatten<br>rate (°/s)</th>
                <th>Approach<br>(kt)</th>
                <th>Min<br>(kt)</th>
                <th>Speed<br>loss (%)</th>
                <th>Recovery<br>(s)</th>
            </tr></thead>
            <tbody>
                ${rows.map(p => {
                    const m = p.metrics;
                    const imuOk = (p.imuHealth || 'ok') === 'ok';
                    const fH = (v, n = 1) => imuOk ? fmt(v, n) : '—';
                    const statusCell = imuOk
                        ? (m.wasRolled ? '<span class="rt-tag-rolled">rolled</span>' : '<span class="rt-tag-flat">flat</span>')
                        : '<span class="rt-imu-warn">⚠ no heel</span>';
                    return `
                        <tr>
                            <td><span class="rt-boat-swatch" style="background:${p.color}"></span>${_tdEscape(_rtBoatLabel(p))}</td>
                            <td>${tt(p.t0Ms)}</td>
                            <td>${statusCell}</td>
                            <td>${fH(m.peakRollExcessDeg)}</td>
                            <td>${fH(m.heelRangeDeg)}</td>
                            <td>${fH(m.peakFlattenRateDegS)}</td>
                            <td>${fmt(m.approachSpeedKn)}</td>
                            <td>${fmt(m.minSpeedKn)}</td>
                            <td>${fmt(m.speedLossPct)}</td>
                            <td>${fmt(m.recoveryTimeS)}</td>
                        </tr>
                    `;
                }).join('')}
            </tbody>
        </table>
    `;
    document.getElementById('rt-table-tack').innerHTML = html;
}

// Move the dashed play cursor on each chart without redrawing the data lines.
function updatePlayCursor(seconds) {
    playCursorSeconds = seconds;
    for (const ch of [speedChart, heelChart, windChart]) {
        if (ch) ch.draw();
    }
}

// --- Playback ---

function setupPlaybackControls() {
    const playBtn = document.getElementById('btn-play');
    const slider = document.getElementById('timeline-slider');
    const speedSelect = document.getElementById('playback-speed');

    playBtn.addEventListener('click', togglePlayback);

    slider.addEventListener('input', (e) => {
        const position = parseInt(e.target.value) / 1000;
        seekTo(position);
    });

    speedSelect.addEventListener('change', (e) => {
        playbackSpeed = parseFloat(e.target.value);
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
        // Never hijack typing — covers the tactics-drawer textarea,
        // the AI-coach chat input, modal inputs, and any contenteditable.
        const t = e.target;
        const tag = t && t.tagName;
        if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA' || (t && t.isContentEditable)) return;

        if (e.code === 'Space') {
            e.preventDefault();
            togglePlayback();
        } else if (e.code === 'ArrowLeft' || e.code === 'ArrowRight') {
            // ←/→ = 1 second nudge for precise frame-by-frame review.
            // Shift+← / Shift+→ = 10 s for faster scanning. raceDuration
            // is in seconds; seekTo() takes a 0..1 fraction.
            if (!raceDuration) return;
            e.preventDefault();
            const step = (e.shiftKey ? 10 : 1) * (e.code === 'ArrowRight' ? 1 : -1);
            const newSec = Math.max(0, Math.min(raceDuration, currentTime + step));
            seekTo(newSec / raceDuration);
        }
    });
}

function togglePlayback() {
    isPlaying = !isPlaying;
    const playBtn = document.getElementById('btn-play');
    playBtn.textContent = isPlaying ? '⏸' : '▶';

    if (isPlaying) {
        startPlayback();
    } else {
        stopPlayback();
    }
}

function startPlayback() {
    if (playbackInterval) clearInterval(playbackInterval);

    playbackInterval = setInterval(() => {
        currentTime += 0.1 * playbackSpeed;

        if (currentTime >= raceDuration) {
            currentTime = 0;
            stopPlayback();
            return;
        }

        updatePlaybackPosition();
    }, 100);
}

function stopPlayback() {
    isPlaying = false;
    document.getElementById('btn-play').textContent = '▶';
    if (playbackInterval) {
        clearInterval(playbackInterval);
        playbackInterval = null;
    }
}

function seekTo(position) {
    currentTime = position * raceDuration;
    updatePlaybackPosition();
}

function updatePlaybackPosition() {
    // Update slider
    const slider = document.getElementById('timeline-slider');
    slider.value = (currentTime / raceDuration) * 1000;

    // Update time display
    document.getElementById('time-current').textContent = formatTime(currentTime);
    document.getElementById('elapsed-time').textContent = formatTime(currentTime);

    // Wall-clock time at the cursor (race.start_time + elapsed). Shown
    // next to the elapsed counter so coaches can correlate with on-
    // water observations / videos that carry absolute timestamps.
    const wallEl = document.getElementById('time-wall');
    if (wallEl) {
        if (currentRace?.start_time) {
            const wallMs = new Date(currentRace.start_time).getTime() + currentTime * 1000;
            wallEl.textContent = new Date(wallMs).toLocaleTimeString('en-US', {
                hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
            });
        } else {
            wallEl.textContent = '';
        }
    }

    // Update boat positions on map
    updateBoatPositions(currentTime);

    // Update AIS traffic positions (map-only; never touches leaderboard)
    updateAISPositions(currentTime);

    // Weather overlay (HRRR wind / sea-breeze / tide / relief / dist) — driven by
    // the race clock. Init once (after map + race are ready), then sync each tick.
    if (window.SFWeather && map && currentRace?.start_time) {
        const wxMs = new Date(currentRace.start_time).getTime() + currentTime * 1000;
        if (!sfwInited) {
            sfwInited = true;
            SFWeather.init({
                map,
                date: currentRace.date || currentRace.start_time.slice(0, 10),
                onObsToggle: (show) => {
                    weatherObsHidden = !show;
                    updateAllWindMarkers(new Date(currentRace.start_time).getTime() + currentTime * 1000);
                },
            });
        }
        SFWeather.setTime(wxMs);
    }

    // Update leaderboard
    renderLeaderboard();
}

function formatTime(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
}

// --- Data Loading ---

// Show / hide the NOR / SI / Website anchors in the toolbar based on
// whether the loaded race's regatta carries each URL. Each anchor
// already has `target="_blank" rel="noopener"`, so a single href set
// is enough.
function updateRegattaDocsBar(regattaId) {
    const slots = [
        // The Website / NOR / SI links live in the Learning menu.
        { id: 'learning-doc-nor', field: 'nor_url' },
        { id: 'learning-doc-si',  field: 'si_url' },
        { id: 'learning-doc-web', field: 'website_url' },
    ];
    const regatta = regattaId ? (regattas || []).find(r => r.regatta_id === regattaId) : null;
    for (const slot of slots) {
        const el = document.getElementById(slot.id);
        if (!el) continue;
        const url = regatta && regatta[slot.field];
        if (url) {
            el.href = url;
            el.style.display = '';
        } else {
            el.removeAttribute('href');
            el.style.display = 'none';
        }
    }
}

async function loadRegattas() {
    try {
        const resp = await fetch(`${API_BASE}/api/regattas`);
        const data = await resp.json();
        regattas = data.regattas || [];

        // Track admin-only regattas so non-admins never see them in the
        // picker and their races never win the site-wide auto-load below.
        hiddenRegattaIds = new Set(
            regattas.filter(r => (r.visibility || 'public') === 'admin')
                    .map(r => r.regatta_id)
        );
        const pickerRegattas = IS_ADMIN
            ? regattas
            : regattas.filter(r => !hiddenRegattaIds.has(r.regatta_id));

        // Populate regatta selects
        const regattaSelect = document.getElementById('regatta-select');
        const regattaInput = document.getElementById('regatta-input');

        const options = pickerRegattas.map(r =>
            `<option value="${r.regatta_id}">${r.name}</option>`
        ).join('');

        regattaSelect.innerHTML = '<option value="">Select Regatta...</option>' +
            '<option value="__all__">All Races</option>' + options;
        regattaInput.innerHTML = '<option value="">None</option>' + options;

        // Clear dependent selects
        document.getElementById('raceday-select').innerHTML = '<option value="">Select Day...</option>';
        document.getElementById('race-select').innerHTML = '<option value="">Select Race...</option>';

    } catch (err) {
        console.error('[Race] Failed to load regattas:', err);
    }
}

async function loadRaceDays(regattaId) {
    const raceDaySelect = document.getElementById('raceday-select');
    const raceSelect = document.getElementById('race-select');

    if (!regattaId) {
        raceDaySelect.innerHTML = '<option value="">Select Day...</option>';
        raceSelect.innerHTML = '<option value="">Select Race...</option>';
        raceDays = [];
        races = [];
        return;
    }

    try {
        // Load races - either for specific regatta or all races
        const url = regattaId === '__all__'
            ? `${API_BASE}/api/races`
            : `${API_BASE}/api/races?regatta_id=${regattaId}`;
        const resp = await fetch(url);
        const data = await resp.json();
        const allRaces = data.races || [];

        // Group by date to get race days
        const dayMap = {};
        for (const race of allRaces) {
            if (!dayMap[race.date]) {
                dayMap[race.date] = [];
            }
            dayMap[race.date].push(race);
        }

        // Sort dates and create race days
        raceDays = Object.keys(dayMap).sort().map(date => ({
            date: date,
            races: dayMap[date].sort((a, b) => a.start_time.localeCompare(b.start_time)),
        }));

        // Populate race day select
        raceDaySelect.innerHTML = '<option value="">Select Day...</option>' +
            raceDays.map(d => {
                const raceCount = d.races.length;
                const dayName = new Date(d.date + 'T12:00:00').toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
                return `<option value="${d.date}">${dayName} (${raceCount} race${raceCount !== 1 ? 's' : ''})</option>`;
            }).join('');

        // Clear race select
        raceSelect.innerHTML = '<option value="">Select Race...</option>';
        races = [];

        console.log('[Race] Loaded race days:', raceDays);

    } catch (err) {
        console.error('[Race] Failed to load race days:', err);
    }
}

function loadRacesForDay(date) {
    const raceSelect = document.getElementById('race-select');

    if (!date) {
        raceSelect.innerHTML = '<option value="">Select Race...</option>';
        races = [];
        currentRaceDay = null;
        return;
    }

    // Find the race day
    currentRaceDay = raceDays.find(d => d.date === date);
    if (!currentRaceDay) {
        raceSelect.innerHTML = '<option value="">Select Race...</option>';
        races = [];
        return;
    }

    races = currentRaceDay.races;

    // Populate race select with race name and start time (local time)
    raceSelect.innerHTML = '<option value="">Select Race...</option>' +
        races.map(r => {
            const startLocal = new Date(r.start_time);
            const startTime = startLocal.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
            return `<option value="${r.race_id}">${r.name} @ ${startTime}</option>`;
        }).join('');

    console.log('[Race] Loaded races for', date, ':', races);
}

async function loadRaceData(raceId) {
    try {
        // Load race definition
        const raceResp = await fetch(`${API_BASE}/api/races/${raceId}`);
        currentRace = await raceResp.json();

        // Stash any new team / boat names from this race so the
        // race-edit autocomplete picks them up on subsequent edits,
        // even if the user is just browsing (didn't open the modal).
        rememberRaceNames(currentRace);

        // Restore the per-user class filter (?class= URL param wins,
        // else last localStorage value, else 'all'). Done before any
        // render so the toggle paints in the right state from the
        // first frame instead of flashing.
        initClassFilterFromPersistence();

        // Merge boat-catalog metadata for any race boats that reference
        // boat_id. Photos / links / skipper / LOA flow from the catalog
        // into the per-race object so the rest of the dashboard can
        // read them off currentRace.boats[*] without having to know
        // about the catalog. Legacy races (no boat_id) keep working
        // unchanged.
        await hydrateBoatsFromCatalog(currentRace);

        // Update UI with local time
        document.getElementById('race-name').textContent = currentRace.name;
        const startLocal = new Date(currentRace.start_time);
        const localTimeStr = startLocal.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
        document.getElementById('race-time').textContent = `${currentRace.date} ${localTimeStr}`;
        document.getElementById('btn-edit-race').disabled = false;
        document.getElementById('btn-duplicate-race').disabled = false;
        const editCourseBtn = document.getElementById('btn-edit-course');
        if (editCourseBtn) editCourseBtn.disabled = false;
        // Copy-course-to-next button: requires a next race on this
        // day AND a course on the current race to copy. Recomputed
        // here on every race load.
        updateCopyCourseButton();

        // Surface the regatta's public docs (NOR / SI / Website) in
        // the toolbar — read from the in-memory `regattas` list that
        // loadRegattas() populated at app start.
        updateRegattaDocsBar(currentRace.regatta_id);

        // Load sensor data for all boats
        const dataResp = await fetch(`${API_BASE}/api/races/${raceId}/data?sensors=gps,imu,wind`);
        raceData = await dataResp.json();

        console.log('[Race] Race time window:', currentRace.start_time, 'to', currentRace.end_time);
        console.log('[Race] Loaded data:', raceData);

        // Calculate race duration
        const start = new Date(currentRace.start_time).getTime();
        const end = new Date(currentRace.end_time).getTime();
        raceDuration = (end - start) / 1000;

        // Update time display
        document.getElementById('time-total').textContent = formatTime(raceDuration);

        // Clear existing layers and add new ones
        clearBoatLayers();
        clearCourseLayers();

        let totalGpsPoints = 0;
        for (const [deviceId, boatData] of Object.entries(raceData.boats)) {
            if (boatData.error || !boatData.sensors?.gps?.length) {
                console.warn(`[Race] No GPS data for ${deviceId}:`, boatData.error || 'empty array');
                continue;
            }

            const gpsCount = boatData.sensors.gps.length;
            totalGpsPoints += gpsCount;
            console.log(`[Race] ${deviceId}: ${gpsCount} GPS points, first:`, boatData.sensors.gps[0]);
            addBoatTrack(deviceId, boatData.sensors.gps, boatData.boat, boatData.sensors.imu || []);
        }

        console.log(`[Race] Total GPS points: ${totalGpsPoints}, boatLayers:`, Object.keys(boatLayers));

        // AIS traffic overlay (non-participant vessels). Separate fetch,
        // separate layer — fire-and-forget so it never blocks the race
        // render; it self-clears the previous race and hides its own
        // toggle when a race has no AIS data.
        loadRaceAIS(raceId);

        // Initial framing: always zoom to start line + first mark on
        // race load — that's where the action begins regardless of
        // whether follow-mode is enabled. The follow-mode throttle is
        // armed (lastFollowPanMs = now) so the first updateBoatPositions
        // tick doesn't immediately re-fly past this framing; subsequent
        // playback ticks then take over once enough time has passed.
        fitMapToBounds();
        lastFollowPanMs = Date.now();

        // Render course (marks + start/finish lines) if present
        renderCourseViewLayer(currentRace);

        // Apply race-level boat class (3 × LOA → mark-zone radius). Must
        // run after renderCourseViewLayer so the freshly-built zone
        // circles are resized in place.
        applyRaceBoatClass();

        // RRS 18 zone sizing per mark: replace the static
        // "largest-boat × 3" fallback with the actual rule —
        // 3 × LOA of the first boat to enter the mark. Runs after
        // applyRaceBoatClass so its values are an upper-bound
        // fallback for marks no boat ever entered.
        computeMarkZonesFirstToEnter();

        // Pre-compute course progress (mark roundings + leg lengths) per
        // boat. Cheap to do once, drives the leaderboard ranking and VMG.
        precomputeAllRoundings();

        // Fetch nearby NOAA wind (Castle Island / Logan / Boston 16NM).
        // Awaited so laylines + the wind chart can use it on first render.
        await loadRaceWindData(currentRace.start_time, currentRace.end_time);

        // Render legend and leaderboard
        renderBoatLegend();
        renderLeaderboard();
        renderClassStartJumps();

        // Apply the multi-class filter to the freshly-added map layers.
        // No-op for single-class races. Must run after addBoatTrack so
        // every track/marker is already on the map.
        applyClassFilterToMap();

        // Lay out laylines from the next windward mark using race-average TWD.
        renderLaylines();

        // Update speed/heel/wind charts (wind chart now uses NOAA TWD)
        updateSpeedChart();

        // Reset playback
        currentTime = 0;
        updatePlaybackPosition();

        console.log('[Race] Loaded race data:', currentRace.name);

        // Honor a ?t=NN permalink: jump the playback cursor to that
        // many seconds from race start. Clamp to [0, raceDuration].
        // Pause so the user lands ON the moment instead of streaming
        // past it, and only do this on first load (subsequent loads
        // for the same race re-read the URL).
        try {
            const params = new URLSearchParams(location.search);
            const tParam = params.get('t');
            if (tParam != null) {
                const tSec = Math.max(0, Math.min(parseInt(tParam, 10) || 0, raceDuration));
                if (isPlaying) togglePlayback();
                currentTime = tSec;
                updatePlaybackPosition();
                updatePlayCursor(tSec);
            }
            // ?focus=fleet → zoom map to the live boats at the cursor
            // moment instead of showing the whole race overview. The
            // setTimeout gives updatePlaybackPosition (above) a tick to
            // refresh layer.current via updateBoatPositions; without it
            // we'd fit to stale (pre-load) marker positions.
            if (params.get('focus') === 'fleet') {
                setTimeout(focusMapOnFleet, 300);
            }
        } catch (e) {
            console.warn('[Race] URL param parse failed:', e);
        }

        // ?tactics=1 → open the discussion drawer on first load. Used by
        // shared WhatsApp links so the recipient lands inside the
        // conversation instead of having to hunt for the button.
        if (typeof _tdMaybeAutoOpenFromUrl === 'function') _tdMaybeAutoOpenFromUrl();

        // Attach the race-coach chat panel. The callback is recomputed
        // on every chat turn so the briefing reflects the latest state
        // (e.g. if the user changes wind source mid-conversation).
        if (window.SailFramesChat?.attach) {
            const ctxFn = () => {
                const allManeuvers = [];
                for (const [deviceId, layer] of Object.entries(boatLayers)) {
                    if (typeof detectManeuversForLayer !== 'function') break;
                    const boat = raceData?.boats?.[deviceId]?.boat;
                    const team = boat?.boat_name || boat?.team_name || deviceId;
                    const win = racingWindowMsFor(boat, layer);
                    for (const m of detectManeuversForLayer(layer, true)) {
                        if (m.tStart < win.startMs || m.tStart > win.endMs) continue;
                        allManeuvers.push({ deviceId, team, ...m });
                    }
                }
                return {
                    currentRace,
                    raceDataBoats: raceData?.boats || {},
                    boatLayers,
                    legRows: typeof computeLegSummary === 'function' ? computeLegSummary() : [],
                    maneuvers: allManeuvers,
                    weatherWindSamples,
                    weatherWindSource,
                    raceContextLabel: typeof buildRaceContextLabel === 'function' ? buildRaceContextLabel() : null,
                    // Current playback cursor in seconds from race
                    // start. Read live by the chat panel so its
                    // "attach race-cursor time" feature can capture
                    // whatever the user is looking at right now.
                    currentTimeSec: Math.max(0, Math.round(currentTime || 0)),
                    raceStartTime: currentRace?.start_time || null,
                    // Wind-station catalogue + currently-selected id so
                    // the briefing can embed the chosen sensor's
                    // coordinates (Castle Is, Logan, FLEET, ...).
                    raceBuoyData,
                    selectedWindStationId,
                    finishOrder: null,  // leaderboard order is recomputed per playback tick;
                                         // briefing's per-boat finish_position derives from
                                         // the per-boat roundingTimes instead.
                };
            };
            window.SailFramesChat.attach(ctxFn);
        }

    } catch (err) {
        console.error('[Race] Failed to load race data:', err);
        alert('Failed to load race data. Check console for details.');
    }
}

// --- Race Editor Modal ---

async function loadAvailableSessions() {
    try {
        const resp = await fetch(`${API_BASE}/api/sessions`);
        const data = await resp.json();
        const sessions = data.sessions || [];

        // Group sessions by device
        availableSessions = {};
        for (const session of sessions) {
            const deviceId = session.device_id;
            if (!availableSessions[deviceId]) {
                availableSessions[deviceId] = [];
            }
            // Full session path is "date-session_id" (e.g., "2026-04-19-154818")
            const fullPath = `${session.date}-${session.session_id}`;

            // Format start time in LOCAL time (not UTC)
            let startTimeStr = '';
            if (session.start_time) {
                const startDate = new Date(session.start_time);
                startTimeStr = startDate.toLocaleTimeString('en-US', {
                    hour: '2-digit',
                    minute: '2-digit',
                    hour12: true
                });
            }

            // Get duration in minutes
            let durationMin = '?';
            if (session.duration_minutes !== undefined && session.duration_minutes !== null) {
                durationMin = session.duration_minutes;
            } else if (session.duration_sec !== undefined && session.duration_sec !== null) {
                durationMin = Math.round(session.duration_sec / 60);
            }

            availableSessions[deviceId].push({
                path: fullPath,
                label: `${session.date} @ ${startTimeStr} (${durationMin}min)`,
                name: session.name || '',
            });
        }
        console.log('[Race] Loaded sessions:', availableSessions);
    } catch (err) {
        console.error('[Race] Failed to load sessions:', err);
    }
}

// `isDuplicatingRace` flag rides through saveRace so the modal's PATCH
// vs POST decision honors a duplicate intent even though currentRace
// still has the original race_id. Reset on close + after each save.
let isDuplicatingRace = false;

async function openRaceModal(race = null, opts = {}) {
    const duplicate = !!opts.duplicate;
    const modal = document.getElementById('race-modal');
    const title = document.getElementById('modal-title');
    const deleteBtn = document.getElementById('btn-delete-race');

    // Reset staged GPX files whenever modal opens
    pendingGpxFiles = {};
    pendingGpxFilesByBoatId = {};
    pendingVkxFilesByBoatId = {};

    // Load available sessions for dropdown
    await loadAvailableSessions();

    // Build the boat-class dropdown once per page lifetime.
    populateBoatClassDropdown();

    if (race && duplicate) {
        title.textContent = 'Duplicate Race';
        populateRaceForm(race);
        deleteBtn.style.display = 'none';   // No delete on a not-yet-saved race
    } else if (race) {
        title.textContent = 'Edit Race';
        populateRaceForm(race);
        deleteBtn.style.display = IS_ADMIN ? 'block' : 'none';  // Show delete button for admins only
    } else {
        title.textContent = 'New Race';
        clearRaceForm();
        deleteBtn.style.display = 'none';   // Hide delete button for new races
    }

    modal.style.display = 'flex';
}

// Open the modal pre-filled with currentRace's data, but treat the
// save as a fresh POST. Copies: name (suffixed), date, regatta,
// boat_class, boats (with their session_path / gpx_path), start/finish
// lines, marks, course. Skips: race_id, start_time, end_time,
// finish_order, results — race-instance state that must be set fresh
// for the new race.
async function duplicateRace() {
    if (!currentRace?.race_id) {
        alert('Load a race first to duplicate it.');
        return;
    }
    const draft = JSON.parse(JSON.stringify(currentRace));
    delete draft.race_id;
    draft.start_time = null;
    draft.end_time = null;
    draft.finish_order = [];
    draft.results = null;
    draft.name = `${currentRace.name || 'Race'} (copy)`;

    isDuplicatingRace = true;
    await openRaceModal(draft, { duplicate: true });
}

function closeRaceModal() {
    document.getElementById('race-modal').style.display = 'none';
    isDuplicatingRace = false;
}

function handleGpxFileSelect(event, deviceId) {
    const file = event.target.files[0];
    if (!file) return;

    pendingGpxFiles[deviceId] = file;

    const row = document.querySelector(`.boat-assignment[data-device="${deviceId}"]`);
    if (!row) return;

    row.dataset.gpxPath = '';  // Will be set by server after upload
    row.querySelector('.session-select').classList.add('hidden');
    row.querySelector('.gpx-badge').classList.remove('hidden');
    row.querySelector('.gpx-badge-name').textContent = file.name;
}

function handleGpxClear(deviceId) {
    delete pendingGpxFiles[deviceId];

    const row = document.querySelector(`.boat-assignment[data-device="${deviceId}"]`);
    if (!row) return;

    row.dataset.gpxPath = '';
    row.querySelector('.session-select').classList.remove('hidden');
    row.querySelector('.gpx-badge').classList.add('hidden');
    row.querySelector('.gpx-badge-name').textContent = '';

    const fileInput = row.querySelector('.gpx-file-input');
    if (fileInput) fileInput.value = '';
}

async function uploadPendingGpxFiles(raceId) {
    // Returns a summary that saveRace surfaces to the user — every
    // failure path used to log silently to the console only, which
    // hid real problems (file dropped on the floor, parse errors,
    // network failures, etc.). Counts AND error messages flow back
    // so the user can act.
    const summary = { ok: [], failed: [], reports: [] };

    async function uploadOne(label, url, file) {
        const formData = new FormData();
        formData.append('file', file);
        try {
            const resp = await fetch(url, { method: 'POST', body: formData });
            if (resp.ok) {
                const result = await resp.json();
                summary.ok.push({ label, file: file.name, ...result });
                if (result.report_url) summary.reports.push(result.report_url);
                console.log(`[Race] uploaded ${label} (${file.name}): ${result.points} points`);
            } else {
                const text = await resp.text();
                summary.failed.push({ label, file: file.name, status: resp.status, error: text });
                console.error(`[Race] upload failed ${label}:`, resp.status, text);
            }
        } catch (err) {
            summary.failed.push({ label, file: file.name, error: err?.message || String(err) });
            console.error(`[Race] upload error ${label}:`, err);
        }
    }

    // Device-keyed GPX (fleet trackers E1..E6).
    for (const [deviceId, file] of Object.entries(pendingGpxFiles)) {
        await uploadOne(`GPX → ${deviceId}`,
            `${API_BASE}/api/races/${raceId}/boats/${deviceId}/gpx`, file);
    }
    pendingGpxFiles = {};

    // Boat-id-keyed GPX (handicap boats — Waterspeed, Garmin, etc.).
    for (const [boatId, file] of Object.entries(pendingGpxFilesByBoatId)) {
        await uploadOne(`GPX → boat ${boatId}`,
            `${API_BASE}/api/races/${raceId}/boats-by-id/${boatId}/gpx`, file);
    }
    pendingGpxFilesByBoatId = {};

    // Boat-id-keyed Vakaros .vkx.
    for (const [boatId, file] of Object.entries(pendingVkxFilesByBoatId)) {
        await uploadOne(`VKX → boat ${boatId}`,
            `${API_BASE}/api/races/${raceId}/boats-by-id/${boatId}/vkx`, file);
    }
    pendingVkxFilesByBoatId = {};

    // Pop each VKX report in its own tab.
    for (const url of summary.reports) {
        try { window.open(url, '_blank', 'noopener'); } catch {}
    }

    return summary;
}

function clearRaceForm() {
    document.getElementById('race-name-input').value = '';
    document.getElementById('race-date-input').value = new Date().toISOString().split('T')[0];
    document.getElementById('start-time-input').value = '18:00';
    document.getElementById('end-time-input').value = '18:30';

    // Pre-fill the regatta from the toolbar selection so "+ New Race"
    // under a chosen regatta carries the context across; boat_class
    // then inherits from that regatta. Setting .value programmatically
    // does NOT fire `change`, so this won't loop with the change
    // listener we wire below.
    const toolbarRegatta = document.getElementById('regatta-select')?.value || '';
    const initialRegattaId = (toolbarRegatta && toolbarRegatta !== '__all__') ? toolbarRegatta : '';
    document.getElementById('regatta-input').value = initialRegattaId;

    const seedRegatta = initialRegattaId
        ? (regattas || []).find(r => r.regatta_id === initialRegattaId)
        : null;
    setBoatClassInForm(seedRegatta?.boat_class || null);   // defaults to J/80 if none

    // "New Race" defaults to single-class fleet mode. If the modal was
    // last opened on a handicap race, the PHRF section is still display
    // toggled on; reset both before rendering so the next save isn't
    // built from a stale roster.
    const fs = document.getElementById('boat-assignments-section');
    const ps = document.getElementById('phrf-roster-section');
    const gs = document.getElementById('gps-attach-section');
    if (fs) fs.style.display = '';
    if (ps) ps.style.display = 'none';
    if (gs) gs.style.display = 'none';

    // Default boat assignments (6 boats)
    renderBoatAssignments([
        { device_id: 'E1', boat_name: '', team_name: '', sail_number: '' },
        { device_id: 'E2', boat_name: '', team_name: '', sail_number: '' },
        { device_id: 'E3', boat_name: '', team_name: '', sail_number: '' },
        { device_id: 'E4', boat_name: '', team_name: '', sail_number: '' },
        { device_id: 'E5', boat_name: '', team_name: '', sail_number: '' },
        { device_id: 'E6', boat_name: '', team_name: '', sail_number: '' },
    ]);
}

function populateRaceForm(race) {
    document.getElementById('race-name-input').value = race.name || '';
    document.getElementById('race-date-input').value = race.date || '';

    // Convert UTC times to local time for display
    if (race.start_time) {
        const startLocal = new Date(race.start_time);
        document.getElementById('start-time-input').value =
            startLocal.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } else {
        document.getElementById('start-time-input').value = '';
    }

    if (race.end_time) {
        const endLocal = new Date(race.end_time);
        document.getElementById('end-time-input').value =
            endLocal.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } else {
        document.getElementById('end-time-input').value = '';
    }

    document.getElementById('regatta-input').value = race.regatta_id || '';
    setBoatClassInForm(race.boat_class || null);

    // Multi-class handicap races use the PHRF roster editor; everything
    // else uses the 6-device fleet-assignments editor. The two sections
    // are mutually exclusive in the modal.
    const isHandicap = Array.isArray(race.classes) && race.classes.length > 0;
    const fleetSection = document.getElementById('boat-assignments-section');
    const phrfSection = document.getElementById('phrf-roster-section');
    const gpsAttachSection = document.getElementById('gps-attach-section');
    const classStartsSection = document.getElementById('class-starts-section');
    if (isHandicap) {
        if (fleetSection) fleetSection.style.display = 'none';
        if (phrfSection) phrfSection.style.display = '';
        if (gpsAttachSection) gpsAttachSection.style.display = '';
        if (classStartsSection) classStartsSection.style.display = '';
        renderClassStartsEditor(race.classes || []);
        renderPHRFRoster(race.boats || []);
        renderGPSAttachStrip();
    } else {
        if (fleetSection) fleetSection.style.display = '';
        if (phrfSection) phrfSection.style.display = 'none';
        if (gpsAttachSection) gpsAttachSection.style.display = 'none';
        if (classStartsSection) classStartsSection.style.display = 'none';
        renderBoatAssignments(race.boats || []);
        renderFinishOrder(race.finish_order || [], race.boats || []);
    }
}

// Render the per-class jump chips in the timeline footer. One button
// per class with a start_time; click → scrub the playback cursor to
// that elapsed offset (and pause). Hidden when there's nothing useful
// to jump to (0 or 1 class, or no start times yet).
function renderClassStartJumps() {
    const el = document.getElementById('class-start-jumps');
    if (!el) return;
    const classes = (currentRace?.classes || []).filter(c => c.start_time);
    const raceStartMs = currentRace?.start_time
        ? new Date(currentRace.start_time).getTime() : null;
    if (classes.length < 2 || raceStartMs == null) {
        el.hidden = true;
        el.innerHTML = '';
        return;
    }
    // Earliest start first — matches the order the on-water sequence ran.
    const sorted = classes.slice().sort((a, b) =>
        new Date(a.start_time).getTime() - new Date(b.start_time).getTime());
    el.hidden = false;
    el.innerHTML = sorted.map(c => {
        const ms = new Date(c.start_time).getTime();
        const elapsedSec = Math.max(0, Math.round((ms - raceStartMs) / 1000));
        const wall = new Date(ms).toLocaleTimeString('en-GB',
            { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        const name = c.name || c.id || '?';
        return `<button type="button" class="class-start-jump"
            data-elapsed="${elapsedSec}"
            title="Jump playback to ${_attrEsc(name)} start (${_attrEsc(wall)})">
            <span class="csj-arrow">→</span>
            <span class="csj-name">${_attrEsc(name)}</span>
            <span class="csj-time">${_attrEsc(wall)}</span>
        </button>`;
    }).join('');
    el.querySelectorAll('.class-start-jump').forEach(btn => {
        btn.addEventListener('click', () => {
            const tSec = Number(btn.dataset.elapsed);
            if (!Number.isFinite(tSec)) return;
            if (isPlaying) togglePlayback();
            window.seekTo(tSec);
        });
    });
}

// Per-class start-time editor — one row per class[*], time-of-day
// picker bound to classes[i].start_time. Edits write back into
// currentRace.classes so the existing classes-round-trip path in
// getFormData() picks them up unchanged.
function renderClassStartsEditor(classes) {
    const container = document.getElementById('class-starts-list');
    if (!container) return;
    if (!classes.length) { container.innerHTML = ''; return; }
    container.innerHTML = classes.map((c, idx) => {
        const label = c.name || c.id || `Class ${idx + 1}`;
        const local = _isoToLocalHMS(c.start_time);
        return `
            <div class="class-start-row" data-class-idx="${idx}">
                <span class="class-start-label">${_attrEsc(label)}</span>
                <input type="time" step="1"
                       class="class-start-input"
                       data-class-idx="${idx}"
                       value="${_attrEsc(local)}">
                <span class="class-start-hint">${_attrEsc(c.rating_type || c.rating_system || '')}</span>
            </div>
        `;
    }).join('');
    container.querySelectorAll('.class-start-input').forEach(inp => {
        inp.addEventListener('change', () => {
            const idx = Number(inp.dataset.classIdx);
            const cls = currentRace?.classes?.[idx];
            if (!cls) return;
            const iso = _localHMSToIso(inp.value, cls.start_time);
            if (iso) cls.start_time = iso;
        });
    });
}

// "2026-05-27T22:35:00Z" → "18:35:00" (browser local). Returns ''
// if missing. Used by the class-starts editor to pre-fill <input type="time">.
function _isoToLocalHMS(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (!Number.isFinite(d.getTime())) return '';
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// Convert HH:MM[:SS] from <input type="time"> back to an ISO UTC string,
// keeping the existing date portion of prevIso (so we don't accidentally
// move the race a day around when only the time-of-day changed).
function _localHMSToIso(hms, prevIso) {
    if (!hms) return null;
    const parts = hms.split(':').map(Number);
    if (parts.length < 2 || parts.some(p => !Number.isFinite(p))) return null;
    const [h, m, s = 0] = parts;
    const base = prevIso ? new Date(prevIso) : new Date();
    if (!Number.isFinite(base.getTime())) return null;
    base.setHours(h, m, s, 0);
    return base.toISOString().replace('.000Z', 'Z');
}

// PHRF roster: one row per boat in the regatta sheet. Editable field
// is device_id only — pick from the unused E1..E6 slots or "No GPS".
// Boat name / class / rating / finish_time stay read-only here; they
// live in the seed script and don't change race-to-race.
function renderPHRFRoster(boats) {
    const container = document.getElementById('phrf-roster');
    if (!container) return;

    const ALL_DEVICES = ['E1', 'E2', 'E3', 'E4', 'E5', 'E6', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6'];

    container.innerHTML = boats.map((boat, idx) => {
        const yacht = (boat.boat_name || '').trim();
        const team = (boat.team_name || '').trim();
        // Identity priority matches the leaderboard: boat name first.
        const displayName = yacht || team || (boat.sail_number ? `#${boat.sail_number}` : `Boat ${idx + 1}`);
        const subtitle = [team && yacht ? team : null, boat.sail_number ? `#${boat.sail_number}` : null, boat.boat_type]
            .filter(Boolean).join(' · ');
        const rating = (typeof boat.rating === 'number') ? boat.rating.toFixed(3) : '—';
        const cls = boat.class || '—';
        const finishLocal = boat.finish_time
            ? new Date(boat.finish_time).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
            : (boat.finish_status || '—');
        const currentDevice = boat.device_id || '';

        const opts = ['<option value="">No GPS</option>']
            .concat(ALL_DEVICES.map(d => `<option value="${d}"${d === currentDevice ? ' selected' : ''}>${d}</option>`))
            .join('');

        // Per-row track upload — separate from the device dropdown so
        // boats without a fleet tracker can still attach an external
        // GPS track. Two formats accepted:
        //   • .gpx   — generic GPX (phone app, Garmin, RaceQs, etc.)
        //   • .vkx   — Vakaros Atlas binary (parsed server-side)
        // Both stage keyed by boat_id and upload on save to their
        // own endpoints; the server writes both into the same
        // by-boat-id gpx_path so the dashboard reads them identically.
        const bid = boat.boat_id || '';
        const hasPendingBoatGpx = bid && !!pendingGpxFilesByBoatId[bid];
        const hasPendingBoatVkx = bid && !!pendingVkxFilesByBoatId[bid];
        const hasUploadedGpx = !!boat.gpx_path;
        const isOwnGpx = hasUploadedGpx && /\/by-boat-id\//.test(boat.gpx_path || '');
        let gpxBadge = '';
        if (hasPendingBoatVkx) {
            gpxBadge = `<span class="phrf-gpx-badge" title="staged: ${_attrEsc(pendingVkxFilesByBoatId[bid].name)}">📍 VKX (staged)</span>`;
        } else if (hasPendingBoatGpx) {
            gpxBadge = `<span class="phrf-gpx-badge" title="staged: ${_attrEsc(pendingGpxFilesByBoatId[bid].name)}">📍 GPX (staged)</span>`;
        } else if (isOwnGpx) {
            gpxBadge = `<span class="phrf-gpx-badge" title="track uploaded — overrides any device session">📍 Track</span>`;
        }
        const trackBtns = bid ? `
            <label class="phrf-gpx-btn" title="Upload GPX track — works without a fleet device">+ GPX<input type="file" accept=".gpx" data-phrf-gpx data-boat-id="${_attrEsc(bid)}" style="display:none"></label>
            <label class="phrf-gpx-btn" title="Upload Vakaros Atlas .vkx binary log">+ VKX<input type="file" accept=".vkx" data-phrf-vkx data-boat-id="${_attrEsc(bid)}" style="display:none"></label>
        ` : '';

        return `
            <div class="phrf-row" data-idx="${idx}" data-boat-id="${_attrEsc(bid)}">
                <span class="phrf-class phrf-cls-${_attrEsc(cls)}">${_attrEsc(cls)}</span>
                <div class="phrf-name-block">
                    <div class="phrf-name">${_attrEsc(displayName)}</div>
                    <div class="phrf-sub">${_attrEsc(subtitle)}</div>
                </div>
                <span class="phrf-rating" title="ORR-EZ rating (multiplier)">${rating}</span>
                <span class="phrf-finish" title="Finish wall-clock">${_attrEsc(finishLocal)}</span>
                <select class="phrf-device" data-field="device_id" title="GPS tracker assignment">
                    ${opts}
                </select>
                <div class="phrf-gpx-cell">${gpxBadge}${trackBtns}</div>
            </div>
        `;
    }).join('');

    // Bind the delegated change listener ONCE per container. Previously
    // every render added a new listener, so by the second file-pick
    // we'd handle the change twice (staging same file repeatedly,
    // triggering recursive re-renders). The container itself is never
    // recreated (only its innerHTML is wiped), so the listener
    // survives renders.
    if (!container.dataset.changeBound) {
        container.addEventListener('change', (e) => {
            // Per-row GPX file picker → stage in pendingGpxFilesByBoatId
            if (e.target?.matches?.('input[type=file][data-phrf-gpx]')) {
                const inp = e.target;
                const bid = inp.dataset.boatId;
                const file = inp.files?.[0];
                inp.value = '';
                if (!bid || !file) return;
                pendingGpxFilesByBoatId[bid] = file;
                // Staging GPX clears any staged VKX for the same boat
                // — a boat ends up with one track source per race.
                delete pendingVkxFilesByBoatId[bid];
                renderPHRFRoster(currentRace?.boats || []);
                return;
            }
            // Per-row VKX (Vakaros Atlas binary) file picker.
            if (e.target?.matches?.('input[type=file][data-phrf-vkx]')) {
                const inp = e.target;
                const bid = inp.dataset.boatId;
                const file = inp.files?.[0];
                inp.value = '';
                if (!bid || !file) return;
                pendingVkxFilesByBoatId[bid] = file;
                delete pendingGpxFilesByBoatId[bid];
                renderPHRFRoster(currentRace?.boats || []);
                return;
            }
            // Device dropdown change → re-check conflicts + refresh strip.
            if (e.target?.matches?.('.phrf-device')) {
                _validatePHRFDeviceConflicts();
                renderGPSAttachStrip();
            }
        });
        container.dataset.changeBound = '1';
    }
    _validatePHRFDeviceConflicts();
}

// Render the "Attach GPS data" strip beneath the PHRF roster — one
// row per device the user has assigned. Each row shows the boat label
// (read-only) + session dropdown + GPX upload button. Preserves any
// pending GPX files staged in pendingGpxFiles across re-renders.
function renderGPSAttachStrip() {
    const container = document.getElementById('gps-attach');
    if (!container) return;

    // Walk the PHRF roster to learn current device assignments and
    // pair them with their boat metadata. This is the source of truth
    // — the persisted race.boats may be stale until save.
    const rows = [];
    document.querySelectorAll('#phrf-roster .phrf-row').forEach(row => {
        const idx = Number(row.dataset.idx);
        const sel = row.querySelector('.phrf-device');
        const deviceId = sel?.value || '';
        if (!deviceId) return;
        const boat = (currentRace?.boats || [])[idx];
        if (!boat) return;
        // Use the latest session/gpx state — pendingGpxFiles wins (just
        // staged this modal-open), then any previously saved gpx_path,
        // then session_path from the boat record.
        const gpxPath = boat.gpx_path || '';
        const hasPendingGpx = !!pendingGpxFiles[deviceId];
        const isGpxActive = hasPendingGpx || !!gpxPath;
        const sessionPath = isGpxActive ? '' : (boat.session_path || '');
        rows.push({ deviceId, idx, boat, sessionPath, gpxPath, hasPendingGpx, isGpxActive });
    });

    if (!rows.length) {
        container.innerHTML = '<div class="gps-attach-empty">No devices assigned yet — pick E1–E6 from the dropdowns above to attach GPS data.</div>';
        return;
    }

    // Sort by deviceId so the strip reads E1 → E6 even if the user
    // assigned them out of order in the PHRF roster.
    rows.sort((a, b) => a.deviceId.localeCompare(b.deviceId));

    container.innerHTML = rows.map(({ deviceId, boat, sessionPath, gpxPath, hasPendingGpx, isGpxActive }) => {
        const sessions = availableSessions[deviceId] || [];
        const sessionOptions = sessions.map(s => {
            const selected = sessionPath === s.path ? 'selected' : '';
            const label = s.name ? `${s.label} - ${s.name}` : s.label;
            return `<option value="${s.path}" ${selected}>${label}</option>`;
        }).join('');
        const color = colorFor(deviceId);
        const yacht = boat?.boat_name || boat?.team_name || (boat?.sail_number ? `#${boat.sail_number}` : '');
        const gpxLabel = hasPendingGpx
            ? pendingGpxFiles[deviceId].name
            : (gpxPath ? 'GPX uploaded' : '');

        return `
            <div class="gps-attach-row" data-device="${deviceId}" data-gpx-path="${gpxPath}">
                <div class="gps-attach-device">
                    <span class="gps-attach-color" style="background:${color}"></span>
                    <span class="gps-attach-label">${deviceId} <span class="gps-attach-arrow">→</span> ${_attrEsc(yacht)}</span>
                </div>
                <div class="session-or-gpx gps-attach-source">
                    <select data-field="session_path" class="session-select${isGpxActive ? ' hidden' : ''}">
                        <option value="">Select session...</option>
                        ${sessionOptions}
                    </select>
                    <div class="gpx-badge${isGpxActive ? '' : ' hidden'}">
                        <span class="gpx-badge-name">${_attrEsc(gpxLabel)}</span>
                        <button class="btn-gpx-clear" type="button" title="Remove GPX">&times;</button>
                    </div>
                    <label class="btn-gpx" title="Upload GPX track">GPX<input type="file" accept=".gpx" class="gpx-file-input" style="display:none"></label>
                </div>
            </div>
        `;
    }).join('');

    // Same file-input + clear handlers as renderBoatAssignments.
    container.querySelectorAll('.gpx-file-input').forEach(input => {
        const deviceId = input.closest('.gps-attach-row').dataset.device;
        input.addEventListener('change', (e) => {
            handleGpxFileSelect(e, deviceId);
            // After staging, re-render so the gpx-badge replaces the dropdown.
            setTimeout(renderGPSAttachStrip, 0);
        });
    });
    container.querySelectorAll('.btn-gpx-clear').forEach(btn => {
        const deviceId = btn.closest('.gps-attach-row').dataset.device;
        btn.addEventListener('click', () => {
            handleGpxClear(deviceId);
            renderGPSAttachStrip();
        });
    });
}

function _validatePHRFDeviceConflicts() {
    const container = document.getElementById('phrf-roster');
    if (!container) return;
    const counts = {};
    for (const sel of container.querySelectorAll('.phrf-device')) {
        const v = sel.value;
        if (!v) continue;
        counts[v] = (counts[v] || 0) + 1;
    }
    for (const sel of container.querySelectorAll('.phrf-device')) {
        const v = sel.value;
        sel.classList.toggle('phrf-conflict', !!v && counts[v] > 1);
    }
}

function renderBoatAssignments(boats) {
    const container = document.getElementById('boat-assignments');

    // Ensure all 6 devices
    const allDevices = ['E1', 'E2', 'E3', 'E4', 'E5', 'E6'];
    const boatMap = {};
    for (const b of boats) {
        boatMap[b.device_id] = b;
    }

    // Build datalist options for autocomplete. Sources: hard-coded
    // fleet roster + everything this browser has saved (auto-harvested
    // from past race loads + saves) + the currently-loaded race's own
    // assignments. Lets a coach add a new team once and have it
    // suggested forever after.
    const boatOptions = knownBoatNames().map(b => `<option value="${_attrEsc(b)}">`).join('');
    const teamOptions = knownTeamNames().map(t => `<option value="${_attrEsc(t)}">`).join('');

    container.innerHTML = `
        <datalist id="boat-names">${boatOptions}</datalist>
        <datalist id="team-names">${teamOptions}</datalist>
    ` + allDevices.map(deviceId => {
        const boat = boatMap[deviceId] || { device_id: deviceId, boat_name: '', team_name: '', sail_number: '', session_path: '', gpx_path: '' };
        const color = BOAT_COLORS[deviceId];
        const sessions = availableSessions[deviceId] || [];

        // Build session dropdown options
        const sessionOptions = sessions.map(s => {
            const selected = boat.session_path === s.path ? 'selected' : '';
            const label = s.name ? `${s.label} - ${s.name}` : s.label;
            return `<option value="${s.path}" ${selected}>${label}</option>`;
        }).join('');

        const gpxActive = !!(boat.gpx_path || pendingGpxFiles[deviceId]);
        const gpxLabel = pendingGpxFiles[deviceId]
            ? pendingGpxFiles[deviceId].name
            : (boat.gpx_path ? 'GPX uploaded' : '');

        return `
            <div class="boat-assignment" data-device="${deviceId}" data-gpx-path="${boat.gpx_path || ''}">
                <div class="boat-assignment-device">
                    <span class="boat-assignment-color" style="background: ${color}"></span>
                    <span>${deviceId}</span>
                </div>
                <input type="text" placeholder="Team" value="${boat.team_name || ''}" data-field="team_name" list="team-names">
                <input type="text" placeholder="Boat" value="${boat.boat_name || ''}" data-field="boat_name" list="boat-names">
                <input type="text" placeholder="Sail #" value="${boat.sail_number || ''}" data-field="sail_number" class="sail-number-input">
                <div class="session-or-gpx">
                    <select data-field="session_path" class="session-select${gpxActive ? ' hidden' : ''}">
                        <option value="">Select session...</option>
                        ${sessionOptions}
                    </select>
                    <div class="gpx-badge${gpxActive ? '' : ' hidden'}">
                        <span class="gpx-badge-name">${gpxLabel}</span>
                        <button class="btn-gpx-clear" type="button" title="Remove GPX">&times;</button>
                    </div>
                    <label class="btn-gpx" title="Upload GPX track">GPX<input type="file" accept=".gpx" class="gpx-file-input" style="display:none"></label>
                </div>
            </div>
        `;
    }).join('');

    // Attach GPX file input and clear button listeners
    container.querySelectorAll('.gpx-file-input').forEach(input => {
        const deviceId = input.closest('.boat-assignment').dataset.device;
        input.addEventListener('change', (e) => handleGpxFileSelect(e, deviceId));
    });
    container.querySelectorAll('.btn-gpx-clear').forEach(btn => {
        const deviceId = btn.closest('.boat-assignment').dataset.device;
        btn.addEventListener('click', () => handleGpxClear(deviceId));
    });
}

function renderFinishOrder(order, boats) {
    const container = document.getElementById('finish-order');

    // Build list of boats with positions
    const boatMap = {};
    for (const b of boats) {
        boatMap[b.device_id] = b;
    }

    // Use order if provided, otherwise use boats array order
    const orderedDevices = order.length > 0 ? order : boats.map(b => b.device_id);

    container.innerHTML = orderedDevices.map((deviceId, index) => {
        const boat = boatMap[deviceId] || { device_id: deviceId, boat_name: deviceId };
        const color = BOAT_COLORS[deviceId];

        return `
            <div class="finish-order-item" draggable="true" data-device="${deviceId}">
                <span class="finish-order-position">${index + 1}</span>
                <div class="finish-order-boat">
                    <span class="boat-assignment-color" style="background: ${color}"></span>
                    <span>${boat.boat_name || deviceId}</span>
                </div>
            </div>
        `;
    }).join('');

    // Setup drag and drop
    setupFinishOrderDragDrop();
}

function setupFinishOrderDragDrop() {
    const container = document.getElementById('finish-order');
    let draggedItem = null;

    container.querySelectorAll('.finish-order-item').forEach(item => {
        item.addEventListener('dragstart', () => {
            draggedItem = item;
            item.style.opacity = '0.5';
        });

        item.addEventListener('dragend', () => {
            draggedItem = null;
            item.style.opacity = '1';
            updateFinishOrderPositions();
        });

        item.addEventListener('dragover', (e) => {
            e.preventDefault();
            const afterElement = getDragAfterElement(container, e.clientY);
            if (afterElement == null) {
                container.appendChild(draggedItem);
            } else {
                container.insertBefore(draggedItem, afterElement);
            }
        });
    });
}

function getDragAfterElement(container, y) {
    const elements = [...container.querySelectorAll('.finish-order-item:not(.dragging)')];

    return elements.reduce((closest, child) => {
        const box = child.getBoundingClientRect();
        const offset = y - box.top - box.height / 2;
        if (offset < 0 && offset > closest.offset) {
            return { offset, element: child };
        } else {
            return closest;
        }
    }, { offset: Number.NEGATIVE_INFINITY }).element;
}

function updateFinishOrderPositions() {
    const items = document.querySelectorAll('.finish-order-item');
    items.forEach((item, index) => {
        item.querySelector('.finish-order-position').textContent = index + 1;
    });
}

function getFormData() {
    const date = document.getElementById('race-date-input').value;
    const startTime = document.getElementById('start-time-input').value || '00:00';
    const endTime = document.getElementById('end-time-input').value || '00:30';

    // Validate date
    if (!date) {
        throw new Error('Date is required');
    }

    // Convert local time to UTC ISO string
    // Input is in local time (user's timezone), we need to convert to UTC for storage
    const startLocal = new Date(`${date}T${startTime}`);
    const endLocal = new Date(`${date}T${endTime}`);

    // Validate dates
    if (isNaN(startLocal.getTime())) {
        throw new Error('Invalid start time');
    }
    if (isNaN(endLocal.getTime())) {
        throw new Error('Invalid end time');
    }

    // toISOString() returns UTC
    const startUTC = startLocal.toISOString();
    const endUTC = endLocal.toISOString();

    // Build boats array from form. Handicap races (currentRace.classes
    // non-empty) take the PHRF-roster path — preserve every boat's
    // class/rating/finish/etc., mutate only device_id from the
    // dropdowns. Single-class fleet races take the legacy 6-device
    // path which fully rebuilds boats from the form.
    const isHandicap = Array.isArray(currentRace?.classes) && currentRace.classes.length > 0;
    let boats;
    if (isHandicap) {
        // Start from the in-memory roster (currentRace.boats is the
        // source of truth for handicap metadata). Apply the user's
        // device_id picks row-by-row by index.
        boats = (currentRace.boats || []).map(b => ({ ...b }));
        document.querySelectorAll('#phrf-roster .phrf-row').forEach(row => {
            const idx = Number(row.dataset.idx);
            const sel = row.querySelector('.phrf-device');
            if (!boats[idx] || !sel) return;
            const v = sel.value || null;
            boats[idx].device_id = v;
            // Clear orphaned session paths if the device assignment was
            // cleared, otherwise the data endpoint would try to fetch
            // a session under no device.
            if (!v) {
                boats[idx].session_path = null;
                // A by-boat-id GPX/VKX (the boat brought its own GPS) is
                // NOT tied to any E-device, so an empty device dropdown
                // must not drop it — otherwise own-GPS tracks get wiped on
                // every re-edit. Only clear device-keyed GPX here.
                if (!/\/by-boat-id\//.test(boats[idx].gpx_path || '')) {
                    boats[idx].gpx_path = null;
                }
            }
        });
        // Merge session/GPX picks from the GPS-attach strip back into
        // the right boat row (matched by device_id). Pending GPX files
        // are uploaded separately after save by uploadPendingGpxFiles.
        document.querySelectorAll('#gps-attach .gps-attach-row').forEach(row => {
            const deviceId = row.dataset.device;
            const target = boats.find(b => b.device_id === deviceId);
            if (!target) return;
            const gpxPath = row.dataset.gpxPath || null;
            const hasPendingGpx = !!pendingGpxFiles[deviceId];
            const isGpxActive = hasPendingGpx || !!gpxPath;
            const sessionPath = isGpxActive ? null : (row.querySelector('[data-field="session_path"]')?.value || null);
            target.session_path = sessionPath;
            target.gpx_path = isGpxActive ? gpxPath : null;
        });
    } else {
        boats = [];
        document.querySelectorAll('.boat-assignment').forEach(row => {
            const deviceId = row.dataset.device;
            const teamName = row.querySelector('[data-field="team_name"]')?.value || '';
            const boatName = row.querySelector('[data-field="boat_name"]')?.value || '';
            const sailNumber = row.querySelector('[data-field="sail_number"]')?.value?.trim() || '';
            const gpxPath = row.dataset.gpxPath || null;
            const hasPendingGpx = !!pendingGpxFiles[deviceId];
            const isGpxActive = hasPendingGpx || !!gpxPath;
            const sessionPath = isGpxActive ? null : (row.querySelector('[data-field="session_path"]')?.value || null);

            if (teamName || boatName || sailNumber || sessionPath || isGpxActive) {
                boats.push({
                    device_id: deviceId,
                    team_name: teamName,
                    boat_name: boatName,
                    sail_number: sailNumber,
                    session_path: sessionPath,
                    gpx_path: isGpxActive ? gpxPath : null,
                });
            }
        });
    }

    // Get finish order
    const finishOrder = [];
    document.querySelectorAll('.finish-order-item').forEach(item => {
        finishOrder.push(item.dataset.device);
    });

    const boatClass = getBoatClassFromForm();   // may throw

    const payload = {
        name: document.getElementById('race-name-input').value,
        date: date,
        start_time: startUTC,
        end_time: endUTC,
        regatta_id: document.getElementById('regatta-input').value || null,
        boat_class: boatClass,
        boats,
        finish_order: finishOrder,
    };
    // Preserve handicap-only fields on save — the editor doesn't yet
    // expose them as form inputs, so they round-trip through
    // currentRace unmodified. Without these, a save would silently
    // strip classes + conditions and drop the race back to single-class.
    if (Array.isArray(currentRace?.classes) && currentRace.classes.length > 0) {
        payload.classes = currentRace.classes;
        if (currentRace.race_conditions) payload.race_conditions = currentRace.race_conditions;
    }
    return payload;
}

async function saveRace() {
    let formData;
    try {
        formData = getFormData();
    } catch (err) {
        alert(err.message);
        return;
    }

    try {
        let resp;
        // Duplicate flow leaves currentRace.race_id pointing at the
        // source race; the flag forces a POST so we don't accidentally
        // overwrite the original.
        const isUpdate = currentRace?.race_id && !isDuplicatingRace;
        if (isUpdate) {
            // Update existing
            resp = await fetch(`${API_BASE}/api/races/${currentRace.race_id}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(formData),
            });
        } else {
            // Create new
            resp = await fetch(`${API_BASE}/api/races`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(formData),
            });
        }

        if (!resp.ok) {
            const errorText = await resp.text();
            console.error('[Race] API error:', resp.status, errorText);
            throw new Error(`HTTP ${resp.status}: ${errorText}`);
        }

        const savedRace = await resp.json();
        console.log('[Race] Saved race:', savedRace);

        // Persist any newly-typed team / boat names so they appear
        // in the autocomplete the next time this browser opens a
        // race-edit modal.
        rememberRaceNames(savedRace);

        // Upload any GPS tracks staged for this race — covers the
        // legacy device-keyed GPX (pendingGpxFiles), boat-id-keyed
        // GPX (pendingGpxFilesByBoatId), and Vakaros .vkx
        // (pendingVkxFilesByBoatId). Checking only pendingGpxFiles
        // was a silent-failure bug — handicap-boat uploads landed in
        // the boat-id maps and were never sent to the server.
        const hasStagedUploads =
            Object.keys(pendingGpxFiles).length > 0 ||
            Object.keys(pendingGpxFilesByBoatId).length > 0 ||
            Object.keys(pendingVkxFilesByBoatId).length > 0;
        if (hasStagedUploads) {
            const summary = await uploadPendingGpxFiles(savedRace.race_id);
            // Surface the outcome so silent failures (parse error,
            // 4xx response, network drop) don't disappear into the
            // console. Successful uploads get a brief confirmation;
            // failures get an alert listing what went wrong.
            if (summary.failed.length) {
                const lines = summary.failed.map(f =>
                    `• ${f.label} (${f.file}): ${f.error || ('HTTP ' + (f.status || '?'))}`);
                alert(`Some GPS uploads failed:\n\n${lines.join('\n')}\n\nThe race was saved without those tracks. Re-open the editor to retry.`);
            } else if (summary.ok.length) {
                // Quietly OK — track will appear after loadRaceData.
                console.log(`[Race] Uploaded ${summary.ok.length} GPS file(s).`);
            }
        }

        closeRaceModal();

        // Directly load the saved race data (this will update map, charts, etc.)
        await loadRaceData(savedRace.race_id);

        // Update dropdown selections to reflect current race
        const regattaId = savedRace.regatta_id || '__all__';
        document.getElementById('regatta-select').value = regattaId;
        await loadRaceDays(regattaId);
        if (savedRace.date) {
            document.getElementById('raceday-select').value = savedRace.date;
            loadRacesForDay(savedRace.date);
            document.getElementById('race-select').value = savedRace.race_id;
        }

    } catch (err) {
        console.error('[Race] Failed to save race:', err);
        alert(`Failed to save race: ${err.message}`);
    }
}

async function matchSessions() {
    if (!currentRace?.race_id) {
        alert('Save the race first before matching sessions.');
        return;
    }

    try {
        const resp = await fetch(`${API_BASE}/api/races/${currentRace.race_id}/match-sessions`, {
            method: 'POST',
        });

        const result = await resp.json();
        console.log('[Race] Matched sessions:', result);

        // Reload race to get updated session paths
        const raceResp = await fetch(`${API_BASE}/api/races/${currentRace.race_id}`);
        const updatedRace = await raceResp.json();

        renderBoatAssignments(updatedRace.boats || []);

        alert(`Matched ${result.matched.filter(m => m.session_path).length} sessions.`);

    } catch (err) {
        console.error('[Race] Failed to match sessions:', err);
        alert('Failed to match sessions. Check console for details.');
    }
}

async function deleteRace() {
    if (!currentRace?.race_id) {
        return;
    }

    const raceName = currentRace.name || 'this race';
    if (!confirm(`Delete "${raceName}"? This cannot be undone.`)) {
        return;
    }

    try {
        const resp = await fetch(`${API_BASE}/api/races/${currentRace.race_id}`, {
            method: 'DELETE',
        });

        if (!resp.ok) {
            const errorText = await resp.text();
            throw new Error(`HTTP ${resp.status}: ${errorText}`);
        }

        console.log('[Race] Deleted race:', currentRace.race_id);

        // Close modal
        closeRaceModal();

        // Clear current race state
        const regattaId = currentRace.regatta_id;
        const raceDate = currentRace.date;
        currentRace = null;
        raceData = null;

        // Clear map and UI
        clearBoatLayers();
        document.getElementById('leaderboard').innerHTML = '<div class="leaderboard-empty">Select a race to view standings</div>';
        document.getElementById('race-name').textContent = 'No race selected';
        document.getElementById('race-time').textContent = '';
        document.getElementById('btn-edit-race').disabled = true;
        document.getElementById('btn-duplicate-race').disabled = true;
        document.getElementById('btn-copy-course-next').disabled = true;
        document.getElementById('btn-copy-course-all').disabled = true;

        // Reload race days and races for current regatta
        if (regattaId) {
            await loadRaceDays(regattaId);
            if (raceDate) {
                document.getElementById('raceday-select').value = raceDate;
                loadRacesForDay(raceDate);
            }
        }

        // Reset race selector
        document.getElementById('race-select').value = '';

    } catch (err) {
        console.error('[Race] Failed to delete race:', err);
        alert(`Failed to delete race: ${err.message}`);
    }
}

// --- Course Editor ---

let editCourseMode = false;
let courseDraft = null;
let courseEditLayer = null;
let courseViewLayer = null;
let markEditors = {};
let lineEditors = {};

// Mark-room "zone" circles: RRS 18 says boats acquire mark-room when
// they enter a circle of THREE BOAT-LENGTHS centred on the mark. For
// the J/80 (LOA 8 m) that's 24 m. Each mark gets a translucent dashed
// Mark-zone circle that fades in when any visible boat is inside, fades
// out otherwise — gives the coach a live visual cue for who's "in the
// zone" during roundings. Radius is 3 × LOA, derived from the race's
// boat_class (defaults to J/80 / 24 m when no class is set).
let MARK_ZONE_RADIUS_M = DEFAULT_BOAT_LOA_M * 3;
let markZoneCircles = [];     // [{ mark, circle, active }] — rebuilt by renderCourseViewLayer

// Live bow-offset for the currently-loaded race. Updated by
// applyRaceBoatClass() and read by zone / OCS code.
let BOW_OFFSET_M = DEFAULT_BOW_OFFSET_M;

// Live hull dimensions for the boats on this race — drives the
// real-scale hull polygon and mainsail boom drawn on the map. Falls
// back to J/80 numbers when the race has no boat_class set.
let HULL_LOA_M  = DEFAULT_BOAT_LOA_M;
let HULL_BEAM_M = DEFAULT_BOAT_BEAM_M;

// Pre-computed unit-LOA hull outline in (x_aft, y_starboard / half_beam)
// — a 9-vertex sailboat profile with pointy bow and small flat transom.
// Multiply x by LOA and y by half_beam at draw time.
const _HULL_OUTLINE = [
    [0.00,  0.00],   // bow tip
    [0.18,  0.55],   // forward shoulder port
    [0.42,  1.00],   // beam max port (amidships)
    [0.92,  0.55],   // stern quarter port
    [1.00,  0.30],   // transom port corner
    [1.00, -0.30],   // transom starboard corner
    [0.92, -0.55],
    [0.42, -1.00],
    [0.18, -0.55],
];

// Push the current race's boat_class into the live map state: zone
// radius, bow offset, and hull dimensions used to draw real-scale
// boat polygons. Cheap and idempotent; safe to call after every
// race load or save.
function applyRaceBoatClass() {
    const cls = currentRace?.boat_class || null;
    const loa = cls?.loa_m;
    const bow = cls?.bow_offset_m;
    const beam = cls?.beam_m;
    HULL_LOA_M = (loa && loa > 0) ? loa : DEFAULT_BOAT_LOA_M;
    // Custom-class fallback: ~32% of LOA matches J/80, Sonar 23,
    // Rhodes 19, and 420 within ±0.02 m.
    HULL_BEAM_M = (beam && beam > 0) ? beam : HULL_LOA_M * 0.32;
    BOW_OFFSET_M = (bow != null && bow >= 0) ? bow : DEFAULT_BOW_OFFSET_M;
    // Mark zone (RRS 18 definition): "The area around a mark within
    // a distance of three hull lengths of the boat nearer to it." —
    // so the *actual* zone for any specific rounding is sized by
    // 3 × LOA of the first boat to enter it, NOT a fleet-wide value.
    //
    // The static circle we render here is a visual reference only —
    // it shows the LARGEST POSSIBLE zone size for this fleet so a
    // viewer can eyeball "is anyone near the zone of anyone?". The
    // real per-rounding zone is always ≤ this circle (and usually
    // smaller for the smaller boats in the fleet). Drawing
    // per-boat-dynamic zones around the boats approaching the mark
    // is the correct long-term render; this circle is a stopgap.
    const handicapMaxLoa = _maxBoatLOA();
    MARK_ZONE_RADIUS_M = (handicapMaxLoa || HULL_LOA_M) * 3;
    for (const entry of markZoneCircles) {
        entry.circle.setRadius(MARK_ZONE_RADIUS_M);
    }

    // Hide the chart's polar-overlay toggle when polar isn't
    // supported for this class — keeping a no-op button visible
    // would just confuse the user.
    const polTog = document.getElementById('polar-overlay-toggle');
    if (polTog) polTog.style.display = polarSupportedForCurrentRace() ? '' : 'none';
}

// Project a GPS (antenna) fix forward by BOW_OFFSET_M metres along the
// boat's course-over-ground, giving an estimated bow position. Used to
// correct zone-entry and OCS detection for the fact that the antenna
// sits at the mast, not at the bow. Equirectangular small-angle
// approximation — accurate to mm at the scales we use (offsets ≤ 5 m).
function projectBowPosition(lat, lon, cog_deg) {
    if (!Number.isFinite(lat) || !Number.isFinite(lon) || BOW_OFFSET_M <= 0
        || cog_deg == null || !Number.isFinite(cog_deg)) {
        return { lat, lon };
    }
    const R = 6371000;
    const rad = cog_deg * Math.PI / 180;
    const dN = BOW_OFFSET_M * Math.cos(rad);   // metres north
    const dE = BOW_OFFSET_M * Math.sin(rad);   // metres east
    const dLat = (dN / R) * 180 / Math.PI;
    const dLon = (dE / (R * Math.cos(lat * Math.PI / 180))) * 180 / Math.PI;
    return { lat: lat + dLat, lon: lon + dLon };
}

// Project a point in boat-frame (forwardM = metres along COG, starboardM
// = metres to the right of bow when looking forward) onto the geographic
// frame as a [lat, lon] pair offset from a reference point. Equi-
// rectangular approximation — fine at the scales we use (≤ ~15 m
// from the antenna).
function _offsetLatLng(refLat, refLon, forwardM, starboardM, cogDeg) {
    if (cogDeg == null || !Number.isFinite(cogDeg)) cogDeg = 0;
    const cog = cogDeg * Math.PI / 180;
    // COG measured clockwise from north → forward unit vector in
    // (north, east) basis = (cos(COG), sin(COG)). Starboard unit
    // vector = forward rotated 90° clockwise = (-sin(COG), cos(COG)).
    const north = forwardM * Math.cos(cog) + starboardM * (-Math.sin(cog));
    const east  = forwardM * Math.sin(cog) + starboardM *  Math.cos(cog);
    const R = 6371000;
    const dLat = (north / R) * 180 / Math.PI;
    const dLon = (east  / (R * Math.cos(refLat * Math.PI / 180))) * 180 / Math.PI;
    return [refLat + dLat, refLon + dLon];
}

// Largest LOA in the loaded race. The static mark-zone circle uses
// this as a visual upper bound (any per-rounding RRS 18 zone is
// ≤ 3 × the entering boat's LOA, so 3 × largest is always at least
// as big as any real zone for this fleet). Returns null if the
// race has no per-boat LOAs (legacy fleet races handled by the
// race-level boat_class instead).
function _maxBoatLOA() {
    if (!Array.isArray(currentRace?.boats)) return null;
    let max = null;
    for (const b of currentRace.boats) {
        if (typeof b.loa_m === 'number' && b.loa_m > 0) {
            if (max == null || b.loa_m > max) max = b.loa_m;
        }
    }
    return max;
}

// Per-device LOA/beam/bow lookup. Handicap races vary by boat; fleet
// races inherit from the race-level boat_class (HULL_LOA_M et al).
// `dims` falls back to those globals when the per-boat catalog data
// isn't available, so legacy races render exactly as before.
function hullDimsForDevice(deviceId) {
    if (!deviceId || !currentRace?.boats) {
        return { loa: HULL_LOA_M, beam: HULL_BEAM_M, bow: BOW_OFFSET_M };
    }
    const boat = currentRace.boats.find(b => b.device_id === deviceId);
    const loa = (boat && boat.loa_m > 0) ? boat.loa_m : HULL_LOA_M;
    // Beam: catalog beam if we ever store it, else 32% of LOA (good
    // fit for J/80, Sonar 23, Rhodes 19, 420; close enough for
    // anything cruiser-racer-shaped).
    const beam = (boat && boat.beam_m > 0) ? boat.beam_m : loa * 0.32;
    // Bow offset: ~38% of LOA is a good catch-all for a mast-stepped
    // antenna on most cruiser-racer hulls.
    const bow = (boat && boat.bow_offset_m != null && boat.bow_offset_m >= 0)
        ? boat.bow_offset_m
        : (loa === HULL_LOA_M ? BOW_OFFSET_M : loa * 0.38);
    return { loa, beam, bow };
}

// Real-scale sailboat hull polygon for the currently-loaded boat class.
// Returns an array of [lat, lon] vertices that closes into a hull
// shape sized to the actual LOA / beam, oriented along COG, anchored
// at the antenna fix. Falls back to an empty polygon when COG is
// missing — better to draw nothing than spin a degenerate hull at
// dock-still speeds.
//
// `dims` is optional — when omitted, uses the race-level boat_class
// dimensions (HULL_LOA_M / HULL_BEAM_M / BOW_OFFSET_M). Handicap-fleet
// callers pass per-boat dims via hullDimsForDevice().
function hullPolygonLatLngs(antennaLat, antennaLon, cogDeg, dims) {
    if (cogDeg == null || !Number.isFinite(cogDeg)) return [];
    const loa = dims?.loa ?? HULL_LOA_M;
    const beam = dims?.beam ?? HULL_BEAM_M;
    const bow = dims?.bow ?? BOW_OFFSET_M;
    const halfBeam = beam / 2;
    return _HULL_OUTLINE.map(([xAft, yScaled]) => {
        const forwardM   = bow - xAft * loa;
        const starboardM = yScaled * halfBeam;
        return _offsetLatLng(antennaLat, antennaLon, forwardM, starboardM, cogDeg);
    });
}

// ---- Inter-boat separation toward next mark (SHOW › ↔ Dist) ----------
// RaceQs-style perpendicular projection: for each adjacent ranked pair
// of boats that are still on the same leg, draw an L-shape:
//   perpRef:  short line through the trailing boat, perpendicular
//             to that boat's bearing to the next mark.
//   dropLine: from the perpRef's far corner up to the leading boat,
//             parallel to the bearing-to-mark axis.
//
// The dropLine length is the along-mark separation in metres — i.e.,
// how much further toward the next mark the leader is. That's the
// metric coaches actually care about (you can be 30 m laterally
// apart but neck-and-neck on progress, or perfectly downcourse but
// 60 m behind on progress).
//
// Pairs are skipped when either boat has finished, when boats are
// on different legs, or when the course / next-mark isn't defined.
let distancePairs = [];

function clearDistanceLines() {
    for (const p of distancePairs) {
        if (p.perpRefF) map.removeLayer(p.perpRefF);
        if (p.perpRefL) map.removeLayer(p.perpRefL);
        if (p.dropLine) map.removeLayer(p.dropLine);
        if (p.label)    map.removeLayer(p.label);
    }
    distancePairs = [];
}

function _ensureDistancePool(n) {
    while (distancePairs.length < n) {
        // Perpendicular reference line through the FOLLOWER's BOW,
        // extending laterally toward the midpoint between the two
        // boats (perp/2 in metres).
        const perpRefF = L.polyline([], {
            color: '#fbbf24', weight: 1.5, opacity: 0.75, interactive: false,
        }).addTo(map);
        // Perpendicular reference line through the LEADER's STERN,
        // extending laterally back toward the same midpoint.
        const perpRefL = L.polyline([], {
            color: '#fbbf24', weight: 1.5, opacity: 0.75, interactive: false,
        }).addTo(map);
        // Drop line between the two perp endpoints — parallel to
        // the bearing-to-mark axis. Its length is exactly the
        // displayed along-mark stern-to-bow gap.
        const dropLine = L.polyline([], {
            color: '#fbbf24', weight: 2, opacity: 0.9, interactive: false,
        }).addTo(map);
        const label = L.marker([0, 0], {
            icon: L.divIcon({
                className: 'distance-label',
                html: '<span class="dist-text"></span>',
                iconSize: [0, 0],
                iconAnchor: [0, 0],
            }),
            interactive: false,
        }).addTo(map);
        distancePairs.push({ perpRefF, perpRefL, dropLine, label });
    }
    while (distancePairs.length > n) {
        const p = distancePairs.pop();
        if (p.perpRefF) map.removeLayer(p.perpRefF);
        if (p.perpRefL) map.removeLayer(p.perpRefL);
        if (p.dropLine) map.removeLayer(p.dropLine);
        if (p.label)    map.removeLayer(p.label);
    }
}

// RRS-style overlap test. The follower is overlapped with the leader
// when its bow is at or past the line drawn abeam from the leader's
// stern (perpendicular to the leader's heading). Equivalently:
//   (F_bow − L_stern) · leader_heading_unit_vector ≥ 0
// Independent of where the next mark is — uses the leader's own
// direction of travel, which is what RRS 18 cares about and what
// matters in tight mark-rounding clusters where bearing-to-mark
// varies wildly per boat.
function _rrsOverlap(F_bow, L_stern, cogL) {
    if (!F_bow || !L_stern) return false;
    if (cogL == null || !Number.isFinite(cogL)) return false;
    const R = 6371000;
    const toRad = (d) => d * Math.PI / 180;
    const mLat = R * toRad(1);
    const mLon = R * toRad(1) * Math.cos(L_stern[0] * toRad(1));
    const dN = (F_bow[0] - L_stern[0]) * mLat;
    const dE = (F_bow[1] - L_stern[1]) * mLon;
    const cogR = cogL * toRad(1);
    const along = dN * Math.cos(cogR) + dE * Math.sin(cogR);
    return along >= 0;
}

// Decompose (toPt − fromPt) into along-mark and perpendicular components,
// using the bearing from `ref` to `mark` as the +along axis.
//   fromPt, toPt:  [lat, lon] tuples — typically follower's bow and
//                  leader's stern when used for inter-boat measurement.
//   ref:           {lat, lon} reference point used to compute the
//                  bearing (small offsets between ref and fromPt
//                  shift bearing by < 0.1° at race distances).
//   mark:          {lat, lon} of the next-leg target.
// Returns:
//   midA / midB:   [lat, lon] endpoints of the perpendicular reference
//                  lines from each side — both at the lateral midpoint
//                  between the two input points, so the line midA→midB
//                  is parallel to the bearing-to-mark axis with
//                  length |alongMark|.
//   alongMark:     unsigned metres separating the two input points
//                  along the bearing axis (the displayed metric).
//   perp:          unsigned lateral separation along the perp axis.
function _perpToMarkProjection(fromPt, toPt, ref, mark) {
    if (!fromPt || !toPt || !ref || !mark) return null;
    if (mark.lat == null || ref.lat == null) return null;

    const R = 6371000;
    const toRad = (d) => d * Math.PI / 180;
    const mPerDegLat = R * toRad(1);
    const mPerDegLon = R * toRad(1) * Math.cos(ref.lat * toRad(1));

    const brgRad = bearingDegrees(ref.lat, ref.lon, mark.lat, mark.lon) * Math.PI / 180;
    const bN = Math.cos(brgRad), bE = Math.sin(brgRad);
    const pN = Math.sin(brgRad), pE = -Math.cos(brgRad);

    const dN = (toPt[0] - fromPt[0]) * mPerDegLat;
    const dE = (toPt[1] - fromPt[1]) * mPerDegLon;

    const along = dN * bN + dE * bE;
    const perp  = dN * pN + dE * pE;

    // Each perp reference line extends from its anchor toward the
    // lateral midpoint (perp/2 metres). Both endpoints land on the
    // same perp coordinate, so midA→midB is purely along-mark.
    const midA_dN = (perp / 2) * pN, midA_dE = (perp / 2) * pE;
    const midB_dN = -(perp / 2) * pN, midB_dE = -(perp / 2) * pE;
    const midA = [
        fromPt[0] + midA_dN / mPerDegLat,
        fromPt[1] + midA_dE / mPerDegLon,
    ];
    const midB = [
        toPt[0] + midB_dN / mPerDegLat,
        toPt[1] + midB_dE / mPerDegLon,
    ];

    return {
        midA, midB,
        alongMark: Math.abs(along),
        perp: Math.abs(perp),
        alongSign: along >= 0 ? 1 : -1,
    };
}

// `positions` is the leaderboard-sorted array from calculatePositions().
// Walks it in pairs (rank N + rank N+1); pair is shown only when both
// boats are still racing AND on the same leg (same next mark).
function updateDistanceLines(positions) {
    if (!markerOverlays.distances || !positions?.length || !currentRace) {
        clearDistanceLines();
        return;
    }
    const courseSeq = currentRace.course || [];
    const totalLegs = currentRace._totalLegs ?? courseSeq.length;
    if (!courseSeq.length) {
        clearDistanceLines();
        return;
    }
    const marksById = buildMarksById(currentRace);
    const sternLen = Math.max(0, HULL_LOA_M - BOW_OFFSET_M);

    // ---- Step 1: build the candidate pair list (same filters as before) ----
    const pairData = [];
    for (let i = 0; i < positions.length - 1; i++) {
        const pLeader   = positions[i];
        const pFollower = positions[i + 1];
        if (pLeader.finished || pFollower.finished) continue;
        if (pLeader.legsCompleted !== pFollower.legsCompleted) continue;
        if (pLeader.legsCompleted >= totalLegs) continue;
        const targetMark = marksById[courseSeq[pLeader.legsCompleted % courseSeq.length]];
        if (!targetMark || targetMark.lat == null) continue;
        const layerL = boatLayers[pLeader.deviceId];
        const layerF = boatLayers[pFollower.deviceId];
        if (!layerL?.visible || !layerF?.visible) continue;
        const leader   = layerL.current;
        const follower = layerF.current;
        if (!leader || !follower) continue;
        if (leader.lat == null || follower.lat == null) continue;
        const cogF = Number.isFinite(follower.course) ? follower.course : 0;
        const cogL = Number.isFinite(leader.course)   ? leader.course   : 0;
        const F_bow   = _offsetLatLng(follower.lat, follower.lon,  BOW_OFFSET_M, 0, cogF);
        const L_stern = _offsetLatLng(leader.lat,   leader.lon,   -sternLen,     0, cogL);
        pairData.push({
            leader, follower, mark: targetMark,
            F_bow, L_stern, cogF, cogL,
            overlapped: _rrsOverlap(F_bow, L_stern, cogL),
        });
    }

    // ---- Step 2: scan for maximal consecutive-overlap chains -------------
    // For each pool slot we'll assign one of three rendering modes:
    //   'lshape'  — non-overlapped clear-water pair, draw full L-shape.
    //   'chain'   — front of a chain: draw ONE line from this pair's
    //               leader's stern to chainTail's follower's bow.
    //   'hidden'  — inside a chain (the line is rendered by the chain
    //               head slot instead), or otherwise suppressed.
    const slotMode  = new Array(pairData.length).fill('hidden');
    const chainTail = new Array(pairData.length).fill(-1);   // index of last pair in chain when mode='chain'
    let i = 0;
    while (i < pairData.length) {
        if (pairData[i].overlapped) {
            let tail = i;
            while (tail + 1 < pairData.length && pairData[tail + 1].overlapped) tail++;
            slotMode[i] = 'chain';
            chainTail[i] = tail;
            // pairs i+1..tail are inside the chain — leave as 'hidden'.
            i = tail + 1;
        } else {
            slotMode[i] = 'lshape';
            i++;
        }
    }

    _ensureDistancePool(pairData.length);

    // ---- Step 3: render each slot per its assigned mode ------------------
    for (let i = 0; i < pairData.length; i++) {
        const slot = distancePairs[i];
        const mode = slotMode[i];

        // Reset all visuals first — the mode branches set whichever
        // ones are needed. Hidden slots stay reset.
        slot.perpRefF.setLatLngs([]);
        slot.perpRefL.setLatLngs([]);
        slot.dropLine.setLatLngs([]);
        const el = slot.label.getElement();
        if (el) {
            const s = el.querySelector('.dist-text');
            if (s) s.textContent = '';
        }
        // Reset styles (chain mode bumps weight; lshape uses defaults).
        slot.dropLine.setStyle({ dashArray: null, weight: 2, opacity: 0.9 });
        slot.perpRefF.setStyle({ weight: 1.5, opacity: 0.75 });
        slot.perpRefL.setStyle({ weight: 1.5, opacity: 0.75 });

        if (mode === 'hidden') continue;

        if (mode === 'chain') {
            // RRS overlap visualization. Per the rule definition, two
            // boats overlap when neither is *clear astern* — i.e. when
            // the trailing boat's bow is on or forward of the line
            // drawn abeam from the leader's transom (perpendicular to
            // the LEADER's centreline). RRS imposes NO sideways limit;
            // the lateral distance between the boats is irrelevant to
            // whether they're overlapped, and only matters for Rule 18
            // (mark-room zone, 3 hull lengths) and contact rules.
            //
            // We render that geometry directly: two parallel ticks
            // perpendicular to the chain leader's heading, one through
            // the leader's transom (A) and one through the trailing
            // boat's bow (B). The slab between the ticks IS the RRS
            // overlap zone. Each tick spans the lateral gap between
            // the boats plus a small overshoot, so the bracket reads
            // even when boats are tracking nearly bow-to-bow.
            const head = pairData[i];
            const tail = pairData[chainTail[i]];
            const A = head.L_stern;
            const B = tail.F_bow;
            const RAD = Math.PI / 180;
            const cogR = (head.cogL || 0) * RAD;
            const cosC = Math.cos(cogR), sinC = Math.sin(cogR);
            const mLat = 6371000 * RAD;
            const mLon = 6371000 * RAD * Math.cos(A[0] * RAD);
            // Lateral component of (B − A) in the leader's right-normal
            // direction (positive = trailing boat is to starboard of
            // leader). Sign drives which side the ticks extend toward.
            const dN = (B[0] - A[0]) * mLat;
            const dE = (B[1] - A[1]) * mLon;
            const lateral = dE * cosC - dN * sinC;
            const OVERSHOOT_M = 3;
            const halfSpan = Math.abs(lateral) / 2 + OVERSHOOT_M;
            const midOff   = lateral / 2;
            // Helper: offset a [lat,lon] point by `vMetres` along the
            // leader's right-normal axis v=(-sinC, cosC) in (N,E).
            const offV = (anchor, vMetres) => [
                anchor[0] + (vMetres * -sinC) / mLat,
                anchor[1] + (vMetres *  cosC) / mLon,
            ];
            // Leader's transom abeam line (through A).
            slot.perpRefL.setLatLngs([
                offV(A, midOff - halfSpan),
                offV(A, midOff + halfSpan),
            ]);
            // Trailing boat's bow abeam line (through B). Mid-offset
            // is negated because B is at v=lateral relative to A, so
            // the lateral midpoint is at v=−lateral/2 relative to B.
            slot.perpRefF.setLatLngs([
                offV(B, -midOff - halfSpan),
                offV(B, -midOff + halfSpan),
            ]);
            // Make the abeam ticks visually distinct: thicker, fully
            // opaque amber. Together they read as the overlap bracket.
            slot.perpRefL.setStyle({ weight: 2.5, opacity: 1.0 });
            slot.perpRefF.setStyle({ weight: 2.5, opacity: 1.0 });
            continue;
        }

        // mode === 'lshape': non-overlapped pair, draw the full
        // along-mark distance visualization.
        const data = pairData[i];
        const proj = _perpToMarkProjection(data.F_bow, data.L_stern, data.follower, data.mark);
        if (!proj) continue;
        slot.perpRefF.setLatLngs([data.F_bow,   proj.midA]);
        slot.perpRefL.setLatLngs([data.L_stern, proj.midB]);
        slot.dropLine.setLatLngs([proj.midA, proj.midB]);
        slot.label.setLatLng([
            (proj.midA[0] + proj.midB[0]) / 2,
            (proj.midA[1] + proj.midB[1]) / 2,
        ]);
        if (el) {
            const span = el.querySelector('.dist-text');
            if (span) {
                span.textContent = proj.alongMark < 1000
                    ? `${Math.round(proj.alongMark)} m`
                    : `${(proj.alongMark / 1000).toFixed(2)} km`;
            }
        }
    }
}

// Mainsail boom line. The boom hangs aft from the mast (≈ antenna
// position) on the side OPPOSITE the wind:
//   - starboard tack (twa > 0, wind from starboard) → boom on port side
//   - port tack      (twa < 0, wind from port)      → boom on starboard
// Boom angle from centerline ≈ |TWA| − 5°, clamped to [0°, 85°]:
// close-hauled ⇒ near-centerline; running ⇒ ~90° to one side.
// Returns [[mast lat,lon], [boom-tip lat,lon]] or null when TWA is
// unavailable.
function boomLatLngs(antennaLat, antennaLon, cogDeg, twaSigned) {
    if (twaSigned == null || !Number.isFinite(twaSigned)) return null;
    const boomLen = HULL_LOA_M * 0.45;
    const angleDeg = Math.min(85, Math.max(0, Math.abs(twaSigned) - 5));
    const angleRad = angleDeg * Math.PI / 180;
    const sideSign = twaSigned > 0 ? -1 : 1;
    // Mast sits roughly at the antenna position (the antenna is on the
    // stern face of the mast, so this is within centimetres for our
    // purposes). Boom tip is `boomLen` from the mast at `angleDeg`
    // off the aft centerline.
    const aftDist  = -boomLen * Math.cos(angleRad);   // negative forward = aft
    const sideDist = sideSign * boomLen * Math.sin(angleRad);
    const mast = [antennaLat, antennaLon];
    const tip  = _offsetLatLng(antennaLat, antennaLon, aftDist, sideDist, cogDeg);
    return [mast, tip];
}

// ---- Boat-class dropdown wiring for the race-setup modal ---------------
// Thin wrappers around the shared `boat-classes.js` module so the race
// form keeps its un-prefixed IDs while sharing the catalogue and
// helpers with events.html's regatta modal.

function populateBoatClassDropdown() { bcPopulateDropdown(''); }
function setBoatClassInForm(boatClass) { bcSetInForm(boatClass, ''); }
function getBoatClassFromForm() { return bcGetFromForm(''); }

const MARK_TYPE_COLORS = {
    windward: '#f4212e',
    leeward: '#00ba7c',
    gate_port: '#a855f7',
    gate_stbd: '#f59e0b',
    offset: '#22d3ee',
    custom: '#ffffff',
};
const MARK_TYPES = ['windward', 'leeward', 'gate_port', 'gate_stbd', 'offset', 'custom'];

// Human-readable label per mark type. Shown in the editor dropdown so it
// reads as plain English rather than internal jargon.
const MARK_TYPE_LABELS = {
    windward:   'Windward (top mark)',
    leeward:    'Leeward (bottom mark)',
    gate_port:  'Leeward gate · port (left)',
    gate_stbd:  'Leeward gate · stbd (right)',
    offset:     'Offset (small mark below windward)',
    custom:     'Custom',
};
const LINE_COLORS = { start_line: '#22d3ee', finish_line: '#f4212e' };

function markTypeColor(type) {
    return MARK_TYPE_COLORS[type] || '#ffffff';
}

function markIcon(type, editable = false) {
    const color = markTypeColor(type);
    const size = editable ? 22 : 14;
    const border = editable ? '3px solid #fff' : '2px solid #fff';
    const html = `<div class="course-mark-dot" style="background:${color};width:${size}px;height:${size}px;border:${border};"></div>`;
    return L.divIcon({ html, className: 'course-mark-divicon', iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
}

function lineEndIcon(kind, editable = false) {
    const color = kind === 'pin' ? '#ffd93d' : '#f97316';
    const size = editable ? 20 : 12;
    const radius = kind === 'pin' ? '50%' : '3px';
    const html = `<div class="course-line-end" style="background:${color};width:${size}px;height:${size}px;border-radius:${radius};border:2px solid #fff;"></div>`;
    return L.divIcon({ html, className: 'course-line-divicon', iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
}

function clearCourseLayers() {
    if (courseEditLayer) { map.removeLayer(courseEditLayer); courseEditLayer = null; }
    if (courseViewLayer) { map.removeLayer(courseViewLayer); courseViewLayer = null; }
    markEditors = {};
    lineEditors = {};
}

function renderCourseViewLayer(race) {
    if (!race) return;
    if (courseViewLayer) { map.removeLayer(courseViewLayer); }
    courseViewLayer = L.featureGroup().addTo(map);

    for (const kind of ['start_line', 'finish_line']) {
        const line = race[kind];
        if (!line || line.pin_lat == null) continue;
        const color = LINE_COLORS[kind];
        const ends = [[line.pin_lat, line.pin_lon], [line.boat_lat, line.boat_lon]];
        const tip = kind === 'start_line' ? 'Start' : 'Finish';
        // White halo underneath so the line stays legible on every
        // basemap (Light Blue, OSM, Satellite). Slimmer than the
        // first attempt — halo + line tuned to be visible without
        // dominating the map.
        L.polyline(ends, {
            color: '#ffffff', weight: 5, opacity: 0.5, lineCap: 'round',
        }).addTo(courseViewLayer);
        L.polyline(ends, {
            color, weight: 2.5, opacity: 1.0, dashArray: '8 5', lineCap: 'round',
        }).bindTooltip(tip).addTo(courseViewLayer);
        L.marker([line.pin_lat, line.pin_lon], { icon: lineEndIcon('pin') })
            .bindTooltip(`${tip} pin`).addTo(courseViewLayer);
        L.marker([line.boat_lat, line.boat_lon], { icon: lineEndIcon('boat') })
            .bindTooltip(`${tip} committee`).addTo(courseViewLayer);
    }

    // Rebuild the zone-circle registry. Each mark gets a 24 m circle
    // sitting on the courseViewLayer (under boats). Starts fully
    // transparent; updateMarkZoneCircles() fades it in/out per frame.
    markZoneCircles = [];
    for (const m of (race.marks || [])) {
        L.marker([m.lat, m.lon], { icon: markIcon(m.mark_type) })
            .bindTooltip(m.name || m.mark_type, { permanent: false })
            .addTo(courseViewLayer);
        if (m.lat == null || m.lon == null) continue;
        const circle = L.circle([m.lat, m.lon], {
            radius: MARK_ZONE_RADIUS_M,
            color: '#fbbf24',
            weight: 1.5,
            opacity: 0,
            fillColor: '#fbbf24',
            fillOpacity: 0,
            dashArray: '5 5',
            interactive: false,
        });
        circle.addTo(courseViewLayer);
        markZoneCircles.push({ mark: m, circle, active: false });
    }
}

// Per-frame: fade each mark's 3-boat-length zone circle in when any
// visible boat is currently inside it, fade out otherwise. Only writes
// `setStyle` on transitions (one boolean per mark per frame), so this
// is cheap to call from updateBoatPositions every cursor tick.
function updateMarkZoneCircles() {
    if (!markZoneCircles.length) return;
    for (const entry of markZoneCircles) {
        const { mark, circle } = entry;
        const radius = entry.radius_m || MARK_ZONE_RADIUS_M;
        let inZone = false;
        for (const layer of Object.values(boatLayers)) {
            if (!layer.visible || !layer.current) continue;
            const c = layer.current;
            if (c.lat == null || c.lon == null) continue;
            // Test the BOW position, not the antenna. The boat enters
            // the zone when any part of the hull crosses 3 × LOA from
            // the mark; the antenna is bow_offset_m aft of the bow,
            // so we project the antenna fix forward along COG first.
            const bow = projectBowPosition(c.lat, c.lon, c.course);
            if (haversineMeters(bow.lat, bow.lon, mark.lat, mark.lon) <= radius) {
                inZone = true;
                break;
            }
        }
        if (inZone !== entry.active) {
            entry.active = inZone;
            circle.setStyle({
                opacity: inZone ? 0.75 : 0,
                fillOpacity: inZone ? 0.08 : 0,
            });
        }
    }
}

// RRS 18 zone sizing — done right.
//
// "The area around a mark within a distance of three hull lengths
//  of the boat nearer to it. A boat is in the zone when any part of
//  her hull is in the zone."
//
// For each mark, we walk every boat's GPS track chronologically and
// find which boat's bow first came within 3 × its own LOA of the mark.
// That boat's LOA defines the zone radius for THIS mark for the
// remainder of the playback. Marks no boat ever entered fall back to
// MARK_ZONE_RADIUS_M (the static upper-bound) so the circle is still
// visible.
//
// Called once after raceData arrives and per-boat LOAs are hydrated.
function computeMarkZonesFirstToEnter() {
    if (!markZoneCircles.length || !raceData?.boats) return;
    // Pre-build a trackKey → loa_m lookup so the inner loop is O(1).
    const loaByKey = {};
    for (const b of (currentRace?.boats || [])) {
        const loa = (typeof b.loa_m === 'number' && b.loa_m > 0) ? b.loa_m : null;
        if (!loa) continue;
        if (b.device_id) loaByKey[b.device_id] = loa;
        if (b.boat_id)   loaByKey[b.boat_id]   = loa;
    }
    for (const entry of markZoneCircles) {
        const { mark } = entry;
        if (mark.lat == null || mark.lon == null) continue;
        let bestMs = Infinity;
        let bestLoa = null;
        let bestKey = null;
        for (const [trackKey, boatData] of Object.entries(raceData.boats)) {
            const loa = loaByKey[trackKey];
            if (!loa) continue;
            const gps = boatData?.sensors?.gps;
            if (!Array.isArray(gps) || !gps.length) continue;
            const threshold = 3 * loa;
            // Walk forward until we find this boat's first zone entry.
            // Could be sped up with a spatial pre-filter but at our
            // scale (3000–6000 points × 6 boats × few marks) the
            // straight linear scan finishes in a few ms.
            for (const p of gps) {
                if (p.lat == null || p.lon == null) continue;
                const bow = projectBowPosition(p.lat, p.lon, p.course);
                if (haversineMeters(bow.lat, bow.lon, mark.lat, mark.lon) <= threshold) {
                    const t = new Date(p.t).getTime();
                    if (Number.isFinite(t) && t < bestMs) {
                        bestMs = t;
                        bestLoa = loa;
                        bestKey = trackKey;
                    }
                    break;
                }
            }
        }
        if (bestLoa) {
            entry.radius_m = bestLoa * 3;
            entry.first_boat_key = bestKey;
            entry.first_entry_ms = bestMs;
            entry.circle.setRadius(entry.radius_m);
            entry.circle.bindTooltip(
                `RRS 18 zone · ${entry.radius_m.toFixed(0)} m (3 × ${bestLoa.toFixed(2)} m LOA of the first boat to enter)`,
                { sticky: true });
        } else {
            // Nobody ever entered — leave the static fallback visible
            // as a reference so the mark is still spatially anchored.
            entry.radius_m = MARK_ZONE_RADIUS_M;
            entry.circle.setRadius(MARK_ZONE_RADIUS_M);
        }
    }
}

function enterCourseEditMode() {
    if (!currentRace) return;
    editCourseMode = true;
    courseDraft = {
        start_line: currentRace.start_line ? { ...currentRace.start_line } : null,
        finish_line: currentRace.finish_line ? { ...currentRace.finish_line } : null,
        marks: (currentRace.marks || []).map(m => ({ ...m })),
        course: [...(currentRace.course || [])],
    };

    if (courseViewLayer) { map.removeLayer(courseViewLayer); courseViewLayer = null; }

    document.getElementById('course-toolbar').style.display = 'flex';
    document.getElementById('mark-list').style.display = 'block';
    document.body.classList.add('course-editing');

    if (courseEditLayer) { map.removeLayer(courseEditLayer); }
    courseEditLayer = L.featureGroup().addTo(map);
    markEditors = {};
    lineEditors = {};

    if (courseDraft.start_line) renderEditableLine('start_line');
    if (courseDraft.finish_line) renderEditableLine('finish_line');
    for (const m of courseDraft.marks) renderEditableMark(m);

    renderMarkList();
}

function exitCourseEditMode() {
    editCourseMode = false;
    courseDraft = null;
    if (courseEditLayer) { map.removeLayer(courseEditLayer); courseEditLayer = null; }
    markEditors = {};
    lineEditors = {};
    document.getElementById('course-toolbar').style.display = 'none';
    document.getElementById('mark-list').style.display = 'none';
    document.body.classList.remove('course-editing');
    renderCourseViewLayer(currentRace);
}

function renderEditableLine(kind) {
    const line = courseDraft[kind];
    if (!line) return;
    if (lineEditors[kind]) {
        const e = lineEditors[kind];
        map.removeLayer(e.pinMarker);
        map.removeLayer(e.boatMarker);
        map.removeLayer(e.polyline);
    }
    const color = LINE_COLORS[kind];
    const pinMarker = L.marker([line.pin_lat, line.pin_lon], {
        icon: lineEndIcon('pin', true), draggable: true,
    }).bindTooltip(`${kind === 'start_line' ? 'Start' : 'Finish'} pin`, { permanent: true, direction: 'right', offset: [10, 0] }).addTo(courseEditLayer);
    const boatMarker = L.marker([line.boat_lat, line.boat_lon], {
        icon: lineEndIcon('boat', true), draggable: true,
    }).bindTooltip(`${kind === 'start_line' ? 'Start' : 'Finish'} committee`, { permanent: true, direction: 'right', offset: [10, 0] }).addTo(courseEditLayer);
    const poly = L.polyline([[line.pin_lat, line.pin_lon], [line.boat_lat, line.boat_lon]], {
        color, weight: 3, dashArray: '6 4',
    }).addTo(courseEditLayer);

    const update = () => {
        const pl = pinMarker.getLatLng();
        const bl = boatMarker.getLatLng();
        courseDraft[kind] = { pin_lat: pl.lat, pin_lon: pl.lng, boat_lat: bl.lat, boat_lon: bl.lng };
        poly.setLatLngs([[pl.lat, pl.lng], [bl.lat, bl.lng]]);
        updateLineListRow(kind);
    };
    pinMarker.on('drag', update);
    boatMarker.on('drag', update);
    lineEditors[kind] = { pinMarker, boatMarker, polyline: poly };
}

function renderEditableMark(mark) {
    const marker = L.marker([mark.lat, mark.lon], {
        icon: markIcon(mark.mark_type, true), draggable: true,
    }).bindTooltip(mark.name || mark.mark_type, { permanent: true, direction: 'right', offset: [12, 0] })
      .addTo(courseEditLayer);
    marker.on('drag', (e) => {
        const ll = e.target.getLatLng();
        mark.lat = ll.lat;
        mark.lon = ll.lng;
        updateMarkListRow(mark.mark_id);
    });
    markEditors[mark.mark_id] = marker;
}

function placeStartLine() {
    if (!courseDraft) return;
    if (courseDraft.start_line) return;
    const c = map.getCenter();
    const dLat = 25 / 111320;
    courseDraft.start_line = {
        pin_lat: c.lat - dLat, pin_lon: c.lng - 0.0004,
        boat_lat: c.lat + dLat, boat_lon: c.lng - 0.0004,
    };
    renderEditableLine('start_line');
    renderMarkList();
}

function placeFinishLine() {
    if (!courseDraft) return;
    if (courseDraft.finish_line) return;
    const c = map.getCenter();
    const dLat = 25 / 111320;
    courseDraft.finish_line = {
        pin_lat: c.lat - dLat, pin_lon: c.lng + 0.0004,
        boat_lat: c.lat + dLat, boat_lon: c.lng + 0.0004,
    };
    renderEditableLine('finish_line');
    renderMarkList();
}

function addMarkAtMapCenter() {
    if (!courseDraft) return;
    const c = map.getCenter();
    const idx = courseDraft.marks.length + 1;
    const mark = {
        mark_id: `m_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
        name: `Mark ${idx}`,
        mark_type: idx % 2 === 1 ? 'windward' : 'leeward',
        lat: c.lat,
        lon: c.lng,
    };
    courseDraft.marks.push(mark);
    courseDraft.course.push(mark.mark_id);
    renderEditableMark(mark);
    renderMarkList();
}

async function autoStartLineFromTracks() {
    if (!currentRace?.race_id || !courseDraft) return;
    try {
        const resp = await fetch(`${API_BASE}/api/races/${currentRace.race_id}/auto-start-line`, { method: 'POST' });
        if (!resp.ok) { alert(`Auto start line failed: ${await resp.text()}`); return; }
        const data = await resp.json();
        courseDraft.start_line = data.start_line;
        renderEditableLine('start_line');
        renderMarkList();
        const l = data.start_line;
        map.panTo([(l.pin_lat + l.boat_lat) / 2, (l.pin_lon + l.boat_lon) / 2]);
    } catch (err) {
        console.error('[Race] Auto start line error:', err);
        alert('Auto start line failed. Check console.');
    }
}

async function autoSuggestMarksFromTracks() {
    if (!currentRace?.race_id || !courseDraft) return;
    try {
        const resp = await fetch(`${API_BASE}/api/races/${currentRace.race_id}/suggest-marks`, { method: 'POST' });
        if (!resp.ok) { alert(`Suggest marks failed: ${await resp.text()}`); return; }
        const data = await resp.json();
        if (!data.marks || data.marks.length === 0) {
            alert(`No mark roundings detected (found ${data.roundings_found || 0} course changes, ${data.clusters_found || 0} clusters).`);
            return;
        }
        for (const m of data.marks) {
            const mark = {
                mark_id: `m_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
                name: m.name,
                mark_type: m.mark_type,
                lat: m.lat,
                lon: m.lon,
            };
            courseDraft.marks.push(mark);
            courseDraft.course.push(mark.mark_id);
            renderEditableMark(mark);
        }
        renderMarkList();
    } catch (err) {
        console.error('[Race] Suggest marks error:', err);
        alert('Suggest marks failed. Check console.');
    }
}

function deleteMark(markId) {
    if (!courseDraft) return;
    courseDraft.marks = courseDraft.marks.filter(m => m.mark_id !== markId);
    courseDraft.course = courseDraft.course.filter(id => id !== markId);
    if (markEditors[markId]) {
        map.removeLayer(markEditors[markId]);
        delete markEditors[markId];
    }
    renderMarkList();
}

function clearLine(kind) {
    if (!courseDraft) return;
    courseDraft[kind] = null;
    if (lineEditors[kind]) {
        map.removeLayer(lineEditors[kind].pinMarker);
        map.removeLayer(lineEditors[kind].boatMarker);
        map.removeLayer(lineEditors[kind].polyline);
        delete lineEditors[kind];
    }
    renderMarkList();
}

function renderMarkList() {
    const el = document.getElementById('mark-list');
    if (!el || !courseDraft) return;
    const rows = [];
    if (courseDraft.start_line) rows.push(renderLineRow('start_line', 'Start Line'));
    if (courseDraft.finish_line) rows.push(renderLineRow('finish_line', 'Finish Line'));
    for (const m of courseDraft.marks) rows.push(renderMarkRow(m));
    const body = rows.length > 0 ? rows.join('') : '<div class="mark-empty">No lines or marks yet. Use the toolbar above.</div>';
    el.innerHTML = `<div class="mark-list-header"><h3>Course</h3><small>Drag on map to reposition</small></div>${body}`;

    for (const m of courseDraft.marks) {
        const row = el.querySelector(`[data-mark-id="${m.mark_id}"]`);
        if (!row) continue;
        row.querySelector('[data-field="name"]')?.addEventListener('input', (e) => {
            m.name = e.target.value;
            const marker = markEditors[m.mark_id];
            if (marker) marker.setTooltipContent(m.name || m.mark_type);
        });
        row.querySelector('[data-field="type"]')?.addEventListener('change', (e) => {
            m.mark_type = e.target.value;
            const marker = markEditors[m.mark_id];
            if (marker) marker.setIcon(markIcon(m.mark_type, true));
            row.querySelector('.mark-swatch').style.background = markTypeColor(m.mark_type);
        });
        row.querySelector('[data-action="delete"]')?.addEventListener('click', () => deleteMark(m.mark_id));
    }
    for (const kind of ['start_line', 'finish_line']) {
        const lineRow = el.querySelector(`[data-line="${kind}"]`);
        if (!lineRow) continue;
        lineRow.querySelector('[data-action="delete"]')?.addEventListener('click', () => clearLine(kind));
    }
}

function renderLineRow(kind, label) {
    const line = courseDraft[kind];
    const color = LINE_COLORS[kind];
    return `
        <div class="mark-row line-row" data-line="${kind}">
            <span class="mark-swatch" style="background:${color}"></span>
            <div class="mark-fields">
                <strong>${label}</strong>
                <small class="line-coords">Pin ${line.pin_lat.toFixed(5)}, ${line.pin_lon.toFixed(5)} · Boat ${line.boat_lat.toFixed(5)}, ${line.boat_lon.toFixed(5)}</small>
            </div>
            <button class="btn-mark-delete" data-action="delete" title="Remove line">✕</button>
        </div>
    `;
}

function renderMarkRow(mark) {
    const opts = MARK_TYPES.map(t =>
        `<option value="${t}" ${t === mark.mark_type ? 'selected' : ''}>${MARK_TYPE_LABELS[t] || t}</option>`
    ).join('');
    const color = markTypeColor(mark.mark_type);
    return `
        <div class="mark-row" data-mark-id="${mark.mark_id}">
            <span class="mark-swatch" style="background:${color}"></span>
            <div class="mark-fields">
                <input type="text" data-field="name" value="${mark.name || ''}" placeholder="Name">
                <select data-field="type">${opts}</select>
                <small class="mark-coords">${mark.lat.toFixed(5)}, ${mark.lon.toFixed(5)}</small>
            </div>
            <button class="btn-mark-delete" data-action="delete" title="Remove mark">✕</button>
        </div>
    `;
}

function updateMarkListRow(markId) {
    const m = courseDraft?.marks.find(x => x.mark_id === markId);
    if (!m) return;
    const el = document.querySelector(`[data-mark-id="${markId}"] .mark-coords`);
    if (el) el.textContent = `${m.lat.toFixed(5)}, ${m.lon.toFixed(5)}`;
}

function updateLineListRow(kind) {
    const line = courseDraft?.[kind];
    if (!line) return;
    const el = document.querySelector(`[data-line="${kind}"] .line-coords`);
    if (el) el.textContent = `Pin ${line.pin_lat.toFixed(5)}, ${line.pin_lon.toFixed(5)} · Boat ${line.boat_lat.toFixed(5)}, ${line.boat_lon.toFixed(5)}`;
}

async function saveCourseDraft() {
    if (!currentRace?.race_id || !courseDraft) return;
    try {
        const body = {
            start_line: courseDraft.start_line,
            finish_line: courseDraft.finish_line,
            marks: courseDraft.marks,
            course: courseDraft.course,
        };
        const resp = await fetch(`${API_BASE}/api/races/${currentRace.race_id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
        currentRace = await resp.json();
        exitCourseEditMode();
        // currentRace.marks / course / lines have changed → recompute
        // whether Copy-Course-Next can fire.
        updateCopyCourseButton();
    } catch (err) {
        console.error('[Race] Failed to save course:', err);
        alert(`Failed to save course: ${err.message}`);
    }
}

// ---- Copy Course → Next Race ------------------------------------------
// Propagate this race's course (start/finish lines, marks, course
// sequence) to the next race on the same day. Useful when the RC
// repositions a mark mid-day — set up the new layout on Race 2 and
// push it to Race 3 with one click. Boats, times, and finish order
// on the target race are intentionally NOT touched.

function getRemainingRacesOfDay() {
    if (!currentRace || !Array.isArray(races) || !races.length) return [];
    const sorted = [...races].sort((a, b) =>
        (a.start_time || '').localeCompare(b.start_time || ''));
    const idx = sorted.findIndex(r => r.race_id === currentRace.race_id);
    if (idx < 0) return [];
    return sorted.slice(idx + 1);
}

function getNextRaceOfDay() {
    return getRemainingRacesOfDay()[0] || null;
}

function currentRaceHasCourse() {
    if (!currentRace) return false;
    const hasStart  = !!(currentRace.start_line && currentRace.start_line.pin_lat != null);
    const hasFinish = !!(currentRace.finish_line && currentRace.finish_line.pin_lat != null);
    const hasMarks  = Array.isArray(currentRace.marks) && currentRace.marks.length > 0;
    return hasStart || hasFinish || hasMarks;
}

function updateCopyCourseButton() {
    const nextBtn = document.getElementById('btn-copy-course-next');
    const allBtn  = document.getElementById('btn-copy-course-all');
    const remaining = getRemainingRacesOfDay();
    const hasCourse = currentRaceHasCourse();

    if (nextBtn) {
        const next = remaining[0] || null;
        const ready = !!(next && hasCourse);
        nextBtn.disabled = !ready;
        if (!currentRace) {
            nextBtn.title = 'Load a race first';
        } else if (!next) {
            nextBtn.title = 'No next race on this day to copy the course to';
        } else if (!hasCourse) {
            nextBtn.title = 'This race has no marks or start/finish lines yet';
        } else {
            nextBtn.title = `Copy this race's course to "${next.name}"`;
        }
    }

    if (allBtn) {
        const ready = !!(remaining.length && hasCourse);
        allBtn.disabled = !ready;
        if (!currentRace) {
            allBtn.title = 'Load a race first';
        } else if (!remaining.length) {
            allBtn.title = 'This is the last race of the day — nothing later to copy to';
        } else if (!hasCourse) {
            allBtn.title = 'This race has no marks or start/finish lines yet';
        } else {
            const names = remaining.map(r => `"${r.name}"`).join(', ');
            allBtn.title = `Copy this race's course to ${remaining.length} later race${remaining.length !== 1 ? 's' : ''}: ${names}`;
        }
    }
}

function _courseBodyFromCurrentRace() {
    return {
        start_line:  currentRace.start_line  || null,
        finish_line: currentRace.finish_line || null,
        marks:       currentRace.marks       || [],
        course:      currentRace.course      || [],
    };
}

function _courseSummaryLine() {
    const markCount = (currentRace.marks || []).length;
    const courseLen = (currentRace.course || []).length;
    const hasStart  = !!(currentRace.start_line  && currentRace.start_line.pin_lat  != null);
    const hasFinish = !!(currentRace.finish_line && currentRace.finish_line.pin_lat != null);
    const parts = [];
    if (hasStart)  parts.push('start line');
    if (hasFinish) parts.push('finish line');
    if (markCount) parts.push(`${markCount} mark${markCount !== 1 ? 's' : ''}`);
    if (courseLen) parts.push(`${courseLen}-leg sequence`);
    return parts.join(', ') || '(empty)';
}

async function copyCourseToNextRace() {
    const next = getNextRaceOfDay();
    if (!next || !currentRace) return;
    const ok = confirm(
        `Copy this race's course to "${next.name}"?\n\n` +
        `Will replace any existing course on "${next.name}" with:\n` +
        `  ${_courseSummaryLine()}\n\n` +
        `Boats, times, and finish order on "${next.name}" stay unchanged.`
    );
    if (!ok) return;

    const btn = document.getElementById('btn-copy-course-next');
    const origLabel = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = 'Copying…'; }
    try {
        const resp = await fetch(`${API_BASE}/api/races/${next.race_id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(_courseBodyFromCurrentRace()),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
        alert(`Course copied to "${next.name}".`);
    } catch (err) {
        console.error('[Race] Copy course failed:', err);
        alert(`Failed to copy course: ${err.message || err}`);
    } finally {
        if (btn) { btn.textContent = origLabel; }
        updateCopyCourseButton();
    }
}

async function copyCourseToAllRemainingRaces() {
    const targets = getRemainingRacesOfDay();
    if (!targets.length || !currentRace) return;
    const names = targets.map(t => `"${t.name}"`).join(', ');
    const ok = confirm(
        `Copy this race's course to ${targets.length} later race${targets.length !== 1 ? 's' : ''} today?\n\n` +
        `Targets: ${names}\n\n` +
        `Will replace any existing course on each with:\n` +
        `  ${_courseSummaryLine()}\n\n` +
        `Boats, times, and finish order on each target stay unchanged.`
    );
    if (!ok) return;

    const btn = document.getElementById('btn-copy-course-all');
    const origLabel = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = 'Copying…'; }

    const body = _courseBodyFromCurrentRace();
    const succeeded = [];
    const failed = [];
    // Sequential PATCHes: keeps error reporting clean and avoids
    // hammering the Lambda with a parallel burst. The race count
    // for a single day is small (typically 2–6) so latency stays
    // under a couple of seconds.
    for (const target of targets) {
        try {
            const resp = await fetch(`${API_BASE}/api/races/${target.race_id}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!resp.ok) {
                failed.push({ name: target.name, detail: `HTTP ${resp.status}` });
            } else {
                succeeded.push(target.name);
            }
        } catch (err) {
            failed.push({ name: target.name, detail: err.message || String(err) });
        }
    }

    if (btn) { btn.textContent = origLabel; }
    updateCopyCourseButton();

    if (failed.length === 0) {
        alert(`Course copied to ${succeeded.length} race${succeeded.length !== 1 ? 's' : ''}: ${succeeded.join(', ')}`);
    } else {
        const failedStr = failed.map(f => `  • ${f.name}: ${f.detail}`).join('\n');
        const okStr = succeeded.length ? `\n\nSucceeded:\n  ${succeeded.join(', ')}` : '';
        alert(`Copied to ${succeeded.length} of ${targets.length} races.${okStr}\n\nFailed:\n${failedStr}`);
    }
}

// --- Event Listeners ---

function setupEventListeners() {
    // Regatta selection → load race days, then auto-select the latest
    // day + first race of that day so the user lands inside the most
    // recent race instead of an empty "Select Day…" state. Saves three
    // clicks every time someone switches series.
    document.getElementById('regatta-select').addEventListener('change', async (e) => {
        const regattaId = e.target.value || null;
        await loadRaceDays(regattaId);
        if (!regattaId || !raceDays.length) return;
        // raceDays is sorted ascending → last entry is the most recent.
        const latest = raceDays[raceDays.length - 1];
        const daySel = document.getElementById('raceday-select');
        if (daySel) daySel.value = latest.date;
        loadRacesForDay(latest.date);
        // races[] now reflects the latest day, sorted by start_time.
        const firstRace = (latest.races && latest.races[0]) || null;
        if (firstRace) {
            const raceSel = document.getElementById('race-select');
            if (raceSel) raceSel.value = firstRace.race_id;
            loadRaceData(firstRace.race_id);
        }
    });

    // Race day selection → load races for that day, then auto-select
    // the first race so the user doesn't sit on an empty state.
    document.getElementById('raceday-select').addEventListener('change', (e) => {
        const date = e.target.value || null;
        loadRacesForDay(date);
        if (!date) return;
        const firstRace = (races && races[0]) || null;
        if (firstRace) {
            const raceSel = document.getElementById('race-select');
            if (raceSel) raceSel.value = firstRace.race_id;
            loadRaceData(firstRace.race_id);
        }
    });

    // Race selection -> load race data
    document.getElementById('race-select').addEventListener('change', (e) => {
        if (e.target.value) {
            loadRaceData(e.target.value);
        }
    });

    // New race button
    document.getElementById('btn-new-race').addEventListener('click', () => {
        currentRace = null;
        openRaceModal();
    });

    // Edit race button
    document.getElementById('btn-edit-race').addEventListener('click', () => {
        if (currentRace) {
            openRaceModal(currentRace);
        }
    });

    // Duplicate race button — opens the modal pre-filled with a copy
    // of the current race; saving POSTs a new race rather than
    // PATCHing the source.
    document.getElementById('btn-duplicate-race').addEventListener('click', duplicateRace);

    // Copy-course buttons (next race + all remaining races on the day).
    document.getElementById('btn-copy-course-next').addEventListener('click', copyCourseToNextRace);
    document.getElementById('btn-copy-course-all').addEventListener('click', copyCourseToAllRemainingRaces);

    // Edit course button (toggles on-map editing)
    const btnEditCourse = document.getElementById('btn-edit-course');
    if (btnEditCourse) {
        btnEditCourse.addEventListener('click', () => {
            if (!currentRace) return;
            if (editCourseMode) exitCourseEditMode();
            else enterCourseEditMode();
        });
    }

    // Course editor toolbar
    document.getElementById('btn-course-start-line')?.addEventListener('click', placeStartLine);
    document.getElementById('btn-course-finish-line')?.addEventListener('click', placeFinishLine);
    document.getElementById('btn-course-add-mark')?.addEventListener('click', addMarkAtMapCenter);
    document.getElementById('btn-course-auto-start')?.addEventListener('click', autoStartLineFromTracks);
    document.getElementById('btn-course-auto-marks')?.addEventListener('click', autoSuggestMarksFromTracks);
    document.getElementById('btn-course-done')?.addEventListener('click', saveCourseDraft);
    document.getElementById('btn-course-cancel')?.addEventListener('click', () => exitCourseEditMode());

    // Modal controls
    document.getElementById('modal-close').addEventListener('click', closeRaceModal);
    document.getElementById('btn-cancel').addEventListener('click', closeRaceModal);
    document.getElementById('btn-save-race').addEventListener('click', saveRace);
    document.getElementById('btn-match-sessions').addEventListener('click', matchSessions);
    document.getElementById('btn-delete-race').addEventListener('click', deleteRace);

    // Regatta picker inside the race-edit modal: when the user
    // flips it, inherit the new regatta's boat_class into the
    // boat-class control. Only fires on user interaction (setting
    // .value programmatically does not dispatch `change`), so the
    // edit-existing-race flow is unaffected.
    document.getElementById('regatta-input').addEventListener('change', (e) => {
        const rid = e.target.value;
        const reg = rid ? (regattas || []).find(r => r.regatta_id === rid) : null;
        setBoatClassInForm(reg?.boat_class || null);
    });

    // Close modal on backdrop click
    document.querySelector('.modal-backdrop').addEventListener('click', closeRaceModal);

    // Map expand button
    const btnExpand = document.getElementById('btn-expand-map');
    const mapPanel = document.getElementById('map-panel');
    if (btnExpand && mapPanel) {
        btnExpand.addEventListener('click', () => {
            mapPanel.classList.toggle('expanded');
            document.body.classList.toggle('map-expanded');
            // Trigger map resize after expansion
            setTimeout(() => {
                if (map) {
                    map.invalidateSize();
                }
            }, 350);
        });
    }

    // Playback controls
    setupPlaybackControls();

    // Start Review button: opens the modal start-sequence player
    // (own map zoomed to start line, RRS Appendix S horn signals,
    // pre-start GPS animation from t=-3:00 to t=+1:00).
    const btnStartReview = document.getElementById('btn-start-review');
    if (btnStartReview) {
        btnStartReview.addEventListener('click', () => {
            if (!window.SailFramesStartReview?.open) return;
            if (!currentRace) {
                alert('Load a race first.');
                return;
            }
            window.SailFramesStartReview.open({
                currentRace,
                raceData,
                boatLayers,
                BOAT_COLORS,
                apiBase: API_BASE,
            });
        });
    }

    // Share-this-moment button: copies a ?t=N permalink to the
    // clipboard and gives quick visual feedback. The chat panel's
    // (t=N) markers go through the same window.SailFramesRace.seekTo
    // path so a link from the AI coach behaves identically.
    const btnShare = document.getElementById('btn-share-moment');
    if (btnShare) {
        btnShare.addEventListener('click', async () => {
            const t = Math.max(0, Math.round(currentTime || 0));
            const u = new URL(location.href);
            u.searchParams.set('t', t);
            const url = u.toString();
            history.replaceState({}, '', u);
            const restore = btnShare.textContent;
            try {
                await navigator.clipboard.writeText(url);
                btnShare.textContent = '✓ Copied';
            } catch {
                // Clipboard API may be blocked; show the URL instead so
                // the user can copy manually.
                btnShare.textContent = '✓';
                window.prompt('Copy this link:', url);
            }
            setTimeout(() => { btnShare.textContent = restore; }, 1600);
        });
    }
}

// ============================================================
// Tactics-discussion drawer
//
// Per-race bulletin board. Anyone can read; verified Google
// users can post and edit/delete their own posts; coaches
// (members of COACH_ALLOWLIST on the backend) can delete any.
//
// Auth model:
//   • If the user is already signed in as a coach (sf-coach-id-token
//     in localStorage), we send that session token.
//   • Otherwise we run a Google Identity Services popup, store the
//     resulting Google ID token under sf-user-id-token, and send
//     that. Google ID tokens expire in 1 hour — we re-prompt on
//     expiry. This is intentionally lighter-weight than the coach
//     session flow so competitors can drop in to ask one question.
//
// All API traffic goes through SAILFRAMES_COACH_API (the coach
// Lambda's Function URL), with three endpoints:
//   GET    /discussions/{race_id}                — public
//   POST   /discussions/{race_id}                — auth, create
//   DELETE /discussions/{race_id}/{post_id}      — auth, mod-aware
// ============================================================

const _TD_GUEST_TOKEN_KEY = 'sf-user-id-token';
const _TD_GUEST_EMAIL_KEY = 'sf-user-email';
const _TD_GUEST_NAME_KEY  = 'sf-user-name';
// Self-declared role for the tactics board ("coach" or "sailor").
// Distinct from any backend allowlist — the user picks at sign-in
// and can change anytime. Empty = not yet chosen (forces picker).
const _TD_ROLE_KEY = 'sf-tactics-role';

function _tdRole() {
    try { return localStorage.getItem(_TD_ROLE_KEY) || ''; } catch { return ''; }
}
function _tdSetRole(role) {
    try {
        if (role) localStorage.setItem(_TD_ROLE_KEY, role);
        else      localStorage.removeItem(_TD_ROLE_KEY);
    } catch {}
}

let _tdLoadedForRaceId = null;
let _tdPostsCache = [];
let _tdRefreshTimer = null;
let _tdAttachTime = false;
let _tdAttachSec = 0;

function _tdGuestToken() {
    try { return localStorage.getItem(_TD_GUEST_TOKEN_KEY) || ''; } catch { return ''; }
}
function _tdGuestEmail() {
    try { return localStorage.getItem(_TD_GUEST_EMAIL_KEY) || ''; } catch { return ''; }
}
function _tdGuestName() {
    try { return localStorage.getItem(_TD_GUEST_NAME_KEY) || ''; } catch { return ''; }
}
function _tdSaveGuestSession(token, email, name) {
    try {
        localStorage.setItem(_TD_GUEST_TOKEN_KEY, token);
        localStorage.setItem(_TD_GUEST_EMAIL_KEY, email || '');
        localStorage.setItem(_TD_GUEST_NAME_KEY, name || '');
    } catch {}
}
function _tdClearGuestSession() {
    try {
        localStorage.removeItem(_TD_GUEST_TOKEN_KEY);
        localStorage.removeItem(_TD_GUEST_EMAIL_KEY);
        localStorage.removeItem(_TD_GUEST_NAME_KEY);
    } catch {}
}

// Decode a JWT payload — same shape works for Google ID tokens and
// our own sf.* session tokens (split on dots, base64url-decode the
// middle segment). Returns null on any failure.
function _tdDecodeJwt(token) {
    try {
        const payload = token.split('.')[1];
        const json = atob(payload.replace(/-/g, '+').replace(/_/g, '/'));
        return JSON.parse(decodeURIComponent(escape(json)));
    } catch { return null; }
}

function _tdCurrentAuth() {
    // Coach session takes precedence — 30-day lifetime, no popup needed.
    const coach = _coachToken();
    if (coach && _coachTokenIsValid()) {
        let email = '';
        try { email = localStorage.getItem('sf-coach-email') || ''; } catch {}
        return { token: coach, email, name: email, kind: 'coach' };
    }
    const guest = _tdGuestToken();
    if (guest) {
        const claims = _tdDecodeJwt(guest);
        const expMs = claims && claims.exp ? claims.exp * 1000 : 0;
        if (expMs && expMs > Date.now() + 30_000) {
            return {
                token: guest,
                email: _tdGuestEmail(),
                name: _tdGuestName(),
                kind: 'guest',
            };
        }
        // Expired — drop it so the UI re-prompts.
        _tdClearGuestSession();
    }
    return null;
}

function _tdApiBase() {
    return (window.SAILFRAMES_COACH_API || '').replace(/\/+$/, '');
}

async function _tdFetchPosts(raceId) {
    const base = _tdApiBase();
    if (!base) return [];
    // Attach auth opportunistically: signed-in viewers get is_mine,
    // is_admin_mod, and my_vote stamps. Unauthed callers still get a
    // clean (PII-redacted) response.
    const auth = _tdCurrentAuth();
    const headers = {};
    if (auth) headers['Authorization'] = 'Bearer ' + auth.token;
    const resp = await fetch(`${base}/discussions/${encodeURIComponent(raceId)}`, { headers });
    if (!resp.ok) throw new Error(`GET /discussions HTTP ${resp.status}`);
    const data = await resp.json();
    return data.posts || [];
}

async function _tdSubmitPost(raceId, body, cursorTSec, role) {
    const auth = _tdCurrentAuth();
    if (!auth) throw new Error('Sign in to post.');
    const base = _tdApiBase();
    if (!base) throw new Error('Discussion API not configured.');
    const resp = await fetch(`${base}/discussions/${encodeURIComponent(raceId)}`, {
        method: 'POST',
        headers: {
            'Authorization': 'Bearer ' + auth.token,
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({
            body,
            cursor_t_sec: cursorTSec,
            role,
            // Stamp the rich context label so the notification email
            // names the race in human terms ("Wednesday Night Series ·
            // Race 2 of 4 · Tue May 12, 2026"). Best computed client-
            // side where we have all the regatta + day state already.
            race_context_label: buildRaceContextLabel(),
        }),
    });
    if (!resp.ok) {
        const t = await resp.text().catch(() => '');
        if (resp.status === 401) {
            // Token rejected — clear guest session and re-prompt.
            if (auth.kind === 'guest') _tdClearGuestSession();
            throw new Error('Your sign-in expired. Please sign in again.');
        }
        throw new Error(`POST failed (${resp.status}): ${t.slice(0, 160)}`);
    }
    return resp.json();
}

async function _tdVoteApi(raceId, postId, vote) {
    const auth = _tdCurrentAuth();
    if (!auth) throw new Error('Sign in to vote.');
    const base = _tdApiBase();
    const resp = await fetch(
        `${base}/discussions/${encodeURIComponent(raceId)}/${encodeURIComponent(postId)}/vote`,
        {
            method: 'POST',
            headers: {
                'Authorization': 'Bearer ' + auth.token,
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ vote }),
        }
    );
    if (!resp.ok) {
        const t = await resp.text().catch(() => '');
        if (resp.status === 401) {
            if (auth.kind === 'guest') _tdClearGuestSession();
            throw new Error('Your sign-in expired.');
        }
        throw new Error(`vote HTTP ${resp.status}: ${t.slice(0, 160)}`);
    }
    return resp.json();
}

async function _tdDeletePost(raceId, postId) {
    const auth = _tdCurrentAuth();
    if (!auth) throw new Error('Sign in required.');
    const base = _tdApiBase();
    const resp = await fetch(
        `${base}/discussions/${encodeURIComponent(raceId)}/${encodeURIComponent(postId)}`,
        {
            method: 'DELETE',
            headers: { 'Authorization': 'Bearer ' + auth.token },
        }
    );
    if (!resp.ok) {
        const t = await resp.text().catch(() => '');
        throw new Error(`DELETE failed (${resp.status}): ${t.slice(0, 160)}`);
    }
    return resp.json();
}

// Render the post list. Newest at the bottom (chat-style) so the
// composer remains directly beneath the freshest content.
function _tdRenderPosts() {
    const list = document.getElementById('td-list');
    if (!list) return;

    if (!_tdPostsCache.length) {
        list.innerHTML = '<div class="td-empty">No posts yet. Start the conversation — observations, questions, what would you have done?</div>';
        return;
    }

    // Server stamps `is_mine`, `is_admin_mod`, `my_vote` per post when
    // the viewer is authenticated; `author_email` is never sent.
    const auth = _tdCurrentAuth();
    const canVote = !!auth;
    const html = _tdPostsCache.map(p => {
        const author = p.author_name || 'Anonymous';
        const when = p.created_at ? new Date(p.created_at).toLocaleString('en-US', {
            month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
        }) : '';
        const isCoach = (p.role === 'coach');
        const canDelete = !!(p.is_mine || p.is_admin_mod);
        const cursorBtn = (p.cursor_t_sec != null && currentRace?.start_time)
            ? `<button class="td-cursor-link" data-cursor-t="${Number(p.cursor_t_sec)}" type="button">→ Jump to ${_tdFmtCursorLocal(p.cursor_t_sec)}</button>`
            : '';
        // Per-post WhatsApp share — sends the full post text + a deep
        // link to the race at the post's attached cursor moment (or
        // race start if no cursor was attached). Always visible so any
        // post can be forwarded to the fleet.
        const shareBtn = `<button class="td-share-post" data-share-post="${p.id}" type="button" title="Share this post on WhatsApp">Share on WhatsApp</button>`;
        const inlineRow = (cursorBtn || shareBtn)
            ? `<div class="td-post-inline">${cursorBtn}${shareBtn}</div>`
            : '';
        const upN   = Number(p.upvotes   || 0);
        const downN = Number(p.downvotes || 0);
        let voteBlock;
        if (p.is_mine) {
            // Authors can't vote on their own posts — show passive tally.
            voteBlock = `<div class="td-votes is-own">▲ ${upN} · ▼ ${downN}</div>`;
        } else if (canVote) {
            const upActive   = p.my_vote === 'up'   ? 'active up'   : '';
            const downActive = p.my_vote === 'down' ? 'active down' : '';
            voteBlock = `
                <div class="td-votes">
                    <button class="td-vote ${upActive}" data-vote="up" data-post="${p.id}" type="button" title="Upvote">▲ <span>${upN}</span></button>
                    <button class="td-vote ${downActive}" data-vote="down" data-post="${p.id}" type="button" title="Downvote">▼ <span>${downN}</span></button>
                </div>`;
        } else {
            // Anonymous viewer — tally read-only.
            voteBlock = `<div class="td-votes is-anon" title="Sign in to vote">▲ ${upN} · ▼ ${downN}</div>`;
        }
        const delBtn = canDelete
            ? `<button class="td-delete-btn" data-delete="${p.id}" type="button">delete</button>`
            : '';
        const actions = (voteBlock || delBtn)
            ? `<div class="td-post-actions">${voteBlock}<span class="td-actions-spacer"></span>${delBtn}</div>`
            : '';
        return `
            <div class="td-post ${isCoach ? 'is-coach' : ''}">
                <div class="td-post-head">
                    <span class="td-author">${_tdEscape(author)}</span>
                    ${isCoach ? '<span class="td-coach-tag">coach</span>' : ''}
                    <span class="td-when">${when}</span>
                </div>
                <div class="td-body">${_tdEscape(p.body || '')}</div>
                ${inlineRow}
                ${actions}
            </div>
        `;
    }).join('');

    list.innerHTML = html;
    list.scrollTop = list.scrollHeight;

    // Wire cursor-jump, share, vote, delete handlers.
    for (const btn of list.querySelectorAll('.td-cursor-link')) {
        btn.addEventListener('click', () => {
            const tSec = parseFloat(btn.getAttribute('data-cursor-t'));
            if (Number.isFinite(tSec) && window.SailFramesRace) {
                window.SailFramesRace.seekTo(tSec);
            }
        });
    }
    for (const btn of list.querySelectorAll('.td-share-post')) {
        btn.addEventListener('click', () => {
            const id = btn.getAttribute('data-share-post');
            const post = _tdPostsCache.find(p => p.id === id);
            if (post) _tdSharePostToWhatsApp(post);
        });
    }
    for (const btn of list.querySelectorAll('.td-vote')) {
        btn.addEventListener('click', () => {
            const postId   = btn.getAttribute('data-post');
            const dir      = btn.getAttribute('data-vote');   // 'up' | 'down'
            _tdHandleVote(postId, dir);
        });
    }
    for (const btn of list.querySelectorAll('.td-delete-btn')) {
        btn.addEventListener('click', async () => {
            if (!confirm('Delete this post?')) return;
            const id = btn.getAttribute('data-delete');
            try {
                await _tdDeletePost(currentRace.race_id, id);
                _tdPostsCache = _tdPostsCache.filter(p => p.id !== id);
                _tdRenderPosts();
            } catch (e) {
                alert('Delete failed: ' + (e.message || e));
            }
        });
    }
}

// Click-handler for the vote buttons. Optimistically updates the local
// cache + UI, then reconciles with the server response. Toggling an
// already-active direction clears the vote; clicking the opposite
// direction switches.
async function _tdHandleVote(postId, direction) {
    const post = _tdPostsCache.find(p => p.id === postId);
    if (!post) return;
    const newVote = post.my_vote === direction ? 'none' : direction;
    const prev = {
        my_vote: post.my_vote || null,
        upvotes: Number(post.upvotes || 0),
        downvotes: Number(post.downvotes || 0),
    };
    // Optimistic — remove existing vote contribution, then apply new.
    let up = prev.upvotes, down = prev.downvotes;
    if (prev.my_vote === 'up')   up   = Math.max(0, up - 1);
    if (prev.my_vote === 'down') down = Math.max(0, down - 1);
    if (newVote === 'up')   up += 1;
    if (newVote === 'down') down += 1;
    post.my_vote  = newVote === 'none' ? null : newVote;
    post.upvotes  = up;
    post.downvotes = down;
    _tdRenderPosts();
    try {
        const result = await _tdVoteApi(currentRace.race_id, postId, newVote);
        post.upvotes  = Number(result.upvotes   || 0);
        post.downvotes = Number(result.downvotes || 0);
        post.my_vote  = result.my_vote || null;
        _tdRenderPosts();
    } catch (e) {
        // Revert and surface.
        Object.assign(post, prev);
        _tdRenderPosts();
        alert('Vote failed: ' + (e.message || e));
    }
}

function _tdFmtTSec(sec) {
    const s = Math.max(0, Math.round(Number(sec) || 0));
    const m = Math.floor(s / 60);
    const ss = String(s % 60).padStart(2, '0');
    return `${m}:${ss}`;
}

// Format a "seconds from race start" offset as a wall-clock time in the
// viewer's local timezone — i.e. what the on-water observer would have
// seen on their watch. Falls back to mm:ss-from-start if the race
// start_time isn't loaded yet.
function _tdFmtCursorLocal(tSec) {
    if (!currentRace?.start_time) return _tdFmtTSec(tSec);
    const ms = new Date(currentRace.start_time).getTime() + Number(tSec) * 1000;
    if (!Number.isFinite(ms)) return _tdFmtTSec(tSec);
    return new Date(ms).toLocaleTimeString('en-US', {
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    });
}

function _tdEscape(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

function _tdRefreshComposeUI() {
    const signin = document.getElementById('td-signin');
    const form = document.getElementById('td-form');
    const formWho = document.getElementById('td-form-who');
    const submitBtn = document.getElementById('td-submit');
    if (!signin || !form) return;
    const auth = _tdCurrentAuth();
    if (!auth) {
        signin.hidden = false;
        form.hidden = true;
        return;
    }
    signin.hidden = true;
    form.hidden = false;
    const who = auth.name || auth.email || 'signed-in user';
    const role = _tdRole();
    if (!role) {
        // No role chosen yet — present the picker before allowing post.
        // Default suggestion: Sailor. The user can switch to Coach at
        // any time via the (change) link once chosen.
        formWho.innerHTML = `
            <span class="td-role-prompt">Hi <b>${_tdEscape(who)}</b> — post as:</span>
            <button class="td-role-btn" data-pickrole="sailor" type="button">⛵ Sailor</button>
            <button class="td-role-btn td-role-btn-coach" data-pickrole="coach" type="button">👨‍🏫 Coach</button>
        `;
        if (submitBtn) submitBtn.disabled = true;
    } else {
        const label = role === 'coach' ? '👨‍🏫 Coach' : '⛵ Sailor';
        formWho.innerHTML = `
            Posting as <b>${label}</b> · <b>${_tdEscape(who)}</b>
            <button class="td-role-change" type="button" title="Change role">change</button>
        `;
        if (submitBtn) submitBtn.disabled = false;
    }
    for (const btn of formWho.querySelectorAll('[data-pickrole]')) {
        btn.addEventListener('click', () => {
            _tdSetRole(btn.getAttribute('data-pickrole'));
            _tdRefreshComposeUI();
        });
    }
    const changeBtn = formWho.querySelector('.td-role-change');
    if (changeBtn) changeBtn.addEventListener('click', () => {
        _tdSetRole('');
        _tdRefreshComposeUI();
    });
}

async function _tdLoadAndRender() {
    if (!currentRace?.race_id) return;
    const list = document.getElementById('td-list');
    if (list) list.innerHTML = '<div class="td-empty">Loading…</div>';
    try {
        _tdPostsCache = await _tdFetchPosts(currentRace.race_id);
        _tdLoadedForRaceId = currentRace.race_id;
        _tdRenderPosts();
    } catch (e) {
        // Print a verbose error to help diagnose Safari "Load failed"
        // and other browser-specific fetch failures. Includes error
        // name + message + the API base so the next bug report tells
        // us more than the generic Safari "Load failed" string.
        console.error('[tactics] load failed', e);
        const apiBase = _tdApiBase() || '(no API base)';
        const detail = `${e.name || 'Error'}: ${e.message || 'unknown'}`;
        if (list) list.innerHTML =
            `<div class="td-empty">Couldn't load posts.<br>` +
            `<small style="opacity:0.75">${_tdEscape(detail)}<br>` +
            `Endpoint: ${_tdEscape(apiBase)}<br>` +
            `Browser: ${_tdEscape(navigator.userAgent.slice(0, 120))}</small></div>`;
    }
}

// Build a human-readable label that names the race in three pieces:
// regatta series name, race # within the day, and date. Used in
// WhatsApp share text and in notification emails so the recipient
// instantly knows which race is being discussed without opening the
// link. Falls back gracefully when any piece is missing.
//
// Example outputs:
//   "Wednesday Night Series · Race 2 of 4 · Tue May 12, 2026"
//   "Race 3 · Tue May 12, 2026"               (no regatta assigned)
//   "Mock Practice · Race 1 · Tue May 12, 2026"   (only race that day)
function buildRaceContextLabel() {
    if (!currentRace) return 'this race';
    const parts = [];
    const regId = currentRace.regatta_id;
    if (regId && Array.isArray(regattas)) {
        const reg = regattas.find(r => r.regatta_id === regId);
        if (reg?.name) parts.push(reg.name);
    }
    const dayRaces = currentRaceDay?.races || [];
    const idx = dayRaces.findIndex(r => r.race_id === currentRace.race_id);
    if (idx >= 0 && dayRaces.length > 1) {
        parts.push(`Race ${idx + 1} of ${dayRaces.length}`);
    } else if (currentRace.race_name) {
        parts.push(currentRace.race_name);
    } else if (idx >= 0) {
        parts.push(`Race ${idx + 1}`);
    }
    if (currentRace.date) {
        try {
            const d = new Date(currentRace.date + 'T12:00:00');
            parts.push(d.toLocaleDateString('en-US', {
                weekday: 'short', month: 'short', day: 'numeric', year: 'numeric',
            }));
        } catch {
            parts.push(currentRace.date);
        }
    }
    return parts.length ? parts.join(' · ') : 'this race';
}

// Build the canonical shareable URL for the tactics discussion of the
// currently-loaded race. Always uses the production origin so the link
// is portable even when generated on staging / localhost. Uses ?race=
// (consistent with the deep-link reader); ?tactics=1 auto-opens the
// drawer for the recipient. When the playback cursor is past t=0 we
// also embed t=<seconds> + focus=fleet so the recipient lands at the
// exact moment AND the map zooms to the boats at that moment instead
// of showing the whole race overview.
function _tdShareUrl() {
    if (!currentRace?.race_id) return null;
    const origin = 'https://sailframes.com';
    const u = new URL(`${origin}/race.html`);
    u.searchParams.set('race', currentRace.race_id);
    u.searchParams.set('tactics', '1');
    // Always embed the cursor time + auto-zoom to the fleet. Using
    // playCursorSeconds with currentTime as a fallback mirrors what
    // the attach-time feature uses — either one can be the freshest
    // depending on which playback path last updated state. Reading
    // only currentTime previously produced "?race=...&tactics=1" with
    // no t/focus when the user hadn't scrubbed via seekTo() yet.
    const tSec = Math.max(0, Math.round(playCursorSeconds || currentTime || 0));
    u.searchParams.set('t', String(tSec));
    u.searchParams.set('focus', 'fleet');
    return u.toString();
}

// Open WhatsApp (app on mobile, Desktop/Web on desktop) with a
// prefilled message linking back to the discussion. The wa.me handler
// lets the user pick the recipient — your sailors group, a contact,
// whatever. Falls back to copying the URL when WhatsApp is unreachable.
//
// Text is intentionally ASCII — emoji (U+1F4AC 💬, U+1F4CD 📍) don't
// render on every device/font and were showing up as � in some users'
// WhatsApp clients.
function _tdShareToWhatsApp() {
    const url = _tdShareUrl();
    if (!url) { alert('No race loaded.'); return; }
    const label = buildRaceContextLabel();
    const tSec = Math.max(0, Math.round(playCursorSeconds || currentTime || 0));
    const tNote = `\nAt ${_tdFmtCursorLocal(tSec)} (race time)`;
    const text = `Tactics discussion on SailFrames\n\n${label}${tNote}\n\nDiscuss this race with the fleet: ${url}`;
    // wa.me universal handler: works on mobile (opens app) and on
    // desktop (opens WhatsApp Web / Desktop). User chooses the group.
    const waUrl = `https://wa.me/?text=${encodeURIComponent(text)}`;
    // Try in a new tab to preserve the page state behind it.
    const w = window.open(waUrl, '_blank', 'noopener,noreferrer');
    if (!w) {
        // Pop-up blocker → fall back to copy.
        try { navigator.clipboard.writeText(url); alert('Link copied to clipboard.'); }
        catch { prompt('Copy this link:', url); }
    }
}

// Per-post WhatsApp share. Composes a message containing the full
// post body + author + a deep link to the race at the post's
// attached cursor moment (so recipients land at the exact frame the
// poster was discussing). If no cursor was attached to the post,
// the link still works — it just lands at race start with the
// discussion drawer open.
function _tdSharePostToWhatsApp(post) {
    if (!currentRace?.race_id || !post) { alert('No race / post.'); return; }
    const origin = 'https://sailframes.com';
    const u = new URL(`${origin}/race.html`);
    u.searchParams.set('race', currentRace.race_id);
    u.searchParams.set('tactics', '1');
    const tSec = (post.cursor_t_sec != null)
        ? Math.max(0, Math.round(Number(post.cursor_t_sec)))
        : 0;
    u.searchParams.set('t', String(tSec));
    u.searchParams.set('focus', 'fleet');
    const url = u.toString();

    const label   = buildRaceContextLabel();
    const author  = post.author_name || 'Anonymous';
    const role    = post.role === 'coach' ? ' (coach)' : '';
    const when    = post.created_at
        ? new Date(post.created_at).toLocaleString('en-US', {
              month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
          })
        : '';
    const tNote = (post.cursor_t_sec != null && currentRace.start_time)
        ? `\nAt ${_tdFmtCursorLocal(post.cursor_t_sec)} (race time)`
        : '';

    // Body in block-quote form so the post text is visually framed.
    // ASCII only — no emoji (some recipient devices show � for them).
    const quoted = (post.body || '')
        .split('\n')
        .map(line => '> ' + line)
        .join('\n');

    const text =
        `Tactics post on SailFrames\n\n` +
        `${label}${tNote}\n\n` +
        `${quoted}\n\n` +
        `— ${author}${role}${when ? ' · ' + when : ''}\n\n` +
        `See the race at this moment: ${url}`;

    const waUrl = `https://wa.me/?text=${encodeURIComponent(text)}`;
    const w = window.open(waUrl, '_blank', 'noopener,noreferrer');
    if (!w) {
        try { navigator.clipboard.writeText(url); alert('Link copied to clipboard.'); }
        catch { prompt('Copy this link:', url); }
    }
}

// Honor ?tactics=1 in the URL to auto-open the discussion drawer
// once the race has loaded. Lets shared WhatsApp links land readers
// directly inside the conversation.
function _tdMaybeAutoOpenFromUrl() {
    try {
        const v = new URLSearchParams(location.search).get('tactics');
        if (v === '1' || v === 'true') {
            // Small delay so the drawer slide animation runs after the
            // initial paint settles.
            setTimeout(_tdOpenDrawer, 150);
        }
    } catch {}
}

// Zoom the map to the bounding box of every visible boat's current
// position. Used by shared discussion links (?focus=fleet) so the
// recipient lands on the action, not the whole course. Requires that
// updateBoatPositions() has already run for the target cursor — the
// init hook adds a short setTimeout to guarantee that ordering.
function focusMapOnFleet() {
    if (!map || !boatLayers) return false;
    const points = [];
    for (const id of Object.keys(boatLayers)) {
        const layer = boatLayers[id];
        if (!layer || !layer.visible) continue;
        const cur = layer.current;
        if (cur && cur.lat != null && cur.lon != null) {
            points.push([cur.lat, cur.lon]);
        }
    }
    if (points.length === 0) return false;
    if (points.length === 1) {
        map.setView(points[0], 18, { animate: true });
        return true;
    }
    const bounds = L.latLngBounds(points);
    map.fitBounds(bounds, {
        padding: [60, 60],
        maxZoom: 18,
        animate: true,
    });
    return true;
}

function _tdOpenDrawer() {
    const drawer = document.getElementById('tactics-drawer');
    if (!drawer) return;
    drawer.classList.add('open');

    // Header subtitle: current race name.
    const nm = document.getElementById('td-race-name');
    if (nm) {
        nm.textContent = currentRace?.race_name
            ? currentRace.race_name + (currentRace.date ? ` · ${currentRace.date}` : '')
            : '';
    }

    _tdRefreshComposeUI();

    // GIS button — render lazily (library is async-loaded). Retry a few
    // times if google.accounts isn't ready yet.
    if (!_tdCurrentAuth()) _tdRenderGsiButton();

    // Always re-fetch on open so a refresh shows fresh posts.
    _tdLoadAndRender();

    // Light auto-poll while drawer is open (every 30 s) so collaborators
    // see each other's posts without a manual refresh.
    if (_tdRefreshTimer) clearInterval(_tdRefreshTimer);
    _tdRefreshTimer = setInterval(() => {
        if (drawer.classList.contains('open') && currentRace?.race_id) {
            _tdLoadAndRender();
        }
    }, 30_000);
}

function _tdCloseDrawer() {
    const drawer = document.getElementById('tactics-drawer');
    if (!drawer) return;
    drawer.classList.remove('open');
    if (_tdRefreshTimer) { clearInterval(_tdRefreshTimer); _tdRefreshTimer = null; }
    if (_tdAttachLabelTimer) { clearInterval(_tdAttachLabelTimer); _tdAttachLabelTimer = null; }
}

let _tdGsiAttempts = 0;
function _tdRenderGsiButton() {
    const slot = document.getElementById('td-gsi-button');
    const errEl = document.getElementById('td-signin-err');
    if (!slot) return;
    const clientId = window.SAILFRAMES_GOOGLE_CLIENT_ID;
    if (!clientId) {
        if (errEl) errEl.textContent = 'Google sign-in not configured (SAILFRAMES_GOOGLE_CLIENT_ID).';
        return;
    }
    const g = window.google && window.google.accounts && window.google.accounts.id;
    if (!g) {
        if (_tdGsiAttempts++ < 20) {
            setTimeout(_tdRenderGsiButton, 250);
        } else if (errEl) {
            errEl.textContent = 'Could not load Google sign-in.';
        }
        return;
    }
    try {
        g.initialize({
            client_id: clientId,
            callback: _tdHandleGoogleCredential,
        });
        slot.innerHTML = '';
        g.renderButton(slot, {
            type: 'standard', theme: 'filled_blue', size: 'medium',
            text: 'signin_with', shape: 'rectangular',
        });
    } catch (e) {
        if (errEl) errEl.textContent = 'Sign-in init failed: ' + (e.message || e);
    }
}

function _tdHandleGoogleCredential(response) {
    const errEl = document.getElementById('td-signin-err');
    if (errEl) errEl.textContent = '';
    const token = response && response.credential;
    if (!token) {
        if (errEl) errEl.textContent = 'No credential returned by Google.';
        return;
    }
    const claims = _tdDecodeJwt(token);
    if (!claims || !claims.email) {
        if (errEl) errEl.textContent = 'Could not read email from Google credential.';
        return;
    }
    _tdSaveGuestSession(token, claims.email, claims.name || claims.email);
    _tdRefreshComposeUI();
}

let _tdAttachLabelTimer = null;

function _tdUpdateAttachTimeLabel() {
    const el = document.getElementById('td-attach-time-val');
    if (!el) return;
    _tdAttachSec = Math.max(0, Math.round(playCursorSeconds || currentTime || 0));
    el.textContent = _tdAttachTime ? `(${_tdFmtCursorLocal(_tdAttachSec)})` : '';
    // Keep the displayed time fresh as the cursor moves so it reflects
    // the moment the user is actually about to attach. Without this the
    // label freezes at whatever the cursor was when the box was ticked.
    if (_tdAttachLabelTimer) { clearInterval(_tdAttachLabelTimer); _tdAttachLabelTimer = null; }
    if (_tdAttachTime) {
        _tdAttachLabelTimer = setInterval(() => {
            const cur = Math.max(0, Math.round(playCursorSeconds || currentTime || 0));
            if (cur !== _tdAttachSec) {
                _tdAttachSec = cur;
                el.textContent = `(${_tdFmtCursorLocal(cur)})`;
            }
        }, 500);
    }
}

// Generic toolbar-dropdown wiring. Used for both the Analytics
// dropdown and the Discuss-Tactics-with… dropdown. Item handlers are
// bound separately (each item still owns its own click listener); this
// function only owns open/close/outside-click/Esc.
function setupToolbarDropdown(triggerId, menuId) {
    const trigger = document.getElementById(triggerId);
    const menu = document.getElementById(menuId);
    if (!trigger || !menu) return;

    const close = () => {
        menu.hidden = true;
        trigger.setAttribute('aria-expanded', 'false');
    };
    const open = () => {
        menu.hidden = false;
        trigger.setAttribute('aria-expanded', 'true');
    };

    trigger.addEventListener('click', (e) => {
        e.stopPropagation();
        menu.hidden ? open() : close();
    });

    // Any item-click closes the menu. The item's own existing handler
    // fires before this thanks to event-bubbling order — we just
    // collapse afterwards.
    menu.addEventListener('click', (e) => {
        const item = e.target.closest('.tb-menu-item');
        if (!item) return;
        close();
    });

    document.addEventListener('click', (e) => {
        if (menu.hidden) return;
        if (e.target === trigger) return;
        if (menu.contains(e.target)) return;
        close();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !menu.hidden) close();
    });
}

function setupTacticsDrawer() {
    const btn = document.getElementById('btn-tactics');
    const closeBtn = document.getElementById('td-close');
    const refreshBtn = document.getElementById('td-refresh');
    const shareBtn = document.getElementById('td-share-wa');
    const submitBtn = document.getElementById('td-submit');
    const signoutBtn = document.getElementById('td-signout');
    const bodyEl = document.getElementById('td-body');
    const attachChk = document.getElementById('td-attach-time');

    if (btn) btn.addEventListener('click', _tdOpenDrawer);
    if (closeBtn) closeBtn.addEventListener('click', _tdCloseDrawer);
    if (refreshBtn) refreshBtn.addEventListener('click', _tdLoadAndRender);
    if (shareBtn) shareBtn.addEventListener('click', _tdShareToWhatsApp);

    if (attachChk) attachChk.addEventListener('change', () => {
        _tdAttachTime = attachChk.checked;
        _tdUpdateAttachTimeLabel();
    });

    if (signoutBtn) signoutBtn.addEventListener('click', () => {
        // Only signs out the guest (coach session is managed by the coach app).
        const auth = _tdCurrentAuth();
        if (auth && auth.kind === 'coach') {
            // Coach sign-out goes through the coach app — be explicit.
            if (confirm('Sign out of the coach session? You can sign back in at /coach/login.html.')) {
                try { localStorage.removeItem('sf-coach-id-token'); localStorage.removeItem('sf-coach-email'); } catch {}
            } else {
                return;
            }
        } else {
            _tdClearGuestSession();
        }
        // Forget the role choice on sign-out so the next signed-in
        // user gets the picker rather than inheriting our role.
        _tdSetRole('');
        _tdRefreshComposeUI();
        _tdRenderGsiButton();
    });

    if (submitBtn) submitBtn.addEventListener('click', async () => {
        const errEl = document.getElementById('td-form-err');
        if (errEl) errEl.textContent = '';
        const body = (bodyEl?.value || '').trim();
        if (!body) {
            if (errEl) errEl.textContent = 'Write something first.';
            return;
        }
        if (!currentRace?.race_id) {
            if (errEl) errEl.textContent = 'No race loaded.';
            return;
        }
        // Recapture at submit time — the user may have scrubbed after
        // ticking the box, and they expect the attached moment to be
        // whatever the cursor shows right now.
        const cursorTSec = _tdAttachTime
            ? Math.max(0, Math.round(playCursorSeconds || currentTime || 0))
            : null;
        const role = _tdRole() || 'sailor';
        submitBtn.disabled = true;
        try {
            const post = await _tdSubmitPost(currentRace.race_id, body, cursorTSec, role);
            _tdPostsCache.push(post);
            if (bodyEl) bodyEl.value = '';
            if (attachChk) attachChk.checked = false;
            _tdAttachTime = false;
            _tdUpdateAttachTimeLabel();
            _tdRenderPosts();
        } catch (e) {
            if (errEl) errEl.textContent = e.message || String(e);
            _tdRefreshComposeUI();
            if (!_tdCurrentAuth()) _tdRenderGsiButton();
        } finally {
            submitBtn.disabled = false;
        }
    });

    // Esc closes the tactics drawer too.
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        const drawer = document.getElementById('tactics-drawer');
        if (drawer?.classList.contains('open')) _tdCloseDrawer();
    });
}

// Public API consumed by the chat panel's (t=N) link handler and any
// external integration. Intentionally tiny — the rest of the dashboard
// state is module-internal.
window.SailFramesRace = {
    seekTo(tSec) {
        const t = Math.max(0, Math.min(parseInt(tSec, 10) || 0, raceDuration || 0));
        if (isPlaying) togglePlayback();
        currentTime = t;
        updatePlaybackPosition();
        updatePlayCursor(t);
        const u = new URL(location.href);
        u.searchParams.set('t', t);
        history.replaceState({}, '', u);
    },
    getCurrentT() { return Math.round(currentTime || 0); },
    getRaceId() { return currentRace?.race_id || null; },
};
