// The agent team (agents/*.md) and each persona's paper track record, rebuilt from the paper fills.
import { get } from '../api.js';
import { h, clear, card, kv, note, table, loading } from '../dom.js';
import { inr, num, signed, tone, when } from '../fmt.js';
import { why } from '../why.js';

const ROLE = { trader: 'trades on paper', research: 'never trades', risk: 'final veto', review: 'read only' };

function money(x) {
  return x === null || x === undefined ? '–' : h('span', { class: tone(x) }, signed(x));
}

function scorecard(c, whyInfo) {
  if (!c || !c.ideas) {
    return note('No paper trades tagged with this persona yet.', 'info');
  }
  return [
    kv([
      ['Closed trades', `${c.closed} (${c.wins} won, ${c.losses} lost` + (c.scratches ? `, ${c.scratches} flat` : '') + ')' +
        (c.open ? ` · ${c.open} open` : '')],
      ['Net P&L after charges', why(signed(c.net_pnl), whyInfo, 'this P&L')],
      ['Charges paid', inr(c.charges)],
      ['Win rate', c.win_rate === null ? '–' : num(c.win_rate, 1) + '%'],
      ['Average win / loss', `${c.avg_win === null ? '–' : inr(c.avg_win)} / ${c.avg_loss === null ? '–' : inr(c.avg_loss)}`],
      ['Profit factor', c.profit_factor === null ? '–' : num(c.profit_factor)],
      ['Expectancy per trade', c.expectancy === null ? '–' : signed(c.expectancy)],
      ['Max drawdown', inr(c.max_drawdown)],
      ['Losing streak now / longest', `${c.losing_streak_now} / ${c.longest_losing_streak}`],
      ['Most entries in a day', String(c.max_entries_in_a_day)],
      ['Tickets: drafted, yes, no', `${c.drafted}, ${c.placed}, ${c.declined}`],
      c.strategies.length ? ['Lab rules traded', c.strategies.join(', ')] : null,
    ]),
    h('p', { class: 'small muted' }, c.verdict),
    c.flags.length ? h('ul', { class: 'warnings small' }, c.flags.map((f) => h('li', null,
      h('span', { class: 'badge warn' }, f.kind), ' ', f.date ? f.date + ': ' : '', f.text))) : null,
    c.recent.length ? table([{ label: 'Symbol' }, { label: 'Setup' }, { label: 'Opened' }, { label: 'Closed' },
      { label: 'P&L after charges', num: true }],
    c.recent.slice().reverse().map((r) => h('tr', null,
      h('td', null, h('a', { href: '#/chart?symbol=' + encodeURIComponent(r.symbol) }, r.symbol)),
      h('td', null, r.strategy ? 'lab: ' + r.strategy : (r.setup || '–')),
      h('td', null, when(r.opened_at)), h('td', null, r.closed ? when(r.closed_at) : 'open'),
      h('td', { class: 'num' }, r.closed ? money(r.pnl) : '–'))),
    { dense: true, caption: 'Recent paper trades of this persona' }) : null,
  ];
}

export async function render(root) {
  root.appendChild(h('h1', { class: 'page-title' }, 'Agent team'));
  root.appendChild(h('p', { class: 'page-lede explain' }, 'Review: each agent persona\'s paper track record after charges, and the habits it shows (over-trading, revenge trades, late exits).'));
  const body = h('div');
  root.appendChild(body);
  loading(body);
  const d = await get('/api/agents');
  clear(body);
  body.appendChild(note('Personas are instructions for an agent that runs these skills. None of them can place a ' +
    'live order: every ticket still needs trader-risk PASS, the risk officer, and your own typed yes.', 'info'));
  if (!d.team.length) body.appendChild(note('No agents/*.md files found next to the skills.', 'warn'));
  for (const p of d.team) {
    body.appendChild(card(p.title,
      h('p', { class: 'small' }, h('span', { class: 'badge paper' }, ROLE[p.role] || p.role), ' ', h('code', null, p.file)),
      h('p', { class: 'small muted' }, p.description),
      scorecard(p.scorecard, d.why)));
  }
  for (const c of d.others) {
    body.appendChild(card(c.persona + ' (no agents/ file)', scorecard(c, d.why)));
  }
  body.appendChild(card('Everything else', kv([
    ['Untagged paper trades', `${d.unattributed.ideas} (${d.unattributed.closed} closed, net ${signed(d.unattributed.net_pnl)})`],
    ['Strategy Lab verdicts', d.lab.length ? d.lab.map((l) => `${l.name} ${l.verdict}`).join(', ') : 'no lab runs yet'],
    ['As of', when(d.as_of)],
  ]), h('p', { class: 'small muted' }, d.how)));
  body.appendChild(h('p', { class: 'muted small' }, d.disclaimer));
}
