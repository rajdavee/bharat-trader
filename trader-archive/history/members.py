"""Point-in-time index membership, rebuilt from NSE Indices' own announcements.

Sources (all public, verified 2026-11-01):
  https://www.niftyindices.com/press-release            one page listing every press release since 1998
                                                        (date, PDF link, title)
  https://www.niftyindices.com/Press_Release/ind_prsDDMMYYYY[_N].pdf
                                                        the index-change announcements: per index, the
                                                        companies excluded and included and the date the
                                                        change takes effect
  https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv (100, 200, 500 likewise)
                                                        today's constituents
  https://nsearchives.nseindia.com/content/equities/symbolchange.csv
                                                        every NSE symbol change with its date

NSE Indices publishes no historical constituent file for free, so membership on a past day is today's
list with every later announced change undone, newest first. Announcements before about 2012 name the
companies but not their symbols: those names are matched to symbols through later announcements, NSE's
list of listed companies and today's index files, and whatever stays unmatched is reported as a gap.

Pure functions only: archive.py fetches, stores and calls these.
"""

import datetime
import html
import re

from .pdftext import pdf_lines

LISTING_URL = "https://www.niftyindices.com/press-release"
PDF_BASE = "https://www.niftyindices.com"
LIST_URLS = {
    "NIFTY 50": "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    "NIFTY NEXT 50": "https://nsearchives.nseindia.com/content/indices/ind_niftynext50list.csv",
    "NIFTY 100": "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv",
    "NIFTY 200": "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
    "NIFTY 500": "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
}
SYMBOL_CHANGES_URL = "https://nsearchives.nseindia.com/content/equities/symbolchange.csv"
EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
# Announcements a later one cancelled: NSE Indices' press release of May 13, 2020 (ind_prs13052020) made the
# replacements due on March 27, 2020 "null and void"; NIFTY 50's had already been brought forward to March 19,
# 2020 by its own announcement (ind_prs16032020), which stays. Indices kept from a voided file are listed.
VOIDED = {"ind_prs18022020.pdf": (), "ind_prs12032020.pdf": (), "ind_prs19032020.pdf": ()}
SIZES = {"NIFTY 50": 50, "NIFTY NEXT 50": 50, "NIFTY 100": 100, "NIFTY 200": 200, "NIFTY 500": 500}

# every name an index has carried (S&P branding until 2013, CNX until November 2015), lower case
ALIASES = {
    "NIFTY 50": ("nifty 50", "nifty50", "nifty", "cnx nifty", "s&p cnx nifty", "nifty fifty"),
    "NIFTY NEXT 50": ("nifty next 50", "niftynext50", "nifty next50", "cnx nifty junior", "nifty junior"),
    "NIFTY 100": ("nifty 100", "nifty100", "cnx 100", "s&p cnx 100"),
    "NIFTY 200": ("nifty 200", "nifty200", "cnx 200", "s&p cnx 200"),
    "NIFTY 500": ("nifty 500", "nifty500", "cnx 500", "s&p cnx 500"),
}
_ALIAS = {a: k for k, v in ALIASES.items() for a in v}
_COMPACT = {a.replace(" ", ""): k for a, k in _ALIAS.items()}
INDEX_KEYS = {"NIFTY": "NIFTY 50", "NIFTY50": "NIFTY 50", "^NSEI": "NIFTY 50", "NIFTYNXT50": "NIFTY NEXT 50",
              "NIFTY100": "NIFTY 100", "NIFTY200": "NIFTY 200", "NIFTY500": "NIFTY 500"}

MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                          "nov", "dec"))}
_DATE = r"([A-Z][a-z]{2,8})\.?\s+(\d{1,2})\s*,?\s*(\d{4})"
_SKIP_TITLE = re.compile(r"\bsme\b|\bipo\b|fixed income|bond|g-sec|gsec|\bsdl\b|t-bill|treasury|\besg\b|shariah|"
                         r"forex|\busd\b|launch|methodolog|total return|dividend point|tracking|target maturity|"
                         r"india fpi|mutual fund|commodit|ethical|dissemination", re.I)
_KEEP_TITLE = re.compile(r"indices|index changes?\b|corporate action adjustment|change in index|changes in index|"
                         r"replacement|exclusion|inclusion|reconstitut|re-?balancing|cnx nifty|cnx 500|cnx 100|cnx 200|"
                         r"nifty 50\b|nifty 100\b|nifty 200\b|nifty 500\b|nifty next 50|nifty junior|"
                         r"corporate adjustment", re.I)
