"""Tests for MomentumScalp candidate-selection priority in scan_universe._scan_one."""
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytz

import engine.equity.scan as scan
from engine.equity.strategies import MomentumScalpStrategy, Signal

ET = pytz.timezone("America/New_York")


class _FakeStrategy:
    """Plain strategy stub — not a Technical/Sentiment/Momentum base, so the
    scan loop calls scan(symbol) with no extra args."""
    def __init__(self, signal):
        self._signal = signal
        self.calls = 0

    def scan(self, symbol):
        self.calls += 1
        return self._signal


class _FakeScalpStrategy(MomentumScalpStrategy):
    """isinstance-true MomentumScalp stub (for scalp-only routing tests)."""
    def __init__(self, signal):
        self._signal = signal
        self.calls = 0

    def scan(self, symbol):
        self.calls += 1
        return self._signal


def _market_state(bull=True):
    return SimpleNamespace(resolve_regime=lambda: bull, vix=None)


def _sig(symbol, strategy, conf):
    return Signal(symbol, "buy", 10.0, conf, "test", strategy)


def _run_scan(signals_for_symbol):
    """scan_universe with guardrails/strategies/news-annotation mocked out.

    ET time is pinned outside the midday chop window so the scalp-priority
    floor is MIN_SIGNAL_CONFIDENCE regardless of when the suite runs."""
    strats = [_FakeStrategy(s) for s in signals_for_symbol]
    with patch.object(scan, "clear_bar_cache"), \
         patch.object(scan, "_prefetch_snapshots"), \
         patch.object(scan, "_passes_guardrails", return_value=(True, None)), \
         patch.object(scan, "get_strategy_instances", return_value=strats), \
         patch.object(scan, "annotate_signal_with_news", side_effect=lambda s: s), \
         patch.object(scan, "MIN_SIGNAL_CONFIDENCE", 0.50), \
         patch.object(scan._cfg, "MIDDAY_CHOP_START", "11:30"), \
         patch.object(scan._cfg, "MIDDAY_CHOP_END", "13:00"), \
         patch.object(scan, "datetime") as fake_dt:
        fake_dt.datetime.now.return_value.strftime.return_value = "10:00"
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

    def test_sub_floor_scalp_does_not_suppress_qualified_other_candidate(self):
        """A scalp below MIN_SIGNAL_CONFIDENCE must not win by default — the
        qualifying MomentumContinuation candidate is preserved so the later
        confidence gate can use it."""
        strats = [_FakeStrategy(s) for s in [
            _sig("TEST", "MomentumContinuation", 0.92),
            _sig("TEST", "MomentumScalp", 0.60),
        ]]
        with patch.object(scan, "clear_bar_cache"), \
             patch.object(scan, "_prefetch_snapshots"), \
             patch.object(scan, "_passes_guardrails", return_value=(True, None)), \
             patch.object(scan, "get_strategy_instances", return_value=strats), \
             patch.object(scan, "annotate_signal_with_news", side_effect=lambda s: s), \
             patch.object(scan, "MIN_SIGNAL_CONFIDENCE", 0.72):
            signals, hit_counts, errors = scan.scan_universe(["TEST"], "neutral", _market_state())

        self.assertEqual(errors, 0)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].strategy, "MomentumContinuation")
        self.assertEqual(signals[0].confidence, 0.92)
        self.assertEqual(hit_counts, {"MomentumContinuation": 1})


