"""Realised volatility, a HAR-RV forecast and the implied-minus-forecast spread. Standard library only.

Every value dated `day` uses bars up to and including that day's close, never later ones:

    daily variance   close-to-close (squared log return), Parkinson (from the high and low) and
                     Garman-Klass (open, high, low, close), each a one-day variance, not annualised
    realised vol     the n-session estimate, annualised in % (sqrt(252)), like trader-regime's realised_vol
    HAR-RV           Corsi (2009): the average variance of the next h sessions regressed on the last day's,
                     the last week's (5) and the last month's (22) average variance, by least squares, on
                     the train window only (a row is used only when its whole target is inside the window)
    spread           IV - forecast RV in vol points, IV from India VIX or the ATM options of the EOD chain
                     (Black-76 on settle prices, the options package's Chains.iv_series)

A bar whose high, low and open all equal its close (an index file with only a close that day) has no range:
Parkinson and Garman-Klass use the close-to-close variance for it, and say so in the flags.
Education and tooling, not financial advice.
"""

import bisect
import math

YEAR = 252
ESTIMATORS = ("close", "parkinson", "garman-klass")
HAR_LAGS = (1, 5, 22)
LN2 = math.log(2.0)


class VolError(ValueError):
    pass


# ---------------------------------------------------------------- one-day variances

def _ok(b):
    return all(b.get(k) not in (None, 0) and b.get(k) > 0 for k in ("o", "h", "l", "c"))


def daily_variance(bars, estimator="close"):
    """[(date, variance or None, flag)] for each bar; the first close-to-close value is None (no previous
    close). flag: "" or "no range: close-to-close" when the bar has no high-low range."""
    if estimator not in ESTIMATORS:
        raise VolError("estimator is one of %s" % ", ".join(ESTIMATORS))
    out = []
    prev = None
    for b in bars:
        c = b.get("c")
        cc = math.log(c / prev) ** 2 if prev and c and prev > 0 and c > 0 else None
        v, flag = cc, ""
        if estimator != "close":
            flat = not _ok(b) or (b["h"] == b["l"])
            if flat:
                flag = "no range: close-to-close"
            elif estimator == "parkinson":
                v = math.log(b["h"] / b["l"]) ** 2 / (4.0 * LN2)
            else:
                hl = math.log(b["h"] / b["l"])
                co = math.log(b["c"] / b["o"])
                v = max(0.0, 0.5 * hl * hl - (2.0 * LN2 - 1.0) * co * co)
        out.append((b["date"], v, flag))
        prev = c if c else prev
    return out


def annualised(var):
    """A one-day variance -> annualised vol in %."""
    return None if var is None else math.sqrt(max(0.0, var) * YEAR) * 100.0


def realised_vol(bars, n=20, estimator="close"):
    """{date: n-session realised vol in %, annualised}. Close-to-close is the sample stdev of the last n
    log returns (the same number as trader-regime's realised_vol); Parkinson and Garman-Klass are the
    square root of the mean of the last n one-day variances."""
    if n < 2:
        raise VolError("n is at least 2")
    out = {}
    if estimator == "close":
        rets = [None]
        for a, b in zip(bars, bars[1:]):
            rets.append(math.log(b["c"] / a["c"]) if a.get("c") and b.get("c") else None)
        for i in range(n, len(bars)):
            w = rets[i - n + 1:i + 1]
            if any(x is None for x in w):
                continue
            m = sum(w) / n
            var = sum((x - m) ** 2 for x in w) / (n - 1)
            out[bars[i]["date"]] = math.sqrt(var) * math.sqrt(YEAR) * 100.0
        return out
    dv = daily_variance(bars, estimator)
    for i in range(n - 1, len(dv)):
        w = [v for _, v, _ in dv[i - n + 1:i + 1]]
        if any(v is None for v in w):
            continue
        out[dv[i][0]] = annualised(sum(w) / n)
    return out


# ---------------------------------------------------------------- HAR-RV

