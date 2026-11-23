'use strict';
let PARTS = [];
let KIND = '';
const listEl = document.getElementById('list');
const open = new Set();

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
const lab = (t, input) => { const w = el('div'); w.append(el('label', null, t), input); return w; };
const money = v => v == null ? '' : '$' + Number(v).toFixed(2);

async function api(method, url, body, raw) {
  const r = await fetch(url, {method, headers: body && !raw ? {'Content-Type': 'application/json'} : {},
                              body: raw ? raw : body ? JSON.stringify(body) : undefined});
  if (r.status === 401) { location.href = '/login'; throw new Error('Login required'); }
  let d = null;
  try { d = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok) throw new Error((d && d.error) || 'Request failed (' + r.status + ')');
  return d;
}

let CH = {vendors: [], categories: [], units: []};
const showOff = () => document.getElementById('showoff').checked;

// A dropdown of fixed choices. `more` adds a "+ New…" entry that asks for a name (vendors, categories).
function choiceSelect(values, current, opts) {
  const sel = el('select');
  const add = (v, t) => { const o = el('option', null, t === undefined ? v : t); o.value = v; sel.append(o); return o; };
  if (opts.blank !== undefined) add('', opts.blank);
  const items = values.map(v => typeof v === 'string' ? [v, v] : [v.value, v.label]);
  if (current && !items.some(([v]) => v === current)) items.push([current, current]);   // an older value stays selectable
  items.forEach(([v, t]) => add(v, t));
  if (opts.more) add('__new__', '+ New ' + opts.more + '…');
  sel.value = current || (opts.blank !== undefined ? '' : (items[0] || [''])[0]);
  if (opts.more) {
    let prev = sel.value;
    sel.onchange = () => {
      if (sel.value !== '__new__') { prev = sel.value; return; }
      const name = (prompt('New ' + opts.more + ' name:') || '').trim();
      if (!name) { sel.value = prev; return; }
      const o = document.createElement('option'); o.value = name; o.textContent = name;
      sel.insertBefore(o, sel.querySelector('option[value="__new__"]'));
      sel.value = prev = name;
    };
  }
  return sel;
}
const vendorSel = cur => choiceSelect(CH.vendors, cur, {blank: 'No vendor', more: 'vendor'});
const categorySel = cur => choiceSelect(CH.categories, cur, {blank: 'No category', more: 'category'});
const unitSel = cur => choiceSelect(CH.units, cur || 'ea', {});
const packUnitSel = cur => choiceSelect(CH.units, cur || '', {blank: '—'});

async function load() {
  [PARTS, CH] = await Promise.all([
    api('GET', '/api/parts' + (showOff() ? '?all=1' : '')).then(r => r.parts),
    api('GET', '/api/parts/choices')]);
  fillAddForm();
  render();
}

function qtyText(p) {
  if (!p.tracked) return {t: 'not tracked', c: 'hint'};
  return {t: p.on_hand + ' ' + p.unit, c: 'qty ' + (p.negative ? 'neg' : p.low ? 'low' : '')};
}

function render() {
  const f = document.getElementById('filter').value.trim().toLowerCase();
  const shown = PARTS.filter(p => (!KIND || (KIND === 'low' ? p.low : p.kind === KIND)) && (!f || p.name.toLowerCase().includes(f) ||
    p.sku.toLowerCase().includes(f) || p.category.toLowerCase().includes(f) || (p.vendor || '').toLowerCase().includes(f)));
  document.getElementById('count').textContent = '(' + shown.length + (shown.length !== PARTS.length ? ' of ' + PARTS.length : '') + ')';
  listEl.replaceChildren();
  if (!shown.length) listEl.append(el('div', 'hint', PARTS.length ? 'No matches.' : 'No parts yet. Add one above or import a CSV.'));
  shown.forEach(p => listEl.append(partRow(p)));
}

