// Home-screen (standalone) mode on iPhone: a file opened inside the app has no Back, Save or Share, so links
// marked data-file="PDF" open a small panel instead:
//   - "Open in Safari": opens the file in the real Safari app, which has Share, Save to Files and a way back. A plain
//     link or window.open stays inside the app (every page is in the app's scope), so the link uses iOS's
//     x-safari-https:// form (iOS 17 and later). Safari does not have this app's login, so the link carries a
//     download token that works for 10 minutes (downloads only).
//   - "View here" (PDFs): an in-page viewer with a Close bar, for when Safari does not open.
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
    '.fsheet .fmsg{font-size:14px;opacity:.75}' +
    '.fview{position:fixed;inset:0;z-index:10000;background:#fff;display:flex;flex-direction:column}' +
    '.fview>div{display:flex;gap:8px;align-items:center;padding:calc(8px + env(safe-area-inset-top)) 12px 8px;' +
    'border-bottom:1px solid #ccc}.fview>div b{flex:1;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    '.fview button{min-height:44px;font-size:16px}.fview iframe{flex:1;border:0;width:100%}';
  document.head.append(css);

  function withToken(href) {
    const u = new URL(href, location.href);
    if (token) u.searchParams.set('ft', token);
    if (/\/api\/export\.pdf$/.test(u.pathname)) u.searchParams.set('inline', '1');   // Safari shows it, with Share
    return u.toString();
  }

  function inSafari(url) {                   // iOS 17+: x-safari-https://host/path opens the Safari app itself
    return url.replace(/^(https?):\/\//, 'x-safari-$1://');
  }

  function viewer(url, label) {
    const v = document.createElement('div'); v.className = 'fview';
    const bar = document.createElement('div');
    const t = document.createElement('b'); t.textContent = label;
    const done = document.createElement('button'); done.type = 'button'; done.className = 'primary'; done.textContent = 'Close';
    done.onclick = () => v.remove();
    const f = document.createElement('iframe'); f.src = url; f.title = label;
    bar.append(t, done); v.append(bar, f);
    document.body.append(v);
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
    const isPdf = /\/api\/export\.pdf$/.test(new URL(href, location.href).pathname);
    const view = document.createElement('button'); view.type = 'button'; view.textContent = 'View here'; view.hidden = !isPdf;
    box.append(title, msg, open, share, view, cancel); wrap.append(box);
    view.onclick = () => {
      const u = new URL(href, location.href); u.searchParams.set('inline', '1');
      close(); viewer(u.toString(), title.textContent);
    };
    const close = () => wrap.remove();
    cancel.onclick = close;
    wrap.onclick = e => { if (e.target === wrap) close(); };
    open.onclick = () => {
      if (!token) { msg.textContent = 'Could not make a Safari link. Use Share instead, or try again in a moment.'; getToken(); return; }
      let left = false;
      const gone = () => { left = true; };
      document.addEventListener('visibilitychange', gone, {once: true});
      location.href = inSafari(withToken(href));          // stays on this page; iOS switches to Safari
      setTimeout(() => {
        document.removeEventListener('visibilitychange', gone);
        if (left || document.hidden) { close(); return; }
        msg.textContent = 'Safari did not open (it needs iOS 17 or later). Use Share / Save to Files' +
          (isPdf ? ' or View here.' : '.');
      }, 1500);
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
