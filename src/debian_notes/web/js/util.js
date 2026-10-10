// Small DOM, string and date helpers.
(function (N) {
  N.$ = (sel, root = document) => root.querySelector(sel);
  N.$$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  N.esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  N.el = function (html) {
    const t = document.createElement('template');
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  };

  N.debounce = function (fn, ms) {
    let t = null;
    const d = (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
    d.cancel = () => clearTimeout(t);
    return d;
  };

  N.clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  N.isMod = (e) => e.ctrlKey || e.metaKey;

  N.fileToBase64 = (file) => new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(',', 2)[1] || '');
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });

  N.formatSize = function (bytes) {
    if (bytes < 1024) return bytes + ' bytes';
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + ' KB';
    return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  };

  // ------------------------------------------------------------------ dates

  const DAY = 86400000;
  const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const timeFmt = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
  const weekdayFmt = new Intl.DateTimeFormat(undefined, { weekday: 'long' });
  const shortFmt = new Intl.DateTimeFormat(undefined, { day: '2-digit', month: '2-digit', year: 'numeric' });
  const monthFmt = new Intl.DateTimeFormat(undefined, { month: 'long' });
  const longFmt = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'long', year: 'numeric' });

  N.date = {
    // "15:43", "Yesterday", "Tuesday", "03/09/2026"
    list(ts) {
      const d = new Date(ts * 1000);
      const today = startOfDay(new Date());
      const t = d.getTime();
      if (t >= today) return timeFmt.format(d);
      if (t >= today - DAY) return 'Yesterday';
      if (t >= today - 6 * DAY) return weekdayFmt.format(d);
      return shortFmt.format(d);
    },
    // "10 October 2026 at 15:43"
    long(ts) {
      const d = new Date(ts * 1000);
      return longFmt.format(d) + ' at ' + timeFmt.format(d);
    },
    group(ts) {
      const d = new Date(ts * 1000);
      const now = new Date();
      const today = startOfDay(now);
      const t = d.getTime();
      if (t >= today) return 'Today';
      if (t >= today - DAY) return 'Yesterday';
      if (t >= today - 7 * DAY) return 'Previous 7 Days';
      if (t >= today - 30 * DAY) return 'Previous 30 Days';
      if (d.getFullYear() === now.getFullYear()) return monthFmt.format(d);
      return String(d.getFullYear());
    },
  };
})(window.N = window.N || {});
