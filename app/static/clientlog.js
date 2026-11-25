// Page-side logging into the JobHub log (Settings → Log and backups), for problems that happen on the phone.
// Always sent: page errors and failed requests. Sent only with Log detail = debug: jhLog('debug', …) steps.
// Text only, 300 characters, at most 40 lines a minute; the server blanks anything that looks like a coordinate.
(function () {
  const meta = document.querySelector('meta[name="jobhub-log"]');
  const debug = meta && meta.content === 'debug';
  let sent = 0, windowStart = Date.now();
  const page = () => location.pathname + (location.hash || '');
  function send(level, msg) {
    if (Date.now() - windowStart > 60000) { windowStart = Date.now(); sent = 0; }
    if (++sent > 40) return;
    const body = JSON.stringify({level, msg: String(msg).slice(0, 300), page: page()});
    try {
      if (navigator.sendBeacon) navigator.sendBeacon('/api/client-log', new Blob([body], {type: 'application/json'}));
      else fetch('/api/client-log', {method: 'POST', headers: {'Content-Type': 'application/json'}, body, keepalive: true});
    } catch (e) { /* logging must never break the page */ }
  }
  window.jhLog = (level, msg) => { if (level !== 'debug' || debug) send(level, msg); };
  window.addEventListener('error', e => send('error', (e.message || 'error') + ' at ' + (e.filename || '').split('/').pop() + ':' + (e.lineno || '?')));
  window.addEventListener('unhandledrejection', e => send('error', 'unhandled: ' + ((e.reason && e.reason.message) || e.reason)));
  if (debug) send('debug', 'page opened (' + (navigator.standalone ? 'Home Screen app' : 'browser') + ', ' + window.innerWidth + 'x' + window.innerHeight + ')');
})();

// Behind Cloudflare Access: when its sign-in runs out, background requests are redirected to Cloudflare's login
// page, which the browser blocks, so fetch fails like a lost connection. Ask once (JobHub's /health never redirects
// by itself): if it is being redirected, reload the page so Cloudflare can show its sign-in. At most once a minute.
(function () {
  const orig = window.fetch && window.fetch.bind(window);
  if (!orig) return;
  let checking = null;
  async function signedOutAtCloudflare() {
    try {
      const r = await orig('/health', {redirect: 'manual', cache: 'no-store', credentials: 'same-origin'});
      return r.type === 'opaqueredirect' || r.status === 401 || r.status === 403;
    } catch (e) { return false; }                          // really offline
  }
  window.fetch = async function (input, init) {
    try { return await orig(input, init); }
    catch (e) {
      if (!checking) checking = signedOutAtCloudflare().finally(() => setTimeout(() => { checking = null; }, 5000));
      if (await checking) {
        const last = Number(sessionStorage.getItem('jhSignInReload') || 0);
        if (Date.now() - last > 60000) {
          sessionStorage.setItem('jhSignInReload', String(Date.now()));
          if (window.jhLog) jhLog('warning', 'Cloudflare sign-in expired; reloading');
          location.reload();
        }
      }
      throw e;
    }
  };
})();
