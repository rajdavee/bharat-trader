// Option chain and strategy builder: routes that exist now and describe what v0.2 will show.
import { get } from '../api.js';
import { h, card, note } from '../dom.js';

export function page(which) {
  return {
    async render(root) {
      const d = await get('/api/' + which);
      root.appendChild(h('h1', { class: 'page-title' }, d.title, ' ', h('span', { class: 'badge warn' }, 'coming in v0.2')));
      root.appendChild(card('What this page will show', h('ul', { class: 'soon' }, d.will_show.map((x) => h('li', null, x))),
        h('p', { class: 'small muted' }, `It will read ${d.reads}, written by the ${d.skill} skill (in the works for ${d.coming}).`)));
      root.appendChild(note('Nothing here yet places or proposes an order. Options orders will go through the same chain: ' +
        'trader-risk PASS, then your typed yes, paper first.', 'info'));
    },
  };
}
