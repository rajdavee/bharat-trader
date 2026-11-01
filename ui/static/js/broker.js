// The live broker, read only: connection status (Settings) and holdings/positions (Portfolio).
// Nothing here can place, change or cancel an order; a live order goes out only with
// trader-broker's `ticket.py place`, in your own terminal, after every gate.
import { get } from './api.js';
import { h, clear, card, kv, note, table, loading } from './dom.js';
import { inr, num, int, signed, tone, when } from './fmt.js';

const BROKERS = [['upstox', 'Upstox'], ['kite', 'Zerodha Kite'], ['dhan', 'Dhan'], ['angel', 'Angel One']];
const LINKED = { oco: 'one GTT OCO after the fill', bracket: 'with the entry, one order' };

function yesNo(v) {
  return h('span', { class: 'badge ' + (v ? 'pass' : 'warn') }, v ? 'yes' : 'no');
}

export function statusCard(st) {
  const rows = st.brokers.map((b) => {
    const creds = Object.entries(b.credentials);
    const set = creds.filter(([, v]) => v).map(([k]) => k);
    const pf = b.preflight;
    return h('tr', null,
      h('td', null, b.title, b.configured ? h('span', { class: 'badge paper' }, 'config broker') : null),
      h('td', { class: 'small' }, set.length ? set.join(', ') : h('span', { class: 'muted' }, 'none set')),
      h('td', null, yesNo(b.token)),
      h('td', null, yesNo(b.live_enabled)),
      h('td', { class: 'small' }, pf ? [h('span', { class: 'badge ' + (pf.passed ? 'pass' : 'block') },
        pf.passed ? 'passed' : 'failed'), ' ', when(pf.at), pf.public_ip ? ' from ' + pf.public_ip : '']
        : h('span', { class: 'muted' }, 'not run')),
      h('td', { class: 'small' }, b.native_oco
        ? h('span', { class: 'badge pass', title: b.native_oco_words }, LINKED[b.native_oco] || b.native_oco)
        : h('span', { class: 'muted', title: b.native_oco_words }, 'leg by leg')),
      h('td', { class: 'small' }, marginText(b.margin_check)),
      h('td', { class: 'small muted' }, b.plans));
  });
  return card('Broker connection (read only)',
    kv([['Mode', st.mode === 'live' ? h('strong', null, 'LIVE with ' + st.broker + ' (real money)')
      : st.mode === 'paper' ? 'Paper (no real money)' : 'Not valid'],
    st.error ? ['Config problem', st.error] : null]),
    table(['Broker', 'BT_* variables set (names only)', 'Token', 'live_enabled', 'Last preflight',
      'Native stop + target', 'Basket margin today', 'Plan'], rows,
      { caption: 'Live broker connection' }),
    h('p', { class: 'small' }, st.rule),
    h('p', { class: 'small muted' }, 'In your terminal: ', h('code', null, st.commands.login), ', then ',
      h('code', null, st.commands.preflight), '. Tokens stay in your shell, never in trader/.'));
}

function marginText(m) {
  if (!m || m.total == null) return h('span', { class: 'muted' }, 'not checked');
  const parts = [inr(m.total), ' at ', when(m.at)];
  if (m.difference != null) {
    parts.push(' (ours ', inr(m.our_estimate), ', ', h('span', { class: tone(-m.difference) }, signed(m.difference)),
      ')');
  }
  return parts;
}

