// Strategy Lab: trader-lab's verdicts, after-charges scorecards, gates and equity curves (read only).
import { get } from '../api.js';
import { h, clear, card, kv, table } from '../dom.js';
import { inr, num, signed, tone } from '../fmt.js';
import { why, help } from '../why.js';
import { metric, empty, skeleton, failure, pageHead } from '../ui.js';
import { plot } from '../plot.js';

const STATE = { plots: [], off: [] };
const redraw = () => STATE.plots.forEach((p) => p.redraw());

const r = (x) => (x === null || x === undefined ? '–' : (Number(x) > 0 ? '+' : '') + num(x, 3) + 'R');
const rupees = (x) => (x === null || x === undefined ? '–' : (Number(x) > 0 ? '+' : '') + inr(Math.round(Number(x))).replace(/\.00$/, ''));
const share = (x) => (x === null || x === undefined ? '–' : num(Number(x) * 100, 0) + '%');

function verdictTag(v) {
  const k = String(v || '').toLowerCase();
  return h('span', { class: 'verdict-tag ' + k }, v || 'no verdict');
}

function gates(list) {
  if (!list.length) return null;
  const passed = list.filter((g) => g.pass).length;
  return h('div', null,
    h('h3', { class: 'sub' }, `Gates written before the run: ${passed} of ${list.length} passed`),
    h('ul', { class: 'gate-list' }, list.map((g) => h('li', { class: g.pass ? 'pass' : 'fail' },
      h('span', { class: 'mark', 'aria-hidden': 'true' }, g.pass ? '✓' : '✕'),
      h('span', null, h('span', { class: 'sr-only' }, g.pass ? 'Passed: ' : 'Failed: '), g.gate,
        h('span', { class: 'muted' }, ', needs ' + g.needs)),
      h('span', { class: 'got' }, String(g.got))))));
}

function curve(run) {
  const eq = run.equity || [];
  if (eq.length < 2) return empty('No equity curve', 'This run wrote no equity.csv.');
  const canvas = h('canvas', { class: 'plot', tabindex: '0', role: 'img',
    'aria-label': `Equity of ${run.name} from ${eq[0].date} to ${eq[eq.length - 1].date}: ` +
      `${inr(eq[0].equity)} to ${inr(eq[eq.length - 1].equity)}. Use the arrow keys to read values.` });
  const box = h('div', { class: 'plot-box' }, canvas);
  const oos = eq.map((p, i) => (p.oos ? i : null)).filter((i) => i !== null);
  const opts = {
    x: eq.map((_, i) => i), xlabel: '', xfmt: (i) => (eq[Math.round(i)] || {}).date || '',
    yfmt: (v) => inr(v).replace(/\.\d\d$/, ''),
    series: [{ y: eq.map((p) => p.equity), color: '--accent', width: 2, label: 'equity' }],
    band: oos.length ? { lo: oos[0], hi: oos[oos.length - 1] } : null,
  };
  requestAnimationFrame(() => STATE.plots.push(plot(canvas, opts)));
  return h('div', null,
    h('ul', { class: 'legend-list' }, h('li', null, h('span', { class: 'swatch accent' }), 'Equity after charges (₹)'),
      oos.length ? h('li', null, h('span', { class: 'swatch band' }), 'Out-of-sample windows') : null),
    box);
}

function regimes(run) {
  const rows = [].concat(...Object.values(run.regimes || {}).map((x) => (Array.isArray(x) ? x : [])));
  if (!rows.length) return null;
  return h('div', null, h('h3', { class: 'sub' }, 'By market regime (whole sample) ', help('regime')),
    table(['Regime', { label: 'Trades', num: true }, { label: 'Win rate', num: true }, { label: 'Expectancy', num: true },
      { label: 'Net after charges', num: true }],
    rows.map((x) => h('tr', null, h('td', null, x.regime), h('td', { class: 'num' }, num(x.trades, 0)),
      h('td', { class: 'num' }, share(x.win_rate)), h('td', { class: 'num' }, r(x.expectancy_r)),
      h('td', { class: 'num' }, h('span', { class: tone(x.net) }, signed(x.net))))), { dense: true, caption: 'Results by regime' }));
}

