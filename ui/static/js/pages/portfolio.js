// The paper account (trader-broker) and its order tickets. A yes covers one ticket, typed by you;
// the paper broker checks it word for word and refuses anything that is not READY.
import { get, post } from '../api.js';
import { h, clear, card, kv, note, table, loading } from '../dom.js';
import { inr, num, int, signed, tone, when } from '../fmt.js';
import { why } from '../why.js';
import { accountCard, status } from '../broker.js';
import { plot } from '../plot.js';

const PLOTS = [];

function output(res) {
  const text = [res.output, res.error].filter(Boolean).join('\n');
  return h('pre', { class: 'out ' + (res.ok ? 'ok' : 'bad'), role: 'status' }, text || (res.ok ? 'Done.' : 'Refused.'));
}

function ticketCard(t, refresh) {
  const parts = String(t.plain || '').split(' / ');
  const done = /^DONE: placed/.test(t.status);
  const badge = t.ready ? 'pass' : /^BLOCKED/.test(t.status) ? 'block' : done ? 'info' : 'warn';
  const msg = h('div', { class: 'ticket-msg' });
  const run = async (path, body, btn) => {
    btn.disabled = true;
    clear(msg);
    try {
      const res = await post(path, body);
      msg.appendChild(output(res));
      if (res.ok) setTimeout(refresh, 900);
    } catch (e) {
      msg.appendChild(note(e.message, 'error'));
    } finally {
      btn.disabled = false;
    }
  };
  const box = h('article', { class: 'ticket', 'aria-label': 'Order ' + t.index },
    h('header', { class: 'ticket-head' }, h('strong', null, `Order ${t.index}`), ' ',
      h('span', { class: 'badge ' + badge }, t.status), h('span', { class: 'badge paper' }, 'PAPER')),
    h('ul', { class: 'ticket-lines' }, parts.map((p) => h('li', null, p))),
    t.why ? h('p', { class: 'small muted' }, 'why: ' + t.why) : null,
    kv([['Most it can lose at the stop', why(inr(t.max_loss), t.explain ? t.explain.max_loss : null,
      'this loss')], ['Fingerprint', h('code', null, t.fingerprint)]]),
    h('details', null, h('summary', null, 'The broker-neutral order'), h('pre', { class: 'json' }, JSON.stringify(t.order, null, 2))));
  if (t.ready) {
    const id = 'yes-' + t.index;
    const input = h('input', { id, type: 'text', class: 'yes-input', autocomplete: 'off', spellcheck: 'false',
      placeholder: 'yes', 'aria-describedby': id + '-hint' });
    const go = h('button', { type: 'submit', class: 'btn primary' }, 'Record my reply');
    const no = h('button', { type: 'button', class: 'btn' }, 'Decline');
    const f = h('form', { class: 'yes-form' },
      h('label', { for: id }, 'Your reply, word for word'),
      h('p', { id: id + '-hint', class: 'small muted' }, 'Type yes to place this ONE order on the paper account. ' +
        'The paper broker refuses “ok”, “sure”, a yes to all, or a yes that names another stock or amount.'),
      h('div', { class: 'row' }, input, go, no));
    f.addEventListener('submit', (e) => {
      e.preventDefault();
      if (!input.value.trim()) { input.focus(); return; }
      run('/api/orders/confirm', { index: t.index, yes_text: input.value }, go);
    });
    no.addEventListener('click', () => run('/api/orders/decline', { index: t.index, reply: input.value.trim() || 'no' }, no));
    box.appendChild(f);
  }
  if (done) {
    const sel = h('select', { name: 'leg', 'aria-label': 'Which part filled' },
      h('option', { value: 'order' }, 'the entry'), h('option', { value: 'stop' }, 'the stop'),
      h('option', { value: 'target' }, 'the target'));
    const price = h('input', { name: 'price', type: 'number', step: '0.05', min: '0', inputmode: 'decimal',
      placeholder: 'price (blank: fresh price, or the leg’s own)', 'aria-label': 'Fill price', class: 'price-input' });
    const go = h('button', { type: 'submit', class: 'btn' }, 'Record paper fill');
    const f = h('form', { class: 'fill-form' }, h('span', { class: 'small muted' }, 'Simulate a paper fill of'), sel,
      price, go);
    f.addEventListener('submit', (e) => {
      e.preventDefault();
      const body = { index: t.index, leg: sel.value };
      if (price.value) body.price = Number(price.value);
      run('/api/orders/fill', body, go);
    });
    box.appendChild(f);
  }
  box.appendChild(msg);
  return box;
}

