"""A dated F&O lot-size history: which lot each contract carried on each day, and how sure we are of it.

NSE publishes today's lot sizes only (fo_mktlots.csv), and its legacy F&O bhavcopies (to July 2024) carry no
lot column, so an option backtest of 2010 to 2023 has to rebuild the lots. The sources, best first:

  bhavcopy      the UDiFF F&O bhavcopy's NewBrdLotQty (2024-01-01 on): every contract, every day   exact
  mkt-activity  NSE's F&O market activity report (archives/fo/mkt/foDDMMYYYY.zip, from March 2011):
                traded quantity / number of contracts of every contract that traded that day        exact
  mktlots       a fo_mktlots.csv snapshot: the archive's own daily copies, and the copies the
                Internet Archive's Wayback Machine kept of NSE's page (2008 to 2024)                 exact
  circular      NSE's index lot circulars ("Revision in Market Lot of Derivative Contracts on
                Indices", NSE/FAOP/...): the present lot on the circular's date, and which
                expiries took the revised lot                                                        exact
  bridged       two exact observations of the same underlying with the same lot, and every contract
                in between rebuilt to that same lot (index: up to 400 days apart, stock: 35)       exact
  rebuilt       the legacy bhavcopy's own numbers: notional turnover / (contracts x price) brackets
                the lot (the price lies between the day's low and high; for an option the notional
                is (strike + premium) x quantity), and the lot divides every open interest and OI
                change (both are in shares). The integers inside the bracket that divide the OI's
                greatest common divisor are the candidates; one candidate over the contract's
                life is the lot                                                                       reconstructed

A contract's days are cut into segments wherever the candidates change (a bonus or split adjusts the lot
mid-life). Pure functions: archive.py reads the files and passes the rows in.
"""

import bisect
import csv
import datetime
import io
import math
import re

EXACT, RECONSTRUCTED, NONE = "exact", "reconstructed", "none"
TABLE_COLS = ["underlying", "expiry", "monthly", "effective_from", "effective_to", "lot", "source", "confidence"]
MKT_COLS = ["underlying", "expiry", "lot", "contracts"]
SNAP_COLS = ["underlying", "symbol", "month", "lot"]
INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "NIFTYIT", "CNXIT", "NFTYMCAP50",
                     "NIFTYMID50", "NIFTYINFRA", "CNXINFRA", "NIFTYPSE", "CNXPSE", "NIFTYCPSE", "MINIFTY", "JUNIOR",
                     "CNX100", "NIFTYFPI", "DJIA", "S&P500", "FTSE100", "INDIAVIX", "BANKEX", "SENSEX"}
BRIDGE_DAYS = {"index": 400, "stock": 35}
TURNOVER_SLACK = 500.0     # the legacy bhavcopy rounds turnover to 0.01 lakh: +-500 rupees
TURNOVER_TOL = 0.005       # and a few trades (spread orders) print outside the day's low-high: +-0.5%
MAX_BRACKET = 20000        # a row's bracket wider than this says nothing (skipped)
MKT_FROM = datetime.date(2011, 3, 1)   # the oldest F&O market activity report NSE serves (2011-02 is 404)

