#!/usr/bin/env python3
"""Option chain analytics for trader-options (NSE). Standard library only.

Reads NSE's option chain for one underlying and one expiry and works out the
numbers an options trader looks at: the forward from put-call parity, our own
implied volatility (Black-76) and Greeks for every strike, PCR, max pain,
call and put walls, OI build-up, the ATM straddle and expected move, the IV
smile, and IV rank / percentile from India VIX history (indices).

    python3 options.py expiries NIFTY
    python3 options.py chain NIFTY                      # nearest expiry, 15 strikes each side
    python3 options.py chain NIFTY --expiry 01-Dec-2026 --strikes 10
    python3 options.py chain RELIANCE --write           # -> trader/options.json
    python3 options.py pcr NIFTY
    python3 options.py maxpain NIFTY
    python3 options.py oi NIFTY                         # long/short build-up, unwinding, covering
    python3 options.py iv NIFTY                         # smile, ATM IV, skew, IV rank
    python3 options.py chain NIFTY --json               # machine-readable

Sources (public NSE pages, no key; a browser-like User-Agent and the
nseindia.com Referer are sent, with a pause between calls):
  https://www.nseindia.com/api/option-chain-contract-info?symbol=X
      the exchange's expiry list (never computed locally) and strikes
  https://www.nseindia.com/api/option-chain-v3?type=Indices|Equity&symbol=X&expiry=DD-Mon-YYYY
      one expiry per call; openInterest, changeinOpenInterest and
      totalTradedVolume are in CONTRACTS (lots); bid/ask quantities in units
  https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv
      lot size per underlying per expiry month
  https://www.nseindia.com/api/historicalOR/vixhistory?from=DD-MM-YYYY&to=DD-MM-YYYY
      India VIX closes (at most 70 rows a call, so fetched in 90-day chunks)
  https://www.nseindia.com/api/allIndices                 India VIX now

Pricing: Black-76 on the forward F of the expiry. F comes from put-call
parity at the strikes nearest the money (F = K + e^(rT) x (C - P), median),
so the cash index freezing at 15:30 while options trade to 15:40 does not
skew the numbers. T is calendar time from the chain's timestamp to 15:30 IST
on expiry day. r is trader/config.json "risk_free_rate" (default 0.0557).
NSE's own impliedVolatility field is kept as nse_iv but never used.

Every public function below is pure (no network, no files) and returns plain
dicts, lists and numbers, so another program can import this file and call
them directly. --offline DIR reads saved responses instead of the network;
--save DIR stores what was fetched under the same names.
"""

import argparse
import bisect
import csv
import datetime as _dt
import gzip
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_HALF_UP, Decimal

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
PAISE = Decimal("0.01")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130 Safari/537.36")
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")

SCHEMA = "bharat-trader/options/1"
INDICES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50")
DEFAULT_RATE = 0.0557
RATE_SOURCE = ("default 5.57%: 91-day T-bill cut-off yield 5.5747% at the 08-Oct-2026 RBI auction "
               "(secondary source); set risk_free_rate in trader/config.json")
EXPIRY_TIME = _dt.time(15, 30)          # options stop trading / settle at 15:30 IST on expiry day
CASH_CLOSE = _dt.time(15, 30)           # the cash index stops moving here
DERIV_CLOSE = _dt.time(15, 40)          # equity derivatives trade until 15:40 (since 03-Aug-2026)
MIN_YEARS = 1.0 / (365 * 24)            # under an hour to expiry: IV and Greeks are not meaningful
IV_LOW, IV_HIGH = 0.001, 5.0            # solver bounds: 0.1% to 500% a year
MAX_SPREAD = 0.30                       # use the bid/ask mid when the spread is within 30% of it
FWD_SPREAD = 0.10                       # strikes used for the parity forward need a tighter quote
ONE_SD_PER_STRADDLE = math.sqrt(math.pi / 2)   # E|X| = 0.798 sigma, so 1 SD = 1.2533 x straddle

BASE = "https://www.nseindia.com/api/"
CONTRACT_URL = BASE + "option-chain-contract-info?symbol=%s"
CHAIN_URL = BASE + "option-chain-v3?type=%s&symbol=%s&expiry=%s"
LOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
VIX_URL = BASE + "historicalOR/vixhistory?from=%s&to=%s"
INDICES_URL = BASE + "allIndices"


class OptionsError(Exception):
    pass


# ---------------------------------------------------------------- formatting

def _group(whole):
    """'12345678' -> '1,23,45,678' (Indian grouping: thousand, lakh, crore)."""
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail])


def inr(x):
    """123456.5 -> '₹1,23,456.50' (Indian digit grouping), -1234 -> '-₹1,234.00'."""
    d = Decimal(str(x)).quantize(PAISE, rounding=ROUND_HALF_UP)
    sign = "-" if d < 0 else ""
    whole, frac = format(abs(d), "f").split(".")
    return "%s₹%s.%s" % (sign, _group(whole), frac)


def qty(n):
    """6601900 -> '66,01,900'; None -> '-'."""
    if n is None:
        return "-"
    n = int(round(n))
    return ("-" if n < 0 else "") + _group(str(abs(n)))


def signed(n):
    """1234 -> '+1,234', -5 -> '-5', None -> '-'."""
    if n is None:
        return "-"
    return ("+" if n > 0 else "") + qty(n)


def fnum(x, places=2):
    """23020.7 -> '23,020.70' (Indian grouping, fixed places); None -> '-'."""
    if x is None:
        return "-"
    d = Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    sign = "-" if d < 0 else ""
    whole, _, frac = format(abs(d), "f").partition(".")
    return sign + _group(whole) + ("." + frac if frac else "")


def _r(x, places):
    return None if x is None else round(x, places)


def _num(v):
    """NSE number or string -> float; 0, '', '-', None stay as given (0.0 / None)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace(",", "")
    if t in ("", "-", "NA"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


# ---------------------------------------------------------------- dates

def now_ist():
    return _dt.datetime.now(IST).replace(tzinfo=None)


def parse_nse_date(s):
    """'03-Nov-2026', '03-NOV-2026', '03-11-2026', '2026-11-03' -> date (or None)."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s.title() if "%b" in fmt else s, fmt).date()
        except ValueError:
            continue
    return None


def parse_nse_time(s):
    """'30-Oct-2026 15:40:00' -> naive IST datetime (or None)."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M"):
        try:
            return _dt.datetime.strptime(s.title() if "%b" in fmt else s, fmt)
        except ValueError:
            continue
    d = parse_nse_date(s)
    return _dt.datetime.combine(d, _dt.time(0, 0)) if d else None


def nse_expiry(d):
    """date -> '03-Nov-2026' (the form the chain URL takes)."""
    return d.strftime("%d-%b-%Y")


def years_to_expiry(expiry, asof):
    """Calendar time in years from asof (naive IST datetime) to 15:30 IST on the expiry date.

    Minutes / 525,600, never whole days: a weekly priced at 4 days vs 3.5 days
    moves IV by more than a point. Returns 0.0 once expiry has passed.
    """
    if isinstance(expiry, str):
        expiry = parse_nse_date(expiry)
    end = _dt.datetime.combine(expiry, EXPIRY_TIME)
    minutes = (end - asof).total_seconds() / 60.0
    return max(0.0, minutes / 525600.0)


# ---------------------------------------------------------------- Black-76

def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1d2(F, K, T, sigma):
    vt = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / vt
    return d1, d1 - vt


def black76_price(F, K, T, r, sigma, kind):
    """Black-76 price of a European option on forward F.

    kind is "CE" (call) or "PE" (put); T in years; r and sigma as decimals
    (0.0557, 0.12). At T or sigma of zero it returns the discounted intrinsic.
    """
    kind = kind.upper()
    df = math.exp(-r * T)
    if T <= 0 or sigma <= 0:
        return df * max(0.0, (F - K) if kind == "CE" else (K - F))
    d1, d2 = _d1d2(F, K, T, sigma)
    if kind == "CE":
        return df * (F * _ncdf(d1) - K * _ncdf(d2))
    return df * (K * _ncdf(-d2) - F * _ncdf(-d1))


def black76_vega_raw(F, K, T, r, sigma):
    """dPrice/dSigma per 1.00 of sigma (not per vol point)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1, _ = _d1d2(F, K, T, sigma)
    return math.exp(-r * T) * F * _npdf(d1) * math.sqrt(T)


