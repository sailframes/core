// tactics-app.js — /tactics day-replay wind field.
// Reads climatology grid.json + fields Parquet straight from CloudFront via
// DuckDB-WASM (HTTP Range), draws the HRRR 10 m wind field as an animated
// arrow canvas over a Leaflet basemap. No backend (spec §4/§8).
//
// Data base: same-origin ./climatology/ (page + data both on sailframes.com,
// or a local http.server during dev). Override with ?base=https://...
const params = new URLSearchParams(location.search);
const BASE = (params.get('base') || './climatology').replace(/\/$/, '');
const KT = 1.943844;

const $ = id => document.getElementById(id);
const statusEl = $('tx-status');
const setStatus = s => { statusEl.textContent = s; };

// ---- Leaflet basemap (Carto light — matches dashboard default) ----
// zoomSnap:0 -> fitBounds can use fractional zoom so the near-square venue bbox
// fills the wide map instead of snapping a full level out (big margins).
const map = L.map('tx-map', { zoomControl: true, attributionControl: false, zoomSnap: 0 })
  .setView([42.35, -70.5], 9);
L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
  { maxZoom: 19, subdomains: 'abcd' }).addTo(map);
window.__map = map;   // debug/headless hook

// wind-arrow canvas overlay pinned over the map pane
const canvas = document.createElement('canvas');
canvas.className = 'tx-arrows';
$('tx-map').appendChild(canvas);
const ctx = canvas.getContext('2d');

let GRID = null;       // {nx, ny, lats[], lons[], land_mask[], ...}
let FRAMES = [];       // FRAMES[h] = {t, u:Float32Array, v:Float32Array} per cycle hour
let curFrame = 18;     // default to ~14 EDT (18Z) — peak sea-breeze hour
let playing = false;
let playTimer = null;

// speed (kt) -> color ramp (calm blue -> breeze green -> fresh orange -> strong red)
function spdColor(kt) {
  const stops = [[0, '#4a90d9'], [6, '#3ec46d'], [12, '#e8b13a'], [18, '#e8603a'], [26, '#c0392b']];
  let c = stops[0][1];
  for (const [s, col] of stops) { if (kt >= s) c = col; }
  return c;
}

function resizeCanvas() {
  const r = $('tx-map').getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = r.width * dpr; canvas.height = r.height * dpr;
  canvas.style.width = r.width + 'px'; canvas.style.height = r.height + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function drawArrow(px, py, u, v) {
  // u,v in m/s (met: u eastward, v northward). Screen y is down -> negate v.
  const spd = Math.hypot(u, v);
  const kt = spd * KT;
  if (spd < 0.1) return;
  const ang = Math.atan2(-v, u);          // screen-space angle of the "to" vector
  const len = Math.min(26, 6 + kt * 1.1); // pixel length scaled by speed
  const ex = px + Math.cos(ang) * len, ey = py + Math.sin(ang) * len;
  ctx.strokeStyle = ctx.fillStyle = spdColor(kt);
  ctx.lineWidth = 1.6;
  ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(ex, ey); ctx.stroke();
  // arrowhead
  const h = 4.5, a2 = ang;
  ctx.beginPath();
  ctx.moveTo(ex, ey);
  ctx.lineTo(ex - h * Math.cos(a2 - 0.4), ey - h * Math.sin(a2 - 0.4));
  ctx.lineTo(ex - h * Math.cos(a2 + 0.4), ey - h * Math.sin(a2 + 0.4));
  ctx.closePath(); ctx.fill();
}

function render() {
  if (!GRID || !FRAMES.length) return;
  resizeCanvas();
  const r = $('tx-map').getBoundingClientRect();
  ctx.clearRect(0, 0, r.width, r.height);
  const fr = FRAMES[curFrame];
  const { nx, ny, lats, lons, land_mask } = GRID;
  // draw every other cell to avoid clutter at low zoom
  const step = map.getZoom() >= 10 ? 1 : 2;
  for (let row = 0; row < ny; row += step) {
    for (let col = 0; col < nx; col += step) {
      const k = row * nx + col;
      const u = fr.u[k], v = fr.v[k];
      if (!isFinite(u) || !isFinite(v)) continue;
      const pt = map.latLngToContainerPoint([lats[k], lons[k]]);
      if (pt.x < -20 || pt.y < -20 || pt.x > r.width + 20 || pt.y > r.height + 20) continue;
      drawArrow(pt.x, pt.y, u, v);
    }
  }
  const d = new Date(fr.t);
  const lt = d.toLocaleString('en-US', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false });
  $('tx-clock').textContent = `${fr.t.slice(0, 10)}  ${lt} EDT  (${fr.t.slice(11, 16)}Z)`;
}

