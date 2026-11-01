// Journal: trader-journal's closed trades, statistics with their ranges, rule breaks and the counterfactual
// ledger. Read only, except your own notes and tags on an entry (POST /api/journal/note).
import { get, post } from '../api.js';
import { h, clear, card, kv, table, note } from '../dom.js';
import { inr, num, signed, tone, when } from '../fmt.js';
import { why, help } from '../why.js';
import { metric, empty, skeleton, failure, pageHead, toast } from '../ui.js';
import { plot } from '../plot.js';

const STATE = { plots: [], off: [], by: 'strategy', data: null };
const redraw = () => STATE.plots.forEach((p) => p.redraw());
const share = (x) => (x === null || x === undefined ? '–' : num(Number(x) * 100, 0) + '%');
const range = (ci, f) => (ci && ci[0] !== null && ci[0] !== undefined ? `${f(ci[0])} to ${f(ci[1])}` : 'needs 2 or more trades');
const rr = (x) => (x === null || x === undefined ? '–' : (Number(x) > 0 ? '+' : '') + num(x, 2) + 'R');
const t = (iso) => when(String(iso || '').replace('T', ' '));
const GROUPS = [['strategy', 'Strategy'], ['persona', 'Persona'], ['regime', 'Regime'], ['tod', 'Time of day'],
  ['weekday', 'Weekday'], ['instrument', 'Instrument']];
const KIND_WORDS = {
  override: 'Placed despite a risk warning', 'after-breaker': 'After a circuit-breaker lock', revenge: 'Revenge trade',
  oversized: 'Oversized', 'outside-window': 'Outside the entry window', 'late-exit': 'Exit after flat-by',
  'off-ticket': 'No ticket (risk never checked it)', overtrading: 'Over the trades-a-day plan',
};

function tiles(d) {
  const s = d.stats;
  const b = d.behaviour;
  return h('div', { class: 'metrics', 'aria-label': 'Journal statistics after charges' },
    metric({ label: 'Closed trades', value: num(s.trades, 0), tone: s.small_sample ? 'warn' : null,
      sub: `${num(s.wins, 0)} won, ${num(s.losses, 0)} lost, ${num(s.scratches, 0)} flat`, explain: s.verdict }),
    metric({ label: 'Win rate', value: why(share(s.win_rate), d.why.win_rate, 'this win rate'),
      sub: `95% range ${range(s.win_rate_ci, share)} (Wilson)`,
      explain: 'The range is what the trades so far allow; it narrows only with more trades.' }),
    metric({ label: 'Expectancy a trade, after charges', term: 'expectancy', tone: tone(s.expectancy) || null,
      value: h('span', { class: tone(s.expectancy) }, why(signed(s.expectancy), d.why.expectancy, 'this expectancy')),
      sub: `95% range ${range(s.expectancy_ci, signed)}; ${s.expectancy_r === null ? 'no R on record'
        : rr(s.expectancy_r) + ' over ' + num(s.r_trades, 0) + ' trades with a max loss'}` }),
    metric({ label: 'Net P&L after charges', tone: tone(s.net) || null, value: h('span', { class: tone(s.net) }, signed(s.net)),
      sub: `Gross ${signed(s.gross)}, charges ${inr(s.charges)}`, explain: b.cost_drag.words }),
    metric({ label: 'Profit factor', value: s.profit_factor === null ? '–' : num(s.profit_factor, 2),
      sub: `Average win ${s.avg_win === null ? '–' : inr(s.avg_win)}, average loss ${s.avg_loss === null ? '–' : inr(s.avg_loss)}`,
      explain: 'Gross wins over gross losses, after charges. Under 1 loses money.' }),
    metric({ label: 'Max drawdown', value: inr(s.max_drawdown), tone: 'warn',
      sub: `Longest streaks: ${num(s.longest_win_streak, 0)} wins, ${num(s.longest_loss_streak, 0)} losses`,
      explain: 'The deepest fall of the equity curve after charges, from a high to a later low.' }));
}

function curve(d) {
  const eq = d.stats.equity || [];
  if (eq.length < 2) return empty('No equity curve yet', 'It needs two closed trades.');
  const canvas = h('canvas', { class: 'plot', tabindex: '0', role: 'img',
    'aria-label': `Equity after charges over ${eq.length} closed trades, from ${signed(eq[0].equity)} to ` +
      `${signed(eq[eq.length - 1].equity)}. Use the arrow keys to read values.` });
  const opts = {
    x: eq.map((_, i) => i), xfmt: (i) => String((eq[Math.round(i)] || {}).at || '').slice(0, 10),
    yfmt: (v) => inr(v).replace(/\.\d\d$/, ''), zero: true,
    series: [{ y: eq.map((p) => p.equity), color: '--accent', width: 2, label: 'equity', points: eq.length < 60 }],
  };
  requestAnimationFrame(() => STATE.plots.push(plot(canvas, opts)));
  return h('div', null,
    h('ul', { class: 'legend-list' }, h('li', null, h('span', { class: 'swatch accent' }), 'Cumulative net P&L after charges (₹), one point per closed trade')),
    h('div', { class: 'plot-box' }, canvas));
}

