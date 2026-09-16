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
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pytz

log = logging.getLogger("ApexTrader")

_ET = pytz.timezone("America/New_York")

# ── Signal dataclass ──────────────────────────────────────────────────────────

@dataclass
class CryptoSignal:
    symbol:     str   # e.g. "BTC/USD"
    action:     str   # "buy" or "sell"
    price:      float
    confidence: float
    reason:     str
    rsi:        float


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
    client=None,
    api_key: str = "",
    api_secret: str = "",
) -> Optional[pd.DataFrame]:
    """Fetch OHLCV bars for a crypto pair via Alpaca.

    Pass an already-constructed *client* (CryptoHistoricalDataClient) to avoid
    creating a new one on every call.
    """
    try:
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        if client is None:
            from alpaca.data.historical import CryptoHistoricalDataClient
            client = CryptoHistoricalDataClient(api_key=api_key or None, secret_key=api_secret or None)

        tf = TimeFrame(timeframe_hours, TimeFrameUnit.Hour)
        end = datetime.datetime.now(pytz.utc)
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
                sl_price = round(entry * (1 - _cfg.CRYPTO_SL_PCT / 100), 8)
                # Check for an existing open SL order before placing a new one (avoids
                # "available: 0" errors when restarting with existing positions + SL orders)
                existing_sl = self._find_existing_sl_order(sym_norm)
                sl_order_id = existing_sl or self._place_sl_order(sym_norm, qty, sl_price)
                self._positions[sym_norm] = CryptoPosition(
                    symbol=sym_norm,
                    entry_price=entry,
                    entry_time=datetime.datetime.now(_ET),
                    qty=qty,
                    notional=notional,
                    tp_price=round(entry * (1 + _cfg.CRYPTO_TP_PCT / 100), 8),
                    sl_price=sl_price,
                    peak_price=entry,
                    sl_order_id=sl_order_id,
                )
            else:
                # Position already tracked — place broker SL if not yet done
                tracked = self._positions[sym_norm]
                if tracked.sl_order_id is None:
                    tracked.sl_order_id = self._place_sl_order(sym_norm, qty, tracked.sl_price)
                # Refresh qty from broker (may differ from estimated qty)
                tracked.qty = qty

        # Remove stale local entries
        stale = [s for s in list(self._positions) if s not in live_symbols]
        for s in stale:
            log.info(f"[CRYPTO] Position closed externally: {s}")
            self._positions.pop(s, None)

    # ── Monitor open positions for TP / SL ───────────────────────────────────

    def monitor_positions(self) -> None:
        """Check TP/SL for every open crypto position. Close when hit."""
        self._sync_positions()

        for sym, pos in list(self._positions.items()):
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
        for sym in symbols:
            if sym in self._positions:
                continue  # already holding, skip new entry
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
            order_req   = StopLimitOrderRequest(
                symbol=alpaca_sym,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=sl_price,
                limit_price=limit_price,
            )
            order = self._client.submit_order(order_req)
            log.info(
                f"[CRYPTO] BROKER SL placed for {symbol} stop={sl_price:.4f} "
                f"limit={limit_price:.4f} qty={qty:.6f} | order={order.id}"
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

    def _get_latest_price(self, symbol: str) -> Optional[float]:
        """Fetch the latest price for a crypto pair."""
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
            return (ask + bid) / 2 if (ask > 0 and bid > 0) else (ask or bid or None)
        except Exception as e:
            log.debug(f"[CRYPTO] Price fetch failed {symbol}: {e}")
            return None

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
