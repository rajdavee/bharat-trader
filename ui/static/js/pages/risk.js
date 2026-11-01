// trader-risk: the kill switch, the circuit-breakers, the F&O book (Greeks and the scenario grid), the
// verdicts it recorded, a fresh preview (not recorded) and your limits.
import { get, post } from '../api.js';
import { h, clear, card, kv, note, table, loading } from '../dom.js';
import { inr, num, when, whenUtc } from '../fmt.js';
import { why } from '../why.js';
import { t, ruleWords } from '../i18n.js';

const MONEY = new Set(['capital', 'max_loss_per_trade', 'max_daily_loss', 'max_order_value', 'min_order_value',
  'max_premium_per_trade', 'max_premium_per_day', 'max_net_vega', 'max_theta_paid_per_day', 'max_scenario_loss']);

function value(k, v) {
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (v === null || v === undefined || v === '' || v === 0) return 'off';
  if (MONEY.has(k)) return inr(v);
  if (k.endsWith('_pct')) return num(v) + '%';
  return String(v);
}

function reason(x, words) {
  const plain = ruleWords(x, words);
  return plain ? h('li', null, h('b', null, plain), h('br'), h('span', { class: 'small muted' }, x)) : h('li', null, x);
}

function results(res, whyInfo, words) {
  const head = h('p', null,
    h('span', { class: 'badge pass' }, `${res.pass} PASS`), ' ', h('span', { class: 'badge block' }, `${res.block} BLOCK`),
    ` at ${res.now_ist} IST · cash after the passed orders `, why(inr(res.cash_after), whyInfo, 'the verdicts'),
    ` (${res.paper ? 'paper' : 'LIVE'} account)`);
  const items = (res.results || []).map((r) => h('li', { class: 'verdict ' + (r.verdict === 'PASS' ? 'pass' : 'block') },
    h('div', { class: 'verdict-head' }, h('span', { class: 'badge ' + (r.verdict === 'PASS' ? 'pass' : 'block') }, r.verdict),
      ` #${r.index} ${r.side} ${r.qty} ${r.contract || r.symbol} (${r.product}), ${r.kind}`),
    (r.info || []).length ? h('ul', { class: 'small' }, r.info.map((x) => h('li', null, x))) : null,
    (r.reasons || []).length ? h('ul', { class: 'reasons' }, r.reasons.map((x) => reason(x, words))) : null,
    (r.warnings || []).length ? h('ul', { class: 'warnings small' }, r.warnings.map((x) => h('li', null, '~ ' + x))) : null));
  return [head, (res.warnings || []).map((w) => note(w, 'warn')), h('ul', { class: 'verdicts' }, items)];
}

function killCard(d, refresh) {
  const k = d.kill;
  const out = h('div');
  const say = (res) => refresh(note([res.output, res.error].filter(Boolean).join(' ') || 'Done.', res.ok ? 'ok' : 'error'));
  if (k.active) {
    const input = h('input', { type: 'text', class: 'yes-input', 'aria-label': `Type ${k.word} to lift the kill switch`,
      placeholder: `type ${k.word}`, autocomplete: 'off' });
    const btn = h('button', { type: 'button', class: 'btn' }, 'Lift the kill switch');
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try { await say(await post('/api/risk/unkill', { typed: input.value })); } catch (e) { await refresh(note(e.message, 'error')); }
    });
    out.appendChild(h('p', null, h('span', { class: 'badge block' }, 'KILL SWITCH ON'),
      ` since ${when(k.since)} (${k.reason || 'no reason given'}). Every new entry is blocked today; exits still pass.`));
    out.appendChild(h('div', { class: 'fill-form' }, input, btn));
  } else {
    const reason = h('input', { type: 'text', class: 'price-input', 'aria-label': 'Why (optional)', placeholder: 'why (optional)', maxlength: 300 });
    const flat = h('input', { type: 'checkbox', id: 'kill-flatten' });
    const btn = h('button', { type: 'button', class: 'btn primary' }, 'Kill switch: stop new entries today');
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try { await say(await post('/api/risk/kill', { reason: reason.value, flatten: flat.checked })); } catch (e) { await refresh(note(e.message, 'error')); }
    });
    out.appendChild(h('p', null, h('span', { class: 'badge pass' }, 'off'),
      ' New entries are checked by every rule. The kill switch blocks them all for the rest of the day and revokes ' +
      'any entry PASS already recorded; exits always pass.'));
    out.appendChild(h('div', { class: 'fill-form' }, reason,
      h('label', { for: 'kill-flatten', class: 'small' }, flat, ' also write exit tickets (flatten)'), btn));
  }
  return card('Kill switch', out);
}

