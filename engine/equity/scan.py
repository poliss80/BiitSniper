"""ApexTrader scan nucleus.

Contains reusable scanning functions for main loop and run_top3 tools.
"""

import datetime
import logging
import threading
import pytz
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Tuple, Set, Optional

from engine import config as _cfg
from engine.config import (
    SCAN_MAX_SYMBOLS,
    SCAN_WORKERS,
    SCAN_SYMBOL_TIMEOUT,
    MIN_DOLLAR_VOLUME,
    MIN_STOCK_PRICE,
    LONG_ONLY_MODE,
    MIN_SIGNAL_CONFIDENCE,
    MAX_SIGNALS_PER_CYCLE,
    RVOL_MIN,
    MAX_GAP_CHASE_PCT,
    GAP_CHASE_CONSOL_BARS,
    TI_MAX_GAP_CHASE_PCT,
    TI_RVOL_MIN,
    TI_MIN_DOLLAR_VOLUME,
    TI_MAX_OVERNIGHT_GAP_PCT,
    BEAR_SHORT_UNIVERSE,
)
from engine.utils import MarketState, clear_bar_cache, get_bars, is_dead_ticker
from engine.utils.bars import get_feed_used as _get_feed_used

# IEX (free) feed captures roughly 15% of consolidated volume vs SIP.
# MDA (Market Data App) provides full consolidated (SIP-equivalent) data, so
# IEX scaling is disabled when MDA is the primary source.
_IEX_THRESHOLD_SCALE = 0.15
import os as _scan_os


def _mda_available() -> bool:
    """Return True when the MDA API key is present in the environment.

    Evaluated lazily (at call time) so it works correctly after load_dotenv().
    """
    return bool(_scan_os.environ.get("MARKETDATA_API_KEY"))


def _is_iex_feed() -> bool:
    """Always return False since market data comes from Schwab (not Alpaca/MDA).

    Schwab provides consolidated data, so IEX threshold scaling is not needed.
    """
    return False
from .universe import get_tier as _get_tier_live, get_latest_batch as _get_latest_batch, get_ti_primary as _get_ti_primary
from .discovery import get_priority_scan_queue as _get_priority_scan_queue

_ET  = pytz.timezone("America/New_York")
_log = logging.getLogger("ApexTrader")

# ── Adaptive Filter State ──
_adaptive_state = {
    "empty_scans": 0,
    "rvol_min": RVOL_MIN,
    "min_conf": MIN_SIGNAL_CONFIDENCE,
}
_ADAPTIVE_MAX_EMPTY = 3  # Number of empty scans before relaxing
_ADAPTIVE_MIN_RVOL = 1.2
_ADAPTIVE_MIN_CONF = 0.60
_ADAPTIVE_STEP_RVOL = 0.2
_ADAPTIVE_STEP_CONF = 0.03
from .strategies import get_strategy_instances, MomentumStrategy, TechnicalStrategy, SentimentStrategy
from engine.data.alpaca_news import annotate_signal_with_news
from engine.utils.market import _is_bull_regime, _INVERSE_ETFS

# Rotating scan offset — advances by SCAN_MAX_SYMBOLS each call so different
# slices of the universe are covered across consecutive cycles.
_scan_offset: int = 0

# ── TI stocks tracking ─────────────────────────────────────────────────────────
# Set of symbols from Trade Ideas (momentum/HSF stocks).
# Populated by get_scan_targets() and used by _passes_guardrails() to apply
# stricter guardrails (lower gap%, higher RVOL, higher dollar-vol).
_ti_stocks: Set[str] = set()

# ── Batch snapshot caches ─────────────────────────────────────────────────────
# _snapshot_cache    : Alpaca snapshot objects (fallback when MDA unavailable)
# _mda_snapshot_cache: MDA bulk-quote dicts {price, volume, open} (primary)
# Populated once at the start of each scan_universe() call.
# _passes_guardrails() checks _mda_snapshot_cache first, then _snapshot_cache,
# then falls back to per-symbol get_bars() (which also uses MDA).
_snapshot_cache: Dict = {}
_mda_snapshot_cache: Dict[str, Dict] = {}
_guardrail_cache: Dict[str, Dict[str, object]] = {}
_guardrail_lock = threading.RLock()


def _prefetch_snapshots(symbols: List[str]) -> None:
    """Batch-fetch price/volume snapshots for *symbols*.

    Primary:  MDA bulk quotes (consolidated, full-feed data) → _mda_snapshot_cache
    Fallback: Alpaca StockSnapshotRequest → _snapshot_cache

    _passes_guardrails() checks _mda_snapshot_cache first, then _snapshot_cache,
    then falls back to per-symbol get_bars() (MDA-primary).
    """
    global _snapshot_cache, _mda_snapshot_cache
    _snapshot_cache = {}
    _mda_snapshot_cache = {}
    if not symbols:
        return

    # MDA bulk quotes removed — relying on per-symbol bar fetch in _passes_guardrails


def _guardrail_cache_key(symbol: str) -> str:
    return symbol.upper()


def _cached_frame_or_default(cached_obj, fallback_obj):
    if cached_obj is not None and hasattr(cached_obj, "empty"):
        try:
            if not cached_obj.empty:
                return cached_obj
        except Exception:
            pass
    if cached_obj is not None:
        return cached_obj
    return fallback_obj


