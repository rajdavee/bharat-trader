// Option chain (trader-options): OI bars, OI change, our Black-76 IV and Greeks, PCR, max pain, walls,
// expected move and the IV smile. Every number is worked out by the skill; this page only draws it.
import { get } from '../api.js';
import { h, clear, card, kv, note, loading } from '../dom.js';
import { num, int, inr, when } from '../fmt.js';
import { why } from '../why.js';
import { plot } from '../plot.js';

const STATE = { plots: [], cleanup: [] };
const GREEKS_KEY = 'bt-options-greeks';

function greeksPref() {
  try { return localStorage.getItem(GREEKS_KEY) === '1'; } catch (e) { return false; }
}

function setGreeksPref(on) {
  try { localStorage.setItem(GREEKS_KEY, on ? '1' : '0'); } catch (e) { /* private window */ }
}

function hashFor(p) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(p)) if (v !== undefined && v !== null && v !== '') q.set(k, v);
  return '#/options?' + q.toString();
}

function bar(value, max, side) {
  const pct = max > 0 ? Math.max(1, Math.round(100 * value / max)) : 0;
  const el = h('span', { class: 'oi-bar ' + side, 'aria-hidden': 'true' });
  el.style.width = pct + '%';
  return el;
}

function signedInt(v) {
  if (v === null || v === undefined) return '–';
  return (v > 0 ? '+' : '') + int(v);
}

function sideCells(s, max, kind, showGreeks) {
  if (!s) {
    const n = showGreeks ? 9 : 5;
    return Array.from({ length: n }, () => h('td', { class: 'num muted' }, '–'));
  }
  const oi = h('td', { class: 'num oi-cell' }, h('span', { class: 'oi-wrap' }, bar(s.oi, max, kind), h('span', { class: 'oi-n' }, int(s.oi))));
  const chg = h('td', { class: 'num ' + (s.oi_chg > 0 ? 'up' : s.oi_chg < 0 ? 'down' : ''), title: s.buildup || '' },
    signedInt(s.oi_chg));
  const vol = h('td', { class: 'num muted' }, int(s.volume));
  const iv = h('td', { class: 'num' }, s.iv === null ? '–' : num(s.iv, 1));
  const px = h('td', { class: 'num strong', title: `bid ${num(s.bid)} · ask ${num(s.ask)} · LTP ${num(s.ltp)} · ${s.price_source || 'no price'}` },
    s.price === null ? '–' : num(s.price));
  const g = showGreeks ? [
    h('td', { class: 'num' }, s.delta === null ? '–' : num(s.delta, 2)),
    h('td', { class: 'num muted' }, s.gamma === null ? '–' : num(s.gamma * 1000, 3)),
    h('td', { class: 'num muted' }, s.theta === null ? '–' : num(s.theta, 2)),
    h('td', { class: 'num muted' }, s.vega === null ? '–' : num(s.vega, 2)),
  ] : [];
  return kind === 'ce' ? [oi, chg, vol, iv, ...g, px] : [px, ...g, iv, vol, chg, oi];
}

function chainTable(d, showGreeks) {
  const max = Math.max(1, ...d.rows.map((r) => Math.max((r.ce || {}).oi || 0, (r.pe || {}).oi || 0)));
  const w = d.why;
  const head = (label, info, what) => (info ? why(label, info, what) : label);
  const greekHeads = showGreeks ? [head('Δ', w.greeks, 'the Greeks'), 'Γ×1000', 'Θ/day', 'Vega'] : [];
  const ceHead = [head('OI', w.oi, 'OI'), head('OI chg', w.oi_chg, 'OI change'), 'Vol', head('IV', w.iv, 'IV'),
    ...greekHeads, 'Price'];
  const peHead = ['Price', ...greekHeads, head('IV', w.iv, 'IV'), 'Vol', head('OI chg', w.oi_chg, 'OI change'),
    head('OI', w.oi, 'OI')];
  const marks = (k) => {
    const out = [];
    if (k === d.atm) out.push('ATM');
    if (k === d.max_pain) out.push('max pain');
    if (k === (d.walls || {}).call) out.push('call wall');
    if (k === (d.walls || {}).put) out.push('put wall');
    return out;
  };
  const rows = d.rows.map((r) => {
    const ceItm = r.strike < d.forward;
    const peItm = r.strike > d.forward;
    const ce = sideCells(r.ce, max, 'ce', showGreeks).map((td) => { if (ceItm) td.classList.add('itm'); return td; });
    const pe = sideCells(r.pe, max, 'pe', showGreeks).map((td) => { if (peItm) td.classList.add('itm'); return td; });
    const m = marks(r.strike);
    return h('tr', { class: r.strike === d.atm ? 'atm' : null },
      ce, h('th', { scope: 'row', class: 'strike num' }, num(r.strike, r.strike % 1 ? 2 : 0),
        m.length ? h('span', { class: 'marks' }, m.join(' · ')) : null), pe);
  });
  return h('div', { class: 'table-wrap chain-wrap' },
    h('table', { class: 'tbl chain' + (showGreeks ? ' greeks' : '') },
      h('caption', { class: 'sr-only' }, `${d.underlying} option chain, expiry ${d.expiry_nse}: calls on the left, puts on the right`),
      h('thead', null,
        h('tr', null, h('th', { colspan: ceHead.length, class: 'side-head' }, 'CALLS (CE)'), h('th', null, ''),
          h('th', { colspan: peHead.length, class: 'side-head' }, 'PUTS (PE)')),
        h('tr', null, ceHead.map((x) => h('th', { scope: 'col', class: 'num' }, x)), h('th', { scope: 'col', class: 'num' }, 'Strike'),
          peHead.map((x) => h('th', { scope: 'col', class: 'num' }, x)))),
      h('tbody', null, rows)));
}

