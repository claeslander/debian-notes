// The note editor: a contenteditable surface with Notes-style blocks, lists, checklists,
// tables and attachments, plus its own undo history and autosave.
(function (N) {
  const E = (N.editor = {});
  const STYLE_TAGS = { title: 'h1', heading: 'h2', subheading: 'h3', body: 'div', mono: 'pre' };
  const BLOCK_RE = /^(H1|H2|H3|DIV|P|PRE|LI|BLOCKQUOTE|TD)$/;

  let root = null;        // the live #editor element
  let note = null;        // meta of the mounted note
  let history = null;
  let readOnly = true;
  let dirty = false;
  let savedRange = null;
  let plainPaste = false;
  let saveTimer = null;

  E.root = () => root;
  E.noteId = () => (note ? note.id : null);
  E.isReadOnly = () => readOnly;
  E.isMounted = () => !!note;

  // ------------------------------------------------------------ selection

  const sel = () => window.getSelection();

  function rangeInEditor() {
    const s = sel();
    if (!root || !s.rangeCount) return null;
    const r = s.getRangeAt(0);
    return root.contains(r.commonAncestorContainer) ? r : null;
  }

  function closestIn(node, selector) {
    const el = node && (node.nodeType === 1 ? node : node.parentElement);
    const found = el && el.closest(selector);
    return found && root && root.contains(found) && found !== root ? found : null;
  }

  function blockOf(node) {
    while (node && node !== root) {
      if (node.nodeType === 1 && BLOCK_RE.test(node.tagName)) return node;
      node = node.parentNode;
    }
    return null;
  }

  function placeCaret(node, offset = 0) {
    const r = document.createRange();
    r.setStart(node, Math.min(offset, node.nodeType === 3 ? node.length : node.childNodes.length));
    r.collapse(true);
    const s = sel();
    s.removeAllRanges();
    s.addRange(r);
  }

  function placeCaretAtEnd(el) {
    const r = document.createRange();
    r.selectNodeContents(el);
    r.collapse(false);
    const s = sel();
    s.removeAllRanges();
    s.addRange(r);
  }

  function ensureSelection() {
    if (!root) return false;
    if (document.activeElement !== root) root.focus({ preventScroll: true });
    if (!rangeInEditor()) {
      if (savedRange && root.contains(savedRange.startContainer)) {
        const s = sel();
        s.removeAllRanges();
        s.addRange(savedRange);
      } else {
        placeCaretAtEnd(root);
      }
    }
    return true;
  }

  function exec(cmd, value = null) {
    ensureSelection();
    return document.execCommand(cmd, false, value);
  }

  // Selection as child-index paths, exact for restoring a snapshot of the same HTML.
  function nodePath(node) {
    const path = [];
    while (node && node !== root) {
      const parent = node.parentNode;
      if (!parent) return null;
      path.unshift(Array.prototype.indexOf.call(parent.childNodes, node));
      node = parent;
    }
    return node === root ? path : null;
  }

  function fromPath(path) {
    let n = root;
    for (const i of path) {
      n = n.childNodes[i];
      if (!n) return null;
    }
    return n;
  }

  function saveSelection() {
    const r = rangeInEditor();
    if (!r) return null;
    const sp = nodePath(r.startContainer);
    const ep = nodePath(r.endContainer);
    return sp && ep ? { sp, so: r.startOffset, ep, eo: r.endOffset } : null;
  }

  function restoreSelection(s) {
    if (!s) { placeCaretAtEnd(root); return; }
    const sn = fromPath(s.sp);
    const en = fromPath(s.ep);
    if (!sn || !en) { placeCaretAtEnd(root); return; }
    try {
      const r = document.createRange();
      r.setStart(sn, s.so);
      r.setEnd(en, s.eo);
      const ws = sel();
      ws.removeAllRanges();
      ws.addRange(r);
    } catch (_) {
      placeCaretAtEnd(root);
    }
  }

  // --------------------------------------------------------------- history

  class History {
    constructor() {
      this.stack = [this.snapshot()];
      this.index = 0;
      this.pending = false;
      this.timer = null;
    }
    snapshot() { return { html: root.innerHTML, sel: saveSelection() }; }
    touch() {
      this.pending = true;
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.commit(), 700);
    }
    commit() {
      clearTimeout(this.timer);
      if (!this.pending) return;
      this.pending = false;
      const snap = this.snapshot();
      if (snap.html === this.stack[this.index].html) return;
      this.stack.length = this.index + 1;
      this.stack.push(snap);
      if (this.stack.length > 400) this.stack.shift();
      this.index = this.stack.length - 1;
    }
    // Refresh the caret stored with the current state so undo returns to where editing started.
    markCaret() {
      if (!this.pending) this.stack[this.index].sel = saveSelection();
    }
    undo() {
      this.commit();
      if (this.index === 0) return;
      this.index--;
      this.apply(this.stack[this.index]);
    }
    redo() {
      this.commit();
      if (this.index >= this.stack.length - 1) return;
      this.index++;
      this.apply(this.stack[this.index]);
    }
    apply(snap) {
      root.innerHTML = snap.html;
      restoreSelection(snap.sel);
      markDirty();
    }
  }

  E.undo = () => { if (history && !readOnly) history.undo(); };
  E.redo = () => { if (history && !readOnly) history.redo(); };

  // ----------------------------------------------------------- mount/save

  E.initialHtml = function () {
    const style = (N.state && N.state.settings.newNoteStyle) || 'title';
    const tag = STYLE_TAGS[style] || 'h1';
    return `<${tag}><br></${tag}>`;
  };

  E.mount = function (meta, body, opts = {}) {
    clearTimeout(saveTimer);
    note = meta;
    readOnly = !!opts.readOnly;
    const ed = document.createElement('div');
    ed.id = 'editor';
    ed.className = 'editor';
    ed.setAttribute('contenteditable', readOnly ? 'false' : 'true');
    ed.spellcheck = true;
    ed.innerHTML = body && body.trim() ? body : E.initialHtml();
    document.getElementById('editor').replaceWith(ed);
    root = ed;
    dirty = false;
    savedRange = null;
    history = new History();
    document.execCommand('defaultParagraphSeparator', false, 'div');
    hideTableTools();
    E.refreshHighlights(true);
    if (opts.focus) {
      root.focus({ preventScroll: true });
      if (opts.caret === 'start') placeCaret(root.firstElementChild || root, 0);
      else placeCaretAtEnd(root);
    }
  };

  E.unmount = function () {
    clearTimeout(saveTimer);
    note = null;
    readOnly = true;
    const ed = document.createElement('div');
    ed.id = 'editor';
    ed.className = 'editor';
    if (root) root.replaceWith(ed);
    else document.getElementById('editor').replaceWith(ed);
    root = ed;
    history = null;
    hideTableTools();
  };

  E.focus = function (where = 'end') {
    if (!root || readOnly) return;
    root.focus({ preventScroll: where !== 'end' });
    if (!rangeInEditor()) {
      if (where === 'start') placeCaret(root.firstElementChild || root, 0);
      else placeCaretAtEnd(root);
    }
  };

  function markDirty() {
    dirty = true;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => E.flush(), 450);
    refreshHighlightsSoon();
  }

  // Called after any edit (native or our own DOM surgery).
  E.changed = function () {
    if (!root || readOnly) return;
    history.touch();
    markDirty();
  };

  E.serialize = function (el = root) {
    let text = (el.innerText || '').replace(/\u00a0/g, ' ');
    // Placeholders for attachments iCloud didn't copy aren't part of the note's text.
    for (const m of el.querySelectorAll('.nt-missing')) text = text.split(m.textContent).join('');
    const lines = text.split('\n').map((s) => s.trim()).filter(Boolean);
    const tags = new Set();
    for (const m of text.matchAll(/(?<![\p{L}\p{N}#&/])#([\p{L}\p{N}][\p{L}\p{N}_\-]*)/gu)) tags.add(m[1].toLowerCase());
    const img = el.querySelector('img.nt-img');
    return {
      body: el.innerHTML,
      title: (lines[0] || '').slice(0, 200),
      snippet: lines.slice(1).join(' ').slice(0, 240),
      plain: text.slice(0, 200000),
      tags: Array.from(tags),
      thumbnail: img ? img.getAttribute('src') : null,
    };
  };

  E.flush = async function () {
    clearTimeout(saveTimer);
    if (!dirty || !note || readOnly) return;
    dirty = false;
    const id = note.id;
    const data = E.serialize();
    try {
      const meta = await N.call('saveNote', id, data);
      N.app.noteSaved(meta);
    } catch (err) {
      dirty = true;
      N.send('log', 'save failed', String(err));
    }
  };

  E.isEmpty = function () {
    if (!root) return true;
    return !root.innerText.trim() && !root.querySelector('img, table, .nt-file');
  };

  E.contentText = () => (root ? root.innerText : '');

  // ------------------------------------------------------------ formatting

  E.state = function () {
    const r = rangeInEditor() || savedRange;
    const n = r ? r.startContainer : null;
    const q = (c) => { try { return document.queryCommandState(c); } catch (_) { return false; } };
    const list = n && closestIn(n, 'ul, ol');
    const heading = !!(n && closestIn(n, 'h1, h2, h3'));
    return {
      style: closestIn(n, 'h1') ? 'title' : closestIn(n, 'h2') ? 'heading' : closestIn(n, 'h3') ? 'subheading'
        : closestIn(n, 'pre') ? 'mono' : 'body',
      bold: !heading && q('bold'), italic: q('italic'), underline: q('underline'), strike: q('strikeThrough'),
      list: list ? kindOf(list) : null,
      quote: !!(n && closestIn(n, 'blockquote')),
    };
  };

  E.setStyle = function (style) {
    if (readOnly || !STYLE_TAGS[style]) return;
    ensureSelection();
    history.commit();
    const r = rangeInEditor();
    const list = r && closestIn(r.startContainer, 'ul, ol');
    if (list) exec(list.tagName === 'OL' ? 'insertOrderedList' : 'insertUnorderedList');
    exec('formatBlock', `<${STYLE_TAGS[style]}>`);
    history.commit();
  };

  E.toggleInline = function (kind) {
    if (readOnly) return;
    exec({ bold: 'bold', italic: 'italic', underline: 'underline', strike: 'strikeThrough' }[kind]);
  };

  E.setColor = function (color) {
    if (readOnly) return;
    ensureSelection();
    const r = rangeInEditor();
    if (r && r.collapsed) {
      const block = blockOf(r.startContainer);
      if (block) { const all = document.createRange(); all.selectNodeContents(block); sel().removeAllRanges(); sel().addRange(all); }
    }
    document.execCommand('styleWithCSS', false, true);
    exec('foreColor', color || 'inherit');
    document.execCommand('styleWithCSS', false, false);
  };

  function kindOf(list) {
    if (list.tagName === 'OL') return 'numbered';
    if (list.classList.contains('checklist')) return 'checklist';
    if (list.classList.contains('dashed')) return 'dashed';
    return 'bullet';
  }

  function setKind(list, kind) {
    list.classList.remove('checklist', 'dashed', 'bullet');
    if (kind === 'checklist' || kind === 'dashed') list.classList.add(kind);
    if (!list.className) list.removeAttribute('class');
    if (kind !== 'checklist') N.$$(':scope > li[data-checked]', list).forEach((li) => li.removeAttribute('data-checked'));
  }

  // Lists are built by hand: WebKit's insert*List merges neighbouring lists and wraps them in divs.

  // The block holding `node`; bare inline content at the root is wrapped in a div first.
  function lineBlock(node) {
    const b = blockOf(node);
    if (b) return b;
    let n = node;
    while (n && n.parentNode !== root) n = n.parentNode;
    if (!n) return null;
    const isInline = (x) => x && !(x.nodeType === 1 && /^(DIV|P|H1|H2|H3|PRE|UL|OL|BLOCKQUOTE|TABLE)$/.test(x.tagName));
    let start = n;
    while (isInline(start.previousSibling)) start = start.previousSibling;
    const div = document.createElement('div');
    start.before(div);
    let cur = start;
    while (isInline(cur)) { const next = cur.nextSibling; div.appendChild(cur); cur = next; }
    return div;
  }

  function selectedSiblings(startEl, endEl) {
    if (startEl === endEl || startEl.parentNode !== endEl.parentNode) return [startEl];
    const out = [];
    for (let n = startEl; n; n = n.nextElementSibling) { out.push(n); if (n === endEl) break; }
    return out;
  }

  function caretOffsetIn(el) {
    const r = rangeInEditor();
    if (!r || !el.contains(r.startContainer)) return null;
    return saveSelectionRelative(el);
  }

  function makeList(blocks, kind) {
    const caretBlock = blocks.find((b) => caretOffsetIn(b) !== null);
    const offset = caretBlock ? caretOffsetIn(caretBlock) : null;
    const list = document.createElement(kind === 'numbered' ? 'ol' : 'ul');
    setKind(list, kind);
    blocks[0].before(list);
    let caretLi = null;
    for (const b of blocks) {
      const li = document.createElement('li');
      while (b.firstChild) li.appendChild(b.firstChild);
      if (!li.textContent && !li.querySelector('img, br')) li.innerHTML = '<br>';
      list.appendChild(li);
      if (b === caretBlock) caretLi = li;
      b.remove();
    }
    // Join with an adjacent list of exactly the same kind, like Notes does.
    const prev = list.previousElementSibling;
    if (prev && prev.tagName === list.tagName && kindOf(prev) === kind) { while (list.firstChild) prev.appendChild(list.firstChild); list.remove(); }
    restoreSelectionRelative(caretLi || (list.isConnected ? list.lastElementChild : prev.lastElementChild), offset ?? 1e9);
  }

  function unList(items) {
    const list = items[0].parentElement;
    const caretItem = items.find((li) => caretOffsetIn(li) !== null);
    const offset = caretItem ? caretOffsetIn(caretItem) : null;
    const tail = list.cloneNode(false);
    let n = items[items.length - 1].nextSibling;
    while (n) { const next = n.nextSibling; tail.appendChild(n); n = next; }
    let anchor = list;
    let caretDiv = null;
    for (const li of items) {
      const div = document.createElement('div');
      while (li.firstChild) div.appendChild(li.firstChild);
      if (!div.firstChild) div.innerHTML = '<br>';
      anchor.after(div);
      anchor = div;
      if (li === caretItem) caretDiv = div;
      li.remove();
    }
    if (tail.firstChild) anchor.after(tail);
    if (!list.firstElementChild) list.remove();
    if (caretDiv) restoreSelectionRelative(caretDiv, offset);
  }

  E.toggleList = function (kind) {
    if (readOnly) return;
    ensureSelection();
    history.commit();
    const r = rangeInEditor();
    const startLi = closestIn(r.startContainer, 'li');
    const list = startLi && startLi.parentElement;
    if (list && /^(UL|OL)$/.test(list.tagName)) {
      const endLi = closestIn(r.endContainer, 'li');
      const items = endLi && endLi.parentElement === list ? selectedSiblings(startLi, endLi) : [startLi];
      const wantTag = kind === 'numbered' ? 'OL' : 'UL';
      if (kindOf(list) === kind) unList(items);
      else if (list.tagName === wantTag) setKind(list, kind);
      else {
        const replacement = document.createElement(wantTag.toLowerCase());
        while (list.firstChild) replacement.appendChild(list.firstChild);
        const off = caretOffsetIn(startLi);
        list.replaceWith(replacement);
        setKind(replacement, kind);
        restoreSelectionRelative(startLi, off);
      }
    } else {
      const a = lineBlock(r.startContainer);
      const b = r.collapsed ? a : lineBlock(r.endContainer) || a;
      if (!a || a.tagName === 'TD') return;
      makeList(selectedSiblings(a, b).filter((x) => !/^(UL|OL|TABLE)$/.test(x.tagName)), kind);
    }
    E.changed();
    history.commit();
  };

  // Enter in an empty list item leaves the list (or outdents a nested one).
  function exitEmptyListItem() {
    const r = rangeInEditor();
    const li = r && r.collapsed && closestIn(r.startContainer, 'li');
    if (!li || li.textContent.replace(/​/g, '') !== '' || li.querySelector('img, ul, ol')) return false;
    history.commit();
    if (li.parentElement.parentElement.closest('ul, ol') && li.parentElement.parentElement !== root) exec('outdent');
    else unList([li]);
    E.changed();
    history.commit();
    return true;
  }

  function toggleChecked(li) {
    history.commit();
    const checked = li.getAttribute('data-checked') === 'true';
    if (checked) li.removeAttribute('data-checked');
    else li.setAttribute('data-checked', 'true');
    if (!checked && N.state.settings.sortChecked) li.parentElement.appendChild(li);
    if (checked && N.state.settings.sortChecked) {
      const firstChecked = N.$(':scope > li[data-checked="true"]', li.parentElement);
      if (firstChecked && firstChecked !== li) firstChecked.before(li);
    }
    E.changed();
    history.commit();
  }

  E.toggleCheckedAtCaret = function () {
    if (readOnly) return;
    const r = rangeInEditor();
    const li = r && closestIn(r.startContainer, 'ul.checklist > li');
    if (li) toggleChecked(li);
  };

  E.indent = function () {
    if (readOnly) return;
    ensureSelection();
    history.commit();
    const r = rangeInEditor();
    const li = closestIn(r.startContainer, 'li');
    if (li) {
      const parent = li.parentElement;
      exec('indent');
      const nl = closestIn(sel().anchorNode, 'ul, ol');
      if (nl && nl !== parent) setKind(nl, kindOf(parent));
    } else {
      const block = blockOf(r.startContainer) || root.firstElementChild;
      if (!block || block.tagName === 'TD') return;
      block.dataset.indent = Math.min(8, (+block.dataset.indent || 0) + 1);
      E.changed();
    }
    history.commit();
  };

  E.outdent = function () {
    if (readOnly) return;
    ensureSelection();
    history.commit();
    const r = rangeInEditor();
    if (closestIn(r.startContainer, 'li')) exec('outdent');
    else {
      const block = blockOf(r.startContainer);
      if (block && block.dataset.indent) {
        const n = +block.dataset.indent - 1;
        if (n > 0) block.dataset.indent = n; else delete block.dataset.indent;
        E.changed();
      }
    }
    history.commit();
  };

  E.toggleQuote = function () {
    if (readOnly) return;
    ensureSelection();
    history.commit();
    const r = rangeInEditor();
    const q = closestIn(r.startContainer, 'blockquote');
    if (q) {
      const div = document.createElement('div');
      while (q.firstChild) div.appendChild(q.firstChild);
      if (!div.firstChild) div.innerHTML = '<br>';
      const s = saveSelectionRelative(q);
      q.replaceWith(div);
      restoreSelectionRelative(div, s);
      E.changed();
    } else exec('formatBlock', '<blockquote>');
    history.commit();
  };

  function saveSelectionRelative(el) {
    const r = rangeInEditor();
    if (!r) return null;
    const pre = document.createRange();
    pre.selectNodeContents(el);
    pre.setEnd(r.startContainer, r.startOffset);
    return pre.toString().length;
  }

  function restoreSelectionRelative(el, offset) {
    if (offset === null) { placeCaretAtEnd(el); return; }
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      if (offset <= n.length) { placeCaret(n, offset); return; }
      offset -= n.length;
    }
    placeCaretAtEnd(el);
  }

  // ---------------------------------------------------------------- tables

  const newCell = () => { const td = document.createElement('td'); td.innerHTML = '<br>'; return td; };

  E.insertTable = function (rows = 2, cols = 2) {
    if (readOnly) return;
    ensureSelection();
    const r = rangeInEditor();
    if (r && closestIn(r.startContainer, 'table')) return;
    history.commit();
    const row = '<tr>' + '<td><br></td>'.repeat(cols) + '</tr>';
    const table = N.el(`<table class="nt-table"><tbody>${row.repeat(rows)}</tbody></table>`);
    const after = N.el('<div><br></div>');
    // Place the table at block level: replace an empty line, otherwise go below the current one.
    let block = lineBlock(r.startContainer);
    while (block && block.parentNode !== root) block = block.parentNode;
    if (!block) root.append(table, after);
    else if (!block.textContent.trim() && !block.querySelector('img, table, .nt-file')) block.replaceWith(table, after);
    else block.after(table, after);
    placeCaret(table.querySelector('td'), 0);
    E.changed();
    history.commit();
  };

  function tableOp(op, td) {
    const tr = td.parentElement;
    const table = td.closest('table');
    const ci = Array.prototype.indexOf.call(tr.children, td);
    history.commit();
    switch (op) {
      case 'rowAbove':
      case 'rowBelow': {
        const nr = document.createElement('tr');
        for (let k = 0; k < tr.children.length; k++) nr.appendChild(newCell());
        if (op === 'rowAbove') tr.before(nr); else tr.after(nr);
        placeCaret(nr.children[ci], 0);
        break;
      }
      case 'colLeft':
      case 'colRight':
        N.$$('tr', table).forEach((row) => {
          const ref = row.children[ci];
          const c = newCell();
          if (!ref) row.appendChild(c);
          else if (op === 'colLeft') ref.before(c);
          else ref.after(c);
        });
        placeCaret(tr.children[op === 'colLeft' ? ci : ci + 1], 0);
        break;
      case 'deleteRow': {
        if (N.$$('tr', table).length <= 1) { removeTable(table); break; }
        const next = tr.nextElementSibling || tr.previousElementSibling;
        tr.remove();
        placeCaret(next.children[Math.min(ci, next.children.length - 1)], 0);
        break;
      }
      case 'deleteCol':
        if (tr.children.length <= 1) { removeTable(table); break; }
        N.$$('tr', table).forEach((row) => row.children[ci] && row.children[ci].remove());
        placeCaret(tr.children[Math.min(ci, tr.children.length - 1)], 0);
        break;
      case 'deleteTable':
        removeTable(table);
        break;
    }
    E.changed();
    history.commit();
    updateTableTools();
  }

  function removeTable(table) {
    const div = document.createElement('div');
    div.innerHTML = '<br>';
    table.replaceWith(div);
    placeCaret(div, 0);
  }

  function moveCell(td, backwards) {
    const cells = N.$$('td', td.closest('table'));
    const i = cells.indexOf(td);
    let target = cells[i + (backwards ? -1 : 1)];
    if (!target && !backwards) {
      tableOp('rowBelow', td);
      return;
    }
    if (target) {
      const r = document.createRange();
      r.selectNodeContents(target);
      sel().removeAllRanges();
      sel().addRange(r);
    }
  }

  function hideTableTools() {
    const tools = document.getElementById('table-tools');
    if (tools) tools.hidden = true;
  }

  function updateTableTools() {
    const tools = document.getElementById('table-tools');
    const r = rangeInEditor();
    const td = r && closestIn(r.startContainer, 'td');
    if (!td || readOnly) { tools.hidden = true; return; }
    if (!tools.firstChild) {
      const btn = (op, icon, title) => `<button data-tt="${op}" title="${title}">${N.icon(icon)}</button>`;
      tools.innerHTML = btn('rowAbove', 'rowAbove', 'Add Row Above') + btn('rowBelow', 'rowBelow', 'Add Row Below') +
        '<span class="tt-sep"></span>' + btn('colLeft', 'colLeft', 'Add Column Before') +
        btn('colRight', 'colRight', 'Add Column After') + '<span class="tt-sep"></span>' +
        btn('deleteRow', 'rowDelete', 'Delete Row') + btn('deleteCol', 'colDelete', 'Delete Column') +
        btn('deleteTable', 'trash', 'Delete Table');
      tools.addEventListener('mousedown', (e) => {
        e.preventDefault();
        const b = e.target.closest('[data-tt]');
        if (b && tools._td && root.contains(tools._td)) tableOp(b.dataset.tt, tools._td);
      });
    }
    tools._td = td;
    const table = td.closest('table');
    const scroller = document.getElementById('editor-scroll');
    const tr = table.getBoundingClientRect();
    const sr = scroller.getBoundingClientRect();
    tools.hidden = false;
    tools.style.left = Math.max(8, tr.left - sr.left) + 'px';
    tools.style.top = (tr.top - sr.top + scroller.scrollTop - tools.offsetHeight - 6) + 'px';
  }

  // ----------------------------------------------------------- attachments

  function fileCard(name, size, uri) {
    return `<div><a class="nt-file" contenteditable="false" href="${N.esc(uri)}">` +
      `<span class="nt-file-icon">${N.icon('file')}</span><span class="nt-file-meta">` +
      `<span class="nt-file-name">${N.esc(name)}</span><span class="nt-file-size">${N.formatSize(size)}</span>` +
      '</span></a></div>';
  }

  E.saveRange = function () {
    const r = rangeInEditor() || savedRange;
    return r ? r.cloneRange() : null;
  };

  function useRange(range) {
    root.focus({ preventScroll: true });
    if (range && root.contains(range.startContainer)) {
      sel().removeAllRanges();
      sel().addRange(range);
    } else if (!rangeInEditor()) placeCaretAtEnd(root);
  }

  E.insertFiles = async function (files, range) {
    if (readOnly || !files.length) return;
    range = range || E.saveRange();
    let html = '';
    for (const f of files) {
      try {
        const res = await N.call('saveAttachment', f.name, await N.fileToBase64(f));
        html += f.type.startsWith('image/')
          ? `<div><img class="nt-img" src="${N.esc(res.uri)}" alt=""></div>`
          : fileCard(f.name, res.size, res.uri);
      } catch (err) {
        N.ui.toast(`Couldn't attach ${f.name}`);
      }
    }
    if (!html) return;
    useRange(range);
    history.commit();
    exec('insertHTML', html + '<div><br></div>');
    E.changed();
    history.commit();
  };

  E.insertImageData = async function (dataUrl, name, cls, range) {
    const res = await N.call('saveAttachment', name, dataUrl.split(',', 2)[1]);
    useRange(range);
    history.commit();
    exec('insertHTML', `<div><img class="nt-img ${cls || ''}" src="${N.esc(res.uri)}" alt=""></div><div><br></div>`);
    E.changed();
    history.commit();
  };

  async function convertDataImages() {
    for (const img of N.$$('img[src^="data:image/"]', root)) {
      const src = img.getAttribute('src');
      const ext = (src.match(/^data:image\/(\w+)/) || [])[1] || 'png';
      try {
        const res = await N.call('saveAttachment', 'pasted.' + ext, src.split(',', 2)[1]);
        img.setAttribute('src', res.uri);
      } catch (_) { /* keep inline data */ }
    }
    E.changed();
  }

  // ----------------------------------------------------- typing behaviours

  function autoFormat() {
    const r = rangeInEditor();
    if (!r || !r.collapsed) return false;
    const block = blockOf(r.startContainer);
    if (!block || /^(LI|TD|PRE|H1|H2|H3)$/.test(block.tagName)) return false;
    const pre = document.createRange();
    pre.setStart(block, 0);
    pre.setEnd(r.startContainer, r.startOffset);
    const marker = pre.toString();
    const kind = { '*': 'bullet', '•': 'bullet', '-': 'dashed', '–': 'dashed', '1.': 'numbered', '1)': 'numbered', '[]': 'checklist', '[ ]': 'checklist' }[marker];
    if (!kind) return false;
    history.commit();
    for (let i = 0; i < marker.length; i++) document.execCommand('delete', false, null);
    E.toggleList(kind);
    return true;
  }

  // Turn a URL just before the caret into a link. Returns true if it inserted `trailing` itself.
  function linkifyBeforeCaret(trailing) {
    const r = rangeInEditor();
    if (!r || !r.collapsed) return false;
    const node = r.startContainer;
    if (node.nodeType !== 3 || closestIn(node, 'a, pre')) return false;
    const before = node.nodeValue.slice(0, r.startOffset);
    const m = before.match(/(?:^|\s)((?:https?:\/\/|www\.)[^\s]+)$/i);
    if (!m) return false;
    const raw = m[1];
    const url = raw.replace(/[.,;:!?)\]'"]+$/, '');
    if (url.length < 5) return false;
    const start = r.startOffset - raw.length;
    const lr = document.createRange();
    lr.setStart(node, start);
    lr.setEnd(node, start + url.length);
    const a = document.createElement('a');
    a.href = /^www\./i.test(url) ? 'https://' + url : url;
    lr.surroundContents(a);
    let rest = a.nextSibling;
    if (!rest || rest.nodeType !== 3) { rest = document.createTextNode(''); a.after(rest); }
    let off = raw.length - url.length;
    if (trailing) { rest.insertData(off, trailing); off += trailing.length; }
    placeCaret(rest, off);
    E.changed();
    return !!trailing;
  }

  function onKeyDown(e) {
    if (!root || !(e.target === root || root.contains(e.target))) return;
    const mod = N.isMod(e);
    const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    if (mod && !e.altKey && key === 'z') { e.preventDefault(); if (e.shiftKey) E.redo(); else E.undo(); return; }
    if (mod && !e.altKey && key === 'y') { e.preventDefault(); E.redo(); return; }
    if (readOnly) return;
    if (mod && !e.shiftKey && !e.altKey && (key === 'b' || key === 'i' || key === 'u')) {
      e.preventDefault();
      E.toggleInline({ b: 'bold', i: 'italic', u: 'underline' }[key]);
      return;
    }
    if (mod && e.shiftKey && key === 'v') { plainPaste = true; return; }
    if (e.key === 'Tab' && !mod && !e.altKey) {
      e.preventDefault();
      const r = rangeInEditor();
      const td = r && closestIn(r.startContainer, 'td');
      if (td) moveCell(td, e.shiftKey);
      else if (r && closestIn(r.startContainer, 'li')) (e.shiftKey ? E.outdent : E.indent)();
      else if (!e.shiftKey) exec('insertText', '\t');
      return;
    }
    if (e.key === ' ' && !mod && !e.altKey) {
      if (autoFormat() || linkifyBeforeCaret(' ')) e.preventDefault();
      return;
    }
    if (e.key === 'Enter' && !mod && !e.shiftKey) {
      if (exitEmptyListItem()) { e.preventDefault(); return; }
      linkifyBeforeCaret(null);
      history.commit();
    }
    if (e.key === 'Backspace' && !mod) {
      // Backspace at the very start of a checklist/list item first removes the list formatting.
      const r = rangeInEditor();
      const li = r && r.collapsed && closestIn(r.startContainer, 'li');
      if (li && atStartOf(li, r) && !li.previousElementSibling && !li.parentElement.parentElement.closest('ul, ol')) {
        e.preventDefault();
        history.commit();
        unList([li]);
        E.changed();
        history.commit();
      }
    }
  }

  function atStartOf(el, r) {
    const pre = document.createRange();
    pre.selectNodeContents(el);
    pre.setEnd(r.startContainer, r.startOffset);
    return pre.toString().length === 0;
  }

  function onBeforeInput(e) {
    if (e.inputType === 'historyUndo') { e.preventDefault(); E.undo(); }
    else if (e.inputType === 'historyRedo') { e.preventDefault(); E.redo(); }
    else if (history) history.markCaret();
  }

  function onInput(e) {
    if (!root || e.target !== root) return;
    if (e.inputType === 'insertParagraph') {
      const li = closestIn(sel().anchorNode, 'ul.checklist > li');
      if (li) li.removeAttribute('data-checked');
      const block = blockOf(sel().anchorNode);
      if (block && block.dataset && block.dataset.indent && !block.textContent) delete block.dataset.indent;
    }
    if (!root.firstChild || root.innerHTML === '<br>') {
      root.innerHTML = '<div><br></div>';
      placeCaret(root.firstChild, 0);
    }
    E.changed();
  }

  function onPaste(e) {
    if (!root || !root.contains(e.target)) return;
    e.preventDefault();
    if (readOnly) return;
    const dt = e.clipboardData;
    const files = Array.from(dt.files || []);
    const html = plainPaste ? '' : dt.getData('text/html');
    const text = N.convert.stripJunk(dt.getData('text/plain') || '');
    plainPaste = false;
    let clean = html ? N.convert.sanitize(html) : '';
    const probe = document.createElement('div');
    probe.innerHTML = clean;
    const htmlHasContent = !!(probe.textContent.trim() || probe.querySelector('table, img'));
    // A bare image (screenshots, images copied from a browser) is stored as an attachment.
    if (files.length && !(htmlHasContent && probe.textContent.trim())) { E.insertFiles(files); return; }
    history.commit();
    if (htmlHasContent) {
      insertPastedHtml(clean);
      if (root.querySelector('img[src^="data:image/"]')) convertDataImages();
    } else if (text) {
      const t = text.trim();
      const table = N.convert.tsvToTable(text);
      if (/^(https?:\/\/|www\.)\S+$/i.test(t)) {
        const href = /^www\./i.test(t) ? 'https://' + t : t;
        exec('insertHTML', `<a href="${N.esc(href)}">${N.esc(t)}</a>&nbsp;`);
      } else if (table) insertPastedHtml(table);
      else if (text.includes('\n')) insertPastedHtml(N.convert.plainToHtml(text));
      else exec('insertText', text);
    }
    E.changed();
    history.commit();
  }

  const PASTE_BLOCK = /^(DIV|P|H1|H2|H3|PRE|UL|OL|TABLE|BLOCKQUOTE)$/;
  const isBlockNode = (n) => n.nodeType === 1 && PASTE_BLOCK.test(n.tagName);
  const hasContent = (el) => !!(el.textContent.trim() || el.querySelector('img, table, .nt-file'));

  // Insert sanitised HTML at the caret. Multi-line content is spliced in as real blocks: the current
  // line is split at the caret, the first pasted line joins its start and the last joins its end.
  function insertPastedHtml(html) {
    const tmp = document.createElement('div');
    tmp.innerHTML = html;
    // Wrap stray inline runs between blocks into divs.
    let run = null;
    for (const n of Array.from(tmp.childNodes)) {
      if (isBlockNode(n)) { run = null; continue; }
      if (n.nodeType === 3 && !n.nodeValue.trim() && !run) { n.remove(); continue; }
      if (!run) { run = document.createElement('div'); n.before(run); }
      run.appendChild(n);
    }
    const nodes = Array.from(tmp.childNodes);
    if (!nodes.length) return;
    ensureSelection();
    let r = rangeInEditor();
    if (!r.collapsed) { document.execCommand('delete', false, null); r = rangeInEditor(); }
    const singleLine = nodes.length === 1 && nodes[0].tagName === 'DIV' && !nodes[0].querySelector('div, table, ul, ol');
    if (singleLine) { exec('insertHTML', nodes[0].innerHTML); return; }
    // Blocks can't go inside a list item, table cell or code block: paste below that whole block.
    const container = closestIn(r.startContainer, 'td, li, pre');
    if (container) {
      let top = container;
      while (top.parentNode !== root) top = top.parentNode;
      const after = document.createElement('div');
      after.innerHTML = '<br>';
      top.after(after);
      r = document.createRange();
      r.setStart(after, 0);
      r.collapse(true);
      sel().removeAllRanges();
      sel().addRange(r);
    }
    // The top-level block holding the caret (wrapping bare root text if needed).
    let blk = r.startContainer;
    if (blk === root) {
      blk = root.childNodes[r.startOffset] || null;
      if (!blk || !isBlockNode(blk)) { const d = document.createElement('div'); d.innerHTML = '<br>'; root.insertBefore(d, blk); blk = d; }
      r = document.createRange(); r.setStart(blk, 0); r.collapse(true);
    }
    while (blk.parentNode !== root) blk = blk.parentNode;
    if (!isBlockNode(blk)) blk = lineBlock(r.startContainer);
    // Split the current line at the caret.
    const tailRange = document.createRange();
    tailRange.setStart(r.startContainer, r.startOffset);
    tailRange.setEnd(blk, blk.childNodes.length);
    const tail = blk.cloneNode(false);
    tail.appendChild(tailRange.extractContents());
    const headEmpty = !hasContent(blk);
    const tailEmpty = !hasContent(tail);
    blk.querySelectorAll('br').forEach((br) => { if (!br.nextSibling && hasContent(blk)) br.remove(); });

    const mergeable = (n) => n && /^(DIV|P)$/.test(n.tagName) && !n.querySelector('div, table, ul, ol');
    let removeHead = false;
    if (mergeable(nodes[0])) {
      // First pasted line continues the current one (an empty title line becomes the pasted title).
      const first = nodes.shift();
      if (headEmpty) blk.replaceChildren();
      while (first.firstChild) blk.appendChild(first.firstChild);
    } else if (headEmpty) removeHead = true;

    let anchor = blk;
    for (const n of nodes) { anchor.after(n); anchor = n; }

    // Where the caret goes: the end of the last pasted line, before any text that followed the caret.
    let caretHost = nodes.length ? anchor : blk;
    while (caretHost && /^(UL|OL)$/.test(caretHost.tagName) && caretHost.lastElementChild) caretHost = caretHost.lastElementChild;
    if (caretHost.tagName === 'TABLE') {
      const line = tailEmpty ? document.createElement('div') : tail;
      if (tailEmpty) line.innerHTML = '<br>';
      caretHost.after(line);
      if (removeHead) blk.remove();
      placeCaret(line, 0);
      return;
    }
    const marker = document.createElement('span');
    caretHost.appendChild(marker);
    if (!tailEmpty) {
      if (mergeable(tail) || caretHost === blk || /^(DIV|P|LI|H1|H2|H3)$/.test(caretHost.tagName)) {
        while (tail.firstChild) caretHost.appendChild(tail.firstChild);
      } else anchor.after(tail);
    }
    if (removeHead) blk.remove();
    const cr = document.createRange();
    cr.setStartBefore(marker);
    cr.collapse(true);
    marker.remove();
    sel().removeAllRanges();
    sel().addRange(cr);
  }

  function onMouseDown(e) {
    if (!root || readOnly) return;
    const li = e.target.closest && e.target.closest('ul.checklist > li');
    if (li && root.contains(li)) {
      const r = li.getBoundingClientRect();
      if (e.clientX - r.left < 26 && e.button === 0) {
        e.preventDefault();
        toggleChecked(li);
        return;
      }
    }
    // Clicking the empty space under the text puts the caret at the end.
    if (e.target.id === 'editor-scroll' && e.clientY > root.getBoundingClientRect().bottom) {
      e.preventDefault();
      root.focus();
      placeCaretAtEnd(root);
    }
  }

  function onClick(e) {
    if (!root) return;
    const a = e.target.closest && e.target.closest('a[href]');
    if (a && root.contains(a)) {
      e.preventDefault();
      if (!sel().isCollapsed && !a.classList.contains('nt-file')) return;
      N.send('openExternal', a.getAttribute('href'));
    }
  }

  function onDblClick(e) {
    const img = e.target.closest && e.target.closest('img.nt-img');
    if (img && root.contains(img)) N.send('openExternal', img.getAttribute('src'));
  }

  function onDragOver(e) {
    if (readOnly || !e.dataTransfer) return;
    if (Array.from(e.dataTransfer.types).includes('Files')) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
    }
  }

  function onDrop(e) {
    if (readOnly || !e.dataTransfer || !e.dataTransfer.files.length) return;
    e.preventDefault();
    const pos = document.caretRangeFromPoint(e.clientX, e.clientY);
    E.insertFiles(Array.from(e.dataTransfer.files), pos && root.contains(pos.startContainer) ? pos : null);
  }

  // ------------------------------------------------------------ highlights

  const hasHighlights = !!(window.CSS && CSS.highlights && window.Highlight);

  E.refreshHighlights = function (scrollToMatch) {
    if (!hasHighlights || !root) return;
    const tags = [];
    const hits = [];
    const query = ((N.state && N.state.search) || '').trim().toLowerCase();
    const terms = query ? query.split(/\s+/) : [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      const text = n.nodeValue;
      for (const m of text.matchAll(/(?<![\p{L}\p{N}#&/])#[\p{L}\p{N}][\p{L}\p{N}_\-]*/gu)) {
        const r = new Range();
        r.setStart(n, m.index);
        r.setEnd(n, m.index + m[0].length);
        tags.push(r);
      }
      if (terms.length) {
        const lower = text.toLowerCase();
        for (const t of terms) {
          let i = lower.indexOf(t);
          while (i !== -1) {
            const r = new Range();
            r.setStart(n, i);
            r.setEnd(n, i + t.length);
            hits.push(r);
            i = lower.indexOf(t, i + t.length);
          }
        }
      }
    }
    CSS.highlights.set('nt-tag', new Highlight(...tags));
    CSS.highlights.set('nt-search', new Highlight(...hits));
    if (scrollToMatch && hits.length) {
      const scroller = document.getElementById('editor-scroll');
      const rr = hits[0].getBoundingClientRect();
      const sr = scroller.getBoundingClientRect();
      if (rr.bottom > sr.bottom || rr.top < sr.top) scroller.scrollTop += rr.top - sr.top - sr.height / 3;
    }
  };

  const refreshHighlightsSoon = N.debounce(() => E.refreshHighlights(false), 250);

  // ------------------------------------------------------------------ init

  E.init = function () {
    const scroller = document.getElementById('editor-scroll');
    scroller.addEventListener('keydown', onKeyDown);
    scroller.addEventListener('beforeinput', onBeforeInput);
    scroller.addEventListener('input', onInput);
    scroller.addEventListener('paste', onPaste);
    scroller.addEventListener('mousedown', onMouseDown);
    scroller.addEventListener('click', onClick);
    scroller.addEventListener('dblclick', onDblClick);
    scroller.addEventListener('dragover', onDragOver);
    scroller.addEventListener('drop', onDrop);
    scroller.addEventListener('scroll', () => {
      const tools = document.getElementById('table-tools');
      if (!tools.hidden) updateTableTools();
    });
    document.addEventListener('selectionchange', () => {
      const r = rangeInEditor();
      if (r) savedRange = r.cloneRange();
      updateTableTools();
    });
  };
})(window.N = window.N || {});
