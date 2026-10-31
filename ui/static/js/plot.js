// A small line chart on a canvas (payoff curves, the IV smile, max pain). No library, no inline styles:
// colours come from the CSS tokens, sizes from the element's box. Every number arrives from the server.
import { token } from './theme.js';
import { num } from './fmt.js';

function niceStep(span, target) {
  const raw = span / Math.max(1, target);
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= raw) return m * p;
  return 10 * p;
}

function ticks(lo, hi, target) {
  if (!(hi > lo)) return [lo];
  const step = niceStep(hi - lo, target);
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Math.round(v / step) * step);
  return out;
}

function shortNum(v) {
  const a = Math.abs(v);
  if (a >= 1e7) return num(v / 1e7, a >= 1e9 ? 0 : 1) + ' cr';
  if (a >= 1e5) return num(v / 1e5, 1) + ' L';
  if (a >= 1e3) return num(v / 1e3, 1) + 'k';
  return num(v, a < 10 ? 2 : 0);
}

// opts: { x: [..], series: [{ y: [..], color: '--token', width, dash: [..], label, points: bool }],
//         vlines: [{ x, color, label, dash }], band: { lo, hi, label }, zero: bool, fill: seriesIndex,
//         xfmt, yfmt, ylabel }
export function plot(canvas, opts) {
  const state = { hover: null };
  const ctx = canvas.getContext('2d');
  const xs = opts.x || [];

  function layout() {
    const r = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(200, Math.round(r.width));
    const hgt = Math.max(160, Math.round(r.height));
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(hgt * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(hgt * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { w, h: hgt };
  }

  function bounds() {
    let lo = Infinity;
    let hi = -Infinity;
    for (const s of opts.series) for (const v of s.y) if (v !== null && v !== undefined) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    if (opts.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
    if (!(hi > lo)) { hi = lo + 1; lo -= 1; }
    const pad = (hi - lo) * 0.08;
    const xvals = xs.filter((v) => v !== null && v !== undefined);
    return { x0: Math.min(...xvals), x1: Math.max(...xvals), y0: lo - pad, y1: hi + pad };
  }

  function draw() {
    const { w, h } = layout();
    const b = bounds();
    const m = { l: 74, r: 14, t: 14, b: 30 };
    const X = (v) => m.l + (v - b.x0) / (b.x1 - b.x0 || 1) * (w - m.l - m.r);
    const Y = (v) => m.t + (b.y1 - v) / (b.y1 - b.y0 || 1) * (h - m.t - m.b);
    const fg = token('--muted');
    const grid = token('--grid');
    ctx.clearRect(0, 0, w, h);
    ctx.font = '11px ' + getComputedStyle(document.body).fontFamily;
    ctx.lineWidth = 1;
    // expected-move band
    if (opts.band && opts.band.lo !== null && opts.band.hi !== null && opts.band.lo !== undefined) {
      ctx.fillStyle = token('--band-fill');
      ctx.fillRect(X(Math.max(b.x0, opts.band.lo)), m.t, X(Math.min(b.x1, opts.band.hi)) - X(Math.max(b.x0, opts.band.lo)),
        h - m.t - m.b);
    }
    // grid and axes
    ctx.strokeStyle = grid;
    ctx.fillStyle = fg;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (const v of ticks(b.y0, b.y1, 6)) {
      ctx.beginPath(); ctx.moveTo(m.l, Y(v)); ctx.lineTo(w - m.r, Y(v)); ctx.stroke();
      ctx.fillText((opts.yfmt || shortNum)(v), m.l - 6, Y(v));
    }
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    const xt = ticks(b.x0, b.x1, Math.max(3, Math.floor((w - m.l) / 90)));
    for (const v of xt) {
      ctx.beginPath(); ctx.moveTo(X(v), m.t); ctx.lineTo(X(v), h - m.b); ctx.stroke();
      ctx.fillText((opts.xfmt || ((x) => num(x, 0)))(v), X(v), h - m.b + 6);
    }
    if (opts.zero && b.y0 < 0 && b.y1 > 0) {
      ctx.strokeStyle = token('--border');
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(m.l, Y(0)); ctx.lineTo(w - m.r, Y(0)); ctx.stroke();
    }
    // profit / loss fill under one series
    if (opts.fill !== undefined && opts.series[opts.fill]) {
      const s = opts.series[opts.fill];
      for (const [sign, col] of [[1, '--up'], [-1, '--down']]) {
        ctx.save();
        ctx.globalAlpha = 0.12;
        ctx.fillStyle = token(col);
        ctx.beginPath();
        ctx.moveTo(X(xs[0]), Y(0));
        xs.forEach((x, i) => {
          const v = s.y[i];
          ctx.lineTo(X(x), Y(sign > 0 ? Math.max(0, v) : Math.min(0, v)));
        });
        ctx.lineTo(X(xs[xs.length - 1]), Y(0));
        ctx.closePath();
        ctx.fill();
        ctx.restore();
      }
    }
    // vertical markers
    ctx.textBaseline = 'top';
    let labelRow = 0;
    for (const v of opts.vlines || []) {
      if (v.x === null || v.x === undefined || v.x < b.x0 || v.x > b.x1) continue;
      ctx.save();
      ctx.strokeStyle = token(v.color || '--muted');
      ctx.setLineDash(v.dash || [4, 4]);
      ctx.beginPath(); ctx.moveTo(X(v.x), m.t); ctx.lineTo(X(v.x), h - m.b); ctx.stroke();
      ctx.restore();
      if (v.label) {
        ctx.fillStyle = token(v.color || '--muted');
        ctx.textAlign = 'left';
        ctx.fillText(v.label, X(v.x) + 3, m.t + 2 + 13 * (labelRow++ % 3));
      }
    }
    // series
    for (const s of opts.series) {
      ctx.save();
      ctx.strokeStyle = token(s.color || '--accent');
      ctx.fillStyle = ctx.strokeStyle;
      ctx.lineWidth = s.width || 2;
      ctx.setLineDash(s.dash || []);
      if (s.points) {
        xs.forEach((x, i) => {
          const v = s.y[i];
          if (v === null || v === undefined) return;
          ctx.beginPath(); ctx.arc(X(x), Y(v), 2.5, 0, Math.PI * 2); ctx.fill();
        });
      } else {
        ctx.beginPath();
        let pen = false;
        xs.forEach((x, i) => {
          const v = s.y[i];
          if (v === null || v === undefined) { pen = false; return; }
          if (pen) ctx.lineTo(X(x), Y(v)); else { ctx.moveTo(X(x), Y(v)); pen = true; }
        });
        ctx.stroke();
      }
      ctx.restore();
    }
    // hover readout
    if (state.hover !== null) {
      const i = state.hover;
      const px = X(xs[i]);
      ctx.save();
      ctx.strokeStyle = token('--text');
      ctx.globalAlpha = 0.4;
      ctx.beginPath(); ctx.moveTo(px, m.t); ctx.lineTo(px, h - m.b); ctx.stroke();
      ctx.restore();
      const lines = [(opts.xlabel || 'x') + ' ' + (opts.xfmt || ((x) => num(x, 2)))(xs[i])];
      for (const s of opts.series) {
        const v = s.y[i];
        if (v !== null && v !== undefined) lines.push((s.label || '') + ' ' + (opts.yfmt || ((y) => num(y)))(v));
      }
      const bw = Math.max(...lines.map((t) => ctx.measureText(t).width)) + 14;
      const bh = lines.length * 15 + 8;
      const bx = px + bw + 12 > w ? px - bw - 8 : px + 8;
      ctx.fillStyle = token('--panel');
      ctx.strokeStyle = token('--border');
      ctx.fillRect(bx, m.t + 4, bw, bh);
      ctx.strokeRect(bx, m.t + 4, bw, bh);
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      lines.forEach((t, k) => {
        ctx.fillStyle = k === 0 ? token('--muted') : token((opts.series[k - 1] || {}).color || '--text');
        ctx.fillText(t, bx + 7, m.t + 8 + k * 15);
      });
    }
    state.geom = { X, m, w };
  }

  function nearest(clientX) {
    const r = canvas.getBoundingClientRect();
    const x = clientX - r.left;
    let best = 0;
    let gap = Infinity;
    xs.forEach((v, i) => {
      const d = Math.abs(state.geom.X(v) - x);
      if (d < gap) { gap = d; best = i; }
    });
    return best;
  }

  const onMove = (e) => { state.hover = nearest(e.clientX); draw(); };
  const onLeave = () => { state.hover = null; draw(); };
  const onKey = (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    const i = state.hover === null ? Math.floor(xs.length / 2) : state.hover;
    state.hover = Math.max(0, Math.min(xs.length - 1, i + (e.key === 'ArrowRight' ? 1 : -1)));
    draw();
  };
  canvas.addEventListener('mousemove', onMove);
  canvas.addEventListener('mouseleave', onLeave);
  canvas.addEventListener('blur', onLeave);
  canvas.addEventListener('keydown', onKey);
  let ro = null;
  if (window.ResizeObserver) {
    ro = new ResizeObserver(() => draw());
    ro.observe(canvas);
  } else {
    window.addEventListener('resize', draw);
  }
  draw();
  return {
    redraw: draw,
    destroy() {
      canvas.removeEventListener('mousemove', onMove);
      canvas.removeEventListener('mouseleave', onLeave);
      canvas.removeEventListener('blur', onLeave);
      canvas.removeEventListener('keydown', onKey);
      if (ro) ro.disconnect(); else window.removeEventListener('resize', draw);
    },
  };
}
