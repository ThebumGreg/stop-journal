'use strict';
// Editable day view. Server is the source of truth: every edit is a request that
// returns the refreshed day, then the list re-renders. Unsaved typing is kept in
// S.drafts (keyed 's<id>' / 'd<id>') so a re-render never loses it.
const S = {date: null, path: [], stops: [], drives: [], totals: {}, tool: null,
           open: new Set(), openDrives: new Set(), sel: null, drafts: {}, flash: ''};
const dateEl = document.getElementById('date');
const listEl = document.getElementById('list');
const footEl = document.getElementById('foot');

const map = L.map('map');
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'}).addTo(map);
map.setView([39, -98], 4);
const pathLayer = L.layerGroup().addTo(map);
const candLayer = L.layerGroup().addTo(map);
let markers = {};

// ---- helpers -----------------------------------------------------------
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
const dur = m => m >= 60 ? Math.floor(m / 60) + 'h ' + (m % 60) + 'm' : m + 'm';
const addMin = (hhmm, n) => {
  const [h, m] = hhmm.split(':').map(Number);
  const t = Math.min(Math.max(h * 60 + m + n, 0), 1439);
  return String(Math.floor(t / 60)).padStart(2, '0') + ':' + String(t % 60).padStart(2, '0');
};

async function api(method, url, body) {
  const r = await fetch(url, {method, headers: body ? {'Content-Type': 'application/json'} : {},
                              body: body ? JSON.stringify(body) : undefined});
  if (r.status === 401) { location.href = '/login'; throw new Error('Login required'); }
  let d = null;
  try { d = await r.json(); } catch (e) { /* non-JSON error page */ }
  if (!r.ok) throw new Error((d && d.error) || 'Request failed (' + r.status + ')');
  return d;
}

function applyDay(d) {
  S.stops = d.stops; S.drives = d.drives; S.totals = d.totals;
  if (d.path) S.path = d.path;
}

// Run a mutation; on success re-render, on failure show the message.
async function run(fn, errEl, onOk) {
  try {
    const d = await fn();
    if (onOk) onOk(d);
    applyDay(d);
    render(false);
  } catch (e) {
    if (errEl) errEl.textContent = e.message; else alert(e.message);
  }
}

// ---- selection / map ---------------------------------------------------
function setSel(id, pan) {
  S.sel = id;
  document.querySelectorAll('.card').forEach(c => c.classList.toggle('sel', +c.dataset.id === id));
  Object.entries(markers).forEach(([sid, m]) => {
    const on = +sid === id, node = m.getElement();
    const pin = node && node.querySelector('.pin');
    if (pin) pin.classList.toggle('sel', on);
    if (on) m.dragging.enable(); else m.dragging.disable();
  });
  if (pan && markers[id]) map.setView(markers[id].getLatLng(), Math.max(map.getZoom(), 16));
}

function setOpen(id, open) {
  if (open) S.open.add(id); else S.open.delete(id);
  const card = document.querySelector('.card[data-id="' + id + '"]');
  if (card) card.querySelector('.edit').hidden = !open;
}

function addMarker(s, i) {
  if (s.lat == null) return;
  const faded = s.kind === 'short' && s.status !== 'Confirmed';
  const icon = L.divIcon({className: '', iconSize: [26, 26], iconAnchor: [13, 13],
    html: '<div class="pin st-' + s.status + (faded ? ' short' : '') + (S.sel === s.id ? ' sel' : '') +
          '">' + (i + 1) + '</div>'});
  const m = L.marker([s.lat, s.lon], {icon, draggable: true}).addTo(pathLayer);
  if (S.sel !== s.id) m.dragging.disable();
  m.on('click', () => {
    setOpen(s.id, true); setSel(s.id, false);
    const card = document.querySelector('.card[data-id="' + s.id + '"]');
    if (card) card.scrollIntoView({block: 'nearest'});
  });
  m.on('dragend', () => {
    const p = m.getLatLng();
    run(() => api('PATCH', '/api/stops/' + s.id, {lat: p.lat, lon: p.lng}), null);
  });
  markers[s.id] = m;
}

