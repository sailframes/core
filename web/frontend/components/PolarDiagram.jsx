import React, { useMemo } from "react";
import Plot from "react-plotly.js";

export default function PolarDiagram({ data }) {
  const traces = useMemo(() => {
    if (!data || !Object.keys(data).length) return [];

    const colors = [
      "#1da1f2", "#17bf63", "#ffad1f", "#e0245e",
      "#794bc4", "#f45d22", "#00bcd4", "#8bc34a",
    ];

    return Object.entries(data).map(([tws, points], i) => ({
      type: "scatterpolar",
      mode: "lines+markers",
      name: `${tws} kts TWS`,
      r: points.map((p) => p.speed),
      theta: points.map((p) => p.angle),
      marker: { size: 4, color: colors[i % colors.length] },
      line: { color: colors[i % colors.length], width: 2 },
      hovertemplate:
        "TWA: %{theta}°<br>Speed: %{r:.1f} kts<br>Samples: %{text}<extra></extra>",
      text: points.map((p) => p.samples),
    }));
  }, [data]);

  if (!data || !Object.keys(data).length) {
    return (
      <div className="panel">
        <h2>Polar Diagram</h2>
        <div className="loading">No polar data available</div>
      </div>
    );
  }

  return (
    <div className="panel">
      <div className="panel-header">
        <h2>Polar Diagram</h2>
      </div>
      <Plot
        data={traces}
        layout={{
          polar: {
            bgcolor: "rgba(0,0,0,0)",
            radialaxis: {
              visible: true,
              color: "#8899a6",
              gridcolor: "#2f3f4f",
              range: [0, Math.max(...traces.flatMap((t) => t.r)) * 1.1],
              title: { text: "Boat Speed (kts)", font: { color: "#8899a6", size: 11 } },
            },
            angularaxis: {
              direction: "clockwise",
              rotation: 90,
              color: "#8899a6",
              gridcolor: "#2f3f4f",
              dtick: 15,
            },
            sector: [0, 180], // Only show one side (symmetric)
          },
          paper_bgcolor: "rgba(0,0,0,0)",
          plot_bgcolor: "rgba(0,0,0,0)",
          font: { color: "#e4e8ec" },
          legend: {
            x: 1.05,
            y: 1,
            font: { size: 11 },
          },
          margin: { t: 20, b: 20, l: 40, r: 120 },
          height: 500,
        }}
        config={{ responsive: true, displayModeBar: false }}
        style={{ width: "100%" }}
      />
    </div>
  );
}
