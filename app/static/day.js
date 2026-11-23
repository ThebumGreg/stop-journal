'use strict';
// Editable day view. Server is the source of truth: every edit is a request that
// returns the refreshed day, then the list re-renders. Unsaved typing is kept in
// S.drafts (keyed 's<id>' / 'd<id>') so a re-render never loses it.
const S = {cal: [], calErr: "", parts: [], date: null, path: [], stops: [], drives: [], totals: {}, tool: null,
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
  S.cal = d.calendar || []; S.calErr = d.calendar_error || '';
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
    if (!SHEET_TYPES.includes(s.stop_type)) setOpen(s.id, true);   // sheet stops: tap the card to open it
    setSel(s.id, false);
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
  if (!d.candidates.length) box.append(el('div', 'hint', 'No matches. Change the name above and tap Find again, or show what is nearby.'));
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
  const fresh = el('div', 'newpart'); fresh.hidden = true;
  go.onclick = () => {
    const v = name.value.trim();
    if (!v) { err.textContent = 'Enter a part name.'; return; }
    const m = S.parts.find(p => p.name.toLowerCase() === v.toLowerCase() || (p.sku && p.sku.toLowerCase() === v.toLowerCase()));
    if (m) { run(() => api('POST', '/api/stops/' + s.id + '/parts', {qty: qty.value, part_id: m.id}), err, () => { loadParts(); }); return; }
    newPartForm(fresh, v, () => qty.value, s, err);       // not in the catalog: ask for unit and cost first
  };
  name.addEventListener('input', () => { fresh.hidden = true; });
  add.append(name, qty, go);
  box.append(add, fresh);
  return box;
}

// A part that is not in the catalog yet: ask what it is counted in and what it costs, so the report is never blank.
const UNITS = [['ea', 'Each'], ['pair', 'Pair'], ['pack', 'Pack'], ['box', 'Box'], ['bag', 'Bag'], ['case', 'Case'],
               ['roll', 'Roll'], ['ft', 'Foot'], ['gal', 'Gallon'], ['lb', 'Pound']];
function newPartForm(box, name, qty, s, err) {
  box.replaceChildren(el('div', 'hint', '“' + name + '” is not in your parts list yet. It is added there too.'));
  const sku = el('input'); sku.placeholder = 'Part # (optional)';
  const unit = el('select');
  UNITS.forEach(([k, t]) => { const o = el('option', null, t); o.value = k; unit.append(o); });
  const cost = el('input'); cost.setAttribute('inputmode', 'decimal'); cost.placeholder = '0.00';
  const kind = el('select');
  [['part', 'Part (billable)'], ['consumable', 'Consumable (not billed)']].forEach(([k, t]) => { const o = el('option', null, t); o.value = k; kind.append(o); });
  const g = el('div', 'two');
  g.append(labeled('Counted in', unit), labeled('Unit cost', cost), labeled('Type', kind), labeled('Part #', sku));
  const ok = el('button', 'primary', 'Add to this stop'); ok.type = 'button';
  const no = el('button', null, 'Cancel'); no.type = 'button';
  no.onclick = () => { box.hidden = true; };
  ok.onclick = () => {
    const c = cost.value.trim().replace(/^\$/, '');
    if (kind.value === 'part' && !c) { err.textContent = 'Enter the unit cost (what you charge per ' + unit.selectedOptions[0].textContent.toLowerCase() + '), or choose Consumable.'; cost.focus(); return; }
    err.textContent = '';
    const body = {name, qty: qty(), unit: unit.value, unit_cost: c || null, type: kind.value,
                  billable: kind.value === 'part', sku: sku.value.trim() || null};
    run(() => api('POST', '/api/stops/' + s.id + '/parts', body), err, () => { box.hidden = true; loadParts(); });
  };
  const acts = el('div', 'cardacts'); acts.append(ok, no);
  box.append(g, acts);
  box.hidden = false;
  cost.focus();
}

// ---- the stop sheet (slides up over the day) ------------------------------------
// One place for everything about a stop, top to bottom: (1) what it is and where/when, (2) the work, (3) Finish.
// Typed fields autosave a moment after you stop typing (and when you leave a field); a status line says whether
// what you typed is saved. Parts, expenses, photos and follow-ups save as they are added.
// Finish checks what the stop's type needs, saves the visit result and confirms the stop. It does not set the
// departure: that keeps following the GPS until you type it or tap Leaving now, so you can finish before driving off.
const CATS = ['Time & Material', 'Quoted project', 'White-label migration', 'Internal operations',
              'Administrative', 'Customer outreach', 'Training'];
const RESULTS = [['complete', 'Complete'], ['return', 'Return visit'], ['waiting', 'Waiting (parts or customer)']];
const FIELDS = ['business_name', 'address', 'category', 'known_site_id', 'arrive', 'depart',
                'notes', 'work_category', 'ticket', 'visit_result'];
const TYPES = [['job', 'Job'], ['vendor', 'Vendor'], ['office', 'Office'], ['gas', 'Gas'],
               ['lunch', 'Lunch'], ['personal', 'Personal'], ['traffic', 'Traffic']];
const TYPE_LABEL = Object.fromEntries(TYPES);
const CONFIRMABLE = ['job', 'vendor', 'office'];
const SHEET_TYPES = CONFIRMABLE.concat(['gas']);     // lunch, personal and traffic need no sheet
const sheet = {id: null, vals: {}, timer: null, saving: false, again: false, failed: false, due: ''};
const backEl = el('div', 'sheet-back'), sheetEl = el('div', 'sheet');
backEl.hidden = sheetEl.hidden = true;
document.body.append(backEl, sheetEl);
let sh = {};

