# Hinglish output (`--lang hi-en`)

Most Indian traders think and talk in a mix of Hindi and English: "Stop ₹2,410 pe hai; charges ke baad R:R 1.7
hai." The tools can answer that way. Hinglish here means **Hindi in Roman script** with the trading words left in
English. It is not Devanagari Hindi and it is not a machine translation: every line was written and reviewed by
hand, and the tests keep the two languages in step.

English stays the default. Nothing changes until you ask for Hinglish.

## Turning it on

For one command, add `--lang hi-en`:

```bash
python3 trader-budget/size.py --lang hi-en
python3 trader-risk/check.py --lang hi-en
python3 trader-broker/ticket.py show --lang hi-en
python3 trader-cotrader/cotrader.py today --lang hi-en
```

For every command, set `lang` in `trader/config.json`:

```json
{ "lang": "hi-en" }
```

`--lang en` on a command still wins over the config, so you can get English back for one answer. In the web UI the
toggle is on the **Settings** page ("Language / Bhasha"); it saves the same `lang` key. Over MCP every tool takes an
optional `lang` argument (see [mcp.md](mcp.md)).

## What is translated

| skill | in Hinglish |
| --- | --- |
| trader-broker | the order ticket, its status, the reply prompt, the confirmation and the decline (`show`, `confirm`, `decline`) |
| trader-risk | the verdict report: every BLOCK rule in plain words, the summary and the next step |
| trader-alerts | every ring (stop, target, level, % move, time, opening bell, pre-open, closing auction) and the `check` summary |
| trader-cotrader | `today` (candidates, odds, the sit-out answer and what would change it) and `explain` (why, invalidation, evidence, what the evidence does NOT show) |
| trader-autopilot | the daily report (`report`, and `report.md` written at the end of the day) |
| trader-exit | the exit plan summary (`plan`, `show`) |
| trader-budget | the sizing report, the refusals and the price notes |
| trader-journal | the weekly review's headings, table labels, verdict and coach notes (`review`) |
| MCP `trade_brief` | the daily brief's headline |
| the web UI | the navigation, the Today page, the Co-trader page's labels and explainers, the Risk page's lede, the order ticket on Portfolio and orders |

What stays English, on purpose:

- **Numbers, ₹ amounts, IST times, dates, tickers and order fields.** `₹1,23,456.50`, `09:40 IST`, `RELIANCE`,
  `SL-M`, `LIMIT`, `MIS`. Indian digit grouping is the same `inr()` everywhere.
- **Technical terms** traders use in English anyway: stop, target, entry, margin, IV, STT, R:R, lot, expiry,
  PASS, BLOCK, READY, PROMOTED, WATCH, RESEARCH.
- **The reply word "yes".** The paper broker accepts only a reply that starts with the English word *yes*, in
  every language. "haan" is refused, the same as "ok" or "sure". This is deliberate: one exact word is harder to
  give by accident, and the parser that guards real orders does not change with the language.
- **The detail under a risk BLOCK.** In Hinglish each blocked rule gets a plain line of its own, and the exact
  English reason with its numbers stays under it in brackets, so nothing is lost in translation:

  ```
  BLOCK #1 BUY 42 RELIANCE (MIS), entry
          - Is trade mein aapke max loss per trade se zyada nuksaan ho sakta hai.
            (loss: 42 shares x 11.00 risk a share = ₹462.00 + ₹52.11 charges = ₹514.11 at the stop, over your ₹300.00 max loss per trade)
  ```

- **JSON and structured data.** `--json` output, the files in `trader/` and the MCP `data` keep English keys and
  values, so scripts and other tools read them the same way in any language.
- Lines other skills write (warnings from a data source, the regime's words, a strategy's own description) and
  the long tail of rare messages and logs. They fall back to English rather than to a guess.

## Style guide

For anyone adding or reviewing a line.

1. **Roman script only.** No Devanagari. Write the way a trader would type in a chat: "aapka", "nahi", "pe",
   "ke baad", "lagbhag".
2. **Short sentences.** One idea each. Split a long English sentence into two Hinglish ones rather than stacking
   clauses.
3. **Keep the trading words in English**: stop, target, entry, margin, premium, lot, expiry, IV, STT, charges,
   order, fill, position, R:R, backtest, regime. Hindi words carry the grammar around them: "Stop ₹2,410 pe hai",
   "charges ke baad", "target pe".
4. **Numbers, ₹, IST and tickers exactly as in English.** They come from the same placeholders; never retype a
   number into the text.
