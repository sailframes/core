/**
 * Charting utilities — Plotly/D3 wrapper for SailFrames dashboard.
 *
 * Provides consistent theming and common chart configurations
 * used across multiple components.
 */

export const THEME = {
  bg: "rgba(0,0,0,0)",
  text: "#e4e8ec",
  textSecondary: "#8899a6",
  grid: "#2f3f4f",
  accent: "#1da1f2",
  success: "#17bf63",
  warning: "#ffad1f",
  danger: "#e0245e",
};

export const DEFAULT_LAYOUT = {
  paper_bgcolor: THEME.bg,
  plot_bgcolor: THEME.bg,
  font: { color: THEME.text, family: "-apple-system, BlinkMacSystemFont, sans-serif" },
  margin: { t: 30, b: 40, l: 50, r: 20 },
};

export const DEFAULT_AXIS = {
  color: THEME.textSecondary,
  gridcolor: THEME.grid,
  zerolinecolor: THEME.grid,
};

export const DEFAULT_CONFIG = {
  responsive: true,
  displayModeBar: false,
};

/**
 * Create a time-series layout with consistent styling.
 */
export function timeSeriesLayout(title, yLabel, height = 300) {
  return {
    ...DEFAULT_LAYOUT,
    title: title ? { text: title, font: { size: 14, color: THEME.text } } : undefined,
    xaxis: { ...DEFAULT_AXIS, type: "date", title: "" },
    yaxis: { ...DEFAULT_AXIS, title: yLabel },
    height,
  };
}

/**
 * Create a scatter trace with SailFrames styling.
 */
export function scatterTrace(x, y, name, color, options = {}) {
  return {
    type: "scatter",
    mode: options.mode || "lines",
    name,
    x,
    y,
    line: { color, width: options.width || 1.5 },
    marker: { color, size: options.markerSize || 4 },
    ...options,
  };
}

/**
 * Downsample data for performance when rendering large datasets.
 */
export function downsample(data, maxPoints = 2000) {
  if (data.length <= maxPoints) return data;
  const step = Math.ceil(data.length / maxPoints);
  return data.filter((_, i) => i % step === 0);
}

/**
 * Convert Unix timestamps to Date objects for Plotly.
 */
export function timestampsToDate(timestamps) {
  return timestamps.map((t) => new Date(t * 1000));
}

/**
 * Format a number to fixed decimal places with unit.
 */
export function formatValue(value, decimals = 1, unit = "") {
  if (value == null || isNaN(value)) return "—";
  return `${Number(value).toFixed(decimals)}${unit ? " " + unit : ""}`;
}
