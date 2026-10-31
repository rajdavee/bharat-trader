// The app shell: hash router, live stream, theme, keyboard shortcuts.
import { get, post } from './api.js';
import { h, clear, note } from './dom.js';
import { hide as hideWhy } from './why.js';
import { isDark } from './theme.js';
import * as dashboard from './pages/dashboard.js';
import * as chart from './pages/chart.js';
import * as scanner from './pages/scanner.js';
import * as portfolio from './pages/portfolio.js';
import * as risk from './pages/risk.js';
import * as settings from './pages/settings.js';
import * as options from './pages/options.js';
import * as strategy from './pages/strategy.js';
import * as about from './pages/about.js';

const PAGES = {
  '': ['dashboard', 'Dashboard', dashboard],
  chart: ['chart', 'Chart', chart],
  scanner: ['scanner', 'Scanner and watchlist', scanner],
  portfolio: ['portfolio', 'Paper portfolio and orders', portfolio],
  risk: ['risk', 'Risk panel', risk],
  options: ['options', 'Option chain', options],
  strategy: ['strategy', 'Strategy builder', strategy],
  settings: ['settings', 'Settings', settings],
  about: ['about', 'About', about],
};
const GO = { d: '#/', c: '#/chart', s: '#/scanner', p: '#/portfolio', r: '#/risk', o: '#/options',
  b: '#/strategy', e: '#/settings', a: '#/about' };

export const bus = new EventTarget();
export const live = { status: null, prices: null, alerts: null };
let current = null;
let seq = 0;

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, qs] = raw.split('?');
  const parts = path.split('/').filter(Boolean);
  const params = Object.fromEntries(new URLSearchParams(qs || ''));
  return { name: parts[0] || '', rest: parts.slice(1), params };
}

