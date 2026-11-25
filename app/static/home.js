// Home: where you are, the next appointment (with drive time / ETA), and what is still open today.
// Refreshes every minute while on screen.
const root = document.getElementById('home'), upd = document.getElementById('upd');

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}
const dur = m => m >= 60 ? Math.floor(m / 60) + ' h' + (m % 60 ? ' ' + (m % 60) + ' min' : '') : m + ' min';
const needsText = n => n.includes('finish') ? 'not finished' : 'needs ' + n.join(', ');

function card(title, ...kids) {
  const c = el('section', 'card');
  c.append(el('h2', null, title), ...kids);
  return c;
}

function nowCard(d) {
  if (d.here) {
    const h = d.here;
    const a = el('a', 'btn primary go', 'Open this stop'); a.href = h.url;
    return card('Now', el('div', 'big', 'At ' + h.name),
                el('div', 'sub', 'Since ' + h.since + ' (' + dur(h.minutes) + ')' + (h.needs.length ? ' · ' + needsText(h.needs) : ' · finished')), a);
  }
  if (d.live) {
    const l = d.live;
    const where = l.moving ? 'Driving' : (l.place ? 'At ' + l.place : 'Stopped');
    return card('Now', el('div', 'big', where), el('div', 'sub', l.moving ? 'Moving' : 'Here ' + dur(Math.round(l.here_s / 60))));
  }
  return card('Now', el('div', 'sub', d.live_error || 'No location yet.'));
}

// ---- Go to a place: pick a saved place, open Apple or Google Maps; the stop there is filled in on arrival ----
const KIND = {client: 'Client', vendor: 'Vendor', office: 'Office', home: 'Home'};
function mapsUrls(p) {
  const dest = p.lat != null ? p.lat + ',' + p.lon : (p.address || p.name);
  return {apple: 'https://maps.apple.com/?daddr=' + encodeURIComponent(dest) + '&dirflg=d',
          google: 'https://www.google.com/maps/dir/?api=1&travelmode=driving&destination=' + encodeURIComponent(dest)};
}
// DELETE and PUT are sent as POST with X-Method-Override: some routers' intrusion prevention blocks a device for minutes
// after a DELETE (seen 2026-11-23). The server turns the header back into the real method.
const wireMethod = m => (m === 'DELETE' || m === 'PUT') ? 'POST' : m;
const overrideHeader = m => (m === 'DELETE' || m === 'PUT') ? {'X-Method-Override': m} : {};
async function post(method, url, body) {
  const r = await fetch(url, {method: wireMethod(method), headers: {...(body ? {'Content-Type': 'application/json'} : {}), ...overrideHeader(method)}, body: body ? JSON.stringify(body) : undefined});
  if (r.status === 401) { location.href = '/login'; throw new Error('Login required'); }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || 'Request failed (' + r.status + ')');
  return d;
}
function mapButtons(p, before) {
  const row = el('div', 'acts2');
  const u = mapsUrls(p);
  [['Apple Maps', u.apple], ['Google Maps', u.google]].forEach(([t, href]) => {
    const b = el('a', 'btn primary', t); b.href = href;
    b.onclick = async ev => {
      if (!before) return;                         // already heading there: just open the map
      ev.preventDefault();
      try { await before(); } catch (e) { alert(e.message); return; }
      location.href = href;
    };
    row.append(b);
  });
  return row;
}
let picking = false, goEl = null;
function goCard(d) {
  const kids = [];
  if (d.trip_done && !d.trip) {
    const a = el('a', 'item'); a.href = d.trip_done.url;
    a.append(el('div', 'n', '✓ ' + d.trip_done.name + ': stop filled in'), el('div', 'm', 'Open it to add the work'));
    kids.push(a);
  }
  if (d.trip) {
    const t = d.trip;
    kids.push(el('div', 'big', t.name), el('div', 'sub', (KIND[t.kind] || '') + (t.address ? ' · ' + t.address : '') + ' · since ' + t.since),
              el('div', 'sub', 'When you stop there, the stop is filled in with this place.'), mapButtons(t, null));
    const c = el('button', null, 'Cancel'); c.type = 'button';
    c.onclick = async () => { try { await post('DELETE', '/api/go'); picking = false; load(); } catch (e) { alert(e.message); } };
    kids.push(c);
    return (goEl = card('Heading to', ...kids));
  }
  const open = el('button', 'pickbtn', 'Pick a place…'); open.type = 'button';
  const box = el('div'); box.hidden = true;
  open.onclick = () => { picking = true; open.hidden = true; box.hidden = false; fill(box); };
  kids.push(el('div', 'sub', 'Pick a saved place and get directions. When you stop there, the stop is filled in for you.'), open, box);
  return (goEl = card('Go to a place', ...kids));
}
async function fill(box) {
  box.replaceChildren(el('div', 'sub', 'Loading places…'));
  let d;
  try { d = await (await fetch('/api/go/places')).json(); } catch (e) { box.replaceChildren(el('div', 'err', 'Could not load places.')); return; }
  const q = el('input'); q.type = 'search'; q.placeholder = 'Search name or address'; q.autocomplete = 'off';
  const list = el('div');
  const done = el('button', null, 'Close'); done.type = 'button';
  done.onclick = () => { picking = false; load(true); };
  box.replaceChildren(q, list, done);
  const show = () => {
    const words = q.value.toLowerCase().split(/\s+/).filter(Boolean);
    const hits = d.places.filter(p => words.every(w => (p.name + ' ' + (p.address || '') + ' ' + p.kind).toLowerCase().includes(w))).slice(0, 12);
    list.replaceChildren(...hits.map(p => {
      const row = el('div', 'place');
      const head = el('button', 'phead'); head.type = 'button';
      head.append(el('div', 'n', p.name), el('div', 'm', [KIND[p.kind], p.address, p.miles != null ? p.miles + ' mi' : ''].filter(Boolean).join(' · ')));
      head.onclick = () => {
        list.querySelectorAll('.acts2').forEach(x => x.remove());
        row.append(mapButtons(p, () => post('POST', '/api/go', {site_id: p.id})));
      };
      row.append(head);
      return row;
    }));
    if (!hits.length) list.append(el('div', 'sub', d.places.length ? 'No saved place matches.' : 'No saved places yet (Places page).'));
  };
  q.oninput = show;
  show();
  if (!d.located) list.prepend(el('div', 'sub', 'Your position is unknown, so places are not sorted by distance.'));
}

