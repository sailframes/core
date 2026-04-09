/**
 * SailFrames Analytics Application
 * Main entry point that coordinates all components
 */

// API base URL - will be set by config or use relative path
// For S3 hosting, this will be injected as window.SAILFRAMES_API_URL
const API_BASE = window.SAILFRAMES_API_URL || '';

// Global component instances
let mapView = null;
let chartPanel = null;
let videoPlayer = null;
let timeline = null;

// Current session
let currentSession = null;
let currentDeviceId = null;
let currentSessionDate = null;

/**
 * Initialize the application
 */
async function init() {
    console.log('Initializing SailFrames Analytics...');

    // Initialize components
    mapView = new MapView('map');
    chartPanel = new ChartPanel();
    videoPlayer = new VideoPlayer();
    timeline = new Timeline();

    // Expose components globally for cross-component communication
    window.timeline = timeline;
    window.mapView = mapView;
    window.videoPlayer = videoPlayer;

    // Check for ?session= URL parameter first (e.g., ?session=E1/2026-04-04-s013-000013)
    const urlParams = new URLSearchParams(window.location.search);
    const sessionParam = urlParams.get('session');

    // Load sessions list (but don't auto-select if URL param exists)
    await loadSessions(!sessionParam);

    // Setup session selector
    const sessionSelect = document.getElementById('session-select');
    sessionSelect.addEventListener('change', (e) => {
        if (e.target.value) {
            const [deviceId, ...dateParts] = e.target.value.split('/');
            const date = dateParts.join('/');
            loadSession(deviceId, date);
        }
    });

    // Load session from URL parameter if present
    if (sessionParam) {
        const slashIndex = sessionParam.indexOf('/');
        if (slashIndex !== -1) {
            const deviceId = sessionParam.substring(0, slashIndex);
            const date = sessionParam.substring(slashIndex + 1);
            console.log(`Loading session from URL: ${deviceId}/${date}`);
            sessionSelect.value = sessionParam;
            loadSession(deviceId, date);
        }
    }

    // Setup save metadata button
    const btnSaveMeta = document.getElementById('btn-save-meta');
    if (btnSaveMeta) {
        btnSaveMeta.addEventListener('click', saveSessionMeta);
    }

    // Setup track layer toggles
    const gpsToggle = document.getElementById('toggle-gps-track');
    const ppkToggle = document.getElementById('toggle-ppk-track');
    if (gpsToggle) {
        gpsToggle.addEventListener('click', () => {
            gpsToggle.classList.toggle('active');
            mapView.toggleGPS(gpsToggle.classList.contains('active'));
        });
    }
    if (ppkToggle) {
        ppkToggle.addEventListener('click', () => {
            if (ppkToggle.disabled) return;
            ppkToggle.classList.toggle('active');
            mapView.togglePPK(ppkToggle.classList.contains('active'));
        });
    }

    console.log('Initialization complete');
}

/**
 * Load available sessions
 * @param {boolean} autoSelect - Whether to auto-select and load first session
 */
async function loadSessions(autoSelect = true) {
    try {
        const response = await fetch(`${API_BASE}/api/sessions`);
        if (!response.ok) throw new Error('Failed to fetch sessions');

        const data = await response.json();
        const sessions = data.sessions || [];

        const select = document.getElementById('session-select');

        // Clear existing options (keep placeholder)
        while (select.options.length > 1) {
            select.remove(1);
        }

        // Add session options with session_id for E1 sessions
        sessions.forEach(session => {
            const option = document.createElement('option');
            // Include session_id in value for proper matching
            const sessionPath = session.session_id
                ? `${session.date}-${session.session_id}`
                : session.date;
            option.value = `${session.device_id}/${sessionPath}`;

            const duration = session.duration_minutes
                ? ` (${session.duration_minutes} min)`
                : '';
            const video = session.has_video ? ' [VIDEO]' : '';

            option.textContent = `${session.date}${session.session_id ? '-' + session.session_id : ''}${duration}${video}`;
            select.appendChild(option);
        });

        // Auto-select first session if requested and available
        if (autoSelect && sessions.length > 0) {
            const first = sessions[0];
            const firstPath = first.session_id
                ? `${first.date}-${first.session_id}`
                : first.date;
            select.value = `${first.device_id}/${firstPath}`;
            loadSession(first.device_id, firstPath);
        }
    } catch (error) {
        console.error('Error loading sessions:', error);
    }
}

/**
 * Load a specific session
 */