class TestScalpPriorityActiveFloor(unittest.TestCase):
    """Scalp candidate priority must use the actual active BUY floor:
    max(adaptive scan floor, MIN_SIGNAL_CONFIDENCE, midday MIDDAY_MIN_CONFIDENCE).
    A scalp the orchestrator's midday gate would reject must not suppress a
    qualifying alternative candidate for the same symbol."""

    def _run(self, signals_for_symbol, hhmm):
        strats = [_FakeStrategy(s) for s in signals_for_symbol]
        with patch.object(scan, "clear_bar_cache"), \
             patch.object(scan, "_prefetch_snapshots"), \
             patch.object(scan, "_passes_guardrails", return_value=(True, None)), \
             patch.object(scan, "get_strategy_instances", return_value=strats), \
             patch.object(scan, "annotate_signal_with_news", side_effect=lambda s: s), \
             patch.object(scan, "MIN_SIGNAL_CONFIDENCE", 0.80), \
             patch.object(scan._cfg, "MIDDAY_CHOP_START", "11:30"), \
             patch.object(scan._cfg, "MIDDAY_CHOP_END", "13:00"), \
             patch.object(scan._cfg, "MIDDAY_MIN_CONFIDENCE", 0.88), \
             patch.object(scan, "datetime") as fake_dt:
            fake_dt.datetime.now.return_value.strftime.return_value = hhmm
            return scan.scan_universe(["TEST"], "neutral", _market_state())

    def test_midday_sub_floor_scalp_does_not_suppress_qualified_alternative(self):
        # Midday floor 0.88: scalp 0.84 would be rejected downstream by the
        # orchestrator, so the qualifying 0.90 alternative must win the slot.
        signals, hit_counts, errors = self._run([
            _sig("TEST", "MomentumScalp", 0.84),
            _sig("TEST", "MomentumContinuation", 0.90),
        ], "12:00")

        self.assertEqual(errors, 0)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].strategy, "MomentumContinuation")
        self.assertEqual(signals[0].confidence, 0.90)
        self.assertEqual(hit_counts, {"MomentumContinuation": 1})

    def test_midday_qualified_scalp_keeps_priority(self):
        # Midday floor 0.88: scalp 0.90 clears every gate it will face, so
        # scalp priority is preserved over the 0.84 alternative.
        signals, hit_counts, errors = self._run([
            _sig("TEST", "MomentumContinuation", 0.84),
            _sig("TEST", "MomentumScalp", 0.90),
        ], "12:00")

        self.assertEqual(errors, 0)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].strategy, "MomentumScalp")
        self.assertEqual(signals[0].confidence, 0.90)
        self.assertEqual(hit_counts, {"MomentumScalp": 1})

    def test_outside_midday_scalp_priority_unchanged(self):
        # Outside the midday window the active floor is MIN_SIGNAL_CONFIDENCE
        # (0.80): scalp 0.84 is qualified and still beats the 0.90 alternative.
        signals, hit_counts, errors = self._run([
            _sig("TEST", "MomentumScalp", 0.84),
            _sig("TEST", "MomentumContinuation", 0.90),
        ], "10:00")

        self.assertEqual(errors, 0)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].strategy, "MomentumScalp")
        self.assertEqual(signals[0].confidence, 0.84)
        self.assertEqual(hit_counts, {"MomentumScalp": 1})


def _completed_daily_dates(n):
    """The n most recent completed ET session dates (strictly before today)."""
    dates = []
    d = datetime.datetime.now(ET).date()
    while len(dates) < n:
        d -= datetime.timedelta(days=1)
        if d.weekday() < 5:
            dates.append(d)
    return list(reversed(dates))