map.on('move zoom viewreset resize', render);
window.addEventListener('resize', render);

// ---- legend ----
function buildLegend() {
  const rows = [[0, 'calm'], [6, '6kt'], [12, '12kt'], [18, '18kt'], [26, '26kt+']];
  $('tx-legend').innerHTML = '<div style="margin-bottom:3px;opacity:.8">10 m wind</div>' +
    rows.map(([s, lab]) => `<div class="row"><span class="sw" style="background:${spdColor(s)}"></span>${lab}</div>`).join('');
}

// ---- scrubber / play ----
$('tx-scrub').addEventListener('input', e => { curFrame = +e.target.value; render(); });
$('tx-play').addEventListener('click', togglePlay);
function togglePlay() {
  playing = !playing;
  $('tx-play').textContent = playing ? '❚❚ Pause' : '▶ Play';
  if (playing) {
    playTimer = setInterval(() => {
      curFrame = (curFrame + 1) % FRAMES.length;
      $('tx-scrub').value = curFrame; render();
    }, 220);
  } else clearInterval(playTimer);
}

// ---- DuckDB-WASM ----
let dbConn = null;
async function initDuck() {
  const duckdb = await import('https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.29.0/+esm');
  const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
  const wurl = URL.createObjectURL(new Blob([`importScripts("${bundle.mainWorker}");`], { type: 'text/javascript' }));
  const db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(duckdb.LogLevel.WARNING), new Worker(wurl));
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  URL.revokeObjectURL(wurl);
  dbConn = await db.connect();
}

async function loadGrid() {
  const r = await fetch(`${BASE}/grid.json`);
  if (!r.ok) throw new Error(`grid.json ${r.status}`);
  GRID = await r.json();
}

async function loadFramesFromUrl(url, note) {
  // DuckDB-WASM httpfs needs an absolute URL (a relative path is read as a local glob).
  const abs = new URL(url, location.href).href;
  setStatus('querying fields…');
  const q = async sql => (await dbConn.query(sql)).toArray().map(x => x.toJSON());
  // one row per (valid_time, gi); pivot into per-cycle frames of length nx*ny
  const rows = await q(
    `SELECT CAST(epoch_ms(valid_time_utc) AS DOUBLE) AS t, CAST(gi AS INTEGER) AS gi, u10, v10
       FROM read_parquet('${abs}') ORDER BY valid_time_utc, gi`);
  const n = GRID.nx * GRID.ny;
  const byT = new Map();
  for (const row of rows) {
    let f = byT.get(row.t);
    if (!f) { f = { t: row.t, u: new Float32Array(n).fill(NaN), v: new Float32Array(n).fill(NaN) }; byT.set(row.t, f); }
    f.u[row.gi] = row.u10; f.v[row.gi] = row.v10;
  }
  FRAMES = [...byT.values()].sort((a, b) => a.t - b.t)
    .map(f => ({ ...f, t: new Date(f.t).toISOString() }));
  $('tx-scrub').max = String(FRAMES.length - 1);
  curFrame = Math.min(curFrame, FRAMES.length - 1);
  $('tx-scrub').value = curFrame;
  setStatus(`${note || FRAMES.length + ' hourly frames'} · ${n} cells`);
}

function loadDay(dateStr) {
  const [y, m, d] = dateStr.split('-');
  return loadFramesFromUrl(`${BASE}/fields/year=${y}/month=${m}/${d}.parquet`);
}

