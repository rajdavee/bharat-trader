// Auto-pilot (paper only): whether the loop runs, today's timeline, open paper positions by strategy, each
// strategy's forward record next to the lab's verdict, start/stop with a typed confirmation, and a replay of a
// past day in a sandbox. Every number comes from trader-autopilot; nothing here can reach a live broker.
import { get, post } from '../api.js';
import { h, clear, card, kv, table, note } from '../dom.js';
import { inr, num } from '../fmt.js';
import { metric, empty, skeleton, failure, pageHead, toast } from '../ui.js';

const rmult = (x) => (x === null || x === undefined ? '–' : (Number(x) > 0 ? '+' : '') + num(x, 2) + 'R');
const KIND_WORDS = {
  'pre-open': 'Pre-open', placed: 'Placed', fill: 'Filled', exit: 'Closed', trail: 'Stop raised', kill: 'Kill switch',
  blocked: 'Blocked', skip: 'Skipped', cancelled: 'Cancelled', eod: 'End of day', carry: 'Carried', note: 'Note',
  refused: 'Refused', 'dry-run': 'Dry run', mtm: 'Marked',
};

function statusCard(d, reload) {
  const running = d.running;
  const badge = h('span', { class: 'badge ' + (running ? 'pass' : 'info') }, running ? 'RUNNING on paper' : 'Not running');
  const rows = [
    ['Account', 'PAPER (no real money). A live config is refused.'],
    ['Day', d.day ? `${d.day} (${d.mode || 'paper'})` : 'not run yet'],
    ['Pre-open', (d.phases || {}).pre_open || '–'],
    ['End of day', (d.phases || {}).eod || '–'],
  ];
  if (d.lock) rows.push(['Loop', `pid ${d.lock.pid}, since ${d.lock.started}`]);
  if (d.heartbeat) rows.push(['Last step', `${d.heartbeat.at}: ${d.heartbeat.phase}`]);
  const out = [h('p', null, badge, d.stopping ? h('span', { class: 'badge warn' }, 'stopping') : null), kv(rows)];
  for (const p of d.problems || []) out.push(note(p, 'error'));
  if (d.entries_stopped) out.push(note('New entries stopped: ' + d.entries_stopped, 'warn'));
  const words = running ? d.stop_words : d.start_words;
  const input = h('input', { type: 'text', class: 'yes-input', autocomplete: 'off', placeholder: `type ${words}`,
    'aria-label': `Type ${words} to ${running ? 'stop' : 'start'} the auto-pilot` });
  const btn = h('button', { type: 'button', class: running ? 'btn' : 'btn primary' },
    running ? 'Stop the auto-pilot' : 'Start on paper');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const r = await post(running ? '/api/autopilot/stop' : '/api/autopilot/start', { typed: input.value });
      toast(r.started || r.stopped || 'Done.');
      reload();
    } catch (e) {
      toast(e.message || String(e), 'error');
      btn.disabled = false;
    }
  });
  out.push(h('div', { class: 'fill-form' }, input, btn));
  out.push(h('p', { class: 'small muted' }, d.how));
  return card('Auto-pilot', ...out);
}

function settingsCard(d) {
  const s = d.settings;
  if (!s) return null;
  const names = Object.keys(s.strategies || {});
  if (!names.length) {
    return card('Strategies', empty('No strategy enabled', 'Add an "autopilot" block to trader/config.json with the ' +
      'trader-lab strategies it may trade and each one\'s capital (docs/autopilot.md).',
    h('code', null, '"autopilot": {"strategies": {"name": {"capital": 50000}}}')));
  }
  const rows = names.map((n) => {
    const b = s.strategies[n];
    const lab = (d.strategies || {})[n] || {};
    const rec = (d.records || {})[n] || {};
    const fwd = lab.forward || {};
    const cls = { PROMOTE: 'promote', WATCH: 'watch' }[lab.verdict] || 'reject';
    return h('tr', null, h('td', null, h('code', null, n)),
      h('td', null, h('span', { class: 'verdict-tag ' + cls }, lab.verdict || 'no lab run')),
      h('td', { class: 'num' }, inr(b.capital)), h('td', { class: 'num' }, num(b.max_positions, 0)),
      h('td', { class: 'num' }, num(rec.trades || 0, 0)), h('td', { class: 'num' }, inr(rec.net || 0)),
      h('td', { class: 'num' }, rmult(rec.expectancy_r)),
      h('td', { class: 'num' }, `${num(fwd.trades || 0, 0)}, ${rmult(fwd.expectancy_r)}`));
  });
  return card('Strategies and their forward records',
    table(['Strategy', 'Lab verdict', { label: 'Capital', num: true }, { label: 'Max positions', num: true },
      { label: 'Paper trades', num: true }, { label: 'Net after charges', num: true }, { label: 'Per trade', num: true },
      { label: 'Lab forward record', num: true }], rows, { caption: 'Each strategy\'s capital bucket and record' }),
    h('p', { class: 'small muted' }, `Tiers traded: ${(s.tiers || []).join(' and ')} (never RESEARCH). Every ` +
      `${s.cadence_min} minutes in the entry window; at most ${s.max_positions} positions` +
      (s.max_daily_loss ? `, ${inr(s.max_daily_loss)} daily loss` : '') + '. The lab promotes or demotes on the ' +
      'forward record, which the auto-pilot\'s paper fills feed.'));
}

