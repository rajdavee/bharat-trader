// Tax: trader-tax's estimate for one financial year, for discussion with a CA. Read only, except the export
// (POST /api/tax/export writes report.md and CSVs to trader/tax/FY<year>/).
import { get, post } from '../api.js';
import { h, clear, card, kv, table, note } from '../dom.js';
import { inr, num, signed, tone, when } from '../fmt.js';
import { why } from '../why.js';
import { metric, empty, skeleton, failure, pageHead, toast } from '../ui.js';

const STATE = { fy: null, book: 'live' };
const KIND = {
  speculative: 'Speculative business loss', non_speculative: 'Non-speculative business loss',
  stcl: 'Short-term capital loss', ltcl: 'Long-term capital loss',
};
const BOOKS = [['live', 'Live (broker files)'], ['paper', 'Paper (practice)'], ['all', 'Both']];
const pct = (x) => `${num(x, Number(x) % 1 ? 1 : 0)}%`;

function banner(d) {
  return h('div', { class: 'note warn', role: 'note' }, h('b', null, 'Estimate only. '), d.disclaimer);
}

function picker(d, root, ctx) {
  const fy = h('select', { id: 'tax-fy', name: 'fy' }, d.years.map((y) => h('option', { value: y, selected: y === d.fy }, 'FY ' + y)));
  const book = h('select', { id: 'tax-book', name: 'book' }, BOOKS.map(([v, l]) => h('option', { value: v, selected: v === d.book }, l)));
  const go = () => { STATE.fy = fy.value; STATE.book = book.value; render(root, ctx); };
  fy.addEventListener('change', go);
  book.addEventListener('change', go);
  const btn = h('button', { type: 'button', class: 'btn' }, 'Export CSV and markdown');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const res = await post('/api/tax/export', { fy: d.fy, book: d.book });
      toast(`Wrote ${res.files.join(', ')} to ${res.folder}`);
    } catch (e) {
      toast(e.message || String(e), 'error');
    } finally {
      btn.disabled = false;
    }
  });
  return h('div', { class: 'toolbar' }, h('label', { for: 'tax-fy' }, 'Financial year'), fy,
    h('label', { for: 'tax-book' }, 'Trades'), book, btn);
}

function tiles(d) {
  const hd = d.heads;
  const t = d.turnover;
  return h('div', { class: 'metrics', 'aria-label': `Income per head, FY ${d.fy}` },
    metric({ label: 'Speculative business (intraday)', tone: tone(hd.speculative.income) || null,
      value: h('span', { class: tone(hd.speculative.income) }, signed(hd.speculative.income)),
      sub: `${num(hd.speculative.trades, 0)} trades; charges ${inr(hd.speculative.charges)}, expenses ${inr(hd.speculative.expenses)}`,
      explain: 'Shares bought and sold the same day, settled without delivery. A loss here only offsets speculative income.' }),
    metric({ label: 'Non-speculative business (F&O)', tone: tone(hd.non_speculative.income) || null,
      value: h('span', { class: tone(hd.non_speculative.income) }, signed(hd.non_speculative.income)),
      sub: `Futures ${signed(hd.non_speculative.futures.net)}, options ${signed(hd.non_speculative.options.net)}, after charges`,
      explain: 'Exchange-traded futures and options. STT and every other charge are business expenses.' }),
    metric({ label: 'Short-term capital gains', tone: tone(hd.stcg.net) || null,
      value: h('span', { class: tone(hd.stcg.net) }, signed(hd.stcg.net)),
      sub: `${num(hd.stcg.trades, 0)} sales held 12 months or less; STT ${inr(hd.stcg.stt_not_deductible)} not deductible` }),
    metric({ label: 'Long-term capital gains', tone: tone(hd.ltcg.net) || null,
      value: h('span', { class: tone(hd.ltcg.net) }, why(signed(hd.ltcg.net), d.why.ltcg, 'the long-term gains')),
      sub: `${num(hd.ltcg.trades, 0)} sales; ${num(hd.ltcg.grandfathered, 0)} grandfathered (bought before 1 Feb 2018)` }),
    metric({ label: 'Business turnover (ICAI)', value: why(inr(t.total), d.why.turnover, 'the turnover'),
      sub: `Intraday ${inr(t.intraday)}, futures ${inr(t.futures)}, options ${inr(t.options)}`,
      explain: 'The absolute sum of each squared-off trade\'s profit or loss: a loss adds to it like a profit.' }));
}

function audit(d) {
  const a = d.audit;
  const kind = a.status === 'audit required' ? 'error' : a.status === 'audit likely' ? 'warn' : 'info';
  return h('div', null,
    note(`${a.status[0].toUpperCase()}${a.status.slice(1)}: turnover ${inr(a.turnover)} against a limit of ${inr(a.limit)}.`, kind),
    h('ul', null, a.words.map((w) => h('li', null, w))),
    h('p', { class: 'small muted' }, d.turnover.premium_words),
    h('details', null, h('summary', null, 'How turnover is computed'), h('p', { class: 'small' }, d.turnover.method),
      h('p', { class: 'small muted' }, d.turnover.source)));
}

