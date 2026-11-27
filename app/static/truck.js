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

// DELETE and PUT are sent as POST with X-Method-Override: some routers' intrusion prevention blocks a device for minutes
// after a DELETE (seen 2026-11-23). The server turns the header back into the real method.
const wireMethod = m => (m === 'DELETE' || m === 'PUT') ? 'POST' : m;
const overrideHeader = m => (m === 'DELETE' || m === 'PUT') ? {'X-Method-Override': m} : {};
async function api(method, url, body, raw) {
  const r = await fetch(url, {method: wireMethod(method), headers: {...(body && !raw ? {'Content-Type': 'application/json'} : {}), ...overrideHeader(method)},
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

// Matches under the box as you type (the iPhone's own suggestion list is unreliable). Every word must appear in the
// name, part #, shelf or bin.
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

function liveSearch(input, items, onPick, after) {
  const box = el('div', 'live-res'); box.hidden = true;
  input.setAttribute('autocomplete', 'off');
  let touching = false;
  box.addEventListener('pointerdown', () => { touching = true; });
  box.addEventListener('pointercancel', () => { touching = false; });
  const show = () => {
    const words = input.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    box.replaceChildren();
    if (!words.length) { box.hidden = true; return; }
    const q = words.join(' ');
    const ranked = [];
    items().forEach(p => {
      const name = (p.name || '').toLowerCase();
      const hay = [name, p.sku, p.vendor_item, p.qbo_item, p.shelf, p.bin].join(' ').toLowerCase();
      if (words.every(w => hay.includes(w))) ranked.push([name.startsWith(q) ? 0 : words.every(w => name.includes(w)) ? 1 : 2, name, p]);
    });
    ranked.sort((a, b) => a[0] - b[0] || a[1].localeCompare(b[1])).slice(0, 12).forEach(([, , p]) => {
      const b = el('button', 'live-hit'); b.type = 'button';
      b.append(el('span', 'n', p.name), el('span', 'm', [p.sku, p.shelf, p.tracked ? p.on_hand + ' ' + p.unit : ''].filter(Boolean).join(' · ')));
      b.onmousedown = e => e.preventDefault();
      let x0 = 0, y0 = 0, done = false;
      const pick = e => { if (done) return; done = true; if (e) { e.preventDefault(); swallowNextClick(box.getBoundingClientRect()); } touching = false; box.hidden = true; onPick(p); };
      b.addEventListener('pointerdown', e => { x0 = e.clientX; y0 = e.clientY; });
      b.addEventListener('pointerup', e => { if (Math.abs(e.clientX - x0) < 12 && Math.abs(e.clientY - y0) < 12) pick(e); });
      b.onclick = () => pick();
      box.append(b);
    });
    box.hidden = !box.childNodes.length;
  };
  input.addEventListener('input', show);
  input.addEventListener('blur', () => setTimeout(() => { if (!touching) box.hidden = true; }, 400));
  (after || input).after(box);
}

async function loadParts() {
  [PARTS, CH] = await Promise.all([
    api('GET', '/api/parts' + (SHOW_OFF ? '?all=1' : '')).then(r => r.parts),
    api('GET', '/api/parts/choices')]);
}

// where it lives on the truck: "Shelf 2 · bin 12"
// what you make per unit: sell price (unit_cost) minus what you pay (last_cost, "Our cost")
const marginText = p => p.margin_pct == null ? '' : 'margin ' + (p.margin_pct >= 0 ? '' : '−') + Math.abs(p.margin_pct) + '%';
const locText = p => [p.shelf, p.bin ? 'bin ' + p.bin : ''].filter(Boolean).join(' · ');
function shelfList() {           // a datalist of the shelves already used, so they are typed the same way
  let dl = document.getElementById('shelves');
  if (!dl) { dl = el('datalist'); dl.id = 'shelves'; document.body.append(dl); }
  dl.replaceChildren(...[...new Set(PARTS.map(p => p.shelf).filter(Boolean))].sort().map(v => { const o = el('option'); o.value = v; return o; }));
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
  const km = /^kit-(\d+|new)$/.exec(h);
  const tm = /^tool-(\d+|new)$/.exec(h);
  const tab = m ? 'parts' : vm ? 'vendors' : km ? 'kits' : tm ? 'tools' : h;
  document.querySelectorAll('#subnav a').forEach(a => a.classList.toggle('on', a.dataset.v === tab));
  view.replaceChildren(el('div', 'hint', 'Loading…'));
  const show = m ? partPage(+m[1]) : vm ? vendorPage(decodeURIComponent(vm[1])) : km ? kitPage(km[1]) : tm ? toolPage(tm[1])
    : ({parts: partsView, stock: stockView, vendors: vendorsView, kits: kitsView, tools: toolsView, fuel: () => fuelView('month'),
        receipts: receiptsView, data: dataView}[tab] || partsView)();
  Promise.resolve(show).catch(e => { view.replaceChildren(el('div', 'err', e.message)); });
}
window.addEventListener('hashchange', route);

// ---- Parts: search, filter, list ----------------------------------------------------------
let FILTER = '';
async function partsView() {
  await loadParts();
  const bar = el('div', 'bar');
  const q = el('input'); q.placeholder = 'Search name, part #, vendor, shelf…'; q.value = FILTER; q.type = 'search';
  const add = el('button', 'primary', '+ New part'); add.type = 'button';
  add.onclick = () => newPartSheet();
  bar.append(q, add);
  const chips = el('div', 'chips');
  [['', 'All'], ['part', 'Parts'], ['consumable', 'Consumables'], ['expense', 'Expenses'], ['low', 'Low stock'], ['below', 'Below cost']].forEach(([k, t]) => {
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
    const shown = PARTS.filter(p => (!KIND || (KIND === 'low' ? p.low : KIND === 'below' ? p.below_cost : p.kind === KIND)) && (!f || p.name.toLowerCase().includes(f) ||
      p.sku.toLowerCase().includes(f) || p.category.toLowerCase().includes(f) || (p.vendor || '').toLowerCase().includes(f) ||
      locText(p).toLowerCase().includes(f)));
    count.textContent = shown.length + (shown.length !== PARTS.length ? ' of ' + PARTS.length : '') + ' item' + (PARTS.length === 1 ? '' : 's');
    list.replaceChildren();
    if (!shown.length) list.append(el('div', 'hint', PARTS.length ? 'No matches.' : 'No parts yet. Tap + New part, or import a CSV (Import / export).'));
    shown.forEach(p => {
      const a = el('a', 'part'); a.href = '#part-' + p.id;
      const body = el('div', 'body');
      body.append(el('div', 'n', p.name),
        el('div', 'm', [p.active ? '' : 'REMOVED', p.kind !== 'part' ? p.kind : '', p.sku, locText(p) ? '📍 ' + locText(p) : '', p.vendor, money(p.unit_cost),
          marginText(p), p.low ? 'LOW (reorder at ' + p.reorder_at + ')' : ''].filter(Boolean).join(' · ')));
      if (p.below_cost) body.append(el('div', 'err', 'Below cost: you charge ' + money(p.unit_cost) + ', you pay ' + money(p.last_cost)));
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
  money_.append(lab('Counted in (unit)', F.unit = unitSel('ea')), lab('Unit cost (you charge)', inp('unit_cost', '0.00', 'decimal')),
                lab('Our cost (you pay, per unit)', inp('vendor_unit_cost', 'optional', 'decimal')),
                lab('Vendor item #', inp('vendor_item', 'optional')), lab('QuickBooks item', inp('qbo_item', 'optional')), bl);
  const stock = el('fieldset'); stock.append(el('legend', null, 'Truck stock (optional)'));
  shelfList();
  const shelfIn = inp('shelf', 'e.g. Shelf 2'); shelfIn.setAttribute('list', 'shelves');
  stock.append(lab('Shelf', shelfIn), lab('Bin', inp('bin', 'e.g. 12')), lab('On the truck now', inp('on_hand', 'blank = do not track stock', 'decimal')),
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
    ['name', 'sku', 'vendor', 'category', 'unit', 'unit_cost', 'vendor_unit_cost', 'vendor_item', 'qbo_item', 'on_hand', 'reorder_at',
     'pack_size', 'pack_unit', 'shelf', 'bin'].forEach(k => { b[k] = F[k].value; });
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
              el('div', 'hint', [p.active ? '' : 'REMOVED', KINDS.find(k => k[0] === p.kind)[1], p.sku ? 'Part # ' + p.sku : '', locText(p) ? '📍 ' + locText(p) : '',
                                 p.vendor, p.vendor_item ? 'vendor # ' + p.vendor_item : '',
                                 money(p.unit_cost) + (p.unit_cost != null ? ' / ' + p.unit : '')].filter(Boolean).join(' · ')));
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
    if (p.last_cost != null) {
      stockCard.append(el('div', p.below_cost ? 'err' : 'hint', 'Our cost ' + money(p.last_cost) + ' / ' + p.unit +
        (p.unit_cost != null ? ' · you charge ' + money(p.unit_cost) + (p.margin != null ? ' · ' + (p.below_cost ? 'LOSS ' : 'margin ') +
          money(Math.abs(p.margin)) + ' (' + p.margin_pct + '%)' : '') : '')));
    }
    const amt = el('input'); amt.inputMode = 'decimal'; amt.placeholder = 'amount';
    const note = el('input'); note.placeholder = 'note (optional)';
    const paid = el('input'); paid.inputMode = 'decimal'; paid.placeholder = '$ paid in total (optional)';
    const vend = vendorSel(p.vendor);
    const buy = el('div', 'grid'); buy.append(lab('Paid (when adding stock)', paid), lab('Bought from', vend));
    const adj = body => async () => {
      err.textContent = '';
      const b = Object.assign({note: note.value}, body());
      if ((b.delta > 0 || b.packs) && paid.value.trim()) { b.paid = paid.value.trim().replace(/^\$/, ''); b.vendor = vend.value; }
      try { await api('POST', '/api/parts/' + p.id + '/adjust', b); route(); }
      catch (e) { err.textContent = e.message; }
    };
    const row = el('div', 'row');
    const add = el('button', null, '+ Add stock'); add.onclick = adj(() => ({delta: Number(amt.value) || amt.value}));
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
    stockCard.append(row, buy, note);
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
    mk('unit_cost', 'Unit cost (you charge)', p.unit_cost, 'decimal'), mk('vendor_unit_cost', 'Our cost (you pay)', p.last_cost, 'decimal'),
    pick('vendor', 'Vendor', vendorSel(p.vendor)), mk('vendor_item', 'Vendor item #', p.vendor_item), mk('qbo_item', 'QuickBooks item', p.qbo_item),
    mk('shelf', 'Shelf', p.shelf), mk('bin', 'Bin', p.bin), pick('category', 'Category', categorySel(p.category)), mk('reorder_at', 'Reorder at', p.reorder_at, 'decimal'),
    mk('pack_size', 'Comes in packs of', p.pack_size, 'decimal'), pick('pack_unit', 'Pack is a', packUnitSel(p.pack_unit)));
  shelfList(); F.shelf.setAttribute('list', 'shelves');
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
  if (!d.totals.stops) {          // never on a stop: it can go for good (typos, test parts, duplicates)
    const purge = el('button', 'danger', 'Delete permanently');
    purge.onclick = async () => {
      if (!confirm('Delete “' + p.name + '” for good? Its stock changes and purchase lines are deleted too. This cannot be undone.')) return;
      try { await api('DELETE', '/api/parts/' + p.id); location.hash = 'parts'; route(); } catch (e) { err.textContent = e.message; }
    };
    acts.append(purge);
  }
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
  [['count', 'Count the truck'], ['shop', 'Shopping list'], ['buys', 'Purchases']].forEach(([k, t]) => {
    const b = el('button', STOCK === k ? 'on' : null, t); b.type = 'button';
    b.onclick = () => { STOCK = k; stockView().catch(e => { out.textContent = e.message; }); };
    tabs.append(b);
  });
  view.replaceChildren(tabs, out);
  out.append(await (STOCK === 'count' ? countBox() : STOCK === 'buys' ? purchasesBox() : shoppingBox()));
}

// Count the truck: shelf by shelf, bin by bin. Buttons along the top show one shelf at a time; what you type on
// one shelf is kept while you move to the next, and Save count saves every line you changed.
let COUNT_SHELF = null;           // null = all shelves
async function countBox() {
  const rows = (await api('GET', '/api/parts/count-sheet')).parts;
  const box = el('div', 'box');
  box.append(el('div', 'hint', 'Enter what is really on the truck. Only the lines you change are saved. “Used” is what stops logged since the last count.'));
  if (!rows.length) { box.append(el('div', 'hint', 'No tracked items yet. Give items an “on truck” quantity or turn on tracking first.')); return box; }
  const shelves = [...new Set(rows.map(p => p.shelf))];
  if (COUNT_SHELF !== null && !shelves.includes(COUNT_SHELF)) COUNT_SHELF = null;
  const chips = el('div', 'chips');
  const sections = [];
  const pick = v => {
    COUNT_SHELF = v;
    chips.querySelectorAll('button').forEach(b => b.classList.toggle('on', b._v === v));
    sections.forEach(([sh, sec]) => { sec.hidden = v !== null && sh !== v; });
  };
  const chip = (v, t) => { const b = el('button', null, t); b.type = 'button'; b._v = v; b.onclick = () => pick(v); chips.append(b); };
  chip(null, 'All shelves');
  shelves.forEach(sh => chip(sh, sh || 'No shelf'));
  box.append(chips);
  const inputs = {};
  shelves.forEach(sh => {
    const sec = el('div');
    const mine = rows.filter(p => p.shelf === sh);
    sec.append(el('h3', null, (sh ? '📍 ' + sh : 'No shelf set') + ' (' + mine.length + ')'));
    mine.forEach(p => {
      const r = el('div', 'crow');
      const l = el('div');
      l.append(el('div', 'n', (p.bin ? 'Bin ' + p.bin + ' · ' : '') + p.name),
        el('div', 'hint', ['expected ' + p.on_hand + ' ' + p.unit, 'used ' + p.used_since, 'counted ' + when(p.last_counted)].join(' · ')));
      const i = el('input'); i.inputMode = 'decimal'; i.placeholder = String(p.on_hand); inputs[p.id] = i;
      r.append(l, i); sec.append(r);
    });
    sections.push([sh, sec]);
    box.append(sec);
  });
  pick(COUNT_SHELF);
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
                                   : it.suggest + ' ' + it.unit) + ' ' + it.name +
                       (it.vendor_item ? ' (vendor #' + it.vendor_item + ')' : it.sku ? ' (#' + it.sku + ')' : '');
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
      const paid = el('input'); paid.inputMode = 'decimal'; paid.placeholder = '$ paid'; paid.title = 'What you paid for this line (optional)';
      paid.style.width = '72px';
      const b = el('button', null, '✓'); b.type = 'button'; b.title = 'Bought: add to the truck';
      const body = () => Object.assign(it.packs ? {id: it.id, packs: q.value} : {id: it.id, qty: q.value},
                                       paid.value.trim() ? {paid: paid.value.trim().replace(/^\$/, '')} : {});
      b.onclick = () => bought(g.vendor, [body()]);
      inputs.push(body);
      const right = el('div', 'row'); right.style.flexWrap = 'nowrap';
      q.style.width = '64px';
      right.append(q, el('span', 'hint', it.packs ? (it.pack_unit || 'pack') + 's' : it.unit), paid, b);
      r.style.gridTemplateColumns = '1fr auto';
      r.append(l, right);
      w.append(r);
    });
    all.onclick = () => bought(g.vendor, inputs.map(f => f()));
    box.append(w);
  });
  return box;
}

// Purchases: what you paid for stock, by vendor and by type, for a date range.
let BUYS = 'month';
async function purchasesBox() {
  const today = new Date(), iso = d => d.toLocaleDateString('en-CA');
  const from = {month: iso(new Date(today.getFullYear(), today.getMonth(), 1)), d30: iso(new Date(today - 29 * 864e5)),
                year: iso(new Date(today.getFullYear(), 0, 1)), all: ''}[BUYS];
  const d = await api('GET', '/api/purchases' + (from ? '?from=' + from : ''));
  const box = el('div');
  const pick = el('div', 'chips');
  [['month', 'This month'], ['d30', 'Last 30 days'], ['year', 'This year'], ['all', 'All']].forEach(([k, t]) => {
    const b = el('button', k === BUYS ? 'on' : null, t); b.type = 'button';
    b.onclick = () => { BUYS = k; stockView(); };
    pick.append(b);
  });
  const tiles = el('div', 'tiles');
  [['Spent', money(d.total) || '$0.00'], ['Parts', money(d.by_kind.part || 0)], ['Consumables', money(d.by_kind.consumable || 0)]].forEach(([k, v]) => {
    const t = el('div', 'tile'); t.append(el('b', null, v), el('span', 'hint', k)); tiles.append(t);
  });
  box.append(pick, tiles);
  if (d.unpriced) box.append(el('div', 'hint', d.unpriced + ' purchase' + (d.unpriced === 1 ? '' : 's') + ' without a price (not in the totals).'));
  if (d.by_vendor.length) {
    const bv = el('section', 'card'); bv.append(el('h2', null, 'By vendor'));
    d.by_vendor.forEach(v => { const a = el('a', 'hrow'); a.href = vendorHref(v.vendor); a.append(v.vendor + ' · ' + money(v.total)); bv.append(a); });
    box.append(bv);
  }
  const list = el('section', 'card'); list.append(el('h2', null, 'Purchases (' + d.purchases.length + ')'));
  if (!d.purchases.length) list.append(el('div', 'hint', 'Nothing bought in this period. Mark items bought on the Shopping list, or add stock with a price on a part.'));
  d.purchases.forEach(x => {
    const a = el('a', 'hrow'); a.href = '#part-' + x.part_id;
    a.append(el('div', null, whenFull(x.ts) + ' · +' + x.qty + ' ' + x.unit + ' ' + x.name),
             el('div', 'm', [x.vendor, x.total != null ? money(x.total) + (x.each != null ? ' (' + money(x.each) + ' / ' + x.unit + ')' : '') : 'no price',
                             x.kind === 'consumable' ? 'consumable' : ''].filter(Boolean).join(' · ')));
    list.append(a);
  });
  box.append(list);
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
  const d = await api('POST', '/api/vendors/detail', {name});
  const back = el('a', 'back', '‹ All vendors'); back.href = '#vendors';
  const err = el('div', 'err');
  const i = d.info;
  const head = el('div');
  head.append(el('h1', 'page-title', d.name), el('div', 'hint', [d.totals.parts + ' parts', d.locations.length + ' location' + (d.locations.length === 1 ? '' : 's'),
    d.totals.visits ? d.totals.visits + ' visits' : '', d.totals.purchased ? money(d.totals.purchased) + ' bought' : '',
    d.totals.expenses ? money(d.totals.expenses) + ' in receipts' : ''].filter(Boolean).join(' · ')));

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
    a.append(el('div', null, p.name + (p.sku ? ' · ' + p.sku : '')), el('div', 'm', [money(p.unit_cost), q.t, locText(p) ? '📍 ' + locText(p) : ''].filter(Boolean).join(' · ')));
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
  buys.append(el('summary', null, 'Purchases (' + d.purchases.length + ')' + (d.totals.purchased ? ' · ' + money(d.totals.purchased) : '')));
  d.purchases.forEach(x => {
    const a = el('a', 'hrow'); a.href = '#part-' + x.part_id;
    a.append(el('div', null, whenFull(x.ts) + ' · +' + x.qty + ' ' + x.unit + ' ' + x.name),
             el('div', 'm', x.total != null ? money(x.total) + (x.each != null ? ' (' + money(x.each) + ' / ' + x.unit + ')' : '') : 'no price'));
    buys.append(a);
  });

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
  const tl = el('details', 'card');
  tl.append(el('summary', null, 'Tools bought here (' + d.tools.length + ')'));
  d.tools.forEach(t => { const a = el('a', 'hrow'); a.href = '#tool-' + t.id; a.append(t.name + (t.purchased ? ' · ' + t.purchased : '') + (t.price != null ? ' · ' + money(t.price) : '')); tl.append(a); });
  view.replaceChildren(back, head, err, info, low, ps, locs, vis, buys, ...(d.tools.length ? [tl] : []), ed);
}

// ---- Kits: a named set of parts added to a stop in one tap -----------------------------------
async function kitsView() {
  const ks = (await api('GET', '/api/kits')).kits;
  const add = el('a', 'btn primary', '+ New kit'); add.href = '#kit-new';
  const list = el('div');
  if (!ks.length) list.append(el('div', 'hint', 'No kits yet. A kit is a set of parts you often use together (a water heater install, a ' +
    'faucet swap). On a stop, “Add a kit” puts every part on it in one tap.'));
  ks.forEach(k => {
    const a = el('a', 'part'); a.href = '#kit-' + k.id;
    const body = el('div', 'body');
    body.append(el('div', 'n', k.name), el('div', 'm', [k.items.length + ' item' + (k.items.length === 1 ? '' : 's'),
      k.price != null ? money(k.price) + ' in parts' : '', k.notes].filter(Boolean).join(' · ')));
    a.append(body, k.short.length ? el('div', 'qty low', 'short') : el('div', 'hint', ''));
    list.append(a);
  });
  view.replaceChildren(el('div', 'row'), list);
  view.firstChild.append(add);
}

async function kitPage(id) {
  await loadParts();
  const k = id === 'new' ? {name: '', notes: '', items: [], short: []} : (await api('GET', '/api/kits')).kits.find(x => x.id === +id);
  if (!k) { view.replaceChildren(el('div', 'err', 'That kit no longer exists.')); return; }
  const back = el('a', 'back', '‹ All kits'); back.href = '#kits';
  const err = el('div', 'err');
  const name = el('input'); name.value = k.name; name.placeholder = 'e.g. Water heater install';
  const notes = el('input'); notes.value = k.notes; notes.placeholder = 'optional';
  const rows = el('div');
  const lines = [];
  const addLine = it => {
    const r = el('div', 'crow'); r.style.gridTemplateColumns = '1fr 80px auto';
    const n = el('input'); n.value = it ? it.name : ''; n.placeholder = 'Start typing a part name or #';
    const q = el('input'); q.inputMode = 'decimal'; q.value = it ? it.qty : '1';
    const x = el('button', 'danger', '✕'); x.type = 'button';
    const line = {n, q, r};
    x.onclick = () => { r.remove(); lines.splice(lines.indexOf(line), 1); };
    r.append(n, q, x); rows.append(r); lines.push(line);
    liveSearch(n, () => PARTS.filter(p => p.kind !== 'expense'), p => { n.value = p.name; q.focus(); }, r);
    if (it && it.short) r.append(el('div', 'hint low', 'only ' + it.on_hand + ' ' + it.unit + ' on the truck'));
  };
  k.items.forEach(addLine);
  if (!k.items.length) { addLine(); addLine(); }
  const more = el('button', null, '+ Another part'); more.type = 'button'; more.onclick = () => addLine();
  const save = el('button', 'primary', id === 'new' ? 'Create kit' : 'Save kit'); save.type = 'button';
  save.onclick = async () => {
    err.textContent = '';
    const items = [];
    for (const l of lines) {
      const v = l.n.value.trim();
      if (!v) continue;
      const p = PARTS.find(x => x.name.toLowerCase() === v.toLowerCase() || (x.sku && x.sku.toLowerCase() === v.toLowerCase()));
      if (!p) { err.textContent = '“' + v + '” is not in your parts list. Add it under Parts first.'; return; }
      items.push({part_id: p.id, qty: l.q.value});
    }
    try {
      const r = await api(id === 'new' ? 'POST' : 'PUT', '/api/kits' + (id === 'new' ? '' : '/' + id), {name: name.value, notes: notes.value, items});
      location.hash = 'kit-' + r.id;
      route();
    } catch (e) { err.textContent = e.message; }
  };
  const acts = el('div', 'row'); acts.append(save);
  if (id !== 'new') {
    const del = el('button', 'danger', 'Delete kit'); del.type = 'button';
    del.onclick = async () => {
      if (!confirm('Delete the kit “' + k.name + '”? Stops that used it keep their parts.')) return;
      try { await api('DELETE', '/api/kits/' + id); location.hash = 'kits'; } catch (e) { err.textContent = e.message; }
    };
    acts.append(del);
  }
  const card = el('section', 'card');
  card.append(lab('Kit name', name), lab('Notes', notes), el('label', null, 'Parts (quantity per kit)'), rows, more);
  if (k.price != null) card.append(el('div', 'hint', 'Parts in this kit come to ' + money(k.price) + ' at your unit costs.'));
  view.replaceChildren(back, el('h1', 'page-title', id === 'new' ? 'New kit' : k.name), err, card, acts);
}

// ---- Tools: what you own and what you paid for it, with the receipt ------------------------
let TOOL_WHERE = 'owned', TOOL_Q = '';
async function toolsView() {
  const d = await api('GET', '/api/tools');
  const t = d.totals;
  const tiles = el('div', 'tiles');
  [['Tools', String(t.count)], ['Value', money(t.value) || '$0.00'], ['Bought in ' + t.year, money(t.this_year) || '$0.00']].forEach(([k, v]) => {
    const x = el('div', 'tile'); x.append(el('b', null, v), el('span', 'hint', k)); tiles.append(x);
  });
  const bar = el('div', 'bar');
  const q = el('input'); q.type = 'search'; q.placeholder = 'Search name, make, model, serial…'; q.value = TOOL_Q;
  const add = el('a', 'btn primary', '+ New tool'); add.href = '#tool-new';
  bar.append(q, add);
  const chips = el('div', 'chips');
  [['owned', 'In use'], ['truck', 'On truck'], ['shop', 'Shop'], ['repair', 'Repair'], ['gone', 'Lost / retired'], ['all', 'All']].forEach(([k, l]) => {
    const b = el('button', TOOL_WHERE === k ? 'on' : null, l); b.type = 'button';
    b.onclick = () => { TOOL_WHERE = k; chips.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b)); draw(); };
    chips.append(b);
  });
  const list = el('div');
  const hint = el('div', 'hint');
  function draw() {
    const words = q.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const shown = d.tools.filter(x => {
      const where = TOOL_WHERE === 'all' || (TOOL_WHERE === 'owned' ? !['lost', 'retired'].includes(x.location)
        : TOOL_WHERE === 'gone' ? ['lost', 'retired'].includes(x.location) : x.location === TOOL_WHERE);
      const hay = [x.name, x.make, x.model, x.serial, x.category, x.vendor].join(' ').toLowerCase();
      return where && words.every(w => hay.includes(w));
    });
    list.replaceChildren();
    if (!d.tools.length) list.append(el('div', 'hint', 'No tools yet. Tap + New tool, or import a spreadsheet under Import / export.'));
    shown.forEach(x => {
      const a = el('a', 'part'); a.href = '#tool-' + x.id;
      const body = el('div', 'body');
      body.append(el('div', 'n', x.name + (x.has_receipt ? ' 🧾' : '')),
                  el('div', 'm', [[x.make, x.model].filter(Boolean).join(' '), x.serial ? 'S/N ' + x.serial : '',
                                  x.purchased, x.location !== 'truck' ? x.location_label : ''].filter(Boolean).join(' · ')));
      a.append(body, el('div', 'qty', x.price != null ? money(x.price) : ''));
      list.append(a);
    });
    hint.textContent = t.no_receipt ? t.no_receipt + ' tool' + (t.no_receipt === 1 ? ' has' : 's have') + ' no receipt yet (no 🧾).' : '';
  }
  q.oninput = () => { TOOL_Q = q.value; draw(); };
  view.replaceChildren(tiles, bar, chips, hint, list);
  draw();
}

