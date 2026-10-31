import { get } from '../api.js';
import { h, clear, card, kv, note, loading } from '../dom.js';
import { inr, num, pct, money, signed, tone, when, whenUtc } from '../fmt.js';
import { why } from '../why.js';

let off = [];

function indexTile(q) {
  return h('div', { class: 'tile', dataset: { sym: q.symbol } },
    h('div', { class: 'tile-name' }, q.name || q.symbol),
    h('div', { class: 'tile-price' }, money(q.symbol, q.price)),
    h('div', { class: 'tile-chg ' + tone(q.change) },
      why(`${signed(q.change, num)} (${pct(q.change_pct)})`, q.why, 'this change')),
    h('div', { class: 'tile-time muted' }, when(q.time)));
}

function marketCard(m) {
  const word = m.open ? 'Open' : m.phase === 'pre-open' ? 'Pre-open' : 'Closed';
  const rows = [
    ['Now', why(m.now_ist + ' IST', m.why, 'open or closed')],
    ['Status', h('span', { class: 'badge ' + (m.open ? 'pass' : m.phase === 'pre-open' ? 'warn' : '') }, word)],
    ['Why', m.reason],
    m.opens ? ['Session', `pre-open ${m.pre_open}, normal ${m.opens} to ${m.closes} IST` +
      (m.special_session ? ` (${m.special_session})` : '')] : null,
    !m.open && m.next_open ? ['Next open', m.next_open + ' IST'] : null,
  ];
  if (m.nse) rows.push(['nseindia.com', `${m.nse.status} (${m.nse.message})` + (m.nse_agrees ? '' : ' · disagrees: trust NSE')]);
  if (m.nse_error) rows.push(['nseindia.com', 'could not cross-check: ' + m.nse_error]);
  return card('NSE market', kv(rows), m.warning ? note(m.warning, 'warn') : null);
}

function pnlCard(a) {
  const t = a.today;
  const used = t.max_daily_loss ? Math.min(100, (100 * t.at_risk) / t.max_daily_loss) : 0;
  return card("Today's paper P&L",
    h('div', { class: 'big ' + tone(t.pnl) }, why(signed(t.pnl), a.why.pnl, "today's P&L")),
    kv([
      ['Realised', why(signed(t.realised), a.why.realised, 'realised P&L', tone(t.realised))],
      ['Open, if closed now', why(signed(t.open_pnl), a.why.open_pnl, 'open P&L', tone(t.open_pnl))],
      ['Charges paid today', inr(t.charges)],
      ['Cash', why(inr(a.cash), a.why.cash, 'cash')],
      ['Trades', `${t.trades} of ${t.max_trades}`],
      ['Orders', `${t.orders} of ${t.max_orders}`],
    ]),
    h('div', { class: 'meter', role: 'meter', 'aria-valuemin': 0, 'aria-valuemax': t.max_daily_loss,
      'aria-valuenow': t.at_risk, 'aria-label': 'Daily loss at risk' },
    h('div', { class: 'meter-bar' + (used > 80 ? ' hot' : '') })),
    h('p', { class: 'small muted' }, 'At risk today: ', why(inr(t.at_risk), a.why.at_risk, 'the amount at risk'),
      ` of ${inr(t.max_daily_loss)} daily loss`),
    h('a', { href: '#/portfolio', class: 'more' }, 'Portfolio and orders →'));
}

function planCard(p) {
  if (!p) return card('Trade plan', note('No trader/plan.json yet. trader-entry and trader-exit write it.', 'info'));
  return card('Trade plan',
    kv([
      ['Symbol', h('a', { href: '#/chart?symbol=' + encodeURIComponent(p.symbol) }, p.symbol)],
      ['Setup', `${p.setup || ''} ${p.side || ''} · ${p.date || ''} ${p.entry_time || ''}`],
      ['Entry', num(p.entry)],
      ['Stop', `${num(p.stop)}${p.stop_label ? ' · ' + p.stop_label : ''}`],
      ['Target', `${num(p.target)}${p.target_label ? ' · ' + p.target_label : ''}`],
      ['Reward : risk', p.reward_risk != null ? num(p.reward_risk) : '–'],
      ['Flat by', (p.flat_by || '') + ' IST'],
    ]),
    h('a', { href: '#/chart?symbol=' + encodeURIComponent(p.symbol), class: 'more' }, 'Open the chart →'));
}

