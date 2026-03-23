import React, { useState, useEffect } from "react";

export default function BoatProfiles() {
  const [boats, setBoats] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch("/api/boats")
      .then((r) => r.json())
      .then((data) => setBoats(data.boats || []))
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return <div className="loading">Loading boat profiles...</div>;
  }

  if (!boats.length) {
    return (
      <div className="panel">
        <div className="panel-header">
          <h2>Boat Profiles</h2>
        </div>
        <p style={{ color: "var(--text-secondary)" }}>
          No boat profiles configured. Boat profiles are loaded from the
          processed data in S3.
        </p>
        <div style={{ marginTop: 16, padding: 16, background: "var(--bg-secondary)", borderRadius: 6, fontSize: 13 }}>
          <div style={{ color: "var(--text-secondary)", marginBottom: 8 }}>Expected fleet:</div>
          <div>Sonar 23 — Keelboat, 3-person crew</div>
          <div>J/80 — Performance keelboat, 4-5 person crew</div>
        </div>
      </div>
    );
  }

  return (
    <div className="panel">
      <div className="panel-header">
        <h2>Boat Profiles</h2>
      </div>
      <div className="grid-2">
        {boats.map((boat) => (
          <div className="stat-card" key={boat.boat_id}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "start" }}>
              <div>
                <div style={{ fontSize: 16, fontWeight: 600 }}>{boat.name}</div>
                <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>{boat.boat_class}</div>
              </div>
              {boat.sail_number && (
                <div style={{
                  background: "var(--accent)",
                  color: "#fff",
                  padding: "2px 8px",
                  borderRadius: 4,
                  fontSize: 12,
                  fontWeight: 600,
                }}>
                  {boat.sail_number}
                </div>
              )}
            </div>
            <div style={{ marginTop: 12, fontSize: 13, color: "var(--text-secondary)" }}>
              {boat.jib_type && <div>Jib: {boat.jib_type}</div>}
              {boat.main_type && <div>Main: {boat.main_type}</div>}
              {boat.crew_weight_kg && <div>Crew weight: {boat.crew_weight_kg} kg</div>}
              {boat.notes && <div style={{ marginTop: 4, fontStyle: "italic" }}>{boat.notes}</div>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
