// The information architecture: one list drives the sidebar, the phone bar, the palette and the keys.

export const GROUPS = [
  { name: null, pages: ['today'] },
  { name: 'Discover', pages: ['chart', 'scanner', 'options'] },
  { name: 'Plan', pages: ['strategy', 'cotrader'] },
  { name: 'Trade', pages: ['portfolio', 'risk', 'autopilot'] },
  { name: 'Review', pages: ['lab', 'journal', 'agents'] },
  { name: 'Setup', pages: ['settings', 'welcome', 'about'] },
];

// route -> label, title, icon, key after g, coming-in-v0.3 flag, words the palette also matches
export const PAGES = {
  today: { label: 'Today', title: 'Today', icon: 'today', key: 'd', words: 'home dashboard cockpit pnl' },
  chart: { label: 'Chart', title: 'Chart', icon: 'chart', key: 'c', words: 'candles vwap ema' },
  scanner: { label: 'Scanner', title: 'Scanner and watchlist', icon: 'scanner', key: 's', words: 'watchlist screener' },
  options: { label: 'Option chain', title: 'Option chain', icon: 'options', key: 'o', words: 'oi pcr max pain iv fno' },
  strategy: { label: 'Strategy builder', title: 'Strategy builder', icon: 'strategy', key: 'b', words: 'payoff legs condor spread' },
  cotrader: { label: 'Co-trader', title: 'Co-trader', icon: 'cotrader', key: 'k', words: 'ideas candidates odds calibration' },
  portfolio: { label: 'Portfolio and orders', title: 'Paper portfolio and orders', icon: 'portfolio', key: 'p', words: 'positions tickets fills yes' },
  risk: { label: 'Risk', title: 'Risk', icon: 'risk', key: 'r', words: 'kill switch breakers limits verdicts' },
  autopilot: { label: 'Auto-pilot', title: 'Auto-pilot', icon: 'autopilot', key: 'u', soon: true, words: 'automatic paper' },
  lab: { label: 'Strategy Lab', title: 'Strategy Lab', icon: 'lab', key: 'l', words: 'backtest scorecard equity walk-forward' },
  journal: { label: 'Journal', title: 'Journal', icon: 'journal', key: 'j', soon: true, words: 'review notes mistakes' },
  agents: { label: 'Agents', title: 'Agent team', icon: 'agents', key: 't', words: 'personas scorecards team' },
  settings: { label: 'Settings', title: 'Settings', icon: 'settings', key: 'e', words: 'config capital limits broker' },
  welcome: { label: 'Setup guide', title: 'Setup guide', icon: 'welcome', key: 'w', words: 'onboarding first run wizard assistant mcp' },
  about: { label: 'About', title: 'About', icon: 'about', key: 'a', words: 'licences version routes' },
};

export const BOTTOM = ['today', 'chart', 'portfolio', 'risk'];

export function groupOf(route) {
  const g = GROUPS.find((x) => x.pages.includes(route));
  return g ? g.name || '' : '';
}
