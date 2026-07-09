// sf-weather-overlay.js — SailFrames weather layers for the RACE map.
// Ports the /tactics HRRR-field / sea-breeze / tide / relief / distance layers onto
// an existing Leaflet race map, driven by the RACE playback clock (epoch ms) instead
// of the tactics scrubber. Reuses the tactics loaders (DuckDB-WASM, lazy) + draw math.
// Winds in the fields parquet are earth-relative (true N) as of the 2026-07-08 fix.
//
// Usage:  SFWeather.init({ map, containerId:'race-map', date:'2026-07-04', base:'/climatology',
//                          onObsToggle:fn(bool) });  then  SFWeather.setTime(epochMs)
window.SFWeather = (function () {
  'use strict';
  const KT = 1.943844;
  let map = null, container = null, BASE = '/climatology', DATE = null, onObsToggle = null;
  let canvas = null, ctx = null;
  let GRID = null, FRAMES = [], CURRENTS = null;
  let reliefLayer = null, coastLayer = null;
  let curMs = null;
  const on = { wind: false, seabreeze: false, tide: false, relief: false, dist: false, obs: false };

  // ---- DuckDB-WASM (lazy: only when the wind/sea-breeze field is first requested) ----
  let dbDb = null, dbConn = null, duckLoading = null;
  const _regFiles = new Set();
  async function initDuck() {
    if (dbConn) return;
    if (!duckLoading) duckLoading = (async () => {
      const duckdb = await import('https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.29.0/+esm');
      const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
      const wurl = URL.createObjectURL(new Blob([`importScripts("${bundle.mainWorker}");`], { type: 'text/javascript' }));
      const db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(duckdb.LogLevel.WARNING), new Worker(wurl));
      await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
      URL.revokeObjectURL(wurl);
      dbDb = db; dbConn = await db.connect();
    })();
    await duckLoading;
  }
  async function ensureParquet(url) {
    const abs = new URL(url, location.href).href;
    const name = 'p_' + abs.replace(/[^a-z0-9]/gi, '_');
    if (!_regFiles.has(name)) {
      const resp = await fetch(abs);
      if (!resp.ok) throw new Error(`${abs} ${resp.status}`);
      await dbDb.registerFileBuffer(name, new Uint8Array(await resp.arrayBuffer()));
      _regFiles.add(name);
    }
    return name;
  }

  // ---- data loaders --------------------------------------------------------------
  async function ensureGrid() {
    if (GRID) return GRID;
    GRID = await (await fetch(`${BASE}/grid.json`)).json();
    return GRID;
  }
  let fieldLoading = null;
  async function ensureField() {
    if (FRAMES.length || !DATE) return;
    if (!fieldLoading) fieldLoading = (async () => {
      await ensureGrid();
      await initDuck();
      const [y, m, d] = DATE.split('-');
      const name = await ensureParquet(`${BASE}/fields/year=${y}/month=${m}/${d}.parquet`);
      const rows = (await dbConn.query(
        `SELECT CAST(epoch_ms(valid_time_utc) AS DOUBLE) AS t, CAST(gi AS INTEGER) AS gi, u10, v10
           FROM read_parquet('${name}') ORDER BY valid_time_utc, gi`)).toArray().map(x => x.toJSON());
      const n = GRID.nx * GRID.ny, byT = new Map();
      for (const row of rows) {
        let f = byT.get(row.t);
        if (!f) { f = { ms: row.t, u: new Float32Array(n).fill(NaN), v: new Float32Array(n).fill(NaN) }; byT.set(row.t, f); }
        f.u[row.gi] = row.u10; f.v[row.gi] = row.v10;
      }
      FRAMES = [...byT.values()].sort((a, b) => a.ms - b.ms);
    })();
    await fieldLoading;
  }
  async function ensureCurrents() {
    if (CURRENTS !== null || !DATE) return;
    try { const r = await fetch(`${BASE}/currents/${DATE}.json`, { cache: 'no-store' }); CURRENTS = r.ok ? await r.json() : false; }
    catch { CURRENTS = false; }
  }
  async function ensureRelief() {
    if (reliefLayer) return;
    try {
      const meta = await (await fetch(`${BASE}/relief.json`, { cache: 'force-cache' })).json();
      reliefLayer = L.imageOverlay(`${BASE}/relief.png`, meta.bounds, { opacity: 0.55, interactive: false, pane: 'sfwRelief' });
      const gj = await (await fetch(`${BASE}/coastline.geojson`, { cache: 'force-cache' })).json();
      coastLayer = L.geoJSON(gj, { pane: 'sfwRelief', style: { color: '#2c3e50', weight: 0.8, opacity: 0.7, fill: false } });
    } catch (e) { reliefLayer = null; }
  }

  // ---- frame interpolation to an arbitrary epoch-ms (the race clock) -------------
  function frameAt(ms) {
    if (!FRAMES.length) return null;
    if (ms <= FRAMES[0].ms) return FRAMES[0];
    const last = FRAMES[FRAMES.length - 1]; if (ms >= last.ms) return last;
    let i = 0; while (i < FRAMES.length - 1 && FRAMES[i + 1].ms <= ms) i++;
    const a = FRAMES[i], b = FRAMES[i + 1], t = (ms - a.ms) / (b.ms - a.ms);
    const n = a.u.length, u = new Float32Array(n), v = new Float32Array(n);
    for (let k = 0; k < n; k++) { u[k] = a.u[k] + (b.u[k] - a.u[k]) * t; v[k] = a.v[k] + (b.v[k] - a.v[k]) * t; }
    return { ms, u, v };
  }

  // ---- canvas + panes ------------------------------------------------------------
  function injectCSS() {
    if (document.getElementById('sfw-css')) return;
    const s = document.createElement('style'); s.id = 'sfw-css';
    s.textContent = `
      .sfw-arrows{position:absolute;left:0;top:0;pointer-events:none}
      .sfw-control{background:rgba(255,255,255,.94);padding:6px 8px;font:12px/1.35 system-ui,sans-serif;color:#1c2b3a;box-shadow:0 1px 4px rgba(0,0,0,.3);border-radius:6px}
      .sfw-control .sfw-title{font-weight:700;margin-bottom:4px}
      .sfw-control .sfw-row{display:block;white-space:nowrap;cursor:pointer;padding:1px 0}
      .sfw-control .sfw-row input{vertical-align:-1px;margin-right:4px}
      .sfw-control .sfw-busy{color:#3a86c8;font-style:italic}
      .sfw-legend{background:rgba(255,255,255,.94);padding:6px 8px;font:11px/1.4 system-ui,sans-serif;color:#1c2b3a;box-shadow:0 1px 4px rgba(0,0,0,.3);border-radius:6px;max-width:250px}
      .sfw-legend .sfw-lg-title{font-weight:700;margin-bottom:3px;font-size:12px}
      .sfw-legend .sfw-lg-row{white-space:nowrap;margin-top:2px}
      .sfw-sw{display:inline-block;width:11px;height:11px;border-radius:2px;vertical-align:-1px;margin:0 2px}`;
    document.head.appendChild(s);
  }
  function mount() {
    injectCSS();
    map.createPane('sfwRelief'); map.getPane('sfwRelief').style.zIndex = 240;
    map.getPane('sfwRelief').style.pointerEvents = 'none';
    map.createPane('sfwField'); const fp = map.getPane('sfwField');
    fp.style.zIndex = 250; fp.style.pointerEvents = 'none';
    canvas = document.createElement('canvas'); canvas.className = 'sfw-arrows';
    fp.appendChild(canvas); ctx = canvas.getContext('2d');
    map.on('move zoom viewreset resize zoomanim', redraw);
    window.addEventListener('resize', redraw);
    addLegend();
  }

  // ---- legend (bottom-left; shows only the enabled layers' keys) ------------------
  let legendDiv = null;
  function addLegend() {
    const ctl = L.control({ position: 'bottomright' });   // bottom-left holds the playback controls
    ctl.onAdd = function () {
      legendDiv = L.DomUtil.create('div', 'sfw-legend'); legendDiv.style.display = 'none';
      L.DomEvent.disableClickPropagation(legendDiv);
      updateLegend(); return legendDiv;
    };
    ctl.addTo(map);
  }
  function updateLegend() {
    if (!legendDiv) return;
    const sw = c => `<span class="sfw-sw" style="background:${c}"></span>`;
    const parts = [];
    if (on.wind) parts.push(`<div class="sfw-lg-row"><b>HRRR wind</b> kt ${[[0, 'calm'], [6, '6'], [12, '12'], [18, '18'], [26, '26+']].map(([s, l]) => sw(spdColor(s)) + l).join(' ')}</div>`);
    if (on.seabreeze) parts.push(`<div class="sfw-lg-row">${sw('rgba(46,160,220,.5)')}sea-breeze zone <span style="color:#d81b1b;font-weight:700;margin-left:3px">▬</span> front</div>`);
    if (on.tide) parts.push(`<div class="sfw-lg-row"><span style="color:#0aa0a0;font-weight:700">⇒⇒</span> tidal current (set/drift, NOAA)</div>`);
    if (on.dist) parts.push(`<div class="sfw-lg-row">dist-to-coast NM ${[[3, '#1f9d55'], [6, '#3a86c8'], [12, '#e8843a'], [20, '#d64545']].map(([Lv, c]) => sw(c) + Lv).join(' ')}</div>`);
    if (on.relief) parts.push(`<div class="sfw-lg-row"><span class="sfw-sw" style="background:linear-gradient(90deg,#2e6e3f,#c8b88a,#efe6d0)"></span>3DEP shaded relief</div>`);
    if (on.obs) parts.push(`<div class="sfw-lg-row">◎ obs buoy wind rose (NOAA)</div>`);
    legendDiv.innerHTML = parts.length ? `<div class="sfw-lg-title">Weather ☁</div>` + parts.join('') : '';
    legendDiv.style.display = parts.length ? 'block' : 'none';
  }
  function sizeCanvas() {
    const size = map.getSize(), dpr = window.devicePixelRatio || 1;
    if (canvas.width !== size.x * dpr || canvas.height !== size.y * dpr) {
      canvas.width = size.x * dpr; canvas.height = size.y * dpr;
      canvas.style.width = size.x + 'px'; canvas.style.height = size.y + 'px';
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    // keep the canvas pinned to the current viewport origin (pane is transformed on pan)
    L.DomUtil.setPosition(canvas, map.containerPointToLayerPoint([0, 0]));
  }

  // ---- ported draw math (from tactics-app.js; earth-relative winds) --------------
  const P = ll => map.latLngToContainerPoint(ll);
  function spdColor(kt) {
    const stops = [[0, '#4a90d9'], [6, '#3ec46d'], [12, '#e8b13a'], [18, '#e8603a'], [26, '#c0392b']];
    let c = stops[0][1]; for (const [s, col] of stops) if (kt >= s) c = col; return c;
  }
  function drawArrow(px, py, u, v) {
    const spd = Math.hypot(u, v); if (spd < 0.1) return;
    const kt = spd * KT, ang = Math.atan2(-v, u), len = Math.min(26, 6 + kt * 1.1);
    const ex = px + Math.cos(ang) * len, ey = py + Math.sin(ang) * len;
    ctx.strokeStyle = ctx.fillStyle = spdColor(kt); ctx.lineWidth = 1.6;
    ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(ex, ey); ctx.stroke();
    const h = 4.5; ctx.beginPath(); ctx.moveTo(ex, ey);
    ctx.lineTo(ex - h * Math.cos(ang - 0.4), ey - h * Math.sin(ang - 0.4));
    ctx.lineTo(ex - h * Math.cos(ang + 0.4), ey - h * Math.sin(ang + 0.4));
    ctx.closePath(); ctx.fill();
  }
  // Dense wind field: draw an arrow on a screen lattice, each sampled from its TRUE
  // nearest grid cell (via the actual per-cell lats/lons — NOT a bbox-linear map,
  // which is wrong because the LCC grid is rotated ~16° and the cell envelope ≠ bbox).
  // The lattice→cell map only changes with the map view, so cache it across scrubs.
  let _latKey = null, _lattice = null;
  function latticeCells(r) {
    const key = map.getZoom() + ':' + map.getCenter().lat.toFixed(4) + ',' + map.getCenter().lng.toFixed(4) + ':' + r.width + 'x' + r.height;
    if (key === _latKey) return _lattice;
    const { lats, lons } = GRID, n = lats.length, step = 46, out = [];
    for (let y = step / 2; y < r.height; y += step) for (let x = step / 2; x < r.width; x += step) {
      const ll = map.containerPointToLatLng([x, y]), cw = Math.cos(ll.lat * Math.PI / 180);
      let bk = -1, bd = Infinity;
      for (let k = 0; k < n; k++) {
        const dla = lats[k] - ll.lat, dlo = (lons[k] - ll.lng) * cw, d = dla * dla + dlo * dlo;
        if (d < bd) { bd = d; bk = k; }
      }
      if (bk >= 0 && bd < 0.0016) out.push({ x, y, k: bk });   // within ~0.04° of a cell
    }
    _latKey = key; _lattice = out; return out;
  }
  function drawWindInterp(fr, r) {
    for (const c of latticeCells(r)) {
      const u = fr.u[c.k], v = fr.v[c.k]; if (!isFinite(u) || !isFinite(v)) continue;
      drawArrow(c.x, c.y, u, v);
    }
  }
  const windFromDeg = (u, v) => (Math.atan2(u, v) * 180 / Math.PI + 180) % 360;
  const angDiffSb = (a, b) => (a - b + 180) % 360 - 180;
  function cellPx() {
    const { lats, lons, nx } = GRID;
    const a = P([lats[0], lons[0]]), bx = P([lats[1], lons[1]]), by = P([lats[nx], lons[nx]]);
    return [Math.max(3, Math.abs(bx.x - a.x)), Math.max(3, Math.abs(by.y - a.y))];
  }
  const SB_HALF = 75, SB_MINKT = 2.5, SB_CONV_TH = 2.0e-4;
  function nearWaterMask() {
    if (GRID._nearWater) return GRID._nearWater;
    const { land_mask, nx, ny } = GRID;
    let cur = Uint8Array.from(land_mask, v => v ? 0 : 1);
    for (let pass = 0; pass < 2; pass++) {
      const nxt = cur.slice();
      for (let row = 0; row < ny; row++) for (let col = 0; col < nx; col++) {
        const k = row * nx + col; if (cur[k]) continue;
        for (let dr = -1; dr <= 1; dr++) for (let dc = -1; dc <= 1; dc++) {
          const r2 = row + dr, c2 = col + dc; if (r2 < 0 || r2 >= ny || c2 < 0 || c2 >= nx) continue;
          if (cur[r2 * nx + c2]) { nxt[k] = 1; dr = dc = 2; }
        }
      }
      cur = nxt;
    }
    GRID._nearWater = cur; return cur;
  }
  function drawSeaBreezeZone(fr, r) {
    const { land_mask, seaward_deg, lats, lons } = GRID; if (!seaward_deg) return;
    const nw = nearWaterMask(), [cw, ch] = cellPx();
    ctx.fillStyle = 'rgba(46,160,220,.28)'; ctx.beginPath();
    for (let k = 0; k < land_mask.length; k++) {
      if (!nw[k]) continue; const u = fr.u[k], v = fr.v[k]; if (!isFinite(u) || !isFinite(v)) continue;
      if (Math.hypot(u, v) * KT < SB_MINKT) continue;
      if (Math.abs(angDiffSb(windFromDeg(u, v), seaward_deg[k])) > SB_HALF) continue;
      const pt = P([lats[k], lons[k]]);
      if (pt.x < -30 || pt.y < -30 || pt.x > r.width + 30 || pt.y > r.height + 30) continue;
      ctx.rect(pt.x - cw / 2 - 1, pt.y - ch / 2 - 1, cw + 2, ch + 2);
    }
    ctx.fill();
  }
  function convergenceField(fr) {
    const { nx, ny, land_mask, cell_km } = GRID, dm = (cell_km || 3) * 1000, N = nx * ny;
    const div = new Float32Array(N).fill(NaN);
    for (let row = 1; row < ny - 1; row++) for (let col = 1; col < nx - 1; col++) {
      const k = row * nx + col; if (land_mask[k]) continue;
      const uE = fr.u[k + 1], uW = fr.u[k - 1], vN = fr.v[k + nx], vS = fr.v[k - nx];
      if (!isFinite(uE) || !isFinite(uW) || !isFinite(vN) || !isFinite(vS)) continue;
      div[k] = (uE - uW) / (2 * dm) + (vN - vS) / (2 * dm);
    }
    const conv = new Float32Array(N).fill(NaN);
    for (let row = 1; row < ny - 1; row++) for (let col = 1; col < nx - 1; col++) {
      const k = row * nx + col; if (!isFinite(div[k])) continue;
      let s = 0, n = 0;
      for (let dr = -1; dr <= 1; dr++) for (let dc = -1; dc <= 1; dc++) { const kk = k + dr * nx + dc; if (isFinite(div[kk])) { s += div[kk]; n++; } }
      conv[k] = -s / n;
    }
    return conv;
  }
  function drawSeaBreezeFront(fr) {
    const { lats, lons, nx, ny } = GRID, conv = convergenceField(fr); if (!conv) return;
    const TH = SB_CONV_TH, val = k => conv[k];
    const scr = (r1, c1, r2, c2) => {
      const va = val(r1 * nx + c1), vb = val(r2 * nx + c2), t = (TH - va) / (vb - va);
      const a = P([lats[r1 * nx + c1], lons[r1 * nx + c1]]), b = P([lats[r2 * nx + c2], lons[r2 * nx + c2]]);
      return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t };
    };
    ctx.strokeStyle = '#d81b1b'; ctx.lineWidth = 3; ctx.setLineDash([]); ctx.beginPath(); let any = false;
    const seg = (p, q) => { ctx.moveTo(p.x, p.y); ctx.lineTo(q.x, q.y); any = true; };
    for (let row = 0; row < ny - 1; row++) for (let col = 0; col < nx - 1; col++) {
      const v0 = val(row * nx + col), v1 = val(row * nx + col + 1), v2 = val((row + 1) * nx + col + 1), v3 = val((row + 1) * nx + col);
      if (!(isFinite(v0) && isFinite(v1) && isFinite(v2) && isFinite(v3))) continue;
      let idx = 0; if (v0 > TH) idx |= 1; if (v1 > TH) idx |= 2; if (v2 > TH) idx |= 4; if (v3 > TH) idx |= 8;
      if (idx === 0 || idx === 15) continue;
      const eB = () => scr(row, col, row, col + 1), eR = () => scr(row, col + 1, row + 1, col + 1),
        eT = () => scr(row + 1, col + 1, row + 1, col), eL = () => scr(row + 1, col, row, col);
      switch (idx) {
        case 1: case 14: seg(eL(), eB()); break; case 2: case 13: seg(eB(), eR()); break;
        case 3: case 12: seg(eL(), eR()); break; case 4: case 11: seg(eR(), eT()); break;
        case 6: case 9: seg(eB(), eT()); break; case 7: case 8: seg(eL(), eT()); break;
        case 5: seg(eL(), eB()); seg(eR(), eT()); break; case 10: seg(eB(), eR()); seg(eL(), eT()); break;
      }
    }
    if (any) ctx.stroke();
  }
  function drawCoastDist(r) {
    const { lats, lons, nx, ny, coast_dist_nm } = GRID; if (!coast_dist_nm) return;
    const levels = [[3, '#1f9d55'], [6, '#3a86c8'], [12, '#e8843a'], [20, '#d64545']];
    const pt = k => P([lats[k], lons[k]]);
    for (const [Lv, col] of levels) {
      ctx.fillStyle = col; let label = null;
      const cross = (ka, kb) => {
        const a = coast_dist_nm[ka], b = coast_dist_nm[kb]; if ((a - Lv) * (b - Lv) >= 0) return;
        const t = (Lv - a) / (b - a), p1 = pt(ka), p2 = pt(kb);
        const x = p1.x + (p2.x - p1.x) * t, y = p1.y + (p2.y - p1.y) * t;
        if (x < 0 || y < 0 || x > r.width || y > r.height) return;
        ctx.beginPath(); ctx.arc(x, y, 1.7, 0, 6.28); ctx.fill();
        if (!label && x > 30 && x < r.width - 34 && y > 14 && y < r.height - 6) label = { x, y };
      };
      for (let row = 0; row < ny; row++) for (let col2 = 0; col2 < nx; col2++) {
        const k = row * nx + col2; if (col2 < nx - 1) cross(k, k + 1); if (row < ny - 1) cross(k, k + nx);
      }
      if (label) {
        ctx.font = 'bold 11px system-ui'; ctx.fillStyle = 'rgba(255,255,255,.85)'; ctx.fillRect(label.x - 1, label.y - 10, 34, 13);
        ctx.fillStyle = col; ctx.fillText(Lv + ' NM', label.x, label.y);
      }
    }
  }
  const CUR_COL = '#0aa0a0';
  function interpVel(series, h) {
    if (!series || !series.length) return null;
    if (h <= series[0][0]) return series[0][1];
    const n = series.length; if (h >= series[n - 1][0]) return series[n - 1][1];
    for (let i = 0; i < n - 1; i++) { const a = series[i], b = series[i + 1]; if (h >= a[0] && h <= b[0]) return a[1] + (b[1] - a[1]) * (h - a[0]) / (b[0] - a[0]); }
    return series[0][1];
  }
  function drawCurrentArrow(px, py, setDeg, kt) {
    if (kt < 0.08) { ctx.fillStyle = 'rgba(10,160,160,.55)'; ctx.beginPath(); ctx.arc(px, py, 2.2, 0, 6.28); ctx.fill(); return; }
    const a = setDeg * Math.PI / 180, dx = Math.sin(a), dy = -Math.cos(a);
    const len = Math.min(40, 8 + kt * 13), ex = px + dx * len, ey = py + dy * len;
    ctx.strokeStyle = 'rgba(255,255,255,.9)'; ctx.lineWidth = 5; ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(ex, ey); ctx.stroke();
    ctx.strokeStyle = CUR_COL; ctx.lineWidth = 2.7; ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(ex, ey); ctx.stroke();
    const ang = Math.atan2(ey - py, ex - px);
    for (const off of [0, 5]) {
      const bx = ex - off * Math.cos(ang), by = ey - off * Math.sin(ang);
      ctx.strokeStyle = 'rgba(255,255,255,.9)'; ctx.lineWidth = 4;
      ctx.beginPath(); ctx.moveTo(bx - 7 * Math.cos(ang - .5), by - 7 * Math.sin(ang - .5)); ctx.lineTo(bx, by); ctx.lineTo(bx - 7 * Math.cos(ang + .5), by - 7 * Math.sin(ang + .5)); ctx.stroke();
      ctx.strokeStyle = CUR_COL; ctx.lineWidth = 2.4;
      ctx.beginPath(); ctx.moveTo(bx - 7 * Math.cos(ang - .5), by - 7 * Math.sin(ang - .5)); ctx.lineTo(bx, by); ctx.lineTo(bx - 7 * Math.cos(ang + .5), by - 7 * Math.sin(ang + .5)); ctx.stroke();
    }
    ctx.fillStyle = CUR_COL; ctx.beginPath(); ctx.arc(px, py, 2.4, 0, 6.28); ctx.fill();
  }
  function drawCurrentLayer(ms, r) {
    if (!CURRENTS || !CURRENTS.stations) return;
    const lt = new Date(ms).toLocaleString('en-US', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false });
    const h = (+lt.slice(0, 2)) + (+lt.slice(3, 5)) / 60;
    for (const s of CURRENTS.stations) {
      const vel = interpVel(s.series, h); if (vel == null) continue;
      const pt = P([s.lat, s.lon]);
      if (pt.x < -20 || pt.y < -20 || pt.x > r.width + 20 || pt.y > r.height + 20) continue;
      drawCurrentArrow(pt.x, pt.y, vel >= 0 ? s.flood : s.ebb, Math.abs(vel));
    }
  }

  // ---- master redraw at the current race time ------------------------------------
  function redraw() {
    if (!canvas || curMs == null) return;
    sizeCanvas();
    const r = { width: map.getSize().x, height: map.getSize().y };
    ctx.clearRect(0, 0, r.width, r.height);
    const needField = on.wind || on.seabreeze;
    const fr = needField ? frameAt(curMs) : null;
    if (on.dist && GRID) drawCoastDist(r);
    if (on.seabreeze && fr) drawSeaBreezeZone(fr, r);
    if (on.wind && fr) drawWindInterp(fr, r);
    if (on.seabreeze && fr) drawSeaBreezeFront(fr);
    if (on.tide) drawCurrentLayer(curMs, r);
  }

  // ---- layer enable/disable ------------------------------------------------------
  async function setLayer(name, val) {
    on[name] = val;
    try {
      if (name === 'wind' || name === 'seabreeze') { if (val) { setBusy(name, true); await ensureField(); setBusy(name, false); } }
      else if (name === 'dist') { if (val) await ensureGrid(); }
      else if (name === 'tide') { if (val) await ensureCurrents(); }
      else if (name === 'relief') {
        if (val) { await ensureRelief(); if (reliefLayer) { reliefLayer.addTo(map); coastLayer.addTo(map); } }
        else if (reliefLayer) { map.removeLayer(reliefLayer); map.removeLayer(coastLayer); }
      } else if (name === 'obs') { if (onObsToggle) onObsToggle(val); }
    } catch (e) { setBusy(name, false); console.warn('[SFWeather]', name, e); }
    try { localStorage.setItem('sfw_' + name, val ? '1' : '0'); } catch {}
    updateLegend();
    redraw();
  }
  function setBusy(name, b) {
    const el = container && container.querySelector(`.sfw-row[data-l="${name}"] .sfw-busy`);
    if (el) el.style.display = b ? 'inline' : 'none';
  }

  // ---- control panel (Leaflet control, topright — matches AIS/layline) -----------
  const LAYERS = [
    ['wind', 'HRRR wind', '🌬'], ['seabreeze', 'Sea-breeze', '⛵'], ['tide', 'Tide current', '🌊'],
    ['obs', 'Obs buoys', '◎'], ['relief', 'Relief', '⛰'], ['dist', 'Dist-to-coast', '▦'],
  ];
  function addControl() {
    const ctl = L.control({ position: 'topright' });
    ctl.onAdd = function () {
      const div = L.DomUtil.create('div', 'leaflet-control-layers sfw-control');
      div.innerHTML = `<div class="sfw-title">Weather ☁</div>` + LAYERS.map(([k, lab, ic]) =>
        `<label class="sfw-row" data-l="${k}"><input type="checkbox" data-layer="${k}"> ${ic} ${lab}<span class="sfw-busy" style="display:none"> …</span></label>`).join('');
      L.DomEvent.disableClickPropagation(div); L.DomEvent.disableScrollPropagation(div);
      div.querySelectorAll('input[data-layer]').forEach(cb => {
        const k = cb.dataset.layer, saved = (() => { try { const ls = localStorage.getItem('sfw_' + k); return ls === '1' || (ls === null && k === 'obs'); } catch { return k === 'obs'; } })();
        cb.checked = saved;
        cb.addEventListener('change', () => setLayer(k, cb.checked));
        if (saved) setLayer(k, true);
      });
      return div;
    };
    ctl.addTo(map);
  }

  // ---- public API ----------------------------------------------------------------
  function init(opts) {
    map = opts.map; container = document.getElementById(opts.containerId || 'race-map');
    BASE = (opts.base || '/climatology').replace(/\/$/, ''); DATE = opts.date; onObsToggle = opts.onObsToggle || null;
    CURRENTS = null;
    mount(); addControl();
    if (window.__sfwHook) window.__sfwHook(api);
  }
  function setTime(ms) { curMs = ms; redraw(); }
  function windAt(lat, lon) {   // debug: wind FROM° / kt at nearest cell for the current time
    if (!GRID || !FRAMES.length || curMs == null) return null;
    const { lats, lons } = GRID, cw = Math.cos(lat * Math.PI / 180); let bk = -1, bd = Infinity;
    for (let k = 0; k < lats.length; k++) { const dla = lats[k] - lat, dlo = (lons[k] - lon) * cw, d = dla * dla + dlo * dlo; if (d < bd) { bd = d; bk = k; } }
    const fr = frameAt(curMs), u = fr.u[bk], v = fr.v[bk]; if (!isFinite(u) || !isFinite(v)) return null;
    return { from: (Math.atan2(-u, -v) * 180 / Math.PI + 360) % 360, kt: Math.hypot(u, v) * KT, cell: bk };
  }
  const api = { init, setTime, setLayer, windAt, _state: () => ({ on, frames: FRAMES.length, date: DATE }) };
  window.__sfweather = api;   // headless-test hook
  return api;
})();
