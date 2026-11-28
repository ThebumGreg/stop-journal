// Settings page: open the group in the address, and the Test buttons.
(function () {
  const open = id => { const d = document.getElementById(id); if (d && d.tagName === 'DETAILS') { d.open = true; d.scrollIntoView({block: 'start'}); } };
  const bad = document.querySelector('details.grp[open]');
  if (bad) bad.scrollIntoView({block: 'start'});
  else if (location.hash) open(location.hash.slice(1));
  window.addEventListener('hashchange', () => open(location.hash.slice(1)));
})();
function wire(btnId, msgId, url) {
  document.getElementById(btnId).onclick = async () => {
    const out = document.getElementById(msgId);
    out.className = ''; out.textContent = 'Working…';
    try {
      const r = await fetch(url, {method: 'POST'});
      const d = await r.json();
      out.className = d.ok ? 'ok' : 'err';
      out.textContent = d.message;
    } catch (e) { out.className = 'err'; out.textContent = 'Request failed.'; }
  };
}
document.getElementById('testcal').onclick = async () => {
  const out = document.getElementById('calmsg');
  out.className = ''; out.textContent = 'Reading the calendar…';
  try {
    const fd = new FormData();
    fd.append('calendar_url', document.getElementById('calendar_url').value);
    fd.append('calendar_keyword', document.getElementById('calendar_keyword').value);
    const d = await (await fetch('/settings/test-calendar', {method: 'POST', body: fd})).json();
    out.className = d.ok ? 'ok' : 'err'; out.textContent = d.message;
  } catch (e) { out.className = 'err'; out.textContent = 'Request failed.'; }
};
document.getElementById('testmail').onclick = async () => {
  const out = document.getElementById('mailmsg');
  out.className = ''; out.textContent = 'Sending…';
  try {
    const fd = new FormData();
    for (const k of ['mail_to', 'smtp_user', 'smtp_password', 'mail_from', 'smtp_host', 'smtp_port'])
      fd.append(k, document.getElementById(k).value);
    const d = await (await fetch('/settings/test-email', {method: 'POST', body: fd})).json();
    out.className = d.ok ? 'ok' : 'err'; out.textContent = d.message;
  } catch (e) { out.className = 'err'; out.textContent = 'Request failed.'; }
};
document.getElementById('testinvite').onclick = async () => {
  const out = document.getElementById('invitemsg');
  out.className = ''; out.textContent = 'Sending…';
  try {
    const d = await (await fetch('/settings/test-invite', {method: 'POST'})).json();
    out.className = d.ok ? 'ok' : 'err'; out.textContent = d.message;
  } catch (e) { out.className = 'err'; out.textContent = 'Request failed.'; }
};
document.getElementById('testgram').onclick = async () => {
  const out = document.getElementById('gramtestmsg');
  out.className = ''; out.textContent = 'Testing…';
  try {
    const fd = new FormData(); fd.append('languagetool_url', document.getElementById('languagetool_url').value);
    const d = await (await fetch('/settings/test-grammar', {method: 'POST', body: fd})).json();
    out.className = d.ok ? 'ok' : 'err'; out.textContent = d.message;
  } catch (e) { out.className = 'err'; out.textContent = 'Request failed.'; }
};
document.getElementById('finder').onclick = async () => {
  const msg = document.getElementById('findmsg'), list = document.getElementById('foundlist');
  msg.className = ''; msg.textContent = 'Looking…'; list.innerHTML = '';
  try {
    const fd = new FormData(); fd.append('recorder_url', document.getElementById('recorder_url').value);
    const d = await (await fetch('/settings/recorder/discover', {method: 'POST', body: fd})).json();
    msg.className = d.ok ? 'ok' : 'err';
    msg.textContent = d.ok ? '' : d.message;
    (d.users || []).forEach(u => u.devices.forEach(dev => {
      const b = document.createElement('button'); b.type = 'button';
      b.style.cssText = 'display:block;width:100%;text-align:left;margin-top:8px';
      b.textContent = u.user + ' / ' + dev.device + ' · last data: ' + dev.last;
      b.onclick = () => {
        document.getElementById('recorder_user').value = u.user;
        document.getElementById('recorder_device').value = dev.device;
        msg.className = 'ok'; msg.textContent = 'Selected ' + u.user + ' / ' + dev.device + '. Now Save settings.';
      };
      list.appendChild(b);
    }));
  } catch (e) { msg.className = 'err'; msg.textContent = 'Request failed.'; }
};
wire('testg', 'testmsg', '/settings/test-google');
wire('backupnow', 'backupmsg', '/settings/backup-now');
wire('testp', 'testpmsg', '/settings/test-pushover');
