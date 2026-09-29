"""Tests for bar normalization and current-day filtering."""
import datetime
import unittest

import pandas as pd
import pytz

from engine.utils.bars import _filter_current_day_minute_bars, completed_daily_history

ET = pytz.timezone("America/New_York")


class TestCurrentDayMinuteBars(unittest.TestCase):

    def test_filters_mixed_dates_using_eastern_calendar_date(self):
        bars = pd.DataFrame({
            "time": pd.to_datetime([
                "2026-09-14 13:59:00+00:00",
                "2026-09-15 10:59:00+00:00",
                "2026-09-15 14:00:00+00:00",
            ]),
            "close": [99.0, 100.0, 101.0],
        })

        filtered = _filter_current_day_minute_bars(
            bars,
            now=ET.localize(datetime.datetime(2026, 9, 15, 12, 0)),
        )

        self.assertEqual(filtered["close"].tolist(), [100.0, 101.0])
        self.assertTrue(filtered["time"].dt.tz is not None)

    def test_preserves_all_current_day_bars_including_extended_hours(self):
        bars = pd.DataFrame({
            "time": pd.date_range("2026-09-15 07:00", periods=3, freq="h", tz=ET),
            "close": [100.0, 101.0, 102.0],
        })

        filtered = _filter_current_day_minute_bars(
            bars,
            now=ET.localize(datetime.datetime(2026, 9, 15, 12, 0)),
        )

        self.assertEqual(len(filtered), 3)
        self.assertEqual(filtered["time"].iloc[0].hour, 7)
        self.assertEqual(filtered["time"].iloc[-1].hour, 9)

    def test_empty_and_no_current_day_data_are_safe(self):
        empty = pd.DataFrame(columns=["time", "close"])
        old = pd.DataFrame({
            "time": pd.to_datetime(["2026-09-14 10:00"], utc=True),
            "close": [99.0],
        })
        now = ET.localize(datetime.datetime(2026, 9, 15, 12, 0))

        self.assertTrue(_filter_current_day_minute_bars(empty, now).empty)
        self.assertTrue(_filter_current_day_minute_bars(old, now).empty)


class TestCompletedDailyHistory(unittest.TestCase):
    """completed_daily_history: robust prior close + last-N completed sessions,
    regardless of whether the provider includes today's partial daily candle."""

    TODAY = datetime.date(2026, 9, 29)  # a Tuesday

    @staticmethod
    def _frame(closes, dates, vols=None):
        vols = vols or [1_000_000.0] * len(closes)
        return pd.DataFrame(
            {"close": closes, "volume": vols},
            index=pd.DatetimeIndex(list(dates)),
        )

    def test_excludes_todays_partial_candle(self):
        daily = self._frame(
            [10.0, 11.0, 50.0],
            ["2026-09-25", "2026-09-28", "2026-09-29"],
        )
        prior_close, completed = completed_daily_history(daily, self.TODAY, lookback=20)
        self.assertEqual(prior_close, 11.0)
        self.assertEqual(len(completed), 2)
        self.assertNotIn(pd.Timestamp("2026-09-29"), completed.index)

    def test_works_when_todays_candle_absent(self):
        daily = self._frame([10.0, 11.0], ["2026-09-25", "2026-09-28"])
        prior_close, completed = completed_daily_history(daily, self.TODAY, lookback=20)
        self.assertEqual(prior_close, 11.0)
        self.assertEqual(len(completed), 2)

    def test_returns_true_last_20_completed_sessions_from_long_frame(self):
        closes = [float(i) for i in range(1, 26)]            # 25 completed days
        vols = [10_000_000.0] * 5 + [1_000_000.0] * 20       # old high-vol tail-off
        dates = pd.date_range(end="2026-09-28", periods=25, freq="B")
        daily = self._frame(closes, dates, vols)
        prior_close, completed = completed_daily_history(daily, self.TODAY, lookback=20)
        self.assertEqual(prior_close, 25.0)
        self.assertEqual(len(completed), 20)
        self.assertEqual(completed["volume"].mean(), 1_000_000.0)
        self.assertEqual(completed["close"].iloc[0], 6.0)    # first 5 dropped

    def test_time_column_tz_aware_utc_uses_eastern_calendar_date(self):
        daily = pd.DataFrame({
            "time": pd.to_datetime([
                "2026-09-29 01:30:00+00:00",  # 2026-09-28 21:30 ET -> completed
                "2026-09-29 13:30:00+00:00",  # 2026-09-29 09:30 ET -> today, excluded
            ]),
            "close": [11.0, 50.0],
            "volume": [1_000_000.0, 1_000_000.0],
        })
        prior_close, completed = completed_daily_history(daily, self.TODAY, lookback=20)
        self.assertEqual(prior_close, 11.0)
        self.assertEqual(len(completed), 1)

    def test_unusable_or_empty_input_returns_none_and_empty(self):
        for bad in (None, pd.DataFrame(), pd.DataFrame({"close": [1.0]})):  # RangeIndex
            prior_close, completed = completed_daily_history(bad, self.TODAY, lookback=20)
            self.assertIsNone(prior_close)
            self.assertTrue(completed.empty)


if __name__ == "__main__":
    unittest.main()