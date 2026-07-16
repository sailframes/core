/**
 * Events Management Application
 * Manages the three-level hierarchy: Regattas → Race Days → Races
 */

import {
    populateBoatClassDropdown as bcPopulateDropdown,
    setBoatClassInForm as bcSetInForm,
    getBoatClassFromForm as bcGetFromForm,
} from './boat-classes.js?v=2';

function isAdmin() {
    return document.cookie.includes('CF_Authorization');
}
const IS_ADMIN = isAdmin();

const API_BASE = window.SAILFRAMES_API_URL || window.location.origin;

// State
let regattas = [];
let raceDays = [];
let races = [];
let selectedRegattaId = null;
let selectedRaceDayId = null;
let editingRegatta = null;
let editingRaceDay = null;
let editingRace = null;

document.addEventListener('DOMContentLoaded', init);

async function init() {
    if (IS_ADMIN) {
        document.getElementById('btn-new-regatta').style.display = 'flex';
        document.getElementById('btn-new-raceday').style.display = 'flex';
        document.getElementById('btn-new-race').style.display = 'flex';
    }

    setupEventListeners();
    await loadRegattas();
}

// --- Data Loading ---

async function loadRegattas() {
    const list = document.getElementById('regattas-list');
    list.innerHTML = '<div class="column-empty">Loading...</div>';
    try {
        const resp = await fetch(`${API_BASE}/api/regattas`);
        const data = await resp.json();
        regattas = data.regattas || [];
        // Hide admin-only regattas from non-admins. Frontend obscurity that
        // mirrors the create-button gating above — the API stays open.
        if (!IS_ADMIN) {
            regattas = regattas.filter(r => (r.visibility || 'public') !== 'admin');
        }
        renderRegattas();
    } catch (err) {
        list.innerHTML = '<div class="column-empty">Failed to load</div>';
        console.error('[Events] loadRegattas:', err);
    }
}

// All races under the selected regatta, cached to keep race-day rendering
// consistent with race counts and to power date-based race filtering.
let regattaRaces = [];

async function loadRaceDays(regattaId) {
    const list = document.getElementById('racedays-list');
    list.innerHTML = '<div class="column-empty">Loading...</div>';
    document.getElementById('races-list').innerHTML = '<div class="column-empty">Select a race day</div>';
    races = [];
    regattaRaces = [];

    try {
        const rdUrl = regattaId
            ? `${API_BASE}/api/racedays?regatta_id=${regattaId}`
            : `${API_BASE}/api/racedays`;
        const raceUrl = regattaId
            ? `${API_BASE}/api/races?regatta_id=${regattaId}`
            : `${API_BASE}/api/races`;
        const [rdResp, raceResp] = await Promise.all([fetch(rdUrl), fetch(raceUrl)]);
        const rdData = await rdResp.json();
        const raceData = await raceResp.json();

        const explicit = rdData.race_days || [];
        regattaRaces = raceData.races || [];

        // Synthesize a raceday for any race date that has no matching explicit
        // raceday (same regatta_id + same date). Keeps events.html in sync with
        // races created on race.html that have no raceday_id.
        const explicitDates = new Set(explicit.map(d => d.date));
        const extraDates = new Set();
        for (const r of regattaRaces) {
            if (!r.date) continue;
            if (explicitDates.has(r.date)) continue;
            extraDates.add(r.date);
        }
        const synthetic = Array.from(extraDates).map(date => ({
            raceday_id: `date:${date}`,
            date,
            type: 'race_day',
            name: null,
            regatta_id: regattaId || null,
            race_ids: regattaRaces.filter(r => r.date === date).map(r => r.race_id),
            _synthetic: true,
        }));

        raceDays = [...explicit, ...synthetic];
        renderRaceDays();
    } catch (err) {
        list.innerHTML = '<div class="column-empty">Failed to load</div>';
        console.error('[Events] loadRaceDays:', err);
    }
}