function breakersCard(b, rate) {
  const s = b.state || {};
  return card('Circuit-breakers (locks on new entries, never on exits)',
    (b.locks || []).map((x) => note(x, 'error')),
    Object.values(b.revenge || {}).map((x) => note(x, 'warn')),
    kv([
      ['Closed trades today', String(s.closed_trades || 0)],
      ['Losing streak', String(s.losing_streak || 0)],
      ['Realised today (after charges)', inr(s.realised_today || 0)],
      ['Drawdown lock at', s.drawdown_limit ? inr(-s.drawdown_limit) : 'off'],
      s.cooloff_until ? ['Cooling-off until', s.cooloff_until + ' IST'] : null,
      ['Order rate', `${rate.per_second} a second (hard cap ${rate.hard_cap}, SEBI registers algos above ${rate.sebi_threshold})`],
    ]),
    !(b.locks || []).length ? note('No breaker is on: entries are checked by the other rules.', 'ok') : null);
}

const GAUGES = [
  ['delta_lots', 'max_net_delta_lots', 'Net delta (lots)', (x) => num(x, 2)],
  ['gamma_lots_1pct', 'max_net_gamma_lots_1pct', 'Gamma (lots of delta per 1%)', (x) => num(x, 3)],
];

function gauge(label, v, limit, fmt, whyInfo) {
  const pctUsed = limit ? Math.min(100, (100 * Math.abs(v)) / limit) : 0;
  const over = limit && Math.abs(v) > limit;
  return h('div', { class: 'gauge' },
    h('div', { class: 'gauge-head' }, h('span', null, label), ' ',
      why(fmt(v), whyInfo, label), h('span', { class: 'muted small' }, limit ? ` of ${fmt(limit)}` : ' (no limit)')),
    h('div', { class: 'meter', role: 'meter', 'aria-label': label, 'aria-valuemin': 0, 'aria-valuemax': limit || 0,
      'aria-valuenow': Math.abs(v) }, styled(h('div', { class: 'meter-bar' + (over ? ' hot' : '') }),
      { width: (limit ? pctUsed : 0) + '%' })));
}

// the page's CSP allows no style attributes: set styles through the CSSOM
function styled(el, props) {
  Object.assign(el.style, props);
  return el;
}

function greeks(port, lim, whyInfo) {
  const g = port.greeks;
  const rows = [];
  for (const [u, x] of Object.entries(g.by_underlying)) {
    rows.push(h('h3', { class: 'small' }, u + (x.lot_size ? ` (lot ${x.lot_size})` : '')));
    for (const [key, lk, label, fmt] of GAUGES) rows.push(gauge(label, x[key], lim[lk], fmt, whyInfo));
  }
  rows.push(gauge('Net vega (₹ per vol point)', g.total.vega, lim.max_net_vega, inr, whyInfo));
  rows.push(gauge('Theta paid (₹ a day)', Math.min(0, g.total.theta), lim.max_theta_paid_per_day, inr, whyInfo));
  rows.push(h('p', { class: 'small muted' }, `Theta ${inr(g.total.theta)} a day; delta ${inr(g.total.delta_rupees_1pct)} per 1% move.`));
  return h('div', { class: 'gauges' }, rows);
}

