// Folder sidebar: account section, nested folders, Recently Deleted and the tag browser.
(function (N) {
  const SB = (N.sidebar = {});
  const S = () => N.state;

  const scopeKey = (scope) => scope.type + (scope.id !== undefined ? ':' + scope.id : '') + (scope.tag ? ':' + scope.tag : '');

  function counts() {
    const c = { all: 0, trash: 0, folders: {}, tags: {} };
    for (const n of S().notes.values()) {
      if (n.deletedAt) { c.trash++; continue; }
      c.all++;
      c.folders[n.folderId] = (c.folders[n.folderId] || 0) + 1;
      for (const t of n.tags) c.tags[t] = (c.tags[t] || 0) + 1;
    }
    return c;
  }

  function folderTree() {
    const byParent = new Map();
    for (const f of S().folders) {
      const k = f.parentId ?? 0;
      if (!byParent.has(k)) byParent.set(k, []);
      byParent.get(k).push(f);
    }
    for (const list of byParent.values()) {
      list.sort((a, b) => (b.isDefault - a.isDefault) || a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }));
    }
    return byParent;
  }

  function row({ scope, icon, label, count, depth = 0, hasChildren = false, collapsed = false, draggable = false, folder }) {
    const key = scopeKey(scope);
    const active = scopeKey(S().scope) === key && !S().search;
    const disclosure = hasChildren
      ? `<span class="sb-disclosure${collapsed ? '' : ' open'}" data-toggle="${folder.id}">${N.icon('disclosure')}</span>`
      : '<span class="sb-disclosure"></span>';
    return `<div class="sb-row${active ? ' active' : ''}" data-scope="${N.esc(key)}" style="--depth:${depth}"` +
      `${draggable ? ' draggable="true"' : ''}${folder ? ` data-folder="${folder.id}"` : ''}>` +
      `${disclosure}<span class="sb-icon">${N.icon(icon)}</span>` +
      `<span class="sb-label">${N.esc(label)}</span>` +
      `<span class="sb-count">${S().settings.showCounts === false ? '' : count || ''}</span></div>`;
  }

  SB.render = function () {
    const c = counts();
    const tree = folderTree();
    const collapsed = new Set(S().settings.collapsedFolders || []);
    const sectionCollapsed = !!S().settings.accountCollapsed;
    let html = '';

    html += `<div class="sb-section"><div class="sb-header" data-drop-root>` +
      `<span>On My Computer</span><span class="sb-header-toggle${sectionCollapsed ? '' : ' open'}" data-section-toggle>${N.icon('chevronDown')}</span></div>`;
    if (!sectionCollapsed) {
      html += row({ scope: { type: 'all' }, icon: 'allNotes', label: 'All on My Computer', count: c.all });
      const walk = (parentKey, depth) => {
        for (const f of tree.get(parentKey) || []) {
          const kids = tree.has(f.id);
          html += row({
            scope: { type: 'folder', id: f.id }, icon: 'folder', label: f.name, count: c.folders[f.id], depth,
            hasChildren: kids, collapsed: collapsed.has(f.id), draggable: !f.isDefault, folder: f,
          });
          if (kids && !collapsed.has(f.id)) walk(f.id, depth + 1);
        }
      };
      walk(0, 0);
      if (c.trash) html += row({ scope: { type: 'trash' }, icon: 'trashSidebar', label: 'Recently Deleted', count: c.trash });
    }
    html += '</div>';

    const tags = Object.keys(c.tags).sort();
    if (tags.length) {
      const cur = S().scope;
      html += '<div class="sb-section"><div class="sb-header"><span>Tags</span></div><div class="sb-tags">';
      html += `<button class="sb-tag${cur.type === 'alltags' && !S().search ? ' active' : ''}" data-scope="alltags">All Tags</button>`;
      for (const t of tags) {
        const active = cur.type === 'tag' && cur.tag === t && !S().search;
        html += `<button class="sb-tag${active ? ' active' : ''}" data-scope="${N.esc('tag:' + t)}">#${N.esc(t)}</button>`;
      }
      html += '</div></div>';
    }
    const scroll = document.getElementById('sidebar-scroll');
    const top = scroll.scrollTop;
    scroll.innerHTML = html;
    scroll.scrollTop = top;
  };

  function parseScope(key) {
    if (key === 'all' || key === 'trash' || key === 'alltags') return { type: key };
    const [type, ...rest] = key.split(':');
    if (type === 'folder') return { type, id: +rest[0] };
    if (type === 'tag') return { type, tag: rest.join(':') };
    return { type: 'all' };
  }

  SB.parseScope = parseScope;
  SB.scopeKey = scopeKey;

  // ------------------------------------------------------------ rename

  SB.beginRename = function (folderId) {
    const rowEl = N.$(`.sb-row[data-folder="${folderId}"]`);
    const f = S().folders.find((x) => x.id === folderId);
    if (!rowEl || !f || f.isDefault) return;
    const label = rowEl.querySelector('.sb-label');
    const input = N.el(`<input class="sb-rename" value="${N.esc(f.name)}" spellcheck="false">`);
    label.replaceWith(input);
    input.focus();
    input.select();
    let done = false;
    const finish = async (commit) => {
      if (done) return;
      done = true;
      const name = input.value.trim();
      if (commit && name && name !== f.name) {
        S().folders = await N.call('renameFolder', folderId, name);
        N.app.refresh();
      } else SB.render();
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      if (e.key === 'Escape') { e.preventDefault(); finish(false); }
      e.stopPropagation();
    });
    input.addEventListener('blur', () => finish(true));
  };

  // -------------------------------------------------------------- events

  SB.init = function () {
    const scroll = document.getElementById('sidebar-scroll');

    scroll.addEventListener('click', (e) => {
      const toggle = e.target.closest('[data-toggle]');
      if (toggle) {
        const id = +toggle.dataset.toggle;
        const set = new Set(S().settings.collapsedFolders || []);
        if (set.has(id)) set.delete(id); else set.add(id);
        N.app.setSetting('collapsedFolders', Array.from(set));
        SB.render();
        return;
      }
      if (e.target.closest('[data-section-toggle]')) {
        N.app.setSetting('accountCollapsed', !S().settings.accountCollapsed);
        SB.render();
        return;
      }
      const target = e.target.closest('[data-scope]');
      if (target && !e.target.closest('.sb-rename')) N.app.setScope(parseScope(target.dataset.scope));
    });

    scroll.addEventListener('dblclick', (e) => {
      const r = e.target.closest('.sb-row[data-folder]');
      if (r && !e.target.closest('[data-toggle]')) SB.beginRename(+r.dataset.folder);
    });

    scroll.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      const r = e.target.closest('.sb-row[data-folder]');
      const items = [{ label: 'New Folder', action: () => N.app.newFolder() }];
      if (r) {
        const f = S().folders.find((x) => x.id === +r.dataset.folder);
        items.push({ label: 'New Subfolder', action: () => N.app.newFolder(f.id) });
        if (!f.isDefault) {
          items.push('-',
            { label: 'Rename Folder', action: () => SB.beginRename(f.id) },
            { label: 'Delete Folder', danger: true, action: () => N.app.deleteFolder(f.id) });
        }
        items.push('-', { label: 'New Note in Folder', action: () => { N.app.setScope({ type: 'folder', id: f.id }); N.app.newNote(); } });
      }
      if (e.target.closest('.sb-row[data-scope="trash"]')) {
        items.length = 0;
        items.push({ label: 'Empty Recently Deleted…', danger: true, action: () => N.app.emptyTrash() });
      }
      N.ui.menu(items, e.clientX, e.clientY);
    });

    // Drag and drop: notes onto folders, folders onto folders (nesting) or the section header (top level).
    scroll.addEventListener('dragstart', (e) => {
      const r = e.target.closest('.sb-row[data-folder]');
      if (!r) return;
      e.dataTransfer.setData('application/x-notes-folder', r.dataset.folder);
      e.dataTransfer.effectAllowed = 'move';
    });

    const dropTarget = (e) => e.target.closest('.sb-row[data-folder], [data-drop-root], .sb-row[data-scope="trash"]');
    const accepts = (e) => {
      const types = Array.from(e.dataTransfer.types);
      return types.includes('application/x-notes-note') || types.includes('application/x-notes-folder');
    };

    scroll.addEventListener('dragover', (e) => {
      const t = dropTarget(e);
      if (!t || !accepts(e)) return;
      e.preventDefault();
      N.$$('.drop-hover', scroll).forEach((x) => x !== t && x.classList.remove('drop-hover'));
      t.classList.add('drop-hover');
    });
    scroll.addEventListener('dragleave', (e) => {
      const t = dropTarget(e);
      if (t && !t.contains(e.relatedTarget)) t.classList.remove('drop-hover');
    });
    scroll.addEventListener('drop', async (e) => {
      const t = dropTarget(e);
      N.$$('.drop-hover', scroll).forEach((x) => x.classList.remove('drop-hover'));
      if (!t) return;
      e.preventDefault();
      const noteId = e.dataTransfer.getData('application/x-notes-note');
      const folderId = e.dataTransfer.getData('application/x-notes-folder');
      if (noteId) {
        if (t.dataset.scope === 'trash') N.app.deleteNote(+noteId);
        else if (t.dataset.folder) N.app.moveNote(+noteId, +t.dataset.folder);
      } else if (folderId) {
        const parent = t.hasAttribute('data-drop-root') ? null : t.dataset.folder ? +t.dataset.folder : undefined;
        if (parent === undefined || parent === +folderId) return;
        try {
          S().folders = await N.call('moveFolder', +folderId, parent);
          N.app.refresh();
        } catch (err) {
          N.ui.toast(err.message);
        }
      }
    });
  };
})(window.N = window.N || {});
