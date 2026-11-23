// Page-side logging into the JobHub log (Settings → General → Log), for problems that happen on the phone.
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
