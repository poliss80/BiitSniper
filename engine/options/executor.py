import threading
import time
"""
ApexTrader - Options Executor (Level 3 Account / Alpaca)
Manages opening, monitoring, and closing options positions via the
Alpaca trading API.

Responsibilities:
  - Enforce 15% portfolio allocation cap across all open options
  - Size each trade (number of contracts) within allocation budget
  - Place buy_to_open (calls/puts) and sell_to_open (covered calls) orders
  - Monitor open options P&L, close at profit target (+50%) or stop (-40%)
  - Cancel expired or near-expiry contracts (DTE <= 1)
"""

import logging
import datetime
import time
from typing import List, Dict, Tuple, Optional
from collections import defaultdict
from dataclasses import dataclass, field
import json
import re
from alpaca.trading.client import TradingClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce, QueryOrderStatus

# Set to True when Alpaca returns 40310000 (liquidation-only restriction).
# Prevents repeated failed order attempts until the bot is restarted or the
# restriction is cleared (checked fresh each cycle via _is_account_tradeable).
_ACCOUNT_RESTRICTED = False

from engine.config import (
    OPTIONS_ENABLED,
    OPTIONS_ALLOCATION_PCT,
    OPTIONS_MAX_POSITIONS,
    MAX_POSITIONS,
    OPTIONS_MAX_MLEG_POSITIONS,
    OPTIONS_MAX_MLEG_CONTRACTS,
    OPTIONS_PROFIT_TARGET_PCT,
    OPTIONS_PROFIT_TARGET_1_PCT,
    OPTIONS_PROFIT_TARGET_1_STOP_PCT,
    OPTIONS_PROFIT_TARGET_2_PCT,
    OPTIONS_STOP_LOSS_PCT,
    OPTIONS_DTE_MIN,
    OPTIONS_ENTRY_GRACE_DAYS,
    PDT_ACCOUNT_MIN, PDT_MAX_TRADES, PDT_OPTIONS_DAY_TRADE_RESERVE,
    OPTIONS_THETA_EXIT_DTE,
    OPTIONS_TRAIL_ACTIVATE_PCT,
    OPTIONS_TRAIL_DRAWDOWN_PCT,
        OPTIONS_LIVE_PROBE_MODE,
        OPTIONS_LIVE_PROBE_STOP_LOSS_PCT,
    API_KEY, API_SECRET, PAPER,
)
from engine.utils import MarketState
from .strategies import OptionSignal, CONTRACT_SIZE, record_stop_cooldown

log = logging.getLogger("ApexTrader.Options")


# ── Helpers ───────────────────────────────────────────────────────────────────

import math as _math

def _bs_option_price(spot: float, strike: float, dte: int, iv: float, call: bool = True) -> float:
    """Thin Black-Scholes pricer used to estimate short-leg credit on auto-derived spreads."""
    T = dte / 365.0
    if T <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return max(0.0, (spot - strike) if call else (strike - spot))
    try:
        d1 = (_math.log(spot / strike) + (0.05 + 0.5 * iv * iv) * T) / (iv * _math.sqrt(T))
        d2 = d1 - iv * _math.sqrt(T)
        def _n(x):
            a = abs(x)
            t = 1.0 / (1.0 + 0.2316419 * a)
            p = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
            r = 1.0 - (1 / _math.sqrt(2 * _math.pi)) * _math.exp(-0.5 * a * a) * p
            return r if x >= 0 else 1.0 - r
        if call:
            return max(0.0, spot * _n(d1) - strike * _math.exp(-0.05 * T) * _n(d2))
        else:
            return max(0.0, strike * _math.exp(-0.05 * T) * _n(-d2) - spot * _n(-d1))
    except Exception:
        return 0.0


def _alpaca_option_symbol(symbol: str, expiry: datetime.date, option_type: str, strike: float) -> str:
    """Build the OCC option symbol used by Alpaca.
    Format: <underlying><YYMMDD><C|P><8-digit-strike-in-thousandths>
    e.g. AAPL260418C00185000
    """
    exp_str    = expiry.strftime("%y%m%d")
    cp         = "C" if option_type.lower() == "call" else "P"
    strike_int = int(round(strike * 1000))
    return f"{symbol}{exp_str}{cp}{strike_int:08d}"


def _extract_strike_from_occ_symbol(occ_symbol: str) -> Optional[float]:
    """Extract strike price from OCC option symbol.
    
    OCC format: <Ticker><YYMMDD><C|P><8-digit-strike>
    Example: BAC260515P00049000 → 49.00
    
    Returns: strike price, or None if parsing fails
    """
    try:
        m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", occ_symbol)
        if m:
            strike_str = m.group(4)
            return int(strike_str) / 1000.0
    except Exception:
        pass
    return None


def calculate_position_size_pct(
    confidence: float,
    rr_ratio: float,
    iv_rank: float,
    base_pct: float = 0.025,
) -> float:
    """Calculate position size as % of portfolio based on setup quality (Phase 2 optimization).
    
    Allocates capital proportionally to setup quality:
    - High confidence (0.88+): up to 4.5% per position (earn for quality)
    - Mid confidence (0.80-0.87): 2.0-3.0% (standard)
    - Low confidence (0.76-0.79): 1.5-2.0% (avoid large capital loss on marginal entries)
    
    Args:
        confidence: Signal confidence (0.76–0.95)
        rr_ratio: Risk/reward ratio (1.0–3.0+)
        iv_rank: IV percentile rank (0–100)
        base_pct: Base allocation (default 2.5%)
    
    Returns:
        Position size as % of portfolio, clamped to [1.5%, 5.0%]
    
    Examples:
        >>> calculate_position_size_pct(0.89, 2.0, 30)  # High quality spread
        0.0425  # 4.25% allocation
        >>> calculate_position_size_pct(0.78, 1.2, 50)  # Marginal setup
        0.020   # 2.0% allocation
    """
    # Confidence multiplier: 0.76→1.0x, 0.95→1.475x
    confidence_capped = min(confidence, 0.95)
    conf_mult = 1.0 + (confidence_capped - 0.76) * 2.5
    
    # R/R multiplier: 1.0x→1.0x, 2.0x→1.2x, 3.0x→1.4x
    # Higher R/R justifies larger position (we're getting paid for the risk)
    rr_capped = min(rr_ratio, 3.0)
    rr_mult = 1.0 + (rr_capped - 1.0) * 0.2
    
    # IV penalty: 0 IV→1.0x, 50 IV→0.85x, 100 IV→0.7x
    # High IV (buying expensive premium) warrants smaller position
    iv_mult = max(0.7, 1.0 - (iv_rank / 100.0) * 0.3)
    
    # Calculate final size
    size = base_pct * conf_mult * rr_mult * iv_mult
    
    # Clamp to realistic bounds: 1.5% minimum (single lottery ticket)
    # to 5.0% maximum (don't get knocked out by one bad trade)
    return max(0.015, min(0.05, size))


def get_tiered_profit_targets(confidence: float) -> Dict[str, float]:
    """Return tiered profit-taking targets based on signal confidence — AGGRESSIVE no-PDT mode.
    
    Aggressive profit-taking without PDT concerns: book profits fast.
    Confidence bands:
    - ≥0.88: High conviction — moderate hold (targets: +25%, +40%, +60%) [was: +30%, +60%, +100%]
    - 0.80-0.87: Standard — balanced (targets: +20%, +35%, +50%) [was: +25%, +60%]
    - <0.80: Conservative — lock in fast (targets: +15%, +30%, +40%) [was: +20%, +50%]
    
    Returns dict with keys:
        - 'tier1_target': First profit level (% to close 50%)
        - 'tier1_close_pct': How much to close at tier1 (0.5 = 50%)
        - 'tier2_target': Second profit level (% to close rest)
        - 'trail_activate_pct': When trailing stop arms
        - 'trail_drawdown_pct': Drawdown threshold for trailing
    """
    if confidence >= 0.88:
        # High conviction: still moderate but book profits faster
        return {
            'tier1_target': 25.0,        # was 30% — primary exit at 25%
            'tier1_close_pct': 0.50,     # Close 50% at tier 1 (was 25%, now more aggressive)
            'tier2_target': 40.0,        # was 60% — secondary exit at 40%
            'tier2_close_pct': 0.50,     # Close remaining 50%
            'trail_activate_pct': 50.0,  # was 100% — arm trailing at +50% (aggressive)
            'trail_drawdown_pct': 12.0,  # was 15pp — tight trailing (12pp drawdown closes)
        }
    elif confidence >= 0.80:
        # Standard quality: balanced but book faster
        return {
            'tier1_target': 20.0,        # was 25% — aggressive entry exit
            'tier1_close_pct': 0.50,     # Close 50% at tier 1 (standard)
            'tier2_target': 35.0,        # was 60% — secondary exit much lower (aggressive)
            'tier2_close_pct': 0.50,     # Close remaining 50%
            'trail_activate_pct': 30.0,  # was 60% — arm trailing at +30% (fast)
            'trail_drawdown_pct': 10.0,  # was 20pp — very tight trailing (10pp closes position)
        }
    else:
        # Conservative/marginal: lock in early & fast
        return {
            'tier1_target': 15.0,        # was 20% — quick first exit
            'tier1_close_pct': 0.60,     # Close 60% at tier 1 (more aggressive than before)
            'tier2_target': 30.0,        # was 50% — much lower secondary target
            'tier2_close_pct': 0.40,     # Close remaining 40%
            'trail_activate_pct': 20.0,  # was 25% — arm early at +20%
            'trail_drawdown_pct': 8.0,   # was 25pp — very tight trailing (8pp closes)
        }


@dataclass
class OptionsPosition:
    """Tracked open options position."""
    occ_symbol:  str               # primary leg OCC symbol (key in _positions dict)
    symbol:      str               # underlying ticker
    option_type: str
    action:      str               # 'buy_to_open' or 'sell_to_open'
    strike:      float             # primary leg strike
    expiry:      datetime.date
    contracts:   int
    entry_price: float             # signed net value at entry: positive = net debit paid,
                                   # negative = net credit received (per share)
    strategy:    str
    entered_at:  datetime.date = field(default_factory=datetime.date.today)
    peak_pnl_pct: float = 0.0     # highest observed P&L % (trailing stop reference)
    # All legs stored at entry — enables strategy-agnostic close and P&L calculation.
    # Each entry: {"occ_symbol": str, "side": "buy"|"sell", "ratio_qty": int}
    legs: list = field(default_factory=list)
    # Open-window / IV-gate fields
    entry_iv:      float = 0.0   # IV% at entry (0-1 scale from yfinance/Alpaca)
    is_naked:      bool  = False  # True = single naked leg, eligible for spread conversion
    open_stop_pct: float = 0.0   # Per-position hard stop override (25% open window, 0 = use global)
    # Butterfly break-even mode: once mark goes negative, lower exit target to 0%
    # (recover original debit) instead of waiting for +45%. Latches True, never resets.
    breakeven_mode: bool = False
    # Tiered profit taking: track if first tier (50%) was closed at +50%
    tier1_closed: bool = False    # True = 50% closed at +50%, hold 50% with new +20% stop for 2nd half
    scaled_out_qty: int = 0       # Number of contracts closed at tier1 target
    # Phase 2: Signal confidence at entry (for confidence-tiered profit targets)
    entry_confidence: float = 0.80  # Confidence score at entry (default 0.80)
    is_live_probe: bool = False
    probe_scale_in_pending: bool = False
    probe_scaled_in: bool = False




