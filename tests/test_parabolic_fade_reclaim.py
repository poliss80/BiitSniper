"""Tests for ParabolicFadeReclaimStrategy — parabolic spike -> fade -> base -> reclaim."""
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytz

import engine.equity.strategies as strategies
from engine.equity.strategies import ParabolicFadeReclaimStrategy

ET = pytz.timezone("America/New_York")


class _FakeDatetime(datetime.datetime):
    """datetime.now(tz) pinned to 11:30 ET (elapsed=120 min, inside the 60-330 window)."""
    @classmethod
    def now(cls, tz=None):
        return datetime.datetime(2026, 9, 9, 11, 30, 0, tzinfo=tz)


class _FakeDatetimeEarly(datetime.datetime):
    """datetime.now(tz) pinned to 10:00 ET (elapsed=30 min, before the entry window)."""
    @classmethod
    def now(cls, tz=None):
        return datetime.datetime(2026, 9, 9, 10, 0, 0, tzinfo=tz)


_FAKE_DATETIME_MODULE = SimpleNamespace(
    datetime=_FakeDatetime,
    timedelta=datetime.timedelta,
    timezone=datetime.timezone,
)

_FAKE_DATETIME_MODULE_EARLY = SimpleNamespace(
    datetime=_FakeDatetimeEarly,
    timedelta=datetime.timedelta,
    timezone=datetime.timezone,
)


def _identity_boost(symbol, conf, *a, **k):
    return conf


def _build_daily(n=20, avg_vol=1_000_000.0):
    """20 daily bars, avg volume 1M (scan averages iloc[:-1] -> 1M)."""
    closes = [50.0 + 0.3 * i for i in range(n)]
    idx = pd.date_range(end="2026-09-08", periods=n, freq="B")
    return pd.DataFrame({
        "open":   closes,
        "high":   [c + 0.3 for c in closes],
        "low":    [c - 0.3 for c in closes],
        "close":  closes,
        "volume": [float(avg_vol)] * n,
    }, index=idx)


def _build_intraday(spike_high=10.60, base_level=8.35, cons_broken=False,
                    end_jump=0.15, n=120):
    """120 x 1m bars (09:30-11:29 ET): open 5.00 -> parabolic spike -> fade -> chop.

    Default shape:
      bars 0-40   : ramp 5.00 -> 10.40, highs[40] = spike_high (+112% from open)
      bars 41-70  : fade down to base_level (fade ~21% off the 10.60 HOD)
      bars 71-114 : tight chop around base_level (consolidation base)
      bars 115-119: reclaim leg in the still-forming 5m bin, closing above the base
    Volume: 15k/min during bars 30-50 (spike/flush), 4k/min elsewhere. Total 711k;
    at elapsed=120 min with 1M avg daily volume -> rvol ~2.3x. Completed-5m max
    bin volume is 75k vs 20k consolidation bins -> volume contraction.
    """
    opens, highs, lows, closes, vols = [], [], [], [], []
    for i in range(n):
        if i <= 40:
            price = 5.0 + (10.40 - 5.0) * i / 40
        elif i <= 70:
            price = 10.40 - (10.40 - base_level) * (i - 40) / 30
        elif i <= 114:
            price = base_level + 0.06 * ((i % 4) / 3.0)
        else:
            price = base_level + end_jump + 0.02 * (i - 114)
        o = price
        c = price + (0.01 if i < 115 else 0.02)
        h = max(o, c) + 0.02
        lo = min(o, c) - 0.02
        if cons_broken and 100 <= i <= 114:
            h = c + 1.00
            lo = o - 1.00
        opens.append(o)
        highs.append(h)
        lows.append(lo)
        closes.append(c)
        vols.append(15_000.0 if 30 <= i <= 50 else 4_000.0)
    highs[40] = spike_high
    idx = pd.date_range(start="2026-09-09 09:30", periods=n, freq="min")
    return pd.DataFrame({
        "open":   opens,
        "high":   highs,
        "low":    lows,
        "close":  closes,
        "volume": vols,
    }, index=idx)


def _scan(intraday, daily, symbol="TEST", fake_dt_module=_FAKE_DATETIME_MODULE):
    def fake_get_bars(symbol, period, interval, *a, **k):
        return intraday.copy() if interval == "1m" else daily.copy()
    with patch.object(strategies, "get_bars", side_effect=fake_get_bars), \
         patch.object(strategies, "_sa_metrics_boost", _identity_boost), \
         patch.object(strategies, "datetime", fake_dt_module):
        return ParabolicFadeReclaimStrategy().scan(symbol)


class TestParabolicFadeReclaim(unittest.TestCase):

    def test_rejects_without_parabolic_spike(self):
        # Spike high only 8.50 from a 5.00 open (+70%) -> below the +100% floor
        sig = _scan(_build_intraday(spike_high=8.50), _build_daily())
        self.assertIsNone(sig)

    def test_rejects_insufficient_fade(self):
        # Post-spike path holds ~9.50 -> only ~10% off the 10.60 HOD (< 20%)
        with self.assertLogs(strategies.log, level="DEBUG") as captured:
            sig = _scan(_build_intraday(base_level=9.55), _build_daily())
        self.assertIsNone(sig)
        self.assertTrue(any("insufficient fade" in message for message in captured.output))

    def test_rejects_outside_entry_window(self):
        # 10:00 ET -> elapsed=30 min, before the 60-minute window start
        sig = _scan(_build_intraday(), _build_daily(),
                    fake_dt_module=_FAKE_DATETIME_MODULE_EARLY)
        self.assertIsNone(sig)

    def test_rejects_without_consolidation_base(self):
        # Consolidation bars range +/-1.00 around mid -> far wider than the band
        sig = _scan(_build_intraday(cons_broken=True), _build_daily())
        self.assertIsNone(sig)

    def test_fires_on_spike_fade_consolidation_reclaim(self):
        sig = _scan(_build_intraday(), _build_daily())

        self.assertIsNotNone(sig)
        self.assertEqual(sig.action, "buy")
        self.assertEqual(sig.strategy, "ParabolicFadeReclaim")
        self.assertGreaterEqual(sig.confidence, 0.72)
        self.assertLessEqual(sig.confidence, 0.95)
        self.assertIsNotNone(sig.atr_stop)
        self.assertGreater(sig.atr_stop, 0)
        # Stop is the consolidation swing low (not capped): ~8.33 vs entry ~8.62
        self.assertLessEqual(sig.atr_stop, sig.price * 0.04 + 1e-9)
        self.assertAlmostEqual(sig.atr_stop, sig.price - 8.33, places=2)
        self.assertIn("TP plan: 50% off at 1:1 R:R", sig.reason)
        self.assertIn("vol-contract", sig.reason)

    def test_stop_capped_at_max_stop_pct(self):
        # Reclaim leg jumps far above the base -> structural stop > 4% -> capped
        sig = _scan(_build_intraday(end_jump=1.0), _build_daily())

        self.assertIsNotNone(sig)
        self.assertAlmostEqual(sig.atr_stop, sig.price * 0.04, places=6)
        self.assertIn("(capped)", sig.reason)

    def test_rejects_inverse_etfs(self):
        sig = _scan(_build_intraday(), _build_daily(), symbol="SQQQ")
        self.assertIsNone(sig)


if __name__ == "__main__":
    unittest.main()
