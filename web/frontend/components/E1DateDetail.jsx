import React, { useState, useEffect, useMemo } from "react";
import { useParams, Link } from "react-router-dom";
import { API_URL } from "../src/config";
import { utcToBoston, formatBostonTimeRange, getBostonTimezoneAbbr } from "../src/timeUtils";

// Get file type from filename
function getFileType(filename) {
  if (filename.includes("_nav.csv")) return "nav";
  if (filename.includes("_imu.csv")) return "imu";
  if (filename.includes("_wind.csv")) return "wind";
  if (filename.includes(".rtcm3")) return "rtcm3";
  return "other";
}

// Extract session identifier from filename (e.g., "s001" from "E1_s001_000061_nav.csv")
function extractSessionFromFilename(filename) {
  const parts = filename.replace(/\.(csv|rtcm3)$/, "").split("_");
  for (const part of parts) {
    if (part.match(/^(s\d+|boot\d+)$/)) {
      return part;
    }
  }
  return null;
}

const FILE_TYPE_COLORS = {
  nav: "var(--accent)",
  imu: "var(--success)",
  wind: "var(--warning)",
  rtcm3: "var(--danger)",
  other: "var(--border)",
};

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// Format UTC ISO timestamp to HH:MM:SS
function formatUtcTime(isoTimestamp) {
  if (!isoTimestamp) return null;
  try {
    const dt = new Date(isoTimestamp);
    return dt.toISOString().slice(11, 19);
  } catch {
    return null;
  }
}

