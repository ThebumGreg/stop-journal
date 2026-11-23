'use strict';
// Truck: tabs for Parts (list -> one page per part), Stock (count, low, shopping), Fuel and Import / export.
// The tab (and an open part) live in the address: #parts, #stock, #fuel, #data, #part-12.
let PARTS = [];
let KIND = '';
let CH = {vendors: [], categories: [], units: []};
let SHOW_OFF = false;
const view = document.getElementById('view');

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}
const lab = (t, input) => { const w = el('div'); w.append(el('label', null, t), input); return w; };
const money = v => v == null ? '' : '$' + Number(v).toFixed(2);
const when = ts => ts ? new Date(ts * 1000).toLocaleDateString([], {month: 'short', day: 'numeric'}) : 'never';
const whenFull = ts => new Date(ts * 1000).toLocaleString([], {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
const niceDate = d => new Date(d + 'T12:00:00').toLocaleDateString([], {month: 'short', day: 'numeric', year: 'numeric'});

async function api(method, url, body, raw) {
  const r = await fetch(url, {method, headers: body && !raw ? {'Content-Type': 'application/json'} : {},
                              body: raw ? raw : body ? JSON.stringify(body) : undefined});
  if (r.status === 401) { location.href = '/login'; throw new Error('Login required'); }
  let d = null;
  try { d = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok) throw new Error((d && d.error) || 'Request failed (' + r.status + ')');
  return d;
}

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
const KINDS = [['part', 'Part (billable)'], ['consumable', 'Consumable (not billed)'], ['expense', 'Expense (money, not stock)']];
function kindSel(cur) {
  const s = el('select');
  KINDS.forEach(([v, t]) => { const o = el('option', null, t); o.value = v; s.append(o); });
  s.value = cur || 'part';
  return s;
}

async function loadParts() {
  [PARTS, CH] = await Promise.all([
    api('GET', '/api/parts' + (SHOW_OFF ? '?all=1' : '')).then(r => r.parts),
    api('GET', '/api/parts/choices')]);
}

function qtyText(p) {
  if (!p.tracked) return {t: 'not tracked', c: 'hint'};
  return {t: p.on_hand + ' ' + p.unit, c: 'qty ' + (p.negative ? 'neg' : p.low ? 'low' : '')};
}

// ---- routing between tabs ---------------------------------------------------------------
function route() {
  const h = location.hash.slice(1) || 'parts';
  const m = /^part-(\d+)$/.exec(h);
  const vm = /^vendor\/(.+)$/.exec(h);
  const tab = m ? 'parts' : vm ? 'vendors' : h;
  document.querySelectorAll('#subnav a').forEach(a => a.classList.toggle('on', a.dataset.v === tab));
  view.replaceChildren(el('div', 'hint', 'Loading…'));
  const show = m ? partPage(+m[1]) : vm ? vendorPage(decodeURIComponent(vm[1]))
    : ({parts: partsView, stock: stockView, vendors: vendorsView, fuel: () => fuelView('month'), data: dataView}[tab] || partsView)();
  Promise.resolve(show).catch(e => { view.replaceChildren(el('div', 'err', e.message)); });
}
window.addEventListener('hashchange', route);

// ---- Parts: search, filter, list ----------------------------------------------------------
let FILTER = '';
async function partsView() {
  await loadParts();
  const bar = el('div', 'bar');
  const q = el('input'); q.placeholder = 'Search name, part #, category or vendor'; q.value = FILTER; q.type = 'search';
  const add = el('button', 'primary', '+ New part'); add.type = 'button';
  add.onclick = () => newPartSheet();
  bar.append(q, add);
  const chips = el('div', 'chips');
  [['', 'All'], ['part', 'Parts'], ['consumable', 'Consumables'], ['expense', 'Expenses'], ['low', 'Low stock']].forEach(([k, t]) => {
    const b = el('button', KIND === k ? 'on' : null, t); b.type = 'button';
    b.onclick = () => { KIND = k; chips.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b)); draw(); };
    chips.append(b);
  });
  const off = el('label', 'chk'); const offc = el('input'); offc.type = 'checkbox'; offc.checked = SHOW_OFF;
  off.append(offc, ' Show removed items');
  offc.onchange = async () => { SHOW_OFF = offc.checked; await loadParts(); draw(); };
  const count = el('div', 'hint');
  const list = el('div');
  function draw() {
    const f = FILTER.trim().toLowerCase();
    const shown = PARTS.filter(p => (!KIND || (KIND === 'low' ? p.low : p.kind === KIND)) && (!f || p.name.toLowerCase().includes(f) ||
      p.sku.toLowerCase().includes(f) || p.category.toLowerCase().includes(f) || (p.vendor || '').toLowerCase().includes(f)));
    count.textContent = shown.length + (shown.length !== PARTS.length ? ' of ' + PARTS.length : '') + ' item' + (PARTS.length === 1 ? '' : 's');
    list.replaceChildren();
    if (!shown.length) list.append(el('div', 'hint', PARTS.length ? 'No matches.' : 'No parts yet. Tap + New part, or import a CSV (Import / export).'));
    shown.forEach(p => {
      const a = el('a', 'part'); a.href = '#part-' + p.id;
      const body = el('div', 'body');
      body.append(el('div', 'n', p.name),
        el('div', 'm', [p.active ? '' : 'REMOVED', p.kind !== 'part' ? p.kind : '', p.sku, p.bin ? '📍 ' + p.bin : '', p.vendor, money(p.unit_cost),
          p.low ? 'LOW (reorder at ' + p.reorder_at + ')' : ''].filter(Boolean).join(' · ')));
      const qt = qtyText(p);
      a.append(body, el('div', qt.c, qt.t));
      list.append(a);
    });
  }
  q.oninput = () => { FILTER = q.value; draw(); };
  view.replaceChildren(bar, chips, off, count, list);
  draw();
}