const money = (x) => (x === null || x === undefined ? '–' : inr(Math.round(Number(x))).replace(/\.00$/, ''));
const pct = (x) => (x === null || x === undefined ? '–' : num(x, 2) + '%');

// An options run: return on margin, tail losses, streaks, time in drawdown and short-strike breaches.
function optionsBlock(run) {
  const o = run.options;
  if (!o) return null;
  const w = o.whole_sample || {};
  const s = o.out_of_sample || {};
  const tiles = h('div', { class: 'metrics' },
    metric({ label: 'Return on margin, a year', value: why(pct(w.return_on_margin_annual_pct), o.why.return_on_margin,
      'return on margin'), tone: tone(w.return_on_margin_annual_pct) || null,
    sub: `${pct(w.return_on_margin_pct)} in all on ${money(w.avg_margin)} average margin; out of sample ${pct(s.return_on_margin_annual_pct)} a year`,
    explain: 'Net after charges over the estimated margin blocked (SPAN-style), not over the premium.' }),
    metric({ label: 'Tail loss (5% / 1%)', value: why(`${rupees(w.tail_loss_5pct)} / ${rupees(w.tail_loss_1pct)}`,
      o.why.tail, 'the tail loss'), tone: 'warn',
    sub: `Worst position ${rupees(w.worst_trade)}; worst week ${rupees(w.worst_week)}`,
    explain: 'What the worst 1 in 20 and 1 in 100 positions lost, after charges.' }),
    metric({ label: 'Short strike breached', value: pct(w.breach_pct),
      sub: `${num(w.expiries, 0)} expiries; ${num(w.max_consecutive_losses, 0)} losses in a row at worst`,
      explain: 'Share of expiries where the index closed beyond a short strike while the position was open.' }),
    metric({ label: 'Time in drawdown', value: pct(w.time_in_drawdown_pct),
      sub: `Max drawdown ${num(w.max_drawdown_mtm_pct, 2)}% of capital, marked to market daily`,
      explain: 'Share of sessions the marked-to-market equity sat below its previous high.' }));
  const rows = [['Positions', 'positions', (x) => num(x, 0)], ['Average margin blocked', 'avg_margin', money],
    ['Return on margin, a position', 'avg_return_on_margin_pct', pct], ['Worst week', 'worst_week', rupees],
    ['Average of the worst 5%', 'tail_avg_worst_5pct', rupees], ['Max consecutive losses', 'max_consecutive_losses', (x) => num(x, 0)],
    ['Short strike breached', 'breach_pct', pct]];
  return h('div', null,
    h('h3', { class: 'sub' }, `Options: ${o.underlying || ''} ${o.structure || ''}`),
    o.rules ? h('p', { class: 'small muted' }, o.rules) : null,
    tiles,
    table(['', { label: 'Whole sample', num: true }, { label: 'Out of sample', num: true }],
      rows.map(([label, k, f]) => h('tr', null, h('td', null, label), h('td', { class: 'num' }, f(w[k])),
        h('td', { class: 'num' }, f(s[k])))), { dense: true, caption: 'Options numbers after charges and slippage' }),
    w.model_priced_marks ? h('p', { class: 'small muted' }, `${num(w.model_priced_marks, 0)} leg marks were priced by ` +
      'Black-76 because the strike did not trade that day.') : null);
}