def black76_iv(price, F, K, T, r, kind, low=IV_LOW, high=IV_HIGH, tol=1e-8, max_iter=100):
    """Implied volatility (decimal, 0.1205 = 12.05%) from a Black-76 price, or None.

    Safeguarded Newton inside a bisection bracket [low, high]: a Newton step
    that leaves the bracket or stalls falls back to bisection, so it cannot
    diverge on deep out-of-the-money or near-expiry options. Returns None when
    the price is not tradeable: missing, at or below discounted intrinsic, at
    or above the no-arbitrage ceiling, beyond the bounds, or T under an hour.
    """
    if price is None or F is None or K is None or T is None:
        return None
    if price <= 0 or F <= 0 or K <= 0 or T < MIN_YEARS:
        return None
    kind = kind.upper()
    df = math.exp(-r * T)
    intrinsic = df * max(0.0, (F - K) if kind == "CE" else (K - F))
    ceiling = df * (F if kind == "CE" else K)
    if price <= intrinsic + 1e-10 or price >= ceiling:
        return None
    f_low = black76_price(F, K, T, r, low, kind) - price
    f_high = black76_price(F, K, T, r, high, kind) - price
    if f_low > 0 or f_high < 0:
        return None                      # outside [0.1%, 500%]: treat as untradeable
    lo, hi = low, high
    sigma = math.sqrt(2.0 * abs(math.log(F / K)) / T) if F != K else 0.2
    sigma = min(max(sigma, 0.05), 1.0) if not (lo < sigma < hi) else sigma
    for _ in range(max_iter):
        diff = black76_price(F, K, T, r, sigma, kind) - price
        if abs(diff) < tol:
            return sigma
        if diff > 0:
            hi = sigma
        else:
            lo = sigma
        vega = black76_vega_raw(F, K, T, r, sigma)
        step = sigma - diff / vega if vega > 1e-12 else None
        sigma = step if step is not None and lo < step < hi else 0.5 * (lo + hi)
        if hi - lo < 1e-10:
            return sigma
    return sigma


def black76_greeks(F, K, T, r, sigma, kind):
    """Black-76 Greeks per unit of the underlying, as a dict.

    delta  dPrice/dF (forward delta; e^-rT N(d1) for a call, -e^-rT N(-d1) for a put)
    gamma  d2Price/dF2, per 1 point of F
    theta  price change per CALENDAR day as time passes (F and sigma fixed), negative for a long
    vega   price change per 1 vol point (sigma +0.01)
    Multiply by the lot size and lots for a position. None values when T < 1 hour.
    """
    kind = kind.upper()
    if sigma is None or T < MIN_YEARS or sigma <= 0:
        return {"delta": None, "gamma": None, "theta": None, "vega": None}
    df = math.exp(-r * T)
    d1, _ = _d1d2(F, K, T, sigma)
    pdf = _npdf(d1)
    sqt = math.sqrt(T)
    price = black76_price(F, K, T, r, sigma, kind)
    delta = df * _ncdf(d1) if kind == "CE" else -df * _ncdf(-d1)
    gamma = df * pdf / (F * sigma * sqt)
    theta_year = -df * F * pdf * sigma / (2.0 * sqt) + r * price
    vega = df * F * pdf * sqt / 100.0
    return {"delta": delta, "gamma": gamma, "theta": theta_year / 365.0, "vega": vega}


# ---------------------------------------------------------------- parsing

def parse_contract_info(data):
    """contract-info JSON -> {"expiries": ["2026-11-03", ...] (sorted), "strikes": [floats]}."""
    if not isinstance(data, dict):
        raise OptionsError("contract-info is not a JSON object")
    exp = sorted({d for d in (parse_nse_date(x) for x in data.get("expiryDates") or []) if d})
    strikes = sorted({v for v in (_num(s) for s in data.get("strikePrice") or []) if v})
    return {"expiries": [d.isoformat() for d in exp], "strikes": strikes}


def parse_lots(text):
    """fo_mktlots.csv text -> {"NIFTY": {"2026-11": 65, "2026-12": 65, ...}, ...}.

    The file is fixed-width padded (every cell is stripped), has an index block
    and then a second header row for stocks; month columns read like NOV-26.
    """
    out, months = {}, None
    for row in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in row]
        if len(cells) < 3:
            continue
        if cells[1].upper() == "SYMBOL":
            months = []
            for c in cells[2:]:
                try:
                    months.append(_dt.datetime.strptime(c.title(), "%b-%y").strftime("%Y-%m"))
                except ValueError:
                    months.append(None)
            continue
        if months is None:
            continue
        sym = cells[1].upper()
        lots = {}
        for m, c in zip(months, cells[2:]):
            v = _num(c)
            if m and v:
                lots[m] = int(v)
        if sym and lots:
            out[sym] = lots
    return out


def lot_size(lots, symbol, expiry):
    """Lot size for symbol in the expiry's month from parse_lots() output, or None."""
    if isinstance(expiry, str):
        expiry = parse_nse_date(expiry)
    row = lots.get(symbol.upper()) or {}
    return row.get(expiry.strftime("%Y-%m")) if expiry else None


def _side(raw):
    """One CE or PE dict from v3 -> our side dict (units: OI and volume in contracts)."""
    if not isinstance(raw, dict):
        return None
    bid, ask = _num(raw.get("buyPrice1")) or 0.0, _num(raw.get("sellPrice1")) or 0.0
    return {
        "ltp": _num(raw.get("lastPrice")) or 0.0,
        "change": _num(raw.get("change")) or 0.0,
        "bid": bid, "ask": ask,
        "bid_qty": int(_num(raw.get("buyQuantity1")) or 0),
        "ask_qty": int(_num(raw.get("sellQuantity1")) or 0),
        "oi": int(_num(raw.get("openInterest")) or 0),
        "oi_chg": int(_num(raw.get("changeinOpenInterest")) or 0),
        "volume": int(_num(raw.get("totalTradedVolume")) or 0),
        "nse_iv": _num(raw.get("impliedVolatility")) or None,
        "identifier": raw.get("identifier"),
    }