def _solve(A, y):
    """Least squares by the normal equations (Gaussian elimination with partial pivoting)."""
    k = len(A[0])
    M = [[sum(r[i] * r[j] for r in A) for j in range(k)] + [sum(r[i] * t for r, t in zip(A, y))] for i in range(k)]
    for c in range(k):
        p = max(range(c, k), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-30:
            raise VolError("the HAR regressors are collinear on this window")
        M[c], M[p] = M[p], M[c]
        for r in range(k):
            if r != c:
                f = M[r][c] / M[c][c]
                M[r] = [a - f * b for a, b in zip(M[r], M[c])]
    return [M[i][k] / M[i][i] for i in range(k)]


def har_rows(var, h=20, lags=HAR_LAGS):
    """[(i, [1, rv_d, rv_w, rv_m], target or None)] for every index i with a full history: the regressors
    are the means of the last 1, 5 and 22 one-day variances up to i; the target is the mean of the next
    h (i+1..i+h), None near the end of the series."""
    n = len(var)
    out = []
    big = max(lags)
    for i in range(big - 1, n):
        hist = var[i - big + 1:i + 1]
        if any(v is None for v in hist):
            continue
        x = [1.0] + [sum(var[i - L + 1:i + 1]) / float(L) for L in lags]
        fut = var[i + 1:i + 1 + h]
        y = sum(fut) / float(h) if len(fut) == h and all(v is not None for v in fut) else None
        out.append((i, x, y))
    return out


def har_fit(bars, train_from, train_to, h=20, estimator="close", lags=HAR_LAGS):
    """Fit HAR-RV on the train window only. A row counts when its date and its whole h-session target are
    inside [train_from, train_to]; nothing after train_to is read for the fit.

    {"coef": [b0, b_day, b_week, b_month], "n", "r2", "h", "estimator", "lags", "train": [from, to],
     "fitted_through"}"""
    dv = daily_variance(bars, estimator)
    dates = [d for d, _, _ in dv]
    var = [v for _, v, _ in dv]
    lo = bisect.bisect_left(dates, train_from)
    hi = bisect.bisect_right(dates, train_to)       # var[hi:] is after the train window: never used
    rows = [(i, x, y) for i, x, y in har_rows(var[:hi], h, lags) if i >= lo and y is not None]
    if len(rows) < 3 * len(lags) + 10:
        raise VolError("%d usable rows on the train window: too few to fit HAR-RV (need %d)"
                       % (len(rows), 3 * len(lags) + 10))
    A = [x for _, x, _ in rows]
    y = [t for _, _, t in rows]
    coef = _solve(A, y)
    fit = [sum(c * v for c, v in zip(coef, x)) for x in A]
    my = sum(y) / len(y)
    ss_t = sum((t - my) ** 2 for t in y)
    ss_r = sum((t - f) ** 2 for t, f in zip(y, fit))
    return {"coef": coef, "n": len(rows), "r2": 1.0 - ss_r / ss_t if ss_t > 0 else None, "h": h,
            "estimator": estimator, "lags": list(lags), "train": [train_from, train_to],
            "fitted_through": dates[rows[-1][0] + h] if rows[-1][0] + h < len(dates) else dates[-1]}


def har_forecast(model, bars):
    """{date: the forecast of the next h sessions' realised vol in %, annualised}, each from the variances
    up to that date's close and the model's (train-fitted) coefficients. A negative forecast variance is
    floored at a tenth of the last month's average variance."""
    dv = daily_variance(bars, model["estimator"])
    var = [v for _, v, _ in dv]
    out = {}
    for i, x, _ in har_rows(var, model["h"], tuple(model["lags"])):
        f = sum(c * v for c, v in zip(model["coef"], x))
        out[dv[i][0]] = annualised(max(f, 0.1 * x[-1]))
    return out


# ---------------------------------------------------------------- the spread

def spread(iv, rv):
    """{date: IV - RV in vol points} on the dates both have."""
    return {d: iv[d] - rv[d] for d in sorted(set(iv) & set(rv)) if iv[d] is not None and rv[d] is not None}


def vix_iv(vix_bars):
    """{date: India VIX close}."""
    return {b["date"]: b["c"] for b in vix_bars or [] if b.get("c")}


def vrp_table(index_bars, iv, model=None, n=20, estimator="close"):
    """{date: {"iv", "rv" (n-session realised), "rv_forecast" (HAR, when a model is given), "spread_rv"
    (iv - rv), "spread_har" (iv - forecast)}}: the whole panel on the dates IV is known."""
    rv = realised_vol(index_bars, n, estimator)
    fc = har_forecast(model, index_bars) if model else {}
    out = {}
    for d in sorted(iv):
        row = {"iv": iv[d], "rv": rv.get(d), "rv_forecast": fc.get(d)}
        row["spread_rv"] = None if row["rv"] is None else iv[d] - row["rv"]
        row["spread_har"] = None if row["rv_forecast"] is None else iv[d] - row["rv_forecast"]
        out[d] = row
    return out


def forward_realised(bars, day, sessions):
    """The realised close-to-close vol (%) over the `sessions` sessions after `day` (the thing the
    forecast and the IV are judged against), or None when the series ends sooner. For scoring only:
    never feed it to a decision."""
    dates = [b["date"] for b in bars]
    i = bisect.bisect_right(dates, day) - 1
    if i < 0 or i + sessions >= len(bars):
        return None
    w = [math.log(bars[j]["c"] / bars[j - 1]["c"]) for j in range(i + 1, i + sessions + 1)]
    return math.sqrt(sum(x * x for x in w) / len(w) * YEAR) * 100.0