def _get_or_load_guardrail_data(symbol: str) -> Dict[str, object]:
    """Load and cache the minimal bar slices needed for a symbol's guardrail pass."""
    key = _guardrail_cache_key(symbol)
    with _guardrail_lock:
        cached = _guardrail_cache.get(key)
        if cached is not None:
            return cached

    data: Dict[str, object] = {}
    try:
        intraday = get_bars(symbol, "1d", "1m")
        if not intraday.empty:
            data["intraday_1d_1m"] = intraday
        past_intraday = get_bars(symbol, "6d", "1m")
        if not past_intraday.empty:
            data["intraday_6d_1m"] = past_intraday
        daily_hist = get_bars(symbol, "20d", "1d")
        if not daily_hist.empty:
            data["daily_20d_1d"] = daily_hist
        if is_ti_stock := symbol in _ti_stocks:
            try:
                yesterday = get_bars(symbol, "2d", "1d")
                if not yesterday.empty:
                    data["daily_2d_1d"] = yesterday
            except Exception:
                pass
    except Exception:
        pass

    _guardrail_cache[key] = data
    return data


def _passes_guardrails(symbol: str, bull_regime: bool = None, market_state: Optional[MarketState] = None, return_reason: bool = False, is_ti_stock: bool = False) -> bool:
    """Pre-scan gates: dollar-volume, RVOL, and gap-chase guard.
    Returns False to skip the symbol; never raises.

    bull_regime: pass the pre-computed regime from scan_universe() to avoid
    a concurrent re-fetch of _is_bull_regime() inside each worker thread.
    If None, falls back to calling _is_bull_regime() directly.

    market_state: shared MarketState for the current scan cycle. If None,
    it will be created lazily.

    is_ti_stock: True if this symbol is from Trade Ideas (momentum/HSF stocks).
    When True, applies stricter guardrails: lower gap-chase %, higher RVOL,
    higher dollar-volume, and checks for large overnight gaps.
    """
    # return_reason is now an explicit argument
    try:
        guard_data = _get_or_load_guardrail_data(symbol)
        # ── Fast path 1: MDA bulk-quote cache (consolidated, full-feed) ───────
        _mda_snap = _mda_snapshot_cache.get(symbol)
        if _mda_snap is not None:
            price    = _mda_snap["price"]
            day_vol  = _mda_snap["volume"]
            open_px  = _mda_snap["open"]
            intraday = guard_data.get("intraday_1d_1m")
        # ── Fast path 2: Alpaca batch-prefetched snapshot (fallback) ─────────
        elif (
            _snapshot_cache.get(symbol) is not None
            and _snapshot_cache[symbol].daily_bar is not None
            and _snapshot_cache[symbol].latest_trade is not None
        ):
            _snap   = _snapshot_cache[symbol]
            price   = float(_snap.latest_trade.price)
            day_vol = float(_snap.daily_bar.volume)
            open_px = float(_snap.daily_bar.open)
            intraday = guard_data.get("intraday_1d_1m")
        else:
            # ── Fallback: fetch 1-min intraday bars ───────────────────────────
            intraday = _cached_frame_or_default(guard_data.get("intraday_1d_1m"), get_bars(symbol, "1d", "1m"))
            if intraday is None or getattr(intraday, "empty", False) or len(intraday) < 5:
                if return_reason:
                    return False, 'other'
                return False
            price   = float(intraday["close"].iloc[-1])
            day_vol = float(intraday["volume"].sum())
            open_px = float(intraday["open"].iloc[0])

        # ── TI OVERNIGHT GAP CHECK: Skip if massive pre-market move ────────────
        # Trade Ideas momentum stocks often gap hard overnight. Skip if >12% gap
        # to avoid chasing already-extended moves on poor risk/reward.
        if is_ti_stock and open_px > 0:
            try:
                # Fetch yesterday's daily bar to calc overnight gap
                yesterday_bars = _cached_frame_or_default(guard_data.get("daily_2d_1d"), get_bars(symbol, "2d", "1d"))
                if yesterday_bars is not None and hasattr(yesterday_bars, "empty") and not yesterday_bars.empty and len(yesterday_bars) >= 1:
                    yesterday_close = float(yesterday_bars["close"].iloc[-2]) if len(yesterday_bars) >= 2 else None
                    if yesterday_close and yesterday_close > 0:
                        overnight_gap = ((open_px - yesterday_close) / yesterday_close) * 100
                        if abs(overnight_gap) > TI_MAX_OVERNIGHT_GAP_PCT:
                            _log.debug(
                                f"[GUARDRAIL] {symbol} blocked: TI stock overnight gap {overnight_gap:.1f}% > "
                                f"TI_MAX_OVERNIGHT_GAP_PCT {TI_MAX_OVERNIGHT_GAP_PCT}%"
                            )
                            if return_reason:
                                return False, 'overnight_gap'
                            return False
            except Exception as _gap_err:
                _log.debug(f"[TI] {symbol}: overnight gap check failed ({_gap_err}) — continuing")

        # Resolve regime, VIX, and IEX-feed status once — reused throughout
        vix = None
        if hasattr(market_state, 'vix') and market_state.vix is not None:
            vix = market_state.vix
        elif hasattr(market_state, 'resolve_vix'):
            vix, _, _ = market_state.resolve_vix()
        bull = bull_regime if bull_regime is not None else market_state.resolve_regime()
        iex_feed = _is_iex_feed()

        # Adaptive MIN_STOCK_PRICE: more flexible for current market
        base_min_price = MIN_STOCK_PRICE
        base_dollar_vol = MIN_DOLLAR_VOLUME
        base_rvol = RVOL_MIN

        # ── TI STOCK OVERRIDES: Stricter guardrails for momentum/HSF stocks ────
        if is_ti_stock:
            base_dollar_vol = TI_MIN_DOLLAR_VOLUME
            base_rvol = TI_RVOL_MIN

        if bull:
            if vix and vix > 25:
                adaptive_min_price = base_min_price + 0.5
                adaptive_dollar_vol = base_dollar_vol * 1.2
                adaptive_rvol = base_rvol + 0.3           # 1.3 — choppy bull, need real surge
            elif vix and vix >= 18:
                adaptive_min_price = base_min_price
                adaptive_dollar_vol = base_dollar_vol
                adaptive_rvol = max(0.8, base_rvol - 0.2) # 0.8
            elif vix and vix >= 15:
                adaptive_min_price = base_min_price
                adaptive_dollar_vol = base_dollar_vol * 0.9
                adaptive_rvol = max(0.5, base_rvol - 0.5) # 0.5
            else:
                adaptive_min_price = max(1.0, base_min_price - 0.5)
                adaptive_dollar_vol = base_dollar_vol * 0.8
                adaptive_rvol = max(0.4, base_rvol - 0.6) # 0.4
        else:
            if vix and vix < 18:
                adaptive_min_price = max(1.0, base_min_price - 0.7)
                adaptive_dollar_vol = base_dollar_vol * 0.6
                adaptive_rvol = max(0.4, base_rvol - 0.7) # 0.4
            else:
                adaptive_min_price = max(1.0, base_min_price - 0.5)
                adaptive_dollar_vol = base_dollar_vol * 0.75
                adaptive_rvol = max(0.6, base_rvol - 0.4) # 0.6

        if price < adaptive_min_price:
            _log.warning(f"[GUARDRAIL] {symbol} blocked: price {price:.2f} < adaptive_min_price {adaptive_min_price}")
            if return_reason:
                return False, 'min_price'
            return False

        # Adaptive RVOL_MIN: higher in bull/high VIX, lower in calm or bear conditions
        # Use regular market hours only so extended-hours volume does not distort the pace.
        #
        # ── History-depth tiers ───────────────────────────────────────────────
        # Tier 1 — 0 daily bars:   skip (no coverage); log [NEW_LISTING]
        # Tier 2 — 1–4 daily bars: no RVOL baseline exists; gate on absolute
        #                          dollar-volume pace instead (avoids blocking
        #                          a genuine high-volume day-1/2 IPO)
        # Tier 3 — 5–14 daily bars: RVOL@TIME with threshold discounted by
        #                          session count (fewer ref sessions = more noise)
        # Tier 4 — 15+ daily bars: full RVOL@TIME threshold (normal operation)
        # ─────────────────────────────────────────────────────────────────────
        _rvol_gate_applied = False  # track whether RVOL@TIME block ran (skip legacy gate below)
        if market_state.is_regular_hours and bull:
            # Determine history depth from daily bars (already cheap — MDA/Alpaca daily)
            _daily_hist = _cached_frame_or_default(guard_data.get("daily_20d_1d"), get_bars(symbol, "20d", "1d"))
            _n_daily = len(_daily_hist)

            if _n_daily == 0:
                # Tier 1: no coverage at all — skip immediately
                _log.info(f"[NEW_LISTING] {symbol}: no historical bar data available — skipping")
                if return_reason:
                    return False, 'other'
                return False

            elif _n_daily <= 4:
                # Tier 2: IPO / very new listing (days 1–4) — no reliable RVOL baseline.
                # Gate on raw dollar-volume pace: require $750K * elapsed_frac of the day
                # (~$1.5M/day equivalent). This lets through genuine high-volume new listings
                # (e.g. day-2 IPO running 85× volume) while blocking quiet ones.
                now_et   = datetime.datetime.now(_ET)
                mkt_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
                elapsed_min  = int(max((now_et - mkt_open).total_seconds() / 60, 1))
                elapsed_frac = min(elapsed_min / 390.0, 1.0)
                dollar_vol_now = price * day_vol
                _NEW_LISTING_DV_THRESHOLD = 750_000
                dv_threshold = _NEW_LISTING_DV_THRESHOLD * max(elapsed_frac, 0.05)
                _log.info(
                    f"[NEW_LISTING] {symbol}: {_n_daily}d history — "
                    f"using dollar-vol gate (no RVOL baseline). "
                    f"dv={dollar_vol_now:,.0f} threshold={dv_threshold:,.0f} "
                    f"elapsed_frac={elapsed_frac:.2f}"
                )
                if dollar_vol_now < dv_threshold:
                    _log.warning(
                        f"[GUARDRAIL] {symbol} blocked: new listing dollar-vol "
                        f"{dollar_vol_now:,.0f} < {dv_threshold:,.0f} "
                        f"({_n_daily}d history)"
                    )
                    if return_reason:
                        return False, 'dollar_vol'
                    return False
                _rvol_gate_applied = True

            else:
                # Tier 3 / Tier 4: enough history for RVOL@TIME
                # Fetch today's and past bars via get_bars (MDA-primary)
                today_intraday = _cached_frame_or_default(guard_data.get("intraday_1d_1m"), get_bars(symbol, "1d", "1m"))
                past_intraday  = _cached_frame_or_default(guard_data.get("intraday_6d_1m"), get_bars(symbol, "6d", "1m"))
                now_et = datetime.datetime.now(_ET)
                mkt_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
                elapsed_min = int(max((now_et - mkt_open).total_seconds() / 60, 1))
                elapsed_min = min(elapsed_min, 390)

                # ── RVOL@TIME: timestamp-based same-window comparison ─────────────
                # Use actual ET timestamps to slice each prior session's bars to the
                # same elapsed-minute window as today.  Robust against:
                #   • sessions with early closes / halts (different bar counts)
                #   • data gaps (missing bars within a session)
                #   • weekends in the calendar-day loop
                # Partial sessions (≥50% of expected bars) are accepted and scaled
                # proportionally so they compare on the same elapsed-time basis.
                # Tier 3 (5–14 daily bars): threshold discounted by session count —
                # fewer reference sessions = noisier baseline = lower bar to clear.
                #   4 sessions → 100% threshold
                #   3 sessions →  90%
                #   2 sessions →  80%
                #   1 session  →  65%
                # Falls back to fractional legacy RVOL only when no valid sessions exist.
                _rvol_computed = False
                cutoff_today = mkt_open + datetime.timedelta(minutes=elapsed_min)
                if not today_intraday.empty and len(today_intraday) >= 3:
                    try:
                        today_mask = (
                            (today_intraday["time"] >= mkt_open) &
                            (today_intraday["time"] <  cutoff_today)
                        )
                        today_vol = float(today_intraday.loc[today_mask, "volume"].sum())

                        avg_vols = []
                        if not past_intraday.empty:
                            for _d in range(1, 8):   # walk back up to 7 cal-days to find 5 sessions
                                if len(avg_vols) >= 5:
                                    break
                                prior_date = (now_et - datetime.timedelta(days=_d)).date()
                                if prior_date.weekday() >= 5:   # skip Saturday/Sunday
                                    continue
                                day_open_dt = _ET.localize(
                                    datetime.datetime.combine(prior_date, datetime.time(9, 30))
                                )
                                day_cutoff  = day_open_dt + datetime.timedelta(minutes=elapsed_min)
                                day_mask    = (
                                    (past_intraday["time"] >= day_open_dt) &
                                    (past_intraday["time"] <  day_cutoff)
                                )
                                day_slice = past_intraday.loc[day_mask, "volume"]
                                n = len(day_slice)
                                # Accept sessions with >=20% bar density (micro-caps trade
                                # only ~20-35% of minutes — 50% was too strict and caused
                                # all illiquid symbols to fall through to the legacy fallback).
                                if n >= max(3, int(elapsed_min * 0.20)):
                                    # Scale partial sessions to full elapsed-window equivalent
                                    avg_vols.append(float(day_slice.sum()) * (elapsed_min / n))

                        if avg_vols:
                            avg_intraday_vol = sum(avg_vols) / len(avg_vols)
                            rvol = today_vol / max(avg_intraday_vol, 1)
                            # Session-count discount: fewer reference sessions → lower threshold
                            _session_discount = {1: 0.65, 2: 0.80, 3: 0.90}.get(len(avg_vols), 1.0)
                            rvol_threshold = (
                                adaptive_rvol * _session_discount *
                                (_IEX_THRESHOLD_SCALE if iex_feed else 1.0)
                            )
                            _log.info(
                                f"[RVOL@TIME] {symbol}: today_vol={today_vol:.0f}, "
                                f"avg_intraday_vol={avg_intraday_vol:.0f} ({len(avg_vols)} sessions), "
                                f"elapsed_min={elapsed_min}, rvol={rvol:.3f}, "
                                f"threshold={rvol_threshold:.2f} "
                                f"(adaptive={adaptive_rvol:.2f} "
                                f"session_discount={_session_discount:.2f} "
                                f"iex_scale={iex_feed})"
                            )
                            if not iex_feed and rvol < rvol_threshold:
                                _log.warning(
                                    f"[GUARDRAIL] {symbol} blocked: RVOL@TIME {rvol:.2f} < "
                                    f"rvol_threshold {rvol_threshold:.2f} "
                                    f"(adaptive={adaptive_rvol:.2f} "
                                    f"sessions={len(avg_vols)} discount={_session_discount:.2f}) "
                                    f"| today_vol={today_vol:.0f} | avg_intraday_vol={avg_intraday_vol:.0f}"
                                )
                                if return_reason:
                                    return False, 'rvol'
                                return False
                            _rvol_computed = True
                    except Exception as _rvol_err:
                        _log.debug(
                            f"[RVOL@TIME] {symbol}: timestamp-based RVOL failed "
                            f"({_rvol_err}) — using fallback"
                        )

                if not _rvol_computed:
                    # Fallback: fractional legacy RVOL — always gates (never silent pass-through)
                    elapsed_frac = elapsed_min / 390.0
                    # Reuse _daily_hist already fetched above — no extra API call
                    if not _daily_hist.empty and len(_daily_hist) >= 2:
                        avg_daily_vol = float(_daily_hist["volume"].iloc[:-1].mean())
                    else:
                        avg_daily_vol = 1.0
                    today_vol_fb = float(day_vol)
                    denom = avg_daily_vol * max(elapsed_frac, 0.02)
                    rvol_fallback = today_vol_fb / denom if denom > 0 else 0.0
                    rvol_threshold = adaptive_rvol * (_IEX_THRESHOLD_SCALE if iex_feed else 1.0)
                    if not iex_feed and rvol_fallback < rvol_threshold:
                        # Only warn (and block) when the symbol actually fails the gate
                        _log.warning(
                            f"[RVOL@TIME FALLBACK] {symbol}: no valid prior-session bars — "
                            f"fractional legacy RVOL: rvol={rvol_fallback:.3f}, "
                            f"threshold={rvol_threshold:.2f} "
                            f"(avg_daily={avg_daily_vol:.0f} elapsed_frac={elapsed_frac:.3f} "
                            f"iex_scale={iex_feed})"
                        )
                        _log.warning(
                            f"[GUARDRAIL] {symbol} blocked: RVOL_FALLBACK {rvol_fallback:.2f} < "
                            f"rvol_threshold {rvol_threshold:.2f} (adaptive={adaptive_rvol:.2f}) "
                            f"| today_vol={today_vol_fb:.0f} | denom={denom:.0f}"
                        )
                        if return_reason:
                            return False, 'rvol'
                        return False
                    else:
                        # Passing symbols: debug only (no need to spam WARNING for every scan)
                        _log.debug(
                            f"[RVOL@TIME FALLBACK] {symbol}: no prior-session 1m bars — "
                            f"fallback rvol={rvol_fallback:.3f} >= threshold={rvol_threshold:.2f} "
                            f"(avg_daily={avg_daily_vol:.0f} elapsed_frac={elapsed_frac:.3f}) — passing"
                        )
                        # Data-quality gate: the fractional legacy RVOL is an unreliable
                        # proxy. When real prior-session intraday bars are unavailable,
                        # block the entry rather than trade on an unconfirmed estimate.
                        if _cfg.REQUIRE_PRIOR_SESSION_BARS:
                            _log.warning(
                                f"[GUARDRAIL] {symbol} blocked: no valid prior-session bars "
                                f"for RVOL@TIME confirmation (REQUIRE_PRIOR_SESSION_BARS=on)"
                            )
                            if return_reason:
                                return False, 'rvol'
                            return False

                _rvol_gate_applied = True  # RVOL@TIME block ran — skip legacy gate below
                # Time-weighted dollar volume guardrail
                elapsed_frac = elapsed_min / 390.0
                # Scale threshold down when IEX feed is used (IEX ~15% of SIP volume)
                dv_scale = _IEX_THRESHOLD_SCALE if iex_feed else 1.0
                tw_dollar_vol = adaptive_dollar_vol * max(elapsed_frac, 0.05) * dv_scale
                dollar_vol = price * day_vol
                _log.debug(
                    f"[DOLLAR_VOL@TIME DEBUG] {symbol}: dollar_vol={dollar_vol:.0f}, "
                    f"tw_dollar_vol={tw_dollar_vol:.0f} (iex_scale={iex_feed}), "
                    f"elapsed_min={elapsed_min}, elapsed_frac={elapsed_frac:.3f}"
                )
                if dollar_vol < tw_dollar_vol:
                    _log.warning(
                        f"[GUARDRAIL] {symbol} blocked: dollar volume {dollar_vol:.0f} < "
                        f"tw_dollar_vol {tw_dollar_vol:.0f} (iex_scale={iex_feed}) "
                        f"| price={price:.2f} | day_vol={day_vol:.0f}"
                    )
                    if return_reason:
                        return False, 'dollar_vol'
                    return False

        # Adaptive MIN_DOLLAR_VOLUME: during market hours the time-weighted check above
        # already ran; only apply the raw absolute threshold outside regular hours.
        dollar_vol = price * day_vol
        eff_dollar_vol_threshold = adaptive_dollar_vol * (_IEX_THRESHOLD_SCALE if iex_feed else 1.0)
        if not market_state.is_regular_hours and dollar_vol < eff_dollar_vol_threshold:
            _log.warning(f"[GUARDRAIL] {symbol} blocked: dollar volume {dollar_vol:.0f} < eff_threshold {eff_dollar_vol_threshold:.0f} (adaptive={adaptive_dollar_vol:.0f} iex_scale={iex_feed}) | price={price:.2f} | day_vol={day_vol:.0f}")
            if return_reason:
                return False, 'dollar_vol'
            return False

        # RVOL gate (adaptive) — skipped when RVOL@TIME block already evaluated RVOL above
        if not _rvol_gate_applied and market_state.is_market_open and bull:
            _daily_for_gate = _cached_frame_or_default(guard_data.get("daily_20d_1d"), get_bars(symbol, "20d", "1d"))
            if _daily_for_gate is not None and hasattr(_daily_for_gate, "empty") and not _daily_for_gate.empty and len(_daily_for_gate) >= 2:
                avg_daily_vol = float(_daily_for_gate["volume"].iloc[:-1].mean())
                if avg_daily_vol > 0:
                    now_et       = datetime.datetime.now(_ET)
                    mkt_open     = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
                    elapsed_min  = max((now_et - mkt_open).total_seconds() / 60, 1.0)
                    elapsed_frac = min(elapsed_min / 390.0, 1.0)
                    rvol = (day_vol / max(elapsed_frac, 0.02)) / avg_daily_vol
                    rvol_threshold = adaptive_rvol * (_IEX_THRESHOLD_SCALE if iex_feed else 1.0)
                    if not iex_feed and rvol < rvol_threshold:
                        _log.warning(f"[GUARDRAIL] {symbol} blocked: RVOL {rvol:.2f} < rvol_threshold {rvol_threshold:.2f} (adaptive={adaptive_rvol:.2f} iex_scale={iex_feed}) | day_vol={day_vol:.0f} | avg_daily_vol={avg_daily_vol:.0f}")
                        if return_reason:
                            return False, 'rvol'
                        return False

        # Adaptive MAX_GAP_CHASE_PCT using market regime and VIX (already resolved above)
        # For TI stocks, use stricter base_gap to avoid chasing tail-end momentum
        base_gap = TI_MAX_GAP_CHASE_PCT if is_ti_stock else MAX_GAP_CHASE_PCT
        if bull:
            if vix and vix > 25:
                adaptive_gap = min(20.0, base_gap + 5.0)
            else:
                adaptive_gap = base_gap
        else:
            if vix and vix < 18:
                adaptive_gap = max(10.0, base_gap - 5.0)
            else:
                adaptive_gap = max(12.0, base_gap - 3.0)

        # Gap-chase guard: skip if up >adaptive_gap% without a tight consolidation base
        if open_px > 0:
            day_gain = ((price - open_px) / open_px) * 100
            if day_gain > adaptive_gap:
                # When 1-min bars are available, require tight recent consolidation.
                # With snapshot-only data, skip the consolidation check (conservative:
                # allows the signal — strategy-level filters apply next).
                _snap_fast_path = (
                    symbol in _mda_snapshot_cache
                    or (
                        _snapshot_cache.get(symbol) is not None
                        and _snapshot_cache[symbol].daily_bar is not None
                        and _snapshot_cache[symbol].latest_trade is not None
                    )
                )
                if not _snap_fast_path:
                    last_n    = intraday.iloc[-GAP_CHASE_CONSOL_BARS:]
                    bar_range = float(last_n["high"].max() - last_n["low"].min())
                    if bar_range > price * 0.02:  # range > 2% = no consolidation
                        _log.debug(f"[GUARDRAIL] {symbol} blocked: gap chase, bar range {bar_range:.2f} > 2% of price {price:.2f}")
                        if return_reason:
                            return False, 'gap_chase'
                        return False

        if return_reason:
            return True, None
        return True
    except Exception as e:
        _log.warning(f"Guardrail check failed for {symbol}: {e} — skipping symbol")
        if return_reason:
            return False, 'other'
        return False  # fail-safe: block on error, never bypass guardrails


