"""Scan-only tests for PreMarketMomentumScalpStrategy (premarket MomentumScalp variant)."""
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from engine.equity import strategies as equity_strategies
from engine.equity.strategies import PreMarketMomentumScalpStrategy


def _fake_datetime_module(hour, minute):
    """datetime module stand-in with now(tz) pinned to 2026-09-25 hour:minute ET."""
    class _FakeDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            naive = datetime.datetime(2026, 9, 25, hour, minute, 0)
            if tz is None:
                return naive
            # pytz zones must be localized (passing tzinfo= directly yields the
            # broken LMT offset); zoneinfo/fixed zones take tzinfo= fine.
            return tz.localize(naive) if hasattr(tz, "localize") else naive.replace(tzinfo=tz)
    return SimpleNamespace(
        datetime=_FakeDatetime,
        timedelta=datetime.timedelta,
        timezone=datetime.timezone,
    )


_FAKE_PM_DATETIME_MODULE = _fake_datetime_module(7, 0)   # 07:00 ET, mid pre-market


class PreMarketMomentumScalpScanTests(unittest.TestCase):
    """Premarket squeeze entry guards: 04:00–09:25 ET window, float cap,
    3%–12% gap band, current-bar volume surge, fresh 5-bar breakout.
    Accepted signals reuse the exact "MomentumScalp" strategy tag."""

    CFG = {
        "enabled": True,
        "window_start_min": 4 * 60,
        "window_end_min": 9 * 60 + 25,
        "max_float_shares": 20_000_000.0,
        "min_gap_pct": 3.0,
        "max_gap_pct": 12.0,
        "min_premarket_bars": 5,
        "breakout_lookback_bars": 5,
        "recent_high_bars": 10,
        "near_high_pct": 0.995,
        "max_bar_age_min": 5,
    }

    SCALP_CFG = {
        "enabled": True, "min_rvol": 3.0, "min_price_up_pct": 5.0,
        "break_lookback_min": 10, "bar_volume_mult": 1.5,
        "position_size_mult": 2.0, "max_bp_pct": 20.0,
        "tp_pct": 5.0, "ratchet_giveback_pct": 2.5,
    }

    @staticmethod
    def _daily(n=5, prior_close=10.0):
        """Daily bars with a flat prior close of 10.0 (scan reads iloc[-2])."""
        closes = [float(prior_close)] * n
        idx = pd.date_range(end="2026-09-24", periods=n, freq="B")
        return pd.DataFrame({
            "open":   closes,
            "high":   [c + 0.3 for c in closes],
            "low":    [c - 0.3 for c in closes],
            "close":  closes,
            "volume": [1_000_000.0] * n,
        }, index=idx)

    @staticmethod
    def _pm_bars(cur_close=10.6, cur_vol=2000.0, base_vol=1000.0, breakout=True, n=30):
        """30 x 1m bars 06:30-06:59 ET ramping 10.20 -> ~10.50 (prior close 10.0).
        breakout=True: last bar closes above the preceding 5-bar high;
        breakout=False: last bar closes just under it, but still within 0.5%
        of the recent high so only the breakout guard can reject."""
        opens, highs, lows, closes, vols = [], [], [], [], []
        for i in range(n):
            price = 10.2 + 0.3 * i / (n - 1)
            o, c = price, price + 0.01
            opens.append(o)
            closes.append(c)
            highs.append(c + 0.02)
            lows.append(o - 0.05)
            vols.append(float(base_vol))
        prior_5_high = max(highs[-6:-1])  # highest high of the 5 bars before the current one
        vols[-1] = float(cur_vol)
        closes[-1] = cur_close if breakout else min(cur_close, prior_5_high - 0.01)
        opens[-1] = closes[-1] - 0.01
        highs[-1] = closes[-1] + 0.02
        idx = pd.date_range(start="2026-09-25 06:30", periods=n, freq="min")
        return pd.DataFrame({
            "open": opens, "high": highs, "low": lows,
            "close": closes, "volume": vols,
        }, index=idx)

    def _scan(self, pm_bars, float_shares=5_000_000.0, symbol="TEST", prior_close=10.0,
              now_module=_FAKE_PM_DATETIME_MODULE, cfg=None):
        def fake_get_bars(sym, period, interval, *a, **k):
            return self._daily(prior_close=prior_close).copy()
        with patch.object(equity_strategies, "get_bars", side_effect=fake_get_bars), \
             patch.object(equity_strategies, "get_premarket_bars", lambda s, *a, **k: pm_bars.copy()), \
             patch.object(equity_strategies, "_get_float_shares", lambda s: float_shares), \
             patch.object(equity_strategies, "_sa_metrics_boost", lambda s, c, *a, **k: c), \
             patch.object(equity_strategies, "PREMARKET_MOMENTUM_SCALP", cfg or self.CFG), \
             patch.object(equity_strategies, "MOMENTUM_SCALP", self.SCALP_CFG), \
             patch.object(equity_strategies, "datetime", now_module):
            return PreMarketMomentumScalpStrategy().scan(symbol)

    def test_qualifies_mid_premarket(self):
        sig = self._scan(self._pm_bars())
        self.assertIsNotNone(sig, "gapped low-float runner on 2x bar volume and a breakout should fire")
        self.assertEqual(sig.strategy, "MomentumScalp")
        self.assertIn("gap=+6.0%", sig.reason)
        self.assertIn("barvol=2.0x", sig.reason)
        self.assertIn("break>$", sig.reason)

    def test_no_signal_when_disabled(self):
        cfg = dict(self.CFG, enabled=False)
        sig = self._scan(self._pm_bars(), cfg=cfg)
        self.assertIsNone(sig, "feature flag off must reject")

    def test_no_signal_before_window_start(self):
        sig = self._scan(self._pm_bars(), now_module=_fake_datetime_module(3, 30))
        self.assertIsNone(sig, "03:30 ET is before the 04:00 window start")

    def test_no_signal_at_window_end(self):
        sig = self._scan(self._pm_bars(), now_module=_fake_datetime_module(9, 25))
        self.assertIsNone(sig, "09:25 ET is at/after the window end")

    def test_rejects_float_over_cap(self):
        sig = self._scan(self._pm_bars(), float_shares=25_000_000.0)
        self.assertIsNone(sig, "float above the 20M cap must reject")

    def test_rejects_gap_below_minimum(self):
        # cur_close 10.53 vs prior close 10.25 -> +2.7% < 3% minimum
        sig = self._scan(self._pm_bars(cur_close=10.53), prior_close=10.25)
        self.assertIsNone(sig, "gap under 3% must reject")

    def test_rejects_gap_over_maximum(self):
        sig = self._scan(self._pm_bars(cur_close=11.5))  # +15% > 12% cap
        self.assertIsNone(sig, "gap over the 12% cap must reject (no chasing)")

    def test_rejects_weak_current_bar_volume(self):
        sig = self._scan(self._pm_bars(cur_vol=1000.0))  # 1.0x trailing avg < 1.5x
        self.assertIsNone(sig, "weak current-bar volume must reject")

    def test_rejects_no_fresh_breakout(self):
        sig = self._scan(self._pm_bars(breakout=False))
        self.assertIsNone(sig, "close below the preceding 5-bar high must reject")

    def test_rejects_insufficient_premarket_bars(self):
        sig = self._scan(self._pm_bars(n=4))
        self.assertIsNone(sig, "fewer than min_premarket_bars valid PM bars must reject")


if __name__ == "__main__":
    unittest.main()
