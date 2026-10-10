// macOS-style chrome: traffic lights, menus, popovers, sheets and toasts.
(function (N) {
  const UI = (N.ui = {});
  const root = () => document.getElementById('overlay-root');

  // ---------------------------------------------------------- traffic lights

  UI.renderTrafficLights = function () {
    const html =
      '<button class="tl tl-close" data-tl="close" title="Close"><svg viewBox="0 0 12 12"><path d="M3.5 3.5l5 5M8.5 3.5l-5 5"/></svg></button>' +
      '<button class="tl tl-min" data-tl="minimize" title="Minimise"><svg viewBox="0 0 12 12"><path d="M2.8 6h6.4"/></svg></button>' +
      '<button class="tl tl-max" data-tl="maximize" title="Zoom"><svg viewBox="0 0 12 12"><path class="fill" d="M3.2 7.6V3.2h4.4zM8.8 4.4v4.4H4.4z"/></svg></button>';
    N.$$('[data-traffic]').forEach((el) => { el.innerHTML = html; });
    document.addEventListener('click', (e) => {
      const b = e.target.closest('[data-tl]');
      if (b) N.send('windowControl', b.dataset.tl);
    });
  };

  // ------------------------------------------------------------------- menus

  let openMenus = [];

  function buildMenu(items, level) {
    const m = document.createElement('div');
    m.className = 'menu';
    m.setAttribute('role', 'menu');
    items.forEach((it) => {
      if (it === '-' || it.separator) {
        m.appendChild(N.el('<div class="menu-sep"></div>'));
        return;
      }
      if (it.header) {
        m.appendChild(N.el(`<div class="menu-header">${N.esc(it.header)}</div>`));
        return;
      }
      const row = N.el(
        `<div class="menu-item${it.disabled ? ' disabled' : ''}${it.danger ? ' danger' : ''}" role="menuitem">` +
        `<span class="menu-check">${it.checked ? '✓' : ''}</span>` +
        `<span class="menu-icon">${it.icon ? N.icon(it.icon) : ''}</span>` +
        `<span class="menu-label">${N.esc(it.label)}</span>` +
        `<span class="menu-shortcut">${N.esc(it.shortcut || '')}</span>` +
        `${it.submenu ? '<span class="menu-arrow">' + N.icon('chevronRight') + '</span>' : ''}</div>`);
      row._item = it;
      m.appendChild(row);
    });
    if (!items.some((it) => it.icon)) m.classList.add('no-icons');
    if (!items.some((it) => it.checked !== undefined)) m.classList.add('no-checks');
    m.addEventListener('mousedown', (e) => e.preventDefault());
    m.addEventListener('mousemove', (e) => {
      const row = e.target.closest('.menu-item');
      if (!row || row.classList.contains('active')) return;
      setActive(m, row, level);
    });
    m.addEventListener('click', (e) => {
      const row = e.target.closest('.menu-item');
      if (!row || row.classList.contains('disabled') || row._item.submenu) return;
      activate(row._item);
    });
    return m;
  }

  function setActive(menu, row, level) {
    N.$$('.menu-item.active', menu).forEach((r) => r.classList.remove('active'));
    closeMenusFrom(level + 1);
    if (!row) return;
    row.classList.add('active');
    if (row._item.submenu && !row.classList.contains('disabled')) {
      const r = row.getBoundingClientRect();
      openMenuAt(row._item.submenu, r.right - 4, r.top - 5, level + 1, r.left + 4);
    }
  }

  function activate(item) {
    UI.closeMenus();
    if (item.action) setTimeout(() => item.action(), 0);
  }

  function closeMenusFrom(level) {
    while (openMenus.length > level) openMenus.pop().remove();
  }

  function openMenuAt(items, x, y, level, flipX) {
    const m = buildMenu(items, level);
    root().appendChild(m);
    const w = m.offsetWidth;
    const h = m.offsetHeight;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let left = x;
    if (left + w > vw - 6) left = flipX !== undefined ? flipX - w : vw - w - 6;
    let top = y;
    if (top + h > vh - 6) top = Math.max(6, vh - h - 6);
    m.style.left = Math.max(6, left) + 'px';
    m.style.top = top + 'px';
    openMenus.push(m);
    return m;
  }

  UI.menu = function (items, x, y) {
    UI.closeTransient();
    openMenuAt(items, x, y, 0);
  };

  UI.menuFor = function (anchor, items) {
    const r = anchor.getBoundingClientRect();
    UI.menu(items, r.left, r.bottom + 5);
  };

  UI.closeMenus = () => closeMenusFrom(0);

  document.addEventListener('mousedown', (e) => {
    if (openMenus.length && !e.target.closest('.menu')) UI.closeMenus();
    if (currentPopover && !e.target.closest('.popover') && !currentPopover.anchor.contains(e.target)) UI.closePopover();
  }, true);

  document.addEventListener('keydown', (e) => {
    if (!openMenus.length) return;
    const menu = openMenus[openMenus.length - 1];
    const level = openMenus.length - 1;
    const rows = N.$$('.menu-item:not(.disabled)', menu);
    const cur = rows.indexOf(N.$('.menu-item.active', menu));
    if (e.key === 'Escape') { if (level) closeMenusFrom(level); else UI.closeMenus(); }
    else if (e.key === 'ArrowDown') setActiveOnly(menu, rows[(cur + 1) % rows.length]);
    else if (e.key === 'ArrowUp') setActiveOnly(menu, rows[(cur - 1 + rows.length) % rows.length]);
    else if (e.key === 'ArrowRight' && cur >= 0 && rows[cur]._item.submenu) setActive(menu, rows[cur], level);
    else if (e.key === 'ArrowLeft' && level) closeMenusFrom(level);
    else if (e.key === 'Enter' && cur >= 0) { if (!rows[cur]._item.submenu) activate(rows[cur]._item); }
    else return;
    e.preventDefault();
    e.stopPropagation();
  }, true);

  function setActiveOnly(menu, row) {
    N.$$('.menu-item.active', menu).forEach((r) => r.classList.remove('active'));
    if (row) row.classList.add('active');
  }

  // ---------------------------------------------------------------- popovers

  let currentPopover = null;

  UI.popover = function (anchor, content, opts = {}) {
    UI.closeTransient();
    const pop = document.createElement('div');
    pop.className = 'popover ' + (opts.className || '');
    pop.innerHTML = '<div class="popover-arrow"></div>';
    pop.appendChild(content);
    root().appendChild(pop);
    const r = anchor.getBoundingClientRect();
    const w = pop.offsetWidth;
    const left = N.clamp(r.left + r.width / 2 - w / 2, 8, window.innerWidth - w - 8);
    pop.style.left = left + 'px';
    pop.style.top = r.bottom + 9 + 'px';
    pop.querySelector('.popover-arrow').style.left = (r.left + r.width / 2 - left - 9) + 'px';
    anchor.classList.add('pressed');
    currentPopover = { pop, anchor, onClose: opts.onClose };
    return pop;
  };

  UI.closePopover = function () {
    if (!currentPopover) return;
    const { pop, anchor, onClose } = currentPopover;
    currentPopover = null;
    anchor.classList.remove('pressed');
    pop.classList.add('closing');
    setTimeout(() => pop.remove(), 120);
    if (onClose) onClose();
  };

  UI.popoverOpen = () => !!currentPopover;

  UI.closeTransient = function () {
    UI.closeMenus();
    UI.closePopover();
  };

  // ------------------------------------------------------------------ sheets

  // UI.sheet({title, message, icon, fields:[{name,label,type,value,placeholder}], buttons:[{label,value,primary,danger}]})
  // resolves to {button, values} — button is null when cancelled with Escape.
  UI.sheet = function (opts) {
    UI.closeTransient();
    return new Promise((resolve) => {
      const backdrop = N.el('<div class="sheet-backdrop"></div>');
      const sheet = N.el('<div class="sheet" role="dialog"></div>');
      if (opts.wide) sheet.classList.add('wide');
      let html = '';
      if (opts.icon !== false) html += `<div class="sheet-icon">${N.icon(opts.icon || 'notesApp')}</div>`;
      if (opts.title) html += `<div class="sheet-title">${N.esc(opts.title)}</div>`;
      if (opts.message) html += `<div class="sheet-message">${N.esc(opts.message)}</div>`;
      if (opts.html) html += `<div class="sheet-body">${opts.html}</div>`;
      (opts.fields || []).forEach((f) => {
        html += `<label class="sheet-field">${f.label ? `<span>${N.esc(f.label)}</span>` : ''}` +
          `<input name="${f.name}" type="${f.type || 'text'}" value="${N.esc(f.value || '')}" ` +
          `placeholder="${N.esc(f.placeholder || '')}" spellcheck="false" autocomplete="off"></label>`;
      });
      html += '<div class="sheet-error" hidden></div><div class="sheet-buttons"></div>';
      sheet.innerHTML = html;
      const btns = sheet.querySelector('.sheet-buttons');
      const buttons = opts.buttons || [{ label: 'Cancel', value: null }, { label: 'OK', value: 'ok', primary: true }];
      buttons.forEach((b) => {
        const el = N.el(`<button class="push-btn${b.primary ? ' primary' : ''}${b.danger ? ' danger' : ''}">${N.esc(b.label)}</button>`);
        el.addEventListener('click', () => finish(b.value));
        btns.appendChild(el);
      });
      backdrop.appendChild(sheet);
      root().appendChild(backdrop);
      requestAnimationFrame(() => backdrop.classList.add('open'));

      const values = () => Object.fromEntries(N.$$('input', sheet).map((i) => [i.name, i.value]));
      const primary = buttons.find((b) => b.primary);

      async function finish(button) {
        if (button !== null && opts.validate) {
          const err = await opts.validate(button, values());
          if (err) {
            const box = sheet.querySelector('.sheet-error');
            box.textContent = err;
            box.hidden = false;
            sheet.classList.remove('shake');
            void sheet.offsetWidth;
            sheet.classList.add('shake');
            return;
          }
        }
        document.removeEventListener('keydown', onKey, true);
        backdrop.classList.remove('open');
        setTimeout(() => backdrop.remove(), 180);
        resolve({ button, values: values() });
      }

      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(null); }
        else if (e.key === 'Enter' && primary) { e.preventDefault(); e.stopPropagation(); finish(primary.value); }
      }
      document.addEventListener('keydown', onKey, true);
      // Focus the first text field; without one, the default (primary) button, like macOS.
      const first = sheet.querySelector('input:not([type="checkbox"]):not([type="radio"])');
      const primaryBtn = btns.querySelector('.push-btn.primary') || btns.lastElementChild;
      setTimeout(() => (first ? first.focus() : primaryBtn.focus()), 30);
      if (opts.onOpen) opts.onOpen(sheet);
    });
  };

  UI.confirm = async function (title, message, okLabel = 'OK', danger = false) {
    const r = await UI.sheet({
      title, message,
      buttons: [{ label: 'Cancel', value: null }, { label: okLabel, value: 'ok', primary: true, danger }],
    });
    return r.button === 'ok';
  };

  UI.alert = function (title, message) {
    return UI.sheet({ title, message, buttons: [{ label: 'OK', value: 'ok', primary: true }] });
  };

  // ------------------------------------------------------------------ toasts

  UI.toast = function (text) {
    const t = N.el(`<div class="toast">${N.esc(text)}</div>`);
    root().appendChild(t);
    requestAnimationFrame(() => t.classList.add('show'));
    setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, 2200);
  };
})(window.N = window.N || {});
