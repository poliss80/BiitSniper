import datetime
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
import pytz

from engine.equity import strategies as equity_strategies
from engine.equity.strategies import MomentumScalpStrategy
from engine.execution import enhanced
from engine.execution.enhanced import EnhancedExecutor
from engine.options.executor import OptionsExecutor
from engine.options.strategies import OptionSignal


class MockPosition:
    def __init__(self, symbol, qty, current_price, market_value=None, avg_entry_price=None, asset_class="us_equity"):
        self.symbol = symbol
        self.qty = qty
        self.current_price = current_price
        self.avg_entry_price = current_price if avg_entry_price is None else avg_entry_price
        self.unrealized_pl = 0.0
        self.market_value = market_value if market_value is not None else float(qty) * current_price
        self.asset_class = asset_class


class MockClient:
    def __init__(self, positions):
        self.positions = positions
        self.orders = []

    def get_all_positions(self):
        return self.positions

    def get_account(self):
        return SimpleNamespace(cash=1_000_000.0)

    def submit_order(self, order):
        self.orders.append(order)

    def get_orders(self):
        return []

    def cancel_order_by_id(self, order_id):
        pass


class FlattenClient(MockClient):
    def __init__(self, positions, close_errors=None):
        super().__init__(positions)
        self.close_errors = close_errors or {}
        self.close_attempts = []

    def close_position(self, symbol):
        self.close_attempts.append(symbol)
        error = self.close_errors.get(symbol)
        if error:
            raise error


def build_executor(client, state_path):
    executor = object.__new__(EnhancedExecutor)
    executor.client = client
    executor._entry_log = {}
    executor._tp_targets = {}
    executor._intermediate_targets = {}
    executor._tightened = set()
    executor._peak_price = {}
    executor._ratchet_armed = set()
    executor._pending_exits = {}
    executor._tp_check_lock = threading.Lock()
    executor._swap_cycle_closed = set()
    executor.order_cache = {}
    executor._live_probe_scaled_in = set()
    executor._live_probe_scale_in_pending = {}
    executor._exit_state_path = state_path
    executor._exit_state_lock = threading.Lock()
    executor._probe_journal_path = state_path.with_name("probe_journal.jsonl")
    executor._probe_journal_lock = threading.Lock()
    return executor


def make_probe_confirmation_bars(entry_time):
    closes = [100.2, 100.4, 100.7, 100.9, 101.0, 101.3]
    return pd.DataFrame({
        "time": [entry_time + datetime.timedelta(minutes=i) for i in range(len(closes))],
        "open": [close - 0.05 for close in closes],
        "high": [100.4, 100.6, 100.9, 101.1, 101.2, 101.5],
        "low": [close - 0.20 for close in closes],
        "close": closes,
        "volume": [10_000] * len(closes),
    })


