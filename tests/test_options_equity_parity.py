import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

import engine.options.strategies as strategies
from engine.options.strategies import (
    GapBreakoutCallStrategy,
    MomentumContinuationCallStrategy,
    OptionSignal,
    OptionsChainInfo,
    _build_equity_parity_call,
    _filter_signals_by_market_regime,
)


class OptionsEquityParityTests(unittest.TestCase):
    def test_shared_builder_applies_option_quality_gates(self):
        chain = OptionsChainInfo(
            symbol="TEST",
            expiry=datetime.date.today() + datetime.timedelta(days=14),
            calls=pd.DataFrame(),
            puts=pd.DataFrame(),
            spot_price=100.0,
            iv_rank=20.0,
            hv_30=35.0,
            atr14=4.0,
        )
        strike = {"strike": 100.0, "mid": 1.0, "delta": 0.55, "iv_pct": 35.0, "openinterest": 1000}
        with patch.object(strategies, "_get_filters", return_value={
            "IV_RANK_CALL_MAX": 35,
            "MAX_PREMIUM_SPOT": 3.0,
            "MIN_RR": 1.5,
        }), patch.object(strategies, "_pick_strike", return_value=strike), patch.object(
            strategies, "_calc_rr", return_value=2.0
        ):
            signal = _build_equity_parity_call(
                "TEST", 100.0, chain, "continuation", 0.80, "MomentumContinuationCall"
            )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.strategy, "MomentumContinuationCall")
        self.assertEqual(signal.action, "buy_to_open")
        self.assertEqual(signal.strike, 100.0)

    def test_new_strategies_are_call_side_in_bear_regime(self):
        expiry = datetime.date.today() + datetime.timedelta(days=14)
        signals = [
            OptionSignal("TEST", "call", "buy_to_open", 100, expiry, 1, 0.90, "test", "MomentumContinuationCall"),
            OptionSignal("TEST", "call", "buy_to_open", 100, expiry, 1, 0.90, "test", "GapBreakoutCall"),
        ]
        with patch.object(strategies, "_classify_symbol_tier", return_value="major_cap"):
            filtered = _filter_signals_by_market_regime(signals, "BEARISH", -0.9)
        self.assertEqual(filtered, [])

    def test_strategy_names_are_distinct_from_daily_momentum_call(self):
        self.assertEqual(MomentumContinuationCallStrategy.name, "MomentumContinuationCall")
        self.assertEqual(GapBreakoutCallStrategy.name, "GapBreakoutCall")
        self.assertNotEqual(MomentumContinuationCallStrategy.name, "MomentumCall")


if __name__ == "__main__":
    unittest.main()
