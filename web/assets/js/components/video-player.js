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
        // Listen directly to TimeController events for reliable sync
        window.timeController.addEventListener('play', () => {
            console.log('TimeController play event');
            this._playAll();
        });

        window.timeController.addEventListener('pause', () => {
            console.log('TimeController pause event');
            this._pauseAll();
        });

        // Sync video on any time change (scrubbing, chart clicks, etc.)
        window.timeController.addEventListener('time-change', (e) => {
            if (e.detail.time) {
                this._seekVideos(e.detail.time);
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

            // Check if any streams available (HLS or direct)
            const hasStreams = Object.values(this.streamInfo).some(s => s && (s.playlist_url || s.direct_url));
            if (!hasStreams) {
                this.videoContainer.style.display = 'none';
                return;
            }

            // Use block for compact layout, flex for full layout
            this.videoContainer.style.display = this.videoContainer.classList.contains('video-compact') ? 'block' : 'flex';

            // Initialize video for each stream (HLS or direct)
            for (const [camera, info] of Object.entries(this.streamInfo)) {
                if (info && info.playlist_url) {
                    this._initHLS(camera, info);
                } else if (info && info.direct_url) {
                    this._initDirect(camera, info);
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

    /**
     * Initialize direct video playback (MP4/LRV files)
     * Maps all direct videos to the cockpit video element
     */
    _initDirect(camera, streamInfo) {
        // Use cockpit video element for all direct videos (only one video element available)
        const video = this.videoElements['cockpit'];
        if (!video) return;

        // Map this camera to cockpit element for playback sync
        this.videoElements[camera] = video;
        this.videosReady[camera] = false;

        // Update streamInfo to use cockpit for seeking
        this.streamInfo['cockpit'] = streamInfo;

        video.src = streamInfo.direct_url;
        video.load();

        const onLoaded = () => {
            console.log(`Direct video ready: ${camera}, duration: ${video.duration}s`);
            this.videosReady[camera] = true;
            this.videosReady['cockpit'] = true;

            // Initial seek to current timeline position
            const currentTime = window.timeController?.getCurrentTime();
            if (currentTime && streamInfo.start_time) {
                const streamStart = new Date(streamInfo.start_time).getTime();
                const offsetSeconds = (currentTime.getTime() - streamStart) / 1000;
                if (offsetSeconds >= 0 && offsetSeconds <= video.duration) {
                    video.currentTime = offsetSeconds;
                    console.log(`Initial seek to ${offsetSeconds.toFixed(1)}s`);
                }
            }
            video.removeEventListener('loadedmetadata', onLoaded);
        };

        video.addEventListener('loadedmetadata', onLoaded);

        video.addEventListener('error', (e) => {
            console.error(`Video error ${camera}:`, video.error?.message || 'Unknown error');
        });
    }

    _seekVideos(time, force = false) {
        if (!time || !this.streamInfo) return;

        // Use cockpit video element directly since all videos map to it
        const video = this.videoElements['cockpit'];
        const info = this.streamInfo['cockpit'];

        if (!video || !info || !info.start_time) return;
        if (!this.videosReady['cockpit']) return;

        const streamStart = new Date(info.start_time).getTime();
        const offsetSeconds = (time.getTime() - streamStart) / 1000;

        if (video.duration && offsetSeconds >= 0 && offsetSeconds <= video.duration) {
            // Only seek if difference > 0.5s (avoid micro-seeks during playback)
            const diff = Math.abs(video.currentTime - offsetSeconds);
            if (force || diff > 0.5) {
                video.currentTime = offsetSeconds;
            }
        }
    }

    _playAll() {
        this.isPlaying = true;
        const video = this.videoElements['cockpit'];
        const info = this.streamInfo?.['cockpit'];
        if (!video || !this.videosReady['cockpit'] || !info) return;

        const currentTime = window.timeController.getCurrentTime();
        if (!currentTime) return;

        const videoStart = new Date(info.start_time).getTime();
        const offsetSeconds = (currentTime.getTime() - videoStart) / 1000;

        // If timeline is before video start, don't play video yet
        if (offsetSeconds < 0) {
            console.log(`Timeline before video start, waiting ${(-offsetSeconds).toFixed(0)}s`);
            video.currentTime = 0;
            video.pause();
        } else if (offsetSeconds <= video.duration) {
            // Timeline is within video range - seek and play
            video.currentTime = offsetSeconds;
            video.play().catch(e => console.warn('Video play failed:', e.message));
        } else {
            // Timeline is past video end
            video.currentTime = video.duration;
            video.pause();
        }

        // Start periodic sync check
        this._startDriftCheck();
    }

    _pauseAll() {
        this.isPlaying = false;
        this._stopDriftCheck();

        const video = this.videoElements['cockpit'];
        if (video) video.pause();
    }

    _startDriftCheck() {
        this._stopDriftCheck();

        // Check every 500ms for sync
        this.syncInterval = setInterval(() => {
            if (!this.isPlaying) return;

            const currentTime = window.timeController.getCurrentTime();
            const video = this.videoElements['cockpit'];
            const info = this.streamInfo?.['cockpit'];

            if (!currentTime || !video || !info?.start_time) return;

            const videoStart = new Date(info.start_time).getTime();
            const expectedOffset = (currentTime.getTime() - videoStart) / 1000;

            // Timeline is before video start - keep video paused at 0
            if (expectedOffset < 0) {
                if (!video.paused) {
                    video.pause();
                    video.currentTime = 0;
                }
                return;
            }

            // Timeline is past video end - pause at end
            if (expectedOffset > video.duration) {
                if (!video.paused) {
                    video.pause();
                    video.currentTime = video.duration;
                }
                return;
            }

            // Timeline is within video range
            // Start video if it was waiting
            if (video.paused) {
                video.currentTime = expectedOffset;
                video.play().catch(e => console.warn('Video play failed:', e.message));
                return;
            }

            // Check for drift and correct
            const actualOffset = video.currentTime;
            const drift = Math.abs(expectedOffset - actualOffset);
            if (drift > 1) {
                console.log(`Correcting drift: ${drift.toFixed(1)}s`);
                video.currentTime = expectedOffset;
            }
        }, 500);
    }

    _stopDriftCheck() {
        if (this.syncInterval) {
            clearInterval(this.syncInterval);
            this.syncInterval = null;
        }
    }

    _setPlaybackRate(rate) {
        const video = this.videoElements['cockpit'];
        if (video) video.playbackRate = rate;
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
