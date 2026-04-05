import React from "react";

const FILE_TYPE_COLORS = {
  nav: "var(--accent)",
  imu: "var(--success)",
  wind: "var(--warning)",
  rtcm3: "var(--danger)",
  processed: "var(--border)",
  unknown: "var(--border)",
};

export default function E1FileList({ files, onDownload }) {
  const allFiles = [
    ...files.raw.map((f) => ({ ...f, category: "raw" })),
    ...files.processed.map((f) => ({ ...f, category: "processed", file_type: "processed" })),
  ].sort((a, b) => a.filename.localeCompare(b.filename));

  if (allFiles.length === 0) {
    return (
      <div style={{ color: "var(--text-secondary)", padding: 20 }}>
        No files found for this date.
      </div>
    );
  }

  return (
    <table>
      <thead>
        <tr>
          <th>Filename</th>
          <th>Type</th>
          <th>Size</th>
          <th>Modified</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {allFiles.map((file) => (
          <tr key={file.key} className="file-row">
            <td style={{ fontFamily: "monospace", fontSize: 13 }}>{file.filename}</td>
            <td>
              <span
                className="file-type-badge"
                style={{
                  background: FILE_TYPE_COLORS[file.file_type] || FILE_TYPE_COLORS.unknown,
                  color: file.file_type === "wind" ? "#000" : "#fff",
                }}
              >
                {file.file_type?.toUpperCase()}
              </span>
            </td>
            <td style={{ color: "var(--text-secondary)" }}>{file.size_formatted}</td>
            <td style={{ color: "var(--text-secondary)", fontSize: 13 }}>
              {file.last_modified?.split("T")[0]}
            </td>
            <td>
              <button
                onClick={() => onDownload(file)}
                className="download-btn"
                style={{ background: "var(--bg-secondary)", border: "1px solid var(--border)" }}
              >
                Download
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
