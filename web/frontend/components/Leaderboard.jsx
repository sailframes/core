import React, { useState, useEffect } from "react";

const METRICS = [
  { value: "max_speed", label: "Max Speed (kts)" },
  { value: "avg_vmg_upwind", label: "Avg VMG Upwind (kts)" },
  { value: "avg_vmg_downwind", label: "Avg VMG Downwind (kts)" },
  { value: "best_tack_speed_loss", label: "Best Tack (least speed loss)" },
  { value: "avg_speed", label: "Avg Speed (kts)" },
];

export default function Leaderboard() {
  const [metric, setMetric] = useState("max_speed");
  const [boatClass, setBoatClass] = useState("");
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    const params = new URLSearchParams({ metric, limit: "20" });
    if (boatClass) params.set("boat_class", boatClass);

    fetch(`/api/leaderboard?${params}`)
      .then((r) => r.json())
      .then((data) => setEntries(data.entries || []))
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [metric, boatClass]);

  const metricLabel = METRICS.find((m) => m.value === metric)?.label || metric;

  return (
    <div className="panel">
      <div className="panel-header">
        <h2>Leaderboard</h2>
        <div style={{ display: "flex", gap: 8 }}>
          <select value={boatClass} onChange={(e) => setBoatClass(e.target.value)}>
            <option value="">All Classes</option>
            <option value="Sonar 23">Sonar 23</option>
            <option value="J/80">J/80</option>
          </select>
          <select value={metric} onChange={(e) => setMetric(e.target.value)}>
            {METRICS.map((m) => (
              <option key={m.value} value={m.value}>{m.label}</option>
            ))}
          </select>
        </div>
      </div>

      {loading ? (
        <div className="loading">Loading leaderboard...</div>
      ) : entries.length === 0 ? (
        <p style={{ color: "var(--text-secondary)", textAlign: "center", padding: 40 }}>
          No leaderboard data yet. Analyze sessions to populate rankings.
        </p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Rank</th>
              <th>Boat</th>
              <th>Date</th>
              <th>{metricLabel}</th>
              <th>Class</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry, i) => (
              <tr key={i}>
                <td>
                  <span style={{
                    fontWeight: 700,
                    color: i < 3 ? ["#ffad1f", "#c0c0c0", "#cd7f32"][i] : "var(--text-secondary)",
                  }}>
                    {i + 1}
                  </span>
                </td>
                <td style={{ fontWeight: 500 }}>{entry.boat_name || entry.device_id}</td>
                <td style={{ color: "var(--text-secondary)" }}>{entry.date}</td>
                <td><b>{entry[metric]?.toFixed?.(2) ?? entry[metric] ?? "—"}</b></td>
                <td style={{ color: "var(--text-secondary)" }}>{entry.boat_class || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