// ---- New part: a sheet with the fields grouped ----------------------------------------------
function newPartSheet() {
  const bg = el('div', 'sheetbg'), box = el('div', 'sheetbox');
  const F = {};
  const inp = (k, ph, mode) => { const i = el('input'); if (ph) i.placeholder = ph; if (mode) i.inputMode = mode; F[k] = i; return i; };
  const basics = el('fieldset'); basics.append(el('legend', null, 'What it is'));
  F.kind = kindSel('part');
  basics.append(lab('Name', inp('name', 'e.g. 1/2 in. ball valve')), lab('Type', F.kind), lab('Part #', inp('sku', 'optional')),
                lab('Vendor', F.vendor = vendorSel('')), lab('Category', F.category = categorySel('')));
  const money_ = el('fieldset'); money_.append(el('legend', null, 'Unit and cost'));
  const bill = el('input'); bill.type = 'checkbox'; bill.checked = true;
  const bl = el('label', 'chk'); bl.append(bill, ' Billable to the customer');
  F.kind.onchange = () => { bill.checked = F.kind.value === 'part'; };
  money_.append(lab('Counted in (unit)', F.unit = unitSel('ea')), lab('Unit cost', inp('unit_cost', '0.00', 'decimal')), bl);
  const stock = el('fieldset'); stock.append(el('legend', null, 'Truck stock (optional)'));
  stock.append(lab('Shelf / bin', inp('bin', 'e.g. Shelf 2')), lab('On the truck now', inp('on_hand', 'blank = do not track stock', 'decimal')),
               lab('Reorder at', inp('reorder_at', '', 'decimal')),
               lab('Comes in packs of', inp('pack_size', 'e.g. 25', 'decimal')), lab('Pack is a', F.pack_unit = packUnitSel('')));
  const err = el('div', 'err');
  const save = el('button', 'primary', 'Add part'); save.type = 'button';
  const cancel = el('button', null, 'Cancel'); cancel.type = 'button';
  const close = () => bg.remove();
  cancel.onclick = close;
  bg.onclick = e => { if (e.target === bg) close(); };
  save.onclick = async () => {
    err.textContent = '';
    const b = {billable: bill.checked, type: F.kind.value};
    ['name', 'sku', 'vendor', 'category', 'unit', 'unit_cost', 'on_hand', 'reorder_at', 'pack_size', 'pack_unit', 'bin'].forEach(k => { b[k] = F[k].value; });
    try {
      const p = await api('POST', '/api/parts', b);
      close();
      location.hash = 'part-' + p.id;
    } catch (e) { err.textContent = e.message; }
  };
  const acts = el('div', 'row'); acts.append(save, cancel);
  const dup = el('div');
  const checkDup = async () => {
    const name = F.name.value.trim(), sku = F.sku.value.trim();
    if (!name && !sku) { dup.replaceChildren(); return; }
    const d = await api('GET', '/api/parts/similar?name=' + encodeURIComponent(name) + '&sku=' + encodeURIComponent(sku)).catch(() => ({parts: []}));
    dup.replaceChildren();
    if (!d.parts.length) return;
    const w = el('div', 'box warnbox');
    w.append(el('div', null, 'You may already have this (' + d.parts[0].why + '):'));
    d.parts.forEach(p => {
      const a = el('a', null, p.name + (p.sku ? ' · ' + p.sku : '') + (p.active ? '' : ' (removed)')); a.href = '#part-' + p.id;
      a.onclick = () => close();
      const line = el('div'); line.append('• ', a, p.tracked ? ' · ' + p.on_hand + ' ' + p.unit : ''); w.append(line);
    });
    w.append(el('div', 'hint', 'Tap one to open it instead, or carry on if this really is a different part.'));
    dup.append(w);
  };
  F.name.addEventListener('change', checkDup); F.sku.addEventListener('change', checkDup);
  box.append(el('h2', null, 'New part'), basics, dup, money_, stock, err, acts);
  bg.append(box);
  document.body.append(bg);
  F.name.focus();
}

