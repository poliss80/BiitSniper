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


if __name__ == "__main__":
    unittest.main()