function shrinkImage(file, side = 1600) {      // the phone shrinks the picture before upload (quick on weak signal)
  return new Promise(resolve => {
    const url = URL.createObjectURL(file), img = new Image();
    img.onload = () => {
      const k = Math.min(1, side / Math.max(img.naturalWidth, img.naturalHeight));
      const cv = document.createElement('canvas');
      cv.width = Math.round(img.naturalWidth * k); cv.height = Math.round(img.naturalHeight * k);
      cv.getContext('2d').drawImage(img, 0, 0, cv.width, cv.height);
      URL.revokeObjectURL(url);
      cv.toBlob(b => resolve(b || file), 'image/jpeg', 0.82);
    };
    img.onerror = () => { URL.revokeObjectURL(url); resolve(file); };
    img.src = url;
  });
}
function viewImage(src) {
  const ov = el('div', 'viewer');
  const close = el('button', 'primary', 'Close'); close.type = 'button'; close.onclick = () => ov.remove();
  const img = el('img'); img.src = src; img.alt = 'Picture';
  ov.append(close, img);
  document.body.append(ov);
}

async function toolPage(id) {
  const all = await api('GET', '/api/tools');
  const isNew = id === 'new';
  const x = isNew ? {name: '', make: '', model: '', serial: '', category: '', purchased: '', price: null, vendor: '',
                     warranty_until: '', location: 'truck', notes: '', files: []} : all.tools.find(t => t.id === +id);
  if (!x) { view.replaceChildren(el('div', 'err', 'That tool no longer exists.')); return; }
  await loadParts();                                  // vendor choices
  const back = el('a', 'back', '‹ All tools'); back.href = '#tools';
  const err = el('div', 'err');
  const F = {};
  const inp = (k, label, val, opt) => {
    const i = el(opt && opt.area ? 'textarea' : 'input'); i.value = val == null ? '' : val;
    if (opt && opt.type) i.type = opt.type;
    if (opt && opt.mode) i.inputMode = opt.mode;
    if (opt && opt.ph) i.placeholder = opt.ph;
    F[k] = i; return lab(label, i);
  };
  const where = el('select');
  all.locations.forEach(l => { const o = el('option', null, l.label); o.value = l.value; where.append(o); });
  where.value = x.location; F.location = where;
  let dl = document.getElementById('toolcats');
  if (!dl) { dl = el('datalist'); dl.id = 'toolcats'; document.body.append(dl); }
  dl.replaceChildren(...all.categories.map(c => { const o = el('option'); o.value = c; return o; }));
  const grid = el('div', 'grid');
  grid.append(inp('name', 'Name', x.name, {ph: 'e.g. Cordless drill'}), inp('make', 'Make', x.make, {ph: 'DeWalt'}),
              inp('model', 'Model', x.model), inp('serial', 'Serial number', x.serial),
              inp('category', 'Category', x.category, {ph: 'Power tools'}), lab('Where', where));
  F.category.setAttribute('list', 'toolcats');
  const buy = el('div', 'grid');
  F.vendor = vendorSel(x.vendor);
  buy.append(inp('purchased', 'Bought on', x.purchased, {type: 'date'}), inp('price', 'Price paid', x.price, {mode: 'decimal', ph: '0.00'}),
             lab('Bought from', F.vendor), inp('warranty_until', 'Warranty until', x.warranty_until, {type: 'date'}));
  const notes = inp('notes', 'Notes', x.notes, {area: true});
  const save = el('button', 'primary', isNew ? 'Add tool' : 'Save'); save.type = 'button';
  save.onclick = async () => {
    err.textContent = '';
    const b = {};
    Object.keys(F).forEach(k => { b[k] = F[k].value; });
    try {
      const r = await api(isNew ? 'POST' : 'PATCH', '/api/tools' + (isNew ? '' : '/' + id), b);
      if (isNew) location.hash = 'tool-' + r.id; else route();
    } catch (e) { err.textContent = e.message; }
  };
  const acts = el('div', 'row'); acts.append(save);
  if (!isNew) {
    const del = el('button', 'danger', 'Delete'); del.type = 'button';
    del.onclick = async () => {
      if (!confirm('Delete “' + x.name + '” and its pictures? To keep the record, set Where to Lost or Retired instead.')) return;
      try { await api('DELETE', '/api/tools/' + id); location.hash = 'tools'; } catch (e) { err.textContent = e.message; }
    };
    acts.append(del);
  }
  const card = el('section', 'card'); card.append(el('h2', null, 'The tool'), grid);
  const card2 = el('section', 'card'); card2.append(el('h2', null, 'Purchase'), buy, notes);
  if (x.under_warranty) card2.append(el('div', 'ok', 'Under warranty until ' + x.warranty_until + '.'));
  const parts_ = [back, el('h1', 'page-title', isNew ? 'New tool' : x.name), err, card, card2];
  if (!isNew) {
    const files = el('section', 'card');
    files.append(el('h2', null, 'Receipt and photos'));
    const th = el('div', 'thumbs');
    x.files.forEach(f => {
      const w = el('div', 'th');
      const img = el('img'); img.src = '/api/tool-files/' + f.id + '.jpg'; img.alt = f.kind; img.loading = 'lazy';
      img.onclick = () => viewImage(img.src);
      const rm = el('button', 'danger x', '✕'); rm.type = 'button'; rm.title = 'Remove';
      rm.onclick = async () => { if (!confirm('Remove this picture?')) return; try { await api('DELETE', '/api/tool-files/' + f.id); route(); } catch (e) { err.textContent = e.message; } };
      w.append(img, el('span', 'k', f.kind === 'receipt' ? 'Receipt' : 'Photo'), rm);
      th.append(w);
    });
    const up = kind => {
      const l = el('label', 'btn'); const i = el('input'); i.type = 'file'; i.accept = 'image/*'; i.hidden = true;
      l.append(kind === 'receipt' ? '+ Receipt' : '+ Photo', i);
      i.onchange = async () => {
        const f = i.files[0]; if (!f) return;
        l.firstChild.textContent = 'Uploading…';
        const fd = new FormData(); fd.append('file', await shrinkImage(f), 'picture.jpg'); fd.append('kind', kind);
        try { await api('POST', '/api/tools/' + id + '/files', fd, fd); route(); }
        catch (e) { err.textContent = e.message; l.firstChild.textContent = kind === 'receipt' ? '+ Receipt' : '+ Photo'; }
      };
      return l;
    };
    const row = el('div', 'row'); row.append(up('receipt'), up('photo'));
    // what the receipt reader found on the newest receipt: offer it, never fill it in by itself
    const rf = x.files.filter(f => f.kind === 'receipt' && f.read && !f.read.dismissed).pop();
    const sug = el('div');
    if (rf && !rf.read.ok) {
      const again = el('button', null, 'Try again'); again.type = 'button';
      again.onclick = async () => { again.textContent = 'Reading…'; try { await api('POST', '/api/tool-files/' + rf.id + '/read'); route(); } catch (e) { err.textContent = e.message; } };
      sug.append(el('div', 'hint', 'Could not read the receipt: ' + rf.read.error), again);
    } else if (rf) {
      const r = rf.read;
      const price = r.items && r.items.length === 1 && r.items[0].amount != null ? r.items[0].amount : r.total;
      const bits = [price != null ? money(price) : '', r.date, r.store].filter(Boolean);
      const same = (price == null || price === x.price) && (!r.date || r.date === x.purchased) && (!r.store || r.store === x.vendor);
      if (bits.length && !same) {
        const box = el('div', 'box');
        box.append(el('div', null, '🧾 Receipt says: ' + bits.join(' · ')));
        if (r.items && r.items.length > 1) box.append(el('div', 'hint', r.items.length + ' items on it; the price used is the receipt total. Change it if the tool was only part of it.'));
        const use = el('button', 'primary', 'Use these'); use.type = 'button';
        use.onclick = async () => {
          const b = {};
          if (price != null) b.price = price;
          if (r.date) b.purchased = r.date;
          if (r.store) b.vendor = r.store;
          try { await api('PATCH', '/api/tools/' + id, b); route(); } catch (e) { err.textContent = e.message; }
        };
        const ign = el('button', null, 'Ignore'); ign.type = 'button';
        ign.onclick = async () => { try { await api('POST', '/api/tool-files/' + rf.id + '/dismiss-read'); route(); } catch (e) { err.textContent = e.message; } };
        const rr = el('div', 'row'); rr.append(use, ign);
        box.append(rr);
        sug.append(box);
      }
    }
    files.append(th, sug, row, el('div', 'hint', 'Pictures are shrunk and their location data removed. They are in the full backup.'));
    parts_.push(files);
  }
  parts_.push(acts);
  view.replaceChildren(...parts_);
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
    'Matching is by part number, else name. A filled on_hand sets the truck count and turns tracking on. ' +
    'A QuickBooks “Products and services” export also works as it is: SKU becomes the part number, Sales price the unit ' +
    'cost, Purchase cost the last paid; QuickBooks stock and Service items are not imported.'), out);
  const runCsv = async dry => {
    out.replaceChildren();
    const f = file.files[0];
    if (!f) { out.append(el('div', 'err', 'Choose a CSV file first.')); return; }
    try {
      const fd = new FormData(); fd.append('file', f);
      const r = await api('POST', '/api/parts/import' + (dry ? '?dry=1' : ''), fd, fd);
      out.append(el('div', 'ok', (dry ? 'Check only, nothing saved. Would import: ' : 'Imported: ') +
        (r.format ? r.format + ': ' : '') + r.created + ' new, ' + r.updated + ' updated, ' + r.skipped + ' skipped' +
        (r.services_skipped ? ', ' + r.services_skipped + ' service item' + (r.services_skipped === 1 ? '' : 's') + ' left out' : '') + '.'));
      if (r.ignored_columns.length) out.append(el('div', 'hint', 'Ignored columns: ' + r.ignored_columns.join(', ')));
      (r.notes || []).forEach(n => out.append(el('div', 'hint', n)));
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
    ['unit_cost', 'the price you CHARGE the customer (the price in reports)'], ['billable', 'yes / no'], ['category', ''], ['vendor', ''], ['on_hand', 'sets the truck count'],
    ['reorder_at', ''], ['pack_size', 'e.g. 25'], ['pack_unit', 'bag, box…'], ['shelf', 'where on the truck, e.g. Shelf 2 or Door'], ['bin', 'bin or drawer on that shelf, e.g. 12'],
    ['vendor_item', 'the supplier’s part number (also accepted: dlv_item, vendor_sku)'],
    ['vendor_unit_cost', 'what you PAY per unit (per ft, each); also accepted: cost, our_cost, purchase_cost, dlv_unit_cost'],
    ['qbo_item', 'the QuickBooks product/service name']];
  cols.forEach(([c, h]) => { const d = el('div', 'hint'); d.append(el('code', null, c), h ? ' – ' + h : ''); fmt.append(d); });
  // tools: same idea, its own file
  const timp = el('section', 'card');
  timp.append(el('h2', null, 'Tools (CSV)'));
  const tfile = el('input'); tfile.type = 'file'; tfile.accept = '.csv,text/csv';
  const tcheck = el('button', null, 'Check file'); tcheck.type = 'button';
  const tgo = el('button', 'primary', 'Import tools'); tgo.type = 'button';
  const tout = el('div');
  const trun = async dry => {
    tout.replaceChildren();
    const f = tfile.files[0];
    if (!f) { tout.append(el('div', 'err', 'Choose a CSV file first.')); return; }
    try {
      const fd = new FormData(); fd.append('file', f);
      const r = await api('POST', '/api/tools/import' + (dry ? '?dry=1' : ''), fd, fd);
      tout.append(el('div', 'ok', (dry ? 'Check only, nothing saved. Would import: ' : 'Imported: ') +
        r.created + ' new, ' + r.updated + ' updated, ' + r.skipped + ' skipped.'));
      if (r.ignored_columns.length) tout.append(el('div', 'hint', 'Ignored columns: ' + r.ignored_columns.join(', ')));
      r.errors.slice(0, 20).forEach(m => tout.append(el('div', 'err', m)));
    } catch (e) { tout.append(el('div', 'err', e.message)); }
  };
  tcheck.onclick = () => trun(true); tgo.onclick = () => trun(false);
  const t1 = el('a', 'btn', 'Download tools list (CSV)'); t1.href = '/api/tools/export.csv'; t1.dataset.file = 'tools CSV';
  const t2 = el('a', 'btn', 'Tools template'); t2.href = '/api/tools/template.csv'; t2.dataset.file = 'tools template';
  const trow = el('div', 'row'); trow.append(tcheck, tgo);
  const trow2 = el('div', 'row'); trow2.append(t1, t2);
  timp.append(tfile, trow, tout, trow2, el('div', 'hint', 'Columns: name (required), make, model, serial, category, purchased ' +
    '(2026-03-14 or 3/14/2026), price, vendor, warranty_until, location (truck, shop, repair, lost, retired), notes. ' +
    'Rows match an existing tool by serial number, else by name and model. The list is what an accountant or insurer needs.'));
  box.append(imp, exp, fmt, timp);
  view.replaceChildren(box);
}