// ---- business lookup ---------------------------------------------------
async function loadCands(s, box, q, pick) {
  box.replaceChildren(el('div', 'hint', 'Looking up places…'));
  candLayer.clearLayers();
  const lat = s.lat != null ? s.lat : map.getCenter().lat;
  const lon = s.lon != null ? s.lon : map.getCenter().lng;
  const u = new URLSearchParams({lat, lon, minutes: s.minutes});
  if (q) u.set('q', q);
  let d;
  try { d = await api('GET', '/api/suggest?' + u); }
  catch (e) { box.replaceChildren(el('div', 'err', 'Lookup failed (' + e.message + ').')); return; }
  box.replaceChildren();
  if (d.error) box.append(el('div', 'err', d.error));
  if (!d.candidates.length) box.append(el('div', 'hint', 'No matches. Try the search box, or type a name above.'));
  d.candidates.forEach(c => {
    const row = el('div', 'cand');
    row.append(el('div', 'n', c.name + (c.source === 'known' ? '  ★ known' : c.source === 'google' ? '  (Google)' : '')),
      el('div', 'a', [c.address, c.category, Math.round(c.distance_m) + ' m'].filter(Boolean).join(' · ')));
    row.onclick = () => {
      pick(c);
      candLayer.clearLayers();
      // Google's terms don't allow Places content on non-Google maps: no pin for those.
      if (c.source !== 'google') L.marker([c.lat, c.lon]).addTo(candLayer).bindPopup(c.name).openPopup();
    };
    box.append(row);
  });
}

// ---- stop card ---------------------------------------------------------
function labeled(label, input) {
  const w = el('div'); w.append(el('label', null, label), input); return w;
}

function stopCard(s, i) {
  const card = el('div', 'card st-' + s.status + (s.kind === 'short' ? ' short' : ''));
  card.dataset.id = s.id;
  if (S.sel === s.id) card.classList.add('sel');

  const head = el('div', 'head');
  const body = el('div', 'body');
  const t = el('div', 't', s.arrive + ' – ' + s.depart + ' · ' + dur(s.minutes));
  if (s.kind === 'short') t.append(' ', el('span', 'chip', 'short'));
  body.append(t, el('div', 'm', s.business_name || (s.source === 'manual' ? 'Manual stop' : 'Unnamed stop')));
  head.append(el('div', 'num', String(i + 1)), body, el('span', 'chip', s.status));
  head.onclick = () => {
    const open = !S.open.has(s.id);
    setOpen(s.id, open);
    if (open) setSel(s.id, true);
  };

  const edit = el('div', 'edit');
  edit.hidden = !S.open.has(s.id);
  buildEdit(edit, s, i);
  card.append(head, edit);
  return card;
}

