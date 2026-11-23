// Home-screen (standalone) mode on iPhone: opening a file there shows it with no way back and no Save, so
// links marked data-file="PDF" go to the share sheet instead (Save to Files, Mail, AirDrop...). iOS only
// opens the sheet from a fresh tap, so it takes two: the first tap prepares the file, the second shares it.
// Everywhere else (Safari, desktop) the link downloads as normal.
(function () {
  const standalone = navigator.standalone === true || matchMedia('(display-mode: standalone)').matches;
  if (!standalone || !navigator.canShare) return;
  document.addEventListener('click', async ev => {
    const a = ev.target.closest && ev.target.closest('a[data-file]');
    if (!a) return;
    const href = a.getAttribute('href');
    if (!href) return;
    ev.preventDefault();
    if (a.dataset.busy) return;
    const label = a.dataset.file;
    if (a._file && a._for === href) {
      try { await navigator.share({files: [a._file]}); }
      catch (e) { if (e.name !== 'AbortError') alert(e.message); }
      return;
    }
    a._orig = a._orig || a.textContent;
    a.dataset.busy = '1'; a.textContent = 'Preparing…';
    try {
      const r = await fetch(href);
      if (r.status === 401) { location.href = '/login'; return; }
      if (!r.ok) {
        let m = 'Could not get the file (' + r.status + ')';
        try { m = (await r.json()).error || m; } catch (e) { /* not JSON */ }
        throw new Error(m);
      }
      const name = ((/filename="?([^";]+)/.exec(r.headers.get('Content-Disposition') || '') || [])[1] || 'download').trim();
      const file = new File([await r.blob()], name, {type: (r.headers.get('Content-Type') || '').split(';')[0] || 'application/octet-stream'});
      if (!navigator.canShare({files: [file]})) throw new Error('This phone cannot share that kind of file.');
      a._file = file; a._for = href; a.textContent = 'Share ' + label + ' ›';
    } catch (e) { alert(e.message); a.textContent = a._orig; }
    finally { delete a.dataset.busy; }
  });
  // a link whose target changes (new date, other sections) must prepare again
  window.resetFileLink = a => { a._file = null; if (a._orig) a.textContent = a._orig; };
})();