const curStop = () => S.stops.find(x => x.id === sheet.id);
const sheetDirty = () => { const s = curStop(); return !!s && FIELDS.some(k => sheet.vals[k] !== s[k]); };
const stopNum = s => S.stops.indexOf(s) + 1;

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
    sheet.failed = false; sh.everSaved = true; sh.err.textContent = '';
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

// Closing always goes back to the list, on this stop.
async function closeSheet(force) {
  if (!force && sheet.id) {
    const ok = await saveSheet();
    if (!ok && !confirm('Your changes could not be saved yet. Close anyway?')) return;
  }
  clearTimeout(sheet.timer);
  const id = sheet.id;
  sheet.id = null; sheet.failed = false;
  sheetEl.hidden = backEl.hidden = true;
  document.body.classList.remove('sheet-open');
  sheetEl.replaceChildren();
  candLayer.clearLayers();
  render(false);
  const card = id && document.querySelector('.card[data-id="' + id + '"]');
  if (card) { setSel(id, false); card.scrollIntoView({block: 'nearest'}); }
}
backEl.onclick = () => closeSheet();
document.addEventListener('keydown', e => { if (e.key === 'Escape' && sheet.id) closeSheet(); });

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


// which field to jump to for a missing item
const NEED_FIELD = {'business name': 'name', 'vendor name': 'name', 'name': 'name', 'category': 'cat',
                    'work performed': 'notes', 'visit result': 'result', 'total paid': 'fuel', 'stop type': 'types'};

// Pick a type (sheet or card). Lunch, Personal and Traffic are done once picked; the others open the sheet.
async function setType(s, k, err) {
  if (k === s.stop_type) return;
  if (sheet.id === s.id && !(await saveSheet())) return;
  try {
    applyDay(await api('PATCH', '/api/stops/' + s.id, {stop_type: k}));
  } catch (e) { if (err) err.textContent = e.message; else alert(e.message); return; }
  if (sheet.id === s.id) { sheet.id = null; sheetEl.replaceChildren(); }   // rebuilt below for the new type
  render(false);
  if (SHEET_TYPES.includes(k)) openSheet(s.id);
  else { sheetEl.hidden = backEl.hidden = true; document.body.classList.remove('sheet-open'); }
}

// The type buttons, with a Tools button in the last slot (under Gas) that opens the stop tools right below.
const toolsOpen = new Set();      // stop ids whose tools are showing (kept across re-renders)
function typeRow(s, err, inSheet) {
  const wrap = el('div');
  const types = el('div', 'types');
  TYPES.forEach(([k, label]) => {
    const b = el('button', s.stop_type === k ? 'on' : (!s.stop_type && k === 'gas' && s.gas_hint ? 'hint' : ''), label);
    b.type = 'button';
    b.onclick = () => setType(s, k, err);
    types.append(b);
  });
  const box = el('div', 'toolbox');
  const tb = el('button', 'toolsbtn', 'Tools ▾'); tb.type = 'button';
  tb.title = 'Split, merge, delete, start or end the day here, save as a place';
  const show = on => {
    box.hidden = !on; tb.classList.toggle('on', on); tb.textContent = on ? 'Tools ▴' : 'Tools ▾';
    if (on) toolsOpen.add(s.id); else toolsOpen.delete(s.id);
    if (on && !inSheet) box.replaceChildren(toolsBlock(s, err, false));
  };
  tb.onclick = () => show(box.hidden);
  types.append(tb);
  wrap.append(types, box);
  wrap.tools = box;
  show(toolsOpen.has(s.id));
  return wrap;
}

// Split, merge, start/end the day here, save as a place, back to draft, delete.
function toolsBlock(s, err, inSheet) {
  const i = S.stops.indexOf(s);
  const more = el('div', 'more');
  const act = fn => run(fn, err, () => { if (inSheet) closeSheet(true); });
  if (s.status === 'Confirmed') {
    const back = el('button', null, 'Back to Draft'); back.type = 'button';
    back.onclick = () => run(() => api('PATCH', '/api/stops/' + s.id, {status: 'Draft'}), err);
    more.append(back);
  }
  const splitRow = el('div', 'two'); splitRow.hidden = true;
  const splitIn = el('input'); splitIn.type = 'time';
  const splitGo = el('button', null, 'Split here'); splitGo.type = 'button';
  splitGo.onclick = () => act(() => api('POST', '/api/stops/' + s.id + '/split', {at: splitIn.value}));
  splitRow.append(splitIn, splitGo);
  const splitB = el('button', null, 'Split…'); splitB.type = 'button';
  splitB.onclick = () => { splitIn.value = addMin(s.arrive, Math.round(s.minutes / 2)); splitRow.hidden = !splitRow.hidden; };
  more.append(splitB);
  const sb = el('button', null, 'Start day here'); sb.type = 'button'; sb.title = 'Work day starts when you arrived at this stop';
  sb.onclick = () => markHere(s, 'start', err);
  const eb = el('button', null, 'End day here'); eb.type = 'button';
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
  const mergeWith = other => act(() => api('POST', '/api/stops/merge', {ids: [s.id, other.id]}));
  if (prev) { const b = el('button', null, 'Merge ↑'); b.type = 'button'; b.title = 'Merge with the previous stop'; b.onclick = () => mergeWith(prev); more.append(b); }
  if (next) { const b = el('button', null, 'Merge ↓'); b.type = 'button'; b.title = 'Merge with the next stop'; b.onclick = () => mergeWith(next); more.append(b); }
  const del = el('button', 'danger', s.kind === 'short' ? 'Dismiss' : 'Delete'); del.type = 'button';
  del.onclick = () => {
    if (!confirm('Delete this stop? Raw GPS points are kept; only this entry is removed. You can Undo it.')) return;
    act(() => api('DELETE', '/api/stops/' + s.id));
  };
  more.append(del);
  const w = el('div'); w.append(more, splitRow);
  return w;
}

