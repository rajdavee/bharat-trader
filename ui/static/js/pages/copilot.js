// Co-pilot (LIVE, real money): which strategies may trade live and why, the waiting live tickets with their
// evidence, an approval dialog (the exact phrase, typed, after a 10-second countdown; never one click), today's live
// caps and the live kill switch. Every number comes from trader-copilot; trader-broker's gates still decide.
import { get, post } from '../api.js';
import { h, clear, card, kv, table, note } from '../dom.js';
import { inr, num } from '../fmt.js';
import { meter, empty, skeleton, failure, pageHead, toast } from '../ui.js';

// user-facing words in one place (so they can be catalogued and translated)
const T = {
  lede: 'Real money, one typed approval per order. Only a PROMOTED strategy whose paper forward record meets your ' +
    'thresholds can make a live proposal. There is no auto-approve.',
  notLive: 'Not live',
  live: (b) => `LIVE with ${b} (real money)`,
  noPending: 'No live proposal is waiting',
  noPendingText: 'Most days that is the honest answer. Propose reads the co-trader\'s last run; it places nothing.',
  propose: 'Make today\'s live proposals',
  approve: 'Approve…',
  reject: 'Reject',
  dialogTitle: 'Approve ONE live order',
  typeLabel: (p) => `Type exactly: ${p}`,
  wait: (s) => `Read the ticket. You can approve in ${s} s.`,
  ready: 'Type the phrase exactly, then send.',
  send: 'Send this live order',
  cancel: 'Cancel',
  kill: 'Live kill switch on',
  killOn: (r) => `LIVE KILL SWITCH ON: ${r}. Lift it in your terminal: copilot.py kill --lift`,
  sent: (id, oid) => `${id} sent to the broker${oid ? ` (order ${oid})` : ''}.`,
  noneToday: 'No live proposal today.',
};

function eligibilityCard(rows) {
  if (!rows.length) {
    return card('Who may trade live', empty('No trader-lab strategy yet', 'Run one in the lab, then build a paper ' +
      'forward record (the auto-pilot does that). Nothing can trade live before that.'));
  }
  return card('Who may trade live', table(['Strategy', 'Lab verdict', 'Mode', { label: 'Forward trades', num: true },
    { label: 'Weeks', num: true }, { label: 'Deepest fall', num: true }, { label: 'Brier', num: true }, 'Why not'],
  rows.map((r) => h('tr', null, h('td', null, h('code', null, r.strategy)), h('td', null, r.verdict),
    h('td', null, h('span', { class: 'badge ' + (r.eligible ? 'pass' : 'info') }, r.mode)),
    h('td', { class: 'num' }, num(r.forward.trades, 0)), h('td', { class: 'num' }, num(r.forward.weeks, 1)),
    h('td', { class: 'num' }, num(r.forward.drawdown_r, 2) + 'R'),
    h('td', { class: 'num' }, r.calibration && r.calibration.brier !== undefined && r.calibration.brier !== null
      ? num(r.calibration.brier, 3) : '–'),
    h('td', { class: 'small' }, r.reasons.length ? h('ul', { class: 'log' }, r.reasons.map((x) => h('li', null, x))) : '–'))),
  { caption: 'Every lab strategy, live proposals or paper only, with each failed check' }));
}

function capsCard(d, reload) {
  const c = d.caps;
  const body = [meter(c.orders.used, c.orders.max, `${c.orders.used} of ${c.orders.max} live orders today`)];
  if (c.at_risk.max !== null) {
    body.push(meter(c.at_risk.used, c.at_risk.max,
      `${inr(c.at_risk.used)} of ${inr(c.at_risk.max)} capital at risk (loss at the stops)`));
  }
  if (c.loss.max !== null) {
    const lost = c.loss.known ? Math.max(0, -(c.loss.today || 0)) : 0;
    body.push(meter(lost, c.loss.max, c.loss.known
      ? `${inr(lost)} of ${inr(c.loss.max)} live loss today (then the live kill switch)`
      : `live loss today unknown: ${c.loss.why || ''}`));
  }
  if (c.cooloff_until) body.push(note(`Cooling off after a live loss until ${c.cooloff_until.slice(11, 16)} IST`, 'warn'));
  if (d.kill) body.push(note(T.killOn(d.kill.reason), 'error'));
  for (const x of d.blocked_now || []) body.push(h('p', { class: 'small muted' }, 'New live proposals: ' + x));
  const kbtn = h('button', { type: 'button', class: 'btn' }, T.kill);
  kbtn.disabled = !!d.kill;
  kbtn.addEventListener('click', async () => {
    kbtn.disabled = true;
    try {
      await post('/api/copilot/kill', {});
      toast('Live kill switch on.');
      reload();
    } catch (e) {
      toast(e.message || String(e), 'error');
      kbtn.disabled = false;
    }
  });
  body.push(h('div', { class: 'row' }, kbtn));
  return card('Today\'s live caps', ...body);
}