class EquityExitLifecycleTests(unittest.TestCase):
    def test_scan_and_trade_keeps_live_probe_scale_checks_during_cutoff(self):
        executor = object.__new__(EnhancedExecutor)
        executor.check_live_probe_scale_ins = Mock()
        ctx = SimpleNamespace(client=None, executor=executor, options_executor=None, crypto_trader=None)

        with patch("engine.orchestrator._manage_intraday_window", return_value=False), patch(
            "engine.orchestrator.log"
        ) as mock_log:
            from engine.orchestrator import scan_and_trade

            scan_and_trade(ctx)

        executor.check_live_probe_scale_ins.assert_called_once()
        mock_log.info.assert_any_call("[SYSTEM] Outside an active intraday window or waiting for portfolio flatten")

    def test_flatten_ignores_inactive_assets(self):
        client = FlattenClient(
            [MockPosition("AVNS", "10", 5.0)],
            {"AVNS": RuntimeError('{"code":40010001,"message":"asset AVNS is not active"}')},
        )
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_flatten_state.json")

        self.assertTrue(executor.flatten_portfolio("INTRADAY FINAL RESET"))
        self.assertEqual(client.close_attempts, ["AVNS"])
        self.assertEqual(executor._flatten_in_progress, set())
        self.assertEqual(executor._flatten_failed, set())
        self.assertTrue(executor.flatten_portfolio("INTRADAY FINAL RESET"))
        self.assertEqual(client.close_attempts, ["AVNS"])

    def test_flatten_proceeds_after_close_requests_are_submitted(self):
        client = FlattenClient([MockPosition("AAPL", "10", 100.0)])
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_flatten_state.json")

        self.assertTrue(executor.flatten_portfolio("INTRADAY FINAL RESET"))
        self.assertEqual(client.close_attempts, ["AAPL"])
        self.assertEqual(executor._flatten_in_progress, {"AAPL"})
        self.assertEqual(executor._flatten_failed, set())

    def test_flatten_retains_options_positions(self):
        client = FlattenClient([
            MockPosition("AAPL", "10", 100.0),
            MockPosition("AAPL260918C00200000", "1", 5.0, asset_class="us_option"),
        ])
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_flatten_state.json")

        self.assertTrue(executor.flatten_portfolio("INTRADAY FINAL RESET"))
        self.assertEqual(client.close_attempts, ["AAPL"])

    def test_flatten_retains_crypto_positions(self):
        client = FlattenClient([
            MockPosition("AAPL", "10", 100.0),
            MockPosition("BTC/USD", "1", 60000.0, asset_class="crypto"),
        ])
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_flatten_state.json")

        self.assertTrue(executor.flatten_portfolio("INTRADAY FINAL RESET"))
        self.assertEqual(client.close_attempts, ["AAPL"])

    def test_intraday_window_reset_retains_options_tracking(self):
        """Regression: _manage_intraday_window must NOT clear
        ctx.options_executor._positions — flatten_portfolio intentionally
        excludes us_option positions, so tracked options are still open and
        must remain monitored across equity session resets."""
        from engine import orchestrator

        fake_now = SimpleNamespace(
            weekday=lambda: 0,  # Monday
            hour=12,
            minute=0,
            date=lambda: datetime.date(2026, 9, 21),
        )
        fake_datetime = SimpleNamespace(datetime=SimpleNamespace(now=lambda tz=None: fake_now))
        fake_cfg = SimpleNamespace(
            INTRADAY_WINDOW_START="09:30",
            INTRADAY_MORNING_CUTOFF="10:55",
            INTRADAY_MORNING_RESET="11:00",
            INTRADAY_RESET_TIME="11:00",
            INTRADAY_FINAL_CUTOFF="14:58",
            INTRADAY_FINAL_RESET="15:03",
            AFTERHOURS_END="20:00",
        )
        options_executor = SimpleNamespace(_positions={"XYZ261030C00050000": object()})
        ctx = SimpleNamespace(
            executor=Mock(),
            options_executor=options_executor,
            crypto_trader=None,
        )
        ctx.executor.flatten_portfolio.return_value = True

        # 12:00 ET, previously in session_1 -> session-2 boundary flatten fires
        with patch.object(orchestrator, "datetime", fake_datetime), \
             patch.object(orchestrator, "cfg", fake_cfg), \
             patch.object(orchestrator, "_load_intraday_state", return_value="session_1"), \
             patch.object(orchestrator, "_save_intraday_state") as save_state:
            self.assertTrue(orchestrator._manage_intraday_window(ctx))

        ctx.executor.flatten_portfolio.assert_called_once()
        save_state.assert_called_once_with(datetime.date(2026, 9, 21), "session_2")
        # The still-open option must remain tracked after the equity flatten
        self.assertIn("XYZ261030C00050000", options_executor._positions)

    def test_flatten_skips_scaled_in_live_probe_positions_by_default(self):
        client = FlattenClient([MockPosition("AAPL", "10", 100.0)])
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_flatten_state.json")
        executor._live_probe_scaled_in = {"AAPL"}

        self.assertTrue(executor.flatten_portfolio("INTRADAY FINAL RESET"))
        self.assertEqual(client.close_attempts, [])
        self.assertEqual(executor._flatten_in_progress, set())
        self.assertEqual(executor._flatten_failed, set())

    def test_guardrail_cache_reuses_symbol_bar_data(self):
        from engine.equity import scan as equity_scan

        equity_scan._guardrail_cache.clear()
        call_count = {"n": 0}
        now = datetime.datetime(2026, 9, 15, 10, 30, tzinfo=datetime.timezone.utc)

        def fake_get_bars(symbol, period, interval):
            call_count["n"] += 1
            if period == "1d" and interval == "1m":
                times = [now - datetime.timedelta(minutes=i) for i in range(20)]
                return pd.DataFrame({
                    "time": times,
                    "open": [100.0] * 20,
                    "high": [101.0] * 20,
                    "low": [99.5] * 20,
                    "close": [100.5] * 20,
                    "volume": [2000] * 20,
                })
            if period == "20d" and interval == "1d":
                return pd.DataFrame({
                    "close": [100.0] * 20,
                    "volume": [100_000] * 20,
                })
            return pd.DataFrame()

        market_state = SimpleNamespace(
            resolve_regime=lambda: True,
            is_regular_hours=False,
            is_market_open=False,
            vix=15.0,
        )

        with patch.object(equity_scan, "get_bars", side_effect=fake_get_bars), patch.object(
            equity_scan, "_mda_snapshot_cache", {}
        ), patch.object(equity_scan, "_snapshot_cache", {}), patch.object(
            equity_scan, "_is_iex_feed", return_value=False
        ):
            passed, reason = equity_scan._passes_guardrails(
                "AAA",
                bull_regime=True,
                market_state=market_state,
                return_reason=True,
                is_ti_stock=False,
            )

        self.assertTrue(passed)
        self.assertIsNone(reason)
        self.assertLess(call_count["n"], 5)

    def test_live_probe_scale_in_submits_one_atm_call_when_available(self):
        executor = object.__new__(EnhancedExecutor)
        options_executor = Mock()
        options_executor.place_option_order.return_value = True
        chain = SimpleNamespace(
            calls=Mock(), expiry=datetime.date(2026, 9, 18), hv_30=25.0, iv_rank=20.0,
        )
        strike_row = {"strike": 100.0, "mid": 2.5, "iv_pct": 25.0, "delta": 0.5, "openinterest": 1_000}

        with patch("engine.execution.enhanced.LIVE_PROBE_SCALE_IN_ATM_OPTION_ENABLED", True), patch(
            "engine.options.strategies._get_options_chain", return_value=chain
        ), patch("engine.options.strategies._pick_strike", return_value=strike_row), patch(
            "engine.options.strategies.get_dynamic_option_filters", return_value={}
        ):
            executor._place_live_probe_atm_option(
                "AAPL", 100.0, SimpleNamespace(is_regular_hours=True), options_executor
            )

        signal = options_executor.place_option_order.call_args.args[0]
        self.assertEqual(signal.symbol, "AAPL")
        self.assertEqual(signal.strike, 100.0)
        self.assertEqual(signal.contract_cap, 1)
        self.assertTrue(signal.force_single_leg)
        self.assertTrue(signal.bypass_portfolio_cap)

    def test_atm_option_is_not_submitted_outside_regular_hours(self):
        executor = object.__new__(EnhancedExecutor)
        options_executor = Mock()

        with patch("engine.execution.enhanced.LIVE_PROBE_SCALE_IN_ATM_OPTION_ENABLED", True):
            executor._place_live_probe_atm_option(
                "AAPL", 100.0, SimpleNamespace(is_regular_hours=False), options_executor
            )

        options_executor.place_option_order.assert_not_called()

    def test_atm_option_waits_for_confirmed_scale_in_fill(self):
        client = MockClient([MockPosition("AAPL", "1", 101.0, avg_entry_price=100.0)])
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_probe_state.json")
        executor._options_cost_reserve = 0.0
        executor._get_account = lambda **_kwargs: SimpleNamespace(equity=10_000.0, buying_power=10_000.0)
        now = datetime.datetime(2026, 8, 20, 10, 30)
        entry_time = now - datetime.timedelta(minutes=10)
        executor._current_market_state = lambda: SimpleNamespace(
            is_regular_hours=True, now=now, resolve_regime=lambda: True,
        )
        executor._entry_log["AAPL"] = {"entry_price": 100.0, "entry_time": entry_time}
        executor._place_live_probe_atm_option = Mock()
        options_executor = Mock()

        with patch.object(enhanced, "LIVE_PROBE_MODE", True), patch.object(
            enhanced, "LIVE_PROBE_SCALE_IN_ENABLED", True
        ), patch.object(enhanced, "LIVE_PROBE_SCALE_IN_MIN_GAIN_PCT", 0.5), patch.object(
            enhanced, "get_dynamic_tier", return_value={"ts": 6.0}
        ), patch.object(enhanced, "get_bars", return_value=make_probe_confirmation_bars(entry_time)
        ):
            executor.check_live_probe_scale_ins(options_executor)
            executor._place_live_probe_atm_option.assert_not_called()
            client.positions[0].qty = "2"
            executor.check_live_probe_scale_ins(options_executor)

        executor._place_live_probe_atm_option.assert_called_once()

    def test_failed_atm_option_attempt_remains_retryable(self):
        client = MockClient([MockPosition("AAPL", "2", 101.0, avg_entry_price=100.0)])
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_probe_state.json")
        executor._get_account = lambda **_kwargs: SimpleNamespace(equity=10_000.0, buying_power=10_000.0)
        now = datetime.datetime(2026, 8, 20, 10, 30)
        executor._current_market_state = lambda: SimpleNamespace(
            is_regular_hours=True, now=now, resolve_regime=lambda: True,
        )
        executor._entry_log["AAPL"] = {"entry_price": 100.0, "entry_time": now - datetime.timedelta(minutes=10)}
        executor._live_probe_scale_in_pending["AAPL"] = {
            "prior_qty": 1,
            "order_id": "scale-in-order",
            "atm_option_pending": True,
        }
        executor._place_live_probe_atm_option = Mock(side_effect=[False, True])

        with patch.object(enhanced, "LIVE_PROBE_MODE", True), patch.object(
            enhanced, "LIVE_PROBE_SCALE_IN_ENABLED", True
        ):
            executor.check_live_probe_scale_ins(Mock())
            self.assertIn("AAPL", executor._live_probe_scale_in_pending)
            executor.check_live_probe_scale_ins(Mock())

        self.assertEqual(executor._place_live_probe_atm_option.call_count, 2)
        self.assertNotIn("AAPL", executor._live_probe_scale_in_pending)

    def test_broker_fill_quantity_protects_stale_position_snapshot(self):
        class FilledOrderClient(MockClient):
            def get_account(self):
                return SimpleNamespace(equity=10_000.0, buying_power=10_000.0)

            def get_order_by_id(self, order_id):
                return SimpleNamespace(status="filled", filled_qty="2")

        client = FilledOrderClient([MockPosition("AAPL", "1", 101.0, avg_entry_price=100.0)])
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_probe_state.json")
        executor._get_account = lambda **_kwargs: SimpleNamespace(equity=10_000.0, buying_power=10_000.0)
        executor._current_market_state = lambda: SimpleNamespace(
            is_regular_hours=True,
            now=datetime.datetime(2026, 8, 20, 10, 30),
            resolve_regime=lambda: True,
        )
        executor._entry_log["AAPL"] = {"entry_price": 100.0}
        executor._live_probe_scale_in_pending["AAPL"] = {
            "prior_qty": 1,
            "order_id": "scale-in-order",
        }
        executor._place_live_probe_atm_option = Mock(return_value=True)

        with patch.object(enhanced, "LIVE_PROBE_MODE", True), patch.object(
            enhanced, "LIVE_PROBE_SCALE_IN_ENABLED", True
        ), patch.object(enhanced, "get_dynamic_tier", return_value={"ts": 6.0}), patch.object(
            enhanced.time, "sleep"
        ):
            executor.check_live_probe_scale_ins(Mock())

        self.assertEqual(client.orders[0].qty, 2)
        self.assertIn("AAPL", executor._live_probe_scaled_in)

    def test_live_probe_confirmation_normalizes_timezone_aware_bars(self):
        executor = object.__new__(EnhancedExecutor)
        entry_time = datetime.datetime(2026, 9, 4, 7, 45)
        bars = pd.DataFrame({
            "time": pd.date_range(
                "2026-09-04 07:45", periods=6, freq="min",
                tz="America/New_York",
            ),
            "high": [48.2, 48.3, 48.4, 48.5, 48.6, 48.8],
            "low": [47.8] * 6,
            "close": [48.1, 48.2, 48.3, 48.4, 48.5, 48.7],
            "volume": [10_000] * 6,
        })

        with patch.object(enhanced, "LIVE_PROBE_SCALE_IN_REQUIRE_VWAP", True), patch.object(
            enhanced, "LIVE_PROBE_SCALE_IN_REQUIRE_NEW_HIGH", True
        ), patch.object(enhanced, "get_bars", return_value=bars):
            confirmed, reason = executor._live_probe_scale_in_confirmation_ok(
                "SOXS", entry_time, 48.7
            )

        self.assertTrue(confirmed)
        self.assertIsNone(reason)

    def test_live_probe_uses_tracked_entry_when_broker_average_is_unavailable(self):
        client = MockClient([MockPosition("SOXS", "1", 101.0, avg_entry_price=0.0)])
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_probe_state.json")
        executor._options_cost_reserve = 0.0
        executor._get_account = lambda **_kwargs: SimpleNamespace(equity=10_000.0, buying_power=10_000.0)
        now = datetime.datetime(2026, 8, 20, 10, 30)
        executor._current_market_state = lambda: SimpleNamespace(
            is_regular_hours=True, now=now, resolve_regime=lambda: True,
        )
        executor._entry_log["SOXS"] = {
            "entry_time": now - datetime.timedelta(minutes=10),
            "entry_price": 100.0,
        }
        executor._place_live_probe_atm_option = Mock()

        with patch.object(enhanced, "LIVE_PROBE_MODE", True), patch.object(
            enhanced, "LIVE_PROBE_SCALE_IN_ENABLED", True
        ), patch.object(enhanced, "LIVE_PROBE_SCALE_IN_MIN_GAIN_PCT", 0.5), patch.object(
            enhanced, "get_dynamic_tier", return_value={"ts": 6.0}
        ), patch.object(enhanced, "get_bars", return_value=make_probe_confirmation_bars(
            now - datetime.timedelta(minutes=10)
        )):
            executor.check_live_probe_scale_ins()

        self.assertEqual(len(client.orders), 1)
        self.assertEqual(client.orders[0].symbol, "SOXS")

    def test_probe_cap_bypass_requires_the_fixed_single_call_contract(self):
        valid_signal = OptionSignal(
            "AAPL", "call", "buy_to_open", 100.0, datetime.date(2026, 9, 18), 2.5,
            1.0, "test", "LiveProbeATMScaleIn", contract_cap=1, force_single_leg=True,
            bypass_portfolio_cap=True,
        )
        invalid_signal = OptionSignal(
            "AAPL", "put", "buy_to_open", 100.0, datetime.date(2026, 9, 18), 2.5,
            1.0, "test", "LiveProbeATMScaleIn", contract_cap=1, force_single_leg=True,
            bypass_portfolio_cap=True,
        )

        self.assertTrue(OptionsExecutor._is_permitted_probe_cap_bypass(valid_signal))
        self.assertFalse(OptionsExecutor._is_permitted_probe_cap_bypass(invalid_signal))

    def test_flatten_still_waits_for_active_close_failure(self):
        client = FlattenClient(
            [MockPosition("AAPL", "10", 100.0)],
            {"AAPL": RuntimeError("temporary broker failure")},
        )
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_flatten_state.json")

        self.assertFalse(executor.flatten_portfolio("INTRADAY FINAL RESET"))
        self.assertEqual(executor._flatten_failed, {"AAPL"})

    def test_live_probe_uses_one_share_after_entry_checks(self):
        executor = object.__new__(EnhancedExecutor)
        executor.use_bracket_orders = True
        submitted_shares = []
        signal = SimpleNamespace(symbol="AAPL", price=100.0, confidence=0.9, strategy="Momentum")
        account = SimpleNamespace(equity=10_000.0, buying_power=40_000.0, daytrade_count=0)
        executor.pdt = SimpleNamespace(add=lambda _date: None, remaining=lambda *_args: 999)
        executor._validate_trade = lambda *_args, **_kwargs: (True, None)
        executor._can_submit_live_probe = lambda: True
        executor._validate_market_price = lambda *_args: (True, 100.0)
        executor._size_with_buying_power = lambda *_args: (50, None)
        executor._current_market_state = lambda: SimpleNamespace(is_regular_hours=True)
        executor._create_bracket_order = lambda _signal, shares, *_args: submitted_shares.append(shares) or True
        executor._record_entry = lambda *_args: None
        executor._get_positions = lambda **_kwargs: None
        executor._get_account = lambda **_kwargs: None

        with patch.object(enhanced, "LIVE", True), patch.object(
            enhanced, "LIVE_PROBE_MODE", True
        ), patch.object(enhanced, "LIVE_PROBE_SHARES", 1), patch.object(
            enhanced, "MARGIN_LEVERAGE", 1.0
        ), patch.object(
            enhanced, "calculate_risk_adjusted_size", return_value={"dollar_amount": 5_000.0}
        ):
            self.assertTrue(executor._execute_entry(signal, account, enhanced.OrderType.LONG))

        self.assertEqual(submitted_shares, [1])

    def test_probe_uses_one_share_in_paper_mode(self):
        executor = object.__new__(EnhancedExecutor)
        executor.use_bracket_orders = True
        submitted_shares = []
        signal = SimpleNamespace(symbol="AAPL", price=100.0, confidence=0.9, strategy="Momentum")
        account = SimpleNamespace(equity=10_000.0, buying_power=40_000.0, daytrade_count=0)
        executor.pdt = SimpleNamespace(add=lambda _date: None, remaining=lambda *_args: 999)
        executor._validate_trade = lambda *_args, **_kwargs: (True, None)
        executor._can_submit_live_probe = lambda: True
        executor._validate_market_price = lambda *_args: (True, 100.0)
        executor._size_with_buying_power = lambda *_args: (50, None)
        executor._current_market_state = lambda: SimpleNamespace(is_regular_hours=True)
        executor._create_bracket_order = lambda _signal, shares, *_args: submitted_shares.append(shares) or True
        executor._record_entry = lambda *_args: None
        executor._get_positions = lambda **_kwargs: None
        executor._get_account = lambda **_kwargs: None

        with patch.object(enhanced, "LIVE", False), patch.object(
            enhanced, "LIVE_PROBE_MODE", True
        ), patch.object(enhanced, "LIVE_PROBE_SHARES", 1), patch.object(
            enhanced, "MARGIN_LEVERAGE", 1.0
        ), patch.object(
            enhanced, "calculate_risk_adjusted_size", return_value={"dollar_amount": 5_000.0}
        ):
            self.assertTrue(executor._execute_entry(signal, account, enhanced.OrderType.LONG))

        self.assertEqual(submitted_shares, [1])

    def test_live_probe_daily_cap_blocks_entry(self):
        executor = object.__new__(EnhancedExecutor)
        executor._live_probe_count_date = datetime.date.today()
        executor._live_probe_entries_today = 10

        with patch.object(enhanced, "LIVE", True), patch.object(
            enhanced, "LIVE_PROBE_MODE", True
        ), patch.object(enhanced, "LIVE_PROBE_MAX_ENTRIES_PER_DAY", 10):
            self.assertFalse(executor._can_submit_live_probe())

    def test_live_probe_initial_validation_does_not_count_as_entry(self):
        executor = object.__new__(EnhancedExecutor)
        executor._live_probe_entries_today = 0
        executor._live_probe_count_date = datetime.date.today()
        executor._save_exit_state = lambda: None
        executor._current_market_state = lambda: SimpleNamespace(resolve_regime=lambda: True)
        signal = SimpleNamespace(symbol="AAPL", strategy="Momentum", confidence=0.9, atr_stop=0.0)

        with patch.object(enhanced, "LIVE_PROBE_MODE", True), patch.object(
            enhanced, "LIVE_PROBE_SHARES", 1
        ):
            executor._record_entry(signal, 100.0)

        self.assertEqual(executor._live_probe_entries_today, 0)

    def test_profitable_live_probe_scales_in_once_to_cap(self):
        client = MockClient([MockPosition("AAPL", "1", 101.0, avg_entry_price=100.0)])
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_probe_state.json")
        executor._options_cost_reserve = 0.0
        executor._pdt_stop_blocked = {}
        executor._get_account = lambda **_kwargs: SimpleNamespace(equity=10_000.0, buying_power=10_000.0)
        now = datetime.datetime(2026, 8, 20, 10, 30)
        entry_time = now - datetime.timedelta(minutes=31)
        executor._current_market_state = lambda: SimpleNamespace(
            is_regular_hours=True,
            now=now,
            resolve_regime=lambda: True,
        )
        executor._entry_log["AAPL"] = {
            "entry_time": entry_time,
            "entry_price": 100.0,
        }

        with patch.object(enhanced, "LIVE", True), patch.object(
            enhanced, "LIVE_PROBE_MODE", True
        ), patch.object(enhanced, "LIVE_PROBE_SCALE_IN_ENABLED", True), patch.object(
            enhanced, "LIVE_PROBE_SCALE_IN_MIN_GAIN_PCT", 0.5
        ), patch.object(enhanced, "LIVE_PROBE_SCALE_IN_BUYING_POWER_PCT", 25.0):
            with patch.object(enhanced, "get_dynamic_tier", return_value={"ts": 6.0}), patch.object(
                enhanced, "get_bars", return_value=make_probe_confirmation_bars(entry_time)
            ):
                executor.check_live_probe_scale_ins()
                client.positions[0].qty = "2"
                executor.check_live_probe_scale_ins()

        self.assertEqual(len(client.orders), 2)
        self.assertEqual(client.orders[0].qty, 5)
        self.assertEqual(client.orders[1].qty, 2)

    def test_live_probe_scale_in_waits_for_minimum_hold_time(self):
        client = MockClient([MockPosition("AAPL", "1", 101.0, avg_entry_price=100.0)])
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_probe_state.json")
        executor._options_cost_reserve = 0.0
        executor._get_account = lambda **_kwargs: SimpleNamespace(equity=10_000.0, buying_power=10_000.0)
        now = datetime.datetime(2026, 8, 20, 10, 30)
        entry_time = now - datetime.timedelta(minutes=2)
        executor._current_market_state = lambda: SimpleNamespace(
            is_regular_hours=True,
            now=now,
            resolve_regime=lambda: True,
        )
        executor._entry_log["AAPL"] = {"entry_price": 100.0, "entry_time": entry_time}

        with patch.object(enhanced, "LIVE_PROBE_MODE", True), patch.object(
            enhanced, "LIVE_PROBE_SCALE_IN_ENABLED", True
        ), patch.object(enhanced, "LIVE_PROBE_SCALE_IN_MIN_GAIN_PCT", 0.5), patch.object(
            enhanced, "LIVE_PROBE_SCALE_IN_MIN_HOLD_MINUTES", 5
        ), patch.object(enhanced, "get_bars", return_value=make_probe_confirmation_bars(entry_time)):
            executor.check_live_probe_scale_ins()

        self.assertEqual(client.orders, [])

    def test_live_probe_scale_in_requires_new_post_entry_high(self):
        client = MockClient([MockPosition("AAPL", "1", 101.0, avg_entry_price=100.0)])
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_probe_state.json")
        executor._options_cost_reserve = 0.0
        executor._get_account = lambda **_kwargs: SimpleNamespace(equity=10_000.0, buying_power=10_000.0)
        now = datetime.datetime(2026, 8, 20, 10, 30)
        entry_time = now - datetime.timedelta(minutes=10)
        executor._current_market_state = lambda: SimpleNamespace(
            is_regular_hours=True,
            now=now,
            resolve_regime=lambda: True,
        )
        bars = make_probe_confirmation_bars(entry_time)
        bars.loc[bars.index[-1], "high"] = bars["high"].iloc[:-1].max()
        executor._entry_log["AAPL"] = {"entry_price": 100.0, "entry_time": entry_time}

        with patch.object(enhanced, "LIVE_PROBE_MODE", True), patch.object(
            enhanced, "LIVE_PROBE_SCALE_IN_ENABLED", True
        ), patch.object(enhanced, "LIVE_PROBE_SCALE_IN_MIN_GAIN_PCT", 0.5), patch.object(
            enhanced, "get_bars", return_value=bars
        ):
            executor.check_live_probe_scale_ins()

        self.assertEqual(client.orders, [])

    def test_live_probe_scale_in_replaces_existing_protective_order(self):
        class WashTradeClient(MockClient):
            def __init__(self):
                super().__init__([MockPosition("AAPL", "1", 101.0, avg_entry_price=100.0)])
                self.open_orders = [
                    SimpleNamespace(symbol="AAPL", id="tracked-scale-in-order", type="market"),
                    SimpleNamespace(symbol="AAPL", id="existing-trailing-stop", type=SimpleNamespace(value="trailing_stop")),
                    SimpleNamespace(symbol="AAPL", id="unrelated-order", type="limit"),
                ]
                self.cancelled_orders = []

            def get_orders(self):
                return self.open_orders

            def cancel_order_by_id(self, order_id):
                self.cancelled_orders.append(order_id)
                self.open_orders = [order for order in self.open_orders if order.id != order_id]

            def submit_order(self, order):
                protective_orders = {
                    "stop", "stop_limit", "trailing_stop",
                }
                def is_protective(open_order):
                    raw_type = getattr(open_order, "type", "")
                    order_type = str(getattr(raw_type, "value", raw_type)).lower()
                    return order_type in protective_orders

                if any(is_protective(open_order) for open_order in self.open_orders) and order.side == enhanced.OrderSide.SELL:
                    raise RuntimeError("potential wash trade detected")
                self.orders.append(order)
                return SimpleNamespace(id="tracked-scale-in-order")

        client = WashTradeClient()
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_probe_state.json")
        executor._options_cost_reserve = 0.0
        executor._get_account = lambda **_kwargs: SimpleNamespace(equity=10_000.0, buying_power=10_000.0)
        now = datetime.datetime(2026, 8, 20, 10, 30)
        entry_time = now - datetime.timedelta(minutes=10)
        executor._current_market_state = lambda: SimpleNamespace(
            is_regular_hours=True,
            now=now,
            resolve_regime=lambda: True,
        )
        executor._entry_log["AAPL"] = {"entry_price": 100.0, "entry_time": entry_time}

        with patch.object(enhanced, "LIVE_PROBE_MODE", True), patch.object(
            enhanced, "LIVE_PROBE_SCALE_IN_ENABLED", True
        ), patch.object(enhanced, "LIVE_PROBE_SCALE_IN_MIN_GAIN_PCT", 0.5), patch.object(
            enhanced, "get_dynamic_tier", return_value={"ts": 6.0}
        ), patch.object(enhanced, "get_bars", return_value=make_probe_confirmation_bars(entry_time)
        ), patch.object(enhanced.time, "sleep"):
            executor.check_live_probe_scale_ins()
            client.positions[0].qty = "2"
            executor.check_live_probe_scale_ins()

        self.assertEqual(
            client.cancelled_orders,
            ["tracked-scale-in-order", "existing-trailing-stop"],
        )
        self.assertEqual(len(client.orders), 2)
        self.assertEqual(client.orders[1].qty, 2)
        self.assertIn("AAPL", executor._live_probe_scaled_in)

    def test_live_probe_scale_in_uses_limit_order_premarket(self):
        client = MockClient([MockPosition("AAPL", "1", 101.0, avg_entry_price=100.0)])
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_probe_state.json")
        executor._options_cost_reserve = 0.0
        executor._get_account = lambda **_kwargs: SimpleNamespace(equity=10_000.0, buying_power=10_000.0)
        entry_time = datetime.datetime.now() - datetime.timedelta(minutes=10)
        executor._current_market_state = lambda: SimpleNamespace(
            is_regular_hours=False,
            now=datetime.datetime.now(),
            resolve_regime=lambda: True,
        )
        executor._after_hours_limit_price = lambda *_args: (101.1, None)
        executor._entry_log["AAPL"] = {"entry_price": 100.0, "entry_time": entry_time}

        with patch.object(enhanced, "LIVE_PROBE_MODE", True), patch.object(
            enhanced, "LIVE_PROBE_SCALE_IN_ENABLED", True
        ), patch.object(enhanced, "LIVE_PROBE_SCALE_IN_MIN_GAIN_PCT", 0.5
        ), patch.object(enhanced, "get_bars", return_value=make_probe_confirmation_bars(entry_time)
        ):
            executor.check_live_probe_scale_ins()

        self.assertEqual(len(client.orders), 1)
        self.assertIsInstance(client.orders[0], enhanced.LimitOrderRequest)
        self.assertTrue(client.orders[0].extended_hours)
        self.assertIn("AAPL", executor._live_probe_scale_in_pending)

    def test_close_long_uses_limit_order_premarket(self):
        client = MockClient([MockPosition("AAPL", "10", 100.0)])
        executor = build_executor(client, Path(tempfile.gettempdir()) / "unused_exit_state.json")
        executor._get_positions = lambda **_kwargs: SimpleNamespace(
            has_position=lambda symbol: symbol == "AAPL",
            positions_dict={"AAPL": SimpleNamespace(qty="10")},
        )
        executor._current_market_state = lambda: SimpleNamespace(is_regular_hours=False)
        executor._after_hours_limit_price = lambda *_args: (99.5, None)
        signal = SimpleNamespace(symbol="AAPL", price=100.0, strategy="Momentum")

        self.assertTrue(executor._close_long_position(signal, 10_000.0))

        self.assertEqual(len(client.orders), 1)
        self.assertIsInstance(client.orders[0], enhanced.LimitOrderRequest)
        self.assertEqual(client.orders[0].limit_price, 99.5)
        self.assertTrue(client.orders[0].extended_hours)

    def test_short_probe_does_not_scale_in(self):
        client = MockClient([MockPosition("AAPL", "-1", 99.0)])
        with tempfile.TemporaryDirectory() as directory:
            executor = build_executor(client, Path(directory) / "exit_state.json")
            executor._options_cost_reserve = 0.0
            executor._pdt_stop_blocked = {}
            executor._get_account = lambda **_kwargs: SimpleNamespace(equity=10_000.0, buying_power=10_000.0)
            executor._current_market_state = lambda: SimpleNamespace(
                is_regular_hours=True,
                now=datetime.datetime(2026, 8, 20, 10, 30),
                resolve_regime=lambda: False,
            )
            executor._entry_log["AAPL"] = {
                "entry_time": datetime.datetime.now() - datetime.timedelta(minutes=31),
                "entry_price": 100.0,
            }

            with patch.object(enhanced, "LIVE", True), patch.object(
                enhanced, "LIVE_PROBE_MODE", True
            ), patch.object(enhanced, "LIVE_PROBE_SCALE_IN_ENABLED", True):
                executor.check_live_probe_scale_ins()

            self.assertEqual(client.orders, [])

    def test_probe_outcome_journal_records_managed_exit(self):
        client = MockClient([])
        with tempfile.TemporaryDirectory() as directory:
            executor = build_executor(client, Path(directory) / "exit_state.json")
            executor._entry_log["AAPL"] = {
                "strategy": "Momentum",
                "regime_at_entry": "bull",
                "entry_time": datetime.datetime(2026, 8, 20, 10, 0),
                "entry_price": 100.0,
            }
            position = MockPosition("AAPL", "2", 105.0)
            with patch.object(enhanced, "LIVE", True), patch.object(enhanced, "LIVE_PROBE_MODE", True):
                executor._record_probe_outcome("AAPL", position, "TP_CLOSE")

            record = json.loads(executor._probe_journal_path.read_text(encoding="utf-8"))
            self.assertEqual(record["strategy"], "Momentum")
            self.assertEqual(record["regime_at_entry"], "bull")
            self.assertEqual(record["estimated_pnl_pct"], 5.0)
            self.assertEqual(record["exit_reason"], "TP_CLOSE")

    def test_exit_state_round_trip_restores_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "position_exit_state.json"
            client = MockClient([MockPosition("AAPL", "10", 106.0)])
            source = build_executor(client, state_path)
            source._entry_log["AAPL"] = {
                "strategy": "Momentum",
                "date": datetime.date.today(),
                "confidence": 0.9,
                "entry_time": datetime.datetime.now() - datetime.timedelta(minutes=10),
                "entry_price": 100.0,
            }
            source._intermediate_targets["AAPL"] = 106.0
            source._tp_targets["AAPL"] = 120.0
            source._tightened.add("AAPL")
            source._save_exit_state()

            restored = build_executor(client, state_path)
            restored._restore_exit_state()

            self.assertEqual(restored._entry_log["AAPL"]["entry_price"], 100.0)
            self.assertIn("AAPL", restored._tightened)
            self.assertEqual(restored._intermediate_targets["AAPL"], 106.0)
            self.assertEqual(restored._tp_targets["AAPL"], 120.0)

    def test_restored_losing_position_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "position_exit_state.json"
            client = MockClient([MockPosition("AAPL", "10", 98.5)])
            source = build_executor(client, state_path)
            source._entry_log["AAPL"] = {
                "strategy": "Momentum",
                "date": datetime.date.today(),
                "confidence": 0.9,
                "entry_time": datetime.datetime.now() - datetime.timedelta(minutes=46),
                "entry_price": 100.0,
                "atr_stop": 0.0,
            }
            source._tp_targets["AAPL"] = 120.0
            source._save_exit_state()

            restored = build_executor(client, state_path)
            restored._restore_exit_state()
            with patch.object(enhanced, "DEAD_MONEY_MINUTES", 45), patch.object(
                enhanced, "DEAD_MONEY_MAX_ADVERSE_DRIFT_PCT", 1.5
            ):
                restored.check_dead_money()

                self.assertEqual(len(client.orders), 1)
                # Accepted ≠ closed: exit state is retained until the broker confirms flat
                self.assertTrue(state_path.exists())
                self.assertIn("AAPL", restored._pending_exits)
                self.assertIn("AAPL", restored._entry_log)

                # While the close is pending, no duplicate close is submitted
                restored.check_dead_money()
                self.assertEqual(len(client.orders), 1, "pending close must not be resubmitted")

                # Broker confirms flat → all exit state is released
                client.positions.clear()
                restored.check_dead_money()
            self.assertFalse(state_path.exists())

    def test_time_loss_retries_terminal_partial_close_for_remainder(self):
        with tempfile.TemporaryDirectory() as directory:
            position = MockPosition("AAPL", "7", 98.5, avg_entry_price=100.0)
            client = MockClient([position])
            executor = build_executor(client, Path(directory) / "exit_state.json")
            executor._entry_log["AAPL"] = {
                "strategy": "Momentum", "date": datetime.date.today(),
                "entry_time": datetime.datetime.now() - datetime.timedelta(minutes=46),
                "entry_price": 100.0, "atr_stop": 0.0,
            }
            executor._pending_exits["AAPL"] = {
                "kind": "close", "qty": 10, "orig_qty": 10,
                "coid": "apex-tm-close-AAPL-old", "order_id": "old-order",
                "submitted_at": 0,
            }

            with patch.object(enhanced, "DEAD_MONEY_MINUTES", 45), patch.object(
                enhanced, "DEAD_MONEY_MAX_ADVERSE_DRIFT_PCT", 1.0
            ):
                executor.check_dead_money()

            closes = [o for o in client.orders if isinstance(o, enhanced.MarketOrderRequest)]
            self.assertEqual(len(closes), 1)
            self.assertEqual(closes[0].qty, 7)
            self.assertEqual(executor._pending_exits["AAPL"]["orig_qty"], 7)

    def test_time_loss_waits_when_pending_order_lookup_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            position = MockPosition("AAPL", "10", 98.5, avg_entry_price=100.0)
            client = MockClient([position])
            client.get_orders = Mock(side_effect=RuntimeError("temporary broker failure"))
            executor = build_executor(client, Path(directory) / "exit_state.json")
            executor._entry_log["AAPL"] = {
                "strategy": "Momentum", "date": datetime.date.today(),
                "entry_time": datetime.datetime.now() - datetime.timedelta(minutes=46),
                "entry_price": 100.0, "atr_stop": 0.0,
            }
            executor._pending_exits["AAPL"] = {
                "kind": "close", "qty": 10, "orig_qty": 10,
                "coid": "apex-tm-close-AAPL-old", "order_id": "old-order",
                "submitted_at": 0,
            }

            with patch.object(enhanced, "DEAD_MONEY_MINUTES", 45), patch.object(
                enhanced, "DEAD_MONEY_MAX_ADVERSE_DRIFT_PCT", 1.0
            ):
                executor.check_dead_money()

            self.assertEqual(client.orders, [], "unknown order state must not trigger a duplicate close")
            self.assertIn("AAPL", executor._pending_exits)

    def test_restored_profitable_position_is_not_time_loss_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "position_exit_state.json"
            client = MockClient([MockPosition("AAPL", "10", 101.5)])
            executor = build_executor(client, state_path)
            executor._entry_log["AAPL"] = {
                "strategy": "Momentum",
                "date": datetime.date.today(),
                "confidence": 0.9,
                "entry_time": datetime.datetime.now() - datetime.timedelta(minutes=46),
                "entry_price": 100.0,
                "atr_stop": 0.0,
            }

            with patch.object(enhanced, "DEAD_MONEY_MINUTES", 45), patch.object(
                enhanced, "DEAD_MONEY_MAX_ADVERSE_DRIFT_PCT", 1.5
            ):
                executor.check_dead_money()

            self.assertEqual(client.orders, [])

    def test_atr_time_loss_allows_normal_high_volatility_pullback(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MockClient([MockPosition("AAPL", "10", 98.5)])
            executor = build_executor(client, Path(directory) / "position_exit_state.json")
            executor._entry_log["AAPL"] = {
                "strategy": "Momentum",
                "date": datetime.date.today(),
                "confidence": 0.9,
                "entry_time": datetime.datetime.now() - datetime.timedelta(minutes=46),
                "entry_price": 100.0,
                "atr_stop": 9.0,
            }

            with patch.object(enhanced, "DEAD_MONEY_MINUTES", 45), patch.object(
                enhanced, "ATR_STOP_MULTIPLIER", 1.5
            ), patch.object(enhanced, "TIME_LOSS_ATR_MULTIPLIER", 0.35), patch.object(
                enhanced, "TIME_LOSS_ATR_MIN_PCT", 1.0
            ), patch.object(enhanced, "TIME_LOSS_ATR_MAX_PCT", 2.5):
                executor.check_dead_money()

            self.assertEqual(client.orders, [])

    def test_eod_close_releases_trailing_order_for_untracked_position(self):
        class EodClient(MockClient):
            def __init__(self):
                super().__init__([MockPosition("AAPL", "10", 100.0)])
                self.cancelled_orders = []

            def get_orders(self):
                return [SimpleNamespace(symbol="AAPL", id="trail-1")]

            def cancel_order_by_id(self, order_id):
                self.cancelled_orders.append(order_id)

        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 19, 15, 56, tzinfo=tz)

        with tempfile.TemporaryDirectory() as directory:
            client = EodClient()
            executor = build_executor(client, Path(directory) / "position_exit_state.json")
            executor._eod_close_done = None

            with patch.object(enhanced, "EOD_CLOSE_ENABLED", True), patch.object(
                enhanced, "EOD_CLOSE_ALL", True
            ), patch.object(enhanced, "EOD_CLOSE_TIME", "15:55"), patch.object(
                enhanced.datetime, "datetime", FixedDateTime
            ):
                summary = executor.close_eod_positions()

            self.assertEqual(client.cancelled_orders, ["trail-1"])
            self.assertEqual(len(client.orders), 1)
            self.assertEqual(summary["closed_count"], 1)

    def test_margin_eod_force_closes_position_ignoring_strategy_filter(self):
        class MarginClient(MockClient):
            def get_account(self):
                return SimpleNamespace(cash=500.0)

        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 19, 15, 56, tzinfo=tz)

        with tempfile.TemporaryDirectory() as directory:
            client = MarginClient([MockPosition("AAPL", "10", 100.0)])  # $1,000 market value > $500 cash
            executor = build_executor(client, Path(directory) / "position_exit_state.json")
            executor._eod_close_done = None
            executor._entry_log["AAPL"] = {"strategy": "Momentum", "date": datetime.date(2026, 8, 19)}

            with patch.object(enhanced, "EOD_CLOSE_ENABLED", True), patch.object(
                enhanced, "EOD_CLOSE_ALL", False
            ), patch.object(enhanced, "EOD_CLOSE_TIME", "15:55"), patch.object(
                enhanced, "MARGIN_EOD_FORCE_CLOSE", True
            ), patch.object(enhanced.datetime, "datetime", FixedDateTime):
                summary = executor.close_eod_positions()

            self.assertEqual(len(client.orders), 1)
            self.assertEqual(summary["closed_count"], 1)

    def test_no_margin_no_force_close_when_strategy_not_eligible(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 19, 15, 56, tzinfo=tz)

        with tempfile.TemporaryDirectory() as directory:
            client = MockClient([MockPosition("AAPL", "10", 100.0)])  # cash covers exposure — no margin
            executor = build_executor(client, Path(directory) / "position_exit_state.json")
            executor._eod_close_done = None
            executor._entry_log["AAPL"] = {"strategy": "Momentum", "date": datetime.date(2026, 8, 19)}

            with patch.object(enhanced, "EOD_CLOSE_ENABLED", True), patch.object(
                enhanced, "EOD_CLOSE_ALL", False
            ), patch.object(enhanced, "EOD_CLOSE_TIME", "15:55"), patch.object(
                enhanced, "MARGIN_EOD_FORCE_CLOSE", True
            ), patch.object(enhanced.datetime, "datetime", FixedDateTime):
                summary = executor.close_eod_positions()

            self.assertEqual(client.orders, [])
            self.assertEqual(summary["closed_count"], 0)

    def test_after_hours_entry_uses_live_quote_for_limit(self):
        class RecordingClient:
            def __init__(self):
                self.last_order = None
            def get_latest_quote(self, symbol):
                return SimpleNamespace(bid_price=98.8, ask_price=99.2)
            def submit_order(self, order):
                self.last_order = order
                return SimpleNamespace(id="order-1")

        client = RecordingClient()
        executor = object.__new__(EnhancedExecutor)
        executor.client = client
        executor.order_cache = {}
        executor._submitted_entry_orders = {}
        executor.market_state = None
        executor._current_market_state = lambda: SimpleNamespace(is_regular_hours=False)

        signal = SimpleNamespace(symbol="AAPL", price=100.0, strategy="Momentum")
        self.assertTrue(executor._create_simple_order(signal, 10, enhanced.OrderType.LONG))
        self.assertLessEqual(client.last_order.limit_price, 99.2)
        self.assertLess(client.last_order.limit_price, 100.0)

    def test_halted_bracket_entry_is_cached_without_market_order_fallback(self):
        class HaltedClient:
            def __init__(self):
                self.submit_calls = 0

            def submit_order(self, order):
                self.submit_calls += 1
                raise RuntimeError('market order rejected due to trading halt on symbol: "APGE"')

        client = HaltedClient()
        executor = object.__new__(EnhancedExecutor)
        executor.client = client
        executor.order_cache = {}
        executor._submitted_entry_orders = {}
        executor._halted_symbols = set()
        executor._intermediate_targets = {}
        executor._tp_targets = {}
        executor.use_bracket_orders = True
        executor._current_market_state = lambda: SimpleNamespace(is_regular_hours=True)
        signal = SimpleNamespace(symbol="APGE", price=135.07, strategy="Sweepea")

        self.assertFalse(executor._create_bracket_order(
            signal, 1, {"stop_loss_pct": 5.0}, enhanced.OrderType.LONG
        ))
        self.assertEqual(client.submit_calls, 1)
        self.assertEqual(executor._halted_symbols, {"APGE"})

    def test_option_retry_accepts_string_filled_quantity(self):
        class RetryClient:
            def __init__(self):
                self.cancelled = []

            def get_orders(self):
                return [SimpleNamespace(id="option-order", filled_qty="1")]

            def cancel_order_by_id(self, order_id):
                self.cancelled.append(order_id)

        executor = object.__new__(OptionsExecutor)
        executor.client = RetryClient()

        with patch.object(OptionsExecutor, "_ORDER_RETRY_TIMEOUT", 0):
            executor._adaptive_limit_retry(
                "option-order", "buy", 1.25, "SOFI", 1, "SOFI260918C00015000", False, None
            )

        self.assertEqual(executor.client.cancelled, [])

    def test_option_retry_cancels_final_retry_order_without_resubmitting(self):
        class FinalRetryClient:
            def __init__(self):
                self.cancelled = []
                self.submitted = []

            def get_orders(self):
                return [SimpleNamespace(id="option-order", filled_qty="0")]

            def cancel_order_by_id(self, order_id):
                self.cancelled.append(order_id)

            def submit_order(self, order):
                self.submitted.append(order)

        executor = object.__new__(OptionsExecutor)
        executor.client = FinalRetryClient()

        with patch.object(OptionsExecutor, "_ORDER_RETRY_TIMEOUT", 0):
            executor._adaptive_limit_retry(
                "option-order", "buy", 1.25, "SOFI", 2, "SOFI260918C00015000",
                False, None, retry_count=OptionsExecutor._ORDER_MAX_RETRIES,
            )

        # Final retry order must still be monitored and canceled, never left live
        self.assertEqual(executor.client.cancelled, ["option-order"])
        self.assertEqual(executor.client.submitted, [])

    def test_option_retry_looks_up_status_when_order_not_open(self):
        class LookupClient:
            def __init__(self):
                self.cancelled = []
                self.looked_up = []

            def get_orders(self):
                return []

            def get_order_by_id(self, order_id):
                self.looked_up.append(order_id)
                return SimpleNamespace(id=order_id, status="filled", filled_qty="1")

            def cancel_order_by_id(self, order_id):
                self.cancelled.append(order_id)

            def get_all_positions(self):
                return []

        executor = object.__new__(OptionsExecutor)
        executor.client = LookupClient()
        executor._positions = {}

        with patch.object(OptionsExecutor, "_ORDER_RETRY_TIMEOUT", 0):
            executor._adaptive_limit_retry(
                "option-order", "buy", 1.25, "SOFI", 1, "SOFI260918C00015000", False, None
            )

        self.assertEqual(executor.client.looked_up, ["option-order"])
        self.assertEqual(executor.client.cancelled, [])

    def test_option_retry_resubmits_only_unfilled_quantity(self):
        class PartialFillClient:
            def __init__(self):
                self.cancelled = []
                self.submitted = []

            def get_orders(self):
                return [SimpleNamespace(id="option-order", filled_qty="1")]

            def cancel_order_by_id(self, order_id):
                self.cancelled.append(order_id)

            def submit_order(self, order):
                self.submitted.append(order)
                return SimpleNamespace(id="retry-order", status="new")

            def get_all_positions(self):
                return []

        executor = object.__new__(OptionsExecutor)
        executor.client = PartialFillClient()
        executor._positions = {}

        with patch.object(OptionsExecutor, "_ORDER_RETRY_TIMEOUT", 0), patch(
            "engine.options.executor.threading"
        ) as mock_threading:
            executor._adaptive_limit_retry(
                "option-order", "buy", 1.25, "SOFI", 3, "SOFI260918C00015000", False, None
            )

        self.assertEqual(executor.client.cancelled, ["option-order"])
        self.assertEqual(len(executor.client.submitted), 1)
        # Partially filled 1 of 3 — resubmit must target only the 2 unfilled contracts
        self.assertEqual(float(executor.client.submitted[0].qty), 2.0)
        mock_threading.Thread.assert_called_once()
        self.assertEqual(mock_threading.Thread.call_args.kwargs["args"][4], 2)

    def test_single_option_limit_uses_current_quote_when_valid(self):
        executor = object.__new__(OptionsExecutor)
        executor.data_client = SimpleNamespace(
            get_option_snapshot=lambda _request: {
                "SOFI260918C00019500": SimpleNamespace(
                    latest_trade=SimpleNamespace(price="0.43"),
                    latest_quote=SimpleNamespace(bid_price="0.39", ask_price="0.44"),
                )
            }
        )

        # Executable quote-derived limits win over the stale last trade
        self.assertEqual(
            executor._get_alpaca_option_limit("SOFI260918C00019500", is_buy=True),
            0.44,
        )
        self.assertEqual(
            executor._get_alpaca_option_limit("SOFI260918C00019500", is_buy=False),
            0.39,
        )

    def test_single_option_limit_falls_back_to_last_trade_without_valid_quote(self):
        executor = object.__new__(OptionsExecutor)
        executor.data_client = SimpleNamespace(
            get_option_snapshot=lambda _request: {
                "SOFI260918C00019500": SimpleNamespace(
                    latest_trade=SimpleNamespace(price="0.43"),
                    latest_quote=SimpleNamespace(bid_price="0.00", ask_price="0.00"),
                )
            }
        )

        self.assertEqual(
            executor._get_alpaca_option_limit("SOFI260918C00019500", is_buy=True),
            0.43,
        )

    def test_single_option_limit_returns_none_without_quote_or_trade(self):
        executor = object.__new__(OptionsExecutor)
        executor.data_client = SimpleNamespace(
            get_option_snapshot=lambda _request: {
                "SOFI260918C00019500": SimpleNamespace(
                    latest_trade=None,
                    latest_quote=SimpleNamespace(bid_price="0.00", ask_price="0.00"),
                )
            }
        )

        self.assertIsNone(
            executor._get_alpaca_option_limit("SOFI260918C00019500", is_buy=True)
        )

    @staticmethod
    def _make_option_order_setup(submit_response, open_orders=None):
        class OrderClient:
            def __init__(self):
                self.submitted = []
                self.open_orders = list(open_orders or [])

            def get_all_positions(self):
                return []

            def get_account(self):
                return SimpleNamespace(
                    equity=100_000.0,
                    buying_power=100_000.0,
                    options_buying_power=None,
                    trading_blocked=False,
                    account_blocked=False,
                )

            def submit_order(self, order):
                self.submitted.append(order)
                return submit_response

            def get_orders(self):
                return list(self.open_orders)

        executor = object.__new__(OptionsExecutor)
        executor.client = OrderClient()
        executor._positions = {}
        executor._get_alpaca_option_limit = Mock(return_value=1.30)
        signal = OptionSignal(
            "SOFI", "call", "buy_to_open", 15.0, datetime.date(2026, 10, 16), 1.25,
            1.0, "test", "TestMomentum", iv_rank=10.0, contract_cap=1,
            force_single_leg=True,
        )
        market_state = SimpleNamespace(is_open_window=False)
        return executor, signal, market_state

    def test_place_option_order_without_order_id_returns_false_and_does_not_track(self):
        executor, signal, market_state = self._make_option_order_setup(
            SimpleNamespace(id=None, status="new", filled_qty="0", qty="1")
        )

        with patch("engine.options.executor.PAPER", True), patch(
            "engine.options.executor.OPTIONS_LIVE_PROBE_MODE", False
        ), patch("engine.options.executor.threading") as mock_threading, patch(
            "requests.get", side_effect=RuntimeError("offline")
        ):
            result = executor.place_option_order(signal, market_state)

        self.assertFalse(result)
        self.assertEqual(len(executor.client.submitted), 1)
        self.assertEqual(executor._positions, {})
        mock_threading.Thread.assert_not_called()
        self.assertIn("order id", executor._last_rejection_reason)

    def test_place_option_order_pending_acceptance_is_not_tracked_as_executed(self):
        executor, signal, market_state = self._make_option_order_setup(
            SimpleNamespace(id="order-1", status="accepted", filled_qty="0", qty="1")
        )

        with patch("engine.options.executor.PAPER", True), patch(
            "engine.options.executor.OPTIONS_LIVE_PROBE_MODE", False
        ), patch("engine.options.executor.threading") as mock_threading, patch(
            "engine.options.executor.log"
        ) as mock_log, patch("requests.get", side_effect=RuntimeError("offline")):
            result = executor.place_option_order(signal, market_state)

        # Submission succeeded, but no fill => no phantom position, no EXECUTED log
        self.assertTrue(result)
        self.assertEqual(executor._positions, {})
        mock_threading.Thread.assert_called_once()  # retry monitor watches the open order
        logged = " ".join(str(c) for c in mock_log.info.call_args_list)
        self.assertIn("SUBMITTED", logged)
        self.assertNotIn("EXECUTED", logged)

    def test_place_option_order_tracks_position_only_on_confirmed_fill(self):
        executor, signal, market_state = self._make_option_order_setup(
            SimpleNamespace(
                id="order-1", status="filled", filled_qty="1", qty="1",
                filled_avg_price="1.30",
            )
        )

        with patch("engine.options.executor.PAPER", True), patch(
            "engine.options.executor.OPTIONS_LIVE_PROBE_MODE", False
        ), patch("engine.options.executor.threading") as mock_threading, patch(
            "engine.options.executor.log"
        ) as mock_log, patch("requests.get", side_effect=RuntimeError("offline")):
            result = executor.place_option_order(signal, market_state)

        self.assertTrue(result)
        primary_occ = "SOFI261016C00015000"
        self.assertIn(primary_occ, executor._positions)
        self.assertAlmostEqual(executor._positions[primary_occ].entry_price, 1.30)
        mock_threading.Thread.assert_not_called()  # no retry monitor for a filled order
        logged = " ".join(str(c) for c in mock_log.info.call_args_list)
        self.assertIn("EXECUTED", logged)

    def test_place_option_order_blocked_by_pending_open_entry_order(self):
        executor, signal, market_state = self._make_option_order_setup(
            SimpleNamespace(id="order-2", status="filled", filled_qty="1", qty="1",
                            filled_avg_price="1.30"),
            open_orders=[
                SimpleNamespace(
                    id="order-1", status="accepted", side="buy", symbol="SOFI261016C00015000",
                    order_class="", legs=None,
                )
            ],
        )

        with patch("engine.options.executor.PAPER", True), patch(
            "engine.options.executor.OPTIONS_LIVE_PROBE_MODE", False
        ), patch("requests.get", side_effect=RuntimeError("offline")):
            result = executor.place_option_order(signal, market_state)

        self.assertFalse(result)
        self.assertEqual(executor.client.submitted, [])  # no duplicate submission
        self.assertEqual(executor._positions, {})
        self.assertIn("pending", executor._last_rejection_reason)

    def test_place_option_order_blocked_by_pending_mleg_entry_for_same_underlying(self):
        executor, signal, market_state = self._make_option_order_setup(
            SimpleNamespace(id="order-2", status="filled", filled_qty="1", qty="1",
                            filled_avg_price="1.30"),
            open_orders=[
                SimpleNamespace(
                    id="order-1", status="new", side=None, symbol="",
                    order_class="mleg",
                    legs=[
                        SimpleNamespace(symbol="SOFI261016C00015000", position_intent="buy_to_open"),
                        SimpleNamespace(symbol="SOFI261016C00017500", position_intent="sell_to_open"),
                    ],
                )
            ],
        )

        with patch("engine.options.executor.PAPER", True), patch(
            "engine.options.executor.OPTIONS_LIVE_PROBE_MODE", False
        ), patch("requests.get", side_effect=RuntimeError("offline")):
            result = executor.place_option_order(signal, market_state)

        self.assertFalse(result)
        self.assertEqual(executor.client.submitted, [])
        self.assertIn("pending", executor._last_rejection_reason)

    def test_place_option_order_ignores_canceled_and_other_underlying_orders(self):
        executor, signal, market_state = self._make_option_order_setup(
            SimpleNamespace(id="order-2", status="filled", filled_qty="1", qty="1",
                            filled_avg_price="1.30"),
            open_orders=[
                # Canceled order for the same underlying — must not block
                SimpleNamespace(
                    id="order-0", status="canceled", side="buy", symbol="SOFI261016C00015000",
                    order_class="", legs=None,
                ),
                # Open entry for a different underlying — must not block
                SimpleNamespace(
                    id="order-1", status="new", side="buy", symbol="AAPL261016C00200000",
                    order_class="", legs=None,
                ),
            ],
        )

        with patch("engine.options.executor.PAPER", True), patch(
            "engine.options.executor.OPTIONS_LIVE_PROBE_MODE", False
        ), patch("engine.options.executor.threading"), patch(
            "requests.get", side_effect=RuntimeError("offline")
        ):
            result = executor.place_option_order(signal, market_state)

        self.assertTrue(result)
        self.assertEqual(len(executor.client.submitted), 1)
        self.assertIn("SOFI261016C00015000", executor._positions)

    def test_place_option_order_open_order_lookup_failure_does_not_block(self):
        executor, signal, market_state = self._make_option_order_setup(
            SimpleNamespace(id="order-1", status="filled", filled_qty="1", qty="1",
                            filled_avg_price="1.30")
        )
        executor.client.get_orders = Mock(side_effect=RuntimeError("api down"))

        with patch("engine.options.executor.PAPER", True), patch(
            "engine.options.executor.OPTIONS_LIVE_PROBE_MODE", False
        ), patch("engine.options.executor.threading"), patch(
            "requests.get", side_effect=RuntimeError("offline")
        ):
            result = executor.place_option_order(signal, market_state)

        self.assertTrue(result)
        self.assertEqual(len(executor.client.submitted), 1)
        self.assertIn("SOFI261016C00015000", executor._positions)

    def test_after_hours_eod_close_uses_executable_limit_order(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 20, 16, 30, tzinfo=tz)

        with tempfile.TemporaryDirectory() as directory:
            client = MockClient([MockPosition("AAPL", "10", 100.0)])
            executor = build_executor(client, Path(directory) / "exit_state.json")
            executor._eod_close_done = None
            executor._record_probe_outcome = lambda *_args: None

            with patch.object(enhanced, "EOD_CLOSE_ENABLED", True), patch.object(
                enhanced, "EOD_CLOSE_ALL", True
            ), patch.object(enhanced, "EOD_CLOSE_TIME", "15:55"), patch.object(
                enhanced.datetime, "datetime", FixedDateTime
            ):
                summary = executor.close_eod_positions()

            request = client.orders[0]
            self.assertEqual(request.limit_price, 99.0)
            self.assertTrue(request.extended_hours)
            self.assertEqual(summary["failed_count"], 0)

    def test_after_hours_eod_close_retries_when_quote_is_unavailable(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 20, 16, 30, tzinfo=tz)

        with tempfile.TemporaryDirectory() as directory:
            client = MockClient([MockPosition("AAPL", "10", 0.0)])
            executor = build_executor(client, Path(directory) / "exit_state.json")
            executor._eod_close_done = None
            executor._record_probe_outcome = lambda *_args: None

            with patch.object(enhanced, "EOD_CLOSE_ENABLED", True), patch.object(
                enhanced, "EOD_CLOSE_ALL", True
            ), patch.object(enhanced, "EOD_CLOSE_TIME", "15:55"), patch.object(
                enhanced.datetime, "datetime", FixedDateTime
            ):
                summary = executor.close_eod_positions()

            self.assertEqual(client.orders, [])
            self.assertEqual(summary["failed_count"], 1)
            self.assertIsNone(executor._eod_close_done)


