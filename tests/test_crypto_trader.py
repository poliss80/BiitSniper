"""Offline unit tests for engine.crypto.trader SL order placement fixes.

Covers:
  BUG 1 — SL placement is deferred while the entry BUY order is still open
          (Alpaca wash-trade rejection 40310000).
  BUG 2 — Submitted SL qty is floored (never rounded up) so it can never
          exceed the true available balance (Alpaca 40310000 "insufficient
          balance" from float round-trip inflation).

All Alpaca client interactions are mocked; no network calls are made.
"""

import math
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import engine.config as _cfg
from engine.crypto.trader import CryptoPosition, CryptoTrader


SYM = "PEPE/USD"
ALPACA_SYM = "PEPEUSD"
# Real-world style broker qty string with a long decimal tail
PEPE_QTY_STR = "398296459.709127232"
PEPE_QTY = float(PEPE_QTY_STR)
ENTRY_PRICE = 0.0000123


def _make_position(symbol=ALPACA_SYM, qty=PEPE_QTY_STR, avg_entry_price=str(ENTRY_PRICE)):
    return SimpleNamespace(symbol=symbol, qty=qty, avg_entry_price=avg_entry_price)


def _make_order(symbol=ALPACA_SYM, side="buy", order_type="limit", order_id="buy-1"):
    return SimpleNamespace(symbol=symbol, side=side, order_type=order_type, id=order_id)


class MockTradingClient:
    """Minimal Alpaca TradingClient stand-in."""

    def __init__(self, positions=None, orders=None):
        self._positions = list(positions or [])
        self._orders = list(orders or [])
        self.submitted = []

    def get_all_positions(self):
        return self._positions

    def get_orders(self, req):
        # Mimic Alpaca's side filter so _find_existing_sl_order (SELL) and
        # _has_open_buy_order (BUY) each see only matching orders.
        side = str(getattr(req, "side", "")).lower()
        want_buy = "buy" in side
        return [
            o for o in self._orders
            if ("buy" in str(getattr(o, "side", "")).lower()) == want_buy
        ]

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(id="sl-order-1")


class TestCryptoTraderSLPlacement(unittest.TestCase):
    """BUG 1: defer SL while entry buy order is still open."""

    def setUp(self):
        self._universe_patch = patch.object(_cfg, "CRYPTO_UNIVERSE", [SYM])
        self._universe_patch.start()
        self.addCleanup(self._universe_patch.stop)

    # ── not-yet-tracked branch ──────────────────────────────────────────────

    def test_sync_defers_sl_when_open_buy_order_exists(self):
        client = MockTradingClient(
            positions=[_make_position()],
            orders=[_make_order(side="buy")],
        )
        trader = CryptoTrader(client)

        trader._sync_positions()

        self.assertEqual(client.submitted, [], "SL order must not be submitted while buy is open")
        self.assertIn(SYM, trader._positions)
        self.assertIsNone(
            trader._positions[SYM].sl_order_id,
            "sl_order_id must stay None so placement retries on a later cycle",
        )

    def test_sync_places_sl_when_no_open_buy_order(self):
        client = MockTradingClient(
            positions=[_make_position()],
            orders=[],  # no open orders at all
        )
        trader = CryptoTrader(client)

        trader._sync_positions()

        self.assertEqual(len(client.submitted), 1, "SL order should be submitted")
        self.assertEqual(trader._positions[SYM].sl_order_id, "sl-order-1")

    # ── already-tracked branch ──────────────────────────────────────────────

    def test_sync_tracked_defers_sl_when_open_buy_order_exists(self):
        client = MockTradingClient(
            positions=[_make_position()],
            orders=[_make_order(side="buy")],
        )
        trader = CryptoTrader(client)
        # Pre-seed a locally tracked position without a broker SL (e.g. just
        # after execute_buy recorded the entry, buy limit still working).
        trader._positions[SYM] = CryptoPosition(
            symbol=SYM,
            entry_price=ENTRY_PRICE,
            entry_time=datetime.now(),
            qty=PEPE_QTY,
            notional=PEPE_QTY * ENTRY_PRICE,
            tp_price=round(ENTRY_PRICE * 1.04, 8),
            sl_price=round(ENTRY_PRICE * 0.975, 8),
            peak_price=ENTRY_PRICE,
            sl_order_id=None,
        )

        trader._sync_positions()

        self.assertEqual(client.submitted, [], "SL order must not be submitted while buy is open")
        self.assertIsNone(trader._positions[SYM].sl_order_id)

    def test_sync_tracked_places_sl_once_buy_order_gone(self):
        client = MockTradingClient(
            positions=[_make_position()],
            orders=[],  # buy fully filled — no longer open
        )
        trader = CryptoTrader(client)
        trader._positions[SYM] = CryptoPosition(
            symbol=SYM,
            entry_price=ENTRY_PRICE,
            entry_time=datetime.now(),
            qty=PEPE_QTY,
            notional=PEPE_QTY * ENTRY_PRICE,
            tp_price=round(ENTRY_PRICE * 1.04, 8),
            sl_price=round(ENTRY_PRICE * 0.975, 8),
            peak_price=ENTRY_PRICE,
            sl_order_id=None,
        )

        trader._sync_positions()

        self.assertEqual(len(client.submitted), 1, "SL order should be submitted")
        self.assertEqual(trader._positions[SYM].sl_order_id, "sl-order-1")