function partRow(p) {
  const row = el('div', 'part');
  const head = el('div', 'head');
  const body = el('div', 'body');
  body.append(el('div', 'n', p.name),
    el('div', 'm', [p.active ? '' : 'REMOVED', p.kind !== 'part' ? p.kind : '', p.sku, p.category, p.vendor, money(p.unit_cost),
      p.billable ? 'billable' : 'not billable',
      p.low ? 'LOW – reorder at ' + p.reorder_at : ''].filter(Boolean).join(' · ')));
  const q = qtyText(p);
  head.append(body, el('div', q.c, q.t));
  const edit = el('div', 'edit'); edit.hidden = !open.has(p.id);
  head.onclick = () => { if (open.has(p.id)) open.delete(p.id); else open.add(p.id); edit.hidden = !open.has(p.id); };

  // stock
  const amt = el('input'); amt.inputMode = 'decimal'; amt.placeholder = 'amount';
  const note = el('input'); note.placeholder = 'note (optional)';
  const err = el('div', 'err');
  const adj = (body) => async () => {
    err.textContent = '';
    try { await api('POST', '/api/parts/' + p.id + '/adjust', Object.assign({note: note.value}, body())); await load(); }
    catch (e) { err.textContent = e.message; }
  };
  const add = el('button', null, '+ Add stock'); add.onclick = adj(() => ({delta: amt.value}));
  const rem = el('button', null, '− Remove stock'); rem.onclick = adj(() => ({delta: amt.value ? -Math.abs(Number(amt.value)) : ''}));
  const set = el('button', null, 'Set stock count to'); set.onclick = adj(() => ({set: amt.value}));
  const stock = el('div', 'row'); stock.append(amt, add);
  if (p.pack_size) {
    const pk = el('button', null, '+ Add ' + (p.pack_unit || 'pack') + ' (' + p.pack_size + ')');
    pk.title = 'Adds the pack size for each pack received; leave the amount blank for one';
    pk.onclick = adj(() => ({packs: amt.value || 1}));
    stock.append(pk);
  }
  stock.append(rem, set);

  // details
  const F = {};
  const mk = (k, label, val, type) => { const i = el('input'); i.value = val == null ? '' : val; if (type) i.inputMode = type; F[k] = i; return lab(label, i); };
  const grid = el('div', 'grid');
  const pick = (k, label, sel) => { F[k] = sel; return lab(label, sel); };
  grid.append(mk('name', 'Name', p.name), mk('sku', 'Part #', p.sku), pick('unit', 'Unit', unitSel(p.unit)),
    mk('unit_cost', 'Unit cost', p.unit_cost, 'decimal'), pick('category', 'Category', categorySel(p.category)),
    mk('reorder_at', 'Reorder at', p.reorder_at, 'decimal'), pick('vendor', 'Vendor', vendorSel(p.vendor)),
    mk('pack_size', 'Comes in packs of', p.pack_size, 'decimal'), pick('pack_unit', 'Pack is a', packUnitSel(p.pack_unit)));
  const kind = el('select');
  [['part', 'Part (billable)'], ['consumable', 'Consumable (not billed)'], ['expense', 'Expense (money, not stock)']].forEach(([v, t]) => { const o = el('option', null, t); o.value = v; kind.append(o); });
  kind.value = p.kind;
  grid.append(lab('Type', kind));
  kind.onchange = () => { bill.checked = kind.value === 'part'; };
  const bill = el('input'); bill.type = 'checkbox'; bill.checked = p.billable;
  const trk = el('input'); trk.type = 'checkbox'; trk.checked = p.tracked;
  const bl = el('label'); bl.append(bill, ' Billable');
  const tl = el('label'); tl.append(trk, ' Track truck stock');
  const flags = el('div', 'row'); flags.append(bl, tl);
  const save = el('button', 'primary', 'Save details');
  save.onclick = async () => {
    err.textContent = '';
    const b = {billable: bill.checked, tracked: trk.checked, type: kind.value};
    Object.keys(F).forEach(k => { b[k] = F[k].value; });
    try { await api('PATCH', '/api/parts/' + p.id, b); await load(); } catch (e) { err.textContent = e.message; }
  };
  const del = el('button', p.active ? 'danger' : null, p.active ? 'Remove from catalog' : 'Restore to catalog');
  del.onclick = async () => {
    if (p.active && !confirm('Remove “' + p.name + '” from the catalog? Past stops keep their records.')) return;
    try { await api('PATCH', '/api/parts/' + p.id, {active: !p.active}); await load(); } catch (e) { err.textContent = e.message; }
  };
  const acts = el('div', 'row'); acts.append(save, del);
  edit.append(el('label', null, 'Truck stock'), stock, note, grid, flags, acts, err);
  row.append(head, edit);
  return row;
}

document.getElementById('filter').oninput = render;
document.getElementById('showoff').onchange = () => load().catch(e => { listEl.textContent = e.message; });

document.querySelectorAll('#tabs button').forEach(b => {
  b.onclick = () => {
    KIND = b.dataset.k;
    document.querySelectorAll('#tabs button').forEach(x => x.classList.toggle('on', x === b));
    render();
  };
});
document.getElementById('a_kind').onchange = ev => { document.getElementById('a_bill').checked = ev.target.value === 'part'; };

// ---- count the truck / shopping list ----------------------------------------
const tools = document.getElementById('tools');
const when = ts => ts ? new Date(ts * 1000).toLocaleDateString([], {month: 'short', day: 'numeric'}) : 'never';

