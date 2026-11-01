# UI design notes

How the local web app (`ui/`) is organised and why. Part one is the research
the redesign started from; part two is an audit of the previous UI; part
three is the design system the app now follows. Update this file when a page
or a component changes its job.

## 1. What good trading and fintech apps get right

These are patterns, not looks. The UI copies no product's visual design.

### The workflow is a loop, and every screen sits on it

Well-regarded trading and options apps arrange their screens around the
trader's day, not around their data sources:

| step | the trader asks | where it lives here |
| --- | --- | --- |
| Watchlist | What is moving, what is on my list? | Today, Scanner, Chart |
| Idea | Is there a setup, and how good is the evidence? | Scanner, Option chain, Co-trader (coming), Strategy Lab |
| Plan | Entry, stop, target, size: what can I lose? | Strategy builder, Chart (plan levels) |
| Risk | Does it pass my rules today? | Risk |
| Order | One explicit yes for one exact order | Portfolio and orders |
| Manage | Where am I now, what is at risk, when must I be out? | Today, Portfolio, Risk |
| Review | Did it work after charges, and was I disciplined? | Strategy Lab, Agents, Journal (coming) |

The navigation groups follow this loop: **Discover** (watchlist, ideas),
**Plan**, **Trade** (risk, orders, manage), **Review**, **Setup**. A trader
always knows which step a page belongs to, and the next step is one link away
(a plan links to the risk check, a PASS links to the ticket).

### Home answers "what should I do now?"

The best home screens are cockpits, not catalogues. In order of attention:

1. **State of the session**: open or closed, how long until the entry window
   closes and until you must be flat. Time is the scarcest resource for an
   intraday trader, so it gets the most distinctive element on the page: a
   session rail from pre-open to the close with the user's own windows on it.
2. **Money today**: day P&L after charges, split into realised and open, with
   how it was worked out one click away.
3. **Risk state**: trades and loss budget used, the kill switch, circuit
   breakers. "How close am I to my limits?" is shown as a fraction with a
   meter, never a bare number.
4. **What needs attention**: open positions, alerts that would ring, data
   problems.
5. **Ideas**: candidates and the plan, last, because acting on a new idea is
   the step that most needs the context above it.

### Numbers carry their context

- Every amount has its unit (₹ with Indian grouping, %, R, points, lots).
- P&L is shown as a signed amount *and* a shape (▲ / ▼) *and* a word where
  space allows ("loss", "gain"). Colour is the third cue, never the only one,
  so the screen works for red-green colour-blind users and in greyscale.
