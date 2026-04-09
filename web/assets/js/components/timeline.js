/**
 * Timeline - Playback controls and scrubber
 */
class Timeline {
    constructor() {
        this.slider = document.getElementById('timeline');
        this.btnPlay = document.getElementById('btn-play');
        this.speedSelect = document.getElementById('playback-speed');
        this.timeCurrent = document.getElementById('time-current');
        this.timeDuration = document.getElementById('time-duration');
        this.timeAbsolute = document.getElementById('time-absolute');
        this.btnTrimBefore = document.getElementById('btn-trim-before');
        this.btnTrimAfter = document.getElementById('btn-trim-after');
        this.btnTrimReset = document.getElementById('btn-trim-reset');
        this.videoCoverage = document.getElementById('video-coverage');

        this._deviceId = null;
        this._sessionDate = null;
        this._sessionStart = null;
        this._sessionEnd = null;

        this._setupControls();
        this._setupTimeSync();
        this._setupMapCollapse();
        this._setupTrimControls();
    }

    _setupControls() {
        // Play/pause button
        this.btnPlay.addEventListener('click', () => {
            window.timeController.toggle();
        });

        // Speed selector
        this.speedSelect.addEventListener('change', (e) => {
            const rate = parseFloat(e.target.value);
            window.timeController.setPlaybackRate(rate);

            // Also update video playback rate
            if (window.videoPlayer) {
                window.videoPlayer.setPlaybackRate(rate);
            }
        });

        // Timeline scrubber
        this.slider.addEventListener('input', (e) => {
            const position = parseFloat(e.target.value) / 100;
            window.timeController.seekToPosition(position);
        });

        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;

            switch (e.code) {
                case 'Space':
                    e.preventDefault();
                    window.timeController.toggle();
                    break;
                case 'ArrowLeft':
                    e.preventDefault();
                    this._seekRelative(-5000); // -5 seconds
                    break;
                case 'ArrowRight':
                    e.preventDefault();
                    this._seekRelative(5000); // +5 seconds
                    break;
                case 'ArrowUp':
                    e.preventDefault();
                    this._changeSpeed(1);
                    break;
                case 'ArrowDown':
                    e.preventDefault();
                    this._changeSpeed(-1);
                    break;
            }
        });
    }

    _setupTimeSync() {
        window.timeController.addEventListener('session-loaded', (e) => {
            this.timeDuration.textContent = this._formatDuration(e.detail.duration);
            this.slider.value = 0;
            this._updateTrimResetButton(e.detail.isTrimmed);

            // Store session bounds for video coverage calculation
            this._sessionStart = e.detail.start;
            this._sessionEnd = e.detail.end;

            // Clear any previous video coverage
            this._clearVideoCoverage();
        });

        window.timeController.addEventListener('time-change', (e) => {
            // Update slider position
            this.slider.value = e.detail.position * 100;

            // Update current time display (elapsed)
            this.timeCurrent.textContent = this._formatDuration(e.detail.elapsed);

            // Update absolute time display
            if (e.detail.time && this.timeAbsolute) {
                this.timeAbsolute.textContent = this._formatAbsoluteTime(e.detail.time);
            }
        });

        window.timeController.addEventListener('play', () => {
            this._updatePlayButton(true);
        });

        window.timeController.addEventListener('pause', () => {
            this._updatePlayButton(false);
        });
    }

    _updatePlayButton(isPlaying) {
        const iconPlay = this.btnPlay.querySelector('.icon-play');
        const iconPause = this.btnPlay.querySelector('.icon-pause');

        if (isPlaying) {
            iconPlay.style.display = 'none';
            iconPause.style.display = 'inline';
        } else {
            iconPlay.style.display = 'inline';
            iconPause.style.display = 'none';
        }
    }

    _seekRelative(deltaMs) {
        const current = window.timeController.getCurrentTime();
        if (current) {
            window.timeController.seek(new Date(current.getTime() + deltaMs));
        }
    }

    _changeSpeed(direction) {
        const options = Array.from(this.speedSelect.options);
        const currentIndex = this.speedSelect.selectedIndex;
        const newIndex = Math.max(0, Math.min(options.length - 1, currentIndex + direction));

        if (newIndex !== currentIndex) {
            this.speedSelect.selectedIndex = newIndex;
            this.speedSelect.dispatchEvent(new Event('change'));
        }
    }

    _formatDuration(ms) {
        const totalSeconds = Math.floor(ms / 1000);
        const hours = Math.floor(totalSeconds / 3600);
        const minutes = Math.floor((totalSeconds % 3600) / 60);
        const seconds = totalSeconds % 60;

        const pad = (n) => n.toString().padStart(2, '0');

        if (hours > 0) {
            return `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`;
        }
        return `${pad(minutes)}:${pad(seconds)}`;
    }

    _formatAbsoluteTime(date) {
        if (!date) return '--:--:--';
        const hours = date.getHours().toString().padStart(2, '0');
        const minutes = date.getMinutes().toString().padStart(2, '0');
        const seconds = date.getSeconds().toString().padStart(2, '0');
        return `${hours}:${minutes}:${seconds}`;
    }

    _setupTrimControls() {
        if (!this.btnTrimBefore || !this.btnTrimAfter) return;

        this.btnTrimBefore.addEventListener('click', () => this._handleTrimBefore());
        this.btnTrimAfter.addEventListener('click', () => this._handleTrimAfter());

        if (this.btnTrimReset) {
            this.btnTrimReset.addEventListener('click', () => this._handleTrimReset());
        }

        // Listen for trim changes to update UI
        window.timeController.addEventListener('trim-change', (e) => {
            this._updateTrimResetButton(e.detail.isTrimmed);
            this.timeDuration.textContent = this._formatDuration(e.detail.duration);
            // Reset slider to start position
            this.slider.value = 0;
            this.timeCurrent.textContent = this._formatDuration(0);
        });
    }

    /**
     * Set current session info (called from app.js when loading session)
     */
    setSessionInfo(deviceId, sessionDate) {
        this._deviceId = deviceId;
        this._sessionDate = sessionDate;

        // Enable trim buttons when session is loaded
        if (this.btnTrimBefore) this.btnTrimBefore.disabled = false;
        if (this.btnTrimAfter) this.btnTrimAfter.disabled = false;
    }

    async _handleTrimBefore() {
        const currentTime = window.timeController.getCurrentTime();
        if (!currentTime || !this._deviceId || !this._sessionDate) return;

        const timeStr = this._formatAbsoluteTime(currentTime);
        const confirmed = confirm(
            `Delete all data BEFORE ${timeStr}?\n\n` +
            `This will permanently remove GPS, IMU, and wind data.\n` +
            `This action cannot be undone.`
        );
        if (!confirmed) return;

        const trimStart = currentTime.toISOString();
        const bounds = window.timeController.getTrimBounds();

        try {
            const result = await this._saveTrim({ start: trimStart, end: bounds.end });
            window.timeController.setTrim(currentTime, null);
            this._showTrimNotification('Data before ' + timeStr + ' deleted');
            if (result.trim_stats) {
                console.log('Trim stats:', result.trim_stats);
            }
        } catch (err) {
            console.error('Failed to trim:', err);
            alert('Failed to trim: ' + err.message);
        }
    }

    async _handleTrimAfter() {
        const currentTime = window.timeController.getCurrentTime();
        if (!currentTime || !this._deviceId || !this._sessionDate) return;

        const timeStr = this._formatAbsoluteTime(currentTime);
        const confirmed = confirm(
            `Delete all data AFTER ${timeStr}?\n\n` +
            `This will permanently remove GPS, IMU, and wind data.\n` +
            `This action cannot be undone.`
        );
        if (!confirmed) return;

        const trimEnd = currentTime.toISOString();
        const bounds = window.timeController.getTrimBounds();

        try {
            const result = await this._saveTrim({ start: bounds.start, end: trimEnd });
            window.timeController.setTrim(null, currentTime);
            this._showTrimNotification('Data after ' + timeStr + ' deleted');
            if (result.trim_stats) {
                console.log('Trim stats:', result.trim_stats);
            }
        } catch (err) {
            console.error('Failed to trim:', err);
            alert('Failed to trim: ' + err.message);
        }
    }

    async _handleTrimReset() {
        // Trim reset no longer supported - data deletion is permanent
        // This button should be hidden, but handle gracefully if called
        alert('Trim is permanent. Deleted data cannot be restored.');
    }

    async _saveTrim(trim) {
        const API_BASE = window.SAILFRAMES_API_URL || '';
        const response = await fetch(`${API_BASE}/api/sessions/${this._deviceId}/${this._sessionDate}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ trim })
        });

        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.error || 'Failed to save trim');
        }

        return response.json();
    }

    _updateTrimResetButton(isTrimmed) {
        // Trim is now permanent (deletes data), so reset button is always hidden
        if (this.btnTrimReset) {
            this.btnTrimReset.style.display = 'none';
        }
    }

    _showTrimNotification(message) {
        // Simple visual feedback - briefly change button color
        const btn = document.activeElement;
        if (btn && btn.classList.contains('btn-trim')) {
            btn.style.background = 'var(--success)';
            btn.style.color = 'white';
            setTimeout(() => {
                btn.style.background = '';
                btn.style.color = '';
            }, 500);
        }
        console.log('Trim:', message);
    }

    /**
     * Set video coverage segments on the timeline
     * @param {Array} videos - Array of video objects with start_time, end_time
     */
    setVideoCoverage(videos) {
        this._clearVideoCoverage();

        if (!videos || videos.length === 0 || !this._sessionStart || !this._sessionEnd) {
            return;
        }

        const sessionStartMs = this._sessionStart.getTime();
        const sessionEndMs = this._sessionEnd.getTime();
        const sessionDuration = sessionEndMs - sessionStartMs;

        if (sessionDuration <= 0) return;

        videos.forEach(video => {
            if (!video.start_time || !video.end_time) return;

            const videoStart = new Date(video.start_time).getTime();
            const videoEnd = new Date(video.end_time).getTime();

            // Calculate position as percentage of session duration
            const startPct = Math.max(0, (videoStart - sessionStartMs) / sessionDuration * 100);
            const endPct = Math.min(100, (videoEnd - sessionStartMs) / sessionDuration * 100);
            const widthPct = endPct - startPct;

            if (widthPct > 0) {
                const segment = document.createElement('div');
                segment.className = 'video-segment';
                segment.style.left = `${startPct}%`;
                segment.style.width = `${widthPct}%`;
                segment.title = `Video: ${this._formatAbsoluteTime(new Date(videoStart))} - ${this._formatAbsoluteTime(new Date(videoEnd))}`;
                this.videoCoverage.appendChild(segment);
            }
        });
    }

    /**
     * Clear video coverage indicators
     */
    _clearVideoCoverage() {
        if (this.videoCoverage) {
            this.videoCoverage.innerHTML = '';
        }
    }

    _setupMapCollapse() {
        const mapPanel = document.getElementById('map-panel');
        const btnCollapse = document.getElementById('btn-collapse-map');
        const resizeHandle = document.getElementById('map-resize-handle');

        if (!mapPanel || !btnCollapse) return;

        // Toggle collapse
        btnCollapse.addEventListener('click', () => {
            mapPanel.classList.toggle('collapsed');
            // Trigger map resize after animation
            setTimeout(() => {
                if (window.mapView && window.mapView.map) {
                    window.mapView.map.invalidateSize();
                }
            }, 350);
        });

        // Resize functionality
        if (resizeHandle) {
            let isResizing = false;
            let startX, startWidth;

            resizeHandle.addEventListener('mousedown', (e) => {
                isResizing = true;
                startX = e.clientX;
                startWidth = mapPanel.offsetWidth;
                resizeHandle.classList.add('resizing');
                document.body.style.cursor = 'ew-resize';
                document.body.style.userSelect = 'none';
                e.preventDefault();
            });

            document.addEventListener('mousemove', (e) => {
                if (!isResizing) return;
                const delta = e.clientX - startX;
                const newWidth = Math.max(200, Math.min(startWidth + delta, window.innerWidth * 0.6));
                mapPanel.style.flexBasis = newWidth + 'px';
            });

            document.addEventListener('mouseup', () => {
                if (isResizing) {
                    isResizing = false;
                    resizeHandle.classList.remove('resizing');
                    document.body.style.cursor = '';
                    document.body.style.userSelect = '';
                    // Trigger map resize
                    if (window.mapView && window.mapView.map) {
                        window.mapView.map.invalidateSize();
                    }
                }
            });
        }
    }
}

window.Timeline = Timeline;