$('tx-date').addEventListener('change', async e => {
  if (playing) togglePlay();
  try { await loadDay(e.target.value); render(); }
  catch (err) { setStatus(`No archived wind field for ${e.target.value} — the field archive covers only recent days (it grows one day at a time).`); }
});

// ================= Calendar + Stats =================
const TYPE_COLORS = { F: '#e8603a', R: '#3ec46d', P: '#b06fe0', G: '#e8b13a', N: '#566270', U: '#2a3038' };
let LABELS = null;        // array of day-label rows
let labelsLoaded = false;

async function loadLabels() {
  if (labelsLoaded) return;
  const url = new URL(`${BASE}/labels.parquet`, location.href).href;
  const q = async sql => (await dbConn.query(sql)).toArray().map(x => x.toJSON());
  LABELS = await q(`SELECT * FROM read_parquet('${url}') ORDER BY date`);
  labelsLoaded = true;
}

// ---- calendar ----
function buildTypeLegend() {
  $('tx-typelegend').innerHTML = Object.entries(TYPE_COLORS).map(([t, c]) =>
    `<span class="chip"><span class="sw" style="background:${c}"></span>${t}</span>`).join('');
}

function renderCalendar() {
  const yearSel = $('tx-year');
  const year = yearSel.value;
  const byDate = new Map(LABELS.map(r => [r.date, r]));
  const months = [];
  for (const r of LABELS) if (r.date.startsWith(year)) months.push(r.date.slice(0, 7));
  const uniqMonths = [...new Set(months)].sort();
  const dow = ['S', 'M', 'T', 'W', 'T', 'F', 'S'];
  const html = uniqMonths.map(ym => {
    const [y, m] = ym.split('-').map(Number);
    const first = new Date(Date.UTC(y, m - 1, 1)).getUTCDay();
    const ndays = new Date(Date.UTC(y, m, 0)).getUTCDate();
    const name = new Date(Date.UTC(y, m - 1, 1)).toLocaleString('en-US', { month: 'long', timeZone: 'UTC' });
    let cells = '';
    for (let i = 0; i < first; i++) cells += `<div class="tx-day empty"></div>`;
    for (let d = 1; d <= ndays; d++) {
      const ds = `${ym}-${String(d).padStart(2, '0')}`;
      const rec = byDate.get(ds);
      if (rec) {
        cells += `<div class="tx-day" style="background:${TYPE_COLORS[rec.type] || '#2a3038'}" `
          + `title="${ds} · ${rec.type} · onset ${fmtHM(rec.onset_lt_44013)}${rec.onset_lt_44013 != null ? ' LT' : ''} · ΔT ${fmt1(rec.dt_c)}°C" `
          + `data-date="${ds}">${d}</div>`;
      } else {
        cells += `<div class="tx-day empty" style="background:#171b22" title="${ds} · no data">${d}</div>`;
      }
    }
    return `<div class="tx-month"><h4>${name} ${y}</h4>`
      + `<div class="dow">${dow.map(x => `<span>${x}</span>`).join('')}</div>`
      + `<div class="tx-days">${cells}</div></div>`;
  }).join('');
  $('tx-calendar').innerHTML = html || '<p class="tx-note">No labelled days for this season yet.</p>';
  $('tx-calendar').querySelectorAll('.tx-day[data-date]').forEach(el =>
    el.addEventListener('click', () => {
      $('tx-date').value = el.dataset.date;
      $('tx-date').dispatchEvent(new Event('change'));
      switchView('replay');
    }));
}

function populateYears() {
  const years = [...new Set(LABELS.map(r => r.date.slice(0, 4)))].sort().reverse();
  $('tx-year').innerHTML = years.map(y => `<option value="${y}">${y}</option>`).join('');
}

// ---- stats ----
let charts = {};
function destroyCharts() { Object.values(charts).forEach(c => c && c.destroy()); charts = {}; }

