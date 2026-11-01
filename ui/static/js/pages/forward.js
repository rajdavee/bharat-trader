// Forward tests (read only): each trader-lab strategy's paper forward record against what its backtest predicted,
// with intervals, a control chart, decay alerts, calibration, the promote / demote timeline and the trades still
// needed for PROMOTE; the intraday option-chain capture job's health; local notifications and a test button.
import { get, post } from '../api.js';
import { h, clear, card, kv, table, note } from '../dom.js';
import { inr, num } from '../fmt.js';
import { metric, empty, skeleton, failure, pageHead, toast } from '../ui.js';

const r = (x) => (x === null || x === undefined ? '–' : (Number(x) > 0 ? '+' : '') + num(x, 3) + 'R');
const share = (x) => (x === null || x === undefined ? '–' : num(Number(x) * 100, 0) + '%');
const range = (a, f = r) => (a && a.length === 2 ? `${f(a[0])} to ${f(a[1])}` : '–');
const WHERE = { inside: 'inside the range', below: 'BELOW the range', above: 'above the range', 'too few': 'too few to judge' };
const SVGNS = ['http:', '', 'www.w3.org', '2000', 'svg'].join('/');   // the SVG namespace (not a URL to fetch)

function verdictTag(v) {
  return h('span', { class: 'verdict-tag ' + String(v || '').toLowerCase() }, v || 'no verdict');
}

function svg(tag, attrs) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
  return el;
}

// The running forward mean R against the predicted range's lower bound, trade by trade (a small chart; the
// numbers are in the table next to it, so the chart is never the only place they are).
function runningChart(row) {
  const pts = row.running || [];
  if (pts.length < 2) return null;
  const vals = pts.flatMap((p) => [p.running_mean_r, p.lower]).filter((v) => v !== null && v !== undefined);
  if (row.backtest && row.backtest.mean_r !== null && row.backtest.mean_r !== undefined) vals.push(row.backtest.mean_r);
  const lo = Math.min(...vals, 0);
  const hi = Math.max(...vals, 0);
  const W = 320;
  const H = 110;
  const x = (i) => 6 + (i / (pts.length - 1)) * (W - 12);
  const y = (v) => H - 8 - ((v - lo) / ((hi - lo) || 1)) * (H - 16);
  const line = (key) => pts.map((p, i) => (p[key] === null || p[key] === undefined ? null : `${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`))
    .filter(Boolean).join(' ');
  const el = svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'fwd-chart', role: 'img',
    'aria-label': `${row.strategy}: running forward mean ${r(pts[pts.length - 1].running_mean_r)} after ${pts.length} ` +
      `trades; the predicted lower bound is ${r(pts[pts.length - 1].lower)}` });
  el.appendChild(svg('line', { x1: 0, x2: W, y1: y(0), y2: y(0), class: 'zero' }));
  if (row.backtest && row.backtest.mean_r !== null && row.backtest.mean_r !== undefined) {
    el.appendChild(svg('line', { x1: 0, x2: W, y1: y(row.backtest.mean_r), y2: y(row.backtest.mean_r), class: 'expect' }));
  }
  el.appendChild(svg('polyline', { points: line('lower'), class: 'bound' }));
  el.appendChild(svg('polyline', { points: line('running_mean_r'), class: 'mean' }));
  return h('figure', { class: 'fwd-figure' }, el,
    h('figcaption', { class: 'small muted' }, 'Running forward mean R (solid), the predicted lower bound (dashed), ' +
      'the backtest expectation (dotted) and zero.'));
}

