import React, { useEffect, useRef, useState, useMemo } from "react";
import L from "leaflet";

const API_URL = import.meta.env.VITE_API_URL || "";

// Wind direction arrow SVG
const windArrowSvg = (dir, speed, color) => {
  const rotation = dir || 0;
  const opacity = speed ? 1 : 0.3;
  return `
    <svg width="32" height="32" viewBox="0 0 32 32" style="transform: rotate(${rotation}deg)">
      <circle cx="16" cy="16" r="14" fill="${color}" fill-opacity="0.2" stroke="${color}" stroke-width="2"/>
      <polygon points="16,4 20,16 16,13 12,16" fill="${color}" fill-opacity="${opacity}"/>
      <text x="16" y="26" text-anchor="middle" fill="white" font-size="8" font-weight="bold">
        ${speed ? Math.round(speed) : "?"}
      </text>
    </svg>
  `;
};

export default function MapPlayer({
  gps,
  maneuvers,
  currentTime,
  onTimeChange,
  sessionStart,
  sessionEnd,
}) {
  const mapRef = useRef(null);
  const mapInstance = useRef(null);
  const trackLine = useRef(null);
  const boatMarker = useRef(null);
  const maneuverMarkers = useRef([]);
  const buoyMarkers = useRef({});

  const [buoys, setBuoys] = useState([]);
  const [buoyData, setBuoyData] = useState({});
  const [buoySnapshot, setBuoySnapshot] = useState({});

  // Fetch buoy metadata
  useEffect(() => {
    fetch(`${API_URL}/api/buoys`)
      .then((r) => r.json())
      .then((d) => setBuoys(d.buoys || []))
      .catch((e) => console.error("Failed to fetch buoys:", e));
  }, []);

  // Fetch buoy data for session time range
  useEffect(() => {
    if (!sessionStart || !sessionEnd) return;

    const startTs = new Date(sessionStart).getTime() / 1000;
    const endTs = new Date(sessionEnd).getTime() / 1000;

    fetch(`${API_URL}/api/buoys/data?start_ts=${startTs}&end_ts=${endTs}`)
      .then((r) => r.json())
      .then((d) => setBuoyData(d.buoys || {}))
      .catch((e) => console.error("Failed to fetch buoy data:", e));
  }, [sessionStart, sessionEnd]);

  // Update buoy snapshot when time changes
  useEffect(() => {
    if (!currentTime || !Object.keys(buoyData).length) return;

    // Interpolate values locally instead of API call for performance
    const snapshot = {};
    for (const [stationId, buoy] of Object.entries(buoyData)) {
      const points = buoy.data_points || [];
      if (!points.length) continue;

      // Find surrounding points
      let before = null;
      let after = null;
      for (const p of points) {
        if (p.unix_ts <= currentTime) before = p;
        else if (!after) after = p;
      }

      const interpolate = (field) => {
        if (!before && !after) return null;
        if (!before) return after[field];
        if (!after) return before[field];
        if (!(field in before) || !(field in after)) return before[field] || after[field];

        const ratio = (currentTime - before.unix_ts) / (after.unix_ts - before.unix_ts);
        return before[field] + ratio * (after[field] - before[field]);
      };

      snapshot[stationId] = {
        ...buoy,
        wind_dir: interpolate("wind_dir"),
        wind_speed_kts: interpolate("wind_speed_kts"),
        wind_gust_kts: interpolate("wind_gust_kts"),
        pressure_hpa: interpolate("pressure_hpa"),
        air_temp_c: interpolate("air_temp_c"),
        water_temp_c: interpolate("water_temp_c"),
        wave_height_m: interpolate("wave_height_m"),
      };
    }
    setBuoySnapshot(snapshot);
  }, [currentTime, buoyData]);

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

  // Add/update buoy markers
  useEffect(() => {
    const map = mapInstance.current;
    if (!map || !buoys.length) return;

    buoys.forEach((buoy) => {
      const snapshot = buoySnapshot[buoy.station_id] || {};
      const windDir = snapshot.wind_dir;
      const windSpeed = snapshot.wind_speed_kts;
      const pressure = snapshot.pressure_hpa;
      const airTemp = snapshot.air_temp_c;
      const waterTemp = snapshot.water_temp_c;
      const waveHeight = snapshot.wave_height_m;

      // Create or update marker
      const icon = L.divIcon({
        html: windArrowSvg(windDir, windSpeed, buoy.color),
        iconSize: [32, 32],
        iconAnchor: [16, 16],
        className: "buoy-marker",
      });

      // Build popup content
      let popup = `<div style="min-width: 120px">
        <b style="color: ${buoy.color}">${buoy.name}</b><br/>
        <small>${buoy.station_id}</small><hr style="margin: 4px 0"/>`;

      if (windDir !== undefined && windSpeed !== undefined) {
        popup += `<b>Wind:</b> ${Math.round(windDir)}° @ ${windSpeed.toFixed(1)} kt<br/>`;
      }
      if (snapshot.wind_gust_kts) {
        popup += `<b>Gust:</b> ${snapshot.wind_gust_kts.toFixed(1)} kt<br/>`;
      }
      if (pressure !== undefined) {
        popup += `<b>Pressure:</b> ${pressure.toFixed(1)} hPa<br/>`;
      }
      if (airTemp !== undefined) {
        popup += `<b>Air:</b> ${airTemp.toFixed(1)}°C<br/>`;
      }
      if (waterTemp !== undefined) {
        popup += `<b>Water:</b> ${waterTemp.toFixed(1)}°C<br/>`;
      }
      if (waveHeight !== undefined) {
        popup += `<b>Waves:</b> ${waveHeight.toFixed(1)} m<br/>`;
      }
      popup += "</div>";

      if (buoyMarkers.current[buoy.station_id]) {
        // Update existing marker
        buoyMarkers.current[buoy.station_id].setIcon(icon);
        buoyMarkers.current[buoy.station_id].setPopupContent(popup);
      } else {
        // Create new marker
        const marker = L.marker([buoy.lat, buoy.lon], { icon })
          .bindPopup(popup)
          .addTo(map);
        buoyMarkers.current[buoy.station_id] = marker;
      }
    });
  }, [buoys, buoySnapshot]);

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
    const heading = closest.course || closest.cog || 0;

    // Rotate boat icon based on heading
    const boatSvg = `
      <svg width="24" height="24" viewBox="0 0 24 24" style="transform: rotate(${heading}deg)">
        <polygon points="12,2 20,22 12,17 4,22" fill="#1da1f2" stroke="#fff" stroke-width="1"/>
      </svg>
    `;

    if (boatMarker.current) {
      boatMarker.current.setLatLng([lat, lon]);
      boatMarker.current.setIcon(
        L.divIcon({
          html: boatSvg,
          iconSize: [24, 24],
          iconAnchor: [12, 12],
          className: "",
        })
      );
    } else {
      const boatIcon = L.divIcon({
        html: boatSvg,
        iconSize: [24, 24],
        iconAnchor: [12, 12],
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
