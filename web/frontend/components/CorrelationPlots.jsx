import React from "react";
import Plot from "react-plotly.js";

export default function CorrelationPlots({ data }) {
  if (!data?.variables || !data?.matrix) {
    return (
      <div className="panel">
        <h2>Correlations</h2>
        <div className="loading">No correlation data available</div>
      </div>
    );
  }

  const { variables, matrix } = data;
  const labels = {
    boat_speed: "Boat Speed",
    tws: "True Wind Speed",
    twa: "True Wind Angle",
    vmg: "VMG",
    heel: "Heel",
  };

  const z = variables.map((row) =>
    variables.map((col) => matrix[row]?.[col] ?? 0)
  );

  const displayLabels = variables.map((v) => labels[v] || v);

  return (
    <div className="panel">
      <div className="panel-header">
        <h2>Variable Correlations</h2>
      </div>
      <Plot
        data={[
          {
            type: "heatmap",
            z,
            x: displayLabels,
            y: displayLabels,
            colorscale: [
              [0, "#e0245e"],
              [0.5, "#1a2736"],
              [1, "#1da1f2"],
            ],
            zmin: -1,
            zmax: 1,
            hovertemplate:
              "%{x} vs %{y}<br>Correlation: %{z:.3f}<extra></extra>",
            text: z.map((row) => row.map((v) => v.toFixed(2))),
            texttemplate: "%{text}",
            textfont: { color: "#e4e8ec", size: 12 },
          },
        ]}
        layout={{
          paper_bgcolor: "rgba(0,0,0,0)",
          plot_bgcolor: "rgba(0,0,0,0)",
          font: { color: "#e4e8ec" },
          height: 450,
          margin: { t: 20, b: 80, l: 100, r: 40 },
          xaxis: { tickangle: -30, color: "#8899a6" },
          yaxis: { color: "#8899a6" },
        }}
        config={{ responsive: true, displayModeBar: false }}
        style={{ width: "100%" }}
      />
    </div>
  );
}
