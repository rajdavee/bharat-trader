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
      explain: 'Average result per trade in units of the amount risked.' }),
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
    ['Data', `${(d.symbols || []).length} stocks, ${run.timeframe} bars, ${d.from || '?'} to ${d.to || '?'} (${num(d.sessions, 0)} sessions)`],
    ['Product', run.product],
    ['Run', (run.updated || '') + ' IST, spec ' + (run.spec_hash || '')],
  ]);
  return card(h('span', null, run.title || run.name, ' ', h('code', { class: 'muted small' }, run.name)),
    h('div', { class: 'card-head' }, h('div', null, verdictTag(run.verdict), ' ', help('verdict')),
      h('span', { class: 'small muted' }, `${run.timeframe}, ${run.product}`)),
    h('p', null, run.verdict_why || ''),
    tiles,
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
  if (!d.runs.length) {
    body.appendChild(card('No lab runs yet', empty('Test a strategy before you trust it',
      'Run a backtest from the built-in library; its scorecard and equity curve appear here.',
      h('code', null, 'python3 trader-lab/lab.py run orb15-vwap'))));
    if (disc) body.appendChild(disc);
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