# NSE's index lot circulars, read from the PDFs at nsearchives.nseindia.com/content/circulars/FAOP<n>.pdf.
# present: the lot every live contract had on the circular's date. new: the revised lot, for expiries from
# monthly_from (monthly contracts) and weekly_from (weekly ones, default the same); every contract from all_from
# when given. before_all: what the contracts at or after the threshold carried before all_from ("old": the
# old lot; "either": the far month was introduced with the new lot while longer-dated ones kept the old).
# 2024-01-01 onward the UDiFF bhavcopy carries every contract's lot, so later circulars are listed for the
# record (present lots) without their expiry rules.
INDEX_CIRCULARS = [
    # (underlying, circular, date, present, new, monthly_from, weekly_from, all_from, before_all)
    # 2014 revisions moved "all contracts" from a date, but NIFTY's long-dated options kept 50 (fo_mktlots of
    # 2014-11-11 shows Mar-15 onward at 50; NSE/FAOP/30450 moved them "from 50 to 75" in 2015): present lots only
    ("CNXIT", "NSE/FAOP/26336", "2014-03-31", 50, 25, None, None, None, None),
    ("NIFTY", "NSE/FAOP/27733", "2014-09-30", 50, 25, None, None, None, None),
    ("NFTYMCAP50", "NSE/FAOP/27733", "2014-09-30", 150, 75, None, None, None, None),
    ("NIFTY", "NSE/FAOP/30449", "2015-08-07", 25, 75, "2015-11-26", None, "2015-09-28", "either"),
    ("BANKNIFTY", "NSE/FAOP/30449", "2015-08-07", 25, 30, "2015-11-26", None, None, "old"),
    ("NFTYMCAP50", "NSE/FAOP/30449", "2015-08-07", 75, 150, "2015-11-26", None, None, "old"),
    ("NIFTY", "NSE/FAOP/32134", "2016-03-31", 75, 75, None, None, None, None),
    ("BANKNIFTY", "NSE/FAOP/32134", "2016-03-31", 30, 40, "2016-07-28", None, None, "old"),
    ("NIFTY", "NSE/FAOP/33317", "2016-09-30", 75, 75, None, None, None, None),
    ("BANKNIFTY", "NSE/FAOP/33317", "2016-09-30", 40, 40, None, None, None, None),
    ("NIFTY", "NSE/FAOP/34531", "2017-03-31", 75, 75, None, None, None, None),
    ("BANKNIFTY", "NSE/FAOP/34531", "2017-03-31", 40, 40, None, None, None, None),
    ("NIFTY", "NSE/FAOP/35977", "2017-09-29", 75, 75, None, None, None, None),
    ("BANKNIFTY", "NSE/FAOP/35977", "2017-09-29", 40, 40, None, None, None, None),
    ("NIFTY", "NSE/FAOP/37375", "2018-03-28", 75, 75, None, None, None, None),
    ("BANKNIFTY", "NSE/FAOP/37375", "2018-03-28", 40, 40, None, None, None, None),
    ("NIFTY", "NSE/FAOP/44039", "2020-03-31", 75, 75, None, None, None, None),
    ("BANKNIFTY", "NSE/FAOP/44039", "2020-03-31", 20, 25, "2020-07-30", None, None, "old"),
    ("NIFTY", "NSE/FAOP/47854", "2021-03-31", 75, 50, "2021-07-29", "2021-08-05", "2021-06-25", "either"),
    ("BANKNIFTY", "NSE/FAOP/47854", "2021-03-31", 25, 25, None, None, None, None),
    ("FINNIFTY", "NSE/FAOP/47854", "2021-03-31", 40, 40, None, None, None, None),
    ("NIFTY", "NSE/FAOP/56233", "2023-03-31", 50, 50, None, None, None, None),
    ("BANKNIFTY", "NSE/FAOP/56233", "2023-03-31", 25, 15, "2023-07-27", "2023-08-03", "2023-06-30", "either"),
    ("FINNIFTY", "NSE/FAOP/56233", "2023-03-31", 40, 40, None, None, None, None),
    ("MIDCPNIFTY", "NSE/FAOP/56233", "2023-03-31", 75, 75, None, None, None, None),
    ("NIFTY", "NSE/FAOP/61415", "2024-04-02", 50, 25, "2024-05-30", "2024-05-02", "2024-04-26", "old"),
    ("BANKNIFTY", "NSE/FAOP/61415", "2024-04-02", 15, 15, None, None, None, None),
    ("FINNIFTY", "NSE/FAOP/61415", "2024-04-02", 40, 25, None, None, None, None),
    ("MIDCPNIFTY", "NSE/FAOP/61415", "2024-04-02", 75, 50, None, None, None, None),
    ("NIFTY", "NSE/FAOP/64625", "2024-10-18", 25, 75, None, None, None, None),
    ("BANKNIFTY", "NSE/FAOP/64625", "2024-10-18", 15, 30, None, None, None, None),
    ("FINNIFTY", "NSE/FAOP/64625", "2024-10-18", 25, 65, None, None, None, None),
    ("MIDCPNIFTY", "NSE/FAOP/64625", "2024-10-18", 50, 120, None, None, None, None),
    ("NIFTYNXT50", "NSE/FAOP/64625", "2024-10-18", 10, 25, None, None, None, None),
    ("NIFTY", "NSE/FAOP/67372", "2025-03-28", 75, 75, None, None, None, None),
    ("BANKNIFTY", "NSE/FAOP/67372", "2025-03-28", 30, 35, None, None, None, None),
    ("NIFTY", "NSE/FAOP/70616", "2025-10-03", 75, 65, None, None, None, None),
    ("BANKNIFTY", "NSE/FAOP/70616", "2025-10-03", 35, 30, None, None, None, None),
]


