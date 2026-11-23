// Home-screen (standalone) mode on iPhone: a file opened inside the app has no Back, Save or Share, and there is no
// way to hand a link to the Safari app from here (window.open stays in the app because every page is in its scope;
// iOS answers x-safari-https:// with "address is invalid"). So links marked data-file="PDF" open a small panel:
//   - "View here" (PDFs): an in-page viewer with Share and Close in a bar at the top.
//   - "Share / Save to Files": the iOS share sheet. iOS only opens it from a fresh tap, so the file is fetched
//     while the panel is open and the button turns on when it is ready.
// Everywhere else (Safari, desktop) the link downloads as normal.
(function () {
  const standalone = navigator.standalone === true || matchMedia('(display-mode: standalone)').matches;
  if (!standalone) return;

  const css = document.createElement('style');
  css.textContent = '.fsheet{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;display:flex;align-items:flex-end}' +
    '.fsheet>div{background:var(--bg,#fff);color:inherit;width:100%;padding:16px 16px calc(16px + env(safe-area-inset-bottom));' +
    'border-radius:14px 14px 0 0;display:grid;gap:10px}.fsheet b{font-size:17px}.fsheet button{min-height:48px;font-size:16px}' +
    '.fsheet .fmsg{font-size:14px;opacity:.75}' +
    '.fview{position:fixed;inset:0;z-index:10000;background:#fff;color:#111;display:flex;flex-direction:column}' +
    '.fview>div{display:flex;gap:8px;align-items:center;padding:calc(8px + env(safe-area-inset-top)) 12px 8px;' +
    'border-bottom:1px solid #ccc}.fview>div b{flex:1;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    '.fview button{min-height:44px;font-size:16px}.fview iframe{flex:1;border:0;width:100%}';
  document.head.append(css);

  async function fetchFile(href) {
    const r = await fetch(href);
    if (r.status === 401) { location.href = '/login'; throw new Error('not logged in'); }
    if (!r.ok) {
      let m = 'Could not get the file (' + r.status + ')';
      try { m = (await r.json()).error || m; } catch (e) { /* not JSON */ }
      throw new Error(m);
    }
    const name = ((/filename\*?=(?:UTF-8'')?"?([^";]+)/i.exec(r.headers.get('Content-Disposition') || '') || [])[1] || 'download').trim();
    const type = (r.headers.get('Content-Type') || '').split(';')[0] || 'application/octet-stream';
    const f = new File([await r.blob()], decodeURIComponent(name), {type});
    if (!navigator.canShare({files: [f]})) throw new Error('this phone cannot share that kind of file');
    return f;
  }

  async function shareFile(file, msg) {
    try { await navigator.share({files: [file]}); return true; }
    catch (e) { if (e.name !== 'AbortError' && msg) msg.textContent = 'Sharing did not work (' + e.message + ').'; }
    return false;
  }

  function viewer(url, label, filePromise) {
    const v = document.createElement('div'); v.className = 'fview';
    const bar = document.createElement('div');
    const t = document.createElement('b'); t.textContent = label;
    const sh = document.createElement('button'); sh.type = 'button'; sh.textContent = 'Share'; sh.disabled = true;
    const done = document.createElement('button'); done.type = 'button'; done.className = 'primary'; done.textContent = 'Close';
    done.onclick = () => v.remove();
    const f = document.createElement('iframe'); f.src = url; f.title = label;
    bar.append(t, sh, done); v.append(bar, f);
    document.body.append(v);
    if (!filePromise) { sh.hidden = true; return; }
    filePromise.then(file => { sh.disabled = false; sh.onclick = () => shareFile(file, null); })
               .catch(() => { sh.hidden = true; });
  }

  function panel(a, href, label) {
    const wrap = document.createElement('div'); wrap.className = 'fsheet';
    const box = document.createElement('div');
    const title = document.createElement('b'); title.textContent = label.charAt(0).toUpperCase() + label.slice(1);
    const msg = document.createElement('div'); msg.className = 'fmsg'; msg.textContent = 'Getting the file ready…';
    const isPdf = /\/api\/export\.pdf$/.test(new URL(href, location.href).pathname);
    const view = document.createElement('button'); view.type = 'button'; view.className = 'primary';
    view.textContent = 'View here'; view.hidden = !isPdf;
    const share = document.createElement('button'); share.type = 'button'; share.textContent = 'Preparing…'; share.disabled = true;
    if (!isPdf) share.className = 'primary';
    const cancel = document.createElement('button'); cancel.type = 'button'; cancel.textContent = 'Cancel';
    box.append(title, msg, view, share, cancel); wrap.append(box);
    const close = () => wrap.remove();
    cancel.onclick = close;
    wrap.onclick = e => { if (e.target === wrap) close(); };
    document.body.append(wrap);

    const canShare = !!navigator.canShare;
    const filePromise = canShare ? fetchFile(href) : null;
    view.onclick = () => {
      const u = new URL(href, location.href); u.searchParams.set('inline', '1');
      close(); viewer(u.toString(), title.textContent, filePromise);
    };
    if (!canShare) {
      share.hidden = true;
      msg.textContent = isPdf ? 'This phone cannot share files from here: use View here.' : 'This phone cannot share files from here.';
      return;
    }
    filePromise.then(file => {
      share.disabled = false; share.textContent = 'Share / Save to Files';
      msg.textContent = 'Ready: ' + file.name + ' (' + Math.max(1, Math.round(file.size / 1024)) + ' KB).';
      share.onclick = async () => { if (await shareFile(file, msg)) close(); };
    }).catch(e => {
      share.hidden = true;
      msg.textContent = 'Share is not available: ' + e.message + '.';
    });
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