class RatchetExitTests(unittest.TestCase):
    def _executor(self, directory, pos):
        client = MockClient([pos])
        executor = build_executor(client, Path(directory) / "exit_state.json")
        executor._record_probe_outcome = lambda *a, **k: None
        executor._save_exit_state = lambda *a, **k: None
        return client, executor

    def test_ratchet_lets_winner_run_then_exits_on_giveback(self):
        with tempfile.TemporaryDirectory() as directory:
            pos = MockPosition("AAPL", "10", 110.0, avg_entry_price=100.0)
            client, executor = self._executor(directory, pos)
            executor._tp_targets["AAPL"] = 110.0
            with patch.object(enhanced, "TP_RATCHET_ENABLED", True), patch.object(
                enhanced, "TP_RATCHET_ARM_PCT", 8.0
            ), patch.object(enhanced, "TP_RATCHET_GIVEBACK_PCT", 8.0):
                # +10% and armed — peak 110, no giveback yet → keep running
                executor.check_tp_targets()
                self.assertEqual(client.orders, [], "winner should keep running past +10%")
                self.assertEqual(executor._peak_price["AAPL"], 110.0)
                # runs to +30% — peak ratchets up, still no exit
                pos.current_price = 130.0
                executor.check_tp_targets()
                self.assertEqual(client.orders, [])
                self.assertEqual(executor._peak_price["AAPL"], 130.0)
                # gives back 8% from peak 130 (trigger 119.6) → exit
                pos.current_price = 119.0
                executor.check_tp_targets()
                self.assertEqual(len(client.orders), 1, "ratchet should exit on giveback from peak")

    def test_ratchet_does_not_exit_below_arm(self):
        with tempfile.TemporaryDirectory() as directory:
            pos = MockPosition("AAPL", "10", 104.0, avg_entry_price=100.0)  # +4%, below +8% arm
            client, executor = self._executor(directory, pos)
            executor._tp_targets["AAPL"] = 110.0
            with patch.object(enhanced, "TP_RATCHET_ENABLED", True), patch.object(
                enhanced, "TP_RATCHET_ARM_PCT", 8.0
            ), patch.object(enhanced, "TP_RATCHET_GIVEBACK_PCT", 8.0):
                executor.check_tp_targets()
                self.assertEqual(client.orders, [], "unarmed position must not ratchet-exit")

    def test_ratchet_peak_survives_restart(self):
        """The high-water mark must persist: a restart that re-anchored the peak to
        the current price would let a winner give back its whole run un-exited."""
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "exit_state.json"
            entry_time = datetime.datetime(2026, 9, 8, 10, 0)

            saver = build_executor(MockClient([MockPosition("AAPL", "10", 130.0, avg_entry_price=100.0)]),
                                   state_path)
            saver._entry_log["AAPL"] = {"entry_time": entry_time, "entry_price": 100.0}
            saver._tp_targets["AAPL"] = 110.0
            saver._peak_price["AAPL"] = 130.0
            saver._save_exit_state()

            # Restart: fresh executor, same state file, price now 8% off the old peak
            pos = MockPosition("AAPL", "10", 119.0, avg_entry_price=100.0)
            client = MockClient([pos])
            executor = build_executor(client, state_path)
            executor._record_probe_outcome = lambda *a, **k: None
            executor._restore_exit_state()
            self.assertEqual(executor._peak_price.get("AAPL"), 130.0)

            with patch.object(enhanced, "TP_RATCHET_ENABLED", True), patch.object(
                enhanced, "TP_RATCHET_ARM_PCT", 8.0
            ), patch.object(enhanced, "TP_RATCHET_GIVEBACK_PCT", 8.0):
                executor.check_tp_targets()
            self.assertEqual(len(client.orders), 1, "restored peak should still trigger the giveback exit")