class TestTIGapGuardrailSkip(unittest.TestCase):
    """_passes_guardrails(skip_gap_checks=True) skips ONLY the TI overnight-gap
    and gap-chase checks; every other guardrail stays fully active."""

    SYMBOL = "GAPY"

    def setUp(self):
        scan._guardrail_cache.clear()

    def tearDown(self):
        scan._guardrail_cache.clear()

    def _fake_bars(self, open_px, price, bar_vol, yesterday_close, include_today, wide_range=False):
        def fake(symbol, period, interval, *a, **k):
            if interval == "1m":
                hi = price * 1.02 if wide_range else price + 0.1
                lo = price * 0.985 if wide_range else price - 0.1
                return pd.DataFrame({
                    "open":   [open_px] + [price] * 19,
                    "high":   [hi] * 20,
                    "low":    [lo] * 20,
                    "close":  [price] * 20,
                    "volume": [bar_vol] * 20,
                })
            if period == "2d" and interval == "1d":
                dates  = _completed_daily_dates(1)
                closes = [yesterday_close]
                if include_today:
                    dates  = dates + [datetime.datetime.now(ET).date()]
                    closes = closes + [999.0]  # garbage partial today candle
                return pd.DataFrame({"close": closes}, index=pd.DatetimeIndex(dates))
            return pd.DataFrame()
        return fake

    def _run(self, *, open_px=15.0, price=15.5, bar_vol=200_000,
             yesterday_close=10.0, include_today=False, wide_range=False,
             skip_gap_checks=False):
        market_state = SimpleNamespace(
            resolve_regime=lambda: True,
            is_regular_hours=False,
            is_market_open=False,
            vix=15.0,
        )
        with patch.object(
            scan, "get_bars",
            side_effect=self._fake_bars(open_px, price, bar_vol, yesterday_close, include_today, wide_range),
        ), patch.object(scan, "_ti_stocks", {self.SYMBOL}), patch.object(
            scan, "_mda_snapshot_cache", {}
        ), patch.object(scan, "_snapshot_cache", {}):
            return scan._passes_guardrails(
                self.SYMBOL, bull_regime=True, market_state=market_state,
                return_reason=True, is_ti_stock=True, skip_gap_checks=skip_gap_checks,
            )

    def test_default_large_ti_overnight_gap_rejects(self):
        # Open $15 vs yesterday's completed close $10 -> +50% > 12% TI cap
        passed, reason = self._run()
        self.assertFalse(passed)
        self.assertEqual(reason, "overnight_gap")

    def test_overnight_gap_uses_completed_prior_close_when_today_candle_present(self):
        # 2d frame includes today's partial candle (close $999) — prior close
        # must still be yesterday's $10, so the +50% gap still rejects.
        passed, reason = self._run(include_today=True)
        self.assertFalse(passed)
        self.assertEqual(reason, "overnight_gap")

    def test_skip_gap_checks_waives_overnight_gap_when_other_guards_pass(self):
        passed, reason = self._run(skip_gap_checks=True)
        self.assertTrue(passed)
        self.assertIsNone(reason)

    def test_skip_gap_checks_still_rejects_low_liquidity(self):
        # price*day_vol = 15.5 * 20k = $310k < TI dollar-vol floor
        passed, reason = self._run(skip_gap_checks=True, bar_vol=1_000)
        self.assertFalse(passed)
        self.assertEqual(reason, "dollar_vol")

    def test_default_gap_chase_rejects_but_skip_waives_it(self):
        # Flat overnight gap (prior close == open) so the overnight-gap check
        # passes; day gain (15->16.5) = +10% > 7% TI gap-chase cap with a wide
        # bar range (no consolidation) -> gap_chase reject by default, waived
        # with the flag.
        passed, reason = self._run(price=16.5, yesterday_close=15.0, wide_range=True)
        self.assertFalse(passed)
        self.assertEqual(reason, "gap_chase")
        passed, reason = self._run(price=16.5, yesterday_close=15.0, wide_range=True, skip_gap_checks=True)
        self.assertTrue(passed)
        self.assertIsNone(reason)


