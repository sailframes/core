import React, { useMemo } from "react";
import Plot from "react-plotly.js";

export default function RigAnalyzer({ session, analysis }) {
  const heelVsSpeed = useMemo(() => {
    if (!analysis?.vmg_series) return null;
    // Plot heel vs boat speed, colored by TWA
    const series = analysis.vmg_series;
    return {
      x: series.map((v) => v.boat_speed_kts),
      y: series.map((v) => Math.abs(v.twa_deg)),
      color: series.map((v) => v.vmg_kts),
    };
  }, [analysis]);

  if (!analysis) {
    return (
      <div className="panel">
        <h2>Rig Analysis</h2>
        <div className="loading">Load a session to analyze rig performance</div>
      </div>
    );
  }

  const stats = analysis.session_stats || {};

  return (
    <div>
      <div className="panel">
        <div className="panel-header">
          <h2>Rig Performance Analysis</h2>
        </div>
        <p style={{ color: "var(--text-secondary)", marginBottom: 16, fontSize: 14 }}>
          Analyze sail trim efficiency through heel angle, speed, and VMG relationships.
        </p>

        <div className="grid-2" style={{ marginBottom: 20 }}>
          <div className="stat-card">
            <div className="label">Avg Heel</div>
            <div className="value">
              {stats.heel?.mean ?? "—"}<span className="unit">°</span>
            </div>
            <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
              Max: {stats.heel?.max ?? "—"}° · STD: {stats.heel?.std ?? "—"}°
            </div>
          </div>
          <div className="stat-card">
            <div className="label">Avg Pitch</div>
            <div className="value">
              {stats.pitch?.mean ?? "—"}<span className="unit">°</span>
            </div>
            <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
              Max: {stats.pitch?.max ?? "—"}° · STD: {stats.pitch?.std ?? "—"}°
            </div>
          </div>
        </div>
      </div>

      {heelVsSpeed && (
        <div className="panel">
          <div className="panel-header">
            <h2>Speed vs TWA (colored by VMG)</h2>
          </div>
          <Plot
            data={[
              {
                type: "scatter",
                mode: "markers",
                x: heelVsSpeed.x,
                y: heelVsSpeed.y,
                marker: {
                  color: heelVsSpeed.color,
                  colorscale: "Viridis",
                  size: 4,
                  opacity: 0.6,
                  colorbar: { title: "VMG (kts)", titlefont: { color: "#8899a6" } },
                },
                hovertemplate:
                  "Speed: %{x:.1f} kts<br>TWA: %{y:.0f}°<br>VMG: %{marker.color:.1f} kts<extra></extra>",
              },
            ]}
            layout={{
              xaxis: { title: "Boat Speed (kts)", color: "#8899a6", gridcolor: "#2f3f4f" },
              yaxis: { title: "True Wind Angle (°)", color: "#8899a6", gridcolor: "#2f3f4f" },
              paper_bgcolor: "rgba(0,0,0,0)",
              plot_bgcolor: "rgba(0,0,0,0)",
              font: { color: "#e4e8ec" },
              height: 400,
              margin: { t: 20, b: 50, l: 60, r: 80 },
            }}
            config={{ responsive: true, displayModeBar: false }}
            style={{ width: "100%" }}
          />
        </div>
      )}

      {/* Heel distribution during upwind vs downwind */}
      {analysis.legs && (
        <div className="panel">
          <div className="panel-header">
            <h2>Heel by Point of Sail</h2>
          </div>
          <Plot
            data={["upwind", "downwind", "reach"]
              .filter((type) => analysis.legs.some((l) => l.leg_type === type && l.avg_heel_deg != null))
              .map((type) => ({
                type: "box",
                name: type.charAt(0).toUpperCase() + type.slice(1),
                y: analysis.legs
                  .filter((l) => l.leg_type === type && l.avg_heel_deg != null)
                  .map((l) => l.avg_heel_deg),
                boxmean: true,
                marker: {
                  color: type === "upwind" ? "#1da1f2" : type === "downwind" ? "#17bf63" : "#ffad1f",
                },
              }))}
            layout={{
              yaxis: { title: "Heel Angle (°)", color: "#8899a6", gridcolor: "#2f3f4f" },
              paper_bgcolor: "rgba(0,0,0,0)",
              plot_bgcolor: "rgba(0,0,0,0)",
              font: { color: "#e4e8ec" },
              height: 300,
              margin: { t: 20, b: 30, l: 50, r: 20 },
              showlegend: false,
            }}
            config={{ responsive: true, displayModeBar: false }}
            style={{ width: "100%" }}
          />
        </div>
      )}
    </div>
  );
}