class MomentumScalpSizingTests(unittest.TestCase):
    """Requirement 1: a MomentumScalp entry keeps its calculated, max_bp_pct-capped
    size even when LIVE_PROBE_MODE would otherwise shrink it to LIVE_PROBE_SHARES."""

    def test_scalp_size_not_overridden_by_live_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MockClient([])
            executor = build_executor(client, Path(directory) / "exit_state.json")
            executor.use_bracket_orders = False
            executor.pdt = Mock()
            executor._submitted_entry_orders = {}
            executor._halted_symbols = set()
            executor._swap_cycle_closed = set()
            executor._validate_trade = Mock(return_value=(True, None))
            executor._can_submit_live_probe = Mock(return_value=True)
            executor._validate_market_price = Mock(return_value=(True, 100.0))
            executor._size_with_buying_power = Mock(return_value=(200, None))
            executor._record_entry = Mock()
            executor._get_positions = Mock(return_value={})
            executor._get_account = Mock()
            executor._create_simple_order = Mock(return_value=True)

            signal = SimpleNamespace(
                symbol="BBNX", strategy="MomentumScalp", price=100.0,
                confidence=0.9, atr_stop=3.0,
            )
            acct = SimpleNamespace(equity=100_000.0, buying_power=100_000.0, daytrade_count=0)
            scalp_cfg = {
                "enabled": True, "min_rvol": 3.0, "min_price_up_pct": 5.0,
                "break_lookback_min": 10, "position_size_mult": 2.0,
                "max_bp_pct": 20.0, "tp_pct": 5.0, "ratchet_giveback_pct": 2.5,
            }

            with patch.object(enhanced, "LIVE_PROBE_MODE", True), patch.object(
                enhanced, "LIVE_PROBE_SHARES", 1
            ), patch.object(
                enhanced, "MOMENTUM_SCALP", scalp_cfg
            ), patch.object(
                enhanced, "calculate_risk_adjusted_size",
                return_value={"dollar_amount": 50_000.0, "stop_loss_pct": 3.0},
            ), patch.object(enhanced, "MARGIN_LEVERAGE", 1.0), patch.object(
                enhanced, "is_high_short_float", return_value=False
            ):
                result = executor._execute_entry(signal, acct, enhanced.OrderType.LONG)

            self.assertTrue(result)
            # Scalp keeps its calculated size (200) — NOT shrunk to LIVE_PROBE_SHARES (1)
            self.assertEqual(executor._create_simple_order.call_args[0][1], 200)
            # Dollar amount was capped at max_bp_pct (20%) of buying power ($100k → $20k)
            sized_risk = executor._size_with_buying_power.call_args[0][2]
            self.assertEqual(sized_risk["dollar_amount"], 20_000.0)


