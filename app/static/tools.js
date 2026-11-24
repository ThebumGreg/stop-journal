// Export & tools page: export, re-detect, debug data for one day. Standalone (no day.js).
const DATE = new URLSearchParams(location.search).get('date') || new Date().toLocaleDateString('en-CA');

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function labeled(label, input) { const w = el('div'); w.append(el('label', null, label), input); return w; }
// DELETE and PUT are sent as POST with X-Method-Override: some routers' intrusion prevention blocks a device for minutes
// after a DELETE (seen 2026-11-23). The server turns the header back into the real method.
const wireMethod = m => (m === 'DELETE' || m === 'PUT') ? 'POST' : m;
const overrideHeader = m => (m === 'DELETE' || m === 'PUT') ? {'X-Method-Override': m} : {};
async function api(method, url, body) {
  const r = await fetch(url, {method: wireMethod(method), headers: {...(body ? {'Content-Type': 'application/json'} : {}), ...overrideHeader(method)},
                              body: body ? JSON.stringify(body) : undefined});
  if (r.status === 401) { location.href = '/login'; throw new Error('Login required'); }
  let d = null;
  try { d = await r.json(); } catch (e) { /* non-JSON error page */ }
  if (!r.ok) throw new Error((d && d.error) || 'Request failed (' + r.status + ')');
  return d;
}

// Sections that can be left out of an export; ticks are remembered on this phone.
const SECTIONS = [['costs', 'Part and material costs'], ['expenses', 'Expenses'], ['fuel', 'Fuel'], ['photos', 'Receipts and photos'], ['materials', 'Non-billable materials'],
  ['categories', 'Hours by category'], ['follow_ups', 'Follow-ups'], ['mileage', 'Mileage table'],
  ['recap', 'Day recap (timesheet)'], ['coordinates', 'GPS coordinates']];