function dialog(p, reload) {
  const back = document.activeElement;
  const lines = h('pre', { class: 'small ticket-text' }, (p.ticket || []).join('\n'));
  const input = h('input', { type: 'text', class: 'yes-input', autocomplete: 'off', spellcheck: 'false',
    'aria-label': T.typeLabel(p.phrase) });
  const status = h('p', { class: 'small muted', 'aria-live': 'polite' }, '');
  const send = h('button', { type: 'button', class: 'btn primary', disabled: true }, T.send);
  const cancel = h('button', { type: 'button', class: 'btn' }, T.cancel);
  let nonce = null;
  let left = 10;
  let timer = null;
  let modal = null;
  const esc = (ev) => { if (ev.key === 'Escape') close(); };
  function close() {
    clearInterval(timer);
    document.removeEventListener('keydown', esc);
    if (modal) modal.remove();
    if (back && back.focus) back.focus();
  }
  const sync = () => { send.disabled = !(nonce && left <= 0 && input.value.trim() === p.phrase); };
  input.addEventListener('input', sync);
  input.addEventListener('paste', (ev) => ev.preventDefault());       // typed, not pasted
  cancel.addEventListener('click', close);
  send.addEventListener('click', async () => {
    send.disabled = true;
    try {
      const r = await post('/api/copilot/approve', { id: p.id, nonce, typed: input.value });
      toast(T.sent(p.id, (r.sent || {}).broker_order_id));
      close();
      reload();
    } catch (e) {
      status.textContent = e.message || String(e);
      nonce = null;
    }
  });
  const box = h('div', { class: 'modal-box', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'cp-title' },
    h('h2', { id: 'cp-title' }, T.dialogTitle), lines, h('label', null, T.typeLabel(p.phrase)), input, status,
    h('div', { class: 'row' }, send, cancel));
  modal = h('div', { class: 'modal', onclick: (ev) => { if (ev.target === modal) close(); } }, box);
  document.addEventListener('keydown', esc);
  document.body.appendChild(modal);
  input.focus();
  post('/api/copilot/arm', { id: p.id }).then((r) => {
    nonce = r.nonce;
    left = r.wait_s;
    status.textContent = T.wait(left);
    timer = setInterval(() => {
      left -= 1;
      status.textContent = left > 0 ? T.wait(left) : T.ready;
      if (left <= 0) clearInterval(timer);
      sync();
    }, 1000);
  }).catch((e) => { status.textContent = e.message || String(e); });
}

function proposalCard(p, reload) {
  const ev = p.evidence || {};
  const o = p.order;
  const rows = [
    ['Order', `${o.side} ${num(o.qty, 0)} ${p.symbol} ${o.product} ${o.order_type} ${o.price ? num(o.price) : ''}`],
    ['Max loss at the stop', `${inr(p.max_loss || 0)} including charges`],
    ['Kind', p.kind === 'flatten' ? `flatten ${p.closes}: ${p.why}` : `${p.tier} ${p.strategy}`],
  ];
  if (ev.p !== undefined && ev.p !== null) {
    rows.push(['Evidence', `${num(ev.p * 100, 0)}% of ${ev.trades_cell} similar past trades made money after charges ` +
      `(95% ${num((ev.p_lo || 0) * 100, 0)}% to ${num((ev.p_hi || 0) * 100, 0)}%)`]);
  }
  if (p.exits_words) rows.push(['Exits', p.exits_words]);
  rows.push(['Broker margin', (p.margin || {}).words || 'not checked']);
  rows.push(['Expires', `${p.expires_at.slice(11, 16)} IST, or if the price moves more than ${p.max_move_pct}%`]);
  const ok = h('button', { type: 'button', class: 'btn primary' }, T.approve);
  ok.addEventListener('click', () => dialog(p, reload));
  const no = h('button', { type: 'button', class: 'btn' }, T.reject);
  no.addEventListener('click', async () => {
    try {
      await post('/api/copilot/reject', { id: p.id });
      reload();
    } catch (e) {
      toast(e.message || String(e), 'error');
    }
  });
  return card(`${p.id}: LIVE ${o.side} ${num(o.qty, 0)} ${p.symbol}`, kv(rows),
    h('p', { class: 'small' }, 'Phrase: ', h('code', null, p.phrase)), h('div', { class: 'row' }, ok, no));
}

function proposeButton(d, load) {
  const btn = h('button', { type: 'button', class: 'btn' }, T.propose);
  btn.disabled = !d.live;
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const r = await post('/api/copilot/propose', {});
      const pr = r.proposed || {};
      toast(pr.made && pr.made.length ? `${pr.made.length} live proposal(s)` : (pr.blocked || [])[0] || T.noneToday);
      load();
    } catch (e) {
      toast(e.message || String(e), 'error');
      btn.disabled = false;
    }
  });
  return btn;
}

export async function render(root) {
  clear(root);
  root.append(...pageHead('Co-pilot', T.lede));
  const body = h('div', null, skeleton(5));
  root.appendChild(body);
  const load = async () => {
    try {
      const d = await get('/api/copilot');
      clear(body);
      body.appendChild(h('p', null, h('span', { class: 'badge ' + (d.live ? 'warn' : 'info') },
        d.live ? T.live(d.broker) : T.notLive)));
      for (const x of d.problems || []) body.appendChild(note(x, 'warn'));
      body.appendChild(eligibilityCard(d.eligibility || []));
      const pend = d.pending || [];
      if (pend.length) pend.forEach((p) => body.appendChild(proposalCard(p, load)));
      else body.appendChild(card('Waiting for your approval', empty(T.noPending, T.noPendingText, proposeButton(d, load))));
      body.appendChild(capsCard(d, load));
      if ((d.today || []).length) {
        body.appendChild(card('Today', table(['Proposal', 'Kind', 'Stock', 'Status'], d.today.map((p) => h('tr', null,
          h('td', null, p.id), h('td', null, p.kind), h('td', null, p.symbol), h('td', null, p.status))))));
      }
      body.appendChild(h('p', { class: 'small muted' }, d.how));
      body.appendChild(h('div', { class: 'note warn' }, h('b', null, 'What this does not show. '), d.note));
    } catch (e) {
      clear(body).appendChild(failure(e.message || String(e), load));
    }
  };
  await load();
}

export function destroy() {}