const DIR_SECTORS = [['N', 337.5, 22.5], ['NE', 22.5, 67.5], ['E', 67.5, 112.5], ['SE', 112.5, 157.5],
                     ['S', 157.5, 202.5], ['SW', 202.5, 247.5], ['W', 247.5, 292.5], ['NW', 292.5, 337.5]];
const SPD_BUCKETS = [['0-6', 0, 6], ['6-12', 6, 12], ['12-18', 12, 18], ['18+', 18, 99]];

function inSector(dir, lo, hi) { return lo > hi ? (dir >= lo || dir < hi) : (dir >= lo && dir < hi); }

function renderMoneyChart() {
  // custom canvas heatmap: rows=speed buckets, cols=dir sectors, color=fill rate (F/R/P)
  const cvs = $('tx-chart-money');
  const box = cvs.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  cvs.width = box.width * dpr; cvs.height = box.height * dpr;
  cvs.style.width = box.width + 'px'; cvs.style.height = box.height + 'px';  // constrain display size
  const g = cvs.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, box.width, box.height);
  const mL = 44, mB = 26, mT = 6, mR = 8;
  const gw = box.width - mL - mR, gh = box.height - mT - mB;
  const cols = DIR_SECTORS.length, rows = SPD_BUCKETS.length;
  const cw = gw / cols, ch = gh / rows;
  const labelled = LABELS.filter(r => r.grad_dir != null && r.grad_spd_kt != null && r.type !== 'U');
  for (let ri = 0; ri < rows; ri++) {
    for (let ci = 0; ci < cols; ci++) {
      const [, slo, shi] = SPD_BUCKETS[ri];
      const [, dlo, dhi] = DIR_SECTORS[ci];
      const bin = labelled.filter(r => r.grad_spd_kt >= slo && r.grad_spd_kt < shi && inSector(r.grad_dir, dlo, dhi));
      const x = mL + ci * cw, y = mT + (rows - 1 - ri) * ch;
      if (bin.length === 0) { g.fillStyle = '#1a1f27'; g.fillRect(x + 1, y + 1, cw - 2, ch - 2); continue; }
      const fill = bin.filter(r => r.type === 'F' || r.type === 'R' || r.type === 'P').length / bin.length;
      const c = Math.round(40 + fill * 190);
      g.fillStyle = `rgb(${c},${Math.round(70 + (1 - fill) * 90)},${Math.round(60 + (1 - fill) * 40)})`;
      g.fillRect(x + 1, y + 1, cw - 2, ch - 2);
      g.fillStyle = fill > 0.5 ? '#fff' : '#c9d2dc'; g.font = '11px ui-monospace, Menlo, monospace';
      g.textAlign = 'center';
      g.fillText(`${Math.round(fill * 100)}%`, x + cw / 2, y + ch / 2 - 1);
      g.fillStyle = '#8b98a5'; g.font = '9px ui-monospace'; g.fillText(`n${bin.length}`, x + cw / 2, y + ch / 2 + 11);
    }
  }
  g.fillStyle = '#8b98a5'; g.font = '10px ui-monospace, Menlo, monospace';
  g.textAlign = 'center';
  DIR_SECTORS.forEach(([lab], ci) => g.fillText(lab, mL + ci * cw + cw / 2, box.height - 10));
  g.textAlign = 'right';
  SPD_BUCKETS.forEach(([lab], ri) => g.fillText(lab + 'kt', mL - 5, mT + (rows - 1 - ri) * ch + ch / 2 + 3));
}

function histogram(vals, lo, hi, n) {
  const bins = new Array(n).fill(0); const w = (hi - lo) / n;
  for (const v of vals) { if (v == null) continue; let k = Math.floor((v - lo) / w); if (k < 0) k = 0; if (k >= n) k = n - 1; bins[k]++; }
  const labels = bins.map((_, i) => (lo + i * w).toFixed(0));
  return { bins, labels };
}