class TestCryptoTraderSLQtySafety(unittest.TestCase):
    """BUG 2: submitted SL qty is floor-only rounded and never exceeds input."""

    def test_sl_qty_never_exceeds_input(self):
        client = MockTradingClient()
        trader = CryptoTrader(client)

        order_id = trader._place_sl_order(SYM, PEPE_QTY, sl_price=0.00001)

        self.assertEqual(order_id, "sl-order-1")
        self.assertEqual(len(client.submitted), 1)
        submitted_qty = float(client.submitted[0].qty)
        self.assertLessEqual(
            submitted_qty, PEPE_QTY,
            f"submitted qty {submitted_qty!r} must not exceed broker qty {PEPE_QTY!r}",
        )
        # Floor-only at 8 decimals with a 1e-6 relative shave
        expected = math.floor(PEPE_QTY * (1 - 1e-6) * 1e8) / 1e8
        self.assertEqual(submitted_qty, expected)
        # Precision is preserved — meaningful qty retained for low-priced tokens
        self.assertGreater(submitted_qty, PEPE_QTY * (1 - 1e-5))

    def test_sl_qty_floor_for_small_qty(self):
        client = MockTradingClient()
        trader = CryptoTrader(client)

        qty = 1.234567895  # would round up to 1.23456790 with round-half-to-even
        trader._place_sl_order(SYM, qty, sl_price=0.5)

        submitted_qty = float(client.submitted[0].qty)
        self.assertLessEqual(submitted_qty, qty)
        self.assertEqual(submitted_qty, math.floor(qty * (1 - 1e-6) * 1e8) / 1e8)


class TestCryptoTraderScanDedup(unittest.TestCase):
    """Regression: scan() must not resubmit a duplicate buy signal for a
    symbol whose earlier entry order is still open — this is what allowed
    10-12 duplicate unfilled buy orders per symbol to accumulate across
    bot restarts (in-memory _positions is empty on restart, but the stale
    order is still resting on the broker)."""

    def setUp(self):
        self._universe_patch = patch.object(_cfg, "CRYPTO_UNIVERSE", [SYM])
        self._universe_patch.start()
        self.addCleanup(self._universe_patch.stop)

    def test_scan_skips_symbol_with_open_buy_order(self):
        client = MockTradingClient(orders=[_make_order(side="buy")])
        trader = CryptoTrader(client)
        with patch.object(trader, "_evaluate") as mock_evaluate:
            signals = trader.scan([SYM])

        mock_evaluate.assert_not_called()
        self.assertEqual(signals, [])

    def test_scan_evaluates_symbol_without_open_buy_order(self):
        client = MockTradingClient(orders=[])
        trader = CryptoTrader(client)
        with patch.object(trader, "_evaluate", return_value=None) as mock_evaluate:
            trader.scan([SYM])

        mock_evaluate.assert_called_once_with(SYM)


