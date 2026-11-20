'use strict';
// Editable day view. Server is the source of truth: every edit is a request that
// returns the refreshed day, then the list re-renders. Unsaved typing is kept in
// S.drafts (keyed 's<id>' / 'd<id>') so a re-render never loses it.
const S = {parts: [], date: null, path: [], stops: [], drives: [], totals: {}, tool: null,
           open: new Set(), openDrives: new Set(), sel: null, drafts: {}, flash: ''};
const dateEl = document.getElementById('date');
const barEl = document.getElementById('bar');
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
  S.stops = d.stops; S.drives = d.drives; S.totals = d.totals; S.undo = d.undo || null;
  S.marks = d.marks || {}; S.endDay = d.end_day || null; S.autoMin = d.auto_sync_min;
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
  const icon = L.divIcon({className: '', iconSize: [30, 30], iconAnchor: [15, 15],
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
async function loadCands(s, box, q, pick, google) {
  box.replaceChildren(el('div', 'hint', 'Looking up places…'));
  candLayer.clearLayers();
  const lat = s.lat != null ? s.lat : map.getCenter().lat;
  const lon = s.lon != null ? s.lon : map.getCenter().lng;
  const u = new URLSearchParams({lat, lon, minutes: s.minutes});
  if (q) u.set('q', q);
  if (google) u.set('google', '1');
  let d;
  try { d = await api('GET', '/api/suggest?' + u); }
  catch (e) { box.replaceChildren(el('div', 'err', 'Lookup failed (' + e.message + ').')); return; }
  box.replaceChildren();
  if (d.error) box.append(el('div', 'err', d.error));
  if (!d.candidates.length) box.append(el('div', 'hint', 'No matches. Try the search box, or type a name above.'));
  d.candidates.forEach(c => {
    const row = el('div', 'cand');
    row.append(el('div', 'n', c.name + (c.source === 'known' ? '  ★ ' + (c.kind && c.kind !== 'client' ? c.kind : 'known') : c.source === 'google' ? '  (Google)' : '')),
      el('div', 'a', [c.address, c.category, Math.round(c.distance_m) + ' m'].filter(Boolean).join(' · ')));
    row.onclick = () => {
      pick(c);
      candLayer.clearLayers();
      // Google's terms don't allow Places content on non-Google maps: no pin for those.
      if (c.source !== 'google') L.marker([c.lat, c.lon]).addTo(candLayer).bindPopup(c.name).openPopup();
    };
    box.append(row);
  });
  box.append(el('div', 'hint', 'Searched: ' + d.sources.join(', ') + '.'));
  if (d.google_available && !d.google_used) {
    const g = el('button', null, 'Also search Google');
    g.type = 'button';
    g.onclick = () => loadCands(s, box, q, pick, true);
    box.append(g);
  }
}

// ---- parts catalog (autocomplete) --------------------------------------
const partList = document.createElement('datalist');
partList.id = 'partlist';
document.body.append(partList);
const expList = document.createElement('datalist');
expList.id = 'explist';
document.body.append(expList);
async function loadParts() {
  try {
    S.parts = (await api('GET', '/api/parts')).parts;
    partList.replaceChildren(); expList.replaceChildren();
    S.parts.forEach(p => {
      const o = document.createElement('option');
      o.value = p.name; if (p.sku) o.label = p.sku;
      (p.kind === 'expense' ? expList : partList).append(o);
    });
  } catch (e) { /* autocomplete is optional */ }
}

function partsBlock(s, err) {
  const box = el('div');
  box.append(el('label', null, 'Parts used'));
  (s.parts || []).filter(p => !p.expense).forEach(p => {
    const row = el('div', 'prow');
    row.append(el('div', 'pn', p.name + (p.sku ? ' · ' + p.sku : '') + (p.unit && p.unit !== 'ea' ? ' (' + p.unit + ')' : '') + (p.consumable ? ' · consumable' : '')));
    const q = el('input'); q.type = 'number'; q.step = 'any'; q.min = '0'; q.value = p.qty; q.title = 'Quantity';
    q.onchange = () => run(() => api('PATCH', '/api/stop-parts/' + p.id, {qty: q.value}), err);
    const b = el('input'); b.type = 'checkbox'; b.checked = p.billable; b.title = 'Billable';
    b.onchange = () => run(() => api('PATCH', '/api/stop-parts/' + p.id, {billable: b.checked}), err);
    const x = el('button', 'danger', '✕'); x.type = 'button'; x.title = 'Remove (returns to truck)';
    x.onclick = () => run(() => api('DELETE', '/api/stop-parts/' + p.id), err);
    row.append(q, b, x);
    box.append(row);
  });
  if ((s.usual || []).length) {
    const u = el('div', 'usual');
    u.append(el('span', 'hint', 'Usual here:'));
    s.usual.forEach(p => {
      const b = el('button', 'quick', '+ ' + p.name + ' × ' + p.qty); b.type = 'button';
      b.onclick = () => run(() => api('POST', '/api/stops/' + s.id + '/parts', {part_id: p.part_id, qty: p.qty}), err, () => { loadParts(); });
      u.append(b);
    });
    box.append(u);
  }
  const add = el('div', 'prow add');
  const name = el('input'); name.setAttribute('list', 'partlist'); name.placeholder = 'Add part (name or part #)';
  const qty = el('input'); qty.type = 'number'; qty.step = 'any'; qty.min = '0'; qty.value = '1'; qty.title = 'Quantity';
  const go = el('button', null, 'Add'); go.type = 'button';
  go.onclick = () => {
    const v = name.value.trim();
    if (!v) { err.textContent = 'Enter a part name.'; return; }
    const m = S.parts.find(p => p.name.toLowerCase() === v.toLowerCase() || (p.sku && p.sku.toLowerCase() === v.toLowerCase()));
    const body = {qty: qty.value, name: v};
    if (m) body.part_id = m.id;
    run(() => api('POST', '/api/stops/' + s.id + '/parts', body), err, () => { loadParts(); });
  };
  add.append(name, qty, go);
  box.append(add);
  return box;
}

// ---- notes & parts sheet (slides up over the day) ------------------------------
// Work performed, category and the other job fields autosave a moment after you stop typing (and when you leave
// a field); the status line says whether what you typed is saved. Parts, expenses and follow-ups save as you add them.
const CATS = ['Time & Material', 'Quoted project', 'White-label migration', 'Internal operations',
              'Administrative', 'Customer outreach', 'Training'];
const RESULTS = [['complete', 'Complete'], ['return', 'Return visit needed'], ['waiting', 'Waiting (parts or customer)']];
const FIELDS = ['notes', 'work_category', 'ticket', 'visit_result'];
const sheet = {id: null, vals: {}, timer: null, saving: false, again: false, failed: false};
const backEl = el('div', 'sheet-back'), sheetEl = el('div', 'sheet');
backEl.hidden = sheetEl.hidden = true;
document.body.append(backEl, sheetEl);
let sh = {};

const curStop = () => S.stops.find(x => x.id === sheet.id);
const sheetDirty = () => { const s = curStop(); return !!s && FIELDS.some(k => sheet.vals[k] !== s[k]); };

function setStat(kind, msg) {
  if (!sh.stat) return;
  const t = {unsaved: 'Unsaved changes', saving: 'Saving…', saved: 'Saved ' + new Date().toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}),
             clean: 'Saved', error: msg || 'Could not save'}[kind];
  sh.stat.textContent = t; sh.stat.className = 'sheet-stat ' + kind;
}
function touched() {
  setStat('unsaved'); clearTimeout(sheet.timer);
  sheet.timer = setTimeout(saveSheet, 2000);
}

