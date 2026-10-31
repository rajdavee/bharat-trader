// The paper account (trader-broker) and its order tickets. A yes covers one ticket, typed by you;
// the paper broker checks it word for word and refuses anything that is not READY.
import { get, post } from '../api.js';
import { h, clear, card, kv, note, table, loading } from '../dom.js';
import { inr, num, int, signed, tone, when } from '../fmt.js';
import { why } from '../why.js';
import { accountCard, status } from '../broker.js';

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

export async function render(root) {
  root.appendChild(h('h1', { class: 'page-title' }, 'Paper portfolio and orders'));
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

    const tickets = h('div', { class: 'tickets' });
    if (!o.orders_file) tickets.appendChild(note(o.note, 'info'));
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
