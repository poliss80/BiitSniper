"""Tests for MomentumScalp candidate-selection priority in scan_universe._scan_one."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import engine.equity.scan as scan
from engine.equity.strategies import Signal


class _FakeStrategy:
    """Plain strategy stub — not a Technical/Sentiment/Momentum base, so the
    scan loop calls scan(symbol) with no extra args."""
    def __init__(self, signal):
        self._signal = signal

    def scan(self, symbol):
        return self._signal


def _market_state(bull=True):
    return SimpleNamespace(resolve_regime=lambda: bull, vix=None)


def _sig(symbol, strategy, conf):
    return Signal(symbol, "buy", 10.0, conf, "test", strategy)


def _run_scan(signals_for_symbol):
    """scan_universe with guardrails/strategies/news-annotation mocked out."""
    strats = [_FakeStrategy(s) for s in signals_for_symbol]
    with patch.object(scan, "clear_bar_cache"), \
         patch.object(scan, "_prefetch_snapshots"), \
         patch.object(scan, "_passes_guardrails", return_value=(True, None)), \
         patch.object(scan, "get_strategy_instances", return_value=strats), \
         patch.object(scan, "annotate_signal_with_news", side_effect=lambda s: s), \
         patch.object(scan, "MIN_SIGNAL_CONFIDENCE", 0.50):
        return scan.scan_universe(["TEST"], "neutral", _market_state())


class TestScalpCandidatePriority(unittest.TestCase):

    def test_scalp_beats_higher_confidence_momentum_continuation(self):
        signals, hit_counts, errors = _run_scan([
            _sig("TEST", "MomentumContinuation", 0.92),
            _sig("TEST", "MomentumScalp", 0.80),
        ])

        self.assertEqual(errors, 0)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].strategy, "MomentumScalp")
        self.assertEqual(signals[0].confidence, 0.80)
        self.assertEqual(hit_counts, {"MomentumScalp": 1})

    def test_highest_confidence_scalp_wins_among_multiple_scalps(self):
        signals, _, errors = _run_scan([
            _sig("TEST", "MomentumScalp", 0.70),
            _sig("TEST", "MomentumScalp", 0.88),
            _sig("TEST", "MomentumContinuation", 0.95),
        ])

        self.assertEqual(errors, 0)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].strategy, "MomentumScalp")
        self.assertEqual(signals[0].confidence, 0.88)

    def test_non_scalp_candidates_still_use_max_confidence(self):
        signals, hit_counts, errors = _run_scan([
            _sig("TEST", "Technical", 0.75),
            _sig("TEST", "MomentumContinuation", 0.92),
            _sig("TEST", "Sentiment", 0.60),
        ])

        self.assertEqual(errors, 0)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].strategy, "MomentumContinuation")
        self.assertEqual(signals[0].confidence, 0.92)
        self.assertEqual(hit_counts, {"MomentumContinuation": 1})


if __name__ == "__main__":
    unittest.main()