// A multi-leg F&O basket is ONE ticket: one typed yes covers every leg, all or nothing (paper only).
function basketCard(b, refresh) {
  const s = b.summary;
  const msg = h('div', { class: 'ticket-msg' });
  const run = async (path, body, btn) => {
    btn.disabled = true;
    clear(msg);
    try {
      const res = await post(path, body);
      msg.appendChild(output(res));
      if (res.ok) setTimeout(refresh, 900);
    } catch (e) {
      msg.appendChild(note(e.message, 'error'));
    } finally {
      btn.disabled = false;
    }
  };
  const placed = /^DONE: placed/.test(b.status);
  const filled = /^DONE: placed, filled/.test(b.status);
  const badge = b.ready ? 'pass' : /^BLOCKED/.test(b.status) ? 'block' : placed ? 'info' : 'warn';
  const box = h('article', { class: 'ticket', 'aria-label': 'Basket ' + b.basket_id },
    h('header', { class: 'ticket-head' }, h('strong', null, `Basket of ${s.legs} leg${s.legs === 1 ? '' : 's'} on ${s.underlying}`),
      ' ', h('span', { class: 'badge ' + badge }, b.status), h('span', { class: 'badge paper' }, 'PAPER')),
    h('ol', { class: 'ticket-lines' }, b.legs.map((l) => h('li', null, l.plain))),
    kv([['Strategy', s.strategy_id ? `${s.name}, ${s.action.replace('-', ' ')} ${s.strategy_id}` : s.name],
      ['Fills hedge first', 'legs ' + s.hedge_first.join(', ')],
      ['Net premium', s.net_premium ? `${inr(Math.abs(s.net_premium))} ${s.net_premium > 0 ? 'received' : 'paid'}` : '–'],
      ['Margin blocked (estimate)', h('span', { title: s.margin_source }, inr(s.margin))],
      ['Max loss at expiry', s.max_loss_unlimited ? 'UNLIMITED' : s.max_loss != null
        ? `${inr(s.max_loss)}; with charges ${inr(s.max_loss_with_charges)}` : '–'],
      ['Charges', `${inr(s.charges)} now, about ${inr(s.round_trip_charges)} round trip`],
      ['Basket id', h('code', null, b.basket_id)]]),
    (b.warnings || []).map((w) => note(w, 'warn')));
  if (b.ready) {
    const id = 'yes-basket';
    const input = h('input', { id, type: 'text', class: 'yes-input', autocomplete: 'off', spellcheck: 'false',
      placeholder: 'yes', 'aria-describedby': id + '-hint' });
    const go = h('button', { type: 'submit', class: 'btn primary' }, 'Record my reply');
    const no = h('button', { type: 'button', class: 'btn' }, 'Decline');
    const f = h('form', { class: 'yes-form' },
      h('label', { for: id }, 'Your reply to this basket, word for word'),
      h('p', { id: id + '-hint', class: 'small muted' }, 'Type yes to place EVERY leg above on the paper account, all or ' +
        'nothing. The paper broker refuses “ok”, “sure”, or a yes that names another underlying or a number not on this ticket.'),
      h('div', { class: 'row' }, input, go, no));
    f.addEventListener('submit', (e) => {
      e.preventDefault();
      if (!input.value.trim()) { input.focus(); return; }
      run('/api/orders/confirm', { basket: true, basket_id: b.basket_id, yes_text: input.value }, go);
    });
    no.addEventListener('click', () => run('/api/orders/decline', { basket: true, reply: input.value.trim() || 'no' }, no));
    box.appendChild(f);
  }
  if (placed && !filled) {
    const go = h('button', { type: 'button', class: 'btn' }, 'Record paper fill (every leg at the latest chain, or none)');
    go.addEventListener('click', () => run('/api/orders/fill', { basket: true }, go));
    box.appendChild(h('div', { class: 'row' }, go));
  }
  box.appendChild(msg);
  return box;
}