def parse_chain(data, symbol=None, expiry=None):
    """option-chain-v3 JSON -> normalised chain dict.

    {"symbol", "expiry" (ISO), "timestamp" (ISO, IST), "spot", "rows": [{"strike",
    "ce": side|None, "pe": side|None}], "totals": {"ce_oi", "pe_oi", "ce_volume",
    "pe_volume"} (NSE's own, contracts), "expiries": [ISO]}.
    Rows are sorted by strike. Raises OptionsError on an empty chain (a wrong
    expiry returns HTTP 200 with no rows and spot 0).
    """
    if not isinstance(data, dict) or not isinstance(data.get("records"), dict):
        raise OptionsError("the option chain has no 'records' (NSE changed the format, or blocked us)")
    rec = data["records"]
    rows = []
    exp = parse_nse_date(expiry) if expiry else None
    for item in rec.get("data") or []:
        k = _num(item.get("strikePrice"))
        if not k:
            continue
        d = parse_nse_date(item.get("expiryDates") or item.get("expiryDate"))
        if exp and d and d != exp:
            continue
        exp = exp or d
        rows.append({"strike": k, "ce": _side(item.get("CE")), "pe": _side(item.get("PE"))})
        if symbol is None:
            symbol = (item.get("CE") or item.get("PE") or {}).get("underlying")
    rows.sort(key=lambda x: x["strike"])
    spot = _num(rec.get("underlyingValue"))
    if not rows or not spot:
        raise OptionsError("NSE returned an empty chain%s (wrong expiry? check `expiries`)" % (
            " for " + nse_expiry(exp) if exp else ""))
    filt = data.get("filtered") or {}
    ce, pe = filt.get("CE") or {}, filt.get("PE") or {}
    ts = parse_nse_time(rec.get("timestamp"))
    return {
        "symbol": (symbol or "").upper(),
        "expiry": exp.isoformat() if exp else None,
        "timestamp": ts.isoformat() if ts else None,
        "spot": spot,
        "rows": rows,
        "totals": {"ce_oi": ce.get("totOI"), "pe_oi": pe.get("totOI"),
                   "ce_volume": ce.get("totVol"), "pe_volume": pe.get("totVol")},
        "expiries": [d.isoformat() for d in sorted({x for x in (parse_nse_date(s) for s in
                                                                 rec.get("expiryDates") or []) if x})],
    }


def parse_vix_history(data):
    """vixhistory JSON (one chunk or a list of chunks) -> [{"date": ISO, "close": float}] sorted, unique."""
    chunks = data if isinstance(data, list) else [data]
    seen = {}
    for ch in chunks:
        for row in (ch or {}).get("data") or []:
            d = parse_nse_date(row.get("EOD_TIMESTAMP"))
            c = _num(row.get("EOD_CLOSE_INDEX_VAL"))
            if d and c:
                seen[d.isoformat()] = c
    return [{"date": d, "close": seen[d]} for d in sorted(seen)]


def vix_now(all_indices):
    """allIndices JSON -> {"last", "previous_close", "timestamp"} for INDIA VIX, or None."""
    for row in (all_indices or {}).get("data") or []:
        if str(row.get("index", "")).upper() == "INDIA VIX":
            return {"last": _num(row.get("last")), "previous_close": _num(row.get("previousClose")),
                    "timestamp": all_indices.get("timestamp")}
    return None


# ---------------------------------------------------------------- analytics

def mid_price(side, max_spread=MAX_SPREAD):
    """(price, source) for one side: the bid/ask mid when both are quoted and the spread
    is within max_spread of the mid, else the LTP if it traded today, else (None, None)."""
    if not side:
        return None, None
    bid, ask = side.get("bid") or 0.0, side.get("ask") or 0.0
    if bid > 0 and ask >= bid:
        mid = 0.5 * (bid + ask)
        if (ask - bid) <= max_spread * mid:
            return mid, "mid"
    if (side.get("ltp") or 0) > 0 and (side.get("volume") or 0) > 0:
        return side["ltp"], "ltp"
    return None, None


def forward_from_parity(rows, spot, T, r, n=5):
    """Forward for the expiry from put-call parity: median of K + e^(rT)(C_mid - P_mid).

    Uses the n strikes nearest the money that have a tight two-sided quote on
    both the call and the put (spread within 10% of mid); falls back to
    traded LTPs, then to spot. Two passes: strikes are re-picked around the
    first estimate. Returns {"forward", "method", "strikes", "basis"}.
    """
    growth = math.exp(r * T)

    def estimate(centre, tight):
        pts = []
        for row in sorted(rows, key=lambda x: abs(x["strike"] - centre)):
            ce, pe = row.get("ce"), row.get("pe")
            if tight:
                c, cs = mid_price(ce, FWD_SPREAD)
                p, ps = mid_price(pe, FWD_SPREAD)
                if cs != "mid" or ps != "mid":
                    continue
            else:
                if not ce or not pe or not (ce["ltp"] > 0 and pe["ltp"] > 0 and ce["volume"] and pe["volume"]):
                    continue
                c, p = ce["ltp"], pe["ltp"]
            pts.append((row["strike"], row["strike"] + growth * (c - p)))
            if len(pts) >= n:
                break
        return pts

    for tight, method in ((True, "parity-mid"), (False, "parity-ltp")):
        pts = estimate(spot, tight)
        if pts:
            f = _median([x[1] for x in pts])
            pts = estimate(f, tight) or pts
            f = _median([x[1] for x in pts])
            return {"forward": f, "method": method, "strikes": [x[0] for x in pts],
                    "basis": f - spot}
    return {"forward": spot, "method": "spot", "strikes": [], "basis": 0.0}


def _median(xs):
    xs = sorted(xs)
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else 0.5 * (xs[m - 1] + xs[m])


def atm_strike(rows, forward):
    """The listed strike nearest the forward."""
    return min(rows, key=lambda x: (abs(x["strike"] - forward), x["strike"]))["strike"] if rows else None


def pcr(rows, atm=None, window=None):
    """Put-call ratios. -> {"oi", "volume", "oi_chg", "ce_oi", "pe_oi", "ce_volume", "pe_volume", "strikes"}.

    Over all rows, or the `window` strikes on each side of `atm`. Units are
    contracts on both sides, so the ratio does not depend on the lot size.
    """
    use = rows
    if atm is not None and window:
        ks = [x["strike"] for x in rows]
        i = ks.index(atm) if atm in ks else bisect.bisect_left(ks, atm)
        use = rows[max(0, i - window): i + window + 1]
    t = {"ce_oi": 0, "pe_oi": 0, "ce_volume": 0, "pe_volume": 0, "ce_oi_chg": 0, "pe_oi_chg": 0}
    for row in use:
        for s in ("ce", "pe"):
            side = row.get(s)
            if side:
                t[s + "_oi"] += side["oi"]
                t[s + "_volume"] += side["volume"]
                t[s + "_oi_chg"] += side["oi_chg"]
    out = dict(t)
    out["oi"] = _r(t["pe_oi"] / float(t["ce_oi"]), 4) if t["ce_oi"] else None
    out["volume"] = _r(t["pe_volume"] / float(t["ce_volume"]), 4) if t["ce_volume"] else None
    out["oi_chg"] = _r(t["pe_oi_chg"] / float(t["ce_oi_chg"]), 4) \
        if t["ce_oi_chg"] > 0 and t["pe_oi_chg"] > 0 else None
    out["strikes"] = [use[0]["strike"], use[-1]["strike"]] if use else None
    return out