function buildEdit(edit, s, i) {
  const key = 's' + s.id;
  const d = Object.assign({business_name: s.business_name, address: s.address, notes: s.notes,
    arrive: s.arrive, depart: s.depart, status: s.status, category: s.category,
    known_site_id: s.known_site_id}, S.drafts[key] || {});
  const remember = (k, v) => { d[k] = v; (S.drafts[key] = S.drafts[key] || {})[k] = v; };
  const err = el('div', 'err');

  const nameIn = el('input'); nameIn.value = d.business_name; nameIn.placeholder = 'Business name';
  nameIn.oninput = () => { remember('business_name', nameIn.value); remember('known_site_id', null); };
  const addrIn = el('input'); addrIn.value = d.address; addrIn.placeholder = 'Address';
  addrIn.oninput = () => remember('address', addrIn.value);

  // business finder
  const find = el('button', null, 'Find business');
  let panel = null;
  find.onclick = () => {
    if (panel) { panel.remove(); panel = null; candLayer.clearLayers(); return; }
    panel = el('div', 'panel');
    const form = el('form'), inp = el('input'), go = el('button', null, 'Search');
    inp.placeholder = 'Search by name or address'; go.type = 'submit';
    const box = el('div');
    form.append(inp, go); panel.append(form, box);
    const pick = c => {
      remember('business_name', c.name); remember('address', c.address);
      remember('category', c.category);
      remember('known_site_id', c.source === 'known' ? c.known_site_id : null);
      nameIn.value = c.name; addrIn.value = c.address;
    };
    form.onsubmit = e => { e.preventDefault(); loadCands(s, box, inp.value.trim(), pick); };
    find.after(panel);
    loadCands(s, box, '', pick);
  };

  const arrIn = el('input'); arrIn.type = 'time'; arrIn.value = d.arrive;
  arrIn.oninput = () => remember('arrive', arrIn.value);
  const depIn = el('input'); depIn.type = 'time'; depIn.value = d.depart;
  depIn.oninput = () => remember('depart', depIn.value);
  const times = el('div', 'two'); times.append(labeled('Arrived', arrIn), labeled('Departed', depIn));

  const notes = el('textarea'); notes.value = d.notes; notes.placeholder = 'Notes';
  notes.oninput = () => remember('notes', notes.value);

  const seg = el('div', 'seg');
  const segBtns = ['Draft', 'Confirmed', 'Personal'].map(st => {
    const b = el('button', d.status === st ? 'on' : '', st); b.type = 'button';
    b.onclick = () => {
      remember('status', st);
      segBtns.forEach(x => x.classList.toggle('on', x.textContent === st));
    };
    return b;
  });
  segBtns.forEach(b => seg.append(b));

  async function save() {
    err.textContent = '';
    const body = {};
    ['business_name', 'address', 'notes', 'arrive', 'depart', 'status', 'category']
      .forEach(k => { if (d[k] !== s[k]) body[k] = d[k]; });
    if (d.known_site_id !== s.known_site_id) body.known_site_id = d.known_site_id;
    if (!Object.keys(body).length) { err.textContent = 'Nothing to save.'; return; }
    await run(() => api('PATCH', '/api/stops/' + s.id, body), err, () => { delete S.drafts[key]; });
  }

  const acts = el('div', 'acts');
  const saveB = el('button', 'primary', 'Save'); saveB.onclick = save;
  const confB = el('button', null, 'Confirm');
  confB.onclick = () => { remember('status', 'Confirmed'); segBtns.forEach(x => x.classList.toggle('on', x.textContent === 'Confirmed')); save(); };
  acts.append(saveB, confB);

  // split
  const splitRow = el('div', 'two'); splitRow.hidden = true;
  const splitIn = el('input'); splitIn.type = 'time';
  const splitGo = el('button', null, 'Split here');
  splitGo.onclick = () => run(() => api('POST', '/api/stops/' + s.id + '/split', {at: splitIn.value}), err);
  splitRow.append(splitIn, splitGo);
  const splitB = el('button', null, 'Split…');
  splitB.onclick = () => {
    splitIn.value = addMin(s.arrive, Math.round(s.minutes / 2));
    splitRow.hidden = !splitRow.hidden;
  };
  acts.append(splitB);

  const prev = S.stops[i - 1], next = S.stops[i + 1];
  const mergeWith = other => run(() => api('POST', '/api/stops/merge', {ids: [s.id, other.id]}), err);
  if (prev) { const b = el('button', null, 'Merge ↑'); b.title = 'Merge with the previous stop'; b.onclick = () => mergeWith(prev); acts.append(b); }
  if (next) { const b = el('button', null, 'Merge ↓'); b.title = 'Merge with the next stop'; b.onclick = () => mergeWith(next); acts.append(b); }
  const del = el('button', 'danger', s.kind === 'short' ? 'Dismiss' : 'Delete');
  del.onclick = () => {
    if (!confirm('Delete this stop? Raw GPS points are kept; only this entry is removed.')) return;
    run(() => api('DELETE', '/api/stops/' + s.id), err, () => { delete S.drafts[key]; S.open.delete(s.id); });
  };
  acts.append(del);

  edit.append(labeled('Business', nameIn), labeled('Address', addrIn), find, times,
              labeled('Notes', notes), seg, acts, splitRow, err);
  if (s.lat != null) edit.append(el('div', 'hint', 'Tip: tap this stop\'s pin on the map, then drag it to move the location.'));
}

