import React from "react";
import Plot from "react-plotly.js";

export default function E1WindChart({ data }) {
  if (!data?.length) {
    return (
      <div className="loading">No wind data available</div>
    );
  }

  const timestamps = data.map((d) => d.timestamp || d.t);

  return (
    <div>
      <Plot
        data={[
          {
            type: "scatter",
            mode: "lines",
            name: "Wind Speed",
            x: timestamps,
            y: data.map((d) => d.apparent_speed_kts ?? d.aws_kn ?? d.aws_kts),
            line: { color: "var(--accent)", width: 1 },
            hovertemplate: "Speed: %{y:.1f} kts<extra></extra>",
          },
        ]}
        layout={{
          title: { text: "Apparent Wind Speed", font: { size: 14, color: "#e4e8ec" } },
          xaxis: {
            title: "Time",
            color: "#8899a6",
            gridcolor: "#2f3f4f",
          },
          yaxis: {
            title: "Speed (kts)",
            color: "#8899a6",
            gridcolor: "#2f3f4f",
          },
          paper_bgcolor: "rgba(0,0,0,0)",
          plot_bgcolor: "rgba(0,0,0,0)",
          font: { color: "#e4e8ec" },
          height: 250,
          margin: { t: 40, b: 50, l: 50, r: 20 },
        }}
        config={{ responsive: true, displayModeBar: false }}
        style={{ width: "100%" }}
      />
      <Plot
        data={[
          {
            type: "scatter",
            mode: "lines",
            name: "Wind Angle",
            x: timestamps,
            y: data.map((d) => d.apparent_angle_deg ?? d.awa ?? d.awa_deg),
            line: { color: "var(--warning)", width: 1 },
            hovertemplate: "Angle: %{y:.0f}deg<extra></extra>",
          },
        ]}
        layout={{
          title: { text: "Apparent Wind Angle", font: { size: 14, color: "#e4e8ec" } },
          xaxis: {
            title: "Time",
            color: "#8899a6",
            gridcolor: "#2f3f4f",
          },
          yaxis: {
            title: "Angle (deg)",
            color: "#8899a6",
            gridcolor: "#2f3f4f",
          },
          paper_bgcolor: "rgba(0,0,0,0)",
          plot_bgcolor: "rgba(0,0,0,0)",
          font: { color: "#e4e8ec" },
          height: 250,
          margin: { t: 40, b: 50, l: 50, r: 20 },
        }}
        config={{ responsive: true, displayModeBar: false }}
        style={{ width: "100%" }}
      />
    </div>
  );
}