class TestTIGapExceptionRouting(unittest.TestCase):
    """_scan_one TI gap exception: a regular-session TI symbol rejected ONLY for
    overnight_gap/gap_chase is re-checked with gap checks skipped and routed to
    MomentumScalp alone — never to other strategies, never outside the rules."""

    def _run(self, guardrail_results, strats, *, ti=True, is_regular=True, scalp_cfg=None):
        calls = []

        def fake_guardrails(symbol, **kwargs):
            calls.append(kwargs)
            return guardrail_results[min(len(calls), len(guardrail_results)) - 1]

        market_state = SimpleNamespace(
            resolve_regime=lambda: True, vix=None, is_regular_hours=is_regular,
        )
        with patch.object(scan, "clear_bar_cache"), \
             patch.object(scan, "_prefetch_snapshots"), \
             patch.object(scan, "_ti_stocks", {"TEST"} if ti else set()), \
             patch.object(scan, "_passes_guardrails", side_effect=fake_guardrails), \
             patch.object(scan, "get_strategy_instances", return_value=strats), \
             patch.object(scan, "annotate_signal_with_news", side_effect=lambda s: s), \
             patch.object(scan, "MOMENTUM_SCALP", scalp_cfg or {"enabled": True, "ti_gap_exempt": True}), \
             patch.object(scan, "MIN_SIGNAL_CONFIDENCE", 0.50):
            signals, hit_counts, errors = scan.scan_universe(["TEST"], "neutral", market_state)
        return signals, hit_counts, errors, calls

    def test_overnight_gap_reject_reroutes_to_scalp_only(self):
        scalp = _FakeScalpStrategy(_sig("TEST", "MomentumScalp", 0.80))
        other = _FakeStrategy(_sig("TEST", "MomentumContinuation", 0.95))
        signals, hit_counts, errors, calls = self._run(
            [(False, "overnight_gap"), (True, None)], [other, scalp],
        )
        self.assertEqual(errors, 0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].get("skip_gap_checks"))
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].strategy, "MomentumScalp")
        self.assertEqual(scalp.calls, 1)
        self.assertEqual(other.calls, 0)  # never exposed to other strategies
        self.assertEqual(hit_counts, {"MomentumScalp": 1})

    def test_gap_chase_reject_also_reroutes(self):
        scalp = _FakeScalpStrategy(_sig("TEST", "MomentumScalp", 0.80))
        other = _FakeStrategy(None)
        signals, _, _, calls = self._run(
            [(False, "gap_chase"), (True, None)], [other, scalp],
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].strategy, "MomentumScalp")
        self.assertEqual(other.calls, 0)

    def test_retry_failure_keeps_rejection(self):
        scalp = _FakeScalpStrategy(_sig("TEST", "MomentumScalp", 0.80))
        signals, _, _, calls = self._run(
            [(False, "overnight_gap"), (False, "dollar_vol")], [scalp],
        )
        self.assertEqual(signals, [])
        self.assertEqual(len(calls), 2)
        self.assertEqual(scalp.calls, 0)

    def test_non_ti_symbol_does_not_retry(self):
        scalp = _FakeScalpStrategy(_sig("TEST", "MomentumScalp", 0.80))
        signals, _, _, calls = self._run(
            [(False, "overnight_gap")], [scalp], ti=False,
        )
        self.assertEqual(signals, [])
        self.assertEqual(len(calls), 1)

    def test_non_gap_reject_does_not_retry(self):
        scalp = _FakeScalpStrategy(_sig("TEST", "MomentumScalp", 0.80))
        signals, _, _, calls = self._run(
            [(False, "rvol")], [scalp],
        )
        self.assertEqual(signals, [])
        self.assertEqual(len(calls), 1)

    def test_premarket_does_not_retry(self):
        scalp = _FakeScalpStrategy(_sig("TEST", "MomentumScalp", 0.80))
        signals, _, _, calls = self._run(
            [(False, "overnight_gap")], [scalp], is_regular=False,
        )
        self.assertEqual(signals, [])
        self.assertEqual(len(calls), 1)

    def test_config_opt_out_does_not_retry(self):
        scalp = _FakeScalpStrategy(_sig("TEST", "MomentumScalp", 0.80))
        signals, _, _, calls = self._run(
            [(False, "overnight_gap")], [scalp],
            scalp_cfg={"enabled": True, "ti_gap_exempt": False},
        )
        self.assertEqual(signals, [])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
