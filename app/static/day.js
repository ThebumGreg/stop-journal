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
// OpenStreetMap refuses tiles without a Referer (its usage policy). The page-wide Referrer-Policy is same-origin, so
// the tile images alone send just our address (origin only: never the page path or query).
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom: 19, attribution: '&copy; OpenStreetMap contributors',
   referrerPolicy: 'strict-origin-when-cross-origin'}).addTo(map);
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

const jl = (level, msg) => { try { if (window.jhLog) window.jhLog(level, msg); } catch (e) { /* never break the page */ } };

// DELETE and PUT are sent as POST with X-Method-Override: some routers' intrusion prevention blocks a device for minutes
// after a DELETE (seen 2026-11-23). The server turns the header back into the real method.
const wireMethod = m => (m === 'DELETE' || m === 'PUT') ? 'POST' : m;
const overrideHeader = m => (m === 'DELETE' || m === 'PUT') ? {'X-Method-Override': m} : {};
async function api(method, url, body) {
  let r;
  try {
    r = await fetch(url, {method: wireMethod(method), headers: {...(body ? {'Content-Type': 'application/json'} : {}), ...overrideHeader(method)},
                          body: body ? JSON.stringify(body) : undefined});
  } catch (e) { jl('warning', method + ' ' + url.split('?')[0] + ' did not reach the server (' + e.message + ')'); throw e; }
  if (r.status === 401) { location.href = '/login'; throw new Error('Login required'); }
  let d = null;
  try { d = await r.json(); } catch (e) { /* non-JSON error page */ }
  if (!r.ok) {
    jl('warning', method + ' ' + url.split('?')[0] + ' answered ' + r.status + ': ' + ((d && d.error) || ''));
    throw new Error((d && d.error) || 'Request failed (' + r.status + ')');
  }
  return d;
}

function applyDay(d) {
  S.stops = d.stops; S.drives = d.drives; S.totals = d.totals; S.undo = d.undo || null;
  S.marks = d.marks || {}; S.endDay = d.end_day || null; S.autoMin = d.auto_sync_min;
  if (d.path) S.path = d.path;
  S.cal = d.calendar || []; S.calErr = d.calendar_error || '';
  if (d.followup_owners) S.owners = d.followup_owners;
}

