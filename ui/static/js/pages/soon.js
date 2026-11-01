// Pages a later v0.3 release fills: Auto-pilot (#45). Clearly labelled.
import { soon } from '../ui.js';

const HONEST = 'Odds always come with their sample size, a range and the result after charges; nothing here will ' +
  'promise a profit, and every order still needs trader-risk PASS and your typed yes.';

const PLACEHOLDERS = {
  autopilot: {
    title: 'Auto-pilot', issue: 45,
    lede: 'Let promoted strategies trade on paper by themselves, inside your limits, and watch what happens.',
    will: [
      'Which strategies may run, their budget and the hours they may trade.',
      'Every paper order they draft, the risk verdict and the fill, as it happens.',
      'A stop-everything switch and the reasons any run paused itself.',
      'Paper only: live trading stays a separate decision made in your own terminal.',
    ],
    honest: HONEST,
    now: [['Risk', '#/risk'], ['Agents', '#/agents']],
  },
};

export async function render(root, ctx) {
  soon(root, PLACEHOLDERS[ctx.page]);
}