function strategyCard(row) {
  const bt = row.backtest || {};
  const fw = row.forward || {};
  const pr = row.predicted || {};
  const cc = row.control_chart || {};
  const cal = row.calibration || {};
  const el = row.eligibility || {};
  const body = [h('p', null, verdictTag(row.verdict), ' ', row.decay ? h('span', { class: 'badge block' }, 'Decay alert') :
    h('span', { class: 'badge ' + (row.where === 'below' ? 'warn' : 'info') }, WHERE[row.where] || row.where))];
  body.push(h('div', { class: 'metrics' },
    metric({ label: 'Forward per trade', value: r(fw.mean_r), sub: `${num(fw.trades || 0, 0)} paper trade(s), 95% ${range(fw.mean_r_ci)}`,
      tone: row.where === 'below' ? 'warn' : null, explain: 'After charges, in R (the loss at the stop).' }),
    metric({ label: 'Backtest predicted', value: range(pr.mean_r), sub: `expects ${r(bt.mean_r)} over ${num(bt.trades || 0, 0)} out-of-sample trades`,
      explain: 'A prediction interval for a record this long: its own noise plus the backtest\'s.' }),
    metric({ label: 'Win rate', value: share(fw.win_rate), sub: `95% ${range(fw.win_rate_ci, share)}; predicted ${range(pr.win_rate, share)}` }),
    metric({ label: 'To PROMOTE', value: el.trades_left ? `${num(el.trades_left, 0)} trade(s)` : (row.verdict === 'PROMOTE' ? 'done' : '0 trades'),
      sub: el.sessions_left ? `about ${num(el.sessions_left, 0)} session(s) at the backtest's pace` : (el.words || '') })));
  const chart = runningChart(row);
  if (chart) body.push(chart);
  body.push(kv([
    ['Net after charges', inr(fw.net || 0)],
    ['Control chart', cc.last === null || cc.last === undefined ? 'no trades yet' :
      `lower CUSUM ${num(cc.last, 2)} (alarm above ${num(cc.h, 1)})${cc.alarm_at ? `, ALARMED at trade ${cc.alarm_at}` : ''}`],
    ['Calibration', cal.n ? `Brier ${num(cal.brier, 3)} on ${num(cal.n, 0)} resolved predictions (a constant guess scores ${num(cal.brier_reference, 3)})` :
      'no resolved co-trader predictions yet'],
    ['PROMOTE rule', el.words || '–'],
  ]));
  if (row.recommendation) {
    body.push(note(`Recommended: DEMOTE. ${row.recommendation.why}. Apply it yourself in a terminal: ${row.recommendation.how}`, 'error'));
  }
  if ((row.timeline || []).length) {
    body.push(h('h3', { class: 'sub' }, 'Promote / demote timeline'));
    body.push(h('ol', { class: 'timeline' }, row.timeline.slice(-8).map((x) => h('li', null,
      h('time', null, x.at || ''), ' ', h('b', null, x.verdict), ': ', x.why || '', x.source === 'trader-cotrader' ? ' (co-trader)' : ''))));
  }
  return card(row.strategy, ...body);
}

function summaryTable(rows) {
  return table(['Strategy', 'Verdict', { label: 'Trades', num: true }, { label: 'Forward', num: true },
    { label: 'Predicted', num: true }, 'Where', { label: 'Brier', num: true }, 'To PROMOTE'],
  rows.map((x) => h('tr', null, h('td', null, h('code', null, x.strategy)), h('td', null, verdictTag(x.verdict)),
    h('td', { class: 'num' }, num(x.forward.trades || 0, 0)), h('td', { class: 'num' }, r(x.forward.mean_r)),
    h('td', { class: 'num' }, range((x.predicted || {}).mean_r)), h('td', null, x.decay ? 'decay alert' : (WHERE[x.where] || x.where)),
    h('td', { class: 'num' }, x.calibration.n ? num(x.calibration.brier, 3) : '–'),
    h('td', null, (x.eligibility || {}).trades_left ? `${x.eligibility.trades_left} trade(s)` : (x.verdict === 'PROMOTE' ? 'promoted' : '–')))),
  { caption: 'Every forward test: the paper record against the backtest' });
}

function captureCard(c) {
  if (!c) return null;
  const body = [];
  const badge = c.running ? h('span', { class: 'badge ' + (c.stale ? 'warn' : 'pass') }, c.stale ? 'running, stale' : 'capturing') :
    h('span', { class: 'badge ' + (c.in_market ? 'block' : 'info') }, c.in_market ? 'not running in market hours' : 'not running');
  body.push(h('p', null, badge, c.enabled === false ? h('span', { class: 'badge info' }, 'switched off') : null));
  const hb = c.heartbeat || {};
  body.push(kv([
    ['Underlyings', `${(c.symbols || []).join(', ')}, every ${c.every_min} min, nearest ${c.expiries} expiries`],
    ['Heartbeat', hb.last ? `last round ${hb.last.slice(11, 16)}, next ${(hb.next || '').slice(11, 16) || '–'} (${c.state})` : (c.state || 'none yet')],
    ['Restarts today', num(c.restarts_today || 0, 0)],
    ['Holdout', (c.holdout || {}).words || '–'],
  ]));
  const today = Object.entries(c.today || {});
  if (today.length) {
    body.push(table(['Underlying', { label: 'Rounds', num: true }, { label: 'Snapshots', num: true }, 'First', 'Last',
      { label: 'Strikes in latest', num: true }], today.map(([k, v]) => h('tr', null, h('td', null, k),
      h('td', { class: 'num' }, num(v.rounds, 0)), h('td', { class: 'num' }, num(v.snapshots, 0)),
      h('td', null, v.first ? `${v.first.slice(0, 2)}:${v.first.slice(2)}` : '–'), h('td', null, v.last ? `${v.last.slice(0, 2)}:${v.last.slice(2)}` : '–'),
      h('td', { class: 'num' }, v.latest_rows === null || v.latest_rows === undefined ? '–' : num(v.latest_rows, 0)))),
    { caption: 'Today\'s option-chain snapshots' }));
  }
  for (const g of c.gaps || []) body.push(note(`Gap in ${g.symbol}: ${g.from} to ${g.to} (${g.minutes} min, ${g.where})`, 'warn'));
  for (const p of c.problems || []) body.push(note(p, 'warn'));
  body.push(h('p', { class: 'small muted' }, 'The auto-pilot keeps trader-archive\'s capture-chains running in market ' +
    'hours. To keep it running without the loop: python3 trader-autopilot/autopilot.py capture --tick from a scheduler ' +
    '(docs/forward-tests.md has launchd, systemd and Task Scheduler examples).'));
  return card('Intraday option-chain capture', ...body);
}

