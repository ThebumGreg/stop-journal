// Home-screen (standalone) mode on iPhone: a file opened inside the app has no Back, Save or Share, so links
// marked data-file="PDF" open a small panel instead:
//   - "Open in Safari": opens the file in Safari, which has Share, Save to Files and a way back. Safari does not
//     have this app's login, so the link carries a download token that works for 10 minutes (downloads only).
//   - "Share / Save to Files": the iOS share sheet. iOS only opens it from a fresh tap, so the file is fetched
//     while the panel is open and the button turns on when it is ready.
// Everywhere else (Safari, desktop) the link downloads as normal.
(function () {
  const standalone = navigator.standalone === true || matchMedia('(display-mode: standalone)').matches;
  if (!standalone) return;
  let token = null, tokenAt = 0;
  async function getToken() {
    try {
      const r = await fetch('/api/file-token');
      if (r.ok) { token = (await r.json()).token; tokenAt = Date.now(); }
    } catch (e) { /* offline: Open in Safari stays off */ }
  }
  getToken();
  setInterval(getToken, 4 * 60 * 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && Date.now() - tokenAt > 4 * 60 * 1000) getToken(); });

  const css = document.createElement('style');
  css.textContent = '.fsheet{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;display:flex;align-items:flex-end}' +
    '.fsheet>div{background:var(--bg,#fff);color:inherit;width:100%;padding:16px 16px calc(16px + env(safe-area-inset-bottom));' +
    'border-radius:14px 14px 0 0;display:grid;gap:10px}.fsheet b{font-size:17px}.fsheet button{min-height:48px;font-size:16px}' +
    '.fsheet .fmsg{font-size:14px;opacity:.75}';
  document.head.append(css);

  function withToken(href) {
    const u = new URL(href, location.href);
    if (token) u.searchParams.set('ft', token);
    if (/\/api\/export\.pdf$/.test(u.pathname)) u.searchParams.set('inline', '1');   // Safari shows it, with Share
    return u.toString();
  }

  function panel(a, href, label) {
    const wrap = document.createElement('div'); wrap.className = 'fsheet';
    const box = document.createElement('div');
    const title = document.createElement('b'); title.textContent = label.charAt(0).toUpperCase() + label.slice(1);
    const msg = document.createElement('div'); msg.className = 'fmsg'; msg.textContent = 'Getting the file ready…';
    const open = document.createElement('button'); open.type = 'button'; open.className = 'primary';
    open.textContent = 'Open in Safari (view, save, share)';
    const share = document.createElement('button'); share.type = 'button'; share.textContent = 'Preparing…'; share.disabled = true;
    const cancel = document.createElement('button'); cancel.type = 'button'; cancel.textContent = 'Cancel';
    box.append(title, msg, open, share, cancel); wrap.append(box);
    const close = () => wrap.remove();
    cancel.onclick = close;
    wrap.onclick = e => { if (e.target === wrap) close(); };
    open.onclick = () => {
      if (!token) { msg.textContent = 'Could not make a Safari link. Use Share instead, or try again in a moment.'; getToken(); return; }
      window.open(withToken(href), '_blank');
      close();
    };
    let file = null;
    share.onclick = async () => {
      if (!file) return;
      try { await navigator.share({files: [file]}); close(); }
      catch (e) { if (e.name !== 'AbortError') msg.textContent = 'Sharing did not work (' + e.message + '). Try Open in Safari.'; }
    };
    document.body.append(wrap);
    if (!navigator.canShare) { share.hidden = true; msg.textContent = 'Open it in Safari to save or share it.'; return; }
    (async () => {
      try {
        const r = await fetch(href);
        if (r.status === 401) { location.href = '/login'; return; }
        if (!r.ok) {
          let m = 'Could not get the file (' + r.status + ')';
          try { m = (await r.json()).error || m; } catch (e) { /* not JSON */ }
          throw new Error(m);
        }
        const name = ((/filename\*?=(?:UTF-8'')?"?([^";]+)/i.exec(r.headers.get('Content-Disposition') || '') || [])[1] || 'download').trim();
        const type = (r.headers.get('Content-Type') || '').split(';')[0] || 'application/octet-stream';
        const f = new File([await r.blob()], decodeURIComponent(name), {type});
        if (!navigator.canShare({files: [f]})) throw new Error('this phone cannot share that kind of file');
        file = f;
        share.disabled = false; share.textContent = 'Share / Save to Files';
        msg.textContent = 'Ready: ' + f.name + ' (' + Math.max(1, Math.round(f.size / 1024)) + ' KB).';
      } catch (e) {
        share.hidden = true;
        msg.textContent = 'Share is not available (' + e.message + '). Open it in Safari instead.';
      }
    })();
  }

  document.addEventListener('click', ev => {
    const a = ev.target.closest && ev.target.closest('a[data-file]');
    if (!a) return;
    const href = a.getAttribute('href');
    ev.preventDefault();
    if (!href) return;                       // not ready yet (the export page is still checking)
    panel(a, href, a.dataset.file);
  });
  window.resetFileLink = () => {};           // kept for the export page; nothing is cached on the link any more
})();
