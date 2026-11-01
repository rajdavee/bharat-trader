// Pages a later v0.3 release fills: Auto-pilot (#45), Journal (#49). Clearly labelled.
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
  journal: {
    title: 'Journal', issue: 49,
    lede: 'Review each trade: what you planned, what happened, and what a different choice would have done.',
    will: [
      'Every closed trade with its plan, the fills, the charges and your notes.',
      'Tags for mistakes and good habits, and how often each one costs or saves money.',
      'What-if replays: the same trade with the plan followed exactly, or without the late exit.',
      'Weekly review in plain words.',
    ],
    honest: 'A journal describes your own past trades. Patterns in a small number of trades can be chance.',
    now: [['Portfolio and orders', '#/portfolio'], ['Agents', '#/agents']],
  },
};

export async function render(root, ctx) {
  soon(root, PLACEHOLDERS[ctx.page]);
}