async function loadRaces(raceDayId) {
    const list = document.getElementById('races-list');
    list.innerHTML = '<div class="column-empty">Loading...</div>';
    try {
        const raceDay = raceDays.find(d => d.raceday_id === raceDayId);
        if (!raceDay) {
            list.innerHTML = '<div class="column-empty">Race day not found</div>';
            return;
        }
        // Include races linked by raceday_id OR by matching (regatta_id, date).
        // The date fallback keeps legacy races (created before raceday_id existed)
        // visible under the appropriate day.
        const source = regattaRaces.length ? regattaRaces : (await (await fetch(`${API_BASE}/api/races`)).json()).races || [];
        races = source.filter(r =>
            (r.raceday_id && r.raceday_id === raceDay.raceday_id && !raceDay._synthetic) ||
            (r.date === raceDay.date && (r.regatta_id || null) === (raceDay.regatta_id || null))
        );
        renderRaces();
    } catch (err) {
        list.innerHTML = '<div class="column-empty">Failed to load</div>';
        console.error('[Events] loadRaces:', err);
    }
}

// --- Render ---

function renderRegattas() {
    const list = document.getElementById('regattas-list');

    if (regattas.length === 0) {
        list.innerHTML = '<div class="column-empty">No regattas yet.<br>Click + to create one.</div>';
        return;
    }

    list.innerHTML = regattas.map(r => {
        const sel = r.regatta_id === selectedRegattaId ? 'selected' : '';
        const dateRange = r.start_date
            ? `${fmtDate(r.start_date)}${r.end_date ? ' – ' + fmtDate(r.end_date) : ''}`
            : '';
        // boat_class may be a legacy string ("J/80") or the structured
        // object {id, name, loa_m, bow_offset_m}.
        const className = (r.boat_class && typeof r.boat_class === 'object')
            ? r.boat_class.name
            : r.boat_class;
        const meta = [r.venue, className].filter(Boolean).join(' · ');
        const actions = IS_ADMIN ? `
            <div class="event-item-actions">
                <button class="btn-item-action" data-action="edit-regatta" data-id="${r.regatta_id}" title="Edit">✎</button>
                <button class="btn-item-action danger" data-action="delete-regatta" data-id="${r.regatta_id}" title="Delete">✕</button>
            </div>` : '';

        return `<div class="event-item ${sel}" data-id="${r.regatta_id}" data-type="regatta">
            <div class="event-item-body">
                <div class="event-item-title">${esc(r.name)}</div>
                ${meta ? `<div class="event-item-subtitle">${esc(meta)}</div>` : ''}
                ${dateRange ? `<div class="event-item-subtitle">${dateRange}</div>` : ''}
            </div>
            ${actions}
        </div>`;
    }).join('');
}

function renderRaceDays() {
    const list = document.getElementById('racedays-list');

    if (raceDays.length === 0) {
        list.innerHTML = '<div class="column-empty">No race days.<br>Click + to create one.</div>';
        return;
    }

    const sorted = [...raceDays].sort((a, b) => a.date.localeCompare(b.date));

    list.innerHTML = sorted.map(d => {
        const sel = d.raceday_id === selectedRaceDayId ? 'selected' : '';
        const typeClass = d.type === 'training_day' ? 'training-day' : 'race-day';
        const typeLabel = d.type === 'training_day' ? 'Training' : 'Race Day';
        const title = d.name || fmtDateLong(d.date);
        const subtitle = d.name ? fmtDateLong(d.date) : '';
        const raceCount = regattaRaces.filter(r =>
            r.date === d.date && (r.regatta_id || null) === (d.regatta_id || null)
        ).length;
        const actions = IS_ADMIN ? `
            <div class="event-item-actions">
                <button class="btn-item-action" data-action="edit-raceday" data-id="${d.raceday_id}" title="${d._synthetic ? 'Promote to named race day' : 'Edit'}">✎</button>
                ${d._synthetic ? '' : `<button class="btn-item-action danger" data-action="delete-raceday" data-id="${d.raceday_id}" title="Delete">✕</button>`}
            </div>` : '';

        return `<div class="event-item ${sel}" data-id="${d.raceday_id}" data-type="raceday">
            <div class="event-item-body">
                <div class="event-item-title">${esc(title)}</div>
                ${subtitle ? `<div class="event-item-subtitle">${subtitle}</div>` : ''}
                <div class="stat-chips">
                    <span class="type-badge ${typeClass}">${typeLabel}</span>
                    ${raceCount ? `<span class="stat-chip">${raceCount} race${raceCount !== 1 ? 's' : ''}</span>` : ''}
                </div>
            </div>
            ${actions}
        </div>`;
    }).join('');
}