class MomentumScalpExitTests(unittest.TestCase):
    SCALP_CFG = {
        "enabled": True, "min_rvol": 3.0, "min_price_up_pct": 5.0,
        "break_lookback_min": 10, "position_size_mult": 2.0,
        "max_bp_pct": 20.0, "tp_pct": 5.0, "ratchet_giveback_pct": 2.5,
    }

    def _executor(self, directory, pos):
        client = MockClient([pos])
        executor = build_executor(client, Path(directory) / "exit_state.json")
        executor._record_probe_outcome = lambda *a, **k: None
        return client, executor

    def _scalp_entry(self, executor, sym, entry_price=100.0):
        executor._entry_log[sym] = {
            "strategy": "MomentumScalp", "ti_profile": "scalp",
            "entry_time": datetime.datetime(2026, 9, 25, 10, 0),
            "entry_price": entry_price,
        }
        executor._tp_targets[sym] = entry_price * 1.10
        executor._intermediate_targets[sym] = entry_price * 1.05

    def test_scalp_scale_out_then_ratchet_closes_remainder_below_arm(self):
        """2-share scalp at +5% submits a 1-share scale-out; completion and
        remainder protection wait for the broker-confirmed fill. The armed
        ratchet then closes the remainder even below the +5% arm."""
        with tempfile.TemporaryDirectory() as directory:
            pos = MockPosition("BBNX", "2", 105.0, avg_entry_price=100.0)
            client, executor = self._executor(directory, pos)
            self._scalp_entry(executor, "BBNX")

            with patch.object(enhanced, "TP_RATCHET_ENABLED", True), patch.object(
                enhanced, "MOMENTUM_SCALP", self.SCALP_CFG
            ):
                # +5% → scale-out SUBMITTED but not yet confirmed
                executor.check_tp_targets()
                market_sells = [o for o in client.orders if isinstance(o, enhanced.MarketOrderRequest)]
                self.assertEqual(len(market_sells), 1, "scalp should submit a half sell at +5%")
                self.assertEqual(market_sells[0].qty, 1)
                coid = str(market_sells[0].client_order_id)
                self.assertTrue(coid.startswith("apex-scalp-out-BBNX"))
                self.assertFalse(
                    executor._entry_log["BBNX"].get("scalp_scaled_out", False),
                    "accepted is not filled — scalp must not be marked scaled out yet",
                )
                self.assertEqual(
                    [o for o in client.orders if isinstance(o, enhanced.TrailingStopOrderRequest)], [],
                    "no protective-stop replacement before fill confirmation",
                )
                self.assertIn("BBNX", executor._pending_exits)

                # While the scale-out order is working, no duplicate partial sell
                working_order = SimpleNamespace(symbol="BBNX", id="o-1", client_order_id=coid)
                client.get_orders = lambda: [working_order]
                executor.check_tp_targets()
                market_sells = [o for o in client.orders if isinstance(o, enhanced.MarketOrderRequest)]
                self.assertEqual(len(market_sells), 1, "no duplicate scale-out while one is pending")
                self.assertFalse(executor._entry_log["BBNX"].get("scalp_scaled_out", False))

                # Broker shows the fill: position shrank 2 → 1
                client.get_orders = lambda: []
                pos.qty = "1"
                executor.check_tp_targets()
                self.assertTrue(executor._entry_log["BBNX"]["scalp_scaled_out"])
                self.assertIn("BBNX", executor._ratchet_armed)
                self.assertNotIn("BBNX", executor._pending_exits)
                trails = [o for o in client.orders if isinstance(o, enhanced.TrailingStopOrderRequest)]
                self.assertEqual(len(trails), 1, "confirmed remainder gets a fresh tight trail")
                self.assertEqual(trails[0].qty, 1)

                # Scalp state must survive a restart
                executor._save_exit_state()
                restored = build_executor(MockClient([pos]), Path(directory) / "exit_state.json")
                restored._restore_exit_state()
                self.assertTrue(restored._entry_log["BBNX"]["scalp_scaled_out"])
                self.assertIn("BBNX", restored._ratchet_armed)
                self.assertEqual(restored._peak_price["BBNX"], 105.0)

                # Run to 106 (new peak), then retrace to 103.3 — below the 2.5%
                # giveback trigger (106 * 0.975 = 103.35) with gain now only +3.3%,
                # under the +5% arm. The armed ratchet must still exit.
                client.orders.clear()
                pos.current_price = 106.0
                executor.check_tp_targets()
                self.assertEqual(client.orders, [], "no giveback yet at the new peak")
                pos.current_price = 103.3
                executor.check_tp_targets()
                closes = [o for o in client.orders if isinstance(o, enhanced.MarketOrderRequest)]
                self.assertEqual(
                    len(closes), 1,
                    "armed ratchet must close the remainder even below the +5% arm",
                )
                self.assertEqual(closes[0].qty, 1)
                self.assertIn(
                    "BBNX", executor._pending_exits,
                    "accepted close is not confirmed — exit state must be retained",
                )
                self.assertIn("BBNX", executor._tp_targets)

                # Broker confirms flat → exit state fully cleaned
                client.positions.clear()
                executor.check_tp_targets()
                self.assertNotIn("BBNX", executor._tp_targets)
                self.assertNotIn("BBNX", executor._pending_exits)

    def test_pending_scale_out_grace_window_blocks_duplicate(self):
        """If the sell is not yet visible in get_orders and the position is
        unchanged, the grace window treats it as working — no duplicate submit."""
        with tempfile.TemporaryDirectory() as directory:
            pos = MockPosition("BBNX", "2", 105.0, avg_entry_price=100.0)
            client, executor = self._executor(directory, pos)
            self._scalp_entry(executor, "BBNX")
            with patch.object(enhanced, "TP_RATCHET_ENABLED", True), patch.object(
                enhanced, "MOMENTUM_SCALP", self.SCALP_CFG
            ):
                executor.check_tp_targets()
                executor.check_tp_targets()
                sells = [o for o in client.orders if isinstance(o, enhanced.MarketOrderRequest)]
                self.assertEqual(len(sells), 1, "grace window must suppress a duplicate scale-out")
                self.assertFalse(executor._entry_log["BBNX"].get("scalp_scaled_out", False))

    def test_failed_scale_out_retries_after_grace_window(self):
        """A scale-out order that died unfilled past the grace window is released
        and re-evaluated — resubmitted while the +5% target still holds."""
        with tempfile.TemporaryDirectory() as directory:
            pos = MockPosition("BBNX", "2", 105.0, avg_entry_price=100.0)
            client, executor = self._executor(directory, pos)
            self._scalp_entry(executor, "BBNX")
            with patch.object(enhanced, "TP_RATCHET_ENABLED", True), patch.object(
                enhanced, "MOMENTUM_SCALP", self.SCALP_CFG
            ):
                executor.check_tp_targets()
                executor._pending_exits["BBNX"]["submitted_at"] = 0  # age past the grace window
                executor.check_tp_targets()
                sells = [o for o in client.orders if isinstance(o, enhanced.MarketOrderRequest)]
                self.assertEqual(len(sells), 2, "dead scale-out order should be retried")
                self.assertFalse(executor._entry_log["BBNX"].get("scalp_scaled_out", False))

    def test_terminal_partial_scale_out_protects_actual_remainder(self):
        """A terminal partial scale-out books what filled and protects the
        broker-reported remainder instead of reselling the original half."""
        with tempfile.TemporaryDirectory() as directory:
            pos = MockPosition("BBNX", "10", 105.0, avg_entry_price=100.0)
            client, executor = self._executor(directory, pos)
            self._scalp_entry(executor, "BBNX")
            with patch.object(enhanced, "TP_RATCHET_ENABLED", True), patch.object(
                enhanced, "MOMENTUM_SCALP", self.SCALP_CFG
            ):
                executor.check_tp_targets()
                self.assertEqual(client.orders[0].qty, 5)
                executor._pending_exits["BBNX"]["submitted_at"] = 0
                pos.qty = "7"  # 3 of the requested 5 shares filled before cancellation
                client.get_orders = lambda: []
                executor.check_tp_targets()

            self.assertTrue(executor._entry_log["BBNX"]["scalp_scaled_out"])
            self.assertNotIn("BBNX", executor._pending_exits)
            trails = [o for o in client.orders if isinstance(o, enhanced.TrailingStopOrderRequest)]
            self.assertEqual(len(trails), 1)
            self.assertEqual(trails[0].qty, 7)
            sells = [o for o in client.orders if isinstance(o, enhanced.MarketOrderRequest)]
            self.assertEqual(len(sells), 1, "do not resubmit the original 5-share partial sell")

    def test_terminal_partial_close_retries_only_remaining_shares(self):
        """A canceled close with some fills re-evaluates using the live remainder."""
        with tempfile.TemporaryDirectory() as directory:
            pos = MockPosition("AAPL", "10", 91.0, avg_entry_price=100.0)
            client, executor = self._executor(directory, pos)
            executor._entry_log["AAPL"] = {
                "strategy": "Momentum", "ti_profile": "ti_momentum",
                "entry_time": datetime.datetime(2026, 9, 25, 10, 0),
                "entry_price": 100.0,
            }
            executor._tp_targets["AAPL"] = 90.0
            executor._pending_exits["AAPL"] = {
                "kind": "close", "qty": 10, "orig_qty": 10,
                "coid": "apex-tp-close-AAPL-old", "order_id": "old-order",
                "submitted_at": 0,
            }
            pos.qty = "7"  # 3 shares filled before the old order became terminal
            client.get_orders = lambda: []

            with patch.object(enhanced, "TP_RATCHET_ENABLED", False):
                executor.check_tp_targets()

            closes = [o for o in client.orders if isinstance(o, enhanced.MarketOrderRequest)]
            self.assertEqual(len(closes), 1)
            self.assertEqual(closes[0].qty, 7)
            self.assertEqual(executor._pending_exits["AAPL"]["orig_qty"], 7)

    def test_pending_exit_survives_restart(self):
        """A submitted-but-unconfirmed scale-out must reload after restart so the
        resumed bot reconciles the working order instead of duplicating it."""
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "exit_state.json"
            pos = MockPosition("BBNX", "2", 105.0, avg_entry_price=100.0)
            client, executor = self._executor(directory, pos)
            self._scalp_entry(executor, "BBNX")
            with patch.object(enhanced, "TP_RATCHET_ENABLED", True), patch.object(
                enhanced, "MOMENTUM_SCALP", self.SCALP_CFG
            ):
                executor.check_tp_targets()
            self.assertIn("BBNX", executor._pending_exits)

            restored_client = MockClient([pos])
            restored = build_executor(restored_client, state_path)
            restored._record_probe_outcome = lambda *a, **k: None
            restored._restore_exit_state()
            self.assertIn("BBNX", restored._pending_exits)
            self.assertEqual(restored._pending_exits["BBNX"]["kind"], "scale_out")

            coid = restored._pending_exits["BBNX"]["coid"]
            restored_client.get_orders = lambda: [
                SimpleNamespace(symbol="BBNX", id="o-1", client_order_id=coid)
            ]
            with patch.object(enhanced, "TP_RATCHET_ENABLED", True), patch.object(
                enhanced, "MOMENTUM_SCALP", self.SCALP_CFG
            ):
                restored.check_tp_targets()
            self.assertEqual(
                restored_client.orders, [],
                "restored pending exit must block a duplicate scale-out",
            )

    def test_tp_check_skipped_while_another_exit_check_holds_lock(self):
        """The nonblocking lock makes concurrent scan/fast-monitor calls skip
        instead of racing into duplicate exits."""
        with tempfile.TemporaryDirectory() as directory:
            pos = MockPosition("BBNX", "2", 105.0, avg_entry_price=100.0)
            client, executor = self._executor(directory, pos)
            self._scalp_entry(executor, "BBNX")
            executor._tp_check_lock.acquire()
            try:
                with patch.object(enhanced, "TP_RATCHET_ENABLED", True), patch.object(
                    enhanced, "MOMENTUM_SCALP", self.SCALP_CFG
                ):
                    executor.check_tp_targets()
            finally:
                executor._tp_check_lock.release()
            self.assertEqual(client.orders, [], "contended exit check must skip, not duplicate")

    def test_only_profiles_keeps_normal_positions_on_scan_cadence(self):
        """The 10s fast monitor (only_profiles={'scalp'}) must not process
        non-scalp positions."""
        with tempfile.TemporaryDirectory() as directory:
            scalp_pos = MockPosition("BBNX", "2", 105.0, avg_entry_price=100.0)
            other_pos = MockPosition("AAPL", "10", 109.0, avg_entry_price=100.0)
            client = MockClient([scalp_pos, other_pos])
            executor = build_executor(client, Path(directory) / "exit_state.json")
            executor._record_probe_outcome = lambda *a, **k: None
            self._scalp_entry(executor, "BBNX")
            executor._entry_log["AAPL"] = {
                "strategy": "TI", "ti_profile": "ti_momentum",
                "entry_time": datetime.datetime(2026, 9, 25, 10, 0),
                "entry_price": 100.0,
            }
            executor._tp_targets["AAPL"] = 110.0

            with patch.object(enhanced, "TP_RATCHET_ENABLED", True), patch.object(
                enhanced, "MOMENTUM_SCALP", self.SCALP_CFG
            ):
                executor.check_tp_targets(only_profiles={"scalp"})
            sells = [o for o in client.orders if isinstance(o, enhanced.MarketOrderRequest)]
            self.assertEqual([s.symbol for s in sells], ["BBNX"])
            self.assertNotIn("AAPL", executor._ratchet_armed,
                             "non-scalp positions wait for the scan cadence")

    def test_scalp_one_share_closes_in_full_at_target(self):
        """A 1-share scalp cannot scale out — it closes entirely at +5%."""
        with tempfile.TemporaryDirectory() as directory:
            pos = MockPosition("BBNX", "1", 105.0, avg_entry_price=100.0)
            client, executor = self._executor(directory, pos)
            self._scalp_entry(executor, "BBNX")

            with patch.object(enhanced, "TP_RATCHET_ENABLED", True), patch.object(
                enhanced, "MOMENTUM_SCALP", self.SCALP_CFG
            ):
                executor.check_tp_targets()
            closes = [o for o in client.orders if isinstance(o, enhanced.MarketOrderRequest)]
            self.assertEqual(len(closes), 1)
            self.assertEqual(closes[0].qty, 1)
            self.assertFalse(executor._entry_log.get("BBNX", {}).get("scalp_scaled_out", False)
                             and "BBNX" in executor._tp_targets,
                             "closed scalp should be cleaned from tp targets")

    def test_generic_ratchet_stays_armed_after_retrace_below_arm(self):
        """Regression: once the peak crosses the arm threshold, the giveback exit
        must fire even if current gain has fallen back below the arm."""
        with tempfile.TemporaryDirectory() as directory:
            pos = MockPosition("AAPL", "10", 109.0, avg_entry_price=100.0)
            client, executor = self._executor(directory, pos)
            executor._entry_log["AAPL"] = {
                "strategy": "TI", "ti_profile": "ti_momentum",
                "entry_time": datetime.datetime(2026, 9, 25, 10, 0),
                "entry_price": 100.0,
            }
            executor._tp_targets["AAPL"] = 110.0

            with patch.object(enhanced, "TP_RATCHET_ENABLED", True), patch.object(
                enhanced, "TP_RATCHET_ARM_PCT", 8.0
            ), patch.object(enhanced, "TP_RATCHET_GIVEBACK_PCT", 2.0):
                # +9% peak → arms, no giveback yet (trigger 106.82)
                executor.check_tp_targets()
                self.assertIn("AAPL", executor._ratchet_armed)
                self.assertEqual(client.orders, [])
                # Retrace to +6.8% — below the +8% arm, but past the giveback trigger
                pos.current_price = 106.8
                executor.check_tp_targets()
                self.assertEqual(
                    len(client.orders), 1,
                    "armed ratchet must exit on giveback even below the arm threshold",
                )


