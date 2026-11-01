// The app shell: navigation (sidebar, drawer, phone bar), hash router, command palette, live stream,
// theme and density, keyboard shortcuts, first-run redirect to the setup guide.
import { get, post } from './api.js';
import { h, clear, note } from './dom.js';
import { hide as hideWhy } from './why.js';
import { isDark } from './theme.js';
import { icon, brandMark } from './icons.js';
import { GROUPS, PAGES, BOTTOM, groupOf } from './nav.js';
import * as palette from './palette.js';
import { toast } from './ui.js';
import * as today from './pages/today.js';
import * as chart from './pages/chart.js';
import * as scanner from './pages/scanner.js';
import * as portfolio from './pages/portfolio.js';
import * as risk from './pages/risk.js';
import * as settings from './pages/settings.js';
import * as options from './pages/options.js';
import * as strategy from './pages/strategy.js';
import * as agents from './pages/agents.js';
import * as about from './pages/about.js';
import * as lab from './pages/lab.js';
import * as welcome from './pages/welcome.js';
import * as journal from './pages/journal.js';
import * as soon from './pages/soon.js';

const MODULES = {
  today, chart, scanner, portfolio, risk, settings, options, strategy, agents, about, lab, welcome, journal,
  cotrader: soon, autopilot: soon,
};
const ALIASES = { '': 'today', dashboard: 'today', onboarding: 'welcome' };

export const bus = new EventTarget();
export const live = { status: null, prices: null, alerts: null, meta: null };
let current = null;
let seq = 0;

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, qs] = raw.split('?');
  const parts = path.split('/').filter(Boolean);
  const params = Object.fromEntries(new URLSearchParams(qs || ''));
  const name = parts[0] || '';
  return { name: ALIASES[name] !== undefined ? ALIASES[name] : name, rest: parts.slice(1), params };
}

// ---------------------------------------------------------------- navigation

function link(route, cls) {
  const p = PAGES[route];
  return h('a', { href: route === 'today' ? '#/' : '#/' + route, dataset: { page: route }, class: cls || null },
    icon(p.icon), h('span', null, p.label), p.soon ? h('span', { class: 'soon-tag' }, 'v0.3') : null);
}

function buildNav() {
  const nav = clear(document.getElementById('nav'));
  for (const g of GROUPS) {
    const id = 'ng-' + (g.name || 'home').toLowerCase();
    nav.appendChild(h('div', { class: 'nav-group' },
      g.name ? h('h2', { id }, g.name) : null,
      h('ul', { class: 'nav-list', 'aria-labelledby': g.name ? id : null },
        g.pages.map((r) => h('li', null, link(r))))));
  }
  const bar = clear(document.getElementById('bottom-bar'));
  for (const r of BOTTOM) {
    bar.appendChild(h('a', { href: r === 'today' ? '#/' : '#/' + r, dataset: { page: r } }, icon(PAGES[r].icon),
      h('span', null, r === 'portfolio' ? 'Portfolio' : PAGES[r].label)));
  }
  bar.appendChild(h('button', { type: 'button', 'aria-controls': 'side', onclick: () => drawer(true) },
    icon('menu'), h('span', null, 'Menu')));
  const mb = document.getElementById('menu-btn');
  mb.appendChild(icon('menu'));
  mb.addEventListener('click', () => drawer(!document.body.classList.contains('nav-open')));
  document.getElementById('scrim').addEventListener('click', () => drawer(false));
  document.getElementById('nav').addEventListener('click', (e) => { if (e.target.closest('a')) drawer(false); });
  document.getElementById('brand-side').prepend(brandMark());
  document.getElementById('brand-top').prepend(brandMark());
  document.getElementById('theme').appendChild(icon('theme'));
  const jump = document.getElementById('jump');
  jump.prepend(icon('search'));
  jump.addEventListener('click', () => palette.open());
  if (/Mac|iPhone|iPad/.test(navigator.platform || '')) document.getElementById('jump-key').textContent = '⌘ K';
  const keys = clear(document.getElementById('keys-list'));
  const row = (k, d) => [h('dt', null, k), h('dd', null, d)];
  keys.append(...row([h('kbd', null, 'Ctrl'), ' ', h('kbd', null, 'K')], 'Jump to a page, chart a symbol, run an action'));
  for (const g of GROUPS) {
    for (const r of g.pages) keys.append(...row([h('kbd', null, 'g'), ' ', h('kbd', null, PAGES[r].key)], PAGES[r].label));
  }
  keys.append(...row(h('kbd', null, '/'), 'Chart a symbol'), ...row(h('kbd', null, 't'), 'Dark or light theme'),
    ...row(h('kbd', null, 'x'), 'Guided or expert detail'), ...row(h('kbd', null, '?'), 'This list'),
    ...row(h('kbd', null, 'Esc'), 'Close a panel'));
}