// ---- One part: stock, details, where it was used, stock changes ------------------------------
async function partPage(id) {
  await loadParts();
  const d = await api('GET', '/api/parts/' + id + '/history');
  const p = d.part;
  const back = el('a', 'back', '‹ All parts'); back.href = '#parts';
  const head = el('div');
  head.append(el('h1', 'page-title', p.name),
              el('div', 'hint', [p.active ? '' : 'REMOVED', KINDS.find(k => k[0] === p.kind)[1], p.sku ? 'Part # ' + p.sku : '', p.bin ? '📍 ' + p.bin : '',
                                 p.vendor, money(p.unit_cost) + (p.unit_cost != null ? ' / ' + p.unit : '')].filter(Boolean).join(' · ')));
  if (p.vendor) { const va = el('a', 'back', 'Vendor: ' + p.vendor + ' ›'); va.href = vendorHref(p.vendor); head.append(va); }
  const err = el('div', 'err');

  // stock card
  const stockCard = el('section', 'card');
  stockCard.append(el('h2', null, 'Truck stock'));
  if (p.kind === 'expense') stockCard.append(el('div', 'hint', 'Expenses are money, not stock.'));
  else {
    const q = qtyText(p);
    stockCard.append(el('div', 'stocknow ' + q.c, p.tracked ? p.on_hand + ' ' + p.unit : 'Not tracked'));
    if (p.tracked && p.reorder_at != null) stockCard.append(el('div', 'hint', 'Reorder at ' + p.reorder_at + (p.low ? ' (low now)' : '')));
    const amt = el('input'); amt.inputMode = 'decimal'; amt.placeholder = 'amount';
    const note = el('input'); note.placeholder = 'note (optional)';
    const adj = body => async () => {
      err.textContent = '';
      try { await api('POST', '/api/parts/' + p.id + '/adjust', Object.assign({note: note.value}, body())); route(); }
      catch (e) { err.textContent = e.message; }
    };
    const row = el('div', 'row');
    const add = el('button', null, '+ Add stock'); add.onclick = adj(() => ({delta: amt.value}));
    const rem = el('button', null, '− Remove stock'); rem.onclick = adj(() => ({delta: amt.value ? -Math.abs(Number(amt.value)) : ''}));
    const set = el('button', null, 'Set count to'); set.onclick = adj(() => ({set: amt.value}));
    row.append(amt, add);
    if (p.pack_size) {
      const pk = el('button', null, '+ Add ' + (p.pack_unit || 'pack') + ' (' + p.pack_size + ')');
      pk.title = 'Adds the pack size for each pack received; leave the amount blank for one';
      pk.onclick = adj(() => ({packs: amt.value || 1}));
      row.append(pk);
    }
    row.append(rem, set);
    stockCard.append(row, note);
    if (!p.tracked) stockCard.append(el('div', 'hint', 'Set a count to start tracking it on the truck.'));
  }

  // used on
  const used = el('section', 'card');
  used.append(el('h2', null, 'Used on'));
  used.append(el('div', 'hint', d.totals.stops ? d.totals.used + ' ' + p.unit + ' on ' + d.totals.stops + ' stop' + (d.totals.stops === 1 ? '' : 's') : 'Not used on a stop yet.'));
  d.uses.slice(0, 15).forEach(u => {
    const a = el('a', 'hrow'); a.href = u.url;
    a.append(el('div', null, niceDate(u.date) + ' · ' + (u.business_name || 'Stop')),
             el('div', 'm', u.qty + ' ' + u.unit + (u.unit_cost != null ? ' @ ' + money(u.unit_cost) : '') + (u.billable ? '' : ' · not billed')));
    used.append(a);
  });

  // details
  const det = el('details', 'card');
  det.append(el('summary', null, 'Edit details'));
  const F = {};
  const mk = (k, label, val, type) => { const i = el('input'); i.value = val == null ? '' : val; if (type) i.inputMode = type; F[k] = i; return lab(label, i); };
  const pick = (k, label, sel) => { F[k] = sel; return lab(label, sel); };
  const grid = el('div', 'grid');
  const kind = kindSel(p.kind);
  grid.append(mk('name', 'Name', p.name), lab('Type', kind), mk('sku', 'Part #', p.sku), pick('unit', 'Unit', unitSel(p.unit)),
    mk('unit_cost', 'Unit cost', p.unit_cost, 'decimal'), pick('vendor', 'Vendor', vendorSel(p.vendor)),
    mk('bin', 'Shelf / bin', p.bin), pick('category', 'Category', categorySel(p.category)), mk('reorder_at', 'Reorder at', p.reorder_at, 'decimal'),
    mk('pack_size', 'Comes in packs of', p.pack_size, 'decimal'), pick('pack_unit', 'Pack is a', packUnitSel(p.pack_unit)));
  const bill = el('input'); bill.type = 'checkbox'; bill.checked = p.billable;
  const trk = el('input'); trk.type = 'checkbox'; trk.checked = p.tracked;
  kind.onchange = () => { bill.checked = kind.value === 'part'; };
  const bl = el('label', 'chk'); bl.append(bill, ' Billable');
  const tl = el('label', 'chk'); tl.append(trk, ' Track truck stock');
  const save = el('button', 'primary', 'Save details');
  save.onclick = async () => {
    err.textContent = '';
    const b = {billable: bill.checked, tracked: trk.checked, type: kind.value};
    Object.keys(F).forEach(k => { b[k] = F[k].value; });
    try { await api('PATCH', '/api/parts/' + p.id, b); route(); } catch (e) { err.textContent = e.message; }
  };
  const del = el('button', p.active ? 'danger' : null, p.active ? 'Remove from catalog' : 'Restore to catalog');
  del.onclick = async () => {
    if (p.active && !confirm('Remove “' + p.name + '” from the catalog? Past stops keep their records.')) return;
    try { await api('PATCH', '/api/parts/' + p.id, {active: !p.active}); route(); } catch (e) { err.textContent = e.message; }
  };
  const flags = el('div', 'row'); flags.append(bl, tl);
  const acts = el('div', 'row'); acts.append(save, del);
  det.append(grid, flags, acts);

  // stock changes
  const log = el('details', 'card');
  log.append(el('summary', null, 'Stock changes (' + d.log.length + ')'));
  d.log.forEach(x => log.append(el('div', 'hrow', whenFull(x.ts) + ' · ' + (x.delta > 0 ? '+' : '') + x.delta + ' · ' + x.reason + (x.note ? ' · ' + x.note : ''))));
  if (!d.log.length) log.append(el('div', 'hint', 'No changes yet.'));

  view.replaceChildren(back, head, err, stockCard, used, det, log);
}