def max_pain(rows, lot=1):
    """Max pain: the listed strike where option buyers, together, would be paid the least at expiry.

    pain(K*) = sum over strikes K of OI_call(K) x max(K* - K, 0) + OI_put(K) x max(K - K*, 0),
    with OI in contracts x lot = units, so pain is in rupees. Every listed strike
    counts, zero-OI ones included. -> {"strike", "pain", "curve": [{"strike", "pain"}]}.
    """
    pts = [(x["strike"], (x.get("ce") or {}).get("oi", 0), (x.get("pe") or {}).get("oi", 0)) for x in rows]
    if not pts:
        return {"strike": None, "pain": None, "curve": []}
    curve = []
    for k_star, _, _ in pts:
        total = 0.0
        for k, c_oi, p_oi in pts:
            if k < k_star:
                total += c_oi * (k_star - k)
            elif k > k_star:
                total += p_oi * (k - k_star)
        curve.append({"strike": k_star, "pain": total * lot})
    best = min(curve, key=lambda x: (x["pain"], x["strike"]))
    return {"strike": best["strike"], "pain": best["pain"], "curve": curve}


def walls(rows, forward=None, top=3):
    """Strikes with the most open interest: the call wall (resistance) and the put wall (support).

    With a forward, only out-of-the-money strikes count (calls at or above it, puts at
    or below it): deep in-the-money OI is old positions, not a level being defended.

    -> {"call": strike, "put": strike, "calls": [{"strike", "oi", "oi_chg"}], "puts": [...],
        "call_adds": [...], "put_adds": [...]} where *_adds rank by today's OI added.
    """
    def ranked(s, key):
        xs = [{"strike": x["strike"], "oi": x[s]["oi"], "oi_chg": x[s]["oi_chg"]}
              for x in rows if x.get(s) and x[s][key] > 0 and (
                  forward is None or (x["strike"] >= forward if s == "ce" else x["strike"] <= forward))]
        xs.sort(key=lambda d: (-d[key], d["strike"]))
        return xs[:top]

    calls, puts = ranked("ce", "oi"), ranked("pe", "oi")
    return {"call": calls[0]["strike"] if calls else None, "put": puts[0]["strike"] if puts else None,
            "calls": calls, "puts": puts,
            "call_adds": ranked("ce", "oi_chg"), "put_adds": ranked("pe", "oi_chg")}


BUILDUP = {
    (1, 1): "long build-up", (-1, 1): "short build-up",
    (-1, -1): "long unwinding", (1, -1): "short covering",
}


def classify_buildup(price_change, oi_change):
    """Price and OI change -> 'long build-up' (up, up), 'short build-up' (down, up),
    'long unwinding' (down, down), 'short covering' (up, down), or None if either is flat."""
    if not price_change or not oi_change:
        return None
    return BUILDUP[(1 if price_change > 0 else -1, 1 if oi_change > 0 else -1)]


def oi_buildup(rows, top=5):
    """OI build-up for each contract and a summary.

    -> {"contracts": [{"strike", "type", "change", "oi_chg", "oi", "buildup"}] (largest |OI change|
    first, `top` per type and build-up), "summary": {"CE": {build-up: OI change}, "PE": {...}}}.
    For options, short build-up in calls is call writing (sellers expect a cap) and
    short build-up in puts is put writing (sellers expect a floor).
    """
    items, summary = [], {"CE": {}, "PE": {}}
    for row in rows:
        for s, t in (("ce", "CE"), ("pe", "PE")):
            side = row.get(s)
            if not side:
                continue
            b = classify_buildup(side["change"], side["oi_chg"])
            if b is None:
                continue
            summary[t][b] = summary[t].get(b, 0) + side["oi_chg"]
            items.append({"strike": row["strike"], "type": t, "change": side["change"],
                          "oi_chg": side["oi_chg"], "oi": side["oi"], "buildup": b})
    items.sort(key=lambda d: (-abs(d["oi_chg"]), d["strike"]))
    kept, count = [], {}
    for d in items:
        key = (d["type"], d["buildup"])
        if count.get(key, 0) < top:
            kept.append(d)
            count[key] = count.get(key, 0) + 1
    return {"contracts": kept, "summary": summary}


def expected_move(straddle, forward, atm_iv, T, r=0.0):
    """One standard deviation move to expiry, two ways.

    From the ATM straddle: E|move| = straddle x e^(rT), and 1 SD = E|move| x sqrt(pi/2)
    (about 1.25 x straddle). From IV: F x sigma x sqrt(T), sigma decimal.
    -> {"straddle", "from_straddle", "from_iv", "move", "pct", "low", "high"}; move is the
    straddle figure when there is one.
    """
    fs = straddle * math.exp(r * T) * ONE_SD_PER_STRADDLE if straddle else None
    fi = forward * atm_iv * math.sqrt(T) if atm_iv and T > 0 else None
    m = fs if fs is not None else fi
    return {"straddle": _r(straddle, 2), "from_straddle": _r(fs, 2), "from_iv": _r(fi, 2),
            "move": _r(m, 2), "pct": _r(100.0 * m / forward, 3) if m else None,
            "low": _r(forward - m, 2) if m else None, "high": _r(forward + m, 2) if m else None}


def iv_rank(history, current=None, sessions=252):
    """IV rank and IV percentile of `current` against the last `sessions` values of history.

    history: [{"date", "close"}] or a list of numbers. current defaults to the last value.
    rank = (current - low) / (high - low) x 100; percentile = % of sessions closing below current.
    -> {"current", "rank", "percentile", "low", "high", "sessions", "from", "to"}.
    """
    vals = [(h["date"], h["close"]) if isinstance(h, dict) else (None, float(h)) for h in history or []]
    vals = vals[-sessions:]
    if not vals:
        return {"current": current, "rank": None, "percentile": None, "low": None, "high": None,
                "sessions": 0, "from": None, "to": None}
    xs = [v for _, v in vals]
    cur = current if current is not None else xs[-1]
    lo, hi = min(xs), max(xs)
    rank = 100.0 * (cur - lo) / (hi - lo) if hi > lo else None
    pct = 100.0 * sum(1 for x in xs if x < cur) / len(xs)
    return {"current": cur, "rank": _r(rank, 1), "percentile": _r(pct, 1), "low": lo, "high": hi,
            "sessions": len(xs), "from": vals[0][0], "to": vals[-1][0]}


def smile(rows, forward):
    """OTM implied vol per strike: put below the forward, call at or above. -> [{"strike", "type", "iv"}]."""
    out = []
    for row in rows:
        t = "pe" if row["strike"] < forward else "ce"
        side = row.get(t)
        if side and side.get("iv") is not None:
            out.append({"strike": row["strike"], "type": t.upper(), "iv": side["iv"]})
    return out


def skew_25d(rows):
    """25-delta risk reversal: IV of the ~-0.25 delta put minus the ~+0.25 delta call (vol points)."""
    def nearest(s, target):
        best = None
        for row in rows:
            side = row.get(s)
            if side and side.get("delta") is not None and side.get("iv") is not None:
                gap = abs(side["delta"] - target)
                if best is None or gap < best[0]:
                    best = (gap, row["strike"], side["iv"], side["delta"])
        return best if best and best[0] < 0.1 else None

    p, c = nearest("pe", -0.25), nearest("ce", 0.25)
    if not p or not c:
        return None
    return {"put_strike": p[1], "put_iv": p[2], "call_strike": c[1], "call_iv": c[2],
            "risk_reversal": _r(p[2] - c[2], 2)}


