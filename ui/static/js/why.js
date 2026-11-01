// The "why" panel: click a number to see its formula, inputs, source and time, all sent by the server.
// help(term): a small ? that explains a term in plain words (glossary.js). Both use one popover.
import { h, clear } from './dom.js';
import { when } from './fmt.js';
import { GLOSSARY } from './glossary.js';

const pop = () => document.getElementById('why');
let opener = null;

function place(btn, el) {
  el.hidden = false;
  const r = btn.getBoundingClientRect();
  const w = Math.min(360, window.innerWidth - 24);
  el.style.width = w + 'px';
  const left = Math.max(12, Math.min(window.scrollX + r.left, window.scrollX + window.innerWidth - w - 12));
  el.style.left = left + 'px';
  el.style.top = (window.scrollY + r.bottom + 8) + 'px';
  opener = btn;
  btn.setAttribute('aria-expanded', 'true');
  el.querySelector('button').focus();
}

function head(title) {
  return h('div', { class: 'why-head' },
    h('h3', { id: 'why-title' }, title),
    h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Close', onclick: hide }, '×'));
}

function show(btn, info, label) {
  const el = pop();
  clear(el);
  const rows = Object.entries(info.inputs || {});
  el.appendChild(head('Why ' + label));
  el.appendChild(h('p', { class: 'why-formula' }, info.formula || ''));
  if (rows.length) {
    el.appendChild(h('dl', { class: 'kv small' }, rows.map(([k, v]) => [
      h('dt', null, k),
      h('dd', null, v === null || v === undefined ? '–' : typeof v === 'object' ? JSON.stringify(v) : String(v)),
    ])));
  }
  el.appendChild(h('p', { class: 'why-src' }, h('span', { class: 'muted' }, 'Source: '), info.source || '–'));
  if (info.as_of) el.appendChild(h('p', { class: 'why-src' }, h('span', { class: 'muted' }, 'As of: '), when(info.as_of)));
  if (info.note) el.appendChild(h('p', { class: 'why-note' }, info.note));
  place(btn, el);
}

function explain(btn, title, text) {
  const el = pop();
  clear(el);
  el.appendChild(head(title));
  el.appendChild(h('p', { class: 'help-text' }, text));
  place(btn, el);
}

export function hide() {
  const el = pop();
  if (!el || el.hidden) return false;
  el.hidden = true;
  if (opener) {
    opener.setAttribute('aria-expanded', 'false');
    opener.focus();
    opener = null;
  }
  return true;
}

function toggle(btn, fn) {
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (opener === btn && !pop().hidden) hide(); else fn();
  });
  return btn;
}

// A number that explains itself. Without a why object it is plain text.
export function why(text, info, label, cls = '') {
  if (!info) return h('span', { class: 'n ' + cls }, text);
  const btn = h('button', {
    type: 'button', class: 'n why ' + cls, 'aria-haspopup': 'dialog', 'aria-expanded': 'false',
    title: 'Why this number?', 'aria-label': `${text}: why ${label}?`,
  }, text);
  return toggle(btn, () => show(btn, info, label));
}

// "What does this mean?" for a glossary term, or for a custom [title, text].
export function help(term) {
  const entry = Array.isArray(term) ? term : GLOSSARY[term];
  if (!entry) return null;
  const btn = h('button', {
    type: 'button', class: 'help', 'aria-haspopup': 'dialog', 'aria-expanded': 'false',
    'aria-label': 'What does ' + entry[0] + ' mean?', title: 'What does this mean?',
  }, '?');
  return toggle(btn, () => explain(btn, entry[0], entry[1]));
}

document.addEventListener('click', (e) => {
  const el = pop();
  if (el && !el.hidden && !el.contains(e.target)) hide();
});
window.addEventListener('resize', () => hide());