function runCard(run) {
  const o = run.oos || {};
  const f = run.full || {};
  const mc = run.monte_carlo || {};
  const d = run.data || {};
  const small = (o.trades || 0) < 30;
  const tiles = h('div', { class: 'metrics' },
    metric({ label: 'Out-of-sample trades', term: 'out_of_sample', value: num(o.trades, 0),
      tone: small ? 'warn' : null, sub: `${num(f.trades, 0)} in the whole sample`,
      explain: small ? 'Fewer than 30: too few to judge the rules.' : 'The sample size behind the numbers below.' }),
    metric({ label: 'Expectancy, after charges', term: 'expectancy', tone: tone(o.expectancy_r) || null,
      value: h('span', { class: tone(o.expectancy_r) }, why(r(o.expectancy_r), run.why.oos_expectancy, 'this expectancy')),
      sub: `Net ${signed(o.net)} out of sample; whole sample ${r(f.expectancy_r)}`,
      explain: run.kind === 'options' ? 'Average return on the margin blocked, per position (R = net / margin).'
        : 'Average result per trade in units of the amount risked.' }),
    metric({ label: 'Final P&L range (5th to 95th percentile)', term: 'monte_carlo',
      value: why(`${rupees(mc.final_p5)} to ${rupees(mc.final_p95)}`, run.why.mc_range, 'this range'),
      sub: `${share(mc.prob_loss)} of ${num(mc.runs, 0)} reshuffles ended in a loss`,
      explain: 'Where the same trades could have ended in another order: luck, not a forecast.' }),
    metric({ label: 'Max drawdown, whole sample', term: 'drawdown',
      value: num(f.max_drawdown_pct, 2) + '% of capital', sub: `Profit factor ${num(o.profit_factor, 2)} out of sample`,
      explain: 'The deepest fall from a high point.' }));
  const robust = kv([
    ['Deflated Sharpe ratio', h('span', null, why(run.dsr === null ? '–' : num(run.dsr, 4), run.why.dsr, 'the deflated Sharpe'), ' ', help('dsr'))],
    ['Probability of overfitting', h('span', null, why(run.pbo === null ? '–' : num(run.pbo, 3), run.why.pbo, 'this probability'), ' ', help('pbo'))],
    ['Paper forward test', run.forward && run.forward.trades ? `${run.forward.trades} trades, ${signed(run.forward.net)}`
      : 'not started (0 trades)'],
    ['Data', run.kind === 'options' ? `${(d.symbols || []).join(', ')} end-of-day option chains, ${d.from || '?'} to ${d.to || '?'} (${num(d.sessions, 0)} sessions)`
      : `${(d.symbols || []).length} stocks, ${run.timeframe} bars, ${d.from || '?'} to ${d.to || '?'} (${num(d.sessions, 0)} sessions)`],
    ['Product', run.product],
    ['Run', (run.updated || '') + ' IST, spec ' + (run.spec_hash || '')],
  ]);
  return card(h('span', null, run.title || run.name, ' ', h('code', { class: 'muted small' }, run.name)),
    h('div', { class: 'card-head' }, h('div', null, verdictTag(run.verdict), ' ', help('verdict')),
      h('span', { class: 'small muted' }, `${run.timeframe}, ${run.product}`)),
    h('p', null, run.verdict_why || ''),
    tiles,
    optionsBlock(run),
    curve(run),
    h('div', { class: 'cols' }, h('div', null, gates(run.gates || []), regimes(run)),
      h('div', null, h('h3', { class: 'sub' }, 'Robustness'), robust)),
    h('div', { class: 'note warn' }, h('b', null, 'What this does not show. '), run.limits),
    h('p', { class: 'small muted' }, 'Full scorecard: ', h('code', null, `python3 trader-lab/lab.py report ${run.name}`)));
}

function discoveryCard(d) {
  const runs = d.discovery || [];
  if (!runs.length) return null;
  const rows = runs.map((x) => {
    const c = x.counts || {};
    const fins = (x.finalists || []).map((f) => {
      const lab = f.lab || {};
      return h('li', null, h('code', null, lab.name || f.family), ' ', lab.verdict ? verdictTag(lab.verdict) : null);
    });
    return h('tr', null,
      h('td', null, h('code', null, x.name)),
      h('td', null, `${x.timeframe}, ${x.symbols} stocks`),
      h('td', null, x.state === 'decided' ? 'decided' : `${x.state}: ${x.trials_run} of ${x.budget} trials`),
      h('td', { class: 'num' }, c.counted === undefined ? '–' : num(c.counted, 0)),
      h('td', { class: 'num' }, c.survived_validation === undefined ? '–' : num(c.survived_validation, 0)),
      h('td', { class: 'num' }, c.finalists === undefined ? '–' : num(c.finalists, 0)),
      h('td', null, fins.length ? h('ul', { class: 'plain' }, fins) : (x.state === 'decided' ? 'none' : '–')));
  });
  return card('Discovery runs',
    h('p', { class: 'explain' }, 'Searches over the lab\'s rules with every trial counted. Trials: all of them, ' +
      'neighbour checks included. Survived validation: before robustness and multiple testing. Finalists: also ' +
      'survived the untouched holdout; they are lab specs above, WATCH at best.'),
    table(['Run', 'Bars', 'State', { label: 'Trials counted', num: true }, { label: 'Survived validation', num: true },
      { label: 'Finalists', num: true }, 'In the lab'], rows, { caption: 'Discovery runs' }),
    h('div', { class: 'note warn' }, h('b', null, 'What this does not show. '), d.discovery_limits || ''),
    h('p', { class: 'small muted' }, 'Full report: ', h('code', null, 'python3 trader-discover/discover.py report RUN')));
}

