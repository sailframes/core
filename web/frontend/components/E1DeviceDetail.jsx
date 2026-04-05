import React, { useState, useEffect, useMemo } from "react";
import { useParams, Link } from "react-router-dom";
import { API_URL } from "../src/config";

const BOSTON_TIMEZONE = "America/New_York";

// Convert UTC ISO timestamp to Boston local date string (YYYY-MM-DD for routing)
function getLocalDateKey(utcTimestamp) {
  if (!utcTimestamp) return null;
  try {
    const dt = new Date(utcTimestamp);
    // Get date parts in Boston timezone
    const parts = dt.toLocaleDateString("en-CA", { timeZone: BOSTON_TIMEZONE }).split("-");
    return parts.join("-"); // YYYY-MM-DD
  } catch {
    return null;
  }
}

// Format UTC timestamp to friendly Boston local date
function formatLocalDate(utcTimestamp) {
  if (!utcTimestamp) return null;
  try {
    const dt = new Date(utcTimestamp);
    return dt.toLocaleDateString("en-US", {
      timeZone: BOSTON_TIMEZONE,
      weekday: "short",
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  } catch {
    return null;
  }
}

export default function E1DeviceDetail() {
  const { deviceId } = useParams();
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Fetch sessions data (has GPS-derived times)
  useEffect(() => {
    setLoading(true);
    fetch(`${API_URL}/api/sessions`)
      .then((r) => {
        if (!r.ok) throw new Error("Failed to load sessions");
        return r.json();
      })
      .then((data) => {
        // Filter sessions for this device
        const deviceSessions = (data.sessions || []).filter(
          (s) => s.device_id === deviceId && s.start_time
        );
        setSessions(deviceSessions);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [deviceId]);

  // Group sessions by local date (Boston time)
  const dateGroups = useMemo(() => {
    const groups = {};

    for (const session of sessions) {
      const localDateKey = getLocalDateKey(session.start_time);
      if (!localDateKey) continue;

      if (!groups[localDateKey]) {
        groups[localDateKey] = {
          localDateKey,
          displayDate: formatLocalDate(session.start_time),
          sessions: [],
          totalSensors: { nav: 0, imu: 0, wind: 0 },
        };
      }

      groups[localDateKey].sessions.push(session);

      // Count sensors
      if (session.sensors?.gps) groups[localDateKey].totalSensors.nav++;
      if (session.sensors?.imu) groups[localDateKey].totalSensors.imu++;
      if (session.sensors?.wind) groups[localDateKey].totalSensors.wind++;
    }

    // Sort by date descending (most recent first)
    return Object.values(groups).sort((a, b) =>
      b.localDateKey.localeCompare(a.localDateKey)
    );
  }, [sessions]);

  const SensorBadge = ({ type, count }) => {
    const colors = {
      nav: "var(--accent)",
      imu: "var(--success)",
      wind: "var(--warning)",
    };
    if (!count) return null;
    return (
      <span
        className="file-type-badge"
        style={{ background: colors[type] || "var(--border)", marginRight: 4 }}
      >
        {type.toUpperCase()}: {count}
      </span>
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
          <h2 style={{ display: "inline" }}>{deviceId}</h2>
        </div>
        <div style={{ fontSize: 13, color: "var(--text-secondary)" }}>
          All times in Boston (America/New_York)
        </div>
      </div>

      {loading ? (
        <div className="loading">Loading sessions...</div>
      ) : error ? (
        <div style={{ color: "var(--danger)" }}>Error: {error}</div>
      ) : dateGroups.length === 0 ? (
        <div style={{ color: "var(--text-secondary)" }}>
          No processed sessions found for this device.
        </div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Date (Boston)</th>
              <th>Sessions</th>
              <th>Sensors</th>
            </tr>
          </thead>
          <tbody>
            {dateGroups.map((group) => (
              <tr key={group.localDateKey}>
                <td>
                  <Link
                    to={`/${deviceId}/${group.localDateKey}`}
                    style={{ color: "var(--accent)", textDecoration: "none" }}
                  >
                    {group.displayDate}
                  </Link>
                </td>
                <td style={{ color: "var(--text-secondary)" }}>
                  {group.sessions.length} session{group.sessions.length !== 1 ? "s" : ""}
                </td>
                <td>
                  <SensorBadge type="nav" count={group.totalSensors.nav} />
                  <SensorBadge type="imu" count={group.totalSensors.imu} />
                  <SensorBadge type="wind" count={group.totalSensors.wind} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