function breakdown(d, box) {
  const rows = (d.by || {})[STATE.by] || [];
  const seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Break down by' }, GROUPS.map(([k, label]) => h('button', {
    type: 'button', class: 'seg-btn', 'aria-current': k === STATE.by ? 'true' : 'false',
    onclick: () => { STATE.by = k; clear(box).appendChild(breakdown(d, box)); },
  }, label)));
  const tbl = rows.length ? table([{ label: STATE.by === 'tod' ? 'Time of day' : STATE.by[0].toUpperCase() + STATE.by.slice(1) },
    { label: 'Trades', num: true }, { label: 'Win rate (95% range)', num: true }, { label: 'Expectancy', num: true },
    { label: 'Net after charges', num: true }, { label: 'Profit factor', num: true }],
  rows.map((r) => h('tr', null, h('td', null, r.group), h('td', { class: 'num' }, num(r.trades, 0)),
    h('td', { class: 'num' }, `${share(r.win_rate)} (${range(r.win_rate_ci, share)})`),
    h('td', { class: 'num' }, h('span', { class: tone(r.expectancy) }, signed(r.expectancy))),
    h('td', { class: 'num' }, h('span', { class: tone(r.net) }, signed(r.net))),
    h('td', { class: 'num' }, r.profit_factor === null ? '–' : num(r.profit_factor, 2)))),
  { dense: true, caption: 'Results by ' + STATE.by }) : empty('Nothing to break down', 'No closed trades yet.');
  return h('div', null, h('div', { class: 'toolbar' }, seg,
    STATE.by === 'regime' ? help('regime') : null), tbl,
  h('p', { class: 'small muted' }, 'A group with a handful of trades says almost nothing: read the range, not the rate.'));
}

function behaviour(d) {
  const b = d.behaviour;
  const o = b.overtrading;
  const over = Object.entries(o.days_over_plan || {});
  return h('div', null,
    kv([
      ['Overtrading index', h('span', null, o.index === null ? '–' : num(o.index, 2),
        h('span', { class: 'muted' }, ` (${num(o.avg_per_day, 2)} entries a traded day against a plan of ${o.plan_per_day || 'no limit'}; 1.00 is the plan)`))],
      ['Days over plan', over.length ? over.map(([k, v]) => `${k} (${v})`).join(', ') : 'none'],
      ['Cost drag', b.cost_drag.words],
      ['Trades with a rule break', `${num(b.rule_break_trades, 0)}, net ${signed(b.rule_break_net)}; without: net ${signed(b.clean_net)}`],
    ]),
    b.log.length ? h('ul', { class: 'log journal-breaks' }, b.log.map((r) => h('li', null,
      h('span', { class: 'badge warn' }, KIND_WORDS[r.kind] || r.kind), ' ', h('b', null, r.symbol), ' ',
      h('span', { class: 'muted' }, t(r.at)), ': ', r.text, ' ', h('span', { class: tone(r.net) }, '(net ' + signed(r.net) + ')'))))
      : note('No rule breaks found in the records.', 'info'));
}

function ledger(d) {
  const g = d.counterfactual.groups;
  const names = ['co-trader skipped', 'co-trader blocked by risk', 'your orders blocked by risk'];
  const counts = (c) => Object.entries(c || {}).map(([k, v]) => `${k} ${v}`).join(', ') || 'none';
  return h('div', null,
    h('p', null, 'Every co-trader candidate you did not take and every risk BLOCK of your own order, followed on the bars ',
      why('with trader-lab\'s fill rules', d.why.counterfactual, 'the counterfactual'), '. Saved = what the losers would have ' +
      'lost; cost = what the winners would have made.'),
    table(['Group', { label: 'Plans', num: true }, { label: 'Replayed', num: true }, { label: 'Saved you', num: true },
      { label: 'Cost you', num: true }, { label: 'Win rate of the replays (95% range)', num: true }, 'Outcomes'],
    names.map((n) => {
      const x = g[n];
      return h('tr', null, h('td', null, n), h('td', { class: 'num' }, num(x.plans, 0)), h('td', { class: 'num' }, num(x.replayed, 0)),
        h('td', { class: 'num' }, inr(x.saved)), h('td', { class: 'num' }, inr(x.cost)),
        h('td', { class: 'num' }, x.replayed ? range(x.win_rate_ci, share) : '–'), h('td', { class: 'small' }, counts(x.counts)));
    }), { dense: true, caption: 'The counterfactual ledger' }),
    h('p', null, h('b', null, 'In all: '), g.all.words + '.'),
    h('details', null, h('summary', null, 'Caveats'), h('ul', null, d.counterfactual.caveats.map((c) => h('li', null, c)))));
}

