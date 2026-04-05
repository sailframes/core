/**
 * Time conversion utilities for SailFrames
 * All display times are in Boston local time (America/New_York)
 */

const BOSTON_TIMEZONE = "America/New_York";

/**
 * Convert UTC ISO timestamp to Boston local time string
 * @param {string} utcTimestamp - ISO format UTC timestamp (e.g., "2026-04-05T18:34:17Z")
 * @param {object} options - Formatting options
 * @returns {string} Formatted local time string
 */
export function utcToBoston(utcTimestamp, options = {}) {
  if (!utcTimestamp) return null;

  try {
    const dt = new Date(utcTimestamp);
    if (isNaN(dt.getTime())) return null;

    const defaultOptions = {
      timeZone: BOSTON_TIMEZONE,
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
    };

    return dt.toLocaleTimeString("en-US", { ...defaultOptions, ...options });
  } catch {
    return null;
  }
}

/**
 * Convert UTC ISO timestamp to Boston local date string
 * @param {string} utcTimestamp - ISO format UTC timestamp
 * @returns {string} Formatted local date (e.g., "Apr 5, 2026")
 */
export function utcToBostonDate(utcTimestamp) {
  if (!utcTimestamp) return null;

  try {
    const dt = new Date(utcTimestamp);
    if (isNaN(dt.getTime())) return null;

    return dt.toLocaleDateString("en-US", {
      timeZone: BOSTON_TIMEZONE,
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  } catch {
    return null;
  }
}

/**
 * Convert UTC ISO timestamp to Boston local date and time string
 * @param {string} utcTimestamp - ISO format UTC timestamp
 * @returns {string} Formatted local date and time (e.g., "Apr 5, 2026 2:34 PM")
 */
export function utcToBostonDateTime(utcTimestamp) {
  if (!utcTimestamp) return null;

  try {
    const dt = new Date(utcTimestamp);
    if (isNaN(dt.getTime())) return null;

    return dt.toLocaleString("en-US", {
      timeZone: BOSTON_TIMEZONE,
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
    });
  } catch {
    return null;
  }
}

/**
 * Convert HHMMSS time string with date to Boston local time
 * @param {string} timeStr - Time in HHMMSS format (e.g., "183417")
 * @param {string} dateStr - Date in YYYY-MM-DD format (e.g., "2026-04-05")
 * @returns {string} Formatted local time (e.g., "2:34 PM")
 */
export function utcHHMMSSToBoston(timeStr, dateStr) {
  if (!timeStr || timeStr.length !== 6 || !dateStr) return null;

  try {
    const hh = timeStr.slice(0, 2);
    const mm = timeStr.slice(2, 4);
    const ss = timeStr.slice(4, 6);
    const utcTimestamp = `${dateStr}T${hh}:${mm}:${ss}Z`;
    return utcToBoston(utcTimestamp);
  } catch {
    return null;
  }
}

/**
 * Format a time range in Boston local time
 * @param {string} startUtc - Start time ISO UTC timestamp
 * @param {string} endUtc - End time ISO UTC timestamp
 * @returns {string} Formatted time range (e.g., "2:34 PM — 3:09 PM")
 */
export function formatBostonTimeRange(startUtc, endUtc) {
  const start = utcToBoston(startUtc);
  const end = utcToBoston(endUtc);

  if (!start && !end) return null;
  if (!start) return end;
  if (!end) return start;

  return `${start} — ${end}`;
}

/**
 * Get Boston timezone abbreviation for a given date
 * @param {string} utcTimestamp - ISO format UTC timestamp
 * @returns {string} Timezone abbreviation (e.g., "EDT" or "EST")
 */
export function getBostonTimezoneAbbr(utcTimestamp) {
  if (!utcTimestamp) return "ET";

  try {
    const dt = new Date(utcTimestamp);
    const parts = dt.toLocaleTimeString("en-US", {
      timeZone: BOSTON_TIMEZONE,
      timeZoneName: "short",
    }).split(" ");
    return parts[parts.length - 1] || "ET";
  } catch {
    return "ET";
  }
}