// Find searches for what is typed in the name field (or the address, if no name), near the stop.
// With both empty it lists what is nearby. No second search box: fix the name above and tap Find again.
function businessFinder(s, nameIn, addrIn, onPick) {
  const word = s.stop_type === 'gas' ? 'station' : 'business';
  const find = el('button', null, 'Find ' + word); find.type = 'button';
  let panel = null;
  const shut = () => { if (panel) panel.remove(); panel = null; candLayer.clearLayers(); find.textContent = 'Find ' + word; };
  find.onclick = () => {
    if (panel) { shut(); return; }
    panel = el('div', 'panel');
    const q = nameIn.value.trim() || addrIn.value.trim();
    const head = el('div', 'hint', q ? 'Matches for “' + q + '” near this stop:' : 'Places near this stop:');
    const box = el('div');
    panel.append(head, box);
    if (q) {
      const near = el('button', null, 'Show what is nearby instead'); near.type = 'button';
      near.onclick = () => { head.textContent = 'Places near this stop:'; near.remove(); loadCands(s, box, '', pick); };
      panel.append(near);
    }
    const pick = c => {     // choosing a business closes the list and saves it
      shut();
      nameIn.value = c.name; addrIn.value = c.address;
      onPick(c);
    };
    find.after(panel);
    find.textContent = 'Close the list';
    loadCands(s, box, q, pick);
  };
  return find;
}

// Saved places of the kind that fits the type (Job: clients, Vendor: vendors, Office: offices), nearest first.
const PLACE_KIND = {job: 'client', vendor: 'vendor', office: 'office'};
function savedPicker(s, onPick) {
  const kind = PLACE_KIND[s.stop_type];
  const box = el('div');
  if (!kind) return box;
  const sel = el('select');
  sel.hidden = true;
  box.append(sel);
  api('GET', '/api/places?kind=' + kind).then(d => {
    const near = p => (s.lat != null && p.lat != null) ? distM(s.lat, s.lon, p.lat, p.lon) : Infinity;
    const list = d.places.map(p => [p, near(p)]).sort((a, b) => a[1] - b[1]);
    if (!list.length) return;
    const first = el('option', null, 'Saved ' + (kind === 'client' ? 'clients' : kind + 's') + ' (' + list.length + ')…');
    first.value = ''; sel.append(first);
    list.forEach(([p, m]) => {
      const far = m === Infinity ? '' : m < 1000 ? ' · ' + Math.round(m) + ' m' : ' · ' + (m / 1609).toFixed(1) + ' mi';
      const o = el('option', null, p.name + far); o.value = p.id; sel.append(o);
    });
    sel.onchange = () => {
      const hit = list.find(([p]) => String(p.id) === sel.value);
      sel.value = '';
      if (hit) onPick(hit[0]);
    };
    sel.hidden = false;
  }, () => {});
  return box;
}

function distM(a, b, c, d) {
  const r = Math.PI / 180, x = (d - b) * r * Math.cos((a + c) / 2 * r), y = (c - a) * r;
  return Math.sqrt(x * x + y * y) * 6371000;
}

