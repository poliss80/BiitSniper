"""Tests for bar normalization and current-day filtering."""
import datetime
import unittest

import pandas as pd
import pytz

from engine.utils.bars import _filter_current_day_minute_bars

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


if __name__ == "__main__":
    unittest.main()