class OptionsExecutor:
    """Manages options positions within a 15% portfolio allocation."""

    # Adaptive limit order retry config
    _ORDER_RETRY_TIMEOUT = 180  # seconds (3 min)
    _ORDER_MAX_RETRIES = 2
    _ORDER_RETRY_STEP = 0.01    # 1% more aggressive each retry

    def _adaptive_limit_retry(self, order_id, side, orig_limit, symbol, contracts, occ_sym, is_mleg, payload, retry_count=0):
        """After timeout, cancel and resubmit limit order at more aggressive price, up to max retries."""
        log.info(f"[OPTIONS][RETRY] Started retry thread for {symbol} order {order_id} (retry {retry_count})")
        if retry_count >= self._ORDER_MAX_RETRIES:
            log.info(f"[OPTIONS][RETRY] Max retries reached for {symbol} order {order_id}, giving up.")
            return
        log.info(f"[OPTIONS][RETRY] Waiting {self._ORDER_RETRY_TIMEOUT}s before checking order {order_id} for {symbol}")
        time.sleep(self._ORDER_RETRY_TIMEOUT)
        # Check if order is still open
        open_orders = self.client.get_orders()
        log.debug(f"[OPTIONS][RETRY] Open orders after timeout: {[getattr(o, 'id', None) for o in open_orders]}")
        order = next((o for o in open_orders if getattr(o, "id", None) == order_id), None)
        if order is None:
            log.info(f"[OPTIONS][RETRY] Order {order_id} for {symbol} not found (likely filled or canceled externally), no retry needed.")
            return
        try:
            filled_qty = int(float(getattr(order, "filled_qty", 0) or 0))
        except (TypeError, ValueError):
            filled_qty = 0
            log.warning(f"[OPTIONS][RETRY] Invalid filled quantity for {symbol} order {order_id}; treating as unfilled")
        log.info(f"[OPTIONS][RETRY] Order {order_id} for {symbol} still open after timeout. Filled qty: {filled_qty}, Contracts: {contracts}")
        if filled_qty >= contracts:
            log.info(f"[OPTIONS][RETRY] Order {order_id} for {symbol} fully filled, no retry needed.")
            return
        # Cancel and resubmit at more aggressive price
        log.info(f"[OPTIONS][RETRY] Cancelling order {order_id} for {symbol} (unfilled qty: {contracts - filled_qty})")
        self.client.cancel_order_by_id(order_id)
        if side == "buy":
            new_limit = round(orig_limit * (1 + self._ORDER_RETRY_STEP * (retry_count + 1)), 2)
        else:
            new_limit = round(orig_limit * (1 - self._ORDER_RETRY_STEP * (retry_count + 1)), 2)
        log.info(f"[OPTIONS][RETRY] Retrying {symbol} order at more aggressive limit: {new_limit} (retry {retry_count+1})")
        if is_mleg:
            payload["limit_price"] = str(new_limit)
            resp = self.client.post("/orders", payload)
            new_order_id = getattr(resp, "id", None)
            log.info(f"[OPTIONS][RETRY] Submitted new MLEG order for {symbol}: {new_order_id}")
            if new_order_id:
                threading.Thread(target=self._adaptive_limit_retry, args=(new_order_id, side, new_limit, symbol, contracts, occ_sym, is_mleg, payload, retry_count+1), daemon=True).start()
        else:
            order_req = LimitOrderRequest(
                symbol=occ_sym,
                qty=contracts,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                limit_price=new_limit,
                time_in_force=TimeInForce.DAY
            )
            resp = self.client.submit_order(order_req)
            new_order_id = getattr(resp, "id", None)
            log.info(f"[OPTIONS][RETRY] Submitted new SINGLE order for {symbol}: {new_order_id}")
            if new_order_id:
                threading.Thread(target=self._adaptive_limit_retry, args=(new_order_id, side, new_limit, symbol, contracts, occ_sym, is_mleg, payload, retry_count+1), daemon=True).start()

    def _calculate_gross_notional(self, extra: Optional[dict] = None) -> float:
        """
        Calculate the gross notional value of all open option positions, plus an optional
        extra (pending) position.  Uses option premium cost (mid_price × 100), not the
        underlying strike notional, so the cap is proportional to actual capital at risk.

        Existing positions: abs(entry_price) × contracts × 100
        Pending order:      mid_price × contracts × n_legs × 100
        """
        total = 0.0
        # Existing positions — use abs(entry_price) which is the premium paid/received per share
        for pos in self._positions.values():
            n_legs = max(1, len(pos.legs))
            total += abs(pos.entry_price) * abs(pos.contracts) * n_legs * 100
        # Add pending order if provided
        if extra:
            mid = abs(extra.get("mid_price", 0.0))
            n_legs = max(1, len(extra.get("legs", [])))
            total += mid * abs(extra.get("contracts", 1)) * n_legs * 100
        return total

    def __init__(self, client: TradingClient):
        self.client = client
        self.data_client = OptionHistoricalDataClient(API_KEY, API_SECRET)
        self._positions: Dict[str, OptionsPosition] = {}   # occ_symbol -> OptionsPosition
        self._last_monitor_ts: float = 0.0
        self._MONITOR_INTERVAL = 20   # seconds between P&L checks (fast enough to catch intraday moves)
        self._last_iv_convert_ts: float = 0.0
        self._IV_CONVERT_INTERVAL = 600.0  # check spread-conversion every 10 min max
        self._reconcile_positions()

    def _reconcile_positions(self) -> None:
        """On startup, re-hydrate _positions from any open options already held in the account.
        Uses avg_entry_price from Alpaca and conservative defaults (global stop/target, no
        open-window flag).  This allows monitor_positions() to manage positions entered in a
        previous session or in paper mode without having to track them from entry.

        Multi-leg detection: groups legs by (ticker, expiry) and reconstructs butterfly
        and iron-condor structures so that P&L and close orders are computed correctly
        as a net position rather than per individual leg.
        """
        try:
            open_pos = [p for p in self.client.get_all_positions()
                        if getattr(p, "asset_class", "") == "us_option"]
        except Exception as e:
            log.warning(f"[OPTIONS] Reconcile: could not fetch positions: {e}")
            return

        if not open_pos:
            return

        today = datetime.date.today()

        # ── Step 1: parse every option position into a plain dict ────────────
        parsed: List[dict] = []
        for p in open_pos:
            occ = p.symbol
            if occ in self._positions:
                continue  # already tracked (entered this session before restart)
            m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", occ)
            if not m:
                continue
            ticker, exp_str, cp_char, strike_str = m.groups()
            try:
                expiry   = datetime.datetime.strptime(exp_str, "%y%m%d").date()
                strike   = int(strike_str) / 1000.0
                opt_type = "call" if cp_char == "C" else "put"
                signed_qty = int(round(float(p.qty)))   # + = long, - = short
                entry_px   = float(p.avg_entry_price)   # always positive (Alpaca convention)
                lastday    = float(getattr(p, "lastday_price", 0) or 0)
                entered_at = today if lastday == 0 else today - datetime.timedelta(days=1)
            except Exception:
                continue
            parsed.append(dict(
                occ=occ, ticker=ticker, expiry=expiry,
                strike=strike, opt_type=opt_type,
                qty=signed_qty, entry_px=entry_px, entered_at=entered_at,
            ))

        if not parsed:
            return

        # ── Step 2: group by (ticker, expiry) for multi-leg detection ────────
        groups: Dict[tuple, list] = defaultdict(list)
        for item in parsed:
            groups[(item["ticker"], item["expiry"])].append(item)

        registered: set = set()   # OCCs already absorbed into a multi-leg position

        for (ticker, expiry), items in groups.items():
            calls = sorted([i for i in items if i["opt_type"] == "call"], key=lambda x: x["strike"])
            puts  = sorted([i for i in items if i["opt_type"] == "put"],  key=lambda x: x["strike"])

            # ── Detect CALL butterfly: [+N, -2N, +N] by ascending strike ─────
            if len(calls) == 3 and all(c["occ"] not in registered for c in calls):
                q0, q1, q2 = calls[0]["qty"], calls[1]["qty"], calls[2]["qty"]
                if q0 > 0 and q2 > 0 and q1 < 0 and q0 == q2 and abs(q1) == 2 * q0:
                    # net_debit = what was paid for both long wings minus credit from short body
                    net_debit = round(
                        calls[0]["entry_px"] + calls[2]["entry_px"] - 2 * calls[1]["entry_px"], 3
                    )
                    primary_occ = calls[0]["occ"]
                    self._positions[primary_occ] = OptionsPosition(
                        occ_symbol  = primary_occ,
                        symbol      = ticker,
                        option_type = "call_butterfly",
                        action      = "buy_to_open",
                        strike      = calls[1]["strike"],   # mid (short body) strike
                        expiry      = expiry,
                        contracts   = q0,
                        entry_price = net_debit,            # positive debit; formula works correctly
                        strategy    = "reconciled_butterfly",
                        legs        = [
                            {"occ_symbol": calls[0]["occ"], "side": "buy",  "ratio_qty": 1},
                            {"occ_symbol": calls[1]["occ"], "side": "sell", "ratio_qty": 2},
                            {"occ_symbol": calls[2]["occ"], "side": "buy",  "ratio_qty": 1},
                        ],
                        entered_at  = calls[0]["entered_at"],
                    )
                    for c in calls:
                        registered.add(c["occ"])
                    log.info(
                        f"[OPTIONS] Reconciled CALL BUTTERFLY: {ticker} "
                        f"{calls[0]['strike']:.2f}/{calls[1]['strike']:.2f}/{calls[2]['strike']:.2f}C "
                        f"net_debit=${net_debit:.3f} ×{q0}c"
                    )
                    continue

            # ── Detect PUT butterfly: [+N, -2N, +N] by ascending strike ──────
            if len(puts) == 3 and all(p_["occ"] not in registered for p_ in puts):
                q0, q1, q2 = puts[0]["qty"], puts[1]["qty"], puts[2]["qty"]
                if q0 > 0 and q2 > 0 and q1 < 0 and q0 == q2 and abs(q1) == 2 * q0:
                    net_debit = round(
                        puts[0]["entry_px"] + puts[2]["entry_px"] - 2 * puts[1]["entry_px"], 3
                    )
                    primary_occ = puts[0]["occ"]
                    self._positions[primary_occ] = OptionsPosition(
                        occ_symbol  = primary_occ,
                        symbol      = ticker,
                        option_type = "put_butterfly",
                        action      = "buy_to_open",
                        strike      = puts[1]["strike"],
                        expiry      = expiry,
                        contracts   = q0,
                        entry_price = net_debit,
                        strategy    = "reconciled_butterfly",
                        legs        = [
                            {"occ_symbol": puts[0]["occ"], "side": "buy",  "ratio_qty": 1},
                            {"occ_symbol": puts[1]["occ"], "side": "sell", "ratio_qty": 2},
                            {"occ_symbol": puts[2]["occ"], "side": "buy",  "ratio_qty": 1},
                        ],
                        entered_at  = puts[0]["entered_at"],
                    )
                    for p_ in puts:
                        registered.add(p_["occ"])
                    log.info(
                        f"[OPTIONS] Reconciled PUT BUTTERFLY: {ticker} "
                        f"{puts[0]['strike']:.2f}/{puts[1]['strike']:.2f}/{puts[2]['strike']:.2f}P "
                        f"net_debit=${net_debit:.3f} ×{q0}c"
                    )
                    continue

            # ── Detect IRON CONDOR: 2 puts + 2 calls, pattern [+,-,-,+] ──────
            # puts sorted asc: [long OTM put (+), short near-ATM put (-)]
            # calls sorted asc: [short near-ATM call (-), long OTM call (+)]
            if (len(puts) == 2 and len(calls) == 2
                    and all(i["occ"] not in registered for i in items)):
                qp0, qp1 = puts[0]["qty"], puts[1]["qty"]
                qc0, qc1 = calls[0]["qty"], calls[1]["qty"]
                base = abs(qp0)
                if (qp0 > 0 and qp1 < 0 and qc0 < 0 and qc1 > 0
                        and abs(qp0) == abs(qp1) == abs(qc0) == abs(qc1)):
                    # net_credit = premiums received (shorts) minus premiums paid (long wings)
                    net_credit = round(
                        puts[1]["entry_px"] + calls[0]["entry_px"]
                        - puts[0]["entry_px"] - calls[1]["entry_px"], 3
                    )
                    primary_occ = calls[0]["occ"]   # short call as position key
                    # For sell_to_open: store entry_price as NEGATIVE so the P&L formula
                    # (current_net_value - entry_price) / abs(entry_price) shows +% when profitable.
                    self._positions[primary_occ] = OptionsPosition(
                        occ_symbol  = primary_occ,
                        symbol      = ticker,
                        option_type = "iron_condor",
                        action      = "sell_to_open",
                        strike      = calls[0]["strike"],
                        expiry      = expiry,
                        contracts   = base,
                        entry_price = -abs(net_credit),     # negative for credit strategy
                        strategy    = "reconciled_condor",
                        legs        = [
                            {"occ_symbol": puts[0]["occ"],  "side": "buy",  "ratio_qty": 1},
                            {"occ_symbol": puts[1]["occ"],  "side": "sell", "ratio_qty": 1},
                            {"occ_symbol": calls[0]["occ"], "side": "sell", "ratio_qty": 1},
                            {"occ_symbol": calls[1]["occ"], "side": "buy",  "ratio_qty": 1},
                        ],
                        entered_at  = calls[0]["entered_at"],
                    )
                    for i in items:
                        registered.add(i["occ"])
                    log.info(
                        f"[OPTIONS] Reconciled IRON CONDOR: {ticker} "
                        f"{puts[0]['strike']:.2f}/{puts[1]['strike']:.2f}P "
                        f"{calls[0]['strike']:.2f}/{calls[1]['strike']:.2f}C "
                        f"net_credit=${net_credit:.3f} ×{base}c"
                    )
                    continue

            # ── Detect CALL/PUT SPREAD: 2 same-type legs [+N, -N] ───────────
            for opt_legs in (calls, puts):
                if len(opt_legs) == 2 and all(l["occ"] not in registered for l in opt_legs):
                    l0, l1 = opt_legs[0], opt_legs[1]   # sorted asc by strike
                    if l0["qty"] > 0 and l1["qty"] < 0 and l0["qty"] == abs(l1["qty"]):
                        # Debit spread: buy lower strike, sell higher strike
                        buy_leg, sell_leg = l0, l1
                    elif l0["qty"] < 0 and l1["qty"] > 0 and abs(l0["qty"]) == l1["qty"]:
                        # Credit spread: sell lower strike, buy higher strike
                        buy_leg, sell_leg = l1, l0
                    else:
                        continue

                    n         = abs(buy_leg["qty"])
                    opt_type  = buy_leg["opt_type"]
                    # net_debit: positive for debit spreads, negative for credit spreads
                    net_debit = round(buy_leg["entry_px"] - sell_leg["entry_px"], 3)
                    primary_occ = buy_leg["occ"]
                    spread_name = f"{opt_type}_spread"
                    self._positions[primary_occ] = OptionsPosition(
                        occ_symbol  = primary_occ,
                        symbol      = ticker,
                        option_type = spread_name,
                        action      = "buy_to_open" if net_debit > 0 else "sell_to_open",
                        strike      = buy_leg["strike"],
                        expiry      = expiry,
                        contracts   = n,
                        entry_price = net_debit,
                        strategy    = "reconciled_spread",
                        legs        = [
                            {"occ_symbol": buy_leg["occ"],  "side": "buy",  "ratio_qty": 1, "strike": buy_leg["strike"]},
                            {"occ_symbol": sell_leg["occ"], "side": "sell", "ratio_qty": 1, "strike": sell_leg["strike"]},
                        ],
                        entered_at  = buy_leg["entered_at"],
                    )
                    registered.add(buy_leg["occ"])
                    registered.add(sell_leg["occ"])
                    direction = "DEBIT" if net_debit > 0 else "CREDIT"
                    log.info(
                        f"[OPTIONS] Reconciled {opt_type.upper()} {direction} SPREAD: {ticker} "
                        f"{buy_leg['strike']:.2f}/{sell_leg['strike']:.2f} "
                        f"net={'${:.3f}'.format(net_debit)} ×{n}c"
                    )

            # ── Fall through: register remaining legs as individual positions ─
            for item in items:
                if item["occ"] in self._positions or item["occ"] in registered:
                    continue
                side   = "buy" if item["qty"] > 0 else "sell"
                action = "buy_to_open" if side == "buy" else "sell_to_open"
                # For single short legs: negate entry_price so P&L formula is correct.
                # Formula: (current_net_value - entry_price) / abs(entry_price)
                # Short: current_net_value = -current_price; with negative entry_price
                # this shows +% when current_price falls below what was received.
                stored_entry = item["entry_px"] if side == "buy" else -item["entry_px"]
                self._positions[item["occ"]] = OptionsPosition(
                    occ_symbol  = item["occ"],
                    symbol      = item["ticker"],
                    option_type = item["opt_type"],
                    action      = action,
                    strike      = item["strike"],
                    expiry      = item["expiry"],
                    contracts   = abs(item["qty"]),
                    entry_price = stored_entry,
                    strategy    = "reconciled",
                    legs        = [{"occ_symbol": item["occ"], "side": side, "ratio_qty": 1}],
                    entered_at  = item["entered_at"],
                )
                log.info(
                    f"[OPTIONS] Reconciled {side.upper()} position: {item['occ']} "
                    f"{abs(item['qty'])}x {item['opt_type']} @{item['strike']} "
                    f"entry=${item['entry_px']:.2f}"
                )

    # ── Allocation / Budget ────────────────────────────────────────────────────

    def _get_options_budget(self) -> Tuple[float, float]:
        """Returns (total_options_budget $, remaining_budget $) using real-time API values.

        Priority order (all sourced from /v2/account — real-time, not dashboard):
          1. options_buying_power  — options-specific BP (may be None on some account tiers)
          2. buying_power          — general real-time BP (always present; used as fallback)
          3. equity * pct          — configured allocation cap (always applied as an upper bound)

        Note: Alpaca dashboard charts show 15-min delayed data. The API fields used here
        (equity, buying_power, options_buying_power) are all real-time SIP data regardless
        of whether the account is live or paper.
        """
        try:
            acct              = self.client.get_account()
            equity            = float(acct.equity)
            configured_budget = equity * (OPTIONS_ALLOCATION_PCT / 100.0)

            # --- Determine the actual available buying power from the API ---
            # options_buying_power: options-specific; may be None on some live tiers.
            _raw_obp = getattr(acct, "options_buying_power", None)
            if _raw_obp is not None and float(_raw_obp) > 0:
                available_bp = float(_raw_obp)
                log.debug(f"[OPTIONS] options_buying_power=${available_bp:.2f} (real-time)")
            else:
                # Fall back to general buying_power — always present and real-time.
                available_bp = float(acct.buying_power)
                log.debug(
                    f"[OPTIONS] options_buying_power unavailable — "
                    f"using buying_power=${available_bp:.2f} (real-time)"
                )

            # Apply configured allocation as an upper cap so we never over-deploy.
            total_budget = min(configured_budget, available_bp)
            if available_bp < configured_budget:
                log.debug(
                    f"[OPTIONS] Available BP ${available_bp:.2f} < configured allocation "
                    f"${configured_budget:.2f} — budget capped at ${total_budget:.2f}"
                )

            used      = self.current_options_cost()
            remaining = max(0.0, total_budget - used)
            return total_budget, remaining

        except Exception as e:
            log.warning(f"[OPTIONS] Could not fetch account budget: {e}", exc_info=True)
            return 0.0, 0.0

    def current_options_cost(self) -> float:
        """Estimate total capital deployed in open options positions."""
        total = 0.0
        for pos in self._positions.values():
            if pos.action == "buy_to_open":
                total += pos.entry_price * CONTRACT_SIZE * pos.contracts
        return total

    def _count_open_options(self) -> int:
        return len(self._positions)

    # ── Position Sizing ────────────────────────────────────────────────────────

    def _calc_contracts(self, signal: OptionSignal, remaining_budget: float) -> int:
        """Calculate how many contracts to buy within the remaining budget.
        Each contract costs: mid_price × CONTRACT_SIZE dollars.
        We size to use ~33% of remaining budget (split across 3 max positions).
        """
        if signal.mid_price <= 0:
            return 0
        per_contract_cost = signal.mid_price * CONTRACT_SIZE
        # Use up to 1/3 of remaining budget per position
        position_budget = remaining_budget / max(1, OPTIONS_MAX_POSITIONS - self._count_open_options())
        contracts = int(position_budget // per_contract_cost)
        return max(0, min(contracts, 10))  # hard cap: never more than 10 contracts

    def _get_alpaca_option_limit(self, occ_sym: str, is_buy: bool) -> Optional[float]:
        """Return an Alpaca option limit using the latest executed trade when available."""
        try:
            from alpaca.data.requests import OptionSnapshotRequest

            snapshots = self.data_client.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=[occ_sym])
            )
            snapshot = snapshots.get(occ_sym)
            trade = getattr(snapshot, "latest_trade", None)
            last_price = float(getattr(trade, "price", 0) or 0)
            quote = getattr(snapshot, "latest_quote", None)
            bid = float(getattr(quote, "bid_price", 0) or 0)
            ask = float(getattr(quote, "ask_price", 0) or 0)
            if last_price > 0:
                log.info(
                    f"[OPTIONS] {occ_sym} Alpaca pricing: last=${last_price:.2f} "
                    f"bid=${bid:.2f} ask=${ask:.2f} limit=${last_price:.2f}"
                )
                return round(last_price, 2)

            if bid <= 0 or ask <= 0 or ask < bid:
                log.warning(f"[OPTIONS] {occ_sym} has no valid Alpaca last trade or bid/ask quote")
                return None

            limit = ask if is_buy else bid
            log.info(
                f"[OPTIONS] {occ_sym} Alpaca pricing: no last trade; bid=${bid:.2f} "
                f"ask=${ask:.2f} limit=${limit:.2f}"
            )
            return round(limit, 2)
        except Exception as e:
            log.warning(f"[OPTIONS] {occ_sym} Alpaca pricing unavailable: {e}")
            return None

    # ── Order Placement ────────────────────────────────────────────────────────

    def _get_premarket_gap(self, symbol: str) -> float:
        """Returns today's open vs yesterday's close as a signed fraction.
        e.g. +0.04 = gapped up 4%.  Uses Alpaca daily bars (already cached by get_bars).
        """
        try:
            from engine.utils import get_bars
            bars = get_bars(symbol, "2d", "1d")
            if len(bars) < 2:
                return 0.0
            prev_close = float(bars["close"].iloc[-2])
            today_open = float(bars["open"].iloc[-1])
            return (today_open - prev_close) / prev_close
        except Exception:
            return 0.0

    def _fetch_current_iv(self, pos: "OptionsPosition") -> Optional[float]:
        """Fetch current implied volatility for a tracked naked position via yfinance.
        Returns 0-1 float (e.g. 0.65 = 65% IV) or None on failure.
        Only called for open naked positions (max 3), every 10 minutes.
        """
        try:
            import yfinance as yf
            ticker = yf.Ticker(pos.symbol)
            exp_str = pos.expiry.strftime("%Y-%m-%d")
            chain = ticker.option_chain(exp_str)
            df = chain.calls if "call" in pos.option_type.lower() else chain.puts
            closest = (df["strike"] - pos.strike).abs()
            if closest.empty:
                return None
            row = df.loc[[closest.idxmin()]]
            iv = float(row["impliedVolatility"].iloc[0])
            return iv if iv > 0 else None
        except Exception:
            return None

    def _maybe_convert_to_spread(self) -> None:
        """For each open naked position: if IV has dropped >=30% from entry AND
        the position is profitable, sell a further-OTM leg to convert to a spread.

        This is called from monitor_positions() but rate-limited to every
        _IV_CONVERT_INTERVAL seconds to avoid excess API calls.
        """
        now_t = time.monotonic()
        if now_t - self._last_iv_convert_ts < self._IV_CONVERT_INTERVAL:
            return
        self._last_iv_convert_ts = now_t

        try:
            all_pos = {p.symbol: p for p in self.client.get_all_positions()}
        except Exception as e:
            log.warning(f"[OPTIONS] IV convert: could not fetch positions: {e}")
            return

        for occ_sym, pos in list(self._positions.items()):
            if not pos.is_naked or pos.entry_iv <= 0:
                continue
            try:
                # Gate 1: current IV must be >=30% below entry IV
                cur_iv = self._fetch_current_iv(pos)
                if cur_iv is None:
                    continue
                drop_pct = (pos.entry_iv - cur_iv) / pos.entry_iv
                if drop_pct < 0.30:
                    log.debug(
                        f"[OPTIONS] IV convert {pos.symbol}: IV {pos.entry_iv:.2f}→{cur_iv:.2f} "
                        f"(drop={drop_pct:.0%} < 30%) — waiting"
                    )
                    continue

                # Gate 2: position must be profitable
                leg_pos = all_pos.get(occ_sym)
                if leg_pos is None:
                    continue
                cur_price = float(leg_pos.current_price)
                pnl_pct = (cur_price - pos.entry_price) / abs(pos.entry_price) * 100
                if pnl_pct <= 0:
                    log.debug(
                        f"[OPTIONS] IV convert {pos.symbol}: not profitable ({pnl_pct:+.1f}%) — holding naked"
                    )
                    continue

                # Compute short leg: 10% further OTM, rounded to nearest $0.50
                cp_type = "call" if "call" in pos.option_type.lower() else "put"
                if cp_type == "put":
                    short_strike = round(pos.strike * 0.90 / 0.5) * 0.5   # 10% below
                else:
                    short_strike = round(pos.strike * 1.10 / 0.5) * 0.5   # 10% above

                short_occ = _alpaca_option_symbol(pos.symbol, pos.expiry, cp_type, short_strike)

                # Submit sell_to_open for the short leg (market order — speed matters)
                order_req = MarketOrderRequest(
                    symbol=short_occ,
                    qty=pos.contracts,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
                self.client.submit_order(order_req)
                log.info(
                    f"[OPTIONS] IV CONVERT {pos.symbol}: added short leg {short_occ} "
                    f"(IV drop={drop_pct:.0%}, P&L={pnl_pct:+.1f}%) — position is now a spread"
                )

                # Update tracking: add short leg, mark as no longer naked
                pos.legs.append({"occ_symbol": short_occ, "side": "sell", "ratio_qty": 1})
                pos.is_naked = False

            except Exception as e:
                log.error(f"[OPTIONS] IV convert failed for {occ_sym}: {e}", exc_info=True)

    def _is_account_tradeable(self) -> bool:
        """Return False if the account is in liquidation-only / trading-blocked state."""
        global _ACCOUNT_RESTRICTED
        # Short-circuit: if we already caught a 40310000 this session, don't retry.
        # The restriction is a regulatory/PDT hold — it won't clear mid-session.
        if _ACCOUNT_RESTRICTED:
            log.warning(
                "[OPTIONS] Account restricted (40310000 / liquidation-only) — "
                "all new entries blocked. Restart bot or resolve via Alpaca dashboard."
            )
            return False
        try:
            acct = self.client.get_account()
            blocked = (
                getattr(acct, "trading_blocked", False)
                or getattr(acct, "account_blocked", False)
            )
            if blocked:
                log.error(
                    "[OPTIONS] Account is trading-blocked (liquidation-only) — "
                    "halting all new option entries until restriction is cleared"
                )
                _ACCOUNT_RESTRICTED = True
                return False
            return True
        except Exception as e:
            log.warning(f"[OPTIONS] Could not verify account tradeable status: {e}")
            return True  # allow attempt; the order itself will catch any restriction

    @staticmethod
    def _is_permitted_probe_cap_bypass(signal: OptionSignal) -> bool:
        """Allow the portfolio-count exception only for the fixed probe ATM call."""
        return (
            signal.strategy == "LiveProbeATMScaleIn"
            and signal.option_type == "call"
            and signal.action == "buy_to_open"
            and signal.contract_cap == 1
            and signal.force_single_leg
        )

    def place_option_order(self, signal: OptionSignal, market_state: MarketState) -> bool:
        """
        Production-ready order placement for ApexTrader.
        Fixes communications with Alpaca API and internal state tracking.
        """
        permitted_probe_bypass = self._is_permitted_probe_cap_bypass(signal)
        if signal.bypass_portfolio_cap and not permitted_probe_bypass:
            log.warning(
                f"[OPTIONS] Rejected unauthorized portfolio-cap bypass for {signal.symbol} "
                f"({signal.strategy})"
            )
            return False
        try:
            broker_positions = self.client.get_all_positions()
            portfolio_count = len(broker_positions)
            if portfolio_count >= MAX_POSITIONS and not signal.bypass_portfolio_cap:
                log.info(
                    f"[OPTIONS] Portfolio position cap reached "
                    f"({portfolio_count}/{MAX_POSITIONS}) — skipping {signal.symbol}"
                )
                return False
            if portfolio_count >= MAX_POSITIONS:
                log.info(
                    f"[OPTIONS] Portfolio position cap bypassed for live-probe scale-in "
                    f"{signal.symbol} ({portfolio_count}/{MAX_POSITIONS})"
                )
            broker_option_count = sum(
                1
                for position in broker_positions
                if getattr(position, "asset_class", "") == "us_option"
            )
            if broker_option_count >= OPTIONS_MAX_POSITIONS:
                log.info(
                    f"[OPTIONS] Broker option cap reached "
                    f"({broker_option_count}/{OPTIONS_MAX_POSITIONS}) — skipping {signal.symbol}"
                )
                return False
        except Exception as e:
            log.warning(f"[OPTIONS] Could not verify portfolio position count: {e} — skipping entry")
            return False

        # Fetch budget once — reused by the gross-notional pre-check (section 0) and
        # contract sizing (section 3) to avoid two account API calls per order.
        total_budget, remaining = self._get_options_budget()

        # 0. Gross Notional Check (LIVE accounts only)
        if not PAPER:
            # Prepare legs for the pending order
            strat = signal.strategy.lower()
            is_butterfly = "butterfly" in strat or "butterfly" in signal.option_type.lower()
            is_condor = "condor" in strat
            is_spread = signal.spread_sell_strike is not None and not is_butterfly and not is_condor
            is_mleg = is_butterfly or is_spread or is_condor
            cp_type = "call" if "call" in signal.option_type.lower() else "put"
            raw_contracts = self._calc_contracts(signal, remaining)
            from engine.config import MIN_SIGNAL_CONFIDENCE, CONF_SCALE_MIN_MULT, CONF_SCALE_FULL_CONF
            _conf_mult = CONF_SCALE_MIN_MULT + (1.0 - CONF_SCALE_MIN_MULT) * min(
                1.0, max(0.0, (signal.confidence - MIN_SIGNAL_CONFIDENCE) / (CONF_SCALE_FULL_CONF - MIN_SIGNAL_CONFIDENCE))
            )
            contracts = max(1, int(round(raw_contracts * _conf_mult)))
            if signal.contract_cap is not None:
                contracts = min(contracts, signal.contract_cap)
            # Build legs for the pending order
            if is_condor:
                legs = [
                    {"strike": signal.put_long_strike, "ratio_qty": 1},
                    {"strike": signal.put_short_strike, "ratio_qty": 1},
                    {"strike": signal.call_short_strike, "ratio_qty": 1},
                    {"strike": signal.call_long_strike, "ratio_qty": 1},
                ]
            elif is_butterfly:
                legs = [
                    {"strike": signal.butterfly_low_strike, "ratio_qty": 1},
                    {"strike": signal.strike, "ratio_qty": 2},
                    {"strike": signal.butterfly_high_strike, "ratio_qty": 1},
                ]
            elif is_spread:
                _eff_spread_sell_strike = signal.spread_sell_strike
                legs = [
                    {"strike": signal.strike, "ratio_qty": 1},
                    {"strike": _eff_spread_sell_strike, "ratio_qty": 1},
                ]
            else:
                legs = [{"strike": signal.strike, "ratio_qty": 1}]

            # Dynamically reduce contracts to fit under 6x equity notional cap
            try:
                acct = self.client.get_account()
                equity = float(acct.equity)
            except Exception as e:
                log.warning(f"[OPTIONS] Could not fetch account equity for notional check: {e}")
                equity = 0.0
            min_contracts = 1
            order_accepted = False
            while contracts >= min_contracts:
                extra = {"contracts": contracts, "legs": legs, "mid_price": signal.mid_price}
                gross_notional = self._calculate_gross_notional(extra=extra)
                if equity > 0 and gross_notional > 6 * equity:
                    contracts -= 1
                    continue
                order_accepted = True
                break
            if not order_accepted:
                log.warning(
                    f"[OPTIONS] Skipping {signal.symbol} order: even 1 contract would exceed gross notional cap (${gross_notional:,.2f} > 6x equity ${equity:,.2f})"
                )
                return False
        # 1. Global Enable Check
        if not getattr(self, "OPTIONS_ENABLED", True):
            return False

        # 1b. Account restriction check (liquidation-only = code 40310000)
        if not self._is_account_tradeable():
            return False

        # 1c. Corporate actions guard — block entry if a reverse split or merger is
        #     pending for the underlying within the next 14 calendar days.
        #     Uses the Alpaca corporate actions endpoint (same auth credentials).
        try:
            import requests as _req
            import datetime as _dt
            _today = _dt.date.today()
            _ca_resp = _req.get(
                "https://data.alpaca.markets/v1beta1/corporate_actions",
                params={
                    "symbols": signal.symbol,
                    "types": "reverse_split,cash_merger,stock_merger,stock_and_cash_merger",
                    "start": _today.isoformat(),
                    "end": (_today + _dt.timedelta(days=14)).isoformat(),
                    "limit": 10,
                },
                headers={
                    "APCA-API-KEY-ID": API_KEY,
                    "APCA-API-SECRET-KEY": API_SECRET,
                },
                timeout=5,
            )
            _ca_resp.raise_for_status()
            _ca_data = _ca_resp.json()
            _ca_events = (
                _ca_data.get("reverse_splits", [])
                + _ca_data.get("cash_mergers", [])
                + _ca_data.get("stock_mergers", [])
                + _ca_data.get("stock_and_cash_mergers", [])
            )
            if _ca_events:
                log.info(
                    f"[OPTIONS] Corporate action pending for {signal.symbol} within 14 days "
                    f"({len(_ca_events)} event(s)) — skipping entry"
                )
                return False
        except Exception:
            pass  # network/parse failure — allow entry rather than block all trades

        # 2. PDT & Account Guard (disabled — options trading allowed regardless of PDT status)
        # is_small_account, dt_remaining = self._check_pdt_status()
        # if is_small_account:
        #     # Reserve day trades only matter for same-day closes.
        #     # Options entered with DTE >= OPTIONS_DTE_MIN are swing trades by design
        #     # and will NOT create a day trade on entry. Only block if we literally have
        #     # no day trades left (would be unable to emergency-exit any position today).
        #     if dt_remaining == 0:
        #         log.info(
        #             f"[OPTIONS] PDT reserve: 0 day trades remaining — skipping {signal.symbol} entry"
        #         )
        #         return False
        #     # Cap to 1 open option position on small accounts to conserve capital
        #     if self._count_open_options() >= 1:
        #         log.info(f"[OPTIONS] Small account 1-position cap reached — skipping {signal.symbol}")
        #         return False

        # 3. Budget & Contract Calculation
        raw_contracts = self._calc_contracts(signal, remaining)
        if raw_contracts <= 0:
            per_contract = signal.mid_price * CONTRACT_SIZE
            log.info(
                f"[OPTIONS] Insufficient budget for {signal.symbol} "
                f"(remaining=${remaining:.2f}, total=${total_budget:.2f}, "
                f"per_contract=${per_contract:.2f})"
            )
            return False
        # Scale contracts by signal confidence (same curve as equity: 0.50× at floor → 1.0× at 0.85+)
        from engine.config import MIN_SIGNAL_CONFIDENCE, CONF_SCALE_MIN_MULT, CONF_SCALE_FULL_CONF
        _conf_mult = CONF_SCALE_MIN_MULT + (1.0 - CONF_SCALE_MIN_MULT) * min(
            1.0, max(0.0, (signal.confidence - MIN_SIGNAL_CONFIDENCE) / (CONF_SCALE_FULL_CONF - MIN_SIGNAL_CONFIDENCE))
        )
        contracts = max(1, int(round(raw_contracts * _conf_mult)))
        if signal.contract_cap is not None:
            contracts = min(contracts, signal.contract_cap)
        log.debug(f"[OPTIONS] {signal.symbol} conf={signal.confidence:.0%} → scale={_conf_mult:.2f}× → {contracts}c")

        # 4. Determine Strategy Type
        strat = signal.strategy.lower()
        is_butterfly = "butterfly" in strat or "butterfly" in signal.option_type.lower()
        is_condor = "condor" in strat
        is_spread = signal.spread_sell_strike is not None and not is_butterfly and not is_condor
        is_mleg = is_butterfly or is_spread or is_condor

        # Hard limit: max 2 open mleg positions at any time
        if is_mleg:
            open_mleg = sum(1 for p in self._positions.values() if len(p.legs) > 1)
            if open_mleg >= OPTIONS_MAX_MLEG_POSITIONS:
                log.info(
                    f"[OPTIONS] Mleg limit ({OPTIONS_MAX_MLEG_POSITIONS}) reached "
                    f"({open_mleg} open spreads) — skipping {signal.symbol}"
                )
                return False
            # Cap contracts per spread entry
            contracts = min(contracts, OPTIONS_MAX_MLEG_CONTRACTS)

        cp_type = "call" if "call" in signal.option_type.lower() else "put"

        # ── 4b. Open Window / IV Gate ─────────────────────────────────────────
        # Decides naked vs spread AFTER type detection so butterflies/condors are untouched.
        _in_open_window = market_state.is_open_window
        _eff_spread_sell_strike = signal.spread_sell_strike  # may be overridden below
        _entry_iv      = signal.iv_pct   # IV% at scan time (stored on position)
        _is_naked_entry = False
        _open_stop_pct  = 0.0            # 0 = use global OPTIONS_STOP_LOSS_PCT
        _net_entry_price = signal.mid_price   # overridden below if auto-spread net debit is computed

        if not is_butterfly and not is_condor:
            if signal.force_single_leg:
                _force_spread = False
            elif _in_open_window:
                # Higher confidence bar — only cleanest signals at the open
                if signal.confidence < 0.85:
                    log.debug(
                        f"[OPTIONS] Open window: {signal.symbol} conf={signal.confidence:.0%} < 85% — skip"
                    )
                    return False
                # 1 naked position max during the open window
                if self._count_open_options() >= 1:
                    log.debug(
                        f"[OPTIONS] Open window: already 1 open position — skip {signal.symbol}"
                    )
                    return False
                # 50% contract count (premium is peak at open)
                contracts = max(1, contracts // 2)
                _open_stop_pct = 25.0
                # Open window: default to spread (IV is peak at open).
                # Only go naked if IV is very low AND gap is small — options are cheap.
                gap = self._get_premarket_gap(signal.symbol)
                _allow_naked = signal.iv_rank < 20 and abs(gap) <= 0.02
                _force_spread = not _allow_naked
                if _allow_naked:
                    log.debug(
                        f"[OPTIONS] Open window: {signal.symbol} IV rank={signal.iv_rank:.0f} < 20 "
                        f"gap={gap:+.1%} — low IV, allowing naked"
                    )
                else:
                    log.debug(
                        f"[OPTIONS] Open window: {signal.symbol} IV rank={signal.iv_rank:.0f} "
                        f"gap={gap:+.1%} → spread (default at open)"
                    )
            else:
                # Normal session: IV rank decides naked vs spread
                from engine.config import OPTIONS_IV_RANK_SPREAD_THRESHOLD
                _force_spread = signal.iv_rank > OPTIONS_IV_RANK_SPREAD_THRESHOLD

            if not _force_spread:
                # Trade as pure naked single-leg — ignore any spread_sell_strike from strategy
                is_spread  = False
                is_mleg    = False
                _is_naked_entry = True
                if _in_open_window:
                    log.debug(
                        f"[OPTIONS] Open window NAKED: {signal.symbol} {cp_type} "
                        f"@{signal.strike} ×{contracts}c | IV={_entry_iv:.0%} | stop=25%"
                    )
            else:
                # Need a spread.  If strategy didn't provide a short leg, derive one.
                if _eff_spread_sell_strike is None:
                    if cp_type == "put":
                        _eff_spread_sell_strike = round(signal.strike * 0.90 / 0.5) * 0.5
                    else:
                        _eff_spread_sell_strike = round(signal.strike * 1.10 / 0.5) * 0.5
                    log.debug(
                        f"[OPTIONS] IV gate forced spread for {signal.symbol}: "
                        f"derived short leg @{_eff_spread_sell_strike}"
                    )
                is_spread = True
                is_mleg   = True
        # ── End 4b ───────────────────────────────────────────────────────────

            is_live_probe = (
                OPTIONS_LIVE_PROBE_MODE
                and not is_mleg
                and signal.action == "buy_to_open"
                and signal.strategy != "LiveProbeATMScaleIn"
            )
            if is_live_probe:
                contracts = 1

        try:
            # NO INTERNAL CALCULATIONS — all pricing from market data (Schwab)
            # For multi-leg spreads: strategy either provides spread_sell_mid OR we fetch from Schwab
            # For single-leg: fetch from Schwab
            # This eliminates Black-Scholes estimates and ensures accuracy
            
            _spread_mid_price = signal.mid_price  # Placeholder, will be overridden by Schwab pricing
            _net_entry_price = signal.mid_price
            
            if is_mleg and signal.spread_sell_mid is not None:
                # Strategy provided explicit spread_sell_mid (from strategy's own market research)
                _spread_mid_price = max(0.01, signal.mid_price - signal.spread_sell_mid)
                _net_entry_price = _spread_mid_price
                log.debug(
                    f"[OPTIONS] Spread net debit (strategy-provided): "
                    f"long=${signal.mid_price:.2f} - short=${signal.spread_sell_mid:.2f} "
                    f"= net=${_spread_mid_price:.2f}"
                )
            elif is_mleg:
                # For auto-derived short leg (IV gate): we'll get ACTUAL price from Schwab in Step 4
                # Don't estimate via Black-Scholes — fetch real market data instead
                log.debug(
                    f"[OPTIONS] IV gate auto-derived spread short leg @{_eff_spread_sell_strike}: "
                    f"will fetch ACTUAL price from Schwab (no internal calculation)"
                )

            if "buy" in signal.action:
                limit_price = round(_spread_mid_price * 0.99, 2)   # debit: bid below mid (pay less)
            else:
                # sell_to_open (credit strategy)
                if is_mleg:
                    # Alpaca mleg convention: positive = debit, NEGATIVE = credit received
                    limit_price = -round(_spread_mid_price * 1.01, 2)  # offer to receive above mid
                else:
                    limit_price = round(_spread_mid_price * 1.01, 2)   # single-leg sell: offer above mid

            # ── STEP 4: GET SCHWAB PRICING FOR COMPLETE SPREAD ─────────────────
            # Before constructing the order, fetch fresh Schwab pricing for multi-leg
            # positions. This ensures we have real-time bid/ask/mark from broker.
            _schwab_spread_mark = None
            _schwab_spread_bid = None
            _schwab_spread_ask = None
            _schwab_limit_override = None
            
            if is_mleg:
                # Build temp legs list for Schwab pricing (before order construction)
                _temp_legs = []
                # We'll populate _temp_legs based on strategy type (same as order legs)
                # This will be populated in each case below before calling Schwab
                
            # ── CASE A: MULTI-LEG (Spreads, Butterflies, Condors) ────────────────
            if is_mleg:
                legs_list = []

                if is_condor:
                    legs_list = [
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, "put", signal.put_long_strike), "side": OrderSide.BUY, "ratio_qty": 1},
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, "put", signal.put_short_strike), "side": OrderSide.SELL, "ratio_qty": 1},
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, "call", signal.call_short_strike), "side": OrderSide.SELL, "ratio_qty": 1},
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, "call", signal.call_long_strike), "side": OrderSide.BUY, "ratio_qty": 1}
                    ]
                elif is_butterfly:
                    legs_list = [
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.butterfly_low_strike), "side": OrderSide.BUY, "ratio_qty": 1},
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.strike), "side": OrderSide.SELL, "ratio_qty": 2},
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.butterfly_high_strike), "side": OrderSide.BUY, "ratio_qty": 1}
                    ]
                else: # Vertical Spread
                    # For debit spreads (buy_to_open): buy primary, sell secondary
                    # For credit spreads (sell_to_open): sell primary, buy secondary
                    is_credit = "sell" in signal.action
                    primary_side   = OrderSide.SELL if is_credit else OrderSide.BUY
                    secondary_side = OrderSide.BUY  if is_credit else OrderSide.SELL
                    legs_list = [
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.strike), "side": primary_side, "ratio_qty": 1},
                        {"symbol": _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, _eff_spread_sell_strike), "side": secondary_side, "ratio_qty": 1}
                    ]

                # Helper defined here so it's in scope for both Schwab pricing and payload construction
                def _leg_side_str(leg_side) -> str:
                    return leg_side.value if hasattr(leg_side, "value") else str(leg_side)

                # ── STEP 4a: Fetch Real-Time Schwab Pricing ───────────────────
                # Build Schwab leg format for pricing API
                try:
                    from engine.utils.schwab_pricing import get_spread_complete_pricing
                    
                    _schwab_legs = []
                    if is_condor:
                        _schwab_legs = [
                            {"occ_symbol": l["symbol"], "side": "buy" if "buy" in _leg_side_str(l["side"]) else "sell", "ratio_qty": l["ratio_qty"], "strike": s, "opt_type": ot}
                            for l, s, ot in zip(
                                legs_list,
                                [signal.put_long_strike, signal.put_short_strike, signal.call_short_strike, signal.call_long_strike],
                                ["put", "put", "call", "call"]
                            )
                        ]
                    elif is_butterfly:
                        _schwab_legs = [
                            {"occ_symbol": l["symbol"], "side": "buy" if "buy" in _leg_side_str(l["side"]) else "sell", "ratio_qty": l["ratio_qty"], "strike": s, "opt_type": cp_type}
                            for l, s in zip(legs_list, [signal.butterfly_low_strike, signal.strike, signal.butterfly_high_strike])
                        ]
                    else:  # Vertical spread
                        _schwab_legs = [
                            {"occ_symbol": l["symbol"], "side": "buy" if "buy" in _leg_side_str(l["side"]) else "sell", "ratio_qty": l["ratio_qty"], "strike": s, "opt_type": cp_type}
                            for l, s in zip(legs_list, [signal.strike, _eff_spread_sell_strike])
                        ]
                    
                    # Get real-time prices from Schwab
                    schwab_pricing = get_spread_complete_pricing(signal.symbol, _schwab_legs, _spread_mid_price)
                    if schwab_pricing:
                        _schwab_spread_mark = schwab_pricing.get("spread_mark")
                        _schwab_spread_bid = schwab_pricing.get("spread_bid")
                        _schwab_spread_ask = schwab_pricing.get("spread_ask")
                        
                        # Abort if credit spread returns near-zero premium — pricing failure
                        if not ("buy" in signal.action) and abs(_schwab_spread_mark) < 0.05:
                            log.warning(
                                f"[OPTIONS] {signal.symbol} Schwab mark=${_schwab_spread_mark:.3f} too low "
                                f"(< $0.05 min credit) — skipping to avoid $0 premium trade"
                            )
                            return

                        # Use Schwab mark to set limit price (1% improvement)
                        if "buy" in signal.action:
                            # Debit: bid 1% below mark (pay less)
                            _schwab_limit_override = round(_schwab_spread_mark * 0.99, 2)
                        else:
                            # Credit: offer 1% above mark (collect more)
                            _schwab_limit_override = -round(_schwab_spread_mark * 1.01, 2)
                        
                        log.info(
                            f"[OPTIONS] {signal.symbol} Schwab pricing: "
                            f"bid=${_schwab_spread_bid:.2f} mark=${_schwab_spread_mark:.2f} ask=${_schwab_spread_ask:.2f} | "
                            f"estimated_mid=${_spread_mid_price:.2f} (diff={_schwab_spread_mark - _spread_mid_price:+.2f})"
                        )
                        log.info(
                            f"[OPTIONS] {signal.symbol} limit price: "
                            f"estimated=${limit_price:.2f} → schwab_real=${_schwab_limit_override:.2f}"
                        )
                        # Use Schwab-based limit price for order
                        limit_price = _schwab_limit_override
                    else:
                        # Schwab returned None — validate estimated fallback before using it
                        if not ("buy" in signal.action) and abs(limit_price) < 0.05:
                            log.warning(
                                f"[OPTIONS] {signal.symbol} Schwab pricing unavailable and estimated "
                                f"credit=${limit_price:.3f} < $0.05 min — skipping trade"
                            )
                            return
                        log.warning(f"[OPTIONS] {signal.symbol} failed to fetch Schwab pricing, using estimated limit=${limit_price:.2f}")
                except Exception as e:
                    # Validate estimated fallback before using it
                    if not ("buy" in signal.action) and abs(limit_price) < 0.05:
                        log.warning(
                            f"[OPTIONS] {signal.symbol} Schwab pricing error and estimated "
                            f"credit=${limit_price:.3f} < $0.05 min — skipping trade. Error: {e}"
                        )
                        return
                    log.error(f"[OPTIONS] {signal.symbol} Schwab pricing error: {e}, using estimated limit=${limit_price:.2f}")

                # Construct Payload
                payload = {
                    "symbol": "", # Must be empty for MLEG
                    "qty": str(int(round(contracts))),
                    # "side" intentionally omitted — not required for mleg per Alpaca docs;
                    # direction is conveyed by each leg's own side field.
                    "type": "limit",
                    "order_class": "mleg",
                    "limit_price": str(limit_price),  # positive=debit, negative=credit
                    "time_in_force": "day",
                    "legs": [
                        {
                            "symbol": l["symbol"],
                            "side": _leg_side_str(l["side"]),
                            "ratio_qty": str(int(l["ratio_qty"])),
                            "position_intent": (
                                "buy_to_open" if _leg_side_str(l["side"]) == "buy" else "sell_to_open"
                            ),
                        } for l in legs_list
                    ]
                }

                log.debug(f"[OPTIONS] Submitting MLEG {signal.symbol}: {json.dumps(payload)}")
                resp = self.client.post("/orders", payload)
                order_id = getattr(resp, "id", None)
                if order_id:
                    threading.Thread(target=self._adaptive_limit_retry, args=(order_id, "buy" if "buy" in signal.action else "sell", float(payload["limit_price"]), signal.symbol, contracts, "", True, payload, 0), daemon=True).start()

            # ── CASE B: SINGLE OPTION (Standard) ─────────────────────────────
            else:
                occ_sym = _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.strike)
                alpaca_limit = self._get_alpaca_option_limit(occ_sym, "buy" in signal.action)
                if alpaca_limit is not None:
                    limit_price = alpaca_limit
                else:
                    log.warning(f"[OPTIONS] {occ_sym} using estimated limit=${limit_price:.2f}")

                order_req = LimitOrderRequest(
                    symbol=occ_sym,
                    qty=contracts,
                    side=OrderSide.BUY if "buy" in signal.action else OrderSide.SELL,
                    limit_price=limit_price,
                    time_in_force=TimeInForce.DAY
                )
                log.debug(f"[OPTIONS] Submitting SINGLE {occ_sym} @ limit=${limit_price:.2f}")
                resp = self.client.submit_order(order_req)
                order_id = getattr(resp, "id", None)
                if order_id:
                    threading.Thread(target=self._adaptive_limit_retry, args=(order_id, "buy" if "buy" in signal.action else "sell", float(limit_price), signal.symbol, contracts, occ_sym, False, None, 0), daemon=True).start()

            # 5. Tracking — store all leg OCC symbols for strategy-agnostic close/monitor
            primary_occ = _alpaca_option_symbol(signal.symbol, signal.expiry, cp_type, signal.strike)
            entry_legs = []
            if is_mleg:
                # Multi-leg (spreads, butterflies, condors): extract strike info
                if is_condor:
                    entry_legs = [
                        {
                            "occ_symbol": l["symbol"],
                            "side": l["side"].value if hasattr(l["side"], "value") else str(l["side"]),
                            "ratio_qty": l["ratio_qty"],
                            "strike": strike,
                            "opt_type": opt_type_leg  # ADDED: Store option type for clarity
                        }
                        for l, strike, opt_type_leg in zip(
                            legs_list,
                            [signal.put_long_strike, signal.put_short_strike, signal.call_short_strike, signal.call_long_strike],
                            ["put", "put", "call", "call"]
                        )
                    ]
                elif is_butterfly:
                    entry_legs = [
                        {
                            "occ_symbol": l["symbol"],
                            "side": l["side"].value if hasattr(l["side"], "value") else str(l["side"]),
                            "ratio_qty": l["ratio_qty"],
                            "strike": strike,
                            "opt_type": cp_type  # ADDED: Store option type
                        }
                        for l, strike in zip(legs_list, [signal.butterfly_low_strike, signal.strike, signal.butterfly_high_strike])
                    ]
                else:  # Vertical spread
                    entry_legs = [
                        {
                            "occ_symbol": l["symbol"],
                            "side": l["side"].value if hasattr(l["side"], "value") else str(l["side"]),
                            "ratio_qty": l["ratio_qty"],
                            "strike": strike,
                            "opt_type": cp_type  # ADDED: Store option type
                        }
                        for l, strike in zip(legs_list, [signal.strike, _eff_spread_sell_strike])
                    ]
            else:
                # Single leg: no secondary strike
                entry_legs = [{
                    "occ_symbol": primary_occ,
                    "side": "buy" if "buy" in signal.action else "sell",
                    "ratio_qty": 1,
                    "strike": signal.strike,
                    "opt_type": cp_type  # ADDED: Store option type
                }]

            self._positions[primary_occ] = OptionsPosition(
                occ_symbol=primary_occ,
                symbol=signal.symbol,
                option_type=signal.option_type,
                action=signal.action,
                strike=signal.strike,
                expiry=signal.expiry,
                contracts=contracts,
                # For sell_to_open (credit strategies): store as negative so the P&L formula
                # (current_net_value - entry_price) / abs(entry_price) shows +% when profitable.
                # At entry, current_net_value ≈ -net_credit; with entry_price = -net_credit → 0%.
                # At max profit (all legs expire worthless), current_net_value → 0 → +100%.
                entry_price=(-abs(_net_entry_price) if "sell" in signal.action else _net_entry_price),
                strategy=signal.strategy,
                legs=entry_legs,
                entry_iv=_entry_iv,
                is_naked=_is_naked_entry,
                open_stop_pct=_open_stop_pct,
                entry_confidence=signal.confidence,  # NEW: Store confidence for tiered profit logic
                    is_live_probe=is_live_probe,
            )

            leg_summary = ", ".join(
                f"{l['side'].upper()} {l['occ_symbol']}" for l in entry_legs
            )
            log.info(
                f"[OPTIONS] EXECUTED {signal.symbol} {signal.option_type} "
                f"{signal.strategy} {contracts}c conf={signal.confidence:.0%} "
                f"legs={len(entry_legs)} open_window={_in_open_window}"
            )
            log.debug(f"[OPTIONS] Tracked {signal.symbol} ({len(entry_legs)} leg(s)): {leg_summary}")
            return True

        except Exception as e:
            # Detect account restriction (40310000) and suppress further order attempts
            if re.search(r'40310000', str(e)):
                global _ACCOUNT_RESTRICTED
                _ACCOUNT_RESTRICTED = True
                log.error(
                    f"[OPTIONS] Account restricted to liquidation-only (40310000) — "
                    f"halting new entries. Resolve via Alpaca dashboard."
                )
            else:
                log.error(f"[OPTIONS] CRITICAL FAILURE for {signal.symbol}: {e}", exc_info=True)
            return False
        
        
    # ── Position Monitoring ────────────────────────────────────────────────────
    def _check_pdt_status(self) -> Tuple[bool, int]:
        """
        Helper to determine if the account is subject to PDT restrictions.
        Returns: (is_small_account, day_trades_remaining)
        """
        try:
            acct = self.client.get_account()
            equity = float(acct.equity)
            pdt_flagged = str(getattr(acct, "pattern_day_trader", False)).lower() in ("1", "true", "yes")
            
            # If under 25k and flagged, we are restricted
            is_small = equity < PDT_ACCOUNT_MIN and pdt_flagged
            trades_used = int(getattr(acct, "daytrade_count", 0))
            remaining = max(0, PDT_MAX_TRADES - trades_used)
            
            return is_small, remaining
        except Exception as e:
            log.warning(f"PDT Status Check Failed: {e}")
            return False, 0
    def monitor_positions(self) -> None:
        """
        Check open options (Single & MLEG) and close at target/stop.
        Handles Net MtM calculation for Butterflies and Iron Condors.
        """
        now = time.monotonic()
        if now - self._last_monitor_ts < self._MONITOR_INTERVAL:
            return
        self._last_monitor_ts = now

        if not self._positions:
            return

        try:
            # Map by OCC symbol for easy leg lookup
            all_positions = {p.symbol: p for p in self.client.get_all_positions()}
        except Exception as e:
            log.warning(f"Options monitor: could not fetch positions: {e}")
            return

        to_close: List[str] = []
        stop_symbols: List[str] = []
        today = datetime.date.today()

        # PDT/Account Status Logic (disabled)
        # pdt_small_account, dt_left_today = self._check_pdt_status()
        pdt_block = False  # PDT checks disabled

        for occ_sym, pos in list(self._positions.items()):
            dte = (pos.expiry - today).days
            same_day_entry = pos.entered_at == today
            days_held = (today - pos.entered_at).days
            in_grace_period = days_held < OPTIONS_ENTRY_GRACE_DAYS
            # pdt_block = pdt_small_account and same_day_entry and dt_left_today <= 1

            # 1. Theta guard: exit within OPTIONS_THETA_EXIT_DTE days to avoid accelerating decay
            # Skip if entered today and PDT-blocked — Alpaca will reject the close anyway
            if dte <= OPTIONS_THETA_EXIT_DTE:
                if same_day_entry:
                    log.debug(
                        f"OPTIONS: {occ_sym} theta guard (DTE={dte}) deferred — entered today, will close tomorrow"
                    )
                else:
                    log.warning(f"OPTIONS: {occ_sym} within {OPTIONS_THETA_EXIT_DTE}d of expiry (DTE={dte}) — closing")
                    to_close.append(occ_sym)
                continue

            # 2. P&L — Use consolidated spread pricing from Schwab API
            # For multi-leg positions: Get complete spread mark/bid/ask prices
            # For single-leg options: Calculate from bid/ask midpoint
            # P&L calculated using MARK price (mid/theoretical value)
            try:
                # Check all legs still exist in the account first
                any_leg_missing = any(
                    all_positions.get(l["occ_symbol"]) is None for l in pos.legs
                )
                if any_leg_missing:
                    log.info(f"OPTIONS: {occ_sym} leg(s) closed externally — clearing tracker")
                    del self._positions[occ_sym]
                    continue

                # Determine if multi-leg or single-leg
                is_mleg = len(pos.legs) > 1
                
                # Get entry price early for use in P&L calculations
                entry_price_signed = float(pos.entry_price)
                entry_mark = abs(entry_price_signed)   # absolute entry premium per share
                entry_cost_dollars = entry_mark * pos.contracts * CONTRACT_SIZE
                if entry_cost_dollars < 0.01:
                    continue
                
                if is_mleg:
                    # Multi-leg (spread, butterfly, condor): Use consolidated Schwab pricing
                    from engine.utils.schwab_pricing import get_spread_complete_pricing
                    
                    # Extract strikes from OCC symbols for Schwab pricing call
                    pricing_legs = []
                    for leg in pos.legs:
                        strike = _extract_strike_from_occ_symbol(leg["occ_symbol"])
                        if strike is None:
                            log.warning(f"[OPTIONS] Failed to extract strike from {leg['occ_symbol']} — falling back to Alpaca")
                            pricing_legs = None
                            break
                        pricing_legs.append({
                            "occ_symbol": leg["occ_symbol"],
                            "side": leg["side"],
                            "ratio_qty": leg.get("ratio_qty", 1),
                            "strike": strike,
                            "opt_type": "call" if "C" in leg["occ_symbol"] else "put"
                        })
                    
                    # Try Schwab first if legs parsed successfully
                    pricing_data = None
                    if pricing_legs is not None:
                        pricing_data = get_spread_complete_pricing(
                            pos.symbol,
                            pricing_legs,
                            pos.entry_price
                        )
                    
                    # Fallback to Alpaca snapshots if OCC parsing failed or Schwab unavailable
                    if pricing_data is None:
                        log.debug(f"[OPTIONS] {occ_sym} - Schwab pricing unavailable, using Alpaca snapshots fallback")
                        try:
                            # Get snapshots for all legs
                            leg_occ_syms = [l["occ_symbol"] for l in pos.legs]
                            from alpaca.data.requests import OptionSnapshotRequest
                            _snaps = self.data_client.get_option_snapshot(
                                OptionSnapshotRequest(symbol_or_symbols=leg_occ_syms)
                            )
                            
                            # Calculate composite spread price from legs
                            spread_mark = 0.0
                            spread_bid = 0.0
                            spread_ask = 0.0
                            
                            for leg, leg_occ_sym in zip(pos.legs, leg_occ_syms):
                                _s = _snaps.get(leg_occ_sym)
                                if _s is None or _s.latest_quote is None:
                                    raise ValueError(f"no snapshot for {leg_occ_sym}")
                                
                                bid = float(_s.latest_quote.bid_price)
                                ask = float(_s.latest_quote.ask_price)
                                mid = (bid + ask) / 2.0
                                
                                # Apply side: buy legs add, sell legs subtract
                                sign = 1.0 if leg["side"].lower() == "buy" else -1.0
                                ratio = leg.get("ratio_qty", 1)
                                
                                spread_bid += sign * bid * ratio
                                spread_ask += sign * ask * ratio
                                spread_mark += sign * mid * ratio
                            
                            # For DEBIT spreads (entry_price_signed > 0): clamp negative composite
                            # prices to $0 — they're pricing artifacts (debit spreads can't go below $0).
                            # For CREDIT spreads (entry_price_signed < 0): a negative composite is
                            # VALID — it means the cost-to-close exceeds the credit received (max loss).
                            # Clamping to 0 would make a max-loss Bear Call show as +100% profit.
                            if entry_price_signed >= 0:
                                spread_bid = max(0.0, spread_bid)
                                spread_ask = max(0.0, spread_ask)
                                spread_mark = max(0.0, spread_mark)
                            
                            current_mark = spread_mark
                            pnl_pct = (spread_mark - entry_price_signed) / abs(entry_price_signed) * 100
                            
                            log.debug(
                                f"[OPTIONS] {pos.symbol} {pos.strategy} (mleg, Alpaca fallback) "
                                f"mark=${current_mark:.2f} bid=${spread_bid:.2f} "
                                f"ask=${spread_ask:.2f} "
                                f"pnl={pnl_pct:+.1f}% dte={dte}d"
                            )
                        except Exception as e:
                            log.warning(f"[OPTIONS] {occ_sym} - Alpaca fallback also failed: {e} — skipping P&L update")
                            continue
                    else:
                        # Use Schwab pricing data
                        current_mark = pricing_data["spread_mark"]
                        pnl_pct = pricing_data["pnl_mark_pct"]
                        dte_from_pricing = pricing_data["dte"]
                        
                        # Override DTE if Schwab has fresher data
                        if dte_from_pricing is not None:
                            dte = dte_from_pricing
                        
                        log.debug(
                            f"[OPTIONS] {pos.symbol} {pos.strategy} (spread/mleg, Schwab) "
                            f"mark=${current_mark:.2f} bid=${pricing_data['spread_bid']:.2f} "
                            f"ask=${pricing_data['spread_ask']:.2f} "
                            f"pnl={pnl_pct:+.1f}% dte={dte}d"
                        )
                else:
                    # Single-leg (naked call/put): Use Alpaca snapshots
                    from alpaca.data.requests import OptionSnapshotRequest
                    _snaps = self.data_client.get_option_snapshot(
                        OptionSnapshotRequest(symbol_or_symbols=[pos.legs[0]["occ_symbol"]])
                    )
                    _s = _snaps.get(pos.legs[0]["occ_symbol"])
                    if _s is None or _s.latest_quote is None:
                        raise ValueError(f"no snapshot/quote for {pos.legs[0]['occ_symbol']}")
                    
                    current_mark = (float(_s.latest_quote.bid_price) + float(_s.latest_quote.ask_price)) / 2.0
                    
                    log.debug(
                        f"[OPTIONS] {pos.symbol} {pos.strategy} (single-leg) "
                        f"bid=${_s.latest_quote.bid_price:.2f} "
                        f"ask=${_s.latest_quote.ask_price:.2f} "
                        f"mark=${current_mark:.2f}"
                    )
                
                # pnl_pct: positive = profitable, negative = losing.
                # For multi-leg: already computed by get_spread_complete_pricing
                # For single-leg: compute here
                if not is_mleg:
                    pnl_pct = (current_mark - entry_price_signed) / entry_mark * 100

                if pnl_pct > pos.peak_pnl_pct:
                    pos.peak_pnl_pct = pnl_pct

                log.debug(
                    f"[OPTIONS] {pos.symbol} {pos.strategy} "
                    f"mark=${current_mark:.2f} entry=${entry_mark:.2f} "
                    f"pnl={pnl_pct:+.1f}% peak={pos.peak_pnl_pct:.1f}%"
                )

                if pos.is_live_probe and not is_mleg and pnl_pct <= -OPTIONS_LIVE_PROBE_STOP_LOSS_PCT:
                    log.warning(
                        f"OPTIONS PROBE: {pos.symbol} stop hit ({pnl_pct:.1f}% <= "
                        f"-{OPTIONS_LIVE_PROBE_STOP_LOSS_PCT:.0f}%) — closing"
                    )
                    to_close.append(occ_sym)
                    stop_symbols.append(pos.symbol)
                    continue

                # 4. Exit decision  (same_day_entry / pdt_block computed at top of loop)

                # Per-position stop: tighter for open-window entries (25%) vs global (30%)
                # Must be computed BEFORE the outer check so tighter stops are reachable.
                _eff_stop = pos.open_stop_pct if pos.open_stop_pct > 0 else OPTIONS_STOP_LOSS_PCT
                # DTE-tightened stop: as expiry nears, tighten stop to preserve remaining value.
                # Only applies to standard spreads/naked (butterfly/condor use separate logic below).
                if dte <= 13:
                    _eff_stop = min(_eff_stop, 15.0)
                elif dte <= 20:
                    _eff_stop = min(_eff_stop, 22.0)

                # ── Spread/Multi-leg exit logic (bull call, bear put, butterfly, condor, etc.) ────
                # NOTE: Spread SL calculation is complex (mark pricing, bid-ask spreads, multi-leg consolidation).
                # Instead of computing SL in code, get SL directly from Schwab API when syncing positions.
                # For now: Skip -35% debit SL for spreads; let Schwab manage via its own stop orders.
                _is_spread = ("spread" in pos.option_type.lower() or "spread" in pos.strategy.lower() or
                             "TrendPullback" in pos.strategy or "BearCall" in pos.strategy)
                _is_butterfly = "butterfly" in pos.option_type.lower() or "butterfly" in pos.strategy.lower()
                _is_condor    = "condor"    in pos.option_type.lower() or "condor"    in pos.strategy.lower()
                _is_mleg = _is_spread or _is_butterfly or _is_condor

                # ── Multi-leg positions: skip percentage-based SL, use Schwab stop orders ────
                if _is_mleg:
                    log.debug(
                        f"[OPTIONS] {pos.symbol} {pos.strategy} (multi-leg) — "
                        f"mark=${current_mark:.2f} pnl={pnl_pct:+.1f}% "
                        f"(SL managed by Schwab stop order, not bot calculation)"
                    )

                # ── Butterfly / Iron Condor exit logic ───────────────────────
                # current_mark and entry_mark already computed above from live quotes.
                # Strategy: HOLD and wait for recovery. Emergency exit only at DTE ≤ 3.
                if _is_butterfly or _is_condor:
                    _mleg_type = "butterfly" if _is_butterfly else "iron condor"

                    # Break-even mode: once mark drops below entry price, latch.
                    # Lowers profit target from +45% to 0% (just recover what was paid).
                    if current_mark < entry_mark and not pos.breakeven_mode:
                        pos.breakeven_mode = True
                        log.info(
                            f"OPTIONS: {pos.symbol} {_mleg_type} mark ${current_mark:.2f} "
                            f"< entry ${entry_mark:.2f} — switching to break-even exit"
                        )

                    # Profit target: current mark recovered to entry (break-even mode)
                    #                or +45% above entry (normal mode).
                    # Only close if mark is positive (we receive money on close).
                    _target_mark = entry_mark if pos.breakeven_mode else entry_mark * (1 + OPTIONS_PROFIT_TARGET_PCT / 100)
                    if occ_sym not in to_close and current_mark > 0 and current_mark >= _target_mark:
                        if not pdt_block:
                            _reason = "break-even" if pos.breakeven_mode else f"target +{OPTIONS_PROFIT_TARGET_PCT:.0f}%"
                            log.info(
                                f"OPTIONS: {pos.symbol} {_mleg_type} {_reason} hit "
                                f"(mark=${current_mark:.2f} >= target=${_target_mark:.2f}) — closing"
                            )
                            to_close.append(occ_sym)

                    # Emergency exit: DTE ≤ 3 with remaining positive value below 55% of entry.
                    # Close to recover what's left rather than let theta eat it to zero.
                    # If mark is inverted (≤ 0): clear tracker and let expire — no close cost.
                    elif occ_sym not in to_close and dte <= 3:
                        if current_mark <= 0:
                            if not same_day_entry:
                                log.info(
                                    f"OPTIONS: {pos.symbol} {_mleg_type} DTE={dte} "
                                    f"mark=${current_mark:.2f} (inverted) — letting expire"
                                )
                                del self._positions[occ_sym]
                        elif current_mark < entry_mark * 0.55:
                            if same_day_entry:
                                log.debug(
                                    f"OPTIONS: {pos.symbol} {_mleg_type} DTE={dte} emergency exit deferred — entered today"
                                )
                            elif not pdt_block:
                                log.warning(
                                    f"OPTIONS: {pos.symbol} {_mleg_type} DTE={dte} emergency exit "
                                    f"— mark=${current_mark:.2f} < 55% of entry=${entry_mark:.2f} — closing"
                                )
                                to_close.append(occ_sym)
                                stop_symbols.append(pos.symbol)


                    continue   # skip standard % stop / trailing stop for mleg structures
                # ── End butterfly/condor logic ────────────────────────────────

                if pnl_pct >= OPTIONS_PROFIT_TARGET_1_PCT and not pos.tier1_closed:
                    # SCALE-OUT: Close 50% of position at +50%, hold 50% with new +20% stop
                    if not pdt_block:
                        _is_short = pos.action == "sell_to_open"
                        if _is_mleg:
                            # Multi-leg spreads: use partial close if 2+ contracts, else close all
                            qty_to_close = int(pos.contracts) // 2
                            if qty_to_close < 1:
                                # 1-spread position: close full
                                log.info(
                                    f"OPTIONS: {pos.symbol} single spread at +{pnl_pct:.1f}% "
                                    f"— closing full position (can't scale out 1 spread)"
                                )
                                to_close.append(occ_sym)
                                pos.tier1_closed = True
                            else:
                                # Scale-out: close half spreads atomically, hold rest
                                qty_remaining = int(pos.contracts) - qty_to_close
                                log.info(
                                    f"OPTIONS: {pos.symbol} spread scale-out at +{pnl_pct:.1f}% "
                                    f"— closing {qty_to_close}/{int(pos.contracts)} spreads"
                                )
                                self._close_option(occ_sym, all_positions=all_positions, qty=qty_to_close)
                                pos.scaled_out_qty = qty_to_close
                                pos.tier1_closed = True
                                pos.tier1_scale_out_stop = entry_mark * (1 + OPTIONS_PROFIT_TARGET_1_STOP_PCT / 100)
                                log.info(
                                    f"OPTIONS: {pos.symbol} second half: {qty_remaining} spreads, "
                                    f"new stop at +{OPTIONS_PROFIT_TARGET_1_STOP_PCT:.0f}%, "
                                    f"target at +{OPTIONS_PROFIT_TARGET_2_PCT:.0f}%"
                                )
                        elif _is_short:
                            # Naked SHORT: close full position at target (BUY-to-close needs margin)
                            log.info(
                                f"OPTIONS: {pos.symbol} naked short at +{pnl_pct:.1f}% "
                                f"— closing full position (buying power constraint)"
                            )
                            to_close.append(occ_sym)
                            pos.tier1_closed = True
                        else:
                            # Naked LONG: scale out 50% at +50%, hold 50% with +20% stop
                            # Alpaca requires integer qty — if only 1 contract, close all
                            qty_to_close = int(pos.contracts) // 2
                            
                            if qty_to_close < 1:
                                # 1-contract position: can't split — close full position
                                log.info(
                                    f"OPTIONS: {pos.symbol} single contract at +{pnl_pct:.1f}% "
                                    f"— closing full position (can't scale out 1 contract)"
                                )
                                to_close.append(occ_sym)
                                pos.tier1_closed = True
                            else:
                                qty_remaining = int(pos.contracts) - qty_to_close
                                
                                log.info(
                                    f"OPTIONS: {pos.symbol} scale-out triggered at +{pnl_pct:.1f}% "
                                    f"— closing {qty_to_close}/{int(pos.contracts)} contracts at market"
                                )
                                
                                # Close first half at market via _close_option partial
                                try:
                                    self._close_option(occ_sym, all_positions=all_positions, qty=qty_to_close)
                                    log.info(
                                        f"OPTIONS: {pos.symbol} first half closed: {qty_to_close} contracts "
                                        f"(P&L: +{pnl_pct:.1f}%)"
                                    )
                                except Exception as e:
                                    log.warning(f"OPTIONS: {pos.symbol} scale-out close failed: {e}")
                                    continue
                                
                                pos.scaled_out_qty = qty_to_close
                                pos.tier1_closed = True
                                pos.tier1_scale_out_stop = entry_mark * (1 + OPTIONS_PROFIT_TARGET_1_STOP_PCT / 100)
                                
                                log.info(
                                    f"OPTIONS: {pos.symbol} second half position: {qty_remaining} contracts, "
                                    f"new stop at +{OPTIONS_PROFIT_TARGET_1_STOP_PCT:.0f}% (${pos.tier1_scale_out_stop:.2f}), "
                                    f"target at +{OPTIONS_PROFIT_TARGET_2_PCT:.0f}%"
                                )

                elif pnl_pct >= OPTIONS_PROFIT_TARGET_2_PCT and pos.tier1_closed:
                    # Second half: close remaining at max profit target
                    if not pdt_block:
                        log.info(
                            f"OPTIONS: {pos.symbol} second-half target hit "
                            f"({pnl_pct:.1f}% >= {OPTIONS_PROFIT_TARGET_2_PCT:.0f}%) — closing remaining {pos.contracts}"
                        )
                        to_close.append(occ_sym)

                # ── Second-half stop loss (if scaled out): New stop at +20% (breakeven guard) ────
                if pos.tier1_closed and pnl_pct <= OPTIONS_PROFIT_TARGET_1_STOP_PCT:
                    # Second half hit its new stop at +20% — close to lock minimum gain
                    if not pdt_block and not in_grace_period:
                        log.warning(
                            f"OPTIONS: {pos.symbol} second-half stop hit at +{pnl_pct:.1f}% "
                            f"(stop={OPTIONS_PROFIT_TARGET_1_STOP_PCT:.0f}%) — closing remaining {pos.contracts}"
                        )
                        to_close.append(occ_sym)

                # ── Percentage-based stop loss: ONLY for naked options, NOT spreads ────
                # Multi-leg spreads have complex mark pricing; SL is managed by Schwab stop orders.
                elif not _is_mleg and pnl_pct <= -_eff_stop:
                    # Grace period: don't stop-out within first N days of entry
                    if in_grace_period:
                        # Still within grace period — hold even if stop is hit
                        log.debug(
                            f"OPTIONS: {pos.symbol} at stop ({pnl_pct:.1f}%) but in grace period "
                            f"({days_held}d < {OPTIONS_ENTRY_GRACE_DAYS}d) — holding"
                        )
                    elif pos.open_stop_pct > 0:
                        # Open-window entry with tighter 25% stop (overrides grace period)
                        log.warning(
                            f"OPTIONS: {pos.symbol} open-window stop {_eff_stop:.0f}% hit "
                            f"({pnl_pct:.1f}%) — closing"
                        )
                        to_close.append(occ_sym)
                        stop_symbols.append(pos.symbol)
                    elif not pdt_block:
                        log.warning(f"OPTIONS: {pos.symbol} stop hit ({pnl_pct:.1f}%) — closing")
                        to_close.append(occ_sym)
                        stop_symbols.append(pos.symbol)

                # 5. Trailing stop — arms once peak >= confidence-tiered threshold
                # Fires when pnl drops confidence-tiered drawdown pp below the peak.
                # Only evaluated when the fixed stop and target haven't already triggered.
                # Skipped during grace period to avoid noise.
                tiers = get_tiered_profit_targets(pos.entry_confidence)
                trail_activate = tiers['trail_activate_pct']
                trail_drawdown = tiers['trail_drawdown_pct']
                
                if (
                    not in_grace_period
                    and not pdt_block
                    and pos.peak_pnl_pct >= trail_activate
                    and pnl_pct <= pos.peak_pnl_pct - trail_drawdown
                ):
                    log.info(
                        f"OPTIONS: {pos.symbol} trailing stop (conf={pos.entry_confidence:.0%}) — "
                        f"peak={pos.peak_pnl_pct:.1f}% "
                        f"current={pnl_pct:.1f}% (drawdown {pos.peak_pnl_pct - pnl_pct:.1f}pp "
                        f"> {trail_drawdown:.0f}pp threshold) — closing"
                    )
                    to_close.append(occ_sym)

            except Exception as e:
                log.error(f"Error monitoring {occ_sym}: {e}")
                # Skip this position and continue with next

        # Execute closes — pass all_positions so _close_option can compute limit price
        for occ_sym in to_close:
            self._close_option(occ_sym, all_positions=all_positions)

        # Record stop cooldowns so stopped symbols can't re-enter within OPTIONS_STOP_COOLDOWN_DAYS
        for sym in stop_symbols:
            record_stop_cooldown(sym)

        # IV conversion check (rate-limited to every 10 min, touches only open naked positions)
        self._maybe_convert_to_spread()

    def _close_option(self, occ_sym: str, all_positions: Optional[dict] = None, qty: Optional[int] = None) -> None:
        """Close an options position by reversing all stored legs atomically.
        Works identically for single, spread, butterfly, and condor positions.

        For multi-leg positions uses a limit order at current_net_mid * 0.97 to avoid
        market-maker slippage on wide bid/ask spreads (AEHR/EOSE/SOUN style names).
        Falls back to market if current mark cannot be computed.

        qty: if provided, close only that many contracts (partial scale-out).
             If qty < pos.contracts, position remains tracked with reduced contracts.
        """
        pos = self._positions.get(occ_sym)
        if pos is None:
            return
        
        close_qty = qty if qty is not None else int(pos.contracts)
        is_partial = close_qty < int(pos.contracts)

        try:
            if len(pos.legs) > 1:
                # Multi-leg: reverse every side and submit as a single mleg limit order
                reversed_legs = [
                    {
                        "symbol": l["occ_symbol"],
                        "side": "sell" if l["side"] == "buy" else "buy",
                        "ratio_qty": str(int(round(l["ratio_qty"]))),
                        "position_intent": (
                            "buy_to_close" if l["side"] == "sell" else "sell_to_close"
                        ),
                    }
                    for l in pos.legs
                ]

                # Compute net mark price to set a sensible limit.
                # For a closing order the net direction is reversed vs entry:
                #   debit entry  → closing is a credit (we receive) → limit < 0 in Alpaca convention
                #   credit entry → closing is a debit (we pay)      → limit > 0
                # We target 97% of the current mid to guarantee a fill without full-spread
                # slippage on all legs simultaneously (critical for low-liquidity names).
                _close_limit_price = None
                if all_positions:
                    try:
                        net_mid = 0.0
                        for l in pos.legs:
                            lp = all_positions.get(l["occ_symbol"])
                            if lp is None:
                                raise ValueError("leg missing")
                            sign = 1 if l["side"] == "buy" else -1
                            net_mid += sign * l["ratio_qty"] * float(lp.current_price)
                        # For closing: reverse the sign (we're doing the opposite trade)
                        close_mid = -net_mid

                        # Safety: for long-debit mleg positions (butterfly, debit spread)
                        # NEVER pay to close when mark has gone negative — that would cost
                        # more than the original debit (exceeding defined max loss).
                        # Correct action: let it expire worthless. Max loss = debit paid.
                        _is_long_debit_mleg = (
                            pos.action == "buy_to_open"
                            and ("butterfly" in pos.option_type.lower()
                                 or "butterfly" in pos.strategy.lower()
                                 or "spread" in pos.strategy.lower())
                        )
                        if close_mid > 0 and _is_long_debit_mleg:
                            log.info(
                                f"[OPTIONS] {pos.symbol} mleg close would cost "
                                f"${close_mid:.2f} (mark negative) — letting expire worthless. "
                                f"Closing would exceed defined max loss."
                            )
                            del self._positions[occ_sym]
                            return

                        if abs(close_mid) > 0.01:
                            # Debit close (we pay): limit slightly above mid to ensure fill
                            # Credit close (we receive): limit at 97% of mid (accept slightly less)
                            if close_mid > 0:
                                _close_limit_price = round(close_mid * 1.03, 2)   # paying — bid up 3%
                            else:
                                _close_limit_price = round(close_mid * 0.97, 2)   # receiving — 3% haircut
                    except Exception as _e:
                        log.debug(f"[OPTIONS] Could not compute mleg close limit for {occ_sym}: {_e}")

                payload = {
                    "symbol": "",
                    "qty": str(close_qty),
                    # "side" intentionally omitted — not required for mleg per Alpaca docs;
                    # each reversed leg carries its own side.
                    "order_class": "mleg",
                    "time_in_force": "day",
                    "legs": reversed_legs,
                }
                if _close_limit_price is not None:
                    payload["type"] = "limit"
                    payload["limit_price"] = str(_close_limit_price)
                    log.info(
                        f"OPTIONS MLEG CLOSE (limit @ {_close_limit_price:+.2f}): "
                        f"{pos.symbol} {pos.strategy} ({len(pos.legs)} legs, {close_qty}/{int(pos.contracts)} contract(s))"
                    )
                else:
                    payload["type"] = "market"
                    log.info(
                        f"OPTIONS MLEG CLOSE (market fallback): {pos.symbol} {pos.strategy} "
                        f"({len(pos.legs)} legs, {close_qty}/{int(pos.contracts)} contract(s))"
                    )
                self.client.post("/orders", payload)

            else:
                # Single leg — always use a limit order (Alpaca rejects market orders
                # for options when no quote is available, error 40310000).
                side = OrderSide.SELL if pos.action == "buy_to_open" else OrderSide.BUY
                _close_limit = None

                # First try: use passed-in all_positions for the mark price
                if all_positions:
                    _lp = all_positions.get(occ_sym)
                    if _lp is not None:
                        _cur = float(_lp.current_price)
                        if _cur > 0.01:
                            # Selling to close: accept 97% of mid to guarantee a fill
                            # Buying to close: pay up 3% to get out quickly
                            _close_limit = round(_cur * 0.97, 2) if side == OrderSide.SELL else round(_cur * 1.03, 2)

                # Second try: fetch a fresh broker quote if still no limit price
                if _close_limit is None:
                    try:
                        _fresh = {p.symbol: p for p in self.client.get_all_positions()}
                        _lp = _fresh.get(occ_sym)
                        if _lp is not None:
                            _cur = float(_lp.current_price)
                            if _cur > 0.01:
                                _close_limit = round(_cur * 0.97, 2) if side == OrderSide.SELL else round(_cur * 1.03, 2)
                    except Exception as _fe:
                        log.debug(f"[OPTIONS] Could not fetch fresh quote for {occ_sym}: {_fe}")

                # Final fallback: near-worthless or unquoted contract — use floor limit
                # Alpaca requires a minimum limit price of $0.05 for options.
                if _close_limit is None:
                    _close_limit = 0.05 if side == OrderSide.SELL else 0.50
                    log.warning(
                        f"[OPTIONS] No quote for {occ_sym} — limit fallback @{_close_limit:.2f} "
                        f"({'floor sell' if side == OrderSide.SELL else 'ceiling buy'})"
                    )

                order_req = LimitOrderRequest(
                    symbol=occ_sym,
                    qty=close_qty,
                    side=side,
                    limit_price=_close_limit,
                    time_in_force=TimeInForce.DAY,
                )
                log.info(f"OPTIONS CLOSE (limit @{_close_limit:.2f}): {side.value.upper()} {close_qty}x {occ_sym}")
                self.client.submit_order(order_req)

            if is_partial:
                # Partial scale-out: update remaining contracts, don't remove from tracker
                pos.contracts = int(pos.contracts) - close_qty
                log.info(
                    f"OPTIONS: {pos.symbol} partial close {close_qty} contracts — "
                    f"{pos.contracts} remaining"
                )
            else:
                del self._positions[occ_sym]

        except Exception as e:
            if "40310100" in str(e):
                # PDT protection — Alpaca rejected the close because it would be a day trade.
                # Leave the position in the tracker so it is retried next session.
                log.warning(
                    f"[OPTIONS] PDT block on close {occ_sym} — position held, will retry next session"
                )
            elif "40310000" in str(e):
                # No available quote — contract is expired, halted, or worthless.
                # There is no market to close into; remove from tracker (max loss already realized).
                log.warning(
                    f"[OPTIONS] No quote available to close {occ_sym} — contract likely expired/worthless. "
                    f"Removing from tracker (max loss realized)."
                )
                self._positions.pop(occ_sym, None)
            else:
                log.error(f"Options close failed for {occ_sym}: {e}", exc_info=True)

    def close_all(self) -> None:
        """Emergency: close all open options positions."""
        for occ_sym in list(self._positions.keys()):
            self._close_option(occ_sym)

    # ── Status ─────────────────────────────────────────────────────────────────

    def status_summary(self) -> str:
        if not self._positions:
            return "Options: no open positions"
        lines = [f"Options: {len(self._positions)} position(s)"]
        today = datetime.date.today()
        for occ_sym, pos in self._positions.items():
            dte = (pos.expiry - today).days
            lines.append(
                f"  {occ_sym} | {pos.contracts}x {pos.strategy} "
                f"entry=${pos.entry_price:.2f} DTE={dte}"
            )
        return "\n".join(lines)
