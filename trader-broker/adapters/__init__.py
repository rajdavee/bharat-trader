"""Broker adapters for trader-broker: Upstox, Zerodha Kite, Dhan and Angel One. Standard library only.

ticket.py loads this folder by path (it travels with the skill). Read-only calls work from any
IP; order calls need a GatePass from base.open_gate() and a static IP the broker knows.

Another broker is one more module here with one more Adapter subclass: the contract is base.Adapter,
and tests/test_broker_adapters.py runs the same contract suite against every entry in ADAPTERS. An
OpenAlgo bridge (HTTP to your own OpenAlgo server, no code copied) would plug in the same way; see
docs/brokers.md, "OpenAlgo bridge (not built yet)".
"""

from . import base, auth  # noqa: F401
from .angel import AngelAdapter
from .dhan import DhanAdapter
from .kite import KiteAdapter
from .upstox import UpstoxAdapter

ADAPTERS = {"upstox": UpstoxAdapter, "kite": KiteAdapter, "dhan": DhanAdapter, "angel": AngelAdapter}
NAMES = tuple(sorted(ADAPTERS))


def make(name, **kw):
    """The adapter for a broker name ("zerodha" means kite, "angelone" angel). Raises base.Unsupported for any
    other name."""
    key = base.canonical_broker(name)
    if key not in ADAPTERS:
        raise base.Unsupported("no adapter for %r: the brokers are %s (or paper)" % (name, ", ".join(NAMES)))
    return ADAPTERS[key](**kw)
