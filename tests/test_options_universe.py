"""Options universe composition tests.

Covers engine.config.get_options_universe:
  - curated liquid FANG + S&P 500 core inclusion (default on)
  - deterministic source ordering and deduplication
  - OPTIONS_LIQUID_CORE_ENABLED / OPTIONS_LIQUID_CORE_EXTRA env switches
  - OPTIONS_UNIVERSE_OVERRIDE short-circuit behavior (preserved)
All TI file inputs are patched so tests are deterministic and offline.
"""

import unittest
from unittest.mock import patch

import engine.config as config


def _run_universe(
    *,
    unusual=("UONE",),
    ti_primary=("TBROAD",),
    paper=False,
    liquid_enabled=True,
    liquid_extra="",
    override="",
    require_ti_file=False,
):
    """Invoke get_options_universe with all external sources patched."""
    with (
        patch.object(config, "_load_options_universe", return_value=list(unusual)),
        patch.object(config, "PAPER", paper),
        patch.object(config, "OPTIONS_LIQUID_CORE_ENABLED", liquid_enabled),
        patch.object(config, "OPTIONS_LIQUID_CORE_EXTRA", liquid_extra),
        patch.object(config, "OPTIONS_UNIVERSE_OVERRIDE", override),
        patch("engine.equity.universe.get_ti_primary", return_value=list(ti_primary)),
        patch("engine.equity.universe.get_tier", return_value=[]),
    ):
        return config.get_options_universe(require_ti_file=require_ti_file)


class LiquidCoreTests(unittest.TestCase):
    def test_liquid_core_included_by_default(self):
        universe = _run_universe()
        # Representative names from each curated core group.
        for sym in ("SPY", "QQQ", "META", "NVDA", "AVGO", "JPM", "UNH"):
            self.assertIn(sym, universe)

    def test_core_names_unique_to_liquid_core_absent_when_disabled(self):
        # AVGO / V / MA / UNH are in the liquid core but NOT in the static fallback.
        universe = _run_universe(liquid_enabled=False)
        for sym in ("AVGO", "V", "MA", "UNH"):
            self.assertNotIn(sym, universe)
        # Static fallback core still present regardless.
        self.assertIn("SPY", universe)
        self.assertIn("AAPL", universe)

    def test_liquid_core_extra_appended_after_builtin_core(self):
        universe = _run_universe(liquid_extra="MELI,SHOP,meli")
        self.assertEqual(universe.count("MELI"), 1)
        self.assertEqual(universe.count("SHOP"), 1)
        # EXTRA lands after the built-in core (JNJ is last) but before the
        # static fallback core (SPXL is fallback-only).
        self.assertGreater(universe.index("MELI"), universe.index("JNJ"))
        self.assertLess(universe.index("MELI"), universe.index("SPXL"))

    def test_liquid_core_extra_rejects_invalid_tickers(self):
        universe = _run_universe(liquid_extra="MELI,1BAD,TOOLONGGG,")
        self.assertIn("MELI", universe)
        self.assertNotIn("1BAD", universe)
        self.assertNotIn("TOOLONGGG", universe)


class OrderingAndDedupTests(unittest.TestCase):
    def test_source_priority_order(self):
        # META appears in the unusual scrape AND the liquid core; NVDA appears
        # in the liquid core AND the broad TI list.
        universe = _run_universe(unusual=("META", "UONE"), ti_primary=("NVDA", "TBROAD"))
        self.assertEqual(universe[0], "META")   # unusual options first
        self.assertEqual(universe[1], "UONE")
        # Liquid core before broad TI names.
        self.assertLess(universe.index("AVGO"), universe.index("TBROAD"))
        # Static fallback core before broad TI names as well.
        self.assertLess(universe.index("SPY"), universe.index("TBROAD"))

    def test_dedup_keeps_first_occurrence(self):
        universe = _run_universe(unusual=("META", "SPY"), ti_primary=("NVDA", "SPY"))
        self.assertEqual(len(universe), len(set(universe)))
        # META/SPY keep their high-priority unusual-options position.
        self.assertEqual(universe.index("META"), 0)
        self.assertEqual(universe.index("SPY"), 1)

    def test_paper_mode_prepends_index_tickers(self):
        universe = _run_universe(paper=True)
        for idx in ("SPX", "NDX", "RUT", "VIX"):
            self.assertIn(idx, universe)
        # Index tickers head the static fallback core (SPXL is fallback-only),
        # which follows the liquid core (JNJ is its last built-in name).
        self.assertLess(universe.index("SPX"), universe.index("SPXL"))
        self.assertGreater(universe.index("SPX"), universe.index("JNJ"))


class OverrideTests(unittest.TestCase):
    def test_universe_override_short_circuits_everything(self):
        universe = _run_universe(override="TSLA, foo ,1X,TOOLONGGG")
        self.assertEqual(universe, ["TSLA", "FOO"])

    def test_override_wins_even_with_liquid_core_disabled(self):
        universe = _run_universe(liquid_enabled=False, override="amd,msft")
        self.assertEqual(universe, ["AMD", "MSFT"])

    def test_require_ti_file_raises_when_all_ti_sources_empty(self):
        with self.assertRaises(FileNotFoundError):
            _run_universe(ti_primary=(), require_ti_file=True)


if __name__ == "__main__":
    unittest.main()