def _day(s):
    return datetime.date.fromisoformat(str(s)[:10])


def _num(x):
    try:
        return float(x) if x not in ("", None) else None
    except ValueError:
        return None


def kind_of(underlying):
    return "index" if underlying.upper() in INDEX_UNDERLYINGS else "stock"


# ---------------------------------------------------------------- sources: parsing

def parse_mktlots_any(text):
    """Any fo_mktlots.csv NSE has published -> [[underlying, symbol, YYYY-MM or "", lot]]. Two layouts:
    one lot per expiry month (2014 on: 'UNDERLYING,SYMBOL,Nov-14,Dec-14,...', padded cells) and the older
    single column ('Underlying,Symbol,Market Lot'), whose month is "" (every live contract)."""
    out, months = [], None
    for row in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in row]
        if len(cells) < 3:
            continue
        if cells[1].upper() == "SYMBOL":
            months = []
            for c in cells[2:]:
                m = re.match(r"^([A-Za-z]{3})-(\d\d)$", c)
                if m and m.group(1).upper() in _MONTHS:
                    months.append("20%s-%02d" % (m.group(2), _MONTHS.index(m.group(1).upper()) + 1))
                elif re.match(r"^market\s*lot", c, re.I):
                    months.append("")
                else:
                    months.append(None)
            continue
        if months is None:
            continue
        for month, lot in zip(months, cells[2:]):
            v = _num(lot)
            if month is not None and v and v > 0 and v == int(v):
                out.append([cells[0], cells[1].upper(), month, int(v)])
    return out


_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _mkt_date(s):
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_mkt_activity(files):
    """NSE's F&O market activity report: {name: text} of the zip's CSVs -> [[underlying, expiry, lot,
    contracts]], one row a contract that traded (futures from fo<date>.csv, options from op<date>.csv). The
    lot is the traded quantity over the number of contracts; a contract whose quantity is not a whole number
    of contracts is left out. Rows of one (underlying, expiry) agree, or the pair is left out."""
    seen = {}
    bad = set()
    for name, text in sorted(files.items()):
        base = name.lower().rsplit("/", 1)[-1]
        if not re.match(r"^(fo|op)\d{8}\.csv$", base):
            continue
        rows = list(csv.reader(io.StringIO(text)))
        if not rows:
            continue
        head = [h.strip().upper() for h in rows[0]]
        try:
            i_sym, i_exp, i_qty, i_con = (head.index("SYMBOL"), head.index("EXP_DATE"), head.index("TRD_QTY"),
                                          head.index("NO_OF_CONT"))
        except ValueError:
            continue
        for r in rows[1:]:
            if len(r) <= max(i_sym, i_exp, i_qty, i_con):
                continue
            q, c = _num(r[i_qty]), _num(r[i_con])
            exp = _mkt_date(r[i_exp])
            if not q or not c or c <= 0 or not exp:
                continue
            lot = q / c
            if lot != int(lot):
                continue
            k = (r[i_sym].strip().upper(), exp)
            if k in seen and seen[k][0] != int(lot):
                bad.add(k)
            n = seen.get(k, (0, 0))[1]
            seen[k] = (int(lot), n + int(c))
    return sorted([u, e, v[0], v[1]] for (u, e), v in seen.items() if (u, e) not in bad)