async function showCount() {
  tools.replaceChildren(el('div', 'hint', 'Loading…'));
  const rows = (await api('GET', '/api/parts/count-sheet' + (KIND ? '?kind=' + KIND : ''))).parts;
  const box = el('div', 'box');
  box.append(el('div', 'hint', 'Enter what is really on the truck. Only the lines you change are saved. “Used” is what stops logged since the last count.'));
  const inputs = {};
  if (!rows.length) box.append(el('div', 'hint', 'No tracked items yet. Give items an “on truck” quantity or turn on tracking first.'));
  rows.forEach(p => {
    const r = el('div', 'crow');
    const l = el('div'); l.append(el('div', 'n', p.name),
      el('div', 'm', ['expected ' + p.on_hand + ' ' + p.unit, 'used ' + p.used_since, 'counted ' + when(p.last_counted)].join(' · ')));
    const i = el('input'); i.inputMode = 'decimal'; i.placeholder = String(p.on_hand); inputs[p.id] = [i, p];
    r.append(l, i); box.append(r);
  });
  const err = el('div', 'err'), res = el('div');
  const save = el('button', 'primary', 'Save count');
  save.onclick = async () => {
    err.textContent = ''; res.replaceChildren();
    const counts = {};
    Object.entries(inputs).forEach(([id, [i]]) => { if (i.value.trim() !== '') counts[id] = i.value.trim(); });
    if (!Object.keys(counts).length) { err.textContent = 'Enter at least one quantity.'; return; }
    try {
      const d = await api('POST', '/api/parts/count', {counts});
      res.append(el('div', 'ok', d.changed + ' changed, ' + d.unchanged + ' matched.'));
      d.details.forEach(x => res.append(el('div', 'hint', x.name + ': ' + x.was + ' → ' + x.now + ' (' + (x.diff > 0 ? '+' : '') + x.diff + ')')));
      await load();
    } catch (e) { err.textContent = e.message; }
  };
  box.append(save, err, res);
  tools.replaceChildren(box);
}

async function showShopping() {
  tools.replaceChildren(el('div', 'hint', 'Loading…'));
  const groups = (await api('GET', '/api/parts/shopping')).groups;
  const box = el('div', 'box');
  if (!groups.length) { box.append(el('div', 'ok', 'Nothing is at or below its reorder level.')); tools.replaceChildren(box); return; }
  let text = '';
  groups.forEach(g => {
    const w = el('div', 'group'); w.append(el('h3', null, g.vendor));
    text += g.vendor + '\n';
    g.items.forEach(it => {
      const line = (it.packs ? it.packs + ' ' + (it.pack_unit || 'pack') + (it.packs > 1 ? 's' : '') + ' of ' + it.pack_size + ' (' + it.suggest + ' ' + it.unit + ')'
                           : it.suggest + ' ' + it.unit) + ' × ' + it.name + (it.sku ? ' (' + it.sku + ')' : '');
      w.append(el('div', null, line)); w.append(el('div', 'hint', 'have ' + it.on_hand + ', reorder at ' + it.reorder_at + (it.kind === 'consumable' ? ' · consumable' : '')));
      text += '  ' + line + '\n';
    });
    box.append(w);
  });
  box.append(el('div', 'hint', 'Suggested quantities restock to twice the reorder level.'));
  const copy = el('button', null, 'Copy list'); copy.onclick = async () => {
    try { await navigator.clipboard.writeText(text); copy.textContent = 'Copied'; } catch (e) { copy.textContent = 'Copy not available'; }
  };
  box.append(copy);
  tools.replaceChildren(box);
}
document.getElementById('countbtn').onclick = () => showCount().catch(e => { tools.textContent = e.message; });
document.getElementById('shopbtn').onclick = () => showShopping().catch(e => { tools.textContent = e.message; });

function fillAddForm() {
  const swap = (id, sel) => { const old = document.getElementById(id); sel.id = id; old.replaceWith(sel); };
  const keep = id => { const e = document.getElementById(id); return e ? e.value : ''; };
  const [v, c, u, pu] = ['a_vendor', 'a_category', 'a_unit', 'a_packunit'].map(keep);
  swap('a_vendor', vendorSel(v)); swap('a_category', categorySel(c));
  swap('a_unit', unitSel(u)); swap('a_packunit', packUnitSel(pu));
}

document.getElementById('a_go').onclick = async () => {
  const err = document.getElementById('a_err'); err.textContent = '';
  const v = id => document.getElementById(id).value;
  try {
    await api('POST', '/api/parts', {name: v('a_name'), sku: v('a_sku'), unit: v('a_unit'),
      unit_cost: v('a_cost'), on_hand: v('a_oh'), billable: document.getElementById('a_bill').checked,
      type: v('a_kind'), vendor: v('a_vendor'), category: v('a_category'), reorder_at: v('a_reorder'),
      pack_size: v('a_pack'), pack_unit: v('a_packunit')});
    ['a_name', 'a_sku', 'a_cost', 'a_oh', 'a_reorder', 'a_pack'].forEach(id => { document.getElementById(id).value = ''; });
    await load();
  } catch (e) { err.textContent = e.message; }
};