// Saves what changed; returns true when everything is saved.
async function saveSheet() {
  clearTimeout(sheet.timer);
  const s = curStop();
  if (!s) return true;
  if (sheet.saving) { sheet.again = true; return false; }
  const body = {};
  FIELDS.forEach(k => { if (sheet.vals[k] !== s[k]) body[k] = sheet.vals[k]; });
  if (!Object.keys(body).length) { if (!sheet.failed) setStat(sh.everSaved ? 'saved' : 'clean'); return true; }
  sheet.saving = true; setStat('saving');
  try {
    applyDay(await api('PATCH', '/api/stops/' + s.id, body));
    sheet.failed = false; sh.everSaved = true;
    render(false);
  } catch (e) {
    sheet.failed = true; setStat('error', 'Could not save: ' + e.message);
  }
  sheet.saving = false;
  if (!sheet.failed) {
    if (sheet.again || sheetDirty()) { sheet.again = false; return saveSheet(); }
    setStat('saved');
  }
  return !sheet.failed && !sheetDirty();
}

async function closeSheet(force) {
  if (!force && sheet.id) {
    const ok = await saveSheet();
    if (!ok && !confirm('Your changes could not be saved yet. Close anyway?')) return;
  }
  clearTimeout(sheet.timer);
  sheet.id = null; sheet.failed = false;
  sheetEl.hidden = backEl.hidden = true;
  document.body.classList.remove('sheet-open');
  sheetEl.replaceChildren();
}
backEl.onclick = () => closeSheet();
document.addEventListener('keydown', e => { if (e.key === 'Escape' && sheet.id) closeSheet(); });
window.addEventListener('beforeunload', e => { if (sheet.id && sheetDirty()) { e.preventDefault(); e.returnValue = ''; } });
if (window.visualViewport) {          // keep the sheet above the iPhone keyboard
  const fit = () => {
    const v = window.visualViewport;
    sheetEl.style.bottom = Math.max(0, window.innerHeight - v.height - v.offsetTop) + 'px';
    sheetEl.style.maxHeight = Math.round(v.height * 0.94) + 'px';
  };
  window.visualViewport.addEventListener('resize', fit);
  window.visualViewport.addEventListener('scroll', fit);
}