def spot_warnings(chain_time, now, spot_basis=None):
    """Plain-words warnings about stale data. chain_time and now are naive IST datetimes."""
    out = []
    if chain_time is None:
        return ["the chain has no timestamp; prices may be old"]
    t = chain_time.time()
    if CASH_CLOSE < t <= DERIV_CLOSE or (now.date() == chain_time.date()
                                         and CASH_CLOSE < now.time() <= DERIV_CLOSE):
        out.append("spot is the 15:30 cash close but options trade until 15:40, so spot is stale "
                   "against option prices; the parity forward is used for pricing%s" % (
                       " (forward - spot = %s)" % fnum(spot_basis) if spot_basis is not None else ""))
    if t < _dt.time(9, 15) and chain_time.weekday() < 5:
        out.append("chain time %s is before the 09:15 open: prices are the previous session's" %
                   chain_time.strftime("%H:%M"))
    if now.date() > chain_time.date():
        out.append("chain is from %s, the last session (market closed now)" %
                   chain_time.strftime("%d-%b-%Y %H:%M"))
    elif now.weekday() < 5 and _dt.time(9, 15) <= now.time() <= DERIV_CLOSE:
        age = (now - chain_time).total_seconds() / 60.0
        if age > 5:
            out.append("chain is %d minutes old" % age)
    return out


def analyse(chain, lot=None, r=DEFAULT_RATE, asof=None, now=None, rate_source=None):
    """The full analysis of one parsed chain: the options.json body (schema bharat-trader/options/1).

    chain: parse_chain() output. lot: lot size (units per contract) for this expiry.
    r: risk-free rate (decimal). asof: valuation time (naive IST); default the chain timestamp.
    now: wall clock for stale-data warnings (naive IST); default now.
    Every IV/Greek is computed by Black-76 on the parity forward; untradeable quotes get None.
    """
    rows_in = chain["rows"]
    ts = parse_nse_time(chain.get("timestamp")) if chain.get("timestamp") else None
    asof = asof or ts or now_ist()
    now = now or now_ist()
    expiry = parse_nse_date(chain["expiry"])
    T = years_to_expiry(expiry, asof)
    spot = chain["spot"]
    fwd = forward_from_parity(rows_in, spot, T, r)
    F = fwd["forward"]
    notes = []
    if T < MIN_YEARS:
        notes.append("under an hour to expiry: IV and Greeks are not meaningful; read premium vs intrinsic")

    rows = []
    for row in rows_in:
        out = {"strike": row["strike"]}
        for s in ("ce", "pe"):
            side = row.get(s)
            if not side:
                out[s] = None
                continue
            price, src = mid_price(side)
            sigma = black76_iv(price, F, row["strike"], T, r, s.upper()) if price else None
            g = black76_greeks(F, row["strike"], T, r, sigma, s.upper())
            d = {k: side[k] for k in ("ltp", "change", "bid", "ask", "bid_qty", "ask_qty",
                                      "oi", "oi_chg", "volume", "nse_iv")}
            d.update({
                "mid": _r(0.5 * (side["bid"] + side["ask"]), 4) if side["bid"] > 0 and side["ask"] > 0 else None,
                "price": price, "price_source": src,
                "iv": _r(sigma * 100, 2) if sigma else None,
                "delta": _r(g["delta"], 4), "gamma": _r(g["gamma"], 6),
                "theta": _r(g["theta"], 4), "vega": _r(g["vega"], 4),
                "buildup": classify_buildup(side["change"], side["oi_chg"]),
            })
            out[s] = d
        rows.append(out)

    atm = atm_strike(rows, F)
    arow = next((x for x in rows if x["strike"] == atm), None)
    ce_p = (arow.get("ce") or {}).get("price") if arow else None
    pe_p = (arow.get("pe") or {}).get("price") if arow else None
    straddle = ce_p + pe_p if ce_p and pe_p else None
    ce_iv = (arow.get("ce") or {}).get("iv") if arow else None
    pe_iv = (arow.get("pe") or {}).get("iv") if arow else None
    ivs = [v for v in (ce_iv, pe_iv) if v is not None]
    atm_iv = sum(ivs) / len(ivs) if ivs else None

    p_all, p_near = pcr(rows), pcr(rows, atm, 10)
    if chain.get("totals") and chain["totals"].get("ce_oi") not in (None, p_all["ce_oi"]):
        notes.append("NSE's chain totals (%s CE OI) differ from the sum of rows (%s)" % (
            qty(chain["totals"]["ce_oi"]), qty(p_all["ce_oi"])))
    mp = max_pain(rows, lot or 1)
    warnings = spot_warnings(ts, now, fwd["basis"] if fwd["method"] != "spot" else None)
    if fwd["method"] == "spot":
        warnings.append("no two-sided quotes near the money: forward = spot, IVs are rough")
    if lot is None:
        warnings.append("lot size not found in fo_mktlots.csv for this expiry month")
    minutes = T * 525600.0
    return {
        "schema": SCHEMA,
        "underlying": chain["symbol"],
        "kind": "index" if chain["symbol"] in INDICES else "stock",
        "expiry": chain["expiry"],
        "expiry_nse": nse_expiry(expiry),
        "days_to_expiry": (expiry - asof.date()).days,
        "minutes_to_expiry": int(round(minutes)),
        "t_years": _r(T, 8),
        "asof": asof.isoformat(timespec="seconds"),
        "spot": spot,
        "forward": _r(F, 2),
        "forward_method": fwd["method"],
        "forward_strikes": fwd["strikes"],
        "basis": _r(fwd["basis"], 2),
        "lot_size": lot,
        "rate": r,
        "rate_source": rate_source or RATE_SOURCE,
        "model": "Black-76 on the parity forward; theta per calendar day, vega per 1 vol point",
        "units": {"oi": "contracts", "oi_chg": "contracts", "volume": "contracts",
                  "bid_qty": "units", "ask_qty": "units", "iv": "percent a year",
                  "price": "rupees per unit", "pain": "rupees"},
        "atm": atm,
        "atm_iv": _r(atm_iv, 2),
        "atm_iv_ce": ce_iv, "atm_iv_pe": pe_iv,
        "straddle": {"strike": atm, "ce": ce_p, "pe": pe_p, "price": _r(straddle, 2),
                     "rupees_per_lot": _r(straddle * lot, 2) if straddle and lot else None},
        "expected_move": expected_move(straddle, F, atm_iv / 100.0 if atm_iv else None, T, r),
        "pcr_oi": p_all["oi"], "pcr_volume": p_all["volume"],
        "pcr": {"all": p_all, "near_atm": p_near},
        "max_pain": mp["strike"],
        "max_pain_detail": {"strike": mp["strike"], "pain": mp["pain"],
                            "distance": _r(mp["strike"] - F, 2) if mp["strike"] else None},
        "walls": walls(rows, F),
        "skew": skew_25d(rows),
        "rows": rows,
        "sources": {"chain_timestamp": chain.get("timestamp"), "spot": spot,
                    "chain": CHAIN_URL % ("Indices" if chain["symbol"] in INDICES else "Equity",
                                          urllib.parse.quote(chain["symbol"]), nse_expiry(expiry)),
                    "lots": LOTS_URL},
        "warnings": warnings,
        "notes": notes,
        "generated_at": now.isoformat(timespec="seconds"),
    }


