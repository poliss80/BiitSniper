"""Focused tests for the options flattening controls:

- Normal mark-driven exits require OPTIONS_EXIT_CONFIRM_CYCLES consecutive
  monitor cycles and reset when the condition stops.
- Wide quote spreads defer price-driven exits (liquidity warning logged).
- Hard risk exits (theta guard / butterfly DTE<=3 emergency) fire immediately,
  ignoring confirmation and the wide-spread deferral.
- Single-leg _close_option prices off the fresh Alpaca quote side (bid for
  sell-to-close, ask for buy-to-close), never the broker current_price unless
  no fresh quote exists (transparent fallback).

All broker/data access is mocked; no external calls.
"""

import datetime
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from alpaca.trading.enums import OrderSide

from engine.options.executor import OptionsExecutor, OptionsPosition

EXECUTOR_MOD = "engine.options.executor"

OCC = "XYZ261030C00050000"


def make_snapshot(bid, ask):
    return SimpleNamespace(latest_quote=SimpleNamespace(bid_price=bid, ask_price=ask))


def make_position(occ=OCC, entry_price=2.0, contracts=2, dte=30, entered_days_ago=5,
                  action="buy_to_open", strategy="MomentumCall", option_type="call",
                  legs=None, **kwargs):
    if legs is None:
        side = "buy" if action == "buy_to_open" else "sell"
        legs = [{"occ_symbol": occ, "side": side, "ratio_qty": 1}]
    return OptionsPosition(
        occ_symbol=occ,
        symbol="XYZ",
        option_type=option_type,
        action=action,
        strike=50.0,
        expiry=datetime.date.today() + datetime.timedelta(days=dte),
        contracts=contracts,
        entry_price=entry_price,
        strategy=strategy,
        entered_at=datetime.date.today() - datetime.timedelta(days=entered_days_ago),
        legs=legs,
        **kwargs,
    )


def build_executor(position, snapshot=None, mock_close=True):
    """OptionsExecutor without __init__ side effects; all I/O mocked."""
    ex = object.__new__(OptionsExecutor)
    ex.client = Mock()
    ex.client.get_all_positions.return_value = [
        SimpleNamespace(symbol=leg["occ_symbol"]) for leg in position.legs
    ]
    ex.data_client = Mock()
    if snapshot is not None:
        ex.data_client.get_option_snapshot.return_value = {position.occ_symbol: snapshot}
    ex._positions = {position.occ_symbol: position}
    ex._exit_confirm = {}
    ex._last_monitor_ts = 0.0
    ex._MONITOR_INTERVAL = 0.0          # never rate-limit between test cycles
    ex._last_iv_convert_ts = time.monotonic()  # skip the IV-convert path
    ex._IV_CONVERT_INTERVAL = 600.0
    if mock_close:
        ex._close_option = Mock()
    return ex