_ALWAYS = re.compile(r"replacements? in (?:the )?indices|changes? in indices|index changes?\b|re-?balancing", re.I)
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
_NOT_SYMBOL = {"LTD", "LTD.", "LIMITED", "INDIA", "(INDIA)", "BANK", "CO.", "CORP.", "INC.", "PLC", "AG", "REIT",
               "TRUST", "SYMBOL", "NAME", "COMPANY", "NO.", "&", "-", "ETF"}


def index_key(name):
    """'NIFTY', 'Nifty 50', 'CNX Nifty', 'nifty500' ... -> the canonical key ('NIFTY 50'), or None."""
    n = " ".join(str(name or "").replace("&amp;", "&").split())
    if n.upper() in INDEX_KEYS:
        return INDEX_KEYS[n.upper()]
    if n.upper() in SIZES:
        return n.upper()
    return _ALIAS.get(n.lower()) or _COMPACT.get(n.lower().replace(" ", ""))


def month_day(text):
    """'September 30, 2026' / 'Sept. 3,2012' -> date or None."""
    m = re.search(_DATE, text or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(1)[:3].lower())
    if not mon:
        return None
    try:
        return datetime.date(int(m.group(3)), mon, int(m.group(2)))
    except ValueError:
        return None


# ---------------------------------------------------------------- the press-release listing

def parse_listing(page):
    """The niftyindices press-release page -> [{"date", "url", "title"}] newest first, every item."""
    out, seen = [], set()
    for m in re.finditer(r'data-date="([^"]+)".*?href=[\'"](/Press_Release/[^\'"]+\.pdf)[\'"][^>]*>([^<]*)</a>',
                         page, re.S):
        url = PDF_BASE + m.group(2)
        if url in seen:
            continue
        seen.add(url)
        d = month_day(m.group(1).replace("Sept", "Sep"))
        out.append({"date": d.isoformat() if d else "", "url": url,
                    "title": " ".join(html.unescape(m.group(3)).split())})
    return out


def wanted(item):
    """Is this press release one that can change NIFTY 50/Next 50/100/200/500 membership?"""
    t = item.get("title", "")
    return bool(_KEEP_TITLE.search(t)) and (not _SKIP_TITLE.search(t) or bool(_ALWAYS.search(t)))


# ---------------------------------------------------------------- one announcement -> changes

