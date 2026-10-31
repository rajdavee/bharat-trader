"""Load a script from a skill folder (the folder names have hyphens)."""

import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")


def load(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("trader_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if hasattr(mod, "fetch_fno_stocks"):
        # the tests never go online: a tool that would refresh NSE's F&O list gets "no network"
        def no_network(timeout=20):
            raise mod.RulesError("no network in the tests")
        mod.fetch_fno_stocks = no_network
    return mod