function tiles(d) {
  const em = d.expected_move || {};
  const st = d.straddle || {};
  const w = d.why;
  const tile = (name, value, sub) => h('div', { class: 'tile' }, h('div', { class: 'tile-name' }, name),
    h('div', { class: 'tile-price' }, value), sub ? h('div', { class: 'tile-time muted' }, sub) : null);
  const rank = d.iv_rank && d.iv_rank.rank !== null && d.iv_rank.rank !== undefined ? d.iv_rank : null;
  return h('div', { class: 'tiles' },
    tile('Spot / forward', why(num(d.forward), w.forward, 'the forward'), `spot ${num(d.spot)} · basis ${num(d.basis)}`),
    tile('ATM IV', why(d.atm_iv === null ? '–' : num(d.atm_iv) + '%', w.atm_iv, 'ATM IV'),
      `ATM ${num(d.atm, 0)} · call ${num(d.atm_iv_ce)} / put ${num(d.atm_iv_pe)}` + (rank ? ` · IV rank ${num(rank.rank, 0)}` : '')),
    tile('Expected move', why('±' + num(em.move), w.expected_move, 'the expected move'),
      `${num(em.low)} – ${num(em.high)} (${num(em.pct)}%)`),
    tile('Straddle', why(num(st.price), w.straddle, 'the straddle'), `${inr(st.rupees_per_lot)} a lot`),
    tile('PCR (OI)', why(num(d.pcr_oi), w.pcr, 'PCR'), `volume ${num(d.pcr_volume)}`),
    tile('Max pain', why(num(d.max_pain, 0), w.max_pain, 'max pain'),
      `${num((d.max_pain_detail || {}).distance)} from the forward`),
    tile('Walls', why(`${num((d.walls || {}).put, 0)} / ${num((d.walls || {}).call, 0)}`, w.walls, 'the walls'),
      'put (support) / call (resistance)'),
    tile('Expiry', d.expiry_nse, `${d.days_to_expiry} days · ${int(d.minutes_to_expiry)} min · lot ${d.lot_size || '?'}`));
}

function chartCard(title, label, opts, caption) {
  const canvas = h('canvas', { class: 'plot', role: 'img', 'aria-label': label, tabindex: '0' });
  const c = card(title, h('div', { class: 'plot-box' }, canvas), caption ? h('p', { class: 'small muted' }, caption) : null);
  return { el: c, canvas, opts };
}