// ---- drive row ---------------------------------------------------------
function driveRow(dr) {
  const key = 'd' + dr.id;
  const row = el('div', 'drive');
  row.append(el('div', null, '↓ Drive ' + dr.depart + ' – ' + dr.arrive + ' · ' + dur(dr.minutes) +
    ' · ~' + dr.miles_approx + ' mi (approx.)' + (dr.notes ? ' · has notes' : '')));
  const edit = el('div', 'edit'); edit.hidden = !S.openDrives.has(dr.id);
  const d = Object.assign({depart: dr.depart, arrive: dr.arrive, notes: dr.notes}, S.drafts[key] || {});
  const remember = (k, v) => { d[k] = v; (S.drafts[key] = S.drafts[key] || {})[k] = v; };
  const dep = el('input'); dep.type = 'time'; dep.value = d.depart; dep.oninput = () => remember('depart', dep.value);
  const arr = el('input'); arr.type = 'time'; arr.value = d.arrive; arr.oninput = () => remember('arrive', arr.value);
  const notes = el('textarea'); notes.value = d.notes; notes.placeholder = 'Drive notes';
  notes.oninput = () => remember('notes', notes.value);
  const two = el('div', 'two'); two.append(labeled('Departed', dep), labeled('Arrived', arr));
  const err = el('div', 'err');
  const save = el('button', 'primary', 'Save drive');
  save.onclick = () => {
    const body = {};
    ['depart', 'arrive', 'notes'].forEach(k => { if (d[k] !== dr[k]) body[k] = d[k]; });
    if (!Object.keys(body).length) { err.textContent = 'Nothing to save.'; return; }
    run(() => api('PATCH', '/api/drives/' + dr.id, body), err, () => { delete S.drafts[key]; });
  };
  const acts = el('div', 'acts'); acts.append(save);
  edit.append(two, labeled('Notes', notes), acts, err);
  row.append(edit);
  row.firstChild.onclick = () => {
    if (S.openDrives.has(dr.id)) S.openDrives.delete(dr.id); else S.openDrives.add(dr.id);
    edit.hidden = !S.openDrives.has(dr.id);
  };
  return row;
}

// ---- toolbar -----------------------------------------------------------
function toolbar() {
  const bar = el('div', 'toolbar');
  const mk = (label, fn) => { const b = el('button', null, label); b.onclick = fn; bar.append(b); return b; };
  mk('+ Add stop', () => { S.tool = S.tool === 'add' ? null : 'add'; render(false); });
  mk('Sync new data', async () => {
    try {
      const d = await api('POST', '/api/day/' + S.date + '/sync');
      S.flash = d.added ? d.added + ' new stop' + (d.added > 1 ? 's' : '') + ' found.' : 'No new stops.';
      applyDay(d); render(true);
    } catch (e) { alert(e.message); }
  });
  mk('Re-detect…', () => { S.tool = S.tool === 'redo' ? null : 'redo'; render(false); });
  if (S.flash) bar.append(el('span', 'hint', S.flash));
  return bar;
}

function addForm() {
  const f = el('div', 'redo');
  const last = S.stops[S.stops.length - 1];
  const a = el('input'); a.type = 'time'; a.value = last ? last.depart : '09:00';
  const d = el('input'); d.type = 'time'; d.value = addMin(a.value, 30);
  const n = el('input'); n.placeholder = 'Business name (optional)';
  const err = el('div', 'err');
  const go = el('button', null, 'Add stop');
  go.onclick = () => run(() => api('POST', '/api/day/' + S.date + '/stops',
    {arrive: a.value, depart: d.value, business_name: n.value}), err, () => { S.tool = null; });
  const two = el('div', 'two'); two.append(labeled('Arrived', a), labeled('Departed', d));
  f.append(el('div', 'hint', 'Add a stop that has no GPS data (e.g. a phone job).'), two, n, go, err);
  return f;
}