function openSheet(id) {
  const s = S.stops.find(x => x.id === id);
  if (!s) return;
  if (!SHEET_TYPES.includes(s.stop_type)) { setOpen(id, true); setSel(id, true); return; }
  sheet.id = id; sheet.failed = false; sheet.again = false; sheet.due = '';
  sheet.vals = {};
  FIELDS.forEach(k => { sheet.vals[k] = s[k]; });
  sh = {everSaved: false};
  sheetEl.replaceChildren();
  const type = s.stop_type;
  const v = sheet.vals;

  const top = el('div', 'sheet-top');
  sh.title = el('div', 'sheet-title');
  const close = el('button', null, 'Close'); close.type = 'button'; close.onclick = () => closeSheet();
  top.append(sh.title, close);
  sh.stat = el('div', 'sheet-stat');
  sh.err = el('div', 'err');
  const field = (k, inp) => {     // typed field: autosave after a pause, and on leaving it
    inp.addEventListener('input', () => { v[k] = inp.value; touched(); });
    inp.addEventListener('blur', () => { if (sheetDirty()) saveSheet(); });
    return inp;
  };

  // (1) what and where
  const sec1 = el('section', 'sec');
  sh.types = typeRow(s, sh.err, true);
  sh.tools = sh.types.tools;
  const nameIn = el('input'); nameIn.value = s.business_name;
  nameIn.placeholder = {vendor: 'Vendor (supply shop)', gas: 'Station (optional)', office: 'Office'}[type] || 'Business name';
  nameIn.addEventListener('input', () => { v.known_site_id = null; });
  field('business_name', nameIn);
  sh.name = nameIn;
  const addrIn = field('address', el('input')); addrIn.value = s.address; addrIn.placeholder = 'Address';
  const find = businessFinder(s, nameIn, addrIn, c => {
    Object.assign(v, {business_name: c.name, address: c.address, category: c.category,
                      known_site_id: c.source === 'known' ? c.known_site_id : null});
    saveSheet();
  });
  const saved = savedPicker(s, p => {
    nameIn.value = p.name; addrIn.value = p.address;
    Object.assign(v, {business_name: p.name, address: p.address, known_site_id: p.id});
    saveSheet();
  });
  const arrIn = el('input'); arrIn.type = 'time'; arrIn.value = s.arrive;
  const depIn = el('input'); depIn.type = 'time'; depIn.value = s.depart;
  arrIn.onchange = () => { v.arrive = arrIn.value; saveSheet(); };
  depIn.onchange = () => { v.depart = depIn.value; saveSheet(); };
  sh.arr = arrIn; sh.dep = depIn;
  const times = el('div', 'two'); times.append(labeled('Arrived', arrIn), labeled('Left', depIn));
  sh.here = el('div');
  sh.cal = el('div');
  sh.useCal = c => {
    nameIn.value = c.name; v.business_name = c.name; v.known_site_id = null;
    if (c.location) { addrIn.value = c.location; v.address = c.location; }
    if (c.ticket && !v.ticket) { v.ticket = c.ticket; if (sh.ticket) sh.ticket.value = c.ticket; }
    saveSheet();
  };
  sec1.append(el('h3', null, '1 · What and where'), sh.types, sh.cal,
              labeled(type === 'vendor' ? 'Vendor' : type === 'gas' ? 'Station' : 'Business', nameIn),
              labeled('Address', addrIn), saved, find, times, sh.here);

  // (2) the work
  const sec2 = el('section', 'sec');
  sh.parts = el('div'); sh.photos = el('div'); sh.exp = el('div'); sh.fu = el('div'); sh.fuel = el('div');
  if (type === 'gas') {
    sec2.append(el('h3', null, '2 · Fill-up'), sh.fuel, sh.photos);
  } else {
    const notes = field('notes', el('textarea', 'sheet-notes')); notes.value = s.notes;
    notes.placeholder = type === 'vendor' ? 'What you picked up or did here' : 'What was wrong, what you did, the result';
    notes.spellcheck = true; notes.setAttribute('autocorrect', 'on'); notes.setAttribute('autocapitalize', 'sentences');
    sh.notes = notes;
    const cat = el('select');
    [['', 'Choose a category…']].concat(CATS.map(c => [c, c])).forEach(([val, t]) => { const o = el('option', null, t); o.value = val; cat.append(o); });
    cat.value = s.work_category;
    cat.onchange = () => { v.work_category = cat.value; saveSheet(); };
    sh.cat = cat;
    sh.catHint = el('div');
    const catBox = labeled('Category', cat); catBox.append(sh.catHint);
    const ticket = field('ticket', el('input')); ticket.value = s.ticket; sh.ticket = ticket; ticket.placeholder = 'e.g. #1042, or the project / milestone';
    const det = el('details', 'sheet-more');
    det.append(el('summary', null, type === 'vendor' ? 'Ticket and follow-ups' : 'Ticket, expenses and follow-ups'));
    det.open = !!(s.ticket || (s.follow_ups || []).length || (type !== 'vendor' && (s.parts || []).some(p => p.expense)));
    const grid = el('div', 'sheet-grid');
    grid.append(labeled('Job / ticket / project', ticket));
    if (type !== 'vendor') grid.append(sh.exp);
    grid.append(sh.fu);
    det.append(grid);
    sec2.append(el('h3', null, '2 · Work'), labeled('Work performed', notes), grammarBlock(notes),
                catBox, sh.parts);
    if (type === 'vendor') sec2.append(sh.exp);          // purchases are the main thing at a vendor
    sec2.append(sh.photos, det);
  }

  // (3) finish
  const sec3 = el('section', 'sec');
  sec3.append(el('h3', null, '3 · Finish'));
  if (type === 'job') {
    const res = el('div', 'seg res');
    sh.resBtns = RESULTS.map(([k, t]) => {
      const b = el('button', null, t); b.type = 'button';
      b.onclick = () => { v.visit_result = k; paintResult(); saveSheet(); };
      res.append(b);
      return [k, b];
    });
    const due = el('input'); due.type = 'date';
    due.onchange = () => { sheet.due = due.value; };
    sh.dueBox = labeled('Due date for the follow-up (optional)', due);
    sh.res = res;
    sec3.append(labeled('Visit result', res), sh.dueBox);
  }
  sh.needs = el('div', 'needs');
  sh.finish = el('div', 'finish');
  sec3.append(sh.needs, sh.finish);

  const body = el('div', 'sheet-body');
  body.append(sh.stat, sh.err, sec1, sec2, sec3);
  sheetEl.append(top, body);
  refreshSheet();
  setStat('clean');
  backEl.hidden = sheetEl.hidden = false;
  document.body.classList.add('sheet-open');
  sheetEl.style.bottom = '0px';
  sheetEl.scrollTop = 0;
  // the stop already has a name: start at the work
  if (s.business_name) requestAnimationFrame(() => { sheetEl.scrollTop = Math.max(0, sec2.offsetTop - 56); });
}

function paintResult() {
  if (!sh.resBtns) return;
  sh.resBtns.forEach(([k, b]) => b.classList.toggle('on', sheet.vals.visit_result === k));
  sh.dueBox.hidden = !['return', 'waiting'].includes(sheet.vals.visit_result);
}

function jumpTo(need) {
  const f = NEED_FIELD[need];
  const target = {name: sh.name, notes: sh.notes, cat: sh.cat, result: sh.res, fuel: sh.fuel, types: sh.types}[f];
  if (!target) return;
  target.scrollIntoView({block: 'center', behavior: 'smooth'});
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) setTimeout(() => target.focus(), 350);
  target.classList.add('pulse'); setTimeout(() => target.classList.remove('pulse'), 1600);
}

