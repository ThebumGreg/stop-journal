// Settings → Account → Notifications: turn native notifications on or off for this phone or browser (Web Push through
// /sw.js), list every device that has them on, send a test. iOS: 16.4+, Home Screen app opened from the https address.
(function () {
  const box = document.getElementById('push');
  if (!box) return;
  const msg = box.querySelector('.pmsg'), list = box.querySelector('.plist');
  const onBtn = document.getElementById('pushon'), offBtn = document.getElementById('pushoff');
  const testBtn = document.getElementById('pushtest'), testMsg = document.getElementById('pushtestmsg');
  const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  const ios = /iPhone|iPad/.test(navigator.userAgent);
  const standalone = navigator.standalone === true || matchMedia('(display-mode: standalone)').matches;
  let key = null, reg = null;
  const say = t => { msg.textContent = t; };
  const when = ts => ts ? new Date(ts * 1000).toLocaleString([], {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}) : '';

  function bytes(b64) {
    const s = b64.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - b64.length % 4) % 4);
    return Uint8Array.from(atob(s), c => c.charCodeAt(0));
  }
  async function post(url, body) {
    const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
    let j = {};
    try { j = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) throw new Error(j.error || ('error ' + r.status));
    return j;
  }
  async function load() {
    const d = await (await fetch('/api/push', {cache: 'no-store'})).json();
    key = d.key;
    list.replaceChildren();
    if (!d.devices.length) { list.textContent = 'No phone or browser has notifications on yet.'; return; }
    for (const v of d.devices) {
      const row = document.createElement('div'); row.className = 'prow';
      const t = document.createElement('div');
      const name = document.createElement('strong'); name.textContent = v.label;
      const sub = document.createElement('div'); sub.className = 'help';
      sub.textContent = 'On since ' + when(v.created_ts) + (v.last_ok_ts ? ' · last delivered ' + when(v.last_ok_ts) : '') +
        (v.last_error ? ' · problem: ' + v.last_error : '');
      t.append(name, sub);
      const rm = document.createElement('button'); rm.type = 'button'; rm.textContent = 'Remove';
      rm.onclick = async () => { await post('/api/push/remove', {id: v.id}); await refresh(); };
      row.append(t, rm); list.append(row);
    }
  }
  async function refresh() {
    try { await load(); } catch (e) { list.textContent = 'Could not load the device list.'; }
    onBtn.hidden = offBtn.hidden = true;
    if (!window.isSecureContext) { say('Notifications need the https address (the Cloudflare tunnel); on the home network address they can’t be turned on.'); return; }
    if (!supported) {
      say(ios && !standalone ? 'On iPhone: open JobHub from its Home Screen icon (added in Safari from the https address: Share → Add to Home Screen), then come back here. Needs iOS 16.4 or later.'
                             : 'This browser does not support notifications.');
      return;
    }
    try { reg = await navigator.serviceWorker.register('/sw.js'); await navigator.serviceWorker.ready; }
    catch (e) { say('Could not start the notification helper (' + e.message + ').'); return; }
    let sub = null;
    try { sub = await reg.pushManager.getSubscription(); }
    catch (e) { say('This browser can\u2019t use notifications right now (' + e.message + ').'); return; }
    if (Notification.permission === 'denied') {
      say(ios ? 'Notifications are blocked for JobHub. iPhone Settings → Notifications → JobHub → Allow Notifications, then reload.'
              : 'Notifications are blocked for this site in the browser settings.');
      return;
    }
    onBtn.hidden = !!sub; offBtn.hidden = !sub;
    say(sub ? 'On for this device.' : 'Off for this device.');
  }

  onBtn.onclick = async () => {
    try {
      if (Notification.permission === 'default') {               // must be the first thing after the tap (iOS)
        if (await Notification.requestPermission() !== 'granted') { say('Not allowed, so notifications stay off.'); return; }
      }
      const r = reg || await navigator.serviceWorker.ready;
      const opts = {userVisibleOnly: true, applicationServerKey: bytes(key)};
      let sub;
      try { sub = await r.pushManager.subscribe(opts); }
      catch (e) {                                                  // signed up earlier with another key (restored backup)
        const old = await r.pushManager.getSubscription();
        if (!old) throw e;
        await old.unsubscribe(); sub = await r.pushManager.subscribe(opts);
      }
      await post('/api/push/subscribe', {subscription: sub.toJSON()});
      if (window.jhLog) jhLog('info', 'notifications turned on');
    } catch (e) { say('Could not turn notifications on: ' + e.message); return; }
    await refresh();
  };
  offBtn.onclick = async () => {
    try {
      const sub = reg && await reg.pushManager.getSubscription();
      if (sub) { await post('/api/push/remove', {endpoint: sub.endpoint}); await sub.unsubscribe(); }
    } catch (e) { say('Could not turn off: ' + e.message); return; }
    await refresh();
  };
  testBtn.onclick = async () => {
    testMsg.textContent = 'Sending…';
    try { testMsg.textContent = (await post('/api/push/test')).message; }
    catch (e) { testMsg.textContent = e.message; }
    load().catch(() => {});
  };
  refresh();
})();