export async function render(root, ctx) {
  const p = ctx.params || {};
  const sym = (p.symbol || 'NIFTY').toUpperCase();
  const strikes = ['10', '15', '25', '0'].includes(p.strikes) ? p.strikes : '15';
  let showGreeks = p.greeks === '1' || (p.greeks === undefined && greeksPref());
  root.appendChild(h('h1', { class: 'page-title' }, 'Option chain', h('span', { class: 'badge' }, 'trader-options')));
  const body = h('div');
  const tools = h('form', { class: 'toolbar', autocomplete: 'off' });
  root.appendChild(tools);
  root.appendChild(body);
  loading(body, 'Loading the ' + sym + ' chain');
  const url = `/api/options?symbol=${encodeURIComponent(sym)}&strikes=${strikes}` + (p.expiry ? '&expiry=' + encodeURIComponent(p.expiry) : '');
  let d;
  try {
    d = await get(url);
  } catch (e) {
    clear(body).appendChild(note(e.message, 'error'));
    d = null;
  }
  const symList = h('datalist', { id: 'opt-syms' }, ((d && d.symbols) || ['NIFTY', 'BANKNIFTY']).map((s) => h('option', { value: s })));
  const symIn = h('input', { id: 'opt-sym', name: 'symbol', value: sym, list: 'opt-syms', class: 'sym-input', spellcheck: 'false',
    autocapitalize: 'characters' });
  const expSel = h('select', { id: 'opt-exp', name: 'expiry' }, ((d && d.expiry_list) || []).map((e) => h('option', {
    value: e.expiry_nse, selected: d && e.expiry === d.expiry ? true : null, disabled: e.available ? null : true,
  }, `${e.expiry_nse} · ${e.weekday} · ${e.days}d${e.monthly ? ' · monthly' : ''}${e.available ? '' : ' (not saved offline)'}`)));
  const nSel = h('select', { id: 'opt-n', name: 'strikes' }, [['10', '10 each side'], ['15', '15 each side'], ['25', '25 each side'],
    ['0', 'every strike']].map(([v, t]) => h('option', { value: v, selected: v === strikes ? true : null }, t)));
  const gBox = h('input', { type: 'checkbox', name: 'greeks', checked: showGreeks ? true : null });
  tools.append(h('label', { class: 'sr-only', for: 'opt-sym' }, 'Underlying'), symIn, symList,
    h('label', { class: 'sr-only', for: 'opt-exp' }, 'Expiry'), expSel,
    h('label', { class: 'sr-only', for: 'opt-n' }, 'Strikes'), nSel,
    h('label', { class: 'chk' }, gBox, 'Greeks'), h('button', { type: 'submit', class: 'btn' }, 'Show'),
    d ? h('a', { class: 'btn', href: `#/strategy?symbol=${encodeURIComponent(d.underlying)}&expiry=${encodeURIComponent(d.expiry_nse)}` },
      'Build a strategy →') : null);
  tools.addEventListener('submit', (e) => {
    e.preventDefault();
    const s = symIn.value.trim().toUpperCase();
    location.hash = hashFor({ symbol: s, expiry: s === sym ? expSel.value : '', strikes: nSel.value, greeks: gBox.checked ? '1' : '0' });
  });
  if (!d) return;

  const draw = () => {
    STATE.plots.forEach((x) => x.destroy());
    STATE.plots = [];
    clear(body);
    for (const wtext of d.warnings || []) body.appendChild(note(wtext, 'warn'));
    body.appendChild(tiles(d));
    body.appendChild(card(`${d.underlying} ${d.expiry_nse}: ${d.rows.length} of ${d.strikes_total} strikes`,
      chainTable(d, showGreeks),
      h('p', { class: 'small muted' }, 'Shaded = in the money (calls below the forward, puts above). OI and volume in contracts ' +
        `(× ${d.lot_size || 'lot'} = units). Price = bid/ask mid, or LTP when the quote is wide; hover for bid, ask and LTP. ` +
        'IV and Greeks are ours (Black-76 on the forward), not NSE\'s. Theta ₹ per unit per calendar day, vega ₹ per vol point.')));
    const smile = d.smile || [];
    const charts = h('div', { class: 'grid' });
    body.appendChild(charts);
    const ivSeries = { x: smile.map((s) => s.strike), series: [{ y: smile.map((s) => s.iv), color: '--accent', label: 'IV %', width: 2 }],
      vlines: [{ x: d.forward, label: 'forward', color: '--muted' }, { x: d.atm, label: 'ATM', color: '--accent', dash: [2, 3] }],
      yfmt: (v) => num(v, 1), xlabel: 'strike' };
    const sm = chartCard('IV smile (out-of-the-money side)', `${d.underlying} IV smile by strike`, ivSeries,
      'Puts below the forward, calls above. ' + (d.skew ? `25-delta risk reversal ${num(d.skew.risk_reversal)} points.` : ''));
    const curve = d.max_pain_curve || [];
    const mp = chartCard('Max pain curve', 'Option buyers\' total payout by settlement strike', {
      x: curve.map((c) => c.strike), series: [{ y: curve.map((c) => c.pain), color: '--warn', label: '₹', width: 2 }],
      vlines: [{ x: d.max_pain, label: 'max pain', color: '--warn' }, { x: d.forward, label: 'forward', color: '--muted' }],
      xlabel: 'strike' }, 'What option buyers together would be paid if the underlying settled at each strike.');
    sm.el.appendChild(h('p', { class: 'small' }, why('Why this curve?', d.why.smile, 'the smile')));
    charts.append(sm.el, mp.el);
    STATE.plots.push(plot(sm.canvas, sm.opts), plot(mp.canvas, mp.opts));
    const b = d.buildup || {};
    const sum = (t) => Object.entries((b.summary || {})[t] || {}).map(([k, v]) => `${k} ${signedInt(v)}`).join(' · ') || '–';
    body.appendChild(card('OI build-up (today)', kv([['Calls', sum('CE')], ['Puts', sum('PE')],
      ['PCR near the money', num((((d.pcr || {}).near_atm) || {}).oi)], ['Chain time', when(d.asof)],
      ['Rate', `${num(d.rate * 100)}% · ${d.rate_source || ''}`]]),
      h('p', { class: 'small muted' }, 'Short build-up in calls is call writing; in puts, put writing. A description of the market, not a signal.')));
    body.appendChild(h('p', { class: 'muted small' }, d.disclaimer));
  };
  draw();
  gBox.addEventListener('change', () => {
    showGreeks = gBox.checked;
    setGreeksPref(showGreeks);
    draw();
  });
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
