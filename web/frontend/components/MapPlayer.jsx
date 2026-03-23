import React, { useEffect, useRef, useMemo } from "react";
import L from "leaflet";

export default function MapPlayer({ gps, maneuvers, currentTime, onTimeChange }) {
  const mapRef = useRef(null);
  const mapInstance = useRef(null);
  const trackLine = useRef(null);
  const boatMarker = useRef(null);
  const maneuverMarkers = useRef([]);

  // Initialize map
  useEffect(() => {
    if (mapInstance.current || !mapRef.current) return;

    mapInstance.current = L.map(mapRef.current, {
      zoomControl: true,
      attributionControl: false,
    }).setView([42.35, -71.05], 13); // Boston Harbor default

    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
      maxZoom: 19,
    }).addTo(mapInstance.current);

    return () => {
      mapInstance.current?.remove();
      mapInstance.current = null;
    };
  }, []);

  // Draw GPS track
  useEffect(() => {
    const map = mapInstance.current;
    if (!map || !gps?.length) return;

    // Remove old track
    if (trackLine.current) map.removeLayer(trackLine.current);
    maneuverMarkers.current.forEach((m) => map.removeLayer(m));
    maneuverMarkers.current = [];

    const points = gps.map((p) => [p.lat || p.latitude, p.lon || p.longitude]);
    trackLine.current = L.polyline(points, {
      color: "#1da1f2",
      weight: 2,
      opacity: 0.8,
    }).addTo(map);

    map.fitBounds(trackLine.current.getBounds(), { padding: [20, 20] });

    // Maneuver markers
    if (maneuvers) {
      maneuvers.forEach((m) => {
        if (!m.start_lat || !m.start_lon) return;
        const color = m.maneuver_type === "tack" ? "#ffad1f" : "#e0245e";
        const marker = L.circleMarker([m.start_lat, m.start_lon], {
          radius: 5,
          color,
          fillColor: color,
          fillOpacity: 0.8,
        }).addTo(map);
        marker.bindPopup(
          `<b>${m.maneuver_type.toUpperCase()}</b><br/>` +
          `Speed loss: ${m.speed_loss_kts} kts<br/>` +
          `Recovery: ${m.recovery_time_sec}s`
        );
        maneuverMarkers.current.push(marker);
      });
    }
  }, [gps, maneuvers]);

  // Update boat position marker
  useEffect(() => {
    const map = mapInstance.current;
    if (!map || !gps?.length || !currentTime) return;

    // Find closest GPS point to current time
    let closest = gps[0];
    let minDiff = Infinity;
    for (const p of gps) {
      const diff = Math.abs(p.timestamp - currentTime);
      if (diff < minDiff) {
        minDiff = diff;
        closest = p;
      }
    }

    const lat = closest.lat || closest.latitude;
    const lon = closest.lon || closest.longitude;

    if (boatMarker.current) {
      boatMarker.current.setLatLng([lat, lon]);
    } else {
      const boatIcon = L.divIcon({
        html: `<svg width="20" height="20" viewBox="0 0 20 20">
          <polygon points="10,2 17,18 10,14 3,18" fill="#1da1f2" stroke="#fff" stroke-width="1"/>
        </svg>`,
        iconSize: [20, 20],
        iconAnchor: [10, 10],
        className: "",
      });
      boatMarker.current = L.marker([lat, lon], { icon: boatIcon }).addTo(map);
    }
  }, [gps, currentTime]);

  // Click on track to seek
  const handleMapClick = (e) => {
    if (!gps?.length || !onTimeChange) return;
    // Find nearest GPS point to click location
    let nearest = gps[0];
    let minDist = Infinity;
    for (const p of gps) {
      const lat = p.lat || p.latitude;
      const lon = p.lon || p.longitude;
      const dist = Math.hypot(lat - e.latlng.lat, lon - e.latlng.lng);
      if (dist < minDist) {
        minDist = dist;
        nearest = p;
      }
    }
    onTimeChange(nearest.timestamp);
  };

  useEffect(() => {
    mapInstance.current?.on("click", handleMapClick);
    return () => mapInstance.current?.off("click", handleMapClick);
  });

  return <div ref={mapRef} className="map-container" style={{ height: 400 }} />;
}