def parse_circular_rows(lines):
    """Lot rows out of the text of an NSE lot circular (history.pdftext lines): '1<TAB>NIFTY 50<TAB>NIFTY<TAB>
    75<TAB>65' -> (symbol, present, revised); a 'no change' table row gives (symbol, present, None). The symbol
    is the last all-capitals word before the numbers."""
    out = []
    for line in lines:
        toks = [t for t in re.split(r"[\t ]+", line.strip()) if t]
        nums = []
        while toks and re.match(r"^\d+$", toks[-1]) and len(nums) < 2:
            nums.insert(0, int(toks.pop()))
        if not nums or len(toks) < 2 or not re.match(r"^\d+$", toks[0]):
            continue
        sym = toks[-1]
        if not re.match(r"^[A-Z][A-Z0-9&]{2,15}$", sym) or sym in ("LOT", "INDEX"):
            continue
        out.append((sym, nums[0], nums[1] if len(nums) == 2 else None))
    return out


# ---------------------------------------------------------------- the circular rules

def _circs(underlying):
    return [c for c in INDEX_CIRCULARS if c[0] == underlying]


def circular_present(underlying):
    """[(date, lot, circular)]: the lot every live contract of the index had on each circular's date."""
    return [(c[2], c[3], c[1]) for c in _circs(underlying)]


def circular_lots(underlying, expiry, day, monthly=True):
    """(set of lots NSE's circulars allow for this contract on `day`, the circular) or (None, None) when no
    circular covers it. Contracts before a revision's threshold keep the present lot; contracts at or after it
    carry the new lot, except before all_from where longer-dated ones may still have the old one."""
    cs = [c for c in _circs(underlying) if c[4] != c[3] and (c[5] or c[7])]
    if not cs:
        return None, None
    first = min(c[2] for c in _circs(underlying))
    if day < first:
        return None, None
    lots, circ = None, None
    for (u, cno, cdate, old, new, mfrom, wfrom, allf, before) in cs:
        thr = mfrom if monthly else (wfrom or mfrom)
        if thr is None:                 # every contract switches on all_from
            if day >= allf:
                lots, circ = {new}, cno
            elif day >= cdate:
                lots, circ = {old}, cno
            continue
        if expiry < thr:
            if day >= cdate:
                lots, circ = {old}, cno
            continue
        if allf and day >= allf:
            lots, circ = {new}, cno
        elif allf and before == "old":
            lots, circ = {old}, cno
        elif allf:
            lots, circ = {old, new}, cno
        else:
            lots, circ = {new}, cno
    return lots, circ


# ---------------------------------------------------------------- the bhavcopy scan (legacy votes, UDiFF lots)

SCAN_COLS = ["kind", "underlying", "expiry", "monthly", "first", "last", "votes", "days", "observed", "lot"]
INF = float("inf")
SPLIT_AFTER = 3          # sessions in a row without the leading lot before a segment is cut (a bonus or split)
LEAD_SHARE = 0.8         # the leading candidate must be in this share of a segment's observed sessions
TOP_VOTES = 8            # candidates kept per segment