function renderRaces() {
    const list = document.getElementById('races-list');

    if (races.length === 0) {
        list.innerHTML = '<div class="column-empty">No races.<br>Click + to create one.</div>';
        return;
    }

    list.innerHTML = races.map(r => {
        const startTime = r.start_time
            ? new Date(r.start_time).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })
            : '--:--';
        const endTime = r.end_time
            ? new Date(r.end_time).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })
            : '--:--';
        const boats = r.boat_count || 0;
        const actions = IS_ADMIN ? `
            <div class="event-item-actions">
                <button class="btn-item-action" data-action="edit-race" data-id="${r.race_id}" title="Edit">✎</button>
                <button class="btn-item-action danger" data-action="delete-race" data-id="${r.race_id}" title="Delete">✕</button>
            </div>` : '';

        return `<div class="event-item" data-id="${r.race_id}" data-type="race">
            <div class="event-item-body">
                <div class="event-item-title">${esc(r.name)}</div>
                <div class="event-item-subtitle">${startTime} – ${endTime}</div>
                <div class="stat-chips">
                    ${boats ? `<span class="stat-chip">${boats} boat${boats !== 1 ? 's' : ''}</span>` : '<span class="stat-chip">no boats</span>'}
                    <a href="./race.html" class="race-dashboard-link" title="Open in Race Dashboard" onclick="event.stopPropagation()">→ Dashboard</a>
                </div>
            </div>
            ${actions}
        </div>`;
    }).join('');
}

// --- Event Listeners ---

function setupEventListeners() {
    document.getElementById('regattas-list').addEventListener('click', handleColumnClick);
    document.getElementById('racedays-list').addEventListener('click', handleColumnClick);
    document.getElementById('races-list').addEventListener('click', handleColumnClick);

    document.getElementById('btn-new-regatta').addEventListener('click', () => openRegattaModal());
    document.getElementById('btn-new-raceday').addEventListener('click', () => openRaceDayModal());
    document.getElementById('btn-new-race').addEventListener('click', () => openRaceModal());

    // Regatta modal
    document.getElementById('regatta-modal-close').addEventListener('click', closeRegattaModal);
    document.getElementById('regatta-cancel').addEventListener('click', closeRegattaModal);
    document.getElementById('regatta-save').addEventListener('click', saveRegatta);
    document.getElementById('btn-delete-regatta').addEventListener('click', () => editingRegatta && confirmDeleteRegatta(editingRegatta.regatta_id));
    document.querySelector('#regatta-modal .modal-backdrop').addEventListener('click', closeRegattaModal);

    // Race Day modal
    document.getElementById('raceday-modal-close').addEventListener('click', closeRaceDayModal);
    document.getElementById('raceday-cancel').addEventListener('click', closeRaceDayModal);
    document.getElementById('raceday-save').addEventListener('click', saveRaceDay);
    document.getElementById('btn-delete-raceday').addEventListener('click', () => editingRaceDay && confirmDeleteRaceDay(editingRaceDay.raceday_id));
    document.querySelector('#raceday-modal .modal-backdrop').addEventListener('click', closeRaceDayModal);

    // Race modal
    document.getElementById('race-modal-close').addEventListener('click', closeRaceModal);
    document.getElementById('race-cancel').addEventListener('click', closeRaceModal);
    document.getElementById('race-save').addEventListener('click', saveRace);
    document.getElementById('btn-delete-race').addEventListener('click', () => editingRace && confirmDeleteRace(editingRace.race_id));
    document.querySelector('#race-modal .modal-backdrop').addEventListener('click', closeRaceModal);
}

