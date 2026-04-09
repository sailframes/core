import React, { useEffect, useState, useMemo } from "react";
import Plot from "react-plotly.js";

const API_URL = import.meta.env.VITE_API_URL || "";

/**
 * BuoyChart - Displays NOAA buoy data aligned with sailing session
 *
 * Shows wind direction, wind speed, pressure, and temperature from
 * multiple Boston Harbor buoys for comparison with onboard sensors.
 */
export default function BuoyChart({
  sessionStart,
  sessionEnd,
  currentTime,
  onTimeChange,
  height = 250,
}) {
  const [buoyData, setBuoyData] = useState({});
  const [loading, setLoading] = useState(false);
  const [selectedMetric, setSelectedMetric] = useState("wind");

  // Fetch buoy data for session time range
  useEffect(() => {
    if (!sessionStart || !sessionEnd) return;

    const startTs = new Date(sessionStart).getTime() / 1000;
    const endTs = new Date(sessionEnd).getTime() / 1000;

    setLoading(true);
    fetch(`${API_URL}/api/buoys/data?start_ts=${startTs}&end_ts=${endTs}`)
      .then((r) => r.json())
      .then((d) => {
        setBuoyData(d.buoys || {});
        setLoading(false);
      })
      .catch((e) => {
        console.error("Failed to fetch buoy data:", e);
        setLoading(false);
      });
  }, [sessionStart, sessionEnd]);

  // Build chart traces based on selected metric
  const { traces, layout } = useMemo(() => {
    const traces = [];

    for (const [stationId, buoy] of Object.entries(buoyData)) {
      const points = buoy.data_points || [];
      if (!points.length) continue;

      const timestamps = points.map((p) => new Date(p.timestamp));

      if (selectedMetric === "wind") {
        // Wind speed trace
        const speeds = points.map((p) => p.wind_speed_kts);
        if (speeds.some((s) => s !== undefined)) {
          traces.push({
            type: "scatter",
            mode: "lines",
            name: `${buoy.name} Wind`,
            x: timestamps,
            y: speeds,
            line: { color: buoy.color, width: 2 },
            hovertemplate: "%{y:.1f} kt<extra></extra>",
          });
        }

        // Wind gust trace (dashed)
        const gusts = points.map((p) => p.wind_gust_kts);
        if (gusts.some((g) => g !== undefined)) {
          traces.push({
            type: "scatter",
            mode: "lines",
            name: `${buoy.name} Gust`,
            x: timestamps,
            y: gusts,
            line: { color: buoy.color, width: 1, dash: "dot" },
            hovertemplate: "%{y:.1f} kt gust<extra></extra>",
            showlegend: false,
          });
        }
      } else if (selectedMetric === "wind_dir") {
        const dirs = points.map((p) => p.wind_dir);
        if (dirs.some((d) => d !== undefined)) {
          traces.push({
            type: "scatter",
            mode: "lines+markers",
            name: `${buoy.name}`,
            x: timestamps,
            y: dirs,
            line: { color: buoy.color, width: 2 },
            marker: { size: 4 },
            hovertemplate: "%{y:.0f}°<extra></extra>",
          });
        }
      } else if (selectedMetric === "pressure") {
        const pressures = points.map((p) => p.pressure_hpa);
        if (pressures.some((p) => p !== undefined)) {
          traces.push({
            type: "scatter",
            mode: "lines",
            name: `${buoy.name}`,
            x: timestamps,
            y: pressures,
            line: { color: buoy.color, width: 2 },
            hovertemplate: "%{y:.1f} hPa<extra></extra>",
          });
        }
      } else if (selectedMetric === "temperature") {
        const airTemps = points.map((p) => p.air_temp_c);
        const waterTemps = points.map((p) => p.water_temp_c);

        if (airTemps.some((t) => t !== undefined)) {
          traces.push({
            type: "scatter",
            mode: "lines",
            name: `${buoy.name} Air`,
            x: timestamps,
            y: airTemps,
            line: { color: buoy.color, width: 2 },
            hovertemplate: "%{y:.1f}°C air<extra></extra>",
          });
        }
        if (waterTemps.some((t) => t !== undefined)) {
          traces.push({
            type: "scatter",
            mode: "lines",
            name: `${buoy.name} Water`,
            x: timestamps,
            y: waterTemps,
            line: { color: buoy.color, width: 2, dash: "dash" },
            hovertemplate: "%{y:.1f}°C water<extra></extra>",
          });
        }
      } else if (selectedMetric === "waves") {
        const heights = points.map((p) => p.wave_height_m);
        if (heights.some((h) => h !== undefined)) {
          traces.push({
            type: "scatter",
            mode: "lines",
            name: `${buoy.name}`,
            x: timestamps,
            y: heights,
            line: { color: buoy.color, width: 2 },
            fill: "tozeroy",
            fillcolor: buoy.color + "20",
            hovertemplate: "%{y:.2f} m<extra></extra>",
          });
        }
      }
    }

    // Add current time cursor
    if (currentTime && traces.length) {
      const cursorTime = new Date(currentTime * 1000);
      traces.push({
        type: "scatter",
        mode: "lines",
        name: "Current",
        x: [cursorTime, cursorTime],
        y: [0, 1000],
        line: { color: "#ffffff", width: 1, dash: "dash" },
        showlegend: false,
        hoverinfo: "skip",
      });
    }

    const yAxisTitle = {
      wind: "Wind Speed (kt)",
      wind_dir: "Wind Direction (°)",
      pressure: "Pressure (hPa)",
      temperature: "Temperature (°C)",
      waves: "Wave Height (m)",
    }[selectedMetric];

    const layout = {
      title: null,
      height,
      margin: { t: 10, r: 10, b: 40, l: 50 },
      paper_bgcolor: "transparent",
      plot_bgcolor: "transparent",
      font: { color: "#9ca3af", size: 11 },
      xaxis: {
        type: "date",
        gridcolor: "#374151",
        linecolor: "#374151",
        tickformat: "%H:%M",
      },
      yaxis: {
        title: yAxisTitle,
        gridcolor: "#374151",
        linecolor: "#374151",
        zeroline: false,
      },
      legend: {
        orientation: "h",
        y: -0.2,
        x: 0.5,
        xanchor: "center",
        bgcolor: "transparent",
      },
      hovermode: "x unified",
    };

    // Adjust y-axis range for wind direction
    if (selectedMetric === "wind_dir") {
      layout.yaxis.range = [0, 360];
      layout.yaxis.tickvals = [0, 90, 180, 270, 360];
      layout.yaxis.ticktext = ["N", "E", "S", "W", "N"];
    }

    return { traces, layout };
  }, [buoyData, selectedMetric, currentTime, height]);

  // Handle click to seek timeline
  const handlePlotClick = (event) => {
    if (!onTimeChange || !event.points?.length) return;
    const clickedTime = new Date(event.points[0].x).getTime() / 1000;
    onTimeChange(clickedTime);
  };

  if (loading) {
    return (
      <div className="buoy-chart-loading" style={{ height, display: "flex", alignItems: "center", justifyContent: "center" }}>
        <span style={{ color: "#9ca3af" }}>Loading NOAA buoy data...</span>
      </div>
    );
  }

  if (!Object.keys(buoyData).length) {
    return (
      <div className="buoy-chart-empty" style={{ height, display: "flex", alignItems: "center", justifyContent: "center" }}>
        <span style={{ color: "#6b7280" }}>No buoy data available for this session</span>
      </div>
    );
  }

  return (
    <div className="buoy-chart">
      <div className="buoy-chart-controls" style={{ display: "flex", gap: "8px", marginBottom: "8px" }}>
        <button
          onClick={() => setSelectedMetric("wind")}
          className={`btn btn-sm ${selectedMetric === "wind" ? "btn-primary" : "btn-secondary"}`}
          style={{
            padding: "4px 12px",
            borderRadius: "4px",
            border: "none",
            cursor: "pointer",
            backgroundColor: selectedMetric === "wind" ? "#1da1f2" : "#374151",
            color: "white",
            fontSize: "12px",
          }}
        >
          Wind Speed
        </button>
        <button
          onClick={() => setSelectedMetric("wind_dir")}
          className={`btn btn-sm ${selectedMetric === "wind_dir" ? "btn-primary" : "btn-secondary"}`}
          style={{
            padding: "4px 12px",
            borderRadius: "4px",
            border: "none",
            cursor: "pointer",
            backgroundColor: selectedMetric === "wind_dir" ? "#1da1f2" : "#374151",
            color: "white",
            fontSize: "12px",
          }}
        >
          Wind Dir
        </button>
        <button
          onClick={() => setSelectedMetric("pressure")}
          className={`btn btn-sm ${selectedMetric === "pressure" ? "btn-primary" : "btn-secondary"}`}
          style={{
            padding: "4px 12px",
            borderRadius: "4px",
            border: "none",
            cursor: "pointer",
            backgroundColor: selectedMetric === "pressure" ? "#1da1f2" : "#374151",
            color: "white",
            fontSize: "12px",
          }}
        >
          Pressure
        </button>
        <button
          onClick={() => setSelectedMetric("temperature")}
          className={`btn btn-sm ${selectedMetric === "temperature" ? "btn-primary" : "btn-secondary"}`}
          style={{
            padding: "4px 12px",
            borderRadius: "4px",
            border: "none",
            cursor: "pointer",
            backgroundColor: selectedMetric === "temperature" ? "#1da1f2" : "#374151",
            color: "white",
            fontSize: "12px",
          }}
        >
          Temp
        </button>
        <button
          onClick={() => setSelectedMetric("waves")}
          className={`btn btn-sm ${selectedMetric === "waves" ? "btn-primary" : "btn-secondary"}`}
          style={{
            padding: "4px 12px",
            borderRadius: "4px",
            border: "none",
            cursor: "pointer",
            backgroundColor: selectedMetric === "waves" ? "#1da1f2" : "#374151",
            color: "white",
            fontSize: "12px",
          }}
        >
          Waves
        </button>
      </div>

      <Plot
        data={traces}
        layout={layout}
        config={{
          responsive: true,
          displayModeBar: false,
        }}
        onClick={handlePlotClick}
        style={{ width: "100%" }}
      />

      <div className="buoy-legend" style={{ fontSize: "11px", color: "#9ca3af", marginTop: "4px" }}>
        Data from NOAA NDBC buoys: Castle Island (wind), Boston 16NM (offshore), Mass Bay A01
      </div>
    </div>
  );
}
