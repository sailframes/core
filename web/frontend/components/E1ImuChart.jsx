import React from "react";
import Plot from "react-plotly.js";

export default function E1ImuChart({ data }) {
  if (!data?.length) {
    return (
      <div className="loading">No IMU data available</div>
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
            name: "Heel",
            x: timestamps,
            y: data.map((d) => d.heel_deg ?? d.heel),
            line: { color: "var(--accent)", width: 1 },
            hovertemplate: "Heel: %{y:.1f}deg<extra></extra>",
          },
          {
            type: "scatter",
            mode: "lines",
            name: "Pitch",
            x: timestamps,
            y: data.map((d) => d.pitch_deg ?? d.pitch),
            line: { color: "var(--warning)", width: 1 },
            hovertemplate: "Pitch: %{y:.1f}deg<extra></extra>",
          },
        ]}
        layout={{
          title: { text: "Heel & Pitch", font: { size: 14, color: "#e4e8ec" } },
          xaxis: {
            title: "Time",
            color: "#8899a6",
            gridcolor: "#2f3f4f",
          },
          yaxis: {
            title: "Degrees",
            color: "#8899a6",
            gridcolor: "#2f3f4f",
          },
          paper_bgcolor: "rgba(0,0,0,0)",
          plot_bgcolor: "rgba(0,0,0,0)",
          font: { color: "#e4e8ec" },
          height: 350,
          margin: { t: 40, b: 50, l: 50, r: 20 },
          legend: { orientation: "h", y: -0.2 },
        }}
        config={{ responsive: true, displayModeBar: false }}
        style={{ width: "100%" }}
      />
    </div>
  );
}
