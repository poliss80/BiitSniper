"""Offline unit tests for Momentum Scanner Stock Race tile label handling.

Covers dynamic race-label extraction, de-duplication, the conservative cap,
the known-label fallback, and key stability — no Selenium/browser access.
"""

import os
import unittest
from unittest.mock import patch

from engine.ti import capture_tradeideas as ti

_FALLBACK_KEYS = [
    "momentum_relative_volume",
    "momentum_fang_and_friends",
    "momentum_sp500_moving_up",
    "momentum_sp500_moving_down",
]


class RaceLabelResolutionTests(unittest.TestCase):
    def test_known_labels_produce_stable_keys(self):
        pairs = ti._resolve_race_labels(list(ti._MOMENTUM_SCANNER_RACES))
        self.assertEqual([k for _, k in pairs], _FALLBACK_KEYS)

    def test_discovers_additional_tiles_beyond_known_four(self):
        raw = [
            "Relative Volume", "FANG and Friends", "SP500 Moving Up",
            "SP500 Moving Down", "Crypto Racers", "Biotech Breakouts",
        ]
        keys = [k for _, k in ti._resolve_race_labels(raw)]
        self.assertEqual(len(keys), 6)
        self.assertIn("momentum_crypto_racers", keys)
        self.assertIn("momentum_biotech_breakouts", keys)

    def test_dedup_preserves_first_occurrence(self):
        raw = ["Relative Volume", "Relative Volume", "  Relative Volume  "]
        pairs = ti._resolve_race_labels(raw)
        self.assertEqual(pairs, [("Relative Volume", "momentum_relative_volume")])

    def test_slug_collisions_get_unique_suffixes(self):
        raw = ["Tech Winners!", "Tech Winners", "tech   winners"]
        keys = [k for _, k in ti._resolve_race_labels(raw)]
        self.assertEqual(
            keys,
            ["momentum_tech_winners", "momentum_tech_winners_2", "momentum_tech_winners_3"],
        )

    def test_odd_and_missing_labels_skipped(self):
        raw = [None, "", "   ", 42, "!!!", "x" * 500, "FANG and Friends"]
        pairs = ti._resolve_race_labels(raw)
        self.assertEqual(pairs, [("FANG and Friends", "momentum_fang_and_friends")])

    def test_ticker_like_labels_not_treated_as_races(self):
        raw = ["SPY", "AAPL", "QQQ", "Relative Volume"]
        pairs = ti._resolve_race_labels(raw)
        self.assertEqual(pairs, [("Relative Volume", "momentum_relative_volume")])

    def test_cap_limits_number_of_races(self):
        raw = [f"Race {i}" for i in range(1, 30)]
        pairs = ti._resolve_race_labels(raw, cap=5)
        self.assertEqual(len(pairs), 5)
        self.assertEqual(pairs[0], ("Race 1", "momentum_race_1"))
        self.assertEqual(pairs[-1], ("Race 5", "momentum_race_5"))

    def test_explicit_cap_is_clamped_to_at_least_one(self):
        raw = ["Race 1", "Race 2"]
        pairs = ti._resolve_race_labels(raw, cap=0)
        self.assertEqual(len(pairs), 1)

    def test_env_var_controls_default_cap(self):
        raw = [f"Race {i}" for i in range(1, 30)]
        with patch.dict(os.environ, {"TI_MOMENTUM_SCANNER_MAX_RACES": "3"}):
            self.assertEqual(ti._momentum_scanner_max_races(), 3)
            self.assertEqual(len(ti._resolve_race_labels(raw)), 3)

    def test_invalid_env_var_uses_default_cap(self):
        raw = [f"Race {i}" for i in range(1, 30)]
        for bad in ("banana", "0", "-4", ""):
            with self.subTest(bad=bad), patch.dict(
                os.environ, {"TI_MOMENTUM_SCANNER_MAX_RACES": bad}
            ):
                self.assertEqual(
                    ti._momentum_scanner_max_races(),
                    ti._MOMENTUM_SCANNER_MAX_RACES_DEFAULT,
                )
        self.assertEqual(
            len(ti._resolve_race_labels(raw, cap=ti._MOMENTUM_SCANNER_MAX_RACES_DEFAULT)),
            ti._MOMENTUM_SCANNER_MAX_RACES_DEFAULT,
        )

    def test_fallback_to_known_labels_when_discovery_empty(self):
        for raw in ([], [None, "", "   ", "SPY"]):
            with self.subTest(raw=raw):
                keys = [k for _, k in ti._resolve_race_labels(raw)]
                self.assertEqual(keys, _FALLBACK_KEYS)

    def test_fallback_keys_match_previous_hardcoded_behavior(self):
        # Keys the old fixed-label loop wrote into results / ti_primary.json
        expected = {
            "Relative Volume": "momentum_relative_volume",
            "FANG and Friends": "momentum_fang_and_friends",
            "SP500 Moving Up": "momentum_sp500_moving_up",
            "SP500 Moving Down": "momentum_sp500_moving_down",
        }
        pairs = ti._resolve_race_labels([])
        self.assertEqual(dict(pairs), expected)


if __name__ == "__main__":
    unittest.main()