class NormalExitConfirmationTests(unittest.TestCase):
    """Requirement: normal exits need OPTIONS_EXIT_CONFIRM_CYCLES cycles and
    pending state resets when the condition stops."""

    def test_stop_requires_confirmation_and_resets_on_recovery(self):
        pos = make_position()
        # mark 1.40 vs entry 2.00 -> -30% (below -25% stop); spread ~7% (not wide)
        ex = build_executor(pos, make_snapshot(1.35, 1.45))

        with patch(f"{EXECUTOR_MOD}.OPTIONS_EXIT_CONFIRM_CYCLES", 2), \
             patch(f"{EXECUTOR_MOD}.OPTIONS_STOP_LOSS_PCT", 25.0), \
             patch(f"{EXECUTOR_MOD}.OPTIONS_ENTRY_GRACE_DAYS", 3):
            # Cycle 1: condition seen, not yet confirmed
            ex.monitor_positions()
            ex._close_option.assert_not_called()
            self.assertEqual(ex._exit_confirm.get((pos.occ_symbol, "stop")), 1)

            # Cycle 2: confirmed -> close
            ex.monitor_positions()
            self.assertEqual(ex._close_option.call_count, 1)
            self.assertEqual(ex._close_option.call_args[0][0], pos.occ_symbol)
            self.assertNotIn((pos.occ_symbol, "stop"), ex._exit_confirm)

            # Price recovers (mark 1.90 -> -5%): pending state must reset
            ex.data_client.get_option_snapshot.return_value = {
                pos.occ_symbol: make_snapshot(1.85, 1.95)
            }
            ex.monitor_positions()
            self.assertEqual(ex._close_option.call_count, 1)
            self.assertEqual(ex._exit_confirm, {})

            # Condition returns: confirmation restarts from cycle 1
            ex.data_client.get_option_snapshot.return_value = {
                pos.occ_symbol: make_snapshot(1.35, 1.45)
            }
            ex.monitor_positions()
            self.assertEqual(ex._close_option.call_count, 1)
            self.assertEqual(ex._exit_confirm.get((pos.occ_symbol, "stop")), 1)
            ex.monitor_positions()
            self.assertEqual(ex._close_option.call_count, 2)


class WideSpreadDeferralTests(unittest.TestCase):
    """Requirement: spread wider than OPTIONS_MAX_EXIT_SPREAD_PCT defers normal
    price-driven exits and logs a liquidity warning with bid/mark/ask."""

    def test_wide_spread_defers_price_driven_exit(self):
        pos = make_position()
        # mark 1.40 (-30%) but spread 0.40/1.40 = 28.6% > 15%
        ex = build_executor(pos, make_snapshot(1.20, 1.60))

        with patch(f"{EXECUTOR_MOD}.OPTIONS_EXIT_CONFIRM_CYCLES", 2), \
             patch(f"{EXECUTOR_MOD}.OPTIONS_MAX_EXIT_SPREAD_PCT", 15.0), \
             patch(f"{EXECUTOR_MOD}.OPTIONS_STOP_LOSS_PCT", 25.0), \
             patch(f"{EXECUTOR_MOD}.OPTIONS_ENTRY_GRACE_DAYS", 3):
            for _ in range(3):
                with self.assertLogs("ApexTrader.Options", level="WARNING") as cm:
                    ex.monitor_positions()
                ex._close_option.assert_not_called()
            self.assertTrue(
                any("wide quote spread" in m and "bid=$1.20" in m
                    and "mark=$1.40" in m and "ask=$1.60" in m for m in cm.output),
                f"liquidity warning with bid/mark/ask not found: {cm.output}",
            )
            # No pending confirmation accumulates while the spread is wide
            self.assertEqual(ex._exit_confirm, {})

            # Spread narrows -> normal confirmation cadence resumes
            ex.data_client.get_option_snapshot.return_value = {
                pos.occ_symbol: make_snapshot(1.35, 1.45)
            }
            ex.monitor_positions()
            ex._close_option.assert_not_called()
            ex.monitor_positions()
            ex._close_option.assert_called_once()


