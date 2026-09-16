"""
tests/test_schwab_intraday_fetch.py

Verifies that multi-day minute-frequency bar fetches use a Schwab date range
(startDate/endDate, epoch ms) instead of the invalid periodType=day&period=N
combination (which Schwab rejects with HTTP 400 for 1-minute bars), that the
daily path is unchanged, and that clean-empty results are negative-cached.
"""

import time
import unittest
from unittest import mock

import pandas as pd

import engine.utils.bars as bars


def _canned_candles(n: int = 5) -> dict:
    base = 1_700_000_000_000  # epoch ms
    return {
        "candles": [
            {
                "datetime": base + i * 60_000,
                "open": 100.0 + i,
                "high": 101.0 + i,
                "low": 99.0 + i,
                "close": 100.5 + i,
                "volume": 10_000 + i,
            }
            for i in range(n)
        ]
    }


class _CapturingClient:
    """Fake Schwab market-data client that captures get_candles kwargs."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_candles(self, symbol, **kwargs):
        self.calls.append({"symbol": symbol, **kwargs})
        return self.payload


class TestSchwabIntradayFetch(unittest.TestCase):
    def setUp(self):
        bars.clear_bar_cache()
        with bars._bar_cache_lock:
            bars._neg_cache.clear()

    def _patch_client(self, payload):
        client = _CapturingClient(payload)
        patcher = mock.patch(
            "engine.broker.schwab_client.get_schwab_market_data_client",
            return_value=client,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def test_minute_uses_date_range_not_period(self):
        client = self._patch_client(_canned_candles())
        bars.get_bars("X", "6d", "1m")

        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        # Minute path must send an epoch-ms date range...
        self.assertIn("start_ms", call)
        self.assertIn("end_ms", call)
        self.assertIsInstance(call["start_ms"], int)
        self.assertIsInstance(call["end_ms"], int)
        self.assertLess(call["start_ms"], call["end_ms"])
        # ...and must NOT send periodType="day" / period=6.
        self.assertNotEqual(call.get("period_type"), "day")
        self.assertNotIn("period", call)
        # Roughly 6 calendar days of range
        span_days = (call["end_ms"] - call["start_ms"]) / 86_400_000
        self.assertAlmostEqual(span_days, 6, delta=0.1)

    def test_minute_returns_bars(self):
        self._patch_client(_canned_candles(n=7))
        df = bars.get_bars("X", "6d", "1m")

        self.assertFalse(df.empty)
        self.assertEqual(len(df), 7)
        for col in ("time", "open", "high", "low", "close", "volume"):
            self.assertIn(col, df.columns)

    def test_daily_still_uses_period(self):
        client = self._patch_client(_canned_candles())
        bars.get_bars("X", "20d", "1d")

        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call.get("period_type"), "year")
        self.assertEqual(call.get("period"), 1)
        self.assertEqual(call.get("frequency_type"), "daily")
        self.assertIsNone(call.get("start_ms"))
        self.assertIsNone(call.get("end_ms"))

    def test_negative_cache(self):
        client = self._patch_client({"candles": []})
        sym = "ILLQD"

        first = bars.get_bars(sym, "6d", "1m")
        self.assertTrue(first.empty)
        self.assertEqual(len(client.calls), 1)

        # Second call within TTL must NOT hit the API again
        bars.clear_bar_cache()  # drop per-cycle cache to isolate neg-cache
        second = bars.get_bars(sym, "6d", "1m")
        self.assertTrue(second.empty)
        self.assertEqual(len(client.calls), 1)

        # A different symbol still fetches normally
        bars.get_bars("OTHER", "6d", "1m")
        self.assertEqual(len(client.calls), 2)

        # Non-empty result pops the symbol from the negative cache.
        # Seed an EXPIRED entry so the fetch actually runs.
        client.payload = _canned_candles()
        with bars._bar_cache_lock:
            bars._neg_cache[sym] = time.time() - bars._NEG_TTL_SEC - 1
        bars.clear_bar_cache()
        df = bars.get_bars(sym, "6d", "1m")
        self.assertFalse(df.empty)
        with bars._bar_cache_lock:
            self.assertNotIn(sym, bars._neg_cache)


if __name__ == "__main__":
    unittest.main()