def _norm_name(name):
    s = html.unescape(name or "").lower().replace("&", " and ").replace("’", "'")
    s = re.sub(r"\blimited\b", "ltd", s)
    s = re.sub(r"\bcompany\b", "co", s)
    s = re.sub(r"\bcorporation\b", "corp", s)
    s = re.sub(r"\(india\)", "india", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(the|ltd|co|corp)\b", " ", s)
    return " ".join(s.split())


def norm_name(name):
    return _norm_name(name)


def _heading(line):
    """A section heading -> canonical index key, 'other' (some other index or section), or None (not one)."""
    s = line.replace("\t", " ").strip()
    m = re.match(r"^(?:\(?[0-9]{1,2}\)|\(?[ivxIVX]{1,4}\)|\(?[A-Za-z]\)|[A-Z]\.|[0-9]{1,2}\.)\s+(.{3,90})$", s)
    if not m:
        return None
    full = m.group(1).strip().rstrip(":").strip()
    body = re.sub(r"\s+(index|indices)$", "", full, flags=re.I).strip()
    key = index_key(body)
    if key:
        return key
    if re.search(r"nifty|cnx|\bindex\b|indices|replacement|review|exclusion|inclusion", full, re.I):
        return "other"
    return None


_INDEX_IN_TEXT = re.compile(r"((?:S&P\s+)?(?:CNX|Nifty|NIFTY)(?:\s?[A-Za-z0-9]+){0,2})\s+[Ii]ndex\b")


def _bare_heading(line, nxt):
    """'S&P CNX 500 Index' on a line of its own, right before 'The following company is being ...'."""
    s = line.replace("\t", " ").strip().rstrip(":")
    if not re.search(r"(?i)\bindex$", s) or "following" not in nxt.lower():
        return None
    return index_key(re.sub(r"\s+index$", "", s, flags=re.I))


def _sentence_index(text):
    """'... make the following change in this regard in S&P CNX 500 index ...' -> 'NIFTY 500' (or None):
    single-index announcements name the index in the sentence instead of a heading."""
    if not re.search(r"following\s+changes?|decided\s+to\s+make", text, re.I):
        return None
    for m in _INDEX_IN_TEXT.finditer(text):
        key = index_key(m.group(1))                 # the whole name: "Nifty India Digital" is not NIFTY 50
        if key:
            return key
    return None


def _effective(text):
    """The first 'effective from <date>' / 'w.e.f. <date>' in a text -> date; '(close of trading on X)'
    without another date means the next weekday."""
    m = re.search(r"(?:effective\s+(?:from|on|as\s+of)?|w\.?\s?e\.?\s?f\.?|with\s+effect\s+from)\s*:?\s*"
                  r"(?:the\s+)?(close\s+of\s+(?:trading\s+(?:hours\s+)?)?(?:on\s+)?)?" + _DATE, text, re.I)
    if not m:
        return None
    mon = MONTHS.get(m.group(2)[:3].lower())
    if not mon:
        return None
    try:
        d = datetime.date(int(m.group(4)), mon, int(m.group(3)))
    except ValueError:
        return None
    if m.group(1):
        d += datetime.timedelta(days=1)
        while d.weekday() >= 5:
            d += datetime.timedelta(days=1)
    return d


def _row(line):
    """A table row -> (name, symbol or '') or None. Rows start with their serial number."""
    s = line.strip()
    m = re.match(r"^(\d{1,3})(?:\s+|\t)(.+)$", s)
    if not m or re.match(r"^\d{1,3}\.", s):
        return None
    rest = m.group(2)
    cells = [c.strip() for c in rest.split("\t") if c.strip()]
    if len(cells) >= 3 and re.match(r"^-[A-Z0-9]{1,3}$", cells[-1]):
        cells[-2:] = [cells[-2] + cells[-1]]              # "MCDOWELL\t-N": one symbol in two pieces
    sym = ""
    last = cells[-1].replace(" ", "") if len(cells) >= 2 and cells[-1].upper() == cells[-1] else ""
    if last and _SYMBOL.match(last) and last not in _NOT_SYMBOL:
        sym, name = last, " ".join(cells[:-1])       # "HIMATSEID E": a symbol split by the PDF writer
    else:
        words = rest.replace("\t", " ").split()
        if len(words) >= 2 and _SYMBOL.match(words[-1]) and words[-1] not in _NOT_SYMBOL and \
                any(ch.islower() for ch in " ".join(words[:-1])):
            sym, name = words[-1], " ".join(words[:-1])
        else:
            name = " ".join(words)
    if not re.search(r"[A-Za-z]{2}", name):
        return None
    return name, sym


def parse_announcement(lines, title="", announced=None):
    """The text lines of one press release -> [{"index", "action" ("exclude"|"include"), "name", "symbol",
    "effective"}] for the indices we track. The title's 'w.e.f.' date wins over the text's."""
    eff_doc = _effective(title) or None
    current, action, table = None, None, False
    out = []
    eff = eff_doc
    joined_prev = ""
    for n, raw in enumerate(lines):
        if raw == "\f":
            continue
        line = raw.strip()
        nxt = next((l for l in lines[n + 1:n + 3] if l.strip() and l != "\f"), "")
        flat = line.replace("\t", " ")
        e = _effective(joined_prev + " " + flat)
        if e and not eff_doc:
            eff = e
        h = _heading(line) or _bare_heading(line, nxt)
        if h:
            current, action, table = h, None, False
            joined_prev = flat
            continue
        sent = _sentence_index(joined_prev + " " + flat)
        if sent and current in (None, "other") and not table:
            current = sent
        low = flat.lower()
        if "being excluded" in low or re.search(r"\bexclusions?\b.*:$", low):
            action, table = "exclude", True
        elif "being included" in low or re.search(r"\binclusions?\b.*:$", low):
            action, table = "include", True
        elif re.match(r"^sr\.?\s*no", low):
            table = action is not None
        elif table and action and current not in (None, "other"):
            r = _row(line)
            if r:
                out.append({"index": current, "action": action, "name": r[0], "symbol": r[1],
                            "effective": (eff or announced or "") and str(eff or announced)})
            elif _SYMBOL.match(flat) and flat not in _NOT_SYMBOL and out and not out[-1]["symbol"]:
                out[-1]["symbol"] = flat                      # the symbol cell fell on its own line
            elif re.match(r"^[A-Z0-9&]+\s*-[A-Z0-9]{1,3}$", flat) and out:
                whole = flat.replace(" ", "")                 # "MCDOWELL -N" after a row that said MCDOWELL
                for o in reversed(out):
                    if o["symbol"] and whole.startswith(o["symbol"]) and o["symbol"] != whole:
                        o["symbol"] = whole
                        break
            elif out and re.search(r"[a-z]", flat) and len(flat.split()) >= 2:
                words = flat.split()
                same = [o for o in out if o["index"] == current and o["action"] == action and
                        _norm_name(o["name"]) == _norm_name(" ".join(words[:-1]))]
                if same and _SYMBOL.match(words[-1]) and not same[-1]["symbol"]:
                    same[-1]["symbol"] = words[-1]           # a second copy of the row, with its symbol
        if low.startswith(("note", "the above", "no inclusion", "no exclusion", "no change", "place:", "date:")):
            table = False
        joined_prev = flat
    if not out:
        out = parse_index_list(lines, title, announced)[0]
    seen, uniq = {}, []
    for o in out:
        k = (o["index"], o["action"], o["symbol"] or _norm_name(o["name"]))
        if k in seen:
            continue
        alt = (o["index"], o["action"], _norm_name(o["name"]))
        if alt in seen and o["symbol"]:
            seen[alt]["symbol"] = seen[alt]["symbol"] or o["symbol"]
            continue
        seen[k] = o
        seen[alt] = o
        uniq.append(o)
    return uniq


_PAREN_SYM = re.compile(r"\(\s*([A-Z][A-Z0-9&\- ]{1,24}?)\s*\)")


def parse_index_list(lines, title="", announced=None):
    """The demerger format: one company excluded from (or a dummy symbol included in) every index in a
    'Sr. No. / Index Name' table. -> (changes, aliases {dummy: real symbol})."""
    flat = [l.replace("\t", " ").strip() for l in lines if l != "\f"]
    text = " ".join(flat)
    indices, in_table = [], False
    for l in flat:
        if re.match(r"^sr\.?\s*no\.?\s+index name", l, re.I):
            in_table = True
            continue
        if in_table:
            m = re.match(r"^\d{1,3}\s+(.+)$", l)
            if m:
                key = index_key(re.sub(r"\s+(index|indices)$", "", m.group(1), flags=re.I))
                if key and key not in indices:
                    indices.append(key)
                continue
            if not re.match(r"^sr\.?\s*no", l, re.I):
                in_table = False
    aliases = {}
    dummies = []
    for d in re.findall(r"\bDUMMY\s?[A-Z0-9]{2,12}\b", text):
        d = d.replace(" ", "")
        if d not in dummies:
            dummies.append(d)
    if not indices:
        return [], aliases
    out = []
    ex = re.search(r"decided\s+to\s+exclude(.{0,300}?)from\s+(?:various|the\s+following|following)", text, re.I)
    if ex:
        lead = text.rfind("In accordance", 0, ex.start())
        part = text[lead if lead >= 0 else ex.start():ex.end()]
        syms = []
        for x in _PAREN_SYM.findall(part) + re.findall(r"exclude\s+([A-Z][A-Z0-9&\-]{1,19})\s+from", part):
            x = x.replace(" ", "")
            if _SYMBOL.match(x) and x not in syms and x not in ("NSE", "BSE") and not x.startswith("DUMMY"):
                syms.append(x)
        listed = re.search(r"after the demerger,(.{0,400}?)(?:was|were|w ere)\s+listed", text, re.I)
        if listed and dummies:
            real = [x.replace(" ", "") for x in _PAREN_SYM.findall(listed.group(1))]
            if len(real) == len(dummies):
                aliases.update(dict(zip(dummies, real)))
        eff = _effective(text[ex.start():]) or _effective(title)
        # "... had announced inclusion of X ... effective from <date>": the spun-off company joined the
        # indices that day (with a dummy symbol, or none at all before about 2024)
        joined = _effective(text[:ex.start()]) if re.search(r"had\s+announced\s+(?:the\s+)?inclusion", text, re.I) \
            else None
        for key in indices:
            for sym in syms:
                out.append({"index": key, "action": "exclude", "name": "", "symbol": sym,
                            "effective": str(eff or announced or "")})
                if joined and eff and joined < eff:
                    out.append({"index": key, "action": "include", "name": "", "symbol": sym,
                                "effective": str(joined), "spun_off": True})
    elif dummies:
        inc = re.search(r"(?:shall|will)\s+be\s+included", text, re.I)
        if inc:
            eff = _effective(text[inc.start():]) or _effective(title)
            for key in indices:
                for d in dummies:
                    out.append({"index": key, "action": "include", "name": "", "symbol": d,
                                "effective": str(eff or announced or ""), "dummy": True})
    return out, aliases


def announcement_changes(pdf_bytes, title="", announced=None):
    return parse_announcement(pdf_lines(pdf_bytes), title, announced)


# ---------------------------------------------------------------- reference tables

def parse_constituents(text):
    """ind_nifty50list.csv -> [{"symbol", "name", "isin"}]."""
    rows = [r for r in (l.split(",") for l in text.splitlines()) if r]
    if not rows or "Symbol" not in [c.strip() for c in rows[0]]:
        raise ValueError("not an index constituent file")
    head = [c.strip() for c in rows[0]]
    import csv
    import io
    out = []
    for rec in csv.DictReader(io.StringIO(text)):
        rec = {k.strip(): (v or "").strip() for k, v in rec.items() if k}
        if rec.get("Symbol"):
            out.append({"symbol": rec["Symbol"], "name": rec.get("Company Name", ""), "isin": rec.get("ISIN Code", "")})
    del head
    return out


def parse_symbol_changes(text):
    """symbolchange.csv -> [{"old", "new", "date"}] oldest first (padded cells trimmed)."""
    import csv
    import io
    out = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 4 or r[1].strip().upper() == "OLD SYMBOL":
            continue
        old, new, d = r[1].strip().upper(), r[2].strip().upper(), r[3].strip()
        m = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$", d)
        if not (old and new and m):
            continue
        out.append({"old": old, "new": new,
                    "date": datetime.date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1))).isoformat()})
    out.sort(key=lambda x: x["date"])
    return out


