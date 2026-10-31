// Version, the routes the server offers, and the third-party notice.
import { get } from '../api.js';
import { h, card, kv, table } from '../dom.js';

async function text(path) {
  const r = await fetch(path, { credentials: 'same-origin' });
  return r.ok ? r.text() : '';
}

export async function render(root) {
  const [m, notice] = await Promise.all([get('/api/meta'), text('/static/vendor/NOTICE')]);
  root.appendChild(h('h1', { class: 'page-title' }, 'About'));
  root.appendChild(card('bharat-trader', kv([
    ['Version', m.version], ['Reading', m.workdir + '/trader'], ['Clock', m.pinned_now ? `pinned to ${m.pinned_now} IST` : 'live'],
    ['Data', m.offline ? 'offline fixtures' : 'live: Yahoo Finance and nseindia.com, delayed, unofficial'],
    ['Account', m.paper ? 'paper' : 'not paper (orders refused)'],
  ]), h('p', { class: 'small muted' }, m.disclaimer)));
  root.appendChild(card('Third-party software',
    h('p', null, 'Candlestick charts use TradingView Lightweight Charts™ 5.2.1, licensed under the Apache License 2.0. ' +
      'The library is bundled unmodified in ui/static/vendor/ with its LICENSE and NOTICE; its logo on each chart links to ' +
      'TradingView.'),
    h('pre', { class: 'notice' }, notice.trim()),
    h('p', { class: 'small' }, h('a', { href: '/static/vendor/LICENSE' }, 'Apache License 2.0 (full text)'))));
  root.appendChild(card('API routes', table(['Method', 'Path', 'What'], m.routes.map((r) => h('tr', null,
    h('td', null, r.method), h('td', null, h('code', null, r.path)), h('td', null, r.about))), { dense: true })));
}
