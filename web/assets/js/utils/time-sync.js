/**
 * TimeController - Central time synchronization for all components
 * Dispatches 'time-change' events when playback position changes
 */
class TimeController extends EventTarget {
    constructor() {
        super();
        this._currentTime = null;
        this._startTime = null;
        this._endTime = null;
        this._originalStartTime = null;
        this._originalEndTime = null;
        this._playbackRate = 1.0;
        this._isPlaying = false;
        this._lastTick = null;
        this._animationId = null;
    }

    /**
     * Set the session time bounds
     * @param {string} startTime - Session start time
     * @param {string} endTime - Session end time
     * @param {object} trim - Optional trim bounds { start, end }
     */
    setSession(startTime, endTime, trim = null) {
        this._originalStartTime = new Date(startTime);
        this._originalEndTime = new Date(endTime);

        // Apply trim if present
        if (trim && trim.start) {
            this._startTime = new Date(trim.start);
        } else {
            this._startTime = new Date(this._originalStartTime);
        }

        if (trim && trim.end) {
            this._endTime = new Date(trim.end);
        } else {
            this._endTime = new Date(this._originalEndTime);
        }

        this._currentTime = new Date(this._startTime);

        this.dispatchEvent(new CustomEvent('session-loaded', {
            detail: {
                start: this._startTime,
                end: this._endTime,
                duration: this._endTime - this._startTime,
                isTrimmed: this.isTrimmed()
            }
        }));

        this._notifyTimeChange();
    }

    /**
     * Seek to a specific timestamp
     */
    seek(timestamp) {
        const time = timestamp instanceof Date ? timestamp : new Date(timestamp);

        // Clamp to session bounds
        if (this._startTime && time < this._startTime) {
            this._currentTime = new Date(this._startTime);
        } else if (this._endTime && time > this._endTime) {
            this._currentTime = new Date(this._endTime);
        } else {
            this._currentTime = time;
        }

        this._notifyTimeChange();
    }

    /**
     * Seek to a position (0-1)
     */
    seekToPosition(position) {
        if (!this._startTime || !this._endTime) return;

        const duration = this._endTime - this._startTime;
        const offset = duration * Math.max(0, Math.min(1, position));
        this.seek(new Date(this._startTime.getTime() + offset));
    }

    /**
     * Get current position as 0-1
     */
    getPosition() {
        if (!this._startTime || !this._endTime || !this._currentTime) return 0;
        const duration = this._endTime - this._startTime;
        if (duration <= 0) return 0;
        return (this._currentTime - this._startTime) / duration;
    }

    /**
     * Start playback
     */
    play() {
        if (this._isPlaying) return;
        this._isPlaying = true;
        this._lastTick = performance.now();

        this.dispatchEvent(new CustomEvent('play'));
        this._tick();
    }

    /**
     * Pause playback
     */
    pause() {
        this._isPlaying = false;
        if (this._animationId) {
            cancelAnimationFrame(this._animationId);
            this._animationId = null;
        }
        this.dispatchEvent(new CustomEvent('pause'));
    }

    /**
     * Toggle play/pause
     */
    toggle() {
        if (this._isPlaying) {
            this.pause();
        } else {
            this.play();
        }
    }

    /**
     * Set playback speed multiplier
     */
    setPlaybackRate(rate) {
        this._playbackRate = rate;
    }

    /**
     * Get current time
     */
    getCurrentTime() {
        return this._currentTime;
    }

    /**
     * Get session duration in milliseconds
     */
    getDuration() {
        if (!this._startTime || !this._endTime) return 0;
        return this._endTime - this._startTime;
    }

    /**
     * Get elapsed time in milliseconds
     */
    getElapsed() {
        if (!this._startTime || !this._currentTime) return 0;
        return this._currentTime - this._startTime;
    }

    /**
     * Check if playing
     */
    isPlaying() {
        return this._isPlaying;
    }

    /**
     * Animation loop
     */
    _tick() {
        if (!this._isPlaying) return;

        const now = performance.now();
        const delta = (now - this._lastTick) * this._playbackRate;
        this._lastTick = now;

        // Advance time
        this._currentTime = new Date(this._currentTime.getTime() + delta);

        // Check if reached end
        if (this._currentTime >= this._endTime) {
            this._currentTime = new Date(this._endTime);
            this.pause();
            this._notifyTimeChange();
            return;
        }

        this._notifyTimeChange();
        this._animationId = requestAnimationFrame(() => this._tick());
    }

    /**
     * Dispatch time change event
     */
    _notifyTimeChange() {
        this.dispatchEvent(new CustomEvent('time-change', {
            detail: {
                time: this._currentTime,
                position: this.getPosition(),
                elapsed: this.getElapsed()
            }
        }));
    }

    /**
     * Set trim bounds (updates effective start/end without changing original)
     * @param {Date|string|null} trimStart - New start time (null to keep current)
     * @param {Date|string|null} trimEnd - New end time (null to keep current)
     */
    setTrim(trimStart, trimEnd) {
        if (trimStart !== null) {
            this._startTime = new Date(trimStart);
        }
        if (trimEnd !== null) {
            this._endTime = new Date(trimEnd);
        }

        // Reset current time to new start
        this._currentTime = new Date(this._startTime);

        this.dispatchEvent(new CustomEvent('trim-change', {
            detail: {
                start: this._startTime,
                end: this._endTime,
                duration: this._endTime - this._startTime,
                isTrimmed: this.isTrimmed()
            }
        }));

        this._notifyTimeChange();
    }

    /**
     * Reset trim to original session bounds
     */
    resetTrim() {
        if (!this._originalStartTime || !this._originalEndTime) return;

        this._startTime = new Date(this._originalStartTime);
        this._endTime = new Date(this._originalEndTime);
        this._currentTime = new Date(this._startTime);

        this.dispatchEvent(new CustomEvent('trim-change', {
            detail: {
                start: this._startTime,
                end: this._endTime,
                duration: this._endTime - this._startTime,
                isTrimmed: false
            }
        }));

        this._notifyTimeChange();
    }

    /**
     * Check if session is currently trimmed
     */
    isTrimmed() {
        if (!this._originalStartTime || !this._originalEndTime) return false;
        return (
            this._startTime.getTime() !== this._originalStartTime.getTime() ||
            this._endTime.getTime() !== this._originalEndTime.getTime()
        );
    }

    /**
     * Get current trim bounds for saving
     */
    getTrimBounds() {
        return {
            start: this._startTime?.toISOString(),
            end: this._endTime?.toISOString(),
            originalStart: this._originalStartTime?.toISOString(),
            originalEnd: this._originalEndTime?.toISOString()
        };
    }

    /**
     * Get original (untrimmed) session bounds
     */
    getOriginalBounds() {
        return {
            start: this._originalStartTime,
            end: this._originalEndTime
        };
    }
}

// Global singleton
window.timeController = new TimeController();
