// Co-trader: trade candidates from backtested rules with their evidence tier, odds with a range and n, the result
// after charges, the risk verdict, why / why not, a draft-to-paper button and the calibration of past odds.
// Sitting out is shown as a decision. Every number comes from trader-cotrader.
import { get, post } from '../api.js';
import { h, clear, card, kv, table, note } from '../dom.js';
import { inr, num } from '../fmt.js';
import { help } from '../why.js';
import { metric, empty, skeleton, failure, pageHead, toast } from '../ui.js';
import { plot } from '../plot.js';

const STATE = { plots: [], off: [], research: false };
const redraw = () => STATE.plots.forEach((p) => p.redraw());

export const share = (x) => (x === null || x === undefined ? '–' : num(Number(x) * 100, 0) + '%');
export const rmult = (x) => (x === null || x === undefined ? '–' : (Number(x) > 0 ? '+' : '') + num(x, 2) + 'R');

export function tierBadge(t) {
  const cls = { PROMOTED: 'promote', WATCH: 'watch', RESEARCH: 'reject' }[t] || '';
  return h('span', { class: 'verdict-tag ' + cls }, t === 'RESEARCH' ? 'RESEARCH: unproven' : t);
}

export function oddsText(ev) {
  return `${share(ev.p)} (95% ${share(ev.p_lo)} to ${share(ev.p_hi)}), n = ${num(ev.trades_cell, 0)} in this regime, ` +
    `${num(ev.trades_all, 0)} in all`;
}

function riskBadge(c) {
  const r = c.risk;
  if (!r) return h('span', { class: 'badge warn' }, c.refusal ? 'Not sized' : 'Not checked');
  return h('span', { class: 'badge ' + (r.verdict === 'PASS' ? 'pass' : 'block') }, 'trader-risk ' + r.verdict);
}

function list(items, cls) {
  return items && items.length ? h('ul', { class: cls || null }, items.map((x) => h('li', null, x))) : null;
}

function candidateCard(c, onDraft, draftable) {
  const ev = c.evidence;
  const p = c.plan;
  const s = c.sizing || {};
  const so = c.second_opinion || {};
  const x = c.explain || {};
  const tiles = h('div', { class: 'metrics' },
    metric({ label: 'Made money after charges, in the past', term: 'probability', value: share(ev.p),
      sub: `95% ${share(ev.p_lo)} to ${share(ev.p_hi)}; n = ${num(ev.trades_cell, 0)} here, ${num(ev.trades_all, 0)} in all`,
      tone: ev.trades_cell < 10 ? 'warn' : null,
      explain: ev.trades_cell < 10 ? 'Few trades in this kind of market: it leans on the rule\'s overall rate.'
        : 'The share of similar past trades, not a promise about this one.' }),
    metric({ label: 'Expected result per trade, after charges', term: 'expectancy', value: rmult(ev.ev_r),
      sub: ev.ev_lo !== null && ev.ev_lo !== undefined ? `95% ${rmult(ev.ev_lo)} to ${rmult(ev.ev_hi)}` +
        (c.ev_rupees !== null && c.ev_rupees !== undefined ? `; about ${inr(c.ev_rupees)}` : '') : '',
      explain: 'In units of the amount risked at the stop.' }),
    metric({ label: 'Time-machine replay', value: so.available ? share(so.p_green) + ' green' : '–',
      sub: so.available ? `${rmult(so.avg_r_net)} after charges` : (so.why || ''),
      tone: (c.disagreements || []).length ? 'warn' : null,
      explain: (c.disagreements || []).length ? 'Disagrees with the history: see below.' : 'A second opinion from the stock\'s own bars.' }));
  const plan = kv([
    ['Plan', `Buy ${c.product} on ${c.date}` + (c.timeframe === '5m' ? ` after ${p.entry_time} IST` : ' at the open')],
    ['Entry, stop, target', `${inr(p.entry)}, ${inr(p.stop)}, ${inr(p.target)}`],
    ['Size', s.qty ? `${num(s.qty, 0)} shares (${inr(s.value)}); loss at the stop ${inr(s.max_loss_with_charges)} with charges`
      : (c.refusal || 'not sized')],
    ['Charges at the target', c.charges_at_target === null || c.charges_at_target === undefined ? '–' : inr(c.charges_at_target)],
    ['Regime', ev.cell_words],
    ['Rank', `#${c.rank} (score ${num(c.score, 3)}${c.rho_with ? `; correlation ${num(c.rho, 2)} with ${c.rho_with}` : ''})`],
  ]);
  const why = h('div', null,
    h('h3', { class: 'sub' }, 'Why this trade'), h('p', null, x.why || ''),
    h('h3', { class: 'sub' }, 'What would invalidate it'), list(x.invalidation),
    (c.why_not || []).length ? h('div', null, h('h3', { class: 'sub' }, 'Why not (your rules)'), list(c.why_not)) : null,
    (c.disagreements || []).length ? note('Disagreement: ' + c.disagreements.join('; '), 'warn') : null);
  const sim = (c.similar || []).length ? h('details', null, h('summary', null, 'Nearest similar past trades'),
    table(['Signal', 'Stock', 'Regime', { label: 'Result', num: true }, { label: 'Net', num: true }, 'Exit'],
      c.similar.map((t) => h('tr', null, h('td', null, t.signal_date), h('td', null, t.symbol), h('td', null, t.regime),
        h('td', { class: 'num' }, rmult(t.r)), h('td', { class: 'num' }, inr(t.net)), h('td', null, t.reason))),
      { dense: true, caption: 'Similar past trades' })) : null;
  const draft = h('button', { type: 'button', class: 'btn primary', disabled: !draftable(c),
    title: draftable(c) ? 'Size it and run the risk check; nothing is placed' : 'Only a ready plan for today can be drafted',
    onclick: () => onDraft(c) }, 'Draft to paper');
  return card(h('span', null, c.symbol, ' ', h('code', { class: 'muted small' }, c.strategy)),
    h('div', { class: 'card-head' }, h('div', null, tierBadge(c.tier), ' ', help('evidence_tier'), ' ', riskBadge(c)),
      h('span', { class: 'small muted' }, c.label)),
    tiles, h('div', { class: 'cols' }, plan, why),
    h('div', { class: 'note warn' }, h('b', null, 'What the evidence does not show. '), list(x.does_not_show)),
    h('p', { class: 'small muted' }, 'Evidence: ', ev.basis), sim,
    h('div', { class: 'row' }, draft, h('code', { class: 'small muted' }, `python3 trader-cotrader/cotrader.py explain ${c.id}`)));
}

