'use strict';
let PLACES = [];
const listEl = document.getElementById('list');
const open = new Set();
const KIND = {client: 'Client', home: 'Home', office: 'Office', vendor: 'Vendor'};

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
const lab = (t, input) => { const w = el('div'); w.append(el('label', null, t), input); return w; };

async function api(method, url, body) {
  const r = await fetch(url, {method, headers: body ? {'Content-Type': 'application/json'} : {},
                              body: body ? JSON.stringify(body) : undefined});
  if (r.status === 401) { location.href = '/login'; throw new Error('Login required'); }
  let d = null;
  try { d = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok) throw new Error((d && d.error) || 'Request failed (' + r.status + ')');
  return d;
}

async function load() {
  const k = document.getElementById('filter').value;
  if (k === 'gas') {           // gas stations come from your fill-ups, they are not saved places
    const st = (await api('GET', '/api/gas-stations')).stations;
    document.getElementById('count').textContent = '(' + st.length + ')';
    listEl.replaceChildren();
    if (!st.length) listEl.append(el('div', 'hint', 'No fill-ups yet. Mark a stop as Gas and enter the total.'));
    st.forEach(g => listEl.append(stationRow(g)));
    return;
  }
  PLACES = (await api('GET', '/api/places' + (k ? '?kind=' + k : ''))).places;
  render();
}

function render() {
  document.getElementById('count').textContent = '(' + PLACES.length + ')';
  listEl.replaceChildren();
  if (!PLACES.length) listEl.append(el('div', 'hint', 'Nothing here yet. Add one above.'));
  PLACES.forEach(p => listEl.append(row(p)));
}

// ---- history: past visits, each opens that stop on its day ---------------------------
const money = n => '$' + Number(n || 0).toFixed(2);
function dur(min) {
  const h = Math.floor(min / 60), m = min % 60;
  return h ? h + ' h' + (m ? ' ' + m + ' min' : '') : m + ' min';
}
function niceDate(d) {
  return new Date(d + 'T12:00:00').toLocaleDateString(undefined, {weekday: 'short', month: 'short', day: 'numeric', year: 'numeric'});
}
function visitRow(v) {
  const a = el('a', 'visit'); a.href = v.url;
  const bits = [v.type_label, v.work_category, v.visit_result].filter(Boolean);
  if (v.status !== 'Confirmed' && v.stop_type !== 'gas' && !['Lunch', 'Personal', 'Traffic'].includes(v.status)) bits.push('not finished');
  a.append(el('div', 'n', niceDate(v.date) + ' · ' + v.arrive + '–' + v.depart + ' (' + dur(v.minutes) + ')'),
           el('div', 'm', bits.join(' · ')));
  if (v.fuel) a.append(el('div', 'm', [v.fuel.gallons != null ? v.fuel.gallons + ' gal' : '', v.fuel.total != null ? money(v.fuel.total) : '',
                                       v.fuel.price_per_gallon ? '$' + v.fuel.price_per_gallon.toFixed(3) + '/gal' : ''].filter(Boolean).join(' · ')));
  if (v.summary) a.append(el('div', 's', v.summary));
  const extra = [v.ticket ? 'Ticket ' + v.ticket : '', v.parts ? v.parts + ' part' + (v.parts === 1 ? '' : 's') : '',
                 v.photos ? v.photos + ' photo' + (v.photos === 1 ? '' : 's') : ''].filter(Boolean);
  if (extra.length) a.append(el('div', 'm', extra.join(' · ')));
  return a;
}
function historyList(box, d, line) {
  box.replaceChildren(el('div', 'tot', line));
  if (!d.visits.length) { box.append(el('div', 'hint', 'No visits recorded here yet.')); return; }
  const list = el('div');
  const show = n => { list.replaceChildren(...d.visits.slice(0, n).map(visitRow)); };
  show(10);
  box.append(list);
  if (d.visits.length > 10) {
    const more = el('button', null, 'Show all ' + d.visits.length); more.type = 'button';
    more.onclick = () => { show(d.visits.length); more.remove(); };
    box.append(more);
  }
}
async function loadHistory(box, p) {
  box.replaceChildren(el('div', 'hint', 'Loading visits…'));
  try {
    const d = await api('GET', '/api/places/' + p.id + '/history');
    const t = d.totals;
    historyList(box, d, t.visits ? t.visits + ' visit' + (t.visits === 1 ? '' : 's') +
      (t.minutes ? ' · ' + dur(t.minutes) + ' on site' : '') + ' · last ' + niceDate(t.last) : 'No visits yet');
  } catch (e) { box.replaceChildren(el('div', 'err', e.message)); }
}
function stationRow(g) {
  const wrap = el('div', 'place');
  const head = el('div', 'head'), body = el('div', 'body');
  body.append(el('div', 'n', g.name),
              el('div', 'm', [g.address, g.fills + ' fill-up' + (g.fills === 1 ? '' : 's'), money(g.total), 'last ' + niceDate(g.last)].filter(Boolean).join(' · ')));
  head.append(body, el('span', 'chip', 'Gas'));
  const box = el('div', 'edit'); box.hidden = true;
  head.onclick = async () => {
    box.hidden = !box.hidden;
    if (box.hidden || box.dataset.loaded) return;
    box.dataset.loaded = '1';
    box.replaceChildren(el('div', 'hint', 'Loading fill-ups…'));
    try {
      const d = await api('GET', '/api/gas-stations/history?key=' + encodeURIComponent(g.key));
      const t = d.totals;
      historyList(box, d, t.visits + ' fill-up' + (t.visits === 1 ? '' : 's') + ' · ' + money(t.total) +
        (t.gallons ? ' · ' + t.gallons + ' gal' : '') + (t.avg_price_per_gallon ? ' · avg $' + t.avg_price_per_gallon.toFixed(3) + '/gal' : ''));
    } catch (e) { box.replaceChildren(el('div', 'err', e.message)); }
  };
  wrap.append(head, box);
  return wrap;
}