async function route() {
  hideWhy();
  const { name, rest, params } = parseHash();
  const entry = PAGES[name] || null;
  const main = document.getElementById('main');
  if (current && current.destroy) {
    try { current.destroy(); } catch (e) { /* page already gone */ }
  }
  current = null;
  clear(main);
  for (const a of document.querySelectorAll('#nav a')) {
    if (entry && a.dataset.page === entry[0]) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  if (!entry) {
    main.appendChild(h('h1', null, 'Not found'));
    main.appendChild(note('There is no page at ' + location.hash + '.', 'warn'));
    return;
  }
  document.title = entry[1] + ' · bharat-trader';
  const mod = entry[2];
  const mine = ++seq;
  const root = h('div', { class: 'page page-' + entry[0] });
  main.appendChild(root);
  current = mod;
  try {
    await mod.render(root, { rest, params, bus, live });
  } catch (e) {
    if (mine !== seq) return;
    root.appendChild(note(e.message || String(e), 'error'));
  }
}

// ---------------------------------------------------------------- market pill and live stream

function marketPill(st) {
  const el = document.getElementById('market');
  if (!st || st.error) {
    el.textContent = 'clock unavailable';
    el.className = 'pill';
    return;
  }
  const word = st.open ? 'NSE open' : st.phase === 'pre-open' ? 'Pre-open' : 'NSE closed';
  el.className = 'pill ' + (st.open ? 'open' : st.phase === 'pre-open' ? 'pre' : 'closed');
  el.textContent = `${word} · ${String(st.now_ist || '').slice(11, 16)} IST`;
  el.title = st.reason + (st.next_open && !st.open ? ' · next open ' + st.next_open + ' IST' : '');
}

function connect() {
  if (!window.EventSource) return;
  const es = new EventSource('/api/stream');
  for (const name of ['status', 'prices', 'alerts']) {
    es.addEventListener(name, (ev) => {
      try {
        live[name] = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      if (name === 'status') marketPill(live.status);
      bus.dispatchEvent(new CustomEvent(name, { detail: live[name] }));
    });
  }
  es.onerror = () => {
    const el = document.getElementById('market');
    if (es.readyState === EventSource.CLOSED) el.textContent = 'offline';
  };
}

// ---------------------------------------------------------------- data self-test badge

function dataBadge(d) {
  const el = document.getElementById('data');
  if (!d || !d.checked) {
    el.className = 'pill data';
    el.textContent = 'Data: not checked';
    el.title = (d && d.note) || 'Data sources self-test (trader-doctor). Click to run it now.';
    return;
  }
  const bad = d.failing || [];
  el.className = 'pill data ' + (bad.length ? 'closed' : 'open');
  el.textContent = bad.length ? `Data: ${bad.length} ${bad.length === 1 ? 'problem' : 'problems'}` : 'Data ok';
  const when = String(d.checked_at || '').slice(0, 16).replace('T', ' ');
  el.title = (bad.length ? bad.map((r) => `${r.name}: ${r.status} (${r.detail}). Fallback: ${r.fallback}`).join('\n')
    : 'Every data source answered as expected.') + `\nChecked ${when} IST. Click to run the self-test again.`;
}

async function runDoctor() {
  const el = document.getElementById('data');
  if (el.disabled) return;
  el.disabled = true;
  el.textContent = 'Data: checking…';
  try {
    dataBadge(await post('/api/doctor/run', {}));
  } catch (e) {
    el.textContent = 'Data: check failed';
    el.title = e.message || String(e);
  } finally {
    el.disabled = false;
  }
}

// ---------------------------------------------------------------- theme

function storedTheme() {
  try { return localStorage.getItem('bt-theme'); } catch (e) { return null; }
}

function applyTheme(t) {
  if (t === 'dark' || t === 'light') document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  bus.dispatchEvent(new CustomEvent('theme'));
}

function toggleTheme() {
  const next = isDark() ? 'light' : 'dark';
  try { localStorage.setItem('bt-theme', next); } catch (e) { /* private window */ }
  applyTheme(next);
}

// ---------------------------------------------------------------- keyboard

function typing(e) {
  const t = e.target;
  return t && (t.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName));
}

function openHelp(open) {
  const m = document.getElementById('help');
  m.hidden = !open;
  if (open) m.querySelector('[data-close]').focus();
}

let pendingG = 0;
function onKey(e) {
  if (e.key === 'Escape') {
    if (!document.getElementById('help').hidden) { openHelp(false); return; }
    if (hideWhy()) return;
    if (typing(e)) e.target.blur();
    return;
  }
  if (typing(e) || e.metaKey || e.ctrlKey || e.altKey) return;
  if (pendingG && Date.now() - pendingG < 1200) {
    pendingG = 0;
    const to = GO[e.key.toLowerCase()];
    if (to) { e.preventDefault(); location.hash = to; }
    return;
  }
  if (e.key === 'g') { pendingG = Date.now(); return; }
  if (e.key === '/') { e.preventDefault(); document.getElementById('q').focus(); return; }
  if (e.key === '?') { e.preventDefault(); openHelp(true); return; }
  if (e.key === 't') { toggleTheme(); }
}

// ---------------------------------------------------------------- start

async function banner() {
  try {
    const m = await get('/api/meta');
    const el = document.getElementById('banner');
    const bits = [];
    if (m.offline) bits.push('OFFLINE DEMO: saved real data, clock pinned to ' + (m.pinned_now || '') + ' IST. ' +
      (m.offline_note || ''));
    else if (m.pinned_now) bits.push('Clock pinned to ' + m.pinned_now + ' IST.');
    if (!m.config_exists) bits.push('No trader/config.json yet: open Settings, or run python3 trader-budget/size.py init.');
    bits.push(m.paper ? 'PAPER account: nothing here places a real order.' : 'Config is not paper: orders are refused.');
    el.textContent = bits.join(' ');
    el.hidden = false;
    el.classList.toggle('demo', !!m.offline);
  } catch (e) {
    /* the page itself shows the error */
  }
}

function start() {
  const t = storedTheme();
  if (t) applyTheme(t);
  document.getElementById('theme').addEventListener('click', toggleTheme);
  document.getElementById('keys-btn').addEventListener('click', () => openHelp(true));
  document.querySelector('#help [data-close]').addEventListener('click', () => openHelp(false));
  document.getElementById('help').addEventListener('click', (e) => {
    if (e.target.id === 'help') openHelp(false);
  });
  document.getElementById('search').addEventListener('submit', (e) => {
    e.preventDefault();
    const q = document.getElementById('q');
    const sym = q.value.trim().toUpperCase();
    if (!sym) return;
    q.value = '';
    q.blur();
    location.hash = '#/chart?symbol=' + encodeURIComponent(sym);
  });
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (!document.documentElement.dataset.theme) bus.dispatchEvent(new CustomEvent('theme'));
    });
  }
  document.addEventListener('keydown', onKey);
  window.addEventListener('hashchange', route);
  get('/api/status').then(marketPill).catch(() => marketPill(null));
  document.getElementById('data').addEventListener('click', runDoctor);
  get('/api/doctor').then(dataBadge).catch(() => dataBadge(null));
  banner();
  route();
  connect();
}

start();