def parse_equity_list(text):
    """EQUITY_L.csv -> {normalized company name: symbol}."""
    import csv
    import io
    out = {}
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 2 or r[0].strip().upper() == "SYMBOL":
            continue
        out.setdefault(_norm_name(r[1]), r[0].strip().upper())
    return out


class Symbols(object):
    """Symbol identity over time from symbolchange.csv: canon() follows renames forward to today's symbol,
    as_of() walks back to the symbol a stock traded under on a day."""

    def __init__(self, changes, aliases=None):
        self.fwd = {}
        self.back = {}
        self.alias = dict(aliases or {})       # a demerger's dummy symbol -> the symbol it listed under
        for c in changes:
            self.fwd.setdefault(c["old"], []).append((c["date"], c["new"]))
            self.back.setdefault(c["new"], []).append((c["date"], c["old"]))

    def canon(self, sym, day="0000-00-00"):
        s, d, hops = sym.upper(), day, 0
        s = self.alias.get(s, s)
        while hops < 12:
            nxt = [(dt, n) for dt, n in self.fwd.get(s, []) if dt > d]
            if not nxt:
                return s
            dt, s = min(nxt)
            d, hops = dt, hops + 1
        return s

    def as_of(self, sym, day):
        s, hops = sym.upper(), 0
        while hops < 12:
            prev = [(dt, o) for dt, o in self.back.get(s, []) if dt > day]
            if not prev:
                return s
            dt, s = max(prev)
            hops += 1
        return s


