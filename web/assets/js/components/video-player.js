/**
 * VideoPlayer - HLS video playback synchronized with timeline
 * Videos sync on play, seek, and periodically during playback to prevent drift
 */
class VideoPlayer {
    constructor() {
        this.hlsInstances = {
            cockpit: null
        };
        this.videoElements = {
            cockpit: document.getElementById('video-cockpit')
        };
        this.videoContainer = document.getElementById('video-container');
        this.streamInfo = null;
        this.videosReady = { cockpit: false };
        this.syncInterval = null;
        this.isPlaying = false;

        this._setupControls();
    }

    _setupControls() {
        // Sync video on any time change (scrubbing, chart clicks, etc.)
        window.timeController.addEventListener('time-change', (e) => {
            // Only seek when paused (during playback, drift correction handles it)
            if (!this.isPlaying && e.detail.time) {
                this._seekVideos(e.detail.time);
            }
        });

        // Play/pause with main controls
        const btnPlay = document.getElementById('btn-play');
        if (btnPlay) {
            btnPlay.addEventListener('click', () => {
                // Small delay to let TimeController state update
                setTimeout(() => {
                    if (window.timeController.isPlaying()) {
                        this._playAll();
                    } else {
                        this._pauseAll();
                    }
                }, 50);
            });
        }

        // Keyboard space bar
        document.addEventListener('keydown', (e) => {
            if (e.code === 'Space' && e.target.tagName !== 'INPUT') {
                setTimeout(() => {
                    if (window.timeController.isPlaying()) {
                        this._playAll();
                    } else {
                        this._pauseAll();
                    }
                }, 50);
            }
        });

        // Speed changes
        const speedSelect = document.getElementById('playback-speed');
        if (speedSelect) {
            speedSelect.addEventListener('change', (e) => {
                const rate = parseFloat(e.target.value);
                this._setPlaybackRate(rate);
            });
        }
    }

    /**
     * Load video streams for a session
     */
    async loadStreams(deviceId, date) {
        try {
            const response = await fetch(`/api/video/${deviceId}/${date}`);
            if (!response.ok) {
                console.warn('No video available for this session');
                this.videoContainer.style.display = 'none';
                return;
            }

            const data = await response.json();
            this.streamInfo = data.streams;

            // Check if any streams available
            const hasStreams = Object.values(this.streamInfo).some(s => s && s.playlist_url);
            if (!hasStreams) {
                this.videoContainer.style.display = 'none';
                return;
            }

            // Use block for compact layout, flex for full layout
            this.videoContainer.style.display = this.videoContainer.classList.contains('video-compact') ? 'block' : 'flex';

            // Initialize HLS for each stream
            for (const [camera, info] of Object.entries(this.streamInfo)) {
                if (info && info.playlist_url) {
                    this._initHLS(camera, info);
                }
            }
        } catch (error) {
            console.error('Error loading video streams:', error);
            this.videoContainer.style.display = 'none';
        }
    }

    _initHLS(camera, streamInfo) {
        const video = this.videoElements[camera];
        if (!video) return;

        // Destroy existing HLS instance
        if (this.hlsInstances[camera]) {
            this.hlsInstances[camera].destroy();
        }

        this.videosReady[camera] = false;

        if (Hls.isSupported()) {
            const hls = new Hls({
                enableWorker: true,
                lowLatencyMode: false,
                // Large buffer for smooth playback
                maxBufferLength: 60,
                maxMaxBufferLength: 120,
                maxBufferSize: 120 * 1000 * 1000,
                // Stability settings
                fragLoadingTimeOut: 20000,
                manifestLoadingTimeOut: 10000,
                levelLoadingTimeOut: 10000
            });

            hls.loadSource(streamInfo.playlist_url);
            hls.attachMedia(video);

            hls.on(Hls.Events.MANIFEST_PARSED, () => {
                console.log(`HLS ready: ${camera}`);
                this.videosReady[camera] = true;

                // Initial seek to current timeline position
                const currentTime = window.timeController.getCurrentTime();
                if (currentTime && streamInfo.start_time) {
                    const streamStart = new Date(streamInfo.start_time).getTime();
                    const offsetSeconds = (currentTime.getTime() - streamStart) / 1000;
                    if (offsetSeconds >= 0 && offsetSeconds <= video.duration) {
                        video.currentTime = offsetSeconds;
                    }
                }
            });

            hls.on(Hls.Events.ERROR, (event, data) => {
                if (data.fatal) {
                    console.error(`HLS error ${camera}:`, data.type);
                    if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
                        hls.startLoad();
                    } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
                        hls.recoverMediaError();
                    }
                }
            });

