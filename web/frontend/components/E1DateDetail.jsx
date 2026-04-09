import React, { useState, useEffect, useMemo } from "react";
import { useParams, Link } from "react-router-dom";
import { API_URL } from "../src/config";
import { utcToBoston, getBostonTimezoneAbbr } from "../src/timeUtils";

const BOSTON_TIMEZONE = "America/New_York";

// Convert UTC ISO timestamp to Boston local date string (YYYY-MM-DD)
function getLocalDateKey(utcTimestamp) {
  if (!utcTimestamp) return null;
  try {
    const dt = new Date(utcTimestamp);
    return dt.toLocaleDateString("en-CA", { timeZone: BOSTON_TIMEZONE });
  } catch {
    return null;
  }
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

// Format date for display
function formatDisplayDate(dateStr) {
  if (!dateStr) return dateStr;
  try {
    const dt = new Date(dateStr + "T12:00:00Z");
    return dt.toLocaleDateString("en-US", {
      timeZone: BOSTON_TIMEZONE,
      weekday: "short",
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  } catch {
    return dateStr;
  }
}

const FILE_TYPE_COLORS = {
  nav: "var(--accent)",
  imu: "var(--success)",
  wind: "var(--warning)",
  rtcm3: "#9333ea",  // Purple for RTCM3
  ppk: "#10b981",    // Green for PPK
  other: "var(--border)",
};

// PPK status display config
const PPK_STATUS_CONFIG = {
  awaiting_cors: { label: "Awaiting CORS", color: "#f59e0b", icon: "⏳" },
  cors_downloading: { label: "Downloading CORS", color: "#3b82f6", icon: "⬇️" },
  cors_ready: { label: "CORS Ready", color: "#3b82f6", icon: "✓" },
  processing: { label: "Processing PPK", color: "#8b5cf6", icon: "⚙️" },
  completed: { label: "PPK Complete", color: "#10b981", icon: "✓" },
  failed: { label: "PPK Failed", color: "#ef4444", icon: "✗" },
  cors_error: { label: "CORS Error", color: "#ef4444", icon: "!" },
};

export default function E1DateDetail() {
  const { deviceId, date } = useParams();
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [deleting, setDeleting] = useState(null);
  const [expandedSessions, setExpandedSessions] = useState(new Set());

  const fetchSessions = () => {
    setLoading(true);
    fetch(`${API_URL}/api/sessions`)
      .then((r) => {
        if (!r.ok) throw new Error("Failed to load sessions");
        return r.json();
      })
      .then((data) => {
        const deviceSessions = (data.sessions || []).filter((s) => {
          if (s.device_id !== deviceId) return false;
          if (!s.start_time) return false;
          const localDate = getLocalDateKey(s.start_time);
          return localDate === date;
        });
        setSessions(deviceSessions);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchSessions();
  }, [deviceId, date]);

  // Process sessions for display
  const sortedSessions = useMemo(() => {
    return sessions
      .map((session) => {
        const sessionIdParts = (session.session_id || "").split("-");
        const baseSessionId = sessionIdParts[0] || session.session_id;

        return {
          sessionId: session.session_id,
          folderDate: session.date,
          displayName: baseSessionId,
          startTime: session.start_time,
          endTime: session.end_time,
          startTimeBoston: utcToBoston(session.start_time),
          endTimeBoston: utcToBoston(session.end_time),
          startTimeUtc: formatUtcTime(session.start_time),
          endTimeUtc: formatUtcTime(session.end_time),
          timezoneAbbr: getBostonTimezoneAbbr(session.start_time),
          durationMinutes: session.duration_minutes,
          sensors: session.sensors || {},
          ppkStatus: session.ppk_status,
          ppkStats: session.ppk_stats,
          ppkError: session.ppk_error,
        };
      })
      .sort((a, b) => a.startTime.localeCompare(b.startTime));
  }, [sessions]);

  // Sessions under 15 minutes
  const shortSessions = useMemo(() => {
    return sortedSessions.filter((s) => s.durationMinutes != null && s.durationMinutes < 15);
  }, [sortedSessions]);

  const handleViewInDashboard = (session) => {
    const sessionPath = session.sessionId
      ? `${session.folderDate}-${session.sessionId}`
      : session.folderDate;
    window.open(`/dashboard/?session=${deviceId}/${sessionPath}`, "_blank");
  };

  const handleDeleteSession = async (session) => {
    const sessionPath = session.sessionId
      ? `${session.folderDate}-${session.sessionId}`
      : session.folderDate;

    const confirmed = window.confirm(
      `Delete session ${session.displayName} (${session.durationMinutes || 0} min)?\n\nThis will permanently delete all data for this session.`
    );

    if (!confirmed) return;

    setDeleting(session.sessionId);
    try {
      const response = await fetch(`${API_URL}/api/sessions/${deviceId}/${sessionPath}`, {
        method: "DELETE",
      });

      if (!response.ok) {
        throw new Error("Failed to delete session");
      }

      // Refresh sessions list
      fetchSessions();
    } catch (err) {
      alert(`Error deleting session: ${err.message}`);
    } finally {
      setDeleting(null);
    }
  };

  const handleDeleteShortSessions = async () => {
    if (shortSessions.length === 0) {
      alert("No sessions under 15 minutes to delete.");
      return;
    }

    const confirmed = window.confirm(
      `Delete ${shortSessions.length} session(s) under 15 minutes?\n\n` +
        shortSessions.map((s) => `  - ${s.displayName} (${s.durationMinutes || 0} min)`).join("\n") +
        `\n\nThis will permanently delete all data for these sessions.`
    );

    if (!confirmed) return;

    setDeleting("bulk");
    let deleted = 0;
    let errors = 0;

    for (const session of shortSessions) {
      const sessionPath = session.sessionId
        ? `${session.folderDate}-${session.sessionId}`
        : session.folderDate;

      try {
        const response = await fetch(`${API_URL}/api/sessions/${deviceId}/${sessionPath}`, {
          method: "DELETE",
        });
        if (response.ok) {
          deleted++;
        } else {
          errors++;
        }
      } catch {
        errors++;
      }
    }

    setDeleting(null);
    fetchSessions();

    if (errors > 0) {
      alert(`Deleted ${deleted} sessions. ${errors} failed.`);
    }
  };

  const getSensorTypes = (sensors) => {
    const types = [];
    if (sensors.gps) types.push("nav");
    if (sensors.imu) types.push("imu");
    if (sensors.wind) types.push("wind");
    if (sensors.rtcm3) types.push("rtcm3");
    return types;
  };

  const togglePpkExpand = (sessionId) => {
    setExpandedSessions((prev) => {
      const newSet = new Set(prev);
      if (newSet.has(sessionId)) {
        newSet.delete(sessionId);
      } else {
        newSet.add(sessionId);
      }
      return newSet;
    });
  };

  // CORS station display names
  const CORS_STATIONS = {
    mami: { name: "Mass Maritime", location: "Buzzards Bay, MA" },
    bosm: { name: "Boston", location: "Boston, MA" },
    njgt: { name: "NJ Gateway", location: "New Jersey" },
  };

  // Render PPK details panel
  const renderPpkDetails = (session) => {
    const stats = session.ppkStats;
    if (!stats) return null;

    const total = (stats.fix_count || 0) + (stats.float_count || 0) + (stats.single_count || 0);
    const fixPct = total > 0 ? ((stats.fix_count || 0) / total * 100) : 0;
    const floatPct = total > 0 ? ((stats.float_count || 0) / total * 100) : 0;
    const singlePct = total > 0 ? ((stats.single_count || 0) / total * 100) : 0;

    const corsInfo = CORS_STATIONS[stats.cors_station] || { name: stats.cors_station?.toUpperCase(), location: "Unknown" };

    // Format accuracy - combine N/E as horizontal
    const avgNE = stats.avg_sdn && stats.avg_sde
      ? Math.sqrt(stats.avg_sdn ** 2 + stats.avg_sde ** 2)
      : null;

    return (
      <div className="ppk-details-panel">
        <div className="ppk-stats-grid">
          <div className="ppk-stat-card">
            <div className="ppk-stat-label">Fix Rate</div>
            <div className="ppk-stat-value">{stats.fix_rate}%</div>
            <div className="ppk-stat-detail">{stats.fix_count?.toLocaleString()} fixed</div>
          </div>
          <div className="ppk-stat-card">
            <div className="ppk-stat-label">Points</div>
            <div className="ppk-stat-value">{stats.points?.toLocaleString()}</div>
            <div className="ppk-stat-detail">
              {session.durationMinutes ? `~${Math.round(stats.points / session.durationMinutes / 60)}Hz` : ""}
            </div>
          </div>
          <div className="ppk-stat-card">
            <div className="ppk-stat-label">Accuracy</div>
            <div className="ppk-stat-value">
              {avgNE ? `${avgNE.toFixed(2)}m` : "—"}
            </div>
            <div className="ppk-stat-detail">
              {stats.avg_sdu ? `${stats.avg_sdu.toFixed(2)}m Up` : "horizontal"}
            </div>
          </div>
          <div className="ppk-stat-card">
            <div className="ppk-stat-label">Base Station</div>
            <div className="ppk-stat-value">{stats.cors_station?.toUpperCase() || "—"}</div>
            <div className="ppk-stat-detail">{corsInfo.name}</div>
          </div>
        </div>

        <div className="ppk-quality-section">
          <div className="ppk-quality-label">Quality Breakdown</div>
          <div className="ppk-quality-bar">
            {fixPct > 0 && (
              <div
                className="ppk-quality-segment ppk-quality-fix"
                style={{ width: `${fixPct}%` }}
                title={`Fix: ${stats.fix_count} (${fixPct.toFixed(1)}%)`}
              />
            )}
            {floatPct > 0 && (
              <div
                className="ppk-quality-segment ppk-quality-float"
                style={{ width: `${floatPct}%` }}
                title={`Float: ${stats.float_count} (${floatPct.toFixed(1)}%)`}
              />
            )}
            {singlePct > 0 && (
              <div
                className="ppk-quality-segment ppk-quality-single"
                style={{ width: `${singlePct}%` }}
                title={`Single: ${stats.single_count} (${singlePct.toFixed(1)}%)`}
              />
            )}
          </div>
          <div className="ppk-quality-legend">
            <span className="ppk-legend-item">
              <span className="ppk-legend-color ppk-quality-fix" /> Fix {fixPct.toFixed(1)}%
            </span>
            <span className="ppk-legend-item">
              <span className="ppk-legend-color ppk-quality-float" /> Float {floatPct.toFixed(1)}%
            </span>
            <span className="ppk-legend-item">
              <span className="ppk-legend-color ppk-quality-single" /> Single {singlePct.toFixed(1)}%
            </span>
          </div>
        </div>

        {stats.processed_at && (
          <div className="ppk-timeline">
            <span className="ppk-timeline-label">Processed:</span>
            <span className="ppk-timeline-value">
              {new Date(stats.processed_at).toLocaleString("en-US", {
                timeZone: BOSTON_TIMEZONE,
                month: "short",
                day: "numeric",
                hour: "numeric",
                minute: "2-digit",
              })}
            </span>
          </div>
        )}
      </div>
    );
  };

  // Render PPK error panel
  const renderPpkError = (session) => {
    if (!session.ppkError) return null;

    return (
      <div className="ppk-details-panel ppk-error-panel">
        <div className="ppk-error">
          <span className="ppk-error-icon">⚠️</span>
          <span className="ppk-error-message">{session.ppkError}</span>
        </div>
      </div>
    );
  };

  // Render PPK status badge (clickable for completed/failed sessions)
  const renderPpkStatus = (session) => {
    if (!session.ppkStatus && !session.sensors.rtcm3) return null;

    const status = session.ppkStatus || "no_rtcm3";
    const config = PPK_STATUS_CONFIG[status];

    if (!config) return null;

    const isExpandable = status === "completed" || status === "failed";
    const isExpanded = expandedSessions.has(session.sessionId);

    return (
      <div
        onClick={isExpandable ? () => togglePpkExpand(session.sessionId) : undefined}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "4px 8px",
          borderRadius: 4,
          background: `${config.color}20`,
          border: `1px solid ${config.color}40`,
          fontSize: 12,
          cursor: isExpandable ? "pointer" : "default",
          userSelect: "none",
        }}
        title={isExpandable ? "Click to expand details" : (session.ppkError || "")}
      >
        <span>{config.icon}</span>
        <span style={{ color: config.color, fontWeight: 500 }}>
          {config.label}
          {session.ppkStats && session.ppkStatus === "completed" && (
            <span style={{ fontWeight: 400, marginLeft: 4 }}>
              ({session.ppkStats.fix_rate}% fix)
            </span>
          )}
        </span>
        {isExpandable && (
          <span style={{ marginLeft: 4, transition: "transform 0.2s", transform: isExpanded ? "rotate(180deg)" : "rotate(0deg)" }}>
            ▼
          </span>
        )}
      </div>
    );
  };

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <Link to="/" style={{ color: "var(--text-secondary)", textDecoration: "none" }}>
            E1 Fleet
          </Link>
          <span style={{ color: "var(--text-secondary)" }}> / </span>
          <Link
            to={`/${deviceId}`}
            style={{ color: "var(--text-secondary)", textDecoration: "none" }}
          >
            {deviceId}
          </Link>
          <span style={{ color: "var(--text-secondary)" }}> / </span>
          <h2 style={{ display: "inline" }}>{formatDisplayDate(date)}</h2>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {shortSessions.length > 0 && (
            <button
              onClick={handleDeleteShortSessions}
              disabled={deleting === "bulk"}
              style={{
                fontSize: 12,
                padding: "6px 12px",
                background: "var(--danger)",
                color: "#fff",
                border: "none",
                borderRadius: 4,
                cursor: deleting === "bulk" ? "wait" : "pointer",
                opacity: deleting === "bulk" ? 0.6 : 1,
              }}
            >
              {deleting === "bulk" ? "Deleting..." : `Delete ${shortSessions.length} < 15min`}
            </button>
          )}
          <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>
            All times from GPS UTC
          </span>
        </div>
      </div>

      {loading ? (
        <div className="loading">Loading...</div>
      ) : error ? (
        <div style={{ color: "var(--danger)" }}>Error: {error}</div>
      ) : sortedSessions.length === 0 ? (
        <div style={{ color: "var(--text-secondary)", padding: 20 }}>
          No sessions found for this date.
        </div>
      ) : (
        <div className="sessions-list">
          {sortedSessions.map((session) => (
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
                    {session.durationMinutes != null && ` · ${session.durationMinutes} min`}
                  </span>
                </div>
              </div>

              <div className="session-details">
                <div className="session-files">
                  {getSensorTypes(session.sensors).map((type) => (
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
                  {session.durationMinutes != null && (
                    <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>
                      {session.durationMinutes} min
                    </span>
                  )}
                  {renderPpkStatus(session)}
                </div>
                <div style={{ display: "flex", gap: 8 }}>
                  <button
                    onClick={() => handleViewInDashboard(session)}
                    style={{ fontSize: 12, padding: "6px 12px" }}
                  >
                    View in Dashboard
                  </button>
                  <button
                    onClick={() => handleDeleteSession(session)}
                    disabled={deleting === session.sessionId}
                    style={{
                      fontSize: 12,
                      padding: "6px 12px",
                      background: "var(--danger)",
                      color: "#fff",
                      border: "none",
                      borderRadius: 4,
                      cursor: deleting === session.sessionId ? "wait" : "pointer",
                      opacity: deleting === session.sessionId ? 0.6 : 1,
                    }}
                  >
                    {deleting === session.sessionId ? "..." : "Delete"}
                  </button>
                </div>
              </div>

              {/* Expandable PPK details panel */}
              {expandedSessions.has(session.sessionId) && session.ppkStatus === "completed" && renderPpkDetails(session)}
              {expandedSessions.has(session.sessionId) && session.ppkStatus === "failed" && renderPpkError(session)}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