// ---- Stock: count the truck, shopping list ------------------------------------------------
let STOCK = 'count';
async function stockView() {
  const tabs = el('div', 'row tools');
  const out = el('div');
  [['count', 'Count the truck'], ['shop', 'Shopping list']].forEach(([k, t]) => {
    const b = el('button', STOCK === k ? 'on' : null, t); b.type = 'button';
    b.onclick = () => { STOCK = k; stockView().catch(e => { out.textContent = e.message; }); };
    tabs.append(b);
  });
  view.replaceChildren(tabs, out);
  out.append(await (STOCK === 'count' ? countBox() : shoppingBox()));
}

async function countBox() {
  const rows = (await api('GET', '/api/parts/count-sheet')).parts;
  const box = el('div', 'box');
  box.append(el('div', 'hint', 'Enter what is really on the truck, shelf by shelf. Only the lines you change are saved. “Used” is what stops logged since the last count.'));
  const inputs = {};
  if (!rows.length) box.append(el('div', 'hint', 'No tracked items yet. Give items an “on truck” quantity or turn on tracking first.'));
  let shelf = null;
  rows.forEach(p => {
    if (p.bin !== shelf) { shelf = p.bin; box.append(el('h3', null, shelf ? '📍 ' + shelf : 'No shelf set')); }
    const r = el('div', 'crow');
    const l = el('div'); l.append(el('div', 'n', p.name),
      el('div', 'hint', ['expected ' + p.on_hand + ' ' + p.unit, 'used ' + p.used_since, 'counted ' + when(p.last_counted)].join(' · ')));
    const i = el('input'); i.inputMode = 'decimal'; i.placeholder = String(p.on_hand); inputs[p.id] = i;
    r.append(l, i); box.append(r);
  });
  const err = el('div', 'err'), res = el('div');
  const save = el('button', 'primary', 'Save count');
  save.onclick = async () => {
    err.textContent = ''; res.replaceChildren();
    const counts = {};
    Object.entries(inputs).forEach(([id, i]) => { if (i.value.trim() !== '') counts[id] = i.value.trim(); });
    if (!Object.keys(counts).length) { err.textContent = 'Enter at least one quantity.'; return; }
    try {
      const d = await api('POST', '/api/parts/count', {counts});
      res.append(el('div', 'ok', d.changed + ' changed, ' + d.unchanged + ' matched.'));
      d.details.forEach(x => res.append(el('div', 'hint', x.name + ': ' + x.was + ' → ' + x.now + ' (' + (x.diff > 0 ? '+' : '') + x.diff + ')')));
    } catch (e) { err.textContent = e.message; }
  };
  box.append(save, err, res);
  return box;
}