function alertsBody(al) {
  const list = h('div', { class: 'alerts-list' });
  const rings = al.rings || [];
  if (rings.length) {
    list.appendChild(h('ul', { class: 'rings' }, rings.map((r) => h('li', { class: 'ring' }, r.message))));
  } else {
    list.appendChild(h('p', { class: 'muted' }, 'Nothing would ring right now.'));
  }
  if (al.rules && al.rules.length) {
    list.appendChild(h('ul', { class: 'rules' }, al.rules.map((r) => h('li', { class: r.active ? '' : 'muted' },
      h('code', null, r.id), ' ', r.type, r.price != null ? ' ' + num(r.price) : '', r.at ? ' at ' + r.at : '',
      r.rang_at ? h('span', { class: 'badge' }, 'rang ' + when(r.rang_at)) : null))));
  } else {
    list.appendChild(h('p', { class: 'muted small' }, 'No rules in trader/alerts.json (alerts.py add --from-plan).'));
  }
  for (const e of al.errors || []) list.appendChild(note(e, 'warn'));
  return list;
}

export async function render(root, ctx) {
  loading(root, 'Loading the dashboard');
  const d = await get('/api/dashboard?verify=1');
  clear(root);
  root.appendChild(h('h1', { class: 'sr-only' }, 'Dashboard'));
  const tiles = h('div', { class: 'tiles', 'aria-label': 'Indices' }, d.indices.map(indexTile));
  root.appendChild(tiles);
  for (const e of d.index_errors || []) root.appendChild(note(e, 'warn'));
  const alertsCard = card('Alerts', alertsBody(d.alerts));
  const grid = h('div', { class: 'grid' },
    marketCard(d.market), pnlCard(d.account), planCard(d.plan), alertsCard,
    card('Risk check', d.risk ? kv([
      ['Last check', d.risk.checked_at ? whenUtc(d.risk.checked_at) : d.risk.date],
      ['Verdicts', h('span', null, h('span', { class: 'badge pass' }, d.risk.pass + ' PASS'), ' ',
        h('span', { class: 'badge block' }, d.risk.block + ' BLOCK'))],
      d.risk.stale ? ['Note', 'from ' + d.risk.date + ', not today: run it again'] : null,
    ]) : note('No risk check recorded yet.', 'info'), h('a', { href: '#/risk', class: 'more' }, 'Risk panel →')),
    card('Watchlist', d.watchlist ? kv([
      ['Hits', String(d.watchlist.hits)], ['Scan', `${d.watchlist.mode || ''} · ${d.watchlist.as_of || ''}`],
    ]) : note('No trader/watchlist.json yet (trader-scanner).', 'info'),
    h('a', { href: '#/scanner', class: 'more' }, 'Scanner →')));
  root.appendChild(grid);
  root.appendChild(h('p', { class: 'muted small' }, d.disclaimer));

  // live updates
  const meter = root.querySelector('.meter-bar');
  const t = d.account.today;
  if (meter) meter.style.width = (t.max_daily_loss ? Math.min(100, (100 * t.at_risk) / t.max_daily_loss) : 0) + '%';
  const onPrices = (ev) => {
    const names = Object.fromEntries(d.indices.map((q) => [q.symbol, q.name]));
    for (const q of ev.detail.quotes || []) {
      const old = tiles.querySelector(`[data-sym="${CSS.escape(q.symbol)}"]`);
      if (old) old.replaceWith(indexTile(Object.assign({ name: names[q.symbol] }, q)));
    }
  };
  const onAlerts = (ev) => {
    const body = alertsCard.querySelector('.alerts-list');
    if (body && ev.detail && !ev.detail.error) {
      body.replaceWith(alertsBody({ rules: d.alerts.rules, rings: ev.detail.rings, errors: [] }));
    }
  };
  ctx.bus.addEventListener('prices', onPrices);
  ctx.bus.addEventListener('alerts', onAlerts);
  off = [() => ctx.bus.removeEventListener('prices', onPrices), () => ctx.bus.removeEventListener('alerts', onAlerts)];
}

export function destroy() {
  off.forEach((f) => f());
  off = [];
}
