"""
engine.crypto.trader
--------------------
Weekend crypto trader for ApexTrader.

Runs when equity markets are closed (Saturday + Sunday).
Uses Alpaca's crypto API (same TradingClient, CryptoHistoricalDataClient).

Strategy: 1h RSI + momentum trend
  - BUY  when RSI(14) crosses above 45 and last 3 closes are ascending
  - SELL when RSI(14) drops below 55 and last 3 closes are descending
  - Hard TP/SL at configurable percentages (default 4% / 2.5%)

Execution: notional dollar orders (Alpaca supports fractional crypto).
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pytz

log = logging.getLogger("ApexTrader")

_ET = pytz.timezone("America/New_York")

# Restart-safe scalp metadata (no secrets) — mirrors the repo-root JSON state
# convention used by .intraday_session_state.json.
_SCALP_STATE_PATH = Path(__file__).resolve().parents[2] / ".crypto_scalp_state.json"

# ── Signal dataclass ──────────────────────────────────────────────────────────

@dataclass
class CryptoSignal:
    symbol:     str   # e.g. "BTC/USD"
    action:     str   # "buy" or "sell"
    price:      float
    confidence: float
    reason:     str
    rsi:        float
    strategy:   str = "baseline"   # "baseline" | "momentum_scalp"


# ── Position tracking ─────────────────────────────────────────────────────────

@dataclass
class CryptoPosition:
    symbol:       str
    entry_price:  float
    entry_time:   datetime.datetime
    qty:          float        # in base currency (BTC, ETH …)
    notional:     float        # USD notional at entry
    tp_price:     float
    sl_price:     float
    peak_price:   float        # trailing: highest price seen since entry
    sl_order_id:  Optional[str] = None  # broker-side stop-limit SL order ID
    strategy:     str = "baseline"      # "baseline" | "momentum_scalp"
    scaled_out:   bool = False          # scalp: partial profit already booked
    pending_exit: Optional[dict] = None # scalp: in-flight exit order metadata


# ── Helpers ───────────────────────────────────────────────────────────────────

def _calc_rsi(closes: pd.Series, period: int = 14) -> float:
    """Return the last RSI value for the given close series."""
    delta = closes.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs  = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def _get_crypto_bars(
    symbol: str,
    timeframe_hours: int = 1,
    limit: int = 60,
    *,
    timeframe_minutes: Optional[int] = None,
    client=None,
    api_key: str = "",
    api_secret: str = "",
) -> Optional[pd.DataFrame]:
    """Fetch OHLCV bars for a crypto pair via Alpaca.

    Pass an already-constructed *client* (CryptoHistoricalDataClient) to avoid
    creating a new one on every call. Pass *timeframe_minutes* for minute bars
    (e.g. 1 for the scalp lane); the default hourly path is unchanged.
    """
    try:
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        if client is None:
            from alpaca.data.historical import CryptoHistoricalDataClient
            client = CryptoHistoricalDataClient(api_key=api_key or None, secret_key=api_secret or None)

        end = datetime.datetime.now(pytz.utc)
        if timeframe_minutes is not None:
            tf = TimeFrame(timeframe_minutes, TimeFrameUnit.Minute)
            start = end - datetime.timedelta(minutes=limit * timeframe_minutes + 5)
        else:
            tf = TimeFrame(timeframe_hours, TimeFrameUnit.Hour)
            start = end - datetime.timedelta(hours=limit * timeframe_hours + 4)

        req = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start,
            end=end,
            limit=limit,
        )
        bars = client.get_crypto_bars(req)
        df = bars.df

        if df is None or df.empty:
            return None

        # Multi-index: (symbol, timestamp) → flatten to timestamp
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol") if symbol in df.index.get_level_values("symbol") else df.droplevel(0)

        df = df.sort_index()
        df = df[["open", "high", "low", "close", "volume"]].tail(limit)
        return df

    except Exception as e:
        log.debug(f"[CRYPTO] bars fetch failed for {symbol}: {e}")
        return None


# ── CryptoTrader ──────────────────────────────────────────────────────────────

class CryptoTrader:
    """Scans crypto pairs and manages weekend positions via Alpaca."""

    def __init__(
        self,
        trading_client,
        api_key: str = "",
        api_secret: str = "",
    ) -> None:
        self._client     = trading_client
        self._api_key    = api_key
        self._api_secret = api_secret
        self._positions: Dict[str, CryptoPosition] = {}
        # Lazily cached data client (avoids constructing a new one every price fetch)
        self._data_client = None
        # Scalp lane state
        self._last_scalp_scan_ts: float = 0.0
        self._scalp_acct_ok: Optional[bool] = None

    # ── Dynamic universe discovery ────────────────────────────────────────────

    def fetch_tradeable_universe(self, configured: List[str]) -> List[str]:
        """Query Alpaca for all currently tradeable crypto assets and intersect
        with *configured* (CRYPTO_UNIVERSE).  Returns *configured* unchanged if
        the API call fails so the bot always has a usable universe.

        Symbols returned by Alpaca use the slash-free form ('BTCUSD'); we
        normalise to 'BTC/USD' via _normalize_symbol before intersecting.
        """
        try:
            from alpaca.trading.requests import GetAssetsRequest
            from alpaca.trading.enums import AssetClass

            req = GetAssetsRequest(asset_class=AssetClass.CRYPTO)
            assets = self._client.get_all_assets(req)
            tradeable = {
                _normalize_symbol(a.symbol)
                for a in assets
                if a.tradable
            }
            # Keep only pairs that are both configured AND live on Alpaca
            result = [s for s in configured if s in tradeable]
            removed = [s for s in configured if s not in tradeable]
            if removed:
                log.warning(f"[CRYPTO] Removed non-tradeable pairs: {removed}")
            log.info(f"[CRYPTO] Tradeable universe ({len(result)}): {result}")
            return result if result else configured
        except Exception as e:
            log.warning(f"[CRYPTO] fetch_tradeable_universe failed, using config list: {e}")
            return configured

    @staticmethod
    def _alpaca_sym(symbol: str) -> str:
        """Convert 'BTC/USD' → 'BTCUSD' (Alpaca trading API format)."""
        return symbol.replace("/", "")

    # ── Sync open positions from broker ──────────────────────────────────────

    def _sync_positions(self) -> None:
        """Pull live crypto positions from Alpaca and reconcile local tracking."""
        try:
            all_pos = self._client.get_all_positions()
        except Exception as e:
            log.warning(f"[CRYPTO] get_all_positions failed: {e}")
            return

        from engine import config as _cfg
        live_symbols = set()
        for pos in all_pos:
            sym = str(pos.symbol)
            # Alpaca returns crypto symbols as "BTCUSD" internally; normalise to "BTC/USD"
            # We also accept already-normalised form.
            if "/" not in sym:
                # e.g. "BTCUSD" → try to detect known pairs
                sym_norm = _normalize_symbol(sym)
            else:
                sym_norm = sym

            # Skip non-crypto positions (equities, options, etc.)
            if sym_norm not in _cfg.CRYPTO_UNIVERSE:
                continue

            qty = float(pos.qty)
            if qty <= 0:
                continue  # no short crypto positions tracked

            live_symbols.add(sym_norm)
            if sym_norm not in self._positions:
                # Position opened externally or bot restarted — recover SL if already live
                entry = float(pos.avg_entry_price)
                notional = qty * entry
                # Restore persisted scalp metadata so a restart does not silently
                # downgrade an open scalp position to the baseline lane.
                meta = self._load_scalp_state().get(sym_norm) if self._scalp_config_enabled() else None
                if meta:
                    sl_price = float(meta.get("sl_price") or round(entry * (1 - _cfg.CRYPTO_SCALP_SL_PCT / 100), 8))
                    tp_price = float(meta.get("tp_price") or round(entry * (1 + _cfg.CRYPTO_SCALP_TP_PCT / 100), 8))
                else:
                    sl_price = round(entry * (1 - _cfg.CRYPTO_SL_PCT / 100), 8)
                    tp_price = round(entry * (1 + _cfg.CRYPTO_TP_PCT / 100), 8)
                # Check for an existing open SL order before placing a new one (avoids
                # "available: 0" errors when restarting with existing positions + SL orders)
                existing_sl = self._find_existing_sl_order(sym_norm)
                if existing_sl:
                    sl_order_id = existing_sl
                elif self._has_open_buy_order(sym_norm):
                    # Entry buy limit still resting — a sell SL below it would trip
                    # Alpaca's wash-trade guard (40310000). Defer to a later cycle.
                    log.debug(f"[CRYPTO] Deferring SL for {sym_norm} — entry buy order still open")
                    sl_order_id = None
                else:
                    sl_order_id = self._place_sl_order(sym_norm, qty, sl_price)
                entry_time = datetime.datetime.now(_ET)
                if meta and meta.get("entry_time"):
                    try:
                        entry_time = datetime.datetime.fromisoformat(meta["entry_time"])
                    except (TypeError, ValueError):
                        pass
                self._positions[sym_norm] = CryptoPosition(
                    symbol=sym_norm,
                    entry_price=entry,
                    entry_time=entry_time,
                    qty=qty,
                    notional=notional,
                    tp_price=tp_price,
                    sl_price=sl_price,
                    peak_price=max(entry, float(meta.get("peak_price") or entry)) if meta else entry,
                    sl_order_id=sl_order_id,
                    strategy="momentum_scalp" if meta else "baseline",
                    scaled_out=bool(meta.get("scaled_out")) if meta else False,
                    pending_exit=meta.get("pending_exit") if meta else None,
                )
            else:
                # Position already tracked — place broker SL if not yet done
                tracked = self._positions[sym_norm]
                if tracked.sl_order_id is None:
                    if self._has_open_buy_order(sym_norm):
                        # Entry buy limit still resting — defer SL to avoid wash-trade rejection
                        log.debug(f"[CRYPTO] Deferring SL for {sym_norm} — entry buy order still open")
                    else:
                        tracked.sl_order_id = self._place_sl_order(sym_norm, qty, tracked.sl_price)
                # Refresh qty from broker (may differ from estimated qty)
                tracked.qty = qty

        # Remove stale local entries
        stale = [s for s in list(self._positions) if s not in live_symbols]
        removed_scalp = False
        for s in stale:
            log.info(f"[CRYPTO] Position closed externally: {s}")
            removed_scalp = removed_scalp or self._positions[s].strategy == "momentum_scalp"
            self._positions.pop(s, None)
        if removed_scalp:
            self._save_scalp_state()

    # ── Monitor open positions for TP / SL ───────────────────────────────────

    def monitor_positions(self) -> None:
        """Check TP/SL for every open crypto position. Close when hit."""
        self._sync_positions()

        for sym, pos in list(self._positions.items()):
            if pos.strategy == "momentum_scalp":
                continue  # scalp positions are managed by the 10s fast monitor
            try:
                snapshot = self._get_latest_price(sym)
                if snapshot is None:
                    continue

                price = snapshot
                pos.peak_price = max(pos.peak_price, price)

                reason = None
                if price >= pos.tp_price:
                    reason = f"TP hit {price:.4f} >= {pos.tp_price:.4f}"
                elif price <= pos.sl_price:
                    reason = f"SL hit {price:.4f} <= {pos.sl_price:.4f}"

                if reason:
                    self._close_position(sym, reason)

            except Exception as e:
                log.warning(f"[CRYPTO] Monitor error for {sym}: {e}")

    # ── Scanner ───────────────────────────────────────────────────────────────

    def scan(self, symbols: List[str]) -> List[CryptoSignal]:
        """Return buy/sell signals for the given crypto symbols."""
        signals = []
        # While the scalp lane is enabled it owns its designated symbols —
        # the baseline RSI scanner must not also enter them.
        scalp_owned = set(self._scalp_universe()) if self._scalp_config_enabled() else set()
        for sym in symbols:
            if sym in scalp_owned:
                continue
            if sym in self._positions:
                continue  # already holding, skip new entry
            # An unfilled entry order can outlive an in-memory-only position
            # tracker across bot restarts; without this check every restart
            # would resubmit a duplicate buy for the same still-open order.
            if self._has_open_buy_order(sym):
                continue
            sig = self._evaluate(sym)
            if sig:
                signals.append(sig)
        return sorted(signals, key=lambda s: s.confidence, reverse=True)

    def _evaluate(self, symbol: str) -> Optional[CryptoSignal]:
        """Generate a signal for one crypto pair, or None."""
        from engine import config as _cfg
        df = _get_crypto_bars(symbol, timeframe_hours=1, limit=50, client=self._get_data_client())
        if df is None or len(df) < 20:
            log.debug(f"[CRYPTO] {symbol}: insufficient bars")
            return None

        closes = df["close"]
        rsi    = _calc_rsi(closes, period=14)
        c0, c1, c2 = float(closes.iloc[-1]), float(closes.iloc[-2]), float(closes.iloc[-3])

        trend_up   = c0 > c1 > c2
        trend_down = c0 < c1 < c2
        price      = c0

        # BUY: RSI in [40, 70) and 3-bar uptrend
        if _cfg.CRYPTO_RSI_BUY_MIN <= rsi < _cfg.CRYPTO_RSI_BUY_MAX and trend_up:
            conf = min(0.95, 0.60 + (rsi - _cfg.CRYPTO_RSI_BUY_MIN) / 60.0 * 0.35)
            return CryptoSignal(
                symbol=symbol,
                action="buy",
                price=price,
                confidence=round(conf, 2),
                reason=f"RSI={rsi:.1f} 3-bar uptrend",
                rsi=rsi,
            )

        # SELL: RSI < SELL threshold and 3-bar downtrend (only if held — scan() filters)
        if rsi < _cfg.CRYPTO_RSI_SELL_MAX and trend_down:
            conf = min(0.90, 0.55 + ((_cfg.CRYPTO_RSI_SELL_MAX - rsi) / 20.0) * 0.35)
            return CryptoSignal(
                symbol=symbol,
                action="sell",
                price=price,
                confidence=round(conf, 2),
                reason=f"RSI={rsi:.1f} 3-bar downtrend",
                rsi=rsi,
            )

        return None

    # ── Execution ─────────────────────────────────────────────────────────────

    def execute_buy(self, signal: CryptoSignal) -> bool:
        """Place a GTC limit buy for the given crypto signal.

        Alpaca does not support bracket/OTOCO orders for crypto (error 42210000).
        Entry: limit at current price + 0.15% — fills quickly on liquid pairs,
               avoids market-order slippage, and persists until filled (GTC).
        SL:    broker-side GTC stop-limit sell, placed by _sync_positions() as
               soon as the buy limit is confirmed filled (position appears on
               Alpaca). This fires even if the bot is offline.
        TP:    software polling via monitor_positions() (no broker-side TP order
               type available for crypto).
        """
        from engine import config as _cfg
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        try:
            account       = self._client.get_account()

            # ── Account / mode safety check ───────────────────────────────────
            # Alpaca paper account numbers start with "PA"; live are numeric only.
            acct_num      = str(getattr(account, "account_number", "") or "")
            is_paper_acct = acct_num.upper().startswith("PA")
            is_paper_cfg  = _cfg.PAPER
            if is_paper_cfg != is_paper_acct:
                log.error(
                    f"[CRYPTO] ACCOUNT MISMATCH — TRADE_MODE={'paper' if is_paper_cfg else 'live'} "
                    f"but connected account is {'PAPER' if is_paper_acct else 'LIVE'} ({acct_num}). "
                    f"Aborting order to prevent trading on wrong account."
                )
                return False
            log.debug(
                f"[CRYPTO] Account verified: {'PAPER' if is_paper_acct else 'LIVE'} ({acct_num}) "
                f"matches TRADE_MODE={'paper' if is_paper_cfg else 'live'}"
            )

            # Alpaca crypto orders are evaluated against non_marginable_buying_power (cash).
            # Using the broader buying_power (which includes margin) causes 40310000 errors.
            cash_bp       = float(getattr(account, "non_marginable_buying_power", None) or account.buying_power)

            # ── Max positions gate ────────────────────────────────────────────
            portfolio_count = len(self._client.get_all_positions())
            if portfolio_count >= _cfg.MAX_POSITIONS:
                log.info(
                    f"[CRYPTO] Portfolio position cap reached "
                    f"({portfolio_count}/{_cfg.MAX_POSITIONS}) — skipping {signal.symbol}"
                )
                return False

            # Size each position as (cash BP) / (max positions) so all slots
            # together consume 100% of available buying power.
            slots         = _cfg.CRYPTO_MAX_POSITIONS  # fixed divisor keeps each slice consistent
            if _cfg.CRYPTO_POSITION_PCT > 0:
                # Legacy explicit % override
                notional  = round(cash_bp * _cfg.CRYPTO_POSITION_PCT / 100, 2)
            else:
                notional  = round(cash_bp / slots, 2)
            notional      = max(_cfg.CRYPTO_MIN_NOTIONAL, notional)
            # Hard cap: never request more than 98% of available cash
            notional      = min(notional, round(cash_bp * 0.98, 2))
            if notional < _cfg.CRYPTO_MIN_NOTIONAL:
                log.warning(
                    f"[CRYPTO] Insufficient cash balance for {signal.symbol} "
                    f"(available={cash_bp:.2f}, min={_cfg.CRYPTO_MIN_NOTIONAL}) — skipping"
                )
                return False

            # Alpaca expects "BTCUSD" style for trading (no slash)
            alpaca_sym = self._alpaca_sym(signal.symbol)

            # Aggressive limit: 0.15% above signal price to fill quickly
            limit_price = round(signal.price * 1.0015, 8)
            tp_price    = round(signal.price * (1 + _cfg.CRYPTO_TP_PCT  / 100), 8)
            sl_price    = round(signal.price * (1 - _cfg.CRYPTO_SL_PCT  / 100), 8)

            # qty derived from notional (plain limit orders require qty, not notional)
            qty = round(notional / limit_price, 8)
            if qty <= 0:
                log.warning(f"[CRYPTO] Calculated qty=0 for {signal.symbol}, skipping")
                return False

            order_req = LimitOrderRequest(
                symbol=alpaca_sym,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
                limit_price=limit_price,
            )
            order = self._client.submit_order(order_req)
            log.info(
                f"[CRYPTO] LIMIT BUY {signal.symbol} qty={qty:.6f} (~${notional:.0f}) "
                f"limit={limit_price:.4f} | software TP={tp_price:.4f} SL={sl_price:.4f} "
                f"| conf={signal.confidence:.0%} | {signal.reason} | order={order.id}"
            )

            # Record position locally; corrected to actual fill on next _sync_positions()
            self._positions[signal.symbol] = CryptoPosition(
                symbol=signal.symbol,
                entry_price=limit_price,
                entry_time=datetime.datetime.now(_ET),
                qty=qty,
                notional=notional,
                tp_price=tp_price,
                sl_price=sl_price,
                peak_price=limit_price,
            )
            return True

        except Exception as e:
            log.error(f"[CRYPTO] BUY order failed for {signal.symbol}: {e}", exc_info=True)
            return False

    def _find_existing_sl_order(self, symbol: str) -> Optional[str]:
        """Return the order ID of any existing open GTC stop-limit SELL order for *symbol*.

        Called on bot restart to avoid placing a duplicate SL when one is already live.
        """
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import OrderSide, QueryOrderStatus
            alpaca_sym = self._alpaca_sym(symbol)
            # Fetch all open SELL orders without a symbol filter — Alpaca's
            # per-symbol filter is unreliable for crypto pairs; we match in Python.
            req = GetOrdersRequest(
                status=QueryOrderStatus.OPEN,
                side=OrderSide.SELL,
                limit=100,
            )
            orders = self._client.get_orders(req)
            log.debug(f"[CRYPTO] _find_existing_sl_order: {len(orders)} open SELL orders for {symbol}")
            for o in orders:
                o_sym = str(getattr(o, "symbol", "")).upper()
                if o_sym != alpaca_sym.upper():
                    continue
                order_type = str(getattr(o, "order_type", getattr(o, "type", ""))).lower().replace(" ", "_").replace("-", "_")
                if "stop" in order_type:
                    log.info(
                        f"[CRYPTO] Reusing existing SL order for {symbol} | type={order_type} | order={o.id}"
                    )
                    return str(o.id)
        except Exception as e:
            log.debug(f"[CRYPTO] _find_existing_sl_order failed for {symbol}: {e}")
        return None

    def _has_open_buy_order(self, symbol: str) -> bool:
        """Return True if any open BUY order exists for *symbol*.

        Used to defer SL placement while the entry buy limit is still resting —
        placing a sell limit below a resting buy limit triggers Alpaca's
        wash-trade rejection (40310000).
        """
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import OrderSide, QueryOrderStatus
            alpaca_sym = self._alpaca_sym(symbol)
            # Fetch all open BUY orders without a symbol filter — Alpaca's
            # per-symbol filter is unreliable for crypto pairs; we match in Python.
            req = GetOrdersRequest(
                status=QueryOrderStatus.OPEN,
                side=OrderSide.BUY,
                limit=100,
            )
            orders = self._client.get_orders(req)
            log.debug(f"[CRYPTO] _has_open_buy_order: {len(orders)} open BUY orders for {symbol}")
            for o in orders:
                o_sym = str(getattr(o, "symbol", "")).upper()
                if o_sym == alpaca_sym.upper():
                    return True
        except Exception as e:
            log.debug(f"[CRYPTO] _has_open_buy_order failed for {symbol}: {e}")
        return False

    def _place_sl_order(self, symbol: str, qty: float, sl_price: float) -> Optional[str]:
        """Submit a broker-side GTC stop-limit SELL order for the SL level.

        Returns the Alpaca order ID on success, or None on failure.
        The limit_price is set 0.5% below the stop to maximise fill probability
        while still bounding slippage.
        """
        from alpaca.trading.requests import StopLimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        try:
            alpaca_sym  = self._alpaca_sym(symbol)
            limit_price = round(sl_price * 0.995, 8)  # 0.5% below stop for fill assurance
            # Qty safety: broker qty strings with many decimals can come back
            # microscopically inflated after float() round-trips; submitting the
            # raw value then fails with "insufficient balance" (40310000).
            # Shave by 1e-6 relative and floor to 8 decimals (never rounds up),
            # so the submitted qty is always <= the true available balance while
            # keeping enough precision for low-priced tokens (e.g. PEPE).
            safe_qty    = math.floor(qty * (1 - 1e-6) * 1e8) / 1e8
            order_req   = StopLimitOrderRequest(
                symbol=alpaca_sym,
                qty=safe_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=sl_price,
                limit_price=limit_price,
            )
            order = self._client.submit_order(order_req)
            log.info(
                f"[CRYPTO] BROKER SL placed for {symbol} stop={sl_price:.4f} "
                f"limit={limit_price:.4f} qty={safe_qty:.8f} | order={order.id}"
            )
            return str(order.id)
        except Exception as e:
            err = str(e)
            # "available: 0" means an open SL order already locks this qty — not a real error
            if '"available":"0"' in err or "available: 0" in err.lower():
                log.debug(f"[CRYPTO] SL already exists for {symbol} (balance locked by open order) — skipping duplicate")
            else:
                log.warning(f"[CRYPTO] SL order placement failed for {symbol}: {e}")
            return None

    def _cancel_sl_order(self, symbol: str, sl_order_id: str) -> None:
        """Cancel the broker-side SL order (called before any forced close)."""
        try:
            self._client.cancel_order_by_id(sl_order_id)
            log.info(f"[CRYPTO] SL order cancelled for {symbol} | order={sl_order_id}")
        except Exception as e:
            log.debug(f"[CRYPTO] SL cancel for {symbol} failed (may already be filled/cancelled): {e}")

    def _close_position(self, symbol: str, reason: str) -> bool:
        """Market-sell the entire position in *symbol* (software TP exit or forced close).

        Cancels the broker-side SL order first to prevent it becoming orphaned.
        """
        try:
            pos = self._positions.get(symbol)
            if pos and pos.sl_order_id:
                self._cancel_sl_order(symbol, pos.sl_order_id)
            alpaca_sym = self._alpaca_sym(symbol)
            self._client.close_position(alpaca_sym)
            self._positions.pop(symbol, None)
            if pos:
                log.info(f"[CRYPTO] CLOSED {symbol} | {reason} | entry={pos.entry_price:.4f}")
            return True
        except Exception as e:
            log.error(f"[CRYPTO] Close failed for {symbol}: {e}", exc_info=True)
            return False

    # ── Momentum scalp lane (PAPER ONLY) ─────────────────────────────────────
    # Independent fast lane for 1-minute momentum scalps on the crypto majors.
    # Hard gate: entries require CRYPTO_SCALP_ENABLED=true, TRADE_MODE=paper,
    # AND a connected Alpaca paper account (account number starts with "PA").
    # There is no path that lets this lane trade a live account.

    def _scalp_config_enabled(self) -> bool:
        from engine import config as _cfg
        return bool(getattr(_cfg, "CRYPTO_SCALP_ENABLED", False))

    def _scalp_universe(self) -> List[str]:
        """Scalp-designated symbols: CRYPTO_SCALP_UNIVERSE ∩ CRYPTO_UNIVERSE."""
        from engine import config as _cfg
        configured = getattr(_cfg, "CRYPTO_SCALP_UNIVERSE", []) or []
        return [s for s in configured if s in _cfg.CRYPTO_UNIVERSE]

    def _scalp_runtime_allowed(self) -> bool:
        """Hard paper-only gate for the scalp lane.

        Requires the config opt-in AND PAPER mode AND a connected Alpaca
        paper account (account_number starts with 'PA'). The account check is
        cached after the first successful lookup. Never True for live.
        """
        from engine import config as _cfg
        if not self._scalp_config_enabled() or not _cfg.PAPER:
            return False
        if self._scalp_acct_ok is None:
            try:
                account  = self._client.get_account()
                acct_num = str(getattr(account, "account_number", "") or "")
                self._scalp_acct_ok = acct_num.upper().startswith("PA")
                if not self._scalp_acct_ok:
                    log.error(
                        f"[CRYPTO][SCALP] DISABLED — connected account {acct_num} "
                        f"is not an Alpaca paper account"
                    )
            except Exception as e:
                log.warning(f"[CRYPTO][SCALP] account verification failed: {e}")
                return False
        return bool(self._scalp_acct_ok)

    # ── Scalp scan (1-minute momentum breakout) ───────────────────────────────

    def scan_momentum_scalp(self, symbols: List[str]) -> List[CryptoSignal]:
        """Scan *symbols* for 1-minute momentum scalp breakouts. Paper-only."""
        if not self._scalp_runtime_allowed():
            return []
        owned = set(self._scalp_universe())
        signals = []
        for sym in symbols:
            if sym not in owned:
                continue
            if sym in self._positions:
                continue  # already holding — no pyramiding
            if self._has_open_buy_order(sym):
                continue
            sig = self._evaluate_scalp(sym)
            if sig:
                signals.append(sig)
        return signals

    def _evaluate_scalp(self, symbol: str) -> Optional[CryptoSignal]:
        """1-minute momentum breakout setup for one symbol, or None.

        Entry conditions (no session assumptions — crypto trades 24/7):
          - current bar close breaks above the highest high of the preceding
            CRYPTO_SCALP_BREAKOUT_BARS completed bars
          - current bar volume >= CRYPTO_SCALP_VOLUME_MULT × mean volume of the
            preceding up-to-CRYPTO_SCALP_VOLUME_LOOKBACK completed bars
          - rolling mean per-bar dollar volume >= CRYPTO_SCALP_MIN_DOLLAR_VOL
          - latest quote spread <= CRYPTO_SCALP_MAX_SPREAD_PCT
        """
        from engine import config as _cfg
        breakout_bars = int(_cfg.CRYPTO_SCALP_BREAKOUT_BARS)
        lookback      = int(_cfg.CRYPTO_SCALP_VOLUME_LOOKBACK)
        df = _get_crypto_bars(
            symbol, timeframe_minutes=1, limit=max(lookback, breakout_bars) + 2,
            client=self._get_data_client(),
        )
        if df is None or len(df) < breakout_bars + 1:
            log.debug(f"[CRYPTO][SCALP] {symbol}: insufficient 1m bars")
            return None

        current   = df.iloc[-1]   # latest (possibly forming) bar
        completed = df.iloc[:-1]  # fully closed bars only
        close = float(current["close"])
        vol   = float(current["volume"])

        prior_highs = completed["high"].tail(breakout_bars)
        if prior_highs.empty or close <= float(prior_highs.max()):
            return None

        prior    = completed.tail(lookback)
        avg_vol  = float(prior["volume"].mean()) if len(prior) else 0.0
        vol_mult = float(_cfg.CRYPTO_SCALP_VOLUME_MULT)
        if avg_vol <= 0 or vol < avg_vol * vol_mult:
            return None

        dollar_vol = float((prior["close"] * prior["volume"]).mean())
        if dollar_vol < float(_cfg.CRYPTO_SCALP_MIN_DOLLAR_VOL):
            return None

        quote = self._get_latest_quote(symbol)
        if quote is None:
            return None
        bid, ask = quote
        if bid <= 0 or ask <= 0:
            return None
        mid        = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100
        if spread_pct > float(_cfg.CRYPTO_SCALP_MAX_SPREAD_PCT):
            log.debug(f"[CRYPTO][SCALP] {symbol}: spread {spread_pct:.3f}% too wide")
            return None

        return CryptoSignal(
            symbol=symbol,
            action="buy",
            price=close,
            confidence=0.85,
            reason=(
                f"1m breakout close={close:.4f}>{float(prior_highs.max()):.4f} "
                f"vol={vol:.0f}>={vol_mult}xavg({avg_vol:.0f}) "
                f"$vol={dollar_vol:.0f} spread={spread_pct:.3f}%"
            ),
            rsi=0.0,
            strategy="momentum_scalp",
        )

    def execute_scalp_buy(self, signal: CryptoSignal) -> bool:
        """Place a GTC limit buy for a scalp signal. PAPER ONLY — hard-gated.

        Uses the same cash-based notional sizing as the baseline lane
        (cash BP / CRYPTO_MAX_POSITIONS); no leverage or size multipliers.
        The broker-side stop-limit SL at CRYPTO_SCALP_SL_PCT is placed by
        _sync_positions() once the entry fills; exits are managed by the
        10-second fast monitor (scale-out at target + peak giveback).
        """
        from engine import config as _cfg
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        if not self._scalp_runtime_allowed():
            log.error(
                f"[CRYPTO][SCALP] Entry BLOCKED for {signal.symbol} — scalp lane is "
                f"paper-only and requires CRYPTO_SCALP_ENABLED=true"
            )
            return False
        try:
            account = self._client.get_account()
            cash_bp = float(getattr(account, "non_marginable_buying_power", None) or account.buying_power)
            slots   = _cfg.CRYPTO_MAX_POSITIONS
            if _cfg.CRYPTO_POSITION_PCT > 0:
                notional = round(cash_bp * _cfg.CRYPTO_POSITION_PCT / 100, 2)
            else:
                notional = round(cash_bp / slots, 2)
            notional = max(_cfg.CRYPTO_MIN_NOTIONAL, notional)
            notional = min(notional, round(cash_bp * 0.98, 2))
            if notional < _cfg.CRYPTO_MIN_NOTIONAL:
                log.warning(
                    f"[CRYPTO][SCALP] Insufficient cash for {signal.symbol} "
                    f"(available={cash_bp:.2f}) — skipping"
                )
                return False

            alpaca_sym  = self._alpaca_sym(signal.symbol)
            limit_price = round(signal.price * 1.0015, 8)
            tp_price    = round(signal.price * (1 + _cfg.CRYPTO_SCALP_TP_PCT / 100), 8)
            sl_price    = round(signal.price * (1 - _cfg.CRYPTO_SCALP_SL_PCT / 100), 8)
            qty = round(notional / limit_price, 8)
            if qty <= 0:
                return False

            coid = f"apex-cscalp-in-{alpaca_sym}-{int(time.time())}"
            order_req = LimitOrderRequest(
                symbol=alpaca_sym,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
                limit_price=limit_price,
                client_order_id=coid,
            )
            order = self._client.submit_order(order_req)
            log.info(
                f"[CRYPTO][SCALP] LIMIT BUY {signal.symbol} qty={qty:.6f} (~${notional:.0f}) "
                f"limit={limit_price:.4f} | scalp TP={tp_price:.4f} SL={sl_price:.4f} "
                f"| {signal.reason} | order={order.id}"
            )
            self._positions[signal.symbol] = CryptoPosition(
                symbol=signal.symbol,
                entry_price=limit_price,
                entry_time=datetime.datetime.now(_ET),
                qty=qty,
                notional=notional,
                tp_price=tp_price,
                sl_price=sl_price,
                peak_price=limit_price,
                strategy="momentum_scalp",
            )
            self._save_scalp_state()
            return True
        except Exception as e:
            log.error(f"[CRYPTO][SCALP] BUY order failed for {signal.symbol}: {e}", exc_info=True)
            return False

    # ── Scalp fast monitor (10s cadence) ──────────────────────────────────────

    def fast_scalp_poll(self) -> None:
        """One fast-poll iteration for the scalp lane: manage open scalp
        positions, then run the throttled scalp scan. No-op unless the lane
        is enabled and the paper-only gate passes."""
        if not self._scalp_config_enabled():
            return
        if not self._scalp_runtime_allowed():
            return
        self._monitor_scalp_positions()
        from engine import config as _cfg
        now = time.time()
        if now - self._last_scalp_scan_ts >= float(getattr(_cfg, "CRYPTO_SCALP_SCAN_INTERVAL_S", 60)):
            self._last_scalp_scan_ts = now
            for sig in self.scan_momentum_scalp(self._scalp_universe()):
                self.execute_scalp_buy(sig)

    def _monitor_scalp_positions(self) -> None:
        """Scale-out / giveback management for scalp positions only.

        Exits are fill-aware: a submitted exit is recorded in
        pos.pending_exit and reconciled against the broker before any state
        change — no duplicate closes, no premature scale-out marking.
        """
        from engine import config as _cfg
        scalp = [(s, p) for s, p in self._positions.items() if p.strategy == "momentum_scalp"]
        if not scalp:
            return
        giveback = float(_cfg.CRYPTO_SCALP_GIVEBACK_PCT) / 100
        for sym, pos in scalp:
            try:
                if pos.pending_exit:
                    self._reconcile_scalp_pending(sym, pos)
                    continue
                price = self._get_latest_price(sym)
                if price is None:
                    continue
                pos.peak_price = max(pos.peak_price, price)
                if not pos.scaled_out and price >= pos.tp_price:
                    log.info(
                        f"[CRYPTO][SCALP] {sym} target hit {price:.4f} >= {pos.tp_price:.4f} — scaling out"
                    )
                    self._submit_scalp_exit(sym, pos, price, kind="scale_out")
                elif pos.scaled_out and price <= pos.peak_price * (1 - giveback):
                    log.info(
                        f"[CRYPTO][SCALP] {sym} giveback exit {price:.4f} <= "
                        f"peak {pos.peak_price:.4f} - {_cfg.CRYPTO_SCALP_GIVEBACK_PCT}%"
                    )
                    self._submit_scalp_exit(sym, pos, price, kind="close")
            except Exception as e:
                log.warning(f"[CRYPTO][SCALP] Monitor error for {sym}: {e}")
        self._save_scalp_state()

    def _submit_scalp_exit(self, sym: str, pos: CryptoPosition, price: float, kind: str) -> bool:
        """Submit a fill-aware scalp exit (scale_out = half, close = all).

        Positions too small to split meaningfully close fully at the target.
        The broker SL is cancelled first; if the sell submission itself fails,
        SL protection is restored for the full quantity.
        """
        from engine import config as _cfg
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        sell_qty = pos.qty
        if kind == "scale_out":
            half      = math.floor(pos.qty / 2 * 1e8) / 1e8
            remainder = pos.qty - half
            min_split = float(_cfg.CRYPTO_SCALP_MIN_SPLIT_QTY)
            if half < min_split or remainder < min_split:
                kind = "close"  # tiny position — close fully at target
            else:
                sell_qty = half
        try:
            if pos.sl_order_id:
                self._cancel_sl_order(sym, pos.sl_order_id)
                pos.sl_order_id = None
            alpaca_sym  = self._alpaca_sym(sym)
            limit_price = round(price * 0.9985, 8)  # aggressive limit sell
            tag  = "out" if kind == "scale_out" else "close"
            coid = f"apex-cscalp-{tag}-{alpaca_sym}-{int(time.time())}"
            order_req = LimitOrderRequest(
                symbol=alpaca_sym,
                qty=sell_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                limit_price=limit_price,
                client_order_id=coid,
            )
            order = self._client.submit_order(order_req)
            pos.pending_exit = {
                "kind":         kind,
                "qty":          sell_qty,
                "orig_qty":     pos.qty,
                "coid":         coid,
                "order_id":     str(order.id),
                "submitted_at": time.time(),
            }
            log.info(
                f"[CRYPTO][SCALP] {kind.upper()} submitted {sym} qty={sell_qty:.8f} "
                f"limit={limit_price:.4f} | order={order.id} coid={coid}"
            )
            self._save_scalp_state()
            return True
        except Exception as e:
            log.error(f"[CRYPTO][SCALP] {kind} submit failed for {sym}: {e}", exc_info=True)
            # Restore broker-side protection for the full actual quantity
            if pos.sl_order_id is None:
                pos.sl_order_id = self._place_sl_order(sym, pos.qty, pos.sl_price)
            self._save_scalp_state()
            return False

    def _reconcile_scalp_pending(self, sym: str, pos: CryptoPosition) -> None:
        """Reconcile a recorded pending scalp exit against the broker.

        Conservative on any fetch failure: keep the pending record and wait —
        never submit a duplicate exit.
        """
        pending = pos.pending_exit or {}
        coid = str(pending.get("coid") or "")
        try:
            if coid:
                order = self._client.get_order_by_client_id(coid)
            else:
                order = self._client.get_order_by_id(str(pending.get("order_id")))
        except Exception as e:
            log.debug(f"[CRYPTO][SCALP] order fetch failed for {sym} — keeping pending exit: {e}")
            return
        status     = str(getattr(order, "status", "") or "").lower()
        filled_qty = float(getattr(order, "filled_qty", 0) or 0)
        if status == "filled" or (
            status in ("canceled", "expired", "rejected") and filled_qty > 0
        ):
            self._handle_scalp_exit_filled(sym, pos, pending, filled_qty)
        elif status in ("canceled", "expired", "rejected"):
            # Order died with no fill — clear pending and restore SL protection.
            log.warning(
                f"[CRYPTO][SCALP] exit order {status} with no fill for {sym} — restoring protection"
            )
            pos.pending_exit = None
            if pos.sl_order_id is None:
                qty = self._broker_qty(sym)
                pos.sl_order_id = self._place_sl_order(sym, qty if qty else pos.qty, pos.sl_price)
            self._save_scalp_state()
        # anything else (new/accepted/partially_filled/…) — still working, wait

    def _handle_scalp_exit_filled(self, sym: str, pos: CryptoPosition, pending: dict, filled_qty: float) -> None:
        """Apply a confirmed (possibly partially) filled scalp exit."""
        from engine import config as _cfg
        kind       = pending.get("kind")
        broker_qty = self._broker_qty(sym)
        min_split  = float(_cfg.CRYPTO_SCALP_MIN_SPLIT_QTY)

        if kind == "scale_out":
            pos.scaled_out   = True
            pos.pending_exit = None
            remaining = broker_qty if broker_qty is not None else max(0.0, pos.qty - filled_qty)
            pos.qty = remaining
            if remaining > min_split:
                # Replace protection: fresh SL for the ACTUAL remaining quantity.
                if pos.sl_order_id:
                    self._cancel_sl_order(sym, pos.sl_order_id)
                pos.sl_order_id = self._place_sl_order(sym, remaining, pos.sl_price)
                log.info(
                    f"[CRYPTO][SCALP] {sym} scale-out filled {filled_qty:.8f} — "
                    f"remaining {remaining:.8f}, SL replaced | order={pending.get('order_id')}"
                )
                self._save_scalp_state()
            else:
                log.info(f"[CRYPTO][SCALP] {sym} scale-out left no meaningful remainder — flat")
                self._drop_position(sym)
        else:  # full close
            if broker_qty is None:
                return  # cannot confirm flat — keep pending, retry next poll
            if broker_qty <= min_split:
                log.info(f"[CRYPTO][SCALP] CLOSED {sym} | entry={pos.entry_price:.4f}")
                self._drop_position(sym)
            else:
                # Remainder still live — clear pending so the monitor
                # re-evaluates the actual remaining quantity.
                pos.qty = broker_qty
                pos.pending_exit = None
                self._save_scalp_state()

    def _broker_qty(self, symbol: str) -> Optional[float]:
        """Actual broker quantity for *symbol*: 0.0 when flat, None on fetch failure."""
        try:
            alpaca_sym = self._alpaca_sym(symbol)
            for p in self._client.get_all_positions():
                if str(getattr(p, "symbol", "")).upper() == alpaca_sym.upper():
                    return float(p.qty)
            return 0.0
        except Exception as e:
            log.debug(f"[CRYPTO][SCALP] broker qty fetch failed for {symbol}: {e}")
            return None

    def _drop_position(self, symbol: str) -> None:
        """Remove local + persisted state for a confirmed-flat position."""
        self._positions.pop(symbol, None)
        self._save_scalp_state()

    # ── Scalp persistence (restart-safe metadata, no secrets) ────────────────

    def _load_scalp_state(self) -> dict:
        try:
            return json.loads(_SCALP_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_scalp_state(self) -> None:
        """Persist scalp-only position metadata so a restart does not silently
        downgrade an open scalp to the baseline lane. Baseline positions are
        intentionally not persisted (they rebuild generically, as before)."""
        data = {}
        for sym, pos in self._positions.items():
            if pos.strategy != "momentum_scalp":
                continue
            data[sym] = {
                "strategy":     pos.strategy,
                "entry_price":  pos.entry_price,
                "entry_time":   pos.entry_time.isoformat() if pos.entry_time else None,
                "tp_price":     pos.tp_price,
                "sl_price":     pos.sl_price,
                "peak_price":   pos.peak_price,
                "scaled_out":   pos.scaled_out,
                "pending_exit": pos.pending_exit,
            }
        try:
            if data:
                _SCALP_STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
            elif _SCALP_STATE_PATH.exists():
                _SCALP_STATE_PATH.unlink()
        except OSError as e:
            log.debug(f"[CRYPTO][SCALP] state save failed: {e}")

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _get_data_client(self):
        """Return a cached CryptoHistoricalDataClient, creating it on first use."""
        if self._data_client is None:
            from alpaca.data.historical import CryptoHistoricalDataClient
            self._data_client = CryptoHistoricalDataClient(
                api_key=self._api_key or None,
                secret_key=self._api_secret or None,
            )
        return self._data_client

    def _get_latest_quote(self, symbol: str) -> Optional[Tuple[float, float]]:
        """Return (bid, ask) for *symbol*, or None on failure. Either side may be 0."""
        try:
            from alpaca.data.requests import CryptoLatestQuoteRequest

            client     = self._get_data_client()
            alpaca_sym = self._alpaca_sym(symbol)
            req    = CryptoLatestQuoteRequest(symbol_or_symbols=alpaca_sym)
            quotes = client.get_crypto_latest_quote(req)
            quote  = quotes.get(alpaca_sym) or quotes.get(symbol)
            if quote is None:
                return None
            ask = float(getattr(quote, "ask_price", 0) or 0)
            bid = float(getattr(quote, "bid_price", 0) or 0)
            return bid, ask
        except Exception as e:
            log.debug(f"[CRYPTO] Quote fetch failed {symbol}: {e}")
            return None

    def _get_latest_price(self, symbol: str) -> Optional[float]:
        """Fetch the latest mid price for a crypto pair."""
        q = self._get_latest_quote(symbol)
        if q is None:
            return None
        bid, ask = q
        return (ask + bid) / 2 if (ask > 0 and bid > 0) else (ask or bid or None)

    def status_summary(self) -> str:
        n = len(self._positions)
        if n == 0:
            return "[CRYPTO] No open positions"
        parts = [f"{s}({p.entry_price:.4f}→TP:{p.tp_price:.4f})" for s, p in self._positions.items()]
        return f"[CRYPTO] {n} open: {', '.join(parts)}"


# ── Symbol normalisation helper ───────────────────────────────────────────────

# Map of Alpaca's slash-free internal symbol names → canonical "BASE/USD" form.
_CRYPTO_SYM_MAP: dict = {
    # ── Majors ──────────────────────────────────────────────────
    "BTCUSD":    "BTC/USD",    "ETHUSD":    "ETH/USD",
    # ── Layer-1 Ecosystems ───────────────────────────────────────
    "SOLUSD":    "SOL/USD",    "ADAUSD":    "ADA/USD",
    "AVAXUSD":   "AVAX/USD",   "DOTUSD":    "DOT/USD",
    "LINKUSD":   "LINK/USD",   "LTCUSD":    "LTC/USD",
    "BCHUSD":    "BCH/USD",    "XTZUSD":    "XTZ/USD",
    "XRPUSD":    "XRP/USD",    "POLUSD":    "POL/USD",
    "MATICUSD":  "POL/USD",    # legacy alias → POL
    # ── DeFi Infrastructure ──────────────────────────────────────
    "RENDERUSD": "RENDER/USD", "FILUSD":    "FIL/USD",
    "GRTUSD":    "GRT/USD",    "ARBUSD":    "ARB/USD",
    "LDOUSD":    "LDO/USD",
    # ── DeFi / DEX tokens ────────────────────────────────────────
    "AAVEUSD":   "AAVE/USD",   "UNIUSD":    "UNI/USD",
    "SUSHIUSD":  "SUSHI/USD",  "YFIUSD":    "YFI/USD",
    "CRVUSD":    "CRV/USD",    "ONDOUSD":   "ONDO/USD",
    "HYPEUSD":   "HYPE/USD",   "SKYUSD":    "SKY/USD",
    # ── Engagement ───────────────────────────────────────────────
    "BATUSD":    "BAT/USD",
    # ── Gold-backed ──────────────────────────────────────────────
    "PAXGUSD":   "PAXG/USD",
    # ── Community / Meme ─────────────────────────────────────────
    "DOGEUSD":   "DOGE/USD",   "SHIBUSD":   "SHIB/USD",
    "BONKUSD":   "BONK/USD",   "PEPEUSD":   "PEPE/USD",
    "WIFUSD":    "WIF/USD",    "TRUMPUSD":  "TRUMP/USD",
}


def _normalize_symbol(sym: str) -> str:
    """Convert 'BTCUSD' → 'BTC/USD' for known crypto pairs."""
    return _CRYPTO_SYM_MAP.get(sym.upper(), sym)