function expensesBlock(s, err) {
  const box = el('div');
  box.append(el('label', null, 'Expenses (parking, tolls, purchases)'));
  (s.parts || []).filter(p => p.expense).forEach(p => {
    const row = el('div', 'prow');
    row.append(el('div', 'pn', p.name));
    const a = el('input'); a.type = 'number'; a.step = '0.01'; a.min = '0'; a.inputMode = 'decimal'; a.title = 'Amount';
    a.value = p.unit_cost == null ? '' : p.unit_cost;
    a.onchange = () => run(() => api('PATCH', '/api/stop-parts/' + p.id, {unit_cost: a.value}), err);
    const b = el('input'); b.type = 'checkbox'; b.checked = p.billable; b.title = 'Billable to the customer';
    b.onchange = () => run(() => api('PATCH', '/api/stop-parts/' + p.id, {billable: b.checked}), err);
    const x = el('button', 'danger', '✕'); x.type = 'button'; x.title = 'Remove';
    x.onclick = () => run(() => api('DELETE', '/api/stop-parts/' + p.id), err);
    row.append(a, b, x);
    box.append(row);
  });
  const add = el('div', 'prow add');
  const name = el('input'); name.setAttribute('list', 'explist'); name.placeholder = 'Expense (e.g. Parking)';
  const amt = el('input'); amt.type = 'number'; amt.step = '0.01'; amt.min = '0'; amt.inputMode = 'decimal'; amt.placeholder = '$'; amt.title = 'Amount';
  const go = el('button', null, 'Add'); go.type = 'button';
  go.onclick = () => {
    const v = name.value.trim();
    if (!v) { err.textContent = 'Enter what the expense was.'; return; }
    const m = S.parts.find(p => p.name.toLowerCase() === v.toLowerCase());
    if (m && m.kind !== 'expense') { err.textContent = '“' + v + '” is a ' + m.kind + ' in your catalog. Use another name.'; return; }
    const body = {name: v, qty: 1, unit_cost: amt.value};
    if (m) body.part_id = m.id; else body.type = 'expense';
    run(() => api('POST', '/api/stops/' + s.id + '/parts', body), err, () => { loadParts(); });
  };
  add.append(name, amt, go);
  box.append(add);
  return box;
}

function followBlock(s, err) {
  const box = el('div');
  box.append(el('label', null, 'Follow-ups (what is still open)'));
  (s.follow_ups || []).forEach(f => {
    const row = el('div', 'fu');
    const patch = body => run(() => api('PATCH', '/api/follow-ups/' + f.id, body), err);
    const t = el('input'); t.value = f.text; t.onchange = () => patch({text: t.value});
    const o = el('input'); o.value = f.owner; o.placeholder = 'Owner'; o.onchange = () => patch({owner: o.value});
    const d = el('input'); d.type = 'date'; d.value = f.due; d.title = 'Due date'; d.onchange = () => patch({due: d.value});
    const x = el('button', 'danger', '✕'); x.type = 'button'; x.title = 'Remove';
    x.onclick = () => run(() => api('DELETE', '/api/follow-ups/' + f.id), err);
    const two = el('div', 'two'); two.append(o, d);
    row.append(t, two, x);
    if (!f.owner || !f.due) row.append(el('div', 'need', 'Needs ' + (!f.owner && !f.due ? 'an owner and a due date' : !f.owner ? 'an owner' : 'a due date') + '.'));
    box.append(row);
  });
  const add = el('div', 'prow add one');
  const t = el('input'); t.placeholder = 'What is still open?';
  const go = el('button', null, 'Add'); go.type = 'button';
  go.onclick = () => {
    if (!t.value.trim()) { err.textContent = 'Say what the follow-up is.'; return; }
    run(() => api('POST', '/api/stops/' + s.id + '/follow-ups', {text: t.value, owner: 'Me', due: ''}), err);
  };
  add.append(t, go);
  box.append(add);
  box.append(el('div', 'hint', '“Me” is you; change the owner if someone else has it. Pick a due date.'));
  return box;
}

// the parts of the sheet that come from the server are rebuilt after every change; typed fields are left alone
function refreshSheet() {
  const s = curStop();
  if (!s) { closeSheet(true); return; }
  const n = S.stops.indexOf(s) + 1;
  sh.title.textContent = n + ' · ' + (s.business_name || 'Stop') + ' · ' + s.arrive + '–' + s.depart;
  sh.parts.replaceChildren(partsBlock(s, sh.err));
  sh.more.replaceChildren(expensesBlock(s, sh.err), followBlock(s, sh.err));
}

