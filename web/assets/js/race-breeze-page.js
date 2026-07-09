// race-breeze-page.js — match every race day to its Bernot sea-breeze analysis.
// Cross-references the race API (/api/races) with the climatology breeze reports
// (climatology/breeze/<date>.json), shows the wind the boats actually had during
// each race, and links out to the race replay + the tactics sea-breeze analysis.
const API_BASE = window.SAILFRAMES_API_URL || window.location.origin;
const CLIMO = '/climatology';
const QCOLOR = { Q1: '#1f9d55', Q2: '#e8b13a', Q3: '#3a86c8', Q4: '#d64545' };
const DIRN = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
const dirName = d => (d == null ? '—' : DIRN[Math.round(d / 22.5) % 16]);
const $ = id => document.getElementById(id);

const localHour = iso => {
  const h = new Date(iso).toLocaleString('en-US', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false });
  return (+h.slice(0, 2)) + (+h.slice(3, 5)) / 60;
};
const localHM = iso => new Date(iso).toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false });
const fmtDate = d => new Date(d + 'T12:00:00').toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });

let STATE = { dates: [], byDate: new Map(), breeze: {}, regName: {} };

async function init() {
  try {
    const [regs, races, bidx] = await Promise.all([
      fetch(`${API_BASE}/api/regattas`).then(r => r.ok ? r.json() : { regattas: [] }).then(d => d.regattas || []).catch(() => []),
      fetch(`${API_BASE}/api/races`).then(r => r.json()).then(d => d.races || []),
      fetch(`${CLIMO}/breeze_index.json`).then(r => r.ok ? r.json() : []).catch(() => []),
    ]);
    STATE.regName = Object.fromEntries(regs.map(r => [r.regatta_id, r.name || r.regatta_name || 'Regatta']));
    const breezeDates = new Set(bidx);
    for (const r of races) {
      if (!r.date) continue;
      if (!STATE.byDate.has(r.date)) STATE.byDate.set(r.date, []);
      STATE.byDate.get(r.date).push(r);
    }
    STATE.dates = [...STATE.byDate.keys()].sort();
    // fetch the breeze report for every race date that has one (parallel, deduped)
    await Promise.all(STATE.dates.filter(d => breezeDates.has(d)).map(async d => {
      try { STATE.breeze[d] = await fetch(`${CLIMO}/breeze/${d}.json`).then(r => r.json()); } catch {}
    }));
    const nAnalysed = STATE.dates.filter(d => STATE.breeze[d]).length;
    const nRaces = [...STATE.byDate.values()].reduce((a, v) => a + v.length, 0);
    $('rb-stat').innerHTML = `<b>${STATE.dates.length}</b> race days · <b>${nRaces}</b> races · <b>${nAnalysed}</b> days with a sea-breeze analysis`;
    render();
  } catch (e) {
    $('rb-list').innerHTML = `<div class="rl-empty">Couldn't load races (${e.message}).</div>`;
  }
  ['rb-only', 'rb-estab'].forEach(id => $(id).addEventListener('change', render));
  $('rb-sort').addEventListener('change', render);
}

function windDuringRace(bz, race) {
  if (!bz || !bz.race_field_hourly) return null;
  const s = localHour(race.start_time), e = localHour(race.end_time || race.start_time);
  const rows = bz.race_field_hourly.filter(r => r[1] != null && r[0] >= Math.floor(s) && r[0] <= Math.ceil(e));
  if (!rows.length) return null;
  const a = rows[0], b = rows[rows.length - 1];
  const one = r => `${dirName(r[1])} ${Math.round(r[1])}° ${r[2].toFixed(0)}kt`;
  return rows.length > 1 ? `${one(a)} → ${one(b)}` : one(a);
}
function raceStatus(bz, race) {
  if (!bz) return { txt: 'no analysis', cls: 'st-na' };
  if (!bz.established) return { txt: 'no sea breeze that day', cls: 'st-no' };
  const onset = bz.onset_lt;
  if (onset == null) return { txt: 'sea breeze established', cls: 'st-yes' };
  const s = localHour(race.start_time), e = localHour(race.end_time || race.start_time);
  if (e < onset) return { txt: `pre-breeze — filled ~${Math.round(onset)}:00 LT`, cls: 'st-pre' };
  if (s >= onset) return { txt: 'raced in the sea breeze', cls: 'st-yes' };
  return { txt: `breeze filled mid-race ~${Math.round(onset)}:00 LT`, cls: 'st-mid' };
}