async function runCsv(dry) {
  const out = document.getElementById('csvres'); out.replaceChildren();
  const f = document.getElementById('csvfile').files[0];
  if (!f) { out.append(el('div', 'err', 'Choose a CSV file first.')); return; }
  try {
    const fd = new FormData(); fd.append('file', f);
    const r = await api('POST', '/api/parts/import' + (dry ? '?dry=1' : ''), fd, fd);
    out.append(el('div', 'ok', (dry ? 'Check only, nothing saved. Would import: ' : 'Imported: ') +
      r.created + ' new, ' + r.updated + ' updated, ' + r.skipped + ' skipped.'));
    if (r.ignored_columns.length) out.append(el('div', 'hint', 'Ignored columns: ' + r.ignored_columns.join(', ')));
    r.errors.slice(0, 20).forEach(m => out.append(el('div', 'err', m)));
    if (r.errors.length > 20) out.append(el('div', 'hint', '…and ' + (r.errors.length - 20) + ' more.'));
    if (!dry) await load();
  } catch (e) { out.append(el('div', 'err', e.message)); }
}
document.getElementById('csvgo').onclick = () => runCsv(false);
document.getElementById('csvcheck').onclick = () => runCsv(true);

load().catch(e => { listEl.textContent = 'Could not load parts (' + e.message + ').'; });

// ---- fuel: fill-ups logged on Gas stops (day page) ----------------------------
async function showFuel(range) {
  range = range || 'month';
  const today = new Date(), iso = d => d.toLocaleDateString('en-CA');
  const from = {month: iso(new Date(today.getFullYear(), today.getMonth(), 1)),
                d30: iso(new Date(today - 29 * 864e5)), year: iso(new Date(today.getFullYear(), 0, 1)), all: ''}[range];
  tools.replaceChildren(el('div', 'hint', 'Loading…'));
  const d = await api('GET', '/api/fuel' + (from ? '?from=' + from : ''));
  const box = el('div', 'box');
  const pick = el('div', 'row');
  [['month', 'This month'], ['d30', 'Last 30 days'], ['year', 'This year'], ['all', 'All']].forEach(([k, t]) => {
    const b = el('button', k === range ? 'on primary' : null, t); b.type = 'button';
    b.onclick = () => showFuel(k).catch(e => { tools.textContent = e.message; });
    pick.append(b);
  });
  const sm = d.summary, money = v => v == null ? '—' : '$' + v.toFixed(2);
  const tiles = el('div', 'tiles');
  [['Spent', money(sm.total)], ['Gallons', sm.gallons ? sm.gallons.toFixed(1) : '—'],
   ['Avg $/gal', sm.avg_price_per_gallon == null ? '—' : '$' + sm.avg_price_per_gallon.toFixed(3)],
   ['MPG', sm.mpg == null ? '—' : sm.mpg.toFixed(1)], ['Miles (odometer)', sm.miles || '—'],
   ['Fuel $/mile', sm.cost_per_mile == null ? '—' : '$' + sm.cost_per_mile.toFixed(3)]].forEach(([k, v]) => {
    const t = el('div', 'tile'); t.append(el('b', null, String(v)), el('span', 'hint', k)); tiles.append(t);
  });
  box.append(pick, tiles);
  if (!d.fills.length) {
    box.append(el('div', 'hint', 'No fill-ups yet. On the day page, open a stop, tap Gas, and enter gallons, total and the odometer.'));
  } else {
    const t = el('table', 'fuellog');
    const hr = el('tr'); ['Date', 'Station', 'Gal', 'Total', '$/gal', 'Odometer', 'MPG'].forEach(h => hr.append(el('th', null, h)));
    t.append(hr);
    d.fills.forEach(f => {
      const r = el('tr');
      const a = el('a', null, f.date.slice(5) + ' ' + f.time); a.href = '/day/' + f.date;
      const c0 = el('td'); c0.append(a);
      r.append(c0, el('td', null, f.station || 'Gas'), el('td', null, f.gallons == null ? '' : f.gallons.toFixed(2)),
               el('td', null, f.total == null ? '' : f.total.toFixed(2)),
               el('td', null, f.price_per_gallon == null ? '' : f.price_per_gallon.toFixed(3)),
               el('td', null, f.odometer == null ? '' : String(f.odometer)), el('td', null, f.mpg == null ? '' : f.mpg.toFixed(1)));
      t.append(r);
    });
    box.append(t);
    box.append(el('div', 'hint', 'MPG uses the odometer change since the previous fill-up, so fill the tank each time and enter the odometer. ' +
      'A missed fill-up shows no MPG for the next one.'));
  }
  tools.replaceChildren(box);
}
document.getElementById('fuelbtn').onclick = () => showFuel().catch(e => { tools.textContent = e.message; });
