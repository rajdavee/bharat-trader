// Strategy builder (trader-strategy): template picker, legs editor, payoff gross vs after charges, today vs
// expiry, Greeks, margin with its method and error, PoP textbook vs simulated, and a paper PROPOSAL that
// still goes through trader-risk and a typed yes per leg. Nothing here places an order.
import { get, post } from '../api.js';
import { h, clear, card, kv, note, table, loading } from '../dom.js';
import { num, inr, int, tone, when } from '../fmt.js';
import { why } from '../why.js';
import { plot } from '../plot.js';

const STATE = { plots: [], cleanup: [] };
const PARAMS = {
  strike: 'Centre / long strike (default ATM)', wings: 'Wings: points from ATM to the shorts', width: 'Width: points to the wing',
  delta: 'Short strikes by delta (0.16)', skip: 'Skip: extra points on one wing', ratio: 'Ratio (shorts per long)', lots: 'Lots per leg',
};
const USES = {
  'bull-call-spread': ['strike', 'width', 'lots'], 'bear-call-spread': ['strike', 'width', 'lots'],
  'bull-put-spread': ['strike', 'width', 'lots'], 'bear-put-spread': ['strike', 'width', 'lots'],
  'long-straddle': ['strike', 'lots'], 'short-straddle': ['strike', 'lots'],
  'long-strangle': ['wings', 'delta', 'lots'], 'short-strangle': ['wings', 'delta', 'lots'],
  'iron-condor': ['wings', 'width', 'delta', 'lots'], 'iron-fly': ['strike', 'width', 'lots'],
  'call-butterfly': ['strike', 'width', 'skip', 'lots'], 'put-butterfly': ['strike', 'width', 'skip', 'lots'],
  'call-ratio-spread': ['strike', 'width', 'ratio', 'lots'], 'put-ratio-spread': ['strike', 'width', 'ratio', 'lots'],
  'covered-call': ['wings', 'delta', 'lots'], 'protective-put': ['wings', 'delta', 'lots'], calendar: ['strike', 'lots'],
};

function maxText(v, unlimited) {
  if (unlimited) return 'unlimited';
  return v === null || v === undefined ? '–' : inr(v);
}