// Run a mutation; on success re-render, on failure show the message.
async function run(fn, errEl, onOk) {
  try {
    const d = await fn();
    if (onOk) onOk(d);
    applyDay(d);
    render(false);
  } catch (e) {
    jl('error', 'action failed: ' + e.message + (e.stack ? ' @ ' + String(e.stack).split('\n')[1] : ''));
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

// ---- live search: matches show under the box as you type (the iPhone's own suggestion list is unreliable) ----
// Every word typed must appear in the name, part #, shelf or bin. Tap a match to pick it.
// After a pick on finger-up, the browser still sends a click. The list is gone by then, so the click would land on
// whatever is underneath (a link, the ✕ of a part) and could navigate away mid-request. Swallow that one click.
function swallowNextClick(r) {
  // the browser's click after a finger-up pick lands where the list was (now hidden): eat that one click only.
  // A click elsewhere (a quick tap on Add) is left alone.
  const inside = e => !r || (e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom);
  const stop = e => { if (!inside(e)) return; e.preventDefault(); e.stopPropagation(); off(); };
  const off = () => document.removeEventListener('click', stop, {capture: true});
  document.addEventListener('click', stop, {capture: true});
  setTimeout(off, 700);
}

function liveSearch(input, items, label, onPick, after) {
  const box = el('div', 'live-res'); box.hidden = true;
  input.setAttribute('autocomplete', 'off'); input.setAttribute('autocorrect', 'off');
  let touching = false;                                    // a finger is on the list: do not hide it under the tap
  box.addEventListener('pointerdown', () => { touching = true; });
  box.addEventListener('pointercancel', () => { touching = false; });
  const show = () => {
    const words = input.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    box.replaceChildren();
    if (!words.length) { box.hidden = true; return; }
    const q = words.join(' ');
    const all = rankParts(items(), words, q), hits = all.slice(0, 12);
    jl('debug', 'search "' + q + '": ' + hits.length + ' shown of ' + all.length + ' matches');
    hits.forEach(p => {
      const b = el('button', 'live-hit'); b.type = 'button';
      const [main, sub] = label(p);
      b.append(el('span', 'n', main), sub ? el('span', 'm', sub) : '');
      b.onmousedown = e => e.preventDefault();          // keep the keyboard up until the tap lands
      // iPhone: pick on finger-up (a click can arrive late or not at all with the keyboard open); a drag is a scroll
      let x0 = 0, y0 = 0, done = false;
      const pick = (e, how) => { if (done) return; done = true; if (e) e.preventDefault(); touching = false;
                                 if (how !== 'click') swallowNextClick(box.getBoundingClientRect());     // the click after finger-up must not land under the list
                                 box.hidden = true;
                                 jl('debug', 'search pick "' + p.name + '" (#' + p.id + ') by ' + how); onPick(p); };
      b.addEventListener('pointerdown', e => { x0 = e.clientX; y0 = e.clientY; jl('debug', 'search touch on "' + p.name + '"'); });
      b.addEventListener('pointerup', e => {
        if (Math.abs(e.clientX - x0) < 12 && Math.abs(e.clientY - y0) < 12) pick(e, 'finger-up');
        else jl('debug', 'search touch moved (scroll), not a pick');
      });
      b.onclick = () => pick(null, 'click');
      box.append(b);
    });
    if (all.length > hits.length) box.append(el('div', 'hint live-more', (all.length - hits.length) + ' more match' +
      (all.length - hits.length === 1 ? '' : 'es') + ': type another word (e.g. plenum, white, 10ft)'));
    box.hidden = !hits.length;
  };
  input.addEventListener('input', show);
  input.addEventListener('focus', show);
  input.addEventListener('blur', () => setTimeout(() => {
    if (!touching) { if (!box.hidden) jl('debug', 'search list hidden (search box lost focus)'); box.hidden = true; }
  }, 400));
  (after || input).after(box);
  return box;
}
// best matches first: name starts with what you typed, then every word in the name, then anywhere (part #, vendor #…)
function rankParts(list, words, q) {
  const out = [];
  list.forEach(p => {
    const name = (p.name || '').toLowerCase();
    const hay = [name, p.sku, p.vendor_item, p.qbo_item, p.shelf, p.bin ? 'bin ' + p.bin : ''].join(' ').toLowerCase();
    if (!words.every(w => hay.includes(w))) return;
    const rank = name.startsWith(q) ? 0 : words.every(w => name.includes(w)) ? 1 : 2;
    out.push([rank, name, p]);
  });
  return out.sort((a, b) => a[0] - b[0] || a[1].localeCompare(b[1])).map(x => x[2]);
}
const partLabel = p => [p.name, [p.sku, [p.shelf, p.bin ? 'bin ' + p.bin : ''].filter(Boolean).join(' · '),
                                 p.tracked ? p.on_hand + ' ' + p.unit + ' on truck' : '', p.kind === 'consumable' ? 'consumable' : '']
                                .filter(Boolean).join(' · ')];

// ---- parts catalog (for the live part search) ------------------------------
async function loadParts() {
  try {
    api('GET', '/api/kits').then(d => { S.kits = d.kits; }, () => {});
    S.parts = (await api('GET', '/api/parts')).parts;
  } catch (e) { /* the search is optional: typing a name and Add still works */ }
}

function partsBlock(s, err) {
  const box = el('div');
  box.append(el('label', null, 'Parts used'));
  (s.parts || []).filter(p => !p.expense).forEach(p => {
    const row = el('div', 'prow'); row.style.gridTemplateColumns = '1fr auto auto 36px';
    const nm = el('div', 'pn');
    nm.append(el('div', null, p.name + (p.sku ? ' · ' + p.sku : '') + (p.unit && p.unit !== 'ea' ? ' (' + p.unit + ')' : '') + (p.consumable ? ' · consumable' : '')),
              el('div', 'lt', p.line_cost != null ? '$' + p.line_cost.toFixed(2) + (p.qty !== 1 ? ' (' + '$' + Number(p.unit_cost).toFixed(2) + ' each)' : '') : 'no price'));
    // quantity: − and + for the usual whole-number change, or type any amount (feet of wire)
    const setQty = v => run(() => api('PATCH', '/api/stop-parts/' + p.id, {qty: v}), err);
    const qs = el('div', 'qtys');
    const minus = el('button', null, '−'); minus.type = 'button'; minus.title = 'One less';
    const q = el('input'); q.type = 'number'; q.step = 'any'; q.min = '0'; q.value = p.qty; q.title = 'Quantity'; q.inputMode = 'decimal';
    const plus = el('button', null, '+'); plus.type = 'button'; plus.title = 'One more';
    minus.onclick = () => { const v = Math.max(0, Math.round((Number(p.qty) - 1) * 1000) / 1000); if (v > 0) setQty(v); };
    plus.onclick = () => setQty(Math.round((Number(p.qty) + 1) * 1000) / 1000);
    q.onchange = () => setQty(q.value);
    qs.append(minus, q, plus);
    const bl = el('label', 'bill');
    const b = el('input'); b.type = 'checkbox'; b.checked = p.billable;
    b.onchange = () => run(() => api('PATCH', '/api/stop-parts/' + p.id, {billable: b.checked}), err);
    bl.append(b, 'Bill');
    bl.title = 'Billable to the customer';
    const x = el('button', 'danger', '✕'); x.type = 'button'; x.title = 'Remove (returns to truck)';
    x.onclick = () => run(() => api('DELETE', '/api/stop-parts/' + p.id), err);
    row.append(nm, qs, bl, x);
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
  const name = el('input'); name.placeholder = 'Add part: start typing a name or part #';
  const qty = el('input'); qty.type = 'number'; qty.step = 'any'; qty.min = '0'; qty.value = '1'; qty.title = 'Quantity';
  const go = el('button', null, 'Add'); go.type = 'button';
  const fresh = el('div', 'newpart'); fresh.hidden = true;
  go.onclick = () => {
    const v = name.value.trim();
    if (!v) { err.textContent = 'Enter a part name.'; return; }
    const m = S.parts.find(p => p.name.toLowerCase() === v.toLowerCase() || (p.sku && p.sku.toLowerCase() === v.toLowerCase()));
    if (m) { jl('debug', 'Add button: exact match "' + m.name + '"');
             run(() => api('POST', '/api/stops/' + s.id + '/parts', {qty: qty.value, part_id: m.id}), err, () => { loadParts(); }); return; }
    jl('debug', 'Add button: "' + v + '" is not an exact part name, opening the new-part form');
    newPartForm(fresh, v, () => qty.value, s, err);       // not in the catalog: ask for unit and cost first
  };
  name.addEventListener('input', () => { fresh.hidden = true; });
  add.append(name, qty, go);
  box.append(add, fresh);
  // tap a match to add it straight away with the quantity in the box
  liveSearch(name, () => S.parts.filter(p => p.active !== false && p.kind !== 'expense'), partLabel, p => {
    jl('debug', 'adding "' + p.name + '" x ' + qty.value + ' to stop ' + s.id);
    name.value = '';
    run(() => api('POST', '/api/stops/' + s.id + '/parts', {qty: qty.value, part_id: p.id}), err, () => { loadParts(); });
  }, add);
  if ((S.kits || []).length) {          // a kit puts all its parts on the stop in one tap
    const kr = el('div', 'prow add');
    const ks = el('select');
    const o0 = el('option', null, 'Add a kit…'); o0.value = ''; ks.append(o0);
    S.kits.forEach(k => { const o = el('option', null, k.name + ' (' + k.items.length + ' items)' + (k.short.length ? ' · short' : '')); o.value = k.id; ks.append(o); });
    const kb = el('button', null, 'Add kit'); kb.type = 'button';
    kb.onclick = () => {
      if (!ks.value) { err.textContent = 'Choose a kit.'; return; }
      run(() => api('POST', '/api/stops/' + s.id + '/kits/' + ks.value, {}), err, () => { loadParts(); });
    };
    kr.append(ks, kb);
    box.append(kr);
  }
  return box;
}

// A part that is not in the catalog yet: ask what it is counted in and what it costs, so the report is never blank.
const UNITS = [['ea', 'Each'], ['pair', 'Pair'], ['pack', 'Pack'], ['box', 'Box'], ['bag', 'Bag'], ['case', 'Case'],
               ['roll', 'Roll'], ['ft', 'Foot'], ['gal', 'Gallon'], ['lb', 'Pound'], ['stick', 'Stick']];
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
  const dup = el('div');
  box.append(dup, g, acts);
  box.hidden = false;
  cost.focus();
  api('GET', '/api/parts/similar?name=' + encodeURIComponent(name)).then(d => {
    const ps = (d.parts || []).filter(p => p.active && p.kind !== 'expense');
    if (!ps.length) return;
    dup.append(el('div', 'hint', 'Did you mean one you already have?'));
    ps.forEach(p => {
      const b = el('button', 'quick', 'Use ' + p.name + (p.sku ? ' (' + p.sku + ')' : '')); b.type = 'button';
      b.onclick = () => run(() => api('POST', '/api/stops/' + s.id + '/parts', {part_id: p.id, qty: qty()}), err, () => { box.hidden = true; loadParts(); });
      dup.append(b);
    });
  }, () => {});
}

// Vendor stops: "Bought here". What you buy goes INTO truck stock and the purchase log; it is not billed at this stop
// (put it on a job when you use it). Lines can be changed (qty, paid) or taken back out.
function boughtBlock(s, err) {
  const box = el('div');
  box.append(el('label', null, 'Bought here (adds to truck stock)'));
  const money = v => v == null || v === '' ? '' : Number(v).toFixed(2);
  (s.bought || []).forEach(b => {
    const row = el('div', 'prow brow'); row.style.gridTemplateColumns = '1fr auto 72px 36px';
    const nm = el('div', 'pn');
    nm.append(el('div', null, b.name + (b.sku ? ' · ' + b.sku : '') + (b.unit && b.unit !== 'ea' ? ' (' + b.unit + ')' : '')),
              el('div', 'lt', (b.paid != null ? '$' + b.paid.toFixed(2) + ' paid' + (b.each != null && b.qty !== 1 ? ' ($' + b.each.toFixed(2) + ' each)' : '')
                                              : 'what you paid: optional') + (b.note && /^\d/.test(b.note) ? ' · ' + b.note : '')));
    const setQty = v => run(() => api('PATCH', '/api/purchases/' + b.id, {qty: v}), err, () => { loadParts(); });
    const qs = el('div', 'qtys');
    const minus = el('button', null, '−'); minus.type = 'button'; minus.title = 'One less';
    const q = el('input'); q.value = b.qty; q.inputMode = 'decimal'; q.title = 'Quantity bought';    // text: no spinner, room for 1000
    const plus = el('button', null, '+'); plus.type = 'button'; plus.title = 'One more';
    minus.onclick = () => { const v = Math.round((Number(b.qty) - 1) * 1000) / 1000; if (v > 0) setQty(v); };
    plus.onclick = () => setQty(Math.round((Number(b.qty) + 1) * 1000) / 1000);
    q.onchange = () => setQty(q.value);
    qs.append(minus, q, plus);
    const paid = el('input'); paid.inputMode = 'decimal'; paid.placeholder = '$ paid'; paid.value = money(b.paid);
    paid.title = 'Total paid for this line';
    paid.onchange = () => run(() => api('PATCH', '/api/purchases/' + b.id, {paid: paid.value.trim().replace(/^\$/, '') || null}), err);
    const x = el('button', 'danger', '✕'); x.type = 'button'; x.title = 'Remove (takes it back out of truck stock)';
    x.onclick = () => { if (confirm('Remove ' + b.name + '? ' + b.qty + ' ' + (b.unit || 'ea') + ' come back out of truck stock.'))
                          run(() => api('DELETE', '/api/purchases/' + b.id), err, () => { loadParts(); }); };
    row.append(nm, qs, paid, x);
    box.append(row);
  });
  // parts put on this stop before "Bought here" existed took stock OFF the truck: offer to turn them into purchases
  const old = (s.parts || []).filter(p => !p.expense);
  if (old.length) {
    const o = el('div', 'readhint');
    o.append(el('div', 'n', 'Entered as used here (this took them OFF truck stock):'));
    old.forEach(p => {
      const r = el('div', 'prow add one');
      r.append(el('div', 'pn', p.qty + ' × ' + p.name));
      const mv = el('button', null, 'Move to Bought here'); mv.type = 'button';
      mv.onclick = () => run(() => api('POST', '/api/stop-parts/' + p.id + '/to-bought'), err, () => { loadParts(); });
      const x = el('button', 'danger', '✕'); x.type = 'button'; x.title = 'Remove (returns to truck)';
      x.onclick = () => run(() => api('DELETE', '/api/stop-parts/' + p.id), err, () => { loadParts(); });
      r.append(mv, x);
      o.append(r);
    });
    box.append(o);
  }
  let chosen = null;
  const add = el('div', 'prow add'); add.style.gridTemplateColumns = '1fr 70px 80px auto';
  const name = el('input'); name.placeholder = 'Bought: start typing a name or part #';
  const qty = el('input'); qty.type = 'number'; qty.step = 'any'; qty.min = '0'; qty.value = '1'; qty.title = 'How many'; qty.inputMode = 'decimal';
  const paid = el('input'); paid.inputMode = 'decimal'; paid.placeholder = '$ paid'; paid.title = 'Total paid (optional)';
  const go = el('button', null, 'Add'); go.type = 'button';
  const packs = el('label', 'bill'); packs.hidden = true;
  const pk = el('input'); pk.type = 'checkbox';
  const pkText = el('span');
  packs.append(pk, pkText);
  const fresh = el('div', 'newpart'); fresh.hidden = true;
  const setChosen = p => {
    chosen = p;
    packs.hidden = !(p && p.pack_size);
    if (p && p.pack_size) { pkText.textContent = 'Count is in ' + (p.pack_unit || 'pack') + 's of ' + p.pack_size + ' ' + (p.unit || 'ea');
                            pk.checked = p.pack_unit === 'stick'; }
  };
  const body = () => {
    const out = {paid: paid.value.trim().replace(/^\$/, '') || null};
    out[chosen && chosen.pack_size && pk.checked ? 'packs' : 'qty'] = qty.value;
    return out;
  };
  const done = () => { name.value = ''; qty.value = '1'; paid.value = ''; setChosen(null); fresh.hidden = true; loadParts(); };
  go.onclick = () => {
    const v = name.value.trim();
    if (!v) { err.textContent = 'Enter what you bought.'; return; }
    const m = chosen && chosen.name.toLowerCase() === v.toLowerCase() ? chosen
            : S.parts.find(p => p.kind !== 'expense' && (p.name.toLowerCase() === v.toLowerCase() || (p.sku && p.sku.toLowerCase() === v.toLowerCase())));
    if (m) { run(() => api('POST', '/api/stops/' + s.id + '/bought', {part_id: m.id, ...body()}), err, done); return; }
    // not in the catalog: what is it counted in, and what do you charge (optional)
    fresh.replaceChildren(el('div', 'hint', '“' + v + '” is not in your parts list yet. It is added there and to truck stock.'));
    const unit = el('select'); UNITS.forEach(([k, t]) => { const o = el('option', null, t); o.value = k; unit.append(o); });
    const kind = el('select');
    [['part', 'Part (billable when used)'], ['consumable', 'Consumable (not billed)']].forEach(([k, t]) => { const o = el('option', null, t); o.value = k; kind.append(o); });
    const sku = el('input'); sku.placeholder = 'Part # (optional)';
    const sell = el('input'); sell.inputMode = 'decimal'; sell.placeholder = 'later is fine';
    const g = el('div', 'two');
    g.append(labeled('Counted in', unit), labeled('Type', kind), labeled('Part #', sku), labeled('You charge per unit', sell));
    const ok = el('button', 'primary', 'Add to Bought here'); ok.type = 'button';
    const no = el('button', null, 'Cancel'); no.type = 'button'; no.onclick = () => { fresh.hidden = true; };
    ok.onclick = () => run(() => api('POST', '/api/stops/' + s.id + '/bought',
      {name: v, unit: unit.value, type: kind.value, sku: sku.value.trim() || null,
       unit_cost: sell.value.trim().replace(/^\$/, '') || null, qty: qty.value, paid: paid.value.trim().replace(/^\$/, '') || null}), err, done);
    fresh.append(g, btnRow(ok, no));
    fresh.hidden = false;
  };
  name.addEventListener('input', () => { fresh.hidden = true; if (chosen && name.value !== chosen.name) setChosen(null); });
  add.append(name, qty, paid, go);
  box.append(add, packs, fresh);
  liveSearch(name, () => S.parts.filter(p => p.active !== false && p.kind !== 'expense'), partLabel, p => {
    name.value = p.name; setChosen(p); qty.focus(); qty.select();
  }, add);
  box.append(el('div', 'hint', 'Not billed here: when you use them on a job, add them to that stop.'));
  return box;
}

// ---- the stop sheet (slides up over the day) ------------------------------------
// One place for everything about a stop, top to bottom: (1) what it is and where/when, (2) the work, (3) Finish.
// Typed fields autosave a moment after you stop typing (and when you leave a field); a status line says whether
// what you typed is saved. Parts, expenses, photos and follow-ups save as they are added.
// Finish checks what the stop's type needs, saves the visit result and confirms the stop. It does not set the
// departure: that keeps following the GPS until you type it or tap Leaving now, so you can finish before driving off.
const CATS = ['Time & Material', 'Quoted project', 'White-label migration', 'Internal operations',
              'Administrative', 'Customer outreach', 'Training'];
const RESULTS = [['complete', 'Complete'], ['return', 'Return visit'], ['waiting_parts', 'Waiting for parts'],
                 ['waiting_customer', 'Waiting for customer']];
const FIELDS = ['business_name', 'address', 'category', 'known_site_id', 'arrive', 'depart',
                'notes', 'work_category', 'ticket', 'qbo_estimate', 'ticket_action', 'visit_result'];
const TYPES = [['job', 'Job'], ['vendor', 'Vendor'], ['office', 'Office'], ['gas', 'Gas'],
               ['break', 'Break'], ['lunch', 'Lunch'], ['personal', 'Personal'], ['traffic', 'Traffic']];
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
             clean: 'Saved', error: msg || 'Could not save',
             offline: 'No signal: kept on this phone. It saves by itself when the signal is back.',
             restored: msg || 'Restored what you typed on this phone; saving it now.'}[kind];
  sh.stat.textContent = t; sh.stat.className = 'sheet-stat ' + kind;
}
function touched() {
  setStat('unsaved'); clearTimeout(sheet.timer); sheet.chain = 0;
  keepDraft();
  sheet.timer = setTimeout(saveSheet, 2000);
}

// ---- poor signal: what you type is kept on the phone until the server has it --------------------
// Every change in the sheet is written to the phone's storage at once (survives a dropped signal, the app being
// closed, a reload). Saves that fail for lack of signal are retried when the signal is back, every 30 s, and when
// the app comes back on screen. Only the sheet's typed fields are kept this way (they are safe to send twice).
const DRAFT_PREFIX = 'jobhub-draft-';
function keepDraft() {
  const s = curStop();
  if (!s) return;
  const vals = {};
  FIELDS.forEach(k => { if (sheet.vals[k] !== s[k]) vals[k] = sheet.vals[k]; });
  try {
    if (Object.keys(vals).length) localStorage.setItem(DRAFT_PREFIX + s.id, JSON.stringify({date: S.date, vals, ts: Date.now()}));
    else localStorage.removeItem(DRAFT_PREFIX + s.id);
  } catch (e) { /* storage full or private mode: the normal save still runs */ }
}
function readDraft(id) {
  try { return JSON.parse(localStorage.getItem(DRAFT_PREFIX + id) || 'null'); } catch (e) { return null; }
}
const isOffline = e => !navigator.onLine || (e && e.name === 'TypeError');   // fetch fails with a TypeError when there is no network
let flushing = false;
async function flushDrafts() {
  if (flushing || !navigator.onLine) return;
  flushing = true;
  try {
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const key = localStorage.key(i);
      if (!key || !key.startsWith(DRAFT_PREFIX)) continue;
      const id = +key.slice(DRAFT_PREFIX.length);
      if (id === sheet.id) continue;                       // the open sheet saves itself
      const d = readDraft(id);
      if (!d || !d.vals) { localStorage.removeItem(key); continue; }
      try {
        const day = await api('PATCH', '/api/stops/' + id, d.vals);
        localStorage.removeItem(key);
        if (day && day.date === S.date) { applyDay(day); S.flash = 'Saved what you typed without signal.'; render(false); }
      } catch (e) {
        if (isOffline(e)) break;                           // still no signal: try again later
        localStorage.removeItem(key);                      // the server refused it (stop deleted, bad time…): do not loop
        S.flash = 'A change kept on this phone could not be saved: ' + e.message;
        render(false);
      }
    }
  } finally { flushing = false; }
}
window.addEventListener('online', () => { flushDrafts(); if (sheet.id && sheet.offline) saveSheet(); });
setInterval(() => { flushDrafts(); if (sheet.id && sheet.offline && navigator.onLine) saveSheet(); }, 30000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) flushDrafts(); });

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
  keepDraft();
  try {
    applyDay(await api('PATCH', '/api/stops/' + s.id, body));
    sheet.failed = false; sheet.offline = false; sh.everSaved = true; sh.err.textContent = '';
    // The server may store a tidied value (spaces trimmed, "#1042" -> "1042", a ticket link -> its ID, 9:05 -> 09:05).
    // Take it over for every field that still holds what was sent, or the sheet would keep re-saving forever.
    const now = curStop();
    if (now) Object.keys(body).forEach(k => {
      if (sheet.vals[k] === body[k] && now[k] !== undefined && now[k] !== body[k]) {
        sheet.vals[k] = now[k];
        const inp = sh.inputs && sh.inputs[k];
        if (inp && document.activeElement !== inp && inp.value === body[k]) inp.value = now[k] == null ? '' : now[k];
      }
    });
    render(false);
  } catch (e) {
    sheet.failed = true;
    sheet.offline = isOffline(e);
    if (sheet.offline) setStat('offline'); else setStat('error', 'Could not save: ' + e.message);
  }
  sheet.saving = false;
  if (!sheet.failed) {
    // save again only for new typing, and never more than a few times in a row (a safety net against a save loop)
    sheet.chain = (sheet.chain || 0) + 1;
    if ((sheet.again || sheetDirty()) && sheet.chain < 4) { sheet.again = false; return saveSheet(); }
    sheet.chain = 0;
    keepDraft();                                           // all saved: clears the copy on the phone
    setStat('saved');
  }
  return !sheet.failed && !sheetDirty();
}