// Shopping list: grouped by vendor. Each line has the amount to buy (in packs when bought in packs) and ✓ Bought,
// which adds it to the truck; "All bought" does the whole vendor. Share sends a clean checklist to Notes / Messages.
async function shoppingBox() {
  const groups = (await api('GET', '/api/parts/shopping')).groups;
  const box = el('div');
  const msg = el('div');
  if (!groups.length) { box.append(el('div', 'box ok', 'Nothing is at or below its reorder level.')); return box; }
  const lineText = it => (it.packs ? it.packs + ' ' + (it.pack_unit || 'pack') + (it.packs > 1 ? 's' : '') + ' of ' + it.pack_size
                                   : it.suggest + ' ' + it.unit) + ' ' + it.name + (it.sku ? ' (#' + it.sku + ')' : '');
  const share = el('button', 'primary', 'Share list'); share.type = 'button';
  share.onclick = async () => {
    const text = 'Shopping list ' + new Date().toLocaleDateString() + '\n\n' +
      groups.map(g => g.vendor + '\n' + g.items.map(it => '☐ ' + lineText(it)).join('\n')).join('\n\n');
    try {
      if (navigator.share) await navigator.share({title: 'Shopping list', text});
      else { await navigator.clipboard.writeText(text); share.textContent = 'Copied'; }
    } catch (e) { if (e.name !== 'AbortError') { try { await navigator.clipboard.writeText(text); share.textContent = 'Copied'; } catch (x) { share.textContent = 'Share not available'; } } }
  };
  const top = el('div', 'row'); top.append(share, el('span', 'hint', 'Restocks to twice the reorder level, in whole packs.'));
  box.append(top, msg);
  const bought = async (vendor, items) => {
    msg.replaceChildren();
    try {
      const d = await api('POST', '/api/parts/bought', {vendor, items});
      const undo = el('button', null, 'Undo'); undo.type = 'button';
      undo.onclick = async () => {
        try {
          for (const b of d.bought) await api('POST', '/api/parts/' + b.id + '/adjust', {delta: -b.added, note: 'Undo bought'});
          stockView();
        } catch (e) { msg.replaceChildren(el('div', 'err', e.message)); }
      };
      const ok = el('div', 'box ok');
      ok.append('Added to the truck: ' + d.bought.map(b => b.added + ' ' + b.unit + ' ' + b.name).join(', ') + '. ', undo);
      const keep = ok;
      await stockView();                       // the list redraws without what was bought
      const m = view.querySelector('.msgslot'); if (m) m.replaceChildren(keep);
    } catch (e) { msg.replaceChildren(el('div', 'err', e.message)); }
  };
  msg.className = 'msgslot';
  groups.forEach(g => {
    const w = el('section', 'card');
    const head = el('div', 'row');
    head.append(el('h3', null, g.vendor));
    const all = el('button', null, '✓ All bought'); all.type = 'button';
    head.append(all);
    w.append(head);
    const inputs = [];
    g.items.forEach(it => {
      const r = el('div', 'crow');
      const l = el('div');
      l.append(el('div', 'n', lineText(it)),
               el('div', 'hint', 'have ' + it.on_hand + ' ' + it.unit + ', reorder at ' + it.reorder_at + (it.packs ? ' · ' + it.suggest + ' ' + it.unit : '') +
                                 (it.kind === 'consumable' ? ' · consumable' : '')));
      const q = el('input'); q.inputMode = 'decimal'; q.value = it.packs || it.suggest;
      q.title = it.packs ? (it.pack_unit || 'pack') + 's bought' : it.unit + ' bought';
      const b = el('button', null, '✓'); b.type = 'button'; b.title = 'Bought: add to the truck';
      const body = () => it.packs ? {id: it.id, packs: q.value} : {id: it.id, qty: q.value};
      b.onclick = () => bought(g.vendor, [body()]);
      inputs.push(body);
      const right = el('div', 'row'); right.style.flexWrap = 'nowrap';
      q.style.width = '64px';
      right.append(q, el('span', 'hint', it.packs ? (it.pack_unit || 'pack') + 's' : it.unit), b);
      r.style.gridTemplateColumns = '1fr auto';
      r.append(l, right);
      w.append(r);
    });
    all.onclick = () => bought(g.vendor, inputs.map(f => f()));
    box.append(w);
  });
  return box;
}

