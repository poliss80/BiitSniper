"""Tests for MomentumContinuationStrategy — buys high-RVOL strength breaking session high."""
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytz

import engine.equity.strategies as strategies
from engine.equity.strategies import MomentumContinuationStrategy

ET = pytz.timezone("America/New_York")


class _FakeDatetime(datetime.datetime):
    """datetime.now(tz) pinned to 11:00 ET (mid regular session, elapsed=90 min)."""
    @classmethod
    def now(cls, tz=None):
        return datetime.datetime(2026, 9, 9, 11, 0, 0, tzinfo=tz)


_FAKE_DATETIME_MODULE = SimpleNamespace(
    datetime=_FakeDatetime,
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


def _build_intraday(total_vol=600_000.0, spike_high_at=None, n=90):
    """90 x 1m bars: open 100.00 -> close 102.50 (+2.5%), high = close + 0.10.

    At 11:00 ET elapsed=90min -> elapsed_frac = 90/390. With avg daily vol 1M,
    total_vol=600k gives rvol = 600000 / (1e6 * 90/390) = 2.6x.
    """
    opens  = [100.0 + (2.5 * i / n) for i in range(n)]
    closes = [100.0 + (2.5 * (i + 1) / n) for i in range(n)]
    highs  = [c + 0.10 for c in closes]
    if spike_high_at is not None:
        highs[spike_high_at] = 103.50   # earlier spike high inside the lookback window
    lows   = [min(o, c) - 0.05 for o, c in zip(opens, closes)]
    idx = pd.date_range(start="2026-09-09 09:30", periods=n, freq="min")
    return pd.DataFrame({
        "open":   opens,
        "high":   highs,
        "low":    lows,
        "close":  closes,
        "volume": [float(total_vol) / n] * n,
    }, index=idx)


def _scan(intraday, daily):
    def fake_get_bars(symbol, period, interval, *a, **k):
        return intraday.copy() if interval == "1m" else daily.copy()
    with patch.object(strategies, "get_bars", side_effect=fake_get_bars), \
         patch.object(strategies, "_sa_metrics_boost", _identity_boost), \
         patch.object(strategies, "datetime", _FAKE_DATETIME_MODULE):
        return MomentumContinuationStrategy().scan("TEST")


class TestMomentumContinuation(unittest.TestCase):

    def test_fires_on_strength(self):
        # Up 2.5% from open, at session high, rvol ~2.6x -> buy Signal
        sig = _scan(_build_intraday(total_vol=600_000.0), _build_daily())
        self.assertIsNotNone(sig)
        self.assertEqual(sig.action, "buy")
        self.assertEqual(sig.strategy, "MomentumContinuation")
        self.assertGreaterEqual(sig.confidence, 0.74)
        self.assertLessEqual(sig.confidence, 0.95)
        self.assertIsNotNone(sig.atr_stop)
        self.assertGreater(sig.atr_stop, 0)

    def test_no_fire_without_volume(self):
        # Same price action but rvol ~0.4x -> None
        sig = _scan(_build_intraday(total_vol=100_000.0), _build_daily())
        self.assertIsNone(sig)

    def test_no_fire_when_not_breaking_high(self):
        # Up 2.5% with rvol ~2.6x but current close below a recent spike high -> None
        sig = _scan(_build_intraday(total_vol=600_000.0, spike_high_at=70), _build_daily())
        self.assertIsNone(sig)


if __name__ == "__main__":
    unittest.main()
