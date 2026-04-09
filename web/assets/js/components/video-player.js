/**
 * VideoPlayer - HLS video playback synchronized with timeline
 * Videos sync on play, seek, and periodically during playback to prevent drift
 * Supports multiple video chapters (GoPro splits recordings every ~17 min)
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
        this.videoChapters = [];
        this.currentChapterIndex = -1;
        this.videosReady = { cockpit: false };
        this.syncInterval = null;
        this.isPlaying = false;
        this._initialLoadComplete = false;  // Don't auto-sync until first chapter loaded

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
                this._syncToTimeline(e.detail.time);
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
            const streams = data.streams || {};

            // Convert to array and sort by chapter number
            this.videoChapters = Object.entries(streams)
                .map(([key, info]) => ({ key, ...info }))
                .filter(v => v.direct_url || v.playlist_url)
                .sort((a, b) => (a.chapter || 0) - (b.chapter || 0));

            if (this.videoChapters.length === 0) {
                this.videoContainer.style.display = 'none';
                // Clear video coverage indicator
                if (window.timeline) {
                    window.timeline.setVideoCoverage([]);
                }
                return;
            }

            // Parse start/end times for each chapter
            this.videoChapters.forEach(ch => {
                if (ch.start_time) ch._startMs = new Date(ch.start_time).getTime();
                if (ch.end_time) ch._endMs = new Date(ch.end_time).getTime();
            });

            const totalDuration = this.videoChapters.reduce((sum, ch) => sum + (ch.duration_seconds || 0), 0);
            const isProxy = this.videoChapters[0]?.is_proxy;
            console.log(`Video: ${isProxy ? 'proxy' : this.videoChapters.length + ' chapters'}, ${(totalDuration / 60).toFixed(1)} min`);

            // Notify timeline of video coverage
            if (window.timeline && this.videoChapters.length > 0) {
                const videos = this.videoChapters.map(ch => ({
                    start_time: ch.start_time,
                    end_time: ch.end_time || (ch.start_time && ch.duration_seconds
                        ? new Date(new Date(ch.start_time).getTime() + ch.duration_seconds * 1000).toISOString()
                        : null)
                }));
                window.timeline.setVideoCoverage(videos);
            }

            // Use block for compact layout, flex for full layout
            this.videoContainer.style.display = this.videoContainer.classList.contains('video-compact') ? 'block' : 'flex';

            // Load the video
            this._loadChapter(0);
        } catch (error) {
            console.error('Error loading video streams:', error);
            this.videoContainer.style.display = 'none';
            // Clear video coverage indicator on error
            if (window.timeline) {
                window.timeline.setVideoCoverage([]);
            }
        }
    }

    /**
     * Find which chapter covers a given timeline time
     * Returns chapter index or -1 if no chapter covers this time
     */
    _findChapterForTime(time) {
        if (!time || this.videoChapters.length === 0) return -1;

        const timeMs = time.getTime();

        for (let i = 0; i < this.videoChapters.length; i++) {
            const ch = this.videoChapters[i];
            if (ch._startMs && ch._endMs) {
                // Use precise start/end times
                if (timeMs >= ch._startMs && timeMs < ch._endMs) {
                    return i;
                }
            } else if (ch._startMs && ch.duration_seconds) {
                // Calculate end from duration
                const endMs = ch._startMs + ch.duration_seconds * 1000;
                if (timeMs >= ch._startMs && timeMs < endMs) {
                    return i;
                }
            }
        }

        // Check if time is before first chapter or after last chapter
        const firstCh = this.videoChapters[0];
        const lastCh = this.videoChapters[this.videoChapters.length - 1];

        if (firstCh._startMs && timeMs < firstCh._startMs) {
            return -1; // Before video starts
        }

        if (lastCh._endMs && timeMs >= lastCh._endMs) {
            return -2; // After video ends
        }

        return -1;
    }

    /**
     * Calculate video position within a chapter for a given timeline time
     */
    _calculatePositionInChapter(time, chapter) {
        if (!time || !chapter || !chapter._startMs) return null;
        return (time.getTime() - chapter._startMs) / 1000;
    }

    /**
     * Load a specific chapter by index
     */
    _loadChapter(index, seekPosition = 0) {
        if (index < 0 || index >= this.videoChapters.length) return;

        // Don't reload if already on this chapter
        if (index === this.currentChapterIndex && this.videosReady['cockpit']) return;

        const chapter = this.videoChapters[index];
        console.log(`Loading video: ${chapter.filename || chapter.key}`);

        this.currentChapterIndex = index;

        if (chapter.playlist_url) {
            this._initHLS('cockpit', chapter, seekPosition);
        } else if (chapter.direct_url) {
            this._initDirect('cockpit', chapter, seekPosition);
        }
    }

    _initHLS(camera, streamInfo, initialSeek = 0) {
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
                maxBufferLength: 60,
                maxMaxBufferLength: 120,
                maxBufferSize: 120 * 1000 * 1000,
                fragLoadingTimeOut: 20000,
                manifestLoadingTimeOut: 10000,
                levelLoadingTimeOut: 10000
            });

            hls.loadSource(streamInfo.playlist_url);
            hls.attachMedia(video);

            hls.on(Hls.Events.MANIFEST_PARSED, () => {
                console.log(`HLS ready: ${camera}, duration: ${video.duration}s`);
                this.videosReady[camera] = true;
                this._initialLoadComplete = true;  // Allow sync after first video loaded

                if (initialSeek > 0 && initialSeek <= video.duration) {
                    video.currentTime = initialSeek;
                }

                if (this.isPlaying) {
                    video.play().catch(e => console.warn('Video play failed:', e.message));
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
                this._initialLoadComplete = true;
                if (initialSeek > 0) video.currentTime = initialSeek;
                if (this.isPlaying) video.play().catch(() => {});
            }, { once: true });
        }
    }

    /**
     * Initialize direct video playback (MP4/LRV files)
     */
    _initDirect(camera, streamInfo, initialSeek = 0) {
        const video = this.videoElements['cockpit'];
        if (!video) return;

        this.videosReady[camera] = false;
        this.videosReady['cockpit'] = false;

        video.src = streamInfo.direct_url;
        video.load();

        const onLoaded = () => {
            console.log(`Direct video ready: ${camera}, duration: ${video.duration}s`);
            this.videosReady[camera] = true;
            this.videosReady['cockpit'] = true;
            this._initialLoadComplete = true;  // Allow sync after first video loaded

            if (initialSeek > 0 && initialSeek <= video.duration) {
                video.currentTime = initialSeek;
                console.log(`Initial seek to ${initialSeek.toFixed(1)}s`);
            }

            if (this.isPlaying) {
                video.play().catch(e => console.warn('Video play failed:', e.message));
            }

            video.removeEventListener('loadedmetadata', onLoaded);
        };

        video.addEventListener('loadedmetadata', onLoaded);

        video.addEventListener('error', (e) => {
            console.error(`Video error ${camera}:`, video.error?.message || 'Unknown error');
        }, { once: true });
    }

    /**
     * Sync video to timeline position - handles chapter switching
     */
    _syncToTimeline(time, force = false) {
        if (!time || this.videoChapters.length === 0) return;

        // Don't auto-sync until first chapter is loaded (prevents jumping on page load)
        if (!this._initialLoadComplete && !force) return;

        const chapterIndex = this._findChapterForTime(time);
        const video = this.videoElements['cockpit'];

        // Timeline is before first video - show first frame, paused
        if (chapterIndex === -1) {
            // Load first chapter if not loaded
            if (this.currentChapterIndex !== 0) {
                this._loadChapter(0, 0);
            } else if (video) {
                video.pause();
                video.currentTime = 0;
            }
            return;
        }

        // Timeline is after last video
        if (chapterIndex === -2) {
            if (video) {
                video.pause();
                if (video.duration) video.currentTime = video.duration;
            }
            return;
        }

        const chapter = this.videoChapters[chapterIndex];
        const positionInChapter = this._calculatePositionInChapter(time, chapter);

        if (positionInChapter === null || positionInChapter < 0) return;

        // Need to switch chapters?
        if (chapterIndex !== this.currentChapterIndex) {
            console.log(`Switching from chapter ${this.currentChapterIndex} to ${chapterIndex}`);
            this._loadChapter(chapterIndex, positionInChapter);
            return;
        }

        // Same chapter - just seek if needed
        if (!video || !this.videosReady['cockpit']) return;

        const diff = Math.abs(video.currentTime - positionInChapter);
        if (force || diff > 0.5) {
            video.currentTime = positionInChapter;
        }
    }

    _playAll() {
        this.isPlaying = true;

        const currentTime = window.timeController.getCurrentTime();
        if (!currentTime) return;

        // Sync and potentially switch chapters
        this._syncToTimeline(currentTime, true);

        const video = this.videoElements['cockpit'];
        if (video && this.videosReady['cockpit']) {
            video.play().catch(e => console.warn('Video play failed:', e.message));
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

        // Check every 500ms for sync and chapter transitions
        this.syncInterval = setInterval(() => {
            if (!this.isPlaying) return;

            const currentTime = window.timeController.getCurrentTime();
            if (!currentTime) return;

            const chapterIndex = this._findChapterForTime(currentTime);
            const video = this.videoElements['cockpit'];

            // Timeline is outside video range
            if (chapterIndex < 0) {
                if (video && !video.paused) {
                    video.pause();
                }
                return;
            }

            // Need to switch chapters?
            if (chapterIndex !== this.currentChapterIndex) {
                const chapter = this.videoChapters[chapterIndex];
                const positionInChapter = this._calculatePositionInChapter(currentTime, chapter);
                console.log(`Auto-switching to chapter ${chapterIndex}`);
                this._loadChapter(chapterIndex, positionInChapter || 0);
                return;
            }

            // Check drift within current chapter
            if (!video || !this.videosReady['cockpit']) return;

            const chapter = this.videoChapters[this.currentChapterIndex];
            const expectedPosition = this._calculatePositionInChapter(currentTime, chapter);

            if (expectedPosition === null) return;

            // Resume paused video if within range
            if (video.paused && expectedPosition >= 0 && expectedPosition <= video.duration) {
                video.currentTime = expectedPosition;
                video.play().catch(e => console.warn('Video play failed:', e.message));
                return;
            }

            // Correct drift
            const drift = Math.abs(expectedPosition - video.currentTime);
            if (drift > 1) {
                console.log(`Correcting drift: ${drift.toFixed(1)}s`);
                video.currentTime = expectedPosition;
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
