'use strict';
let PLACES = [], ALL = [];
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

// DELETE and PUT are sent as POST with X-Method-Override: some routers' intrusion prevention blocks a device for minutes
// after a DELETE (seen 2026-11-23). The server turns the header back into the real method.
const wireMethod = m => (m === 'DELETE' || m === 'PUT') ? 'POST' : m;
const overrideHeader = m => (m === 'DELETE' || m === 'PUT') ? {'X-Method-Override': m} : {};
async function api(method, url, body) {
  const r = await fetch(url, {method: wireMethod(method), headers: {...(body ? {'Content-Type': 'application/json'} : {}), ...overrideHeader(method)},
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
  [PLACES, ALL] = await Promise.all([api('GET', '/api/places' + (k ? '?kind=' + k : '')).then(d => d.places),
                                     api('GET', '/api/places').then(d => d.places)]);
  render();
}

// Places with the same name are one row: a vendor's branches, or a customer's old and new address after a move.
function render() {
  document.getElementById('count').textContent = '(' + PLACES.length + ')';
  listEl.replaceChildren();
  if (!PLACES.length) listEl.append(el('div', 'hint', 'Nothing here yet. Add one above.'));
  const groups = new Map();
  PLACES.forEach(p => {
    const k = p.link_id ? 'link:' + p.link_id : p.name.trim().toLowerCase();   // linked places, else the same name
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(p);
  });
  groups.forEach(ps => listEl.append(ps.length > 1 ? groupRow(ps) : row(ps[0])));
}

const openGroups = new Set();
function groupRow(ps) {
  ps = ps.slice().sort((a, b) => a.old_address - b.old_address);          // the current address first
  const linked = ps[0].link_id;
  const key = linked ? 'link:' + linked : ps[0].name.trim().toLowerCase();
  const wrap = el('div', 'place');
  const head = el('div', 'head'), body = el('div', 'body');
  const kinds = [...new Set(ps.map(p => p.kind))];
  body.append(el('div', 'n', ps[0].name + (linked ? ' 🔗' : '')),
              el('div', 'm', ps.length + ' locations · ' + ps.map(p => (p.address || 'no address') + (p.old_address ? ' (old)' : '')).join(' | ')));
  head.append(body, el('span', 'chip ' + kinds[0], kinds.map(k => KIND[k] || k).join(' / ')));
  const inside = el('div', 'edit'); inside.hidden = !openGroups.has(key);
  const hist = el('div', 'hist');
  const locs = el('div');
  const load_ = async () => {
    hist.replaceChildren(el('div', 'hint', 'Loading visits…'));
    try {
      const d = await api('POST', '/api/places/group-history', linked ? {group: linked} : {name: ps[0].name});
      const t = d.totals;
      historyList(hist, d, t.visits ? t.visits + ' visit' + (t.visits === 1 ? '' : 's') + ' at ' + ps.length + ' locations' +
        (t.minutes ? ' · ' + dur(t.minutes) + ' on site' : '') + ' · last ' + niceDate(t.last) : 'No visits yet', true);
    } catch (e) { hist.replaceChildren(el('div', 'err', e.message)); }
  };
  head.onclick = () => {
    if (openGroups.has(key)) openGroups.delete(key); else openGroups.add(key);
    inside.hidden = !openGroups.has(key);
    if (!inside.hidden) load_();
  };
  locs.append(el('div', 'tot', 'Locations'));
  ps.forEach(p => locs.append(row(p, true)));
  inside.append(hist, locs);
  wrap.append(head, inside);
  if (!inside.hidden) load_();
  return wrap;
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
function visitRow(v, withAddress) {
  const a = el('a', 'visit'); a.href = v.url;
  const bits = [v.type_label, v.work_category, v.visit_result].filter(Boolean);
  if (v.status !== 'Confirmed' && v.stop_type !== 'gas' && !['Lunch', 'Personal', 'Traffic'].includes(v.status)) bits.push('not finished');
  a.append(el('div', 'n', niceDate(v.date) + ' · ' + v.arrive + '–' + v.depart + ' (' + dur(v.minutes) + ')'),
           el('div', 'm', bits.join(' · ')));
  if (v.fuel) a.append(el('div', 'm', [v.fuel.gallons != null ? v.fuel.gallons + ' gal' : '', v.fuel.total != null ? money(v.fuel.total) : '',
                                       v.fuel.price_per_gallon ? '$' + v.fuel.price_per_gallon.toFixed(3) + '/gal' : ''].filter(Boolean).join(' · ')));
  if (withAddress && v.address) a.append(el('div', 'm', '📍 ' + v.address));
  if (v.summary) a.append(el('div', 's', v.summary));
  const extra = [v.ticket ? 'Ticket ' + v.ticket : '', v.parts ? v.parts + ' part' + (v.parts === 1 ? '' : 's') : '',
                 v.photos ? v.photos + ' photo' + (v.photos === 1 ? '' : 's') : ''].filter(Boolean);
  if (extra.length) a.append(el('div', 'm', extra.join(' · ')));
  return a;
}
function historyList(box, d, line, withAddress) {
  box.replaceChildren(el('div', 'tot', line));
  if (!d.visits.length) { box.append(el('div', 'hint', 'No visits recorded here yet.')); return; }
  const list = el('div');
  const show = n => { list.replaceChildren(...d.visits.slice(0, n).map(v => visitRow(v, withAddress))); };
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
      const d = await api('POST', '/api/gas-stations/history', {key: g.key});
      const t = d.totals;
      historyList(box, d, t.visits + ' fill-up' + (t.visits === 1 ? '' : 's') + ' · ' + money(t.total) +
        (t.gallons ? ' · ' + t.gallons + ' gal' : '') + (t.avg_price_per_gallon ? ' · avg $' + t.avg_price_per_gallon.toFixed(3) + '/gal' : ''));
    } catch (e) { box.replaceChildren(el('div', 'err', e.message)); }
  };
  wrap.append(head, box);
  return wrap;
}

// the group title is the current address's name; a location with another name shows it
const ps0name = p => { const g = ALL.filter(x => x.link_id && x.link_id === p.link_id).sort((a, b) => a.old_address - b.old_address); return g.length ? g[0].name : p.name; };
function row(p, inGroup) {
  const wrap = el('div', 'place');
  const head = el('div', 'head');
  const body = el('div', 'body');
  body.append(el('div', 'n', (inGroup ? '📍 ' + (p.address || 'No address saved') + (p.name !== ps0name(p) ? ' · ' + p.name : '') : p.name) +
                              (p.old_address ? ' (old address)' : '')),
    el('div', 'm', [inGroup ? '' : p.address, p.kind === 'client' ? p.times_confirmed + ' visit' + (p.times_confirmed === 1 ? '' : 's') : '',
      p.default_status === 'Personal' ? 'pre-marked Personal' : ''].filter(Boolean).join(' · ')));
  head.append(body, el('span', 'chip ' + p.kind, KIND[p.kind] || p.kind));
  const edit = el('div', 'edit'); edit.hidden = !open.has(p.id);
  const hist = el('div', 'hist');
  const toggle = () => { edit.hidden = !open.has(p.id); if (!edit.hidden) loadHistory(hist, p); };
  head.onclick = () => { if (open.has(p.id)) open.delete(p.id); else open.add(p.id); toggle(); };

  const name = el('input'); name.value = p.name;
  // the address in parts: the agent bills and matches on the suite (the one-line address is made from these)
  const ap = p.address_parts || {};
  const af = {};
  [['line1', 'Street'], ['line2', 'Suite / unit'], ['city', 'City'], ['state', 'State'], ['zip', 'ZIP']].forEach(([k]) => {
    af[k] = el('input'); af[k].value = ap[k] || '';
  });
  af.line2.placeholder = 'e.g. Suite 100'; af.state.maxLength = 20; af.state.autocapitalize = 'characters';
  af.zip.inputMode = 'numeric'; af.zip.maxLength = 10;
  const addrGrid = el('div', 'grid addr');
  addrGrid.append(lab('Street', af.line1), lab('Suite / unit', af.line2), lab('City', af.city), lab('State', af.state),
                  lab('ZIP', af.zip));
  const addrBox = el('div');
  addrBox.append(addrGrid, el('div', 'hint', 'Reports send the address in these parts. Add the suite: invoices are matched on it.'));
  const kind = el('select');
  Object.entries(KIND).forEach(([v, t]) => { const o = el('option', null, t); o.value = v; kind.append(o); });
  kind.value = p.kind;
  const status = el('select');
  [['Draft', 'Draft (I’ll review it)'], ['Personal', 'Personal']].forEach(([v, t]) => { const o = el('option', null, t); o.value = v; status.append(o); });
  status.value = p.default_status;
  const radius = el('input'); radius.inputMode = 'numeric'; radius.value = Math.round(p.radius_m);
  const grid = el('div', 'grid');
  grid.append(lab('Name', name), lab('Type', kind), lab('Pre-mark stops here as', status), lab('Radius (m)', radius));
  const hs = el('input'); hs.value = p.hubspot_company_id || ''; hs.inputMode = 'numeric'; hs.placeholder = 'e.g. 18234567890';
  const hsWrap = lab('HubSpot company ID', hs);
  hsWrap.append(el('div', 'hint', 'The digits in the company’s HubSpot URL. Reports carry it so the right company is updated.'));
  const qbo = el('input'); qbo.value = p.qbo_customer_id || ''; qbo.placeholder = 'optional';
  const bill = el('input'); bill.type = 'email'; bill.value = p.billing_email || ''; bill.placeholder = 'optional';
  bill.autocapitalize = 'off';
  hsWrap.append(lab('QuickBooks customer ID', qbo), lab('Billing email', bill),
                el('div', 'hint', 'Billing email: filled in for you on Send for invoice. QuickBooks keeps the customer’s own; this is only what was given on site.'));
  hsWrap.hidden = kind.value !== 'client';
  // link to another place (a customer's old and new address, or two names for one customer)
  const linkSel = el('select');
  const none = el('option', null, 'Not linked'); none.value = ''; linkSel.append(none);
  const mates = ALL.filter(x => p.link_id && x.link_id === p.link_id && x.id !== p.id);
  ALL.filter(x => x.id !== p.id).sort((a, b) => a.name.localeCompare(b.name)).forEach(x => {
    const o = el('option', null, x.name + (x.address ? ' – ' + x.address : '') + (x.old_address ? ' (old)' : ''));
    o.value = x.id; linkSel.append(o);
  });
  linkSel.value = mates.length ? String(mates[0].id) : '';
  const oldBox = el('input'); oldBox.type = 'checkbox'; oldBox.checked = p.old_address;
  const oldLab = el('label', 'chk'); oldLab.append(oldBox, ' Old address (the customer moved from here)');
  const linkBox = el('div');
  linkBox.append(lab('Linked to', linkSel), oldLab);
  if (mates.length) linkBox.append(el('div', 'hint', 'Linked with: ' + mates.map(x => x.name + (x.address ? ' (' + x.address + ')' : '')).join(', ')));
  linkBox.append(el('div', 'hint', 'Linked places show as one group with all their visits, even with different names.'));
  const err = el('div', 'err');
  const save = el('button', 'primary', 'Save');
  save.onclick = async () => {
    err.textContent = '';
    try {
      const body = {name: name.value, kind: kind.value, default_status: status.value,
                    radius_m: radius.value, old_address: oldBox.checked, hubspot_company_id: hs.value,
                    qbo_customer_id: qbo.value, billing_email: bill.value};
      Object.entries(af).forEach(([k, i]) => { body[k] = i.value; });
      if (linkSel.value !== (mates.length ? String(mates[0].id) : '')) body.link_to = linkSel.value || null;
      await api('PATCH', '/api/places/' + p.id, body);
      await load();
    } catch (e) { err.textContent = e.message; }
  };
  kind.onchange = () => { status.value = kind.value === 'home' ? 'Personal' : 'Draft'; hsWrap.hidden = kind.value !== 'client'; };
  const del = el('button', 'danger', 'Delete');
  del.onclick = async () => {
    if (!confirm('Delete “' + p.name + '”? Past stops keep their names.')) return;
    try { await api('DELETE', '/api/places/' + p.id); await load(); } catch (e) { err.textContent = e.message; }
  };
  const acts = el('div', 'row'); acts.append(save, del);
  const form = el('details', 'editplace');
  form.append(el('summary', null, 'Edit place'), grid, el('div', 'sub', 'Address'), addrBox, hsWrap, linkBox, acts, err);
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
    const d = await api('POST', '/api/places/search', {q: document.getElementById('q').value.trim(), google: forceGoogle});
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

// HubSpot company IDs in bulk: places CSV export / import (Check file first)
(function () {
  const msg = document.getElementById('hsmsg'), file = document.getElementById('hsfile');
  if (!msg) return;
  async function send(dry) {
    if (!file.files[0]) { msg.className = 'err'; msg.textContent = 'Choose the CSV file first.'; return; }
    const fd = new FormData(); fd.append('file', file.files[0]);
    msg.className = 'hint'; msg.textContent = dry ? 'Checking…' : 'Importing…';
    try {
      const r = await fetch('/api/places/import' + (dry ? '?dry=1' : ''), {method: 'POST', body: fd});
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || 'Request failed');
      const bits = [(dry ? 'Would update ' : 'Updated ') + d.updated + ' place' + (d.updated === 1 ? '' : 's'),
                    d.unchanged + ' unchanged'];
      if (!dry && d.stops_filled) bits.push(d.stops_filled + ' past stop' + (d.stops_filled === 1 ? '' : 's') + ' filled in');
      msg.className = d.errors.length ? 'err' : 'ok';
      msg.textContent = bits.join(' · ') + '.' + (d.errors.length ? ' Fix these first (nothing was saved): ' + d.errors.join('; ') : '') +
        (d.not_found.length ? ' Not found: ' + d.not_found.join('; ') + '.' : '');
      if (!dry && !d.errors.length) load();
    } catch (e) { msg.className = 'err'; msg.textContent = e.message; }
  }
  document.getElementById('hscheck').onclick = () => send(true);
  document.getElementById('hsimport').onclick = () => send(false);
})();