function handleColumnClick(e) {
    const actionBtn = e.target.closest('[data-action]');
    if (actionBtn) {
        e.stopPropagation();
        const { action, id } = actionBtn.dataset;
        if (action === 'edit-regatta') openRegattaModal(regattas.find(r => r.regatta_id === id));
        if (action === 'delete-regatta') confirmDeleteRegatta(id);
        if (action === 'edit-raceday') openRaceDayModal(raceDays.find(d => d.raceday_id === id));
        if (action === 'delete-raceday') confirmDeleteRaceDay(id);
        if (action === 'edit-race') openRaceModalById(id);
        if (action === 'delete-race') confirmDeleteRace(id);
        return;
    }

    const item = e.target.closest('.event-item');
    if (!item) return;

    const { type, id } = item.dataset;
    if (type === 'regatta') {
        selectedRegattaId = id;
        selectedRaceDayId = null;
        renderRegattas();
        loadRaceDays(id);
    } else if (type === 'raceday') {
        selectedRaceDayId = id;
        renderRaceDays();
        loadRaces(id);
    }
}

// --- Regatta Modal ---

function openRegattaModal(regatta = null) {
    editingRegatta = regatta;
    document.getElementById('regatta-modal-title').textContent = regatta ? 'Edit Regatta' : 'New Regatta';
    document.getElementById('regatta-name').value = regatta?.name || '';
    document.getElementById('regatta-venue').value = regatta?.venue || '';
    document.getElementById('regatta-rating-system').value = regatta?.rating_system || 'ORR-EZ';
    document.getElementById('regatta-start-sequence').value = String(regatta?.start_sequence_minutes || 5);
    document.getElementById('regatta-start-date').value = regatta?.start_date || '';
    document.getElementById('regatta-end-date').value = regatta?.end_date || '';
    document.getElementById('regatta-nor-url').value = regatta?.nor_url || '';
    document.getElementById('regatta-si-url').value = regatta?.si_url || '';
    document.getElementById('regatta-website-url').value = regatta?.website_url || '';

    // Boat class — same dropdown as the race-setup form, prefixed
    // with `regatta-` to keep the two modals' IDs unique.
    bcPopulateDropdown('regatta-');
    bcSetInForm(regatta?.boat_class || null, 'regatta-');

    document.getElementById('btn-delete-regatta').style.display = (regatta && IS_ADMIN) ? 'block' : 'none';
    document.getElementById('regatta-modal').style.display = 'flex';
    document.getElementById('regatta-name').focus();
}

function closeRegattaModal() {
    document.getElementById('regatta-modal').style.display = 'none';
    editingRegatta = null;
}

