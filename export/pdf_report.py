"""PDF race report generation.

Creates a multi-page PDF summarizing a race session with
charts, maps, and statistics using matplotlib.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_pdf import PdfPages


def generate_report(analysis_path: str, output_path: str | None = None):
    """Generate a PDF race report from analysis JSON.

    Args:
        analysis_path: Path to analysis.json file.
        output_path: Output PDF path. Defaults to same directory.
    """
    analysis_path = Path(analysis_path)
    data = json.loads(analysis_path.read_text())

    if output_path is None:
        output_path = analysis_path.parent / "race_report.pdf"
    else:
        output_path = Path(output_path)

    with PdfPages(str(output_path)) as pdf:
        # Page 1: Title and summary
        _page_summary(pdf, data)

        # Page 2: Maneuver analysis
        if data.get("maneuvers"):
            _page_maneuvers(pdf, data)

        # Page 3: Polar diagram
        if data.get("polar"):
            _page_polar(pdf, data)

        # Page 4: Leg performance
        if data.get("legs"):
            _page_legs(pdf, data)

    print(f"Report saved to {output_path}")
    return str(output_path)


def _page_summary(pdf, data):
    """Title page with session summary stats."""
    fig, ax = plt.subplots(figsize=(8.5, 11))
    ax.axis("off")

    stats = data.get("session_stats", {})
    summary = data.get("maneuver_summary", {})

    ax.text(0.5, 0.92, "SailFrames Race Report", fontsize=24, fontweight="bold",
            ha="center", transform=ax.transAxes)

    lines = []
    if stats.get("speed"):
        s = stats["speed"]
        lines.append(f"Max Speed: {s['max']} kts")
        lines.append(f"Avg Speed: {s['mean']} kts")
    if stats.get("apparent_wind_speed"):
        w = stats["apparent_wind_speed"]
        lines.append(f"Avg Wind: {w['mean']} kts")
    if summary.get("tacks"):
        lines.append(f"Tacks: {summary['tacks']['count']}")
    if summary.get("gybes"):
        lines.append(f"Gybes: {summary['gybes']['count']}")
    if stats.get("heel"):
        lines.append(f"Avg Heel: {stats['heel']['mean']}°")

    for i, line in enumerate(lines):
        ax.text(0.5, 0.75 - i * 0.05, line, fontsize=14, ha="center",
                transform=ax.transAxes)

    ax.text(0.5, 0.02, f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            fontsize=9, ha="center", color="gray", transform=ax.transAxes)

    pdf.savefig(fig)
    plt.close(fig)


def _page_maneuvers(pdf, data):
    """Maneuver performance charts."""
    maneuvers = data["maneuvers"]
    tacks = [m for m in maneuvers if m["maneuver_type"] == "tack"]
    gybes = [m for m in maneuvers if m["maneuver_type"] == "gybe"]

    fig, axes = plt.subplots(2, 1, figsize=(8.5, 11))

    # Speed loss scatter
    ax = axes[0]
    if tacks:
        ax.scatter(range(len(tacks)), [m["speed_loss_kts"] for m in tacks],
                   label="Tacks", color="#ffad1f", s=40)
    if gybes:
        ax.scatter(range(len(gybes)), [m["speed_loss_kts"] for m in gybes],
                   label="Gybes", color="#e0245e", s=40)
    ax.set_xlabel("Maneuver #")
    ax.set_ylabel("Speed Loss (kts)")
    ax.set_title("Speed Loss per Maneuver")
    ax.legend()
    ax.grid(True, alpha=0.3)

    # Recovery time bars
    ax = axes[1]
    labels, values, colors = [], [], []
    for i, m in enumerate(tacks):
        labels.append(f"T{i+1}")
        values.append(m["recovery_time_sec"])
        colors.append("#ffad1f")
    for i, m in enumerate(gybes):
        labels.append(f"G{i+1}")
        values.append(m["recovery_time_sec"])
        colors.append("#e0245e")
    ax.bar(labels, values, color=colors)
    ax.set_ylabel("Recovery Time (s)")
    ax.set_title("Recovery Time per Maneuver")
    ax.grid(True, alpha=0.3, axis="y")

    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)


def _page_polar(pdf, data):
    """Polar diagram page."""
    polar = data["polar"]
    fig, ax = plt.subplots(subplot_kw={"projection": "polar"}, figsize=(8.5, 11))

    colors = plt.cm.viridis(np.linspace(0, 1, len(polar)))
    for (tws, points), color in zip(sorted(polar.items()), colors):
        angles = [np.radians(p["angle"]) for p in points]
        speeds = [p["speed"] for p in points]
        ax.plot(angles, speeds, "o-", label=f"{tws} kts", color=color, markersize=3)

    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    ax.set_thetamin(0)
    ax.set_thetamax(180)
    ax.set_title("Polar Diagram", pad=20, fontsize=16)
    ax.legend(loc="upper right", bbox_to_anchor=(1.3, 1.0), fontsize=8)

    pdf.savefig(fig)
    plt.close(fig)


def _page_legs(pdf, data):
    """Leg performance summary table."""
    legs = data["legs"]
    fig, ax = plt.subplots(figsize=(8.5, 11))
    ax.axis("off")

    ax.text(0.5, 0.95, "Straight Line Performance", fontsize=18, fontweight="bold",
            ha="center", transform=ax.transAxes)

    headers = ["#", "Type", "Duration", "Avg Speed", "VMG", "TWA", "Heel"]
    rows = []
    for i, leg in enumerate(legs[:20]):  # max 20 legs
        dur = f"{int(leg['duration_sec']//60)}:{int(leg['duration_sec']%60):02d}"
        rows.append([
            str(i + 1),
            leg["leg_type"],
            dur,
            f"{leg['avg_speed_kts']} kts",
            f"{leg['avg_vmg_kts']} kts",
            f"{leg.get('avg_twa_deg', '—')}°" if leg.get("avg_twa_deg") else "—",
            f"{leg.get('avg_heel_deg', '—')}°" if leg.get("avg_heel_deg") else "—",
        ])

    table = ax.table(cellText=rows, colLabels=headers, loc="center",
                     cellLoc="center", colColours=["#e8e8e8"] * len(headers))
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.4)

    pdf.savefig(fig)
    plt.close(fig)


def main():
    if len(sys.argv) < 2:
        print("Usage: python pdf_report.py <analysis.json> [output.pdf]")
        sys.exit(1)
    output = sys.argv[2] if len(sys.argv) > 2 else None
    generate_report(sys.argv[1], output)


if __name__ == "__main__":
    main()