// Optional grammar check (LanguageTool): the button only appears when the server has one configured.
let grammarOn = null;
function grammarBlock(notes) {
  const box = el('div', 'gram');
  const btn = el('button', 'quick', 'Check grammar'); btn.type = 'button'; btn.hidden = true;
  const out = el('div', 'gram-out');
  const run = async () => {
    out.replaceChildren(el('div', 'muted', 'Checking…'));
    let r;
    try { r = await api('POST', '/api/grammar', {text: notes.value}); }
    catch (e) { out.replaceChildren(el('div', 'err', e.message)); return; }
    if (!r.issues.length) { out.replaceChildren(el('div', 'gram-ok', 'No problems found.')); return; }
    out.replaceChildren(...r.issues.map(i => {
      const row = el('div', 'gram-row');
      row.append(el('div', 'gram-msg', '"' + notes.value.substr(i.offset, i.length) + '": ' + i.message));
      i.replacements.forEach(v => {
        const b = el('button', 'quick', v || '(remove)'); b.type = 'button';
        b.onclick = () => {
          notes.value = notes.value.slice(0, i.offset) + v + notes.value.slice(i.offset + i.length);
          sheet.vals.notes = notes.value; touched(); run();
        };
        row.append(b);
      });
      return row;
    }));
  };
  btn.onclick = run;
  box.append(btn, out);
  (grammarOn === null ? api('GET', '/api/grammar/status').then(r => (grammarOn = r.enabled), () => false) : Promise.resolve(grammarOn))
    .then(on => { btn.hidden = !on; });
  return box;
}