route();

// ---- Receipts: add past gas and vendor stops from receipt photos (#receipts) ------------------------------
// Each photo is read once by Claude (store, date, time, total, items). Nothing is added until "Add to JobHub";
// each store item is handled its own way: truck stock, used on a job, expense, or skip.
const RECEIPT_ACTIONS = [['stock', 'Truck stock'], ['job', 'Used on a job'], ['expense', 'Expense'], ['skip', 'Skip']];
const jobsCache = {};
async function jobsFor(date) {
  if (!jobsCache[date]) jobsCache[date] = api('GET', '/api/receipts/jobs?date=' + encodeURIComponent(date || '')).then(r => r.jobs);
  return jobsCache[date];
}

async function receiptsView() {
  const [d] = await Promise.all([api('GET', '/api/receipts/inbox'), PARTS.length ? null : loadParts()]);
  const wrap = el('div');
  wrap.append(el('div', 'hint', 'Photos of past gas and store receipts become stops on their day, with the receipt attached. ' +
    'Each photo is read by Claude (about 2–3¢). Nothing is added until you check it and tap Add to JobHub.'));
  if (!d.ready) {
    const a = el('a', null, 'Settings → AI'); a.href = '/settings';
    const m = el('div', 'err'); m.append('Reading receipts needs the Anthropic API key: add it in ', a, '.');
    wrap.append(m); view.replaceChildren(wrap); return;
  }
  const file = el('input'); file.type = 'file'; file.accept = 'image/*'; file.multiple = true; file.hidden = true;
  const pickBtn = el('button', 'primary', '+ Add receipt photos'); pickBtn.type = 'button'; pickBtn.onclick = () => file.click();
  const prog = el('div', 'hint');
  file.onchange = async () => {
    const files = [...file.files];
    if (!files.length) return;
    pickBtn.disabled = true;
    const fails = [];
    for (let i = 0; i < files.length; i++) {
      prog.textContent = 'Reading ' + (i + 1) + ' of ' + files.length + '…';
      try {
        const fd = new FormData(); fd.append('file', await shrinkImage(files[i]), 'receipt.jpg');
        await api('POST', '/api/receipts/inbox', fd, fd);
      } catch (e) { fails.push((files[i].name || 'photo ' + (i + 1)) + ': ' + e.message); }
    }
    pickBtn.disabled = false;
    await receiptsView();
    if (fails.length) view.prepend(el('div', 'err', 'Not added: ' + fails.join('; ')));
  };
  wrap.append(el('div', 'row'), file, pickBtn, prog);
  const pending = d.items.filter(x => x.status === 'new'), rest = d.items.filter(x => x.status !== 'new');
  wrap.append(el('h3', null, pending.length ? pending.length + ' to check' : 'Nothing to check'));
  pending.forEach(it => wrap.append(receiptCard(it)));
  if (rest.length) {
    const det = el('details', 'card');
    det.append(el('summary', null, 'Done and skipped (' + rest.length + ')'));
    rest.forEach(x => {
      const r = x.read || {}, a = x.applied;
      const line = el(a ? 'a' : 'div', 'hrow');
      if (a) line.href = '/day/' + a.date + '?stop=' + a.stop_id;
      line.append(el('div', null, (x.status === 'done' ? '✓ ' : 'Skipped: ') + (r.store || 'Receipt') + (r.date ? ' · ' + niceDate(r.date) : '') +
                                 (r.total != null ? ' · ' + money(r.total) : '')),
                  el('div', 'm', a ? (a.type === 'gas' ? 'Gas stop' : 'Vendor stop') + ' on ' + niceDate(a.date) +
                                     (a.items.length ? ' · ' + a.items.length + ' item' + (a.items.length === 1 ? '' : 's') : '') : ''));
      det.append(line);
    });
    wrap.append(det);
  }
  view.replaceChildren(wrap);
}

