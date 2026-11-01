"""Intraday candles of EXPIRED option contracts from a broker's history API (your own token).

What exists (researched 2026-11-01; neither could be called live here without a paid plan's token):
  Upstox  GET /v2/expired-instruments/expiries?instrument_key=NSE_INDEX|Nifty 50
          GET /v2/expired-instruments/option/contract?instrument_key=...&expiry_date=YYYY-MM-DD
          GET /v2/expired-instruments/historical-candle/{expired key}/{interval}/{to}/{from}
          intervals 1minute, 3minute, 5minute, 15minute, 30minute, day; candles are
          [timestamp, open, high, low, close, volume, oi]. Upstox Plus plan only.
  Dhan    POST /v2/charts/rollingoption: expired options on a rolling basis (ATM, ATM+1 ... ATM+10 for
          index options near expiry, ATM+-3 otherwise), minute bars with iv, oi, spot and strike, up
          to 30 days a call, about 5 years back. Needs Dhan's Data API plan.
NSE itself publishes no intraday option history; Kite's historical API needs Kite Connect's paid plan
and serves only live (not expired) instruments.

Pure helpers: request paths and payloads, and parsers that turn the answers into bars.
"""

import datetime
import urllib.parse

UPSTOX_INTERVALS = {"1m": "1minute", "3m": "3minute", "5m": "5minute", "15m": "15minute", "30m": "30minute",
                    "1d": "day"}
DHAN_INTERVALS = {"1m": "1", "5m": "5", "15m": "15", "25m": "25", "60m": "60"}
DHAN_INDEX_IDS = {"NIFTY": 13, "BANKNIFTY": 25, "FINNIFTY": 27, "MIDCPNIFTY": 442}
DHAN_MAX_DAYS = 30
OPT_COLS = ["date", "time", "open", "high", "low", "close", "volume", "oi", "strike", "spot", "iv"]


def upstox_expiries_path(underlying_key):
    return "/v2/expired-instruments/expiries?instrument_key=%s" % urllib.parse.quote(underlying_key, safe="")


def upstox_contracts_path(underlying_key, expiry):
    return "/v2/expired-instruments/option/contract?instrument_key=%s&expiry_date=%s" % (
        urllib.parse.quote(underlying_key, safe=""), expiry)


def upstox_candles_path(expired_key, interval, start, end):
    if interval not in UPSTOX_INTERVALS:
        raise ValueError("Upstox expired candles come in %s" % ", ".join(sorted(UPSTOX_INTERVALS)))
    return "/v2/expired-instruments/historical-candle/%s/%s/%s/%s" % (
        urllib.parse.quote(expired_key, safe=""), UPSTOX_INTERVALS[interval], end, start)


def upstox_list(doc):
    """{"status": "success", "data": [...]} -> the list (or [])."""
    data = (doc or {}).get("data") if isinstance(doc, dict) else doc
    return data if isinstance(data, list) else []


def upstox_contract(rec):
    """One expired option contract record -> {"key", "strike", "type", "expiry", "symbol", "lot"}."""
    return {"key": rec.get("instrument_key"), "strike": float(rec.get("strike_price") or 0),
            "type": str(rec.get("instrument_type") or "").upper(), "expiry": str(rec.get("expiry") or "")[:10],
            "symbol": rec.get("trading_symbol") or "", "lot": rec.get("lot_size")}


def upstox_bars(doc, strike=None):
    """{"data": {"candles": [[ts, o, h, l, c, v, oi], ...]}} -> bars (IST date and time), oldest first."""
    candles = (((doc or {}).get("data") or {}).get("candles") or []) if isinstance(doc, dict) else []
    out = {}
    for c in candles:
        t = str(c[0])
        if len(t) < 16 or None in c[1:5]:
            continue
        key = (t[:10], t[11:16])
        out[key] = {"date": key[0], "time": key[1], "o": float(c[1]), "h": float(c[2]), "l": float(c[3]),
                    "c": float(c[4]), "v": int(float(c[5] or 0)) if len(c) > 5 else 0,
                    "oi": int(float(c[6])) if len(c) > 6 and c[6] is not None else None, "strike": strike,
                    "spot": None, "iv": None}
    return [out[k] for k in sorted(out)]


def near_strikes(contracts, spot, count):
    """The `count` strikes nearest to spot on each side (calls and puts), as contract records."""
    strikes = sorted(set(c["strike"] for c in contracts))
    if not strikes:
        return []
    if spot is None:
        spot = strikes[len(strikes) // 2]
    centre = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    keep = set(strikes[max(0, centre - count):centre + count + 1])
    return [c for c in contracts if c["strike"] in keep]


def dhan_windows(start, end):
    """[(a, b)] windows of at most 30 days covering start..end (ISO strings)."""
    a, b = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    out = []
    while a <= b:
        z = min(b, a + datetime.timedelta(days=DHAN_MAX_DAYS - 1))
        out.append((a.isoformat(), z.isoformat()))
        a = z + datetime.timedelta(days=1)
    return out


def dhan_payload(underlying, strike, opt_type, start, end, interval="1m", expiry_flag="WEEK", expiry_code=1):
    """The rollingoption request body. strike is 'ATM', 'ATM+1', 'ATM-2' ...; opt_type CALL or PUT."""
    sym = underlying.upper()
    if sym not in DHAN_INDEX_IDS:
        raise ValueError("Dhan rolling options are wired here for %s" % ", ".join(sorted(DHAN_INDEX_IDS)))
    if interval not in DHAN_INTERVALS:
        raise ValueError("Dhan rolling options come in %s" % ", ".join(sorted(DHAN_INTERVALS)))
    return {"exchangeSegment": "NSE_FNO", "interval": DHAN_INTERVALS[interval], "securityId": DHAN_INDEX_IDS[sym],
            "instrument": "OPTIDX", "expiryFlag": expiry_flag, "expiryCode": int(expiry_code), "strike": strike,
            "drvOptionType": opt_type, "requiredData": ["open", "high", "low", "close", "volume", "oi", "iv",
                                                        "strike", "spot"],
            "fromDate": start, "toDate": end}


IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def dhan_bars(doc, side):
    """{"data": {"ce": {"timestamp": [...], "open": [...], ...}, "pe": ...}} -> bars for side 'ce'/'pe'."""
    data = ((doc or {}).get("data") or {}) if isinstance(doc, dict) else {}
    series = data.get(side.lower()) or data.get(side.upper()) or {}
    ts = series.get("timestamp") or []
    out = []
    for i, t in enumerate(ts):
        def col(k):
            v = series.get(k) or []
            return v[i] if i < len(v) else None
        if None in (col("open"), col("high"), col("low"), col("close")):
            continue
        when = datetime.datetime.fromtimestamp(float(t), IST)
        out.append({"date": when.date().isoformat(), "time": when.strftime("%H:%M"), "o": float(col("open")),
                    "h": float(col("high")), "l": float(col("low")), "c": float(col("close")),
                    "v": int(float(col("volume") or 0)), "oi": int(float(col("oi"))) if col("oi") is not None else None,
                    "strike": col("strike"), "spot": col("spot"), "iv": col("iv")})
    return out