class TestCryptoMinConfidenceGate(unittest.TestCase):
    """Regression: _run_crypto_cycle must skip buy signals below
    CRYPTO_MIN_CONFIDENCE instead of executing every signal regardless
    of confidence."""

    def test_low_confidence_buy_is_skipped(self):
        from types import SimpleNamespace as _NS
        from engine import orchestrator

        low_conf_sig = _NS(symbol=SYM, action="buy", price=1.0, confidence=0.65, reason="test")
        high_conf_sig = _NS(symbol="ETH/USD", action="buy", price=1.0, confidence=0.90, reason="test")

        crypto_trader = SimpleNamespace(
            monitor_positions=lambda: None,
            status_summary=lambda: "ok",
            scan=lambda universe: [high_conf_sig, low_conf_sig],
            execute_buy=lambda sig: True,
        )
        ctx = SimpleNamespace(crypto_trader=crypto_trader)

        with patch.object(_cfg, "CRYPTO_MIN_CONFIDENCE", 0.70), patch.object(
            orchestrator.cfg, "CRYPTO_MIN_CONFIDENCE", 0.70
        ), patch.object(orchestrator.cfg, "CRYPTO_UNIVERSE", [SYM, "ETH/USD"]):
            calls = []
            crypto_trader.execute_buy = lambda sig: calls.append(sig.symbol) or True
            orchestrator._run_crypto_cycle(ctx)

        self.assertEqual(calls, ["ETH/USD"], "only the >=70% confidence signal should execute")


# ── Crypto momentum scalp lane (paper-only) ──────────────────────────────────

import tempfile
from pathlib import Path

import pandas as pd

SCALP_SYM = "BTC/USD"
SCALP_ASYM = "BTCUSD"


class ScalpMockTradingClient(MockTradingClient):
    """Extends the base mock with account / order-by-coid / cancel / close."""

    def __init__(self, positions=None, orders=None, account_number="PA123456"):
        super().__init__(positions, orders)
        self._account_number = account_number
        self.cancelled = []
        self.closed = []
        self.orders_by_coid = {}
        self._next_id = 0

    def get_account(self):
        return SimpleNamespace(
            account_number=self._account_number,
            non_marginable_buying_power="10000",
            buying_power="10000",
        )

    def submit_order(self, req):
        self._next_id += 1
        self.submitted.append(req)
        return SimpleNamespace(
            id=f"order-{self._next_id}",
            client_order_id=getattr(req, "client_order_id", None),
        )

    def get_order_by_client_id(self, coid):
        if coid in self.orders_by_coid:
            return self.orders_by_coid[coid]
        raise Exception(f"order not found: {coid}")

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(order_id)

    def close_position(self, symbol):
        self.closed.append(symbol)


def _make_1m_bars(rows):
    """rows: list of (high, close, volume), oldest first; last row = current bar."""
    idx = pd.date_range("2026-09-26 10:00", periods=len(rows), freq="1min", tz="UTC")
    return pd.DataFrame(
        [
            {"open": c, "high": h, "low": min(h, c), "close": c, "volume": v}
            for h, c, v in rows
        ],
        index=idx,
    )


def _passing_rows():
    # 5 completed bars (high=100, close=100, vol=1000) + current bar breaking out
    return [(100.0, 100.0, 1000.0)] * 5 + [(101.0, 101.0, 2000.0)]


