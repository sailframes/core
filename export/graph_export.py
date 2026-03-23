"""Graph and chart export to PNG/clipboard.

Renders analysis charts as standalone PNG images for sharing,
presentations, or clipboard copy.
"""

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# Dark theme matching the web dashboard
DARK_THEME = {
    "figure.facecolor": "#0f1923",
    "axes.facecolor": "#1e2d3d",
    "axes.edgecolor": "#2f3f4f",
    "axes.labelcolor": "#e4e8ec",
    "text.color": "#e4e8ec",
    "xtick.color": "#8899a6",
    "ytick.color": "#8899a6",
    "grid.color": "#2f3f4f",
    "grid.alpha": 0.5,
}


def export_polar(analysis_path: str, output_path: str | None = None) -> str:
    """Export polar diagram as PNG."""
    data = json.loads(Path(analysis_path).read_text())
    polar = data.get("polar", {})

    with plt.rc_context(DARK_THEME):
        fig, ax = plt.subplots(subplot_kw={"projection": "polar"}, figsize=(8, 8))

        colors = plt.cm.cool(np.linspace(0, 1, max(len(polar), 1)))
        for (tws, points), color in zip(sorted(polar.items()), colors):
            angles = [np.radians(p["angle"]) for p in points]
            speeds = [p["speed"] for p in points]
            ax.plot(angles, speeds, "o-", label=f"{tws} kts", color=color, markersize=4)

        ax.set_theta_zero_location("N")
        ax.set_theta_direction(-1)
        ax.set_thetamin(0)
        ax.set_thetamax(180)
        ax.set_title("Polar Diagram", pad=20, fontsize=16, color="#e4e8ec")
        ax.legend(loc="upper right", bbox_to_anchor=(1.3, 1.0), fontsize=9)

    output = output_path or str(Path(analysis_path).parent / "polar.png")
    fig.savefig(output, dpi=150, bbox_inches="tight", facecolor="#0f1923")
    plt.close(fig)
    print(f"Polar exported to {output}")
    return output


def export_maneuvers(analysis_path: str, output_path: str | None = None) -> str:
    """Export maneuver analysis chart as PNG."""
    data = json.loads(Path(analysis_path).read_text())
    maneuvers = data.get("maneuvers", [])

    tacks = [m for m in maneuvers if m["maneuver_type"] == "tack"]
    gybes = [m for m in maneuvers if m["maneuver_type"] == "gybe"]

    with plt.rc_context(DARK_THEME):
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

        # Speed loss
        if tacks:
            ax1.bar(range(len(tacks)), [m["speed_loss_kts"] for m in tacks],
                    label="Tacks", color="#ffad1f", alpha=0.8)
        if gybes:
            offset = len(tacks)
            ax1.bar(range(offset, offset + len(gybes)),
                    [m["speed_loss_kts"] for m in gybes],
                    label="Gybes", color="#e0245e", alpha=0.8)
        ax1.set_xlabel("Maneuver #")
        ax1.set_ylabel("Speed Loss (kts)")
        ax1.set_title("Speed Loss")
        ax1.legend()
        ax1.grid(True)

        # Recovery time
        all_maneuvers = tacks + gybes
        colors = ["#ffad1f"] * len(tacks) + ["#e0245e"] * len(gybes)
        labels = [f"T{i+1}" for i in range(len(tacks))] + [f"G{i+1}" for i in range(len(gybes))]
        ax2.bar(labels, [m["recovery_time_sec"] for m in all_maneuvers], color=colors)
        ax2.set_ylabel("Recovery Time (s)")
        ax2.set_title("Recovery Time")
        ax2.grid(True, axis="y")

    output = output_path or str(Path(analysis_path).parent / "maneuvers.png")
    fig.savefig(output, dpi=150, bbox_inches="tight", facecolor="#0f1923")
    plt.close(fig)
    print(f"Maneuvers exported to {output}")
    return output


def export_speed_trace(analysis_path: str, output_path: str | None = None) -> str:
    """Export speed time-series as PNG."""
    data = json.loads(Path(analysis_path).read_text())
    vmg_series = data.get("vmg_series", [])

    if not vmg_series:
        print("No VMG series data to plot")
        return ""

    with plt.rc_context(DARK_THEME):
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)

        timestamps = [v["timestamp"] for v in vmg_series]
        t0 = timestamps[0]
        minutes = [(t - t0) / 60 for t in timestamps]

        # Boat speed
        ax1.plot(minutes, [v["boat_speed_kts"] for v in vmg_series],
                 color="#1da1f2", linewidth=1, label="Boat Speed")
        ax1.set_ylabel("Speed (kts)")
        ax1.set_title("Boat Speed & VMG")
        ax1.legend()
        ax1.grid(True)

        # VMG
        ax2.plot(minutes, [v["vmg_kts"] for v in vmg_series],
                 color="#17bf63", linewidth=1, label="VMG")
        ax2.set_xlabel("Time (minutes)")
        ax2.set_ylabel("VMG (kts)")
        ax2.legend()
        ax2.grid(True)

    output = output_path or str(Path(analysis_path).parent / "speed_trace.png")
    fig.savefig(output, dpi=150, bbox_inches="tight", facecolor="#0f1923")
    plt.close(fig)
    print(f"Speed trace exported to {output}")
    return output


def export_all(analysis_path: str, output_dir: str | None = None):
    """Export all charts from an analysis file."""
    analysis_path = Path(analysis_path)
    out_dir = Path(output_dir) if output_dir else analysis_path.parent

    exports = []
    data = json.loads(analysis_path.read_text())

    if data.get("polar"):
        exports.append(export_polar(str(analysis_path), str(out_dir / "polar.png")))
    if data.get("maneuvers"):
        exports.append(export_maneuvers(str(analysis_path), str(out_dir / "maneuvers.png")))
    if data.get("vmg_series"):
        exports.append(export_speed_trace(str(analysis_path), str(out_dir / "speed_trace.png")))

    return exports


def main():
    if len(sys.argv) < 2:
        print("Usage: python graph_export.py <analysis.json> [chart_type] [output.png]")
        print("  chart_type: polar, maneuvers, speed, all (default: all)")
        sys.exit(1)

    analysis = sys.argv[1]
    chart_type = sys.argv[2] if len(sys.argv) > 2 else "all"
    output = sys.argv[3] if len(sys.argv) > 3 else None

    exporters = {
        "polar": export_polar,
        "maneuvers": export_maneuvers,
        "speed": export_speed_trace,
        "all": lambda p, _: export_all(p),
    }

    if chart_type not in exporters:
        print(f"Unknown chart type: {chart_type}")
        sys.exit(1)

    exporters[chart_type](analysis, output)


if __name__ == "__main__":
    main()