function renderStats() {
  destroyCharts();
  renderMoneyChart();
  const L = LABELS.filter(r => r.type !== 'U');
  // onset histogram
  const oh = histogram(L.map(r => r.onset_lt_44013).filter(v => v != null), 10, 19, 9);
  charts.onset = new Chart($('tx-chart-onset'), {
    type: 'bar', data: { labels: oh.labels.map(x => x + 'h'), datasets: [{ data: oh.bins, backgroundColor: '#3aa0ff' }] },
    options: { plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#8b98a5' } }, y: { ticks: { color: '#8b98a5' } } } }
  });
  // type mix
  const types = ['F', 'R', 'P', 'G', 'N'];
  charts.types = new Chart($('tx-chart-types'), {
    type: 'bar', data: { labels: types, datasets: [{ data: types.map(t => LABELS.filter(r => r.type === t).length), backgroundColor: types.map(t => TYPE_COLORS[t]) }] },
    options: { plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#8b98a5' } }, y: { ticks: { color: '#8b98a5' } } } }
  });
  // tide phase at onset
  const th = histogram(L.map(r => r.tide_phase_hw_h).filter(v => v != null), -6.2, 6.2, 12);
  charts.tide = new Chart($('tx-chart-tide'), {
    type: 'bar', data: { labels: th.labels.map(x => x + 'h'), datasets: [{ data: th.bins, backgroundColor: '#3ec46d' }] },
    options: { plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#8b98a5' } }, y: { ticks: { color: '#8b98a5' } } } }
  });
}

// ================= Race briefing / analog finder (§7) =================
let BRIEF = null;
let FIELD_DATES = null;   // Set of YYYY-MM-DD that have an archived wind field (replayable)
function doyOf(dateStr) {
  const d = new Date(dateStr + 'T00:00:00Z');
  return Math.floor((d - Date.UTC(d.getUTCFullYear(), 0, 0)) / 86400000);
}
const DIRNAME = d => ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(((d % 360) / 45)) % 8];
const fmt1 = v => (v == null || isNaN(v)) ? '—' : (+v).toFixed(1);   // one decimal, always
const fmtHM = v => {                                                 // decimal hours -> H:MM local
  if (v == null || isNaN(v)) return '—';
  let h = Math.floor(v), m = Math.round((v - h) * 60);
  if (m === 60) { h += 1; m = 0; }
  return `${h}:${String(m).padStart(2, '0')}`;
};

async function loadFieldIndex() {
  if (FIELD_DATES) return;
  try {
    const r = await fetch(new URL(`${BASE}/fields_index.json`, location.href).href, { cache: 'no-store' });
    FIELD_DATES = new Set(r.ok ? await r.json() : []);
  } catch { FIELD_DATES = new Set(); }
}

function findAnalogs(brief, k = 30) {
  // feature vector: standardized [grad_u, grad_v, dt_c, sst, sin/cos DOY] over
  // days within ±45 DOY. Drop any feature today is missing. (Cloud/DSWRF are
  // shown as inputs but excluded from matching until fields are backfilled for
  // the historical labels that populate tcdc_am — else the pool empties.)
  const base = [['grad_u', brief.grad_u], ['grad_v', brief.grad_v], ['dt_c', brief.dt_c],
                ['sst', brief.sst]].filter(([, v]) => v != null);
  const tdoy = brief.doy;
  const pool = LABELS.filter(r => {
    if (r.type === 'U') return false;
    for (const [f] of base) if (r[f] == null) return false;
    const dd = Math.abs(doyOf(r.date) - tdoy);
    return Math.min(dd, 365 - dd) <= 45;
  });
  if (pool.length < 5) return { pool, analogs: [] };
  const vec = r => [...base.map(([f]) => r[f]),
                    Math.sin(2 * Math.PI * doyOf(r.date) / 365), Math.cos(2 * Math.PI * doyOf(r.date) / 365)];
  const tvec = [...base.map(([, v]) => v), brief.sin_doy, brief.cos_doy];
  const mat = pool.map(vec);
  const mean = [], std = [];
  for (let j = 0; j < tvec.length; j++) {
    const col = mat.map(v => v[j]); const m = col.reduce((a, b) => a + b, 0) / col.length;
    mean[j] = m; std[j] = Math.sqrt(col.reduce((a, b) => a + (b - m) ** 2, 0) / col.length) || 1;
  }
  const z = v => v.map((x, j) => (x - mean[j]) / std[j]);
  const tz = z(tvec);
  const scored = pool.map((r, i) => {
    const zv = z(mat[i]); let dsum = 0;
    for (let j = 0; j < zv.length; j++) dsum += (zv[j] - tz[j]) ** 2;
    return { r, dist: Math.sqrt(dsum) };
  }).sort((a, b) => a.dist - b.dist);
  return { pool, analogs: scored.slice(0, k) };
}

function quantile(arr, q) {
  if (!arr.length) return null;
  const s = [...arr].sort((a, b) => a - b); const i = (s.length - 1) * q;
  const lo = Math.floor(i), hi = Math.ceil(i);
  return s[lo] + (s[hi] - s[lo]) * (i - lo);
}

async function loadBriefing() {
  try {
    const r = await fetch(new URL(`${BASE}/today/briefing.json`, location.href).href, { cache: 'no-store' });
    BRIEF = r.ok ? await r.json() : null;
  } catch { BRIEF = null; }
}

function renderBriefing() {
  const el = $('tx-briefing');
  if (!BRIEF) { el.innerHTML = `<p class="tx-note">No live briefing yet — the hourly feed (<code>today/briefing.json</code>) runs in season. The analog finder needs it.</p>`; return; }
  $('tx-brief-run').textContent = `HRRR run ${BRIEF.run?.slice(0, 16) || ''}Z`;
  const { analogs } = findAnalogs(BRIEF);
  const n = analogs.length;
  const cnt = t => analogs.filter(a => a.r.type === t).length;
  const pct = c => n ? Math.round(100 * c / n) : 0;
  const filled = analogs.filter(a => ['F', 'R', 'P'].includes(a.r.type));
  const onsets = filled.map(a => a.r.onset_lt_44013).filter(v => v != null);
  // vector-mean fill direction
  let ux = 0, uy = 0, nd = 0;
  for (const a of filled) if (a.r.dir_onset != null) { ux += Math.sin(a.r.dir_onset * Math.PI / 180); uy += Math.cos(a.r.dir_onset * Math.PI / 180); nd++; }
  const fillDir = nd ? (Math.atan2(ux, uy) * 180 / Math.PI + 360) % 360 : null;
  const g = BRIEF;
  const inputs = [
    ['Morning gradient', g.grad_dir != null ? `${DIRNAME(g.grad_dir)} ${Math.round(g.grad_dir)}° @ ${fmt1(g.grad_spd_kt)} kt` : '—',
      "Vector-mean 06–10 LT wind at buoy 44013 — the background flow before any sea breeze. Direction is °T (compass 'from')."],
    ['ΔT (land−sea)', g.dt_c != null ? `${fmt1(g.dt_c)} °C` : '—',
      'Forecast daytime land temp (near Bedford KBED) minus sea-surface temp. The sea-breeze engine — bigger ΔT → stronger breeze.'],
    ['Cloud / DSWRF (fcst AM)', `${g.tcdc_am ?? '—'}% / ${g.dswrf_am ?? '—'} W/m²`,
      'Forecast morning cloud cover and downward shortwave radiation (sunlight heating the land). Context only — not used in matching yet.'],
    ['SST', g.sst != null ? `${fmt1(g.sst)} °C` : '—', 'Sea-surface temperature at buoy 44013.'],
  ];
  const has = FIELD_DATES || new Set();
  el.innerHTML = `
    <div class="tx-brief-grid">
      <div class="tx-card">
        <h3>Today — ${g.date}</h3>
        <table class="tx-kv">${inputs.map(([k, v, tip]) => `<tr><td>${k}${tip ? ` <span class="tx-help" title="${tip}">?</span>` : ''}</td><td>${v}</td></tr>`).join('')}</table>
        <p class="tx-cardnote">Inputs from the latest HRRR run + obs-so-far. Hover “?” for definitions.</p>
      </div>
      <div class="tx-card">
        <h3>Analog outcome <span class="tx-hint">(${n} nearest days, ±45 DOY)</span></h3>
        <div class="tx-bigstat">${pct(cnt('F') + cnt('R') + cnt('P'))}%<span>fill onshore</span></div>
        <table class="tx-kv">
          <tr><td>Frontal flip (F) <span class="tx-help" title="Morning offshore breeze that rotates hard (≥90°) into an onshore sea breeze in the afternoon — the Marblehead classic.">?</span></td><td>${pct(cnt('F'))}%</td></tr>
          <tr><td>Reinforcement (R) <span class="tx-help" title="Morning S–SW that just builds and settles onshore in the afternoon — no big rotation.">?</span></td><td>${pct(cnt('R'))}%</td></tr>
          <tr><td>Pinned inshore (P) <span class="tx-help" title="Sea breeze fills only close to shore (KBOS/KBVY flip) while mid-bay 44013 never does. Tactically gold.">?</span></td><td>${pct(cnt('P'))}%</td></tr>
          <tr><td>No fill (N/G) <span class="tx-help" title="N = no organized fill. G = gradient-dominated (mean ≥15 kt, breeze never takes over).">?</span></td><td>${pct(cnt('N') + cnt('G'))}%</td></tr>
          <tr><td>Onset (p25–p50–p75) <span class="tx-help" title="Local clock time the onshore breeze filled at buoy 44013 across the fill-days, as 25th/50th/75th percentiles.">?</span></td><td>${onsets.length ? [0.25, 0.5, 0.75].map(q => fmtHM(quantile(onsets, q))).join(' – ') + ' LT' : '—'}</td></tr>
          <tr><td>Fill direction <span class="tx-help" title="Vector-mean wind direction (°T, compass 'from') at onset across the fill-days.">?</span></td><td>${fillDir != null ? DIRNAME(fillDir) + ' ' + Math.round(fillDir) + '°' : '—'}</td></tr>
        </table>
      </div>
    </div>
    <div class="tx-card tx-card-wide">
      <h3>Matched days <span class="tx-hint">(nearest analogs — click a highlighted day to replay its wind field)</span></h3>
      <div class="tx-analogs">${analogs.slice(0, 12).map(a => {
        const replay = has.has(a.r.date);
        return `<button class="tx-analog${replay ? '' : ' noplay'}" data-date="${a.r.date}" data-type="${a.r.type}" data-replay="${replay}"
           title="${replay ? 'Click to replay the wind field for this day' : 'No archived wind field for this day — its type came from buoy+ASOS data'}"
           style="border-left:4px solid ${TYPE_COLORS[a.r.type]}">
           <b>${a.r.date}</b> <span class="tag">${a.r.type}</span>${replay ? ' <span class="tx-play-dot">▶</span>' : ''}
           <span class="tx-hint">onset ${fmtHM(a.r.onset_lt_44013)}${a.r.onset_lt_44013 != null ? ' LT' : ''} · ΔT ${fmt1(a.r.dt_c)}°C · dist ${a.dist.toFixed(2)}</span>
         </button>`; }).join('')}</div>
    </div>
    <details class="tx-card tx-card-wide tx-legend-block">
      <summary><b>What these numbers mean</b> — glossary &amp; method (full write-up on the <a href="./tactics-methodology.html">Methodology page</a>)</summary>
      <table class="tx-kv tx-gloss">
        <tr><td><b>Fill onshore %</b></td><td>Share of the ${n} analog days that developed an onshore sea breeze (type F, R or P). Higher = more reliable breeze today.</td></tr>
        <tr><td><b>F / R / P / N / G</b></td><td>Day types from the classifier: <b>F</b> frontal flip, <b>R</b> reinforcement, <b>P</b> pinned inshore, <b>N</b> none, <b>G</b> gradient-dominated.</td></tr>
        <tr><td><b>Onset (LT)</b></td><td>Local-time hour the breeze filled at mid-bay buoy 44013 (e.g. 12.8 = 12:48). “—” = that day never filled.</td></tr>
        <tr><td><b>Fill direction</b></td><td>Compass direction (°T, “from”) the breeze came from at onset, vector-averaged over fill-days.</td></tr>
        <tr><td><b>Morning gradient</b></td><td>Today’s vector-mean 06–10 LT wind at 44013 — the background flow before any sea breeze.</td></tr>
        <tr><td><b>ΔT (land−sea)</b></td><td>Forecast daytime land temperature (near Bedford, KBED) minus sea-surface temp. The sea-breeze engine — bigger ΔT drives a stronger breeze.</td></tr>
        <tr><td><b>Cloud / DSWRF</b></td><td>Forecast morning cloud cover (%) and downward shortwave radiation (W/m²) — how much sun heats the land. Shown for context; not used in matching yet.</td></tr>
        <tr><td><b>SST</b></td><td>Sea-surface temperature at 44013.</td></tr>
        <tr><td><b>dist</b></td><td>How similar an analog day is to today (standardized Euclidean distance on gradient, ΔT, SST, day-of-year). Smaller = closer match; 0 would be identical.</td></tr>
        <tr><td><b>Replayable ▶</b></td><td>Only days in the wind-field archive can be replayed. The archive is built forward one day at a time, so most historical analogs have no field yet (their type still comes from buoy+ASOS data).</td></tr>
      </table>
    </details>`;
  el.querySelectorAll('.tx-analog').forEach(b => b.addEventListener('click', async () => {
    const date = b.dataset.date;
    if (b.dataset.replay === 'true') {
      switchView('replay'); $('tx-date').value = date; $('tx-date').dispatchEvent(new Event('change'));
    } else {
      setStatus(`No archived wind field for ${date} (type ${b.dataset.type}, from buoy+ASOS). Replay covers only recent days.`);
    }
  }));
}

$('tx-brief-fcst')?.addEventListener('click', async () => {
  switchView('replay');
  try { await loadFramesFromUrl(`${BASE}/today/latest.parquet`, "today's forecast F00–F18"); curFrame = 0; $('tx-scrub').value = 0; setTimeout(() => { map.invalidateSize(); render(); }, 30); }
  catch (e) { setStatus('no forecast feed yet'); }
});
$('tx-brief-print')?.addEventListener('click', () => window.print());

// ---- tab switching ----
async function switchView(view) {
  document.querySelectorAll('.tx-tab').forEach(t => t.classList.toggle('active', t.dataset.view === view));
  document.querySelectorAll('.tx-view').forEach(s => s.hidden = (s.id !== 'view-' + view));
  if (view === 'replay') { setTimeout(() => { map.invalidateSize(); render(); }, 30); }
  if (view === 'calendar' || view === 'stats' || view === 'briefing') {
    try {
      await loadLabels();
      if (view === 'calendar') { if (!$('tx-year').options.length) { populateYears(); buildTypeLegend(); } renderCalendar(); }
      else if (view === 'stats') renderStats();
      else { await Promise.all([loadBriefing(), loadFieldIndex()]); renderBriefing(); }
    } catch (e) { setStatus('labels unavailable: ' + e.message); }
  }
}
document.querySelectorAll('.tx-tab').forEach(t => t.addEventListener('click', () => switchView(t.dataset.view)));
$('tx-year')?.addEventListener('change', renderCalendar);
window.addEventListener('resize', () => { if (!$('view-stats').hidden) renderMoneyChart(); });

(async function main() {
  buildLegend();
  try {
    setStatus('starting DuckDB-WASM…');
    await Promise.all([initDuck(), loadGrid()]);
    // ensure Leaflet has the container's final size before fitting (else it
    // computes the zoom against a stale/smaller size and sits too far out)
    map.invalidateSize();
    map.fitBounds([[Math.min(...GRID.lats), Math.min(...GRID.lons)],
                   [Math.max(...GRID.lats), Math.max(...GRID.lons)]], { padding: [8, 8] });
    await loadDay($('tx-date').value);
    render();
    window.__tacticsReady = true;   // headless-test hook
  } catch (err) {
    setStatus('load failed: ' + err.message);
    window.__tacticsError = String(err);
  }
})();