async function loadSession(deviceId, date) {
    console.log(`Loading session: ${deviceId}/${date}`);
    showLoading(true);

    // Store current session info for saving
    currentDeviceId = deviceId;
    currentSessionDate = date;

    try {
        // Load session data
        const response = await fetch(
            `${API_BASE}/api/data/${deviceId}/${date}?sensors=gps,imu,wind,pressure,ppk`
        );

        if (!response.ok) throw new Error('Failed to fetch session data');

        const sessionData = await response.json();
        currentSession = sessionData;

        // Update session meta UI
        updateSessionMetaUI(sessionData);

        // Extract GPS data for map
        const gpsData = sessionData.data
            .filter(p => p.gps)
            .map(p => ({
                t: p.t,
                lat: p.gps.lat,
                lon: p.gps.lon,
                speed_kn: p.gps.speed_kn,
                course: p.gps.course
            }));

        // Extract wind data for map overlay
        const windData = sessionData.data
            .filter(p => p.wind)
            .map(p => ({
                t: p.t,
                awa: p.wind.awa,
                aws_kn: p.wind.aws_kn
            }));

        // Extract PPK data for map overlay
        const ppkData = sessionData.data
            .filter(p => p.ppk)
            .map(p => ({
                t: p.t,
                lat: p.ppk.lat,
                lon: p.ppk.lon,
                quality: p.ppk.quality,
                sdn: p.ppk.sdn,
                sde: p.ppk.sde,
                sdu: p.ppk.sdu,
                sats: p.ppk.sats
            }));

        // Update components
        mapView.setData(gpsData);
        mapView.setPPKData(ppkData);
        mapView.setWindData(windData);
        chartPanel.setData(sessionData);

        // Fetch NOAA buoy data for session time range
        await loadBuoyData(sessionData.start_time, sessionData.end_time);

        // Set time controller bounds (with optional trim)
        if (sessionData.start_time && sessionData.end_time) {
            window.timeController.setSession(
                sessionData.start_time,
                sessionData.end_time,
                sessionData.trim || null
            );
        }

        // Set session info for timeline trim controls
        if (timeline && timeline.setSessionInfo) {
            timeline.setSessionInfo(deviceId, date);
        }

        // Load video streams
        await videoPlayer.loadStreams(deviceId, date);

        console.log(`Loaded ${sessionData.sample_count} data points`);
    } catch (error) {
        console.error('Error loading session:', error);
    } finally {
        showLoading(false);
    }
}

/**
 * Show/hide loading overlay
 */
function showLoading(show) {
    const overlay = document.getElementById('loading');
    overlay.style.display = show ? 'flex' : 'none';
}

/**
 * Load NOAA buoy data for session time range
 */
async function loadBuoyData(startTime, endTime) {
    if (!startTime || !endTime) return;

    try {
        const startTs = new Date(startTime).getTime() / 1000;
        const endTs = new Date(endTime).getTime() / 1000;

        const response = await fetch(
            `${API_BASE}/api/buoys/data?start_ts=${startTs}&end_ts=${endTs}`
        );

        if (!response.ok) {
            console.warn('Failed to fetch buoy data:', response.status);
            return;
        }

        const data = await response.json();
        const buoyData = data.buoys || {};

        // Update map with buoy markers
        if (mapView) {
            mapView.setBuoyData(buoyData);
        }

        // Update chart with buoy time series
        if (chartPanel) {
            chartPanel.setBuoyData(buoyData);
        }

        console.log(`Loaded buoy data for ${Object.keys(buoyData).length} stations`);
    } catch (error) {
        console.error('Error loading buoy data:', error);
    }
}

/**
 * Update session metadata UI fields
 */
function updateSessionMetaUI(sessionData) {
    const metaContainer = document.getElementById('session-meta');
    const nameInput = document.getElementById('session-name');
    const boatSelect = document.getElementById('boat-select');

    if (metaContainer) {
        metaContainer.style.display = 'flex';
    }

    if (nameInput) {
        nameInput.value = sessionData.name || '';
    }

    if (boatSelect) {
        boatSelect.value = sessionData.boat || '';
    }
}

/**
 * Save session metadata (name, boat)
 */
async function saveSessionMeta() {
    if (!currentDeviceId || !currentSessionDate) {
        console.error('No session loaded');
        return;
    }

    const nameInput = document.getElementById('session-name');
    const boatSelect = document.getElementById('boat-select');
    const btnSave = document.getElementById('btn-save-meta');

    const name = nameInput?.value?.trim() || null;
    const boat = boatSelect?.value || null;

    // Disable button while saving
    if (btnSave) {
        btnSave.disabled = true;
        btnSave.textContent = 'Saving...';
    }

    try {
        const response = await fetch(`${API_BASE}/api/sessions/${currentDeviceId}/${currentSessionDate}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, boat })
        });

        if (!response.ok) {
            throw new Error('Failed to save');
        }

        // Visual feedback
        if (btnSave) {
            btnSave.textContent = 'Saved!';
            btnSave.style.background = 'var(--success)';
            setTimeout(() => {
                btnSave.textContent = 'Save';
                btnSave.style.background = '';
            }, 1500);
        }

        console.log('Session metadata saved');
    } catch (err) {
        console.error('Failed to save session metadata:', err);
        alert('Failed to save: ' + err.message);
    } finally {
        if (btnSave) {
            btnSave.disabled = false;
            if (btnSave.textContent === 'Saving...') {
                btnSave.textContent = 'Save';
            }
        }
    }
}

// Initialize on DOM ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