// The F&O book, one card per strategy. The buttons only WRITE A PROPOSAL (ticket.py adjust): nothing is placed.
function strategyCard(s, refresh) {
  const msg = h('div', { class: 'ticket-msg' });
  const adjust = async (body, btn) => {
    btn.disabled = true;
    clear(msg);
    try {
      const res = await post('/api/fno/adjust', Object.assign({ strategy: s.id }, body));
      msg.appendChild(output(res));
      if (res.ok) {
        msg.appendChild(note('A proposal is in Order tickets below: run the risk check, then type your yes. Nothing was placed.', 'info'));
        setTimeout(refresh, 1200);
      }
    } catch (e) {
      msg.appendChild(note(e.message, 'error'));
    } finally {
      btn.disabled = false;
    }
  };
  const g = (x) => (x == null ? '–' : num(x));
  const rows = s.positions.map((r) => {
    const close = h('button', { type: 'button', class: 'btn small' }, 'Close');
    close.addEventListener('click', () => adjust({ action: 'close-leg', leg: r.leg }, close));
    return h('tr', null, h('td', null, String(r.leg)), h('td', null, r.contract), h('td', { class: 'num' }, signed(r.qty, int)),
      h('td', { class: 'num' }, num(r.avg_price)), h('td', { class: 'num' }, r.mark != null ? num(r.mark) : 'no quote'),
      h('td', { class: 'num ' + tone(r.unrealised) }, r.unrealised != null ? signed(r.unrealised) : '–'),
      h('td', { class: 'num' }, r.iv != null ? num(r.iv) + '%' : '–'),
      h('td', { class: 'num' }, r.greeks ? g(r.greeks.delta) : '–'), h('td', { class: 'num' }, r.greeks ? g(r.greeks.theta) : '–'),
      h('td', null, close));
  });
  const box = card(`${s.id} · ${s.name} on ${s.underlying}`,
    kv([['P&L', h('span', { class: tone(s.pnl) }, `${signed(s.pnl)} (open ${signed(s.unrealised)}, realised ${signed(s.realised)})`)],
      ['Margin blocked', h('span', { title: s.margin_source || '' }, inr(s.margin))],
      ['Greeks', `Δ ${g(s.greeks.delta)} · Γ ${g(s.greeks.gamma)} · Θ ${g(s.greeks.theta)}/day · vega ${g(s.greeks.vega)}`],
      ['Charges paid', inr(s.charges)]]),
    rows.length ? table([{ label: 'Leg' }, { label: 'Contract' }, { label: 'Qty', num: true }, { label: 'Average', num: true },
      { label: 'Mark', num: true }, { label: 'P&L', num: true }, { label: 'IV', num: true }, { label: 'Δ', num: true },
      { label: 'Θ/day', num: true }, { label: 'Adjust' }], rows, { dense: true, caption: 'Legs of ' + s.id }) : null);
  if (s.payoff) {
    const canvas = h('canvas', { class: 'plot', role: 'img', tabindex: '0',
      'aria-label': `Payoff at expiry of ${s.id} on ${s.underlying}, in rupees, against the underlying` });
    box.appendChild(h('div', { class: 'plot-box' }, canvas));
    box.appendChild(h('p', { class: 'small muted' }, 'Payoff at expiry of the open legs from their average prices (gross, before charges). ' +
      'Hover or use the arrow keys for values.'));
    const vlines = [{ x: s.payoff.now, label: 'forward ' + num(s.payoff.now), color: '--text', dash: [4, 4] }];
    requestAnimationFrame(() => PLOTS.push(plot(canvas, { x: s.payoff.x, series: [{ y: s.payoff.y, color: '--accent', width: 2.2, label: 'expiry' }],
      vlines, zero: true, fill: 0, xlabel: s.underlying, yfmt: (v) => inr(v) })));
  }
  const all = h('button', { type: 'button', class: 'btn' }, 'Close all (one basket)');
  all.addEventListener('click', () => adjust({ action: 'close-all' }, all));
  const legIn = h('input', { type: 'number', min: '1', step: '1', class: 'price-input', 'aria-label': 'Leg to roll', placeholder: 'leg' });
  const kIn = h('input', { type: 'number', min: '1', step: '50', class: 'price-input', 'aria-label': 'New strike', placeholder: 'new strike' });
  const roll = h('button', { type: 'button', class: 'btn' }, 'Roll leg');
  roll.addEventListener('click', () => adjust({ action: 'roll-leg', leg: Number(legIn.value), strike: Number(kIn.value) }, roll));
  const hedgeIn = h('input', { type: 'text', class: 'yes-input', 'aria-label': 'Hedge to add', placeholder: 'BUY 1 PE 22500' });
  const hedge = h('button', { type: 'button', class: 'btn' }, 'Add hedge');
  hedge.addEventListener('click', () => adjust({ action: 'add-hedge', hedge: hedgeIn.value }, hedge));
  box.appendChild(h('div', { class: 'row' }, all, legIn, kIn, roll, hedgeIn, hedge));
  box.appendChild(h('p', { class: 'small muted' }, 'Each button writes a proposal ticket only. It goes through trader-risk and your typed yes like any order.'));
  box.appendChild(msg);
  return box;
}

