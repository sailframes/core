import React, { useState, useMemo } from "react";

export default function StraightLineTable({ legs, comparison }) {
  const [sortBy, setSortBy] = useState("avg_vmg_kts");
  const [filterType, setFilterType] = useState("all");

  const filteredLegs = useMemo(() => {
    if (!legs) return [];
    let result = [...legs];
    if (filterType !== "all") {
      result = result.filter((l) => l.leg_type === filterType);
    }
    result.sort((a, b) => (b[sortBy] || 0) - (a[sortBy] || 0));
    return result;
  }, [legs, sortBy, filterType]);

  if (!legs?.length) {
    return (
      <div className="panel">
        <h2>Straight Line Performance</h2>
        <div className="loading">No leg data available</div>
      </div>
    );
  }

  const typeColors = { upwind: "#1da1f2", downwind: "#17bf63", reach: "#ffad1f" };

  return (
    <div>
      {/* Comparison summary */}
      {comparison && Object.keys(comparison).length > 0 && (
        <div className="panel">
          <div className="panel-header">
            <h2>Leg Comparison</h2>
          </div>
          <div className="grid-3">
            {Object.entries(comparison).map(([type, stats]) => (
              <div className="stat-card" key={type} style={{ borderLeft: `3px solid ${typeColors[type] || "#8899a6"}` }}>
                <div className="label">{type}</div>
                <div style={{ marginTop: 8, fontSize: 13 }}>
                  <div>{stats.count} legs · {stats.total_distance_nm} nm</div>
                  <div>Avg Speed: <b>{stats.avg_speed_kts}</b> kts</div>
                  <div>Max Speed: <b>{stats.max_speed_kts}</b> kts</div>
                  <div>Avg VMG: <b>{stats.avg_vmg_kts}</b> kts</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Detailed table */}
      <div className="panel">
        <div className="panel-header">
          <h2>All Legs</h2>
          <div style={{ display: "flex", gap: 8 }}>
            <select value={filterType} onChange={(e) => setFilterType(e.target.value)}>
              <option value="all">All Types</option>
              <option value="upwind">Upwind</option>
              <option value="downwind">Downwind</option>
              <option value="reach">Reach</option>
            </select>
            <select value={sortBy} onChange={(e) => setSortBy(e.target.value)}>
              <option value="avg_vmg_kts">Sort: VMG</option>
              <option value="avg_speed_kts">Sort: Speed</option>
              <option value="duration_sec">Sort: Duration</option>
              <option value="distance_nm">Sort: Distance</option>
            </select>
          </div>
        </div>
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Type</th>
              <th>Duration</th>
              <th>Distance</th>
              <th>Avg Speed</th>
              <th>Max Speed</th>
              <th>VMG</th>
              <th>TWA</th>
              <th>Heel</th>
              <th>Heading STD</th>
            </tr>
          </thead>
          <tbody>
            {filteredLegs.map((leg, i) => (
              <tr key={i}>
                <td>{i + 1}</td>
                <td>
                  <span style={{
                    background: typeColors[leg.leg_type] || "#8899a6",
                    color: "#000",
                    padding: "2px 8px",
                    borderRadius: 4,
                    fontSize: 11,
                    fontWeight: 600,
                  }}>
                    {leg.leg_type}
                  </span>
                </td>
                <td>{formatDuration(leg.duration_sec)}</td>
                <td>{leg.distance_nm} nm</td>
                <td>{leg.avg_speed_kts} kts</td>
                <td>{leg.max_speed_kts} kts</td>
                <td><b>{leg.avg_vmg_kts}</b> kts</td>
                <td>{leg.avg_twa_deg ?? "—"}°</td>
                <td>{leg.avg_heel_deg ?? "—"}°</td>
                <td>{leg.std_heading_deg}°</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function formatDuration(sec) {
  if (!sec) return "—";
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}
