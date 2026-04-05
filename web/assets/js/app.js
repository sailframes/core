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

    try {
        // Load session data
        const response = await fetch(
            `${API_BASE}/api/data/${deviceId}/${date}?sensors=gps,imu,wind,pressure`
        );

        if (!response.ok) throw new Error('Failed to fetch session data');

        const sessionData = await response.json();
        currentSession = sessionData;

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

        // Update components
        mapView.setData(gpsData);
        chartPanel.setData(sessionData);

        // Set time controller bounds
        if (sessionData.start_time && sessionData.end_time) {
            window.timeController.setSession(
                sessionData.start_time,
                sessionData.end_time
            );
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

// Initialize on DOM ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