export async function render(root) {
  root.appendChild(h('h1', { class: 'page-title' }, 'Paper portfolio and orders'));
  root.appendChild(h('p', { class: 'page-lede explain' }, 'Trade and manage: proposals become paper orders only after a risk check PASS and your typed yes. Positions are marked to delayed prices, after charges.'));
  const body = h('div');
  root.appendChild(body);
  const live = h('div');
  root.appendChild(live);
  status().then((st) => {
    const named = st.brokers.find((b) => b.configured);
    live.appendChild(accountCard(named ? named.broker : 'upstox'));
  }).catch((e) => live.appendChild(note(e.message, 'error')));
  const refresh = async () => {
    loading(body);
    const [a, o] = await Promise.all([get('/api/portfolio'), get('/api/orders')]);
    PLOTS.splice(0).forEach((x) => x.destroy());
    clear(body);
    const t = a.today;
    const refreshBtn = h('button', { type: 'button', class: 'btn' }, 'Refresh prices');
    const refreshMsg = h('div');
    refreshBtn.addEventListener('click', async () => {
      refreshBtn.disabled = true;
      try {
        const res = await post('/api/prices/refresh', {});
        clear(refreshMsg).appendChild(res.ok ? note('Saved fresh quotes to trader/prices.json.', 'ok') : output(res));
        setTimeout(refresh, 700);
      } catch (e) {
        clear(refreshMsg).appendChild(note(e.message, 'error'));
      } finally {
        refreshBtn.disabled = false;
      }
    });
    body.appendChild(h('div', { class: 'grid' },
      card('Account (paper)', kv([
        ['Cash', why(inr(a.cash), a.why.cash, 'cash')],
        a.margin_blocked ? ['Margin blocked (F&O)', why(inr(a.margin_blocked), a.fno.why.margin, 'the margin')] : null,
        a.margin_blocked ? ['Free cash', inr(a.free_cash)] : null,
        ["Today's P&L", why(signed(t.pnl), a.why.pnl, "today's P&L", tone(t.pnl))],
        ['Realised today', why(signed(t.realised), a.why.realised, 'realised P&L', tone(t.realised))],
        ['Open, if closed now', why(signed(t.open_pnl), a.why.open_pnl, 'open P&L', tone(t.open_pnl))],
        ['Charges paid today', inr(t.charges)],
      ]), h('div', { class: 'row' }, refreshBtn), refreshMsg),
      card('Limits today', kv([
        ['Trades', `${t.trades} of ${t.max_trades}`],
        ['Orders', `${t.orders} of ${t.max_orders}`],
        ['Loss at risk', why(`${inr(t.at_risk)} of ${inr(t.max_daily_loss)}`, a.why.at_risk, 'the amount at risk')],
        ['Date', a.date],
      ]))));

    const pos = a.positions.map((p) => h('tr', null,
      h('td', null, h('a', { href: '#/chart?symbol=' + encodeURIComponent(p.symbol) }, p.symbol)),
      h('td', null, p.product),
      h('td', { class: 'num' }, int(p.qty)),
      h('td', { class: 'num' }, num(p.avg_price)),
      h('td', { class: 'num' }, p.price != null ? num(p.price) : '–'),
      h('td', { class: 'num' }, p.value != null ? inr(p.value) : '–'),
      h('td', { class: 'num ' + tone(p.pnl_if_closed) }, p.pnl_if_closed != null
        ? why(signed(p.pnl_if_closed), p.why.pnl_if_closed, 'this P&L') : '–'),
      h('td', { class: 'small muted' }, p.price_source ? `${p.price_source}, ${when(p.price_time)}` : 'no price')));
    body.appendChild(card('Positions', pos.length ? table([{ label: 'Symbol' }, { label: 'Product' },
      { label: 'Qty', num: true }, { label: 'Average', num: true }, { label: 'Price', num: true },
      { label: 'Value', num: true }, { label: 'P&L if closed', num: true }, { label: 'Marked at' }], pos,
    { caption: 'Open paper positions' }) : note('No open positions.', 'info'),
    a.working_legs.length ? h('ul', { class: 'legs' }, a.working_legs.map((l) => h('li', null,
      `Working: ${l.symbol} SL-M stop ${num(l.stop)} / LIMIT target ${num(l.target)} (order ${l.index}, ${l.paper_order_id})`)))
      : null));

    const f = a.fno;
    if (f && f.strategies.length) {
      const t = f.totals;
      body.appendChild(card('F&O on paper (marked to trader/options.json)', kv([
        ['Open P&L', why(signed(t.unrealised), f.why.unrealised, 'open F&O P&L', tone(t.unrealised))],
        ['Portfolio Greeks', why(`Δ ${num(t.greeks.delta)} · Γ ${num(t.greeks.gamma)} · Θ ${num(t.greeks.theta)}/day · vega ${num(t.greeks.vega)}`,
          f.why.greeks, 'the Greeks')],
        ['Margin blocked', inr(t.margin_blocked)], ['Marked at', when(f.as_of)]]),
      f.missing_quotes.length ? note('No quote for ' + f.missing_quotes.join(', ') + ': refresh trader/options.json.', 'warn') : null));
      for (const s of f.strategies) body.appendChild(strategyCard(s, refresh));
      for (const p of f.positions) if (p.warning) body.appendChild(note(p.warning, 'warn'));
    }

    const tickets = h('div', { class: 'tickets' });
    if (!o.orders_file) tickets.appendChild(note(o.note, 'info'));
    else if (o.basket) tickets.appendChild(basketCard(o.basket, refresh));
    else if (!o.tickets.length) tickets.appendChild(note('trader/orders.json has no orders.', 'info'));
    for (const tk of o.tickets) tickets.appendChild(ticketCard(tk, refresh));
    body.appendChild(card('Order tickets', o.source ? h('p', { class: 'small muted' }, `From ${o.source}. ${o.rule}`) : null,
      tickets, h('p', { class: 'small' }, h('a', { href: '#/risk' }, 'Run the risk check →'),
        ' An order is READY only after trader-risk passed this exact order today.')));

    const fills = a.fills_today.map((f) => h('tr', null, h('td', null, String(f.at || '').slice(11, 16)),
      h('td', null, f.side), h('td', null, f.symbol), h('td', { class: 'num' }, int(f.qty)),
      h('td', { class: 'num' }, num(f.price)), h('td', null, f.leg),
      h('td', { class: 'num' }, inr(f.charges)),
      h('td', { class: 'num ' + tone(f.realised_pnl) }, f.realised_pnl != null ? signed(f.realised_pnl) : '')));
    body.appendChild(card('Fills today', fills.length ? table(['Time', 'Side', 'Symbol', { label: 'Qty', num: true },
      { label: 'Price', num: true }, 'Leg', { label: 'Charges', num: true }, { label: 'Realised', num: true }], fills,
    { dense: true, caption: 'Paper fills today' }) : note('No fills today.', 'info')));

    const hist = a.history.slice().reverse().map((r) => h('li', null, h('span', { class: 'muted' }, when(r.at) + ' '),
      h('span', { class: 'badge' }, r.status), ' ', r.plain || r.symbol || '',
      r.yes_text ? h('span', { class: 'muted' }, ` · reply: “${r.yes_text}”`) : null));
    body.appendChild(card('Decisions log (trader/state/broker.json)', hist.length ? h('ul', { class: 'log' }, hist)
      : note('Nothing recorded yet.', 'info')));
    body.appendChild(h('p', { class: 'muted small' }, a.disclaimer));
  };
  await refresh();
}
