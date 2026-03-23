/**
 * Video <-> data synchronization utilities.
 *
 * Manages time synchronization between video playback,
 * map position, and chart cursors across the dashboard.
 */

/**
 * Simple event emitter for time synchronization across components.
 */
class TimeSyncBus {
  constructor() {
    this.listeners = new Map();
    this.currentTime = 0;
    this.isPlaying = false;
    this.playbackRate = 1.0;
  }

  /**
   * Subscribe to time changes.
   * @param {string} id - Unique subscriber ID.
   * @param {function} callback - Called with (time, source) on time change.
   * @returns {function} Unsubscribe function.
   */
  subscribe(id, callback) {
    this.listeners.set(id, callback);
    return () => this.listeners.delete(id);
  }

  /**
   * Broadcast a time change to all subscribers except the source.
   * @param {number} time - Unix timestamp.
   * @param {string} source - ID of the component that triggered the change.
   */
  seek(time, source) {
    this.currentTime = time;
    for (const [id, callback] of this.listeners) {
      if (id !== source) {
        callback(time, source);
      }
    }
  }

  /**
   * Start synchronized playback.
   */
  play(source) {
    this.isPlaying = true;
    for (const [id, callback] of this.listeners) {
      if (id !== source) {
        callback(this.currentTime, source, "play");
      }
    }
  }

  /**
   * Pause synchronized playback.
   */
  pause(source) {
    this.isPlaying = false;
    for (const [id, callback] of this.listeners) {
      if (id !== source) {
        callback(this.currentTime, source, "pause");
      }
    }
  }
}

// Singleton instance
export const syncBus = new TimeSyncBus();

/**
 * Find the data point closest to a given timestamp.
 * @param {Array} data - Array of objects with `timestamp` field.
 * @param {number} time - Target timestamp.
 * @returns {Object|null} Closest data point.
 */
export function findClosestPoint(data, time) {
  if (!data?.length) return null;

  // Binary search for efficiency with large datasets
  let lo = 0;
  let hi = data.length - 1;

  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (data[mid].timestamp < time) {
      lo = mid + 1;
    } else {
      hi = mid;
    }
  }

  // Check neighbors for closest
  if (lo > 0) {
    const prev = data[lo - 1];
    const curr = data[lo];
    if (Math.abs(prev.timestamp - time) < Math.abs(curr.timestamp - time)) {
      return prev;
    }
  }

  return data[lo];
}

/**
 * Interpolate between two data points at a given timestamp.
 * @param {Array} data - Sorted array with `timestamp` field.
 * @param {number} time - Target timestamp.
 * @param {Array<string>} fields - Fields to interpolate.
 * @returns {Object} Interpolated values.
 */
export function interpolateAt(data, time, fields) {
  if (!data?.length) return null;

  // Find bracketing points
  let i = 0;
  while (i < data.length - 1 && data[i + 1].timestamp <= time) {
    i++;
  }

  if (i >= data.length - 1) return data[data.length - 1];

  const a = data[i];
  const b = data[i + 1];
  const t = (time - a.timestamp) / (b.timestamp - a.timestamp);

  const result = { timestamp: time };
  for (const field of fields) {
    if (field in a && field in b) {
      result[field] = a[field] + (b[field] - a[field]) * t;
    }
  }

  return result;
}

/**
 * Compute video time offset from session start time and video start time.
 * Handles clock drift between Pi system clock and GPS time.
 */
export function videoTimeOffset(sessionStartTime, videoStartTime) {
  return videoStartTime - sessionStartTime;
}