function openSheet(id) {
  const s = S.stops.find(x => x.id === id);
  if (!s) return;
  sheet.id = id; sheet.failed = false; sheet.again = false;
  sheet.vals = {notes: s.notes, work_category: s.work_category, ticket: s.ticket, visit_result: s.visit_result};
  sh = {everSaved: false};
  sheetEl.replaceChildren();

  const top = el('div', 'sheet-top');
  sh.title = el('div', 'sheet-title');
  const done = el('button', 'primary', 'Done'); done.type = 'button'; done.onclick = () => closeSheet();
  top.append(sh.title, done);
  sh.stat = el('div', 'sheet-stat');
  sh.err = el('div', 'err');

  const notes = el('textarea', 'sheet-notes'); notes.value = s.notes;
  notes.placeholder = 'What was wrong, what you did, the result';
  notes.spellcheck = true; notes.setAttribute('autocorrect', 'on'); notes.setAttribute('autocapitalize', 'sentences');
  notes.oninput = () => { sheet.vals.notes = notes.value; touched(); };
  notes.onblur = () => { if (sheetDirty()) saveSheet(); };

  const cat = el('select');
  [['', 'Choose a category…']].concat(CATS.map(c => [c, c])).forEach(([v, t]) => { const o = el('option', null, t); o.value = v; cat.append(o); });
  cat.value = s.work_category;
  cat.onchange = () => { sheet.vals.work_category = cat.value; saveSheet(); };
  const catBox = labeled('Category', cat);
  if (!s.work_category && s.category_hint) {
    const q = el('button', 'quick', 'Use ' + s.category_hint + ' (last time here)'); q.type = 'button';
    q.onclick = () => { cat.value = s.category_hint; sheet.vals.work_category = cat.value; q.remove(); saveSheet(); };
    catBox.append(q);
  }

  const ticket = el('input'); ticket.value = s.ticket; ticket.placeholder = 'e.g. #1042, or the project / milestone';
  ticket.oninput = () => { sheet.vals.ticket = ticket.value; touched(); };
  ticket.onblur = () => { if (sheetDirty()) saveSheet(); };
  const result = el('select');
  [['', '—']].concat(RESULTS).forEach(([v, t]) => { const o = el('option', null, t); o.value = v; result.append(o); });
  result.value = s.visit_result;
  result.onchange = () => { sheet.vals.visit_result = result.value; saveSheet(); };

  sh.parts = el('div');
  const det = el('details', 'sheet-more');
  det.append(el('summary', null, 'More details'));
  det.open = !!(s.ticket || s.visit_result || (s.follow_ups || []).length || (s.parts || []).some(p => p.expense));
  sh.more = el('div', 'sheet-grid');
  det.append(labeled('Job / ticket / project', ticket), labeled('Visit result', result), sh.more);

  const done2 = el('button', 'primary wide', 'Done'); done2.type = 'button'; done2.onclick = () => closeSheet();
  const body = el('div', 'sheet-body');
  const gram = grammarBlock(notes);
  body.append(sh.stat, sh.err, labeled('Work performed', notes), gram, catBox, sh.parts, det, done2);
  sheetEl.append(top, body);
  refreshSheet();
  setStat('clean');
  backEl.hidden = sheetEl.hidden = false;
  document.body.classList.add('sheet-open');
  sheetEl.scrollTop = 0;
  sheetEl.style.bottom = '0px';
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
  if (s.status === 'Confirmed') {
    const miss = [];
    if (!s.notes.trim()) miss.push('notes');
    if (!s.work_category) miss.push('category');
    if (miss.length) body.append(el('div', 'need', 'Needs ' + miss.join(' and ')));
  }
  if (s.here_now) {
    body.append(el('div', 'here', '● Here now · keeps growing while you are on site'));
    if (s.status === 'Draft' || s.status === 'Confirmed') {
      const lv = el('button', 'quick', 'Leaving now'); lv.type = 'button';
      lv.title = 'Set the departure to the current time';
      lv.onclick = async ev => {
        ev.stopPropagation();
        try { applyDay(await api('POST', '/api/stops/' + s.id + '/leave')); render(false); }
        catch (e) { alert(e.message); }
      };
      body.append(lv);
    }
  }
  if (s.vendor_hint) {
    const vb = el('button', 'quick', 'Save as Vendor'); vb.type = 'button';
    vb.title = 'A supply shop: later visits are pre-filled and the report says "from Vendor"';
    vb.onclick = async ev => {
      ev.stopPropagation();
      try { applyDay(await api('POST', '/api/stops/' + s.id + '/save-place', {kind: 'vendor'})); render(false); }
      catch (e) { alert(e.message); }
    };
    body.append(vb);
  }
  head.append(el('div', 'num', String(i + 1)), body, el('span', 'chip', s.status));
  if (s.suggest) {
    const q = el('button', 'quick', (s.business_name ? 'Confirm ' : 'Confirm as ') + s.suggest.name);
    q.onclick = async ev => {
      ev.stopPropagation();
      try { applyDay(await api('POST', '/api/stops/' + s.id + '/confirm-known')); render(false); }
      catch (e) { alert(e.message); }
    };
    body.append(q);
  }
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

async function markHere(s, which, err) {
  const n = S.stops.filter(x => x.id !== s.id && x.status !== 'Personal' &&
    (which === 'start' ? x.arrive_ts < s.arrive_ts : x.arrive_ts > s.arrive_ts)).length;
  if (n && !confirm('Make this the ' + (which === 'start' ? 'start' : 'end') + ' of the day? ' + n + ' stop' +
      (n > 1 ? 's' : '') + (which === 'start' ? ' before' : ' after') + ' it will be marked Personal (you can undo this on the Export & tools page).')) return;
  await run(() => api('POST', '/api/stops/' + s.id + '/mark', {which}), err,
            () => { S.flash = n ? n + ' stop' + (n > 1 ? 's' : '') + ' marked Personal.' : ''; });
}

function buildEdit(edit, s, i) {
  const key = 's' + s.id;
  const d = Object.assign({business_name: s.business_name, address: s.address,
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
      panel.remove(); panel = null;  // choosing a business closes the list
      candLayer.clearLayers();
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

  const flat = (s.notes || '').replace(/\s+/g, ' ').trim();
  const np = el('button', 'notesbtn'); np.type = 'button';
  const bits = [];
  const nParts = (s.parts || []).filter(p => !p.expense).length, nExp = (s.parts || []).filter(p => p.expense).length;
  if (nParts) bits.push(nParts + ' part' + (nParts > 1 ? 's' : ''));
  if (nExp) bits.push(nExp + ' expense' + (nExp > 1 ? 's' : ''));
  if ((s.follow_ups || []).length) bits.push(s.follow_ups.length + ' follow-up' + (s.follow_ups.length > 1 ? 's' : ''));
  if (s.work_category) bits.push(s.work_category);
  np.append(el('span', 'nb-t', 'Notes & parts ›'),
            el('span', 'nb-m' + (flat ? '' : ' nb-empty'), flat ? flat.slice(0, 110) + (flat.length > 110 ? '…' : '') : 'No work notes yet'),
            el('span', 'nb-m', bits.join(' · ') || 'No parts yet'));
  np.onclick = () => openSheet(s.id);

  const seg = el('div', 'seg');
  const segBtns = ['Draft', 'Confirmed', 'Personal', 'Lunch', 'Traffic'].map(st => {
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
    ['business_name', 'address', 'arrive', 'depart', 'status', 'category']
      .forEach(k => { if (d[k] !== s[k]) body[k] = d[k]; });
    if (d.known_site_id !== s.known_site_id) body.known_site_id = d.known_site_id;
    if (!Object.keys(body).length) { err.textContent = 'Nothing to save.'; return; }
    await run(() => api('PATCH', '/api/stops/' + s.id, body), err, () => { delete S.drafts[key]; });
  }

  const acts = el('div', 'acts');
  const saveB = el('button', 'primary', 'Save'); saveB.onclick = save;
  const confB = el('button', null, 'Confirm');
  confB.onclick = () => { remember('status', 'Confirmed'); segBtns.forEach(x => x.classList.toggle('on', x.textContent === 'Confirmed')); save(); };
  const moreB = el('button', null, 'More ▾');
  acts.append(saveB, confB, moreB);
  const more = el('div', 'more'); more.hidden = true;
  moreB.onclick = () => { more.hidden = !more.hidden; moreB.textContent = more.hidden ? 'More ▾' : 'More ▴'; };

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
  more.append(splitB);

  // make this stop the start and/or end of the work day
  const sb = el('button', null, 'Start day here'); sb.title = 'Work day starts when you arrived at this stop';
  sb.onclick = () => markHere(s, 'start', err);
  const eb = el('button', null, 'End day here');
  eb.title = s.status === 'Personal' ? 'Work day ends when you arrived here' : 'Work day ends when you left this stop';
  eb.onclick = () => markHere(s, 'end', err);
  more.append(sb, eb);
  if (s.lat != null) {
    [['vendor', 'Vendor'], ['office', 'Office'], ['home', 'Home']].forEach(([k, t]) => {
      if (s.site_kind === k) return;
      const pb = el('button', null, 'Save as ' + t); pb.type = 'button';
      pb.title = 'Remember this spot as a ' + t + ' place; later stops here are pre-filled';
      pb.onclick = () => run(() => api('POST', '/api/stops/' + s.id + '/save-place', {kind: k}), err);
      more.append(pb);
    });
  }

  const prev = S.stops[i - 1], next = S.stops[i + 1];
  const mergeWith = other => run(() => api('POST', '/api/stops/merge', {ids: [s.id, other.id]}), err);
  if (prev) { const b = el('button', null, 'Merge ↑'); b.title = 'Merge with the previous stop'; b.onclick = () => mergeWith(prev); more.append(b); }
  if (next) { const b = el('button', null, 'Merge ↓'); b.title = 'Merge with the next stop'; b.onclick = () => mergeWith(next); more.append(b); }
  const del = el('button', 'danger', s.kind === 'short' ? 'Dismiss' : 'Delete');
  del.onclick = () => {
    if (!confirm('Delete this stop? Raw GPS points are kept; only this entry is removed.')) return;
    run(() => api('DELETE', '/api/stops/' + s.id), err, () => { delete S.drafts[key]; S.open.delete(s.id); });
  };
  more.append(del);

  edit.append(seg, acts, more, splitRow, err);
  if (s.status === 'Draft' || s.status === 'Confirmed') edit.append(np);
  edit.append(labeled('Business', nameIn), labeled('Address', addrIn), find, times);
  if (s.lat != null) edit.append(el('div', 'hint', 'Tip: tap this stop\'s pin on the map, then drag it to move the location.'));
}

// ---- drive row ---------------------------------------------------------
function endRow(e) {
  const r = el('div', 'drive');
  let t = '■ End Day ' + e.time;
  if (e.kind === 'home') t += ' · at home (drive home counted)';
  else if (e.miles_to_home != null)
    t += ' · ' + e.miles_to_home + ' mi from the last job to home' + (e.method === 'estimate' ? ' (est.)' : '') + ' (in mileage, not drive time)';
  else t += ' · save a Home on the Places page to include the miles home';
  r.append(el('div', 'hint', t));
  return r;
}

function driveRow(dr) {
  const key = 'd' + dr.id;
  const row = el('div', 'drive');
  row.append(el('div', null, '↓ Drive ' + dr.depart + ' – ' + dr.arrive + ' · ' + dur(dr.minutes) +
    ' · ~' + dr.miles_approx + ' mi (approx.)' + (dr.estimated ? ' · estimated' : '') + (dr.counts === false ? ' · not counted' : '') + (dr.notes ? ' · has notes' : '')));
  if (dr.gap) {
    row.append(el('div', 'gapnote', '⚠ No location data ' + dr.gap.from + ' – ' + dr.gap.to + ' (' + dur(dr.gap.minutes) + ')' +
      (dr.estimated ? '. Drive time is an estimate from the distance. Tap here and enter the real times if you remember them.'
                    : '. Times were set by you.')));
  }
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
  const isToday = S.date === new Date().toLocaleDateString('en-CA');
  const mk0 = S.marks || {};
  if (isToday && !mk0.start) {
    const openForm = () => { S.tool = S.tool === 'day' ? null : 'day'; render(false); };
    const b = mk('Start day', () => (mk0.end ? openForm() : markNow('start')));
    b.className = 'primary';
    if (!mk0.end) { mk('End day', () => markNow('end')); mk('End at home', endAtHome); }
    else mk('Day —–' + mk0.end, () => { S.tool = S.tool === 'day' ? null : 'day'; render(false); });
  } else if (isToday && !mk0.end) {
    const b = mk('End day', () => markNow('end'));
    b.className = 'primary';
    mk('End at home', endAtHome);
  } else {
    mk(mk0.start || mk0.end ? 'Day ' + (mk0.start || '—') + '–' + (mk0.end || '—') : 'Day times…',
       () => { S.tool = S.tool === 'day' ? null : 'day'; render(false); });
  }
  mk('+ Add stop', () => { S.tool = S.tool === 'add' ? null : 'add'; render(false); });
  if (S.flash) bar.append(el('span', 'hint flash', S.flash));
  return bar;
}

async function endAtHome() {
  try {
    const d = await api('POST', '/api/day/' + S.date + '/end-at-home', {});
    applyDay(d);
    S.flash = 'Day ended at home.'; render(true);
  } catch (e) { alert(e.message); }
}

async function markNow(which) {
  try {
    const d = await api('POST', '/api/day/' + S.date + '/mark', {which});
    applyDay(d);
    if (which === 'end') {
      const n = S.totals.draft;
      if (!n) { location.href = '/tools?date=' + S.date; return; }
      S.flash = n + ' stop' + (n > 1 ? 's' : '') + ' still in Draft: review them, then use Export.';
    } else S.flash = 'Day started.';
    render(false);
  } catch (e) { alert(e.message); }
}

function dayForm() {
  const f = el('div', 'redo');
  const m = S.marks || {};
  const a = el('input'); a.type = 'time'; a.value = m.start || '';
  const b = el('input'); b.type = 'time'; b.value = m.end || '';
  const two = el('div', 'two'); two.append(labeled('Day started', a), labeled('Day ended', b));
  const err = el('div', 'err');
  const save = el('button', null, 'Save');
  save.onclick = async () => {
    try {
      let d;
      for (const [which, v, old] of [['start', a.value, m.start], ['end', b.value, m.end]]) {
        if (v && v !== old) d = await api('POST', '/api/day/' + S.date + '/mark', {which, time: v});
        else if (!v && old) d = await api('POST', '/api/day/' + S.date + '/mark', {which, clear: true});
      }
      if (d) applyDay(d);
      S.tool = null; render(false);
    } catch (e) { err.textContent = e.message; }
  };
  f.append(el('div', 'hint', 'When did your work day start and end? Used for the report and for no-data alerts. Optional.'),
           two, save, err);
  return f;
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

// ---- render ------------------------------------------------------------
function render(fit) {
  const keep = listEl.scrollTop;
  pathLayer.clearLayers(); candLayer.clearLayers(); markers = {};
  barEl.replaceChildren(toolbar());
  listEl.replaceChildren();
  if (S.tool === 'add') listEl.append(addForm());
  if (S.tool === 'day') listEl.append(dayForm());
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
    if (S.endDay && S.endDay.stop_id === s.id) listEl.append(endRow(S.endDay));
    const dr = S.drives.find(x => x.from_stop_id === s.id);
    if (dr) listEl.append(driveRow(dr));
  });
  if (fit && bounds.length) map.fitBounds(bounds, {padding: [30, 30]});
  listEl.scrollTop = keep;
  const t = S.totals || {};
  const drafts = t.draft ? ' · ' + t.draft + ' draft' + (t.draft_short ? ' (+' + t.draft_short + ' short)' : '') : '';
  footEl.textContent = 'On site ' + dur(Math.round((t.on_site_s || 0) / 60)) + ' · Driving ' +
    dur(Math.round((t.drive_s || 0) / 60)) + ' · ~' + (t.miles_approx || 0) + ' mi · ' + (t.confirmed || 0) +
    ' confirmed' + drafts + (t.personal ? ' · ' + t.personal + ' personal' : '') + (t.lunch ? ' · lunch' : '') + (t.traffic ? ' · ' + t.traffic + ' traffic' : '');
  footEl.style.color = t.draft ? 'var(--err)' : '';
  if (sheet.id) refreshSheet();
}

async function load() {
  S.date = dateEl.value;
  document.getElementById('toolslink').href = '/tools?date=' + S.date;
  history.replaceState(null, '', '/day/' + S.date);
  if (sheet.id) await closeSheet();
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
  dateEl.value = x.toLocaleDateString('en-CA'); loadParts();
load();
}
// ---- auto-sync: while today's page is open, pick up new points and stops every few minutes ---------------
(function () {
  let busy = false, last = '';
  const sig = () => JSON.stringify([S.stops.map(x => [x.id, x.arrive_ts, x.depart_ts, x.here_now, x.status]),
                                    S.drives.map(x => [x.id, x.depart_ts, x.arrive_ts]), S.totals]);
  const typing = () => { const a = document.activeElement; return a && listEl.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName); };
  async function tick() {
    const isToday = S.date === new Date().toLocaleDateString('en-CA');
    if (busy || document.hidden || !isToday || !S.autoMin || sheet.id || S.tool || typing()) return;
    busy = true;
    try {
      const d = await api('POST', '/api/day/' + S.date + '/auto-sync');
      const before = sig();
      applyDay(d);
      if (sig() !== before && !sheet.id && !typing()) render(false);
    } catch (e) { /* Recorder or network down: try again next time */ }
    busy = false;
  }
  let next = 0;
  setInterval(() => { if (Date.now() >= next && S.autoMin) { next = Date.now() + S.autoMin * 60000; tick(); } }, 15000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && S.autoMin) tick(); });
})();