async function finishStop() {
  const s = curStop();
  if (!s) return;
  if (!(await saveSheet())) return;
  const body = {};
  if (s.stop_type === 'job') { body.visit_result = sheet.vals.visit_result || ''; if (sheet.due) body.due = sheet.due; }
  try {
    applyDay(await api('POST', '/api/stops/' + s.id + '/finish', body));
  } catch (e) {
    sh.err.textContent = e.message;
    render(false);
    const first = ((curStop() || {}).needs || [])[0];
    if (first) jumpTo(first);
    return;
  }
  S.flash = 'Stop ' + stopNum(s) + ' finished.';
  await closeSheet(true);
}

// the parts of the sheet that come from the server are rebuilt after every change; typed fields are left alone
function refreshSheet() {
  const s = curStop();
  if (!s || !SHEET_TYPES.includes(s.stop_type)) { closeSheet(true); return; }
  sh.title.textContent = stopNum(s) + ' · ' + (s.business_name || TYPE_LABEL[s.stop_type]) + ' · ' + s.arrive + '–' + (s.here_now ? 'now' : s.depart);
  // times the server moved (Leaving now, the GPS) show up unless you are editing them
  [['arrive', sh.arr], ['depart', sh.dep]].forEach(([k, inp]) => {
    if (document.activeElement !== inp && sheet.vals[k] === inp.value && inp.value !== s[k]) { inp.value = s[k]; sheet.vals[k] = s[k]; }
  });
  sh.cal.replaceChildren();
  if (s.calendar && s.stop_type !== 'gas') {
    const c = s.calendar;
    const b = el('button', 'quick', '📅 Use calendar: ' + c.name + (c.location ? ' – ' + c.location : '')); b.type = 'button';
    b.title = 'Appointment ' + c.start + '–' + c.end;
    b.onclick = () => sh.useCal(c);
    sh.cal.append(b);
  }
  sh.here.replaceChildren();
  if (s.here_now) {
    sh.here.append(el('div', 'here', '● Here now. Leave “Left” alone: it follows the GPS until you drive away.'));
    const lv = el('button', 'quick', 'Leaving now'); lv.type = 'button';
    lv.onclick = () => run(() => api('POST', '/api/stops/' + s.id + '/leave'), sh.err);
    sh.here.append(lv);
  }
  if (s.stop_type === 'gas') {
    sh.fuel.replaceChildren(...fuelBox(s, sh.err));
  } else {
    sh.parts.replaceChildren(partsBlock(s, sh.err));
    sh.exp.replaceChildren(expensesBlock(s, sh.err));
    sh.fu.replaceChildren(followBlock(s, sh.err));
    sh.catHint.replaceChildren();
    if (!s.work_category && s.category_hint) {
      sh.catHint.append(el('div', 'hint', 'Last time here: ' + s.category_hint + '. Used when you finish, unless you pick one.'));
      const q = el('button', 'quick', 'Use ' + s.category_hint); q.type = 'button';
      q.onclick = () => { sh.cat.value = s.category_hint; sheet.vals.work_category = s.category_hint; saveSheet(); };
      sh.catHint.append(q);
    }
  }
  sh.photos.replaceChildren(el('label', null, s.stop_type === 'gas' || s.stop_type === 'vendor' ? 'Receipts and photos' : 'Photos and receipts'),
                            ...photoBox(s, sh.err));
  paintResult();
  // what is still needed, from the server (the same check Finish uses)
  const needs = (s.needs || []).filter(n => !(n === 'visit result' && sheet.vals.visit_result));
  const done = !needs.length && (s.stop_type === 'gas' || s.status === 'Confirmed');
  sh.needs.replaceChildren();
  if (needs.length) {
    sh.needs.append(el('span', 'need', 'Still needed: '));
    needs.forEach((n, k) => {
      const a = el('button', 'link', n); a.type = 'button'; a.onclick = () => jumpTo(n);
      sh.needs.append(a);
      if (k < needs.length - 1) sh.needs.append(', ');
    });
  } else if (!done) {
    sh.needs.append(el('span', 'ready', 'Ready to finish.'));
  }
  sh.finish.replaceChildren();
  if (done) {
    sh.finish.append(el('div', 'done', '✓ Finished. Changes you make here still save.'));
    const c = el('button', 'primary wide', 'Close'); c.type = 'button'; c.onclick = () => closeSheet();
    sh.finish.append(c);
  } else {
    const fin = el('button', 'primary wide' + (needs.length ? ' notready' : ''), 'Finish stop ✓'); fin.type = 'button';
    fin.onclick = finishStop;
    const later = el('button', 'wide', 'Save & close (finish later)'); later.type = 'button'; later.onclick = () => closeSheet();
    sh.finish.append(fin, later);
  }
  sh.tools.replaceChildren(toolsBlock(s, sh.err, true));
}

// ---- stop card ---------------------------------------------------------
function labeled(label, input) {
  const w = el('div'); w.append(el('label', null, label), input); return w;
}


function cardTitle(s) {
  if (s.business_name) return s.business_name;
  if (s.stop_type === 'gas') return 'Gas';
  if (['lunch', 'personal', 'traffic'].includes(s.stop_type)) return TYPE_LABEL[s.stop_type];
  return s.source === 'manual' ? 'Manual stop' : 'New stop';
}
function chipText(s) {
  const t = TYPE_LABEL[s.stop_type];
  if (!t) return 'No type';
  if (CONFIRMABLE.includes(s.stop_type)) return t + (s.status === 'Confirmed' ? ' ✓' : ' · Draft');
  return t;
}

