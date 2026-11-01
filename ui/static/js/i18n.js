// The UI's own words in English and Hinglish (Hindi in Roman script, trading words in English; docs/hinglish.md).
// The language is trader/config.json's "lang" (GET /api/meta), switched on the Settings page (POST /api/lang).
// Numbers, ₹, IST and tickers never change. The text the tools write (tickets, verdicts, rings) comes from the
// server already in the chosen language. tests/test_ui.py reads the JSON between the markers below: every key in
// both languages, with the same {placeholders}.
const MSG = /* catalogue:start */ {
  "en": {
    "nav.today": "Today", "nav.chart": "Chart", "nav.scanner": "Scanner", "nav.options": "Option chain",
    "nav.strategy": "Strategy builder", "nav.cotrader": "Co-trader", "nav.ask": "Ask",
    "nav.portfolio": "Portfolio and orders", "nav.risk": "Risk", "nav.autopilot": "Auto-pilot",
    "nav.lab": "Strategy Lab", "nav.journal": "Journal", "nav.tax": "Tax", "nav.agents": "Agents",
    "nav.settings": "Settings", "nav.welcome": "Setup guide", "nav.about": "About",
    "nav.portfolio_short": "Portfolio", "nav.menu": "Menu",
    "group.Discover": "Discover", "group.Plan": "Plan", "group.Trade": "Trade", "group.Review": "Review",
    "group.Setup": "Setup",

    "lang.title": "Language",
    "lang.lede": "The language of the plain-words text: the tools' tickets, verdicts and reports, and this page's labels. Numbers, ₹, IST and tickers stay the same. Saved as \"lang\" in trader/config.json.",
    "lang.en": "English", "lang.hi-en": "Hinglish",
    "lang.saved": "Saved: {name}.",
    "lang.browser": "Kept in this browser until the setup guide creates trader/config.json.",
    "lang.yes_note": "The reply that places a paper order is still the English word yes, in every language.",

    "today.title": "Today",
    "today.session": "Session",
    "today.times": "Both times come from your settings. Entry window ",
    "today.flat_by": ", flat by ",
    "today.pnl": "Day P&L, after charges",
    "today.realised": "Realised ", "today.open": ", open ", "today.charges": ", charges ",
    "today.pnl_zero": "No profit or loss yet today.",
    "today.pnl_up": "A gain so far today on paper. Open positions can still give it back.",
    "today.pnl_down": "A loss so far today on paper. Your daily loss limit stops new entries before it grows too far.",
    "today.budget": "Daily loss budget",
    "today.budget_of": " of {max}",
    "today.budget_meter": "{pct}% at risk in placed entries",
    "today.budget_explain": "What placed entries could lose at their stops, charges included, against your limit for the day.",
    "today.trades": "Trades today",
    "today.trades_value": "{n} of {max}",
    "today.trades_left": "{left} entries left; orders {orders} of {max_orders}",
    "today.trades_none": "You have used every entry your settings allow today. Exits still work.",
    "today.trades_explain": "Each new entry uses one. The cap keeps a bad day from becoming a worse one.",
    "today.risk": "Risk state",
    "today.risk_unread": "Could not read the risk panel.",
    "today.kill_on": "Kill switch on",
    "today.breakers_on": "{n} breaker(s) on",
    "today.clear": "Clear",
    "today.kill_sub": "No new entries today{reason}. Exits still pass.",
    "today.clear_sub": "No kill switch, no circuit breaker. New entries are checked by every rule.",
    "today.risk_explain": "Locks stop new entries only; closing a position is always allowed.",
    "today.positions": "Open positions",
    "today.no_positions": "No open positions",
    "today.no_positions_sub": "When a paper order fills it shows here, marked to the last delayed price.",
    "today.alerts": "Alerts",
    "today.no_ring": "Nothing would ring right now.",
    "today.week": "This week",
    "today.market": "Market",
    "today.next_step": "Next step",
    "today.your_plan": "Next step: your plan",
    "today.check_plan": "Check it against my rules",
    "today.candidates": "Co-trader candidates",
    "today.open_cotrader": "Open the co-trader",
    "today.odds_note": "As of {as_of} IST. Odds are the share of similar past trades that made money after charges, not a promise. Each one still needs your typed yes. ",

    "ct.lede": "Candidates from backtested rules, each with its evidence tier, the odds with a range and sample size, the result after charges and your risk check. Most days there is no edge, and that is the answer.",
    "ct.refresh": "Refresh candidates",
    "ct.show_research": " Show RESEARCH ideas (unproven)",
    "ct.ready": "Ready candidates",
    "ct.blocked_sub": "{n} blocked by your rules",
    "ct.ready_explain": "Ready means trader-risk passed it; your typed yes still decides.",
    "ct.market_now": "Market now",
    "ct.market_explain": "Each strategy is judged on its record in this kind of market.",
    "ct.p_label": "Made money after charges, in the past",
    "ct.p_few": "Few trades in this kind of market: it leans on the rule's overall rate.",
    "ct.p_explain": "The share of similar past trades, not a promise about this one.",
    "ct.ev_label": "Expected result per trade, after charges",
    "ct.ev_explain": "In units of the amount risked at the stop.",
    "ct.tm_label": "Time-machine replay",
    "ct.tm_disagrees": "Disagrees with the history: see below.",
    "ct.tm_explain": "A second opinion from the stock's own bars.",
    "ct.why": "Why this trade",
    "ct.invalidate": "What would invalidate it",
    "ct.why_not": "Why not (your rules)",
    "ct.dns": "What the evidence does not show. ",
    "ct.draft": "Draft to paper",
    "ct.no_trade": "No trade today",
    "ct.ne_why": "Why",
    "ct.ne_change": "What would change that",
    "ct.passed_over": "Passed over",

    "risk.title": "Risk",
    "risk.lede": "Trade: every order is checked against your own limits before it can be confirmed. The kill switch stops new entries for the day; exits always pass.",
    "risk.run": "Run risk check",
    "risk.recorded": "Recorded verdicts (what the paper broker trusts)",
    "risk.preview": "Preview now (not recorded)",
    "risk.limits": "Your limits (trader/config.json, defaults filled in)",

    "pf.title": "Paper portfolio and orders",
    "pf.lede": "Trade and manage: proposals become paper orders only after a risk check PASS and your typed yes. Positions are marked to delayed prices, after charges.",
    "pf.order": "Order {n}",
    "pf.most_loss": "Most it can lose at the stop",
    "pf.why": "why: ",
    "pf.neutral": "The broker-neutral order",
    "pf.reply": "Your reply, word for word",
    "pf.reply_hint": "Type yes to place this ONE order on the paper account. The paper broker refuses “ok”, “sure”, a yes to all, or a yes that names another stock or amount.",
    "pf.record": "Record my reply",
    "pf.decline": "Decline"
  },
  "hi-en": {
    "nav.today": "Aaj", "nav.chart": "Chart", "nav.scanner": "Scanner", "nav.options": "Option chain",
    "nav.strategy": "Strategy builder", "nav.cotrader": "Co-trader", "nav.ask": "Poochiye",
    "nav.portfolio": "Portfolio aur orders", "nav.risk": "Risk", "nav.autopilot": "Auto-pilot",
    "nav.lab": "Strategy Lab", "nav.journal": "Journal", "nav.tax": "Tax", "nav.agents": "Agents",
    "nav.settings": "Settings", "nav.welcome": "Setup guide", "nav.about": "About",
    "nav.portfolio_short": "Portfolio", "nav.menu": "Menu",
    "group.Discover": "Dhoondhiye", "group.Plan": "Plan", "group.Trade": "Trade", "group.Review": "Review",
    "group.Setup": "Setup",

    "lang.title": "Bhasha (language)",
    "lang.lede": "Plain-words text ki bhasha: tools ke tickets, verdicts aur reports, aur is page ke labels. Numbers, ₹, IST aur tickers wahi rehte hain. trader/config.json mein \"lang\" ke roop mein save hota hai.",
    "lang.en": "English", "lang.hi-en": "Hinglish",
    "lang.saved": "Save ho gaya: {name}.",
    "lang.browser": "Jab tak setup guide trader/config.json nahi banata, yeh is browser mein rakha gaya hai.",
    "lang.yes_note": "Paper order place karne wala reply har bhasha mein ab bhi English word yes hi hai.",

    "today.title": "Aaj",
    "today.session": "Session",
    "today.times": "Dono time aapki settings se aate hain. Entry window ",
    "today.flat_by": ", flat by ",
    "today.pnl": "Aaj ka P&L, charges ke baad",
    "today.realised": "Realised ", "today.open": ", open ", "today.charges": ", charges ",
    "today.pnl_zero": "Aaj abhi tak koi profit ya loss nahi.",
    "today.pnl_up": "Paper pe aaj abhi tak profit. Open positions yeh wapas bhi le sakti hain.",
    "today.pnl_down": "Paper pe aaj abhi tak loss. Aapki daily loss limit isse zyada badhne se pehle nayi entries rok deti hai.",
    "today.budget": "Daily loss budget",
    "today.budget_of": " / {max}",
    "today.budget_meter": "placed entries mein {pct}% risk pe",
    "today.budget_explain": "Placed entries apne stops pe kitna kho sakti hain, charges milakar, aaj ki aapki limit ke against.",
    "today.trades": "Aaj ke trades",
    "today.trades_value": "{n} / {max}",
    "today.trades_left": "{left} entries baaki; orders {orders} / {max_orders}",
    "today.trades_none": "Aaj ki saari entries jo aapki settings allow karti hain, ho chuki hain. Exits ab bhi chalte hain.",
    "today.trades_explain": "Har nayi entry ek use karti hai. Yeh cap bure din ko aur bura banne se rokta hai.",
    "today.risk": "Risk state",
    "today.risk_unread": "Risk panel padha nahi ja saka.",
    "today.kill_on": "Kill switch ON",
    "today.breakers_on": "{n} breaker(s) ON",
    "today.clear": "Clear",
    "today.kill_sub": "Aaj koi nayi entry nahi{reason}. Exits ab bhi pass hote hain.",
    "today.clear_sub": "Na kill switch, na circuit breaker. Har nayi entry har rule se check hoti hai.",
    "today.risk_explain": "Locks sirf nayi entries rokte hain; position close karna hamesha allowed hai.",
    "today.positions": "Open positions",
    "today.no_positions": "Koi open position nahi",
    "today.no_positions_sub": "Paper order fill hote hi yahan dikhega, last delayed price pe marked.",
    "today.alerts": "Alerts",
    "today.no_ring": "Abhi kuch ring nahi hoga.",
    "today.week": "Is hafte",
    "today.market": "Market",
    "today.next_step": "Agla kadam",
    "today.your_plan": "Agla kadam: aapka plan",
    "today.check_plan": "Ise mere rules pe check karo",
    "today.candidates": "Co-trader candidates",
    "today.open_cotrader": "Co-trader kholiye",
    "today.odds_note": "{as_of} IST tak. Odds ka matlab hai milte-julte purane trades mein se kitne charges ke baad profit mein rahe, koi vaada nahi. Har ek ko ab bhi aapka typed yes chahiye. ",

    "ct.lede": "Backtested rules se candidates, har ek ke saath evidence tier, range aur sample size ke saath odds, charges ke baad result aur aapka risk check. Zyaadatar din koi edge nahi hota, aur yahi jawab hai.",
    "ct.refresh": "Candidates refresh kijiye",
    "ct.show_research": " RESEARCH ideas dikhaiye (unproven)",
    "ct.ready": "Ready candidates",
    "ct.blocked_sub": "{n} aapke rules ne block kiye",
    "ct.ready_explain": "Ready ka matlab trader-risk ne pass kiya; faisla ab bhi aapka typed yes karta hai.",
    "ct.market_now": "Abhi market",
    "ct.market_explain": "Har strategy ko is tarah ke market mein uske record se judge kiya jaata hai.",
    "ct.p_label": "Past mein charges ke baad profit",
    "ct.p_few": "Is tarah ke market mein kam trades: yeh rule ke overall rate pe zyada tika hai.",
    "ct.p_explain": "Milte-julte purane trades ka share, is trade ke baare mein koi vaada nahi.",
    "ct.ev_label": "Charges ke baad har trade ka expected result",
    "ct.ev_explain": "Stop pe risk ki gayi raqam ki units mein.",
    "ct.tm_label": "Time-machine replay",
    "ct.tm_disagrees": "History se mel nahi khaata: neeche dekhiye.",
    "ct.tm_explain": "Stock ke apne bars se doosri raay.",
    "ct.why": "Yeh trade kyun",
    "ct.invalidate": "Yeh kab galat saabit hoga",
    "ct.why_not": "Kyun nahi (aapke rules)",
    "ct.dns": "Evidence kya nahi dikhata. ",
    "ct.draft": "Paper pe draft kijiye",
    "ct.no_trade": "Aaj koi trade nahi",
    "ct.ne_why": "Kyun",
    "ct.ne_change": "Kya badle to yeh badlega",
    "ct.passed_over": "Chhod diye gaye",

    "risk.title": "Risk",
    "risk.lede": "Trade: har order confirm hone se pehle aapki apni limits pe check hota hai. Kill switch din bhar ke liye nayi entries rok deta hai; exits hamesha pass hote hain.",
    "risk.run": "Risk check chalaiye",
    "risk.recorded": "Record hue verdicts (jin pe paper broker bharosa karta hai)",
    "risk.preview": "Abhi ka preview (record nahi hota)",
    "risk.limits": "Aapki limits (trader/config.json, defaults bhar diye gaye)",

    "pf.title": "Paper portfolio aur orders",
    "pf.lede": "Trade aur manage: proposals paper orders tabhi bante hain jab risk check PASS ho aur aapka typed yes ho. Positions delayed prices pe, charges ke baad, marked hain.",
    "pf.order": "Order {n}",
    "pf.most_loss": "Stop pe zyada se zyada nuksaan",
    "pf.why": "kyun: ",
    "pf.neutral": "Broker-neutral order",
    "pf.reply": "Aapka reply, word for word",
    "pf.reply_hint": "Sirf is EK order ko paper account pe place karne ke liye yes likhiye. Paper broker “ok”, “sure”, “haan”, sab ke liye ek yes, ya kisi doosre stock ya amount wala yes nahi maanta.",
    "pf.record": "Mera reply record kijiye",
    "pf.decline": "Mana kijiye"
  }
} /* catalogue:end */;

let current = 'en';

export function lang() {
  return current;
}

export function setLang(code) {
  current = MSG[code] ? code : 'en';
  document.documentElement.lang = current === 'hi-en' ? 'hi-Latn' : 'en';
  try { localStorage.setItem('bt-lang', current); } catch (e) { /* private mode */ }
  return current;
}

export function storedLang() {
  try { return localStorage.getItem('bt-lang'); } catch (e) { return null; }
}

// t('today.trades_value', { n: 2, max: 3 }): the text in the current language, English when a key is missing.
export function t(key, vars) {
  const s = (MSG[current] && MSG[current][key]) || MSG.en[key] || key;
  return vars ? s.replace(/\{(\w+)\}/g, (m, k) => (vars[k] === undefined ? m : String(vars[k]))) : s;
}

// A risk reason "loss: ..." -> its rule in plain words, from the server's rule_words (null in English).
export function ruleWords(reason, words) {
  if (!words) return null;
  const i = String(reason).indexOf(':');
  return i > 0 ? words[String(reason).slice(0, i).trim().toLowerCase()] || null : null;
}