export function noEdgeCard(ne, research) {
  return card('No trade today',
    h('p', null, h('b', null, ne.headline)),
    h('h3', { class: 'sub' }, 'Why'), list(ne.reasons),
    (ne.would_change || []).length ? h('div', null, h('h3', { class: 'sub' }, 'What would change that'), list(ne.would_change)) : null,
    h('p', { class: 'small muted' }, 'Market now: ', ne.regime_now || '–', research ? '' : '. RESEARCH ideas are hidden.'));
}

function strategiesCard(run) {
  const rows = run.strategies || [];
  if (!rows.length) return null;
  return card('Strategies looked at', table(['Strategy', 'Tier', 'Bars', 'Today'],
    rows.map((r) => h('tr', null, h('td', null, h('code', null, r.name)), h('td', null, tierBadge(r.tier)),
      h('td', null, r.timeframe || '–'), h('td', null, r.status || ''))), { caption: 'Strategies' }),
  (run.notes || []).length ? h('details', null, h('summary', null, `${run.notes.length} notes`), list(run.notes)) : null);
}

function calibrationCard(cal) {
  if (!cal || cal.error) return card('Calibration', note('Calibration: ' + ((cal && cal.error) || 'not available'), 'warn'));
  const g = cal.overall || {};
  const body = [h('p', null, cal.words, ' ', help('calibration'))];
  const bins = g.bins || [];
  if (bins.length) {
    const canvas = h('canvas', { class: 'plot', tabindex: '0', role: 'img',
      'aria-label': 'Reliability: ' + bins.map((b) => `predicted ${share(b.mean_p)}, observed ${share(b.observed)} (n ${b.n})`).join('; ') });
    const xs = [0].concat(bins.map((b) => b.mean_p)).concat([1]);
    const opts = {
      x: xs, xfmt: (v) => share(v), yfmt: (v) => share(v),
      series: [
        { y: xs, color: '--muted', width: 1, dash: [4, 4], label: 'perfect calibration' },
        { y: [null].concat(bins.map((b) => b.observed)).concat([null]), color: '--accent', width: 2, points: true,
          label: 'observed' },
      ],
    };
    requestAnimationFrame(() => STATE.plots.push(plot(canvas, opts)));
    body.push(h('ul', { class: 'legend-list' }, h('li', null, h('span', { class: 'swatch accent' }), 'Observed share that made money'),
      h('li', null, h('span', { class: 'swatch dashed' }), 'Perfect calibration')), h('div', { class: 'plot-box' }, canvas));
    body.push(table([{ label: 'Predicted', num: true }, { label: 'n', num: true }, { label: 'Observed', num: true }, '95% range'],
      bins.map((b) => h('tr', null, h('td', { class: 'num' }, share(b.mean_p)), h('td', { class: 'num' }, num(b.n, 0)),
        h('td', { class: 'num' }, share(b.observed)), h('td', null, `${share(b.ci[0])} to ${share(b.ci[1])}`))),
      { dense: true, caption: 'Reliability bins' }));
  }
  const by = Object.entries(cal.by_strategy || {});
  if (by.length) {
    body.push(h('h3', { class: 'sub' }, 'By strategy'), table(['Strategy', { label: 'n', num: true },
      { label: 'Predicted', num: true }, { label: 'Observed', num: true }, { label: 'Brier', num: true }],
    by.map(([k, v]) => h('tr', null, h('td', null, h('code', null, k)), h('td', { class: 'num' }, num(v.n, 0)),
      h('td', { class: 'num' }, share(v.mean_p)), h('td', { class: 'num' }, share(v.observed)),
      h('td', { class: 'num' }, num(v.brier, 3)))), { dense: true, caption: 'Calibration by strategy' }));
  }
  for (const d of cal.demote || []) body.push(note(`Overconfident: ${d.strategy}. ${d.why}`, 'warn'));
  return card('Calibration: did past odds come true?', ...body);
}

