/**
 * ChartPanel - Unified time-series chart with toggleable measurements
 */
class ChartPanel {
    constructor() {
        this.chart = null;
        this.data = {};
        this.dataIndex = {};
        this.rawData = {};
        this.visibleSeries = new Set(['heel', 'pitch']);  // Default visible
        this.zoomLevel = 1;
        this.zoomCenter = 0.5;  // Center of visible range (0-1)
        this.verticalLinePlugin = this._createVerticalLinePlugin();

        this._init();
        this._setupTimeSync();
        this._setupToggles();
        this._setupZoom();
    }

    _createVerticalLinePlugin() {
        return {
            id: 'verticalLine',
            afterDraw: (chart) => {
                if (chart.verticalLineX === undefined) return;

                const ctx = chart.ctx;
                const x = chart.verticalLineX;
                const topY = chart.chartArea.top;
                const bottomY = chart.chartArea.bottom;

                ctx.save();
                ctx.beginPath();
                ctx.moveTo(x, topY);
                ctx.lineTo(x, bottomY);
                ctx.lineWidth = 2;
                ctx.strokeStyle = '#00ba7c';
                ctx.stroke();
                ctx.restore();
            }
        };
    }

    _init() {
        const ctx = document.getElementById('chart-unified');
        if (!ctx) return;

        // Define all series with their properties
        this.seriesConfig = {
            heel:     { label: 'Heel (°)',      color: '#1d9bf0', yAxis: 'yDeg' },
            pitch:    { label: 'Pitch (°)',     color: '#ffad1f', yAxis: 'yDeg' },
            aws:      { label: 'AWS (kn)',      color: '#00ba7c', yAxis: 'ySpeed' },
            awa:      { label: 'AWA (°)',       color: '#f4212e', yAxis: 'yAngle' },
            sog:      { label: 'SOG (kn)',      color: '#e879f9', yAxis: 'ySpeed' },
            heading:  { label: 'Heading (°)',   color: '#38bdf8', yAxis: 'yAngle' },
            course:   { label: 'Course (°)',    color: '#fbbf24', yAxis: 'yAngle' },
            accelX:   { label: 'Accel X (m/s²)',color: '#fb7185', yAxis: 'yAccel' },
            accelY:   { label: 'Accel Y (m/s²)',color: '#4ade80', yAxis: 'yAccel' },
            pressure: { label: 'Pressure (hPa)',color: '#a78bfa', yAxis: 'yPressure' }
        };

        // Create datasets for each series
        const datasets = Object.entries(this.seriesConfig).map(([key, config]) => ({
            label: config.label,
            data: [],
            borderColor: config.color,
            backgroundColor: 'transparent',
            borderWidth: 1.5,
            pointRadius: 0,
            tension: 0.1,
            hidden: !this.visibleSeries.has(key),
            yAxisID: config.yAxis,
            seriesKey: key
        }));

        this.chart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: [],
                datasets: datasets
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                interaction: {
                    mode: 'index',
                    intersect: false
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        enabled: true,
                        mode: 'index',
                        intersect: false,
                        callbacks: {
                            title: (items) => {
                                if (items.length > 0) {
                                    const d = new Date(items[0].label);
                                    return d.toLocaleTimeString();
                                }
                                return '';
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        display: true,
                        type: 'category',
                        ticks: {
                            color: '#8b98a5',
                            font: { size: 10 },
                            maxTicksLimit: 12,
                            callback: function(value, index) {
                                const label = this.getLabelForValue(value);
                                if (label) {
                                    const d = new Date(label);
                                    return d.toLocaleTimeString('en-US', {
                                        hour: '2-digit',
                                        minute: '2-digit',
                                        second: '2-digit'
                                    });
                                }
                                return '';
                            }
                        },
                        grid: { color: 'rgba(47, 51, 54, 0.3)' }
                    },
                    yDeg: {
                        type: 'linear',
                        display: 'auto',
                        position: 'left',
                        title: { display: true, text: 'Degrees', color: '#8b98a5' },
                        ticks: { color: '#8b98a5', font: { size: 10 } },
                        grid: { color: 'rgba(47, 51, 54, 0.3)' },
                        min: -45,
                        max: 45
                    },
                    ySpeed: {
                        type: 'linear',
                        display: 'auto',
                        position: 'right',
                        title: { display: true, text: 'Knots', color: '#8b98a5' },
                        ticks: { color: '#8b98a5', font: { size: 10 } },
                        grid: { display: false },
                        min: 0,
                        max: 30
                    },
                    yAngle: {
                        type: 'linear',
                        display: 'auto',
                        position: 'right',
                        title: { display: true, text: 'Angle (°)', color: '#8b98a5' },
                        ticks: { color: '#8b98a5', font: { size: 10 } },
                        grid: { display: false },
                        min: 0,
                        max: 360
                    },
                    yAccel: {
                        type: 'linear',
                        display: 'auto',
                        position: 'left',
                        title: { display: true, text: 'm/s²', color: '#8b98a5' },
                        ticks: { color: '#8b98a5', font: { size: 10 } },
                        grid: { display: false },
                        min: -5,
                        max: 5
                    },
                    yPressure: {
                        type: 'linear',
                        display: 'auto',
                        position: 'right',
                        title: { display: true, text: 'hPa', color: '#8b98a5' },
                        ticks: { color: '#8b98a5', font: { size: 10 } },
                        grid: { display: false }
                    }
                }
            },
            plugins: [this.verticalLinePlugin]
        });

        this._setupChartInteraction();
    }

    _setupTimeSync() {
        window.timeController.addEventListener('time-change', (e) => {
            this.updateCursor(e.detail.time);
        });
    }

    _setupToggles() {
        const container = document.getElementById('chart-toggles');
        if (!container) return;

        container.querySelectorAll('.toggle-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const series = btn.dataset.series;
                btn.classList.toggle('active');

                if (btn.classList.contains('active')) {
                    this.visibleSeries.add(series);
                } else {
                    this.visibleSeries.delete(series);
                }

                this._updateSeriesVisibility();
            });
        });
    }

    _setupZoom() {
        const zoomIn = document.getElementById('btn-zoom-in');
        const zoomOut = document.getElementById('btn-zoom-out');
        const zoomReset = document.getElementById('btn-zoom-reset');

        if (zoomIn) {
            zoomIn.addEventListener('click', () => this._zoom(1.5));
        }
        if (zoomOut) {
            zoomOut.addEventListener('click', () => this._zoom(1 / 1.5));
        }
        if (zoomReset) {
            zoomReset.addEventListener('click', () => this._resetZoom());
        }

        // Mouse wheel zoom on chart
        const canvas = document.getElementById('chart-unified');
        if (canvas) {
            canvas.addEventListener('wheel', (e) => {
                e.preventDefault();
                const factor = e.deltaY < 0 ? 1.2 : 1 / 1.2;

                // Get mouse position relative to chart for zoom center
                const rect = canvas.getBoundingClientRect();
                const chartArea = this.chart?.chartArea;
                if (chartArea) {
                    const x = e.clientX - rect.left;
                    const relX = (x - chartArea.left) / (chartArea.right - chartArea.left);
                    this.zoomCenter = Math.max(0, Math.min(1, relX));
                }

                this._zoom(factor);
            }, { passive: false });
        }
    }

    _zoom(factor) {
        const newZoom = Math.max(1, Math.min(50, this.zoomLevel * factor));
        if (newZoom === this.zoomLevel) return;

        this.zoomLevel = newZoom;
        this._applyZoom();
        this._updateZoomLabel();
    }

    _resetZoom() {
        this.zoomLevel = 1;
        this.zoomCenter = 0.5;
        this._applyZoom();
        this._updateZoomLabel();
    }

    _applyZoom() {
        if (!this.chart || !this.fullLabels) return;

        const totalPoints = this.fullLabels.length;
        const visiblePoints = Math.max(10, Math.floor(totalPoints / this.zoomLevel));

        // Calculate start/end based on zoom center
        const halfVisible = visiblePoints / 2;
        const centerIndex = Math.floor(this.zoomCenter * totalPoints);

        let startIndex = Math.floor(centerIndex - halfVisible);
        let endIndex = Math.floor(centerIndex + halfVisible);

        // Clamp to bounds
        if (startIndex < 0) {
            startIndex = 0;
            endIndex = Math.min(totalPoints, visiblePoints);
        }
        if (endIndex > totalPoints) {
            endIndex = totalPoints;
            startIndex = Math.max(0, totalPoints - visiblePoints);
        }

        // Slice data for visible range
        const visibleLabels = this.fullLabels.slice(startIndex, endIndex);

        this.chart.data.labels = visibleLabels;

        // Update each dataset with sliced data
        const seriesKeys = ['heel', 'pitch', 'aws', 'awa', 'sog', 'heading', 'course', 'accelX', 'accelY', 'pressure'];
        this.chart.data.datasets.forEach((dataset, i) => {
            const key = seriesKeys[i];
            if (this.fullData[key]) {
                dataset.data = this.fullData[key].slice(startIndex, endIndex);
            }
        });

        // Store visible range for cursor mapping
        this.visibleStartIndex = startIndex;
        this.visibleEndIndex = endIndex;

        this.chart.update('none');
    }

    _updateZoomLabel() {
        const label = document.getElementById('zoom-label');
        if (label) {
            label.textContent = `${Math.round(this.zoomLevel * 100)}%`;
        }
    }

    _updateSeriesVisibility() {
        if (!this.chart) return;

        this.chart.data.datasets.forEach(dataset => {
            const key = dataset.seriesKey;
            dataset.hidden = !this.visibleSeries.has(key);
        });

        this.chart.update('none');
    }

    _setupChartInteraction() {
        if (!this.chart) return;

        const canvas = this.chart.canvas;
        let isDragging = false;

        const getTimeFromEvent = (e) => {
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;

            const chartArea = this.chart.chartArea;
            if (!chartArea) return null;

            if (x < chartArea.left || x > chartArea.right) return null;

            const position = (x - chartArea.left) / (chartArea.right - chartArea.left);
            const visibleLabels = this.chart.data.labels;
            if (!visibleLabels || visibleLabels.length === 0) return null;

            const index = Math.round(position * (visibleLabels.length - 1));
            const clampedIndex = Math.max(0, Math.min(visibleLabels.length - 1, index));

            const timestamp = visibleLabels[clampedIndex];
            if (!timestamp) return null;

            return new Date(timestamp);
        };

        const handleSeek = (e) => {
            const time = getTimeFromEvent(e);
            if (time) {
                window.timeController.seek(time);
            }
        };

        canvas.addEventListener('mousedown', (e) => {
            if (e.button !== 0) return;
            isDragging = true;
            handleSeek(e);
            e.preventDefault();
        });

        canvas.addEventListener('mousemove', (e) => {
            if (!isDragging) return;
            handleSeek(e);
        });

        canvas.addEventListener('mouseup', () => {
            isDragging = false;
        });

        canvas.addEventListener('mouseleave', () => {
            isDragging = false;
        });

        canvas.style.cursor = 'crosshair';
    }

    /**
     * Load session data
     */
    setData(sessionData) {
        const data = sessionData.data || [];

        // Extract all data arrays
        const labels = [];
        const heel = [], pitch = [];
        const aws = [], awa = [];
        const sog = [], course = [];
        const heading = [];
        const accelX = [], accelY = [];
        const pressure = [];

        this.dataIndex = {};

        data.forEach((point, i) => {
            labels.push(point.t);
            this.dataIndex[point.t.substring(0, 19)] = i;

            // IMU data
            if (point.imu) {
                heel.push(point.imu.heel);
                pitch.push(point.imu.pitch);
                heading.push(point.imu.heading);
                accelX.push(point.imu.accel_x);
                accelY.push(point.imu.accel_y);
            } else {
                heel.push(null);
                pitch.push(null);
                heading.push(null);
                accelX.push(null);
                accelY.push(null);
            }

            // Wind data
            if (point.wind) {
                aws.push(point.wind.aws_kn);
                awa.push(point.wind.awa);
            } else {
                aws.push(null);
                awa.push(null);
            }

            // GPS data
            if (point.gps) {
                sog.push(point.gps.speed_kn);
                course.push(point.gps.course);
            } else {
                sog.push(null);
                course.push(null);
            }

            // Pressure data
            if (point.pressure) {
                pressure.push(point.pressure.hpa);
            } else {
                pressure.push(null);
            }
        });

        // Store full data for zooming
        this.fullLabels = labels;
        this.fullData = { heel, pitch, aws, awa, sog, heading, course, accelX, accelY, pressure };
        this.data.labels = labels;

        // Reset zoom and apply data
        this.zoomLevel = 1;
        this._updateZoomLabel();
        this._applyZoom();
    }

    /**
     * Update cursor position
     */
    updateCursor(time) {
        if (!time || !this.chart || !this.chart.data.labels) return;

        const timeStr = time.toISOString().substring(0, 19);

        // Find index in visible data
        const visibleLabels = this.chart.data.labels;
        let index = -1;

        for (let i = 0; i < visibleLabels.length; i++) {
            if (visibleLabels[i].substring(0, 19) === timeStr) {
                index = i;
                break;
            }
        }

        if (index === -1) {
            // Find closest
            const targetMs = time.getTime();
            let minDiff = Infinity;
            visibleLabels.forEach((label, i) => {
                const diff = Math.abs(new Date(label).getTime() - targetMs);
                if (diff < minDiff) {
                    minDiff = diff;
                    index = i;
                }
            });
        }

        if (index >= 0) {
            const meta = this.chart.getDatasetMeta(0);
            if (meta.data[index]) {
                this.chart.verticalLineX = meta.data[index].x;
                this.chart.draw();
            }
        }
    }
}

window.ChartPanel = ChartPanel;
