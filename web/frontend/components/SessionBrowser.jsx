import React from "react";
import { utcToBoston, utcToBostonDate, formatBostonTimeRange, getBostonTimezoneAbbr } from "../src/timeUtils";

export default function SessionBrowser({ sessions, activeSession, onSelect }) {
  if (!sessions?.length) {
    return <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>No sessions found</div>;
  }

  return (
    <div>
      <div style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: 8, textTransform: "uppercase" }}>
        Sessions (Boston Time)
      </div>
      <div style={{ maxHeight: 300, overflowY: "auto" }}>
        {sessions.map((s) => {
          const isActive = activeSession?.device_id === s.device_id && activeSession?.date === s.date;
          const localDate = utcToBostonDate(s.start_time) || s.date;
          const timeRange = formatBostonTimeRange(s.start_time, s.end_time);
          const tzAbbr = getBostonTimezoneAbbr(s.start_time);

          return (
            <div
              key={`${s.device_id}-${s.date}-${s.session_id || ''}`}
              onClick={() => onSelect(s)}
              style={{
                padding: "8px 10px",
                borderRadius: 6,
                cursor: "pointer",
                background: isActive ? "var(--bg-card)" : "transparent",
                marginBottom: 2,
                fontSize: 13,
              }}
            >
              <div style={{ fontWeight: isActive ? 600 : 400 }}>
                {localDate}
                {s.session_id && <span style={{ color: "var(--accent)", marginLeft: 6 }}>{s.session_id}</span>}
              </div>
              <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>
                {timeRange && `${timeRange} ${tzAbbr}`}
                {timeRange && s.duration_minutes && " · "}
                {s.duration_minutes ? `${s.duration_minutes}min` : ""}
                {s.has_video && " · 📹"}
              </div>
              <div style={{ fontSize: 10, color: "var(--text-secondary)", opacity: 0.7 }}>
                {s.device_id}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