class ScalpFastMonitorTests(unittest.TestCase):
    """The 10s daemon monitor polls MomentumScalp profit targets between scans;
    PDT software stops and non-scalp positions keep their existing cadence."""

    def test_poll_checks_scalp_profit_targets(self):
        from engine import orchestrator

        executor = Mock()
        executor._pdt_stop_blocked = {}
        executor.has_active_scalp_positions = Mock(return_value=True)
        ctx = SimpleNamespace(executor=executor)

        orchestrator._software_stop_poll_once(ctx)

        executor.check_tp_targets.assert_called_once_with(only_profiles={"scalp"})
        executor.check_software_stops.assert_not_called()

    def test_poll_runs_software_stops_and_skips_scalp_when_none_active(self):
        from engine import orchestrator

        executor = Mock()
        executor._pdt_stop_blocked = {"WFF": 1.0}
        executor.has_active_scalp_positions = Mock(return_value=False)
        ctx = SimpleNamespace(executor=executor)

        orchestrator._software_stop_poll_once(ctx)

        executor.check_software_stops.assert_called_once()
        executor.check_tp_targets.assert_not_called()

    def test_has_active_scalp_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = build_executor(MockClient([]), Path(directory) / "exit_state.json")
            self.assertFalse(executor.has_active_scalp_positions())

            executor._entry_log["AAPL"] = {"ti_profile": "ti_momentum"}
            executor._tp_targets["AAPL"] = 110.0
            self.assertFalse(executor.has_active_scalp_positions())

            executor._entry_log["BBNX"] = {"ti_profile": "scalp"}
            executor._tp_targets["BBNX"] = 110.0
            self.assertTrue(executor.has_active_scalp_positions())

            # A pending (not yet confirmed) scalp exit keeps fast monitoring on
            executor._tp_targets.clear()
            executor._pending_exits["BBNX"] = {"kind": "close"}
            self.assertTrue(executor.has_active_scalp_positions())


