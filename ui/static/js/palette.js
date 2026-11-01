// The command palette (Ctrl/Cmd-K): jump to a page, chart a symbol, or run a safe action.
import { h, clear } from './dom.js';
import { icon } from './icons.js';

let items = () => [];
let shown = [];
let sel = 0;
let lastFocus = null;

const box = () => document.getElementById('palette');
const input = () => document.getElementById('palette-q');
const list = () => document.getElementById('palette-list');

function score(item, q) {
  if (!q) return 1;
  const hay = (item.label + ' ' + (item.words || '') + ' ' + (item.group || '')).toLowerCase();
  if (item.label.toLowerCase().startsWith(q)) return 3;
  if (hay.includes(q)) return 2;
  let i = 0;
  for (const ch of hay) if (ch === q[i]) i += 1;
  return i === q.length ? 1 : 0;
}

function render() {
  const raw = input().value.trim();
  const q = raw.toLowerCase();
  const base = items();
  const scored = base.map((it) => [score(it, q), it]).filter((x) => x[0] > 0).sort((a, b) => b[0] - a[0]);
  shown = scored.map((x) => x[1]);
  if (/^\^?[A-Za-z][A-Za-z0-9&.-]{1,19}$/.test(raw)) {
    const sym = raw.toUpperCase();
    const item = { label: 'Chart ' + sym, group: 'Symbol', icon: 'chart',
      run: () => { location.hash = '#/chart?symbol=' + encodeURIComponent(sym); } };
    // a page or action whose name matches wins; otherwise the symbol comes first
    if (scored.length && scored[0][0] >= 2) shown.push(item); else shown.unshift(item);
  }
  sel = Math.min(sel, Math.max(0, shown.length - 1));
  const ul = clear(list());
  if (!shown.length) {
    ul.appendChild(h('li', { class: 'muted', role: 'option', 'aria-disabled': 'true' }, 'Nothing matches “' + raw + '”'));
    input().removeAttribute('aria-activedescendant');
    return;
  }
  shown.forEach((it, i) => {
    const li = h('li', { id: 'pal-' + i, role: 'option', 'aria-selected': i === sel ? 'true' : 'false' },
      icon(it.icon || 'about'), h('span', null, it.label), it.group ? h('span', { class: 'grp' }, it.group) : null);
    li.addEventListener('mousemove', () => { if (sel !== i) { sel = i; mark(); } });
    li.addEventListener('click', () => choose(i));
    ul.appendChild(li);
  });
  mark();
}

function mark() {
  [...list().children].forEach((li, i) => li.setAttribute('aria-selected', i === sel ? 'true' : 'false'));
  const cur = document.getElementById('pal-' + sel);
  if (cur) {
    input().setAttribute('aria-activedescendant', cur.id);
    cur.scrollIntoView({ block: 'nearest' });
  }
}

function choose(i) {
  const it = shown[i];
  if (!it) return;
  close();
  it.run();
}

export function open() {
  lastFocus = document.activeElement;
  box().hidden = false;
  input().value = '';
  sel = 0;
  render();
  input().focus();
}

export function close() {
  if (box().hidden) return false;
  box().hidden = true;
  if (lastFocus && lastFocus.focus) lastFocus.focus();
  return true;
}

export function isOpen() {
  return !box().hidden;
}

export function setup(source) {
  items = source;
  input().addEventListener('input', () => { sel = 0; render(); });
  input().addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(shown.length - 1, sel + 1); mark(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(0, sel - 1); mark(); }
    else if (e.key === 'Enter') { e.preventDefault(); choose(sel); }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
    else if (e.key === 'Tab') { e.preventDefault(); }
  });
  box().addEventListener('click', (e) => { if (e.target === box()) close(); });
}
