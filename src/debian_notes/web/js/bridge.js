// JS <-> Python bridge. N.call(method, ...args) returns a Promise resolved by Python.
(function (N) {
  const pending = new Map();
  let seq = 0;

  N.call = function (method, ...args) {
    return new Promise((resolve, reject) => {
      const id = ++seq;
      pending.set(id, { resolve, reject, method });
      window.webkit.messageHandlers.bridge.postMessage(JSON.stringify({ id, method, args }));
    });
  };

  // Fire-and-forget variant for calls whose result we don't need.
  N.send = function (method, ...args) {
    window.webkit.messageHandlers.bridge.postMessage(JSON.stringify({ id: null, method, args }));
  };

  N._resolve = function (id, ok, payload) {
    const p = pending.get(id);
    if (!p) return;
    pending.delete(id);
    if (ok) p.resolve(payload);
    else {
      const err = new Error(payload);
      err.method = p.method;
      p.reject(err);
    }
  };

  window.addEventListener('error', (e) => N.send('log', 'error:', e.message, e.filename + ':' + e.lineno));
  window.addEventListener('unhandledrejection', (e) =>
    N.send('log', 'unhandled rejection:', String(e.reason && (e.reason.stack || e.reason.message) || e.reason)));
})(window.N = window.N || {});