def window_rows(rows, atm, n):
    """The n strikes on each side of atm (all rows when n is 0 or None)."""
    if not n:
        return rows
    ks = [x["strike"] for x in rows]
    i = ks.index(atm) if atm in ks else bisect.bisect_left(ks, atm)
    return rows[max(0, i - n): i + n + 1]


def vix_chunks(end, days=365, chunk=90):
    """Date ranges (start, end) covering `days` back from `end`, each at most `chunk` days
    (NSE returns at most 70 rows a call, about 3 months of sessions)."""
    start = end - _dt.timedelta(days=days)
    out = []
    while start <= end:
        stop = min(end, start + _dt.timedelta(days=chunk - 1))
        out.append((start, stop))
        start = stop + _dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    """Polite GET with retries and back-off for NSE; or reads saved files with offline=DIR."""

    def __init__(self, offline=None, save=None, pause=1.5, retries=3):
        self.offline, self.save, self.pause, self.retries, self._last = offline, save, pause, retries, 0.0

    def raw(self, url, name):
        if self.offline:
            for n in (name, name + ".gz"):
                path = os.path.join(self.offline, n)
                if os.path.isfile(path):
                    with open(path, "rb") as fh:
                        data = fh.read()
                    return gzip.decompress(data) if n.endswith(".gz") else data
            raise OptionsError("offline: no file %s" % os.path.join(self.offline, name))
        delay = 2.0
        for attempt in range(self.retries + 1):
            gap = time.time() - self._last
            if gap < self.pause:
                time.sleep(self.pause - gap)
            self._last = time.time()
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json, text/csv, */*",
                "Referer": "https://www.nseindia.com/", "Accept-Encoding": "gzip"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
                    if self.save:
                        os.makedirs(self.save, exist_ok=True)
                        with open(os.path.join(self.save, name), "wb") as fh:
                            fh.write(data)
                    return data
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise OptionsError("HTTP %s for %s" % (exc.code, url))
            except (urllib.error.URLError, OSError) as exc:
                if attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise OptionsError("network error for %s: %s" % (url, exc))
        raise OptionsError("gave up on %s" % url)

    def json(self, url, name):
        data = self.raw(url, name)
        try:
            return json.loads(data.decode("utf-8-sig"))
        except ValueError:
            raise OptionsError("NSE did not return JSON for %s (blocked, retired or down? try later)" % url)


def fetch_contract_info(fetcher, symbol):
    """-> parse_contract_info() for symbol (network or offline)."""
    return parse_contract_info(fetcher.json(CONTRACT_URL % urllib.parse.quote(symbol),
                                            "contract-info-%s.json" % symbol))


def fetch_chain(fetcher, symbol, expiry):
    """-> parse_chain() for symbol and expiry (a date or ISO string)."""
    if isinstance(expiry, str):
        expiry = parse_nse_date(expiry)
    kind = "Indices" if symbol in INDICES else "Equity"
    data = fetcher.json(CHAIN_URL % (kind, urllib.parse.quote(symbol), nse_expiry(expiry)),
                        "chain-%s-%s.json" % (symbol, nse_expiry(expiry)))
    return parse_chain(data, symbol, expiry.isoformat())


def fetch_lots(fetcher):
    data = fetcher.raw(LOTS_URL, "fo_mktlots.csv")
    return parse_lots(data.decode("utf-8-sig", errors="replace"))


def fetch_vix(fetcher, end):
    chunks = []
    for a, b in vix_chunks(end):
        f, t = a.strftime("%d-%m-%Y"), b.strftime("%d-%m-%Y")
        try:
            chunks.append(fetcher.json(VIX_URL % (f, t), "vixhistory-%s-%s.json" % (f, t)))
        except OptionsError:
            continue
    return parse_vix_history(chunks)


def fetch_vix_now(fetcher):
    try:
        return vix_now(fetcher.json(INDICES_URL, "allIndices.json"))
    except OptionsError:
        return None


def pick_expiry(expiries, wanted, now):
    """The expiry to use: `wanted` (must be in the exchange list) or the nearest one still trading."""
    dates = [parse_nse_date(e) for e in expiries]
    if wanted:
        d = parse_nse_date(wanted)
        if d is None or d not in dates:
            raise OptionsError("%s is not an expiry NSE lists. Listed: %s" % (
                wanted, ", ".join(nse_expiry(x) for x in dates[:8])))
        return d
    for d in dates:
        if _dt.datetime.combine(d, EXPIRY_TIME) > now:
            return d
    raise OptionsError("no expiry still trading in NSE's list")


def load_json(path, default):
    if not path or not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def risk_free_rate(config="trader/config.json"):
    """(rate, source) from trader/config.json "risk_free_rate" (decimal; 5.57 is read as 5.57%)."""
    cfg = load_json(config, {})
    v = cfg.get("risk_free_rate") if isinstance(cfg, dict) else None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return DEFAULT_RATE, RATE_SOURCE
    if v > 1:
        v /= 100.0
    if not 0 <= v < 0.5:
        return DEFAULT_RATE, RATE_SOURCE
    return v, "trader/config.json risk_free_rate"


# ---------------------------------------------------------------- output

def _iv(x):
    return "-" if x is None else "%.1f" % x


def _d(x):
    return "-" if x is None else "%+.2f" % x


def _px(x):
    return "-" if not x else fnum(x)


def render_header(a):
    lines = ["%s options, expiry %s (%d days, %s minutes to 15:30 IST), data %s" % (
        a["underlying"], a["expiry_nse"], a["days_to_expiry"], qty(a["minutes_to_expiry"]),
        a["asof"].replace("T", " ")[:16])]
    lines.append("Spot %s | forward %s (%s, %s vs spot) | lot %s | r %.2f%%" % (
        fnum(a["spot"]), fnum(a["forward"]), a["forward_method"], ("%+.2f" % a["basis"]),
        a["lot_size"] or "?", a["rate"] * 100))
    st, em = a["straddle"], a["expected_move"]
    if st["price"]:
        lines.append("ATM %s: straddle %s (%s per lot) | ATM IV %s%% (call %s, put %s) | "
                     "expected move +/-%s (%.2f%%): %s to %s" % (
                         fnum(a["atm"], 0), fnum(st["price"]),
                         inr(st["rupees_per_lot"]) if st["rupees_per_lot"] else "-",
                         _iv(a["atm_iv"]), _iv(a["atm_iv_ce"]), _iv(a["atm_iv_pe"]),
                         fnum(em["move"]), em["pct"] or 0, fnum(em["low"]), fnum(em["high"])))
    w = a["walls"]
    lines.append("PCR OI %s, volume %s | max pain %s | call wall %s, put wall %s" % (
        a["pcr_oi"] if a["pcr_oi"] is not None else "-", a["pcr_volume"] if a["pcr_volume"] is not None else "-",
        fnum(a["max_pain"], 0) if a["max_pain"] else "-",
        fnum(w["call"], 0) if w["call"] else "-", fnum(w["put"], 0) if w["put"] else "-"))
    return "\n".join(lines) + "\n"


def render_footer(a):
    lines = []
    for wtxt in a["warnings"]:
        lines.append("WARNING: " + wtxt)
    for n in a["notes"]:
        lines.append("NOTE: " + n)
    lines.append("OI and volume in contracts (1 contract = %s units). IV is ours (Black-76 on the forward), "
                 "not NSE's. Education, not advice." % (a["lot_size"] or "lot"))
    return "\n".join(lines) + "\n"


def render_chain(a, n):
    rows = window_rows(a["rows"], a["atm"], n)
    head = "%9s %9s %6s %6s %9s | %9s | %9s %6s %6s %9s %9s" % (
        "CE OI", "OI chg", "IV", "delta", "price", "strike", "price", "delta", "IV", "OI chg", "PE OI")
    out = [render_header(a), head, "-" * len(head)]
    w = a["walls"]
    for x in rows:
        ce, pe = x["ce"] or {}, x["pe"] or {}
        mark = "*" if x["strike"] == a["atm"] else " "
        tag = ""
        if x["strike"] == w["call"]:
            tag += " call wall"
        if x["strike"] == w["put"]:
            tag += " put wall"
        if x["strike"] == a["max_pain"]:
            tag += " max pain"
        out.append("%9s %9s %6s %6s %9s |%s%8s | %9s %6s %6s %9s %9s%s" % (
            qty(ce.get("oi")), signed(ce.get("oi_chg")), _iv(ce.get("iv")), _d(ce.get("delta")),
            _px(ce.get("price")), mark, fnum(x["strike"], 0 if x["strike"] == int(x["strike"]) else 2),
            _px(pe.get("price")), _d(pe.get("delta")), _iv(pe.get("iv")), signed(pe.get("oi_chg")),
            qty(pe.get("oi")), tag))
    out.append("* = ATM (nearest the forward). price = bid/ask mid, or LTP when the quote is wide.")
    return "\n".join(out) + "\n" + render_footer(a)


def render_pcr(a):
    al, near = a["pcr"]["all"], a["pcr"]["near_atm"]
    out = [render_header(a)]
    out.append("PCR (OI)      %s  = put OI %s / call OI %s contracts" % (
        al["oi"], qty(al["pe_oi"]), qty(al["ce_oi"])))
    out.append("PCR (volume)  %s  = put volume %s / call volume %s contracts" % (
        al["volume"], qty(al["pe_volume"]), qty(al["ce_volume"])))
    out.append("PCR (OI added today) %s  = puts %s / calls %s" % (
        al["oi_chg"] if al["oi_chg"] is not None else "- (one side shed OI)",
        signed(al["pe_oi_chg"]), signed(al["ce_oi_chg"])))
    if near["strikes"]:
        out.append("Near the money (%s to %s): PCR OI %s, volume %s" % (
            fnum(near["strikes"][0], 0), fnum(near["strikes"][1], 0), near["oi"], near["volume"]))
    v = al["oi"]
    if v is not None:
        out.append("Reading: %s. PCR is a crowd gauge, not a signal." % (
            "more puts than calls open (put writers see a floor, or hedgers are heavy)" if v > 1.2 else
            "more calls than puts open (call writers see a cap)" if v < 0.8 else "balanced"))
    return "\n".join(out) + "\n" + render_footer(a)


def render_maxpain(a):
    out = [render_header(a)]
    mp = a["max_pain_detail"]
    out.append("Max pain %s: %s from the forward. Option buyers together would be paid the least "
               "(%s) if %s settled there." % (fnum(mp["strike"], 0), "%+.0f points" % mp["distance"],
                                              inr(mp["pain"]), a["underlying"]))
    mpc = max_pain([{"strike": x["strike"], "ce": x["ce"], "pe": x["pe"]} for x in a["rows"]],
                   a["lot_size"] or 1)["curve"]
    ks = [c["strike"] for c in mpc]
    i = ks.index(mp["strike"])
    out.append("%10s %22s" % ("settle at", "paid to buyers"))
    for c in mpc[max(0, i - 6): i + 7]:
        out.append("%10s %22s%s" % (fnum(c["strike"], 0), inr(c["pain"]),
                                    "  <- max pain" if c["strike"] == mp["strike"] else ""))
    out.append("Max pain is a weak pull, strongest on expiry day; big moves ignore it.")
    return "\n".join(out) + "\n" + render_footer(a)


def render_oi(a):
    out = [render_header(a)]
    b = oi_buildup(a["rows"])
    for t, name in (("CE", "Calls"), ("PE", "Puts")):
        s = b["summary"][t]
        out.append("%s: %s" % (name, ", ".join("%s %s" % (k, signed(v)) for k, v in
                                               sorted(s.items(), key=lambda kv: -abs(kv[1]))) or "no change"))
    out.append("")
    out.append("%10s %3s %9s %10s %10s  %s" % ("strike", "", "price chg", "OI chg", "OI", "build-up"))
    for d in sorted(b["contracts"], key=lambda d: -abs(d["oi_chg"]))[:20]:
        out.append("%10s %3s %+9.2f %10s %10s  %s" % (fnum(d["strike"], 0), d["type"], d["change"],
                                                     signed(d["oi_chg"]), qty(d["oi"]), d["buildup"]))
    w = a["walls"]
    out.append("")
    out.append("Call OI (resistance): " + ", ".join("%s (%s)" % (fnum(c["strike"], 0), qty(c["oi"]))
                                                    for c in w["calls"]))
    out.append("Put OI (support):     " + ", ".join("%s (%s)" % (fnum(c["strike"], 0), qty(c["oi"]))
                                                    for c in w["puts"]))
    out.append("Most added today: calls " + ", ".join("%s (%s)" % (fnum(c["strike"], 0), signed(c["oi_chg"]))
                                                      for c in w["call_adds"]) +
               "; puts " + ", ".join("%s (%s)" % (fnum(c["strike"], 0), signed(c["oi_chg"]))
                                     for c in w["put_adds"]))
    out.append("Short build-up in calls = call writing; in puts = put writing. Build-up compares "
               "today's price change with today's OI change for each contract.")
    return "\n".join(out) + "\n" + render_footer(a)


def render_iv(a, n):
    out = [render_header(a)]
    out.append("ATM IV %s%% at %s: call %s, put %s (Black-76 on the forward; the two agree when the "
               "forward is right)" % (_iv(a["atm_iv"]), fnum(a["atm"], 0), _iv(a["atm_iv_ce"]),
                                      _iv(a["atm_iv_pe"])))
    sk = a["skew"]
    if sk:
        out.append("25-delta skew: put %s at %s%% vs call %s at %s%% -> risk reversal %+.2f vol points" % (
            fnum(sk["put_strike"], 0), _iv(sk["put_iv"]), fnum(sk["call_strike"], 0), _iv(sk["call_iv"]),
            sk["risk_reversal"]))
    rank = a.get("iv_rank")
    if rank and rank.get("rank") is not None:
        out.append("India VIX %s: IV rank %s, IV percentile %s over %d sessions (%s to %s, low %s, high %s)" % (
            rank["current"], rank["rank"], rank["percentile"], rank["sessions"], rank["from"], rank["to"],
            rank["low"], rank["high"]))
        out.append("  VIX is NIFTY's 30-day implied vol; it is a proxy for other indices and for this expiry.")
    elif rank is not None:
        out.append("IV rank: %s" % rank.get("note", "not available"))
    out.append("")
    out.append("Smile (out-of-the-money side at each strike):")
    sm = {s["strike"]: s for s in smile(a["rows"], a["forward"])}
    for x in window_rows(a["rows"], a["atm"], n):
        s = sm.get(x["strike"])
        if not s:
            continue
        bar = "#" * int(round(s["iv"] / 1.0)) if s["iv"] < 120 else "#" * 120
        out.append("%10s %s %6s%%  %s" % (fnum(x["strike"], 0), s["type"], _iv(s["iv"]), bar[:60]))
    return "\n".join(out) + "\n" + render_footer(a)


def render_expiries(sym, rows, lots_found):
    out = ["%s expiries (NSE's list):" % sym]
    for r in rows:
        out.append("  %s  %s  %4d days  lot %s%s" % (r["expiry_nse"], r["weekday"], r["days"],
                                                     r["lot_size"] or "?", "  monthly" if r["monthly"] else ""))
    if not lots_found:
        out.append("NOTE: lot sizes not found in fo_mktlots.csv for %s" % sym)
    out.append("Expiries come from the exchange; holidays move them (e.g. a Monday expiry).")
    return "\n".join(out) + "\n"


def expiry_table(expiries, lots, symbol, now):
    """[{"expiry", "expiry_nse", "weekday", "days", "lot_size", "monthly"}] for the exchange's list."""
    dates = [parse_nse_date(e) for e in expiries]
    out = []
    for i, d in enumerate(dates):
        later_same_month = any(x.year == d.year and x.month == d.month and x > d for x in dates)
        out.append({"expiry": d.isoformat(), "expiry_nse": nse_expiry(d), "weekday": d.strftime("%a"),
                    "days": (d - now.date()).days, "lot_size": lot_size(lots, symbol, d),
                    "monthly": not later_same_month})
    return out


# ---------------------------------------------------------------- CLI

def _parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    def common(p, strikes=15):
        p.add_argument("symbol", help="NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50 or an F&O stock")
        p.add_argument("--expiry", help="DD-Mon-YYYY from `expiries` (default: the nearest)")
        p.add_argument("--strikes", type=int, default=strikes, help="strikes each side of ATM to show (0 = all)")
        p.add_argument("--rate", type=float, help="risk-free rate, e.g. 0.0557 (default: config)")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--write", nargs="?", const="trader/options.json", metavar="PATH",
                       help="write the full analysis (all strikes) to trader/options.json")
        p.add_argument("--json", action="store_true")
        p.add_argument("--now", help="'YYYY-MM-DD HH:MM' IST for the stale-data checks (default: now)")
        p.add_argument("--offline", metavar="DIR", help="read saved responses from DIR")
        p.add_argument("--save", metavar="DIR", help="save the raw responses to DIR")

    for name, text in (("chain", "the option chain with our IV and Greeks"),
                       ("pcr", "put-call ratio by OI and volume"),
                       ("maxpain", "max pain strike and curve"),
                       ("oi", "OI build-up and call/put walls"),
                       ("iv", "IV smile, ATM IV, skew, IV rank from India VIX")):
        p = sub.add_parser(name, help=text)
        common(p, 15 if name != "iv" else 12)
    p = sub.add_parser("expiries", help="the exchange's expiry list with lot sizes")
    p.add_argument("symbol")
    p.add_argument("--json", action="store_true")
    p.add_argument("--now", help="'YYYY-MM-DD HH:MM' IST (default: now)")
    p.add_argument("--offline", metavar="DIR")
    p.add_argument("--save", metavar="DIR")
    return ap


def _symbol(s):
    sym = (s or "").strip().upper()
    if sym.endswith(".NS"):
        sym = sym[:-3]
    sym = {"NIFTY50": "NIFTY", "NIFTY 50": "NIFTY", "BANK NIFTY": "BANKNIFTY", "NIFTYBANK": "BANKNIFTY"}.get(sym, sym)
    if not SYMBOL_RE.match(sym):
        raise OptionsError("%r is not an NSE F&O symbol (e.g. NIFTY, BANKNIFTY, RELIANCE, M&M)" % s)
    return sym


def _now(text):
    if not text:
        return now_ist()
    t = parse_nse_time(text)
    if t is None:
        raise OptionsError("--now is 'YYYY-MM-DD HH:MM'")
    return t


def utf8_console():
    """Print the rupee sign on any console, including a Windows pipe (cp1252 by default)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None):
    ap = _parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    try:
        return _run(args)
    except OptionsError as exc:
        print("options: %s" % exc, file=sys.stderr)
        return 2


def write_json(path, payload):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _run(args):
    sym = _symbol(args.symbol)
    now = _now(args.now)
    fetcher = Fetcher(offline=args.offline, save=args.save)
    info = fetch_contract_info(fetcher, sym)
    if not info["expiries"]:
        raise OptionsError("NSE lists no option expiries for %s (not in F&O?)" % sym)
    try:
        lots = fetch_lots(fetcher)
    except OptionsError:
        lots = {}

    if args.cmd == "expiries":
        rows = expiry_table(info["expiries"], lots, sym, now)
        if args.json:
            print(json.dumps({"underlying": sym, "expiries": rows}, indent=2, ensure_ascii=False))
        else:
            sys.stdout.write(render_expiries(sym, rows, sym in lots))
        return 0

    expiry = pick_expiry(info["expiries"], args.expiry, now)
    chain = fetch_chain(fetcher, sym, expiry)
    if args.rate is not None:
        r, src = args.rate / 100.0 if args.rate > 1 else args.rate, "--rate"
    else:
        r, src = risk_free_rate(args.config)
    a = analyse(chain, lot_size(lots, sym, expiry), r, now=now, rate_source=src)

    if args.cmd == "iv" or args.write:
        if sym in INDICES:
            hist = fetch_vix(fetcher, now.date())
            live = fetch_vix_now(fetcher)
            cur = live["last"] if live and live.get("last") else None
            rank = iv_rank(hist, cur)
            rank["source"] = "India VIX (NIFTY 30-day implied vol)"
            rank["vix_live"] = live
        else:
            rank = {"rank": None, "percentile": None, "source": None,
                    "note": "IV history for stocks needs a bhavcopy backfill (not built yet); "
                            "India VIX covers NIFTY only"}
        a["iv_rank"] = rank
        a["smile"] = smile(a["rows"], a["forward"])

    if args.write:
        write_json(args.write, a)

    if args.json:
        out = dict(a)
        out["rows"] = window_rows(a["rows"], a["atm"], args.strikes)
        if args.cmd == "maxpain":
            out["max_pain_curve"] = max_pain(a["rows"], a["lot_size"] or 1)["curve"]
        if args.cmd == "oi":
            out["buildup"] = oi_buildup(a["rows"])
        print(json.dumps(out, indent=2, ensure_ascii=False))
    elif args.cmd == "chain":
        sys.stdout.write(render_chain(a, args.strikes))
    elif args.cmd == "pcr":
        sys.stdout.write(render_pcr(a))
    elif args.cmd == "maxpain":
        sys.stdout.write(render_maxpain(a))
    elif args.cmd == "oi":
        sys.stdout.write(render_oi(a))
    else:
        sys.stdout.write(render_iv(a, args.strikes))
    if args.write and not args.json:
        print("Wrote %s (%d strikes)." % (args.write, len(a["rows"])))
    return 0


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