function huntTag(st) {
  const k = { watch: 'watch', promoted: 'promote', rejected: 'reject' }[st] || '';
  return h('span', { class: 'verdict-tag ' + k }, st || 'registered');
}

function edgeHuntCard(d) {
  const e = d.edge_hunt || {};
  const rows = (e.hypotheses || []).map((x) => h('tr', null,
    h('td', { class: 'num' }, num(x.rank, 0)),
    h('td', null, h('code', null, x.id), h('div', { class: 'small muted' }, x.title || '')),
    h('td', null, x.family), h('td', null, x.engine),
    h('td', null, huntTag(x.status), x.registered_ok === false ? h('div', { class: 'small' }, 'file changed: ' + (x.problem || '')) : null),
    h('td', { class: 'num' }, `${num(x.trials_run, 0)} of ${num(x.trial_budget, 0)}`),
    h('td', null, x.holdout_opened ? `opened ${x.holdout_opened}` + (x.p_value === null || x.p_value === undefined ? '' : `, p ${num(x.p_value, 3)}`) : 'sealed'),
    h('td', null, x.verdict ? h('span', null, verdictTag(x.verdict), ' ', h('span', { class: 'small muted' }, x.why || '')) : '–')));
  if (!rows.length) return null;
  return card('Edge hunt: pre-registered hypotheses',
    h('p', { class: 'explain' }, 'Ideas written down with their data, grid, trial budget and gates before any run. ' +
      'Trials: grid points run on train and validation. The holdout is opened once, only for a finalist that passed ' +
      'every validation gate. ' + (e.note || '') + '.'),
    table([{ label: 'Rank', num: true }, 'Hypothesis', 'Family', 'Engine', 'Status', { label: 'Trials', num: true },
      'Holdout', 'Outcome'], rows, { caption: 'Edge-hunt hypotheses' }),
    h('div', { class: 'note warn' }, h('b', null, 'What this does not show. '), e.limits || ''),
    h('p', { class: 'small muted' }, 'Run one: ', h('code', null, 'python3 trader-lab/lab.py hunt run EH-ID --dry-run')));
}

// Strategy portfolio: the correlation heatmap, weights per sizing scheme, the walk-forward mix with its bands.
const heatClass = (v) => {
  const k = Math.max(-3, Math.min(3, Math.round(Number(v) * 3)));
  return 'heat ' + (k < 0 ? 'heat-n' + -k : 'heat-p' + k);
};

// Columns are numbered (1, 2, ...) like the rows, so three matrices fit side by side.
function heatmap(names, m, caption) {
  if (!m) return null;
  return table([''].concat(names.map((n, i) => ({ label: String(i + 1), num: true }))),
    names.map((n, i) => h('tr', null, h('th', { scope: 'row' }, `${i + 1}. ${n}`),
      m[i].map((v, j) => h('td', { class: 'num ' + (i === j ? 'heat heat-self' : heatClass(v)) }, num(v, 2))))),
    { dense: true, caption });
}

