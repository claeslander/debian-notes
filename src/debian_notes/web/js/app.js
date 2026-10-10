// Application controller: state, navigation, toolbar actions, menus, locking, settings.
(function (N) {
  const S = (N.state = {
    folders: [],
    notes: new Map(),
    settings: {},
    defaultFolderId: null,
    scope: { type: 'all' },
    currentNoteId: null,
    view: 'list',
    galleryOpen: false,
    search: '',
    searchIds: null,
    systemTheme: 'light',
    lock: { configured: false, unlocked: false, hint: '' },
    version: '',
  });
  const A = (N.app = {});
  const body = document.body;

  // ------------------------------------------------------------- settings

  A.setSetting = function (key, value) {
    S.settings[key] = value;
    N.send('setSetting', key, value);
  };

  function resolvedTheme() {
    const a = S.settings.appearance || 'system';
    return a === 'system' ? S.systemTheme : a;
  }

  function applyTheme() {
    const t = resolvedTheme();
    document.documentElement.dataset.theme = t;
    N.send('setTheme', t);
  }

  function applyTextSize() {
    document.documentElement.style.setProperty('--editor-size', (S.settings.textSize || 15) + 'px');
  }

  // Sidebar transparency: 0 (off) .. 0.2. Older setting `translucentSidebar: false` means off.
  function sidebarGlass() {
    if (S.settings.sidebarGlass !== undefined) return +S.settings.sidebarGlass || 0;
    return S.settings.translucentSidebar === false ? 0 : 0.04;
  }

  function applyLayout() {
    const app = document.getElementById('app');
    app.style.setProperty('--sidebar-w', (S.settings.sidebarWidth || 220) + 'px');
    app.style.setProperty('--list-w', (S.settings.listWidth || 300) + 'px');
    body.classList.toggle('sidebar-hidden', S.settings.sidebarVisible === false);
    const glass = sidebarGlass();
    body.classList.toggle('glass', glass > 0);
    document.documentElement.style.setProperty('--glass', String(glass));
    body.classList.toggle('view-gallery', S.view === 'gallery');
    body.classList.toggle('gallery-open', S.view === 'gallery' && S.galleryOpen);
    N.$('[data-action="viewList"]').classList.toggle('active', S.view === 'list');
    N.$('[data-action="viewGallery"]').classList.toggle('active', S.view === 'gallery');
    updateDragRegionsSoon();
  }

  N.onSystemTheme = (t) => { S.systemTheme = t; applyTheme(); };
  N.onWindowActive = (active) => body.classList.toggle('window-inactive', !active);
  N.onWindowState = (st) => { body.classList.toggle('maximized', !!st.maximized); updateDragRegionsSoon(); };

  // ------------------------------------------------------------ rendering

  // Re-read folders and notes from disk (after an import).
  A.reloadData = async function (opts = {}) {
    const data = await N.call('bootstrap');
    S.folders = data.folders;
    S.notes = new Map(data.notes.map((n) => [n.id, n]));
    A.refresh();
    N.notelist.updateSelection();
    if (S.currentNoteId != null && !S.notes.has(S.currentNoteId)) {
      await A.selectNote(N.notelist.order[0] ?? null, { force: true });
    } else if (opts.changed && opts.changed.includes(S.currentNoteId) && !N.editor.isDirty()) {
      await openNote(S.currentNoteId);  // updated from iCloud while open and not being edited
    }
  };

  // ------------------------------------------------- iCloud test push

  const pushEnabled = () => !!S.settings.icloudTestPush;
  let pushed = new Set();

  let tracked = new Set();  // local notes that mirror an iCloud note

  // Pull: download changes from iCloud. Nothing is sent.
  A.pull = function () { N.send('icloudSyncNow'); };

  // Push: send local edits of iCloud notes and new notes in iCloud folders, after confirming what goes out.
  A.push = async function () {
    if (!pushEnabled()) return;
    await N.editor.flush();
    const c = await N.call('icloudSyncCandidates');
    if (!c.edited.length && !c.new.length && !c.deleted.length) { N.ui.toast('Nothing to push — iCloud is up to date with your changes'); return; }
    const titles = (list) => list.map((n) => '• ' + ((S.notes.get(n.id) || {}).title || 'New Note')).join('\n');
    const parts = [];
    if (c.new.length) parts.push(`${c.new.length} new ${c.new.length === 1 ? 'note' : 'notes'}:\n${titles(c.new)}`);
    if (c.edited.length) parts.push(`${c.edited.length} changed ${c.edited.length === 1 ? 'note' : 'notes'}:\n${titles(c.edited)}`);
    if (c.deleted.length) parts.push(`${c.deleted.length} deleted ${c.deleted.length === 1 ? 'note' : 'notes'} (to Recently Deleted in iCloud):\n` +
      c.deleted.map((d) => '• ' + (d.title || 'Untitled')).join('\n'));
    const r = await N.ui.sheet({
      icon: 'cloudUp', title: 'Push to iCloud?', wide: true,
      message: parts.join('\n\n') + (c.edited.length ? '\n\nThe current iCloud version of each changed note is saved in Version History first.' : ''),
      buttons: [{ label: 'Cancel', value: null }, { label: 'Push', value: 'ok', primary: true }],
    });
    if (r.button !== 'ok') return;
    const scratch = document.createElement('div');
    const toParas = (body) => { scratch.innerHTML = body; return N.convert.toAppleParagraphs(scratch); };
    N.send('icloudPushNow', {
      edited: c.edited.map((n) => ({ id: n.id, paragraphs: toParas(n.body) })),
      new: c.new.map((n) => ({ id: n.id, folder: n.folder, paragraphs: toParas(n.body) })),
      deleted: c.deleted.map((d) => d.id),
    });
  };

  A.refreshTracked = () => N.call('icloudTrackedIds').then((ids) => { tracked = new Set(ids); }).catch(() => {});

  function pushMenuItems(id = S.currentNoteId) {
    const n = id != null ? S.notes.get(id) : null;
    if (!n || n.deletedAt || n.locked) return [];
    const items = ['-', { label: 'Version History…', icon: 'undo', action: () => showVersions(id) }];
    if (!pushEnabled()) return items;
    if (tracked.has(id) || pushed.has(id)) items.push({ label: 'Send Changes to iCloud (Test)…', icon: 'cloud', action: () => pushChanges(id) });
    else items.push({ label: 'Send to iCloud (Test)…', icon: 'cloud', action: () => pushNote(id) });
    return items;
  }

  async function pushChanges(id) {
    if (S.currentNoteId !== id) await A.selectNote(id);
    await N.editor.flush();
    const root = N.editor.root();
    if (!root) return;
    const ok = await N.ui.confirm('Send your changes to iCloud?',
      'Experimental: your edits are applied to this note in iCloud, the same way an iPhone would. ' +
      'The current iCloud version is saved in Version History first, so you can always go back.', 'Send Changes');
    if (!ok) return;
    N.ui.toast('Sending changes to iCloud…');
    N.send('icloudPushChanges', id, { paragraphs: N.convert.toAppleParagraphs(root) });
  }

  // ------------------------------------------------------------ version history

  const REASONS = {
    edit: 'Edited here', 'before-push': 'iCloud version before sending', pushed: 'Sent to iCloud',
    'before-sync': 'Before an iCloud update', 'before-restore': 'Before restoring',
  };

  async function showVersions(id) {
    if (S.currentNoteId !== id) await A.selectNote(id);
    await N.editor.flush();
    const list = await N.call('noteVersions', id);
    if (!list.length) {
      N.ui.alert('No earlier versions yet', 'Versions are saved automatically while you edit (at most every 10 minutes), and before any change from or to iCloud.');
      return;
    }
    const fmt = (ts) => N.date.long(ts);
    let selected = list[0].id;
    const html = `<div class="versions">
      <div class="versions-list">${list.map((v) => `<button class="version-row" data-v="${v.id}">
        <b>${N.esc(fmt(v.createdAt))}</b><span>${N.esc(REASONS[v.reason] || v.reason)}</span></button>`).join('')}</div>
      <div class="versions-preview editor"></div></div>`;
    const res = await N.ui.sheet({
      title: 'Version History', icon: false, wide: true, html,
      buttons: [{ label: 'Close', value: null }, { label: 'Restore This Version', value: 'restore', primary: true }],
      onOpen(sheet) {
        sheet.classList.add('versions-sheet');
        const preview = sheet.querySelector('.versions-preview');
        const pick = async (vid) => {
          selected = vid;
          sheet.querySelectorAll('.version-row').forEach((b) => b.classList.toggle('active', +b.dataset.v === vid));
          preview.innerHTML = (await N.call('noteVersion', vid)).body;
          preview.scrollTop = 0;
        };
        sheet.querySelector('.versions-list').addEventListener('click', (e) => {
          const b = e.target.closest('[data-v]');
          if (b) pick(+b.dataset.v);
        });
        pick(selected);
      },
    });
    if (res.button !== 'restore') return;
    const v = await N.call('noteVersion', selected);
    await N.call('snapshotNote', id, 'before-restore');
    const n = S.notes.get(id);
    N.editor.mount(n, v.body, { readOnly: false });
    N.editor.changed();
    await N.editor.flush();
    N.ui.toast('Version restored');
  }

  async function pushNote(id) {
    if (S.currentNoteId !== id) await A.selectNote(id);
    await N.editor.flush();
    const root = N.editor.root();
    if (!root) return;
    const ok = await N.ui.confirm('Send this note to iCloud?',
      'Experimental: a new note is created in the “From Debian” folder in iCloud. Your other iCloud notes are not touched. ' +
      'Check it on your iPhone or Mac afterwards.', 'Send to iCloud');
    if (!ok) return;
    N.ui.toast('Sending to iCloud…');
    N.send('icloudPushNote', id, { paragraphs: N.convert.toAppleParagraphs(root) });
  }

  N.sync = N.sync || {};
  N.sync.onPush = function (res) {
    if (res.removed !== undefined || (res.noteId == null && res.ok)) {
      pushed = new Set();
      N.ui.alert('Test notes removed', `${res.removed || 0} test ${res.removed === 1 ? 'note was' : 'notes were'} deleted from iCloud. The copies on this computer are kept.`);
      return;
    }
    if (!res.ok) {
      if (res.signin) { N.ui.toast('Sign in to iCloud first'); N.importer.open({ reconnect: true }); return; }
      N.ui.alert('Couldn’t send to iCloud', res.message || 'Something went wrong.');
      return;
    }
    if (res.unchanged) { N.ui.toast('Nothing to send — iCloud already has this version'); return; }
    if (res.trashed !== undefined) {
      N.ui.toast(res.trashed ? `Moved ${res.trashed === 1 ? 'the note' : res.trashed + ' notes'} to Recently Deleted in iCloud` : 'Already deleted in iCloud');
      A.refreshTracked();
      return;
    }
    if (res.updated) {
      N.ui.alert('Changes sent to iCloud',
        (res.verified ? 'iCloud stored exactly your text. ' : 'iCloud accepted the changes, but reading them back didn’t match — please check the note on your iPhone. ') +
        'The previous iCloud version is in Version History if you need it.');
      return;
    }
    pushed.add(res.noteId);
    A.refreshTracked();
    N.ui.alert('Sent to iCloud',
      `The note was created in the “${res.folder}” folder.` +
      (res.verified ? ' iCloud returned exactly the same text.' : ' iCloud accepted it, but reading it back didn’t match — please check it on your iPhone.') +
      ' Open Notes on your iPhone or Mac to check that it looks right.');
  };

  async function enableTestPush(on) {
    if (!on) { A.setSetting('icloudTestPush', false); return true; }
    const r = await N.ui.sheet({
      icon: 'cloudAlert', title: 'Enable test push to iCloud?',
      message: 'This experimental feature creates new notes in your iCloud account, in a folder called “From Debian”. ' +
        'It never edits or deletes your existing iCloud notes, but the format is reverse-engineered: a note could look wrong ' +
        'or fail to open on your Apple devices. Trying it with a separate test Apple ID first is strongly recommended. ' +
        'You can remove all test notes again from Settings.',
      buttons: [{ label: 'Cancel', value: null }, { label: 'Enable', value: 'ok', primary: true }],
    });
    if (r.button !== 'ok') return false;
    A.setSetting('icloudTestPush', true);
    if (S.settings['icloud.pushSince'] === undefined) A.setSetting('icloud.pushSince', Date.now() / 1000);
    return true;
  }

  // ------------------------------------------------------------ iCloud sync

  const SYNC_ICON = { idle: 'cloud', syncing: 'sync', pushing: 'sync', 'signin-needed': 'cloudAlert', error: 'cloudAlert', offline: 'cloudAlert' };
  N.sync = Object.assign(N.sync || {}, {
    state: { state: 'off' },
    onStatus(st) {
      const prev = N.sync.state;
      N.sync.state = st;
      renderSyncStatus();
      const c = st.changes;
      if (st.state === 'idle' && (prev.state === 'syncing' || prev.state === 'pushing') && c && (c.added || c.updated || c.removed || c.moved || c.conflicts || c.sent || c.created || c.deleted || c.skipped || (c.changed || []).length)) {
        A.reloadData({ changed: c.changed || [] });
        A.refreshTracked();
        const parts = [];
        if (c.added) parts.push(`${c.added} new`);
        if (c.updated) parts.push(`${c.updated} updated`);
        if (c.removed) parts.push(`${c.removed} removed`);
        if (c.conflicts) parts.push(`${c.conflicts} kept as copies`);
        if (c.sent) parts.push(`${c.sent} sent`);
        if (c.created) parts.push(`${c.created} created in iCloud`);
        if (c.deleted) parts.push(`${c.deleted} deleted in iCloud`);
        if (c.skipped) parts.push(`${c.skipped} couldn't be sent`);
        if (c.created || c.sent) A.refreshTracked();
        if (parts.length) N.ui.toast(`iCloud: ${parts.join(', ')}`);
      }
    },
  });

  function relTime(ts) {
    if (!ts) return '';
    const s = Date.now() / 1000 - ts;
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return N.date.list(ts);
  }

  function renderSyncStatus() {
    const bar = document.getElementById('sync-bar');
    const el = document.getElementById('sync-status');
    const st = N.sync.state;
    if (!st || st.state === 'off' || st.enabled === false) { bar.hidden = true; return; }
    bar.hidden = false;
    const text = {
      idle: st.last ? `Synced ${relTime(st.last)}` : 'iCloud',
      syncing: 'Pulling from iCloud…',
      pushing: 'Pushing to iCloud…',
      'signin-needed': 'Sign in to keep syncing',
      offline: 'Offline',
      error: 'Sync problem',
    }[st.state] || '';
    bar.className = 'sync-bar state-' + st.state;
    el.title = st.message || '';
    el.innerHTML = `<span class="sync-icon">${N.icon(SYNC_ICON[st.state] || 'cloud')}</span><span>${N.esc(text)}</span>`;
    const pull = bar.querySelector('[data-sync="pull"]');
    const push = bar.querySelector('[data-sync="push"]');
    pull.innerHTML = N.icon('cloudDown') + '<span>Pull</span>';
    push.innerHTML = N.icon('cloudUp') + '<span>Push</span>';
    push.hidden = !pushEnabled();
    const busy = st.state === 'syncing' || st.state === 'pushing';
    pull.disabled = push.disabled = busy || st.state === 'signin-needed';
    if (st.state === 'signin-needed') el.title = 'Click to sign in again';
  }
  setInterval(renderSyncStatus, 30000);

  A.refresh = function () {
    N.sidebar.render();
    N.notelist.render();
    updateToolbar();
  };

  function currentNote() {
    return S.currentNoteId != null ? S.notes.get(S.currentNoteId) : null;
  }

  function updateToolbar() {
    const n = currentNote();
    const editable = !!n && !n.deletedAt && N.editor.isMounted() && !N.editor.isReadOnly();
    N.$$('[data-needs-note]').forEach((b) => { b.disabled = !editable; });
    N.$$('[data-needs-note-any]').forEach((b) => { b.disabled = !n || !!n.deletedAt; });
    N.$('[data-action="deleteNote"]').disabled = !n;
    const lockBtn = N.$('[data-action="lockMenu"]');
    const icon = n && n.locked ? (S.lock.unlocked ? 'lockOpen' : 'lock') : 'lock';
    lockBtn.dataset.icon = icon;
    lockBtn.classList.toggle('is-locked', !!(n && n.locked));
    N.renderIcons(lockBtn.parentElement);
  }

  function showView(which) {
    document.getElementById('editor-scroll').hidden = which !== 'editor';
    document.getElementById('locked-view').hidden = which !== 'locked';
    document.getElementById('empty-view').hidden = which !== 'empty';
  }

  // ----------------------------------------------------------- navigation

  A.setScope = async function (scope, opts = {}) {
    if (S.search) clearSearch(false);
    S.scope = scope;
    A.setSetting('lastScope', N.sidebar.scopeKey(scope));
    if (S.view === 'gallery' && S.galleryOpen) { S.galleryOpen = false; applyLayout(); }
    N.sidebar.render();
    N.notelist.render();
    if (opts.keepNote && N.notelist.order.includes(S.currentNoteId)) return;
    const first = N.notelist.order[0];
    await A.selectNote(first ?? null);
  };

  async function leaveCurrent() {
    const prevId = N.editor.noteId();
    if (prevId == null) return;
    const prev = S.notes.get(prevId);
    const discard = prev && !prev.deletedAt && !prev.locked && N.editor.isEmpty();
    if (discard) {
      N.editor.unmount();
      S.notes.delete(prevId);
      await N.call('deleteNote', prevId, true);
      N.sidebar.render();
      N.notelist.render();
    } else {
      await N.editor.flush();
    }
  }

  A.selectNote = async function (id, opts = {}) {
    if (id === S.currentNoteId && !opts.force && N.editor.noteId() === id) {
      if (opts.focus) N.editor.focus();
      return;
    }
    await leaveCurrent();
    S.currentNoteId = id;
    N.notelist.updateSelection();
    A.setSetting('lastNote', id);
    await openNote(id, opts);
  };

  async function openNote(id, opts = {}) {
    const n = id != null ? S.notes.get(id) : null;
    document.getElementById('trash-banner').hidden = true;
    if (!n) {
      N.editor.unmount();
      showView('empty');
      updateToolbar();
      return;
    }
    document.getElementById('note-date').textContent = N.date.long(n.updatedAt);
    if (n.locked && !S.lock.unlocked) {
      N.editor.unmount();
      showLockedView();
      updateToolbar();
      return;
    }
    let content = '';
    try {
      content = (await N.call('getNote', id)).body;
    } catch (err) {
      if (err.message === 'locked') {
        S.lock.unlocked = false;
        N.editor.unmount();
        showLockedView();
        updateToolbar();
        return;
      }
      throw err;
    }
    if (S.currentNoteId !== id) return;
    N.editor.mount(n, content, { readOnly: !!n.deletedAt, focus: opts.focus, caret: opts.caret });
    document.getElementById('trash-banner').hidden = !n.deletedAt;
    showView('editor');
    document.getElementById('editor-scroll').scrollTop = 0;
    if (S.search) N.editor.refreshHighlights(true);
    updateToolbar();
  }

  A.noteSaved = function (meta) {
    const old = S.notes.get(meta.id);
    S.notes.set(meta.id, meta);
    if (meta.id === S.currentNoteId) document.getElementById('note-date').textContent = N.date.long(meta.updatedAt);
    N.notelist.render();
    N.notelist.updateSelection();
    if (!old || old.tags.join() !== meta.tags.join()) N.sidebar.render();
  };

  // ---------------------------------------------------------------- notes

  A.newNote = async function () {
    if (S.search) clearSearch(false);
    let folderId = S.scope.type === 'folder' ? S.scope.id : S.defaultFolderId;
    let initial = '';
    if (S.scope.type === 'trash') await A.setScope({ type: 'folder', id: folderId });
    if (S.scope.type === 'tag') initial = N.editor.initialHtml() + `<div>#${N.esc(S.scope.tag)}</div>`;
    await leaveCurrent();
    const meta = await N.call('createNote', folderId, initial);
    S.notes.set(meta.id, meta);
    if (S.view === 'gallery') S.galleryOpen = true;
    applyLayout();
    N.sidebar.render();
    N.notelist.render();
    await A.selectNote(meta.id, { focus: true, caret: 'start', force: true });
  };

  function neighbourOf(id) {
    const order = N.notelist.order;
    const i = order.indexOf(id);
    if (i === -1) return order[0] ?? null;
    return order[i + 1] ?? order[i - 1] ?? null;
  }

  A.deleteNote = async function (id = S.currentNoteId) {
    const n = S.notes.get(id);
    if (!n) return;
    if (n.deletedAt) {
      const ok = await N.ui.confirm('Are you sure you want to delete this note?',
        "This note will be deleted immediately. You can't undo this action.", 'Delete', true);
      if (!ok) return;
    }
    const next = neighbourOf(id);
    if (N.editor.noteId() === id) {
      await N.editor.flush();
      N.editor.unmount();
    }
    if (n.deletedAt) {
      await N.call('deleteNote', id, true);
      S.notes.delete(id);
    } else {
      S.notes.set(id, await N.call('deleteNote', id, false));
    }
    if (S.currentNoteId === id) S.currentNoteId = null;
    A.refresh();
    if (S.view === 'gallery' && S.galleryOpen) { S.galleryOpen = false; applyLayout(); }
    await A.selectNote(N.notelist.order.includes(next) ? next : N.notelist.order[0] ?? null, { force: true });
  };

  A.recoverNote = async function (id = S.currentNoteId) {
    const n = S.notes.get(id);
    if (!n || !n.deletedAt) return;
    const next = neighbourOf(id);
    S.notes.set(id, await N.call('recoverNote', id));
    A.refresh();
    if (S.scope.type === 'trash') await A.selectNote(N.notelist.order.includes(next) ? next : N.notelist.order[0] ?? null, { force: true });
    else await openNote(id);
    N.ui.toast('Note recovered');
  };

  A.moveNote = async function (id, folderId) {
    const n = S.notes.get(id);
    if (!n || (n.folderId === folderId && !n.deletedAt)) return;
    if (N.editor.noteId() === id) await N.editor.flush();
    const next = neighbourOf(id);
    S.notes.set(id, await N.call('moveNote', id, folderId));
    A.refresh();
    if (id === S.currentNoteId && !N.notelist.order.includes(id)) {
      await A.selectNote(N.notelist.order.includes(next) ? next : N.notelist.order[0] ?? null);
    } else if (id === S.currentNoteId) {
      await openNote(id);
    }
    const f = S.folders.find((x) => x.id === folderId);
    N.ui.toast(`Moved to “${f ? f.name : 'Notes'}”`);
  };

  A.togglePin = async function (id = S.currentNoteId) {
    const n = S.notes.get(id);
    if (!n || n.deletedAt) return;
    S.notes.set(id, await N.call('setPinned', id, !n.pinned));
    N.notelist.render();
  };

  A.duplicateNote = async function (id = S.currentNoteId) {
    if (N.editor.noteId() === id) await N.editor.flush();
    const meta = await N.call('duplicateNote', id);
    S.notes.set(meta.id, meta);
    A.refresh();
    await A.selectNote(meta.id);
  };

  A.emptyTrash = async function () {
    const ok = await N.ui.confirm('Permanently delete all notes in Recently Deleted?', "You can't undo this action.", 'Delete All', true);
    if (!ok) return;
    if (currentNote() && currentNote().deletedAt) N.editor.unmount();
    await N.call('emptyTrash');
    for (const [id, n] of S.notes) if (n.deletedAt) S.notes.delete(id);
    if (S.scope.type === 'trash') await A.setScope({ type: 'all' });
    else A.refresh();
  };

  A.noteMenuItems = function (id) {
    const n = S.notes.get(id);
    if (!n) return [];
    if (n.deletedAt) {
      return [
        { label: 'Recover', action: () => A.recoverNote(id) },
        { label: 'Move to', submenu: folderMenu((f) => A.moveNote(id, f.id)) },
        '-',
        { label: 'Delete Permanently…', danger: true, action: () => A.deleteNote(id) },
      ];
    }
    return [
      { label: n.pinned ? 'Unpin Note' : 'Pin Note', icon: 'pinOutline', action: () => A.togglePin(id) },
      { label: n.locked ? 'Remove Lock' : 'Lock Note', icon: 'lock', shortcut: 'Ctrl+L', action: () => (n.locked ? removeLock(id) : lockNote(id)) },
      '-',
      { label: 'Move to', icon: 'folder', submenu: folderMenu((f) => A.moveNote(id, f.id), n.folderId) },
      { label: 'Duplicate Note', action: () => A.duplicateNote(id) },
      ...pushMenuItems(id),
      '-',
      { label: 'Delete', icon: 'trash', shortcut: 'Ctrl+⌫', action: () => A.deleteNote(id) },
    ];
  };

  function folderMenu(onPick, currentId) {
    const out = [];
    const walk = (parent, depth) => {
      S.folders.filter((f) => (f.parentId ?? null) === parent)
        .sort((a, b) => (b.isDefault - a.isDefault) || a.name.localeCompare(b.name))
        .forEach((f) => {
          out.push({ label: ' '.repeat(depth) + f.name, icon: 'folder', disabled: f.id === currentId, action: () => onPick(f) });
          walk(f.id, depth + 1);
        });
    };
    walk(null, 0);
    return out;
  }

  // -------------------------------------------------------------- folders

  A.newFolder = async function (parentId = null) {
    const f = await N.call('createFolder', 'New Folder', parentId);
    S.folders.push(f);
    if (parentId != null) {
      const set = new Set(S.settings.collapsedFolders || []);
      set.delete(parentId);
      A.setSetting('collapsedFolders', Array.from(set));
    }
    if (S.settings.accountCollapsed) A.setSetting('accountCollapsed', false);
    await A.setScope({ type: 'folder', id: f.id });
    N.sidebar.beginRename(f.id);
  };

  A.deleteFolder = async function (id) {
    const f = S.folders.find((x) => x.id === id);
    if (!f) return;
    const ok = await N.ui.confirm(`Are you sure you want to delete the folder “${f.name}”?`,
      'All notes and subfolders in this folder will be moved to Recently Deleted.', 'Delete Folder', true);
    if (!ok) return;
    await N.editor.flush();
    const res = await N.call('deleteFolder', id);
    S.folders = res.folders;
    S.notes = new Map(res.notes.map((n) => [n.id, n]));
    const scopeGone = S.scope.type === 'folder' && !S.folders.some((x) => x.id === S.scope.id);
    if (scopeGone) await A.setScope({ type: 'all' });
    else { A.refresh(); await A.selectNote(S.currentNoteId, { force: true }); }
  };

  // --------------------------------------------------------------- search

  const searchInput = () => document.getElementById('search-input');

  const runSearch = N.debounce(async () => {
    const q = searchInput().value.trim();
    S.search = q;
    document.getElementById('tb-search').classList.toggle('has-text', !!q);
    if (!q) { clearSearch(true); return; }
    S.searchIds = new Set(await N.call('search', q));
    N.sidebar.render();
    N.notelist.render();
    if (!N.notelist.order.includes(S.currentNoteId)) await A.selectNote(N.notelist.order[0] ?? null);
    else N.editor.refreshHighlights(true);
  }, 160);

  function clearSearch(reselect) {
    const had = !!S.searchIds;
    searchInput().value = '';
    document.getElementById('tb-search').classList.remove('has-text');
    S.search = '';
    S.searchIds = null;
    N.editor.refreshHighlights(false);
    if (reselect && had) A.setScope(S.scope, { keepNote: true });
  }

  // ------------------------------------------------------------- locking

  function showLockedView() {
    showView('locked');
    document.getElementById('unlock-hint').hidden = true;
    const input = document.getElementById('unlock-input');
    input.value = '';
    setTimeout(() => input.focus(), 30);
  }

  async function createPassword() {
    const r = await N.ui.sheet({
      icon: 'lockLarge',
      title: 'Create a password for locked notes',
      message: "Locked notes are encrypted with this password. If you forget it, you can't view your locked notes.",
      fields: [
        { name: 'pw', type: 'password', placeholder: 'Password' },
        { name: 'verify', type: 'password', placeholder: 'Verify' },
        { name: 'hint', placeholder: 'Hint (recommended)' },
      ],
      buttons: [{ label: 'Cancel', value: null }, { label: 'Set Password', value: 'ok', primary: true }],
      validate: (_b, v) => (!v.pw ? 'Enter a password.' : v.pw !== v.verify ? "The passwords don't match." : null),
    });
    if (r.button !== 'ok') return false;
    await N.call('setLockPassword', r.values.pw, r.values.hint);
    S.lock = { configured: true, unlocked: true, hint: r.values.hint };
    return true;
  }

  async function askPassword() {
    const r = await N.ui.sheet({
      icon: 'lockLarge',
      title: 'Enter the notes password',
      message: S.lock.hint ? `Hint: ${S.lock.hint}` : '',
      fields: [{ name: 'pw', type: 'password', placeholder: 'Password' }],
      buttons: [{ label: 'Cancel', value: null }, { label: 'OK', value: 'ok', primary: true }],
      validate: async (_b, v) => ((await N.call('unlock', v.pw)) ? null : 'Wrong password. Try again.'),
    });
    if (r.button !== 'ok') return false;
    S.lock.unlocked = true;
    return true;
  }

  async function ensureUnlocked() {
    if (!S.lock.configured) return createPassword();
    if (S.lock.unlocked) return true;
    return askPassword();
  }

  async function lockNote(id = S.currentNoteId) {
    const n = S.notes.get(id);
    if (!n || n.locked || n.deletedAt) return;
    if (!(await ensureUnlocked())) return;
    if (N.editor.noteId() !== id) await A.selectNote(id);
    await N.editor.flush();
    const meta = await N.call('lockNote', id, N.editor.serialize());
    S.notes.set(id, meta);
    A.refresh();
    N.notelist.updateSelection();
    N.ui.toast('Note locked');
  }

  async function removeLock(id = S.currentNoteId) {
    const n = S.notes.get(id);
    if (!n || !n.locked) return;
    if (!(await ensureUnlocked())) return;
    await N.editor.flush();
    const res = await N.call('removeLock', id);
    S.notes.set(id, res.meta);
    if (N.editor.noteId() !== id) await A.selectNote(id, { force: true });
    else await openNote(id);
    const meta = await N.call('saveNote', id, N.editor.serialize());
    A.noteSaved(meta);
    updateToolbar();
    N.ui.toast('Lock removed');
  }

  async function closeLockedNotes() {
    await N.editor.flush();
    await N.call('closeLockedNotes');
    S.lock.unlocked = false;
    const n = currentNote();
    if (n && n.locked) await openNote(n.id);
    updateToolbar();
  }

  async function changePassword() {
    const r = await N.ui.sheet({
      icon: 'lockLarge',
      title: 'Change Password',
      fields: [
        { name: 'old', type: 'password', placeholder: 'Old password' },
        { name: 'pw', type: 'password', placeholder: 'New password' },
        { name: 'verify', type: 'password', placeholder: 'Verify' },
        { name: 'hint', placeholder: 'Hint (recommended)' },
      ],
      buttons: [{ label: 'Cancel', value: null }, { label: 'Change Password', value: 'ok', primary: true }],
      validate: async (_b, v) => {
        if (!v.pw) return 'Enter a new password.';
        if (v.pw !== v.verify) return "The passwords don't match.";
        return (await N.call('changeLockPassword', v.old, v.pw, v.hint)) ? null : 'The old password is incorrect.';
      },
    });
    if (r.button === 'ok') {
      S.lock.hint = r.values.hint;
      S.lock.unlocked = true;
      N.ui.toast('Password changed');
    }
  }

  // -------------------------------------------------------- toolbar menus

  function formatPopover(anchor) {
    const st = N.editor.state();
    const content = N.el(`
      <div class="fmt">
        <div class="fmt-title">Format</div>
        <div class="fmt-styles">
          <button data-style="title" class="fs-title">Title</button>
          <button data-style="heading" class="fs-heading">Heading</button>
          <button data-style="subheading" class="fs-subheading">Subheading</button>
          <button data-style="body" class="fs-body">Body</button>
          <button data-style="mono" class="fs-mono">Monostyled</button>
        </div>
        <div class="fmt-group fmt-inline">
          <button data-inline="bold" title="Bold (Ctrl+B)"><b>B</b></button>
          <button data-inline="italic" title="Italic (Ctrl+I)"><i>I</i></button>
          <button data-inline="underline" title="Underline (Ctrl+U)"><u>U</u></button>
          <button data-inline="strike" title="Strikethrough"><s>S</s></button>
        </div>
        <div class="fmt-row">
          <div class="fmt-group">
            <button data-list="bullet" title="Bulleted List (Ctrl+Shift+7)">${N.icon('bulleted')}</button>
            <button data-list="dashed" title="Dashed List (Ctrl+Shift+8)">${N.icon('dashed')}</button>
            <button data-list="numbered" title="Numbered List (Ctrl+Shift+9)">${N.icon('numbered')}</button>
          </div>
          <div class="fmt-group">
            <button data-cmd="outdent" title="Decrease Indent (Ctrl+[)">${N.icon('outdent')}</button>
            <button data-cmd="indent" title="Increase Indent (Ctrl+])">${N.icon('indent')}</button>
          </div>
          <div class="fmt-group">
            <button data-cmd="quote" title="Block Quote (Ctrl+')">${N.icon('quote')}</button>
          </div>
        </div>
        <div class="fmt-colors">
          <button data-color="" class="fc-none" title="Default colour"></button>
          <button data-color="#af52de" style="--c:#af52de" title="Purple"></button>
          <button data-color="#ff2d55" style="--c:#ff2d55" title="Pink"></button>
          <button data-color="#ff9500" style="--c:#ff9500" title="Orange"></button>
          <button data-color="#00c7be" style="--c:#00c7be" title="Mint"></button>
          <button data-color="#0a84ff" style="--c:#0a84ff" title="Blue"></button>
        </div>
      </div>`);
    const sync = () => {
      const s = N.editor.state();
      N.$$('[data-style]', content).forEach((b) => b.classList.toggle('active', b.dataset.style === s.style));
      N.$$('[data-inline]', content).forEach((b) => b.classList.toggle('active', !!s[b.dataset.inline]));
      N.$$('[data-list]', content).forEach((b) => b.classList.toggle('active', b.dataset.list === s.list));
      N.$('[data-cmd="quote"]', content).classList.toggle('active', s.quote);
    };
    content.addEventListener('mousedown', (e) => e.preventDefault());
    content.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      if (b.dataset.style) N.editor.setStyle(b.dataset.style);
      else if (b.dataset.inline) N.editor.toggleInline(b.dataset.inline);
      else if (b.dataset.list) N.editor.toggleList(b.dataset.list);
      else if (b.dataset.cmd === 'indent') N.editor.indent();
      else if (b.dataset.cmd === 'outdent') N.editor.outdent();
      else if (b.dataset.cmd === 'quote') N.editor.toggleQuote();
      else if (b.dataset.color !== undefined) N.editor.setColor(b.dataset.color);
      sync();
    });
    N.ui.popover(anchor, content, { className: 'format-popover' });
    void st;
    sync();
  }

  function pickFiles(accept, cb) {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    if (accept) input.accept = accept;
    input.addEventListener('change', () => cb(Array.from(input.files)));
    input.click();
  }

  A.insertPhotos = () => { const range = N.editor.saveRange(); pickFiles('image/*', (f) => N.editor.insertFiles(f, range)); };
  A.attachFile = () => { const range = N.editor.saveRange(); pickFiles('', (f) => N.editor.insertFiles(f, range)); };

  function mediaMenu(anchor) {
    const range = N.editor.saveRange();
    N.ui.menuFor(anchor, [
      { label: 'Photos…', icon: 'photo', action: () => pickFiles('image/*', (f) => N.editor.insertFiles(f, range)) },
      { label: 'Attach File…', icon: 'paperclip', action: () => pickFiles('', (f) => N.editor.insertFiles(f, range)) },
      '-',
      { label: 'Sketch', icon: 'pen', action: () => N.sketch() },
    ]);
  }

  function lockMenu(anchor) {
    const n = currentNote();
    if (!n) return;
    const items = [];
    if (!n.locked) items.push({ label: 'Lock Note', shortcut: 'Ctrl+L', action: () => lockNote() });
    else items.push({ label: 'Remove Lock', action: () => removeLock() });
    if (S.lock.unlocked) items.push({ label: 'Close All Locked Notes', action: () => closeLockedNotes() });
    if (S.lock.configured) items.push('-', { label: 'Change Password…', action: () => changePassword() });
    N.ui.menuFor(anchor, items);
  }

  function shareMenu(anchor) {
    N.ui.menuFor(anchor, [
      { label: 'Export as PDF…', icon: 'file', action: () => exportNote('pdf') },
      { label: 'Export as HTML…', action: () => exportNote('html') },
      { label: 'Export as Markdown…', action: () => exportNote('markdown') },
      { label: 'Export as Plain Text…', action: () => exportNote('text') },
      '-',
      { label: 'Copy Note Text', action: () => copyText(N.editor.contentText()) },
      { label: 'Copy as Markdown', action: () => copyText(N.convert.toMarkdown(N.editor.root())) },
      '-',
      { label: 'Print…', shortcut: 'Ctrl+P', action: () => N.send('print') },
      ...pushMenuItems(),
    ]);
  }

  function moreMenu(anchor) {
    const sortBy = S.settings.sortBy || 'updated';
    const setSort = (v) => { A.setSetting('sortBy', v); N.notelist.render(); };
    N.ui.menuFor(anchor, [
      { label: 'New Folder', shortcut: 'Ctrl+Shift+N', action: () => A.newFolder() },
      { label: 'Import from iCloud…', action: () => N.importer.open() },
      { label: 'Import Files…', action: () => importNotes() },
      '-',
      { label: 'View as List', checked: S.view === 'list', shortcut: 'Ctrl+1', action: () => setView('list') },
      { label: 'View as Gallery', checked: S.view === 'gallery', shortcut: 'Ctrl+2', action: () => setView('gallery') },
      {
        label: 'Sort By', submenu: [
          { label: 'Date Edited', checked: sortBy === 'updated', action: () => setSort('updated') },
          { label: 'Date Created', checked: sortBy === 'created', action: () => setSort('created') },
          { label: 'Title', checked: sortBy === 'title', action: () => setSort('title') },
        ],
      },
      { label: 'Group by Date', checked: S.settings.groupByDate !== false, action: () => { A.setSetting('groupByDate', S.settings.groupByDate === false); N.notelist.render(); } },
      { label: S.settings.sidebarVisible === false ? 'Show Folders' : 'Hide Folders', shortcut: 'Ctrl+Alt+S', action: () => toggleSidebar() },
      '-',
      { label: 'Settings…', shortcut: 'Ctrl+,', action: () => openSettings() },
      { label: 'Keyboard Shortcuts', action: () => showShortcuts() },
      { label: 'About Notes', action: () => showAbout() },
    ]);
  }

  // --------------------------------------------------------- export/import

  async function exportNote(fmt) {
    const n = currentNote();
    if (!n || !N.editor.root()) return;
    await N.editor.flush();
    const root = N.editor.root();
    const title = n.title || 'New Note';
    let content = '';
    if (fmt === 'html') content = N.convert.toHtmlDocument(title, root.innerHTML);
    else if (fmt === 'markdown') content = N.convert.toMarkdown(root);
    else if (fmt === 'text') content = root.innerText;
    const ok = await N.call('exportNote', fmt, content, title);
    if (ok && fmt !== 'pdf') N.ui.toast('Note exported');
  }

  function copyText(text) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
    N.ui.toast('Copied');
  }

  async function importNotes() {
    const files = await N.call('importFiles');
    if (!files.length) return;
    const folderId = S.scope.type === 'folder' ? S.scope.id : S.defaultFolderId;
    const scratch = document.createElement('div');
    scratch.className = 'editor import-scratch';
    document.body.appendChild(scratch);
    let last = null;
    for (const f of files) {
      let html;
      if (f.ext === 'md' || f.ext === 'markdown') html = N.convert.markdownToHtml(f.text);
      else if (f.ext === 'html' || f.ext === 'htm') html = N.convert.sanitize(f.text);
      else html = N.convert.textToHtml(f.text);
      scratch.innerHTML = html;
      const first = scratch.firstElementChild;
      if (!first || first.tagName !== 'H1') scratch.insertAdjacentHTML('afterbegin', `<h1>${N.esc(f.name)}</h1>`);
      last = await N.call('importNote', folderId, N.editor.serialize(scratch));
      S.notes.set(last.id, last);
    }
    scratch.remove();
    A.refresh();
    if (last) await A.selectNote(last.id);
    N.ui.toast(`Imported ${files.length} ${files.length === 1 ? 'note' : 'notes'}`);
  }

  // -------------------------------------------------------------- sheets

  function openSettings() {
    const sel = (name, options, value) => `<select name="${name}">` +
      options.map(([v, l]) => `<option value="${v}"${String(v) === String(value) ? ' selected' : ''}>${l}</option>`).join('') + '</select>';
    const check = (name, label, value) =>
      `<label class="pref-check"><input type="checkbox" name="${name}"${value ? ' checked' : ''}><span>${label}</span></label>`;
    const html = `
      <div class="prefs">
        <div class="pref-row"><span>Appearance:</span>${sel('appearance', [['system', 'System'], ['light', 'Light'], ['dark', 'Dark']], S.settings.appearance || 'system')}</div>
        <div class="pref-row"><span>Default text size:</span>${sel('textSize', [12, 13, 14, 15, 16, 17, 18, 20, 22].map((s) => [s, s + ' pt']), S.settings.textSize || 15)}</div>
        <div class="pref-row"><span>New notes start with:</span>${sel('newNoteStyle', [['title', 'Title'], ['heading', 'Heading'], ['subheading', 'Subheading'], ['body', 'Body']], S.settings.newNoteStyle || 'title')}</div>
        <div class="pref-row"><span>Sidebar transparency:</span>${sel('sidebarGlass', [[0, 'Off'], [0.04, 'Subtle'], [0.1, 'Light'], [0.2, 'Medium']], sidebarGlass())}</div>
        <div class="pref-row"><span>Sort notes by:</span>${sel('sortBy', [['updated', 'Date Edited'], ['created', 'Date Created'], ['title', 'Title']], S.settings.sortBy || 'updated')}</div>
        <div class="pref-row"><span></span><div class="pref-checks">
          ${check('groupByDate', 'Group notes by date', S.settings.groupByDate !== false)}
          ${check('sortChecked', 'Sort checked items to the bottom', !!S.settings.sortChecked)}
          ${check('showCounts', 'Show note counts', S.settings.showCounts !== false)}
          ${check('slashCommands', 'Slash commands — type / to format a line', !!S.settings.slashCommands)}

        </div></div>
        <div class="pref-row pref-top"><span>iCloud:</span><div class="pref-checks">
          ${check('icloudSync', 'Keep notes in sync with iCloud (download only)', !!(S.settings['icloud.sync'] || {}).enabled)}
          <div class="pref-inline">Pull ${sel('icloudInterval', [[0, 'only when I click Pull'], [5, 'every 5 minutes'], [15, 'every 15 minutes'], [30, 'every 30 minutes'], [60, 'every hour']], (S.settings['icloud.sync'] || {}).interval || 0)}</div>
          <div class="pref-inline"><button class="push-btn" data-pref="syncNow">Pull Now</button>
          ${S.settings.icloudTestPush ? '<button class="push-btn" data-pref="pushNow">Push Now</button>' : ''}
          <span class="pref-muted">${N.sync.state.last ? 'Last synced ' + relTime(N.sync.state.last) : ''}</span></div>
          <span class="pref-muted">Sync only downloads. If a note changes in both places, both versions are kept.</span>
          ${check('icloudTestPush', 'Experimental: send notes to iCloud (test)', !!S.settings.icloudTestPush)}
          ${S.settings.icloudTestPush ? '<div class="pref-inline"><button class="push-btn" data-pref="removePushed">Remove Test Notes from iCloud…</button></div>' : ''}
        </div></div>
        <div class="pref-row"><span>Locked notes:</span><div>
          ${S.lock.configured ? '<button class="push-btn" data-pref="changePassword">Change Password…</button>' : '<span class="pref-muted">No password set yet</span>'}
        </div></div>
      </div>`;
    N.ui.sheet({
      title: 'Settings', icon: false, html, wide: true,
      buttons: [{ label: 'Done', value: 'ok', primary: true }],
      onOpen(sheet) {
        sheet.addEventListener('change', (e) => {
          const t = e.target;
          if (t.name === 'icloudTestPush') {
            const want = t.checked;
            enableTestPush(want).then((done) => {
              if (!done) t.checked = !want;
              const box = sheet.querySelector('[data-pref="removePushed"]');
              if (want && done && !box) t.closest('.pref-checks').insertAdjacentHTML('beforeend',
                '<div class="pref-inline"><button class="push-btn" data-pref="removePushed">Remove Test Notes from iCloud…</button></div>');
              if (!want && box) box.parentElement.remove();
            });
            return;
          }
          if (t.name === 'icloudSync' || t.name === 'icloudInterval') {
            const changes = t.name === 'icloudSync' ? { enabled: t.checked } : { interval: +t.value };
            N.call('icloudSetSync', changes).then((cfg) => {
              S.settings['icloud.sync'] = cfg;
              N.sync.onStatus(Object.assign({}, N.sync.state, { enabled: cfg.enabled, state: cfg.enabled ? N.sync.state.state === 'off' ? 'idle' : N.sync.state.state : 'off' }));
            });
            return;
          }
          const v = t.type === 'checkbox' ? t.checked : (t.name === 'textSize' || t.name === 'sidebarGlass') ? +t.value : t.value;
          A.setSetting(t.name, v);
          if (t.name === 'appearance') applyTheme();
          if (t.name === 'textSize') applyTextSize();
          if (['sortBy', 'groupByDate'].includes(t.name)) N.notelist.render();
          if (t.name === 'showCounts') N.sidebar.render();
          if (t.name === 'sidebarGlass') applyLayout();
        });
        sheet.addEventListener('click', (e) => {
          if (e.target.closest('[data-pref="syncNow"]')) { A.pull(); return; }
          if (e.target.closest('[data-pref="pushNow"]')) { A.push(); return; }
          if (e.target.closest('[data-pref="removePushed"]')) {
            N.ui.confirm('Remove test notes from iCloud?',
              'Every note this app created in iCloud (and the “From Debian” folder) is deleted from iCloud. The copies on this computer stay.',
              'Remove', true).then((ok) => ok && N.send('icloudRemovePushed'));
            return;
          }
          if (e.target.closest('[data-pref="changePassword"]')) {
            sheet.closest('.sheet-backdrop').remove();
            changePassword();
          }
        });
      },
    });
  }

  function showShortcuts() {
    const rows = [
      ['New note', 'Ctrl+N'], ['New folder', 'Ctrl+Shift+N'], ['Search', 'Ctrl+F'], ['Delete note', 'Ctrl+Backspace'],
      ['Lock note', 'Ctrl+L'], ['Print', 'Ctrl+P'], ['Title', 'Ctrl+Shift+T'], ['Heading', 'Ctrl+Shift+H'],
      ['Subheading', 'Ctrl+Shift+J'], ['Body', 'Ctrl+Shift+B'], ['Monostyled', 'Ctrl+Shift+M'],
      ['Checklist', 'Ctrl+Shift+L'], ['Mark as checked', 'Ctrl+Shift+U'], ['Bulleted list', 'Ctrl+Shift+7'],
      ['Dashed list', 'Ctrl+Shift+8'], ['Numbered list', 'Ctrl+Shift+9'], ['Block quote', "Ctrl+'"],
      ['Table', 'Ctrl+Alt+T'], ['Indent / outdent', 'Ctrl+] / Ctrl+['], ['Bold / italic / underline', 'Ctrl+B / I / U'],
      ['Paste and match style', 'Ctrl+Shift+V'], ['List / gallery view', 'Ctrl+1 / Ctrl+2'], ['Show or hide folders', 'Ctrl+Alt+S'],
      ['Bigger / smaller text', 'Ctrl+= / Ctrl+-'],
      ['Command menu (enable in Settings)', '/'],
    ];
    N.ui.sheet({
      title: 'Keyboard Shortcuts', icon: false, wide: true,
      html: '<table class="shortcuts">' + rows.map(([a, k]) => `<tr><td>${a}</td><td><kbd>${k}</kbd></td></tr>`).join('') + '</table>',
      buttons: [{ label: 'Done', value: 'ok', primary: true }],
    });
  }

  function showAbout() {
    N.ui.sheet({
      title: 'Notes',
      message: `Version ${S.version}\nA fast, private notes app for Debian. Your notes are stored on this computer only.`,
      buttons: [{ label: 'OK', value: 'ok', primary: true }],
    });
  }

  // ---------------------------------------------------------- view modes

  function setView(view) {
    S.view = view;
    S.galleryOpen = false;
    A.setSetting('view', view);
    applyLayout();
    N.notelist.render();
    N.notelist.updateSelection();
  }

  A.openGalleryNote = async function (id) {
    await A.selectNote(id);
    S.galleryOpen = true;
    applyLayout();
  };

  async function galleryBack() {
    await leaveCurrent();
    S.galleryOpen = false;
    applyLayout();
    N.notelist.render();
    N.notelist.updateSelection();
    document.getElementById('note-list').focus();
  }

  function toggleSidebar() {
    A.setSetting('sidebarVisible', S.settings.sidebarVisible === false);
    applyLayout();
  }

  // ---------------------------------------------------------- dividers

  function initDividers() {
    N.$$('[data-divider]').forEach((d) => {
      d.addEventListener('mousedown', (e) => {
        e.preventDefault();
        const which = d.dataset.divider;
        const key = which === 'sidebar' ? 'sidebarWidth' : 'listWidth';
        const start = S.settings[key] || (which === 'sidebar' ? 220 : 300);
        const x0 = e.clientX;
        const [min, max] = which === 'sidebar' ? [160, 360] : [220, 520];
        body.classList.add('resizing');
        let w = start;
        const move = (ev) => {
          w = N.clamp(start + ev.clientX - x0, min, max);
          document.getElementById('app').style.setProperty(which === 'sidebar' ? '--sidebar-w' : '--list-w', w + 'px');
        };
        const up = () => {
          document.removeEventListener('mousemove', move);
          document.removeEventListener('mouseup', up);
          body.classList.remove('resizing');
          A.setSetting(key, w);
          updateDragRegionsSoon();
        };
        document.addEventListener('mousemove', move);
        document.addEventListener('mouseup', up);
      });
    });
  }

  // --------------------------------------------------- window drag regions

  function updateDragRegions() {
    const overlayOpen = document.querySelector('#overlay-root .sheet-backdrop, #overlay-root .menu, #overlay-root .popover, #overlay-root .importer-backdrop');
    const rect = (el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; };
    const visible = (el) => el.offsetParent !== null && el.getBoundingClientRect().width > 0;
    const areas = overlayOpen ? [] : N.$$('.toolbar').filter(visible).map(rect);
    const exclude = N.$$('.toolbar button, .toolbar input, .tb-search, .segmented, .traffic').filter(visible).map(rect);
    N.send('setDragRegions', { areas, exclude });
  }
  const updateDragRegionsSoon = N.debounce(updateDragRegions, 60);

  // ----------------------------------------------------------- shortcuts

  function onGlobalKey(e) {
    if (document.querySelector('#overlay-root .sheet-backdrop')) return;
    const mod = N.isMod(e);
    const ed = N.editor.root();
    const inEditor = ed && ed.contains(e.target);
    const inInput = e.target.matches && e.target.matches('input, textarea, select');
    const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    let handled = true;

    if (e.key === 'Escape' && e.target === searchInput()) { clearSearch(true); searchInput().blur(); }
    else if (mod && e.altKey && k === 't') N.editor.insertTable();
    else if (mod && e.altKey && k === 's') toggleSidebar();
    else if (mod && e.altKey && k === 'f') searchInput().focus();
    else if (mod && e.shiftKey && !e.altKey) {
      if (k === 'n') A.newFolder();
      else if (k === 't') N.editor.setStyle('title');
      else if (k === 'h') N.editor.setStyle('heading');
      else if (k === 'j') N.editor.setStyle('subheading');
      else if (k === 'b') N.editor.setStyle('body');
      else if (k === 'm') N.editor.setStyle('mono');
      else if (k === 'l') N.editor.toggleList('checklist');
      else if (k === 'u') N.editor.toggleCheckedAtCaret();
      else if (e.code === 'Digit7') N.editor.toggleList('bullet');
      else if (e.code === 'Digit8') N.editor.toggleList('dashed');
      else if (e.code === 'Digit9') N.editor.toggleList('numbered');
      else handled = false;
    } else if (mod && !e.altKey) {
      if (k === 'n') A.newNote();
      else if (k === 'f') { searchInput().focus(); searchInput().select(); }
      else if (k === 'p') N.send('print');
      else if (k === ',') openSettings();
      else if (k === 'l') { const n = currentNote(); if (n) (n.locked ? removeLock() : lockNote()); }
      else if (e.code === 'Digit1') setView('list');
      else if (e.code === 'Digit2') setView('gallery');
      else if (e.code === 'BracketRight') N.editor.indent();
      else if (e.code === 'BracketLeft') N.editor.outdent();
      else if (e.code === 'Quote' || k === "'") N.editor.toggleQuote();
      else if (k === '=' || k === '+') setTextSize(1);
      else if (k === '-') setTextSize(-1);
      else if (k === '0') { A.setSetting('textSize', 15); applyTextSize(); }
      else if ((e.key === 'Backspace' || e.key === 'Delete') && !inEditor && !inInput) A.deleteNote();
      else handled = false;
    } else handled = false;

    if (handled) { e.preventDefault(); e.stopPropagation(); }
  }

  function setTextSize(delta) {
    A.setSetting('textSize', N.clamp((S.settings.textSize || 15) + delta, 10, 32));
    applyTextSize();
  }

  // -------------------------------------------------------------- actions

  const ACTIONS = {
    toggleSidebar,
    newFolder: () => A.newFolder(),
    newNote: () => A.newNote(),
    deleteNote: () => A.deleteNote(),
    recoverNote: () => A.recoverNote(),
    viewList: () => setView('list'),
    viewGallery: () => setView('gallery'),
    galleryBack,
    formatMenu: (b) => formatPopover(b),
    checklist: () => N.editor.toggleList('checklist'),
    table: () => N.editor.insertTable(),
    mediaMenu: (b) => mediaMenu(b),
    shareMenu: (b) => shareMenu(b),
    lockMenu: (b) => lockMenu(b),
    moreMenu: (b) => moreMenu(b),
    clearSearch: () => { clearSearch(true); searchInput().focus(); },
    showHint: () => {
      const h = document.getElementById('unlock-hint');
      h.textContent = S.lock.hint ? `Hint: ${S.lock.hint}` : 'No hint was set.';
      h.hidden = false;
    },
  };

  function initActions() {
    // Toolbar buttons keep the editor selection intact.
    document.addEventListener('mousedown', (e) => {
      const b = e.target.closest('.toolbar button, .table-tools button');
      if (b) e.preventDefault();
    });
    document.addEventListener('click', (e) => {
      const b = e.target.closest('[data-action]');
      if (!b || b.disabled) return;
      const fn = ACTIONS[b.dataset.action];
      if (!fn) return;
      if (b.classList.contains('pressed') && N.ui.popoverOpen()) { N.ui.closePopover(); return; }
      fn(b);
    });
    document.addEventListener('contextmenu', (e) => {
      const ed = N.editor.root();
      if (ed && ed.contains(e.target)) return; // native menu with spelling suggestions
      if (e.target.matches('input, textarea')) return;
      e.preventDefault();
    });
    searchInput().addEventListener('input', runSearch);
    document.getElementById('sync-bar').addEventListener('click', (e) => {
      const st = N.sync.state.state;
      if (st === 'syncing' || st === 'pushing') return;
      if (st === 'signin-needed') { N.importer.open({ reconnect: true }); return; }
      const b = e.target.closest('[data-sync]');
      if (b && b.dataset.sync === 'pull') A.pull();
      else if (b && b.dataset.sync === 'push') A.push();
    });
    document.getElementById('unlock-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const input = document.getElementById('unlock-input');
      if (await N.call('unlock', input.value)) {
        S.lock.unlocked = true;
        await openNote(S.currentNoteId);
      } else {
        input.value = '';
        const form = e.target;
        form.classList.remove('shake');
        void form.offsetWidth;
        form.classList.add('shake');
      }
    });
  }

  // ----------------------------------------------------------------- boot

  N.beforeQuit = async function () {
    try { await leaveCurrent(); } catch (_) { /* quitting anyway */ }
    N.send('quitReady');
  };

  async function boot() {
    const data = await N.call('bootstrap');
    S.version = data.version;
    S.folders = data.folders;
    S.notes = new Map(data.notes.map((n) => [n.id, n]));
    S.settings = data.settings || {};
    S.defaultFolderId = data.defaultFolderId;
    S.systemTheme = data.systemTheme;
    S.lock = { configured: data.lockConfigured, unlocked: false, hint: data.lockHint };
    S.view = S.settings.view === 'gallery' ? 'gallery' : 'list';
    N.onWindowActive(data.windowActive);
    body.classList.toggle('rounded', !!data.rounded);
    body.classList.toggle('maximized', !!data.maximized);

    applyTheme();
    applyTextSize();
    N.ui.renderTrafficLights();
    N.renderIcons();
    N.editor.init();
    N.sidebar.init();
    N.notelist.init();
    initActions();
    initDividers();
    applyLayout();
    document.addEventListener('keydown', onGlobalKey);
    window.addEventListener('resize', updateDragRegionsSoon);
    new MutationObserver(updateDragRegionsSoon).observe(document.getElementById('overlay-root'), { childList: true });

    let scope = N.sidebar.parseScope(S.settings.lastScope || 'all');
    if (scope.type === 'folder' && !S.folders.some((f) => f.id === scope.id)) scope = { type: 'all' };
    S.scope = scope;
    N.sidebar.render();
    N.notelist.render();
    const last = S.settings.lastNote;
    const start = N.notelist.order.includes(last) ? last : N.notelist.order[0] ?? null;
    await A.selectNote(start, { force: true });
    document.getElementById('note-list').focus({ preventScroll: true });
    body.classList.add('ready');
    N.call('icloudSyncState').then((st) => N.sync.onStatus(st)).catch(() => {});
    N.call('icloudPushedIds').then((ids) => { pushed = new Set(ids); }).catch(() => {});
    A.refreshTracked();
    updateDragRegions();
  }

  boot().catch((err) => N.send('log', 'boot failed', String(err && err.stack || err)));
})(window.N = window.N || {});