// ---- Vendors: one per name (all branches), with what you buy there --------------------------
const vendorHref = name => '#vendor/' + encodeURIComponent(name);
async function vendorsView() {
  const vs = (await api('GET', '/api/vendors')).vendors;
  const list = el('div');
  if (!vs.length) list.append(el('div', 'hint', 'No vendors yet. Set a vendor on a part, or save a stop as a Vendor place.'));
  vs.forEach(v => {
    const a = el('a', 'part'); a.href = vendorHref(v.name);
    const body = el('div', 'body');
    body.append(el('div', 'n', v.name), el('div', 'm', [
      v.locations ? v.locations + ' location' + (v.locations === 1 ? '' : 's') : 'no saved location',
      v.parts + ' part' + (v.parts === 1 ? '' : 's'), v.last_visit ? 'last visit ' + niceDate(v.last_visit) : ''].filter(Boolean).join(' · ')));
    a.append(body, v.low ? el('div', 'qty low', v.low + ' low') : el('div', 'hint', ''));
    list.append(a);
  });
  view.replaceChildren(el('div', 'hint', 'A vendor covers all its branches. Low items show first.'), list);
}

async function vendorPage(name) {
  const d = await api('GET', '/api/vendors/detail?name=' + encodeURIComponent(name));
  const back = el('a', 'back', '‹ All vendors'); back.href = '#vendors';
  const err = el('div', 'err');
  const i = d.info;
  const head = el('div');
  head.append(el('h1', 'page-title', d.name), el('div', 'hint', [d.totals.parts + ' parts', d.locations.length + ' location' + (d.locations.length === 1 ? '' : 's'),
    d.totals.visits ? d.totals.visits + ' visits' : '', d.totals.expenses ? money(d.totals.expenses) + ' in receipts' : ''].filter(Boolean).join(' · ')));

  // contact card
  const info = el('section', 'card');
  info.append(el('h2', null, 'Account and contact'));
  if (i.phone) { const a = el('a', 'btn', '📞 ' + i.phone); a.href = 'tel:' + i.phone.replace(/[^\d+]/g, ''); info.append(a); }
  [['Account #', i.account], ['Contact', i.contact], ['Website', i.website], ['Notes', i.notes]].forEach(([k, v]) => {
    if (v) info.append(el('div', null, k + ': ' + v));
  });
  if (!i.phone && !i.account && !i.contact && !i.website && !i.notes) info.append(el('div', 'hint', 'Add your account number, phone and a contact under Edit.'));

  // low here
  const low = el('section', 'card');
  low.append(el('h2', null, 'Low at the truck (' + d.low.length + ')'));
  if (!d.low.length) low.append(el('div', 'hint', 'Nothing from this vendor is low.'));
  d.low.forEach(it => {
    const r = el('div', 'crow'); r.style.gridTemplateColumns = '1fr auto';
    const amt = it.packs ? it.packs + ' ' + (it.pack_unit || 'pack') + (it.packs > 1 ? 's' : '') + ' of ' + it.pack_size : it.suggest + ' ' + it.unit;
    const l = el('div'); l.append(el('div', 'n', amt + ' ' + it.name), el('div', 'hint', 'have ' + it.on_hand + ', reorder at ' + it.reorder_at));
    const b = el('button', null, '✓ Bought'); b.type = 'button';
    b.onclick = async () => {
      try { await api('POST', '/api/parts/bought', {vendor: d.name, items: [it.packs ? {id: it.id, packs: it.packs} : {id: it.id, qty: it.suggest}]}); route(); }
      catch (e) { err.textContent = e.message; }
    };
    r.append(l, b); low.append(r);
  });

  // parts
  const ps = el('details', 'card');
  ps.append(el('summary', null, 'Parts from this vendor (' + d.parts.length + ')'));
  d.parts.forEach(p => {
    const a = el('a', 'hrow'); a.href = '#part-' + p.id;
    const q = qtyText(p);
    a.append(el('div', null, p.name + (p.sku ? ' · ' + p.sku : '')), el('div', 'm', [money(p.unit_cost), q.t, p.bin ? '📍 ' + p.bin : ''].filter(Boolean).join(' · ')));
    ps.append(a);
  });

  // locations and visits
  const locs = el('section', 'card');
  locs.append(el('h2', null, 'Locations'));
  if (!d.locations.length) locs.append(el('div', 'hint', 'No saved location. On a stop at this vendor, use Tools → Save as Vendor.'));
  d.locations.forEach(l => {
    const a = el('a', 'hrow'); a.href = 'https://maps.apple.com/?daddr=' + encodeURIComponent(l.address || (l.lat + ',' + l.lon));
    a.append(el('div', null, '📍 ' + (l.address || 'No address saved')), el('div', 'm', l.times_confirmed + ' visit' + (l.times_confirmed === 1 ? '' : 's') + ' · directions'));
    locs.append(a);
  });
  const vis = el('details', 'card');
  vis.append(el('summary', null, 'Visits (' + d.totals.visits + ')'));
  d.visits.slice(0, 20).forEach(v => {
    const a = el('a', 'hrow'); a.href = v.url;
    a.append(el('div', null, niceDate(v.date) + ' · ' + v.arrive + '–' + v.depart), el('div', 'm', [v.address, v.summary].filter(Boolean).join(' · ')));
    vis.append(a);
  });
  const buys = el('details', 'card');
  buys.append(el('summary', null, 'Bought from the shopping list (' + d.bought.length + ')'));
  d.bought.forEach(b => buys.append(el('div', 'hrow', whenFull(b.ts) + ' · +' + b.qty + ' ' + b.unit + ' ' + b.name)));

  // edit
  const ed = el('details', 'card');
  ed.append(el('summary', null, 'Edit'));
  const F = {};
  const mk = (k, label, val, ph) => { const x = k === 'notes' ? el('textarea') : el('input'); x.value = val || ''; if (ph) x.placeholder = ph; F[k] = x; return lab(label, x); };
  const ren = el('input'); ren.value = d.name;
  ed.append(lab('Name', ren), mk('account', 'Account #', i.account), mk('phone', 'Phone', i.phone, '555-0100'),
            mk('contact', 'Contact', i.contact, 'counter, rep…'), mk('website', 'Website', i.website), mk('notes', 'Notes', i.notes));
  if (F.phone) F.phone.inputMode = 'tel';
  ed.append(el('div', 'hint', 'Renaming to the name of another vendor merges them (their parts and locations move over).'));
  const save = el('button', 'primary', 'Save'); save.type = 'button';
  save.onclick = async () => {
    err.textContent = '';
    const b = {name: d.name, rename: ren.value.trim()};
    Object.keys(F).forEach(k => { b[k] = F[k].value; });
    try { const r = await api('PUT', '/api/vendors', b); location.hash = vendorHref(r.name); route(); }
    catch (e) { err.textContent = e.message; }
  };
  ed.append(save);
  view.replaceChildren(back, head, err, info, low, ps, locs, vis, buys, ed);
}

