"""Broker adapters for trader-broker: Upstox, Zerodha Kite and Dhan. Standard library only.

ticket.py loads this folder by path (it travels with the skill). Read-only calls work from any
IP; order calls need a GatePass from base.open_gate() and a static IP the broker knows.
"""

from . import base, auth  # noqa: F401
from .dhan import DhanAdapter
from .kite import KiteAdapter
from .upstox import UpstoxAdapter

ADAPTERS = {"upstox": UpstoxAdapter, "kite": KiteAdapter, "dhan": DhanAdapter}
NAMES = tuple(sorted(ADAPTERS))


def make(name, **kw):
    """The adapter for a broker name ("zerodha" means kite). Raises base.Unsupported for any other name."""
    key = base.canonical_broker(name)
    if key not in ADAPTERS:
        raise base.Unsupported("no adapter for %r: the brokers are %s (or paper)" % (name, ", ".join(NAMES)))
    return ADAPTERS[key](**kw)