class ScalpTestBase(unittest.TestCase):
    """Common config patching: scalp enabled, paper mode, BTC/USD scalp universe,
    temp persistence path so tests never touch the repo-root state file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "scalp_state.json"
        patches = [
            patch.object(_cfg, "CRYPTO_SCALP_ENABLED", True),
            patch.object(_cfg, "PAPER", True),
            patch.object(_cfg, "CRYPTO_SCALP_UNIVERSE", [SCALP_SYM]),
            patch.object(_cfg, "CRYPTO_UNIVERSE", [SCALP_SYM, "ETH/USD"]),
            patch.object(_cfg, "CRYPTO_SCALP_MIN_DOLLAR_VOL", 10.0),
            patch("engine.crypto.trader._SCALP_STATE_PATH", self.state_path),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def make_trader(self, account_number="PA123456", positions=None, orders=None):
        return CryptoTrader(ScalpMockTradingClient(
            positions=positions, orders=orders, account_number=account_number,
        ))

    def seed_scalp_position(self, trader, qty=1.0, entry=100.0, scaled_out=False,
                            peak=None):
        pos = CryptoPosition(
            symbol=SCALP_SYM,
            entry_price=entry,
            entry_time=datetime.now(),
            qty=qty,
            notional=qty * entry,
            tp_price=round(entry * (1 + _cfg.CRYPTO_SCALP_TP_PCT / 100), 8),
            sl_price=round(entry * (1 - _cfg.CRYPTO_SCALP_SL_PCT / 100), 8),
            peak_price=peak if peak is not None else entry,
            sl_order_id="sl-1",
            strategy="momentum_scalp",
            scaled_out=scaled_out,
        )
        trader._positions[SCALP_SYM] = pos
        return pos


class TestCryptoScalpScanGuards(ScalpTestBase):
    """1-minute breakout + volume/dollar-vol/spread guards."""

    def _trader_with_bars(self, rows, quote=(100.0, 100.05)):
        trader = self.make_trader()
        bars_mock = patch(
            "engine.crypto.trader._get_crypto_bars",
            return_value=_make_1m_bars(rows),
        ).start()
        self.addCleanup(patch.stopall)
        trader._get_latest_quote = lambda sym: quote
        return trader, bars_mock

    def test_breakout_signal_with_scalp_metadata(self):
        trader, bars_mock = self._trader_with_bars(_passing_rows())
        signals = trader.scan_momentum_scalp([SCALP_SYM])

        self.assertEqual(len(signals), 1)
        sig = signals[0]
        self.assertEqual(sig.action, "buy")
        self.assertEqual(sig.strategy, "momentum_scalp")
        self.assertIn("1m breakout", sig.reason)
        # 1-minute bars requested via the compatible minute path
        self.assertEqual(bars_mock.call_args.kwargs.get("timeframe_minutes"), 1)

    def test_no_breakout_no_signal(self):
        # current close 100.5 does not exceed preceding max high of 101
        rows = [(101.0, 100.0, 1000.0)] * 5 + [(100.6, 100.5, 5000.0)]
        trader, _ = self._trader_with_bars(rows)
        self.assertEqual(trader.scan_momentum_scalp([SCALP_SYM]), [])

    def test_volume_guard_blocks_signal(self):
        # breakout ok but current volume below 1.5x average
        rows = [(100.0, 100.0, 1000.0)] * 5 + [(101.0, 101.0, 1000.0)]
        trader, _ = self._trader_with_bars(rows)
        self.assertEqual(trader.scan_momentum_scalp([SCALP_SYM]), [])

    def test_dollar_volume_guard_blocks_signal(self):
        trader, _ = self._trader_with_bars(_passing_rows())
        with patch.object(_cfg, "CRYPTO_SCALP_MIN_DOLLAR_VOL", 1e12):
            self.assertEqual(trader.scan_momentum_scalp([SCALP_SYM]), [])

    def test_spread_guard_blocks_signal(self):
        trader, _ = self._trader_with_bars(_passing_rows(), quote=(100.0, 101.0))
        self.assertEqual(trader.scan_momentum_scalp([SCALP_SYM]), [])


class TestCryptoScalpLiveGate(ScalpTestBase):
    """Hard gate: scalp entries are paper-only, no matter the config."""

    def test_disabled_flag_blocks_scan_and_entry(self):
        with patch.object(_cfg, "CRYPTO_SCALP_ENABLED", False):
            trader = self.make_trader()
            bars_mock = patch("engine.crypto.trader._get_crypto_bars").start()
            self.addCleanup(patch.stopall)

            self.assertEqual(trader.scan_momentum_scalp([SCALP_SYM]), [])
            bars_mock.assert_not_called()
            sig = SimpleNamespace(symbol=SCALP_SYM, price=100.0, reason="t")
            self.assertFalse(trader.execute_scalp_buy(sig))
            self.assertEqual(trader._client.submitted, [])

    def test_live_trade_mode_blocks_scan_and_entry(self):
        with patch.object(_cfg, "PAPER", False):
            trader = self.make_trader()
            bars_mock = patch("engine.crypto.trader._get_crypto_bars").start()
            self.addCleanup(patch.stopall)

            self.assertEqual(trader.scan_momentum_scalp([SCALP_SYM]), [])
            bars_mock.assert_not_called()
            sig = SimpleNamespace(symbol=SCALP_SYM, price=100.0, reason="t")
            self.assertFalse(trader.execute_scalp_buy(sig))
            self.assertEqual(trader._client.submitted, [])

    def test_live_account_number_blocks_even_in_paper_mode(self):
        trader = self.make_trader(account_number="3PL1234")  # live-style numeric
        bars_mock = patch("engine.crypto.trader._get_crypto_bars").start()
        self.addCleanup(patch.stopall)

        self.assertEqual(trader.scan_momentum_scalp([SCALP_SYM]), [])
        bars_mock.assert_not_called()
        sig = SimpleNamespace(symbol=SCALP_SYM, price=100.0, reason="t")
        self.assertFalse(trader.execute_scalp_buy(sig))
        self.assertEqual(trader._client.submitted, [])


class TestCryptoScalpThrottleAndIsolation(ScalpTestBase):
    """Scan throttling from the fast poll + baseline/scalp symbol isolation."""

    def test_fast_poll_throttles_scan(self):
        trader = self.make_trader()
        calls = []
        trader.scan_momentum_scalp = lambda syms: calls.append(list(syms)) or []

        trader._last_scalp_scan_ts = 0.0
        trader.fast_scalp_poll()
        trader.fast_scalp_poll()  # within CRYPTO_SCALP_SCAN_INTERVAL_S
        self.assertEqual(len(calls), 1, "second poll inside the interval must not rescan")

        trader._last_scalp_scan_ts = 0.0
        trader.fast_scalp_poll()
        self.assertEqual(len(calls), 2)

    def test_baseline_scan_excludes_scalp_symbols_when_enabled(self):
        trader = self.make_trader()
        with patch.object(trader, "_evaluate", return_value=None) as mock_eval:
            trader.scan([SCALP_SYM, "ETH/USD"])
        called = [c.args[0] for c in mock_eval.call_args_list]
        self.assertEqual(called, ["ETH/USD"], "scalp-owned symbol must be left to the scalp lane")

    def test_baseline_scan_covers_scalp_symbols_when_disabled(self):
        with patch.object(_cfg, "CRYPTO_SCALP_ENABLED", False):
            trader = self.make_trader()
            with patch.object(trader, "_evaluate", return_value=None) as mock_eval:
                trader.scan([SCALP_SYM])
            mock_eval.assert_called_once_with(SCALP_SYM)

    def test_baseline_monitor_does_not_close_scalp_at_generic_tp(self):
        trader = self.make_trader()
        pos = self.seed_scalp_position(trader, qty=1.0, entry=100.0)
        trader._sync_positions = lambda: None
        trader._get_latest_price = lambda sym: 110.0  # above scalp TP (105) too
        closed = []
        trader._close_position = lambda sym, reason: closed.append(sym) or True

        trader.monitor_positions()

        self.assertEqual(closed, [], "baseline monitor must not close scalp positions")
        self.assertEqual(pos.peak_price, 100.0, "baseline monitor must not even update scalp peak")


class TestCryptoScalpExitLifecycle(ScalpTestBase):
    """Fill-aware scale-out / giveback exits with broker reconciliation."""

    def _poll_at(self, trader, price):
        trader._get_latest_price = lambda sym: price
        trader._monitor_scalp_positions()

    def test_target_submits_half_scale_out_pending(self):
        trader = self.make_trader()
        pos = self.seed_scalp_position(trader, qty=1.0, entry=100.0)

        self._poll_at(trader, 106.0)  # >= tp 105

        self.assertEqual(len(trader._client.submitted), 1)
        req = trader._client.submitted[0]
        self.assertEqual(float(req.qty), 0.5)
        self.assertTrue(req.client_order_id.startswith("apex-cscalp-out-BTCUSD-"))
        self.assertIn("sl-1", trader._client.cancelled, "old SL must be cancelled before the sell")
        self.assertIsNone(pos.sl_order_id)
        self.assertFalse(pos.scaled_out, "scale-out must NOT be marked done at submit time")
        self.assertEqual(pos.pending_exit["kind"], "scale_out")

    def test_confirmed_fill_replaces_sl_with_actual_remainder(self):
        trader = self.make_trader()
        pos = self.seed_scalp_position(trader, qty=1.0, entry=100.0)
        self._poll_at(trader, 106.0)
        coid = pos.pending_exit["coid"]

        # Broker confirms the fill; position shrank to 0.5
        trader._client.orders_by_coid[coid] = SimpleNamespace(status="filled", filled_qty="0.5")
        trader._client._positions = [
            SimpleNamespace(symbol=SCALP_ASYM, qty="0.5", avg_entry_price="100.0")
        ]
        self._poll_at(trader, 106.0)

        self.assertTrue(pos.scaled_out)
        self.assertIsNone(pos.pending_exit)
        self.assertEqual(pos.qty, 0.5)
        # Fresh SL placed for the actual remaining qty (floored, never rounded up)
        self.assertEqual(len(trader._client.submitted), 2)
        sl_req = trader._client.submitted[1]
        import math as _math
        self.assertEqual(float(sl_req.qty), _math.floor(0.5 * (1 - 1e-6) * 1e8) / 1e8)
        self.assertIsNotNone(pos.sl_order_id)

    def test_no_duplicate_exit_while_partial_pending(self):
        trader = self.make_trader()
        pos = self.seed_scalp_position(trader, qty=1.0, entry=100.0)
        self._poll_at(trader, 106.0)
        coid = pos.pending_exit["coid"]
        trader._client.orders_by_coid[coid] = SimpleNamespace(status="new", filled_qty="0")

        self._poll_at(trader, 107.0)
        self._poll_at(trader, 108.0)

        self.assertEqual(len(trader._client.submitted), 1, "no duplicate exit while one is working")

    def test_giveback_exit_closes_remainder_after_scaleout(self):
        trader = self.make_trader()
        pos = self.seed_scalp_position(trader, qty=0.5, entry=100.0,
                                     scaled_out=True, peak=110.0)

        self._poll_at(trader, 107.0)  # <= 110 * (1 - 2.5%) = 107.25

        self.assertEqual(len(trader._client.submitted), 1)
        req = trader._client.submitted[0]
        self.assertEqual(float(req.qty), 0.5)
        self.assertTrue(req.client_order_id.startswith("apex-cscalp-close-BTCUSD-"))
        self.assertEqual(pos.pending_exit["kind"], "close")

        # Broker confirms fill and position is flat → local state cleared
        coid = pos.pending_exit["coid"]
        trader._client.orders_by_coid[coid] = SimpleNamespace(status="filled", filled_qty="0.5")
        trader._client._positions = []
        self._poll_at(trader, 107.0)

        self.assertNotIn(SCALP_SYM, trader._positions)

    def test_full_close_keeps_state_until_broker_flat(self):
        trader = self.make_trader()
        pos = self.seed_scalp_position(trader, qty=0.5, entry=100.0,
                                     scaled_out=True, peak=110.0)
        self._poll_at(trader, 107.0)
        coid = pos.pending_exit["coid"]
        trader._client.orders_by_coid[coid] = SimpleNamespace(status="filled", filled_qty="0.5")
        # Broker still shows the position (fill not settled yet)
        trader._client._positions = [
            SimpleNamespace(symbol=SCALP_ASYM, qty="0.5", avg_entry_price="100.0")
        ]
        self._poll_at(trader, 107.0)

        self.assertIn(SCALP_SYM, trader._positions, "state clears only after broker is flat")

    def test_order_fetch_failure_keeps_pending_no_duplicate(self):
        trader = self.make_trader()
        pos = self.seed_scalp_position(trader, qty=1.0, entry=100.0)
        self._poll_at(trader, 106.0)
        # orders_by_coid left empty → get_order_by_client_id raises

        self._poll_at(trader, 108.0)

        self.assertIsNotNone(pos.pending_exit, "pending exit must survive a fetch failure")
        self.assertEqual(len(trader._client.submitted), 1, "no duplicate after fetch failure")

    def test_canceled_exit_with_no_fill_restores_protection(self):
        trader = self.make_trader()
        pos = self.seed_scalp_position(trader, qty=1.0, entry=100.0)
        self._poll_at(trader, 106.0)
        coid = pos.pending_exit["coid"]
        trader._client.orders_by_coid[coid] = SimpleNamespace(status="canceled", filled_qty="0")
        trader._client._positions = [
            SimpleNamespace(symbol=SCALP_ASYM, qty="1.0", avg_entry_price="100.0")
        ]

        self._poll_at(trader, 106.0)

        self.assertIsNone(pos.pending_exit)
        self.assertFalse(pos.scaled_out)
        self.assertEqual(len(trader._client.submitted), 2, "fresh SL must be re-placed for actual qty")
        self.assertIsNotNone(pos.sl_order_id)

    def test_tiny_position_closes_fully_at_target(self):
        trader = self.make_trader()
        pos = self.seed_scalp_position(trader, qty=5e-7, entry=100.0)  # below MIN_SPLIT_QTY

        self._poll_at(trader, 106.0)

        req = trader._client.submitted[0]
        self.assertEqual(float(req.qty), 5e-7, "tiny position closes fully instead of splitting")
        self.assertTrue(req.client_order_id.startswith("apex-cscalp-close-"))
        self.assertEqual(pos.pending_exit["kind"], "close")


class TestCryptoScalpPersistence(ScalpTestBase):
    """Restart-safe scalp metadata round-trip."""

    def test_restart_restores_scalp_metadata(self):
        broker_pos = SimpleNamespace(symbol=SCALP_ASYM, qty="0.5", avg_entry_price="100.0")
        pending = {
            "kind": "scale_out", "qty": 0.5, "orig_qty": 1.0,
            "coid": "apex-cscalp-out-BTCUSD-1", "order_id": "o-1", "submitted_at": 1.0,
        }
        trader1 = self.make_trader(positions=[broker_pos])
        pos = self.seed_scalp_position(trader1, qty=0.5, entry=100.0,
                                       scaled_out=True, peak=110.0)
        pos.pending_exit = pending
        trader1._save_scalp_state()
        self.assertTrue(self.state_path.exists())

        # Fresh trader = bot restart; _sync_positions rebuilds from broker + state file
        trader2 = self.make_trader(positions=[broker_pos])
        trader2._sync_positions()

        restored = trader2._positions[SCALP_SYM]
        self.assertEqual(restored.strategy, "momentum_scalp")
        self.assertTrue(restored.scaled_out)
        self.assertEqual(restored.peak_price, 110.0)
        self.assertEqual(restored.tp_price, pos.tp_price)
        self.assertEqual(restored.sl_price, pos.sl_price)
        self.assertEqual(restored.pending_exit["coid"], "apex-cscalp-out-BTCUSD-1")

    def test_baseline_positions_are_not_persisted(self):
        trader = self.make_trader()
        trader._positions[SCALP_SYM] = CryptoPosition(
            symbol=SCALP_SYM,
            entry_price=100.0,
            entry_time=datetime.now(),
            qty=1.0,
            notional=100.0,
            tp_price=104.0,
            sl_price=97.5,
            peak_price=100.0,
        )
        trader._save_scalp_state()
        self.assertFalse(self.state_path.exists(), "baseline-only state file must not be written")


if __name__ == "__main__":
    unittest.main()
