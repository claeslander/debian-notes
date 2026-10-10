// A small drawing sheet; the result is inserted into the note as a transparent PNG.
(function (N) {
  const COLORS = ['#1d1d1f', '#0a84ff', '#30c759', '#ffcc00', '#ff3b30', '#af52de'];
  const TOOLS = { pen: { width: 2.5, alpha: 1 }, marker: { width: 14, alpha: 0.35 }, eraser: { width: 22, alpha: 1 } };

  N.sketch = function () {
    const range = N.editor.saveRange();
    N.ui.closeTransient();
    const backdrop = N.el('<div class="sheet-backdrop"></div>');
    const sheet = N.el(`
      <div class="sheet sketch-sheet">
        <div class="sketch-bar">
          <button class="push-btn" data-sk="cancel">Cancel</button>
          <div class="sketch-tools">
            <button class="sk-tool active" data-tool="pen" title="Pen">${N.icon('pen')}</button>
            <button class="sk-tool" data-tool="marker" title="Marker">${N.icon('marker')}</button>
            <button class="sk-tool" data-tool="eraser" title="Eraser">${N.icon('eraser')}</button>
            <span class="sk-sep"></span>
            ${COLORS.map((c, i) => `<button class="sk-color${i ? '' : ' active'}" data-color="${c}" style="--c:${c}"></button>`).join('')}
            <span class="sk-sep"></span>
            <button class="sk-tool" data-sk="undo" title="Undo">${N.icon('undo')}</button>
            <button class="sk-tool" data-sk="clear" title="Clear">${N.icon('trash')}</button>
          </div>
          <button class="push-btn primary" data-sk="done">Done</button>
        </div>
        <canvas class="sketch-canvas"></canvas>
      </div>`);
    backdrop.appendChild(sheet);
    document.getElementById('overlay-root').appendChild(backdrop);
    requestAnimationFrame(() => backdrop.classList.add('open'));

    const canvas = sheet.querySelector('canvas');
    const ratio = window.devicePixelRatio || 1;
    const W = 760;
    const H = 460;
    canvas.width = W * ratio;
    canvas.height = H * ratio;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    const ctx = canvas.getContext('2d');
    ctx.scale(ratio, ratio);

    const strokes = [];
    let current = null;
    let tool = 'pen';
    let color = COLORS[0];

    function redraw() {
      ctx.clearRect(0, 0, W, H);
      for (const s of strokes) drawStroke(s);
      if (current) drawStroke(current);
    }

    function drawStroke(s) {
      const t = TOOLS[s.tool];
      ctx.save();
      ctx.globalCompositeOperation = s.tool === 'eraser' ? 'destination-out' : 'source-over';
      ctx.globalAlpha = t.alpha;
      ctx.strokeStyle = s.color;
      ctx.lineWidth = t.width;
      ctx.lineCap = s.tool === 'marker' ? 'square' : 'round';
      ctx.lineJoin = 'round';
      ctx.beginPath();
      s.points.forEach((p, i) => {
        if (i === 0) ctx.moveTo(p[0], p[1]);
        else {
          const prev = s.points[i - 1];
          ctx.quadraticCurveTo(prev[0], prev[1], (prev[0] + p[0]) / 2, (prev[1] + p[1]) / 2);
        }
      });
      if (s.points.length === 1) ctx.lineTo(s.points[0][0] + 0.1, s.points[0][1]);
      ctx.stroke();
      ctx.restore();
    }

    const pos = (e) => {
      const r = canvas.getBoundingClientRect();
      return [(e.clientX - r.left) * (W / r.width), (e.clientY - r.top) * (H / r.height)];
    };

    canvas.addEventListener('pointerdown', (e) => {
      canvas.setPointerCapture(e.pointerId);
      current = { tool, color, points: [pos(e)] };
      redraw();
    });
    canvas.addEventListener('pointermove', (e) => {
      if (!current) return;
      current.points.push(pos(e));
      redraw();
    });
    const end = () => {
      if (!current) return;
      strokes.push(current);
      current = null;
      redraw();
    };
    canvas.addEventListener('pointerup', end);
    canvas.addEventListener('pointercancel', end);

    function close() {
      document.removeEventListener('keydown', onKey, true);
      backdrop.remove();
    }

    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); close(); }
      else if (N.isMod(e) && e.key.toLowerCase() === 'z') { e.preventDefault(); strokes.pop(); redraw(); }
    }
    document.addEventListener('keydown', onKey, true);

    sheet.addEventListener('click', async (e) => {
      const t = e.target.closest('[data-tool]');
      if (t) {
        tool = t.dataset.tool;
        N.$$('[data-tool]', sheet).forEach((b) => b.classList.toggle('active', b === t));
        return;
      }
      const c = e.target.closest('[data-color]');
      if (c) {
        color = c.dataset.color;
        if (tool === 'eraser') {
          tool = 'pen';
          N.$$('[data-tool]', sheet).forEach((b) => b.classList.toggle('active', b.dataset.tool === 'pen'));
        }
        N.$$('[data-color]', sheet).forEach((b) => b.classList.toggle('active', b === c));
        return;
      }
      const a = e.target.closest('[data-sk]');
      if (!a) return;
      if (a.dataset.sk === 'cancel') close();
      else if (a.dataset.sk === 'undo') { strokes.pop(); redraw(); }
      else if (a.dataset.sk === 'clear') { strokes.length = 0; redraw(); }
      else if (a.dataset.sk === 'done') {
        close();
        if (strokes.length) await N.editor.insertImageData(trimmed(), 'sketch.png', 'nt-sketch', range);
      }
    });

    // Crop the PNG to the drawn area plus a margin.
    function trimmed() {
      const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
      let x0 = canvas.width, y0 = canvas.height, x1 = 0, y1 = 0;
      for (let y = 0; y < canvas.height; y++) {
        for (let x = 0; x < canvas.width; x++) {
          if (data[(y * canvas.width + x) * 4 + 3]) {
            if (x < x0) x0 = x; if (x > x1) x1 = x;
            if (y < y0) y0 = y; if (y > y1) y1 = y;
          }
        }
      }
      if (x1 < x0) return canvas.toDataURL('image/png');
      const m = 12 * ratio;
      x0 = Math.max(0, x0 - m); y0 = Math.max(0, y0 - m);
      x1 = Math.min(canvas.width, x1 + m); y1 = Math.min(canvas.height, y1 + m);
      const out = document.createElement('canvas');
      out.width = x1 - x0;
      out.height = y1 - y0;
      out.getContext('2d').drawImage(canvas, x0, y0, out.width, out.height, 0, 0, out.width, out.height);
      return out.toDataURL('image/png');
    }
  };
})(window.N = window.N || {});