function redoForm() {
  const f = el('div', 'redo');
  f.append(el('div', 'hint', 'Detect again with different thresholds. Leave blank for your saved defaults. ' +
    'This discards edits to detected stops for this day; manual stops are kept.'));
  const row = el('div', 'r');
  const mk = (label) => { const i = el('input'); i.inputMode = 'numeric'; row.append(label, i); return i; };
  const min = mk('min (min)'), rad = mk('radius (m)'), gap = mk('gap (min)');
  const err = el('div', 'err');
  const go = el('button', null, 'Re-detect');
  go.onclick = () => {
    if (!confirm('Discard edits to detected stops for this day and detect again?')) return;
    const body = {};
    if (min.value) body.min = min.value;
    if (rad.value) body.radius = rad.value;
    if (gap.value) body.gap = gap.value;
    run(() => api('POST', '/api/day/' + S.date + '/redetect', body), err,
        () => { S.tool = null; S.drafts = {}; S.open.clear(); S.openDrives.clear(); S.sel = null; });
  };
  f.append(row, go, err);
  return f;
}

// ---- render ------------------------------------------------------------
function render(fit) {
  const keep = listEl.scrollTop;
  pathLayer.clearLayers(); candLayer.clearLayers(); markers = {};
  listEl.replaceChildren(toolbar());
  if (S.tool === 'add') listEl.append(addForm());
  if (S.tool === 'redo') listEl.append(redoForm());
  const bounds = [];
  if (S.path.length) {
    L.polyline(S.path, {color: '#0b66d0', weight: 3, opacity: .7}).addTo(pathLayer);
    S.path.forEach(p => bounds.push(p));
  }
  if (!S.stops.length) {
    const m = el('div', null, S.path.length ? 'No stops detected for this day.' : 'No location points for this day.');
    m.id = 'msg'; listEl.append(m);
  }
  S.stops.forEach((s, i) => {
    listEl.append(stopCard(s, i));
    addMarker(s, i);
    if (s.lat != null && !S.path.length) bounds.push([s.lat, s.lon]);
    const dr = S.drives.find(x => x.from_stop_id === s.id);
    if (dr) listEl.append(driveRow(dr));
  });
  if (fit && bounds.length) map.fitBounds(bounds, {padding: [30, 30]});
  listEl.scrollTop = keep;
  const t = S.totals || {};
  const drafts = t.draft ? ' · ' + t.draft + ' draft' + (t.draft_short ? ' (+' + t.draft_short + ' short)' : '') : '';
  footEl.textContent = 'On site ' + dur(Math.round((t.on_site_s || 0) / 60)) + ' · Driving ' +
    dur(Math.round((t.drive_s || 0) / 60)) + ' · ~' + (t.miles_approx || 0) + ' mi · ' + (t.confirmed || 0) +
    ' confirmed' + drafts + (t.personal ? ' · ' + t.personal + ' personal' : '');
  footEl.style.color = t.draft ? 'var(--err)' : '';
}

async function load() {
  S.date = dateEl.value;
  history.replaceState(null, '', '/day/' + S.date);
  S.tool = null; S.sel = null; S.flash = ''; S.drafts = {}; S.open.clear(); S.openDrives.clear();
  pathLayer.clearLayers(); candLayer.clearLayers(); markers = {};
  const m = el('div', null, 'Loading…'); m.id = 'msg'; listEl.replaceChildren(m);
  try {
    applyDay(await api('GET', '/api/day/' + S.date));
  } catch (e) { m.textContent = 'Could not load this day (' + e.message + ').'; return; }
  render(true);
}

function shift(n) {
  const x = new Date(dateEl.value + 'T12:00:00'); x.setDate(x.getDate() + n);
  dateEl.value = x.toLocaleDateString('en-CA'); load();
}
document.getElementById('prev').onclick = () => shift(-1);
document.getElementById('next').onclick = () => shift(1);
dateEl.onchange = load;
load();
