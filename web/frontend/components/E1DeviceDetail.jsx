import React, { useState, useEffect } from "react";
import { useParams, Link } from "react-router-dom";
import { API_URL } from "../src/config";

export default function E1DeviceDetail() {
  const { deviceId } = useParams();
  const [uploads, setUploads] = useState([]);
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    setLoading(true);
    const params = new URLSearchParams();
    if (startDate) params.set("start_date", startDate);
    if (endDate) params.set("end_date", endDate);

    fetch(`${API_URL}/api/e1/devices/${deviceId}/uploads?${params}`)
      .then((r) => {
        if (!r.ok) throw new Error("Failed to load uploads");
        return r.json();
      })
      .then((data) => setUploads(data.uploads || []))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [deviceId, startDate, endDate]);

  const FileTypeBadge = ({ type, count }) => {
    const colors = {
      nav: "var(--accent)",
      imu: "var(--success)",
      wind: "var(--warning)",
      rtcm3: "var(--danger)",
    };
    if (!count) return null;
    return (
      <span
        className="file-type-badge"
        style={{ background: colors[type] || "var(--border)", marginRight: 4 }}
      >
        {type}: {count}
      </span>
    );
  };

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <Link to="/e1" style={{ color: "var(--text-secondary)", textDecoration: "none" }}>
            E1 Fleet
          </Link>
          <span style={{ color: "var(--text-secondary)" }}> / </span>
          <h2 style={{ display: "inline" }}>{deviceId}</h2>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <label style={{ fontSize: 13, color: "var(--text-secondary)" }}>From:</label>
          <input
            type="date"
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
          />
          <label style={{ fontSize: 13, color: "var(--text-secondary)" }}>To:</label>
          <input
            type="date"
            value={endDate}
            onChange={(e) => setEndDate(e.target.value)}
          />
          {(startDate || endDate) && (
            <button
              onClick={() => { setStartDate(""); setEndDate(""); }}
              style={{ background: "var(--bg-secondary)", padding: "8px 12px" }}
            >
              Clear
            </button>
          )}
        </div>
      </div>

      {loading ? (
        <div className="loading">Loading uploads...</div>
      ) : error ? (
        <div style={{ color: "var(--danger)" }}>Error: {error}</div>
      ) : uploads.length === 0 ? (
        <div style={{ color: "var(--text-secondary)" }}>
          No uploads found for this device{startDate || endDate ? " in selected date range" : ""}.
        </div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>Files</th>
              <th>Total Size</th>
              <th>Processed</th>
            </tr>
          </thead>
          <tbody>
            {uploads.map((upload) => (
              <tr key={upload.date}>
                <td>
                  <Link
                    to={`/e1/${deviceId}/${upload.date}`}
                    style={{ color: "var(--accent)", textDecoration: "none" }}
                  >
                    {upload.date}
                  </Link>
                </td>
                <td>
                  <FileTypeBadge type="nav" count={upload.file_type_counts?.nav} />
                  <FileTypeBadge type="imu" count={upload.file_type_counts?.imu} />
                  <FileTypeBadge type="wind" count={upload.file_type_counts?.wind} />
                  <FileTypeBadge type="rtcm3" count={upload.file_type_counts?.rtcm3} />
                </td>
                <td style={{ color: "var(--text-secondary)" }}>
                  {upload.total_size_formatted}
                </td>
                <td>
                  {upload.has_manifest ? (
                    <span style={{ color: "var(--success)" }}>Yes</span>
                  ) : (
                    <span style={{ color: "var(--text-secondary)" }}>No</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