function isNarrow() {
  return window.matchMedia && window.matchMedia('(max-width: 1099px)').matches;
}

function drawer(open) {
  if (!isNarrow()) open = false;
  document.body.classList.toggle('nav-open', open);
  document.getElementById('scrim').hidden = !open;
  document.getElementById('menu-btn').setAttribute('aria-expanded', String(open));
  const side = document.getElementById('side');
  if (open) {
    side.setAttribute('role', 'dialog');
    side.setAttribute('aria-modal', 'true');
    const first = side.querySelector('a[aria-current="page"]') || side.querySelector('a');
    if (first) first.focus();
  } else {
    side.removeAttribute('role');
    side.removeAttribute('aria-modal');
  }
}

// ---------------------------------------------------------------- router

async function route() {
  hideWhy();
  palette.close();
  drawer(false);
  const { name, rest, params } = parseHash();
  const page = PAGES[name] ? name : null;
  const main = document.getElementById('main');
  if (current && current.destroy) {
    try { current.destroy(); } catch (e) { /* page already gone */ }
  }
  current = null;
  clear(main);
  for (const a of document.querySelectorAll('#nav a, #bottom-bar a')) {
    if (a.dataset.page === page) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  if (!page) {
    main.appendChild(h('h1', { class: 'page-title' }, 'Not found'));
    main.appendChild(note('There is no page at ' + location.hash + '. Press Ctrl K to jump to one.', 'warn'));
    return;
  }
  const p = PAGES[page];
  document.title = p.title + ' · bharat-trader';
  const mod = MODULES[page];
  const mine = ++seq;
  const root = h('div', { class: 'page page-' + page, dataset: { group: groupOf(page) } });
  main.appendChild(root);
  current = mod;
  try {
    await mod.render(root, { rest, params, bus, live, page, shell: { refreshMeta, runDoctor, setDensity } });
  } catch (e) {
    if (mine !== seq) return;
    root.appendChild(note(e.message || String(e), 'error'));
  }
}

// ---------------------------------------------------------------- market chip and live stream

function marketChip(st) {
  const el = clear(document.getElementById('market'));
  if (!st || st.error) {
    el.className = 'chip';
    el.append(icon('clock'), 'Clock unavailable');
    return;
  }
  const word = st.open ? 'NSE open' : st.phase === 'pre-open' ? 'Pre-open' : 'NSE closed';
  el.className = 'chip ' + (st.open ? 'ok' : st.phase === 'pre-open' ? 'wait' : 'bad');
  el.append(icon('clock'), h('span', { class: 'chip-txt' }, word),
    h('span', { class: 'chip-long chip-txt' }, String(st.now_ist || '').slice(11, 16) + ' IST'));
  el.title = st.reason + (st.next_open && !st.open ? '. Next open ' + st.next_open + ' IST' : '');
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
      if (name === 'status') marketChip(live.status);
      bus.dispatchEvent(new CustomEvent(name, { detail: live[name] }));
    });
  }
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) {
      const el = document.getElementById('market');
      el.className = 'chip bad';
      el.textContent = 'Offline';
    }
  };
}

// ---------------------------------------------------------------- data self-test chip