function row(p) {
  const wrap = el('div', 'place');
  const head = el('div', 'head');
  const body = el('div', 'body');
  body.append(el('div', 'n', p.name),
    el('div', 'm', [p.address, p.kind === 'client' ? p.times_confirmed + ' visit' + (p.times_confirmed === 1 ? '' : 's') : '',
      p.default_status === 'Personal' ? 'pre-marked Personal' : ''].filter(Boolean).join(' · ')));
  head.append(body, el('span', 'chip ' + p.kind, KIND[p.kind] || p.kind));
  const edit = el('div', 'edit'); edit.hidden = !open.has(p.id);
  const hist = el('div', 'hist');
  const toggle = () => { edit.hidden = !open.has(p.id); if (!edit.hidden) loadHistory(hist, p); };
  head.onclick = () => { if (open.has(p.id)) open.delete(p.id); else open.add(p.id); toggle(); };

  const name = el('input'); name.value = p.name;
  const addr = el('input'); addr.value = p.address;
  const kind = el('select');
  Object.entries(KIND).forEach(([v, t]) => { const o = el('option', null, t); o.value = v; kind.append(o); });
  kind.value = p.kind;
  const status = el('select');
  [['Draft', 'Draft (I’ll review it)'], ['Personal', 'Personal']].forEach(([v, t]) => { const o = el('option', null, t); o.value = v; status.append(o); });
  status.value = p.default_status;
  const radius = el('input'); radius.inputMode = 'numeric'; radius.value = Math.round(p.radius_m);
  const grid = el('div', 'grid');
  grid.append(lab('Name', name), lab('Type', kind), lab('Pre-mark stops here as', status), lab('Radius (m)', radius));
  const err = el('div', 'err');
  const save = el('button', 'primary', 'Save');
  save.onclick = async () => {
    err.textContent = '';
    try {
      await api('PATCH', '/api/places/' + p.id, {name: name.value, address: addr.value, kind: kind.value,
        default_status: status.value, radius_m: radius.value});
      await load();
    } catch (e) { err.textContent = e.message; }
  };
  kind.onchange = () => { status.value = kind.value === 'home' ? 'Personal' : 'Draft'; };
  const del = el('button', 'danger', 'Delete');
  del.onclick = async () => {
    if (!confirm('Delete “' + p.name + '”? Past stops keep their names.')) return;
    try { await api('DELETE', '/api/places/' + p.id); await load(); } catch (e) { err.textContent = e.message; }
  };
  const acts = el('div', 'row'); acts.append(save, del);
  const form = el('details', 'editplace');
  form.append(el('summary', null, 'Edit place'), grid, lab('Address', addr), acts, err);
  edit.append(hist, form);
  wrap.append(head, edit);
  if (!edit.hidden) loadHistory(hist, p);
  return wrap;
}

// ---- adding ---------------------------------------------------------------
const addErr = document.getElementById('adderr');
const kindSel = document.getElementById('kind'), nameIn = document.getElementById('pname');
const results = document.getElementById('results');
let forceGoogle = false;

document.getElementById('here').onclick = async () => {
  addErr.textContent = '';
  try {
    await api('POST', '/api/places/from-last', {kind: kindSel.value, name: nameIn.value});
    nameIn.value = ''; await load();
  } catch (e) { addErr.textContent = e.message; }
};

document.getElementById('searchform').onsubmit = async ev => {
  ev.preventDefault();
  addErr.textContent = ''; results.replaceChildren(el('div', 'hint', 'Searching…'));
  try {
    const d = await api('GET', '/api/places/search?q=' + encodeURIComponent(document.getElementById('q').value.trim()) + (forceGoogle ? '&google=1' : ''));
    forceGoogle = false;
    results.replaceChildren();
    if (d.error) addErr.textContent = d.error;
    if (!d.candidates.length) results.append(el('div', 'hint', 'No matches.'));
    d.candidates.forEach(c => {
      const r = el('div', 'cand');
      r.append(el('div', 'n', c.name), el('div', 'a', c.address));
      r.onclick = async () => {
        try {
          await api('POST', '/api/places', {name: nameIn.value || c.name, address: c.address, lat: c.lat,
            lon: c.lon, kind: kindSel.value});
          nameIn.value = ''; results.replaceChildren(); await load();
        } catch (e) { addErr.textContent = e.message; }
      };
      results.append(r);
    });
    if (d.google_available && !d.google_used) {
      const g = el('button', null, 'Also search Google'); g.type = 'button';
      g.onclick = () => { forceGoogle = true; document.getElementById('searchform').requestSubmit(); };
      results.append(g);
    }
  } catch (e) { results.replaceChildren(); addErr.textContent = e.message; }
};

document.getElementById('filter').onchange = load;
load().catch(e => { listEl.textContent = 'Could not load places (' + e.message + ').'; });