export function marginCard(defaultBroker) {
  const pick = h('select', { 'aria-label': 'Broker for the margin check' },
    BROKERS.map(([v, t]) => h('option', { value: v, selected: v === defaultBroker ? true : null }, t)));
  const go = h('button', { type: 'button', class: 'btn' }, 'Check basket margin');
  const out = h('div');
  go.addEventListener('click', async () => {
    go.disabled = true;
    loading(out, 'Asking the broker');
    try {
      const d = await get('/api/broker/margin?broker=' + encodeURIComponent(pick.value));
      clear(out);
      if (!d.connected) {
        out.appendChild(note(d.note, 'info'));
        if (d.saved && d.saved.total != null) out.appendChild(h('p', { class: 'small' }, 'Saved today: ', marginText(d.saved)));
        return;
      }
      out.appendChild(kv([
        ['Broker basket margin', inr(d.broker_margin)],
        ['Our SPAN-style estimate', d.our_estimate != null ? inr(d.our_estimate) : 'not for this basket'],
        d.difference != null ? ['Difference', [signed(d.difference), ' (', num(d.difference_pct, 1), '%)']] : null,
        d.hedge_benefit ? ['Hedge benefit', inr(d.hedge_benefit)] : null,
        ['Legs', int(d.legs) + ' from ' + d.source]]));
      out.appendChild(h('p', { class: 'small muted' }, `${d.title}, ${when(d.as_of)}. ${d.note}`));
    } catch (e) {
      clear(out).appendChild(note(e.message, 'error'));
    } finally {
      go.disabled = false;
    }
  });
  return card('Broker basket margin (read only)',
    h('p', { class: 'small muted' }, 'The broker’s own margin for the F&O basket you built, next to our estimate. ' +
      'Read only: its margin calculator, nothing is placed.'),
    h('div', { class: 'row' }, pick, go), out);
}

function rowsTable(kind, d) {
  if (kind === 'holdings') {
    return table([{ label: 'Symbol' }, { label: 'Qty', num: true }, { label: 'Average', num: true },
      { label: 'Last', num: true }, { label: 'P&L', num: true }], d.rows.map((r) => h('tr', null,
      h('td', null, r.symbol), h('td', { class: 'num' }, int(r.qty)), h('td', { class: 'num' }, num(r.avg_price)),
      h('td', { class: 'num' }, r.last_price != null ? num(r.last_price) : '–'),
      h('td', { class: 'num ' + tone(r.pnl) }, r.pnl != null ? signed(r.pnl) : '–'))),
    { dense: true, caption: d.title + ' holdings' });
  }
  return table([{ label: 'Symbol' }, { label: 'Exchange' }, { label: 'Product' }, { label: 'Qty', num: true },
    { label: 'Average', num: true }, { label: 'P&L', num: true }], d.rows.map((r) => h('tr', null,
    h('td', null, r.symbol), h('td', null, r.exchange), h('td', null, r.product),
    h('td', { class: 'num' }, int(r.qty)), h('td', { class: 'num' }, num(r.avg_price)),
    h('td', { class: 'num ' + tone(r.pnl) }, r.pnl != null ? signed(r.pnl) : '–'))),
  { dense: true, caption: d.title + ' positions' });
}

export function accountCard(defaultBroker) {
  const pick = h('select', { 'aria-label': 'Broker' },
    BROKERS.map(([v, t]) => h('option', { value: v, selected: v === defaultBroker ? true : null }, t)));
  const go = h('button', { type: 'button', class: 'btn' }, 'Load from broker');
  const out = h('div');
  const load = async () => {
    go.disabled = true;
    loading(out, 'Asking the broker');
    try {
      const q = '?broker=' + encodeURIComponent(pick.value);
      const [ho, po] = await Promise.all([get('/api/broker/holdings' + q), get('/api/broker/positions' + q)]);
      clear(out);
      if (!ho.connected) { out.appendChild(note(ho.note, 'info')); return; }
      out.appendChild(h('p', { class: 'small muted' }, `${ho.title}, ${when(ho.as_of)}. ${ho.note}`));
      out.appendChild(h('h3', null, 'Holdings'));
      out.appendChild(ho.rows.length ? rowsTable('holdings', ho) : note('No holdings.', 'info'));
      if (ho.pnl != null) out.appendChild(h('p', { class: 'small' }, 'Holdings P&L ', h('span', { class: tone(ho.pnl) }, signed(ho.pnl)),
        ', value about ', inr(ho.value)));
      out.appendChild(h('h3', null, 'Positions'));
      out.appendChild(po.rows.length ? rowsTable('positions', po) : note('No positions today.', 'info'));
    } catch (e) {
      clear(out).appendChild(note(e.message, 'error'));
    } finally {
      go.disabled = false;
    }
  };
  go.addEventListener('click', load);
  return card('Broker account (read only)',
    h('p', { class: 'small muted' }, 'Your real holdings and positions, straight from the broker’s API. Nothing ' +
      'here places or changes an order.'),
    h('div', { class: 'row' }, pick, go), out);
}

export async function status() {
  return get('/api/broker');
}