function positionsCard(d) {
  const ps = d.positions || [];
  if (!ps.length) return card('Open paper positions', h('p', { class: 'muted' }, 'None.'));
  return card('Open paper positions, by strategy', table(['Strategy', 'Position', 'Stock', 'Status',
    { label: 'Qty', num: true }, { label: 'Entry', num: true }, { label: 'Stop', num: true }, { label: 'Target', num: true }],
  ps.map((p) => h('tr', null, h('td', null, h('code', null, p.strategy)), h('td', null, p.id),
    h('td', null, `${p.symbol} ${p.product}`), h('td', null, p.status), h('td', { class: 'num' }, num(p.qty, 0)),
    h('td', { class: 'num' }, p.entry_price ? num(p.entry_price, 2) : `LIMIT ${num(p.entry_limit, 2)}`),
    h('td', { class: 'num' }, p.stop ? num(p.stop, 2) : '–'), h('td', { class: 'num' }, p.target ? num(p.target, 2) : '–')))));
}

function timelineCard(events, title) {
  if (!events || !events.length) return card(title, h('p', { class: 'muted' }, 'Nothing yet.'));
  return card(title, h('ol', { class: 'timeline' }, events.map((e) => h('li', null,
    h('time', null, e.at.slice(11, 16)), ' ', h('b', null, KIND_WORDS[e.kind] || e.kind), ': ', e.words))));
}

function reportCard(r) {
  if (!r) return null;
  const t = r.totals || {};
  const body = [h('div', { class: 'metrics' },
    metric({ label: 'Realised after charges', value: inr(t.realised || 0), tone: (t.realised || 0) < 0 ? 'warn' : null,
      sub: `${num(t.closed || 0, 0)} trade(s) closed`, explain: 'Paper fills on the bars, the spec\'s slippage, exact charges.' }),
    metric({ label: 'Charges paid', value: inr(t.charges || 0), sub: `${num(t.fills || 0, 0)} fill(s)` }))];
  const by = Object.entries(r.by_strategy || {});
  if (by.length) {
    body.push(table(['Strategy', { label: 'Fills', num: true }, { label: 'Closed', num: true }, { label: 'Won', num: true },
      { label: 'Charges', num: true }, { label: 'Realised', num: true }], by.map(([k, v]) => h('tr', null,
      h('td', null, h('code', null, k)), h('td', { class: 'num' }, num(v.fills, 0)), h('td', { class: 'num' }, num(v.closed, 0)),
      h('td', { class: 'num' }, num(v.wins, 0)), h('td', { class: 'num' }, inr(v.charges)), h('td', { class: 'num' }, inr(v.realised))))));
  }
  const fwd = Object.entries(r.forward || {}).filter(([, v]) => v && typeof v === 'object');
  for (const [k, v] of fwd) {
    if (v.changed) body.push(note(`${k}: the lab moved it from ${v.before} to ${v.after}. ${v.why || ''}`, 'warn'));
  }
  if (r.calibration && r.calibration.words) body.push(h('p', { class: 'small' }, 'Calibration: ' + r.calibration.words));
  return card(`Report, ${r.date} (${r.mode})`, ...body);
}

function replayCard(d, show) {
  const day = h('input', { type: 'date', class: 'price-input', 'aria-label': 'Replay this day' });
  const to = h('input', { type: 'date', class: 'price-input', 'aria-label': 'to (optional)' });
  const btn = h('button', { type: 'button', class: 'btn' }, 'Replay');
  const out = h('div');
  btn.addEventListener('click', async () => {
    if (!day.value) { toast('Pick a day to replay', 'error'); return; }
    btn.disabled = true;
    clear(out).appendChild(skeleton(4));
    try {
      const r = await post('/api/autopilot/replay', { date: day.value, to: to.value || undefined });
      clear(out);
      for (const rep of r.reports || []) {
        out.appendChild(reportCard(rep));
        out.appendChild(timelineCard(rep.timeline, `Timeline, ${rep.date}`));
      }
      out.appendChild(h('p', { class: 'small muted' }, `In the sandbox ${r.sandbox}: your own account was not touched.`));
    } catch (e) {
      clear(out).appendChild(note(e.message || String(e), 'error'));
    }
    btn.disabled = false;
  });
  const past = (d.replays || []).map((x) => h('li', null, h('code', null, x.name), ` (${x.days.length} day(s))`));
  return card('Replay a past day', h('p', null, 'The same loop on saved bars with a simulated clock, from the pre-open to ' +
    'the end-of-day report. The lab is re-run as of the close before, so the replay knows nothing later.'),
  h('div', { class: 'fill-form' }, day, to, btn), past.length ? h('ul', { class: 'small' }, past) : null, out);
}

export async function render(root, ctx) {
  clear(root);
  root.append(...pageHead('Auto-pilot', 'Validated strategies trade on paper by themselves, inside your limits, so ' +
    'each one builds a real forward record. Paper only: it refuses a live config.'));
  const body = h('div', null, skeleton(5));
  root.appendChild(body);
  const load = async () => {
    try {
      const d = await get('/api/autopilot');
      clear(body);
      body.appendChild(statusCard(d, load));
      const s = settingsCard(d);
      if (s) body.appendChild(s);
      body.appendChild(positionsCard(d));
      const rep = reportCard(d.report);
      if (rep) body.appendChild(rep);
      body.appendChild(timelineCard(d.timeline, d.day ? `Today's actions, ${d.day}` : 'Today\'s actions'));
      body.appendChild(replayCard(d));
      body.appendChild(h('div', { class: 'note warn' }, h('b', null, 'What this does not show. '), d.note));
    } catch (e) {
      clear(body).appendChild(failure(e.message || String(e), load));
    }
  };
  await load();
}

export function destroy() {}