export async function render(root, ctx) {
  clear(root);
  if (ctx && ctx.bus) {
    ctx.bus.addEventListener('theme', redraw);
    STATE.off.push(() => ctx.bus.removeEventListener('theme', redraw));
  }
  const toggle = h('input', { type: 'checkbox', id: 'ct-research' });
  toggle.checked = STATE.research;
  const refresh = h('button', { type: 'button', class: 'btn' }, 'Refresh candidates');
  root.append(...pageHead('Co-trader', 'Candidates from backtested rules, each with its evidence tier, the odds with a ' +
    'range and sample size, the result after charges and your risk check. Most days there is no edge, and that is the answer.',
  h('div', { class: 'row' }, h('label', { class: 'small' }, toggle, ' Show RESEARCH ideas (unproven)'), refresh)));
  const body = h('div', null, skeleton(5));
  root.appendChild(body);

  const show = (d) => {
    STATE.plots.forEach((p) => p.destroy());
    STATE.plots = [];
    clear(body);
    const run = d.run;
    if (!run) {
      body.appendChild(card('No co-trader run yet', empty('Look for candidates',
        'Refresh reads the Strategy Lab\'s runs and today\'s bars. Without validated strategies the answer is: sit out.',
        h('code', null, 'python3 trader-cotrader/cotrader.py today'))));
      body.appendChild(calibrationCard(d.calibration));
      return;
    }
    STATE.research = run.view === 'research';
    toggle.checked = STATE.research;
    const all = run.candidates || [];
    const shown = all.filter((c) => STATE.research || c.tier !== 'RESEARCH');
    const ready = shown.filter((c) => c.status === 'ready');
    body.appendChild(h('div', { class: 'metrics' },
      metric({ label: 'Ready candidates', value: num(ready.length, 0), sub: `${shown.length - ready.length} blocked by your rules`,
        explain: 'Ready means trader-risk passed it; your typed yes still decides.' }),
      metric({ label: 'Market now', term: 'regime', value: (run.regime_now || {}).words ? (run.regime_now.words.split(': ')[1] || run.regime_now.words) : '–',
        sub: `as of ${run.as_of} IST`, explain: 'Each strategy is judged on its record in this kind of market.' }),
      metric({ label: 'View', term: 'evidence_tier', value: run.view === 'research' ? 'All tiers' : 'PROMOTED + WATCH',
        sub: run.hidden_research ? `${run.hidden_research} RESEARCH strategies hidden` : '' })));
    if (d.stale) body.appendChild(note(`This run is from ${run.as_of} IST, not today: refresh for today's bars.`, 'warn'));
    if (run.no_edge) body.appendChild(noEdgeCard(run.no_edge, run.view === 'research'));
    const draftable = (c) => c.status === 'ready';
    const onDraft = async (c) => {
      try {
        const r = await post('/api/cotrader/draft', { id: c.id });
        toast(r.next || 'Drafted. Nothing placed.');
        location.hash = '#/portfolio';
      } catch (e) {
        toast(e.message || String(e), 'error');
      }
    };
    for (const c of shown) body.appendChild(candidateCard(c, onDraft, draftable));
    if ((run.passed_over || []).length) {
      body.appendChild(card('Passed over', h('ul', null, run.passed_over.slice(0, 20).map((x) =>
        h('li', null, h('b', null, x.symbol), ` (${x.strategy}, ${x.tier}): ${x.why}`)))));
    }
    const sc = strategiesCard(run);
    if (sc) body.appendChild(sc);
    body.appendChild(calibrationCard(d.calibration));
    body.appendChild(h('div', { class: 'note warn' }, h('b', null, 'What this does not show. '), d.limits));
  };

  const load = async (fn) => {
    try {
      show(await fn());
    } catch (e) {
      clear(body).appendChild(failure(e.message || String(e), () => render(root, ctx)));
    }
  };
  toggle.addEventListener('change', () => {
    STATE.research = toggle.checked;
    refresh.click();
  });
  refresh.addEventListener('click', async () => {
    refresh.disabled = true;
    clear(body).appendChild(skeleton(5));
    await load(() => post('/api/cotrader/run', { tier: STATE.research ? 'research' : 'validated' }));
    refresh.disabled = false;
  });
  await load(() => get('/api/cotrader'));
}

export function destroy() {
  STATE.plots.forEach((p) => p.destroy());
  STATE.plots = [];
  STATE.off.forEach((f) => f());
  STATE.off = [];
}

