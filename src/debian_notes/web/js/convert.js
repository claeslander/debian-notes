// Conversions between note HTML and the outside world: paste sanitising, Markdown, plain text, export.
(function (N) {
  const C = (N.convert = {});

  // -------------------------------------------------------------- sanitize

  const RENAME = {
    STRONG: 'b', B: 'b', EM: 'i', I: 'i', U: 'u', INS: 'u', S: 's', STRIKE: 's', DEL: 's',
    H1: 'h1', H2: 'h2', H3: 'h3', H4: 'h3', H5: 'h3', H6: 'h3',
    P: 'div', DIV: 'div', SECTION: 'div', ARTICLE: 'div', HEADER: 'div', FOOTER: 'div', MAIN: 'div',
    UL: 'ul', OL: 'ol', LI: 'li', PRE: 'pre', BLOCKQUOTE: 'blockquote', BR: 'br', A: 'a', IMG: 'img',
    TABLE: 'table', TBODY: 'tbody', THEAD: 'tbody', TFOOT: 'tbody', TR: 'tr', TD: 'td', TH: 'td',
    CODE: 'code', SUB: 'sub', SUP: 'sup',
  };
  // Dropped with their content. <object>/<embed> are *unwrapped* instead: Apple Notes wraps tables in <object>.
  const DROP = new Set(['SCRIPT', 'STYLE', 'META', 'LINK', 'TITLE', 'HEAD', 'NOSCRIPT', 'IFRAME', 'SVG',
    'CANVAS', 'VIDEO', 'AUDIO', 'FORM', 'INPUT', 'BUTTON', 'SELECT', 'TEXTAREA', 'TEMPLATE', 'COLGROUP', 'COL']);
  const KEEP_CLASSES = new Set(['nt-missing', 'checklist', 'dashed', 'bullet', 'nt-table', 'nt-img', 'nt-sketch', 'nt-file', 'nt-audio',
    'nt-file-icon', 'nt-file-meta', 'nt-file-name', 'nt-file-size']);
  // U+FFFC marks an attachment (table, image, drawing) in Apple's text; it renders as an "OBJ" box.
  const JUNK_CHARS = /[\uFFFC\uFFFD\u200B\u2028\u2029]/g;
  const MONO_FONT = /mono|menlo|courier|consolas|sf mono|ui-monospace/i;

  C.stripJunk = (text) => text.replace(JUNK_CHARS, '');

  C.sanitize = function (html) {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    appleWebNotes(doc);
    const out = document.createElement('div');
    walk(doc.body, out);
    fixHeadings(out);
    fixLists(out);
    fixTables(out);
    liftBlocks(out);
    return out.innerHTML;
  };

  // Tables and lists must sit at the top level, not inside a <div> line; empty <div>s are dropped.
  function liftBlocks(root) {
    for (const el of Array.from(root.querySelectorAll('table, ul, ol'))) {
      const parent = el.parentElement;
      if (!parent || parent === root || parent.tagName !== 'DIV' || el.closest('td, li') !== null && el.closest('td, li') !== el) continue;
      const before = document.createElement('div');
      const after = document.createElement('div');
      while (parent.firstChild !== el) before.appendChild(parent.firstChild);
      parent.removeChild(el);
      while (parent.firstChild) after.appendChild(parent.firstChild);
      const keep = (d) => d.textContent.trim() || d.querySelector('img, .nt-file');
      parent.replaceWith(...[keep(before) && before, el, keep(after) && after].filter(Boolean));
    }
    root.querySelectorAll('div').forEach((d) => { if (!d.firstChild) d.remove(); });
  }

  // ------------------------------------------------- iCloud.com Notes clipboard

  // iCloud's web Notes tags every run with data-tt='{"paragraphStyle":{"style":N,...},"attachmentInfo":{...}}'.
  // Paragraph styles follow Notes' own numbering; attachments (tables, images, drawings) arrive only as
  // a U+FFFC placeholder, so we show a visible marker instead of silently dropping them.
  const APPLE_STYLES = { 0: 'h1', 1: 'h2', 2: 'h3', 4: 'pre', 100: 'bullet', 101: 'dashed', 102: 'numbered', 103: 'checklist' };
  const ATTACHMENT_NAMES = { 'com.apple.notes.table': 'Table', 'com.apple.drawing': 'Drawing', 'com.apple.drawing.2': 'Drawing',
    'com.apple.notes.gallery': 'Scan', 'public.url': 'Link', 'com.apple.m4a-audio': 'Audio recording' };

  function parseTT(el) {
    try { return JSON.parse(el.getAttribute('data-tt')); } catch (_) { return null; }
  }

  function appleWebNotes(doc) {
    if (!doc.querySelector('[data-tt]')) return;
    // Attachment placeholders -> visible markers.
    doc.querySelectorAll('span[data-tt]').forEach((span) => {
      const info = parseTT(span);
      const att = info && info.attachmentInfo;
      if (!att) return;
      const uti = att.typeUTI || '';
      const name = ATTACHMENT_NAMES[uti] || (/image|jpeg|png|heic/.test(uti) ? 'Image' : /pdf/.test(uti) ? 'PDF' : 'Attachment');
      const marker = doc.createElement('span');
      marker.className = 'nt-missing';
      marker.setAttribute('data-kind', name);
      marker.textContent = `${name} not included: iCloud doesn't copy attachments`;
      span.replaceWith(marker);
    });
    // Paragraph styles -> headings, code and lists (consecutive list paragraphs are grouped).
    let list = null;
    let listKind = null;
    for (const p of Array.from(doc.body.children)) {
      const first = p.querySelector('span[data-tt]');
      const info = first && parseTT(first);
      const ps = (info && info.paragraphStyle) || {};
      const kind = APPLE_STYLES[ps.style];
      const raw = first ? first.getAttribute('data-tt') : '';
      if (kind === 'bullet' || kind === 'dashed' || kind === 'numbered' || kind === 'checklist') {
        if (!list || listKind !== kind) {
          list = doc.createElement(kind === 'numbered' ? 'ol' : 'ul');
          if (kind === 'dashed' || kind === 'checklist') list.className = kind;
          p.before(list);
          listKind = kind;
        }
        const li = doc.createElement('li');
        if (kind === 'checklist' && /"done"\s*:\s*(true|1)/.test(raw)) li.setAttribute('data-checked', 'true');
        while (p.firstChild) li.appendChild(p.firstChild);
        list.appendChild(li);
        p.remove();
        continue;
      }
      list = null;
      if (kind === 'h1' || kind === 'h2' || kind === 'h3' || kind === 'pre') {
        const el = doc.createElement(kind);
        while (p.firstChild) el.appendChild(p.firstChild);
        p.replaceWith(el);
      }
    }
    // Runs end with a literal newline inside pre-wrap spans; those aren't line breaks for us.
    doc.querySelectorAll('span[data-tt]').forEach((span) => {
      const t = span.lastChild;
      if (t && t.nodeType === 3) t.nodeValue = t.nodeValue.replace(/\n$/, '');
      // Headings are bold already.
      if (span.closest('h1, h2, h3')) span.style.fontWeight = '';
    });
  }

  function fontSizePx(style) {
    const m = (style && style.fontSize || '').match(/^([\d.]+)(px|pt)$/);
    if (!m) return 0;
    return m[2] === 'pt' ? parseFloat(m[1]) * 4 / 3 : parseFloat(m[1]);
  }

  function walk(src, dst) {
    for (const node of Array.from(src.childNodes)) {
      if (node.nodeType === 3) {
        dst.appendChild(document.createTextNode(node.nodeValue.replace(JUNK_CHARS, '').replace(/[\r\n]+/g, ' ').replace(/[ \t]{2,}/g, ' ')));
        continue;
      }
      if (node.nodeType !== 1 || DROP.has(node.tagName)) continue;
      if (node.classList && node.classList.contains('nt-missing')) {
        const m = document.createElement('span');
        m.className = 'nt-missing';
        m.setAttribute('contenteditable', 'false');
        m.textContent = node.textContent;
        dst.appendChild(m);
        continue;
      }
      const tag = RENAME[node.tagName];
      if (!tag) { walkStyled(node, dst); continue; }
      let el;
      if (tag === 'code') {
        if (node.parentNode && node.parentNode.tagName === 'PRE') { walk(node, dst); continue; }
        el = document.createElement('span');
        el.style.fontFamily = 'var(--mono-font)';
      } else {
        el = document.createElement(tag);
        if (tag === 'a') {
          const href = node.getAttribute('href') || '';
          if (/^(https?:|mailto:|tel:|file:|notes-locked:)/i.test(href)) el.setAttribute('href', href);
        } else if (tag === 'img') {
          // Safari/Notes put placeholders like webkit-fake-url:// on the clipboard; they never load.
          const src = node.getAttribute('src') || '';
          if (!/^(https?:|file:|notes-locked:|data:image\/)/i.test(src)) continue;
          el.setAttribute('src', src);
          el.className = node.classList.contains('nt-sketch') ? 'nt-img nt-sketch' : 'nt-img';
          dst.appendChild(el);
          continue;
        }
        const cls = Array.from(node.classList).filter((c) => KEEP_CLASSES.has(c));
        if (cls.length) el.className = cls.join(' ');
        if (tag === 'ul' && /dash/i.test(node.className)) el.className = 'dashed';
        if (tag === 'ul' && /check|todo/i.test(node.className)) el.className = 'checklist';
        if (tag === 'li') markChecklistItem(node, el);
        if (node.getAttribute('contenteditable') === 'false' && cls.includes('nt-file')) el.setAttribute('contenteditable', 'false');
        // Audio cards keep what the editor rebuilds their player from.
        if (cls.includes('nt-audio')) {
          for (const k of ['title', 'data-duration', 'data-peaks']) if (node.hasAttribute(k)) el.setAttribute(k, node.getAttribute(k));
        }
        if (tag === 'table') el.className = 'nt-table';
        if (/^(div|h1|h2|h3)$/.test(tag) && MONO_FONT.test(node.style.fontFamily || '')) el = document.createElement('pre');
      }
      if (el.tagName === 'PRE') {
        el.textContent = textWithBreaks(node).replace(JUNK_CHARS, '');
        dst.appendChild(el);
        continue;
      }
      walk(node, el);
      dst.appendChild(el);
    }
  }

  // Spans, fonts and other inline wrappers: keep only what their inline style means.
  function walkStyled(node, dst) {
    const st = node.style || {};
    let outer = null;
    let inner = null;
    const wrap = (el) => { if (inner) inner.appendChild(el); else outer = el; inner = el; };
    const size = fontSizePx(st);
    if (size >= 15) { const s = document.createElement('span'); s.dataset.size = Math.round(size); wrap(s); }
    if (/^(bold|bolder|[6-9]00)$/.test(st.fontWeight || '')) wrap(document.createElement('b'));
    if (st.fontStyle === 'italic') wrap(document.createElement('i'));
    if (/underline/.test(st.textDecoration || st.textDecorationLine || '')) wrap(document.createElement('u'));
    if (/line-through/.test(st.textDecoration || st.textDecorationLine || '')) wrap(document.createElement('s'));
    if (MONO_FONT.test(st.fontFamily || '') || node.tagName === 'TT') {
      const m = document.createElement('span');
      m.style.fontFamily = 'var(--mono-font)';
      wrap(m);
    }
    if (!outer) { walk(node, dst); return; }
    walk(node, inner);
    dst.appendChild(outer);
  }

  function textWithBreaks(node) {
    let out = '';
    node.childNodes.forEach((c) => {
      if (c.nodeType === 3) out += c.nodeValue;
      else if (c.nodeName === 'BR') out += '\n';
      else if (c.nodeType === 1) out += textWithBreaks(c) + (/^(DIV|P|LI)$/.test(c.nodeName) ? '\n' : '');
    });
    return out.replace(/\n$/, '');
  }

  // Checklist items arrive as <li><input type="checkbox" checked>…, or with checked/done classes and attributes.
  function markChecklistItem(src, li) {
    if (src.getAttribute('data-checked') === 'true') { li.setAttribute('data-checked', 'true'); li.dataset.cl = '1'; return; }
    const box = src.querySelector('input[type="checkbox"]');
    const cls = src.className || '';
    const aria = src.getAttribute('aria-checked') || src.getAttribute('data-checked');
    if (box || /check|todo|task/i.test(cls) || aria !== null) {
      li.dataset.cl = '1';
      if ((box && (box.checked || box.hasAttribute('checked'))) || /\b(checked|done|complete)/i.test(cls) || aria === 'true') {
        li.setAttribute('data-checked', 'true');
      }
    }
  }

  // Apple writes titles as <div><b><span style="font-size: 24px">…</span></b></div>; turn those into real headings.
  function fixHeadings(root) {
    for (const block of Array.from(root.querySelectorAll('div'))) {
      const sized = Array.from(block.querySelectorAll('[data-size]'));
      if (!sized.length || block.querySelector('div, table, ul, ol, img')) continue;
      const text = block.textContent.trim();
      const sizedText = sized.map((s) => s.textContent).join('').trim();
      if (!text || text !== sizedText) continue;
      const size = Math.max(...sized.map((s) => +s.dataset.size));
      const tag = size >= 22 ? 'h1' : size >= 17 ? 'h2' : null;
      if (!tag) continue;
      const h = document.createElement(tag);
      h.innerHTML = block.innerHTML;
      // Headings are bold already; drop the redundant <b>.
      h.querySelectorAll('b').forEach((b) => b.replaceWith(...b.childNodes));
      block.replaceWith(h);
    }
    root.querySelectorAll('[data-size]').forEach((s) => s.replaceWith(...s.childNodes));
  }

  function fixLists(root) {
    root.querySelectorAll('ul, ol').forEach((list) => {
      const items = Array.from(list.children).filter((c) => c.tagName === 'LI');
      if (items.length && items.some((li) => li.dataset.cl)) {
        const ul = list.tagName === 'UL' ? list : document.createElement('ul');
        if (ul !== list) { while (list.firstChild) ul.appendChild(list.firstChild); list.replaceWith(ul); }
        ul.className = 'checklist';
      }
    });
    root.querySelectorAll('li[data-cl]').forEach((li) => li.removeAttribute('data-cl'));
  }

  // Make every pasted table a clean grid: one tbody, equal column counts, no empty cells, unwrapped single divs.
  function fixTables(root) {
    root.querySelectorAll('table').forEach((table) => {
      const rows = Array.from(table.querySelectorAll('tr')).filter((tr) => tr.closest('table') === table);
      if (!rows.length) { table.remove(); return; }
      const tbody = document.createElement('tbody');
      const cols = Math.max(...rows.map((tr) => Array.from(tr.children).filter((c) => c.tagName === 'TD').length), 1);
      for (const tr of rows) {
        const row = document.createElement('tr');
        const cells = Array.from(tr.children).filter((c) => c.tagName === 'TD');
        for (let i = 0; i < cols; i++) {
          const td = cells[i] || document.createElement('td');
          // Trim trailing empty lines Apple leaves in cells.
          while (td.lastChild && ((td.lastChild.nodeType === 3 && !td.lastChild.nodeValue.trim()) ||
            (td.lastChild.nodeName === 'DIV' && !td.lastChild.textContent.trim() && !td.lastChild.querySelector('img')))) {
            td.lastChild.remove();
          }
          // A cell holding exactly one <div> is just text.
          if (td.children.length === 1 && td.firstElementChild.tagName === 'DIV' && td.childNodes.length === 1) {
            td.replaceChildren(...td.firstElementChild.childNodes);
          }
          if (!td.textContent.trim() && !td.querySelector('img')) td.innerHTML = '<br>';
          row.appendChild(td);
        }
        tbody.appendChild(row);
      }
      table.replaceChildren(tbody);
      table.className = 'nt-table';
    });
  }

  // Plain text from Notes and similar apps: keep lines, turn "1. " / "- " / "• " runs into real lists.
  C.plainToHtml = function (text) {
    const lines = text.replace(/\r\n?/g, '\n').replace(JUNK_CHARS, '').split('\n').map((l) => l.replace(/\s+$/, ''));
    const out = [];
    let list = null;
    const close = () => { if (list) { out.push(`</${list.split(' ')[0]}>`); list = null; } };
    for (const line of lines) {
      const num = line.match(/^\s*\d+[.)]\s+(.*)$/);
      const dash = line.match(/^\s*[-–—]\s+(.*)$/);
      const bullet = line.match(/^\s*[•*◦▪]\s+(.*)$/);
      const item = num || dash || bullet;
      const want = num ? 'ol' : dash ? 'ul class="dashed"' : bullet ? 'ul' : null;
      if (item) {
        if (list !== want) { close(); out.push(`<${want}>`); list = want; }
        out.push(`<li>${linkifyEscaped(N.esc(item[1].trim())) || '<br>'}</li>`);
        continue;
      }
      // A blank line between list items doesn't end the list in Notes' text.
      if (!line.trim() && list) continue;
      close();
      out.push(`<div>${line.trim() ? linkifyEscaped(N.esc(line)) : '<br>'}</div>`);
    }
    close();
    return out.join('');
  };

  // Tab-separated text (what spreadsheets and Notes put on the clipboard for tables) becomes a table.
  C.tsvToTable = function (text) {
    const lines = text.replace(/\r\n?/g, '\n').replace(/\n+$/, '').split('\n');
    if (lines.length < 2 && !(lines[0] || '').includes('\t')) return null;
    const rows = lines.map((l) => l.split('\t'));
    const cols = rows[0].length;
    if (cols < 2 || rows.some((r) => r.length !== cols)) return null;
    return '<table class="nt-table"><tbody>' + rows.map((r) =>
      '<tr>' + r.map((c) => `<td>${c.trim() ? N.esc(c.trim()) : '<br>'}</td>`).join('') + '</tr>').join('') +
      '</tbody></table>';
  };

  // ----------------------------------------------------- plain text -> html

  C.textToHtml = function (text) {
    return text.replace(/\r\n?/g, '\n').split('\n')
      .map((line) => `<div>${line ? linkifyEscaped(N.esc(line)) : '<br>'}</div>`).join('');
  };

  function linkifyEscaped(s) {
    return s.replace(/\b(https?:\/\/[^\s<]+[^\s<.,;:!?)\]'"])/g, '<a href="$1">$1</a>');
  }

  // --------------------------------------------------------- markdown -> html

  function inlineMd(s) {
    s = N.esc(s);
    s = s.replace(/`([^`]+)`/g, '<span style="font-family: var(--mono-font)">$1</span>');
    s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, '<img class="nt-img" src="$2" alt="$1">');
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2">$1</a>');
    s = s.replace(/\*\*([^*]+)\*\*|__([^_]+)__/g, (m, a, b) => `<b>${a || b}</b>`);
    s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<i>$2</i>');
    s = s.replace(/(^|\W)_([^_\s][^_]*)_(?=\W|$)/g, '$1<i>$2</i>');
    s = s.replace(/~~([^~]+)~~/g, '<s>$1</s>');
    s = s.replace(/&lt;u&gt;(.*?)&lt;\/u&gt;/g, '<u>$1</u>');
    if (!/<a /.test(s)) s = linkifyEscaped(s);
    return s;
  }

  C.markdownToHtml = function (md) {
    const lines = md.replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      let m;
      if (/^```/.test(line)) {
        const buf = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
        i++;
        out.push(`<pre>${N.esc(buf.join('\n'))}</pre>`);
        continue;
      }
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        const tag = m[1].length === 1 ? 'h1' : m[1].length === 2 ? 'h2' : 'h3';
        out.push(`<${tag}>${inlineMd(m[2])}</${tag}>`);
        i++;
        continue;
      }
      if (/^\s*>/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ''));
        out.push(`<blockquote>${buf.map(inlineMd).join('<br>')}</blockquote>`);
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(line)) {
        const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
          const cells = lines[i].trim().slice(1, -1).split('|').map((c) => c.trim());
          if (!cells.every((c) => /^:?-{2,}:?$/.test(c))) rows.push(cells);
          i++;
        }
        out.push('<table class="nt-table"><tbody>' + rows.map((r) =>
          '<tr>' + r.map((c) => `<td>${inlineMd(c) || '<br>'}</td>`).join('') + '</tr>').join('') + '</tbody></table>');
        continue;
      }
      if ((m = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/))) {
        const items = [];
        let kind = null;
        while (i < lines.length && (m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/))) {
          let text = m[3];
          let checked = null;
          const cb = text.match(/^\[( |x|X)\]\s+(.*)$/);
          if (cb) { checked = cb[1] !== ' '; text = cb[2]; }
          const k = cb ? 'checklist' : /\d/.test(m[2]) ? 'numbered' : m[2] === '-' ? 'dashed' : 'bullet';
          if (kind && k !== kind) break;
          kind = k;
          items.push({ text, checked });
          i++;
        }
        const tag = kind === 'numbered' ? 'ol' : 'ul';
        const cls = kind === 'numbered' ? '' : ` class="${kind}"`;
        out.push(`<${tag}${cls}>` + items.map((it) =>
          `<li${it.checked ? ' data-checked="true"' : ''}>${inlineMd(it.text) || '<br>'}</li>`).join('') + `</${tag}>`);
        continue;
      }
      out.push(line.trim() ? `<div>${inlineMd(line)}</div>` : '<div><br></div>');
      i++;
    }
    return out.join('');
  };

  // --------------------------------------------------------- html -> markdown

  C.toMarkdown = function (root) {
    const blocks = [];
    for (const node of Array.from(root.childNodes)) blocks.push(blockMd(node, 0));
    return blocks.join('\n').replace(/\n{3,}/g, '\n\n').trim() + '\n';
  };

  function inline(node) {
    if (node.nodeType === 3) return node.nodeValue.replace(/ /g, ' ');
    if (node.nodeType !== 1) return '';
    const inner = () => Array.from(node.childNodes).map(inline).join('');
    switch (node.tagName) {
      case 'B': case 'STRONG': return wrap(inner(), '**');
      case 'I': case 'EM': return wrap(inner(), '*');
      case 'S': case 'STRIKE': case 'DEL': return wrap(inner(), '~~');
      case 'U': return `<u>${inner()}</u>`;
      case 'A': return node.classList.contains('nt-file')
        ? `[${node.querySelector('.nt-file-name')?.textContent || node.getAttribute('title') || 'Attachment'}](${node.getAttribute('href')})`
        : `[${inner()}](${node.getAttribute('href')})`;
      case 'IMG': return `![](${node.getAttribute('src')})`;
      case 'BR': return '\n';
      default: return inner();
    }
  }

  const wrap = (s, m) => (s.trim() ? s.replace(/^(\s*)(.*?)(\s*)$/s, `$1${m}$2${m}$3`) : s);

  function listMd(list, depth) {
    const kind = list.tagName === 'OL' ? 'numbered' : list.classList.contains('checklist') ? 'checklist' : 'bullet';
    let n = 0;
    return Array.from(list.children).map((li) => {
      const nested = Array.from(li.children).filter((c) => c.tagName === 'UL' || c.tagName === 'OL');
      const text = Array.from(li.childNodes).filter((c) => !nested.includes(c)).map(inline).join('').trim();
      const marker = kind === 'numbered' ? `${++n}.` : kind === 'checklist'
        ? (li.getAttribute('data-checked') === 'true' ? '- [x]' : '- [ ]') : '-';
      return '  '.repeat(depth) + marker + ' ' + text +
        nested.map((l) => '\n' + listMd(l, depth + 1)).join('');
    }).join('\n');
  }

  function blockMd(node, depth) {
    if (node.nodeType === 3) return node.nodeValue.trim() ? node.nodeValue : '';
    if (node.nodeType !== 1) return '';
    const text = () => Array.from(node.childNodes).map(inline).join('');
    switch (node.tagName) {
      case 'H1': return '# ' + text().trim() + '\n';
      case 'H2': return '## ' + text().trim() + '\n';
      case 'H3': return '### ' + text().trim() + '\n';
      case 'PRE': return '```\n' + node.textContent + '\n```\n';
      case 'BLOCKQUOTE': return text().split('\n').map((l) => '> ' + l).join('\n') + '\n';
      case 'UL': case 'OL': return listMd(node, depth) + '\n';
      case 'TABLE': {
        const rows = Array.from(node.querySelectorAll('tr')).map((tr) =>
          Array.from(tr.children).map((td) => inline(td).replace(/\n/g, ' ').replace(/\|/g, '\\|').trim()));
        if (!rows.length) return '';
        const cols = Math.max(...rows.map((r) => r.length));
        const line = (r) => '| ' + Array.from({ length: cols }, (_, k) => r[k] || '').join(' | ') + ' |';
        return [line(rows[0]), '| ' + Array(cols).fill('---').join(' | ') + ' |', ...rows.slice(1).map(line)].join('\n') + '\n';
      }
      default: {
        const t = text();
        return t === '\n' ? '' : t;
      }
    }
  }

  // ------------------------------------------------- html -> Apple paragraphs

  // The structure the Python encoder turns into Apple's note format (test push to iCloud).
  // Tables become tab-separated lines; images and files are left out for now.
  C.toAppleParagraphs = function (root) {
    const paras = [];
    const STYLE = { H1: 0, H2: 1, H3: 2 };

    function runsOf(node, fmt, out) {
      for (const n of Array.from(node.childNodes)) {
        if (n.nodeType === 3) {
          const t = n.nodeValue.replace(/ /g, ' ').replace(/\n/g, ' ');
          if (t) out.push(Object.assign({ text: t }, fmt));
          continue;
        }
        if (n.nodeType !== 1) continue;
        // Attachments (images, files, placeholders) travel as markers so the iCloud original stays in place.
        if (n.classList.contains('nt-missing') || n.classList.contains('nt-file')) { out.push({ attachment: 'file' }); continue; }
        const tag = n.tagName;
        if (tag === 'BR') { out.push({ br: true }); continue; }
        if (tag === 'IMG') { out.push({ attachment: 'image' }); continue; }
        if (tag === 'UL' || tag === 'OL' || tag === 'TABLE') continue;
        const f = Object.assign({}, fmt);
        if (tag === 'B' || tag === 'STRONG' || /^(bold|[6-9]00)$/.test(n.style.fontWeight)) f.bold = true;
        if (tag === 'I' || tag === 'EM') f.italic = true;
        if (tag === 'U') f.underline = true;
        if (tag === 'S' || tag === 'STRIKE' || tag === 'DEL') f.strike = true;
        if (tag === 'A' && n.getAttribute('href')) f.link = n.getAttribute('href');
        runsOf(n, f, out);
      }
      return out;
    }

    // Split runs at <br> into separate paragraphs with the same style.
    function push(style, runs, extra = {}) {
      // A trailing <br> only keeps an empty line open in the editor; it isn't an extra line.
      if (runs.length && runs[runs.length - 1].br) runs = runs.slice(0, -1);
      let cur = [];
      for (const r of runs) {
        if (r.br) { paras.push(Object.assign({ style, runs: cur }, extra)); cur = []; } else cur.push(r);
      }
      paras.push(Object.assign({ style, runs: cur }, extra));
    }

    function list(el, depth) {
      const kind = el.tagName === 'OL' ? 102 : el.classList.contains('checklist') ? 103 : el.classList.contains('dashed') ? 101 : 100;
      for (const child of Array.from(el.children)) {
        if (child.tagName === 'LI') {
          push(kind, runsOf(child, {}, []), { indent: depth, checked: child.getAttribute('data-checked') === 'true' });
          child.querySelectorAll(':scope > ul, :scope > ol').forEach((sub) => list(sub, depth + 1));
        } else if (child.tagName === 'UL' || child.tagName === 'OL') list(child, depth + 1);
      }
    }

    for (const block of Array.from(root.childNodes)) {
      if (block.nodeType === 3) { if (block.nodeValue.trim()) push(null, [{ text: block.nodeValue }]); continue; }
      if (block.nodeType !== 1) continue;
      const tag = block.tagName;
      if (STYLE[tag] !== undefined) push(STYLE[tag], runsOf(block, {}, []));
      else if (tag === 'PRE') block.textContent.split('\n').forEach((line) => paras.push({ style: 4, runs: [{ text: line }] }));
      else if (tag === 'UL' || tag === 'OL') list(block, 0);
      else if (tag === 'TABLE') {
        // One attachment marker; the tab-separated text is used when a brand-new note is created.
        const tsv = Array.from(block.querySelectorAll('tr')).map((tr) =>
          Array.from(tr.children).map((td) => td.textContent.replace(/\s+/g, ' ').trim()).join('\t')).join('\n');
        paras.push({ style: null, runs: [{ attachment: 'table', fallback: tsv }] });
      } else push(null, runsOf(block, {}, []), block.dataset.indent ? { indent: +block.dataset.indent } : {});
    }
    while (paras.length > 1 && !paras[paras.length - 1].runs.some((r) => (r.text && r.text.trim()) || r.attachment)) paras.pop();
    return paras;
  };

  // ------------------------------------------------------------ html export

  C.toHtmlDocument = function (title, bodyHtml) {
    // The editor's player needs its scripts; an exported page gets the browser's own.
    const tmp = document.createElement('div');
    tmp.innerHTML = bodyHtml;
    for (const a of tmp.querySelectorAll('a.nt-audio')) {
      const audio = document.createElement('audio');
      audio.controls = true;
      audio.setAttribute('src', a.getAttribute('href'));
      a.replaceWith(audio);
    }
    bodyHtml = tmp.innerHTML;
    return `<!doctype html>
<html><head><meta charset="utf-8"><title>${N.esc(title || 'Note')}</title>
<style>
:root { --mono-font: ui-monospace, "SF Mono", "JetBrains Mono", "DejaVu Sans Mono", monospace; }
body { font: 15px/1.45 -apple-system, "SF Pro Text", Inter, Cantarell, "Noto Sans", sans-serif; color: #1d1d1f; max-width: 760px; margin: 40px auto; padding: 0 24px; }
h1 { font-size: 28px; } h2 { font-size: 21px; } h3 { font-size: 17px; }
pre { font-family: var(--mono-font); font-size: 13px; white-space: pre-wrap; }
blockquote { border-left: 3px solid #d0d0d5; margin: 0; padding-left: 12px; }
ul.dashed { list-style: none; padding-left: 22px; } ul.dashed > li::before { content: "–"; margin-left: -16px; width: 16px; display: inline-block; }
ul.checklist { list-style: none; padding-left: 4px; } ul.checklist > li::before { content: "○"; margin-right: 8px; color: #999; }
ul.checklist > li[data-checked="true"]::before { content: "●"; color: #e5a900; }
table { border-collapse: collapse; } td { border: 1px solid #ddd; padding: 6px 10px; vertical-align: top; }
img { max-width: 100%; border-radius: 6px; } a { color: #b8860b; }
</style></head><body>${bodyHtml}</body></html>`;
  };
})(window.N = window.N || {});