def get_scan_targets(excluded: Set[str] = None) -> List[str]:
    global _scan_offset

    if excluded is None:
        excluded = set()

    delisted = set(_cfg.DELISTED_STOCKS)



    # PRIMARY: latest captured TI tickers from ti_primary.json.
    # FALLBACK: active TI tickers from universe.json tiers 1+2.
    ti_primary = [s for s in _get_ti_primary() if s not in delisted]

    # Always initialize p1 and p2 to avoid UnboundLocalError
    p1, p2 = [], []

    # Universe health check
    _MIN_TI = 5
    if len(ti_primary) == 0:
        _log.error("[UNIVERSE HEALTH] ti_primary.json is empty! No tickers to scan. Check data pipeline.")
    elif len(ti_primary) < _MIN_TI:
        _log.warning(f"[UNIVERSE HEALTH] ti_primary.json too small ({len(ti_primary)}). Falling back to static config lists.")
        p1, p2, _ = _cfg.get_dynamic_universe()
        # Alert if static lists are also empty
        if len(p1) + len(p2) == 0:
            _log.error("[UNIVERSE HEALTH] Static universe lists are empty! No tickers to scan. Check config/universe sources.")
    else:
        p1, p2 = ti_primary, []

    # live_p1/p2 = same as p1/p2 (TI-primary); kept for TI_FRONT push below.
    live_p1 = p1
    live_p2 = p2

    # Limit how many fresh TI primary tickers are guaranteed into each cycle.
    # This keeps the scan set tight and avoids scanning excessively large TI batches.
    max_fresh = min(_cfg.TI_PRIMARY_SCAN_BATCH_LIMIT, SCAN_MAX_SYMBOLS)
    latest_batch = ti_primary[:max_fresh] if ti_primary else [
        s for s in _get_latest_batch(window_minutes=5)
        if s not in delisted
    ]
    latest_batch = list(dict.fromkeys(latest_batch))[:max_fresh]

    # Rotate through the combined universe so every cycle scans a different slice.
    # TI-promoted tickers sit at the front and always make it in regardless of offset.
    combined_base = p2 + p1   # tier-2 first in bear, then tier-1
    slice_size = max(SCAN_MAX_SYMBOLS * 2, len(combined_base))   # rotation window
    if len(combined_base) > 0:
        off = _scan_offset % len(combined_base)
        rotated_base = combined_base[off:] + combined_base[:off]
    else:
        rotated_base = combined_base
    _scan_offset = (_scan_offset + SCAN_MAX_SYMBOLS) % max(len(combined_base), 1)

    # Default slice: top 50% from each list (marketscope360 + highshortfloat)
    p1_slice = p1[:max(1, len(p1) // 2)]
    p2_slice = p2[:max(1, len(p2) // 2)]

    in_bear = not _is_bull_regime()
    targets = []
    seen = set()

    # Inverse ETFs guaranteed first in bear — they profit from market decline
    # and are valid LONG buys with LONG_ONLY_MODE=True.

    def _push(symbols: List[str], limit: int = None) -> None:
        for s in symbols:
            if limit is not None and len(targets) >= limit:
                break
            if s in seen or s in excluded or s in delisted:
                continue
            if is_dead_ticker(s):
                continue
            seen.add(s)
            targets.append(s)

    # TI-latest-batch guarantee: push a capped slice of TI primary tickers so
    # they appear in every cycle regardless of universe size.
    # NOTE: do NOT override the max_fresh cap here — the original assignment above
    # already limits to TI_PRIMARY_SCAN_BATCH_LIMIT so BEAR_SHORT_UNIVERSE has room.
    if not latest_batch:
        # Fallback: guarantee top-N newest from each tier
        TI_FRONT = 10
        latest_batch = list(dict.fromkeys(live_p1[:TI_FRONT] + live_p2[:TI_FRONT]))

    if in_bear:
        # Always seed with inverse ETFs first — they are valid longs in bear regime
        _push(_INVERSE_ETFS)
        # Sympathy + EDGAR tickers queued this cycle — push before TI batch
        _push(_get_priority_scan_queue())
        # Push capped TI batch (respects max_fresh so bear-universe gets slots)
        _push(latest_batch)
        # Guarantee bear short universe symbols get into every bear cycle scan.
        # These large/mid-cap names are what BearBreakdownStrategy fires on.
        # Use SCAN_MAX_SYMBOLS as the ceiling so all slots up to the max are filled.
        _push(list(BEAR_SHORT_UNIVERSE), limit=SCAN_MAX_SYMBOLS)
        # Fill any remaining capacity from the rotating universe
        _push(rotated_base, limit=SCAN_MAX_SYMBOLS)
    else:
        # Bull/neutral: sympathy/EDGAR tickers first, then latest batch + rotating universe
        _push(_get_priority_scan_queue())
        _push(latest_batch)
        _push(rotated_base, limit=SCAN_MAX_SYMBOLS)
        if len(targets) < SCAN_MAX_SYMBOLS:
            _push(p1_slice + p2_slice, limit=SCAN_MAX_SYMBOLS)

    # ── SA v2: inject day-watch movers + leading stories as scan targets ──────
    # These are market-wide high-conviction movers SA tracks in real-time.
    # Added last so they fill any remaining capacity without displacing TI/priority tickers.
    if len(targets) < SCAN_MAX_SYMBOLS:
        try:
            from engine.data.seeking_alpha import get_sa_day_watch, get_sa_leading_story
            dw = get_sa_day_watch()
            if dw:
                sa_tickers = (
                    dw.get("top_gainers", [])[:8]
                    + dw.get("sp500_gainers", [])[:5]
                    + dw.get("most_active", [])[:5]
                    + get_sa_leading_story()[:3]
                )
                _push(sa_tickers, limit=SCAN_MAX_SYMBOLS)
        except Exception:
            pass

    # Track TI stocks for guardrail filtering
    global _ti_stocks
    _ti_stocks = set(ti_primary) if ti_primary else set()

    return targets


def scan_universe(scan_targets: List[str], sentiment: str, market_state: MarketState) -> Tuple[List, Dict[str, int], int]:
    clear_bar_cache()
    with _guardrail_lock:
        _guardrail_cache.clear()

    # Batch-prefetch stock snapshots for all scan targets in one API call.
    # Populates _snapshot_cache so _passes_guardrails() avoids per-symbol
    # get_bars("1d","1m") requests — the dominant I/O cost of each scan cycle.
    _prefetch_snapshots(scan_targets)

    # Compute regime ONCE here before spawning workers — avoids a thread race where
    # multiple workers concurrently hit the 15-min TTL expiry and each make a
    # separate get_bars("SPY") call to refresh the shared _regime_cache dict.
    bull_regime = market_state.resolve_regime()
    regime_str  = "bull" if bull_regime else "bear"
    strats = get_strategy_instances(bull_regime)


    signals = []
    hit_counts = {}
    scan_errors = 0
    scan_lock = threading.Lock()
    guardrail_rejections = {
        'dollar_vol': 0,
        'rvol': 0,
        'gap_chase': 0,
        'min_price': 0,
        'overnight_gap': 0,
        'other': 0
    }

    def _scan_one(symbol: str):
        # Dead-ticker check already done in get_scan_targets() — skip here.
        # Pass pre-computed regime into guardrails to avoid re-calling _is_bull_regime()
        # Custom: get rejection reason from _passes_guardrails
        is_ti = symbol in _ti_stocks
        passed, reason = _passes_guardrails(symbol, bull_regime=bull_regime, market_state=market_state, return_reason=True, is_ti_stock=is_ti)
        if not passed:
            with scan_lock:
                if reason in guardrail_rejections:
                    guardrail_rejections[reason] += 1
                else:
                    guardrail_rejections['other'] += 1
            return None

        candidates = []
        for s in strats:
            try:
                if isinstance(s, TechnicalStrategy):
                    sig = s.scan(symbol, sentiment)
                elif isinstance(s, SentimentStrategy):
                    sig = s.scan(symbol, sentiment)
                elif isinstance(s, MomentumStrategy):
                    sig = s.scan(symbol, regime_str)
                else:
                    sig = s.scan(symbol)
                if sig:
                    candidates.append(sig)
            except Exception as _ex:
                _log.debug(f"[SCAN] {symbol} {type(s).__name__}: {_ex}")

        if not candidates:
            return None
        signal = max(candidates, key=lambda s: s.confidence)
        return annotate_signal_with_news(signal)



    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        future_map = {pool.submit(_scan_one, sym): sym for sym in scan_targets}
        for future in as_completed(future_map):
            sym = future_map[future]
            try:
                sig = future.result(timeout=SCAN_SYMBOL_TIMEOUT)
                if sig:
                    with scan_lock:
                        signals.append(sig)
                        hit_counts[sig.strategy] = hit_counts.get(sig.strategy, 0) + 1
            except Exception as e:
                scan_errors += 1
                _log.error(f"[SCAN ERROR] {sym}: {e}")

    # Log guardrail rejection summary
    total_rejected = sum(guardrail_rejections.values())
    if total_rejected > 0:
        _log.info(f"[GUARDRAIL SUMMARY] Rejected: {total_rejected} | DollarVol: {guardrail_rejections['dollar_vol']} | RVOL: {guardrail_rejections['rvol']} | GapChase: {guardrail_rejections['gap_chase']} | MinPrice: {guardrail_rejections['min_price']} | Other: {guardrail_rejections['other']}")

    # Funnel: how many symbols produced a strategy signal before gating.
    signals_generated = len(signals)

    signals.sort(key=lambda x: x.confidence, reverse=True)
    # Adaptive confidence filter using pre-intelligence (market regime, VIX)
    vix = None
    if hasattr(market_state, 'vix') and market_state.vix is not None:
        vix = market_state.vix
    elif hasattr(market_state, 'resolve_vix'):
        vix, _, _ = market_state.resolve_vix()
    bull = market_state.resolve_regime()
    base_conf = MIN_SIGNAL_CONFIDENCE
    if bull:
        if vix and vix > 25:
            adaptive_conf = min(0.80, base_conf + 0.05)  # stricter in high-vol bull
        else:
            adaptive_conf = base_conf
    else:
        if vix and vix < 18:
            adaptive_conf = max(0.65, base_conf - 0.05)  # looser in calm bear
        else:
            adaptive_conf = max(0.68, base_conf - 0.02)
    signals = [s for s in signals if s.confidence >= adaptive_conf]
    # Funnel: confidence gate drop count.
    conf_dropped = signals_generated - len(signals)

    # Dynamic sector/industry weighting cap
    # Limit to max 3 signals per sector (can be tuned)
    from collections import defaultdict
    sector_cap = 3
    sector_counts = defaultdict(int)
    filtered_signals = []
    sector_dropped = 0
    for sig in signals:
        sector = getattr(sig, 'sector', None)
        if sector is None:
            filtered_signals.append(sig)  # If no sector info, allow
            continue
        if sector_counts[sector] < sector_cap:
            filtered_signals.append(sig)
            sector_counts[sector] += 1
        else:
            sector_dropped += 1
    signals = filtered_signals
    # Funnel: per-stage drop counts so signal starvation is diagnosable.
    _log.info(
        f"[FUNNEL] targets={len(scan_targets)} guardrail_rejected={total_rejected} "
        f"(rvol={guardrail_rejections['rvol']}) -> signals_generated={signals_generated} "
        f"-> conf_gate(-{conf_dropped}) -> sector_cap(-{sector_dropped}) -> final={len(signals)}"
    )
    if LONG_ONLY_MODE:
        # Long-only enforcement: drop sell/short signals only when LONG_ONLY_MODE is active
        pre_len = len(signals)
        signals = [s for s in signals if s.action == "buy"]
        if len(signals) != pre_len:
            _log.info(f"Long-only enforced in scan_universe: dropping {pre_len-len(signals)} short signals")

    # Adaptive filter logic: relax after N empty scans, reset after success
    if len(signals) == 0:
        _adaptive_state["empty_scans"] += 1
        if _adaptive_state["empty_scans"] >= _ADAPTIVE_MAX_EMPTY:
            # Relax RVOL and confidence stepwise
            if _adaptive_state["rvol_min"] > _ADAPTIVE_MIN_RVOL:
                _adaptive_state["rvol_min"] = max(_ADAPTIVE_MIN_RVOL, _adaptive_state["rvol_min"] - _ADAPTIVE_STEP_RVOL)
                _log.info(f"[ADAPTIVE] Lowered RVOL_MIN to {_adaptive_state['rvol_min']:.2f}")
            if _adaptive_state["min_conf"] > _ADAPTIVE_MIN_CONF:
                _adaptive_state["min_conf"] = max(_ADAPTIVE_MIN_CONF, _adaptive_state["min_conf"] - _ADAPTIVE_STEP_CONF)
                _log.info(f"[ADAPTIVE] Lowered MIN_SIGNAL_CONFIDENCE to {_adaptive_state['min_conf']:.2f}")
    else:
        if _adaptive_state["empty_scans"] > 0:
            _log.info(f"[ADAPTIVE] Resetting adaptive filters after successful scan.")
        _adaptive_state["empty_scans"] = 0
        _adaptive_state["rvol_min"] = RVOL_MIN
        _adaptive_state["min_conf"] = MIN_SIGNAL_CONFIDENCE
    return signals, hit_counts, scan_errors


def filter_signals(signals, long_only: bool = False, min_conf: float = 0.0, cap: int = None):
    if long_only:
        signals = [s for s in signals if s.action == "buy"]

    signals = [s for s in signals if s.confidence >= min_conf]

    if cap is not None:
        signals = signals[:cap]
    return signals