class HardExitTests(unittest.TestCase):
    """Requirement: theta/DTE and butterfly/condor DTE<=3 emergency exits are
    immediate — no confirmation, no wide-spread deferral."""

    def test_theta_exit_ignores_confirmation_and_wide_spread(self):
        pos = make_position(dte=2)  # <= OPTIONS_THETA_EXIT_DTE (4)
        # Wide-spread quote that would otherwise defer a price-driven exit
        ex = build_executor(pos, make_snapshot(1.20, 1.60))

        with patch(f"{EXECUTOR_MOD}.OPTIONS_EXIT_CONFIRM_CYCLES", 5), \
             patch(f"{EXECUTOR_MOD}.OPTIONS_THETA_EXIT_DTE", 4):
            ex.monitor_positions()
            ex._close_option.assert_called_once()
            self.assertEqual(ex._exit_confirm, {})

    def test_butterfly_dte_emergency_ignores_confirmation_and_wide_spread(self):
        legs = [
            {"occ_symbol": "XYZ261003C00045000", "side": "buy", "ratio_qty": 1},
            {"occ_symbol": OCC, "side": "sell", "ratio_qty": 2},
            {"occ_symbol": "XYZ261003C00055000", "side": "buy", "ratio_qty": 1},
        ]
        pos = make_position(dte=3, strategy="Butterfly", option_type="butterfly",
                            legs=legs, entry_price=2.0)
        ex = build_executor(pos)
        # Consolidated Schwab pricing: mark $1.00 < 55% of entry, 80% spread
        pricing = {
            "spread_mark": 1.00,
            "spread_bid": 0.60,
            "spread_ask": 1.40,
            "pnl_mark_pct": -50.0,
            "dte": None,
        }

        with patch(f"{EXECUTOR_MOD}.OPTIONS_THETA_EXIT_DTE", 2), \
             patch(f"{EXECUTOR_MOD}.OPTIONS_EXIT_CONFIRM_CYCLES", 5), \
             patch("engine.utils.schwab_pricing.get_spread_complete_pricing",
                   return_value=pricing):
            ex.monitor_positions()
            ex._close_option.assert_called_once()
            self.assertEqual(ex._exit_confirm, {})


class CloseOptionPricingTests(unittest.TestCase):
    """Requirement: single-leg close limits come from the fresh Alpaca quote
    side, not the broker Position.current_price."""

    def test_long_close_uses_fresh_bid_over_stale_current_price(self):
        pos = make_position()
        ex = build_executor(pos, make_snapshot(1.90, 2.10), mock_close=False)
        stale = SimpleNamespace(symbol=pos.occ_symbol, current_price="5.00")

        ex._close_option(pos.occ_symbol, all_positions={pos.occ_symbol: stale})

        ex.client.submit_order.assert_called_once()
        order = ex.client.submit_order.call_args[0][0]
        self.assertEqual(order.side, OrderSide.SELL)
        self.assertEqual(float(order.limit_price), round(1.90 * 0.97, 2))  # 1.84, not 4.85
        self.assertNotIn(pos.occ_symbol, ex._positions)
        self.assertEqual(ex._exit_confirm, {})

    def test_short_close_uses_fresh_ask_over_stale_current_price(self):
        pos = make_position(occ="XYZ261030P00050000", action="sell_to_open",
                            option_type="put", strategy="CoveredPut")
        ex = build_executor(pos, make_snapshot(1.90, 2.10), mock_close=False)
        stale = SimpleNamespace(symbol=pos.occ_symbol, current_price="0.50")

        ex._close_option(pos.occ_symbol, all_positions={pos.occ_symbol: stale})

        ex.client.submit_order.assert_called_once()
        order = ex.client.submit_order.call_args[0][0]
        self.assertEqual(order.side, OrderSide.BUY)
        self.assertEqual(float(order.limit_price), round(2.10 * 1.03, 2))  # 2.16, not 0.52
        self.assertNotIn(pos.occ_symbol, ex._positions)

    def test_close_falls_back_to_current_price_only_without_fresh_quote(self):
        pos = make_position()
        ex = build_executor(pos, mock_close=False)
        ex.data_client.get_option_snapshot.side_effect = RuntimeError("no quote")
        stale = SimpleNamespace(symbol=pos.occ_symbol, current_price="5.00")

        with self.assertLogs("ApexTrader.Options", level="WARNING") as cm:
            ex._close_option(pos.occ_symbol, all_positions={pos.occ_symbol: stale})

        order = ex.client.submit_order.call_args[0][0]
        self.assertEqual(float(order.limit_price), round(5.00 * 0.97, 2))  # 4.85 fallback
        self.assertTrue(
            any("no fresh option quote" in m for m in cm.output),
            f"transparent fallback warning not found: {cm.output}",
        )


if __name__ == "__main__":
    unittest.main()