document.getElementById('prev').onclick = () => shift(-1);
document.getElementById('next').onclick = () => shift(1);
dateEl.onchange = load;

// ---- resizable map: press the bar and drag --------------------------------
// Phone layout (map above list): drag up/down. Wide layout: drag left/right.
// The size is remembered per layout; double-tap the bar to reset.
(function () {
  const main = document.getElementById('main'), bar = document.getElementById('split');
  const narrow = () => window.matchMedia('(max-width:700px)').matches;
  const key = () => narrow() ? 'jobhub.mapPct' : 'jobhub.listPx';
  const store = {
    get() { try { return localStorage.getItem(key()); } catch (e) { return null; } },
    set(v) { try { localStorage.setItem(key(), v); } catch (e) { /* private mode */ } },
    del() { try { localStorage.removeItem(key()); } catch (e) { /* ignore */ } },
  };
  let raf = 0;
  const refresh = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => map.invalidateSize({pan: false})); };
  function apply(v) {
    if (narrow()) main.style.setProperty('--map-h', Math.min(Math.max(v, 12), 80) + '%');
    else main.style.setProperty('--list-w', Math.min(Math.max(v, 240), main.clientWidth - 240) + 'px');
    refresh();
  }
  function reset() { main.style.removeProperty('--map-h'); main.style.removeProperty('--list-w'); store.del(); refresh(); }
  function current() {
    const r = main.getBoundingClientRect(), m = document.getElementById('map').getBoundingClientRect();
    return narrow() ? m.height / r.height * 100 : document.getElementById('list').getBoundingClientRect().width;
  }
  const saved = store.get();
  if (saved) apply(parseFloat(saved));

  let dragging = false, moved = false, lastTap = 0;
  bar.addEventListener('pointerdown', e => { dragging = true; moved = false; bar.setPointerCapture(e.pointerId); e.preventDefault(); });
  bar.addEventListener('pointermove', e => {
    if (!dragging) return;
    moved = true;
    const r = main.getBoundingClientRect();
    if (narrow()) apply((e.clientY - r.top) / r.height * 100);   // map is above the bar
    else apply(e.clientX - r.left);
  });
  const stop = () => { if (dragging) { dragging = false; if (moved) store.set(String(current())); } };
  bar.addEventListener('pointerup', () => {
    const now = Date.now();
    if (!moved && now - lastTap < 350) reset();   // double-tap (iOS has no reliable dblclick)
    lastTap = moved ? 0 : now;
    stop();
  });
  bar.addEventListener('pointercancel', stop);
  bar.addEventListener('keydown', e => {   // keyboard: arrow keys nudge the split
    const step = e.key === 'ArrowUp' || e.key === 'ArrowLeft' ? -4 : e.key === 'ArrowDown' || e.key === 'ArrowRight' ? 4 : 0;
    if (!step) return;
    e.preventDefault();
    apply(current() + (narrow() ? step : step * 8)); store.set(String(current()));
  });
  window.addEventListener('resize', refresh);
  window.addEventListener('orientationchange', refresh);
})();