class _StaleOrderClient(MockClient):
    def __init__(self, order, quote=None, quote_error=None):
        super().__init__([])
        self._stale_order = order
        self._quote = quote
        self._quote_error = quote_error
        self.cancelled = []

    def get_orders(self):
        return [self._stale_order]

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(order_id)

    def get_latest_quote(self, symbol):
        if self._quote_error is not None:
            raise self._quote_error
        return self._quote

    def submit_order(self, order):
        self.orders.append(order)
        return SimpleNamespace(id="order-2")


class StaleOrderExtendedHoursTests(unittest.TestCase):
    """Extended-hours stale-order replacement must validate the replacement
    quote BEFORE cancelling the working order (WFF paper-log regression: the
    16:02 TIME LOSS SELL was cancelled, then every replacement failed with
    'unable to determine quote', leaving no working exit)."""

    def _stale_sell_order(self, age_hours=1):
        return SimpleNamespace(
            symbol="WFF",
            id="order-1",
            order_type="market",
            order_class="",
            client_order_id="",
            qty="5",
            side=enhanced.OrderSide.SELL,
            limit_price=None,
            created_at=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=age_hours),
        )

    def _executor(self, client, directory, regular=False):
        executor = build_executor(client, Path(directory) / "exit_state.json")
        executor.market_state = SimpleNamespace(is_regular_hours=regular)
        return executor

    def test_missing_quote_preserves_working_exit_order(self):
        with tempfile.TemporaryDirectory() as directory:
            client = _StaleOrderClient(
                self._stale_sell_order(), quote=SimpleNamespace(bid_price=0.0, ask_price=0.0)
            )
            executor = self._executor(client, directory)
            executor.update_stale_orders()

            self.assertEqual(client.cancelled, [],
                             "working exit order must NOT be cancelled without a validated quote")
            self.assertEqual(client.orders, [], "no replacement submitted without a quote")

    def test_quote_exception_preserves_working_exit_order(self):
        with tempfile.TemporaryDirectory() as directory:
            client = _StaleOrderClient(
                self._stale_sell_order(), quote_error=RuntimeError("quote unavailable")
            )
            executor = self._executor(client, directory)
            executor.update_stale_orders()

            self.assertEqual(client.cancelled, [],
                             "quote failure must preserve the working exit order")
            self.assertEqual(client.orders, [])

    def test_valid_quote_cancels_then_replaces_extended_hours(self):
        with tempfile.TemporaryDirectory() as directory:
            client = _StaleOrderClient(
                self._stale_sell_order(), quote=SimpleNamespace(bid_price=10.5, ask_price=10.7)
            )
            executor = self._executor(client, directory)
            executor.update_stale_orders()

            self.assertEqual(client.cancelled, ["order-1"])
            self.assertEqual(len(client.orders), 1)
            req = client.orders[0]
            self.assertIsInstance(req, enhanced.LimitOrderRequest)
            self.assertEqual(req.limit_price, 10.5)  # sell → replacement priced at the bid
            self.assertTrue(req.extended_hours)

    def test_regular_hours_still_cancels_then_resubmits_market(self):
        """Regular-hours behavior is unchanged: cancel first, then market resubmit."""
        with tempfile.TemporaryDirectory() as directory:
            # Age past the 360-minute regular-hours stale cutoff
            client = _StaleOrderClient(self._stale_sell_order(age_hours=10))
            executor = self._executor(client, directory, regular=True)
            executor.update_stale_orders()

            self.assertEqual(client.cancelled, ["order-1"])
            self.assertEqual(len(client.orders), 1)
            self.assertIsInstance(client.orders[0], enhanced.MarketOrderRequest)


