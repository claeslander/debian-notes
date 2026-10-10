// The middle column: notes grouped by date (list view) or as cards (gallery view).
(function (N) {
  const L = (N.notelist = {});
  const S = () => N.state;

  L.visible = function () {
    const s = S();
    let arr = Array.from(s.notes.values());
    if (s.searchIds) arr = arr.filter((n) => !n.deletedAt && s.searchIds.has(n.id));
    else {
      const sc = s.scope;
      arr = arr.filter((n) => {
        if (sc.type === 'trash') return !!n.deletedAt;
        if (n.deletedAt) return false;
        if (sc.type === 'folder') return n.folderId === sc.id;
        if (sc.type === 'tag') return n.tags.includes(sc.tag);
        if (sc.type === 'alltags') return n.tags.length > 0;
        return true;
      });
    }
    const by = s.settings.sortBy || 'updated';
    const key = by === 'created' ? 'createdAt' : 'updatedAt';
    arr.sort(by === 'title'
      ? (a, b) => (a.title || 'New Note').localeCompare(b.title || 'New Note', undefined, { sensitivity: 'base' })
      : (a, b) => b[key] - a[key]);
    return arr;
  };

  // Sections in display order: [{label, notes}]
  L.sections = function (notes) {
    const s = S();
    const showPinned = s.scope.type !== 'trash' && !s.searchIds;
    const pinned = showPinned ? notes.filter((n) => n.pinned) : [];
    const rest = showPinned ? notes.filter((n) => !n.pinned) : notes;
    const out = [];
    if (pinned.length) out.push({ label: 'Pinned', notes: pinned, pinned: true });
    const by = s.settings.sortBy || 'updated';
    if (s.searchIds) {
      if (rest.length) out.push({ label: 'Notes', notes: rest });
    } else if (by === 'title' || s.settings.groupByDate === false) {
      if (rest.length) out.push({ label: pinned.length ? 'Notes' : '', notes: rest });
    } else {
      const key = by === 'created' ? 'createdAt' : 'updatedAt';
      let cur = null;
      for (const n of rest) {
        const label = N.date.group(n[key]);
        if (!cur || cur.label !== label) out.push((cur = { label, notes: [] }));
        cur.notes.push(n);
      }
    }
    return out;
  };

  L.order = [];

  function folderName(id) {
    const f = S().folders.find((x) => x.id === id);
    return f ? f.name : 'Notes';
  }

  function showFolder() {
    const t = S().scope.type;
    return !!S().searchIds || t === 'all' || t === 'tag' || t === 'alltags' || t === 'trash';
  }

  function highlight(text) {
    const q = (S().search || '').trim();
    const safe = N.esc(text);
    if (!q) return safe;
    const terms = q.split(/\s+/).filter(Boolean).map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    if (!terms.length) return safe;
    return safe.replace(new RegExp('(' + terms.map((t) => N.esc(t)).join('|') + ')', 'gi'), '<mark>$1</mark>');
  }

  function rowHtml(n) {
    const s = S();
    const date = N.date.list((s.settings.sortBy === 'created' ? n.createdAt : n.updatedAt));
    const title = n.title || 'New Note';
    const sub = n.locked ? 'Locked' : (n.snippet || 'No additional text');
    const thumb = n.thumbnail && !n.locked ? `<img class="note-thumb" src="${N.esc(n.thumbnail)}" alt="" loading="lazy">` : '';
    return `<div class="note-row${n.id === s.currentNoteId ? ' selected' : ''}" data-id="${n.id}" draggable="true">` +
      '<div class="note-row-text">' +
      `<div class="note-title">${n.locked ? `<span class="note-lock">${N.icon('lockSmall')}</span>` : ''}${highlight(title)}</div>` +
      `<div class="note-sub"><span class="note-date">${N.esc(date)}</span><span class="note-snippet">${highlight(sub)}</span></div>` +
      (showFolder() ? `<div class="note-folder">${N.icon(n.deletedAt ? 'trashSidebar' : 'folder')}<span>${N.esc(folderName(n.folderId))}</span></div>` : '') +
      `</div>${thumb}</div>`;
  }

  function cardHtml(n) {
    const s = S();
    const date = N.date.list(s.settings.sortBy === 'created' ? n.createdAt : n.updatedAt);
    let page;
    if (n.locked) page = `<div class="gc-locked">${N.icon('lock')}</div>`;
    else if (n.thumbnail) page = `<img src="${N.esc(n.thumbnail)}" alt="" loading="lazy">`;
    else {
      const lines = (n.preview || '').split('\n').filter((l) => l.trim());
      page = `<div class="gc-text"><div class="gc-text-title">${N.esc(lines[0] || '')}</div>${N.esc(lines.slice(1).join('\n'))}</div>`;
    }
    return `<div class="gallery-card${n.id === s.currentNoteId ? ' selected' : ''}" data-id="${n.id}" draggable="true">` +
      `<div class="gc-page">${page}${n.pinned ? `<span class="gc-pin">${N.icon('pin')}</span>` : ''}</div>` +
      `<div class="gc-title">${n.locked ? N.icon('lockSmall') : ''}${N.esc(n.title || 'New Note')}</div>` +
      `<div class="gc-date">${N.esc(date)}</div></div>`;
  }

  L.render = function () {
    const s = S();
    const listEl = document.getElementById('note-list');
    const notes = L.visible();
    L.order = [];
    const sections = L.sections(notes);
    const gallery = s.view === 'gallery';
    let html = '';
    for (const sec of sections) {
      sec.notes.forEach((n) => L.order.push(n.id));
      html += `<div class="note-section${sec.pinned ? ' pinned' : ''}">`;
      if (sec.label) html += `<div class="note-section-title">${N.esc(sec.label)}</div>`;
      html += gallery ? '<div class="gallery-grid">' + sec.notes.map(cardHtml).join('') + '</div>'
        : '<div class="note-rows">' + sec.notes.map(rowHtml).join('') + '</div>';
      html += '</div>';
    }
    if (!notes.length) {
      html = `<div class="list-empty">${s.searchIds ? 'No Results' : 'No Notes'}</div>`;
    }
    const top = listEl.scrollTop;
    listEl.innerHTML = html;
    listEl.scrollTop = top;
    renderHeader(notes.length);
  };

  function renderHeader(count) {
    const s = S();
    const h = document.getElementById('list-header');
    let title = '';
    if (s.searchIds) title = 'Search Results';
    else if (s.scope.type === 'trash') title = 'Recently Deleted';
    if (s.view === 'gallery') {
      const sc = s.scope;
      title = s.searchIds ? 'Search Results' : sc.type === 'folder' ? folderName(sc.id) : sc.type === 'trash'
        ? 'Recently Deleted' : sc.type === 'tag' ? '#' + sc.tag : sc.type === 'alltags' ? 'All Tags' : 'All on My Computer';
    }
    h.innerHTML = title ? `<div class="list-header-title">${N.esc(title)}</div><div class="list-header-count">${count} ${count === 1 ? 'note' : 'notes'}</div>` : '';
    h.hidden = !title;
    if (s.scope.type === 'trash' && !s.searchIds && count) {
      h.innerHTML += '<div class="list-header-note">Notes are permanently deleted after 30 days.</div>';
    }
  }

  L.updateSelection = function () {
    const id = S().currentNoteId;
    N.$$('#note-list [data-id]').forEach((el) => el.classList.toggle('selected', +el.dataset.id === id));
    const sel = N.$(`#note-list [data-id="${id}"]`);
    if (sel) sel.scrollIntoView({ block: 'nearest' });
  };

  L.updateRow = function (meta) {
    const el = N.$(`#note-list [data-id="${meta.id}"]`);
    if (!el) return false;
    const fresh = N.el(S().view === 'gallery' ? cardHtml(meta) : rowHtml(meta));
    el.replaceWith(fresh);
    return true;
  };

  L.init = function () {
    const listEl = document.getElementById('note-list');

    listEl.addEventListener('mousedown', (e) => {
      const r = e.target.closest('[data-id]');
      if (r && e.button === 0) N.app.selectNote(+r.dataset.id);
      setTimeout(() => listEl.focus({ preventScroll: true }), 0);
    });
    listEl.addEventListener('dblclick', (e) => {
      const r = e.target.closest('[data-id]');
      if (!r) return;
      if (S().view === 'gallery') N.app.openGalleryNote(+r.dataset.id);
      else N.editor.focus();
    });
    listEl.addEventListener('focus', () => document.body.classList.add('list-focused'));
    listEl.addEventListener('blur', () => document.body.classList.remove('list-focused'));

    listEl.addEventListener('keydown', (e) => {
      const order = L.order;
      if (!order.length) return;
      const i = order.indexOf(S().currentNoteId);
      const gallery = S().view === 'gallery';
      let cols = 1;
      if (gallery) {
        const cards = N.$$('.gallery-card', listEl);
        if (cards.length > 1) {
          const y = cards[0].offsetTop;
          cols = cards.findIndex((c) => c.offsetTop !== y);
          if (cols <= 0) cols = cards.length;
        }
      }
      let next = null;
      if (e.key === 'ArrowDown') next = order[Math.min(order.length - 1, i + cols)];
      else if (e.key === 'ArrowUp') next = order[Math.max(0, i - cols)];
      else if (gallery && e.key === 'ArrowRight') next = order[Math.min(order.length - 1, i + 1)];
      else if (gallery && e.key === 'ArrowLeft') next = order[Math.max(0, i - 1)];
      else if (e.key === 'Home') next = order[0];
      else if (e.key === 'End') next = order[order.length - 1];
      else if (e.key === 'Enter' || (e.key === 'Tab' && !e.shiftKey && !gallery)) {
        e.preventDefault();
        if (gallery) N.app.openGalleryNote(S().currentNoteId);
        else N.editor.focus();
        return;
      } else if ((e.key === 'Delete' || e.key === 'Backspace') && S().currentNoteId) {
        e.preventDefault();
        N.app.deleteNote(S().currentNoteId);
        return;
      } else return;
      e.preventDefault();
      if (next !== undefined && next !== null) N.app.selectNote(next);
    });

    listEl.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      const r = e.target.closest('[data-id]');
      if (!r) {
        N.ui.menu([{ label: 'New Note', shortcut: 'Ctrl+N', action: () => N.app.newNote() }], e.clientX, e.clientY);
        return;
      }
      const id = +r.dataset.id;
      N.app.selectNote(id);
      N.ui.menu(N.app.noteMenuItems(id), e.clientX, e.clientY);
    });

    listEl.addEventListener('dragstart', (e) => {
      const r = e.target.closest('[data-id]');
      if (!r) return;
      e.dataTransfer.setData('application/x-notes-note', r.dataset.id);
      e.dataTransfer.effectAllowed = 'move';
    });
  };
})(window.N = window.N || {});