function heatmap(s, limit, whyInfo) {
  const all = s.rows.flatMap((r) => r.pnl);
  const top = Math.max(1, ...all.map((x) => Math.abs(x)));
  const tint = (x) => ({
    background: `color-mix(in srgb, var(${x < 0 ? '--down' : '--up'}) ${Math.round((60 * Math.abs(x)) / top)}%, transparent)`,
  });
  const headers = [{ label: 'spot \\ IV' }].concat(s.iv_shifts.map((v) => ({ label: `${v > 0 ? '+' : ''}${v} vol`, num: true })));
  const rows = s.rows.map((r) => h('tr', null,
    h('th', { scope: 'row' }, `${r.sigma > 0 ? '+' : ''}${r.sigma}σ (${Object.values(r.move_pct).map((x) => (x > 0 ? '+' : '') + num(x)).join(', ')}%)`),
    r.pnl.map((x) => styled(h('td', { class: 'num heat' + (limit && x < -limit ? ' hot' : ''),
      title: limit && x < -limit ? 'over your max scenario loss' : null }, inr(x)), tint(x)))));
  const w = s.worst;
  return [table(headers, rows, { dense: true, caption: 'Scenario grid: P&L after charges' }),
    h('p', { class: 'small' }, 'Worst: ', why(inr(w.pnl), whyInfo, 'the scenario grid'),
      ` at ${w.sigma > 0 ? '+' : ''}${w.sigma}σ with IV ${w.iv_shift > 0 ? '+' : ''}${w.iv_shift} vol` +
      (limit ? ` (limit ${inr(-limit)})` : ' (no limit)') + `; over ${s.horizon_days} session(s).`)];
}

function bookCard(d) {
  const sc = d.scenarios;
  const lim = Object.fromEntries((d.fno_limits || []).map((x) => [x.key, x.value]));
  const pick = sc.with_proposals && sc.with_proposals.legs ? sc.with_proposals : sc.open;
  if (!pick || !pick.legs) {
    return card('F&O book: Greeks and scenarios',
      (pick && pick.errors || []).map((e) => note(e, 'warn')),
      note('No open F&O positions and no F&O proposals in trader/orders.json.', 'info'));
  }
  const label = pick === sc.open ? 'open positions' : 'open positions + every F&O proposal, as if filled';
  return card('F&O book: Greeks and scenarios',
    h('p', { class: 'small muted' }, `${pick.legs} leg(s): ${label}. ${sc.disclaimer}`),
    (pick.errors || []).map((e) => note(e, 'warn')),
    greeks(pick, lim, d.why.greeks),
    heatmap(pick.scenarios, lim.max_scenario_loss, d.why.scenarios));
}

export async function render(root) {
  root.appendChild(h('h1', { class: 'page-title' }, t('risk.title')));
  root.appendChild(h('p', { class: 'page-lede explain' }, t('risk.lede')));
  const body = h('div');
  root.appendChild(body);
  const refresh = async (flash) => {
    loading(body);
    const d = await get('/api/risk');
    clear(body);
    if (flash) body.appendChild(flash);
    const btn = h('button', { type: 'button', class: 'btn primary' }, t('risk.run'));
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const res = await post('/api/risk/check', {});
        await refresh(res.ok ? note('Recorded the verdicts in trader/state/risk.json.', 'ok')
          : note([res.output, res.error].filter(Boolean).join(' ') || 'The check did not run.', 'error'));
      } catch (e) {
        await refresh(note(e.message, 'error'));
      }
    });
    body.appendChild(killCard(d, refresh));
    body.appendChild(breakersCard(d.breakers, d.rate));
    body.appendChild(bookCard(d));
    const last = d.last_check;
    body.appendChild(card(t('risk.recorded'),
      last ? h('p', { class: 'small muted' }, `Checked ${whenUtc(last.checked_at)} for ${last.date}` +
        (last.prices_as_of ? `, prices as of ${when(last.prices_as_of)}` : '')) : null,
      d.stale ? note(`These verdicts are from ${last.date}, not today. Run the check again.`, 'warn') : null,
      last ? results(last, d.why.verdict, d.rule_words) : note('No risk check recorded yet.', 'info'),
      h('div', { class: 'row' }, btn)));
    body.appendChild(card(t('risk.preview'), d.preview ? results(d.preview, d.why.verdict, d.rule_words)
      : note(d.preview_error || 'No trader/orders.json to check (trader-budget writes it).', d.preview_error ? 'warn' : 'info')));
    body.appendChild(card(t('risk.limits'), table(['Rule', 'Value'],
      d.limits.concat(d.fno_limits || []).map((l) => h('tr', null, h('td', null, h('code', null, l.key)), h('td', null, value(l.key, l.value)))),
      { dense: true, caption: 'Risk limits' }), h('a', { href: '#/settings', class: 'more' }, 'Edit in Settings →')));
    body.appendChild(h('p', { class: 'muted small' }, d.disclaimer));
  };
  await refresh();
}
