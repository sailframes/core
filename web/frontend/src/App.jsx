import React, { useState, useEffect, useCallback } from "react";
import { NavLink, Routes, Route } from "react-router-dom";
import { API_URL } from "./config";
import SessionBrowser from "../components/SessionBrowser";
import MapPlayer from "../components/MapPlayer";
import VideoPlayer from "../components/VideoPlayer";
import PolarDiagram from "../components/PolarDiagram";
import ManeuverCharts from "../components/ManeuverCharts";
import ViolinPlots from "../components/ViolinPlots";
import CorrelationPlots from "../components/CorrelationPlots";
import StraightLineTable from "../components/StraightLineTable";
import RigAnalyzer from "../components/RigAnalyzer";
import BoatProfiles from "../components/BoatProfiles";
import Leaderboard from "../components/Leaderboard";

export default function App() {
  const [sessions, setSessions] = useState([]);
  const [activeSession, setActiveSession] = useState(null);
  const [sensorData, setSensorData] = useState(null);
  const [analysis, setAnalysis] = useState(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    fetch(`${API_URL}/api/sessions`)
      .then((r) => r.json())
      .then((data) => setSessions(data.sessions || []))
      .catch(console.error);
  }, []);

  const loadSession = useCallback(async (session) => {
    setLoading(true);
    setActiveSession(session);
    const { device_id, date } = session;

    try {
      const [dataRes, analysisRes] = await Promise.all([
        fetch(`${API_URL}/api/data/${device_id}/${date}`).then((r) => r.json()),
        fetch(`${API_URL}/api/analysis/${device_id}/${date}`).then((r) => r.json()).catch(() => null),
      ]);
      setSensorData(dataRes);
      setAnalysis(analysisRes);
      if (dataRes.gps?.length) {
        setCurrentTime(dataRes.gps[0].timestamp);
      }
    } catch (err) {
      console.error("Failed to load session:", err);
    } finally {
      setLoading(false);
    }
  }, []);

  const handleTimeChange = useCallback((time) => {
    setCurrentTime(time);
  }, []);

  const navLinks = [
    { to: "/", label: "Overview" },
    { to: "/map", label: "Map & Replay" },
    { to: "/maneuvers", label: "Maneuvers" },
    { to: "/legs", label: "Straight Lines" },
    { to: "/polar", label: "Polar Diagram" },
    { to: "/stats", label: "Statistics" },
    { to: "/correlations", label: "Correlations" },
    { to: "/rig", label: "Rig Analysis" },
    { to: "/boats", label: "Boat Profiles" },
    { to: "/leaderboard", label: "Leaderboard" },
  ];

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="sidebar-header">
          <h1>SailFrames</h1>
          <p>Race Analysis Dashboard</p>
        </div>
        <nav>
          {navLinks.map(({ to, label }) => (
            <NavLink key={to} to={to} end={to === "/"}>
              {label}
            </NavLink>
          ))}
        </nav>
        <div style={{ padding: "12px", borderTop: "1px solid var(--border)", marginTop: "auto" }}>
          <SessionBrowser
            sessions={sessions}
            activeSession={activeSession}
            onSelect={loadSession}
          />
        </div>
      </aside>

      <main className="main-content">
        {loading && <div className="loading">Loading session data...</div>}

        <Routes>
          <Route
            path="/"
            element={
              <OverviewPage
                session={activeSession}
                analysis={analysis}
                sensorData={sensorData}
              />
            }
          />
          <Route
            path="/map"
            element={
              <div>
                <div className="grid-2">
                  <div className="panel">
                    <MapPlayer
                      gps={sensorData?.gps}
                      maneuvers={analysis?.maneuvers}
                      currentTime={currentTime}
                      onTimeChange={handleTimeChange}
                    />
                  </div>
                  <div className="panel">
                    <VideoPlayer
                      session={activeSession}
                      currentTime={currentTime}
                      onTimeChange={handleTimeChange}
                    />
                  </div>
                </div>
              </div>
            }
          />
          <Route
            path="/maneuvers"
            element={
              <div>
                <ManeuverCharts maneuvers={analysis?.maneuvers} summary={analysis?.maneuver_summary} />
                <ViolinPlots data={analysis?.violin} />
              </div>
            }
          />
          <Route
            path="/legs"
            element={<StraightLineTable legs={analysis?.legs} comparison={analysis?.leg_comparison} />}
          />
          <Route
            path="/polar"
            element={<PolarDiagram data={analysis?.polar} />}
          />
          <Route
            path="/stats"
            element={<ViolinPlots data={analysis?.violin} />}
          />
          <Route
            path="/correlations"
            element={<CorrelationPlots data={analysis?.correlations} />}
          />
          <Route
            path="/rig"
            element={<RigAnalyzer session={activeSession} analysis={analysis} />}
          />
          <Route path="/boats" element={<BoatProfiles />} />
          <Route path="/leaderboard" element={<Leaderboard />} />
        </Routes>
      </main>
    </div>
  );
}

function OverviewPage({ session, analysis, sensorData }) {
  if (!session) {
    return (
      <div className="panel">
        <h2>Welcome to SailFrames Analysis</h2>
        <p style={{ color: "var(--text-secondary)", marginTop: 12 }}>
          Select a session from the sidebar to begin analysis.
        </p>
      </div>
    );
  }

  const stats = analysis?.session_stats || {};
  const summary = analysis?.maneuver_summary || {};

  return (
    <div>
      <div className="panel">
        <div className="panel-header">
          <h2>Session: {session.date} — {session.device_id}</h2>
        </div>
        <div className="grid-3">
          <div className="stat-card">
            <div className="label">Max Speed</div>
            <div className="value">
              {stats.speed?.max ?? "—"} <span className="unit">kts</span>
            </div>
          </div>
          <div className="stat-card">
            <div className="label">Avg Speed</div>
            <div className="value">
              {stats.speed?.mean ?? "—"} <span className="unit">kts</span>
            </div>
          </div>
          <div className="stat-card">
            <div className="label">Avg Wind</div>
            <div className="value">
              {stats.apparent_wind_speed?.mean ?? "—"} <span className="unit">kts</span>
            </div>
          </div>
          <div className="stat-card">
            <div className="label">Tacks</div>
            <div className="value">{summary.tacks?.count ?? 0}</div>
          </div>
          <div className="stat-card">
            <div className="label">Gybes</div>
            <div className="value">{summary.gybes?.count ?? 0}</div>
          </div>
          <div className="stat-card">
            <div className="label">Avg Heel</div>
            <div className="value">
              {stats.heel?.mean ?? "—"} <span className="unit">°</span>
            </div>
          </div>
        </div>
      </div>

      {analysis?.legs && (
        <div className="panel">
          <div className="panel-header">
            <h2>Leg Summary</h2>
          </div>
          <div className="grid-3">
            {Object.entries(analysis.leg_comparison || {}).map(([type, data]) => (
              <div className="stat-card" key={type}>
                <div className="label">{type} legs</div>
                <div className="value">
                  {data.count} <span className="unit">legs</span>
                </div>
                <div style={{ fontSize: 13, color: "var(--text-secondary)", marginTop: 4 }}>
                  Avg {data.avg_speed_kts} kts · VMG {data.avg_vmg_kts} kts
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