function dayVerdict(bz) {
  if (!bz) return `<span class="rb-noan">— no sea-breeze analysis for this day —</span>`;
  const q = bz.quadrant, badge = q ? `<span class="rb-qbadge" style="background:${QCOLOR[q] || '#566270'}">${q}</span>` : '';
  if (!bz.established) return `<span class="rb-verdict st-no">⛔ no sea breeze ${badge}</span>`;
  const onset = bz.onset_lt != null ? ` · filled ~${Math.round(bz.onset_lt)}:00 LT` : '';
  const peak = bz.peak_kt != null ? ` · peak ${Math.round(bz.peak_kt)} kt` : '';
  return `<span class="rb-verdict st-yes">🌬 sea breeze ${badge}${onset}${peak}</span>`;
}

function render() {
  const onlyBz = $('rb-only').checked, onlyEst = $('rb-estab').checked;
  const asc = $('rb-sort').value === 'oldest';
  let dates = STATE.dates.slice().sort((a, b) => asc ? a.localeCompare(b) : b.localeCompare(a));
  if (onlyBz) dates = dates.filter(d => STATE.breeze[d]);
  if (onlyEst) dates = dates.filter(d => STATE.breeze[d] && STATE.breeze[d].established);
  if (!dates.length) { $('rb-list').innerHTML = `<div class="rl-empty">No race days match.</div>`; return; }
  $('rb-list').innerHTML = dates.map(d => {
    const bz = STATE.breeze[d];
    const races = STATE.byDate.get(d).slice().sort((a, b) => (a.start_time || '').localeCompare(b.start_time || ''));
    const tacticsLink = bz ? `<a class="rb-day-link" href="./tactics.html?date=${d}&view=breeze">Sea-breeze analysis ▸</a>`
      : `<a class="rb-day-link" href="./tactics.html?date=${d}">Open day in Tactics ▸</a>`;
    const sum = bz && bz.summary_verdict ? `<div class="rb-sum">${bz.summary_verdict}</div>` : '';
    const rows = races.map(r => {
      const reg = r.regatta_id && STATE.regName[r.regatta_id] ? `<span class="rb-reg">${STATE.regName[r.regatta_id]} · </span>` : '';
      const time = r.end_time ? `${localHM(r.start_time)}–${localHM(r.end_time)}` : localHM(r.start_time);
      const boats = r.boat_count != null ? ` · ${r.boat_count} boats` : '';
      const wind = windDuringRace(bz, r), st = raceStatus(bz, r);
      const windHtml = `${wind ? wind + ' ' : ''}<span class="rb-status ${st.cls}">${st.txt}</span>`;
      return `<div class="rb-race">
        <span class="rb-race-name">${reg}${r.name || 'Race'}</span>
        <span class="rb-race-time">${time}${boats}</span>
        <span class="rb-race-wind">${windHtml}</span>
        <a class="rb-race-link" href="./race.html?race=${r.race_id}">Race replay ▸</a>
      </div>`;
    }).join('');
    return `<div class="rb-day">
      <div class="rb-day-head"><span class="rb-date">${fmtDate(d)}</span>${dayVerdict(bz)}${tacticsLink}</div>
      ${sum}
      <div class="rb-races">${rows}</div>
    </div>`;
  }).join('');
}

init();
