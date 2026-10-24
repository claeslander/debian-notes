// Line icons in the spirit of SF Symbols. All drawn on a 24×24 grid with currentColor.
(function (N) {
  const S = (body, extra = '') =>
    `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ${extra}>${body}</svg>`;

  const P = {
    sidebar: S('<rect x="3" y="4.5" width="18" height="15" rx="3"/><path d="M9.5 4.5v15"/><path d="M5.6 8h1.6M5.6 10.8h1.6M5.6 13.6h1.6"/>'),
    list: S('<path d="M9 7h11M9 12h11M9 17h11"/><circle cx="4.8" cy="7" r=".9" fill="currentColor"/><circle cx="4.8" cy="12" r=".9" fill="currentColor"/><circle cx="4.8" cy="17" r=".9" fill="currentColor"/>'),
    gallery: S('<rect x="4" y="4" width="6.6" height="6.6" rx="1.6"/><rect x="13.4" y="4" width="6.6" height="6.6" rx="1.6"/><rect x="4" y="13.4" width="6.6" height="6.6" rx="1.6"/><rect x="13.4" y="13.4" width="6.6" height="6.6" rx="1.6"/>'),
    trash: S('<path d="M4.5 6.5h15"/><path d="M9.5 6.5V5a1.5 1.5 0 0 1 1.5-1.5h2A1.5 1.5 0 0 1 14.5 5v1.5"/><path d="M6.5 6.5l.9 12.1A2 2 0 0 0 9.4 20.5h5.2a2 2 0 0 0 2-1.9l.9-12.1"/><path d="M10 10v7M14 10v7"/>'),
    compose: S('<path d="M19 13v4.5A2.5 2.5 0 0 1 16.5 20h-10A2.5 2.5 0 0 1 4 17.5v-10A2.5 2.5 0 0 1 6.5 5H11"/><path d="M17.6 3.6a1.9 1.9 0 0 1 2.8 2.8L12 14.8l-3.6.8.8-3.6z"/>'),
    checklist: S('<circle cx="5.6" cy="7" r="2.3"/><path d="M4.6 7l.8.8 1.4-1.6"/><circle cx="5.6" cy="16.5" r="2.3"/><path d="M10.5 7H20M10.5 16.5H20"/>'),
    table: S('<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M3.5 9.5h17M3.5 14.5h17M9.2 4.5v15M14.8 4.5v15"/>'),
    photo: S('<rect x="3" y="5" width="18" height="14" rx="2.5"/><circle cx="8.5" cy="10" r="1.6"/><path d="M3.5 17l5-4.6 3.5 3 3-2.6 5.5 4.7"/>'),
    share: S('<path d="M12 3.5v11"/><path d="M8.2 7.2L12 3.5l3.8 3.7"/><path d="M8.5 10.5H7A2.5 2.5 0 0 0 4.5 13v5A2.5 2.5 0 0 0 7 20.5h10a2.5 2.5 0 0 0 2.5-2.5v-5a2.5 2.5 0 0 0-2.5-2.5h-1.5"/>'),
    lock: S('<rect x="5" y="10.5" width="14" height="10" rx="2.2"/><path d="M8 10.5V7.8a4 4 0 0 1 8 0v2.7"/>'),
    lockOpen: S('<rect x="5" y="10.5" width="14" height="10" rx="2.2"/><path d="M8 10.5V7.8a4 4 0 0 1 7.8-1.2"/>'),
    lockSmall: S('<rect x="5" y="10.5" width="14" height="10" rx="2.2" fill="currentColor" stroke="none"/><path d="M8 10.5V7.8a4 4 0 0 1 8 0v2.7" stroke-width="2.2"/>'),
    lockLarge: S('<rect x="5" y="10.5" width="14" height="10" rx="2.2"/><path d="M8 10.5V7.8a4 4 0 0 1 8 0v2.7"/>', 'stroke-width="1"'),
    more: S('<circle cx="12" cy="12" r="8.5"/><circle cx="8" cy="12" r=".7" fill="currentColor"/><circle cx="12" cy="12" r=".7" fill="currentColor"/><circle cx="16" cy="12" r=".7" fill="currentColor"/>'),
    search: S('<circle cx="10.5" cy="10.5" r="6"/><path d="M15 15l5 5"/>'),
    xCircle: S('<circle cx="12" cy="12" r="9" fill="currentColor" stroke="none"/><path d="M9 9l6 6M15 9l-6 6" stroke="var(--x-glyph, #fff)" stroke-width="1.8"/>'),
    chevronLeft: S('<path d="M14.5 5.5L8 12l6.5 6.5"/>'),
    chevronRight: S('<path d="M9.5 5.5L16 12l-6.5 6.5"/>'),
    chevronDown: S('<path d="M5.5 9.5L12 16l6.5-6.5"/>'),
    disclosure: S('<path d="M9.5 6.5L15 12l-5.5 5.5" stroke-width="2"/>'),
    folder: S('<path d="M3.5 7.5A2 2 0 0 1 5.5 5.5h3.6l2 2.2h7.4a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/><path d="M3.5 10h17"/>'),
    allNotes: S('<path d="M3.5 7.5A2 2 0 0 1 5.5 5.5h3.6l2 2.2h7.4a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/><path d="M7.5 13h9M7.5 16h6"/>'),
    trashSidebar: S('<path d="M4.5 6.5h15"/><path d="M9.5 6.5V5a1.5 1.5 0 0 1 1.5-1.5h2A1.5 1.5 0 0 1 14.5 5v1.5"/><path d="M6.5 6.5l.9 12.1A2 2 0 0 0 9.4 20.5h5.2a2 2 0 0 0 2-1.9l.9-12.1"/>'),
    tag: S('<path d="M5 9h15M4 15h15M10 4L8 20M16 4l-2 16"/>'),
    plusCircle: S('<circle cx="12" cy="12" r="8.5"/><path d="M12 8v8M8 12h8"/>'),
    plus: S('<path d="M12 5v14M5 12h14"/>'),
    minus: S('<path d="M5 12h14"/>'),
    pin: S('<path d="M14.5 3.5l6 6-2.4.6-3.6 3.6.4 4-1.6 1.6-3.8-3.8L5 20l4.5-5.5-3.8-3.8L7.3 9l4 .4L15 5.9z" fill="currentColor"/>'),
    pinOutline: S('<path d="M14.5 3.5l6 6-2.4.6-3.6 3.6.4 4-1.6 1.6-3.8-3.8L5 20l4.5-5.5-3.8-3.8L7.3 9l4 .4L15 5.9z"/>'),
    bulleted: S('<path d="M9.5 7h10M9.5 12h10M9.5 17h10"/><circle cx="5" cy="7" r="1.1" fill="currentColor"/><circle cx="5" cy="12" r="1.1" fill="currentColor"/><circle cx="5" cy="17" r="1.1" fill="currentColor"/>'),
    dashed: S('<path d="M9.5 7h10M9.5 12h10M9.5 17h10M4 7h2.5M4 12h2.5M4 17h2.5"/>'),
    numbered: S('<path d="M10 7h10M10 12h10M10 17h10"/><path d="M4.4 5.4l1.2-.7v4" stroke-width="1.3"/><path d="M4.3 11.1a1 1 0 0 1 1.9.4c0 .8-2 1.7-2 2.6h2.1" stroke-width="1.3"/><path d="M4.3 15.6h1.8l-1 1.2a1 1 0 1 1-.9 1.5" stroke-width="1.3"/>'),
    indent: S('<path d="M11 7h9M11 12h9M11 17h9M4 9l3 3-3 3"/>'),
    outdent: S('<path d="M11 7h9M11 12h9M11 17h9M7 9l-3 3 3 3"/>'),
    quote: S('<path d="M5 5v14" stroke-width="2.6"/><rect x="9" y="6" width="11" height="12" rx="1.5" fill="currentColor" stroke="none"/>'),
    textTitle: S('<text x="12" y="17" text-anchor="middle" font-size="15" font-weight="800" fill="currentColor" stroke="none" font-family="sans-serif">T</text>'),
    textHeading: S('<text x="12" y="16.5" text-anchor="middle" font-size="12" font-weight="800" fill="currentColor" stroke="none" font-family="sans-serif">H1</text>'),
    textSub: S('<text x="12" y="16" text-anchor="middle" font-size="10.5" font-weight="700" fill="currentColor" stroke="none" font-family="sans-serif">H2</text>'),
    textBold: S('<text x="12" y="17" text-anchor="middle" font-size="15" font-weight="800" fill="currentColor" stroke="none" font-family="sans-serif">B</text>'),
    textItalic: S('<text x="12" y="17" text-anchor="middle" font-size="15" font-style="italic" font-family="serif" fill="currentColor" stroke="none">I</text>'),
    textUnderline: S('<text x="12" y="16" text-anchor="middle" font-size="15" fill="currentColor" stroke="none" font-family="sans-serif">U</text><path d="M7 20.5h10"/>'),
    textStrike: S('<text x="12" y="17" text-anchor="middle" font-size="15" fill="currentColor" stroke="none" font-family="sans-serif">S</text><path d="M6 11.5h12"/>'),
    textBody: S('<path d="M5 7h14M5 12h14M5 17h9"/>'),
    textMono: S('<path d="M9 7l-5 5 5 5M15 7l5 5-5 5"/>'),
    computer: S('<rect x="3.5" y="4.5" width="17" height="11.5" rx="2"/><path d="M9 20h6M12 16v4"/>'),
    cloud: S('<path d="M7 18h10.5a4.5 4.5 0 0 0 .6-8.96A6 6 0 0 0 6.6 10.1 4 4 0 0 0 7 18z"/>'),
    cloudDown: S('<path d="M7 18h10.5a4.5 4.5 0 0 0 .6-8.96A6 6 0 0 0 6.6 10.1 4 4 0 0 0 7 18z"/><path d="M12 10.5v5M9.8 13.6l2.2 2.2 2.2-2.2"/>'),
    cloudUp: S('<path d="M7 18h10.5a4.5 4.5 0 0 0 .6-8.96A6 6 0 0 0 6.6 10.1 4 4 0 0 0 7 18z"/><path d="M12 15.8v-5M9.8 12.7l2.2-2.2 2.2 2.2"/>'),
    sync: S('<path d="M19.5 12a7.5 7.5 0 0 1-13.1 5"/><path d="M4.5 12a7.5 7.5 0 0 1 13.1-5"/><path d="M17.6 3.5V7h-3.5M6.4 20.5V17h3.5"/>'),
    cloudAlert: S('<path d="M7 18h10.5a4.5 4.5 0 0 0 .6-8.96A6 6 0 0 0 6.6 10.1 4 4 0 0 0 7 18z"/><path d="M12 10.5v3M12 15.8v.1"/>'),
    pen: S('<path d="M15.5 4.5l4 4L9 19H5v-4z"/><path d="M13.5 6.5l4 4"/>'),
    calendar: S('<rect x="3.5" y="5" width="17" height="15" rx="2.5"/><path d="M3.5 9.5h17M8 3v4M16 3v4"/>'),
    clock: S('<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>'),
    paperclip: S('<path d="M18.5 11.5l-6.4 6.4a4.2 4.2 0 0 1-6-6l7-7a2.8 2.8 0 0 1 4 4l-7 7a1.4 1.4 0 0 1-2-2l6.4-6.4"/>'),
    file: S('<path d="M14 3.5H7.5A2 2 0 0 0 5.5 5.5v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8z"/><path d="M14 3.5V8h4.5"/>'),
    check: S('<path d="M5 12.5l4.5 4.5L19 7.5" stroke-width="2"/>'),
    rowAbove: S('<rect x="4" y="11" width="16" height="9" rx="2"/><path d="M12 3v6M9 6h6"/>'),
    rowBelow: S('<rect x="4" y="4" width="16" height="9" rx="2"/><path d="M12 15v6M9 18h6"/>'),
    colLeft: S('<rect x="11" y="4" width="9" height="16" rx="2"/><path d="M3 12h6M6 9v6"/>'),
    colRight: S('<rect x="4" y="4" width="9" height="16" rx="2"/><path d="M15 12h6M18 9v6"/>'),
    rowDelete: S('<rect x="4" y="8" width="16" height="8" rx="2"/><path d="M9 12h6"/>'),
    colDelete: S('<rect x="8" y="4" width="8" height="16" rx="2"/><path d="M12 9v6" transform="rotate(90 12 12)"/>'),
    undo: S('<path d="M9 14L4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>'),
    eraser: S('<path d="M8.5 20.5h11"/><path d="M4.4 14.6l9.2-9.2a2 2 0 0 1 2.8 0l3.2 3.2a2 2 0 0 1 0 2.8l-8.2 8.2a2 2 0 0 1-1.4.6H8.3a2 2 0 0 1-1.4-.6l-2.5-2.5a1.8 1.8 0 0 1 0-2.5z"/><path d="M9.5 9.5l5 5"/>'),
    marker: S('<path d="M9 15l-3 6h6l1-3"/><path d="M9 15l7.5-11 4 2.7-6.4 11.5z"/>'),
    notesApp: '<svg viewBox="0 0 64 64" aria-hidden="true"><defs><linearGradient id="nag" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffd84a"/><stop offset="1" stop-color="#f5b800"/></linearGradient></defs><rect x="2" y="2" width="60" height="60" rx="14" fill="#fff"/><path d="M2 16A14 14 0 0 1 16 2h32a14 14 0 0 1 14 14v4H2z" fill="url(#nag)"/><g stroke="#d9d9de" stroke-width="1.4"><path d="M10 31h44M10 40h44M10 49h44"/></g><g fill="#c7c7cc"><circle cx="13" cy="25" r="1"/><circle cx="20" cy="25" r="1"/><circle cx="27" cy="25" r="1"/><circle cx="34" cy="25" r="1"/><circle cx="41" cy="25" r="1"/><circle cx="48" cy="25" r="1"/></g></svg>',
  };

  N.icon = (name) => P[name] || '';

  N.renderIcons = function (root = document) {
    root.querySelectorAll('[data-icon]').forEach((el) => {
      if (el.dataset.iconDone === el.dataset.icon) return;
      const keep = Array.from(el.childNodes).filter((n) => !(n.nodeType === 1 && n.classList.contains('icon')));
      el.innerHTML = N.icon(el.dataset.icon);
      keep.forEach((n) => el.appendChild(n));
      el.dataset.iconDone = el.dataset.icon;
    });
  };
})(window.N = window.N || {});