loadParts();
load();


// ---- live status: last known position and time spent there ---------------
(function () {
  const box = document.getElementById('live');
  let data = null, got = 0;
  const mins = s => {
    const m = Math.floor(s / 60);
    if (m < 1) return 'under a minute';
    if (m < 60) return m + ' min';
    const h = Math.floor(m / 60), r = m % 60;
    if (h < 48) return h + ' h ' + String(r).padStart(2, '0') + ' min';
    return Math.floor(h / 24) + ' days';
  };
  function draw() {
    if (!data) return;
    const skew = Math.floor((Date.now() - got) / 1000);   // seconds since we fetched
    const here = data.here_s + skew, age = data.last_update_s + skew;
    const place = data.place || data.address || (data.moving ? 'On the move' : 'Unnamed location');
    const t = new Date((data.now + skew - here) * 1000)
      .toLocaleTimeString([], {hour: 'numeric', minute: '2-digit', timeZone: data.tz});
    const where = el('span', 'where', place);
    const dur = el('span', 'dur', data.moving ? 'just arrived / moving' : 'here ' + mins(here) + ' (since ' + t + ')');
    const link = el('a', null, 'Map');
    link.href = 'https://maps.google.com/?q=' + data.lat + ',' + data.lon;
    link.target = '_blank'; link.rel = 'noopener';
    const sub = el('div', 'sub' + (age > 1800 ? ' stale' : ''),
      'Updated ' + (age < 60 ? 'just now' : mins(age) + ' ago') +
      (data.batt != null ? ' · battery ' + data.batt + '%' : '') + ' · ');
    sub.append(link);
    box.replaceChildren(el('span', null, '\u{1F4CD}'), where, dur, sub);
    box.hidden = false;
  }
  async function poll() {
    if (document.hidden) return;
    try {
      const r = await fetch('/api/live');
      if (r.status === 401) return;
      const d = await r.json();
      if (!r.ok) { data = null; box.replaceChildren(el('span', 'sub stale', d.error || 'Live status unavailable')); box.hidden = false; return; }
      data = d; got = Date.now(); draw();
    } catch (e) { /* offline: keep showing the last status */ }
  }
  poll();
  setInterval(poll, 60000);
  setInterval(draw, 15000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
})();
