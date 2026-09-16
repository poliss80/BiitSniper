"""Regression tests for the active equity trendline-breakout adapter."""

import unittest
from unittest.mock import patch

import pandas as pd

from engine.equity.strategies import SweepeaStrategy, TrendlineBreakoutStrategy, get_strategy_instances
from engine.config import TRENDLINE_BREAKOUT
from scripts.auto_trendline import AutoTrendline, TrendlineConfig
from scripts.trendline_breakout import make_synthetic_ohlcv


class TrendlineBreakoutStrategyTests(unittest.TestCase):
    def test_adaptive_engine_archives_broken_lines_with_a_cap(self):
        close = [10, 9, 11, 8, 12, 9, 13, 10, 14, 9, 15, 8, 16, 7, 17, 6, 18, 5, 19, 4, 20]
        data = pd.DataFrame({
            "high": [price + 0.2 for price in close],
            "low": [price - 0.2 for price in close],
            "close": close,
        }, index=pd.date_range("2026-01-01", periods=len(close), freq="h"))

        result = AutoTrendline(TrendlineConfig(
            primary_lookback=2,
            bars_from_edge=1,
            breakout_threshold_price=0.5,
            max_history_lines=2,
        )).run(data)

        self.assertGreater(len(result.breakouts), 0)
        self.assertLessEqual(len(result.history_upper), 2)
        self.assertTrue(all(line.side == "upper" for line in result.history_upper))

    def test_adaptive_mode_uses_the_existing_equity_signal_contract(self):
        bars = make_synthetic_ohlcv(rows=160)
        original_engine = TRENDLINE_BREAKOUT["engine"]
        TRENDLINE_BREAKOUT["engine"] = "adaptive"
        try:
            with patch("engine.equity.strategies.get_bars", return_value=bars), \
                    patch("engine.equity.strategies.AutoTrendline.latest", return_value={
                        "resistance_break": True,
                        "support_break": False,
                        "close": 102.0,
                        "resistance": object(),
                        "resistance_price": 101.0,
                    }), \
                    patch("engine.equity.strategies._calc_atr14", return_value=1.5):
                signal = TrendlineBreakoutStrategy().scan("AAPL")
        finally:
            TRENDLINE_BREAKOUT["engine"] = original_engine

        self.assertIsNotNone(signal)
        self.assertEqual(signal.action, "buy")
        self.assertEqual(signal.strategy, "TrendlineBreakout")
        self.assertEqual(signal.atr_stop, 1.5)

    def test_strategy_converts_confirmed_breakout_to_equity_signal(self):
        original_engine = TRENDLINE_BREAKOUT["engine"]
        TRENDLINE_BREAKOUT["engine"] = "regression"
        try:
            with patch("engine.equity.strategies.get_bars", return_value=make_synthetic_ohlcv()):
                signal = TrendlineBreakoutStrategy().scan("AAPL")
        finally:
            TRENDLINE_BREAKOUT["engine"] = original_engine

        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "AAPL")
        self.assertEqual(signal.action, "buy")
        self.assertEqual(signal.strategy, "TrendlineBreakout")
        self.assertGreater(signal.atr_stop, 0.0)
        self.assertIn("target $", signal.reason)

    def test_strategy_is_in_active_registry(self):
        strategy_names = {strategy.__class__.__name__ for strategy in get_strategy_instances()}
        self.assertIn("TrendlineBreakoutStrategy", strategy_names)

    @patch("engine.equity.strategies._is_bull_regime", return_value=True)
    @patch("engine.equity.strategies.get_bars")
    def test_sweepea_rejects_low_price_and_low_liquidity_names(self, mocked_get_bars, _mock_regime):
        closes = [4.10 + i * 0.03 for i in range(90)]
        lows = [max(3.8, c - 0.25) for c in closes]
        highs = [c + 0.30 for c in closes]
        volumes = [200_000] * 89 + [220_000]
        daily = pd.DataFrame({
            "open": [c - 0.05 for c in closes],
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        })
        mocked_get_bars.return_value = daily

        signal = SweepeaStrategy().scan("PENNY")

        self.assertIsNone(signal)


if __name__ == "__main__":
    unittest.main()