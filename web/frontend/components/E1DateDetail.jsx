import React, { useState, useEffect, useMemo } from "react";
import { useParams, Link } from "react-router-dom";
import { API_URL } from "../src/config";
import { utcHHMMSSToBoston, getBostonTimezoneAbbr } from "../src/timeUtils";

// Extract session info from E1 filename
// Patterns: E1_s001_000061_nav.csv, E1_boot17_122131_nav.csv
// Returns combined sessionId (e.g., "boot17-122131") and separate timeStr
function parseFilename(filename) {
  const parts = filename.replace(/\.(csv|rtcm3)$/, "").split("_");

  let sessionPart = null;
  let timeStr = null;

  for (let i = 0; i < parts.length; i++) {
    const part = parts[i];
    // Session ID: s001, s002, boot17, etc.
    if (part.match(/^(s\d+|boot\d+)$/)) {
      sessionPart = part;
    }
    // Time: 6 digits like 122131 (HHMMSS)
    if (part.match(/^\d{6}$/) && parseInt(part.slice(0, 2)) < 24) {
      timeStr = part;
    }
  }

  // Combined session ID includes both boot/session part and time
  // This distinguishes multiple recording sessions on the same boot
  const sessionId = sessionPart && timeStr ? `${sessionPart}-${timeStr}` : sessionPart;

  return { sessionId, timeStr };
}

// Convert HHMMSS to readable UTC time
function formatTimeUtc(timeStr) {
  if (!timeStr || timeStr.length !== 6) return null;
  const hh = timeStr.slice(0, 2);
  const mm = timeStr.slice(2, 4);
  const ss = timeStr.slice(4, 6);
  return `${hh}:${mm}:${ss}`;
}

// Get file type from filename
function getFileType(filename) {
  if (filename.includes("_nav.csv")) return "nav";
  if (filename.includes("_imu.csv")) return "imu";
  if (filename.includes("_wind.csv")) return "wind";
  if (filename.includes(".rtcm3")) return "rtcm3";
  return "other";
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

export default function E1DateDetail() {
  const { deviceId, date } = useParams();
  const [files, setFiles] = useState({ raw: [], processed: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Fetch file list
  useEffect(() => {
    setLoading(true);
    fetch(`${API_URL}/api/e1/devices/${deviceId}/uploads`)
      .then((r) => {
        if (!r.ok) throw new Error("Failed to load files");
        return r.json();
      })
      .then((data) => {
        const upload = data.uploads?.find((u) => u.date === date);
        if (upload) {
          setFiles({ raw: upload.raw_files || [], processed: upload.processed_files || [] });
        }
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [deviceId, date]);

  // Group files by session
  const sessions = useMemo(() => {
    const groups = {};

    for (const file of files.raw) {
      const { sessionId, timeStr } = parseFilename(file.filename);
      const key = sessionId || "unknown";

      if (!groups[key]) {
        groups[key] = {
          sessionId: key,
          files: [],
          times: new Set(),
          totalSize: 0,
          fileTypes: new Set(),
        };
      }

      groups[key].files.push(file);
      if (timeStr) groups[key].times.add(timeStr);
      groups[key].totalSize += file.size_bytes || 0;
      groups[key].fileTypes.add(getFileType(file.filename));
    }

    // Convert to array and compute time ranges
    return Object.values(groups)
      .map((session) => {
        const sortedTimes = Array.from(session.times).sort();
        const startTimeUtc = sortedTimes[0];
        const endTimeUtc = sortedTimes[sortedTimes.length - 1];

        return {
          ...session,
          startTimeUtc: formatTimeUtc(startTimeUtc),
          endTimeUtc: formatTimeUtc(endTimeUtc),
          startTimeBoston: utcHHMMSSToBoston(startTimeUtc, date),
          endTimeBoston: utcHHMMSSToBoston(endTimeUtc, date),
          timezoneAbbr: getBostonTimezoneAbbr(`${date}T${formatTimeUtc(startTimeUtc) || "12:00:00"}Z`),
          fileTypes: Array.from(session.fileTypes),
        };
      })
      .sort((a, b) => (a.startTimeUtc || "").localeCompare(b.startTimeUtc || ""));
  }, [files.raw, date]);

  const handleViewInDashboard = (sessionId) => {
    // Open main dashboard with this session selected
    // Session path format: deviceId/date-sessionId (e.g., E1/2026-04-05-s001)
    const sessionPath = sessionId ? `${date}-${sessionId}` : date;
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
      ) : sessions.length === 0 ? (
        <div style={{ color: "var(--text-secondary)", padding: 20 }}>
          No sessions found for this date.
        </div>
      ) : (
        <div className="sessions-list">
          {sessions.map((session) => (
            <div key={session.sessionId} className="session-card">
              <div className="session-header">
                <div className="session-id">
                  <span className="session-label">Session</span>
                  <span className="session-name">{session.sessionId}</span>
                </div>
                <div className="session-time">
                  {session.startTimeBoston && session.endTimeBoston ? (
                    <>
                      <span className="time-local">
                        {session.startTimeBoston} — {session.endTimeBoston} {session.timezoneAbbr}
                      </span>
                      <span className="time-utc">
                        ({session.startTimeUtc} — {session.endTimeUtc} UTC)
                      </span>
                    </>
                  ) : (
                    <span className="time-utc">Time unknown</span>
                  )}
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
                  onClick={() => handleViewInDashboard(session.sessionId !== "unknown" ? session.sessionId : null)}
                  style={{ fontSize: 12, padding: "6px 12px" }}
                >
                  View in Dashboard
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