- Risk numbers are shown against their limit ("₹514 of ₹1,000 per-trade
  limit"), odds with their sample size and range ("65 trades out of sample;
  final P&L 5th to 95th percentile −₹6,772 to +₹2,808"), and every backtest
  with what it does *not* show.
- A dotted underline means "click for the formula, inputs, source and time"
  (the existing why panels). A small ? next to a term means "what does this
  mean?", in plain words.

### Plain language and progressive disclosure

Beginners need the meaning; experts need density. Good apps serve both by
layering rather than by building two apps:

- **Beginner** density (the default for a new workdir): larger type, a one-
  line "what this means" under key tiles, glossary ? buttons visible.
- **Expert** density: tighter spacing, explanations hidden, more rows on
  screen. Same data, same order; one toggle in the top bar.
- Advanced controls (raw JSON config, Greeks columns, F&O limits) stay behind
  a disclosure or a toggle. Destructive or order-path actions always ask for
  typed confirmation, in both modes.
- Empty screens say what the step is for and what to do next, in user terms
  ("Run a scan to fill your watchlist"), with the command when one is needed;
  file names come second.

### Onboarding

The strongest first-run flows are short, skippable and immediately useful:
ask only what changes the numbers (capital, risk per trade, product, broker
for square-off times), show the consequences in rupees before saving, and then
offer the optional connections (an AI assistant over MCP, a broker read-only).
Every step can be skipped and revisited from Settings.

### Mobile

On a phone the trader mostly *monitors*: P&L, positions, alerts, the session
clock. Patterns that work: a bottom tab bar for the four most used places plus
a menu, cards that stack in priority order, tables that scroll inside their
card (never the page) with the first column fixed in meaning, and touch
targets of at least 40 px. Order entry stays deliberate (typed yes), not
swipe-to-trade.

### Accessibility

- WCAG AA contrast for text in both themes; focus always visible.
- Keyboard first: every page reachable by keys, a command palette
  (Ctrl/Cmd-K) for jumping and actions, `?` lists the keys.
- No meaning in colour alone (shapes, signs and words for gain/loss, pass/
  block, open/closed); live regions for status that changes; reduced motion
  respected.

## 2. Audit of the previous UI (v0.2)

Screenshots of `python3 ui/serve.py --offline tests/fixtures/ui` at 1280, 1440
and 390 px wide (`python3 ui/screenshots.py`).

1. **Navigation overflow (#39).** Nine top-bar links plus search and two pills
   do not fit: at 1440 px "Agents" is cut and "Settings" is unreachable; at
   1280 px Strategy, Agents and Settings are hidden with no scroll cue (the
   scrollbar is hidden).
2. **The phone layout hides most pages.** At 390 px the header wraps to three
   rows, only five of nine page links are visible in a sideways-scrolling strip
   with no cue, and the Settings and Option chain pages scroll sideways (31 px
   and 2 px wider than the screen).
3. **The demo banner is a wall of text** (two to five lines of amber text
   above every page) and repeats the footer's disclaimer.
4. **The home page has no hierarchy.** Seven equal cards: the watchlist count
   weighs the same as the day P&L; open positions and circuit breakers are not
   on it at all; the session clock is a raw timestamp.
5. **Colour-only state.** Gain/loss is green/red text; the market and data
   pills use a coloured dot only.
6. **Discoverability.** The dotted underline is the only cue for why panels;
   keyboard hints are letter chips inside every nav link (clutter), and there
   is no way to jump to a page by name.
7. **First run is a dead end.** A fresh folder shows a banner telling you to
   run a terminal command; Settings lists every one of 40+ raw keys.
8. **Typography fights the data.** Tracked uppercase card titles everywhere,
   monospace and sans numbers mixed on the same card, date formats mixed
   ("2026-10-29 14:30 IST" next to "1 Nov 01:12 IST").
9. **Empty states speak in file names** ("No trader/plan.json yet") instead of
   the next action.
10. **Missing places.** No Strategy Lab page (lab verdicts are buried in
    Agents), no space for the v0.3 features (co-trader, auto-pilot, journal).
11. **Tables** lose their headers on long lists (only the option chain had a
    sticky header).

## 3. Design system v2

### Direction

A trader's cockpit for the Indian session: calm indigo ink on cool paper (or
deep indigo at night), one warm marigold accent reserved for *time* (the "now"
needle, the current page marker). Everything else is quiet so that P&L, risk
and time stand out. One family of sans type with tabular figures; monospace
only for symbols and code.

### Tokens (`ui/static/css/app.css`, `:root`)

| group | tokens |
| --- | --- |
| Colour, surfaces | `--bg`, `--panel`, `--panel-2`, `--border`, `--grid`, `--text`, `--muted` |
| Colour, roles | `--accent` (actions), `--marigold` (time, current), `--focus` |
| Colour, semantic | `--up` (gain), `--down` (loss), `--warn`, `--info`, `--neutral`; each paired with an icon or word in components |
| Type | `--fs-xs` 12 … `--fs-3xl` 34 px (a 1.2 ratio), `--lh`, `--sans`, `--mono` |
| Space | `--sp-1` 4 px … `--sp-7` 48 px |
| Radius | `--r-sm` 6 px (inputs, chips), `--r-md` 10 px (cards), `--r-lg` 16 px (drawers, palette) |
| Elevation | `--e-1` (cards), `--e-2` (popovers), `--e-3` (dialogs) |

Dark and light themes swap the colour tokens only. `[data-density]` on
`<html>` switches beginner and expert spacing and type. `prefers-reduced-motion`
turns transitions off.

### Components (`ui/static/js/ui.js` unless noted)

| component | use |
| --- | --- |
| App shell (`index.html`, `app.js`) | grouped sidebar at 1100 px and wider; below that a menu button opens the same nav as a drawer; under 760 px a bottom tab bar (Today, Chart, Portfolio, Risk, Menu) |
| Command palette (`palette.js`) | Ctrl/Cmd-K: jump to a page, chart a symbol, switch theme or density, run the data check |
| Metric tile `metric()` | label, value with unit, tone with ▲/▼, a why panel, a one-line meaning in beginner mode, an optional ? help |
| Card `card()` (`dom.js`) | a titled section; sentence-case titles |
| Data table `table()` (`dom.js`) | sticky header inside its own scroll box, numbers right-aligned with tabular figures |
| Help `help()` (`why.js`) | a ? button opening a plain-words definition from `glossary.js` |
| Why panel `why()` (`why.js`) | formula, inputs, source and time of one number |
| Stepper `stepper()` | numbered steps with done / current / to-do states (the setup guide) |
| Drawer | the phone and tablet nav; Esc or the scrim closes it |
| Toast `toast()` | short confirmation of an action ("Saved trader/config.json"), polite live region |
| Empty, loading, error | `empty()` says what the step is for and the next action; `skeleton()` while loading; `failure()` with a retry |
| Meter `meter()` | used against limit, with the fraction in words |
| Session rail `sessionRail()` | the NSE day with your windows and a now needle |
| Placeholder `soon()` | a page that a later release fills, labelled "Coming in v0.3" |

### Pages and groups

| group | pages |
| --- | --- |
| Today | the cockpit (home) |
| Discover | Chart, Scanner, Option chain |
| Plan | Strategy builder, Co-trader (coming in v0.3) |
| Trade | Portfolio and orders, Risk, Auto-pilot (coming in v0.3) |
| Review | Strategy Lab, Journal (coming in v0.3), Agents |
| Setup | Settings, Setup guide, About |

### Writing

Sentence case, plain verbs, the action's name kept through the flow (the
button "Save settings" leads to the toast "Settings saved"). Errors say what
happened and how to fix it. Never "guaranteed", "sure-shot" or "risk-free";
odds always come with their sample size and range.