            this.hlsInstances[camera] = hls;
        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
            video.src = streamInfo.playlist_url;
            video.addEventListener('loadedmetadata', () => {
                this.videosReady[camera] = true;
            });
        }
    }

    _seekVideos(time, force = false) {
        if (!time || !this.streamInfo) return;

        for (const [camera, info] of Object.entries(this.streamInfo)) {
            if (!info || !info.start_time) continue;

            const video = this.videoElements[camera];
            if (!video || !this.videosReady[camera]) continue;

            const streamStart = new Date(info.start_time).getTime();
            const offsetSeconds = (time.getTime() - streamStart) / 1000;

            if (offsetSeconds >= 0 && video.duration && offsetSeconds <= video.duration) {
                // Only seek if there's significant difference or forced
                const diff = Math.abs(video.currentTime - offsetSeconds);
                if (force || diff > 0.5) {
                    video.currentTime = offsetSeconds;
                    console.log(`Seek ${camera} to ${offsetSeconds.toFixed(1)}s (diff: ${diff.toFixed(1)}s)`);
                }
            }
        }
    }

    _playAll() {
        this.isPlaying = true;

        // Sync position before playing
        const currentTime = window.timeController.getCurrentTime();
        if (currentTime) {
            this._seekVideos(currentTime, true);
        }

        Object.entries(this.videoElements).forEach(([camera, video]) => {
            if (video && this.videosReady[camera]) {
                video.play().catch(e => console.warn(`Play failed ${camera}:`, e.message));
            }
        });

        // Start periodic drift check
        this._startDriftCheck();
    }

    _pauseAll() {
        this.isPlaying = false;
        this._stopDriftCheck();

        Object.values(this.videoElements).forEach(video => {
            if (video) video.pause();
        });
    }

    _startDriftCheck() {
        this._stopDriftCheck();

        // Check every 2 seconds for drift
        this.syncInterval = setInterval(() => {
            if (!this.isPlaying) return;

            const currentTime = window.timeController.getCurrentTime();
            if (!currentTime || !this.streamInfo) return;

            // Check each video for drift
            for (const [camera, info] of Object.entries(this.streamInfo)) {
                if (!info || !info.start_time) continue;

                const video = this.videoElements[camera];
                if (!video || !this.videosReady[camera] || video.paused) continue;

                const streamStart = new Date(info.start_time).getTime();
                const expectedOffset = (currentTime.getTime() - streamStart) / 1000;
                const actualOffset = video.currentTime;
                const drift = Math.abs(expectedOffset - actualOffset);

                // If drift > 1 second, correct it
                if (drift > 1 && expectedOffset >= 0 && expectedOffset <= video.duration) {
                    console.log(`Correcting ${camera} drift: ${drift.toFixed(1)}s`);
                    video.currentTime = expectedOffset;
                }
            }
        }, 2000);
    }

    _stopDriftCheck() {
        if (this.syncInterval) {
            clearInterval(this.syncInterval);
            this.syncInterval = null;
        }
    }

    _setPlaybackRate(rate) {
        Object.values(this.videoElements).forEach(video => {
            if (video) video.playbackRate = rate;
        });
    }

    /**
     * Cleanup
     */
    destroy() {
        this._stopDriftCheck();
        Object.values(this.hlsInstances).forEach(hls => {
            if (hls) hls.destroy();
        });
        this.hlsInstances = { cockpit: null };
    }
}

window.VideoPlayer = VideoPlayer;