# ---------------------------------------------------------------- reconstruction

def revised_dates(lines):
    """'... shall be effective November 18, 2013 instead of November 15, 2013' -> (new, old) ISO dates or None."""
    text = " ".join(l.replace("\t", " ") for l in lines)
    m = re.search(r"effective\s+(?:from\s+)?" + _DATE + r"\s+instead\s+of\s+" + _DATE, text, re.I)
    if not m:
        return None
    new, old = month_day(" ".join(m.group(1, 2, 3))), month_day(" ".join(m.group(4, 5, 6)))
    return (new.isoformat(), old.isoformat()) if new and old else None


def drop_superseded(changes, revisions):
    """Drop the changes a later announcement moved to another day: revisions is [(source, new, old)]."""
    out = changes
    for source, new, old in revisions:
        moved = set((c["index"], c["action"], c["symbol"]) for c in changes if c.get("source") == source)
        out = [c for c in out if not (c["effective"] == old and c.get("source") != source and
                                      (c["index"], c["action"], c["symbol"]) in moved)]
    return out


def drop_voided(changes):
    """Changes minus those a later announcement cancelled (VOIDED), by their "source" PDF name."""
    return [c for c in changes if c.get("source") not in VOIDED or c["index"] in VOIDED[c["source"]]]


def resolve_names(changes, names):
    """Fill missing symbols from {normalized name: symbol}. Returns the names still unresolved."""
    missing = []
    for c in changes:
        if c["symbol"]:
            continue
        n = _norm_name(c["name"])
        sym = names.get(n)
        if not sym:
            # a looser match: the same words without the last one ("Sesa Goa" vs "Sesa Goa Ltd")
            cands = [v for k, v in names.items() if k and (k.startswith(n + " ") or n.startswith(k + " "))]
            sym = cands[0] if len(set(cands)) == 1 else None
        if sym:
            c["symbol"], c["resolved_by"] = sym, "name"
        else:
            missing.append({"index": c["index"], "effective": c["effective"], "name": c["name"],
                            "action": c["action"]})
    return missing