def day_contracts(rows):
    """One F&O bhavcopy day's rows (archive FO_COLS order) -> {(underlying, expiry): info}. info: "lot" (the
    UDiFF lot column, else None), "cands" (the lot candidates that day: the integers inside every traded row's
    bracket turnover / (contracts x (strike + high)) .. turnover / (contracts x (strike + low)), widened for
    the turnover's rounding, that divide the gcd of the day's open interest and OI changes; [] when nothing
    traded), "monthly" (a future with that expiry exists)."""
    acc = {}
    for r in rows:
        if len(r) < 16 or not r[2]:
            continue
        sym, kind, exp = r[0], r[1], r[2]
        b = acc.get((sym, exp))
        if b is None:
            b = acc[(sym, exp)] = {"lot": None, "rows": [], "g": 0, "monthly": False}
        if kind in ("IDF", "STF"):
            b["monthly"] = True
        lot = _num(r[15])
        if lot:
            b["lot"] = int(lot)
            continue
        for i in (11, 12):
            v = _num(r[i])
            if v:
                b["g"] = math.gcd(b["g"], abs(int(v)))
        c, v, hi, lo = _num(r[13]), _num(r[14]), _num(r[6]), _num(r[7])
        if not c or not v or not hi or not lo or c <= 0 or lo <= 0 or hi < lo:
            continue
        strike = (_num(r[3]) or 0.0) if kind in ("IDO", "STO") else 0.0
        lo_b = (v - TURNOVER_SLACK) / c / (strike + hi) * (1 - TURNOVER_TOL)
        hi_b = (v + TURNOVER_SLACK) / c / (strike + lo) * (1 + TURNOVER_TOL)
        if hi_b - lo_b <= MAX_BRACKET:
            b["rows"].append((c, lo_b, hi_b))
    out = {}
    for k, b in acc.items():
        cands = []
        if b["lot"] is None and b["rows"]:
            lo = max(x[1] for x in b["rows"])
            hi = min(x[2] for x in b["rows"])
            if lo > hi:                  # rows that disagree (a stray trade): the most traded row decides
                best = max(b["rows"])
                lo, hi = best[1], best[2]
            cands = candidates(lo, hi, b["g"])
            if not cands and b["g"]:
                # open interest that is no whole number of lots: positions carried through an index lot
                # revision (long-dated contracts); the OI says nothing about the lot that day
                cands = candidates(lo, hi, 0)
        out[k] = {"lot": b["lot"], "cands": cands, "monthly": b["monthly"]}
    return out


def candidates(lo, hi, g, limit=64):
    """The integers in [lo, hi] that divide g (any when g is 0); [] when more than `limit` would (the
    bracket says nothing) or there is no upper bound."""
    if lo is None or hi is None or hi == INF or hi < 1:
        return []
    a, b = max(1, int(math.ceil(lo - 1e-9))), int(math.floor(hi + 1e-9))
    if g == 0 and b - a + 1 > limit:
        return []
    out = []
    for n in range(a, b + 1):
        if g == 0 or g % n == 0:
            out.append(n)
            if len(out) > limit:
                return []
    return out


def leader(votes, observed):
    """(the lot, [the candidates still in play]) of a segment's votes {lot: sessions}: the lot is the one
    candidate in at least LEAD_SHARE of the observed sessions (None when none or several are)."""
    if not observed or not votes:
        return None, []
    need = LEAD_SHARE * observed
    top = sorted((n for n, v in votes.items() if v >= need), key=lambda n: (-votes[n], n))
    return (top[0] if len(top) == 1 else None), top


def _votes_text(votes):
    return ";".join("%d:%d" % (n, v) for n, v in sorted(votes.items(), key=lambda x: (-x[1], x[0]))[:TOP_VOTES])


def parse_votes(text):
    out = {}
    for part in str(text or "").split(";"):
        if ":" in part:
            n, v = part.split(":", 1)
            out[int(n)] = int(v)
    return out