function receiptCard(it) {
  const card = el('section', 'card rcard');
  const thumb = el('img', 'rthumb'); thumb.src = it.image; thumb.alt = 'Receipt'; thumb.onclick = () => viewImage(it.image);
  const err = el('div', 'err');
  const del = el('button', 'danger', 'Delete'); del.type = 'button';
  del.onclick = async () => { if (!confirm('Delete this receipt photo from the list?')) return;
    try { await api('DELETE', '/api/receipts/inbox/' + it.id); card.remove(); } catch (e) { err.textContent = e.message; } };
  if (it.error || !it.proposal) {
    const again = el('button', null, 'Read again'); again.type = 'button';
    again.onclick = async () => { err.textContent = 'Reading…';
      try { const r = await api('POST', '/api/receipts/inbox/' + it.id + '/read'); card.replaceWith(receiptCard(r.item)); }
      catch (e) { err.textContent = e.message; } };
    const acts = el('div', 'row'); acts.append(again, del);
    card.append(thumb, el('div', 'err', it.error || 'Could not read this receipt.'), acts, err);
    return card;
  }
  const p = it.proposal, rd = it.read;
  let type = p.type;
  const inp = (v, ph, mode) => { const i = el('input'); i.value = v == null ? '' : v; if (ph) i.placeholder = ph; if (mode) i.inputMode = mode; return i; };
  const seg = el('div', 'seg');
  const gasB = el('button', null, '⛽ Gas'), venB = el('button', null, '🏪 Vendor');
  [gasB, venB].forEach(b => { b.type = 'button'; seg.append(b); });
  const store = inp(p.store, 'Store or station');
  const date = el('input'); date.type = 'date'; date.value = p.date;
  const tm = el('input'); tm.type = 'time'; tm.value = p.time;
  const arr = el('input'); arr.type = 'time'; arr.value = p.arrive;
  const dep = el('input'); dep.type = 'time'; dep.value = p.depart;
  const match = el('select');
  const optNew = el('option', null, 'Make a new stop'); optNew.value = ''; match.append(optNew);
  p.candidates.forEach(c => { const o = el('option', null, 'Use the stop at ' + c.arrive + '–' + c.depart + (c.name ? ' (' + c.name + ')' : '')); o.value = c.id; match.append(o); });
  match.value = p.stop_id ? String(p.stop_id) : '';
  const total = inp(rd.total, '0.00', 'decimal');
  const times = el('div', 'two'); times.append(lab('Arrived', arr), lab('Left', dep));
  const syncMatch = () => { times.hidden = !!match.value; };
  match.onchange = syncMatch;
  const gal = inp(rd.gallons, 'gallons', 'decimal'), odo = inp('', 'optional', 'numeric');
  const gasBox = el('div', 'two'); gasBox.append(lab('Gallons', gal), lab('Odometer', odo));
  // store items
  const itemsBox = el('div');
  const rows = (it.items || []).map(x => receiptItemRow(x, () => date.value));
  if (rows.length) {
    itemsBox.append(el('label', null, 'Items on the receipt'));
    rows.forEach(r => itemsBox.append(r.el));
  }
  const taxBox = el('label', 'chk'), taxC = el('input'); taxC.type = 'checkbox'; taxC.checked = !!rd.tax;
  taxBox.append(taxC, ' Add the tax ' + (rd.tax ? money(rd.tax) + ' ' : '') + 'as an expense');
  taxBox.hidden = !rd.tax;
  const wholeBox = el('label', 'chk'), wholeC = el('input'); wholeC.type = 'checkbox'; wholeC.checked = true;
  wholeBox.append(wholeC, ' No items were read: add the whole total as an expense');
  wholeBox.hidden = rows.length > 0;
  const venBox = el('div'); venBox.append(itemsBox, taxBox, wholeBox);
  const setType = t => { type = t; gasB.classList.toggle('on', t === 'gas'); venB.classList.toggle('on', t === 'vendor');
                         gasBox.hidden = t !== 'gas'; venBox.hidden = t !== 'vendor'; };
  gasB.onclick = () => setType('gas'); venB.onclick = () => setType('vendor');
  setType(type); syncMatch();
  const add = el('button', 'primary', 'Add to JobHub'); add.type = 'button';
  const skip = el('button', null, 'Skip'); skip.type = 'button';
  skip.onclick = async () => { try { await api('POST', '/api/receipts/inbox/' + it.id + '/skip'); card.remove(); } catch (e) { err.textContent = e.message; } };
  add.onclick = async () => {
    err.textContent = '';
    const body = {type, store: store.value, date: date.value, time: tm.value, arrive: arr.value, depart: dep.value,
                  stop_id: match.value ? +match.value : null, total: total.value};
    if (type === 'gas') Object.assign(body, {gallons: gal.value, odometer: odo.value});
    else Object.assign(body, {items: rows.map(r => r.value()), tax: rd.tax, tax_expense: taxC.checked, whole_as_expense: !rows.length && wholeC.checked});
    if (!body.stop_id && (!body.arrive || !body.depart)) { err.textContent = 'Give the arrived and left times (or the time on the receipt).'; return; }
    add.disabled = true;
    try {
      const r = await api('POST', '/api/receipts/inbox/' + it.id + '/apply', body);
      const a = r.item.applied, done = el('a', 'hrow', '');
      done.href = '/day/' + a.date + '?stop=' + a.stop_id;
      done.append(el('div', null, '✓ Added: ' + (a.type === 'gas' ? 'Gas' : 'Vendor') + ' stop, ' + store.value + ', ' + niceDate(a.date) + ' ›'));
      card.replaceChildren(done);
    } catch (e) { err.textContent = e.message; add.disabled = false; }
  };
  tm.onchange = () => {                       // a new receipt time moves a new stop's times with it
    if (!tm.value || match.value) return;
    const [h, m] = tm.value.split(':').map(Number), t = h * 60 + m, f = x => String(Math.floor(((x + 1440) % 1440) / 60)).padStart(2, '0') + ':' + String((x + 1440) % 60).padStart(2, '0');
    arr.value = f(t - (type === 'gas' ? 8 : 20)); dep.value = f(t + 2);
  };
  date.onchange = () => rows.forEach(r => r.refreshJobs());
  const head = el('div', 'rhead'); head.append(thumb, el('div', null, ''));
  head.lastChild.append(seg, lab('Store', store));
  const g1 = el('div', 'two'); g1.append(lab('Date', date), lab('Time on receipt', tm));
  const acts = el('div', 'row'); acts.append(add, skip, del);
  card.append(head, g1, lab('Stop', match), times, lab('Total', total), gasBox, venBox, acts, err);
  return card;
}