5. **Polite and plain.** Use *aap* and the *-iye* form ("check kijiye", "chhod dijiye"). No slang, no jokes.
6. **Never casual about risk.** A loss is "nuksaan", not a "small hit". A stop is "guarantee nahi". Do not soften
   a BLOCK or a warning, and never add certainty that the English does not have.
7. **Words this pack never uses, in either language**: guaranteed, sure-shot, risk-free, pakka, 100% safe. The
   tests check every line for them.
8. **Safety lines are translated faithfully, not loosely.** They are below; a change to any of them needs a second
   reader.

## The safety lines

These carry the pack's safety rules. Each Hinglish line says the same thing as the English one, with nothing
added and nothing dropped.

| English | Hinglish |
| --- | --- |
| Education and tooling, not financial advice. | Yeh education aur tooling hai, financial advice nahi. |
| Reply "yes" to place this one order on the paper account, or "no" to skip it. | Sirf is EK order ko paper account pe place karne ke liye "yes" likhiye, ya chhodne ke liye "no". |
| One order at a time: a yes covers one ticket, never the batch. | Ek baar mein ek order: ek yes sirf ek ticket ke liye hai, poore batch ke liye kabhi nahi. |
| Nothing is sent yet. This is a LIVE order (real money). | Abhi kuch bhi bheja nahi gaya. Yeh LIVE order hai (asli paisa). |
| Your yes is recorded; the order goes out only with ticket.py place. | Aapka yes record hoga; order sirf ticket.py place se hi jaata hai. |
| Blocked orders do not go to the broker. | Block hue orders broker tak nahi jaate. |
| Every order passed. trader-broker still asks you yes or no for each one. | Saare orders pass hue. trader-broker phir bhi har order ke liye aapse yes ya no poochega. |
| A stop is an order, not a promise: in a fast market it can fill below the stop. | Stop ek order hai, guarantee nahi: tez market mein yeh stop se neeche bhi fill ho sakta hai. |
| Every order still needs trader-risk PASS and your typed yes; RESEARCH ideas are never for a live order. | Har order ko ab bhi trader-risk PASS aur aapka typed yes chahiye; RESEARCH ideas kabhi bhi live order ke liye nahi hain. |
| A probability of 54% means about that share of similar past trades made money after charges, not that this one will. | 54% probability ka matlab hai ki milte-julte purane trades mein se lagbhag itne charges ke baad profit mein rahe, yeh nahi ki yeh trade bhi rahega. |
| No trade today: sitting out is the decision, not a failure. | Aaj koi trade nahi: baith kar intezaar karna bhi ek faisla hai, galti nahi. |
| Paper trading only. Nothing here can place a live order. | Sirf paper trading. Yahan se koi live order place nahi ho sakta. |

The `## Safety` blocks in the SKILL.md files stay in English: they are the rule text the tests compare word for
word, and the rules themselves (paper by default, trader-risk PASS, your typed yes, one order at a time) do not
depend on the language.

## How it is built

Each skill stays self-contained. A script with Hinglish output has its own catalogue and a tiny helper:

```python
MSG = {
    "en": {"stop_promise": "A stop is an order, not a promise: in a fast market it can fill below {stop}."},
    "hi-en": {"stop_promise": "Stop ek order hai, guarantee nahi: tez market mein yeh {stop} se neeche bhi fill ho sakta hai."},
}

t("stop_promise", lang, stop="1163.30")
```

`t(key, lang, **values)` fills a message with `str.format`; a missing language or key falls back to English.
`pick_lang(flag, cfg)` picks `--lang` first, then the config's `lang`, then English (`hinglish` is accepted as a
spelling of `hi-en`). The helper block is copied into each script, like `inr()`, and `tests/test_repo.py` checks
that every copy is identical. The UI has the same idea in `ui/static/js/i18n.js`.

The tests (`tests/test_i18n.py`):

- every key exists in both languages, with the same `{placeholders}`, and formats without an error;
- no line in either language uses the oversell words above;
- the safety lines keep their meaning (one order, the word "yes", nothing sent, not financial advice);
- three golden outputs, made from the real-data fixtures the skills' own tests use, stay exactly as reviewed: a
  paper ticket, a risk BLOCK and the co-trader's sit-out answer (`tests/fixtures/i18n/`);
- English output is unchanged.

To add a line: add the key to both `"en"` and `"hi-en"` in the script's `MSG`, call `t()` where it is printed, run
`python3 -m unittest discover -s tests`, and if a golden output changed on purpose, review the new text and
regenerate it with `BT_GOLDEN=write python3 -m unittest tests.test_i18n`.

Education and tooling, not financial advice.
