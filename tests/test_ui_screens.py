"""Screenshot smoke test of the local UI (ui/screenshots.py) on the offline demo.

Runs only where a headless Chromium-family browser is installed, and not in CI unless
BT_UI_SCREENS=1 (a browser in CI is optional and slow). It renders the key pages at a desktop and a
phone width, and checks that each page drew without an error note, that the page never scrolls
sideways, and that all 16 navigation links are present (issue #39: nothing cut off).
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import ROOT  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "ui"))
import screenshots  # noqa: E402

KEY_PAGES = ["today", "lab", "risk", "portfolio", "options", "settings", "cotrader", "ask"]
WIDTHS = [1440, 1280, 390]


def _skip_reason():
    if os.environ.get("CI") and os.environ.get("BT_UI_SCREENS") != "1":
        return "screenshots run locally (set BT_UI_SCREENS=1 to run them in CI)"
    if not screenshots.find_browser():
        return "no headless browser (Chrome, Chromium or Edge) found; set BT_BROWSER"
    return None


@unittest.skipIf(_skip_reason(), _skip_reason())
class Screens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = tempfile.mkdtemp(prefix="bt-screens-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def check(self, shots):
        for path, probe in shots:
            with self.subTest(path=os.path.basename(path)):
                self.assertGreater(os.path.getsize(path), 5000)
                with open(path, "rb") as fh:
                    self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")
                self.assertLessEqual(probe["overflow"], 0, "the page scrolls sideways")
                self.assertEqual(probe["errors"], [])
                self.assertEqual(probe["nav"], 16)

    def test_key_pages_at_desktop_and_phone_widths(self):
        self.check(screenshots.run(self.out, KEY_PAGES, WIDTHS))

    def test_first_run_setup_on_an_empty_folder(self):
        shots = screenshots.run(self.out, ["welcome", "today"], [1280, 390], fresh=True, prefix="fresh-")
        self.check(shots)


if __name__ == "__main__":
    unittest.main()