export default function E1DateDetail() {
  const { deviceId, date } = useParams();
  const [files, setFiles] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Fetch both file list and sessions data
  useEffect(() => {
    setLoading(true);

    Promise.all([
      // Fetch raw files
      fetch(`${API_URL}/api/e1/devices/${deviceId}/uploads`)
        .then((r) => {
          if (!r.ok) throw new Error("Failed to load files");
          return r.json();
        }),
      // Fetch processed sessions (has correct times from GPS data)
      fetch(`${API_URL}/api/sessions`)
        .then((r) => {
          if (!r.ok) throw new Error("Failed to load sessions");
          return r.json();
        }),
    ])
      .then(([uploadsData, sessionsData]) => {
        // Get files for this date
        const upload = uploadsData.uploads?.find((u) => u.date === date);
        setFiles(upload?.raw_files || []);

        // Filter sessions for this device and date
        const deviceSessions = (sessionsData.sessions || []).filter(
          (s) => s.device_id === deviceId && s.date === date
        );
        setSessions(deviceSessions);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [deviceId, date]);

  // Group files by session and merge with session metadata
  const { sessionsWithTime, sessionsWithoutTime } = useMemo(() => {
    // Group files by session identifier extracted from filename
    const filesBySession = {};

    for (const file of files) {
      const sessionPart = extractSessionFromFilename(file.filename);
      const key = sessionPart || "unknown";

      if (!filesBySession[key]) {
        filesBySession[key] = {
          files: [],
          totalSize: 0,
          fileTypes: new Set(),
        };
      }

      filesBySession[key].files.push(file);
      filesBySession[key].totalSize += file.size_bytes || 0;
      filesBySession[key].fileTypes.add(getFileType(file.filename));
    }

    // Match with session metadata (which has correct times from GPS data)
    const withTime = [];
    const withoutTime = [];

    for (const session of sessions) {
      // Session ID in API might be "s001" or "s001-000061" depending on processing
      // Extract the base session part (e.g., "s001" from "s001-000061")
      const sessionIdParts = (session.session_id || "").split("-");
      const baseSessionId = sessionIdParts[0];

      // Find matching files
      const fileGroup = filesBySession[baseSessionId] || filesBySession[session.session_id];

      if (fileGroup) {
        const sessionData = {
          sessionId: session.session_id,
          displayName: baseSessionId,
          startTime: session.start_time,
          endTime: session.end_time,
          startTimeBoston: utcToBoston(session.start_time),
          endTimeBoston: utcToBoston(session.end_time),
          startTimeUtc: formatUtcTime(session.start_time),
          endTimeUtc: formatUtcTime(session.end_time),
          timezoneAbbr: getBostonTimezoneAbbr(session.start_time),
          durationMinutes: session.duration_minutes,
          files: fileGroup.files,
          totalSize: fileGroup.totalSize,
          fileTypes: Array.from(fileGroup.fileTypes),
          sensors: session.sensors || {},
        };

        if (session.start_time) {
          withTime.push(sessionData);
        } else {
          withoutTime.push(sessionData);
        }

        // Mark as used
        delete filesBySession[baseSessionId];
        delete filesBySession[session.session_id];
      }
    }

    // Add any remaining file groups that didn't match a session (no GPS time)
    for (const [key, fileGroup] of Object.entries(filesBySession)) {
      withoutTime.push({
        sessionId: key,
        displayName: key,
        startTime: null,
        endTime: null,
        startTimeBoston: null,
        endTimeBoston: null,
        startTimeUtc: null,
        endTimeUtc: null,
        timezoneAbbr: null,
        durationMinutes: null,
        files: fileGroup.files,
        totalSize: fileGroup.totalSize,
        fileTypes: Array.from(fileGroup.fileTypes),
        sensors: {},
      });
    }

    // Sort sessions with time by start time (earliest first)
    withTime.sort((a, b) => a.startTime.localeCompare(b.startTime));

    // Sort sessions without time by session ID
    withoutTime.sort((a, b) => (a.sessionId || "").localeCompare(b.sessionId || ""));

    return { sessionsWithTime: withTime, sessionsWithoutTime: withoutTime };
  }, [files, sessions]);

  const handleViewInDashboard = (sessionId) => {
    // Open main dashboard with this session selected
    // Session path format: deviceId/date-sessionId (e.g., E1/2026-04-05-s001)
    const sessionPath = sessionId && sessionId !== "unknown" ? `${date}-${sessionId}` : date;
    window.open(`/?session=${deviceId}/${sessionPath}`, "_blank");
  };

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <Link to="/e1" style={{ color: "var(--text-secondary)", textDecoration: "none" }}>
            E1 Fleet
          </Link>
          <span style={{ color: "var(--text-secondary)" }}> / </span>
          <Link
            to={`/e1/${deviceId}`}
            style={{ color: "var(--text-secondary)", textDecoration: "none" }}
          >
            {deviceId}
          </Link>
          <span style={{ color: "var(--text-secondary)" }}> / </span>
          <h2 style={{ display: "inline" }}>{date}</h2>
        </div>
      </div>

      {loading ? (
        <div className="loading">Loading...</div>
      ) : error ? (
        <div style={{ color: "var(--danger)" }}>Error: {error}</div>
      ) : sessionsWithTime.length === 0 && sessionsWithoutTime.length === 0 ? (
        <div style={{ color: "var(--text-secondary)", padding: 20 }}>
          No sessions found for this date.
        </div>
      ) : (
        <>
          {/* Sessions with GPS time - sorted by local time */}
          {sessionsWithTime.length > 0 && (
            <div className="sessions-list">
              {sessionsWithTime.map((session) => (
                <div key={session.sessionId} className="session-card">
                  <div className="session-header">
                    <div className="session-id">
                      <span className="session-label">Session</span>
                      <span className="session-name">{session.displayName}</span>
                    </div>
                    <div className="session-time">
                      <span className="time-local">
                        {session.startTimeBoston} — {session.endTimeBoston} {session.timezoneAbbr}
                      </span>
                      <span className="time-utc">
                        ({session.startTimeUtc} — {session.endTimeUtc} UTC)
                        {session.durationMinutes && ` · ${session.durationMinutes} min`}
                      </span>
                    </div>
                  </div>

                  <div className="session-details">
                    <div className="session-files">
                      {session.fileTypes.map((type) => (
                        <span
                          key={type}
                          className="file-type-badge"
                          style={{
                            background: FILE_TYPE_COLORS[type] || FILE_TYPE_COLORS.other,
                            color: type === "wind" ? "#000" : "#fff",
                          }}
                        >
                          {type.toUpperCase()}
                        </span>
                      ))}
                      <span className="file-count">
                        {session.files.length} file{session.files.length !== 1 ? "s" : ""}
                      </span>
                      <span className="file-size">{formatBytes(session.totalSize)}</span>
                    </div>
                    <button
                      onClick={() => handleViewInDashboard(session.sessionId)}
                      style={{ fontSize: 12, padding: "6px 12px" }}
                    >
                      View in Dashboard
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Sessions without GPS time - shown in separate section below */}
          {sessionsWithoutTime.length > 0 && (
            <>
              <div style={{
                marginTop: sessionsWithTime.length > 0 ? 24 : 0,
                marginBottom: 12,
                fontSize: 13,
                color: "var(--text-secondary)",
                textTransform: "uppercase",
                letterSpacing: "0.5px"
              }}>
                No GPS Time Available
              </div>
              <div className="sessions-list">
                {sessionsWithoutTime.map((session) => (
                  <div key={session.sessionId} className="session-card" style={{ opacity: 0.7 }}>
                    <div className="session-header">
                      <div className="session-id">
                        <span className="session-label">Session</span>
                        <span className="session-name">{session.displayName}</span>
                      </div>
                      <div className="session-time">
                        <span className="time-utc">Not yet processed</span>
                      </div>
                    </div>

                    <div className="session-details">
                      <div className="session-files">
                        {session.fileTypes.map((type) => (
                          <span
                            key={type}
                            className="file-type-badge"
                            style={{
                              background: FILE_TYPE_COLORS[type] || FILE_TYPE_COLORS.other,
                              color: type === "wind" ? "#000" : "#fff",
                            }}
                          >
                            {type.toUpperCase()}
                          </span>
                        ))}
                        <span className="file-count">
                          {session.files.length} file{session.files.length !== 1 ? "s" : ""}
                        </span>
                        <span className="file-size">{formatBytes(session.totalSize)}</span>
                      </div>
                      <button
                        onClick={() => handleViewInDashboard(session.sessionId)}
                        style={{ fontSize: 12, padding: "6px 12px" }}
                      >
                        View in Dashboard
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