function mixCurve(mix) {
  const sc = mix.schemes.find((x) => x.chosen) || {};
  const eq = sc.curve || [];
  if (eq.length < 2) return empty('No walk-forward curve', 'The overlap is too short to test the weights.');
  const at = {};
  eq.forEach((p, i) => { at[p.date] = i; });
  const lo = eq.map(() => null);
  const hi = eq.map(() => null);
  for (const b of sc.bands || []) {
    if (at[b.date] !== undefined) { lo[at[b.date]] = b.p5; hi[at[b.date]] = b.p95; }
  }
  const fill = (xs) => { let last = null; return xs.map((v) => { if (v !== null) last = v; return v === null ? last : v; }); };
  const others = mix.schemes.filter((x) => !x.chosen && (x.curve || []).length === eq.length);
  const canvas = h('canvas', { class: 'plot', tabindex: '0', role: 'img',
    'aria-label': `Walk-forward growth of 1 rupee in the ${sc.label} mix from ${eq[0].date} to ${eq[eq.length - 1].date}: ` +
      `${num(eq[eq.length - 1].equity, 3)} at the end. Use the arrow keys to read values.` });
  const opts = {
    x: eq.map((_, i) => i), xlabel: '', xfmt: (i) => (eq[Math.round(i)] || {}).date || '',
    yfmt: (v) => num(v, 3),
    series: [{ y: eq.map((p) => p.equity), color: '--accent', width: 2, label: sc.label }]
      .concat(sc.bands && sc.bands.length ? [
        { y: fill(lo), color: '--muted', width: 1, dash: [4, 4], label: 'bootstrap 5th percentile' },
        { y: fill(hi), color: '--muted', width: 1, dash: [4, 4], label: 'bootstrap 95th percentile' }] : [])
      .concat(others.map((x, k) => ({ y: x.curve.map((p) => p.equity), color: ['--ema1', '--ema2', '--vwap'][k % 3],
        width: 1, label: x.label }))),
  };
  requestAnimationFrame(() => STATE.plots.push(plot(canvas, opts)));
  return h('div', null,
    h('ul', { class: 'legend-list' }, h('li', null, h('span', { class: 'swatch accent' }), `${sc.label} (chosen), growth of ₹1`),
      sc.bands && sc.bands.length ? h('li', null, h('span', { class: 'swatch dashed' }), why('Bootstrap 5th to 95th percentile',
        mix.why.bands, 'these bands')) : null,
      others.map((x, k) => h('li', null, h('span', { class: 'swatch ' + ['ema1', 'ema2', 'vwap'][k % 3] }), x.label))),
    h('div', { class: 'plot-box' }, canvas));
}