// Closing always goes back to the list, on this stop.
async function closeSheet(force) {
  if (!force && sheet.id) {
    const ok = await saveSheet();
    // without signal the typing is kept on the phone and sent later, so closing is safe
    if (!ok && !sheet.offline && !confirm('Your changes could not be saved yet. Close anyway?')) return;
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
  const name = el('input'); name.placeholder = 'Expense (e.g. Parking)';
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
  // an expense needs its own amount: a match only fills the name
  liveSearch(name, () => S.parts.filter(p => p.kind === 'expense' && p.active !== false), p => [p.name, ''],
             p => { name.value = p.name; amt.focus(); }, add);
  return box;
}

function followBlock(s, err) {
  const box = el('div');
  box.append(el('label', null, 'Follow-ups (what is still open)'));
  (s.follow_ups || []).forEach(f => {
    const row = el('div', 'fu');
    const patch = body => run(() => api('PATCH', '/api/follow-ups/' + f.id, body), err);
    const t = el('input'); t.value = f.text; t.onchange = () => patch({text: t.value});
    const o = el('select'); o.title = 'Owner';                    // names from Settings → Me → Follow-up owners
    const names = S.owners || ['Me'];
    const optFor = (v, t) => { const x = el('option', null, t); x.value = v; return x; };
    if (!f.owner) o.append(optFor('', 'Owner…'));
    names.forEach(n => o.append(optFor(n, n)));
    if (f.owner && !names.some(n => n.toLowerCase() === f.owner.toLowerCase())) o.append(optFor(f.owner, f.owner + ' (not in the list)'));
    o.value = names.find(n => n.toLowerCase() === (f.owner || '').toLowerCase()) || f.owner || '';
    o.onchange = () => patch({owner: o.value});
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
  box.append(el('div', 'hint', '“Me” is you; pick another owner if someone else has it (the list is in Settings → Me). Pick a due date.'));
  return box;
}

// Optional grammar check (LanguageTool): the button only appears when the server has one configured.
let grammarOn = null;
let phrases = null;
// Under Work performed: tap-to-add quick phrases (Settings → My notes) and, on an empty note, "Start from last visit here".
function notesHelpers(s, notes) {
  const box = el('div');
  const add = text => {
    const cur = notes.value.replace(/\s+$/, '');
    notes.value = cur ? cur + (/[.!?]$/.test(cur) ? ' ' : '. ') + text : text;
    sheet.vals.notes = notes.value; touched();
  };
  if (s.last_notes && !notes.value.trim()) {
    const ln = s.last_notes;
    const b = el('button', 'quick', '↺ Start from last visit (' + ln.date + ')'); b.type = 'button';
    b.title = ln.text.slice(0, 200);
    b.onclick = () => { if (!notes.value.trim()) { notes.value = ln.text; sheet.vals.notes = ln.text; touched(); } b.remove(); };
    box.append(b);
  }
  const chips = el('div', 'phrases');
  box.append(chips);
  (phrases ? Promise.resolve(phrases) : api('GET', '/api/note-phrases').then(d => (phrases = d.phrases), () => []))
    .then(list => list.forEach(t => {
      const c = el('button', null, '+ ' + t); c.type = 'button';
      c.onclick = () => add(t);
      chips.append(c);
    }));
  return box;
}
let aiOn = null, aiAsk = null;
const aiStatus = () => aiOn !== null ? Promise.resolve(aiOn)
  : (aiAsk = aiAsk || api('GET', '/api/ai/status').then(r => (aiOn = r.notes), () => (aiOn = false)));

// ✨ Polish notes (Settings → AI): sends only this text to Claude and shows the cleaned-up version next to yours.
// Nothing changes until you tap Use it.
function polishBlock(notes) {
  const box = el('div', 'polish');
  const btn = el('button', 'quick', '✨ Polish notes'); btn.type = 'button'; btn.hidden = true;
  btn.title = 'Fix spelling and grammar and tidy the wording (same facts). Sends only these notes to the AI.';
  const out = el('div');
  btn.onclick = async () => {
    const text = notes.value.trim();
    if (!text) { out.replaceChildren(el('div', 'hint', 'Write some notes first.')); return; }
    btn.disabled = true; btn.textContent = 'Polishing…'; out.replaceChildren();
    try {
      const d = await api('POST', '/api/notes/polish', {text});
      if (d.text === text) { out.replaceChildren(el('div', 'hint', 'Looks good already: nothing to change.')); return; }
      const card = el('div', 'suggest');
      card.append(el('div', 'n', 'Suggested'), el('div', 'sugtext', d.text));
      const use = el('button', 'primary', 'Use it'); use.type = 'button';
      use.onclick = () => { notes.value = d.text; sheet.vals.notes = d.text; touched(); out.replaceChildren(el('div', 'hint', 'Updated. Your words are replaced; Undo with the keyboard if needed.')); };
      const keep = el('button', null, 'Keep mine'); keep.type = 'button';
      keep.onclick = () => out.replaceChildren();
      const row = el('div', 'cardacts'); row.append(use, keep);
      card.append(row);
      out.replaceChildren(card);
    } catch (e) { out.replaceChildren(el('div', 'err', e.message)); }
    finally { btn.disabled = false; btn.textContent = '✨ Polish notes'; }
  };
  box.append(btn, out);
  aiStatus().then(on => { btn.hidden = !on; });
  return box;
}
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
  // one writing helper: with AI notes on, ✨ Polish notes does this job, so Check grammar steps aside
  Promise.all([grammarOn === null ? api('GET', '/api/grammar/status').then(r => (grammarOn = r.enabled), () => false) : grammarOn,
               aiStatus()])
    .then(([on, ai_]) => { btn.hidden = !on || ai_; });
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
      const o = el('option', null, p.name + (p.old_address ? ' (old address)' : '') + far); o.value = p.id; sel.append(o);
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
  sheet.offline = false;
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
  sh.inputs = {};
  const field = (k, inp) => {     // typed field: autosave after a pause, and on leaving it
    sh.inputs[k] = inp;
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
  sh.left = el('div', 'leftline');
  sh.cal = el('div');
  sh.useCal = c => {
    nameIn.value = c.name; v.business_name = c.name; v.known_site_id = null;
    if (c.location) { addrIn.value = c.location; v.address = c.location; }
    if (c.ticket && !v.ticket) { v.ticket = c.ticket; if (sh.ticket) sh.ticket.value = c.ticket; }
    if (c.estimate && !v.qbo_estimate) { v.qbo_estimate = c.estimate; if (sh.est) sh.est.value = c.estimate; }
    if (c.category && !v.work_category) { v.work_category = c.category; if (sh.cat) sh.cat.value = c.category; }
    saveSheet();
  };
  // once the stop has a name, "what and where" folds into one line (✎ opens it); the sheet starts at the work
  sh.what = el('div', 'what');
  sh.what.append(sh.types, labeled(type === 'vendor' ? 'Vendor' : type === 'gas' ? 'Station' : 'Business', nameIn),
                 labeled('Address', addrIn), saved, find, times, sh.left);
  sh.what.hidden = !!s.business_name;
  sh.sum = el('div', 'sumline');
  sh.sumText = el('div', 'st');
  const edit_ = el('button', 'quick', '✎ Edit'); edit_.type = 'button';
  edit_.onclick = () => { sh.what.hidden = !sh.what.hidden; edit_.textContent = sh.what.hidden ? '✎ Edit' : 'Done'; };
  sh.sumEdit = edit_;
  sh.sum.append(sh.sumText, edit_);
  sec1.append(el('h3', null, '1 · What and where'), sh.sum, sh.here, sh.cal, sh.what);

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
    const ticket = field('ticket', el('input')); ticket.value = s.ticket; sh.ticket = ticket;
    ticket.placeholder = 'Optional: paste the ticket link or ID'; ticket.autocapitalize = 'off';
    // new work or the same job? Lets the agent pick or create the HubSpot ticket when none is filled in
    const tact = el('div', 'seg tact');
    const tactHint = el('div', 'hint');
    const tChoices = [['continue', 'Continuing a job'], ['new', 'A new job'], ['unsure', 'Not sure']];
    const showTact = () => {
      const cur = v.ticket_action !== undefined ? v.ticket_action : s.ticket_action;
      tact.querySelectorAll('button').forEach(b => b.classList.toggle('on', b._v === cur));
      tactHint.textContent = ticket.value.trim() ? 'The ticket above is used.' :
        !cur && s.ticket_action_default === 'continue' ? 'The last visit here needed a return visit, so this is treated as continuing that job unless you pick otherwise.' :
        'No ticket? Fine: the office’s agent finds the open ticket or creates one.';
    };
    tChoices.forEach(([val, t]) => {
      const b = el('button', null, t); b.type = 'button'; b._v = val;
      b.onclick = () => { const cur = v.ticket_action !== undefined ? v.ticket_action : s.ticket_action;
                          v.ticket_action = cur === val ? '' : val; showTact(); saveSheet(); };
      tact.append(b);
    });
    ticket.addEventListener('input', showTact);
    showTact();
    const est = field('qbo_estimate', el('input')); est.value = s.qbo_estimate || ''; sh.est = est; est.placeholder = 'e.g. 2210';
    const det = el('details', 'sheet-more');
    det.append(el('summary', null, type === 'vendor' ? 'Ticket and follow-ups' : 'Ticket, estimate, expenses and follow-ups'));
    det.open = !!(s.ticket || s.qbo_estimate || (s.follow_ups || []).length || (type !== 'vendor' && (s.parts || []).some(p => p.expense)));
    const grid = el('div', 'sheet-grid');
    const tbox = labeled('HubSpot ticket', ticket); tbox.append(el('label', null, 'This visit is'), tact, tactHint);
    grid.append(tbox);
    if (type !== 'vendor') grid.append(labeled('QuickBooks estimate #', est));
    if (type !== 'vendor') grid.append(sh.exp);
    grid.append(sh.fu);
    det.append(grid);
    sec2.append(el('h3', null, '2 · Work'), labeled('Work performed', notes), notesHelpers(s, notes), grammarBlock(notes), polishBlock(notes),
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
  restoreDraft(s);
}

// Typing kept on the phone (no signal, app closed) comes back into the sheet and is saved.
function restoreDraft(s) {
  const d = readDraft(s.id);
  if (!d || !d.vals) return;
  const changed = Object.keys(d.vals).filter(k => FIELDS.includes(k) && d.vals[k] !== s[k]);
  if (!changed.length) { localStorage.removeItem(DRAFT_PREFIX + s.id); return; }
  changed.forEach(k => {
    sheet.vals[k] = d.vals[k];
    const inp = (sh.inputs || {})[k] || {arrive: sh.arr, depart: sh.dep, work_category: sh.cat}[k];
    if (inp) inp.value = d.vals[k] == null ? '' : d.vals[k];
  });
  refreshSheet();
  setStat('restored');
  clearTimeout(sheet.timer);
  sheet.timer = setTimeout(saveSheet, 500);
}

function paintResult() {
  if (!sh.resBtns) return;
  sh.resBtns.forEach(([k, b]) => b.classList.toggle('on', sheet.vals.visit_result === k));
  sh.dueBox.hidden = !['return', 'waiting_parts', 'waiting_customer'].includes(sheet.vals.visit_result);
}

function jumpTo(need) {
  const f = NEED_FIELD[need];
  const target = {name: sh.name, notes: sh.notes, cat: sh.cat, result: sh.res, fuel: sh.fuel, types: sh.types}[f];
  if (!target) return;
  if (sh.what && sh.what.contains(target) && sh.what.hidden) { sh.what.hidden = false; sh.sumEdit.textContent = 'Done'; }
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
  // the next stop that still needs something: later ones first, then earlier ones
  const i = S.stops.findIndex(x => x.id === s.id);
  const open_ = x => x.id !== s.id && x.kind === 'stop' && (!x.stop_type || (SHEET_TYPES.includes(x.stop_type) &&
                     ((x.needs || []).length || (x.stop_type !== 'gas' && x.status !== 'Confirmed'))));
  S.flashNext = (S.stops.slice(i + 1).find(open_) || S.stops.slice(0, i).find(open_) || {}).id || null;
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
    const plan = el('div', 'calplan');
    plan.append(el('div', 'n', '📅 Appointment ' + c.start + '–' + c.end + ' · ' + c.name));
    if (c.work) plan.append(el('div', null, 'Planned work: ' + c.work));
    if (c.contact) {
      const line = el('div', null, 'Contact: ' + c.contact + ' ');
      const ph = (c.contact.match(/\+?[\d(][\d\s().-]{6,}\d/) || [])[0];
      if (ph) { const a = el('a', null, '📞 Call'); a.href = 'tel:' + ph.replace(/[^\d+]/g, ''); line.append(a); }
      plan.append(line);
    }
    if (c.offer) {
      const b = el('button', 'quick', '📅 Use calendar: ' + c.name + (c.location ? ' – ' + c.location : '')); b.type = 'button';
      b.onclick = () => sh.useCal(c);
      plan.append(b);
    }
    sh.cal.append(plan);
  }
  sh.sumText.textContent = [TYPE_LABEL[s.stop_type], s.business_name || 'No name yet', s.address,
                            s.arrive + '–' + (s.here_now ? 'now' : s.depart)].filter(Boolean).join(' · ');
  sh.sum.hidden = !s.business_name;
  if (!s.business_name) sh.what.hidden = false;
  // "Left": following the GPS, or a time you set (with a way back)
  sh.left.replaceChildren();
  if (!s.here_now) {
    if (s.depart_pinned) {
      sh.left.append(el('span', 'hint', 'Left ' + s.depart + ' was set by you. '));
      if (s.can_follow_gps) {
        const fg = el('button', 'quick', 'Follow GPS again'); fg.type = 'button';
        fg.title = 'Use the time the GPS shows you drove away';
        fg.onclick = () => run(() => api('POST', '/api/stops/' + s.id + '/follow-gps'), sh.err);
        sh.left.append(fg);
      }
    } else if (s.source === 'detected') sh.left.append(el('span', 'hint', 'Left follows the GPS (type a time to set it yourself).'));
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
    sh.parts.replaceChildren(s.stop_type === 'vendor' ? boughtBlock(s, sh.err) : partsBlock(s, sh.err));
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
    if (s.stop_type === 'job' && s.work_category === 'Time & Material') sh.finish.append(invoiceBlock(s, sh.err));
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

// ---- Send for invoice (finished Time & Material stop) ------------------------------------
// A preview of exactly what the agent gets (labor minutes through now, billable parts and expenses), then one tap
// sends it. Never automatic. The agent prices labor and handles the invoice and payment.
function invoiceBlock(s, err) {
  const box = el('div', 'invoice');
  const sent = s.invoice_request;
  box.append(el('h4', null, 'Invoice'));
  if (sent) box.append(el('div', 'done', '✓ Sent for invoice ' + sent.sent_time + ' (' + sent.time_on_site_minutes +
                                          ' min, through ' + sent.billed_through + ')' + (sent.version > 1 ? ', update ' + sent.version : '') + '.'));
  const go = el('button', sent ? 'wide' : 'primary wide', sent ? 'Send an updated invoice request…' : 'Send for invoice…');
  go.type = 'button';
  const pv = el('div', 'invprev'); pv.hidden = true;
  go.onclick = async () => {
    go.disabled = true; err.textContent = '';
    try { showInvoicePreview(pv, s, await api('GET', '/api/stops/' + s.id + '/invoice-request'), err); pv.hidden = false; go.hidden = true; }
    catch (e) { err.textContent = e.message; }
    go.disabled = false;
  };
  box.append(go, pv);
  return box;
}
function showInvoicePreview(pv, s, d, err) {
  const r = d.request, lab = r.labor;
  const money = v => v == null ? 'no price' : '$' + Number(v).toFixed(2);
  const hm = m => m >= 60 ? Math.floor(m / 60) + ' h ' + (m % 60) + ' min' : m + ' min';
  pv.replaceChildren(el('div', 'n', r.customer.business_name + (r.hubspot_ticket ? ' · ticket ' + r.hubspot_ticket : '') +
                                    (r.qbo_estimate ? ' · estimate ' + r.qbo_estimate : '')));
  pv.append(el('div', null, 'Labor: ' + lab.arrival + ' – ' + lab.billed_through + ' = ' + hm(lab.time_on_site_minutes) +
                            (lab.still_on_site ? ' (through now)' : '') + (lab.lunch_non_billable.length ? ', lunch left out' : '')));
  pv.append(el('div', 'hint', lab.still_on_site ? 'You are still here, so time runs to the moment you send.'
                                                 : 'Ends at your departure. Wrong? Fix “Left” (✎ Edit above) first.'));
  const list = (title, items, line) => {
    if (!items.length) return;
    pv.append(el('div', 'm', title));
    items.forEach(x => pv.append(el('div', 'li', line(x))));
  };
  list('Parts (billed)', r.parts, p => p.quantity + ' ' + p.unit + ' × ' + p.name + (p.part_number ? ' (' + p.part_number + ')' : '') +
                                       ' · ' + money(p.unit_cost) + (p.unit_cost != null ? ' each = ' + money(p.line_cost) : ''));
  if (!r.parts.length) pv.append(el('div', 'm', 'No billable parts.'));
  list('Not billed', r.parts_not_billed, p => p.quantity + ' × ' + p.name);
  list('Expenses (billed)', r.expenses, x => x.name + (x.notes ? ' (' + x.notes + ')' : '') + ' · ' + money(x.line_cost));
  (d.warnings || []).forEach(w => pv.append(el('div', 'warn', '⚠ ' + w)));
  const ticks = el('div', 'row');
  const tick = (label, on) => { const l = el('label', 'chk'); const c = el('input'); c.type = 'checkbox'; c.checked = on; l.append(c, ' ' + label); ticks.append(l); return c; };
  const pdfT = tick('PDF', true), picT = tick('Photos (' + r.photos.length + ')', r.photos.length > 0);
  if (!r.photos.length) picT.disabled = true;
  pv.append(el('div', 'm', 'The JSON is always attached.'), ticks);
  // the billing email the customer gives on site (optional): remembered for this place and filled in next time
  const bill = el('input'); bill.type = 'email'; bill.value = d.billing_email || ''; bill.placeholder = 'optional';
  bill.autocapitalize = 'off'; bill.autocomplete = 'off'; bill.spellcheck = false;
  pv.append(labeled('Customer billing email', bill),
            el('div', 'hint', 'Only if the customer gives one now. It is saved for this place.'));
  if (!d.ready) pv.append(el('div', 'warn', '⚠ Email is not set up yet (Settings → Email).'));
  const send = el('button', 'primary wide', 'Send to ' + (d.to || 'your agent')); send.type = 'button';
  const cancel = el('button', 'wide', 'Cancel'); cancel.type = 'button';
  cancel.onclick = () => { pv.hidden = true; pv.previousSibling.hidden = false; };
  send.onclick = () => {
    send.disabled = true; send.textContent = 'Sending…';
    run(() => api('POST', '/api/stops/' + s.id + '/invoice-request',
                  {pdf: pdfT.checked, photos: picT.checked, invoice_to_email: bill.value.trim()}), err,
        () => { jl('info', 'invoice request sent'); }).finally(() => { send.disabled = false; });
  };
  pv.append(send, cancel, el('div', 'hint', 'Labor is sent as time on site; your agent applies the rates, creates the invoice and handles payment.'));
}

// ---- stop card ---------------------------------------------------------
function labeled(label, input) {
  const w = el('div'); w.append(el('label', null, label), input); return w;
}


function cardTitle(s) {
  if (s.business_name) return s.business_name;
  if (s.stop_type === 'gas') return 'Gas';
  if (['break', 'lunch', 'personal', 'traffic'].includes(s.stop_type)) return s.stop_type === 'break' ? 'Short break' : TYPE_LABEL[s.stop_type];
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
  if (!s.stop_type && s.gas_hint) {     // a short stop at a gas station: a fill-up, or a quick personal stop?
    body.append(el('div', 'ask', '⛽ At a gas station. What was it?'));
    const g = el('button', 'primary', '⛽ Fill-up'); g.type = 'button';
    g.onclick = stop(() => setType(s, 'gas'));
    const p = el('button', null, '☕ Short break'); p.type = 'button';
    p.title = 'Restroom, snack: paid, counted as drive time';
    p.onclick = stop(() => setType(s, 'break'));
    const j = el('button', null, 'Job here'); j.type = 'button';
    j.onclick = stop(() => setType(s, 'job'));
    btns.append(g, p, j);
  } else if (!s.stop_type) {
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
  // a saved place already has its name: the card does not offer another (the stop sheet still does)
  if (s.calendar && s.calendar.offer && !s.known_site_id) {
    const cb = el('button', 'quick', '📅 Use ' + s.calendar.name); cb.type = 'button';
    cb.title = 'From your calendar (' + s.calendar.start + '–' + s.calendar.end + '): fills in the name and address';
    cb.onclick = stop(() => {
      const c = s.calendar, body = {business_name: c.name};
      if (c.location) body.address = c.location;
      if (c.ticket && !s.ticket) body.ticket = c.ticket;
      if (c.estimate && !s.qbo_estimate) body.qbo_estimate = c.estimate;
      if (c.category && !s.work_category) body.work_category = c.category;
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
      (n > 1 ? 's' : '') + (which === 'start' ? ' before' : ' after') + ' it will be marked Personal (you can undo this on the Reports page).')) return;
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
// When the picture was taken (its EXIF DateTimeOriginal, "2026:11:18 10:12:05"), read before shrink() re-encodes it and
// the metadata is lost. Only this one value is read and sent; '' when the file has none (screenshots, some apps).
async function exifTime(file) {
  try {
    const v = new DataView(await file.slice(0, 262144).arrayBuffer());
    if (v.getUint16(0) !== 0xFFD8) return '';
    for (let o = 2; o + 10 < v.byteLength;) {
      const marker = v.getUint16(o), len = v.getUint16(o + 2);
      if ((marker & 0xFF00) !== 0xFF00) return '';
      if (marker === 0xFFE1 && v.getUint32(o + 4) === 0x45786966) {          // APP1 "Exif"
        const t = o + 10, le = v.getUint16(t) === 0x4949;
        const u16 = p => v.getUint16(t + p, le), u32 = p => v.getUint32(t + p, le);
        const find = (dir, tag) => { const n = u16(dir); for (let i = 0; i < n; i++) { const e = dir + 2 + i * 12; if (u16(e) === tag) return e; } return -1; };
        const ex = find(u32(4), 0x8769);                                     // the Exif sub-directory
        const e = ex < 0 ? -1 : find(u32(ex + 8), 0x9003);                   // DateTimeOriginal
        if (e < 0) return '';
        const at = t + u32(e + 8);
        let s = ''; for (let i = 0; i < 19; i++) s += String.fromCharCode(v.getUint8(at + i));
        return /^\d{4}:\d\d:\d\d \d\d:\d\d:\d\d$/.test(s) ? s : '';
      }
      o += 2 + len;
    }
  } catch (e) {}
  return '';
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
      fd.append('taken', await exifTime(f));
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
        run(async () => {
          const d = await api('PUT', '/api/stops/' + s.id + '/fuel', body);
          return r.store && !s.business_name ? api('PATCH', '/api/stops/' + s.id, {business_name: r.store}) : d;   // the station's name too
        }, err);
      };
      hint.append(el('div', 'n', '🧾 Receipt says: ' + bits.join(' · ') + (r.store ? ' (' + r.store + ')' : '')),
                  el('div', 'm', r.checks ? '✓ Gallons × price matches the total.' : 'Check these against the receipt before using them.'),
                  btnRow(use, ignore));
    } else {
      if (r.total == null && !(r.items || []).length) return;
      const where = [r.store, r.date].filter(Boolean).join(' · ');
      hint.append(el('div', 'n', '🧾 Receipt' + (where ? ' – ' + where : '') + (r.total != null ? ': ' + $(r.total) : '') +
                                 (r.tax ? ' (tax ' + $(r.tax) + ')' : '')));
      const added = (s.parts || []).some(x => x.expense && r.total != null && Math.abs((x.unit_cost || 0) - r.total) < 0.005);
      const acts_ = [];
      if (r.total != null && !added) {
        const use = el('button', 'primary', 'Add ' + $(r.total) + ' as an expense'); use.type = 'button';
        use.onclick = () => run(() => api('POST', '/api/stops/' + s.id + '/parts',
                                          {name: r.store ? 'Purchase – ' + r.store : 'Store purchase', type: 'expense', qty: 1, unit_cost: r.total}),
                                err, () => { loadParts(); });
        acts_.push(use);
      }
      // line items: add any of them to this stop as parts (a part you already have keeps your price; a new one starts
      // at the receipt price, which you can change on the Truck page)
      const vendor = s.stop_type === 'vendor';           // at a vendor the items were bought for the truck
      const have = new Set((vendor ? (s.bought || []) : (s.parts || []).filter(x => !x.expense)).map(x => x.name.toLowerCase()));
      const items = (r.items || []).filter(it => !have.has(it.description.toLowerCase()));
      if (items.length) {
        const det = el('details');
        det.append(el('summary', null, 'Items on the receipt (' + items.length + ')'));
        items.forEach(it => {
          const row = el('div', 'prow add one');
          row.append(el('div', 'pn', (it.qty && it.qty !== 1 ? it.qty + ' × ' : '') + it.description +
                                     (it.unit_price != null ? ' · ' + $(it.unit_price) : '')));
          const b = el('button', null, vendor ? '+ Add as bought' : '+ Add'); b.type = 'button';
          b.title = vendor ? 'Add to truck stock (Bought here)' : 'Add to this stop\'s parts';
          b.onclick = () => {
            const m = S.parts.find(p => p.name.toLowerCase() === it.description.toLowerCase() || (it.sku && p.sku && p.sku.toLowerCase() === it.sku.toLowerCase()));
            if (vendor) {
              const paid = it.amount != null ? it.amount : it.unit_price != null ? Math.round(it.unit_price * (it.qty || 1) * 100) / 100 : null;
              const body = m ? {part_id: m.id, qty: it.qty || 1, paid}
                             : {name: it.description, sku: it.sku || null, qty: it.qty || 1, paid, type: 'part'};
              run(() => api('POST', '/api/stops/' + s.id + '/bought', body), err, () => { loadParts(); });
              return;
            }
            const body = m ? {part_id: m.id, qty: it.qty || 1}
                           : {name: it.description, sku: it.sku || null, qty: it.qty || 1, unit_cost: it.unit_price, type: 'part'};
            run(() => api('POST', '/api/stops/' + s.id + '/parts', body), err, () => { loadParts(); });
          };
          row.append(b);
          det.append(row);
        });
        hint.append(det);
      }
      acts_.push(ignore);
      hint.append(btnRow(...acts_));
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
  const nx = S.flash && S.flashNext && S.stops.find(x => x.id === S.flashNext);
  if (nx) {
    const need = !nx.stop_type ? 'needs a type' : (nx.needs || []).length ? 'needs ' + nx.needs.join(', ') : 'not finished';
    const b = el('button', 'quick', 'Next: Stop ' + stopNum(nx) + ' ' + need + ' ›'); b.type = 'button';
    b.onclick = () => { S.flash = ''; S.flashNext = null; setSel(nx.id, true); openSheet(nx.id); };
    bar.append(b);
  }
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
      S.flash = n + ' stop' + (n > 1 ? 's' : '') + ' still in Draft: review them, then use Reports.';
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
  S.tool = null; S.sel = null; S.flash = ''; S.flashNext = null; S.drafts = {}; S.open.clear(); S.openDrives.clear();
  pathLayer.clearLayers(); candLayer.clearLayers(); markers = {};
  const m = el('div', null, 'Loading…'); m.id = 'msg'; listEl.replaceChildren(m);
  try {
    applyDay(await api('GET', '/api/day/' + S.date));
  } catch (e) { m.textContent = 'Could not load this day (' + e.message + ').'; return; }
  render(true);
  flushDrafts();                         // anything typed without signal earlier goes up now
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