def rebuild(index, current, snapshot_date, changes, symbols):
    """Membership intervals of one index, newest first, from today's list and the announced changes.

    current: today's symbols; snapshot_date: the day that list was fetched (ISO); changes: parsed
    announcements (all indices; only `index` is used); symbols: a Symbols. Returns
    {"intervals": [{"from", "to", "members"(canonical)}], "issues": [...]}: "from" is the first day of
    the composition, "to" the last (None for the open interval). Changes dated after the snapshot are
    applied forwards (announced, not yet effective)."""
    evs = {}
    for c in changes:
        if c["index"] != index or not c["symbol"] or not c["effective"]:
            continue
        canon = symbols.canon(c["symbol"], c["effective"])
        evs.setdefault(c["effective"], {"include": set(), "exclude": set()})[c["action"]].add(canon)
    for d in evs:                                   # a stock in and out on the same day cancels
        both = evs[d]["include"] & evs[d]["exclude"]
        evs[d]["include"] -= both
        evs[d]["exclude"] -= both
    state = set(symbols.canon(s, snapshot_date) for s in current)
    issues = []
    future = sorted(d for d in evs if d > snapshot_date)
    past = sorted((d for d in evs if d <= snapshot_date), reverse=True)
    intervals = []
    fwd_state, fwd = set(state), []
    for d in future:
        missing_out = sorted(evs[d]["exclude"] - fwd_state)
        if missing_out:
            issues.append({"date": d, "kind": "excluded but not a member", "symbols": missing_out})
        fwd_state = (fwd_state - evs[d]["exclude"]) | evs[d]["include"]
        fwd.append((d, sorted(fwd_state)))
    for i in range(len(fwd) - 1, -1, -1):
        to = _day_before(fwd[i + 1][0]) if i + 1 < len(fwd) else None
        intervals.append({"from": fwd[i][0], "to": to, "members": fwd[i][1]})
    upper = _day_before(future[0]) if future else None
    for d in past:
        intervals.append({"from": d, "to": upper, "members": sorted(state)})
        not_in = sorted(evs[d]["include"] - state)
        already = sorted(evs[d]["exclude"] & state)
        if not_in:
            issues.append({"date": d, "kind": "included then but not in the later list", "symbols": not_in})
        if already:
            issues.append({"date": d, "kind": "excluded then but in the later list", "symbols": already})
        state = (state - evs[d]["include"]) | evs[d]["exclude"]
        upper = _day_before(d)
    intervals.append({"from": None, "to": upper, "members": sorted(state)})
    size = SIZES.get(index)
    for iv in intervals:
        if size and len(iv["members"]) != size:
            issues.append({"date": iv["from"] or "start", "kind": "size", "count": len(iv["members"]), "expected": size,
                           "until": iv["to"]})
    return {"intervals": intervals, "issues": issues}


def _day_before(iso_day):
    return (datetime.date.fromisoformat(iso_day) - datetime.timedelta(days=1)).isoformat()


def members_on(intervals, day, symbols=None):
    """The members on `day` (ISO), as the symbols they traded under that day."""
    for iv in intervals:
        if (iv["from"] is None or iv["from"] <= day) and (iv["to"] is None or day <= iv["to"]):
            mem = iv["members"]
            return sorted(symbols.as_of(s, day) for s in mem) if symbols else list(mem)
    return []
