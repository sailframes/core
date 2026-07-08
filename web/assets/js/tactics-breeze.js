/* tactics-breeze.js — the Bernot sea-breeze decision walkthrough for one day.
   Reads climatology/breeze/<date>.json + grid.json and renders a figure-rich,
   per-step, real-data-validated report. Canvas for the quadrant map + loop
   cross-section; Chart.js (already loaded) for the time series. Standalone module:
   hooks the "Sea-breeze analysis" tab and renders into #view-breeze. */
(() => {
  const BASE = (window.location.pathname.includes('/climatology') ? '.' : './climatology');
  const $ = id => document.getElementById(id);
  const KT = 1.943844;
  let CHARTS = [];
  let loaded = null;   // cache by date
  let RELIEF = null;   // {img, meta, coast} — 3DEP shaded relief + coastline

  async function ensureReliefBz() {
    if (RELIEF) return RELIEF;
    try {
      const [meta, coast] = await Promise.all([
        fetch(`${BASE}/relief.json`, { cache: 'force-cache' }).then(r => r.json()),
        fetch(`${BASE}/coastline.geojson`, { cache: 'force-cache' }).then(r => r.json()),
      ]);
      const img = await new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = `${BASE}/relief.png`; });
      RELIEF = { img, meta, coast };
    } catch (e) { RELIEF = { img: null }; }
    return RELIEF;
  }

  const VERDICT = {
    yes: ['✓', '#1f9d55', 'confirmed'], no: ['✗', '#d64545', 'not confirmed'],
    info: ['ℹ', '#3a86c8', 'observed'], 'n/a': ['–', '#8b98a5', 'n/a'], null: ['–', '#8b98a5', '—'],
  };
  const QCOLOR = { Q1: '#1f9d55', Q2: '#e8b13a', Q3: '#3a86c8', Q4: '#d64545' };

  async function j(url) { const r = await fetch(url, { cache: 'no-store' }); if (!r.ok) throw new Error(url + ' ' + r.status); return r.json(); }
  const fmt = (v, u = '', d = 0) => (v == null ? '—' : (typeof v === 'number' ? v.toFixed(d) : v) + u);
  const dirName = deg => { if (deg == null) return '—'; const n = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']; return n[Math.round(deg / 22.5) % 16]; };

  // ---------------------------------------------------------------- header
  function headerHTML(d) {
    const q = d.quadrant, qc = QCOLOR[q] || '#566270';
    const est = d.established;
    const kv = (k, v) => `<div class="bz-kv"><span>${k}</span><b>${v}</b></div>`;
    const s = d.morning_synoptic || {}, sfc = d.surface_gradient || {}, w9 = d.wind_925mb || {};
    return `
    <div class="bz-head">
      <div class="bz-qbadge" style="background:${qc}">${q || '?'}</div>
      <div class="bz-headmain">
        <div class="bz-date">${d.date} · ${d.venue || ''}</div>
        <div class="bz-verdict ${est ? 'ok' : 'bad'}">${est ? '🌬 Sea breeze established' : '⛔ Sea breeze did NOT establish'}</div>
        <div class="bz-sum">${d.summary_verdict || ''}</div>
      </div>
    </div>
    <div class="bz-kvs">
      ${kv('850 hPa synoptic', `${dirName(s.from_deg)} ${fmt(s.from_deg, '°')} @ ${fmt(s.speed_kt, ' kt', 0)}`)}
      ${kv('Surface gradient', `${dirName(sfc.from_deg)} ${fmt(sfc.from_deg, '°')} @ ${fmt(sfc.speed_kt, ' kt', 0)}`)}
      ${kv('925 hPa (~750 m)', `${dirName(w9.from_deg)} ${fmt(w9.from_deg, '°')} @ ${fmt(w9.speed_kt, ' kt', 0)}`)}
      ${kv('Coast faces', `${dirName(d.coast_faces_deg)} ${fmt(d.coast_faces_deg, '°')}`)}
      ${kv('ΔT (land − sea)', fmt(d.dt_c, ' °C', 1))}
      ${kv('SST', fmt(d.sst_c, ' °C', 1))}
      ${kv('Land Tmax', fmt(d.tmax_c, ' °C', 0))}
      ${kv('CAPE (midday)', fmt(d.cape_midday, ' J/kg', 0))}
      ${kv('Wisdorff', `${fmt(d.wisdorff.total)} → ${fmt(d.wisdorff.pred_kt, ' kt')}`)}
    </div>`;
  }

  // ---------------------------------------------------------------- steps
  function stepHTML(s) {
    const v = s.validation || {}, vk = VERDICT[v.verdict] || VERDICT.null;
    const pred = predictionHTML(s);
    return `
    <div class="bz-step">
      <div class="bz-step-h"><span class="bz-step-n">${s.n}</span><span class="bz-step-t">${s.title}</span>
        <span class="bz-badge" style="background:${vk[1]}">${vk[0]} ${vk[2]}</span></div>
      <div class="bz-rule">${s.rule || ''}</div>
      <div class="bz-two">
        <div class="bz-pred"><div class="bz-lbl">Prediction (data)</div>${pred}</div>
        <div class="bz-val"><div class="bz-lbl">Validation (real obs)</div>
          <div class="bz-valtext">${v.detail || ''}</div>
          ${figureSlot(s)}
        </div>
      </div>
    </div>`;
  }

  function predictionHTML(s) {
    const p = s.prediction || {};
    if (s.n === 1) return `<div class="bz-p">ΔT <b>${fmt(p.dt_c, ' °C', 1)}</b> · synoptic <b>${fmt(p.synoptic_kt, ' kt', 0)}</b> (${dirName(p.synoptic_dir)})<br><span class="bz-note">${p.note || ''}</span></div>
      <canvas class="bz-gauge" data-syn="${p.synoptic_kt}" width="320" height="46"></canvas>`;
    if (s.n === 2) return `<div class="bz-p">Coast faces <b>${dirName(p.coast_faces_deg)}</b>, synoptic from <b>${dirName(p.synoptic_from_deg)}</b> → <b style="color:${QCOLOR[p.quadrant]}">${p.quadrant}</b> · ${p.from_land ? 'from land' : 'from sea'} · ${p.divergence || ''}<br><span class="bz-note"><b>${p.behaviour || ''}</b> Strategy: ${p.strategy || ''}</span></div>
      <canvas class="bz-qmap" width="300" height="300"></canvas>`;
    if (s.n === 4) return wisdorffHTML(p);
    if (s.n === 5) return `<div class="bz-p">Horizon clearing (offshore visibility): <b>${p.horizon_cleared == null ? '—' : (p.horizon_cleared ? 'yes' : 'no')}</b></div><canvas class="bz-vis" width="330" height="150"></canvas>`;
    if (s.n === 6) return `<div class="bz-p">Expected right-rotation <b>~10°/h</b> once established. ${p.applies ? '' : '<span class="bz-note">(no breeze — synoptic direction shown)</span>'}</div>`;
    if (s.n === 8) return `<div class="bz-p">A sea breeze is stronger at the coast; watch the offshore transition.</div>`;
    return `<div class="bz-p">${JSON.stringify(p)}</div>`;
  }

  function wisdorffHTML(p) {
    const rows = (p.components || []).map(c => {
      const w = c.weight, col = w > 0 ? '#1f9d55' : (w < 0 ? '#d64545' : '#8b98a5');
      return `<tr class="${c.known ? '' : 'bz-unknown'}"><td>${c.name}</td><td>${c.detail}</td><td style="color:${col};text-align:right">${w > 0 ? '+' : ''}${w}</td></tr>`;
    }).join('');
    return `<table class="bz-wis"><tr><th>Criterion</th><th>Value</th><th>Wt</th></tr>${rows}
      <tr class="bz-wis-tot"><td colspan="2">Total → predicted max afternoon breeze</td><td style="text-align:right"><b>${p.total} → ${p.pred_kt} kt</b></td></tr></table>`;
  }

  function figureSlot(s) {
    if (s.n === 4) return `<canvas class="bz-wisbar" width="330" height="60"></canvas>`;
    if (s.n === 6) return `<canvas class="bz-rot" width="330" height="160"></canvas>`;
    if (s.n === 8) return `<canvas class="bz-io" width="330" height="90"></canvas>`;
    return '';
  }

  // ---------------------------------------------------------------- drawings
  function drawGauge(cv, synKt) {
    const g = cv.getContext('2d'), W = cv.width, H = cv.height; g.clearRect(0, 0, W, H);
    const max = 24, x = v => 8 + (W - 16) * Math.min(v, max) / max;
    g.fillStyle = '#e7edf3'; g.fillRect(8, 16, W - 16, 14);
    // ceiling zone 18-24
    g.fillStyle = 'rgba(214,69,69,.25)'; g.fillRect(x(18), 16, x(max) - x(18), 14);
    g.fillStyle = synKt >= 18 ? '#d64545' : '#3a86c8'; g.fillRect(8, 16, x(synKt) - 8, 14);
    g.strokeStyle = '#d64545'; g.lineWidth = 2; g.beginPath(); g.moveTo(x(18), 10); g.lineTo(x(18), 34); g.stroke();
    g.fillStyle = '#333'; g.font = '11px system-ui'; g.fillText('0', 8, 44); g.fillText('18 kt ceiling', x(18) - 26, 44); g.fillText(synKt == null ? '' : synKt.toFixed(0) + ' kt', x(synKt) - 10, 12);
  }

  function drawQuadrantMap(cv, d, grid) {
    const g = cv.getContext('2d'), W = cv.width, H = cv.height; g.clearRect(0, 0, W, H);
    const b = grid.bbox, nx = grid.nx, ny = grid.ny, lm = grid.land_mask, la = grid.lats, lo = grid.lons;
    const X = lon => (lon - b.lon_min) / (b.lon_max - b.lon_min) * W;
    const Y = lat => (b.lat_max - lat) / (b.lat_max - b.lat_min) * H;
    // sea backdrop
    g.fillStyle = '#cfe0ef'; g.fillRect(0, 0, W, H);
    // basemap: 3DEP shaded relief (cropped to the analysis bbox), else land/water cells
    if (RELIEF && RELIEF.img) {
      const rb = RELIEF.meta.bounds, RW = RELIEF.img.naturalWidth, RH = RELIEF.img.naturalHeight;
      const [[rlatS, rlonW], [rlatN, rlonE]] = rb;
      const sx = (b.lon_min - rlonW) / (rlonE - rlonW) * RW;
      const sx2 = (b.lon_max - rlonW) / (rlonE - rlonW) * RW;
      const sy = (rlatN - b.lat_max) / (rlatN - rlatS) * RH;
      const sy2 = (rlatN - b.lat_min) / (rlatN - rlatS) * RH;
      try { g.drawImage(RELIEF.img, sx, sy, sx2 - sx, sy2 - sy, 0, 0, W, H); } catch (e) { }
      // vector coastline
      g.strokeStyle = 'rgba(44,62,80,.7)'; g.lineWidth = 0.8; g.beginPath();
      for (const f of (RELIEF.coast.features || [])) for (const line of f.geometry.coordinates) {
        let started = false;
        for (const [lon, lat] of line) { const px = X(lon), py = Y(lat); if (!started) { g.moveTo(px, py); started = true; } else g.lineTo(px, py); }
      }
      g.stroke();
    } else {
      const cw = W / nx * 1.4, ch = H / ny * 1.4;
      for (let k = 0; k < lm.length; k++) { g.fillStyle = lm[k] ? '#dfe6d8' : '#cfe0ef'; g.fillRect(X(lo[k]) - cw / 2, Y(la[k]) - ch / 2, cw, ch); }
    }
    // center = venue racing area
    const cx = W * 0.42, cy = H * 0.5;
    // coast facing normal (seaward)
    const cf = d.coast_faces_deg;
    if (cf != null) {
      const a = (cf) * Math.PI / 180; const dx = Math.sin(a), dy = -Math.cos(a);
      g.strokeStyle = '#6b7b52'; g.lineWidth = 2; g.setLineDash([4, 3]);
      g.beginPath(); g.moveTo(cx - dx * 60, cy - dy * 60); g.lineTo(cx + dx * 60, cy + dy * 60); g.stroke(); g.setLineDash([]);
      g.fillStyle = '#6b7b52'; g.font = '11px system-ui'; g.fillText('seaward', cx + dx * 66 - 10, cy + dy * 66);
    }
    // synoptic wind arrow (from -> center)
    const s = d.morning_synoptic || {};
    if (s.from_deg != null) {
      const a = s.from_deg * Math.PI / 180; const fx = Math.sin(a), fy = -Math.cos(a);   // points to source
      const qc = QCOLOR[d.quadrant] || '#333';
      const L = 70;
      const sx = cx + fx * L, sy = cy + fy * L;   // source
      g.strokeStyle = qc; g.lineWidth = 3.5; g.beginPath(); g.moveTo(sx, sy); g.lineTo(cx, cy); g.stroke();
      // arrowhead at center
      const ang = Math.atan2(cy - sy, cx - sx);
      g.beginPath(); g.moveTo(cx, cy); g.lineTo(cx - 10 * Math.cos(ang - .4), cy - 10 * Math.sin(ang - .4)); g.lineTo(cx - 10 * Math.cos(ang + .4), cy - 10 * Math.sin(ang + .4)); g.closePath(); g.fillStyle = qc; g.fill();
      g.fillStyle = qc; g.font = 'bold 13px system-ui'; g.fillText(d.quadrant + ' synoptic', sx - 20, sy - 6);
    }
    g.strokeStyle = '#8b98a5'; g.strokeRect(0.5, 0.5, W - 1, H - 1);
    g.fillStyle = '#8b98a5'; g.font = '10px system-ui'; g.fillText('N↑  land=green sea=blue', 6, H - 6);
  }

  function drawCrossSection(cv, d) {
    const g = cv.getContext('2d'), W = cv.width, H = cv.height; g.clearRect(0, 0, W, H);
    const seaY = H - 40, coastX = W * 0.5;
    // sea + land
    g.fillStyle = '#cfe0ef'; g.fillRect(0, seaY, coastX, 40);
    g.fillStyle = '#dfe6d8'; g.beginPath(); g.moveTo(coastX, seaY); g.lineTo(coastX + 40, seaY - 22); g.lineTo(W, seaY - 30); g.lineTo(W, H); g.lineTo(coastX, H); g.closePath(); g.fill();
    g.fillStyle = '#33475b'; g.font = '12px system-ui'; g.fillText('sea', 20, seaY + 24); g.fillText('land (Tmax ' + fmt(d.loop.dt_c != null ? (d.sst_c + d.loop.dt_c) : d.tmax_c, '°C', 0) + ')', coastX + 40, seaY - 26);
    const est = d.established;
    const depth = d.loop.depth_m, topY = Math.max(20, seaY - 150 * Math.min(1, (depth || 300) / 500) - 40);
    g.globalAlpha = est ? 1 : 0.35;
    // loop arrows
    const arrow = (x1, y1, x2, y2, col, lw) => { g.strokeStyle = col; g.fillStyle = col; g.lineWidth = lw; g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke(); const a = Math.atan2(y2 - y1, x2 - x1); g.beginPath(); g.moveTo(x2, y2); g.lineTo(x2 - 9 * Math.cos(a - .4), y2 - 9 * Math.sin(a - .4)); g.lineTo(x2 - 9 * Math.cos(a + .4), y2 - 9 * Math.sin(a + .4)); g.closePath(); g.fill(); };
    arrow(60, seaY - 12, coastX - 10, seaY - 12, '#1f6fb0', 3);                 // surface inflow (sea->land)
    g.fillStyle = '#1f6fb0'; g.globalAlpha = est ? 1 : .5; g.fillText('surface inflow ' + fmt(d.loop.surface_kt, ' kt', 0), 60, seaY - 18); g.globalAlpha = est ? 1 : .35;
    arrow(coastX - 10, topY, 70, topY, '#c0453a', 3);                           // return aloft (land->sea)
    g.fillStyle = '#c0453a'; g.fillText('return aloft ' + fmt(d.loop.return_kt, ' kt', 0) + ' (' + fmt(depth, ' m', 0) + ' deep)', coastX - 130, topY - 6);
    arrow(coastX - 6, seaY - 16, coastX - 6, topY + 8, '#c0453a', 2.5);         // rising over land
    arrow(66, topY + 8, 66, seaY - 18, '#1f6fb0', 2.5);                         // subsiding offshore
    g.globalAlpha = 1;
    if (!est) { g.fillStyle = '#d64545'; g.font = 'bold 15px system-ui'; g.fillText('loop did NOT form (breeze suppressed)', 40, 24); }
  }

  function newChart(ctx, cfg) { const c = new Chart(ctx, cfg); CHARTS.push(c); return c; }

  function drawRaceField(d) {
    const rf = d.race_field_hourly || [];
    const cf = d.coast_faces_deg != null ? d.coast_faces_deg : 113;
    const dir = $('bz-race-dir');
    if (dir) newChart(dir.getContext('2d'), {
      data: {
        datasets: [
          { type: 'line', label: 'onshore arc', data: rf.map(r => ({ x: r[0], y: Math.min(360, cf + 85) })), borderWidth: 0, pointRadius: 0, backgroundColor: 'rgba(31,157,85,.13)', fill: '+1' },
          { type: 'line', label: '_lo', data: rf.map(r => ({ x: r[0], y: Math.max(0, cf - 85) })), borderWidth: 0, pointRadius: 0, fill: false },
          { type: 'line', label: 'field wind (from°)', data: rf.map(r => ({ x: r[0], y: r[1] })), borderColor: '#1f6fb0', tension: .2, pointRadius: 2.5 },
        ]
      }, options: chartOpts('LT hour', 'wind from °', { yMax: 360, yStep: 90 })
    });
    const spd = $('bz-race-spd');
    if (spd) newChart(spd.getContext('2d'), {
      data: {
        datasets: [
          { type: 'line', label: 'mean kt', data: rf.map(r => ({ x: r[0], y: r[2] })), borderColor: '#1f9d55', tension: .3, pointRadius: 2 },
          { type: 'line', label: 'max cell kt', data: rf.map(r => ({ x: r[0], y: r[3] })), borderColor: '#e8b13a', borderDash: [4, 3], tension: .3, pointRadius: 2 },
        ]
      }, options: chartOpts('LT hour', 'kt')
    });
  }

  function drawObs(d) {
    const meta = d.stations_meta || {};
    const hours = Array.from({ length: 24 }, (_, i) => i);
    const cols = { '44013': '#3a86c8', A01: '#7b5cd6', BOS: '#e8603a', BVY: '#e8b13a' };
    // direction-by-hour chart (step 6) + speed chart on the overview
    const dirData = [], spdData = [];
    for (const [s, series] of Object.entries(d.obs_hourly || {})) {
      const pts = series.map(([h, dir, spd]) => ({ x: h, y: dir }));
      dirData.push({ label: s, data: pts, borderColor: cols[s] || '#888', backgroundColor: cols[s] || '#888', showLine: false, pointRadius: 3 });
      spdData.push({ label: s, data: series.map(([h, dir, spd]) => ({ x: h, y: spd })), borderColor: cols[s] || '#888', backgroundColor: 'transparent', tension: .3, pointRadius: 2 });
    }
    const dc = document.querySelector('.bz-rot');
    if (dc) newChart(dc.getContext('2d'), { type: 'scatter', data: { datasets: dirData }, options: chartOpts('LT hour', 'wind from °', { yMax: 360, yStep: 90, ann: d.coast_faces_deg }) });
    const oc = $('bz-obs-speed');
    if (oc) newChart(oc.getContext('2d'), { type: 'line', data: { datasets: spdData }, options: chartOpts('LT hour', 'wind kt') });
    const ov = $('bz-obs-dir');
    if (ov) newChart(ov.getContext('2d'), { type: 'scatter', data: { datasets: dirData }, options: chartOpts('LT hour', 'wind from °', { yMax: 360, yStep: 90, ann: d.coast_faces_deg }) });
    // VIS timeline (step 5)
    const step5 = (d.steps || []).find(s => s.n === 5);
    const vc = document.querySelector('.bz-vis');
    if (vc && step5) newChart(vc.getContext('2d'), { type: 'line', data: { datasets: [{ label: 'offshore VIS km', data: (step5.prediction.vis_offshore_km_by_hour || []).map(([h, v]) => ({ x: h, y: v })), borderColor: '#1f9d55', tension: .3, pointRadius: 2 }] }, options: chartOpts('LT hour', 'VIS km') });
    // Wisdorff predicted vs observed (step 4)
    const step4 = (d.steps || []).find(s => s.n === 4);
    const wb = document.querySelector('.bz-wisbar');
    if (wb && step4) newChart(wb.getContext('2d'), { type: 'bar', data: { labels: ['predicted max', 'observed onshore peak'], datasets: [{ data: [step4.prediction.pred_kt, step4.validation.observed_peak_onshore_kt], backgroundColor: ['#8b98a5', step4.validation.verdict === 'yes' ? '#1f9d55' : '#d64545'] }] }, options: { indexAxis: 'y', plugins: { legend: { display: false } }, scales: { x: { title: { display: true, text: 'kt' } } } } });
    // inshore vs offshore (step 8)
    const step8 = (d.steps || []).find(s => s.n === 8);
    const io = document.querySelector('.bz-io');
    if (io && step8) newChart(io.getContext('2d'), { type: 'bar', data: { labels: ['coast (Logan/Beverly)', 'offshore (44013/A01)'], datasets: [{ data: [step8.validation.inshore_onshore_comp_kt, step8.validation.offshore_onshore_comp_kt], backgroundColor: ['#1f6fb0', '#8bb0d6'] }] }, options: { indexAxis: 'y', plugins: { legend: { display: false } }, scales: { x: { title: { display: true, text: 'onshore comp kt' } } } } });
  }

  // Breeze polygon (Wisdorff, Ch.7 §2.4): solar-hour wind vectors from a common
  // origin O; the tips trace the day's evolution; G = centroid = daily-mean wind.
  // We plot the FROM-bearing rose (radius = speed) so the right-rotation is visible.
  function drawBreezePolygon(cv, d) {
    const g = cv.getContext('2d'), W = cv.width, H = cv.height; g.clearRect(0, 0, W, H);
    const O = { x: W / 2, y: H / 2 + 4 }, R = Math.min(W, H) / 2 - 22;
    const rf = (d.race_field_hourly || []).filter(r => r[1] != null && r[0] >= 8 && r[0] <= 19);
    const maxkt = Math.max(8, ...rf.map(r => r[2]));
    const P = (brg, kt) => ({ x: O.x + (kt / maxkt * R) * Math.sin(brg * Math.PI / 180), y: O.y - (kt / maxkt * R) * Math.cos(brg * Math.PI / 180) });
    // compass rings + spokes
    g.strokeStyle = '#2a3038'; g.fillStyle = '#8b98a5'; g.font = '10px system-ui';
    for (const rr of [0.5, 1]) { g.beginPath(); g.arc(O.x, O.y, R * rr, 0, 6.28); g.stroke(); }
    for (const [b, lab] of [[0, 'N'], [90, 'E'], [180, 'S'], [270, 'W']]) {
      const e = P(b, maxkt); g.strokeStyle = '#242a31'; g.beginPath(); g.moveTo(O.x, O.y); g.lineTo(e.x, e.y); g.stroke();
      g.fillStyle = '#8b98a5'; g.fillText(lab, e.x - 3, e.y + (b === 0 ? -2 : b === 180 ? 10 : 3));
    }
    g.fillStyle = '#8b98a5'; g.fillText(maxkt.toFixed(0) + ' kt', O.x + 3, O.y - R + 2);
    // the hour trace (morning cool -> afternoon warm colour), tips connected
    g.lineWidth = 2; g.beginPath();
    rf.forEach((r, i) => { const p = P(r[1], r[2]); if (i) g.lineTo(p.x, p.y); else g.moveTo(p.x, p.y); });
    g.strokeStyle = '#3a86c8'; g.stroke();
    let su = 0, sv = 0;
    rf.forEach(r => {
      const p = P(r[1], r[2]); const warm = (r[0] - 8) / 11;
      g.fillStyle = `rgb(${Math.round(58 + warm * 180)},${Math.round(134 - warm * 70)},${Math.round(200 - warm * 160)})`;
      g.beginPath(); g.arc(p.x, p.y, 3, 0, 6.28); g.fill();
      if ([9, 12, 15, 18].includes(Math.round(r[0]))) { g.fillStyle = '#dfe6ee'; g.font = '9px system-ui'; g.fillText(Math.round(r[0]) + 'h', p.x + 4, p.y); }
      su += Math.sin(r[1] * Math.PI / 180) * r[2]; sv += Math.cos(r[1] * Math.PI / 180) * r[2];
    });
    // G = daily mean (centroid)
    if (rf.length) {
      const gb = (Math.atan2(su, sv) * 180 / Math.PI + 360) % 360, gk = Math.hypot(su, sv) / rf.length;
      const p = P(gb, gk); g.fillStyle = '#1f9d55'; g.beginPath(); g.arc(p.x, p.y, 4.5, 0, 6.28); g.fill();
      g.fillStyle = '#1f9d55'; g.font = 'bold 10px system-ui'; g.fillText('G', p.x + 5, p.y + 3);
    }
    g.fillStyle = '#8b98a5'; g.font = '9px system-ui'; g.fillText('wind FROM · morning→afternoon · G = daily mean', 4, H - 4);
  }

  function chartOpts(xt, yt, extra = {}) {
    const o = { animation: false, plugins: { legend: { labels: { boxWidth: 10, font: { size: 10 }, filter: it => !String(it.text).startsWith('_') } } }, scales: { x: { type: 'linear', min: 0, max: 24, title: { display: true, text: xt }, ticks: { stepSize: 6 } }, y: { title: { display: true, text: yt } } } };
    if (extra.yMax) { o.scales.y.max = extra.yMax; o.scales.y.min = 0; o.scales.y.ticks = { stepSize: extra.yStep }; }
    return o;
  }

  // ---------------------------------------------------------------- main
  async function render() {
    const date = ($('tx-date') && $('tx-date').value) || '2026-07-04';
    const body = $('breeze-body');
    if (!body) return;
    if (loaded && loaded.date === date) return;   // already shown
    body.innerHTML = '<p class="tx-note">Loading sea-breeze analysis…</p>';
    let d, grid;
    try { [d, grid] = await Promise.all([j(`${BASE}/breeze/${date}.json`), j(`${BASE}/grid.json`)]); }
    catch (e) { body.innerHTML = `<p class="tx-note">No sea-breeze analysis for ${date} yet. (Analysis runs from 2026-07-04 forward.)</p>`; return; }
    CHARTS.forEach(c => c.destroy()); CHARTS = [];
    const steps = (d.steps || []).map(stepHTML).join('');
    body.innerHTML = `${headerHTML(d)}
      <div class="bz-section-t">Racing-area wind — HRRR field <span class="tx-hint">(central Mass Bay; the sea-breeze evidence the replay shows)</span></div>
      <div class="bz-two"><div><div class="bz-lbl">Direction by hour — backs to onshore then veers right</div><canvas id="bz-race-dir" width="330" height="175"></canvas></div>
        <div><div class="bz-lbl">Speed by hour — mean &amp; max cell (convergence band)</div><canvas id="bz-race-spd" width="330" height="175"></canvas></div></div>
      <div class="bz-two"><div><div class="bz-section-t">Loop cross-section</div>
        <canvas class="bz-xsec" id="bz-xsec" width="440" height="240"></canvas></div>
        <div><div class="bz-section-t">Breeze polygon <span class="tx-hint">(Wisdorff — solar-hour wind, G = daily mean)</span></div>
        <canvas id="bz-polygon" width="240" height="240"></canvas></div></div>
      <div class="bz-section-t">Decision walkthrough — prediction vs. what actually happened</div>
      ${steps}
      <div class="bz-section-t">Point observations (stations) <span class="tx-hint">— secondary; the enclosed corner can hold the synoptic</span></div>
      <div class="bz-two"><div><div class="bz-lbl">Wind direction by hour</div><canvas id="bz-obs-dir" width="330" height="170"></canvas></div>
        <div><div class="bz-lbl">Wind speed by hour</div><canvas id="bz-obs-speed" width="330" height="170"></canvas></div></div>
      <p class="tx-note">Method: Jean-Yves Bernot, <i>Météo locale et stratégie</i> (theory of quadrants; Wisdorff force grid). Validated against the day's HRRR field over the racing area + real Logan (KBOS), Beverly (KBVY), NDBC 44013 &amp; NERACOOS A01 observations. See <a href="./tactics-methodology.html">How it works</a>.</p>`;
    // draw
    drawRaceField(d);
    drawCrossSection($('bz-xsec'), d);
    if ($('bz-polygon')) drawBreezePolygon($('bz-polygon'), d);
    document.querySelectorAll('.bz-gauge').forEach(c => drawGauge(c, +c.dataset.syn));
    await ensureReliefBz();
    document.querySelectorAll('.bz-qmap').forEach(c => drawQuadrantMap(c, d, grid));
    drawObs(d);
    loaded = d;
  }

  // hook the tab
  function hook() {
    const tab = document.querySelector('.tx-tab[data-view="breeze"]');
    if (tab) tab.addEventListener('click', () => setTimeout(render, 30));
    // re-render when the day changes while on the breeze tab
    const dateEl = $('tx-date');
    if (dateEl) dateEl.addEventListener('change', () => { loaded = null; if (!$('view-breeze').hidden) render(); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', hook); else hook();
  window.__renderBreeze = render;
})();
