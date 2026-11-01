// Pages a later v0.3 release fills: Co-trader (#44), Auto-pilot (#45). Clearly labelled.
import { soon } from '../ui.js';

const HONEST = 'Odds always come with their sample size, a range and the result after charges; nothing here will ' +
  'promise a profit, and every order still needs trader-risk PASS and your typed yes.';

const PLACEHOLDERS = {
  cotrader: {
    title: 'Co-trader', issue: 44,
    lede: 'Trade candidates from backtested rules, with the evidence for and against each one.',
    will: [
      'Candidate cards: setup, entry, stop, target and size from your own limits.',
      'A calibrated probability with its range and the number of past cases behind it.',
      'Expected result after charges, the market regime it was tested in, and why or why not to take it.',
      'A track record of how well past probabilities matched outcomes.',
    ],
    honest: HONEST,
    now: [['Strategy Lab', '#/lab'], ['Scanner', '#/scanner']],
  },
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
