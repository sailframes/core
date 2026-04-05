import React from "react";
import { Routes, Route } from "react-router-dom";
import E1Dashboard from "../components/E1Dashboard";
import E1DeviceDetail from "../components/E1DeviceDetail";
import E1DateDetail from "../components/E1DateDetail";

export default function App() {
  return (
    <div style={{ minHeight: "100vh", background: "var(--bg-primary)", padding: "20px" }}>
      <header style={{ marginBottom: "20px" }}>
        <h1 style={{ color: "var(--text-primary)", margin: 0 }}>SailFrames</h1>
        <p style={{ color: "var(--text-secondary)", margin: "4px 0 0 0" }}>Fleet Data</p>
      </header>
      <Routes>
        <Route path="/" element={<E1Dashboard />} />
        <Route path="/:deviceId" element={<E1DeviceDetail />} />
        <Route path="/:deviceId/:date" element={<E1DateDetail />} />
      </Routes>
    </div>
  );
}
