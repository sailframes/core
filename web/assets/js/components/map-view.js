/**
 * MapView - GPS track visualization with Leaflet
 */
class MapView {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        this.map = null;
        this.trackLayer = null;
        this.positionMarker = null;
        this.data = [];
        this.dataIndex = {};

        this._init();
        this._setupTimeSync();
    }

    _init() {
        // Initialize Leaflet map
        this.map = L.map(this.container, {
            center: [42.35, -71.05], // Boston Harbor default
            zoom: 13,
            zoomControl: true
        });

        // Dark tile layer (CartoDB Dark Matter)
        L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
            attribution: '&copy; OpenStreetMap &copy; CartoDB',
            maxZoom: 19
        }).addTo(this.map);

        // Track polyline
        this.trackLayer = L.polyline([], {
            color: '#1d9bf0',
            weight: 3,
            opacity: 0.8
        }).addTo(this.map);

        // Boat position marker
        const boatIcon = L.divIcon({
            className: 'boat-marker',
            html: `<svg viewBox="0 0 24 24" width="24" height="24">
                <path fill="#00ba7c" d="M12 2L4 22h16L12 2z"/>
            </svg>`,
            iconSize: [24, 24],
            iconAnchor: [12, 12]
        });

        this.positionMarker = L.marker([0, 0], {
            icon: boatIcon,
            rotationOrigin: 'center center'
        }).addTo(this.map);

        // Add marker rotation support
        this._addMarkerRotation();
    }

    _addMarkerRotation() {
        // Rotation support that preserves Leaflet's translate transform
        const marker = this.positionMarker;
        marker._rotationAngle = 0;

        marker.setRotation = function(angle) {
            this._rotationAngle = angle;
            const icon = this.getElement();
            if (icon) {
                // Find the SVG inside and rotate only that, not the container
                const svg = icon.querySelector('svg');
                if (svg) {
                    svg.style.transform = `rotate(${angle}deg)`;
                    svg.style.transformOrigin = 'center center';
                }
            }
        };
    }

    _setupTimeSync() {
        window.timeController.addEventListener('time-change', (e) => {
            this.updatePosition(e.detail.time);
        });
    }

    /**
     * Load GPS track data
     */
    setData(gpsData) {
        this.data = gpsData || [];

        // Build time index for fast lookup
        this.dataIndex = {};
        this.data.forEach((point, i) => {
            const key = point.t.substring(0, 19); // Truncate to second
            this.dataIndex[key] = i;
        });

        // Draw full track
        const latlngs = this.data.map(p => [p.lat, p.lon]);
        this.trackLayer.setLatLngs(latlngs);

        // Fit bounds
        if (latlngs.length > 0) {
            this.map.fitBounds(this.trackLayer.getBounds(), {
                padding: [50, 50]
            });
        }

        // Set initial position
        if (this.data.length > 0) {
            const first = this.data[0];
            this.positionMarker.setLatLng([first.lat, first.lon]);
            if (first.course) {
                this.positionMarker.setRotation(first.course);
            }
        }
    }

    /**
     * Update position based on current time
     */
    updatePosition(time) {
        if (!time || this.data.length === 0) return;

        const point = this._findClosestPoint(time);
        if (!point) return;

        this.positionMarker.setLatLng([point.lat, point.lon]);

        if (point.course !== undefined) {
            this.positionMarker.setRotation(point.course);
        }

        // Update stats display
        const speedEl = document.getElementById('stat-speed');
        const courseEl = document.getElementById('stat-course');

        if (speedEl) speedEl.textContent = point.speed_kn?.toFixed(1) || '--';
        if (courseEl) courseEl.textContent = point.course?.toFixed(0) || '--';
    }

    /**
     * Find closest data point to given time
     */
    _findClosestPoint(time) {
        const timeStr = time.toISOString().substring(0, 19);

        // Try exact match first
        if (this.dataIndex[timeStr] !== undefined) {
            return this.data[this.dataIndex[timeStr]];
        }

        // Binary search for closest
        const targetMs = time.getTime();
        let low = 0;
        let high = this.data.length - 1;

        while (low < high) {
            const mid = Math.floor((low + high) / 2);
            const midTime = new Date(this.data[mid].t).getTime();

            if (midTime < targetMs) {
                low = mid + 1;
            } else {
                high = mid;
            }
        }

        return this.data[low] || this.data[this.data.length - 1];
    }
}

window.MapView = MapView;