class Scanner(object):
    """Feed it the bhavcopy one session at a time, oldest first; rows() -> SCAN_COLS rows. Kind "X": a run of
    days a contract's UDiFF lot stayed the same (exact). Kind "R": a run of legacy days and the votes of each
    lot candidate (sessions it was a candidate in). SPLIT_AFTER sessions in a row without the leading lot cut
    the segment there (a bonus or split adjusted the contract's lot mid-life); fewer are stray days."""

    def __init__(self):
        self.x, self.r, self.done = {}, {}, []

    def feed(self, day, rows):
        for k, b in day_contracts(rows).items():
            if b["lot"] is not None:
                s = self.x.get(k)
                if s is not None and s["lot"] != b["lot"]:
                    self.done.append(self._xrow(k, s))
                    s = None
                if s is None:
                    s = self.x[k] = {"first": day, "last": day, "lot": b["lot"], "m": False, "days": 0}
                s["last"], s["m"], s["days"] = day, s["m"] or b["monthly"], s["days"] + 1
                continue
            s = self.r.get(k)
            if s is None:
                s = self.r[k] = self._new(day)
            s["m"] = s["m"] or b["monthly"]
            dc = b["cands"]
            if not dc:
                s["last"], s["days"] = day, s["days"] + 1
                continue
            lead, _ = leader(s["votes"], s["obs"])
            if lead is not None and s["obs"] >= SPLIT_AFTER and lead not in dc:
                s["miss"].append((day, dc))
                if len(s["miss"]) >= SPLIT_AFTER and not any(lead in c for _, c in s["miss"]):
                    missed = s["miss"]
                    s["miss"] = []
                    self.done.append(self._rrow(k, s))
                    s = self.r[k] = self._new(missed[0][0])
                    s["m"] = b["monthly"]
                    for d, c in missed:
                        self._vote(s, d, c)
                continue
            for d, c in s["miss"]:           # stray days: they still count
                self._vote(s, d, c)
            s["miss"] = []
            self._vote(s, day, dc)

    @staticmethod
    def _new(day):
        return {"first": day, "last": day, "votes": {}, "m": False, "days": 0, "obs": 0, "miss": []}

    @staticmethod
    def _vote(s, day, dc):
        for n in dc:
            s["votes"][n] = s["votes"].get(n, 0) + 1
        s["obs"] += 1
        s["days"] += 1
        s["last"] = max(s["last"], day)

    @staticmethod
    def _xrow(k, s):
        return ["X", k[0], k[1], "M" if s["m"] else "W", s["first"], s["last"], "", s["days"], s["days"], s["lot"]]

    def _rrow(self, k, s):
        for d, c in s.get("miss") or []:
            self._vote(s, d, c)
        return ["R", k[0], k[1], "M" if s["m"] else "W", s["first"], s["last"], _votes_text(s["votes"]), s["days"],
                s["obs"], ""]

    def rows(self):
        out = list(self.done)
        out += [self._xrow(k, s) for k, s in self.x.items()]
        out += [self._rrow(k, s) for k, s in self.r.items()]
        return sorted(out, key=lambda r: (r[1], r[2], r[4], r[0]))


def merge_scan(rows):
    """SCAN_COLS rows of consecutive years (each year scanned alone) -> one list, a contract's pieces joined
    across the year boundary: X runs of the same lot, R runs with the same leading lot (or one unobserved)."""
    out = []
    last = {}
    for r in sorted(rows, key=lambda x: (x[1], x[2], x[0], x[4])):
        r = list(r)
        key = (r[0], r[1], r[2])
        p = last.get(key)
        if p is not None:
            if r[0] == "X" and str(p[9]) == str(r[9]):
                p[5], p[7], p[8] = r[5], int(p[7]) + int(r[7]), int(p[8]) + int(r[8])
                p[3] = "M" if "M" in (p[3], r[3]) else "W"
                continue
            if r[0] == "R":
                pv, rv = parse_votes(p[6]), parse_votes(r[6])
                pl, rl = leader(pv, int(p[8]))[0], leader(rv, int(r[8]))[0]
                if not int(p[8]) or not int(r[8]) or (pl is not None and pl == rl):
                    for n, v in rv.items():
                        pv[n] = pv.get(n, 0) + v
                    p[5], p[6] = r[5], _votes_text(pv)
                    p[7], p[8] = int(p[7]) + int(r[7]), int(p[8]) + int(r[8])
                    p[3] = "M" if "M" in (p[3], r[3]) else "W"
                    continue
        out.append(r)
        last[key] = r
    return sorted(out, key=lambda r: (r[1], r[2], r[4], r[0]))


# ---------------------------------------------------------------- the table

def _next_session(sessions, d):
    i = bisect.bisect_left(sessions, d)
    return sessions[i] if i < len(sessions) else d