function close(modal, back) {
  modal.remove();
  document.removeEventListener('keydown', modal._esc);
  if (back) back.focus();
}

function drawer(e, back, root, ctx) {
  const notesList = h('ul', { class: 'log' }, (e.notes || []).map((n) => h('li', null, h('span', { class: 'muted' }, t(n.at) + ': '), n.text)));
  const text = h('textarea', { id: 'jn-text', rows: '3', maxlength: '2000', class: 'yes-input' });
  const tags = h('input', { id: 'jn-tags', type: 'text', class: 'yes-input', placeholder: 'fomo, followed-plan' });
  const save = h('button', { type: 'submit', class: 'btn primary' }, 'Save note');
  const form = h('form', { class: 'yes-form', onsubmit: async (ev) => {
    ev.preventDefault();
    save.disabled = true;
    try {
      const res = await post('/api/journal/note', { id: e.id, text: text.value, tags: tags.value });
      e.notes = res.notes;
      e.tags = res.tags;
      clear(notesList);
      res.notes.forEach((n) => notesList.appendChild(h('li', null, h('span', { class: 'muted' }, t(n.at) + ': '), n.text)));
      tagLine.textContent = res.tags.length ? 'Tags: ' + res.tags.join(', ') : '';
      text.value = '';
      tags.value = '';
      toast('Note saved to trader/journal/notes.json');
    } catch (err) {
      toast(err.message || String(err), 'error');
    } finally {
      save.disabled = false;
    }
  } }, h('label', { for: 'jn-text' }, 'Your note'), text, h('label', { for: 'jn-tags' }, 'Tags (comma separated)'), tags,
  h('div', { class: 'row' }, save));
  const tagLine = h('p', { class: 'small' }, (e.tags || []).length ? 'Tags: ' + e.tags.join(', ') : '');
  const title = e.kind === 'equity' ? `${e.symbol}, ${e.side} ${num(e.qty, 0)}` : `${e.underlying} ${e.strategy} (${e.legs} legs)`;
  const closeBtn = h('button', { type: 'button', class: 'btn', onclick: () => close(modal, back) }, 'Close');
  const box = h('div', { class: 'modal-box journal-drawer', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'jd-title' },
    h('h2', { id: 'jd-title' }, title),
    kv([
      ['Entry', e.id],
      ['Opened / closed', `${t(e.opened_at)} / ${t(e.closed_at)} (${num(e.holding_minutes, 0)} min)`],
      e.kind === 'equity' ? ['Prices', `in ${num(e.entry_price)} , out ${num(e.exit_price)}`] : ['Opened for', `${e.opened_for} of ${inr(e.entry_price)}`],
      e.lots ? ['Lots', num(e.lots, 0)] : null,
      ['Gross / charges / net', h('span', null, signed(e.gross), ' / ', inr(e.charges), e.charges_estimated ? ' (estimated)' : '', ' / ',
        h('span', { class: tone(e.net) }, signed(e.net)))],
      ['R multiple', e.r_multiple === null ? 'no max loss on record' : `${rr(e.r_multiple)} on a max loss of ${inr(e.risk)}`],
      ['MAE / MFE', e.mae === null || e.mae === undefined ? 'no bars for this trade' : `${inr(e.mae)} / ${inr(e.mfe)}`],
      ['Persona / strategy', `${e.persona} / ${e.strategy}`],
      ['Regime', h('span', null, e.regime, ' ', help('regime'))],
      ['When', `${e.weekday}, ${e.tod}`],
      ['Source', `${e.source}${e.ticketed ? ', ticketed' : ', no ticket'}`],
    ]),
    e.rule_notes.length ? h('ul', { class: 'log' }, e.rule_notes.map((n) => h('li', null, h('span', { class: 'badge warn' },
      KIND_WORDS[n.kind] || n.kind), ' ', n.text))) : h('p', { class: 'small muted' }, 'No rule notes.'),
    h('h3', { class: 'sub' }, 'Fills'),
    table(['Time', 'Side', { label: 'Qty', num: true }, 'Contract', { label: 'Price', num: true }, { label: 'Charges', num: true }],
      e.fills.map((f) => h('tr', null, h('td', null, t(f.at)), h('td', null, f.side), h('td', { class: 'num' }, num(f.qty, 0)),
        h('td', null, f.symbol), h('td', { class: 'num' }, num(f.price)), h('td', { class: 'num' }, inr(f.charges || 0)))),
      { dense: true, caption: 'Fills of this trade' }),
    h('h3', { class: 'sub' }, 'Your notes'), notesList, tagLine, form, h('div', { class: 'row' }, closeBtn));
  const modal = h('div', { class: 'modal', onclick: (ev) => { if (ev.target === modal) close(modal, back); } }, box);
  modal._esc = (ev) => { if (ev.key === 'Escape') close(modal, back); };
  document.addEventListener('keydown', modal._esc);
  document.body.appendChild(modal);
  closeBtn.focus();
}