class _ScalpFakeDatetime(datetime.datetime):
    """datetime.now(tz) pinned to 2026-09-25 10:30 ET (elapsed=60 min, in-window)."""
    @classmethod
    def now(cls, tz=None):
        return datetime.datetime(2026, 9, 25, 10, 30, 0, tzinfo=tz)


_FAKE_SCALP_DATETIME_MODULE = SimpleNamespace(
    datetime=_ScalpFakeDatetime,
    timedelta=datetime.timedelta,
    timezone=datetime.timezone,
)


class MomentumScalpScanTests(unittest.TestCase):
    """MomentumScalp entry guards: immediate candle-volume confirmation
    (current 1m bar >= bar_volume_mult x trailing up-to-20-bar avg) and a
    strict breakout above the preceding 5 session bars (current excluded)."""

    SCALP_CFG = {
        "enabled": True, "min_rvol": 3.0, "min_price_up_pct": 5.0,
        "break_lookback_min": 10, "bar_volume_mult": 1.5,
        "position_size_mult": 2.0, "max_bp_pct": 20.0,
        "tp_pct": 5.0, "ratchet_giveback_pct": 2.5,
    }

    @staticmethod
    def _daily(n=20, avg_vol=1_000_000.0):
        """20 daily bars, avg volume 1M (scan averages iloc[:-1] -> 1M)."""
        closes = [50.0 + 0.3 * i for i in range(n)]
        idx = pd.date_range(end="2026-09-24", periods=n, freq="B")
        return pd.DataFrame({
            "open":   closes,
            "high":   [c + 0.3 for c in closes],
            "low":    [c - 0.3 for c in closes],
            "close":  closes,
            "volume": [float(avg_vol)] * n,
        }, index=idx)

    @staticmethod
    def _intraday(cur_vol=16_000.0, base_vol=8_000.0, breakout=True, n=60):
        """60 x 1m bars 09:30-10:29 ET ramping 10.00 -> ~10.80 (+8% from open).
        Base volume 8k/min -> day_vol ~480k -> rvol ~3.1x at elapsed=60 min.
        breakout=True: last bar closes above the preceding 5-bar high;
        breakout=False: last bar closes just under it, but still within 0.5%
        of the recent high so only the breakout guard can reject."""
        opens, highs, lows, closes, vols = [], [], [], [], []
        for i in range(n):
            price = 10.0 + 0.8 * i / (n - 1)
            o, c = price, price + 0.01
            opens.append(o)
            closes.append(c)
            highs.append(c + 0.02)
            lows.append(o - 0.05)
            vols.append(float(base_vol))
        prior_5_high = max(highs[-6:-1])  # highest high of the 5 bars before the current one
        vols[-1] = float(cur_vol)
        closes[-1] = prior_5_high + 0.05 if breakout else prior_5_high - 0.01
        opens[-1] = closes[-1] - 0.01
        highs[-1] = closes[-1] + 0.02
        idx = pd.date_range(start="2026-09-25 09:30", periods=n, freq="min")
        return pd.DataFrame({
            "open": opens, "high": highs, "low": lows,
            "close": closes, "volume": vols,
        }, index=idx)

    def _scan(self, intraday, symbol="TEST"):
        def fake_get_bars(sym, period, interval, *a, **k):
            return intraday.copy() if interval == "1m" else self._daily().copy()
        with patch.object(equity_strategies, "get_bars", side_effect=fake_get_bars), \
             patch.object(equity_strategies, "_sa_metrics_boost", lambda s, c, *a, **k: c), \
             patch.object(equity_strategies, "MOMENTUM_SCALP", self.SCALP_CFG), \
             patch.object(equity_strategies, "datetime", _FAKE_SCALP_DATETIME_MODULE):
            return MomentumScalpStrategy().scan(symbol)

    def test_accepts_confirmed_breakout_bar(self):
        sig = self._scan(self._intraday(cur_vol=16_000.0, breakout=True))
        self.assertIsNotNone(sig, "extended runner with 2x bar volume and a breakout should fire")
        self.assertEqual(sig.strategy, "MomentumScalp")
        self.assertIn("barvol=2.0x", sig.reason)
        self.assertIn("break>$", sig.reason)

    def test_rejects_weak_current_bar_volume(self):
        # Current bar at base volume (1.0x trailing avg < 1.5x) — breakout shape intact
        sig = self._scan(self._intraday(cur_vol=8_000.0, breakout=True))
        self.assertIsNone(sig, "weak current-bar volume must reject")

    def test_rejects_close_below_prior_5bar_high(self):
        # Strong bar volume, but close does not clear the preceding 5-bar high
        sig = self._scan(self._intraday(cur_vol=16_000.0, breakout=False))
        self.assertIsNone(sig, "no breakout above the preceding 5-bar high must reject")


if __name__ == "__main__":
    unittest.main()