import React, { useMemo } from "react";
import Plot from "react-plotly.js";

export default function ManeuverCharts({ maneuvers, summary }) {
  const chartData = useMemo(() => {
    if (!maneuvers?.length) return null;

    const tacks = maneuvers.filter((m) => m.maneuver_type === "tack");
    const gybes = maneuvers.filter((m) => m.maneuver_type === "gybe");

    return { tacks, gybes };
  }, [maneuvers]);

  if (!chartData) {
    return (
      <div className="panel">
        <h2>Maneuver Analysis</h2>
        <div className="loading">No maneuver data available</div>
      </div>
    );
  }

  const { tacks, gybes } = chartData;

  return (
    <div className="panel">
      <div className="panel-header">
        <h2>Maneuver Analysis</h2>
      </div>

      {/* Summary stats */}
      <div className="grid-3" style={{ marginBottom: 20 }}>
        <div className="stat-card">
          <div className="label">Tacks</div>
          <div className="value">{summary?.tacks?.count ?? tacks.length}</div>
          {summary?.tacks?.avg_speed_loss_kts && (
            <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
              Avg loss: {summary.tacks.avg_speed_loss_kts} kts
            </div>
          )}
        </div>
        <div className="stat-card">
          <div className="label">Gybes</div>
          <div className="value">{summary?.gybes?.count ?? gybes.length}</div>
          {summary?.gybes?.avg_speed_loss_kts && (
            <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
              Avg loss: {summary.gybes.avg_speed_loss_kts} kts
            </div>
          )}
        </div>
        <div className="stat-card">
          <div className="label">Avg Recovery</div>
          <div className="value">
            {summary?.tacks?.avg_recovery_sec ?? "—"} <span className="unit">s</span>
          </div>
        </div>
      </div>

      {/* Speed loss over time scatter */}
      <Plot
        data={[
          {
            type: "scatter",
            mode: "markers",
            name: "Tacks",
            x: tacks.map((_, i) => i + 1),
            y: tacks.map((m) => m.speed_loss_kts),
            marker: { color: "#ffad1f", size: 8 },
            hovertemplate:
              "Tack #%{x}<br>Speed loss: %{y:.1f} kts<extra></extra>",
          },
          {
            type: "scatter",
            mode: "markers",
            name: "Gybes",
            x: gybes.map((_, i) => i + 1),
            y: gybes.map((m) => m.speed_loss_kts),
            marker: { color: "#e0245e", size: 8 },
            hovertemplate:
              "Gybe #%{x}<br>Speed loss: %{y:.1f} kts<extra></extra>",
          },
        ]}
        layout={{
          title: { text: "Speed Loss per Maneuver", font: { size: 14, color: "#e4e8ec" } },
          xaxis: { title: "Maneuver #", color: "#8899a6", gridcolor: "#2f3f4f" },
          yaxis: { title: "Speed Loss (kts)", color: "#8899a6", gridcolor: "#2f3f4f" },
          paper_bgcolor: "rgba(0,0,0,0)",
          plot_bgcolor: "rgba(0,0,0,0)",
          font: { color: "#e4e8ec" },
          height: 300,
          margin: { t: 40, b: 40, l: 50, r: 20 },
          legend: { orientation: "h", y: -0.2 },
        }}
        config={{ responsive: true, displayModeBar: false }}
        style={{ width: "100%" }}
      />

      {/* Recovery time bar chart */}
      <Plot
        data={[
          {
            type: "bar",
            name: "Tacks",
            x: tacks.map((_, i) => `T${i + 1}`),
            y: tacks.map((m) => m.recovery_time_sec),
            marker: { color: "#ffad1f" },
          },
          {
            type: "bar",
            name: "Gybes",
            x: gybes.map((_, i) => `G${i + 1}`),
            y: gybes.map((m) => m.recovery_time_sec),
            marker: { color: "#e0245e" },
          },
        ]}
        layout={{
          title: { text: "Recovery Time", font: { size: 14, color: "#e4e8ec" } },
          xaxis: { color: "#8899a6", gridcolor: "#2f3f4f" },
          yaxis: { title: "Seconds", color: "#8899a6", gridcolor: "#2f3f4f" },
          paper_bgcolor: "rgba(0,0,0,0)",
          plot_bgcolor: "rgba(0,0,0,0)",
          font: { color: "#e4e8ec" },
          height: 300,
          margin: { t: 40, b: 40, l: 50, r: 20 },
          barmode: "group",
          legend: { orientation: "h", y: -0.2 },
        }}
        config={{ responsive: true, displayModeBar: false }}
        style={{ width: "100%" }}
      />
    </div>
  );
}