async function saveRegatta() {
    let boatClass;
    try { boatClass = bcGetFromForm('regatta-'); }
    catch (err) { alert(err.message); return; }

    const body = {
        name: document.getElementById('regatta-name').value.trim(),
        venue: document.getElementById('regatta-venue').value.trim(),
        boat_class: boatClass,
        rating_system: document.getElementById('regatta-rating-system').value,
        start_sequence_minutes: Number(document.getElementById('regatta-start-sequence').value) || 5,
        start_date: document.getElementById('regatta-start-date').value,
        end_date: document.getElementById('regatta-end-date').value,
        nor_url: document.getElementById('regatta-nor-url').value.trim() || null,
        si_url: document.getElementById('regatta-si-url').value.trim() || null,
        website_url: document.getElementById('regatta-website-url').value.trim() || null,
    };
    if (!body.name) { alert('Name is required'); return; }

    try {
        const url = editingRegatta
            ? `${API_BASE}/api/regattas/${editingRegatta.regatta_id}`
            : `${API_BASE}/api/regattas`;
        const resp = await fetch(url, {
            method: editingRegatta ? 'PATCH' : 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        closeRegattaModal();
        await loadRegattas();
    } catch (err) {
        alert(`Failed to save: ${err.message}`);
    }
}

async function confirmDeleteRegatta(id) {
    const r = regattas.find(x => x.regatta_id === id);
    if (!confirm(`Delete "${r?.name}"?\n\nThis will not delete its races or race days.`)) return;
    closeRegattaModal();
    try {
        const resp = await fetch(`${API_BASE}/api/regattas/${id}`, { method: 'DELETE' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        if (selectedRegattaId === id) {
            selectedRegattaId = null;
            document.getElementById('racedays-list').innerHTML = '<div class="column-empty">Select a regatta</div>';
            document.getElementById('races-list').innerHTML = '<div class="column-empty">Select a race day</div>';
        }
        await loadRegattas();
    } catch (err) {
        alert(`Failed to delete: ${err.message}`);
    }
}

// --- Race Day Modal ---

function openRaceDayModal(raceDay = null) {
    editingRaceDay = raceDay;
    document.getElementById('raceday-modal-title').textContent = raceDay ? 'Edit Race Day' : 'New Race Day';
    document.getElementById('raceday-date').value = raceDay?.date || todayISO();
    document.getElementById('raceday-type').value = raceDay?.type || 'race_day';
    document.getElementById('raceday-name').value = raceDay?.name || '';

    const sel = document.getElementById('raceday-regatta');
    sel.innerHTML = '<option value="">None</option>' + regattas.map(r => {
        const picked = raceDay?.regatta_id === r.regatta_id || (!raceDay && r.regatta_id === selectedRegattaId);
        return `<option value="${r.regatta_id}" ${picked ? 'selected' : ''}>${esc(r.name)}</option>`;
    }).join('');

    const isRealRaceDay = raceDay && !raceDay._synthetic;
    document.getElementById('btn-delete-raceday').style.display = (isRealRaceDay && IS_ADMIN) ? 'block' : 'none';
    document.getElementById('raceday-modal-title').textContent =
        isRealRaceDay ? 'Edit Race Day' : (raceDay?._synthetic ? 'Promote Race Day' : 'New Race Day');
    document.getElementById('raceday-modal').style.display = 'flex';
}

function closeRaceDayModal() {
    document.getElementById('raceday-modal').style.display = 'none';
    editingRaceDay = null;
}

async function saveRaceDay() {
    const body = {
        date: document.getElementById('raceday-date').value,
        type: document.getElementById('raceday-type').value,
        name: document.getElementById('raceday-name').value.trim() || null,
        regatta_id: document.getElementById('raceday-regatta').value || null,
    };
    if (!body.date) { alert('Date is required'); return; }

    // Synthetic racedays have no real row in the DB, so "edit" on them POSTs a
    // new raceday with the same date/regatta — effectively promoting them.
    const isPromote = editingRaceDay && editingRaceDay._synthetic;
    const isEdit = editingRaceDay && !editingRaceDay._synthetic;

    try {
        const url = isEdit
            ? `${API_BASE}/api/racedays/${editingRaceDay.raceday_id}`
            : `${API_BASE}/api/racedays`;
        const resp = await fetch(url, {
            method: isEdit ? 'PATCH' : 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const saved = await resp.json().catch(() => null);
        closeRaceDayModal();
        // If we just promoted the currently selected synthetic day, carry the
        // selection over to the newly created real raceday.
        if (isPromote && saved?.raceday_id && selectedRaceDayId === editingRaceDay?.raceday_id) {
            selectedRaceDayId = saved.raceday_id;
        }
        await loadRaceDays(selectedRegattaId);
    } catch (err) {
        alert(`Failed to save: ${err.message}`);
    }
}

async function confirmDeleteRaceDay(id) {
    const d = raceDays.find(x => x.raceday_id === id);
    const label = d?.name || fmtDateLong(d?.date);
    if (!confirm(`Delete "${label}"?\n\nThis will not delete the races in this day.`)) return;
    closeRaceDayModal();
    try {
        const resp = await fetch(`${API_BASE}/api/racedays/${id}`, { method: 'DELETE' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        if (selectedRaceDayId === id) {
            selectedRaceDayId = null;
            document.getElementById('races-list').innerHTML = '<div class="column-empty">Select a race day</div>';
        }
        await loadRaceDays(selectedRegattaId);
    } catch (err) {
        alert(`Failed to delete: ${err.message}`);
    }
}

// --- Race Modal ---

function openRaceModal(race = null) {
    editingRace = race;
    document.getElementById('race-modal-title').textContent = race ? 'Edit Race' : 'New Race';
    document.getElementById('race-name').value = race?.name || '';
    document.getElementById('race-date').value = race?.date || todayISO();

    if (race?.start_time) {
        const d = new Date(race.start_time);
        document.getElementById('race-start-time').value =
            d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } else {
        document.getElementById('race-start-time').value = '18:00:00';
    }

    if (race?.end_time) {
        const d = new Date(race.end_time);
        document.getElementById('race-end-time').value =
            d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } else {
        document.getElementById('race-end-time').value = '18:30:00';
    }

    const regattaSel = document.getElementById('race-regatta');
    regattaSel.innerHTML = '<option value="">None</option>' + regattas.map(r =>
        `<option value="${r.regatta_id}" ${race?.regatta_id === r.regatta_id ? 'selected' : ''}>${esc(r.name)}</option>`
    ).join('');

    const rdSel = document.getElementById('race-raceday');
    rdSel.innerHTML = '<option value="">None</option>' + raceDays.map(d => {
        const label = d.name || fmtDateLong(d.date);
        const picked = race?.raceday_id === d.raceday_id || (!race && d.raceday_id === selectedRaceDayId);
        return `<option value="${d.raceday_id}" ${picked ? 'selected' : ''}>${esc(label)}</option>`;
    }).join('');

    document.getElementById('btn-delete-race').style.display = (race && IS_ADMIN) ? 'block' : 'none';
    document.getElementById('race-modal').style.display = 'flex';
    document.getElementById('race-name').focus();
}

async function openRaceModalById(raceId) {
    try {
        const resp = await fetch(`${API_BASE}/api/races/${raceId}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        openRaceModal(await resp.json());
    } catch (err) {
        alert('Failed to load race');
    }
}

function closeRaceModal() {
    document.getElementById('race-modal').style.display = 'none';
    editingRace = null;
}

async function saveRace() {
    const date = document.getElementById('race-date').value;
    const startTime = document.getElementById('race-start-time').value || '00:00';
    const endTime = document.getElementById('race-end-time').value || '00:30';
    if (!date) { alert('Date is required'); return; }

    const body = {
        name: document.getElementById('race-name').value.trim() || `Race ${fmtDate(date)}`,
        date,
        start_time: new Date(`${date}T${startTime}`).toISOString(),
        end_time: new Date(`${date}T${endTime}`).toISOString(),
        regatta_id: document.getElementById('race-regatta').value || null,
        raceday_id: document.getElementById('race-raceday').value || null,
    };

    try {
        const url = editingRace
            ? `${API_BASE}/api/races/${editingRace.race_id}`
            : `${API_BASE}/api/races`;
        const resp = await fetch(url, {
            method: editingRace ? 'PATCH' : 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        closeRaceModal();
        if (selectedRaceDayId) await loadRaces(selectedRaceDayId);
    } catch (err) {
        alert(`Failed to save: ${err.message}`);
    }
}

async function confirmDeleteRace(raceId) {
    const r = races.find(x => x.race_id === raceId);
    if (!confirm(`Delete "${r?.name}"? This cannot be undone.`)) return;
    closeRaceModal();
    try {
        const resp = await fetch(`${API_BASE}/api/races/${raceId}`, { method: 'DELETE' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        if (selectedRaceDayId) await loadRaces(selectedRaceDayId);
    } catch (err) {
        alert(`Failed to delete: ${err.message}`);
    }
}

// --- Utils ---

function fmtDate(dateStr) {
    if (!dateStr) return '';
    return new Date(dateStr + 'T12:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function fmtDateLong(dateStr) {
    if (!dateStr) return '';
    return new Date(dateStr + 'T12:00:00').toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
}

function todayISO() {
    return new Date().toISOString().split('T')[0];
}

function esc(str) {
    return String(str || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}
