"""Tests for Sweepea Path A levers: dynamic confidence (Lever B) + trend gate (Lever C)."""
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytz

import engine.equity.strategies as strategies
from engine.equity.strategies import SweepeaStrategy

ET = pytz.timezone("America/New_York")


class _FakeDatetime(datetime.datetime):
    """datetime.now(tz) pinned to 11:00 ET so Path A (hour >= 10) is active."""
    @classmethod
    def now(cls, tz=None):
        return datetime.datetime(2026, 9, 8, 11, 0, 0, tzinfo=tz)


_FAKE_DATETIME_MODULE = SimpleNamespace(
    datetime=_FakeDatetime,
    timedelta=datetime.timedelta,
    timezone=datetime.timezone,
)


def _identity_boost(symbol, conf, *a, **k):
    return conf


def _build_daily(closes, volumes=None, tag_low_ema8=True, pin_close_to_ema8=False):
    """Build a daily OHLCV df from a close series.

    tag_low_ema8:      last bar's low tags the 8-EMA (low <= ema8*1.005) so pb8 holds.
    pin_close_to_ema8: last bar's close is set to the 8-EMA (marginal reclaim,
                       close >= ema8*0.995 holds exactly).
    """
    closes = [float(c) for c in closes]
    n = len(closes)
    if volumes is None:
        volumes = [1_000_000.0] * n
    if pin_close_to_ema8:
        for _ in range(3):  # iterate: moving close shifts the EMA slightly
            ema8 = float(pd.Series(closes).ewm(span=8, adjust=False).mean().iloc[-1])
            closes[-1] = ema8
    ema8_final = float(pd.Series(closes).ewm(span=8, adjust=False).mean().iloc[-1])
    highs = [c + 0.4 for c in closes]
    lows = [c - 0.4 for c in closes]
    if tag_low_ema8:
        lows[-1] = min(ema8_final * 1.004, closes[-1])
    idx = pd.date_range(end="2026-09-08", periods=n, freq="B")
    return pd.DataFrame({
        "open":   closes,
        "high":   highs,
        "low":    lows,
        "close":  closes,
        "volume": [float(v) for v in volumes],
    }, index=idx)


def _scan(df, require_trend=True):
    def fake_get_bars(symbol, period, interval, *a, **k):
        return df.copy() if interval == "1d" else pd.DataFrame()
    with patch.object(strategies, "get_bars", side_effect=fake_get_bars), \
         patch.object(strategies, "_is_bull_regime", return_value=True), \
         patch.object(strategies, "_sa_metrics_boost", _identity_boost), \
         patch.object(strategies, "datetime", _FAKE_DATETIME_MODULE), \
         patch.object(strategies, "SWEEPEA_DYNAMIC_CONFIDENCE", True), \
         patch.object(strategies, "SWEEPEA_REQUIRE_TREND", require_trend):
        return SweepeaStrategy().scan("TEST")


class TestSweepeaConfidenceTrend(unittest.TestCase):

    def test_strong_setup_high_confidence(self):
        # Steep rising stack, close well above ema8, current volume ~3x average
        closes = [40 + 0.8 * i for i in range(90)]
        volumes = [1_000_000.0] * 89 + [3_000_000.0]
        df = _build_daily(closes, volumes)
        sig = _scan(df)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.action, "buy")
        self.assertGreaterEqual(sig.confidence, 0.85)

    def test_weak_flat_setup_low_confidence(self):
        # Rise, then flat/mild-decline tail: ema20 > ema50 still holds but the
        # ema8 > ema20 leg is broken (weak setup). rvol ~1x, marginal reclaim.
        closes = ([40 + 0.4 * i for i in range(60)]
                  + [63.6 - 0.25 * j for j in range(1, 26)]
                  + [57.6, 57.9, 58.2, 58.5, 58.5])
        df = _build_daily(closes, pin_close_to_ema8=True)
        # DEVIATION: this weak/flat series fails the Lever C trend gate
        # (ema8 < ema20), so SWEEPEA_REQUIRE_TREND is patched False for THIS
        # test only in order to assert on the dynamic confidence value.
        sig = _scan(df, require_trend=False)
        self.assertIsNotNone(sig)
        self.assertLessEqual(sig.confidence, 0.75)

    def test_downtrend_rejected(self):
        # Broken stack, close < ema50, last bar tags ema8 -> trend gate rejects
        closes = [100 - 0.6 * i for i in range(90)]
        df = _build_daily(closes, pin_close_to_ema8=True)
        sig = _scan(df)
        self.assertIsNone(sig)

    def test_clean_uptrend_pullback_buy(self):
        # Clean stacked rising EMAs, last bar pulls back to tag ema8
        closes = [30 + 0.5 * i for i in range(90)]
        df = _build_daily(closes)
        sig = _scan(df)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.action, "buy")

    def test_sweepea_pullback_now_passes_trend_advisory(self):
        # Moderate/partially-stacked trend: strong rise, shallow pullback, mild
        # recovery -> ema8 < ema20 (stack leg broken) while close stays above
        # ema50. Previously HARD-BLOCKED by the Lever C trend gate (trend_ok
        # False); now trend is advisory, so the pullback fires with LOWER
        # confidence than the strong fully-stacked case.
        closes = ([50 + 0.5 * i for i in range(76)]
                  + [87.5 - 0.7 * j for j in range(1, 10)]
                  + [81.2 + 0.6 * k for k in range(1, 5)])
        df = _build_daily(closes)
        # Sanity: this series breaks the old hard gate (ema8 !> ema20)
        ema8  = float(df["close"].ewm(span=8,  adjust=False).mean().iloc[-1])
        ema20 = float(df["close"].ewm(span=20, adjust=False).mean().iloc[-1])
        self.assertLess(ema8, ema20)   # stack broken -> old trend gate would reject
        sig = _scan(df, require_trend=True)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.action, "buy")
        # Strong fully-stacked comparison case must score strictly higher
        strong_closes = [40 + 0.8 * i for i in range(90)]
        strong_df = _build_daily(strong_closes,
                                 [1_000_000.0] * 89 + [3_000_000.0])
        strong_sig = _scan(strong_df, require_trend=True)
        self.assertIsNotNone(strong_sig)
        self.assertLess(sig.confidence, strong_sig.confidence)


if __name__ == "__main__":
    unittest.main()