function setoff(d) {
  const s = d.setoff;
  const rows = s.carry_forward.map((c) => h('tr', null, h('td', null, KIND[c.kind] || c.kind), h('td', null, 'FY ' + c.fy),
    h('td', { class: 'num' }, inr(c.amount)), h('td', null, 'FY ' + c.usable_until)));
  return h('div', null,
    s.steps.length ? h('ol', { class: 'log' }, s.steps.map((w) => h('li', null, w))) : h('p', { class: 'muted' }, 'Nothing to set off this year.'),
    rows.length ? table(['Loss', 'From', { label: 'Amount', num: true }, 'Usable until'], rows,
      { dense: true, caption: 'Losses that carry forward' }) : h('p', { class: 'muted' }, 'No loss to carry forward.'),
    s.lapsed.length ? h('ul', { class: 'small' }, s.lapsed.map((x) => h('li', null,
      h('span', { class: 'badge warn' }, 'lapsed'), ` ${KIND[x.kind] || x.kind} of FY ${x.fy}, ${inr(x.amount)}: ${x.why}`))) : null,
    h('p', { class: 'small muted' }, 'A loss carries forward only if the return is filed by its due date.'));
}

function taxable(d) {
  const t = d.taxable;
  const rows = d.special_rate.map((x) => h('tr', null, h('td', null, x.what), h('td', null, x.section),
    h('td', { class: 'num' }, inr(x.amount)), h('td', { class: 'num' }, pct(x.rate_pct)), h('td', { class: 'num' }, inr(x.at_rate))));
  return h('div', null,
    kv([
      ['Speculative business income', `${inr(t.speculative)} (at your slab rate)`],
      ['Non-speculative business income', `${inr(t.non_speculative)} (at your slab rate)`],
      ['Long-term gains', `${inr(t.ltcg_total)}, of which ${inr(t.ltcg_exempt)} exempt`],
    ]),
    rows.length ? table(['Special rate', 'Section', { label: 'Taxable', num: true }, { label: 'Rate', num: true },
      { label: 'At that rate', num: true }], rows, { dense: true, caption: 'Special-rate amounts' }) : null,
    h('p', { class: 'small muted' }, d.special_rate_note));
}

function advance(d) {
  const rows = d.advance_tax.dates.map((x) => h('tr', null, h('td', null, x.due), h('td', { class: 'num' }, pct(x.cumulative_pct)),
    h('td', { class: 'num' }, signed(x.business_net_to_date)), h('td', { class: 'num' }, signed(x.capital_gains_to_date)),
    h('td', null, h('span', { class: 'badge' + (x.status === 'past' ? '' : ' warn') }, x.status))));
  return h('div', null, table(['Due', { label: 'Cumulative', num: true }, { label: 'Business net to date', num: true },
    { label: 'Capital gains to date', num: true }, ''], rows, { dense: true, caption: 'Advance-tax dates' }),
  h('ul', { class: 'small' }, d.advance_tax.words.map((w) => h('li', null, w))));
}

function itr(d) {
  return h('div', null, h('p', { class: 'small muted' }, d.itr_form),
    kv(d.itr.map((x) => [x.what.replace('_', ' '), x.where])));
}

function rules(d) {
  return h('details', null, h('summary', null, `${d.rules.length} rules used, each with its section, source and date`),
    table(['What', 'Rule', 'Section', 'Source', 'From'], d.rules.map((r) => h('tr', null, h('td', null, r.what), h('td', null, r.rule),
      h('td', null, r.section), h('td', { class: 'small' }, r.source), h('td', null, r.effective))), { dense: true, caption: 'Rules used' }));
}

export async function render(root, ctx) {
  clear(root);
  root.append(...pageHead('Tax', 'A financial-year estimate of your trading by income head, the turnover and audit check, ' +
    'set-off and carry-forward, for discussion with your chartered accountant.'));
  const body = h('div', null, skeleton(5));
  root.appendChild(body);
  let d;
  const q = new URLSearchParams();
  if (STATE.fy) q.set('fy', STATE.fy);
  q.set('book', STATE.book);
  try {
    d = await get('/api/tax?' + q.toString());
  } catch (e) {
    clear(body).appendChild(failure(e.message || String(e), () => render(root, ctx)));
    return;
  }
  clear(body);
  body.appendChild(banner(d));
  body.appendChild(picker(d, root, ctx));
  d.notes.forEach((n) => body.appendChild(note(n, n.startsWith('PRACTICE') ? 'warn' : 'info')));
  if (!d.sources.fills) {
    body.appendChild(empty(`No ${d.book} trades yet`, 'Import your broker\'s tradebook (tax.py import FILE --broker zerodha, ' +
      'or upstox, dhan, angel, groww), or pick Paper for a practice report.'));
  }
  body.appendChild(tiles(d));
  if (d.needs_input.length) {
    body.appendChild(card('Needs your input', h('ul', null, d.needs_input.map((x) => h('li', null, x)))));
  }
  body.appendChild(h('div', { class: 'cols' },
    h('div', { class: 'stack' }, card('Turnover and the audit check', audit(d)), card('Taxable amounts', taxable(d))),
    h('div', { class: 'stack' }, card('Set-off and carry-forward', setoff(d)), card('Advance tax', advance(d)))));
  body.appendChild(card(`Where each number goes (${d.act})`, itr(d), rules(d)));
  body.appendChild(h('p', { class: 'small muted' }, `FY ${d.fy}, ${d.period.from} to ${d.period.to}${d.complete ? '' : ' (not over yet)'}. ` +
    `As of ${when(d.as_of.replace('T', ' '))}. ${num(d.sources.fills, 0)} fills, ${num(d.trade_count, 0)} taxable pieces; ` +
    `trader/tax/: ${num(d.sources.holdings, 0)} holdings, ${num(d.sources.carry_forward, 0)} carried losses, ${num(d.sources.expenses, 0)} expenses. ` +
    d.disclaimer));
}

export function destroy() {}