export function dataChip(d) {
  live.doctor = d;
  const el = clear(document.getElementById('data'));
  el.appendChild(icon('pulse'));
  if (!d || !d.checked) {
    el.className = 'chip';
    el.append(h('span', { class: 'chip-txt' }, 'Data'), h('span', { class: 'chip-long chip-txt' }, 'not checked'));
    el.title = (d && d.note) || 'Data sources self-test. Click to run it now.';
    return;
  }
  const bad = d.failing || [];
  el.className = 'chip ' + (bad.length ? 'bad' : 'ok');
  el.append(h('span', { class: 'chip-txt' }, bad.length ? `Data: ${bad.length} ${bad.length === 1 ? 'problem' : 'problems'}`
    : 'Data ok'));
  const at = String(d.checked_at || '').slice(0, 16).replace('T', ' ');
  el.title = (bad.length ? bad.map((r) => `${r.name}: ${r.status} (${r.detail}). Fallback: ${r.fallback}`).join('\n')
    : 'Every data source answered as expected.') + `\nChecked ${at} IST. Click to run the self-test again.`;
}

export async function runDoctor() {
  const el = document.getElementById('data');
  if (el.disabled) return;
  el.disabled = true;
  clear(el).append(icon('pulse'), h('span', { class: 'chip-txt' }, 'Checking data…'));
  try {
    const d = await post('/api/doctor/run', {});
    dataChip(d);
    bus.dispatchEvent(new CustomEvent('doctor', { detail: d }));
    toast((d.failing || []).length ? 'Data check finished: ' + d.failing.length + ' source(s) failing' : 'Data check passed');
  } catch (e) {
    clear(el).append(icon('pulse'), h('span', { class: 'chip-txt' }, 'Data check failed'));
    el.title = e.message || String(e);
    toast('Data check failed: ' + (e.message || e), 'error');
  } finally {
    el.disabled = false;
  }
}

// ---------------------------------------------------------------- theme and density

function stored(key) {
  try { return localStorage.getItem(key); } catch (e) { return null; }
}

function store(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* private window */ }
}

function applyTheme(t) {
  if (t === 'dark' || t === 'light') document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  bus.dispatchEvent(new CustomEvent('theme'));
}

function toggleTheme() {
  const next = isDark() ? 'light' : 'dark';
  store('bt-theme', next);
  applyTheme(next);
}

export function setDensity(d) {
  const v = d === 'expert' ? 'expert' : 'beginner';
  document.documentElement.dataset.density = v;
  for (const b of document.querySelectorAll('#density button')) {
    b.setAttribute('aria-pressed', String(b.dataset.density === v));
  }
  bus.dispatchEvent(new CustomEvent('density'));
}

function toggleDensity() {
  const next = document.documentElement.dataset.density === 'expert' ? 'beginner' : 'expert';
  store('bt-density', next);
  setDensity(next);
  toast(next === 'expert' ? 'Expert detail: explanations hidden' : 'Guided detail: explanations shown');
}

// ---------------------------------------------------------------- palette items

function paletteItems() {
  const out = [];
  for (const g of GROUPS) {
    for (const r of g.pages) {
      const p = PAGES[r];
      out.push({ label: p.label + (p.soon ? ' (coming in v0.3)' : ''), group: g.name || 'Home', icon: p.icon,
        words: p.words, run: () => { location.hash = r === 'today' ? '#/' : '#/' + r; } });
    }
  }
  out.push(
    { label: 'Switch dark or light theme', group: 'Action', icon: 'theme', words: 'dark light mode', run: toggleTheme },
    { label: 'Switch guided or expert detail', group: 'Action', icon: 'keys', words: 'density beginner expert', run: toggleDensity },
    { label: 'Run the data self-test', group: 'Action', icon: 'pulse', words: 'doctor health check', run: runDoctor },
    { label: 'Run the risk check on proposals', group: 'Action', icon: 'risk', words: 'verdict pass block',
      run: () => { location.hash = '#/risk'; } },
    { label: 'Keyboard shortcuts', group: 'Help', icon: 'keys', words: 'keys help', run: () => openHelp(true) },
  );
  return out;
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

const GO = Object.fromEntries(Object.entries(PAGES).map(([r, p]) => [p.key, r === 'today' ? '#/' : '#/' + r]));
let pendingG = 0;
function onKey(e) {
  if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    if (palette.isOpen()) palette.close(); else palette.open();
    return;
  }
  if (e.key === 'Escape') {
    if (palette.close()) return;
    if (!document.getElementById('help').hidden) { openHelp(false); return; }
    if (hideWhy()) return;
    if (document.body.classList.contains('nav-open')) { drawer(false); document.getElementById('menu-btn').focus(); return; }
    if (typing(e)) e.target.blur();
    return;
  }
  if (typing(e) || e.metaKey || e.ctrlKey || e.altKey || palette.isOpen()) return;
  if (pendingG && Date.now() - pendingG < 1200) {
    pendingG = 0;
    const to = GO[e.key.toLowerCase()];
    if (to) { e.preventDefault(); location.hash = to; }
    return;
  }
  if (e.key === 'g') { pendingG = Date.now(); return; }
  if (e.key === '/') { e.preventDefault(); palette.open(); return; }
  if (e.key === '?') { e.preventDefault(); openHelp(true); return; }
  if (e.key === 't') { toggleTheme(); return; }
  if (e.key === 'x') { toggleDensity(); }
}