function mixCard(mix) {
  const names = mix.strategies.map((x) => x.name);
  const research = mix.strategies.filter((x) => !x.eligible).map((x) => x.name);
  const ov = mix.overlap || {};
  const chosen = mix.schemes.find((x) => x.chosen) || {};
  const m = chosen.metrics || {};
  const b = chosen.bootstrap || {};
  const tiles = h('div', { class: 'metrics' },
    metric({ label: 'Sessions every strategy shares', value: num(ov.sessions, 0), tone: ov.sessions < 252 ? 'warn' : null,
      sub: `${ov.from || '?'} to ${ov.to || '?'}; weights tested on ${num(mix.scored.sessions, 0)} of them`,
      explain: 'Only the overlap is used: out-of-sample and paper forward days, never the in-sample ones.' }),
    metric({ label: 'Walk-forward Sharpe (chosen scheme)', value: num(m.sharpe, 2), tone: tone(m.sharpe) || null,
      sub: `CAGR ${pct(m.cagr_pct)}, volatility ${pct(m.vol_pct)}, max drawdown ${pct(m.max_drawdown_pct)}`,
      explain: 'After charges; weights fitted on earlier sessions only.' }),
    metric({ label: 'Diversification ratio', value: num(chosen.diversification_ratio, 2),
      sub: `About ${num(chosen.effective_bets, 1)} independent bets from ${names.length} strategies`,
      explain: 'Weighted volatility of the parts over the volatility of the mix; 1 means no diversification.' }),
    metric({ label: 'Bootstrap final return (5th to 95th)', tone: 'warn',
      value: b.final_pct ? `${pct(b.final_pct.p5)} to ${pct(b.final_pct.p95)}` : '–',
      sub: b.max_drawdown_pct ? `Max drawdown ${pct(b.max_drawdown_pct.p95)} at the 95th percentile; ${share(b.prob_loss)} of paths lost` : '',
      explain: 'The same past days reshuffled in blocks: luck, not a forecast.' }));
  const weights = table(['Scheme'].concat(names.map((n) => ({ label: n, num: true })),
    [{ label: 'Cash', num: true }, { label: 'Sharpe', num: true }, { label: 'Max drawdown', num: true },
      { label: 'Worst month', num: true }, { label: 'Time in drawdown', num: true }]),
  mix.schemes.map((x) => {
    const mm = x.metrics || {};
    return h('tr', { class: x.chosen ? 'chosen' : null },
      h('td', null, x.label, x.chosen ? h('span', { class: 'small muted' }, ' (chosen)') : null),
      names.map((_, i) => h('td', { class: 'num' }, x.weights ? share(x.weights[i]) : '–')),
      h('td', { class: 'num' }, share(x.cash)), h('td', { class: 'num' }, num(mm.sharpe, 2)),
      h('td', { class: 'num' }, pct(mm.max_drawdown_pct)),
      h('td', { class: 'num' }, mm.worst_month ? `${mm.worst_month} ${pct(mm.worst_month_pct)}` : '–'),
      h('td', { class: 'num' }, pct(mm.time_in_drawdown_pct)));
  }), { caption: 'Weights per sizing scheme' });
  const c = mix.correlation || {};
  const cl = mix.clusters || {};
  const ap = (mix.proposals || {}).autopilot;
  const rk = (mix.proposals || {}).risk;
  const buckets = ap ? Object.entries((ap.autopilot || {}).strategies || {}) : [];
  return card(h('span', null, 'Portfolio ', h('code', { class: 'muted small' }, mix.name)),
    research.length ? h('div', { class: 'note warn' }, h('b', null, 'Research only. '),
      `${research.join(', ')} ${research.length === 1 ? 'is' : 'are'} not WATCH or PROMOTE: studied here, never proposed.`) : null,
    tiles,
    h('h3', { class: 'sub' }, 'Correlation of daily returns after charges ', why('(how)', mix.why.correlation, 'this correlation')),
    h('div', { class: 'heat-grid' },
      h('div', null, h('p', { class: 'small muted' }, 'Pearson'), heatmap(names, c.pearson, 'Pearson correlation')),
      h('div', null, h('p', { class: 'small muted' }, 'Downside (losing days)'), heatmap(names, c.downside, 'Downside correlation')),
      h('div', null, h('p', { class: 'small muted' }, `Stress (the mix's worst ${num(mix.stress_days, 0)} days; biased, read a rise)`),
        heatmap(names, c.stress, 'Stress correlation'))),
    (cl.duplicates || []).length ? h('div', { class: 'note warn' }, h('b', null, 'Same bet. '),
      cl.duplicates.map((g) => g.join(' + ')).join('; ') + ` correlate at ${num(cl.threshold, 2)} or more.`) : null,
    h('h3', { class: 'sub' }, 'Weights per scheme (latest walk-forward fit) and how each mix did'),
    weights,
    h('p', { class: 'small muted' }, mix.chosen_why + '.'),
    mixCurve(mix),
    ap ? h('div', null, h('h3', { class: 'sub' }, 'Proposals (files only, never applied)'),
      kv([['Auto-pilot buckets', buckets.length ? buckets.map(([n, x]) => `${n} ${x.capital ? money(x.capital) : share(x.weight)}`).join(', ')
        : 'none (no WATCH or PROMOTE lab strategy in this mix)'],
      ['trader-risk', rk ? `max_daily_drawdown_pct ${num(rk.max_daily_drawdown_pct, 1)}; pause and review at a ${num(rk.portfolio_drawdown_pause_pct, 1)}% portfolio drawdown` : '–'],
      ['Files', h('code', null, `trader/lab/portfolio/${mix.name}/proposal-autopilot.json, proposal-risk.json`)]]),
      h('p', { class: 'small muted' }, ap.how)) : null,
    (mix.notes || []).length ? h('div', { class: 'note warn' }, h('b', null, 'Read this first. '),
      h('ul', { class: 'plain' }, mix.notes.map((x) => h('li', null, x)))) : null,
    h('div', { class: 'note warn' }, h('b', null, 'What this does not show. '), (mix.limits || []).join(' ')),
    h('p', { class: 'small muted' }, 'Full scorecard: ', h('code', null, `python3 trader-lab/lab.py portfolio report ${mix.name}`)));
}

