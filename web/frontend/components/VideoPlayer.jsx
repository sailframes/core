import React, { useRef, useEffect, useState } from "react";
import Hls from "hls.js";
import { API_URL } from "../src/config";

export default function VideoPlayer({ session, currentTime, onTimeChange }) {
  const videoRef = useRef(null);
  const hlsRef = useRef(null);
  const [videoInfo, setVideoInfo] = useState(null);
  const [activeCamera, setActiveCamera] = useState("cockpit");

  // Load video info when session changes
  useEffect(() => {
    if (!session) return;
    fetch(`${API_URL}/api/video/${session.device_id}/${session.date}`)
      .then((r) => r.json())
      .then((data) => setVideoInfo(data.streams || data.cameras || {}))
      .catch(() => setVideoInfo(null));
  }, [session]);

  // Initialize video (HLS or direct)
  useEffect(() => {
    const video = videoRef.current;
    const cam = videoInfo?.[activeCamera];
    if (!video || !cam) return;

    // Clean up previous HLS instance
    if (hlsRef.current) {
      hlsRef.current.destroy();
      hlsRef.current = null;
    }

    // Direct video URL (e.g., MP4/LRV files)
    if (cam.direct_url) {
      video.src = cam.direct_url;
      return;
    }

    // HLS playlist
    if (cam.playlist_url) {
      if (Hls.isSupported()) {
        const hls = new Hls({ enableWorker: true });
        hls.loadSource(cam.playlist_url);
        hls.attachMedia(video);
        hlsRef.current = hls;
      } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = cam.playlist_url;
      }
    }

    return () => {
      hlsRef.current?.destroy();
      hlsRef.current = null;
    };
  }, [videoInfo, activeCamera]);

  // Sync video to currentTime
  useEffect(() => {
    const video = videoRef.current;
    const cam = videoInfo?.[activeCamera];
    if (!video || !cam?.start_time || !currentTime) return;

    const videoTime = currentTime - cam.start_time;
    if (Math.abs(video.currentTime - videoTime) > 1) {
      video.currentTime = Math.max(0, videoTime);
    }
  }, [currentTime, videoInfo, activeCamera]);

  // Broadcast time changes from video
  const handleTimeUpdate = () => {
    const video = videoRef.current;
    const cam = videoInfo?.[activeCamera];
    if (!video || !cam?.start_time || !onTimeChange) return;
    onTimeChange(cam.start_time + video.currentTime);
  };

  if (!session) {
    return (
      <div className="video-container" style={{ height: 300, display: "flex", alignItems: "center", justifyContent: "center" }}>
        <span style={{ color: "var(--text-secondary)" }}>No session selected</span>
      </div>
    );
  }

  const cameras = videoInfo ? Object.keys(videoInfo) : [];

  return (
    <div>
      <div className="panel-header">
        <h2>Video</h2>
        {cameras.length > 1 && (
          <select value={activeCamera} onChange={(e) => setActiveCamera(e.target.value)}>
            {cameras.map((cam) => (
              <option key={cam} value={cam}>{cam}</option>
            ))}
          </select>
        )}
      </div>
      <div className="video-container">
        <video
          ref={videoRef}
          controls
          onTimeUpdate={handleTimeUpdate}
          style={{ width: "100%", background: "#000" }}
        />
      </div>
    </div>
  );
}