function addDays(iso, n) {
  const d = new Date(iso.slice(0, 10) + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

function field(label, input) {
  return h('label', { class: 'fld' }, h('span', { class: 'fld-label' }, label), input);
}

function legsText(rows) {
  return rows.map((r) => {
    const s = (r.side === 'SELL' ? '-' : '+') + r.lots + ' ' + r.type;
    return r.type === 'FUT' ? s + ' @' + r.expiry : `${s} ${r.strike} @${r.expiry}`;
  }).join(', ');
}

function tile(name, value, sub, cls) {
  return h('div', { class: 'tile' + (cls ? ' ' + cls : '') }, h('div', { class: 'tile-name' }, name),
    h('div', { class: 'tile-price' }, value), sub ? h('div', { class: 'tile-time muted' }, sub) : null);
}

function results(d) {
  const e = d.expiry;
  const m = d.margin;
  const pr = d.probability;
  const t = pr.textbook;
  const s = pr.simulated;
  const w = d.why;
  const prem = d.net_premium;
  return h('div', { class: 'tiles' },
    tile('Net premium', why(inr(Math.abs(prem.amount)), w.net_premium, 'the net premium'),
      prem.kind === 'credit' ? 'received (credit)' : prem.kind === 'debit' ? 'paid (debit)' : '',
      prem.kind === 'credit' ? 'up' : ''),
    tile('Max profit at expiry', why(maxText(e.max_profit, e.unlimited_profit), w.max_profit, 'max profit'),
      'after charges ' + maxText(e.max_profit_after_charges, e.unlimited_profit)),
    tile('Max loss at expiry', why(maxText(e.max_loss, e.unlimited_loss), w.max_loss, 'max loss'),
      'after charges ' + maxText(e.max_loss_after_charges, e.unlimited_loss) + (e.max_loss_at === 0 ? ' · at an underlying of 0' : '') +
      (e.exact ? '' : ' · approximate (two expiries)')),
    tile('Breakevens', why(e.breakevens.map((b) => num(b)).join(' / ') || 'none', w.breakevens, 'the breakevens'),
      'after charges ' + (e.breakevens_after_charges.map((b) => num(b)).join(' / ') || 'none')),
    tile(m.method === 'premium' ? 'Margin required' : `Margin required (estimate, ±${m.error_pct}%)`,
      why((m.method === 'premium' ? '' : '≈ ') + inr(m.required), w.margin, 'the margin required'),
      m.method === 'premium' ? 'premium paid up front (exact)' : `${inr(m.low)} – ${inr(m.high)} · what a broker would block`),
    tile('Return on margin', m.return_on_margin === null ? '–' : num(m.return_on_margin) + '%',
      'max profit at expiry / margin required'),
    tile('PoP textbook', why(num(t.pop) + '%', w.pop_textbook, 'the textbook probability'),
      `EV ${inr(t.ev)} · 5% tail ${inr(t.tail_5pct)}`),
    tile('PoP simulated', why(s ? num(s.pop) + '%' : '–', w.pop_simulated, 'the simulated probability'),
      s ? `EV ${inr(s.ev)} · 5% tail ${inr(s.tail_5pct)}` : (pr.simulated_skipped || 'not run')),
    tile('Net Greeks', why(`Δ ${num(d.greeks.delta)}`, w.greeks, 'the Greeks'),
      `${num(d.greeks.delta_lots, 3)} lots · Θ ${inr(d.greeks.theta)}/day · vega ${inr(d.greeks.vega)}`));
}

function payoffCard(d) {
  const c = d.curve;
  const atExpiry = c.horizon === 'expiry';
  const series = [
    { y: c.expiry, color: '--accent', width: 2.2, label: 'expiry' },
    { y: c.expiry_net, color: '--warn', width: 1.8, dash: [6, 4], label: 'expiry, after charges' },
  ];
  if (!atExpiry) {
    series.push({ y: c.at, color: '--ema2', width: 1.8, label: c.horizon === 'now' ? 'now' : c.horizon.replace('T', ' ') });
    series.push({ y: c.at_net, color: '--ema2', width: 1.2, dash: [3, 3], label: 'after charges' });
  }
  const vlines = [{ x: d.spot, label: 'spot ' + num(d.spot), color: '--muted', dash: [2, 3] },
    { x: d.forward, label: 'forward ' + num(d.forward), color: '--text', dash: [4, 4] }];
  for (const b of d.expiry.breakevens_after_charges) vlines.push({ x: b, label: 'BE ' + num(b), color: '--warn', dash: [1, 3] });
  const em = c.expected_move || {};
  const canvas = h('canvas', { class: 'plot tall', role: 'img', tabindex: '0',
    'aria-label': `Payoff of ${d.name} on ${d.underlying}: P&L in rupees against the underlying at expiry, gross and after charges` });
  const legend = h('ul', { class: 'legend-list' }, series.map((s) => h('li', null,
    h('span', { class: 'swatch ' + s.color.slice(2) + (s.dash ? ' dashed' : '') }), s.label)),
  h('li', null, h('span', { class: 'swatch band' }), '1-SD expected move'));
  const el = card('Payoff (₹ for the whole position)', legend, h('div', { class: 'plot-box tall' }, canvas),
    h('p', { class: 'small muted' }, 'Hover or use the arrow keys for values. ' + (atExpiry ? 'Pick a date below the chart for a "today" curve. ' : '') +
      'The after-charges line takes off every leg\'s round trip at the 2026 rates, with STT on exercise for longs that end in the money.'));
  const opts = { x: c.x, series, vlines, band: { lo: em.low, hi: em.high }, zero: true, fill: 1, xlabel: d.underlying,
    yfmt: (v) => inr(v) };
  return { el, canvas, opts };
}

function legsTable(d) {
  return table([{ label: 'Leg' }, { label: 'Price', num: true }, { label: 'Source' }, { label: 'IV', num: true },
    { label: 'Δ', num: true }, { label: 'Θ/day', num: true }, { label: 'Qty', num: true }],
  d.legs.map((l) => h('tr', null,
    h('td', null, h('span', { class: 'badge ' + (l.side === 'BUY' ? 'pass' : 'block') }, l.side), ` ${l.lots} × `,
      l.instrument === 'FUT' ? 'FUT' : `${num(l.strike, 0)} ${l.option_type}`, h('span', { class: 'muted small' }, ' ' + l.expiry)),
    h('td', { class: 'num' }, num(l.price)), h('td', { class: 'muted small' }, l.price_source + (l.iv_source && l.iv_source !== 'chain' ? ' · IV ' + l.iv_source : '')),
    h('td', { class: 'num' }, l.iv === null ? '–' : num(l.iv)),
    h('td', { class: 'num' }, l.greeks.delta === null ? '–' : num(l.greeks.delta, 2)),
    h('td', { class: 'num' }, l.greeks.theta === null ? '–' : num(l.greeks.theta, 2)),
    h('td', { class: 'num' }, int(l.qty)))), { dense: true, caption: 'Priced legs' });
}

function probabilityCard(d) {
  const t = d.probability.textbook;
  const s = d.probability.simulated;
  const row = (k, a, b, f) => h('tr', null, h('th', { scope: 'row' }, k), h('td', { class: 'num' }, f(a)), h('td', { class: 'num' }, s ? f(b) : '–'));
  const pct = (v) => (v === null || v === undefined ? '–' : num(v) + '%');
  const tb = h('div', { class: 'table-wrap' }, h('table', { class: 'tbl dense' },
    h('thead', null, h('tr', null, h('th', null, `At ${d.probability.horizon}`), h('th', { class: 'num' }, 'Textbook'), h('th', { class: 'num' }, 'Simulated'))),
    h('tbody', null,
      row('PoP after charges', t.pop, s && s.pop, pct), row('PoP gross', t.pop_gross, s && s.pop_gross, pct),
      row('Expected value', t.ev, s && s.ev, inr), row('Median', t.median, s && s.median, inr),
      row('5% tail (5th percentile)', t.tail_5pct, s && s.tail_5pct, inr), row('Worst 5% average', t.tail_5pct_avg, s && s.tail_5pct_avg, inr),
      row('Volatility', t.sigma, s && s.realised_vol, pct))));
  const parts = [tb, h('p', { class: 'small muted' }, 'Textbook: ' + t.method + '.'),
    s ? h('p', { class: 'small muted' }, 'Simulated: ' + s.method + '.') : note(d.probability.simulated_skipped || 'No simulation.', 'info')];
  const sc = d.probability.scenarios || [];
  if (sc.length) {
    parts.push(h('h3', { class: 'sub' }, 'IV-shift scenarios (returns rescaled to that vol, legs alive valued at it)'));
    parts.push(table(['IV shift', { label: 'Vol', num: true }, { label: 'PoP', num: true }, { label: 'EV', num: true },
      { label: '5% tail', num: true }], sc.map((x) => h('tr', null, h('td', null, (x.iv_shift > 0 ? '+' : '') + num(x.iv_shift, 0) + ' pts'),
      h('td', { class: 'num' }, num(x.vol) + '%'), h('td', { class: 'num' }, num(x.pop) + '%'),
      h('td', { class: 'num ' + tone(x.ev) }, inr(x.ev)), h('td', { class: 'num' }, inr(x.tail_5pct)))), { dense: true }));
  }
  return card('Probability of profit: textbook vs simulated', ...parts);
}

function marginCard(d) {
  const m = d.margin;
  const sp = m.span_style;
  return card('Margin', kv([
    ['Method', m.method === 'premium' ? 'bought options only: the premium paid up front' : `SPAN-style estimate, ±${m.error_pct}%`],
    [m.label, why(inr(m.required), d.why.margin, 'the margin required')],
    ['Range', `${inr(m.low)} – ${inr(m.high)}`],
    ['Max loss at expiry (not the margin)', m.max_loss_unlimited ? 'unlimited' : inr(m.max_loss)],
    ['Return on margin', m.return_on_margin === null ? '–' : num(m.return_on_margin) + '% (max profit / margin required)'],
    ['SPAN-style detail', `scan risk ${inr(sp.scan_risk)} + exposure ${inr(sp.exposure)}` +
      (sp.expiry_day_elm ? ` + expiry-day ${inr(sp.expiry_day_elm)}` : '') + ` + premium paid ${inr(sp.premium_paid)} = ${inr(sp.total)} (±${sp.error_pct}%)`],
    ['Assumptions', `price scan ${num(sp.assumptions.price_scan)} (${num(sp.assumptions.scan_pct, 1)}%), vol scan ${num(sp.assumptions.vol_scan_points, 0)} pts, ELM ${num(sp.assumptions.elm_pct, 1)}%`],
    ['Broker basket', m.broker_basket === null ? 'not connected' : inr(m.broker_basket)],
  ]), h('p', { class: 'small muted' }, m.explain), h('p', { class: 'small muted' }, m.broker_note));
}

function chargesCard(d) {
  const ch = d.charges;
  return card('Charges', kv([
    ['Held to expiry at the forward', why(inr(ch.at_forward_expiry), d.why.charges, 'the charges')],
    ['Per leg', ch.at_forward_expiry_detail.map((x) => `${x.leg}: ${inr(x.total)}`).join(' · ')],
    ['Breakeven shift', d.expiry.breakeven_shift.map((x) => (x.shift > 0 ? '+' : '') + num(x.shift)).join(' / ') || '–'],
    ['Crossing the spread', inr(d.spread_cost)],
    ['Rates (from the dated table)', `STT ${ch.rates_from.stt_sell_pct}% sold, ${ch.rates_from.stt_exercise_pct}% on exercise · ` +
      `₹${ch.rates_from.brokerage_flat} an order · NSE ${ch.rates_from.exchange_txn_pct}% · stamp ${ch.rates_from.stamp_buy_pct}% · GST ${ch.rates_from.gst_pct}%`],
  ]), h('p', { class: 'small muted' }, ch.note));
}

export async function render(root, ctx) {
  const p = ctx.params || {};
  root.appendChild(h('h1', { class: 'page-title' }, 'Strategy builder', h('span', { class: 'badge' }, 'trader-strategy'),
    h('span', { class: 'badge paper' }, 'paper')));
  const meta = await get('/api/strategy');
  const st = {
    symbol: (p.symbol || meta.default || 'NIFTY').toUpperCase(), expiry: p.expiry || '', far: '', mode: p.legs ? 'custom' : 'template',
    template: p.template || 'iron-condor', params: {}, legs: p.legs || '', fill: 'mid', days: null, iv: 0, simulate: true,
  };
  const form = h('form', { class: 'strategy-form card', autocomplete: 'off' });
  const out = h('div', { class: 'strategy-out' });
  root.append(form, out);
  let chain = null;

  const symIn = h('input', { name: 'symbol', value: st.symbol, class: 'sym-input', list: 'stg-syms', spellcheck: 'false' });
  const symList = h('datalist', { id: 'stg-syms' }, meta.symbols.map((s) => h('option', { value: s })));
  const expSel = h('select', { name: 'expiry' });
  const farSel = h('select', { name: 'far_expiry' });
  const modeSel = h('select', { name: 'mode' }, h('option', { value: 'template' }, 'Template'), h('option', { value: 'custom' }, 'Custom legs'));
  modeSel.value = st.mode;
  const tplSel = h('select', { name: 'template' }, meta.templates.map((t) => h('option', { value: t.name, title: t.about }, `${t.name} (${t.kind})`)));
  tplSel.value = st.template;
  const tplAbout = h('p', { class: 'small muted' });
  const paramBox = h('div', { class: 'params' });
  const inputs = {};
  for (const [k, label] of Object.entries(PARAMS)) {
    inputs[k] = h('input', { name: k, type: 'number', step: k === 'delta' ? '0.01' : 'any', min: '0', placeholder: 'auto', class: 'num-in' });
    paramBox.appendChild(field(label, inputs[k]));
  }
  const typeSel = h('select', { name: 'type' }, h('option', { value: 'CE' }, 'CE'), h('option', { value: 'PE' }, 'PE'));
  const typeField = field('Calendar type', typeSel);
  paramBox.appendChild(typeField);
  const legsIn = h('textarea', { name: 'legs', rows: '2', placeholder: '-1 CE ATM+200, +1 CE ATM+300, -1 PE ATM-200, +1 PE ATM-300' });
  legsIn.value = st.legs;
  const legsField = h('div', { class: 'legs-text' }, field('Legs', legsIn), h('p', { class: 'small muted' }, meta.legs_help));
  const fillSel = h('select', { name: 'fill' }, h('option', { value: 'mid' }, 'mid (chain price)'), h('option', { value: 'touch' }, 'touch (ask to buy, bid to sell)'));
  const daysIn = h('input', { type: 'range', name: 'days', min: '0', max: '0', step: '1', value: '0' });
  const daysOut = h('output', { class: 'n' }, 'expiry');
  const ivIn = h('input', { type: 'range', name: 'iv', min: '-10', max: '10', step: '0.5', value: '0' });
  const ivOut = h('output', { class: 'n' }, '+0.0');
  const simBox = h('input', { type: 'checkbox', name: 'simulate', checked: true });
  const go = h('button', { type: 'submit', class: 'btn primary' }, 'Price it');
  form.append(
    h('div', { class: 'form-row' }, field('Underlying', symIn), symList, field('Expiry', expSel), field('Build from', modeSel),
      field('Template', tplSel), field('Far expiry (calendar)', farSel)),
    tplAbout, paramBox, legsField,
    h('div', { class: 'form-row' }, field('Fill', fillSel),
      field('Curve date', h('span', { class: 'range' }, daysIn, daysOut)), field('IV shift (vol pts)', h('span', { class: 'range' }, ivIn, ivOut)),
      h('label', { class: 'chk' }, simBox, 'Simulate (bootstrap)'), go));

  function syncForm() {
    const custom = modeSel.value === 'custom';
    tplSel.disabled = custom;
    paramBox.hidden = custom;
    legsField.hidden = !custom;
    const uses = USES[tplSel.value] || [];
    for (const [k, el] of Object.entries(inputs)) el.closest('.fld').hidden = !uses.includes(k);
    typeField.hidden = tplSel.value !== 'calendar';
    farSel.closest('.fld').hidden = custom || tplSel.value !== 'calendar';
    const t = meta.templates.find((x) => x.name === tplSel.value);
    tplAbout.textContent = custom ? '' : (t ? t.about : '');
  }
  modeSel.addEventListener('change', syncForm);
  tplSel.addEventListener('change', syncForm);
  syncForm();

  async function loadChain() {
    chain = null;
    clear(expSel);
    clear(farSel);
    try {
      chain = await get(`/api/options?symbol=${encodeURIComponent(symIn.value.trim().toUpperCase())}&strikes=0` +
        (st.expiry ? '&expiry=' + encodeURIComponent(st.expiry) : ''));
    } catch (e) {
      clear(out).appendChild(note(e.message, 'error'));
      return false;
    }
    for (const e of chain.expiry_list) {
      const label = `${e.expiry_nse} · ${e.days}d${e.available ? '' : ' (not saved offline)'}`;
      expSel.appendChild(h('option', { value: e.expiry_nse, selected: e.expiry === chain.expiry ? true : null, disabled: e.available ? null : true }, label));
      if (e.expiry > chain.expiry) farSel.appendChild(h('option', { value: e.expiry_nse, disabled: e.available ? null : true }, label));
    }
    const firstFar = Array.from(farSel.options).find((o) => !o.disabled);
    if (firstFar) farSel.value = firstFar.value;
    daysIn.max = String(Math.max(0, chain.days_to_expiry));
    daysIn.value = daysIn.max;
    daysOut.textContent = 'expiry';
    return true;
  }

  function horizon() {
    const n = Number(daysIn.value);
    if (!chain || n >= Number(daysIn.max)) return 'expiry';
    if (n <= 0) return 'today';
    return addDays(chain.asof, n);
  }
  daysIn.addEventListener('input', () => {
    const at = horizon();
    daysOut.textContent = at === 'expiry' ? 'expiry' : at === 'today' ? 'now' : at;
  });
  ivIn.addEventListener('input', () => { ivOut.textContent = (Number(ivIn.value) >= 0 ? '+' : '') + Number(ivIn.value).toFixed(1); });

  function request(legsOverride) {
    const body = { symbol: symIn.value.trim().toUpperCase(), expiry: expSel.value || undefined, fill: fillSel.value,
      at: horizon(), iv_shift: Number(ivIn.value), simulate: simBox.checked };
    if (legsOverride || modeSel.value === 'custom') {
      body.legs = legsOverride || legsIn.value.trim();
    } else {
      body.template = tplSel.value;
      body.params = {};
      for (const k of USES[tplSel.value] || []) if (inputs[k].value !== '') body.params[k] = Number(inputs[k].value);
      if (tplSel.value === 'calendar') { body.params.type = typeSel.value; body.far_expiry = farSel.value; }
    }
    return body;
  }

  function editor(d) {
    const strikes = (chain ? chain.rows : []).map((r) => r.strike);
    const expiries = (chain ? chain.expiry_list : []).filter((e) => e.available);
    const rows = d.legs.map((l) => ({ side: l.side, lots: l.lots, type: l.instrument === 'FUT' ? 'FUT' : l.option_type,
      strike: l.strike, expiry: (expiries.find((e) => e.expiry === l.expiry) || {}).expiry_nse || l.expiry }));
    const body = h('tbody');
    const paint = () => {
      clear(body);
      rows.forEach((r, i) => {
        const side = h('select', { 'aria-label': 'Side of leg ' + (i + 1) }, ['BUY', 'SELL'].map((s) => h('option', { value: s, selected: r.side === s ? true : null }, s)));
        const lots = h('input', { type: 'number', min: '1', max: '100', step: '1', value: String(r.lots), class: 'num-in', 'aria-label': 'Lots of leg ' + (i + 1) });
        const type = h('select', { 'aria-label': 'Type of leg ' + (i + 1) }, ['CE', 'PE', 'FUT'].map((s) => h('option', { value: s, selected: r.type === s ? true : null }, s)));
        const strike = h('select', { 'aria-label': 'Strike of leg ' + (i + 1), disabled: r.type === 'FUT' ? true : null },
          strikes.map((k) => h('option', { value: String(k), selected: k === r.strike ? true : null }, num(k, k % 1 ? 2 : 0))));
        const exp = h('select', { 'aria-label': 'Expiry of leg ' + (i + 1) }, expiries.map((e) => h('option', { value: e.expiry_nse, selected: e.expiry_nse === r.expiry ? true : null }, e.expiry_nse)));
        const del = h('button', { type: 'button', class: 'btn', 'aria-label': 'Remove leg ' + (i + 1) }, '×');
        side.addEventListener('change', () => { r.side = side.value; });
        lots.addEventListener('change', () => { r.lots = Math.max(1, parseInt(lots.value, 10) || 1); });
        type.addEventListener('change', () => { r.type = type.value; strike.disabled = r.type === 'FUT'; });
        strike.addEventListener('change', () => { r.strike = Number(strike.value); });
        exp.addEventListener('change', () => { r.expiry = exp.value; });
        del.addEventListener('click', () => { rows.splice(i, 1); paint(); });
        body.appendChild(h('tr', null, h('td', null, side), h('td', null, lots), h('td', null, type), h('td', null, strike), h('td', null, exp), h('td', null, del)));
      });
    };
    paint();
    const add = h('button', { type: 'button', class: 'btn' }, '+ Add leg');
    add.addEventListener('click', () => {
      rows.push({ side: 'BUY', lots: 1, type: 'CE', strike: chain ? chain.atm : rows[0] && rows[0].strike, expiry: expiries[0] ? expiries[0].expiry_nse : '' });
      paint();
    });
    const re = h('button', { type: 'button', class: 'btn primary' }, 'Reprice these legs');
    re.addEventListener('click', () => {
      if (!rows.length) return;
      const text = legsText(rows);
      modeSel.value = 'custom';
      legsIn.value = text;
      syncForm();
      price(text);
    });
    return card('Legs editor', h('div', { class: 'table-wrap' }, h('table', { class: 'tbl dense legs-edit' },
      h('caption', { class: 'sr-only' }, 'Edit the legs, then reprice'),
      h('thead', null, h('tr', null, ['Side', 'Lots', 'Type', 'Strike', 'Expiry', ''].map((x) => h('th', { scope: 'col' }, x)))), body)),
    h('div', { class: 'row' }, add, re), h('p', { class: 'small muted' }, 'Quantities are in lots (× ' + d.lot_size + ' units).'));
  }

  function paper(d) {
    const prod = h('select', { name: 'product' }, h('option', { value: 'NRML' }, 'NRML (carry forward)'), h('option', { value: 'MIS' }, 'MIS (intraday)'));
    const rep = h('input', { type: 'checkbox', name: 'replace' });
    const btn = h('button', { type: 'button', class: 'btn' }, 'Write paper proposal');
    const res = h('div');
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      clear(res);
      try {
        const r = await post('/api/strategy/draft', Object.assign(request(d.name === 'custom' ? legsIn.value.trim() : null),
          { product: prod.value, replace: rep.checked }));
        res.appendChild(note(r.next, 'ok'));
        res.appendChild(h('ul', { class: 'small' }, r.orders.map((o) => h('li', null,
          `${o.side} ${o.lots} lot (${o.qty}) ${o.symbol} ${o.instrument} ${o.strike ? num(o.strike, 0) + ' ' + o.option_type : ''} ${o.expiry} ` +
          `${o.product} LIMIT ${num(o.price)} on ${o.exchange}`))));
        res.appendChild(h('div', { class: 'row' }, h('a', { class: 'btn', href: '#/risk' }, 'Risk panel →'), h('a', { class: 'btn', href: '#/portfolio' }, 'Orders and typed yes →')));
      } catch (e) {
        res.appendChild(note(e.message, 'error'));
      } finally {
        btn.disabled = false;
      }
    });
    return card('Send to paper (a proposal, never a live order)',
      h('p', { class: 'small' }, 'This writes each leg as a proposed order in trader/orders.json and the strategy in trader/strategy.json. ' +
        'Nothing is placed: trader-risk checks every leg, then the paper broker shows the legs as ONE basket ticket and asks for your typed yes to it (every leg or none).'),
      h('div', { class: 'form-row' }, field('Product', prod), h('label', { class: 'chk' }, rep, 'Replace other proposals'), btn), res);
  }

  function show(d) {
    STATE.plots.forEach((x) => x.destroy());
    STATE.plots = [];
    clear(out);
    const head = h('p', { class: 'muted' }, `${d.name} on ${d.underlying} · expiry ${d.expiries.join(', ')} · ${d.days_to_expiry} days · ` +
      `chain ${when(d.asof)} · spot ${num(d.spot)} · forward ${num(d.forward)} · ATM IV ${num(d.atm_iv)}% · lot ${d.lot_size}`);
    out.appendChild(head);
    for (const w of d.warnings || []) out.appendChild(note(w, w.startsWith('STT') || w.includes('DELIVERY') || w.startsWith('expiry day') ? 'warn' : 'info'));
    for (const n of d.notes || []) out.appendChild(note(n, 'info'));
    out.appendChild(results(d));
    const pc = payoffCard(d);
    out.appendChild(pc.el);
    STATE.plots.push(plot(pc.canvas, pc.opts));
    out.appendChild(card('Priced legs', legsTable(d)));
    out.appendChild(editor(d));
    const grid = h('div', { class: 'grid' });
    grid.append(probabilityCard(d), marginCard(d));
    out.appendChild(grid);
    out.appendChild(chargesCard(d));
    out.appendChild(paper(d));
    out.appendChild(h('p', { class: 'muted small' }, 'Probabilities are models, not promises. ' + d.disclaimer));
  }

  async function price(legsOverride) {
    go.disabled = true;
    loading(out, 'Pricing');
    try {
      show(await post('/api/strategy/price', request(legsOverride)));
    } catch (e) {
      clear(out).appendChild(note(e.message, 'error'));
    } finally {
      go.disabled = false;
    }
  }

  form.addEventListener('submit', (e) => { e.preventDefault(); price(); });
  symIn.addEventListener('change', async () => { st.expiry = ''; if (await loadChain()) price(); });
  expSel.addEventListener('change', async () => { st.expiry = expSel.value; if (await loadChain()) price(); });
  if (meta.saved) {
    out.appendChild(note(`Saved: ${meta.saved.name} on ${meta.saved.underlying} (${meta.saved.legs} legs, ${meta.saved.near_expiry}) in trader/strategy.json.`, 'info'));
  }
  if (await loadChain()) await price();
  const onTheme = () => STATE.plots.forEach((x) => x.redraw());
  ctx.bus.addEventListener('theme', onTheme);
  STATE.cleanup.push(() => ctx.bus.removeEventListener('theme', onTheme));
}

export function destroy() {
  STATE.plots.forEach((x) => x.destroy());
  STATE.plots = [];
  STATE.cleanup.forEach((f) => f());
  STATE.cleanup = [];
}