// The card is a summary with one main button; the sheet does the rest. Lunch, Personal, Traffic and stops with
// no type yet open a small area on the card instead (type buttons, times, stop tools).
function stopCard(s, i) {
  const card = el('div', 'card st-' + s.status + (s.kind === 'short' ? ' short' : ''));
  card.dataset.id = s.id;
  if (S.sel === s.id) card.classList.add('sel');
  const sheetType = SHEET_TYPES.includes(s.stop_type);

  const head = el('div', 'head');
  const body = el('div', 'body');
  const t = el('div', 't', s.arrive + ' – ' + (s.here_now ? 'now' : s.depart) + ' · ' + dur(s.minutes));
  if (s.kind === 'short') t.append(' ', el('span', 'chip', 'short'));
  body.append(t, el('div', 'm', cardTitle(s)));
  const needs = (s.needs || []).filter(n => n !== 'stop type');
  if (needs.length && sheetType) body.append(el('div', 'need', 'Needs ' + needs.join(', ')));
  if (s.here_now) body.append(el('div', 'here', '● Here now'));

  const btns = el('div', 'cardacts');
  const stop = fn => ev => { ev.stopPropagation(); fn(); };
  if (!s.stop_type) {
    const go = el('button', 'primary', 'Start job'); go.type = 'button';
    go.onclick = stop(() => setType(s, 'job'));
    const other = el('button', null, 'Other type…'); other.type = 'button';
    other.onclick = stop(() => { setOpen(s.id, !S.open.has(s.id)); setSel(s.id, true); });
    btns.append(go, other);
  } else if (sheetType) {
    const finished = !needs.length && (s.stop_type === 'gas' || s.status === 'Confirmed');
    const go = el('button', finished ? null : 'primary',
                  finished ? 'Review ›' : s.stop_type === 'gas' ? 'Fill-up ›' : 'Continue ›');
    go.type = 'button';
    go.onclick = stop(() => { setSel(s.id, true); openSheet(s.id); });
    btns.append(go);
  }
  if (s.calendar) {
    const cb = el('button', 'quick', '📅 Use ' + s.calendar.name); cb.type = 'button';
    cb.title = 'From your calendar (' + s.calendar.start + '–' + s.calendar.end + '): fills in the name and address';
    cb.onclick = stop(() => {
      const c = s.calendar, body = {business_name: c.name};
      if (c.location) body.address = c.location;
      if (c.ticket && !s.ticket) body.ticket = c.ticket;
      if (!s.stop_type) body.stop_type = 'job';
      run(() => api('PATCH', '/api/stops/' + s.id, body), null, () => { if (!s.stop_type) setTimeout(() => openSheet(s.id), 0); });
    });
    btns.append(cb);
  }
  if (s.suggest && s.status === 'Draft') {
    const q = el('button', 'quick', (s.business_name ? 'Confirm ' : 'Confirm as ') + s.suggest.name); q.type = 'button';
    q.title = 'A saved place: confirm it in one tap (add the notes later)';
    q.onclick = stop(() => run(() => api('POST', '/api/stops/' + s.id + '/confirm-known'), null));
    btns.append(q);
  }
  if (s.vendor_hint && s.stop_type !== 'vendor' && s.status !== 'Confirmed') {
    const vb = el('button', 'quick', 'Save as Vendor'); vb.type = 'button';
    vb.title = 'A supply shop: later visits are pre-filled and the report says "from Vendor"';
    vb.onclick = stop(() => run(() => api('POST', '/api/stops/' + s.id + '/save-place', {kind: 'vendor'}), null));
    btns.append(vb);
  }
  if (btns.childNodes.length) body.append(btns);
  head.append(el('div', 'num', String(i + 1)), body, el('span', 'chip', chipText(s)));
  head.onclick = () => {
    if (sheetType) { setSel(s.id, true); openSheet(s.id); return; }
    const open = !S.open.has(s.id);
    if (!open) flushCard(s.id);
    setOpen(s.id, open);
    if (open) setSel(s.id, true);
  };

  const edit = el('div', 'edit');
  edit.hidden = sheetType || !S.open.has(s.id);
  if (!sheetType) buildEdit(edit, s);
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

// ---- autosave for the stop card ------------------------------------------
// Typed fields save shortly after you leave the card's fields (not on every blur, so moving from Business to
// Address does not redraw the card under your finger). Any button on the card sends unsaved typing with it.
const CARD_FIELDS = ['business_name', 'address', 'arrive', 'depart', 'category', 'known_site_id'];
const cardTimers = {};
S.savedAt = {};
function pendingFor(id) {
  const s = S.stops.find(x => x.id === id), d = S.drafts['s' + id];
  if (!s || !d) return {};
  const body = {};
  CARD_FIELDS.forEach(k => { if (k in d && d[k] !== s[k]) body[k] = d[k]; });
  return body;
}
async function saveCard(id, extra, errEl) {
  clearTimeout(cardTimers[id]);
  const body = Object.assign(pendingFor(id), extra || {});
  if (!Object.keys(body).length) return true;
  if (errEl) errEl.textContent = '';
  let ok = true;
  await run(() => api('PATCH', '/api/stops/' + id, body), errEl, () => {
    delete S.drafts['s' + id];
    S.savedAt[id] = new Date().toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
  }).then(() => { ok = !(errEl && errEl.textContent); });
  return ok;
}
function flushCard(id) { if (Object.keys(pendingFor(id)).length) saveCard(id); }
function laterSave(id, errEl) {
  clearTimeout(cardTimers[id]);
  cardTimers[id] = setTimeout(() => {
    const card = document.querySelector('.card[data-id="' + id + '"]');
    if (card && card.contains(document.activeElement) && /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) return;
    saveCard(id, null, errEl);
  }, 700);
}

function fuelBox(s, err) {
  const f = s.fuel || {};
  const box = el('div', 'fuel');
  const mk = (label, key, val, mode, ph) => {
    const i = el('input'); i.inputMode = mode; i.value = val == null ? '' : val; i.placeholder = ph;
    i.onchange = () => run(() => api('PUT', '/api/stops/' + s.id + '/fuel', {[key]: i.value.trim()}), err);
    return labeled(label, i);
  };
  const ppg = f.price_per_gallon ? '$' + f.price_per_gallon.toFixed(3) + ' / gal' : '';
  box.append(mk('Gallons', 'gallons', f.gallons, 'decimal', '12.345'), mk('Total $', 'total', f.total, 'decimal', '48.20'),
             mk('Odometer', 'odometer', f.odometer, 'numeric', 'miles on the dash'));
  const out = el('div', 'hint', (ppg ? ppg + '. ' : '') + 'Fill the tank each time and enter the odometer for MPG (Truck page, Fuel).');
  return [box, out];
}

// Photos and receipts: the phone shrinks the picture (and the re-encode drops its location data) before
// uploading; the server does the same again. iPhone offers Take Photo or Photo Library for this input.
function shrink(file, side = 1600) {
  return new Promise(resolve => {
    const url = URL.createObjectURL(file), img = new Image();
    img.onload = () => {
      const k = Math.min(1, side / Math.max(img.naturalWidth, img.naturalHeight));
      const c = document.createElement('canvas');
      c.width = Math.round(img.naturalWidth * k); c.height = Math.round(img.naturalHeight * k);
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(url);
      c.toBlob(b => resolve(b || file), 'image/jpeg', 0.82);
    };
    img.onerror = () => { URL.revokeObjectURL(url); resolve(file); };   // let the server try
    img.src = url;
  });
}
// Full-size view over the page with a Close button (opening the picture on its own would leave the
// home-screen app with no way back).
function viewPhoto(src) {
  const ov = el('div', 'viewer');
  const close = el('button', 'primary', 'Close'); close.type = 'button';
  const img = el('img'); img.src = src; img.alt = 'Picture';
  ov.append(close, img);
  const done = () => { ov.remove(); document.removeEventListener('keydown', esc); };
  const esc = e => { if (e.key === 'Escape') done(); };
  close.onclick = done; ov.onclick = e => { if (e.target === ov) done(); };
  document.addEventListener('keydown', esc);
  document.body.append(ov);
}
function photoBox(s, err) {
  const box = el('div', 'photos');
  (s.photos || []).forEach(p => {
    const w = el('div', 'ph');
    const a = el('a'); a.href = '/api/photos/' + p.id + '.jpg';
    a.onclick = ev => { ev.preventDefault(); viewPhoto(a.href); };
    const im = el('img'); im.src = a.href; im.alt = p.kind; im.loading = 'lazy'; a.append(im);
    const x = el('button', 'danger', '✕'); x.type = 'button'; x.title = 'Remove this picture';
    x.onclick = () => { if (confirm('Remove this ' + (p.kind === 'receipt' ? 'receipt' : 'photo') + '?'))
      run(() => api('DELETE', '/api/photos/' + p.id), err); };
    w.append(a, el('span', 'phk', p.kind === 'receipt' ? 'Receipt' : 'Photo'), x);
    box.append(w);
  });
  const add = kind => {
    const lab = el('label', 'quick addph'); const inp = el('input'); inp.type = 'file'; inp.accept = 'image/*'; inp.hidden = true;
    lab.append(kind === 'receipt' ? '+ Receipt' : '+ Photo', inp);
    inp.onchange = async () => {
      const f = inp.files[0]; if (!f) return;
      lab.firstChild.textContent = 'Uploading…';
      const fd = new FormData();
      fd.append('file', await shrink(f), 'picture.jpg'); fd.append('kind', kind);
      await run(async () => {
        const r = await fetch('/api/stops/' + s.id + '/photos', {method: 'POST', body: fd});
        let d = null; try { d = await r.json(); } catch (e) {}
        if (!r.ok) throw new Error((d && d.error) || 'Upload failed (' + r.status + ')');
        return d;
      }, err);
    };
    return lab;
  };
  const row = el('div', 'acts'); row.append(add('receipt'), add('photo'));
  return [box, receiptHints(s, err), row];
}

// "Receipt says …": what the receipt reader found, as a suggestion. Nothing changes until you tap Use.
const $ = n => '$' + Number(n).toFixed(2);
function receiptHints(s, err) {
  const box = el('div');
  (s.photos || []).filter(p => p.kind === 'receipt' && p.read && !p.read.dismissed).forEach(p => {
    const r = p.read;
    const ignore = el('button', null, 'Ignore'); ignore.type = 'button';
    ignore.onclick = () => run(() => api('POST', '/api/photos/' + p.id + '/dismiss-read'), err);
    const hint = el('div', 'readhint');
    if (!r.ok) {
      const again = el('button', null, 'Try again'); again.type = 'button';
      again.onclick = () => { again.textContent = 'Reading…'; run(() => api('POST', '/api/photos/' + p.id + '/read'), err); };
      hint.append(el('div', 'm', 'Could not read the receipt: ' + r.error), btnRow(again, ignore));
      box.append(hint); return;
    }
    const f = s.fuel || {};
    if (s.stop_type === 'gas') {
      if (f.total === r.total && (r.gallons == null || f.gallons === r.gallons)) return;      // already used
      const bits = [r.total != null ? $(r.total) : '', r.gallons != null ? r.gallons + ' gal' : '',
                    r.price_per_gallon != null ? '$' + r.price_per_gallon.toFixed(3) + '/gal' : ''].filter(Boolean);
      const use = el('button', 'primary', 'Use these'); use.type = 'button';
      use.onclick = () => {
        const body = {};
        if (r.total != null) body.total = r.total;
        if (r.gallons != null) body.gallons = r.gallons;
        run(() => api('PUT', '/api/stops/' + s.id + '/fuel', body), err);
      };
      hint.append(el('div', 'n', '🧾 Receipt says: ' + bits.join(' · ')),
                  el('div', 'm', r.checks ? '✓ Gallons × price matches the total.' : 'Check these against the receipt before using them.'),
                  btnRow(use, ignore));
    } else {
      if (r.total == null) return;
      if ((s.parts || []).some(x => x.expense && Math.abs((x.unit_cost || 0) - r.total) < 0.005)) return;  // already added
      const use = el('button', 'primary', 'Add ' + $(r.total) + ' as an expense'); use.type = 'button';
      use.onclick = () => run(() => api('POST', '/api/stops/' + s.id + '/parts',
                                        {name: 'Store purchase', type: 'expense', qty: 1, unit_cost: r.total}), err, () => { loadParts(); });
      hint.append(el('div', 'n', '🧾 Receipt total: ' + $(r.total)), btnRow(use, ignore));
    }
    box.append(hint);
  });
  return box;
}
function btnRow(...b) { const d = el('div', 'cardacts'); d.append(...b); return d; }

// The small area on the card for stops with no sheet: type buttons, times (and a name for Personal), stop tools.
function buildEdit(edit, s) {
  const key = 's' + s.id;
  const d = Object.assign({business_name: s.business_name, arrive: s.arrive, depart: s.depart}, S.drafts[key] || {});
  const err = el('div', 'err');
  const stat = el('div', 'saved', S.drafts[key] ? 'Unsaved changes' : (S.savedAt[s.id] ? 'Saved ' + S.savedAt[s.id] : ''));
  const remember = (k, val) => { d[k] = val; (S.drafts[key] = S.drafts[key] || {})[k] = val; stat.textContent = 'Unsaved changes'; };
  if (!s.stop_type) edit.append(el('div', 'ask', 'What is this stop?' + (s.gas_hint ? ' (looks like a gas station)' : '')));
  edit.append(typeRow(s, err));
  const arrIn = el('input'); arrIn.type = 'time'; arrIn.value = d.arrive;
  arrIn.oninput = () => remember('arrive', arrIn.value);
  arrIn.onchange = () => saveCard(s.id, null, err);
  const depIn = el('input'); depIn.type = 'time'; depIn.value = d.depart;
  depIn.oninput = () => remember('depart', depIn.value);
  depIn.onchange = () => saveCard(s.id, null, err);
  const times = el('div', 'two'); times.append(labeled('Arrived', arrIn), labeled('Left', depIn));
  if (s.stop_type === 'personal') {
    const nameIn = el('input'); nameIn.value = d.business_name; nameIn.placeholder = 'Name (optional, e.g. Home)';
    nameIn.oninput = () => { remember('business_name', nameIn.value); remember('known_site_id', null); };
    nameIn.onblur = () => laterSave(s.id, err);
    edit.append(labeled('Name', nameIn));
  }
  edit.append(times, err, stat);
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
  const ty = el('select');
  [['', 'Type: choose later']].concat(TYPES).forEach(([v, t]) => { const o = el('option', null, t); o.value = v; ty.append(o); });
  const err = el('div', 'err');
  const go = el('button', null, 'Add stop');
  go.onclick = () => run(() => api('POST', '/api/day/' + S.date + '/stops',
    {arrive: a.value, depart: d.value, business_name: n.value, stop_type: ty.value}), err, () => { S.tool = null; });
  const two = el('div', 'two'); two.append(labeled('Arrived', a), labeled('Departed', d));
  f.append(el('div', 'hint', 'Add a stop that has no GPS data (a phone job, or a fill-up the phone missed).'), two, ty, n, go, err);
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
  if (S.cal.length || S.calErr) listEl.append(calendarBox());
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

// On-site appointments from the calendar, with the stop each one matched (tap to open it).
function calendarBox() {
  const box = el('details', 'calbox');
  box.open = S.calOpen !== false;
  box.ontoggle = () => { S.calOpen = box.open; };
  const left = S.cal.filter(e => e.state === 'upcoming').length;
  box.append(el('summary', null, '📅 On-site appointments (' + S.cal.length + ')' + (left ? ' · ' + left + ' to go' : '')));
  if (S.calErr) box.append(el('div', 'err', 'Calendar: ' + S.calErr));
  S.cal.forEach(e => {
    const r = el('div', 'calrow ' + e.state);
    const st = S.stops.find(x => x.id === e.stop_id);
    const state = e.state === 'done' || e.state === 'here'
      ? (e.state === 'here' ? 'Here now · stop ' : 'Stop ') + (st ? stopNum(st) : '')
      : e.state === 'upcoming' ? (e.minutes_to_start > 0 ? 'in ' + dur(e.minutes_to_start) : 'now')
      : 'No visit detected';
    r.append(el('div', 'n', e.start + '–' + e.end + ' · ' + e.name), el('div', 'm', [e.location, state].filter(Boolean).join(' · ')));
    if (st) { r.classList.add('link'); r.onclick = () => { setSel(st.id, true); if (SHEET_TYPES.includes(st.stop_type)) openSheet(st.id); else setOpen(st.id, true); }; }
    box.append(r);
  });
  return box;
}

// /day/<date>?stop=<id> (alerts, Places history) opens that stop once the day is loaded
let wantStop = +new URLSearchParams(location.search).get('stop') || null;
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
  if (wantStop) {
    const id = wantStop; wantStop = null;
    if (S.stops.some(x => x.id === id)) openSheet(id);
  }
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