// ---- Fuel: fill-ups logged on Gas stops (day page) ------------------------------------------
async function fuelView(range) {
  const today = new Date(), iso = d => d.toLocaleDateString('en-CA');
  const from = {month: iso(new Date(today.getFullYear(), today.getMonth(), 1)),
                d30: iso(new Date(today - 29 * 864e5)), year: iso(new Date(today.getFullYear(), 0, 1)), all: ''}[range];
  const d = await api('GET', '/api/fuel' + (from ? '?from=' + from : ''));
  const box = el('div');
  const pick = el('div', 'row tools');
  [['month', 'This month'], ['d30', 'Last 30 days'], ['year', 'This year'], ['all', 'All']].forEach(([k, t]) => {
    const b = el('button', k === range ? 'on' : null, t); b.type = 'button';
    b.onclick = () => fuelView(k).catch(e => { view.textContent = e.message; });
    pick.append(b);
  });
  const sm = d.summary, m = v => v == null ? '—' : '$' + v.toFixed(2);
  const tiles = el('div', 'tiles');
  [['Spent', m(sm.total)], ['Gallons', sm.gallons ? sm.gallons.toFixed(1) : '—'],
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
  view.replaceChildren(box);
}

// ---- Import / export: the parts CSV ------------------------------------------------------
function dataView() {
  const box = el('div');
  const imp = el('section', 'card');
  imp.append(el('h2', null, 'Import parts from a CSV'));
  const file = el('input'); file.type = 'file'; file.accept = '.csv,text/csv';
  const check = el('button', null, 'Check file'); check.type = 'button';
  const go = el('button', 'primary', 'Import'); go.type = 'button';
  const out = el('div');
  const row = el('div', 'row'); row.append(check, go);
  imp.append(file, row, el('div', 'hint', 'Check file shows what an import would do without changing anything. ' +
    'Matching is by part number, else name. A filled on_hand sets the truck count and turns tracking on.'), out);
  const runCsv = async dry => {
    out.replaceChildren();
    const f = file.files[0];
    if (!f) { out.append(el('div', 'err', 'Choose a CSV file first.')); return; }
    try {
      const fd = new FormData(); fd.append('file', f);
      const r = await api('POST', '/api/parts/import' + (dry ? '?dry=1' : ''), fd, fd);
      out.append(el('div', 'ok', (dry ? 'Check only, nothing saved. Would import: ' : 'Imported: ') +
        r.created + ' new, ' + r.updated + ' updated, ' + r.skipped + ' skipped.'));
      if (r.ignored_columns.length) out.append(el('div', 'hint', 'Ignored columns: ' + r.ignored_columns.join(', ')));
      r.errors.slice(0, 20).forEach(m => out.append(el('div', 'err', m)));
      if (r.errors.length > 20) out.append(el('div', 'hint', '…and ' + (r.errors.length - 20) + ' more.'));
    } catch (e) { out.append(el('div', 'err', e.message)); }
  };
  check.onclick = () => runCsv(true);
  go.onclick = () => runCsv(false);

  const exp = el('section', 'card');
  exp.append(el('h2', null, 'Export'));
  const a1 = el('a', 'btn', 'Download current parts (CSV)'); a1.href = '/api/parts/export.csv'; a1.dataset.file = 'parts CSV';
  const a2 = el('a', 'btn', 'Download blank template'); a2.href = '/api/parts/template.csv'; a2.dataset.file = 'template';
  const r2 = el('div', 'row'); r2.append(a1, a2);
  exp.append(r2);

  const fmt = el('details', 'card');
  fmt.append(el('summary', null, 'CSV columns'));
  const cols = [['name', 'required'], ['sku', 'part number'], ['type', 'part, consumable or expense'],
    ['unit', 'each, pair, pack, box, bag, case, roll, foot, gallon or pound (any other unit rejects the row)'],
    ['unit_cost', ''], ['billable', 'yes / no'], ['category', ''], ['vendor', ''], ['on_hand', 'sets the truck count'],
    ['reorder_at', ''], ['pack_size', 'e.g. 25'], ['pack_unit', 'bag, box…'], ['bin', 'shelf or bin on the truck, e.g. Shelf 2']];
  cols.forEach(([c, h]) => { const d = el('div', 'hint'); d.append(el('code', null, c), h ? ' – ' + h : ''); fmt.append(d); });
  box.append(imp, exp, fmt);
  view.replaceChildren(box);
}

route();
