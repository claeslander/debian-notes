// Application controller: state, navigation, toolbar actions, menus, locking, settings.
(function (N) {
  const S = (N.state = {
    folders: [],
    notes: new Map(),
    settings: {},
    defaultFolderId: null,
    scope: { type: 'local' },
    mode: 'icloud',  // 'icloud': iCloud Notes on the web; 'local': the notes on this computer
    currentNoteId: null,
    selection: new Set(),  // more than one id while several notes are selected
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

  // Transparency is off unless turned on in Settings.
  const noTransparency = () => S.settings.noTransparency !== false;

  // Sidebar transparency: 0 (off) .. 0.2. Older setting `translucentSidebar: false` means off.
  function sidebarGlass() {
    if (noTransparency()) return 0;
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
    body.classList.toggle('no-transparency', noTransparency());
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

  const readOnlyToast = () => N.ui.toast('iCloud notes are read-only here. Use Edit in iCloud or make a local copy');

  function versionMenuItems(id = S.currentNoteId) {
    const n = id != null ? S.notes.get(id) : null;
    if (!n || n.deletedAt || n.locked || n.icloud) return [];
    return ['-', { label: 'Version History…', icon: 'undo', action: () => showVersions(id) }];
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
      N.ui.alert('No earlier versions yet', 'Versions are saved automatically while you edit (at most every 10 minutes).');
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

  // ------------------------------------------------------------ iCloud sync

  N.sync = Object.assign(N.sync || {}, {
    state: { state: 'off' },
    onStatus(st) {
      const prev = N.sync.state;
      N.sync.state = st;
      const c = st.changes;
      if (st.state === 'idle' && prev.state === 'syncing' && c && (c.added || c.updated || c.removed || c.moved || c.conflicts || c.copies || (c.changed || []).length)) {
        A.reloadData({ changed: c.changed || [] });
        const parts = [];
        if (c.added) parts.push(`${c.added} new`);
        if (c.updated) parts.push(`${c.updated} updated`);
        if (c.removed) parts.push(`${c.removed} removed`);
        if (c.conflicts) parts.push(`${c.conflicts} kept as local copies`);
        if (c.copies) parts.push(`${c.copies} ${c.copies === 1 ? 'duplicate' : 'duplicates'} removed`);
        if (parts.length) N.ui.toast(`iCloud: ${parts.join(', ')}`);
      }
    },
  });

  A.refresh = function () {
    N.sidebar.render();
    N.notelist.render();
    updateToolbar();
  };

  function currentNote() {
    return S.currentNoteId != null ? S.notes.get(S.currentNoteId) : null;
  }

  function updateToolbar() {
    const n = S.selection.size > 1 ? null : currentNote();
    const editable = !!n && !n.deletedAt && N.editor.isMounted() && !N.editor.isReadOnly();
    N.$$('[data-needs-note]').forEach((b) => { b.disabled = !editable; });
    N.$$('[data-needs-note-any]').forEach((b) => { b.disabled = !n || !!n.deletedAt; });
    N.$$('[data-needs-view]').forEach((b) => { b.disabled = !n || !!n.deletedAt || !N.editor.isMounted(); });
    N.$('[data-action="lockMenu"]').disabled = !n || !!n.deletedAt || n.icloud;
    N.$('[data-action="deleteNote"]').disabled = !n && S.selection.size < 2;
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
    document.getElementById('multi-view').hidden = which !== 'multi';
  }

  // ----------------------------------------------------------- navigation

  A.setScope = async function (scope, opts = {}) {
    if (S.search) clearSearch(false);
    S.scope = scope;
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
    const discard = prev && !prev.deletedAt && !prev.locked && !prev.icloud && N.editor.isEmpty();
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
    if (S.selection.size) {
      if (S.selection.size > 1) opts = Object.assign({}, opts, { force: true });
      S.selection = new Set();
    }
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
    document.getElementById('icloud-banner').hidden = true;
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
    N.editor.mount(n, content, { readOnly: !!n.deletedAt || n.icloud, focus: opts.focus && !n.icloud, caret: opts.caret });
    document.getElementById('trash-banner').hidden = !n.deletedAt;
    document.getElementById('icloud-banner').hidden = !n.icloud || !!n.deletedAt;
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
    let folderId = S.scope.type === 'folder' ? S.scope.id : S.scope.type === 'folders' ? S.scope.ids[0] : S.defaultFolderId;
    let initial = '';
    if (S.scope.type === 'trash') await A.setScope({ type: 'folder', id: folderId });
    if (S.mode === 'icloud') {
      if (!localEnabled()) return;  // Local is turned off: new notes are made in iCloud itself
      await A.setMode('local');  // new notes are always local
    }
    if (S.scope.type === 'alltags') await A.setScope({ type: 'local' });  // it has no tags yet, so it wouldn't show
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

  // ------------------------------------------- undo for actions on notes
  // Ctrl+Z outside the editor undoes moving, pinning, deleting, recovering and duplicating notes.

  const undoStack = [];
  const redoStack = [];

  function record(label, undo, redo) {
    undoStack.push({ label, undo, redo });
    if (undoStack.length > 100) undoStack.shift();
    redoStack.length = 0;
  }

  A.undo = async function () {
    const step = undoStack.pop();
    if (!step) { N.ui.toast('Nothing to undo'); return; }
    await step.undo();
    redoStack.push(step);
    N.ui.toast(`Undo ${step.label}`);
  };

  A.redo = async function () {
    const step = redoStack.pop();
    if (!step) { N.ui.toast('Nothing to redo'); return; }
    await step.redo();
    undoStack.push(step);
    N.ui.toast(`Redo ${step.label}`);
  };

  // Where notes are: folder, pin and Recently Deleted. Moves, pins, deletes and recovers only change these.
  const placeOf = (ids) => ids.map((id) => S.notes.get(id)).filter(Boolean)
    .map((n) => ({ id: n.id, folderId: n.folderId, pinned: n.pinned, deletedAt: n.deletedAt }));

  async function putBack(places) {
    if (N.editor.noteId() != null) await N.editor.flush();
    const ids = [];
    for (const p of places) {
      if (!S.notes.has(p.id)) continue;  // deleted permanently since
      S.notes.set(p.id, await N.call('restoreNoteState', p.id, p.folderId, p.pinned, p.deletedAt));
      ids.push(p.id);
    }
    S.selection = new Set();
    A.refresh();
    const visible = N.notelist.order.filter((id) => ids.includes(id));
    if (visible.length > 1) await showSelection(visible, visible[0]);
    else await A.selectNote(visible[0] ?? (N.notelist.order.includes(S.currentNoteId) ? S.currentNoteId : N.notelist.order[0] ?? null), { force: true });
  }

  function recordPlaces(label, before) {
    const after = placeOf(before.map((p) => p.id));
    record(label, () => putBack(before), () => putBack(after));
  }

  A.deleteNote = async function (id = S.currentNoteId) {
    const n = S.notes.get(id);
    if (!n) return;
    const before = placeOf([id]);
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
      recordPlaces('Delete', before);
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
    const before = placeOf([id]);
    S.notes.set(id, await N.call('recoverNote', id));
    recordPlaces('Recover', before);
    A.refresh();
    if (S.scope.type === 'trash') await A.selectNote(N.notelist.order.includes(next) ? next : N.notelist.order[0] ?? null, { force: true });
    else await openNote(id);
    N.ui.toast('Note recovered');
  };

  A.moveNote = async function (id, folderId) {
    const n = S.notes.get(id);
    if (!n || (n.folderId === folderId && !n.deletedAt)) return;
    if (n.icloud) { readOnlyToast(); return; }
    if (N.editor.noteId() === id) await N.editor.flush();
    const next = neighbourOf(id);
    const before = placeOf([id]);
    S.notes.set(id, await N.call('moveNote', id, folderId));
    recordPlaces('Move', before);
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
    if (n.icloud) { readOnlyToast(); return; }
    const before = placeOf([id]);
    S.notes.set(id, await N.call('setPinned', id, !n.pinned));
    recordPlaces(n.pinned ? 'Unpin' : 'Pin', before);
    N.notelist.render();
  };

  A.duplicateNote = async function (id = S.currentNoteId) {
    if (N.editor.noteId() === id) await N.editor.flush();
    const meta = await N.call('duplicateNote', id);
    S.notes.set(meta.id, meta);
    let copy = meta;
    record('Duplicate', async () => {
      const cur = S.notes.get(copy.id);
      if (!cur) return;
      if (N.editor.noteId() === copy.id) { await N.editor.flush(); N.editor.unmount(); }
      const edited = (S.notes.get(copy.id) || cur).updatedAt !== copy.updatedAt;
      // An untouched copy simply disappears; one edited since goes to Recently Deleted, so nothing is lost.
      if (edited) S.notes.set(copy.id, await N.call('deleteNote', copy.id, false));
      else { await N.call('deleteNote', copy.id, true); S.notes.delete(copy.id); }
      if (S.currentNoteId === copy.id) S.currentNoteId = null;
      A.refresh();
      await A.selectNote(N.notelist.order.includes(id) ? id : N.notelist.order[0] ?? null, { force: true });
    }, async () => {
      if (!S.notes.has(id)) return;
      copy = await N.call('duplicateNote', id);
      S.notes.set(copy.id, copy);
      A.refresh();
      await A.selectNote(copy.id);
    });
    A.refresh();
    await A.selectNote(meta.id);
  };

  // ------------------------------------------------------ multi-selection

  async function showSelection(ids, anchor = S.currentNoteId) {
    await leaveCurrent();  // may discard an empty new note
    ids = ids.filter((id) => S.notes.has(id));
    if (ids.length < 2) { await A.selectNote(ids[0] ?? null, { force: true }); return; }
    N.editor.unmount();
    S.selection = new Set(ids);
    S.currentNoteId = anchor;
    document.getElementById('multi-title').textContent = `${ids.length} Notes Selected`;
    showView('multi');
    N.notelist.updateSelection();
    updateToolbar();
  }

  A.selectAll = () => showSelection(N.notelist.order.slice());

  A.toggleSelect = function (id) {
    const ids = new Set(S.selection.size > 1 ? S.selection : S.currentNoteId != null ? [S.currentNoteId] : []);
    if (ids.has(id)) ids.delete(id); else ids.add(id);
    const list = N.notelist.order.filter((x) => ids.has(x));
    return list.length > 1 ? showSelection(list, id) : A.selectNote(list[0] ?? id, { force: true });
  };

  A.selectRange = function (id) {
    const order = N.notelist.order;
    const a = order.indexOf(S.currentNoteId);
    const b = order.indexOf(id);
    if (a === -1 || b === -1 || a === b) return A.selectNote(id);
    return showSelection(order.slice(Math.min(a, b), Math.max(a, b) + 1), S.currentNoteId);
  };

  A.deleteNotes = async function (ids) {
    if (ids.length === 1 && S.selection.size < 2) return A.deleteNote(ids[0]);
    const notes = ids.map((id) => S.notes.get(id)).filter(Boolean);
    if (!notes.length) return;
    if (notes.some((n) => n.deletedAt)) {
      const ok = await N.ui.confirm(`Are you sure you want to delete ${notes.length} notes?`,
        "Notes in Recently Deleted are deleted immediately. You can't undo this action.", 'Delete', true);
      if (!ok) return;
    }
    if (ids.includes(N.editor.noteId())) { await N.editor.flush(); N.editor.unmount(); }
    const before = placeOf(notes.filter((n) => !n.deletedAt).map((n) => n.id));
    for (const n of notes) {
      if (n.deletedAt) { await N.call('deleteNote', n.id, true); S.notes.delete(n.id); }
      else S.notes.set(n.id, await N.call('deleteNote', n.id, false));
    }
    if (before.length) recordPlaces('Delete', before);
    S.selection = new Set();
    S.currentNoteId = null;
    A.refresh();
    await A.selectNote(N.notelist.order[0] ?? null, { force: true });
  };
  A.deleteSelected = () => A.deleteNotes(N.notelist.order.filter((id) => S.selection.has(id)));

  A.moveNotes = async function (ids, folderId) {
    if (ids.length === 1 && S.selection.size < 2) return A.moveNote(ids[0], folderId);
    const movable = ids.map((id) => S.notes.get(id)).filter((n) => n && !n.icloud);
    if (!movable.length) { readOnlyToast(); return; }
    if (ids.includes(N.editor.noteId())) await N.editor.flush();
    const before = placeOf(movable.map((n) => n.id));
    for (const n of movable) S.notes.set(n.id, await N.call('moveNote', n.id, folderId));
    recordPlaces('Move', before);
    S.selection = new Set();
    A.refresh();
    await A.selectNote(N.notelist.order.includes(S.currentNoteId) ? S.currentNoteId : N.notelist.order[0] ?? null, { force: true });
    const f = S.folders.find((x) => x.id === folderId);
    const skipped = ids.length - movable.length;
    N.ui.toast(`Moved ${movable.length} ${movable.length === 1 ? 'note' : 'notes'} to “${f ? f.name : 'Notes'}”` +
      (skipped ? `. ${skipped} iCloud ${skipped === 1 ? 'note stays' : 'notes stay'} in iCloud's folders` : ''));
  };

  async function recoverSelected() {
    const ids = [...S.selection];
    const before = placeOf(ids);
    for (const id of ids) S.notes.set(id, await N.call('recoverNote', id));
    recordPlaces('Recover', before);
    S.selection = new Set();
    A.refresh();
    await A.selectNote(N.notelist.order[0] ?? null, { force: true });
    N.ui.toast(`${ids.length} notes recovered`);
  }

  A.selectionMenuItems = function () {
    const ids = N.notelist.order.filter((id) => S.selection.has(id));
    const notes = ids.map((id) => S.notes.get(id)).filter(Boolean);
    const count = `${notes.length} Notes`;
    if (notes.every((n) => n.deletedAt)) {
      return [
        { label: `Recover ${count}`, action: () => recoverSelected() },
        '-',
        { label: `Delete ${count} Permanently…`, danger: true, action: () => A.deleteSelected() },
      ];
    }
    return [
      ...(notes.some((n) => !n.icloud) ? [{ label: 'Move to', icon: 'folder', submenu: folderMenu((f) => A.moveNotes(ids, f.id)) }, '-'] : []),
      { label: `Delete ${count}`, icon: 'trash', shortcut: 'Ctrl+⌫', action: () => A.deleteSelected() },
    ];
  };

  // Edit an iCloud note where it lives: iCloud Notes on the web, signed in with the app's session.
  A.editInICloud = async function (id = S.currentNoteId) {
    const n = S.notes.get(id);
    if (!n || !n.icloud) return;
    try {
      pendingICloudUrl = await N.call('icloudNoteUrl', id);
      await A.setMode('icloud');
    } catch (err) {
      N.ui.alert('Couldn’t open iCloud', err.message);
    }
  };

  // ------------------------------------------------- iCloud / Local
  // Two modes, switched at the top left. iCloud: the window lays iCloud Notes on the web (signed in with
  // the app's iCloud session) over everything but its top bar. Local: this app, with only the notes made
  // on this computer.

  let icloudWebShown = false;
  let pendingICloudUrl = null;
  let icloudFrozen = false;
  let freezing = null;  // promise while the live view is being swapped for a picture of it
  let freezeDone = null;
  let thawTimer = null;

  const isCovered = () => !!document.querySelector('#overlay-root .sheet-backdrop, #overlay-root .menu, #overlay-root .popover, #overlay-root .importer-backdrop');
  const nextFrame = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));

  // Before a sheet or menu of the page opens over iCloud: show a picture of iCloud in its place and only
  // then hide the live view (it lies above the page, so the menu would otherwise appear underneath it).
  A.freezeICloud = function () {
    if (S.mode !== 'icloud' || !icloudWebShown) return freezing || Promise.resolve();
    icloudWebShown = false;
    freezing = new Promise((resolve) => { freezeDone = resolve; });
    N.call('freezeICloudWeb').catch(() => A.icloudFrozen(null));
    return freezing;
  };

  // Called by Python with the picture (or null when there is none, e.g. while iCloud is still loading).
  A.icloudFrozen = async function (url) {
    if (url) {
      icloudFrozen = true;
      document.getElementById('icloud-area').style.backgroundImage = `url("${url}?${Date.now()}")`;
      await nextFrame();
    }
    await N.call('hideICloudWeb').catch(() => {});
    const done = freezeDone;
    freezing = freezeDone = null;
    if (done) done();
  };

  function unfreeze() {
    if (!icloudFrozen) return;
    icloudFrozen = false;
    document.getElementById('icloud-area').style.backgroundImage = '';
    N.call('forgetFrozenICloud').catch(() => {});
  }

  const icloudEnabled = () => S.settings.icloudEnabled !== false;
  const localEnabled = () => S.settings.localEnabled !== false;

  // iCloud or Local can be turned off in Settings: then the switch goes and the app stays in the other one.
  function applyParts() {
    body.classList.toggle('single-part', !icloudEnabled() || !localEnabled());
    N.send('partsChanged', icloudEnabled());
    if (S.mode === 'icloud' && !icloudEnabled()) A.setMode('local');
    else if (S.mode === 'local' && !localEnabled()) A.setMode('icloud');
  }

  A.setMode = async function (mode) {
    if ((mode === 'icloud' && !icloudEnabled()) || (mode === 'local' && !localEnabled())) return;
    if (mode === S.mode && body.classList.contains('mode-' + mode)) return;
    if (mode === 'icloud') await leaveCurrent();
    S.mode = mode;
    unfreeze();
    N.send('modeChanged', mode);
    A.setSetting('lastMode', mode);  // the app opens where it was closed
    body.classList.toggle('mode-icloud', mode === 'icloud');
    N.$$('[data-action="modeICloud"]').forEach((b) => b.classList.toggle('active', mode === 'icloud'));
    N.$$('[data-action="modeLocal"]').forEach((b) => b.classList.toggle('active', mode === 'local'));
    syncICloudWeb();
    updateDragRegionsSoon();
    if (mode === 'local') {
      A.refresh();
      await A.selectNote(N.notelist.order.includes(S.currentNoteId) ? S.currentNoteId : N.notelist.order[0] ?? null, { force: true });
    }
  };

  function showLive() {
    const r = document.getElementById('icloud-area').getBoundingClientRect();
    const box = { left: r.left, top: r.top, right: window.innerWidth - r.right, bottom: window.innerHeight - r.bottom };
    N.call('showICloudWeb', box, pendingICloudUrl).catch(() => {});
    pendingICloudUrl = null;
    icloudWebShown = true;
    if (icloudFrozen) setTimeout(unfreeze, 150);  // the live view is back on top: drop the picture of it
  }

  function syncICloudWeb() {
    const active = S.mode === 'icloud';
    const covered = isCovered();
    body.classList.toggle('icloud-covered', active && covered);
    if (active && covered) {
      if (icloudWebShown) A.freezeICloud();
    } else if (active) {
      if (icloudFrozen || freezing) {
        // Wait a moment: a menu often closes to open a sheet (Settings…), and iCloud shouldn't flash in between.
        clearTimeout(thawTimer);
        thawTimer = setTimeout(() => { if (S.mode === 'icloud' && !isCovered()) showLive(); }, 150);
      } else {
        showLive();
      }
    } else if (icloudWebShown) {
      N.call('hideICloudWeb').catch(() => {});
      icloudWebShown = false;
    }
  }


  A.localCopy = async function (id = S.currentNoteId) {
    await A.duplicateNote(id);
    N.ui.toast('Local copy made. You can edit this one');
  };

  A.emptyTrash = async function () {
    const ok = await N.ui.confirm('Permanently delete all notes in Recently Deleted?', "You can't undo this action.", 'Delete All', true);
    if (!ok) return;
    if (currentNote() && currentNote().deletedAt) N.editor.unmount();
    await N.call('emptyTrash');
    for (const [id, n] of S.notes) if (n.deletedAt) S.notes.delete(id);
    if (S.scope.type === 'trash') await A.setScope({ type: 'local' });
    else A.refresh();
  };

  A.noteMenuItems = function (id) {
    const n = S.notes.get(id);
    if (!n) return [];
    if (n.deletedAt) {
      return [
        { label: 'Recover', action: () => A.recoverNote(id) },
        ...(n.icloud ? [] : [{ label: 'Move to', submenu: folderMenu((f) => A.moveNote(id, f.id)) }]),
        '-',
        { label: 'Delete Permanently…', danger: true, action: () => A.deleteNote(id) },
      ];
    }
    if (n.icloud) {
      return [
        { label: 'Edit in iCloud…', icon: 'cloud', action: () => A.editInICloud(id) },
        { label: 'Make Local Copy', action: () => A.localCopy(id) },
        '-',
        { label: 'Delete', icon: 'trash', shortcut: 'Ctrl+⌫', action: () => A.deleteNote(id) },
      ];
    }
    return [
      { label: n.pinned ? 'Unpin Note' : 'Pin Note', icon: 'pinOutline', action: () => A.togglePin(id) },
      { label: n.locked ? 'Remove Lock' : 'Lock Note', icon: 'lock', shortcut: 'Ctrl+L', action: () => (n.locked ? removeLock(id) : lockNote(id)) },
      '-',
      { label: 'Move to', icon: 'folder', submenu: folderMenu((f) => A.moveNote(id, f.id), n.folderId) },
      { label: 'Duplicate Note', action: () => A.duplicateNote(id) },
      ...versionMenuItems(id),
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
    const scopeGone = (S.scope.type === 'folder' && !S.folders.some((x) => x.id === S.scope.id)) ||
      (S.scope.type === 'folders' && S.scope.ids.some((id) => !S.folders.some((x) => x.id === id)));
    if (scopeGone) await A.setScope({ type: 'local' });
    else { A.refresh(); await A.selectNote(S.currentNoteId, { force: true }); }
  };

  A.deleteFolders = async function (ids) {
    const ok = await N.ui.confirm(`Are you sure you want to delete ${ids.length} folders?`,
      'All notes and subfolders in these folders will be moved to Recently Deleted.', 'Delete Folders', true);
    if (!ok) return;
    await N.editor.flush();
    let res = null;
    for (const id of ids) {
      if (!S.folders.some((x) => x.id === id)) continue;  // already gone as a subfolder of another
      res = await N.call('deleteFolder', id);
      S.folders = res.folders;
    }
    if (res) S.notes = new Map(res.notes.map((n) => [n.id, n]));
    await A.setScope({ type: 'local' });
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
    if (n.icloud) { readOnlyToast(); return; }
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
      ...versionMenuItems(),
    ]);
  }

  // The same ⋯ menu in iCloud, with what applies there.
  async function icloudMenu(anchor) {
    await A.freezeICloud();  // so the menu opens above iCloud straight away
    N.ui.menuFor(anchor, [
      { label: 'Settings…', shortcut: 'Ctrl+,', action: () => openSettings() },
      { label: 'Keyboard Shortcuts', action: () => showShortcuts() },
      { label: 'About Notes', action: () => showAbout() },
      '-',
      { label: 'Quit Notes', shortcut: 'Ctrl+Q', action: () => N.send('quitApp') },
    ]);
  }

  function moreMenu(anchor) {
    const sortBy = S.settings.sortBy || 'updated';
    const setSort = (v) => { A.setSetting('sortBy', v); N.notelist.render(); };
    N.ui.menuFor(anchor, [
      { label: 'New Folder', shortcut: 'Ctrl+Shift+N', action: () => A.newFolder() },
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
      '-',
      { label: 'Settings…', shortcut: 'Ctrl+,', action: () => openSettings() },
      { label: 'Keyboard Shortcuts', action: () => showShortcuts() },
      { label: 'About Notes', action: () => showAbout() },
      '-',
      { label: 'Quit Notes', shortcut: 'Ctrl+Q', action: () => N.send('quitApp') },
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

  A.openSettings = async () => {
    if (document.querySelector('#overlay-root .sheet-backdrop')) return;
    await A.freezeICloud();
    openSettings();
  };

  function openSettings() {
    const sel = (name, options, value) => `<select name="${name}">` +
      options.map(([v, l]) => `<option value="${v}"${String(v) === String(value) ? ' selected' : ''}>${l}</option>`).join('') + '</select>';
    const check = (name, label, value) =>
      `<label class="pref-check"><input type="checkbox" name="${name}"${value ? ' checked' : ''}><span>${label}</span></label>`;
    const html = `
      <div class="prefs">
        <div class="pref-section">General<small>Everywhere in Notes</small></div>
        <div class="pref-row"><span>Appearance:</span>${sel('appearance', [['system', 'System'], ['light', 'Light'], ['dark', 'Dark']], S.settings.appearance || 'system')}</div>
        <div class="pref-row pref-top"><span>Background:</span><div class="pref-checks">
          ${check('background', 'Keep Notes running in the background', !!S.settings.background)}
          <span class="pref-muted">Opens instantly: closing only hides Notes. Uses about 650 MB of memory.</span>
        </div></div>

        <div class="pref-section">iCloud<small>iCloud Notes on the web, in the app</small></div>
        <div class="pref-row"><span></span>${check('icloudEnabled', 'Use iCloud', icloudEnabled())}</div>

        <div class="pref-section">Local<small>Notes stored on this computer</small></div>
        <div class="pref-row"><span></span>${check('localEnabled', 'Use Local', localEnabled())}</div>
        <div class="pref-row"><span>Default text size:</span>${sel('textSize', [12, 13, 14, 15, 16, 17, 18, 20, 22].map((s) => [s, s + ' pt']), S.settings.textSize || 15)}</div>
        <div class="pref-row"><span>New notes start with:</span>${sel('newNoteStyle', [['title', 'Title'], ['heading', 'Heading'], ['subheading', 'Subheading'], ['body', 'Body']], S.settings.newNoteStyle || 'title')}</div>
        <div class="pref-row"><span>Sort notes by:</span>${sel('sortBy', [['updated', 'Date Edited'], ['created', 'Date Created'], ['title', 'Title']], S.settings.sortBy || 'updated')}</div>
        <div class="pref-row"><span></span><div class="pref-checks">
          ${check('groupByDate', 'Group notes by date', S.settings.groupByDate !== false)}
          ${check('sortChecked', 'Sort checked items to the bottom', !!S.settings.sortChecked)}
          ${check('showCounts', 'Show note counts', S.settings.showCounts !== false)}
          ${check('slashCommands', 'Slash commands: type / to format a line', !!S.settings.slashCommands)}
        </div></div>
        <div class="pref-row pref-top"><span>Sidebar:</span><div class="pref-checks">
          ${check('noTransparency', 'Disable transparency', noTransparency())}
          <div class="pref-inline" data-glass-row ${noTransparency() ? 'hidden' : ''}>Transparency ${sel('sidebarGlass', [[0.04, 'subtle'], [0.1, 'light'], [0.2, 'medium']], sidebarGlass() || 0.04)}</div>
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
          if (t.name === 'icloudEnabled' || t.name === 'localEnabled') {
            // One of the two always stays on.
            const other = sheet.querySelector(`[name="${t.name === 'icloudEnabled' ? 'localEnabled' : 'icloudEnabled'}"]`);
            if (!t.checked && !other.checked) { t.checked = true; N.ui.toast('Keep iCloud or Local on'); return; }
            A.setSetting(t.name, t.checked);
            applyParts();
            return;
          }
          if (t.name === 'background') {
            S.settings.background = t.checked;
            N.call('setBackground', t.checked);
            return;
          }
          const v = t.type === 'checkbox' ? t.checked : (t.name === 'textSize' || t.name === 'sidebarGlass') ? +t.value : t.value;
          A.setSetting(t.name, v);
          if (t.name === 'appearance') applyTheme();
          if (t.name === 'textSize') applyTextSize();
          if (['sortBy', 'groupByDate'].includes(t.name)) N.notelist.render();
          if (t.name === 'showCounts') N.sidebar.render();
          if (t.name === 'sidebarGlass') applyLayout();
          if (t.name === 'noTransparency') {
            applyLayout();
            sheet.querySelector('[data-glass-row]').hidden = t.checked;
          }
        });
        sheet.addEventListener('click', (e) => {
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
      ['Paste and match style', 'Ctrl+Shift+V'], ['List / gallery view', 'Ctrl+1 / Ctrl+2'],
      ['Bigger / smaller text', 'Ctrl+= / Ctrl+-'], ['iCloud / Local', 'Ctrl+← / Ctrl+→, or Ctrl+Tab'], ['Quit', 'Ctrl+Q'],
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
      else if (k === 'z' && !inEditor && !inInput) A.redo();
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
      else if ((e.key === 'Backspace' || e.key === 'Delete') && !inEditor && !inInput) ACTIONS.deleteNote();
      else if (k === 'a' && !inEditor && !inInput) A.selectAll();
      else if (k === 'z' && !inEditor && !inInput) A.undo();
      else if (k === 'y' && !inEditor && !inInput) A.redo();
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
    icloudMenu: (b) => icloudMenu(b),
    modeICloud: () => A.setMode('icloud'),
    modeLocal: () => A.setMode('local'),
    newFolder: () => A.newFolder(),
    newNote: () => A.newNote(),
    deleteNote: () => (S.selection.size > 1 ? A.deleteSelected() : A.deleteNote()),
    deleteSelected: () => A.deleteSelected(),
    localCopy: () => A.localCopy(),
    editInICloud: () => A.editInICloud(),
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
    document.getElementById('editor-scroll').addEventListener('dblclick', () => {
      const n = currentNote();
      if (n && n.icloud && !n.deletedAt) A.editInICloud(n.id);  // read-only here: edit it in iCloud
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

  // Background mode hides the window instead of closing it: save what is being edited.
  N.beforeHide = async function () {
    try { await N.editor.flush(); } catch (_) { /* nothing to save */ }
  };

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
    S.settings.sidebarVisible = true;  // the sidebar can't be hidden any more
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

    // The app opens where it was closed (iCloud by default, Local until iCloud is set up), and chooses
    // before anything is drawn so the other one never flashes first.
    S.scope = { type: 'local' };
    const startOn = !localEnabled() ? 'icloud' : !icloudEnabled() ? 'local'
      : data.icloudConfigured && S.settings.lastMode !== 'local' ? 'icloud' : 'local';
    body.classList.toggle('single-part', !icloudEnabled() || !localEnabled());
    await A.setMode(startOn);
    N.sidebar.render();
    N.notelist.render();
    const last = S.settings.lastNote;
    const start = N.notelist.order.includes(last) ? last : N.notelist.order[0] ?? null;
    await A.selectNote(start, { force: true });
    document.getElementById('note-list').focus({ preventScroll: true });
    body.classList.add('ready');
    // Keep the iCloud view laid exactly over its place, and out of the way of sheets and menus.
    new ResizeObserver(() => { if (S.mode === 'icloud') syncICloudWeb(); })
      .observe(document.getElementById('icloud-area'));
    new MutationObserver(() => { if (S.mode === 'icloud') syncICloudWeb(); })
      .observe(document.getElementById('overlay-root'), { childList: true });
    updateDragRegions();
  }

  boot().catch((err) => N.send('log', 'boot failed', String(err && err.stack || err)));
})(window.N = window.N || {});
