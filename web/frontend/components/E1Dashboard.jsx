import React, { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import { API_URL } from "../src/config";

export default function E1Dashboard() {
  const [devices, setDevices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetch(`${API_URL}/api/e1/devices`)
      .then((r) => {
        if (!r.ok) throw new Error("Failed to load devices");
        return r.json();
      })
      .then((data) => setDevices(data.devices || []))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="panel">
        <div className="loading">Loading E1 devices...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="panel">
        <h2>E1 Fleet Data</h2>
        <div style={{ color: "var(--danger)", marginTop: 12 }}>Error: {error}</div>
      </div>
    );
  }

  if (devices.length === 0) {
    return (
      <div className="panel">
        <h2>E1 Fleet Data</h2>
        <p style={{ color: "var(--text-secondary)", marginTop: 12 }}>
          No E1 device data found in S3. Upload data from your E1 devices to get started.
        </p>
      </div>
    );
  }

  return (
    <div className="panel">
      <div className="panel-header">
        <h2>E1 Fleet Tracker Data</h2>
        <span style={{ color: "var(--text-secondary)", fontSize: 14 }}>
          {devices.length} device{devices.length !== 1 ? "s" : ""} with data
        </span>
      </div>

      <div className="grid-3">
        {devices.map((device) => (
          <Link
            key={device.device_id}
            to={`/${device.device_id}`}
            style={{ textDecoration: "none" }}
          >
            <div className="stat-card device-card">
              <div className="label">Device</div>
              <div className="value" style={{ color: "var(--accent)" }}>
                {device.device_id}
              </div>
              <div style={{ marginTop: 12 }}>
                <div style={{ fontSize: 13, color: "var(--text-secondary)" }}>
                  {device.total_sessions} session{device.total_sessions !== 1 ? "s" : ""}
                </div>
                <div style={{ fontSize: 13, color: "var(--text-secondary)" }}>
                  {device.total_files} files ({device.total_size_formatted})
                </div>
                <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 8 }}>
                  {device.first_upload} to {device.last_upload}
                </div>
              </div>
            </div>
          </Link>
        ))}
      </div>
    </div>
  );
}