function nextCard(d) {
  if (!d.calendar_on) {
    const a = el('a', null, 'Settings → My calendar'); a.href = '/settings#my-calendar';
    const s = el('div', 'sub'); s.append('Connect your Outlook calendar in ', a, ' to see your next on-site appointment and drive time here.');
    return card('Next appointment', s);
  }
  if (d.calendar_error) return card('Next appointment', el('div', 'err', d.calendar_error));
  const n = d.next;
  if (!n) return card('Next appointment', el('div', 'sub', 'No more on-site appointments today.'));
  const when = n.minutes_to_start > 0 ? 'in ' + dur(n.minutes_to_start) : n.minutes_to_start > -15 ? 'now' : 'started ' + dur(-n.minutes_to_start) + ' ago';
  const kids = [el('div', 'big', n.name), el('div', 'sub', n.start + '–' + n.end + ' · ' + when), n.location ? el('div', 'sub', n.location) : null];
  const e = n.eta;
  if (e && e.arrived) kids.push(el('div', 'eta', 'You are there.'));
  else if (e) {
    const box = el('div', 'eta');
    const diff = e.early_min >= 0 ? el('span', 'early', e.early_min + ' min early') : el('span', 'late', -e.early_min + ' min late');
    if (e.driving) {
      box.append(el('b', null, 'Arriving ' + e.arrive), el('div', null, ''));
      box.lastChild.append(dur(e.minutes) + ' · ' + e.miles + ' mi · ', diff);
    } else {
      box.append(el('b', null, 'Leave by ' + e.leave_by), el('div', null, dur(e.minutes) + ' drive · ' + e.miles + ' mi · you would arrive ' + e.arrive + ' ('));
      box.lastChild.append(diff, ')');
    }
    box.append(el('div', 'sub', (e.method === 'routed' ? 'Road route, no traffic.' : 'Rough estimate (routing unavailable).') +
                                ' Address from ' + e.found_by + '.'));
    kids.push(box);
  } else if (n.eta_note) kids.push(el('div', 'sub', n.eta_note));
  if (n.late || n.late_notice) kids.push(lateBox(n));
  if (n.location) kids.push(mapButtons({address: n.location, name: n.name}, null));
  return card('Next appointment', ...kids.filter(Boolean));
}