function trades(d, root, ctx) {
  const es = d.entries || [];
  if (!es.length) {
    return empty('No closed trades yet', 'Paper fills come from the paper broker. Import a broker trade book with ' +
      'journal.py import FILE --broker zerodha (or upstox, dhan, angel, groww).');
  }
  return table(['Opened', 'Trade', 'Persona / strategy', 'Regime', { label: 'Net after charges', num: true },
    { label: 'R', num: true }, 'Rule notes', ''],
  es.map((e) => {
    const btn = h('button', { type: 'button', class: 'link', onclick: (ev) => drawer(e, ev.currentTarget, root, ctx) }, 'Details');
    return h('tr', null, h('td', null, t(e.opened_at)),
      h('td', null, e.kind === 'equity' ? `${e.symbol} ${e.side} ${num(e.qty, 0)}` : `${e.underlying} ${e.strategy}`,
        h('span', { class: 'small muted' }, ' ' + e.source)),
      h('td', null, `${e.persona} / ${e.strategy}`), h('td', null, e.regime),
      h('td', { class: 'num' }, h('span', { class: tone(e.net) }, signed(e.net))), h('td', { class: 'num' }, rr(e.r_multiple)),
      h('td', null, e.rule_notes.map((n) => h('span', { class: 'badge warn' }, n.kind))), h('td', null, btn));
  }), { dense: true, caption: 'Closed trades, newest first' });
}

export async function render(root, ctx) {
  clear(root);
  root.append(...pageHead('Journal', 'Every closed trade after charges, what the numbers can and cannot say, the rules you ' +
    'broke, and what the trades you did not take would have done.'));
  const body = h('div', null, skeleton(5));
  root.appendChild(body);
  let d;
  try {
    d = await get('/api/journal');
  } catch (e) {
    clear(body).appendChild(failure(e.message || String(e), () => render(root, ctx)));
    return;
  }
  STATE.data = d;
  clear(body);
  if (!d.stats.trades) {
    body.appendChild(empty('No closed trades yet', 'Trades appear here as they close: paper fills from the paper broker, ' +
      'and your broker\'s trade book once imported (journal.py import FILE --broker NAME).',
    h('a', { class: 'btn', href: '#/portfolio' }, 'Portfolio and orders')));
  } else {
    body.appendChild(tiles(d));
    if (d.stats.small_sample) body.appendChild(note(d.stats.verdict, 'warn'));
  }
  const byBox = h('div');
  byBox.appendChild(breakdown(d, byBox));
  body.appendChild(h('div', { class: 'cols' },
    h('div', { class: 'stack' }, card('Equity curve after charges', curve(d)), card('Breakdown', byBox)),
    h('div', { class: 'stack' }, card('Rule breaks and costs', behaviour(d)), card('Counterfactual ledger', ledger(d)))));
  body.appendChild(card('Closed trades', trades(d, root, ctx),
    (d.open || []).length ? h('p', { class: 'small muted' }, 'Still open: ' + d.open.map((o) => o.group).join(', ')) : null));
  body.appendChild(h('p', { class: 'small muted' }, `As of ${t(d.as_of)}. Sources: ${num(d.sources.paper_fills, 0)} paper fills, ` +
    `${num(d.sources.imported_fills, 0)} imported, ${num(d.sources.risk_checks, 0)} risk checks; regime from ${d.sources.regime}. ` +
    d.disclaimer));
  window.addEventListener('resize', redraw);
  STATE.off.push(() => window.removeEventListener('resize', redraw));
}

export function destroy() {
  STATE.off.forEach((f) => f());
  STATE.off = [];
  STATE.plots = [];
  document.querySelectorAll('.modal .journal-drawer').forEach((b) => b.parentNode.remove());
}
