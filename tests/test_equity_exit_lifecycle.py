import datetime
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

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
            self.assertFalse(state_path.exists())

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

    def test_single_option_limit_uses_alpaca_last_executed_trade(self):
        executor = object.__new__(OptionsExecutor)
        executor.data_client = SimpleNamespace(
            get_option_snapshot=lambda _request: {
                "SOFI260918C00019500": SimpleNamespace(
                    latest_trade=SimpleNamespace(price="0.43"),
                    latest_quote=SimpleNamespace(bid_price="0.39", ask_price="0.44"),
                )
            }
        )

        self.assertEqual(
            executor._get_alpaca_option_limit("SOFI260918C00019500", is_buy=True),
            0.43,
        )

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


if __name__ == "__main__":
    unittest.main()