// Running late: "Tell the customer" emails the work AI agent a JobHub Late Notice. Only on this tap, after a confirm.
let lateNote = '';                       // kept across the once-a-minute refresh
const clock = ts => new Date(ts * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
function lateBox(n) {
  const box = el('div', 'latebox');
  const e = n.eta || {};
  if (n.late) box.append(el('b', 'late', '⚠ You will be about ' + e.late_min + ' min late (arriving ' + e.arrive + ').'));
  if (n.late_notice) box.append(el('div', 'sub', '✓ Late notice sent ' + (n.late_notice.time || clock(n.late_notice.ts)) + ' (' + n.late_notice.minutes_late +
                                                 ' min late) to ' + n.late_notice.mail_to + '.'));
  if (!n.late) return box;
  const note = el('input'); note.placeholder = 'Note (optional): stuck in traffic';
  note.value = lateNote; note.maxLength = 300;
  note.oninput = () => { lateNote = note.value; };
  const err = el('div', 'err');
  const b = el('button', 'primary', n.late_notice ? 'Send an updated late notice' : 'Tell the customer');
  b.type = 'button';
  b.onclick = async () => {
    if (!confirm('Email your work AI agent a late notice for ' + n.name + ' (' + n.start + '): arriving about ' + e.arrive +
                 ', ' + e.late_min + ' min late?')) return;
    b.disabled = true; b.textContent = 'Sending…'; err.textContent = '';
    try { await post('POST', '/api/late-notice', {uid: n.uid, note: note.value}); lateNote = ''; load(true); }
    catch (x) { err.textContent = x.message; b.disabled = false; b.textContent = 'Tell the customer'; }
  };
  box.append(note, b, err,
             el('div', 'sub', 'The agent gets the ticket, customer, appointment and new arrival time, and contacts the customer.'));
  return box;
}

function todayCard(d) {
  const t = d.today;
  const stats = el('div', 'stats');
  [[t.stops, 'stops'], [t.finished, 'finished'], [dur(t.on_site_min), 'on site'], [dur(t.drive_min), 'driving']].forEach(([v, l]) => {
    const b = el('div'); b.append(el('b', null, v), el('span', null, l)); stats.append(b);
  });
  const kids = [stats, el('div', 'sub', '~' + t.miles + ' mi' + (t.ended ? ' · day ended' : t.started ? ' · day started' : ''))];
  d.unfinished.forEach(u => {
    const a = el('a', 'item'); a.href = u.url;
    a.append(el('div', 'n', 'Stop ' + u.num + ' · ' + u.name + (u.here ? ' (here now)' : '')),
             el('div', 'm', u.arrive + '–' + u.depart + ' · ' + needsText(u.needs)));
    kids.push(a);
  });
  if (d.appointments.length) {
    kids.push(el('h2', null, 'Appointments'));
    d.appointments.forEach(a => {
      const st = a.state === 'done' ? ' ✓' : a.state === 'here' ? ' · here now' : a.state === 'missed' ? ' · no visit detected' : '';
      kids.push(el('div', 'appt ' + a.state, a.start + ' ' + a.name + st));
    });
  }
  const go = el('a', 'btn go', 'Open today'); go.href = d.day_url;
  kids.push(go);
  return card('Today', ...kids);
}

async function load(force) {
  try {
    const r = await fetch('/api/summary');
    if (r.status === 401) { location.href = '/login'; return; }
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Could not load (' + r.status + ')');
    if (!force && document.activeElement && document.activeElement.closest && document.activeElement.closest('.latebox')) return;
    const go = picking && goEl && !force && !d.trip ? goEl : goCard(d);   // keep an open picker as it is
    root.replaceChildren(nowCard(d), go, nextCard(d), todayCard(d));
    upd.textContent = 'Updated ' + d.now_text;
  } catch (e) {
    upd.textContent = 'Could not update: ' + e.message;
  }
}
load();
setInterval(() => { if (!document.hidden) load(); }, 60000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
