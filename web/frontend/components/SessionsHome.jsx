import React, { useState, useEffect, useMemo } from "react";
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
  rtcm3: "#9333ea",
  ppk: "#10b981",
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

// CORS station display names
const CORS_STATIONS = {
  mami: { name: "Mass Maritime", location: "Buzzards Bay, MA" },
  bosm: { name: "Boston", location: "Boston, MA" },
  njgt: { name: "NJ Gateway", location: "New Jersey" },
};

const BOAT_OPTIONS = [
  { value: "", label: "Select boat..." },
  { value: "Sonar 23", label: "Sonar 23" },
  { value: "J/80", label: "J/80" },
  { value: "Tesla", label: "Tesla" },
];

export default function SessionsHome() {
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [deleting, setDeleting] = useState(null);
  const [editing, setEditing] = useState(null); // { deviceId, sessionPath, field }
  const [editValue, setEditValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [expandedPpk, setExpandedPpk] = useState(new Set());

  const fetchSessions = () => {
    setLoading(true);
    fetch(`${API_URL}/api/sessions`)
      .then((r) => {
        if (!r.ok) throw new Error("Failed to load sessions");
        return r.json();
      })
      .then((data) => {
        setSessions(data.sessions || []);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchSessions();
  }, []);

  // Group sessions by local date
  const sessionsByDate = useMemo(() => {
    const groups = {};

    sessions.forEach((session) => {
      const localDate = getLocalDateKey(session.start_time) || session.date;
      if (!groups[localDate]) {
        groups[localDate] = [];
      }

      const sessionIdParts = (session.session_id || "").split("-");
      const baseSessionId = sessionIdParts[0] || session.session_id;

      groups[localDate].push({
        ...session,
        localDate,
        displayName: baseSessionId || session.date,
        startTimeBoston: utcToBoston(session.start_time),
        endTimeBoston: utcToBoston(session.end_time),
        startTimeUtc: formatUtcTime(session.start_time),
        endTimeUtc: formatUtcTime(session.end_time),
        timezoneAbbr: getBostonTimezoneAbbr(session.start_time),
        sessionPath: session.session_id
          ? `${session.date}-${session.session_id}`
          : session.date,
      });
    });

    // Sort sessions within each date by start time
    Object.values(groups).forEach((group) => {
      group.sort((a, b) => (a.start_time || "").localeCompare(b.start_time || ""));
    });

    // Return sorted by date (most recent first)
    return Object.entries(groups)
      .sort(([a], [b]) => b.localeCompare(a))
      .map(([date, sessions]) => ({ date, sessions }));
  }, [sessions]);

  const handleViewInDashboard = (session) => {
    window.open(`/dashboard/?session=${session.device_id}/${session.sessionPath}`, "_blank");
  };

  const handleDeleteSession = async (session) => {
    const confirmed = window.confirm(
      `Delete session ${session.displayName} (${session.duration_minutes || 0} min)?\n\nThis will permanently delete all data for this session.`
    );

    if (!confirmed) return;

    setDeleting(`${session.device_id}-${session.sessionPath}`);
    try {
      const response = await fetch(
        `${API_URL}/api/sessions/${session.device_id}/${session.sessionPath}`,
        { method: "DELETE" }
      );

      if (!response.ok) {
        throw new Error("Failed to delete session");
      }

      fetchSessions();
    } catch (err) {
      alert(`Error deleting session: ${err.message}`);
    } finally {
      setDeleting(null);
    }
  };

  const handleStartEdit = (session, field) => {
    setEditing({
      deviceId: session.device_id,
      sessionPath: session.sessionPath,
      field,
    });
    setEditValue(session[field] || "");
  };

  const handleSaveEdit = async () => {
    if (!editing) return;

    setSaving(true);
    try {
      const response = await fetch(
        `${API_URL}/api/sessions/${editing.deviceId}/${editing.sessionPath}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ [editing.field]: editValue || null }),
        }
      );

      if (!response.ok) {
        throw new Error("Failed to save");
      }

      fetchSessions();
      setEditing(null);
    } catch (err) {
      alert(`Error saving: ${err.message}`);
    } finally {
      setSaving(false);
    }
  };

  const handleCancelEdit = () => {
    setEditing(null);
    setEditValue("");
  };

  const handleKeyDown = (e) => {
    if (e.key === "Enter") {
      handleSaveEdit();
    } else if (e.key === "Escape") {
      handleCancelEdit();
    }
  };

  const getSensorTypes = (sensors) => {
    const types = [];
    if (sensors?.gps) types.push("nav");
    if (sensors?.imu) types.push("imu");
    if (sensors?.wind) types.push("wind");
    if (sensors?.rtcm3) types.push("rtcm3");
    return types;
  };

  const togglePpkExpand = (sessionKey) => {
    setExpandedPpk((prev) => {
      const newSet = new Set(prev);
      if (newSet.has(sessionKey)) {
        newSet.delete(sessionKey);
      } else {
        newSet.add(sessionKey);
      }
      return newSet;
    });
  };

  const getSessionKey = (session) => `${session.device_id}-${session.sessionPath}`;

  // Render PPK status badge (clickable for completed/failed sessions)
  const renderPpkStatus = (session) => {
    if (!session.ppk_status && !session.sensors?.rtcm3) return null;

    const status = session.ppk_status || "no_rtcm3";
    const config = PPK_STATUS_CONFIG[status];

    if (!config) return null;

    const sessionKey = getSessionKey(session);
    const isExpandable = status === "completed" || status === "failed";
    const isExpanded = expandedPpk.has(sessionKey);

    return (
      <div
        onClick={isExpandable ? () => togglePpkExpand(sessionKey) : undefined}
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
        title={isExpandable ? "Click to expand details" : (session.ppk_error || "")}
      >
        <span>{config.icon}</span>
        <span style={{ color: config.color, fontWeight: 500 }}>
          {config.label}
          {session.ppk_stats && session.ppk_status === "completed" && (
            <span style={{ fontWeight: 400, marginLeft: 4 }}>
              ({session.ppk_stats.fix_rate}% fix)
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

  // Render PPK details panel
  const renderPpkDetails = (session) => {
    const stats = session.ppk_stats;
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
              {session.duration_minutes ? `~${Math.round(stats.points / session.duration_minutes / 60)}Hz` : ""}
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
    if (!session.ppk_error) return null;

    return (
      <div className="ppk-details-panel ppk-error-panel">
        <div className="ppk-error">
          <span className="ppk-error-icon">⚠️</span>
          <span className="ppk-error-message">{session.ppk_error}</span>
        </div>
      </div>
    );
  };

  const isEditing = (session, field) =>
    editing?.deviceId === session.device_id &&
    editing?.sessionPath === session.sessionPath &&
    editing?.field === field;

  const isDeleting = (session) =>
    deleting === `${session.device_id}-${session.sessionPath}`;

  return (
    <div>
      {loading ? (
        <div className="loading">Loading sessions...</div>
      ) : error ? (
        <div style={{ color: "var(--danger)" }}>Error: {error}</div>
      ) : sessionsByDate.length === 0 ? (
        <div style={{ color: "var(--text-secondary)", padding: 20 }}>
          No sessions found.
        </div>
      ) : (
        sessionsByDate.map(({ date, sessions: dateSessions }) => (
          <div key={date} className="panel" style={{ marginBottom: 20 }}>
            <div className="panel-header">
              <h2 style={{ margin: 0 }}>{formatDisplayDate(date)}</h2>
              <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>
                {dateSessions.length} session{dateSessions.length !== 1 ? "s" : ""}
              </span>
            </div>

            <div className="sessions-list">
              {dateSessions.map((session) => (
                <div key={`${session.device_id}-${session.sessionPath}`} className="session-card">
                  <div className="session-header">
                    <div className="session-id">
                      <span
                        className="device-badge"
                        style={{
                          background: session.device_id === "E1" ? "var(--accent)" : "#9333ea",
                          color: "#fff",
                          padding: "2px 6px",
                          borderRadius: 4,
                          fontSize: 11,
                          fontWeight: 600,
                          marginRight: 8,
                        }}
                      >
                        {session.device_id}
                      </span>

                      {/* Session Name - editable */}
                      {isEditing(session, "name") ? (
                        <input
                          type="text"
                          value={editValue}
                          onChange={(e) => setEditValue(e.target.value)}
                          onKeyDown={handleKeyDown}
                          onBlur={handleSaveEdit}
                          autoFocus
                          placeholder="Session name..."
                          style={{
                            background: "var(--bg-secondary)",
                            border: "1px solid var(--accent)",
                            borderRadius: 4,
                            padding: "4px 8px",
                            color: "var(--text-primary)",
                            fontSize: 14,
                            width: 200,
                          }}
                        />
                      ) : (
                        <span
                          className="session-name"
                          onClick={() => handleStartEdit(session, "name")}
                          style={{ cursor: "pointer" }}
                          title="Click to edit name"
                        >
                          {session.name || session.displayName}
                          {!session.name && (
                            <span style={{ color: "var(--text-secondary)", marginLeft: 4 }}>
                              (click to name)
                            </span>
                          )}
                        </span>
                      )}
                    </div>

                    <div className="session-time">
                      <span className="time-local">
                        {session.startTimeBoston} — {session.endTimeBoston} {session.timezoneAbbr}
                      </span>
                      <span className="time-utc">
                        ({session.startTimeUtc} — {session.endTimeUtc} UTC)
                        {session.duration_minutes != null && ` · ${session.duration_minutes} min`}
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

                      {/* Boat - editable */}
                      {isEditing(session, "boat") ? (
                        <select
                          value={editValue}
                          onChange={(e) => {
                            setEditValue(e.target.value);
                          }}
                          onBlur={handleSaveEdit}
                          autoFocus
                          style={{
                            background: "var(--bg-secondary)",
                            border: "1px solid var(--accent)",
                            borderRadius: 4,
                            padding: "4px 8px",
                            color: "var(--text-primary)",
                            fontSize: 12,
                          }}
                        >
                          {BOAT_OPTIONS.map((opt) => (
                            <option key={opt.value} value={opt.value}>
                              {opt.label}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <span
                          onClick={() => handleStartEdit(session, "boat")}
                          style={{
                            cursor: "pointer",
                            fontSize: 12,
                            color: session.boat ? "var(--text-primary)" : "var(--text-secondary)",
                            padding: "2px 6px",
                            borderRadius: 4,
                            background: session.boat ? "var(--bg-secondary)" : "transparent",
                          }}
                          title="Click to set boat"
                        >
                          {session.boat || "Set boat..."}
                        </span>
                      )}

                      {session.duration_minutes != null && (
                        <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>
                          {session.duration_minutes} min
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
                        disabled={isDeleting(session)}
                        style={{
                          fontSize: 12,
                          padding: "6px 12px",
                          background: "var(--danger)",
                          color: "#fff",
                          border: "none",
                          borderRadius: 4,
                          cursor: isDeleting(session) ? "wait" : "pointer",
                          opacity: isDeleting(session) ? 0.6 : 1,
                        }}
                      >
                        {isDeleting(session) ? "..." : "Delete"}
                      </button>
                    </div>
                  </div>

                  {/* Expandable PPK details panel */}
                  {expandedPpk.has(getSessionKey(session)) && session.ppk_status === "completed" && renderPpkDetails(session)}
                  {expandedPpk.has(getSessionKey(session)) && session.ppk_status === "failed" && renderPpkError(session)}
                </div>
              ))}
            </div>
          </div>
        ))
      )}
    </div>
  );
}
