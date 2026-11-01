"""The lab's v2 engines and the edge-hunt executor. Standard library only.

lab.py loads this package by path (engines_api) and binds it to the lab itself (charges, the deflated Sharpe
ratio, PBO, the gate defaults, inr) and the sibling skills it is handed (trader-archive's read API,
trader-regime, trader-strategy's SPAN figures). Nothing here imports another skill.

    core       the run shape, costs, the session calendar, portfolio metrics, bootstrap p-values, BH
    market     the data adapter (the archive, or a plain folder)
    futures    continuous series, lots by date, margin, rolls, overnight or intraday holding, daily MTM
    portfolio  cross-sectional ranking portfolios on a point-in-time universe
    events     calendar windows and results-date events
    pairs      pair selection on train only (correlation + Engle-Granger) and z-score trading
    jobs       a registered hypothesis -> engine runs, per family
    executor   train + validation for every grid point, the gates, the sealed holdout opened once
    fwdtest    the forward-test dashboard: the paper forward record against the backtest's predicted range

Education and research tooling, not financial advice. Nothing here places an order.
"""

from . import core, events, executor, futures, fwdtest, jobs, market, pairs, portfolio  # noqa: F401


def bind(lab, archive=None, regime=None, strategy=None):
    core.ENV["lab"] = lab
    for k, v in (("archive", archive), ("regime", regime), ("strategy", strategy)):
        if v is not None:
            core.ENV[k] = v