function portfolioCards(d) {
  const p = d.portfolio || {};
  const mixes = p.mixes || [];
  if (!mixes.length) {
    return [card('Portfolio', empty('No strategy portfolio yet',
      (p.eligible || []).length ? `WATCH and PROMOTE strategies that can go in one: ${p.eligible.join(', ')}.`
        : 'A portfolio combines WATCH and PROMOTE strategies; none yet. Most ideas are REJECT, and that is the lab working.',
      h('code', null, 'python3 trader-lab/lab.py portfolio build my-mix')))];
  }
  return mixes.map(mixCard);
}

export async function render(root, ctx) {
  clear(root);
  if (ctx && ctx.bus) {
    ctx.bus.addEventListener('theme', redraw);
    STATE.off.push(() => ctx.bus.removeEventListener('theme', redraw));
  }
  root.append(...pageHead('Strategy Lab', 'Honest backtests: every number is after charges and slippage, scored out of ' +
    'sample, with the gates written down before the run.'));
  const body = h('div', null, skeleton(5));
  root.appendChild(body);
  let d;
  try {
    d = await get('/api/lab');
  } catch (e) {
    clear(body).appendChild(failure(e.message || String(e), () => render(root, ctx)));
    return;
  }
  clear(body);
  const disc = discoveryCard(d);
  const hunt = edgeHuntCard(d);
  if (!d.runs.length) {
    body.appendChild(card('No lab runs yet', empty('Test a strategy before you trust it',
      'Run a backtest from the built-in library; its scorecard and equity curve appear here.',
      h('code', null, 'python3 trader-lab/lab.py run orb15-vwap'))));
    if (hunt) body.appendChild(hunt);
    if (disc) body.appendChild(disc);
    for (const c of portfolioCards(d)) body.appendChild(c);
    return;
  }
  const counts = {};
  for (const x of d.runs) counts[x.verdict] = (counts[x.verdict] || 0) + 1;
  body.appendChild(card('Runs', table(['Strategy', 'Verdict', { label: 'Out-of-sample trades', num: true },
    { label: 'Expectancy', num: true }, { label: 'Net after charges', num: true }, 'Updated'],
  d.runs.map((x) => h('tr', null, h('td', null, h('a', { href: '#lab-' + x.name }, x.title || x.name)),
    h('td', null, verdictTag(x.verdict)), h('td', { class: 'num' }, num(x.oos.trades, 0)),
    h('td', { class: 'num' }, r(x.oos.expectancy_r)),
    h('td', { class: 'num' }, h('span', { class: tone(x.oos.net) }, signed(x.oos.net))), h('td', null, (x.updated || '') + ' IST'))),
  { caption: 'Lab runs' }), h('p', { class: 'explain' }, Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(', ') +
    '. A REJECT is the lab doing its job: most ideas should fail here, before they cost money.')));
  if (hunt) body.appendChild(hunt);
  for (const c of portfolioCards(d)) body.appendChild(c);
  if (disc) body.appendChild(disc);
  for (const run of d.runs) {
    const c = runCard(run);
    c.id = 'lab-' + run.name;
    c.classList.add('lab-run');
    body.appendChild(c);
  }
  for (const a of body.querySelectorAll('a[href^="#lab-"]')) {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      const t = document.getElementById(a.getAttribute('href').slice(1));
      if (t) { t.scrollIntoView(); t.setAttribute('tabindex', '-1'); t.focus({ preventScroll: true }); }
    });
  }
}

export function destroy() {
  STATE.plots.forEach((p) => p.destroy());
  STATE.plots = [];
  STATE.off.forEach((f) => f());
  STATE.off = [];
}
