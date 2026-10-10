// "Import from iCloud" — a guided, multi-step import screen.
(function (N) {
  const I = (N.importer = {});
  const STEPS = ['welcome', 'signin', 'choose', 'import'];
  const STEP_LABELS = { welcome: 'Welcome', signin: 'Sign In', choose: 'Choose', import: 'Import' };

  let el = null;      // overlay element
  let st = null;      // view state

  const fresh = () => ({
    reconnect: false,
    step: 'welcome', waiting: false, notice: '', account: null, records: 0, plan: null,
    selected: new Set(), keepFolders: true, skipExisting: true, includeDeleted: false, sync: true,
    progress: { done: 0, total: 0, title: '' }, result: null, error: null, busy: false,
  });

  // ------------------------------------------------------------- open/close

  // opts.reconnect: only sign in again (e.g. sync lost its session); closes and syncs once signed in.
  I.open = function (opts = {}) {
    if (el) return;
    N.ui.closeTransient();
    st = fresh();
    st.reconnect = !!opts.reconnect;
    el = N.el('<div class="importer-backdrop"><div class="importer" role="dialog" aria-label="Import from iCloud"></div></div>');
    document.getElementById('overlay-root').appendChild(el);
    el.addEventListener('click', onClick);
    el.addEventListener('change', onChange);
    document.addEventListener('keydown', onKey, true);
    requestAnimationFrame(() => el.classList.add('open'));
    if (st.reconnect) { st.step = 'signin'; st.waiting = true; N.send('icloudStart'); }
    render();
  };

  function close() {
    if (!el) return;
    if (st.step === 'importing') N.send('icloudCancel');
    document.removeEventListener('keydown', onKey, true);
    const node = el;
    el = null;
    node.classList.remove('open');
    setTimeout(() => node.remove(), 200);
  }

  function onKey(e) {
    if (!el) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); if (st.step !== 'importing') close(); }
  }

  function go(step, patch = {}) {
    Object.assign(st, patch, { step });
    render();
  }

  // ----------------------------------------------------------------- events

  I.onEvent = function (ev) {
    if (!el) return;
    switch (ev.type) {
      case 'signin-needed':
        go('signin', { waiting: false, notice: ev.expired ? 'Your iCloud session has expired. Please sign in again.' : '' });
        break;
      case 'signin-closed':
        if (st.step === 'signin') go('signin', { waiting: false, notice: 'The sign-in window was closed before you signed in.' });
        break;
      case 'signed-in':
        if (st.reconnect) {
          // Back in: no need to walk through the import again.
          go('reconnected', { account: ev.account, waiting: false });
          N.send('icloudSyncNow');
          setTimeout(close, 1400);
          break;
        }
        go('scanning', { account: ev.account, waiting: false, notice: '', records: 0 });
        N.send('icloudScan');
        break;
      case 'scan-progress':
        st.records = ev.notes;
        if (st.step === 'scanning') {
          const c = el.querySelector('[data-records]');
          if (c) c.textContent = ev.notes.toLocaleString();
        }
        break;
      case 'scan-done':
        st.plan = ev;
        st.account = ev.account || st.account;
        st.selected = new Set(ev.folders.filter((f) => f.count > 0).map((f) => f.id));
        go('choose');
        break;
      case 'import-progress':
        st.progress = { done: ev.done, total: ev.total, title: ev.title };
        if (st.step !== 'importing') go('importing');
        else updateProgress();
        break;
      case 'import-done':
        go('done', { result: ev });
        N.app.reloadData();
        N.call('icloudSyncState').then((s) => N.sync.onStatus(s));
        break;
      case 'cancelled':
        if (st.step === 'scanning') go('welcome');
        break;
      case 'error':
        go('error', { error: ev.message, errorFrom: st.step });
        break;
      case 'signed-out':
        N.ui.toast('Signed out of iCloud');
        if (st.step !== 'done') go('welcome', { account: null, plan: null });
        break;
    }
  };

  // --------------------------------------------------------------- actions

  function onClick(e) {
    const b = e.target.closest('[data-do]');
    if (!b || b.disabled) return;
    const action = b.dataset.do;
    if (action === 'close') close();
    else if (action === 'continue') { go('signin', { waiting: true, notice: '' }); N.send('icloudStart'); }
    else if (action === 'open-signin') { st.waiting = true; st.notice = ''; render(); N.send('icloudSignIn'); }
    else if (action === 'back-welcome') { if (st.reconnect) close(); else go('welcome'); }
    else if (action === 'finish') { close(); if (N.sync.state && N.sync.state.state !== 'off') N.send('icloudSyncNow'); }
    else if (action === 'back-choose') go('choose');
    else if (action === 'cancel-scan') { N.send('icloudCancel'); go('welcome'); }
    else if (action === 'cancel-import') { N.send('icloudCancel'); b.disabled = true; b.textContent = 'Stopping…'; }
    else if (action === 'sign-out') N.send('icloudSignOut');
    else if (action === 'rescan') { go('scanning', { records: 0 }); N.send('icloudScan'); }
    else if (action === 'retry') {
      if (st.account) { go('scanning', { records: 0 }); N.send('icloudScan'); }
      else { go('signin', { waiting: true }); N.send('icloudStart'); }
    } else if (action === 'select-all') {
      const all = st.plan.folders.filter((f) => f.count > 0).map((f) => f.id);
      st.selected = st.selected.size === all.length ? new Set() : new Set(all);
      render();
    } else if (action === 'start-import') {
      go('importing', { progress: { done: 0, total: importCount(), title: '' } });
      N.send('icloudImport', {
        folders: Array.from(st.selected), keepFolders: st.keepFolders,
        skipExisting: st.skipExisting, includeDeleted: st.includeDeleted, sync: st.sync,
      });
    } else if (action === 'show-notes') {
      close();
      N.app.setScope({ type: 'all' });
    } else if (action === 'toggle-adp') {
      const box = el.querySelector('.imp-adp');
      box.classList.toggle('open');
    }
  }

  function onChange(e) {
    const t = e.target;
    if (t.dataset.folder) {
      if (t.checked) st.selected.add(t.dataset.folder); else st.selected.delete(t.dataset.folder);
    } else if (t.name === 'keepFolders') st.keepFolders = t.checked;
    else if (t.name === 'skipExisting') st.skipExisting = t.checked;
    else if (t.name === 'includeDeleted') st.includeDeleted = t.checked;
    else if (t.name === 'sync') st.sync = t.checked;
    render();
  }

  function importCount() {
    if (!st.plan) return 0;
    const key = st.skipExisting ? 'newCount' : 'count';
    let n = st.plan.folders.filter((f) => st.selected.has(f.id)).reduce((a, f) => a + (f[key] ?? f.count), 0);
    if (st.includeDeleted) n += st.plan.deleted;
    return n;
  }

  // ---------------------------------------------------------------- render

  const esc = N.esc;
  const tile = (icon, color) => `<span class="imp-tile" style="--tile:${color}">${N.icon(icon)}</span>`;

  function stepper() {
    if (st.reconnect) return '<div class="imp-steps"><div class="imp-step current"><span class="imp-step-dot">' + N.icon('cloud') + '</span><span>Reconnect to iCloud</span></div></div>';
    const map = { welcome: 0, signin: 1, scanning: 1, choose: 2, importing: 3, done: 3, error: -1 };
    const cur = map[st.step];
    return '<div class="imp-steps">' + STEPS.map((s, i) =>
      `<div class="imp-step${i < cur ? ' done' : ''}${i === cur ? ' current' : ''}">` +
      `<span class="imp-step-dot">${i < cur ? N.icon('check') : i + 1}</span><span>${STEP_LABELS[s]}</span></div>`)
      .join('<span class="imp-step-line"></span>') + '</div>';
  }

  function hero() {
    return `<div class="imp-hero"><span class="imp-cloud">${CLOUD}</span>` +
      `<span class="imp-arrow">${N.icon('chevronRight')}${N.icon('chevronRight')}</span>` +
      `<span class="imp-appicon">${N.icon('notesApp')}</span></div>`;
  }

  const CLOUD = '<svg viewBox="0 0 64 64" aria-hidden="true"><defs><linearGradient id="impcl" x1="0" y1="0" x2="0" y2="1">' +
    '<stop offset="0" stop-color="#5ac8fa"/><stop offset="1" stop-color="#0a84ff"/></linearGradient></defs>' +
    '<rect x="2" y="2" width="60" height="60" rx="14" fill="url(#impcl)"/>' +
    '<path d="M21 43h23a8 8 0 0 0 1.2-15.9A11 11 0 0 0 24.4 25 9 9 0 0 0 21 43z" fill="#fff"/></svg>';

  const VIEWS = {
    welcome: () => `
      ${hero()}
      <h1 class="imp-title">Import your iCloud Notes</h1>
      <p class="imp-sub">Bring your notes, folders, checklists and tables from iCloud to this computer.</p>
      <div class="imp-features">
        <div class="imp-feature">${tile('folder', '#f5b800')}<div><b>Folders and notes</b><span>Your folder structure comes along, with the original dates.</span></div></div>
        <div class="imp-feature">${tile('checklist', '#30c759')}<div><b>Formatting, checklists and tables</b><span>Titles, lists, ticked items and tables are kept.</span></div></div>
        <div class="imp-feature">${tile('photo', '#0a84ff')}<div><b>Images and attachments</b><span>Photos, sketches and files are saved with your notes.</span></div></div>
      </div>
      <div class="imp-privacy">${N.icon('lock')}<span>You sign in on Apple's own page. Notes never sees or stores your password, and your notes are copied straight to this computer.</span></div>
      <button class="imp-link" data-do="toggle-adp">Using Advanced Data Protection?</button>
      <div class="imp-adp">With Advanced Data Protection, iCloud.com can't open your notes by default. On your iPhone, go to
        <b>Settings → [your name] → iCloud</b> and turn on <b>Access iCloud Data on the Web</b>, then come back here.</div>`,

    signin: () => `
      <div class="imp-icon-big">${N.icon('lockLarge')}</div>
      <h1 class="imp-title">${st.reconnect ? 'Sign in again to keep syncing' : 'Sign in to iCloud'}</h1>
      <p class="imp-sub">${st.reconnect ? 'Your iCloud session ended. Sign in once more and syncing continues right away.' : 'A secure Apple sign-in window opens next to this one.'}</p>
      <ol class="imp-howto">
        <li><span>1</span><div>Click <b>Open Apple Sign-In</b>.</div></li>
        <li><span>2</span><div>Sign in with your Apple ID, tick <b>Keep me signed in</b>, and enter the code that appears on your iPhone or Mac.</div></li>
        <li><span>3</span><div>If Apple asks whether to trust this browser, choose <b>Trust</b> — you won't need a code next time.</div></li>
        <li><span>4</span><div>That's it. This screen continues on its own as soon as you're signed in.</div></li>
      </ol>
      ${st.notice ? `<div class="imp-notice">${esc(st.notice)}</div>` : ''}
      <div class="imp-status${st.waiting ? ' active' : ''}">${st.waiting ? '<span class="imp-spinner"></span>Waiting for you to sign in…' : ''}</div>`,

    scanning: () => `
      <div class="imp-center">
        <div class="imp-spinner big"></div>
        <h1 class="imp-title">Looking for your notes…</h1>
        <p class="imp-sub">${st.account ? `Signed in as <b>${esc(st.account.name || st.account.appleId)}</b>. ` : ''}This can take a minute if you have many notes.</p>
        <div class="imp-counter"><span data-records>${st.records.toLocaleString()}</span> notes found so far</div>
      </div>`,

    choose: () => {
      const p = st.plan;
      const kids = new Map();
      p.folders.forEach((f) => { const k = f.parentId || ''; if (!kids.has(k)) kids.set(k, []); kids.get(k).push(f); });
      kids.forEach((list) => list.sort((a, b) => (b.id.startsWith('DefaultFolder') - a.id.startsWith('DefaultFolder')) || a.name.localeCompare(b.name)));
      let rows = '';
      const walk = (parent, depth) => (kids.get(parent) || []).forEach((f) => {
        rows += `<label class="imp-folder${f.count ? '' : ' empty'}" style="--depth:${depth}">` +
          `<input type="checkbox" data-folder="${esc(f.id)}"${st.selected.has(f.id) ? ' checked' : ''}${f.count ? '' : ' disabled'}>` +
          `<span class="imp-folder-icon">${N.icon('folder')}</span><span class="imp-folder-name">${esc(f.name)}</span>` +
          `<span class="imp-folder-count">${f.count}</span></label>`;
        walk(f.id, depth + 1);
      });
      walk('', 0);
      const allCount = p.folders.filter((f) => f.count > 0).length;
      const n = importCount();
      return `
        <div class="imp-account">${tile('allNotes', '#0a84ff')}<div><b>${esc(st.account && (st.account.name || st.account.appleId) || 'iCloud')}</b>
          <span>${esc(st.account && st.account.appleId || '')}</span></div>
          <button class="imp-link" data-do="sign-out">Sign Out</button></div>
        <div class="imp-choose-head"><h2>Choose folders</h2><span>${p.total} notes in ${p.folders.length} ${p.folders.length === 1 ? 'folder' : 'folders'}</span>
          <button class="imp-link" data-do="select-all">${st.selected.size === allCount ? 'Deselect All' : 'Select All'}</button></div>
        <div class="imp-folders">${rows || '<div class="imp-empty">No notes found in iCloud.</div>'}</div>
        <div class="imp-options">
          <label><input type="checkbox" name="sync"${st.sync ? ' checked' : ''}><span>Keep in sync with iCloud<small>New and changed notes in these folders are downloaded automatically. Nothing is sent to iCloud.</small></span></label>
          <label><input type="checkbox" name="keepFolders"${st.keepFolders ? ' checked' : ''}><span>Keep my iCloud folders<small>Otherwise everything goes into one folder called “iCloud”.</small></span></label>
          <label><input type="checkbox" name="skipExisting"${st.skipExisting ? ' checked' : ''}><span>Skip notes I've already imported<small>${p.alreadyImported ? `${p.alreadyImported} already here — changed ones are updated. Notes in Recently Deleted are restored.` : 'Changed notes are updated instead of duplicated.'}</small></span></label>
          ${p.deleted ? `<label><input type="checkbox" name="includeDeleted"${st.includeDeleted ? ' checked' : ''}><span>Also import Recently Deleted<small>${p.deleted} ${p.deleted === 1 ? 'note goes' : 'notes go'} to Recently Deleted here too.</small></span></label>` : ''}
        </div>
        ${p.locked ? `<div class="imp-notice">${N.icon('lock')} ${p.locked} locked ${p.locked === 1 ? 'note is' : 'notes are'} skipped. Remove the lock on your iPhone to import ${p.locked === 1 ? 'it' : 'them'}.</div>` : ''}
        ${n ? '' : `<div class="imp-uptodate">${N.icon('check')} Everything in these folders is already on this computer${st.sync ? ' — sync keeps it up to date' : ''}.</div>`}
        <div data-count hidden>${n}</div>`;
    },

    importing: () => {
      const { done, total, title } = st.progress;
      const pct = total ? Math.round((done / total) * 100) : 0;
      return `
        <div class="imp-center">
          <div class="imp-ring"><svg viewBox="0 0 120 120"><circle class="track" cx="60" cy="60" r="52"/>
            <circle class="bar" cx="60" cy="60" r="52" style="stroke-dashoffset:${326.7 * (1 - pct / 100)}"/></svg>
            <span data-pct>${pct}%</span></div>
          <h1 class="imp-title">Importing your notes…</h1>
          <p class="imp-sub" data-progress>${done} of ${total}</p>
          <p class="imp-current" data-current>${esc(title)}</p>
        </div>`;
    },

    done: () => {
      const r = st.result || { stats: {} };
      const s = r.stats || {};
      const tiles = [['Notes imported', s.notes], ['Updated', s.updated], ['Tables', s.tables], ['Attachments', s.attachments]]
        .filter(([, v], i) => i === 0 || v)
        .map(([label, v]) => `<div class="imp-stat"><b>${(v || 0).toLocaleString()}</b><span>${label}</span></div>`).join('');
      const issues = [];
      if (s.failed) issues.push(`${s.failed} ${s.failed === 1 ? 'note' : 'notes'} couldn't be read.`);
      if (s.skippedAttachments) issues.push(`${s.skippedAttachments} ${s.skippedAttachments === 1 ? 'attachment' : 'attachments'} couldn't be imported; ${s.skippedAttachments === 1 ? 'it is' : 'they are'} marked in the note.`);
      if (st.plan && st.plan.locked) issues.push(`${st.plan.locked} locked ${st.plan.locked === 1 ? 'note was' : 'notes were'} skipped.`);
      return `
        <div class="imp-center">
          <div class="imp-check${r.cancelled ? ' stopped' : ''}">${N.icon(r.cancelled ? 'xCircle' : 'check')}</div>
          <h1 class="imp-title">${r.cancelled ? 'Import stopped' : 'Your notes are here'}</h1>
          <p class="imp-sub">${r.cancelled ? 'Everything imported so far has been kept.' : 'Your iCloud notes are now stored on this computer.'}</p>
          <div class="imp-stats">${tiles}</div>
          ${issues.length ? `<ul class="imp-issues">${issues.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
        </div>`;
    },

    reconnected: () => `
      <div class="imp-center">
        <div class="imp-check">${N.icon('check')}</div>
        <h1 class="imp-title">You're signed in again</h1>
        <p class="imp-sub">Syncing with iCloud now.</p>
      </div>`,

    error: () => `
      <div class="imp-center">
        <div class="imp-check error">!</div>
        <h1 class="imp-title">Couldn't import</h1>
        <p class="imp-sub">${esc(st.error || 'Something went wrong.')}</p>
      </div>`,
  };

  const FOOTERS = {
    welcome: () => '<button class="push-btn" data-do="close">Cancel</button><button class="push-btn primary" data-do="continue">Continue</button>',
    signin: () => `<button class="push-btn" data-do="back-welcome">${st.reconnect ? 'Cancel' : 'Back'}</button><button class="push-btn primary" data-do="open-signin">Open Apple Sign-In</button>`,
    reconnected: () => '<button class="push-btn primary" data-do="close">Done</button>',
    scanning: () => '<button class="push-btn" data-do="cancel-scan">Cancel</button>',
    choose: () => {
      const n = importCount();
      return '<button class="push-btn" data-do="close">Cancel</button>' +
        (n ? `<button class="push-btn primary" data-do="start-import">Import ${n} ${n === 1 ? 'Note' : 'Notes'}</button>`
          : '<button class="push-btn primary" data-do="finish">Done</button>');
    },
    importing: () => '<button class="push-btn" data-do="cancel-import">Stop</button>',
    done: () => '<button class="push-btn imp-left" data-do="sign-out">Sign Out of iCloud</button><button class="push-btn primary" data-do="show-notes">Show My Notes</button>',
    error: () => '<button class="push-btn" data-do="close">Close</button><button class="push-btn primary" data-do="retry">Try Again</button>',
  };

  function render() {
    if (!el) return;
    const card = el.querySelector('.importer');
    const scroll = card.querySelector('.imp-body');
    const top = scroll ? scroll.scrollTop : 0;
    card.className = `importer step-${st.step}`;
    card.innerHTML =
      `<button class="imp-close" data-do="close" title="Close">${N.icon('xCircle')}</button>` +
      `${stepper()}<div class="imp-body">${VIEWS[st.step]()}</div>` +
      `<div class="imp-footer">${FOOTERS[st.step]()}</div>`;
    const body = card.querySelector('.imp-body');
    if (body && st.step === 'choose') body.scrollTop = top;
  }

  function updateProgress() {
    const { done, total, title } = st.progress;
    const pct = total ? Math.round((done / total) * 100) : 0;
    const bar = el.querySelector('.imp-ring .bar');
    if (!bar) { render(); return; }
    bar.style.strokeDashoffset = 326.7 * (1 - pct / 100);
    el.querySelector('[data-pct]').textContent = pct + '%';
    el.querySelector('[data-progress]').textContent = `${done} of ${total}`;
    el.querySelector('[data-current]').textContent = title;
  }
})(window.N = window.N || {});
