// Today: the cockpit. Session state first, then money, risk, what needs attention, and ideas last.
import { get } from '../api.js';
import { h, clear, card, kv, note, table } from '../dom.js';
import { inr, num, pct, money, signed, tone, when, whenUtc } from '../fmt.js';
import { why, help } from '../why.js';
import { metric, meter, empty, skeleton, failure, sessionRail, pageHead } from '../ui.js';

let off = [];

const settle = (p) => p.then((v) => ({ ok: true, v }), (e) => ({ ok: false, e }));

function limit(risk, key, fallback) {
  const row = risk && (risk.limits || []).find((l) => l.key === key);
  return row && row.value !== undefined && row.value !== null ? row.value : fallback;
}

function dateWords(iso) {
  const d = new Date(String(iso || '').slice(0, 10) + 'T12:00:00Z');
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleDateString('en-IN', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' });
}

function railCard(m, risk) {
  return card('Session', sessionRail({
    now: String(m.now_ist || '').slice(11, 16), open: m.open, phase: m.phase, nextOpen: m.next_open,
    noBefore: limit(risk, 'no_entries_before', '09:30'), noAfter: limit(risk, 'no_entries_after', '14:45'),
    flatBy: limit(risk, 'flat_by', '15:10'),
  }), h('p', { class: 'explain' }, 'Both times come from your settings. Entry window ', help('entry_window'),
    ', flat by ', help('flat_by')));
}

function pnlTile(a) {
  const t = a.today;
  return metric({
    label: 'Day P&L, after charges', term: 'day_pnl', tone: tone(t.pnl) || null,
    value: h('span', { class: tone(t.pnl) }, why(signed(t.pnl), a.why.pnl, "today's P&L")),
    sub: h('span', null, 'Realised ', why(signed(t.realised), a.why.realised, 'realised P&L'), ', open ',
      why(signed(t.open_pnl), a.why.open_pnl, 'open P&L'), ', charges ', inr(t.charges)),
    explain: Number(t.pnl) === 0 ? 'No profit or loss yet today.' : Number(t.pnl) > 0
      ? 'A gain so far today on paper. Open positions can still give it back.'
      : 'A loss so far today on paper. Your daily loss limit stops new entries before it grows too far.',
  });
}

function budgetTile(a) {
  const t = a.today;
  return metric({
    label: 'Daily loss budget', term: 'loss_budget',
    value: h('span', null, why(inr(t.at_risk), a.why.at_risk, 'the amount at risk'), h('span', { class: 'metric-sub' },
      ' of ' + inr(t.max_daily_loss))),
    sub: meter(Number(t.at_risk) || 0, Number(t.max_daily_loss) || 0,
      `${t.max_daily_loss ? Math.round((100 * t.at_risk) / t.max_daily_loss) : 0}% at risk in placed entries`),
    explain: 'What placed entries could lose at their stops, charges included, against your limit for the day.',
  });
}

function tradesTile(a) {
  const t = a.today;
  const left = Math.max(0, (t.max_trades || 0) - (t.trades || 0));
  return metric({
    label: 'Trades today', term: 'trades_left', tone: left === 0 ? 'warn' : null,
    value: `${t.trades} of ${t.max_trades}`,
    sub: `${left} ${left === 1 ? 'entry' : 'entries'} left; orders ${t.orders} of ${t.max_orders}`,
    explain: left === 0 ? 'You have used every entry your settings allow today. Exits still work.'
      : 'Each new entry uses one. The cap keeps a bad day from becoming a worse one.',
  });
}

function riskTile(r) {
  if (!r) return metric({ label: 'Risk state', value: '–', sub: 'Could not read the risk panel.' });
  const locks = (r.breakers && r.breakers.locks) || [];
  const killed = r.kill && r.kill.active;
  const state = killed ? 'Kill switch on' : locks.length ? `${locks.length} breaker${locks.length > 1 ? 's' : ''} on` : 'Clear';
  return metric({
    label: 'Risk state', term: killed ? 'kill_switch' : 'breakers', tone: killed || locks.length ? 'down' : 'up',
    value: h('span', { class: 'badge ' + (killed || locks.length ? 'block' : 'pass') }, state),
    sub: killed ? 'No new entries today' + (r.kill.reason ? ': ' + r.kill.reason : '') + '. Exits still pass.'
      : locks.length ? locks[0] : 'No kill switch, no circuit breaker. New entries are checked by every rule.',
    explain: 'Locks stop new entries only; closing a position is always allowed.',
  });
}

function positionsCard(p) {
  if (!p) return card('Open positions', failure('Could not read the paper portfolio.'));
  const rows = p.positions || [];
  const body = rows.length ? table([
    'Symbol', 'Product', { label: 'Qty', num: true }, { label: 'Average', num: true }, { label: 'Last', num: true },
    { label: 'P&L if closed now', num: true },
  ], rows.map((r) => h('tr', null,
    h('td', null, h('a', { href: '#/chart?symbol=' + encodeURIComponent(r.symbol) }, r.symbol)),
    h('td', null, r.product), h('td', { class: 'num' }, num(r.qty, 0)), h('td', { class: 'num' }, inr(r.avg_price)),
    h('td', { class: 'num', title: r.price_source || '' }, r.price ? inr(r.price) : '–'),
    h('td', { class: 'num' }, r.pnl_if_closed === undefined ? '–' : h('span', { class: tone(r.pnl_if_closed) },
      why(signed(r.pnl_if_closed), r.why && r.why.pnl_if_closed, 'this P&L'))))), { caption: 'Open paper positions' })
    : empty('No open positions', 'When a paper order fills it shows here, marked to the last delayed price.',
      h('a', { class: 'btn', href: '#/portfolio' }, 'Open portfolio and orders'));
  const legs = (p.working_legs || []).length;
  return card('Open positions', body,
    legs ? h('p', { class: 'small muted' }, `${legs} working stop/target ${legs === 1 ? 'leg' : 'legs'} at the paper broker.`) : null,
    h('p', { class: 'small muted' }, 'Cash ', why(inr(p.cash), p.why && p.why.cash, 'cash'), '; free cash ', inr(p.free_cash), '.'),
    h('a', { href: '#/portfolio', class: 'more' }, 'Portfolio and orders'));
}

function marketCard(d, doctor) {
  const m = d.market;
  const vix = (d.indices || []).find((q) => q.symbol === '^INDIAVIX');
  const bad = doctor && doctor.checked ? (doctor.failing || []) : null;
  return card('Market',
    kv([
      ['NSE', h('span', null, h('span', { class: 'badge ' + (m.open ? 'pass' : m.phase === 'pre-open' ? 'warn' : 'block') },
        m.open ? 'Open' : m.phase === 'pre-open' ? 'Pre-open' : 'Closed'), ' ', why(m.reason, m.why, 'open or closed'))],
      m.nse && !m.nse_agrees ? ['nseindia.com', `says ${m.nse.status}: trust NSE`] : null,
      ['Regime', h('span', { class: 'today-regime' }, h('span', { class: 'muted' }, 'Reading…'), ' ', help('regime'))],
      vix ? ['India VIX', h('span', null, num(vix.price), ' ', h('span', { class: 'small ' + tone(vix.change) },
        `${signed(vix.change, num)} (${pct(vix.change_pct)})`))] : null,
      ['Data health', h('span', null, bad === null ? h('span', { class: 'badge' }, 'Not checked')
        : h('span', { class: 'badge ' + (bad.length ? 'block' : 'pass') }, bad.length ? `${bad.length} failing` : 'All sources ok'),
      ' ', help('data_health'))],
    ]),
    h('div', { class: 'tiles today-indices', 'aria-label': 'Indices' }, (d.indices || []).filter((q) => q.symbol !== '^INDIAVIX')
      .map(indexTile)),
    m.warning ? note(m.warning, 'warn') : null);
}

// NIFTY's regime from GET /api/regime (trader-regime), filled in after the page is up: it can take a few seconds.
function regimeCell(res) {
  const r = res && (res.regimes || [])[0];
  if (!r) {
    const why0 = (res && (res.errors || [])[0]) || 'no daily bars';
    return h('span', { class: 'today-regime' }, h('span', { class: 'badge' }, 'Not available'), ' ',
      h('span', { class: 'small muted' }, why0), ' ', help('regime'));
  }
  const d = r.daily;
  const label = { trend_up: 'Trending up', trend_down: 'Trending down', range: 'Ranging', transition: 'No clear trend',
    unknown: 'Unknown' }[d.trend.label] || d.trend.label;
  const vol = d.vol.bucket === 'unknown' ? 'vol unknown' : `${d.vol.bucket} vol`;
  const words = String(r.words || '').replace(/^NIFTY: /, '');
  return h('span', { class: 'today-regime' }, h('span', { class: 'badge' }, `${label}, ${vol}`), ' ',
    why(words, res.why, 'the regime'), ' ', h('span', { class: 'small muted' }, `(as of the ${d.date} close)`), ' ',
    help('regime'));
}

function indexTile(q) {
  return h('div', { class: 'tile', dataset: { sym: q.symbol } },
    h('div', { class: 'tile-name' }, q.name || q.symbol),
    h('div', { class: 'tile-price' }, money(q.symbol, q.price), h('span', { class: 'small muted' }, q.symbol.startsWith('^') ? ' pts' : '')),
    h('div', { class: 'tile-chg ' + tone(q.change) }, why(`${signed(q.change, num)} (${pct(q.change_pct)})`, q.why, 'this change')),
    h('div', { class: 'tile-time muted' }, when(q.time)));
}

function alertsBody(al) {
  const box = h('div', { class: 'alerts-list' });
  const rings = al.rings || [];
  if (rings.length) box.appendChild(h('ul', { class: 'rings' }, rings.map((r) => h('li', null, r.message))));
  else box.appendChild(h('p', { class: 'muted' }, 'Nothing would ring right now.'));
  if (al.rules && al.rules.length) {
    box.appendChild(h('ul', { class: 'rules small' }, al.rules.map((r) => h('li', { class: r.active ? '' : 'muted' },
      r.type, r.price != null ? ' at ' + num(r.price) : '', r.at ? ' at ' + r.at + ' IST' : '', ' ',
      h('code', { class: 'muted' }, r.id)))));
  } else {
    box.appendChild(empty('No alert rules', 'Add your plan\'s stop and target as alerts in a terminal:',
      h('code', null, 'python3 trader-alerts/alerts.py add --from-plan')));
  }
  for (const e of al.errors || []) box.appendChild(note(e, 'warn'));
  return box;
}

function planCard(p, risk) {
  if (!p) {
    return card('Next step', empty('No trade plan yet', 'Find a setup in the scanner or price a strategy; a plan with entry, ' +
      'stop and target appears here.', h('div', { class: 'row' }, h('a', { class: 'btn', href: '#/scanner' }, 'Open the scanner'),
      h('a', { class: 'btn', href: '#/strategy' }, 'Build a strategy'))));
  }
  const rr = p.reward_risk != null ? num(p.reward_risk) + ' : 1' : '–';
  return card('Next step: your plan',
    kv([
      ['Symbol', h('a', { href: '#/chart?symbol=' + encodeURIComponent(p.symbol) }, p.symbol)],
      ['Setup', `${p.setup || ''} ${p.side || ''}`.trim() + (p.entry_time ? `, ${p.entry_time} IST` : '')],
      ['Entry', inr(p.entry)], ['Stop', inr(p.stop) + (p.stop_label ? ` (${p.stop_label})` : '')],
      ['Target', inr(p.target) + (p.target_label ? ` (${p.target_label})` : '')],
      ['Reward to risk', h('span', null, rr, ' ', help('r_multiple'))],
    ]),
    risk && risk.last_check ? h('p', { class: 'small' }, 'Last risk check ', whenUtc(risk.last_check.checked_at), ': ',
      h('span', { class: 'badge pass' }, (risk.last_check.pass || 0) + ' pass'), ' ',
      h('span', { class: 'badge block' }, (risk.last_check.block || 0) + ' block'),
      risk.stale ? h('span', { class: 'badge warn' }, 'not today') : null) : null,
    h('div', { class: 'row' }, h('a', { class: 'btn primary', href: '#/risk' }, 'Check it against my rules'),
      h('a', { class: 'btn', href: '#/chart?symbol=' + encodeURIComponent(p.symbol) }, 'Open the chart')));
}

function candidatesCard() {
  return card('Co-trader candidates',
    empty('Coming in v0.3', 'Backtested trade candidates with their evidence: the probability with its range and sample ' +
      'size, the result after charges, the market regime, and what the evidence does not show. Each one still goes ' +
      'through your risk check and your typed yes.', h('a', { class: 'btn', href: '#/lab' }, 'See the Strategy Lab now')),
    h('span', { class: 'badge soon' }, 'Issue #44'));
}

export async function render(root, ctx) {
  clear(root);
  root.append(...pageHead('Today', null, h('span', { class: 'muted' }, '')));
  const body = h('div', null, skeleton(4));
  root.appendChild(body);
  const [dr, pr, rr] = await Promise.all([settle(get('/api/dashboard?verify=1')), settle(get('/api/portfolio')),
    settle(get('/api/risk'))]);
  clear(body);
  if (!dr.ok) {
    body.appendChild(failure(dr.e.message || String(dr.e), () => render(root, ctx)));
    return;
  }
  const d = dr.v;
  const p = pr.ok ? pr.v : null;
  const r = rr.ok ? rr.v : null;
  root.querySelector('.page-head .muted').textContent = dateWords(d.market.now_ist);
  body.appendChild(railCard(d.market, r));
  body.appendChild(h('div', { class: 'metrics', 'aria-label': 'Money and risk today' },
    pnlTile(d.account), budgetTile(d.account), tradesTile(d.account), riskTile(r)));
  const alertsCard = card('Alerts', alertsBody(d.alerts));
  body.appendChild(h('div', { class: 'cols' },
    h('div', { class: 'stack' }, positionsCard(p), candidatesCard()),
    h('div', { class: 'stack' }, planCard(d.plan, r), alertsCard, marketCard(d, ctx.live.doctor))));
  for (const e of d.index_errors || []) body.appendChild(note(e, 'warn'));
  settle(get('/api/regime?symbols=NIFTY')).then((g) => {
    const el = body.querySelector('.today-regime');
    if (el) el.replaceWith(regimeCell(g.ok ? g.v : { errors: [g.e.message || String(g.e)] }));
  });
  if (!pr.ok) body.appendChild(note('Portfolio: ' + (pr.e.message || pr.e), 'warn'));
  if (!rr.ok) body.appendChild(note('Risk: ' + (rr.e.message || rr.e), 'warn'));

  const names = Object.fromEntries((d.indices || []).map((q) => [q.symbol, q.name]));
  const onPrices = (ev) => {
    for (const q of ev.detail.quotes || []) {
      const old = body.querySelector(`.today-indices [data-sym="${CSS.escape(q.symbol)}"]`);
      if (old) old.replaceWith(indexTile(Object.assign({ name: names[q.symbol] }, q)));
    }
  };
  const onAlerts = (ev) => {
    const el = alertsCard.querySelector('.alerts-list');
    if (el && ev.detail && !ev.detail.error) el.replaceWith(alertsBody({ rules: d.alerts.rules, rings: ev.detail.rings, errors: [] }));
  };
  ctx.bus.addEventListener('prices', onPrices);
  ctx.bus.addEventListener('alerts', onAlerts);
  off = [() => ctx.bus.removeEventListener('prices', onPrices), () => ctx.bus.removeEventListener('alerts', onAlerts)];
}

export function destroy() {
  off.forEach((f) => f());
  off = [];
}
