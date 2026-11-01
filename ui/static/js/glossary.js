// "What does this mean?" in plain words. Shown by help() in why.js. Keep each answer short and honest.

export const GLOSSARY = {
  day_pnl: ['Day P&L', 'Profit or loss today after charges: what you booked on closed trades plus what open positions ' +
    'would add if you closed them at the last price. Paper money here, not a real account.'],
  realised: ['Realised', 'P&L on trades already closed today, after brokerage, STT, exchange fees, GST and stamp duty.'],
  open_pnl: ['Open P&L', 'What your open positions would add if you closed them now at the last delayed price, after the ' +
    'selling charges. It changes until you close.'],
  charges: ['Charges', 'Brokerage, STT, exchange transaction charge, SEBI fee, stamp duty and GST. Small per trade, ' +
    'large over many trades: every number here is after them.'],
  loss_budget: ['Daily loss budget', 'The most you allowed yourself to lose today (max_daily_loss in your settings). ' +
    'Once realised losses plus the risk of placed entries reach it, no new entries pass the risk check.'],
  trades_left: ['Trades left', 'How many more entries your settings allow today (max_trades_per_day). A cap on trades ' +
    'is one of the simplest guards against over-trading.'],
  kill_switch: ['Kill switch', 'One button that stops new entries for the rest of the day. Exits still work. Lifting it ' +
    'needs the typed word unkill, so it cannot be undone by a stray click.'],
  breakers: ['Circuit breakers', 'Automatic locks on new entries after a losing streak, a deep drawdown, or a trade right ' +
    'after a stop-out (revenge trading). They never block an exit.'],
  entry_window: ['Entry window', 'The time between no_entries_before and no_entries_after in your settings when new ' +
    'trades may start. The first minutes after the open are volatile; late entries leave too little time.'],
  flat_by: ['Flat by', 'The time every intraday (MIS) position must be closed. It is set a few minutes before your ' +
    'broker\'s automatic square-off, which charges a fee and fills at any price.'],
  regime: ['Market regime', 'A label for the kind of market today: trending or range-bound, calm or volatile (India VIX). ' +
    'Strategies that work in one regime often fail in another.'],
  data_health: ['Data health', 'A self-test of every free data source (Yahoo Finance, nseindia.com). If one fails, ' +
    'numbers that depend on it may be stale; the badge says which and the fallback.'],
  r_multiple: ['R (risk multiple)', 'A trade\'s result divided by what it risked at the stop. +2R means it made twice ' +
    'what it risked; -1R means it lost exactly the planned amount.'],
  expectancy: ['Expectancy', 'The average R per trade. Above 0 after charges means the rules made money on that ' +
    'sample. It says nothing certain about the next trade.'],
  profit_factor: ['Profit factor', 'Gross profit divided by gross loss. 1.0 breaks even; 1.2 or more after charges is ' +
    'a common bar. Small samples can flatter it.'],
  drawdown: ['Max drawdown', 'The deepest fall from a high point of the equity curve, as a share of capital. It is ' +
    'what living with the strategy felt like at its worst.'],
  out_of_sample: ['Out of sample', 'Results on data the parameters were not chosen on (walk-forward). They are a fairer ' +
    'test than the whole-sample numbers, which include the data used to tune.'],
  monte_carlo: ['Monte Carlo range', 'The same trades reshuffled 1,000 times: the 5th to 95th percentile of where the ' +
    'P&L could have ended. A wide range means luck mattered a lot.'],
  dsr: ['Deflated Sharpe ratio', 'The chance the strategy\'s true Sharpe ratio is above zero once you allow for how ' +
    'many variations were tried. Trying many ideas makes one look good by chance; this corrects for it.'],
  pbo: ['Probability of overfitting', 'How often the best-looking parameters in one part of the data rank below the ' +
    'median in another part. Above 0.5 means the "best" settings are likely a fluke.'],
  verdict: ['Lab verdict', 'REJECT: not good enough on this evidence. WATCH: passed the gates; paper forward test ' +
    'first. PROMOTE: the forward test agreed. None of them promises a profit.'],
  mis: ['MIS and CNC', 'MIS is intraday: the broker squares it off before the close. CNC is delivery: you keep the ' +
    'shares overnight, with different charges and no leverage.'],
  risk_pct: ['Risk per trade', 'The share of your capital one trade may lose at its stop, charges included. 0.5% to 1% ' +
    'is common: ten losses in a row then cost about 5% to 10%.'],
  broker_profile: ['Broker profile', 'Your broker\'s auto square-off time for intraday positions. The tools exit a few ' +
    'minutes before it. Leave it blank if unsure; a safe default is used.'],
  paper: ['Paper trading', 'Orders are simulated against delayed prices with real charges. Nothing reaches a broker. ' +
    'Going live is a separate, deliberate step in your own terminal.'],
  evidence_tier: ['Evidence tier', 'PROMOTED: the rule passed the lab\'s gates and its paper forward test. WATCH: it passed ' +
    'the gates, the forward test is still running, so paper only. RESEARCH: unproven past numbers, for study or paper ' +
    'only, never for a live order.'],
  probability: ['Probability, with its range', 'The share of similar past trades (same rule, same kind of market) that ' +
    'made money after charges, nudged toward the rule\'s overall rate when there are few of them. The range is where ' +
    'the true rate plausibly sits; n is how many trades it rests on. It is not a promise about this trade.'],
  calibration: ['Calibration', 'Whether past probabilities matched what happened: of the trades given about 40%, did ' +
    'about 40% make money? The Brier score is the average squared miss (lower is better); a rule that keeps ' +
    'promising more than it delivers is demoted.'],
  mcp: ['Connect an AI assistant', 'Any MCP-compatible assistant can use these tools on your trader/ folder: read the ' +
    'market, draft paper orders, run the risk check. It still needs your typed yes for any order.'],
};