def build_table(scan, mkt, snaps, sessions):
    """The lot table (TABLE_COLS rows, sorted by underlying, expiry, effective_from) and a report.

    scan: merged SCAN_COLS rows. mkt: [(day, underlying, expiry, lot)] from the market activity reports.
    snaps: [(day, underlying, "YYYY-MM" or "", lot, source)] from fo_mktlots snapshots. sessions: the
    archive's F&O sessions, sorted (a snapshot taken on a weekend counts for the next session)."""
    report = {"conflicts": [], "segments": 0, EXACT: 0, RECONSTRUCTED: 0, NONE: 0, "by_source": {}}
    by_u = {}
    for r in scan:
        u, e = r[1], r[2]
        if r[0] == "X":
            s = {"e": e, "m": r[3] == "M", "from": r[4], "to": r[5], "cands": [int(r[9])], "lot": int(r[9]),
                 "src": "NSE F&O bhavcopy (NewBrdLotQty)", "conf": EXACT}
        else:
            lot, cands = leader(parse_votes(r[6]), int(r[8]))
            s = {"e": e, "m": r[3] == "M", "from": r[4], "to": r[5], "cands": cands,
                 "lot": lot, "conf": RECONSTRUCTED if lot is not None else NONE,
                 "src": "rebuilt from the legacy bhavcopy (turnover / contracts x price, open interest)"}
        by_u.setdefault(u, {}).setdefault(e, []).append(s)
    for u in by_u:
        for e in by_u[u]:
            by_u[u][e].sort(key=lambda s: s["from"])

    def seg_on(u, e, d):
        for s in by_u.get(u, {}).get(e, ()):
            if s["from"] <= d <= s["to"]:
                return s
        return None

    def confirm(u, s, lot, src, d, strict=False):
        """An exact observation of this contract's lot. strict (a circular's general statement, not about this
        contract): only taken when the bhavcopy agrees."""
        if s["conf"] == EXACT:
            if s["lot"] != lot and not strict:
                report["conflicts"].append([u, s["e"], d, s["lot"], lot, src])
            return
        if strict and lot not in s["cands"]:
            return
        if s["cands"] and lot not in s["cands"]:
            report["conflicts"].append([u, s["e"], d, "/".join(str(c) for c in s["cands"][:4]), lot, src])
        s["lot"], s["conf"], s["src"], s["cands"] = lot, EXACT, src, [lot]

    for d, u, e, lot in mkt:
        s = seg_on(u, e, d)
        if s is not None:
            confirm(u, s, lot, "NSE F&O market activity report %s (traded quantity / contracts)" % d, d)
    for d, u, month, lot, src in snaps:
        d2 = _next_session(sessions, d)
        for e, ss in by_u.get(u, {}).items():
            if month and e[:7] != month:
                continue
            s = seg_on(u, e, d2)
            if s is not None and s["m"]:
                confirm(u, s, lot, "%s %s" % (src, d), d2)
    for u, es in by_u.items():
        if kind_of(u) != "index":
            continue
        pres = [(_next_session(sessions, cd), lot, cno, cd) for cd, lot, cno in circular_present(u)]
        for ss in es.values():
            for s in ss:
                for d2, lot, cno, cd in pres:
                    if s["from"] <= d2 <= s["to"]:
                        confirm(u, s, lot, "NSE circular %s of %s (the present lot)" % (cno, cd), d2, strict=True)
                if s["conf"] == EXACT:
                    continue
                a, cno = circular_lots(u, s["e"], s["from"], s["m"])
                b, _ = circular_lots(u, s["e"], s["to"], s["m"])
                if a and len(a) == 1 and a == b:
                    lot = next(iter(a))
                    if lot in s["cands"]:
                        s["lot"], s["conf"], s["cands"] = lot, EXACT, [lot]
                        s["src"] = "NSE circular %s (revision rule; the bhavcopy agrees)" % cno
    for u, es in by_u.items():
        _bridge(u, [s for ss in es.values() for s in ss])
    rows = []
    for u in sorted(by_u):
        for e in sorted(by_u[u]):
            for s in by_u[u][e]:
                if s["lot"] is None:
                    s["conf"] = NONE
                    s["src"] = ("rebuilt: candidates %s" % "/".join(str(c) for c in s["cands"][:6])) if s["cands"] \
                        else "no trade to rebuild the lot from"
                report[s["conf"]] += 1
                key = source_kind(s["src"])
                report["by_source"][key] = report["by_source"].get(key, 0) + 1
                rows.append([u, e, "M" if s["m"] else "W", s["from"], s["to"], "" if s["lot"] is None else s["lot"],
                             s["src"], s["conf"]])
    report["segments"] = len(rows)
    return rows, report