// ---------------------------------------------------------------- status strip and first run

function strip(m) {
  const el = clear(document.getElementById('banner'));
  const parts = [];
  if (m.offline) {
    parts.push(h('span', { class: 'tag' }, 'Offline demo'),
      h('span', null, 'Saved real data, clock pinned to ' + (m.pinned_now || '') + ' IST.'));
  } else if (m.pinned_now) {
    parts.push(h('span', { class: 'tag' }, 'Clock pinned'), h('span', null, m.pinned_now + ' IST.'));
  }
  parts.push(h('span', { class: 'badge ' + (m.paper ? 'paper' : 'block') },
    m.paper ? 'Paper account: nothing here places a real order' : 'Config is not paper: orders are refused here'));
  if (!m.config_exists) {
    parts.push(h('a', { href: '#/welcome', class: 'btn primary' }, 'Set up your account'));
  }
  if (m.offline && m.offline_note) {
    parts.push(h('details', null, h('summary', null, 'About this demo'), h('p', null, m.offline_note)));
  }
  el.append(...parts);
  el.classList.toggle('demo', !!m.offline);
  el.hidden = false;
}

async function boot() {
  let m = null;
  try {
    m = await get('/api/meta');
    live.meta = m;
    strip(m);
  } catch (e) {
    /* the page itself shows the error */
  }
  const { name } = parseHash();
  if (m && !m.config_exists && (name === 'today') && !location.hash.replace(/^#\/?/, '') && stored('bt-setup-skipped') !== '1') {
    location.replace('#/welcome');
  }
  route();
}

export function refreshMeta() {
  return get('/api/meta').then((m) => { live.meta = m; strip(m); return m; }).catch(() => null);
}

function start() {
  const t = stored('bt-theme');
  if (t) applyTheme(t);
  setDensity(stored('bt-density') || 'beginner');
  buildNav();
  palette.setup(paletteItems);
  document.getElementById('theme').addEventListener('click', toggleTheme);
  for (const b of document.querySelectorAll('#density button')) {
    b.addEventListener('click', () => { store('bt-density', b.dataset.density); setDensity(b.dataset.density); });
  }
  document.getElementById('keys-btn').addEventListener('click', () => openHelp(true));
  document.querySelector('#help [data-close]').addEventListener('click', () => openHelp(false));
  document.getElementById('help').addEventListener('click', (e) => {
    if (e.target.id === 'help') openHelp(false);
  });
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (!document.documentElement.dataset.theme) bus.dispatchEvent(new CustomEvent('theme'));
    });
    window.matchMedia('(max-width: 1099px)').addEventListener('change', () => drawer(false));
  }
  document.addEventListener('keydown', onKey);
  window.addEventListener('hashchange', route);
  get('/api/status').then(marketChip).catch(() => marketChip(null));
  document.getElementById('data').addEventListener('click', runDoctor);
  get('/api/doctor').then(dataChip).catch(() => dataChip(null));
  boot();
  connect();
}

start();
