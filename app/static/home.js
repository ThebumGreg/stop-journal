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

function nextCard(d) {
  if (!d.calendar_on) {
    const a = el('a', null, 'Settings → Calendar'); a.href = '/settings';
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
  if (n.location) {
    const m = el('a', 'btn go', 'Directions in Maps');
    m.href = 'https://maps.apple.com/?daddr=' + encodeURIComponent(n.location);
    kids.push(m);
  }
  return card('Next appointment', ...kids.filter(Boolean));
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

async function load() {
  try {
    const r = await fetch('/api/summary');
    if (r.status === 401) { location.href = '/login'; return; }
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Could not load (' + r.status + ')');
    root.replaceChildren(nowCard(d), nextCard(d), todayCard(d));
    upd.textContent = 'Updated ' + d.now_text;
  } catch (e) {
    upd.textContent = 'Could not update: ' + e.message;
  }
}
load();
setInterval(() => { if (!document.hidden) load(); }, 60000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