def source_kind(src):
    """A table row's source -> its short name (bhavcopy, mkt-activity, mktlots, wayback, circular, bridged,
    rebuilt, none)."""
    s = src.lower()
    for key, name in (("bridged", "bridged"), ("bhavcopy (newbrdlotqty)", "bhavcopy"), ("market activity", "mkt-activity"),
                      ("wayback", "wayback"), ("fo_mktlots", "mktlots"), ("circular", "circular"),
                      ("rebuilt from", "rebuilt")):
        if key in s:
            return name
    return "none"


def _bridge(u, ss):
    """Mark a segment exact when the nearest exact segments of the same underlying before and after it carry
    the same lot within BRIDGE_DAYS, and every segment with a known lot in between agrees."""
    limit = BRIDGE_DAYS[kind_of(u)]
    ex = sorted((s for s in ss if s["conf"] == EXACT), key=lambda s: s["from"])
    if not ex:
        return
    starts = [s["from"] for s in ex]
    by_end = sorted(ex, key=lambda s: s["to"])
    ends = [s["to"] for s in by_end]
    known = sorted((s["from"], s["lot"]) for s in ss if s["lot"] is not None)
    kfrom = [k[0] for k in known]
    for s in ss:
        if s["conf"] == EXACT:
            continue
        i = bisect.bisect_right(starts, s["from"]) - 1
        j = bisect.bisect_left(ends, s["to"])
        if i < 0 or j >= len(by_end):
            continue
        a, b = ex[i], by_end[j]
        if a["lot"] != b["lot"] or (s["cands"] and a["lot"] not in s["cands"]):
            continue
        if (_day(b["to"]) - _day(a["from"])).days > limit:
            continue
        lo, hi = bisect.bisect_left(kfrom, a["from"]), bisect.bisect_right(kfrom, b["to"])
        if any(k[1] != a["lot"] for k in known[lo:hi]):
            continue
        s["lot"], s["conf"], s["cands"] = a["lot"], EXACT, [a["lot"]]
        s["src"] = "bridged between two exact lots of %d (%s; %s); every contract in between rebuilt to %d" % (
            a["lot"], source_kind(a["src"]), source_kind(b["src"]), a["lot"])


# ---------------------------------------------------------------- reading the table

def pick(rows, day, expiry=None):
    """The table row for a contract on `day`, from rows of one underlying (TABLE_COLS). expiry given: that
    contract's segment holding the day, else its segment nearest in time. expiry None: the nearest monthly
    contract alive that day (the smallest monthly expiry on or after it), else any contract alive."""
    if expiry:
        mine = [r for r in rows if r[1] == expiry]
        for r in mine:
            if r[3] <= day <= r[4]:
                return r
        if not mine:
            return None
        return min(mine, key=lambda r: abs((_day(r[3] if r[3] > day else r[4]) - _day(day)).days))
    alive = [r for r in rows if r[3] <= day <= r[4] and r[1] >= day]
    if not alive:
        return None
    mon = [r for r in alive if r[2] == "M"]
    return min(mon or alive, key=lambda r: (r[1], r[3]))


def coverage(rows, start, end, monthly_only=False):
    """{year: {"exact", "reconstructed", "none", "total"}} in contract-days (calendar days of each segment
    inside start..end), with the whole window under "all"."""
    out = {}
    a, b = _day(start), _day(end)
    for r in rows:
        if monthly_only and r[2] != "M":
            continue
        f, t = max(_day(r[3]), a), min(_day(r[4]), b)
        conf = r[7] if r[7] in (EXACT, RECONSTRUCTED) else NONE
        while f <= t:
            ye = min(t, datetime.date(f.year, 12, 31))
            n = (ye - f).days + 1
            for k in (str(f.year), "all"):
                c = out.setdefault(k, {EXACT: 0, RECONSTRUCTED: 0, NONE: 0, "total": 0})
                c[conf] += n
                c["total"] += n
            f = ye + datetime.timedelta(days=1)
    return out