function receiptItemRow(x, getDate) {
  const box = el('div', 'ritem');
  const name = el('input'); name.value = x.description;
  const qty = el('input'); qty.value = x.qty; qty.inputMode = 'decimal';
  const amt = el('input'); amt.value = x.amount == null ? '' : x.amount; amt.inputMode = 'decimal'; amt.placeholder = 'amount';
  const act = el('select');
  RECEIPT_ACTIONS.forEach(([v, t]) => { const o = el('option', null, t); o.value = v; act.append(o); });
  act.value = x.action;
  let part = x.part;                               // {id, name, unit, pack_size, pack_unit} or null (= a new part)
  const partLine = el('div', 'hint');
  const packC = el('input'); packC.type = 'checkbox'; packC.checked = !!x.packs;
  const packL = el('label', 'chk'); packL.append(packC, '');
  const showPack = () => {
    const ok = part && part.pack_size;
    packL.hidden = !ok;
    if (ok) packL.lastChild.textContent = ' The receipt counts ' + (part.pack_unit || 'pack') + 's (' + part.pack_size + ' ' +
      part.unit + ' each): ' + (Number(qty.value) || 0) + ' = ' + Math.round((Number(qty.value) || 0) * part.pack_size * 1000) / 1000 + ' ' + part.unit;
  };
  const partIn = el('input'); partIn.placeholder = 'Search your parts (blank = new part with this name)';
  const showPart = () => { partLine.textContent = part ? 'Part: ' + part.name + (x.sku ? ' · receipt #' + x.sku : '') : 'New part: “' + name.value + '”'; showPack(); };
  const partBox = el('div'); partBox.append(partLine, packL, partIn);
  liveSearch(partIn, () => PARTS.filter(p => p.kind !== 'expense'), p => {
    part = {id: p.id, name: p.name, unit: p.unit, pack_size: p.pack_size, pack_unit: p.pack_unit};
    packC.checked = !!(p.pack_size && x.unit_price != null && p.last_cost && Math.abs(x.unit_price - p.last_cost * p.pack_size) < Math.abs(x.unit_price - p.last_cost));
    partIn.value = ''; showPart(); });
  partIn.addEventListener('input', () => { if (!partIn.value.trim()) { /* keep the current choice */ } });
  const clear = el('button', null, 'New part instead'); clear.type = 'button'; clear.onclick = () => { part = null; showPart(); };
  partBox.append(clear);
  const job = el('select');
  const jobBox = lab('Used on job', job);
  async function refreshJobs() {
    const keep = job.value;
    job.replaceChildren(el('option', null, 'Loading jobs…'));
    try {
      const jobs = await jobsFor(getDate());
      job.replaceChildren();
      const none = el('option', null, jobs.length ? 'Pick the job…' : 'No jobs within 60 days'); none.value = ''; job.append(none);
      jobs.forEach(j => { const o = el('option', null, niceDate(j.date) + ' ' + j.arrive + ' · ' + j.name + (j.ticket ? ' · #' + j.ticket : '')); o.value = j.id; job.append(o); });
      job.value = keep;
    } catch (e) { job.replaceChildren(el('option', null, e.message)); }
  }
  const sync = () => { partBox.hidden = !['stock', 'job'].includes(act.value); jobBox.hidden = act.value !== 'job';
                       box.classList.toggle('skip', act.value === 'skip'); if (act.value === 'job' && !job.options.length) refreshJobs(); };
  act.onchange = sync; name.oninput = showPart; qty.oninput = showPack;
  const line = el('div', 'rline'); line.append(lab('Qty', qty), lab('Amount', amt), lab('This item is', act));
  box.append(name, line, partBox, jobBox);
  showPart(); sync();
  return {el: box, refreshJobs,
          value: () => ({action: act.value, name: name.value, sku: x.sku || '', qty: qty.value, amount: amt.value,
                         packs: !!(part && part.pack_size && packC.checked),
                         part_id: part ? part.id : null, job_stop_id: job.value ? +job.value : null})};
}