function notifyCard(n) {
  if (!n) return null;
  const s = n.settings || {};
  const btn = h('button', { type: 'button', class: 'btn' }, 'Send a test notification');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const r2 = await post('/api/notify/test', {});
      toast(r2.sent ? 'Sent: ' + Object.keys(r2.channels).join(', ') : 'Not sent: ' + r2.why, r2.sent ? 'ok' : 'error');
    } catch (e) {
      toast(e.message || String(e), 'error');
    }
    btn.disabled = false;
  });
  const events = Object.entries(s.events || {}).filter(([k]) => k !== 'test');
  return card('Notifications', n.problem ? note(n.problem, 'error') : null, kv([
    ['Status', s.enabled ? 'ON' : 'off (the default)'],
    ['Desktop', s.desktop ? `on (${n.desktop_tool || 'no notifier on this system'})` : 'off'],
    ['Webhook', s.webhook ? (n.webhook_set ? `on, to ${n.webhook_host}` : `on, but ${n.webhook_note}`) : 'off'],
    ['Quiet hours', s.quiet_hours ? `${s.quiet_hours[0]} to ${s.quiet_hours[1]} IST` : 'none'],
    ['Limits', `one per event each ${s.min_gap_min} min, ${s.max_per_hour} an hour`],
    ['Events', events.map(([k, v]) => `${k.replace(/_/g, ' ')} ${v ? 'on' : 'off'}`).join(', ')],
  ]), h('p', { class: 'small muted' }, 'Set them in trader/config.json under "notify" (docs/notifications.md). A webhook gets ' +
    'only a short summary; never positions, keys or approval phrases.'), h('div', { class: 'fill-form' }, btn));
}

export async function render(root) {
  clear(root);
  root.append(...pageHead('Forward tests', 'Each strategy\'s paper forward record against what its backtest ' +
    'predicted, with the ranges, a control chart and decay alerts. Read only: a DEMOTE is applied only when you ask.'));
  const body = h('div', null, skeleton(5));
  root.appendChild(body);
  const load = async () => {
    try {
      const d = await get('/api/forward');
      clear(body);
      body.appendChild(h('p', { class: 'page-lede' }, d.words));
      for (const s of d.decay_alerts || []) body.appendChild(note(`Decay alert: ${s}. See its card below.`, 'error'));
      const rows = d.strategies || [];
      if (!rows.length) {
        body.appendChild(card('No forward test yet', empty('Nothing to compare yet', 'A strategy enters a forward test ' +
          'when trader-lab gives it WATCH; the auto-pilot then builds its paper record.', h('code', null, 'python3 trader-lab/lab.py run NAME'))));
      } else {
        body.appendChild(card('All forward tests', summaryTable(rows)));
        for (const row of rows) body.appendChild(strategyCard(row));
      }
      const ids = (d.edge_hunt_watch || []).map((x) => x.id).filter(Boolean);
      if (ids.length) body.appendChild(note(`Edge-hunt WATCH hypotheses waiting for a paper forward test: ${ids.join(', ')}`, 'info'));
      const cap = captureCard(d.capture);
      if (cap) body.appendChild(cap);
      const nc = notifyCard(d.notify);
      if (nc) body.appendChild(nc);
      body.appendChild(card('Thresholds', kv(Object.entries(d.thresholds || {}))));
      if (d.calibration_note) body.appendChild(h('p', { class: 'small muted' }, 'Calibration: ' + d.calibration_note));
      body.appendChild(h('div', { class: 'note warn' }, h('b', null, 'What this does not show. '),
        [...(d.does_not_show || []), d.note].join(' ')));
      body.appendChild(h('p', { class: 'small muted' }, d.how));
    } catch (e) {
      clear(body).appendChild(failure(e.message || String(e), load));
    }
  };
  await load();
}

export function destroy() {}
