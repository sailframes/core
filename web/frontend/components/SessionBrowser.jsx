import React from "react";

export default function SessionBrowser({ sessions, activeSession, onSelect }) {
  if (!sessions?.length) {
    return <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>No sessions found</div>;
  }

  return (
    <div>
      <div style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: 8, textTransform: "uppercase" }}>
        Sessions
      </div>
      <div style={{ maxHeight: 300, overflowY: "auto" }}>
        {sessions.map((s) => {
          const isActive = activeSession?.device_id === s.device_id && activeSession?.date === s.date;
          return (
            <div
              key={`${s.device_id}-${s.date}`}
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
              <div style={{ fontWeight: isActive ? 600 : 400 }}>{s.date}</div>
              <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>
                {s.device_id}
                {s.duration_sec ? ` · ${Math.round(s.duration_sec / 60)}min` : ""}
                {s.has_video && " · 📹"}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