function exportForm() {
  const f = el('div', 'box');
  const from = el('input'); from.type = 'date'; from.value = DATE;
  const to = el('input'); to.type = 'date'; to.value = DATE;
  const two = el('div', 'two'); two.append(labeled('From', from), labeled('To', to));
  const info = el('div', 'hint', 'Checking…');
  const warn = el('div', 'box warnbox'); warn.hidden = true;
  const pdf = el('a', 'btn primary', 'Download PDF'), js = el('a', 'btn', 'Download JSON');
  const zip = el('a', 'btn', 'Zip: PDF + photos');
  zip.title = 'The PDF, the JSON and every receipt and photo in the report, filed by year/month/day';
  const dbg = el('a', 'btn', 'Debug data');
  dbg.title = 'Raw GPS points, detected stops and your edits for the “From” day (contains locations)';
  const acts = el('div', 'acts'); acts.append(pdf, zip, js, dbg);
  pdf.dataset.file = 'PDF'; zip.dataset.file = 'zip'; js.dataset.file = 'JSON'; dbg.dataset.file = 'debug data';  // share sheet in home-screen mode (share.js)

  // Include: ticks for each optional section, with two presets
  let omit = [];
  try { omit = JSON.parse(localStorage.getItem('export_omit') || '[]'); } catch (e) {}
  const inc = el('details', 'box'); inc.append(el('summary', null, 'Include in the report'));
  const incHint = el('div', 'hint');
  const boxes = {};
  const pre = el('div', 'acts');
  const all = el('button', null, 'Include everything'); all.type = 'button';
  all.onclick = () => { omit = []; syncBoxes(); };
  pre.append(all);
  inc.append(pre);
  SECTIONS.forEach(([k, t]) => {
    const l = el('label', 'hint'), c = el('input'); c.type = 'checkbox';
    c.onchange = () => { omit = SECTIONS.map(x => x[0]).filter(x => !boxes[x].checked); syncBoxes(); };
    boxes[k] = c; l.append(c, ' ' + t); inc.append(l);
  });
  inc.append(incHint);
  function syncBoxes() {
    SECTIONS.forEach(([k]) => { boxes[k].checked = !omit.includes(k); });
    localStorage.setItem('export_omit', JSON.stringify(omit));
    incHint.textContent = omit.length ? 'Left out: ' + SECTIONS.filter(x => omit.includes(x[0])).map(x => x[1]).join(', ') +
      '. The report says so, so the reader does not take it for missing data.' : 'Everything is included.';
    inc.querySelector('summary').textContent = 'Include in the report' + (omit.length ? ' (' + omit.length + ' left out)' : '');
    setLinks();
  }
  const q = () => 'from=' + from.value + '&to=' + (to.value || from.value) + (omit.length ? '&omit=' + omit.join(',') : '');
  let blocked = false;
  const overBox = el('input'); overBox.type = 'checkbox';
  const over = el('label', 'hint'); over.append(overBox, ' Export anyway, leaving those out'); over.hidden = true;
  function setLinks() {
    const on = !blocked || overBox.checked;
    for (const a of [pdf, zip, js]) {
      a.classList.toggle('disabled', !on);
      a.setAttribute('aria-disabled', on ? 'false' : 'true');
    }
    if (on && !info.textContent.startsWith('Checking')) { pdf.href = '/api/export.pdf?' + q(); zip.href = '/api/export.zip?' + q(); js.href = '/api/export.json?' + q(); }
    else { pdf.removeAttribute('href'); zip.removeAttribute('href'); js.removeAttribute('href'); }
    if (window.resetFileLink) [pdf, zip, js].forEach(resetFileLink);
  }
  overBox.onchange = setLinks;

  // Email report: sent only when tapped, after checking the day. Attachment ticks are remembered on this phone.
  const mail = el('div', 'box mailbox');
  const mailHead = el('div', 'hint');
  let mopts = {pdf: true, json: true, photos: false};
  try { mopts = {...mopts, ...JSON.parse(localStorage.getItem('email_opts') || '{}')}; } catch (e) {}
  const mticks = el('div', 'acts');
  [['json', 'JSON (for the AI agent)'], ['pdf', 'PDF'], ['photos', 'Receipts and photos']].forEach(([k, t]) => {
    const l = el('label', 'hint'), c = el('input'); c.type = 'checkbox'; c.checked = !!mopts[k];
    c.onchange = () => { mopts[k] = c.checked; localStorage.setItem('email_opts', JSON.stringify(mopts)); };
    l.append(c, ' ' + t); mticks.append(l);
  });
  // format 2 (the agent's new message format) for one send, before switching in Settings → Email (never remembered)
  const v2Lab = el('label', 'hint'), v2Box = el('input'); v2Box.type = 'checkbox';
  v2Lab.append(v2Box, ' Send as format 2 (test for the agent)'); v2Lab.hidden = true;
  const send = el('button', 'primary', 'Email report');
  const mailMsg = el('div', 'hint');
  const mailLog = el('div', 'hint');
  mail.append(mailHead, mticks, v2Lab, send, mailMsg, mailLog);
  let mailTo = '';
  function showLog(h) {
    mailLog.replaceChildren();
    (h || []).slice(0, 5).forEach(x => {
      const t = new Date(x.ts * 1000).toLocaleString([], {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
      mailLog.append(el('div', x.ok ? null : 'err', (x.ok ? '✓ Sent ' : '✗ Failed ') + t + ' · ' + x.subject +
        (x.ok && x.format === '2' ? ' · format 2' : '') +
        (x.ok ? (x.attached ? ' · ' + x.attached : '') : ' · ' + (x.error || ''))));
    });
  }
  async function mailInfo() {
    try {
      const m = await api('GET', '/api/export/emails?' + q());
      mailTo = m.to;
      if (!m.ready) {
        mailHead.replaceChildren('Set up Gmail in ');
        const a = el('a', null, 'Settings → Email'); a.href = '/settings'; mailHead.append(a, ' to email the report to your work address.');
        send.disabled = true; mticks.hidden = true;
      } else {
        mailHead.textContent = 'Sends the report to ' + m.to + ' (with the Include choices above).' +
          (m.format === '2' ? ' Message format 2.' : '');
        send.disabled = false; mticks.hidden = false;
        v2Lab.hidden = m.format === '2';
      }
      showLog(m.history);
    } catch (e) { mailHead.textContent = e.message; }
  }
  send.onclick = async () => {
    if (blocked && !overBox.checked) { mailMsg.className = 'err'; mailMsg.textContent = 'Some stops are not confirmed yet. Confirm them, or tick “Export anyway”.'; return; }
    const dates = from.value + (to.value && to.value !== from.value ? ' to ' + to.value : '');
    if (!confirm('Email the report for ' + dates + ' to ' + mailTo + '?')) return;
    send.disabled = true; mailMsg.className = 'hint'; mailMsg.textContent = 'Sending…';
    try {
      const r = await api('POST', '/api/export/email?' + q(),
                          {...mopts, anyway: overBox.checked, ...(v2Box.checked ? {format: '2'} : {})});
      v2Box.checked = false;
      mailMsg.className = 'ok';
      mailMsg.textContent = 'Sent to ' + r.mail_to + ': “' + r.subject + '”.' +
        (r.skipped_photos ? ' ' + r.skipped_photos + ' photo(s) were too large for one email and were left off.' : '');
      showLog(r.history);
    } catch (e) { mailMsg.className = 'err'; mailMsg.textContent = e.message; mailInfo(); }
    send.disabled = false;
  };
  async function refresh() {
    info.className = 'hint'; info.textContent = 'Checking…';
    pdf.removeAttribute('href'); zip.removeAttribute('href'); js.removeAttribute('href'); dbg.removeAttribute('href');
    try {
      const c = await api('GET', '/api/export/check?' + q());
      const bits = [c.confirmed + ' confirmed stop' + (c.confirmed === 1 ? '' : 's') + ' will be included'];
      if (c.not_confirmed) bits.push(c.not_confirmed + ' not confirmed (left out)');
      if (c.personal) bits.push(c.personal + ' personal, lunch or traffic (left out)');
      info.textContent = bits.join(' · ') + (c.not_confirmed
        ? '. Confirm each one, or mark it Personal, Lunch or Traffic, before exporting.' : '.');
      info.className = c.not_confirmed ? 'err' : 'hint';
      warn.replaceChildren();
      const ws = c.warnings || [];
      if (ws.length) {
        warn.append(el('div', 'wtitle', 'Worth a look before you export (you can still export):'));
        const ul = el('ul'); ws.forEach(w => ul.append(el('li', null, w))); warn.append(ul);
      }
      warn.hidden = !ws.length;
      blocked = c.not_confirmed > 0;
      over.hidden = !blocked; overBox.checked = false;
      setLinks();
      dbg.href = '/api/debug/' + from.value + '.json';
      mailInfo();
    } catch (e) { info.className = 'err'; info.textContent = e.message; }
  }
  from.onchange = () => { if (to.value < from.value) to.value = from.value; refresh(); };
  to.onchange = refresh;
  f.append(el('div', 'hint', 'Only Confirmed stops are included in detail. Pick a range to export several days.'),
           two, inc, info, warn, over, acts, el('h3', null, 'Email report'), mail);
  syncBoxes();
  refresh();
  return f;
}

function redoForm() {
  const f = el('div', 'box');
  f.append(el('div', 'hint', 'Detect the day again, optionally with different thresholds (blank = your saved defaults). ' +
    'This discards edits to detected stops for this day; manual stops are kept. You can Undo it on the day page.'));
  const row = el('div', 'row');
  const mk = (label) => { const w = el('div'); const i = el('input'); i.inputMode = 'numeric';
    w.append(el('label', null, label), i); row.append(w); return i; };
  const min = mk('Min stop (min)'), rad = mk('Radius (m)'), gap = mk('Gap (min)');
  const err = el('div', 'err');
  const go = el('button', null, 'Re-detect ' + DATE);
  go.onclick = async () => {
    if (!confirm('Discard edits to detected stops for ' + DATE + ' and detect again?')) return;
    const body = {};
    if (min.value) body.min = min.value;
    if (rad.value) body.radius = rad.value;
    if (gap.value) body.gap = gap.value;
    try { await api('POST', '/api/day/' + DATE + '/redetect', body); location.href = '/day/' + DATE; }
    catch (e) { err.textContent = e.message; }
  };
  f.append(row, go, err);
  return f;
}

function dayActions() {
  const f = el('div', 'box');
  const info = el('div', 'hint', 'Loading…'), err = el('div', 'err');
  const acts = el('div', 'acts');
  f.append(info, acts, err);
  async function act(fn, msg) {
    err.textContent = '';
    try { const d = await fn(); await load(msg(d)); } catch (e) { err.textContent = e.message; }
  }
  async function load(note) {
    let d;
    try { d = await api('GET', '/api/day/' + DATE); } catch (e) { info.textContent = ''; err.textContent = e.message; return; }
    const ready = d.stops.filter(x => x.suggest).length;
    info.textContent = (note ? note + ' ' : '') + d.stops.length + ' stop' + (d.stops.length === 1 ? '' : 's') + ' on ' + DATE + '.';
    acts.replaceChildren();
    const sync = el('button', null, 'Sync new data');
    sync.onclick = () => act(() => api('POST', '/api/day/' + DATE + '/sync'),
                             r => r.added ? r.added + ' new stop' + (r.added > 1 ? 's' : '') + ' found.' : 'No new stops.');
    acts.append(sync);
    if (ready) {
      const b = el('button', 'primary', 'Confirm ' + ready + ' known');
      b.onclick = () => {
        if (!confirm('Confirm ' + ready + ' stop' + (ready > 1 ? 's' : '') + ' at saved places? You can undo this.')) return;
        act(() => api('POST', '/api/day/' + DATE + '/confirm-known'), () => 'Confirmed ' + ready + '.');
      };
      acts.append(b);
    }
    if (d.undo) {
      const u = el('button', 'danger', 'Undo: ' + d.undo.label.toLowerCase());
      u.onclick = () => {
        if (!confirm('Put the day back as it was before “' + d.undo.label.toLowerCase() + '”? Edits made since then are lost.')) return;
        act(() => api('POST', '/api/day/' + DATE + '/undo'), () => 'Undone.');
      };
      acts.append(u);
    }
  }
  load('');
  return f;
}

const dateIn = document.getElementById('tdate');
dateIn.value = DATE;
dateIn.onchange = () => { location.href = '/tools?date=' + dateIn.value; };
document.getElementById('backlink').href = '/day/' + DATE;
document.getElementById('actions').append(dayActions());
document.getElementById('export').append(exportForm());
document.getElementById('redo').append(redoForm());
