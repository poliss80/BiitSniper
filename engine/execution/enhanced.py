"""
ApexTrader - Enhanced Executor
Optimized trade executor with consolidated logic:
  - Reduced API calls through caching
  - Unified buy/short entry paths
  - Bracket orders with tiered SL/TP
  - PDT compliance
  - Market-data-only pricing (Alpaca snapshot)
  - Real-time bid/ask validation before order submission
"""

import datetime
import json
import logging
import re
import threading
import time
from typing import Optional, Dict, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import pandas as pd

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    StopOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
    ReplaceOrderRequest,
    TrailingStopOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.trading.enums import OrderType as AlpacaOrderType

from engine.config import (
    PDT_ACCOUNT_MIN, PDT_MAX_TRADES,
    MAX_POSITIONS,
    SWAP_ON_FULL,
    SWAP_MIN_CONFIDENCE,
    EXTENDED_HOURS,
    USE_DYNAMIC_TIERS,
    USE_RISK_EQUALIZED_SIZING,
    USE_VIX_ROC_FILTER,
    MIN_BUYING_POWER_PCT, MIN_POSITION_DOLLARS, PDT_WARN_AT_REMAINING,
    TAKE_PROFIT_NORMAL, TAKE_PROFIT_HIGH, STOP_LOSS_PCT,
    ATR_TP_RATIO, MAX_SHORT_FLOAT_PCT, HIGH_SHORT_FLOAT_STOCKS, is_high_short_float,
    EOD_CLOSE_ENABLED, EOD_CLOSE_ALL, EOD_CLOSE_TIME, EOD_AFTERHOURS_LIMIT_BUFFER_PCT,
    MARGIN_EOD_FORCE_CLOSE, EOD_CLOSE_STRATEGIES,
    LONG_ONLY_MODE,
    STALE_ORDER_MINUTES, STALE_ORDER_MINUTES_INTRADAY,
    KILL_MODE_TRAIL_PCT,
    SMALL_ACCOUNT_EQUITY_THRESHOLD, SMALL_ACCOUNT_MAX_POSITIONS,
    SMALL_ACCOUNT_MIN_POSITION_DOLLARS,
    POSITION_SIZE_PCT, SMALL_ACCOUNT_POSITION_SIZE_PCT,
    LIVE_PROBE_MODE, LIVE_PROBE_SHARES, LIVE_PROBE_MAX_ENTRIES_PER_DAY,
    LIVE_PROBE_SCALE_IN_ENABLED,
    LIVE_PROBE_SCALE_IN_MIN_GAIN_PCT, LIVE_PROBE_SCALE_IN_BUYING_POWER_PCT,
    LIVE_PROBE_SCALE_IN_MAX_MULTIPLE, LIVE_PROBE_SCALE_IN_MIN_HOLD_MINUTES,
    LIVE_PROBE_SCALE_IN_REQUIRE_VWAP, LIVE_PROBE_SCALE_IN_REQUIRE_NEW_HIGH,
    LIVE_PROBE_SCALE_IN_MAX_TOTAL_RISK_PCT,
    LIVE_PROBE_MAX_TOTAL_BUYING_POWER_PCT,
    LIVE_PROBE_SCALE_IN_ATM_OPTION_ENABLED,
    LONG_TERM_HOLD_STRATEGIES, LONG_TERM_HOLD_TICKERS,
    LIVE_PROBE_SCALE_IN_ORDER_TTL_CYCLES,
    LIVE_PROBE_SCALE_IN_ATM_OPTION_MAX_ATTEMPTS,
    LIVE_PROBE_SCALE_IN_ATM_OPTION_MAX_MINUTES,
    INTRADAY_MOMENTUM_EXEMPTIONS, INTRADAY_MOMENTUM_MIN_GAIN_PCT,
    INTRADAY_MOMENTUM_MIN_RVOL, INTRADAY_MOMENTUM_MIN_5M_RETURN_PCT,
    CONF_SCALE_MIN_MULT, CONF_SCALE_FULL_CONF,
    MARGIN_LEVERAGE,
    API_KEY, API_SECRET,
    SCALEOUT_TRAIL_PCT, PROTECT_POSITIONS_ENABLED,
    TP_INTERMEDIATE_PCT, TP_INTERMEDIATE_TRAIL_PCT, TP_FINAL_PCT,
    TP_RATCHET_ENABLED, TP_RATCHET_ARM_PCT, TP_RATCHET_GIVEBACK_PCT,
    DEAD_MONEY_MINUTES, DEAD_MONEY_MAX_ADVERSE_DRIFT_PCT,
    TIME_LOSS_ATR_MULTIPLIER, TIME_LOSS_ATR_MIN_PCT, TIME_LOSS_ATR_MAX_PCT,
    ATR_STOP_MULTIPLIER, SQUEEZE_TP1_PCT, SQUEEZE_TP2_PCT, classify_ti_profile,
    ORB,
    LIVE,
)
from engine.equity.strategies import Signal
from engine.utils import MarketState, calculate_risk_adjusted_size, check_vix_roc_filter, get_bars, get_dynamic_tier
from engine.notifications.notifications import send_email

log = logging.getLogger("ApexTrader")


def _is_inactive_asset_error(error: Exception) -> bool:
    """Return whether Alpaca rejected an order because the asset is inactive."""
    message = str(error).lower()
    return "40010001" in message or ("asset" in message and "not active" in message)


# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Helpers
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
class OrderType(Enum):
    LONG  = "long"
    SHORT = "short"


@dataclass
class PDTTracker:
    """Pattern Day Trader tracking — syncs with live Alpaca daytrade_count."""
    trades: list = field(default_factory=list)

    def add(self, date: datetime.date) -> None:
        self.trades.append(date)
        cutoff = date - datetime.timedelta(days=7)
        self.trades = [d for d in self.trades if d > cutoff]

    def remaining(self, equity: float, live_count: int, pdt_flagged: bool = False) -> int:
        """Returns day trades remaining. 999 = exempt if account is PDT-exempt or equity >= $25k."""
        if equity >= PDT_ACCOUNT_MIN or not pdt_flagged:
            return 999
        used = max(live_count, len(self.trades))
        return max(0, PDT_MAX_TRADES - used)

    def can_trade(self, equity: float, live_count: int = 0, pdt_flagged: bool = False) -> bool:
        return self.remaining(equity, live_count, pdt_flagged) > 0


@dataclass
class PositionInfo:
    """Cached snapshot of open positions."""
    positions_dict: Dict[str, any]
    total_count:    int

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions_dict

    def is_long(self, symbol: str) -> bool:
        return self.has_position(symbol) and float(self.positions_dict[symbol].qty) > 0

    def is_short(self, symbol: str) -> bool:
        return self.has_position(symbol) and float(self.positions_dict[symbol].qty) < 0


@dataclass
class AccountSnapshot:
    """Cached Alpaca account state — equity, buying power, live PDT count."""
    equity:              float
    buying_power:        float
    daytrade_count:      int
    pattern_day_trader:  bool = False
    timestamp:           float = field(default=0.0)


# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Executor
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
class EnhancedExecutor:
    """Optimized trade executor with consolidated long/short logic."""

    def __init__(self, client: TradingClient, use_bracket_orders: bool = True):
        self.client              = client
        self.use_bracket_orders  = use_bracket_orders
        self.pdt                 = PDTTracker()
        self.order_cache:  Dict[str, str] = {}
        self._position_cache: Optional[PositionInfo]    = None
        self._cache_timestamp: float = 0
        self._cache_ttl:       float = 5.0
        self._account_cache:  Optional[AccountSnapshot] = None
        self._account_ttl:    float = 2.0   # tight TTL — buying power must be fresh between orders
        self._htb_cache:      set   = set()   # hard-to-borrow symbols — skip shorts this session
        self._halted_symbols: set   = set()   # broker-reported halts — skip entries for this session
        self._entry_log:   Dict[str, dict] = {}  # {symbol: {"strategy": str, "date": date}}
        self._swap_cycle_closed: set = set()     # positions already swapped this scan cycle
        self._tp_targets:          Dict[str, float] = {}  # {symbol: final close price (+10%)}
        self._intermediate_targets: Dict[str, float] = {}  # {symbol: tighten-trail price (+5%)}
        self._tightened:            set  = set()           # symbols whose trail has been tightened
        self._peak_price:          Dict[str, float] = {}  # {symbol: high-water mark for ratchet exit}
        self._scaled_out:           set  = set()           # legacy; kept for compatibility
        self.shorting_blocked: bool = False  # set true when broker rejects all short attempts for account
        self._pdt_stop_blocked: Dict[str, float] = {}  # {symbol: stop_price} — broker-rejected stops; monitored in software
        self._pdt_overnight_forced: set = set()  # symbols where PDT also blocks close — forced overnight, no retries
        self._pdt_violation_alerted: bool = False  # tracks whether the PDT violation email has been sent this session
        self._eod_close_done: object = None  # date of last completed EOD close (prevents duplicate runs)
        self.market_state: Optional[MarketState] = None
        self._options_cost_reserve: float = 0.0  # $ currently deployed in options — set by orchestrator each cycle
        self._exit_state_path = Path(__file__).resolve().parent.parent / ".position_exit_state.json"
        self._exit_state_lock = threading.Lock()
        self._probe_journal_path = Path(__file__).resolve().parent.parent / ".probe_journal.jsonl"
        self._probe_journal_lock = threading.Lock()
        self._submitted_entry_orders: Dict[str, object] = {}
        self._live_probe_count_date: Optional[datetime.date] = None
        self._live_probe_entries_today = 0
        self._live_probe_scaled_in: set = set()
        self._live_probe_scale_in_pending: Dict[str, dict] = {}
        self._restore_exit_state()
        self._rebuild_entry_log_from_orders()
        self._restore_live_probe_count()

    def update_market_state(self, market_state: MarketState) -> None:
        """Store the active market snapshot for per-cycle execution decisions."""
        self.market_state = market_state

    def set_options_cost_reserve(self, cost: float) -> None:
        """Called by the orchestrator each cycle with the current options capital deployed.

        The equity executor deducts this from available buying power so equity trades
        cannot double-spend the capital already committed to open options positions.
        """
        self._options_cost_reserve = max(0.0, cost)

    def _save_exit_state(self) -> None:
        """Persist entry and target data needed to resume exit management after restart."""
        entries = {}
        for sym, info in self._entry_log.items():
            entry_time = info.get("entry_time")
            entry_price = float(info.get("entry_price") or 0)
            if not isinstance(entry_time, datetime.datetime) or entry_price <= 0:
                continue
            entries[sym] = {
                "strategy": info.get("strategy", "restored"),
                "date": str(info.get("date") or entry_time.date()),
                "confidence": float(info.get("confidence", 0) or 0),
                "entry_time": entry_time.isoformat(),
                "entry_price": entry_price,
                "atr_stop": info.get("atr_stop"),
                "regime_at_entry": info.get("regime_at_entry", "unknown"),
                "long_term_hold": bool(info.get("long_term_hold", False)),
                "ti_profile": info.get("ti_profile", ""),
                "scaled_in": sym in self._live_probe_scaled_in,
                "intermediate_target": self._intermediate_targets.get(sym),
                "final_target": self._tp_targets.get(sym),
                "tightened": sym in self._tightened,
                "peak_price": self._peak_price.get(sym),
            }

        try:
            with self._exit_state_lock:
                pending_scale_ins = {
                    sym: pending
                    for sym, pending in self._live_probe_scale_in_pending.items()
                    if sym in entries
                }
                if not entries and not pending_scale_ins:
                    self._exit_state_path.unlink(missing_ok=True)
                    return
                temp_path = self._exit_state_path.with_suffix(".tmp")
                temp_path.write_text(
                    json.dumps({"entries": entries, "live_probe_scale_in_pending": pending_scale_ins}),
                    encoding="utf-8",
                )
                temp_path.replace(self._exit_state_path)
        except Exception as e:
            log.warning(f"Could not save position exit state: {e}")

    def _restore_exit_state(self) -> None:
        """Restore exit state for positions that are still open at process startup."""
        try:
            if not self._exit_state_path.exists():
                return
            saved_state = json.loads(self._exit_state_path.read_text(encoding="utf-8"))
            raw_entries = saved_state.get("entries", {})
            open_symbols = {p.symbol for p in self.client.get_all_positions()}
            for sym, saved in raw_entries.items():
                if sym not in open_symbols:
                    continue
                entry_time = datetime.datetime.fromisoformat(saved["entry_time"])
                self._entry_log[sym] = {
                    "strategy": saved.get("strategy", "restored"),
                    "date": datetime.date.fromisoformat(saved.get("date", str(entry_time.date()))),
                    "confidence": float(saved.get("confidence", 0) or 0),
                    "entry_time": entry_time,
                    "entry_price": float(saved["entry_price"]),
                    "atr_stop": float(saved.get("atr_stop") or 0),
                    "regime_at_entry": saved.get("regime_at_entry", "unknown"),
                    "long_term_hold": bool(saved.get("long_term_hold", False)),
                    "ti_profile": saved.get("ti_profile", ""),
                }
                if saved.get("intermediate_target") is not None:
                    self._intermediate_targets[sym] = float(saved["intermediate_target"])
                if saved.get("final_target") is not None:
                    self._tp_targets[sym] = float(saved["final_target"])
                if saved.get("tightened"):
                    self._tightened.add(sym)
                if saved.get("peak_price") is not None:
                    self._peak_price[sym] = float(saved["peak_price"])
                if saved.get("scaled_in"):
                    self._live_probe_scaled_in.add(sym)
            self._live_probe_scale_in_pending = {
                sym: pending
                for sym, pending in saved_state.get("live_probe_scale_in_pending", {}).items()
                if sym in self._entry_log
            }
            self._save_exit_state()
            if self._entry_log:
                log.info(f"Restored position exit state: {', '.join(self._entry_log)}")
        except Exception as e:
            log.warning(f"Could not restore position exit state: {e}")

    def _record_entry(self, signal: Signal, fallback_price: float) -> None:
        """Track an entry using the broker-reported fill price when immediately available."""
        submitted_orders = getattr(self, "_submitted_entry_orders", {})
        order = submitted_orders.pop(signal.symbol, None) if isinstance(submitted_orders, dict) else None
        entry_price = float(getattr(order, "filled_avg_price", 0) or 0)
        if entry_price <= 0:
            entry_price = fallback_price
        entry_log = getattr(self, "_entry_log", None)
        if entry_log is None:
            entry_log = {}
            self._entry_log = entry_log
        try:
            regime_at_entry = "bull" if self._current_market_state().resolve_regime() else "bear"
        except Exception:
            regime_at_entry = "unknown"
        entry_log[signal.symbol] = {
            "strategy": signal.strategy,
            "date": datetime.date.today(),
            "confidence": signal.confidence,
            "entry_time": datetime.datetime.now(),
            "entry_price": entry_price,
            "atr_stop": float(getattr(signal, "atr_stop", 0) or 0),
            "regime_at_entry": regime_at_entry,
            "long_term_hold": (
                signal.symbol.upper() in LONG_TERM_HOLD_TICKERS
                or signal.strategy in LONG_TERM_HOLD_STRATEGIES
            ),
            "ti_profile": classify_ti_profile(signal.symbol, signal.strategy),
        }
        if hasattr(self, "_save_exit_state"):
            self._save_exit_state()

    def _mark_live_probe_scaled_in(self, sym: str) -> None:
        """Count only realized scale-ins as actual live-probe entries."""
        if sym in self._live_probe_scaled_in:
            return
        self._live_probe_scaled_in.add(sym)
        if LIVE_PROBE_MODE:
            today = datetime.date.today()
            if getattr(self, "_live_probe_count_date", None) != today:
                self._live_probe_count_date = today
            self._live_probe_entries_today = int(getattr(self, "_live_probe_entries_today", 0)) + 1

    @staticmethod
    def _is_option_market_open(market_state: MarketState) -> bool:
        """Return whether regular-session single-stock option orders are permitted."""
        return bool(getattr(market_state, "is_regular_hours", False))

    @staticmethod
    def _entry_age_minutes(entry_time, market_state: MarketState) -> Optional[float]:
        if entry_time is None:
            return None
        now = getattr(market_state, "now", datetime.datetime.now())
        try:
            if getattr(entry_time, "tzinfo", None) is not None and getattr(now, "tzinfo", None) is None:
                now = now.replace(tzinfo=entry_time.tzinfo)
            elif getattr(entry_time, "tzinfo", None) is None and getattr(now, "tzinfo", None) is not None:
                entry_time = entry_time.replace(tzinfo=now.tzinfo)
            return max(0.0, (now - entry_time).total_seconds() / 60)
        except Exception:
            return None

    def _live_probe_scale_in_confirmation_ok(self, symbol: str, entry_time, current_price: float, bars_cache=None) -> tuple[bool, str | None]:
        if not (LIVE_PROBE_SCALE_IN_REQUIRE_VWAP or LIVE_PROBE_SCALE_IN_REQUIRE_NEW_HIGH):
            return True, None
        try:
            if bars_cache is not None and symbol in bars_cache:
                bars = bars_cache[symbol]
            else:
                bars = get_bars(symbol, "1d", "1m")
                if bars_cache is not None:
                    bars_cache[symbol] = bars
            if bars.empty or len(bars) < 6:
                return False, "missing intraday bars for VWAP/new-high confirmation"
            bars = bars.copy()
            entry_timestamp = self._normalize_confirmation_timestamp(entry_time)
            if "time" in bars.columns:
                bar_times = pd.to_datetime(bars["time"])
                if entry_timestamp is not None:
                    if getattr(bar_times.dt, "tz", None) is not None:
                        bar_times = bar_times.dt.tz_convert("UTC")
                    else:
                        bar_times = bar_times.dt.tz_localize("UTC")
                    bars = bars[bar_times >= entry_timestamp]
            elif entry_time is not None and hasattr(bars.index, "to_series"):
                index_times = pd.to_datetime(bars.index.to_series())
                if entry_timestamp is not None:
                    if getattr(index_times.dt, "tz", None) is not None:
                        index_times = index_times.dt.tz_convert("UTC")
                    else:
                        index_times = index_times.dt.tz_localize("UTC")
                    bars = bars[index_times >= entry_timestamp]
            if len(bars) < 6:
                return False, "not enough post-entry bars for VWAP/new-high confirmation"

            typical_price = (bars["high"] + bars["low"] + bars["close"]) / 3
            cumulative_volume = bars["volume"].cumsum()
            vwap = (typical_price * bars["volume"]).cumsum() / cumulative_volume.replace(0, float("nan"))
            last_close = float(bars["close"].iloc[-1])
            last_high = float(bars["high"].iloc[-1])
            if LIVE_PROBE_SCALE_IN_REQUIRE_VWAP and last_close < float(vwap.iloc[-1]):
                return False, f"price {last_close:.2f} below VWAP {float(vwap.iloc[-1]):.2f}"
            if LIVE_PROBE_SCALE_IN_REQUIRE_NEW_HIGH:
                prior_high = float(bars["high"].iloc[:-1].max())
                if last_high <= prior_high and current_price <= prior_high:
                    return False, f"no new post-entry high above {prior_high:.2f}"
            return True, None
        except Exception as confirmation_error:
            return False, f"confirmation data error: {confirmation_error}"

    @staticmethod
    def _normalize_confirmation_timestamp(value):
        """Return a UTC-aware timestamp for safe comparisons with market bars."""
        if value is None:
            return None
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return timestamp

    def _record_probe_outcome(self, sym: str, pos, exit_reason: str) -> None:
        """Append a secret-free mark-to-market outcome for a bot-managed live probe exit."""
        if not LIVE_PROBE_MODE:
            return
        info = self._entry_log.get(sym)
        if not info:
            return
        entry_price = float(info.get("entry_price") or 0)
        exit_price = float(getattr(pos, "current_price", 0) or 0)
        qty = int(float(getattr(pos, "qty", 0) or 0))
        if entry_price <= 0 or exit_price <= 0 or qty == 0:
            return
        direction = "long" if qty > 0 else "short"
        pnl_pct = ((exit_price - entry_price) / entry_price * 100) * (1 if qty > 0 else -1)
        payload = {
            "timestamp": datetime.datetime.now().isoformat(),
            "symbol": sym,
            "strategy": info.get("strategy", "unknown"),
            "regime_at_entry": info.get("regime_at_entry", "unknown"),
            "direction": direction,
            "entry_time": info.get("entry_time").isoformat() if isinstance(info.get("entry_time"), datetime.datetime) else None,
            "entry_price": entry_price,
            "exit_mark_price": exit_price,
            "quantity": abs(qty),
            "estimated_pnl_pct": round(pnl_pct, 4),
            "scaled_in": sym in self._live_probe_scaled_in,
            "exit_reason": exit_reason,
        }
        try:
            with self._probe_journal_lock:
                with self._probe_journal_path.open("a", encoding="utf-8") as journal:
                    journal.write(json.dumps(payload) + "\n")
        except Exception as e:
            log.warning(f"Could not record probe outcome for {sym}: {e}")

    def _restore_live_probe_count(self) -> None:
        """Recount today's probe entries so the live cap survives watchdog restarts."""
        if not LIVE_PROBE_MODE:
            return
        today = datetime.date.today()
        try:
            import pytz
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus

            today_start = datetime.datetime.combine(today, datetime.time.min).replace(tzinfo=pytz.UTC)
            orders = self.client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.ALL, after=today_start))
            self._live_probe_entries_today = sum(
                1 for order in orders
                if str(getattr(order, "client_order_id", "")).startswith("apex-probe-scale-")
                and str(getattr(order, "side", "")).lower() in ("buy", "sell")
            )
            self._live_probe_count_date = today
            log.info(
                f"LIVE PROBE daily count restored: {self._live_probe_entries_today}/"
                f"{LIVE_PROBE_MAX_ENTRIES_PER_DAY}"
            )
        except Exception as e:
            self._live_probe_count_date = None
            log.warning(f"Could not restore live probe daily count: {e}")

    def _can_submit_live_probe(self) -> bool:
        """Return whether the live probe daily cap permits another entry."""
        if not LIVE_PROBE_MODE:
            return True
        if self._live_probe_count_date != datetime.date.today():
            self._restore_live_probe_count()
        if self._live_probe_count_date != datetime.date.today():
            log.warning("LIVE PROBE skipped: unable to verify today's entry count")
            return False
        if self._live_probe_entries_today >= LIVE_PROBE_MAX_ENTRIES_PER_DAY:
            log.info(
                f"LIVE PROBE skipped: daily cap reached "
                f"({self._live_probe_entries_today}/{LIVE_PROBE_MAX_ENTRIES_PER_DAY})"
            )
            return False
        return True

    def _resolve_scale_in_order(self, symbol: str, current_price: float, side: OrderSide, market_state: MarketState) -> tuple[str, float, str | None]:
        """Return (order_kind, price, reason) for the current probe scale-in window."""
        now = getattr(market_state, "now", None)
        if now is not None and hasattr(now, "hour"):
            is_post_market = now.weekday() < 5 and now.hour >= 16
        else:
            is_post_market = False
        if is_post_market:
            limit_price, reason = self._after_hours_limit_price(symbol, current_price, side)
            return "limit", limit_price, reason
        if getattr(market_state, "is_regular_hours", False):
            return "market", current_price, None
        limit_price, reason = self._after_hours_limit_price(symbol, current_price, side)
        return "limit", limit_price, reason

    def _place_live_probe_atm_option(self, symbol: str, spot: float, market_state: MarketState, options_executor) -> bool:
        """Submit one ATM call only after a qualifying long probe scale-in succeeds."""
        if not LIVE_PROBE_SCALE_IN_ATM_OPTION_ENABLED or options_executor is None:
            return False
        if not self._is_option_market_open(market_state):
            log.info(f"LIVE PROBE {symbol}: ATM option deferred; options market is closed")
            return False
        try:
            from engine.options.strategies import OptionSignal, _get_options_chain, _pick_strike, get_dynamic_option_filters

            chain = _get_options_chain(symbol)
            if chain is None:
                log.info(f"LIVE PROBE {symbol}: ATM option skipped; no usable option chain")
                return False
            strike_row = _pick_strike(chain.calls, spot, 0.50, get_dynamic_option_filters())
            if strike_row is None:
                log.info(f"LIVE PROBE {symbol}: ATM option skipped; no liquid ATM call")
                return False
            mid_price = float(strike_row.get("mid", strike_row.get("lastprice", 0)) or 0)
            if mid_price <= 0:
                log.info(f"LIVE PROBE {symbol}: ATM option skipped; no executable call price")
                return False
            option_signal = OptionSignal(
                symbol=symbol, option_type="call", action="buy_to_open",
                strike=float(strike_row["strike"]), expiry=chain.expiry,
                mid_price=mid_price, confidence=1.0,
                reason="Profitable live-probe equity scale-in; ATM call confirmation",
                strategy="LiveProbeATMScaleIn",
                iv_pct=float(strike_row.get("iv_pct", chain.hv_30)),
                iv_rank=chain.iv_rank, delta=float(strike_row.get("delta", 0.50)),
                open_interest=int(strike_row.get("openinterest", 0)),
                contract_cap=1, force_single_leg=True, bypass_portfolio_cap=True,
            )
            if options_executor.place_option_order(option_signal, market_state):
                log.info(f"LIVE PROBE ATM OPTION {symbol}: 1x ${option_signal.strike:.2f} call submitted")
                return True
        except Exception as option_error:
            log.warning(f"LIVE PROBE {symbol}: ATM option scale-in skipped: {option_error}")
        return False

    def check_live_probe_scale_ins(self, options_executor=None) -> None:
        """Add once to a profitable live probe after the strategy scan completes."""
        if not (LIVE_PROBE_MODE and LIVE_PROBE_SCALE_IN_ENABLED):
            return
        if not self._entry_log:
            return
        market_state = self._current_market_state()

        try:
            positions = {p.symbol: p for p in self.client.get_all_positions()}
            account = self._get_account(force_refresh=True)
        except Exception as e:
            log.warning(f"LIVE PROBE scale-in check failed: {e}")
            return

        state_changed = False
        is_bull = market_state.resolve_regime()
        pending_scale_ins = getattr(self, "_live_probe_scale_in_pending", {})
        confirmation_bars_cache = {}
        for sym, info in list(self._entry_log.items()):
            pos = positions.get(sym)
            entry_price = float(getattr(pos, "avg_entry_price", 0) or 0) if pos is not None else 0
            if entry_price <= 0 and pos is not None:
                entry_price = float(info.get("entry_price") or 0)
                if entry_price > 0:
                    log.debug(f"LIVE PROBE {sym}: using tracked entry price; broker average unavailable")
            if entry_price <= 0 or pos is None:
                log.info(f"LIVE PROBE {sym}: scale-in skipped; broker fill price unavailable")
                continue

            qty = int(float(getattr(pos, "qty", 0) or 0))
            current_price = float(getattr(pos, "current_price", 0) or 0)
            if qty == 0 or current_price <= 0:
                log.info(f"LIVE PROBE {sym}: scale-in skipped; position has no usable quantity/price")
                continue
            is_long = qty > 0
            pending_scale_in = pending_scale_ins.get(sym)
            if pending_scale_in is not None:
                if pending_scale_in.get("atm_option_deferred") or pending_scale_in.get("atm_option_pending"):
                    if self._is_option_market_open(market_state):
                        now_utc = datetime.datetime.now(datetime.timezone.utc)
                        started_at = pending_scale_in.get("atm_retry_started_at")
                        try:
                            retry_started = datetime.datetime.fromisoformat(started_at) if started_at else now_utc
                            if retry_started.tzinfo is None:
                                retry_started = retry_started.replace(tzinfo=datetime.timezone.utc)
                        except (TypeError, ValueError):
                            retry_started = now_utc
                        attempts = int(pending_scale_in.get("atm_attempt_count", 0) or 0)
                        retry_expired = (
                            attempts >= LIVE_PROBE_SCALE_IN_ATM_OPTION_MAX_ATTEMPTS
                            or (now_utc - retry_started).total_seconds() >= LIVE_PROBE_SCALE_IN_ATM_OPTION_MAX_MINUTES * 60
                        )
                        if retry_expired:
                            pending_scale_ins.pop(sym, None)
                            state_changed = True
                            log.info(f"LIVE PROBE {sym}: ATM option retry window expired")
                            continue
                        if self._place_live_probe_atm_option(sym, current_price, market_state, options_executor):
                            pending_scale_ins.pop(sym, None)
                            state_changed = True
                        else:
                            pending_scale_in["atm_option_pending"] = True
                            pending_scale_in["atm_option_deferred"] = False
                            pending_scale_in["atm_attempt_count"] = attempts + 1
                            pending_scale_in["atm_retry_started_at"] = retry_started.isoformat()
                            state_changed = True
                    continue
                order_id = str(pending_scale_in.get("order_id") or "")
                order_status = ""
                broker_filled_qty = 0
                if order_id:
                    try:
                        broker_order = self.client.get_order_by_id(order_id)
                        raw_status = getattr(broker_order, "status", "")
                        order_status = str(getattr(raw_status, "value", raw_status)).lower()
                        if "." in order_status:
                            order_status = order_status.rsplit(".", 1)[-1]
                        broker_filled_qty = int(float(getattr(broker_order, "filled_qty", 0) or 0))
                    except Exception as order_error:
                        log.debug(f"LIVE PROBE {sym}: unable to query scale-in order {order_id}: {order_error}")
                if order_status in {"canceled", "cancelled", "rejected", "expired"}:
                    pending_scale_ins.pop(sym, None)
                    state_changed = True
                    log.info(f"LIVE PROBE {sym}: scale-in order {order_status}; eligible to retry")
                    continue
                prior_qty = int(pending_scale_in["prior_qty"])
                effective_qty = max(abs(qty), broker_filled_qty)
                if effective_qty > prior_qty:
                    try:
                        if order_id:
                            try:
                                self.client.cancel_order_by_id(order_id)
                            except Exception as cancel_error:
                                log.debug(f"LIVE PROBE {sym}: scale-in order cleanup skipped: {cancel_error}")
                        # The original probe protection still reserves the old quantity.
                        # Cancel only stop/trailing protection orders, never unrelated symbol orders.
                        for open_order in self.client.get_orders() or []:
                            if str(getattr(open_order, "symbol", "")) != sym:
                                continue
                            open_id = str(getattr(open_order, "id", "") or "")
                            if not open_id or open_id == order_id:
                                continue
                            raw_type = getattr(open_order, "type", "")
                            open_type = str(getattr(raw_type, "value", raw_type)).lower()
                            if "." in open_type:
                                open_type = open_type.rsplit(".", 1)[-1]
                            if open_type in {"stop", "stop_limit", "trailing_stop"}:
                                try:
                                    self.client.cancel_order_by_id(open_id)
                                except Exception as cancel_error:
                                    log.debug(f"LIVE PROBE {sym}: protection cleanup skipped: {cancel_error}")
                        # Alpaca cancellation is asynchronous; wait until held quantity is released.
                        deadline = time.time() + 3.0
                        while time.time() < deadline:
                            active_protection = False
                            for open_order in self.client.get_orders() or []:
                                if str(getattr(open_order, "symbol", "")) != sym:
                                    continue
                                raw_type = getattr(open_order, "type", "")
                                open_type = str(getattr(raw_type, "value", raw_type)).lower()
                                if "." in open_type:
                                    open_type = open_type.rsplit(".", 1)[-1]
                                if open_type in {"stop", "stop_limit", "trailing_stop"}:
                                    active_protection = True
                                    break
                            if not active_protection:
                                break
                            time.sleep(0.2)
                        trail_pct = get_dynamic_tier(sym, current_price)["ts"]
                        self.client.submit_order(TrailingStopOrderRequest(
                            symbol=sym, qty=effective_qty,
                            side=OrderSide.SELL if is_long else OrderSide.BUY,
                            type=AlpacaOrderType.TRAILING_STOP,
                            time_in_force=TimeInForce.GTC,
                            trail_percent=trail_pct,
                        ))
                    except Exception as protection_error:
                        log.error(
                            f"LIVE PROBE {sym}: premarket scale-in filled but protection replacement "
                            f"failed: {protection_error}"
                        )
                        continue
                    self._mark_live_probe_scaled_in(sym)
                    if self._is_option_market_open(market_state):
                        if self._place_live_probe_atm_option(sym, current_price, market_state, options_executor):
                            pending_scale_ins.pop(sym, None)
                        else:
                            pending_scale_in["atm_option_pending"] = True
                            pending_scale_in["atm_attempt_count"] = 1
                            pending_scale_in["atm_retry_started_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    else:
                        pending_scale_in["atm_option_deferred"] = True
                        pending_scale_in["atm_attempt_count"] = 0
                        pending_scale_in["atm_retry_started_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    state_changed = True
                    log.info(f"LIVE PROBE {sym}: scale-in filled; protection replaced for {effective_qty} shares")
                    continue
                if "filled" in order_status:
                    log.info(f"LIVE PROBE {sym}: broker fill confirmed; awaiting position quantity refresh")
                    continue
                pending_scale_in["poll_count"] = int(pending_scale_in.get("poll_count", 0) or 0) + 1
                if pending_scale_in["poll_count"] >= LIVE_PROBE_SCALE_IN_ORDER_TTL_CYCLES:
                    cancellation_confirmed = not order_id
                    if order_id:
                        try:
                            self.client.cancel_order_by_id(order_id)
                            cancellation_confirmed = True
                        except Exception as cancel_error:
                            log.debug(f"LIVE PROBE {sym}: stale scale-in cancellation failed: {cancel_error}")
                    if cancellation_confirmed:
                        pending_scale_ins.pop(sym, None)
                        state_changed = True
                        log.info(
                            f"LIVE PROBE {sym}: stale scale-in order canceled after "
                            f"{LIVE_PROBE_SCALE_IN_ORDER_TTL_CYCLES} checks"
                        )
                    else:
                        pending_scale_in["poll_count"] = 0
                        state_changed = True
                        log.warning(f"LIVE PROBE {sym}: stale order retained; cancellation not confirmed")
                    continue
                log.info(f"LIVE PROBE {sym}: scale-in order still pending fill confirmation")
                continue

            if sym in self._live_probe_scaled_in:
                continue

            gain_pct = ((current_price - entry_price) / entry_price * 100) * (1 if is_long else -1)
            if gain_pct < LIVE_PROBE_SCALE_IN_MIN_GAIN_PCT:
                log.info(f"LIVE PROBE {sym}: scale-in skipped; gain {gain_pct:+.2f}% < {LIVE_PROBE_SCALE_IN_MIN_GAIN_PCT:.2f}%")
                continue
            if not (is_long and is_bull):
                log.info(f"LIVE PROBE {sym}: scale-in skipped; requires long position in bullish regime")
                continue
            entry_age_min = self._entry_age_minutes(info.get("entry_time"), market_state)
            if entry_age_min is None or entry_age_min < LIVE_PROBE_SCALE_IN_MIN_HOLD_MINUTES:
                log.info(
                    f"LIVE PROBE {sym}: scale-in skipped; held "
                    f"{entry_age_min if entry_age_min is not None else 0:.1f} min < "
                    f"{LIVE_PROBE_SCALE_IN_MIN_HOLD_MINUTES} min"
                )
                continue
            confirmed, confirm_reason = self._live_probe_scale_in_confirmation_ok(
                sym, info.get("entry_time"), current_price, confirmation_bars_cache
            )
            if not confirmed:
                log.info(f"LIVE PROBE {sym}: scale-in skipped; {confirm_reason}")
                continue

            add_cap = int(abs(qty) * max(0.0, LIVE_PROBE_SCALE_IN_MAX_MULTIPLE))
            if add_cap < 1:
                log.info(f"LIVE PROBE {sym}: scale-in skipped; max multiple cap allows no additional shares")
                continue

            atr_stop = float(info.get("atr_stop") or 0.0)
            if atr_stop > 0 and LIVE_PROBE_SCALE_IN_MAX_TOTAL_RISK_PCT > 0:
                account_equity = float(getattr(account, "equity", 0.0) or 0.0)
                max_risk_dollars = account_equity * LIVE_PROBE_SCALE_IN_MAX_TOTAL_RISK_PCT / 100
                risk_cap = int(max(0.0, max_risk_dollars / atr_stop) - abs(qty))
                add_cap = min(add_cap, risk_cap)
                if add_cap < 1:
                    log.info(
                        f"LIVE PROBE {sym}: scale-in skipped; total risk cap "
                        f"{LIVE_PROBE_SCALE_IN_MAX_TOTAL_RISK_PCT:.2f}% already reached"
                    )
                    continue

            margin = 1.0 if is_long else 2.0
            # Scale-in allocation is based on the broker-reported buying power.
            # Options reserve and prior symbols still remain separate accounting data;
            # this per-symbol budget intentionally uses the full available BP figure.
            available_bp = max(0.0, float(account.buying_power or 0.0))
            current_exposure = abs(qty) * current_price
            max_total_exposure = available_bp * LIVE_PROBE_MAX_TOTAL_BUYING_POWER_PCT / 100
            remaining_exposure = max(0.0, max_total_exposure - current_exposure)
            scale_budget = min(
                available_bp * LIVE_PROBE_SCALE_IN_BUYING_POWER_PCT / 100,
                remaining_exposure,
            )
            add_shares = min(
                int(scale_budget / (current_price * margin)),
                int(available_bp / (current_price * margin)),
                add_cap,
            )
            if add_shares < 1:
                log.info(f"LIVE PROBE {sym}: scale-in skipped; insufficient buying power")
                continue

            side = OrderSide.BUY if is_long else OrderSide.SELL
            order_kind, order_price, quote_reason = self._resolve_scale_in_order(sym, current_price, side, market_state)
            if quote_reason is not None:
                log.info(f"LIVE PROBE {sym}: scale-in skipped; {quote_reason}; no executable quote")
                continue

            try:
                client_order_id = f"apex-probe-scale-{sym}-{int(time.time())}"
                if order_kind == "limit":
                    order = self.client.submit_order(LimitOrderRequest(
                        symbol=sym, qty=add_shares, side=side,
                        time_in_force=TimeInForce.DAY,
                        limit_price=order_price, extended_hours=True,
                        client_order_id=client_order_id,
                    ))
                    pending_scale_ins[sym] = {
                        "prior_qty": abs(qty), "order_id": str(getattr(order, "id", "") or ""),
                        "submitted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "attempt_count": 1, "poll_count": 0,
                    }
                    log.info(
                        f"LIVE PROBE SCALE-IN LIMIT {sym}: +{add_shares} shares @ "
                        f"${order_price:.2f} (extended-hours)"
                    )
                else:
                    order = self.client.submit_order(MarketOrderRequest(
                        symbol=sym, qty=add_shares, side=side,
                        time_in_force=TimeInForce.DAY,
                        client_order_id=client_order_id,
                    ))
                    pending_scale_ins[sym] = {
                        "prior_qty": abs(qty), "order_id": str(getattr(order, "id", "") or ""),
                        "submitted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "attempt_count": 1, "poll_count": 0,
                    }
                    log.info(f"LIVE PROBE SCALE-IN {sym}: +{add_shares} shares at {gain_pct:+.2f}% pending fill confirmation")

                state_changed = True
            except Exception as scale_error:
                log.warning(f"LIVE PROBE {sym}: scale-in order failed: {scale_error}")
                continue

        if state_changed:
            self._save_exit_state()

    # -- Entry Log Rebuild (survive restarts) ----------------------------
    def _rebuild_entry_log_from_orders(self) -> None:
        """On startup, reconstruct today's entry log from Alpaca filled buy orders.
        Prevents swap-closes of same-day positions after a bot restart, which would
        trigger Alpaca PDT protection (error 40310100)."""
        try:
            today = datetime.date.today()
            import pytz
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            et       = pytz.timezone("America/New_York")
            # Filter to today only — avoids fetching the full account order history
            # on accounts with months of activity (can be thousands of orders).
            today_start = datetime.datetime.combine(today, datetime.time.min).replace(tzinfo=pytz.UTC)
            req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=today_start)
            filled_orders = self.client.get_orders(filter=req)
            for order in filled_orders:
                filled_at = getattr(order, "filled_at", None)
                if filled_at is None:
                    continue
                if hasattr(filled_at, "astimezone"):
                    order_date = filled_at.astimezone(et).date()
                else:
                    order_date = today  # conservative fallback
                if order_date != today:
                    continue
                side = str(getattr(order, "side", "")).lower()
                if side != "buy":
                    continue
                sym = order.symbol
                if sym not in self._entry_log:
                    self._entry_log[sym] = {
                        "strategy": "restored",
                        "date": today,
                        "confidence": 0.0,
                        "long_term_hold": sym.upper() in LONG_TERM_HOLD_TICKERS,
                    }
            if self._entry_log:
                log.info(
                    f"Entry log rebuilt from today's orders: "
                    f"{', '.join(self._entry_log.keys())}"
                )
        except Exception as e:
            log.warning(f"_rebuild_entry_log_from_orders failed (non-fatal): {e}")

    def _current_market_state(self) -> MarketState:
        if self.market_state is not None:
            return self.market_state
        raise RuntimeError("EnhancedExecutor requires market_state to be set before execution")

    def _after_hours_limit_price(self, symbol: str, signal_price: float, side: OrderSide) -> tuple[float, str | None]:
        """Return (limit_price, reason) using live quote-first execution rules.

        For pre/post-market limits, the current bid/ask is the real executable price.
        We intentionally skip stale or too-passive orders instead of submitting a
        limit that has almost no chance to fill.
        """
        fallback = round(signal_price * 1.002, 2) if side == OrderSide.SELL else round(signal_price * 0.998, 2)
        try:
            if hasattr(self.client, "get_latest_quote"):
                quote = self.client.get_latest_quote(symbol)
            else:
                data_client = getattr(self, "_stock_data_client", None)
                if data_client is None:
                    data_client = StockHistoricalDataClient(API_KEY, API_SECRET)
                    self._stock_data_client = data_client
                quotes = data_client.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=symbol)
                )
                quote = quotes[symbol]
            bid = float(getattr(quote, "bid_price", 0) or 0)
            ask = float(getattr(quote, "ask_price", 0) or 0)
            if bid <= 0 and ask <= 0:
                return fallback, "no live quote"

            if side == OrderSide.BUY:
                if ask <= 0:
                    return round(bid, 2), None
                limit = round(ask * 0.9995, 2)
                if limit <= 0:
                    return fallback, "bad quote"
                return limit, None

            if bid <= 0:
                return round(ask, 2), None
            limit = round(bid * 1.0005, 2)
            if limit <= 0:
                return fallback, "bad quote"
            return limit, None
        except Exception as exc:
            log.warning("After-hours quote lookup failed for %s: %s", symbol, exc)
            return fallback, "quote fetch error"

    # -- Position Cache ----------------------------------------------------
    def _find_weakest_position(self) -> Optional[str]:
        """Return the symbol of the open long position with the worst unrealized P&L %.
        Only considers active longs (price > 0) with no shares held for pending orders.
        Skips positions entered today and those already closed this cycle.
        Returns None if no closable position found."""
        try:
            today = datetime.date.today()
            entered_today = {
                sym for sym, info in self._entry_log.items()
                if info.get("date") == today
            }
            # Use _get_positions() so inactive/dead positions are already excluded
            active = self._get_positions().positions_dict.values()
            longs = [
                p for p in active
                if float(p.qty) > 0
                and float(getattr(p, "qty_available", p.qty)) > 0
                and p.symbol not in self._swap_cycle_closed
                and p.symbol not in entered_today
                and not re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', p.symbol)  # skip OCC option symbols
            ]
            if not longs:
                return None
            worst = min(longs, key=lambda p: float(p.unrealized_plpc or 0))
            return worst.symbol
        except Exception as e:
            log.warning(f"_find_weakest_position error: {e}")
            return None

    def _find_least_confident_position(self, min_new_conf: float = 0.0) -> tuple:
        """Return (symbol, entry_confidence) of the held long position with the lowest
        entry confidence that is strictly below min_new_conf.
        Skips positions entered today (give them a full day) and those already swapped.
        Returns (None, 1.0) if no suitable candidate found."""
        try:
            today = datetime.date.today()
            entered_today = {
                sym for sym, info in self._entry_log.items()
                if info.get("date") == today
            }
            positions = self.client.get_all_positions()
            candidates = [
                p for p in positions
                if float(p.qty) > 0
                and float(getattr(p, "qty_available", p.qty)) > 0
                and p.symbol not in self._swap_cycle_closed
                and p.symbol not in entered_today
                and not re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', p.symbol)  # skip OCC option symbols
            ]
            if not candidates:
                return None, 1.0

            def _entry_conf(p):
                return self._entry_log.get(p.symbol, {}).get("confidence", 0.0)

            worst = min(candidates, key=_entry_conf)
            worst_conf = _entry_conf(worst)
            # Only swap if new signal is meaningfully more confident (>5% gap)
            if worst_conf >= min_new_conf - 0.05:
                return None, worst_conf
            return worst.symbol, worst_conf
        except Exception as e:
            log.warning(f"_find_least_confident_position error: {e}")
            return None, 1.0

    def _get_positions(self, force_refresh: bool = False) -> PositionInfo:
        now = time.time()
        if force_refresh or self._position_cache is None or (now - self._cache_timestamp) > self._cache_ttl:
            raw = self.client.get_all_positions()
            # Filter out inactive positions: no current price (expired options,
            # delisted stocks) or zero qty. These are stuck/dead positions that
            # have no market value and should not count toward MAX_POSITIONS.
            active = [
                p for p in raw
                if float(getattr(p, "current_price", None) or 0) > 0
                and float(getattr(p, "qty", 0) or 0) != 0
            ]
            self._position_cache = PositionInfo(
                positions_dict={p.symbol: p for p in active},
                total_count=len(active),
            )
            self._cache_timestamp = now
        return self._position_cache

    # -- Account Cache -----------------------------------------------------
    def _get_account(self, force_refresh: bool = False) -> AccountSnapshot:
        now = time.time()
        if force_refresh or self._account_cache is None or (now - self._account_cache.timestamp) > self._account_ttl:
            raw = self.client.get_account()
            self._account_cache = AccountSnapshot(
                equity=float(raw.equity or 0),
                buying_power=float(raw.buying_power or 0),
                daytrade_count=int(raw.daytrade_count or 0),
                pattern_day_trader=str(getattr(raw, "pattern_day_trader", False)).lower() in ("1", "true", "yes"),
                timestamp=now,
            )
        return self._account_cache

    # -- Validation --------------------------------------------------------
    def _validate_trade(self, signal: Signal, acct: AccountSnapshot, order_type: OrderType, swap_only: bool = False) -> Tuple[bool, Optional[str]]:
        if USE_VIX_ROC_FILTER:
            allow, roc = check_vix_roc_filter()
            if not allow:
                return False, f"VIX spike filter: {roc:.1f}% increase"

        # PDT checks disabled — trading is now allowed regardless of day trade count
        # if acct.pattern_day_trader and acct.equity < PDT_ACCOUNT_MIN and acct.daytrade_count > PDT_MAX_TRADES:
        #     msg = (
        #         f"PDT VIOLATION: {acct.daytrade_count} day trades used "
        #         f"(limit {PDT_MAX_TRADES}, equity ${acct.equity:,.0f}) — "
        #         f"account may be flagged as Pattern Day Trader. Review immediately!"
        #     )
        #     log.error(msg)
        #     if not getattr(self, "_pdt_violation_alerted", False):
        #         send_email("[APEXTRADER] PDT VIOLATION ALERT", msg)
        #         self._pdt_violation_alerted = True
        #     return False, f"PDT violation: {acct.daytrade_count}/{PDT_MAX_TRADES} day trades exceeded"
        # dt_left = self.pdt.remaining(acct.equity, acct.daytrade_count, acct.pattern_day_trader)
        # if acct.pattern_day_trader and dt_left <= PDT_WARN_AT_REMAINING and acct.equity < PDT_ACCOUNT_MIN:
        #     log.warning(f"PDT WARNING: only {dt_left} day trade(s) remaining (equity ${acct.equity:,.0f})")

        # Skip hard-to-borrow shorts cached from previous failures this session
        if order_type == OrderType.SHORT and signal.symbol in self._htb_cache:
            return False, f"{signal.symbol} hard-to-borrow (cached)"

        if signal.symbol in getattr(self, "_halted_symbols", set()):
            return False, f"{signal.symbol} halted (cached for this session)"

        # Asset tradability check: skip halted or suspended symbols
        try:
            asset = self.client.get_asset(signal.symbol)
            raw_status = getattr(asset, "status", "active")
            status = str(getattr(raw_status, "value", raw_status)).lower()
            if status != "active":
                return False, f"{signal.symbol} not tradable: asset status={raw_status}"
            if not getattr(asset, "tradable", True):
                return False, f"{signal.symbol} not tradable: asset.tradable=False"
        except Exception as e:
            log.warning(f"{signal.symbol}: asset status check failed ({e}) — proceeding cautiously")

        # Pending order guard: don't submit a second order if one is already live/filling
        if signal.symbol in self.order_cache:
            cached_id = self.order_cache[signal.symbol]
            try:
                cached_order = self.client.get_order_by_id(cached_id)
                active_statuses = {"new", "partially_filled", "pending_new", "accepted", "held"}
                if str(getattr(cached_order, "status", "")).lower() in active_statuses:
                    return False, f"Pending order already active for {signal.symbol} (id={cached_id})"
                else:
                    # Order is filled/cancelled — remove stale cache entry
                    del self.order_cache[signal.symbol]
            except Exception:
                # Can't verify — keep cache entry intact to avoid double-submit risk
                return False, f"Could not verify order status for {signal.symbol} (id={cached_id}) — skipping to be safe"

        positions = self._get_positions()

        # Dynamic max positions: use equity-based strategic capacity (not raw buying_power).
        # buying_power can be artificially depressed by leveraged/inverse ETF margin requirements,
        # causing the bot to permanently block new entries even when capital is available.
        # We compute effective_max from equity × position_size_pct, then separately gate each
        # execution on whether buying_power is sufficient for one position.
        _pos_size_pct = (
            SMALL_ACCOUNT_POSITION_SIZE_PCT
            if acct.equity < SMALL_ACCOUNT_EQUITY_THRESHOLD
            else POSITION_SIZE_PCT
        )
        _pos_size_dollars = max(MIN_POSITION_DOLLARS, acct.equity * _pos_size_pct / 100.0)
        # Strategic max: how many positions our equity allocation strategy supports
        equity_capacity = max(1, int(acct.equity * 0.95 / _pos_size_dollars))
        effective_max = min(MAX_POSITIONS, equity_capacity)
        log.debug(
            f"[DBG] effective_max={effective_max} equity={acct.equity:.0f} bp={acct.buying_power:.0f} "
            f"pos_size=${_pos_size_dollars:.0f} ({_pos_size_pct:.0f}%) equity_cap={equity_capacity}"
        )

        # ── Buying power gate (must come first) ───────────────────────────
        # Deduct capital already committed to open options positions so equity
        # cannot double-spend the options allocation.
        effective_bp = acct.buying_power - self._options_cost_reserve
        if self._options_cost_reserve > 0:
            log.debug(
                f"[DBG] BP after options reserve deduction: "
                f"${acct.buying_power:,.0f} - ${self._options_cost_reserve:,.0f} = ${effective_bp:,.0f}"
            )
        margin = 2.0 if order_type == OrderType.SHORT else 1.0
        min_usable = (SMALL_ACCOUNT_MIN_POSITION_DOLLARS 
                      if acct.equity < SMALL_ACCOUNT_EQUITY_THRESHOLD 
                      else MIN_POSITION_DOLLARS)
        min_bp_needed = min_usable * margin
        
        if effective_bp < min_bp_needed:
            return False, (
                f"Insufficient buying power: ${effective_bp:,.0f} "
                f"(need ${min_bp_needed:,.0f} for minimum position, "
                f"${self._options_cost_reserve:,.0f} reserved for options)"
            )
        
        # ── Max positions gate (secondary; optional swap if at limit) ─────
        if positions.total_count >= effective_max:
            if not (SWAP_ON_FULL and signal.confidence >= SWAP_MIN_CONFIDENCE):
                log.info(
                    f"Max positions reached ({positions.total_count}/{effective_max}) — "
                    f"skipping {signal.symbol}"
                )
                return False, f"Max positions reached: {positions.total_count}/{effective_max}"
            else:
                # Strong confidence signal + at max: prefer swap to maintain position count
                label = "SWAP (bear)" if swap_only else "SWAP"
                weakest = self._find_weakest_position()
                if weakest:
                    log.info(
                        f"{label}: closing {weakest} (weakest) to make room for "
                        f"{signal.symbol} (conf={signal.confidence:.0%})"
                    )
                    try:
                        self.client.close_position(weakest)
                        self._swap_cycle_closed.add(weakest)
                        log.info(
                            f"SWAP close submitted for {weakest}; waiting for position count "
                            f"to fall below {effective_max} before entering {signal.symbol}"
                        )
                        return False, f"Swap close submitted for {weakest}; retry after close"
                    except Exception as e:
                        err_str = str(e)
                        if "40310100" in err_str:
                            # Alpaca PDT protection: position was entered today.
                            self._entry_log[weakest] = {
                                "strategy": "restored",
                                "date": datetime.date.today(),
                                "confidence": 0.0,
                            }
                            log.warning(
                                f"SWAP skip {weakest}: PDT same-day protection (40310100) — "
                                f"marked as today entry, will not retry this session"
                            )
                            # Don't block the new signal — allow entry without the swap
                        elif "40010001" in err_str or "not active" in err_str.lower():
                            # Asset is inactive/delisted — add to entry_log so _find_weakest skips it
                            self._entry_log[weakest] = {
                                "strategy": "inactive",
                                "date": datetime.date.today(),
                                "confidence": 0.0,
                            }
                            log.warning(
                                f"SWAP skip {weakest}: asset not active (40010001) — "
                                f"excluded from future swaps, allowing entry for {signal.symbol}"
                            )
                            # Don't block the new signal — allow entry without the swap
                        else:
                            log.warning(f"SWAP close failed for {weakest}: {e}")
                            return False, f"Swap close failed: {e}"
                else:
                    # No position to swap, but BP available — allow entry anyway
                    log.debug(
                        f"No swappable position found, but allowing entry due to available BP ${acct.buying_power:,.0f}"
                    )

        if positions.has_position(signal.symbol):
            if order_type == OrderType.LONG  and positions.is_long(signal.symbol):
                return False, f"Already long {signal.symbol}"
            if order_type == OrderType.SHORT and positions.is_short(signal.symbol):
                return False, f"Already short {signal.symbol}"

        return True, None

    # -- Buying Power Sizing -----------------------------------------------
    def _size_with_buying_power(
        self, buying_power: float, signal: Signal,
        risk_info: Dict, order_type: OrderType
    ) -> Tuple[int, Optional[str]]:
        """Returns (shares, skip_reason). Downsizes if BP constrained, skips if below min."""
        margin  = 2.0 if order_type == OrderType.SHORT else 1.0
        usable  = buying_power * (1.0 - MIN_BUYING_POWER_PCT / 100.0)
        desired = int(risk_info["dollar_amount"] / signal.price)
        max_bp  = int(usable / (signal.price * margin))
        shares  = min(desired, max_bp)

        account_snapshot = self._account_cache or self._get_account()  # use cached if available
        min_position = SMALL_ACCOUNT_MIN_POSITION_DOLLARS if account_snapshot.equity < SMALL_ACCOUNT_EQUITY_THRESHOLD else MIN_POSITION_DOLLARS

        if shares < 1:
            return 0, (
                f"Insufficient BP: ${buying_power:,.0f} usable ${usable:,.0f} "
                f"for {signal.symbol} @ ${signal.price:.2f} (x{margin:.0f} margin)"
            )

        cost = shares * signal.price

        # Debug trace for min position handling.
        log.debug(
            f"size check {signal.symbol}: equity={account_snapshot.equity:.2f}, "
            f"min_position=${min_position:.2f}, shares={shares}, cost=${cost:.2f}, desired={desired}, max_bp={max_bp}, usable=${usable:.2f}"
        )

        if cost < min_position:
            return 0, f"{signal.symbol} too small after downsize: ${cost:.0f} < min ${min_position:.0f}"

        if shares < desired:
            log.info(
                f"  BP downsize {signal.symbol}: {desired} -> {shares} shares "
                f"(BP ${buying_power:,.0f}, usable ${usable:,.0f}, cost ${cost:,.0f})"
            )
        return shares, None

    # ── Bracket Prices ──────────────────────────────────────────────────────────
    def _calculate_bracket_prices(self, signal: Signal, risk_info: Dict, order_type: OrderType) -> tuple:
        if signal.atr_stop and signal.atr_stop > 0:
            # ATR-based 2:1 R:R — stop at 1.5×ATR, target at 2× the risk
            risk_dist = signal.atr_stop
            if order_type == OrderType.LONG:
                sl = round(signal.price - risk_dist, 2)
                tp = round(signal.price + ATR_TP_RATIO * risk_dist, 2)
            else:
                sl = round(signal.price + risk_dist, 2)
                tp = round(signal.price - ATR_TP_RATIO * risk_dist, 2)
        else:
            # Percentage-based fallback
            if order_type == OrderType.LONG:
                sl = round(signal.price * (1 - risk_info["stop_loss_pct"] / 100), 2)
                tp = round(signal.price * (1 + risk_info["tp"]            / 100), 2)
            else:
                sl = round(signal.price * (1 + risk_info["stop_loss_pct"] / 100), 2)
                tp = round(signal.price * (1 - risk_info["tp"]            / 100), 2)
        return sl, tp

    # ── Entry + Trailing Stop Order ──────────────────────────────────────────
    def _create_bracket_order(self, signal: Signal, shares: int, risk_info: Dict, order_type: OrderType) -> bool:
        """Submit market entry then a GTC trailing stop at risk_info['stop_loss_pct']%.
        TP bracket leg is intentionally dropped — the trailing stop locks in gains
        automatically; swap logic and EOD close handle opportunity exits."""
        side      = OrderSide.BUY  if order_type == OrderType.LONG else OrderSide.SELL
        stop_side = OrderSide.SELL if order_type == OrderType.LONG else OrderSide.BUY
        trail_pct = risk_info["stop_loss_pct"]  # tiered: NORMAL=3%, MEDIUM=4%, HIGH=5%, EXTREME=7%

        # ── Step 1: Entry order (failure aborts the whole bracket) ──────────
        try:
            entry_req = MarketOrderRequest(
                symbol          = signal.symbol,
                qty             = shares,
                side            = side,
                time_in_force   = TimeInForce.DAY,
                client_order_id = f"apex-{signal.strategy}-{signal.symbol}-{int(time.time())}",
            )
            order = self.client.submit_order(entry_req)
            self.order_cache[signal.symbol] = order.id
            self._submitted_entry_orders[signal.symbol] = order

            # Set dual-phase TP targets from entry price
            _ep = signal.price
            ti_profile = classify_ti_profile(signal.symbol, signal.strategy)
            _tp1_pct = SQUEEZE_TP1_PCT if ti_profile in {"squeeze", "high_short_float"} else TP_INTERMEDIATE_PCT
            _tp2_pct = SQUEEZE_TP2_PCT if ti_profile in {"squeeze", "high_short_float"} else TP_FINAL_PCT
            _int_price = round(_ep * (1 + _tp1_pct / 100), 2) if order_type == OrderType.LONG else round(_ep * (1 - _tp1_pct / 100), 2)
            _final_price = round(_ep * (1 + _tp2_pct / 100), 2) if order_type == OrderType.LONG else round(_ep * (1 - _tp2_pct / 100), 2)
            self._intermediate_targets[signal.symbol] = _int_price
            self._tp_targets[signal.symbol]           = _final_price
            log.info(
                f"TP targets set {signal.symbol}: tighten@${_int_price:.2f} (+{_tp1_pct:.0f}%) "
                f"close@${_final_price:.2f} (+{_tp2_pct:.0f}%) profile={ti_profile}"
            )

        except Exception as e:
            err = str(e).lower()
            if "trading halt" in err or "halted" in err:
                if not hasattr(self, "_halted_symbols"):
                    self._halted_symbols = set()
                self._halted_symbols.add(signal.symbol)
                log.warning(f"Bracket skip {signal.symbol}: broker reports trading halt; cached for this session")
            elif order_type == OrderType.SHORT and ("cannot be sold short" in err or "40310000" in err or "account is not allowed to short" in err):
                # Symbol-level HTB: block only this ticker for the session
                self._htb_cache.add(signal.symbol)
                if "account is not allowed to short" in err:
                    # Account-level: no short permission at all — disable all shorts
                    self.shorting_blocked = True
                    log.warning(
                        f"Short entry blocked for {signal.symbol} (account permission). "
                        "Disabling shorts for this session."
                    )
                else:
                    log.warning(f"Short blocked {signal.symbol} (HTB/insufficient BP): {e}")
            elif order_type != OrderType.SHORT and ("cannot be sold short" in err or "40310000" in err):
                # Inverse ETF or other buy rejected by broker — do not poison short flag
                log.warning(f"Buy rejected for {signal.symbol} (broker): {e}")
            elif "insufficient buying power" in err:
                log.warning(f"Bracket skip {signal.symbol}: insufficient buying power")
            else:
                log.error(f"Bracket order failed {signal.symbol}: {e}")
            return False

        # ── Step 2: Trailing stop — placed immediately at entry ──────────────
        # High-volatile stocks need protection from the start.
        # trail_pct comes from the ATR-based tier (NORMAL=5%, HIGH=6%, EXTREME=8%).
        # After TP scale-out, check_tp_targets() replaces this with a fresh
        # SCALEOUT_TRAIL_PCT stop on the remaining 50%.
        # Wait for the entry to fill first: a GTC trailing stop submitted while the
        # market buy is still pending is rejected ("cannot open a short sell while a
        # long buy order is open"), and PROTECT_POSITIONS_ENABLED is off so there is
        # no later retry — the position would run the whole session unprotected.
        self._wait_for_entry_fill(signal.symbol, order.id)
        try:
            ts_req = TrailingStopOrderRequest(
                symbol        = signal.symbol,
                qty           = shares,
                side          = stop_side,
                type          = AlpacaOrderType.TRAILING_STOP,
                time_in_force = TimeInForce.GTC,
                trail_percent = trail_pct,
            )
            self.client.submit_order(ts_req)
            log.info(f"Trailing stop placed {signal.symbol}: {trail_pct:.1f}% GTC ({shares} shares)")
        except Exception as e:
            log.warning(
                f"Trailing stop skipped {signal.symbol}: {e} — "
                "TP scale-out and EOD close will handle the exit"
            )

        self._log_bracket(signal, shares, risk_info, trail_pct, None, order_type)
        return True

    def _wait_for_entry_fill(self, symbol: str, order_id, timeout: float = 3.0) -> bool:
        """Poll an entry order until it fills so a protective stop can attach cleanly.
        Returns True if the broker reports the order filled within the timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                o = self.client.get_order_by_id(order_id)
            except Exception:
                time.sleep(0.3)
                continue
            status = str(getattr(o, "status", "")).lower()
            if "filled" in status and "partially" not in status:
                return True
            if status in ("canceled", "cancelled", "rejected", "expired"):
                log.warning(f"Entry {symbol}: order {status} before protective stop could attach")
                return False
            time.sleep(0.3)
        log.info(f"Entry {symbol}: fill not confirmed within {timeout:.0f}s — placing protective stop anyway")
        return False

    def _log_bracket(self, signal, shares, risk_info, trail_pct, _tp_unused, order_type):
        action    = "BUY"  if order_type == OrderType.LONG else "SHORT"
        tier      = risk_info["tier"]
        atr_pct   = risk_info.get("atr_pct", 0)
        alloc_pct = risk_info["allocation_pct"]

        if USE_DYNAMIC_TIERS and atr_pct > 0 and USE_RISK_EQUALIZED_SIZING:
            log.info(f"{action} {signal.symbol}: {shares} @ ${signal.price:.2f} "
                     f"({alloc_pct:.1f}% pos) | TRAILING SL {trail_pct:.1f}% "
                     f"| Tier: {tier} (ATR {atr_pct:.1f}%) | {signal.strategy}")
        else:
            log.info(f"{action} {signal.symbol}: {shares} @ ${signal.price:.2f} "
                     f"| TRAILING SL {trail_pct:.1f}% | Tier: {tier} | {signal.strategy}")

    # ΓöÇΓöÇ Simple Order ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
    def _create_simple_order(self, signal: Signal, shares: int, order_type: OrderType) -> bool:
        side   = OrderSide.BUY if order_type == OrderType.LONG else OrderSide.SELL
        action = "BUY"         if order_type == OrderType.LONG else "SHORT"

        try:
            coid = f"apex-{signal.strategy}-{signal.symbol}-{int(time.time())}"
            if EXTENDED_HOURS and not self._current_market_state().is_regular_hours:
                limit, quote_reason = self._after_hours_limit_price(signal.symbol, float(signal.price or 0), side)
                if quote_reason is not None:
                    log.info(f"{action} LIMIT SKIP {signal.symbol}: {quote_reason}; no executable quote for after-hours order")
                    return False
                req = LimitOrderRequest(
                    symbol          = signal.symbol,
                    qty             = shares,
                    side            = side,
                    time_in_force   = TimeInForce.DAY,
                    limit_price     = limit,
                    extended_hours  = True,
                    client_order_id = coid,
                )
                order = self.client.submit_order(req)
                self.order_cache[signal.symbol] = order.id
                self._submitted_entry_orders[signal.symbol] = order
                log.info(f"{action} LIMIT {signal.symbol}: {shares} @ ${limit:.2f} (ext-hours quote-first) | {signal.strategy}")
                return True
            else:
                req = MarketOrderRequest(
                    symbol          = signal.symbol,
                    qty             = shares,
                    side            = side,
                    time_in_force   = TimeInForce.DAY,
                    client_order_id = coid,
                )
                order = self.client.submit_order(req)
                self.order_cache[signal.symbol] = order.id
                self._submitted_entry_orders[signal.symbol] = order
                log.info(f"{action} {signal.symbol}: {shares} @ ${signal.price:.2f} | {signal.strategy}")
                return True

        except Exception as e:
            err = str(e).lower()
            if order_type == OrderType.SHORT and ("cannot be sold short" in err or "40310000" in err or "account is not allowed to short" in err):
                # Symbol-level HTB: block only this ticker for the session
                self._htb_cache.add(signal.symbol)
                if "account is not allowed to short" in err:
                    # Account-level: no short permission at all — disable all shorts
                    self.shorting_blocked = True
                    log.warning(
                        f"Short entry blocked for {signal.symbol} (account permission). "
                        "Disabling shorts for this session."
                    )
                else:
                    log.warning(f"Short blocked {signal.symbol} (HTB/insufficient BP): {e}")
            elif order_type != OrderType.SHORT and ("cannot be sold short" in err or "40310000" in err):
                # Inverse ETF or other buy rejected by broker — do not poison short flag
                log.warning(f"Buy rejected for {signal.symbol} (broker): {e}")
            elif "insufficient buying power" in err:
                log.warning(f"Skip {signal.symbol}: insufficient buying power")
            else:
                log.error(f"{action} order error {signal.symbol}: {e}")
            return False

    # -- Entry (unified) ---------------------------------------------------
    # ─── Market Data Validation (Entry) ──────────────────────────────────────
    def _validate_market_price(self, symbol: str, signal_price: float, order_type: OrderType, max_divergence_pct: float = 5.0) -> Tuple[bool, float]:
        """Fetch current market snapshot and validate signal price is still reasonable.

        Args:
            symbol: Ticker symbol
            signal_price: Price from the signal (entry recommendation)
            order_type: OrderType.LONG or OrderType.SHORT
            max_divergence_pct: Max % divergence allowed (default 5%)

        Returns:
            (valid, market_price_to_use) where market_price_to_use is from snapshot bid/ask
            Returns (True, signal_price) if snapshot unavailable (fallback to signal)
            Returns (False, 0) if price divergence exceeds threshold
        """
        try:
            snapshot = self.client.get_latest_trade(symbol)
            if snapshot is None or snapshot.price is None:
                log.debug(f"Market snapshot unavailable for {symbol} — using signal price ${signal_price:.2f}")
                return True, signal_price
            
            market_price = float(snapshot.price)
            divergence_pct = abs(market_price - signal_price) / signal_price * 100
            
            if divergence_pct > max_divergence_pct:
                log.warning(
                    f"Price divergence {symbol}: signal ${signal_price:.2f} vs market ${market_price:.2f} "
                    f"({divergence_pct:.1f}% > {max_divergence_pct}%) — REJECT"
                )
                return False, 0
            
            # Use market price, adjusted slightly for order type
            if order_type == OrderType.LONG:
                # Buying: use bid + 0.5% slippage buffer for real-time validity
                adjusted_price = market_price * 1.005 if market_price > 0 else signal_price
            else:
                # Shorting: use ask - 0.5% slippage buffer
                adjusted_price = market_price * 0.995 if market_price > 0 else signal_price
            
            log.debug(
                f"Price validation {symbol}: signal ${signal_price:.2f} → market ${market_price:.2f} "
                f"(divergence {divergence_pct:.1f}%) → use ${adjusted_price:.2f}"
            )
            return True, adjusted_price
            
        except Exception as e:
            log.debug(f"Market price snapshot failed for {symbol}: {e} — using signal price")
            return True, signal_price

    def _execute_entry(self, signal: Signal, acct: AccountSnapshot, order_type: OrderType, swap_only: bool = False) -> bool:
        valid, reason = self._validate_trade(signal, acct, order_type, swap_only=swap_only)
        if not valid:
            if reason:
                log.info(f"Skip {signal.symbol}: {reason}")
            return False

        if not self._can_submit_live_probe():
            return False

        # ── Market Data Price Validation: ensure signal price is still valid ────
        price_valid, market_price = self._validate_market_price(signal.symbol, signal.price, order_type)
        if not price_valid:
            log.info(f"Skip {signal.symbol}: market price divergence too large")
            return False
        
        # Use market-derived price instead of signal price (market-data-only)
        # Create a working copy of the signal with market-validated price
        entry_signal = signal
        if market_price > 0 and market_price != signal.price:
            # Log the price adjustment for transparency
            log.info(f"Price adjustment {signal.symbol}: ${signal.price:.2f} → ${market_price:.2f} (market data)")
            # We'll use market_price in the risk calculation below

        risk_info = calculate_risk_adjusted_size(acct.equity, signal.symbol, market_price if market_price > 0 else signal.price)

        # 4× margin gate: verify the stock is marginable before applying leverage
        if MARGIN_LEVERAGE > 1.0:
            try:
                asset = self.client.get_asset(signal.symbol)
                if not getattr(asset, "marginable", True):
                    log.info(
                        f"Skip {signal.symbol}: not marginable — "
                        f"{MARGIN_LEVERAGE:.0f}× leverage requires a marginable stock (price ≥ $5, major exchange)"
                    )
                    return False
            except Exception as _e:
                log.debug(f"Marginable check failed for {signal.symbol}: {_e} — proceeding without leverage guard")
        from engine.config import MIN_SIGNAL_CONFIDENCE
        _conf_floor = MIN_SIGNAL_CONFIDENCE
        _conf_mult = CONF_SCALE_MIN_MULT + (1.0 - CONF_SCALE_MIN_MULT) * min(
            1.0, max(0.0, (signal.confidence - _conf_floor) / (CONF_SCALE_FULL_CONF - _conf_floor))
        )
        risk_info = dict(risk_info, dollar_amount=round(risk_info["dollar_amount"] * _conf_mult, 2))
        if MARGIN_LEVERAGE > 1.0:
            risk_info = dict(risk_info, dollar_amount=round(risk_info["dollar_amount"] * MARGIN_LEVERAGE, 2))
        log.debug(
            f"[SIZE] {signal.symbol} conf={signal.confidence:.0%} "
            f"scale={_conf_mult:.2f}× leverage={MARGIN_LEVERAGE:.0f}× → ${risk_info['dollar_amount']:,.0f}"
        )

        shares, skip_reason = self._size_with_buying_power(acct.buying_power, signal, risk_info, order_type)
        if shares < 1:
            # Confidence-swap: if a held position has lower entry confidence, rotate into the new signal.
            # Skip entirely when PDT = 0 — closing a same-day position would itself be a day trade.
            _dt_left_swap = self.pdt.remaining(acct.equity, acct.daytrade_count)
            if order_type == OrderType.LONG and _dt_left_swap > 0:
                victim, victim_conf = self._find_least_confident_position(signal.confidence)
                if victim:
                    log.info(
                        f"CONF-SWAP: closing {victim} (conf={victim_conf:.0%}) "
                        f"to make room for {signal.symbol} (conf={signal.confidence:.0%})"
                    )
                    try:
                        self.client.close_position(victim)
                        self._swap_cycle_closed.add(victim)
                        # Do not count the close as a day trade (exits are always allowed)
                        acct = self._get_account(force_refresh=True)
                        shares, skip_reason = self._size_with_buying_power(acct.buying_power, signal, risk_info, order_type)
                    except Exception as e:
                        log.warning(f"Conf-swap close failed for {victim}: {e}")
            if shares < 1:
                log.info(f"Skip {signal.symbol}: {skip_reason}")
                return False

        # Short-float position cap: never exceed 20% of equity in a single squeeze ticker
        if is_high_short_float(signal.symbol):
            cap_shares = max(0, int(acct.equity * (MAX_SHORT_FLOAT_PCT / 100) / (market_price if market_price > 0 else signal.price)))
            if shares > cap_shares:
                log.info(
                    f"Short-float cap {signal.symbol}: {shares}→{cap_shares} shares "
                    f"({MAX_SHORT_FLOAT_PCT:.0f}% equity max, equity ${acct.equity:,.0f})"
                )
                shares = cap_shares
            if shares < 1:
                log.info(f"Skip {signal.symbol}: too small after short-float cap")
                return False

        if order_type == OrderType.SHORT and LONG_ONLY_MODE:
            log.info(f"Skipping {signal.symbol} SHORT because LONG_ONLY_MODE is active")
            return False

        if LIVE_PROBE_MODE:
            log.info(
                f"LIVE PROBE {signal.symbol}: sizing {shares} → {LIVE_PROBE_SHARES} share(s); "
                "no automatic scale-in"
            )
            shares = LIVE_PROBE_SHARES

        # Create new signal with market-validated price for order submission
        market_signal = entry_signal
        if market_price > 0 and market_price != signal.price:
            # Use a shallow copy with updated price
            market_signal = entry_signal
            market_signal.price = market_price  # Update price to market data

        if self.use_bracket_orders and self._current_market_state().is_regular_hours:
            if self._create_bracket_order(market_signal, shares, risk_info, order_type):
                self.pdt.add(datetime.date.today())
                self._record_entry(signal, market_price if market_price > 0 else signal.price)
                # No swap protection — position can be re-evaluated immediately
                self._get_positions(force_refresh=True)
                self._get_account(force_refresh=True)
                return True
            if signal.symbol in getattr(self, "_halted_symbols", set()):
                return False

        if self._create_simple_order(market_signal, shares, order_type):
            self.pdt.add(datetime.date.today())
            self._record_entry(signal, market_price if market_price > 0 else signal.price)
            # No swap protection — position can be re-evaluated immediately
            self._get_positions(force_refresh=True)
            self._get_account(force_refresh=True)
            return True

        return False

    # -- Public: Execute ---------------------------------------------------
    def execute(self, signal: Signal, swap_only: bool = False) -> bool:
        try:
            acct      = self._get_account()
            positions = self._get_positions()

            if signal.action == "buy":
                if positions.has_position(signal.symbol) and positions.is_short(signal.symbol):
                    return self._close_short_position(signal, acct.equity)
                return self._execute_entry(signal, acct, OrderType.LONG, swap_only=swap_only)

            elif signal.action in ("sell", "short"):
                if LONG_ONLY_MODE:
                    log.info(
                        f"Skipping {signal.symbol} {signal.action.upper()} because LONG_ONLY_MODE is enabled"
                    )
                    return False
                if self.shorting_blocked:
                    log.info(
                        f"Skipping {signal.symbol} {signal.action.upper()} because shorting is blocked for this account/session"
                    )
                    return False

                if positions.has_position(signal.symbol) and positions.is_long(signal.symbol):
                    return self._close_long_position(signal, acct.equity)
                return self._execute_entry(signal, acct, OrderType.SHORT, swap_only=swap_only)

        except Exception as e:
            log.error(f"Execute error {signal.symbol}: {e}")
        return False

    # ΓöÇΓöÇ Close Short ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
    def _close_short_position(self, signal: Signal, equity: float) -> bool:
        positions = self._get_positions()
        if not positions.has_position(signal.symbol):
            log.info(f"No short position in {signal.symbol}")
            return False
        try:
            qty = abs(int(positions.positions_dict[signal.symbol].qty))
            if EXTENDED_HOURS and not self._current_market_state().is_regular_hours:
                price = self._after_hours_limit_price(signal.symbol, float(signal.price or 0), OrderSide.BUY)
                req = LimitOrderRequest(
                    symbol=signal.symbol, qty=qty, side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    limit_price=price, extended_hours=True,
                )
            else:
                req = MarketOrderRequest(
                    symbol=signal.symbol, qty=qty, side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
            self.client.submit_order(req)
            # Closing a short that was opened today is a day trade round-trip
            self.pdt.add(datetime.date.today())
            log.info(f"COVER {signal.symbol}: {qty} @ ${signal.price:.2f} | {signal.strategy}")
            return True
        except Exception as e:
            log.error(f"Cover error {signal.symbol}: {e}")
            return False

    # ΓöÇΓöÇ Close Long ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
    def _close_long_position(self, signal: Signal, equity: float) -> bool:
        positions = self._get_positions()
        if not positions.has_position(signal.symbol):
            log.info(f"No position in {signal.symbol}")
            return False
        # Closes are ALWAYS allowed regardless of PDT — never block an exit

        qty = abs(int(float(positions.positions_dict[signal.symbol].qty)))
        try:
            if EXTENDED_HOURS and not self._current_market_state().is_regular_hours:
                limit, quote_reason = self._after_hours_limit_price(
                    signal.symbol, float(signal.price or 0), OrderSide.SELL
                )
                if quote_reason is not None:
                    log.info(
                        f"SELL LIMIT SKIP {signal.symbol}: {quote_reason}; "
                        "no executable quote for after-hours order"
                    )
                    return False
                req = LimitOrderRequest(
                    symbol=signal.symbol, qty=qty, side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                    limit_price=limit, extended_hours=True,
                )
            else:
                req = MarketOrderRequest(
                    symbol=signal.symbol, qty=qty,
                    side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                )
            self.client.submit_order(req)
            # NOTE: closing an existing position is NOT a new day trade.
            # Alpaca counts the round-trip (open+close same day) as one trade;
            # pdt.add() is intentionally omitted here — it was already counted at entry.
            self._get_positions(force_refresh=True)
            log.info(f"SELL {signal.symbol}: {qty} shares | {signal.strategy}")
            return True
        except Exception as e:
            log.error(f"Sell error {signal.symbol}: {e}")
            return False

    # ─── Protect Open Positions ──────────────────────────────────────────────
    def protect_positions(self) -> None:
        """Disabled — PROTECT_POSITIONS_ENABLED=false. Trailing stops are only placed
        after the 50% scale-out at TP via check_tp_targets()."""
        if not PROTECT_POSITIONS_ENABLED:
            return
        positions = []
        covered = set()

        # Resist transient connection drops by retrying fetch operations.
        for attempt in range(1, 4):
            try:
                positions = self.client.get_all_positions()
                open_orders = self.client.get_orders()
                covered = {o.symbol for o in open_orders}
                break
            except Exception as e:
                log.warning(
                    f"protect_positions: data fetch attempt {attempt}/3 failed: {e}"
                )
                if attempt < 3:
                    time.sleep(2)
                else:
                    log.error("protect_positions: all fetch retries failed; skipping this cycle")
                    return

        for pos in positions:
            sym = pos.symbol

            # Skip options legs — OCC symbols (e.g. AEHR260515C00080000) are managed
            # by OptionsExecutor.monitor_positions(); trailing stops are invalid for options
            # (Alpaca error 42210000).  OCC symbols always match <ticker><YYMMDD><C|P><8digits>.
            if re.match(r'^[A-Z]+\d{6}[CP]\d{8}$', sym):
                continue

            # Skip crypto positions — Alpaca does not support TRAILING_STOP for crypto
            # (error 40010001). Crypto risk is managed by CryptoTrader's own SL logic.
            if getattr(pos, 'asset_class', '') == 'crypto':
                continue

            # Primary guard: don't add orders if symbol already has any active order
            if sym in covered:
                continue

            # Skip positions confirmed as forced overnight holds (PDT blocks close too)
            if sym in self._pdt_overnight_forced:
                continue

            # Secondary guard: skip if broker reports zero available qty
            try:
                qty_available = int(float(pos.qty_available))
            except (AttributeError, TypeError, ValueError):
                qty_available = 0
            if qty_available <= 0:
                continue

            if pos.qty is None or pos.current_price is None:
                log.warning(f"protect_positions {sym}: missing market data (price={pos.current_price}, qty={pos.qty}), skipping")
                continue

            try:
                qty         = int(float(pos.qty))
                avail       = abs(qty_available)
                current     = float(pos.current_price)
                is_long_pos = qty > 0

                tier_info  = get_dynamic_tier(sym, current)
                trail_pct  = tier_info["ts"]
                tier_label = tier_info["tier"]

                stop_side = OrderSide.SELL if is_long_pos else OrderSide.BUY
                self.client.submit_order(TrailingStopOrderRequest(
                    symbol        = sym,
                    qty           = avail,
                    side          = stop_side,
                    type          = AlpacaOrderType.TRAILING_STOP,
                    time_in_force = TimeInForce.GTC,
                    trail_percent = trail_pct,
                ))
                direction = "LONG" if is_long_pos else "SHORT"
                log.info(f"PROTECT {direction} {sym} [{tier_label}]: trailing stop {trail_pct:.1f}% GTC")
            except Exception as e:
                err_str = str(e)
                if "40310100" in err_str:
                    # Broker PDT protection rejects the stop for today's entry.
                    # Fall back to software stop monitoring via check_software_stops().
                    if sym not in self._pdt_stop_blocked:
                        try:
                            entry_price = float(pos.avg_entry_price or pos.current_price)
                            tier_info   = get_dynamic_tier(sym, float(pos.current_price))
                            stop_pct    = tier_info["ts"]
                            stop_price  = round(
                                entry_price * (1 - stop_pct / 100) if qty > 0
                                else entry_price * (1 + stop_pct / 100),
                                2,
                            )
                            self._pdt_stop_blocked[sym] = stop_price
                            log.warning(
                                f"protect_positions {sym}: broker PDT stop rejected — "
                                f"software SL set at ${stop_price:.2f} ({stop_pct:.1f}% from ${entry_price:.2f})"
                            )
                        except Exception:
                            log.warning(f"protect_positions {sym}: PDT stop rejected (software SL unavailable)")
                    else:
                        log.debug(f"protect_positions {sym}: PDT stop still rejected (software SL active @ ${self._pdt_stop_blocked[sym]:.2f})")
                else:
                    log.error(f"protect_positions {sym}: {e}")

    def check_software_stops(self) -> None:
        """Market-close any position whose broker-rejected PDT stop has been breached.
        Called every scan cycle for positions in _pdt_stop_blocked."""
        if not self._pdt_stop_blocked:
            return
        try:
            positions = {p.symbol: p for p in self.client.get_all_positions()}
        except Exception as e:
            log.warning(f"check_software_stops: fetch failed: {e}")
            return
        for sym, stop_price in list(self._pdt_stop_blocked.items()):
            pos = positions.get(sym)
            if pos is None:
                # Position already closed (stop filled or manual)
                self._pdt_stop_blocked.pop(sym, None)
                continue
            try:
                current = float(pos.current_price)
                qty     = int(float(pos.qty))
                is_long = qty > 0
                hit     = (is_long and current <= stop_price) or (not is_long and current >= stop_price)
                if hit:
                    side = OrderSide.SELL if is_long else OrderSide.BUY
                    try:
                        self.client.submit_order(MarketOrderRequest(
                            symbol        = sym,
                            qty           = abs(qty),
                            side          = side,
                            time_in_force = TimeInForce.DAY,
                        ))
                        self._pdt_stop_blocked.pop(sym, None)
                        log.warning(
                            f"SOFTWARE SL HIT {sym}: price ${current:.2f} crossed stop ${stop_price:.2f} — "
                            f"market {'SELL' if is_long else 'BUY-TO-COVER'} submitted"
                        )
                    except Exception as close_err:
                        if "40310100" in str(close_err):
                            # Broker PDT also blocks same-day close — position is a forced
                            # overnight hold.  Stop retrying; it will carry to next session.
                            self._pdt_stop_blocked.pop(sym, None)
                            self._pdt_overnight_forced.add(sym)
                            log.warning(
                                f"SOFTWARE SL {sym}: stop breached at ${current:.2f} but PDT blocks "
                                f"same-day close — holding overnight (stop was ${stop_price:.2f})"
                            )
                        else:
                            log.error(f"check_software_stops {sym}: {close_err}")
                else:
                    log.debug(f"SOFTWARE SL {sym}: current ${current:.2f} | stop ${stop_price:.2f} | margin ${current - stop_price:+.2f}")
            except Exception as e:
                log.error(f"check_software_stops {sym}: {e}")

    # ── Scheduled portfolio flatten ──────────────────────────────────────────
    def _momentum_exemptions(self, positions: list) -> set:
        """Return at most two fresh, strongly trending equity symbols to retain."""
        from engine.utils import get_bars

        try:
            open_orders = self.client.get_orders() or []
        except Exception as e:
            log.info(f"MOMENTUM RETAIN: skipped; protective-order check failed ({e})")
            return set()
        protected_symbols = {
            order.symbol for order in open_orders
            if "stop" in str(getattr(order, "type", "")).lower()
        }
        candidates = []
        for pos in positions:
            if getattr(pos, "asset_class", "us_equity") != "us_equity":
                continue
            entry = float(getattr(pos, "avg_entry_price", 0) or 0)
            current = float(getattr(pos, "current_price", 0) or 0)
            if float(getattr(pos, "qty", 0) or 0) <= 0 or entry <= 0 or current <= 0:
                continue
            if pos.symbol not in protected_symbols:
                log.info(f"MOMENTUM RETAIN {pos.symbol}: skipped; no protective stop found")
                continue
            gain_pct = (current - entry) / entry * 100
            if gain_pct < INTRADAY_MOMENTUM_MIN_GAIN_PCT:
                continue
            try:
                bars = get_bars(pos.symbol, period="1d", interval="5m")
                if bars is None or len(bars) < 4:
                    continue
                closes = [float(v) for v in bars["close"].tail(4)]
                volumes = [float(v) for v in bars["volume"].iloc[:-1] if float(v) > 0]
                latest_volume = float(bars["volume"].iloc[-1])
                typical = (bars["high"] + bars["low"] + bars["close"]) / 3
                vwap = float((typical * bars["volume"]).sum() / bars["volume"].sum())
                rvol = latest_volume / (sum(volumes) / len(volumes)) if volumes else 0
                return_5m = (closes[-1] / closes[-2] - 1) * 100
                if current > vwap and closes[-1] > closes[0] and return_5m >= INTRADAY_MOMENTUM_MIN_5M_RETURN_PCT and rvol >= INTRADAY_MOMENTUM_MIN_RVOL:
                    candidates.append((gain_pct, pos.symbol))
            except Exception as e:
                log.info(f"MOMENTUM RETAIN {pos.symbol}: skipped; fresh data unavailable ({e})")
        candidates.sort(reverse=True)
        retained = {symbol for _, symbol in candidates[:max(0, INTRADAY_MOMENTUM_EXEMPTIONS)]}
        if retained:
            log.warning(f"MOMENTUM RETAIN: exempting {sorted(retained)} from {INTRADAY_MOMENTUM_EXEMPTIONS}-position reset allowance")
        return retained

    def flatten_portfolio(self, reason: str, allow_momentum_exemptions: bool = False) -> bool:
        """Cancel equity orders and close equity positions, optionally retaining strong equities."""
        try:
            positions = self.client.get_all_positions()
        except Exception as e:
            log.error(f"{reason}: position fetch failed: {e}")
            return False

        option_symbols = {
            p.symbol for p in positions
            if str(getattr(p, "asset_class", "")).lower() == "us_option"
        }
        active = {
            p.symbol for p in positions
            if float(getattr(p, "qty", 0) or 0) != 0 and p.symbol not in option_symbols
        }
        if option_symbols:
            log.info(f"{reason}: retaining options positions: {sorted(option_symbols)}")

        scaled_in_symbols = set(getattr(self, "_live_probe_scaled_in", set())) & active
        if scaled_in_symbols:
            log.info(f"{reason}: retaining scaled-in live-probe positions: {sorted(scaled_in_symbols)}")

        long_term_symbols = {
            symbol for symbol in active
            if self._entry_log.get(symbol, {}).get("long_term_hold")
        }
        if long_term_symbols:
            log.info(f"{reason}: retaining long-term positions: {sorted(long_term_symbols)}")

        if not active:
            self._flatten_in_progress = set()
            self._flatten_failed = set()
            return True

        retained = set(scaled_in_symbols) | long_term_symbols
        if allow_momentum_exemptions:
            retained |= self._momentum_exemptions(positions)
        ignored = getattr(self, "_flatten_ignored", set()) & active
        close_symbols = active - retained - ignored

        try:
            for order in self.client.get_orders() or []:
                try:
                    if order.symbol not in close_symbols:
                        continue
                    self.client.cancel_order_by_id(str(order.id))
                    log.info(f"{reason}: cancelled order {order.id} ({order.symbol})")
                except Exception as e:
                    log.warning(f"{reason}: could not cancel order {order.id}: {e}")
        except Exception as e:
            log.error(f"{reason}: open-order fetch failed: {e}")
            return False

        requested = getattr(self, "_flatten_in_progress", set()) & close_symbols
        failed = getattr(self, "_flatten_failed", set()) & active
        retry = (close_symbols - requested) | failed
        failed = set()
        for pos in positions:
            if pos.symbol not in retry:
                continue
            try:
                self.client.close_position(pos.symbol)
                requested.add(pos.symbol)
                log.warning(f"{reason}: close submitted for {pos.symbol} qty={pos.qty}")
            except Exception as e:
                if _is_inactive_asset_error(e):
                    ignored.add(pos.symbol)
                    requested.discard(pos.symbol)
                    log.warning(f"{reason}: ignoring inactive asset {pos.symbol}: {e}")
                    continue
                failed.add(pos.symbol)
                log.error(f"{reason}: close failed for {pos.symbol}: {e}")
        self._flatten_in_progress = requested
        self._flatten_failed = failed
        self._flatten_ignored = ignored
        remaining = close_symbols & (requested | failed)
        if failed:
            log.warning(f"{reason}: close submissions failed; remaining={sorted(failed)}")
        elif requested:
            log.info(
                f"{reason}: close requests submitted; continuing without waiting for "
                f"confirmation: {sorted(requested)}"
            )
        else:
            log.info(f"{reason}: flatten complete; ignored_inactive={sorted(ignored)}")
        return not failed

    # ── Legacy EOD Close ─────────────────────────────────────────────────────
    def close_eod_positions(self) -> Optional[dict]:
        """Close all intraday-strategy positions at EOD_CLOSE_TIME.
        Targets FloatRotation, GapBreakout, ORB, VWAPReclaim opened today."""
        if not EOD_CLOSE_ENABLED:
            return None

        import pytz
        now_et = datetime.datetime.now(pytz.timezone("America/New_York"))
        close_h, close_m = map(int, EOD_CLOSE_TIME.split(":"))
        if now_et.hour < close_h or (now_et.hour == close_h and now_et.minute < close_m):
            return None  # Not yet EOD close time
        if now_et.hour >= 20:
            return None  # Extended hours ended
        regular_hours = (now_et.hour, now_et.minute) < (16, 0)

        today = datetime.date.today()
        if getattr(self, "_eod_close_done", None) == today:
            return None  # EOD close already processed for today

        try:
            positions = self.client.get_all_positions()
        except Exception as e:
            log.error(f"close_eod_positions: fetch failed: {e}")
            return None

        # ── Margin safety: never carry margin-funded exposure overnight ──────
        # Identify positions whose combined market value exceeds available cash
        # and force-close them first, regardless of EOD_CLOSE_ALL/strategy filters.
        margin_force_syms: set = set()
        if MARGIN_EOD_FORCE_CLOSE:
            try:
                acct = self.client.get_account()
                cash = float(getattr(acct, "cash", 0) or 0)
                exposures = sorted(
                    ((p.symbol, abs(float(getattr(p, "market_value", 0) or 0))) for p in positions),
                    key=lambda item: item[1], reverse=True,
                )
                running = sum(mv for _, mv in exposures)
                for sym, mv in exposures:
                    if running <= cash:
                        break
                    margin_force_syms.add(sym)
                    running -= mv
                if margin_force_syms:
                    log.warning(f"MARGIN EOD: cash=${cash:,.0f} exposure requires closing {sorted(margin_force_syms)} to avoid overnight margin")
            except Exception as e:
                log.warning(f"MARGIN EOD check failed (non-fatal): {e}")

        closed_items = []
        failed_items = []

        # Close margin-forced positions first so partial failures don't skip them.
        positions = sorted(positions, key=lambda p: p.symbol not in margin_force_syms)

        for pos in positions:
            sym = pos.symbol
            qty = int(float(pos.qty))
            if qty == 0:
                continue

            entry_info = self._entry_log.get(sym)
            is_margin_force = sym in margin_force_syms
            if entry_info and entry_info.get("long_term_hold") and not is_margin_force:
                log.info(f"EOD HOLD {sym}: long-term hold policy active")
                continue
            if not EOD_CLOSE_ALL and not is_margin_force:
                if not entry_info:
                    continue
                if entry_info.get("date") != today:
                    continue
                if entry_info.get("strategy") not in EOD_CLOSE_STRATEGIES:
                    continue

            try:
                # Cancel open orders before closing so broker-reserved shares are
                # available for the EOD market order.
                try:
                    sym_orders = [
                        o for o in (self.client.get_orders() or [])
                        if o.symbol == sym
                    ]
                    for _o in sym_orders:
                        try:
                            self.client.cancel_order_by_id(str(_o.id))
                        except Exception:
                            pass
                    if sym_orders:
                        time.sleep(0.4)
                except Exception:
                    pass

                side = OrderSide.SELL if qty > 0 else OrderSide.BUY
                if regular_hours:
                    req = MarketOrderRequest(
                        symbol=sym, qty=abs(qty),
                        side=side, time_in_force=TimeInForce.DAY,
                    )
                else:
                    current_price = float(getattr(pos, "current_price", 0) or 0)
                    limit_price = round(
                        current_price * (1 - EOD_AFTERHOURS_LIMIT_BUFFER_PCT / 100)
                        if qty > 0 else current_price * (1 + EOD_AFTERHOURS_LIMIT_BUFFER_PCT / 100),
                        2,
                    )
                    if limit_price <= 0:
                        raise ValueError("no usable after-hours position mark")
                    req = LimitOrderRequest(
                        symbol=sym, qty=abs(qty), side=side,
                        limit_price=limit_price,
                        time_in_force=TimeInForce.DAY,
                        extended_hours=True,
                    )
                self.client.submit_order(req)
                exit_reason = "MARGIN_EOD_CLOSE" if is_margin_force else "EOD_CLOSE"
                self._record_probe_outcome(sym, pos, exit_reason)
                self._entry_log.pop(sym, None)
                self._tp_targets.pop(sym, None)
                self._intermediate_targets.pop(sym, None)
                self._tightened.discard(sym)
                self._peak_price.pop(sym, None)

                pnl = float(pos.unrealized_pl)
                strategy = entry_info.get("strategy", "unknown") if entry_info else "unknown"
                closed_items.append({
                    "symbol": sym,
                    "qty": abs(qty),
                    "strategy": strategy,
                    "pnl": pnl,
                })

                order_style = "market" if regular_hours else f"extended-hours limit ${limit_price:.2f}"
                label = "MARGIN EOD CLOSE" if is_margin_force else "EOD CLOSE"
                log.info(
                    f"{label} {sym}: {abs(qty)} shares | {order_style} | "
                    f"strategy={strategy} | P&L ${pnl:.2f}"
                )
            except Exception as e:
                failed_items.append({"symbol": sym, "error": str(e)})
                log.error(f"EOD close failed {sym}: {e}")

        if not failed_items:
            self._eod_close_done = today
        self._save_exit_state()

        summary = {
            "date": today.isoformat(),
            "closed_count": len(closed_items),
            "failed_count": len(failed_items),
            "closed_items": closed_items,
            "failed_items": failed_items,
            "asof": now_et.isoformat(),
        }
        return summary

    # ── Kill Mode: Emergency Close All ───────────────────────────────────────
    def emergency_close_all(self, equity: float) -> None:
        """
        Kill mode emergency exit. Closes every open position as safely as possible.

        PDT rules (equity < $25k):
          - Positions opened on a PRIOR day → cancel any open orders then market-close.
            These are NOT day trades so no PDT count is consumed.
          - Positions opened TODAY → cannot close without a day-trade violation.
            Instead, a hairpin trailing stop of KILL_MODE_TRAIL_PCT (0.5%) is placed
            so the position exits automatically within minutes via the stop engine.

        PDT-exempt (equity >= $25k): cancel all open orders + market-close everything.
        """
        import time as _t

        pdt_exempt = equity >= PDT_ACCOUNT_MIN
        today      = datetime.date.today()

        try:
            positions   = self.client.get_all_positions()
            open_orders = self.client.get_orders()
        except Exception as e:
            log.error(f"KILL MODE: failed to fetch data: {e}")
            return

        orders_by_sym: dict = {}
        for o in open_orders:
            orders_by_sym.setdefault(o.symbol, []).append(o)

        closed: list    = []
        protected: list = []

        for pos in positions:
            sym = pos.symbol
            qty = int(float(pos.qty))
            if qty == 0:
                continue

            entry_date = self._entry_log.get(sym, {}).get("date")
            is_today   = entry_date == today

            if not pdt_exempt and is_today:
                # Today's position — tighten trailing stop to hairpin; do NOT market-close
                for o in orders_by_sym.get(sym, []):
                    try:
                        self.client.cancel_order_by_id(str(o.id))
                    except Exception:
                        pass
                _t.sleep(0.3)
                try:
                    stop_side = OrderSide.SELL if qty > 0 else OrderSide.BUY
                    self.client.submit_order(TrailingStopOrderRequest(
                        symbol        = sym,
                        qty           = abs(qty),
                        side          = stop_side,
                        type          = AlpacaOrderType.TRAILING_STOP,
                        time_in_force = TimeInForce.GTC,
                        trail_percent = KILL_MODE_TRAIL_PCT,
                    ))
                    cur = float(pos.current_price or 0)
                    log.warning(
                        f"KILL MODE [PDT-SAFE] {sym}: hairpin trailing stop "
                        f"{KILL_MODE_TRAIL_PCT}% @ ${cur:.2f} "
                        f"(opened today — closing via stop to avoid PDT violation)"
                    )
                    protected.append(sym)
                except Exception as e:
                    log.error(f"KILL MODE: hairpin stop failed {sym}: {e}")
                continue

            # Prior-day position (or PDT-exempt): cancel standing orders, then market-close
            for o in orders_by_sym.get(sym, []):
                try:
                    self.client.cancel_order_by_id(str(o.id))
                except Exception:
                    pass
            _t.sleep(0.3)

            try:
                side = OrderSide.SELL if qty > 0 else OrderSide.BUY
                self.client.submit_order(MarketOrderRequest(
                    symbol        = sym,
                    qty           = abs(qty),
                    side          = side,
                    time_in_force = TimeInForce.DAY,
                ))
                pnl = float(pos.unrealized_pl or 0)
                log.warning(
                    f"KILL MODE CLOSE {sym}: {abs(qty)} shares "
                    f"{'SELL' if qty > 0 else 'BUY-TO-COVER'} | unrealized ${pnl:+.2f}"
                )
                closed.append(sym)
            except Exception as e:
                log.error(f"KILL MODE: close failed {sym}: {e}")

        log.warning(
            f"KILL MODE COMPLETE — "
            f"market-closed: {len(closed)} {closed} | "
            f"hairpin stops (PDT-safe): {len(protected)} {protected}"
        )

    # ── Stale Order Updater ───────────────────────────────────────────────────
    def update_stale_orders(self) -> None:
        """
        Find open orders older than STALE_ORDER_MINUTES and re-submit them:
          - Regular hours   → cancel + market order (instant fill)
          - Extended hours  → cancel + limit order at current price (IOC)
        Only applies to entry/exit orders (buy/sell), not bracket legs (stop/limit TP-SL).
        Also resets _swap_cycle_closed so each scan cycle starts fresh.
        """
        import time
        self._swap_cycle_closed.clear()  # reset per-cycle swap dedup
        try:
            open_orders = self.client.get_orders()
        except Exception as e:
            log.warning(f"update_stale_orders: fetch failed: {e}")
            return

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        regular = self._current_market_state().is_regular_hours

        for order in open_orders:
            # Only handle plain entry/exit orders, not bracket legs or protective stops
            order_type = getattr(order, "order_type", "") or ""
            order_class = str(getattr(order, "order_class", "") or "")
            if order_class in ("bracket", "oco"):
                continue
            # Never cancel GTC trailing stop orders — they are protective stops,
            # not stale entry orders.  Killing them leaves positions unprotected.
            if "trailing_stop" in str(order_type).lower():
                continue

            created_at = getattr(order, "created_at", None)
            if created_at is None:
                continue

            # Pick timeout: intraday strategies use short cutoff to avoid lunchtime fills
            coid = str(getattr(order, "client_order_id", "") or "")
            is_intraday = False
            if coid.startswith("apex-"):
                parts = coid.split("-", 2)   # ["apex", strategy, symbol]
                if len(parts) >= 2 and parts[1] in EOD_CLOSE_STRATEGIES:
                    is_intraday = True
            cutoff_secs = (STALE_ORDER_MINUTES_INTRADAY if is_intraday else STALE_ORDER_MINUTES) * 60

            age_secs = (now_utc - created_at).total_seconds()
            if age_secs < cutoff_secs:
                continue

            sym = order.symbol
            qty = int(float(order.qty))
            side = order.side  # OrderSide enum
            order_id = str(order.id)

            log.info(
                f"STALE ORDER: {sym} {side} {qty} — age {age_secs/60:.1f}m "
                f"(cutoff {'intraday 30m' if is_intraday else '6h'}) "
                f"→ {'market' if regular else 'limit @ current price'}"
            )

            try:
                self.client.cancel_order_by_id(order_id)
                time.sleep(0.3)

                if regular:
                    # If the original was a limit buy and the limit was more than 1%
                    # below the current ask, the order was defensive/passive — don't
                    # blast it to market (bad fill); just cancel and let the next
                    # scan cycle re-evaluate.
                    orig_limit = float(getattr(order, "limit_price", None) or 0)
                    if orig_limit > 0 and str(order_type).lower() == "limit":
                        try:
                            quote = self.client.get_latest_quote(sym)
                            cur_ask = float(getattr(quote, "ask_price", orig_limit))
                        except Exception:
                            cur_ask = orig_limit
                        if cur_ask > 0 and orig_limit < cur_ask * 0.99:
                            log.info(
                                f"STALE ORDER {sym}: limit ${orig_limit:.2f} is defensive "
                                f"(ask=${cur_ask:.2f}) — cancelling without re-entry"
                            )
                            continue  # skip re-submit; cancelled above

                    req = MarketOrderRequest(
                        symbol=sym, qty=qty, side=side,
                        time_in_force=TimeInForce.DAY,
                    )
                else:
                    # Best-effort limit at current price for extended hours
                    try:
                        bar = self.client.get_latest_quote(sym)
                        cur_price = round(
                            (float(bar.ask_price) + float(bar.bid_price)) / 2, 2
                        )
                    except Exception:
                        cur_price = float(getattr(order, "limit_price", None) or 0)
                    if cur_price <= 0:
                        log.warning(f"STALE ORDER {sym}: can't determine price, skipping")
                        continue
                    req = LimitOrderRequest(
                        symbol=sym, qty=qty, side=side,
                        limit_price=cur_price,
                        time_in_force=TimeInForce.DAY,
                        extended_hours=True,
                    )

                self.client.submit_order(req)
                log.info(f"STALE ORDER {sym}: replaced successfully")
            except Exception as e:
                log.warning(f"STALE ORDER {sym}: replace failed: {e}")

    # ── ATR Take-Profit Checker ────────────────────────────────────────────────
    def check_tp_targets(self) -> None:
        """Dual-phase profit exit:
          Phase 1 (+TP_INTERMEDIATE_PCT, default +5%): cancel wide trail, place TP_INTERMEDIATE_TRAIL_PCT% trail
          Phase 2 (+TP_FINAL_PCT, default +10%): close full position at market
        Called once per scan cycle.
        """
        if not self._tp_targets and not self._intermediate_targets:
            return
        try:
            positions = {p.symbol: p for p in self.client.get_all_positions()}
        except Exception as e:
            log.warning(f"check_tp_targets: fetch failed: {e}")
            return

        all_syms = set(self._tp_targets) | set(self._intermediate_targets)
        to_clean = []
        state_changed = False

        for sym in list(all_syms):
            if self._entry_log.get(sym, {}).get("long_term_hold"):
                continue
            pos = positions.get(sym)
            if pos is None:
                to_clean.append(sym)
                continue
            qty = int(float(pos.qty))
            if qty == 0:
                to_clean.append(sym)
                continue
            cur = float(getattr(pos, "current_price", 0) or 0)
            if cur <= 0:
                continue
            is_long = qty > 0

            # ── Phase 2: profit exit (ratchet or legacy fixed target) ───────────
            final_tp = self._tp_targets.get(sym)
            if final_tp is not None:
                should_close = False
                close_log    = ""
                if TP_RATCHET_ENABLED:
                    # Peak-ratchet: let winners run; exit only on giveback from peak.
                    entry_px = float(getattr(pos, "avg_entry_price", 0) or 0)
                    prev_peak = self._peak_price.get(sym)
                    peak = cur if prev_peak is None else (
                        max(prev_peak, cur) if is_long else min(prev_peak, cur))
                    if peak != prev_peak:
                        self._peak_price[sym] = peak
                        state_changed = True
                    gain_pct = ((cur - entry_px) / entry_px * 100) if entry_px > 0 else 0.0
                    if not is_long:
                        gain_pct = -gain_pct
                    if gain_pct >= TP_RATCHET_ARM_PCT:
                        trigger = (peak * (1 - TP_RATCHET_GIVEBACK_PCT / 100) if is_long
                                   else peak * (1 + TP_RATCHET_GIVEBACK_PCT / 100))
                        if (is_long and cur <= trigger) or (not is_long and cur >= trigger):
                            should_close = True
                            peak_gain = ((peak - entry_px) / entry_px * 100) if entry_px > 0 else 0.0
                            if not is_long:
                                peak_gain = -peak_gain
                            close_log = (
                                f"RATCHET EXIT {sym}: ${cur:.2f} gave back "
                                f"{TP_RATCHET_GIVEBACK_PCT:.0f}% from peak ${peak:.2f} "
                                f"(peak +{peak_gain:.1f}%, exit +{gain_pct:.1f}%) → market close"
                            )
                else:
                    if (is_long and cur >= final_tp) or (not is_long and cur <= final_tp):
                        should_close = True
                        close_log = (
                            f"TP CLOSE {sym}: ${cur:.2f} hit +{TP_FINAL_PCT:.0f}% target "
                            f"${final_tp:.2f} → market close (full position)"
                        )

                if should_close:
                    try:
                        # Cancel any standing protective order first — otherwise the
                        # shares stay "held_for_orders" and the market close is rejected.
                        try:
                            sym_orders = [o for o in (self.client.get_orders() or []) if o.symbol == sym]
                            for _o in sym_orders:
                                try:
                                    self.client.cancel_order_by_id(str(_o.id))
                                except Exception:
                                    pass
                            if sym_orders:
                                time.sleep(0.4)
                        except Exception:
                            pass
                        side = OrderSide.SELL if is_long else OrderSide.BUY
                        self.client.submit_order(MarketOrderRequest(
                            symbol=sym, qty=abs(qty), side=side,
                            time_in_force=TimeInForce.DAY,
                        ))
                        self._record_probe_outcome(sym, pos, "TP_CLOSE")
                        log.info(close_log)
                        to_clean.append(sym)
                    except Exception as e:
                        log.warning(f"TP close failed {sym}: {e}")
                    continue  # closed — skip phase-1

                if TP_RATCHET_ENABLED:
                    continue  # ratchet governs the winner; skip legacy phase-1 tighten

            # ── Phase 1: tighten trail (legacy; skipped when ratchet enabled) ────
            if sym in self._tightened:
                continue  # already tightened
            int_tp = self._intermediate_targets.get(sym)
            if int_tp is None:
                continue
            hit1 = (is_long and cur >= int_tp) or (not is_long and cur <= int_tp)
            if not hit1:
                continue

            # Cancel existing wide trailing stop
            stop_side = OrderSide.SELL if is_long else OrderSide.BUY
            try:
                open_orders = self.client.get_orders() or []
                for o in open_orders:
                    if (o.symbol == sym
                            and str(getattr(o, "type", "")).lower() == "trailing_stop"
                            and str(getattr(o, "time_in_force", "")).upper() == "GTC"):
                        try:
                            self.client.cancel_order_by_id(str(o.id))
                        except Exception:
                            pass
            except Exception:
                pass

            # Place tight trail
            try:
                self.client.submit_order(TrailingStopOrderRequest(
                    symbol=sym, qty=abs(qty), side=stop_side,
                    type=AlpacaOrderType.TRAILING_STOP,
                    time_in_force=TimeInForce.GTC,
                    trail_percent=TP_INTERMEDIATE_TRAIL_PCT,
                ))
                log.info(
                    f"TP TIGHTEN {sym}: ${cur:.2f} hit +{TP_INTERMEDIATE_PCT:.0f}% "
                    f"→ trail tightened to {TP_INTERMEDIATE_TRAIL_PCT:.0f}% (locks in ~+{TP_INTERMEDIATE_PCT - TP_INTERMEDIATE_TRAIL_PCT:.0f}%)"
                )
                self._tightened.add(sym)
                self._intermediate_targets.pop(sym, None)  # phase 1 done
                state_changed = True
            except Exception as e:
                log.warning(f"TP tighten trail failed {sym}: {e}")

        for sym in to_clean:
            self._tp_targets.pop(sym, None)
            self._intermediate_targets.pop(sym, None)
            self._tightened.discard(sym)
            self._peak_price.pop(sym, None)
            state_changed = True

        if state_changed:
            self._save_exit_state()

    def check_dead_money(self) -> None:
        """Close positions still moving adversely after DEAD_MONEY_MINUTES.
        A profitable or recovering position remains managed by its trailing stop and TP targets.
        """
        if not self._entry_log:
            return
        now = datetime.datetime.now()
        try:
            positions = {p.symbol: p for p in self.client.get_all_positions()}
        except Exception as e:
            log.warning(f"check_dead_money: fetch failed: {e}")
            return

        state_changed = False
        for sym, info in list(self._entry_log.items()):
            if info.get("long_term_hold"):
                continue
            entry_time = info.get("entry_time")
            if entry_time is None:
                continue
            elapsed_min = (now - entry_time).total_seconds() / 60
            time_stop_minutes = (
                ORB["time_stop_minutes"]
                if info.get("strategy") == "ORB"
                else DEAD_MONEY_MINUTES
            )
            if elapsed_min < time_stop_minutes:
                continue

            pos = positions.get(sym)
            if pos is None:
                self._entry_log.pop(sym, None)
                self._tp_targets.pop(sym, None)
                self._intermediate_targets.pop(sym, None)
                self._tightened.discard(sym)
                self._peak_price.pop(sym, None)
                state_changed = True
                continue
            qty = int(float(pos.qty))
            if qty == 0:
                continue

            entry_price = float(info.get("entry_price") or 0)
            current_price = float(getattr(pos, "current_price", 0) or 0)
            if entry_price <= 0 or current_price <= 0:
                log.warning(f"DEAD MONEY {sym}: missing entry or current price; skipping drift check")
                continue

            price_change_pct = (current_price - entry_price) / entry_price * 100
            adverse_drift_pct = -price_change_pct if qty > 0 else price_change_pct
            if info.get("strategy") == "ORB" and abs(price_change_pct) <= ORB["flat_max_gain_pct"]:
                adverse_threshold_pct = -1.0
            else:
                adverse_threshold_pct = None
            atr_stop = float(info.get("atr_stop") or 0)
            if adverse_threshold_pct is None and atr_stop > 0 and ATR_STOP_MULTIPLIER > 0:
                atr_pct = atr_stop / ATR_STOP_MULTIPLIER / entry_price * 100
                adverse_threshold_pct = min(
                    TIME_LOSS_ATR_MAX_PCT,
                    max(TIME_LOSS_ATR_MIN_PCT, atr_pct * TIME_LOSS_ATR_MULTIPLIER),
                )
            elif adverse_threshold_pct is None:
                adverse_threshold_pct = DEAD_MONEY_MAX_ADVERSE_DRIFT_PCT

            if adverse_drift_pct < adverse_threshold_pct:
                continue

            try:
                # Cancel any standing protective order first — otherwise the
                # shares stay "held_for_orders" and the market close is rejected.
                try:
                    sym_orders = [o for o in (self.client.get_orders() or []) if o.symbol == sym]
                    for _o in sym_orders:
                        try:
                            self.client.cancel_order_by_id(str(_o.id))
                        except Exception:
                            pass
                    if sym_orders:
                        time.sleep(0.4)
                except Exception:
                    pass
                side = OrderSide.SELL if qty > 0 else OrderSide.BUY
                self.client.submit_order(MarketOrderRequest(
                    symbol=sym, qty=abs(qty), side=side,
                    time_in_force=TimeInForce.DAY,
                ))
                self._record_probe_outcome(sym, pos, "TIME_LOSS")
                log.info(
                    f"TIME LOSS {sym}: held {elapsed_min:.0f} min, adverse drift {adverse_drift_pct:.2f}% "
                    f">= {adverse_threshold_pct:.2f}% → closing"
                )
                self._entry_log.pop(sym, None)
                self._tp_targets.pop(sym, None)
                self._intermediate_targets.pop(sym, None)
                self._tightened.discard(sym)
                self._peak_price.pop(sym, None)
                state_changed = True
            except Exception as e:
                log.warning(f"Dead money close failed {sym}: {e}")

        if state_changed:
            self._save_exit_state()

    # ── Health ─────────────────────────────────────────────────────────────────
    def get_health(self) -> Dict:
        try:
            acct = self._get_account(force_refresh=True)
            dt_left = self.pdt.remaining(acct.equity, acct.daytrade_count)
            return {
                "equity":           acct.equity,
                "cash":             acct.buying_power,
                "buying_power":     acct.buying_power,
                "pdt_protected":    acct.equity >= PDT_ACCOUNT_MIN,
                "day_trade_count":  acct.daytrade_count,
                "day_trades_left":  dt_left,
            }
        except Exception as e:
            log.error(f"Health check error: {e}")
            return {}
