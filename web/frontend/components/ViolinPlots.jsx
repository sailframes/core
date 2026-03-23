import React from "react";
import Plot from "react-plotly.js";

export default function ViolinPlots({ data }) {
  if (!data || !Object.keys(data).length) {
    return (
      <div className="panel">
        <h2>Performance Distributions</h2>
        <div className="loading">No distribution data available</div>
      </div>
    );
  }

  const metrics = ["speed_loss_kts", "recovery_time_sec", "duration_sec"];
  const metricLabels = {
    speed_loss_kts: "Speed Loss (kts)",
    recovery_time_sec: "Recovery Time (s)",
    duration_sec: "Duration (s)",
  };
  const colors = { tack: "#ffad1f", gybe: "#e0245e" };

  return (
    <div className="panel">
      <div className="panel-header">
        <h2>Performance Distributions</h2>
      </div>
      <div className="grid-3">
        {metrics.map((metric) => {
          const traces = Object.entries(data)
            .filter(([, mdata]) => mdata[metric])
            .map(([mtype, mdata]) => ({
              type: "violin",
              y: mdata[metric].values,
              name: mtype.charAt(0).toUpperCase() + mtype.slice(1),
              box: { visible: true },
              meanline: { visible: true },
              fillcolor: colors[mtype] || "#1da1f2",
              line: { color: colors[mtype] || "#1da1f2" },
              opacity: 0.7,
            }));

          return (
            <Plot
              key={metric}
              data={traces}
              layout={{
                title: { text: metricLabels[metric], font: { size: 13, color: "#e4e8ec" } },
                yaxis: { color: "#8899a6", gridcolor: "#2f3f4f" },
                paper_bgcolor: "rgba(0,0,0,0)",
                plot_bgcolor: "rgba(0,0,0,0)",
                font: { color: "#e4e8ec" },
                height: 300,
                margin: { t: 40, b: 30, l: 50, r: 20 },
                showlegend: false,
              }}
              config={{ responsive: true, displayModeBar: false }}
              style={{ width: "100%" }}
            />
          );
        })}
      </div>
    </div>
  );
}
