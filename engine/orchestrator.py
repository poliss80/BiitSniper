"""
ApexTrader orchestrator — Stage 3 refactor.

scan_and_trade() decomposed into focused private functions:
  _run_options_cycle()     — options monitor + new entries
  _run_discovery()         — all universe refresh sources
  _resolve_market_regime() — regime detection with safe fallback
  _build_scan_targets()    — universe assembly + position filtering
  _filter_eligible()       — confidence gate + long-only enforcement
  _log_skipped()           — skip diagnostics for top-10 non-qualifiers
  _execute_bear_plan()     — bear regime: 1 swap-long + N shorts with cooldown
  _execute_bull_plan()     — bull regime: top-N by confidence
  _build_short_queue()     — pre-screen shorts (tradability + cooldown)

AppContext dataclass holds all runtime singletons so they are never
instantiated at import time — importing this module no longer opens a
broker connection.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import schedule
import pytz
REPO_ROOT = Path(__file__).resolve().parent.parent

from . import config as cfg
from .utils import (
    setup_logging,
    MarketState,
    get_market_sentiment,
    get_market_hours_interval,
    get_position_tuning_interval,
    get_vix_interval,
    get_live_holdings,
)
from .equity.strategies import Signal
from .equity.scan import get_scan_targets, scan_universe
from .equity.universe import filter_universe_by_positions
from .equity import discovery as _discovery
from .notifications import notify_scan_results
from .predictions import save_day_picks
from . import session as _session
from engine.broker.broker_factory import BrokerFactory
from engine.execution.enhanced import EnhancedExecutor
from engine.options.executor import OptionsExecutor
from engine.options.strategies import scan_options_universe
from engine.risk import kill_mode as _kill_mode
from engine.crypto.trader import CryptoTrader

log = setup_logging()

_WINDOW_FLAT_STATE: dict = {}
_WINDOW_STATE_PATH = REPO_ROOT / ".intraday_session_state.json"

import logging as _logging
_logging.getLogger("WDM").setLevel(_logging.ERROR)
_logging.getLogger("webdriver_manager").setLevel(_logging.ERROR)


# ── AppContext ────────────────────────────────────────────────────────────────
# Holds all runtime singletons. Instantiated once inside start()/run() so
# importing this module never opens a broker connection.

@dataclass
class AppContext:
    client:           object
    executor:         EnhancedExecutor
    options_executor: Optional[OptionsExecutor]
    crypto_trader:    Optional[CryptoTrader] = None
    # Per-session state
    last_market_regime:   str  = "bull"
    market_state:         Optional[MarketState] = None
    # Short-fail cooldown: {symbol: monotonic_ts_until_retry}
    # Merged here from the old module-level global so it survives restarts
    # via executor._htb_cache and is accessible to the bear plan.
    short_fail_cooldown: dict = field(default_factory=dict)


def _build_context() -> AppContext:
    """Create and wire all runtime singletons. Called once at startup."""
    client   = BrokerFactory.create_stock_client(cfg.STOCKS_BROKER)
    executor = EnhancedExecutor(client, use_bracket_orders=True)
    opts     = OptionsExecutor(client) if cfg.OPTIONS_ENABLED else None
    if opts:
        log.info(
            f"Options trading ENABLED ({int(cfg.OPTIONS_ALLOCATION_PCT)}% allocation, "
            f"{cfg.OPTIONS_DTE_MIN}-{cfg.OPTIONS_DTE_MAX} DTE)"
        )
    crypto = None
    if cfg.CRYPTO_ENABLED:
        crypto = CryptoTrader(client, api_key=cfg.API_KEY, api_secret=cfg.API_SECRET)
        # Prune CRYPTO_UNIVERSE to pairs Alpaca currently marks as tradeable
        cfg.CRYPTO_UNIVERSE = crypto.fetch_tradeable_universe(cfg.CRYPTO_UNIVERSE)
        log.info(
            f"Crypto trading ENABLED — universe: {cfg.CRYPTO_UNIVERSE} "
            f"| pos_pct={cfg.CRYPTO_POSITION_PCT}% | TP={cfg.CRYPTO_TP_PCT}% | SL={cfg.CRYPTO_SL_PCT}%"
        )
    log.info(f"Trade mode: {cfg.TRADE_MODE} (PAPER={cfg.PAPER}, LIVE={cfg.LIVE})")
    if not cfg.LONG_ONLY_MODE:
        log.info("Shorting enabled (LONG_ONLY_MODE=False)")
    return AppContext(client=client, executor=executor, options_executor=opts, crypto_trader=crypto)


# ── Discovery wrappers ────────────────────────────────────────────────────────
# Thin wrappers that forward config into discovery — keeps scan_and_trade lean.

def _run_discovery(ctx: AppContext, market_state: MarketState) -> None:
    """Fire all configured universe refresh sources (each throttled internally)."""
    _discovery.scan_trending_stocks(
        use_live_trending=cfg.USE_LIVE_TRENDING,
        use_finnhub=cfg.USE_FINNHUB_DISCOVERY,
        use_sentiment_gate=cfg.USE_SENTIMENT_GATE,
        trending_max=cfg.TRENDING_MAX_RESULTS,
        trending_interval_min=cfg.TRENDING_SCAN_INTERVAL,
        trending_min_momentum=cfg.TRENDING_MIN_MOMENTUM,
        priority_1=cfg.PRIORITY_1_MOMENTUM,
    )
    _discovery.scan_tradeideas_universe(
        enabled=cfg.USE_TRADEIDEAS_DISCOVERY,
        scan_interval_min=cfg.TRADEIDEAS_SCAN_INTERVAL_MIN,
        headless=cfg.TRADEIDEAS_HEADLESS,
        chrome_profile=cfg.TRADEIDEAS_CHROME_PROFILE,
        update_config=cfg.TRADEIDEAS_UPDATE_CONFIG_FILE,
        priority_1=cfg.PRIORITY_1_MOMENTUM,
        priority_2=cfg.PRIORITY_2_ESTABLISHED,
        browser=cfg.TRADEIDEAS_BROWSER,
        remote_debug_port=9222,
    )
    _discovery.scan_tradeideas_unusual_options(
        enabled=cfg.USE_TRADEIDEAS_UNUSUAL_OPTIONS_DISCOVERY and not cfg.USE_TRADEIDEAS_DISCOVERY,
        scan_interval_min=cfg.TRADEIDEAS_UNUSUAL_OPTIONS_SCAN_INTERVAL_MIN,
        headless=cfg.TRADEIDEAS_HEADLESS,
        chrome_profile=cfg.TRADEIDEAS_CHROME_PROFILE,
        update_config=cfg.TRADEIDEAS_UPDATE_CONFIG_FILE,
        priority_1=cfg.PRIORITY_1_MOMENTUM,
        priority_2=cfg.PRIORITY_2_ESTABLISHED,
        browser=cfg.TRADEIDEAS_BROWSER,
        remote_debug_port=9222,
    )
    _discovery.scan_tradeideas_toplists(
        enabled=cfg.USE_TRADEIDEAS_TOPLISTS_DISCOVERY and not cfg.USE_TRADEIDEAS_DISCOVERY,
        scan_interval_min=cfg.TRADEIDEAS_TOPLISTS_SCAN_INTERVAL_MIN,
        headless=cfg.TRADEIDEAS_HEADLESS,
        chrome_profile=cfg.TRADEIDEAS_CHROME_PROFILE,
        update_config=cfg.TRADEIDEAS_UPDATE_CONFIG_FILE,
        priority_1=cfg.PRIORITY_1_MOMENTUM,
        priority_2=cfg.PRIORITY_2_ESTABLISHED,
        browser=cfg.TRADEIDEAS_BROWSER,
        remote_debug_port=9222,
    )
    _discovery.scan_sympathy_and_edgar(
        sympathy_enabled=cfg.USE_SECTOR_SYMPATHY,
        edgar_enabled=cfg.USE_EDGAR_SCANNER,
        sympathy_interval_min=cfg.SECTOR_SYMPATHY_INTERVAL_MIN,
        edgar_interval_min=cfg.EDGAR_SCANNER_INTERVAL_MIN,
        priority_1=cfg.PRIORITY_1_MOMENTUM,
        priority_2=cfg.PRIORITY_2_ESTABLISHED,
    )
    _discovery.scan_alpaca_movers(
        interval_min=cfg.ALPACA_MOVER_SCAN_INTERVAL_MIN,
        market_state=market_state,
    )
    _discovery.scan_preopen_intelligence(
        enabled=cfg.USE_PREOPEN_INTELLIGENCE,
        interval_min=cfg.PREOPEN_INTELLIGENCE_SCAN_INTERVAL_MIN,
        market_state=market_state,
        priority_1=cfg.PRIORITY_1_MOMENTUM,
        priority_2=cfg.PRIORITY_2_ESTABLISHED,
        max_watchlist=cfg.PREOPEN_INTELLIGENCE_MAX_TICKERS,
        use_regime_gating=cfg.PREOPEN_USE_REGIME_GATING,
        use_sentiment_gating=cfg.PREOPEN_USE_SENTIMENT_GATING,
    )


# ── Options cycle ─────────────────────────────────────────────────────────────

def _run_options_cycle(ctx: AppContext, market_state: MarketState) -> None:
    """Monitor existing options positions and attempt one new entry per cycle."""
    if ctx.options_executor is None:
        return

    # Allow FORCE_EQUITY to bypass the regular-hours gate (weekend/after-hours testing).
    if not market_state.is_regular_hours and not cfg.FORCE_EQUITY:
        return

    try:
        ctx.options_executor.monitor_positions()
        if not cfg.OPTIONS_STANDALONE_ENTRIES_ENABLED:
            log.info("[OPTIONS] Standalone entries disabled — monitoring only")
            return
        if market_state.is_options_lull_hours:
            log.info("[OPTIONS] Lull period — monitoring only, no new entries")
            return
        all_positions = ctx.client.get_all_positions()
        held_map      = {p.symbol: int(float(p.qty)) for p in all_positions if float(p.qty) > 0}
        existing_syms = {pos.occ_symbol for pos in ctx.options_executor._positions.values()}
        opt_signals   = scan_options_universe(held_map, existing_syms, ctx.market_state)
        if opt_signals:
            top = opt_signals[:10]
            log.info(
                f"[OPTIONS] {len(opt_signals)} candidates | open={len(ctx.options_executor._positions)} "
                f"| top: {top[0].symbol} {top[0].option_type} conf={top[0].confidence:.0%}"
            )
            executed = False
            rejected = []
            for sig in top:
                if ctx.options_executor.place_option_order(sig, market_state):
                    executed = True
                    break
                rejected.append(
                    f"{sig.symbol}/{sig.strategy}: "
                    f"{getattr(ctx.options_executor, '_last_rejection_reason', 'unspecified rejection')}"
                )
            if not executed:
                log.info(
                    f"[OPTIONS] No option order executed this cycle | "
                    f"open={len(ctx.options_executor._positions)} | "
                    f"rejections={' ; '.join(rejected)}"
                )
        else:
            log.info(
                f"[OPTIONS] No qualifying signals this cycle | open={len(ctx.options_executor._positions)}"
            )
        log.info(f"[OPTIONS] {ctx.options_executor.status_summary()}")
    except Exception as e:
        log.error(f"[OPTIONS] Cycle error: {e}", exc_info=True)


# ── Market regime ─────────────────────────────────────────────────────────────

def _resolve_market_regime(ctx: AppContext, market_state: MarketState) -> Tuple[str, int]:
    """Return (regime, signals_cap). Falls back to last known regime on failure."""
    if not cfg.USE_MARKET_REGIME_FILTER:
        return ctx.last_market_regime, cfg.MAX_SIGNALS_PER_CYCLE
    try:
        is_bull        = market_state.resolve_regime()
        regime         = "bull" if is_bull else "bear"
        ctx.last_market_regime = regime
        signals_cap    = cfg.MAX_SIGNALS_PER_CYCLE if is_bull else cfg.MARKET_REGIME_SIGNALS_CAP
        if is_bull:
            log.info(f"[SCAN] BULL REGIME — cap {signals_cap}/cycle")
        else:
            short_cap = 0 if (cfg.LONG_ONLY_MODE or ctx.executor.shorting_blocked) else cfg.BEAR_SHORT_SIGNALS_CAP
            log.info(f"[SCAN] BEAR REGIME — long cap {cfg.MARKET_REGIME_SIGNALS_CAP}, short cap {short_cap}/cycle")
        return regime, signals_cap
    except Exception as e:
        log.error(f"[SYSTEM] Regime check failed — retaining '{ctx.last_market_regime}': {e}", exc_info=True)
        return ctx.last_market_regime, cfg.MAX_SIGNALS_PER_CYCLE


# ── Universe assembly ─────────────────────────────────────────────────────────

def _build_scan_targets(ctx: AppContext) -> Tuple[List[str], set]:
    """Return (scan_targets, excluded) after universe assembly and position filtering."""
    _, _, excluded = get_live_holdings(ctx.client)
    targets = filter_universe_by_positions(get_scan_targets(), excluded)
    log.info(
        f"[SCAN] {len(targets)} symbols (filtered, {cfg.SCAN_WORKERS} workers): "
        f"{', '.join(targets)}"
    )
    return targets, excluded


# ── Signal filtering ──────────────────────────────────────────────────────────

def _filter_eligible(
    ctx: AppContext,
    signals: list,
    fresh_held: set,
    regime: str,
) -> list:
    """Apply confidence gate, midday chop filter, position cross-ref, and long-only enforcement.

    Returns the eligible signal list ready for execution.
    """
    import pytz as _ptz, datetime as _dt
    _now_et   = _dt.datetime.now(_ptz.timezone("America/New_York"))
    _now_hhmm = _now_et.strftime("%H:%M")
    _midday   = cfg.MIDDAY_CHOP_START <= _now_hhmm < cfg.MIDDAY_CHOP_END
    _conf_floor = cfg.MIDDAY_MIN_CONFIDENCE if _midday else cfg.MIN_SIGNAL_CONFIDENCE
    if _midday:
        log.info(
            f"[MIDDAY] Chop filter active ({cfg.MIDDAY_CHOP_START}–{cfg.MIDDAY_CHOP_END} ET) — "
            f"min confidence raised to {_conf_floor:.0%}"
        )

    short_min_conf = cfg.MIN_SHORT_CONFIDENCE_BEAR if regime == "bear" else _conf_floor
    long_only      = cfg.LONG_ONLY_MODE or ctx.executor.shorting_blocked

    if ctx.executor.shorting_blocked and not cfg.LONG_ONLY_MODE:
        log.warning("Shorting blocked by broker (40310000) — effective long-only this session")

    eligible = []
    for s in signals:
        if s.symbol in fresh_held:
            continue
        conf = round(float(s.confidence), 2)
        if s.action == "buy" and conf >= _conf_floor:
            eligible.append(s)
        elif (
            s.action in ("sell", "short")
            and not long_only
            and conf >= short_min_conf
        ):
            eligible.append(s)

    # Strip shorts when effectively long-only
    if long_only:
        eligible = [s for s in eligible if s.action == "buy"]

    # Long-only fallback: if nothing qualifies, pick the best buy above min conf
    if long_only and not eligible:
        fallback = next(
            (s for s in signals
             if s.action == "buy"
             and s.symbol not in fresh_held
             and round(float(s.confidence), 2) >= cfg.MIN_SIGNAL_CONFIDENCE),
            None,
        )
        if fallback:
            log.warning(
                f"Long-only fallback: {fallback.symbol} buy @ ${fallback.price:.2f} "
                f"conf={fallback.confidence:.0%}"
            )
            eligible = [fallback]

    log.info(
        f"Confidence gate (long>={cfg.MIN_SIGNAL_CONFIDENCE:.0%}, "
        f"short>={short_min_conf:.0%}) + cross-ref: {len(eligible)} eligible"
    )
    return eligible


def _log_skipped(signals: list, eligible: list, fresh_held: set, regime: str, executor: EnhancedExecutor) -> None:
    """Log skip reason for each top-10 raw signal that did not make it to eligible."""
    short_min_conf = cfg.MIN_SHORT_CONFIDENCE_BEAR if regime == "bear" else cfg.MIN_SIGNAL_CONFIDENCE
    eligible_syms  = {s.symbol for s in eligible}
    top10          = sorted(signals, key=lambda s: s.confidence, reverse=True)[:10]
    for s in top10:
        if s.symbol in eligible_syms:
            continue
        conf = round(float(s.confidence), 2)
        if s.symbol in fresh_held:
            reason = "already held/ordered"
        elif s.action == "buy" and conf < cfg.MIN_SIGNAL_CONFIDENCE:
            reason = f"conf {conf:.0%} < long min {cfg.MIN_SIGNAL_CONFIDENCE:.0%}"
        elif s.action in ("sell", "short") and conf < short_min_conf:
            reason = f"conf {conf:.0%} < short min {short_min_conf:.0%}"
        elif executor.shorting_blocked and s.action in ("sell", "short"):
            reason = "shorting blocked by broker"
        elif cfg.LONG_ONLY_MODE and s.action != "buy":
            reason = "long-only mode"
        else:
            reason = "filtered"
        log.info(f"[SCAN] SKIP {s.symbol} {s.action.upper()} ${s.price:.2f} conf={s.confidence:.0%} [{s.strategy}] — {reason}")


# ── Short pre-screening ───────────────────────────────────────────────────────

def _build_short_queue(ctx: AppContext, short_candidates: list) -> list:
    """Pre-screen short candidates: remove cooldown hits and non-shortable assets.

    Returns the filtered short_queue ready for bear execution.
    """
    now_ts = time.monotonic()
    # Prune expired cooldowns
    expired = [sym for sym, ts in ctx.short_fail_cooldown.items() if ts <= now_ts]
    for sym in expired:
        ctx.short_fail_cooldown.pop(sym, None)

    queue = []
    for s in short_candidates:
        cool_until = ctx.short_fail_cooldown.get(s.symbol, 0.0)
        if cool_until > now_ts:
            log.info(f"Pre-skip {s.symbol} SHORT: cooldown {(cool_until - now_ts) / 60:.1f}m remaining")
            continue
        try:
            asset     = ctx.client.get_asset(s.symbol)
            status    = str(getattr(getattr(asset, "status", "active"), "value", getattr(asset, "status", "active"))).lower()
            tradable  = bool(getattr(asset, "tradable",  True))
            shortable = bool(getattr(asset, "shortable", True))
            if status != "active" or not tradable or not shortable:
                log.info(f"Pre-skip {s.symbol} SHORT: status={status} tradable={tradable} shortable={shortable}")
                ctx.short_fail_cooldown[s.symbol] = now_ts + cfg.SHORT_FAIL_COOLDOWN_MIN * 60
                continue
        except Exception as e:
            log.warning(f"Pre-check asset failed {s.symbol}: {e} — keeping candidate")
        queue.append(s)
    return queue


# ── Execution plans ───────────────────────────────────────────────────────────

def _entries_allowed(market_state) -> tuple[bool, str]:
    """Return (allowed, reason) for placing NEW entries this cycle.

    Exits and position management run regardless of this gate — it only blocks
    opening fresh positions outside the high-edge session.
    """
    if not cfg.ENTRY_WINDOW_ENABLED:
        return True, ""
    if cfg.BLOCK_EXTENDED_HOURS_ENTRIES and not market_state.is_regular_hours:
        return False, f"extended hours ({market_state.phase})"
    if market_state.hour >= cfg.ENTRY_CUTOFF_HOUR:
        return False, f"after entry cutoff {cfg.ENTRY_CUTOFF_HOUR:.2f}h ET"
    return True, ""


def _execute_bear_plan(
    ctx: AppContext,
    eligible: list,
    daily_loss_limit: float,
    loss_pct: float,
) -> None:
    """Bear regime: attempt 1 swap-long then up to BEAR_SHORT_SIGNALS_CAP shorts."""
    long_sigs         = [s for s in eligible if s.action == "buy"][:cfg.MARKET_REGIME_SIGNALS_CAP]
    short_candidates  = [] if (cfg.LONG_ONLY_MODE or ctx.executor.shorting_blocked) else \
                        [s for s in eligible if s.action in ("sell", "short")]
    if cfg.LONG_ONLY_MODE and any(s.action in ("sell", "short") for s in eligible):
        log.warning(f"LONG_ONLY_MODE — dropping {len([s for s in eligible if s.action in ('sell','short')])} short(s)")
    if ctx.executor.shorting_blocked and short_candidates:
        log.warning(f"Shorting blocked — dropping {len(short_candidates)} short(s)")

    short_queue  = _build_short_queue(ctx, short_candidates)
    short_target = 0 if (cfg.LONG_ONLY_MODE or ctx.executor.shorting_blocked) else cfg.BEAR_SHORT_SIGNALS_CAP
    log.info(f"[TRADE] BEAR plan: {len(long_sigs)} long(s) swap-only, target {short_target} short(s) from {len(short_queue)} queued")

    # One swap-long per bear cycle
    for sig in long_sigs:
        _session.refresh_daily_pnl(ctx.client)
        if _session.daily_pnl <= daily_loss_limit:
            log.warning(f"Daily loss limit mid-cycle ({loss_pct:.0f}%): ${_session.daily_pnl:.2f} — halting")
            return
        log.info(f"[TRADE] EXECUTE: {sig.action.upper()} {sig.symbol} @ ${sig.price:.2f} | {sig.strategy} | {sig.reason}")
        if ctx.executor.execute(sig, swap_only=True):
            _session.trades += 1
            break
        time.sleep(1)

    # Short queue
    short_success = 0
    for sig in short_queue:
        if short_target <= 0 or short_success >= short_target:
            break
        _session.refresh_daily_pnl(ctx.client)
        if _session.daily_pnl <= daily_loss_limit:
            log.warning(f"Daily loss limit mid-cycle ({loss_pct:.0f}%): ${_session.daily_pnl:.2f} — halting")
            break
        log.info(f"[TRADE] EXECUTE: {sig.action.upper()} {sig.symbol} @ ${sig.price:.2f} | {sig.strategy} | {sig.reason}")
        if ctx.executor.execute(sig, swap_only=False):
            _session.trades += 1
            short_success += 1
            ctx.short_fail_cooldown.pop(sig.symbol, None)
        else:
            ctx.short_fail_cooldown[sig.symbol] = time.monotonic() + cfg.SHORT_FAIL_COOLDOWN_MIN * 60
            log.info(f"SHORT failed {sig.symbol} — cooldown {cfg.SHORT_FAIL_COOLDOWN_MIN}m")
        time.sleep(1)


def _execute_bull_plan(
    ctx: AppContext,
    eligible: list,
    signals_cap: int,
    regime: str,
    daily_loss_limit: float,
    loss_pct: float,
) -> None:
    """Bull (or neutral) regime: execute top-N eligible signals by confidence."""
    top_signals = sorted(eligible, key=lambda s: s.confidence, reverse=True)[:signals_cap]
    log.info(f"Executing top {len(top_signals)} signal(s) (cap={signals_cap})")
    for sig in top_signals:
        swap_only = (regime == "bear") and sig.action not in ("sell", "short")
        _session.refresh_daily_pnl(ctx.client)
        if _session.daily_pnl <= daily_loss_limit:
            log.warning(f"Daily loss limit mid-cycle ({loss_pct:.0f}%): ${_session.daily_pnl:.2f} — halting")
            break
        log.info(f"EXECUTE: {sig.action.upper()} {sig.symbol} @ ${sig.price:.2f} | {sig.strategy} | {sig.reason}")
        if ctx.executor.execute(sig, swap_only=swap_only):
            _session.trades += 1
        time.sleep(1)


# ── Crypto weekend cycle ─────────────────────────────────────────────────────

def _run_crypto_cycle(ctx: AppContext) -> None:
    """Scan and trade crypto pairs. Runs only on weekends."""
    if ctx.crypto_trader is None:
        log.info("[CRYPTO] Trader not initialised (CRYPTO_ENABLED=false) — skipping")
        return
    try:
        ctx.crypto_trader.monitor_positions()
        log.info(ctx.crypto_trader.status_summary())

        signals = ctx.crypto_trader.scan(cfg.CRYPTO_UNIVERSE)
        if not signals:
            log.info("[CRYPTO] No signals this cycle")
            return

        for idx, s in enumerate(signals[:5], 1):
            log.info(
                f"[CRYPTO] #{idx}: {s.symbol} {s.action.upper()} "
                f"@ {s.price:.4f} conf={s.confidence:.0%} | {s.reason}"
            )

        buys_this_cycle = 0
        for sig in signals:
            if sig.action == "buy":
                if sig.confidence < cfg.CRYPTO_MIN_CONFIDENCE:
                    log.info(
                        f"[CRYPTO] SKIP {sig.symbol} conf={sig.confidence:.0%} "
                        f"< min {cfg.CRYPTO_MIN_CONFIDENCE:.0%}"
                    )
                    continue
                # No hard position-count cap — CRYPTO_MIN_NOTIONAL already
                # stops new buys once available cash is too small to fund one.
                if ctx.crypto_trader.execute_buy(sig):
                    buys_this_cycle += 1
        if buys_this_cycle == 0 and signals:
            log.info("[CRYPTO] No new entries this cycle (all signals rejected)")
    except Exception as e:
        log.error(f"[CRYPTO] Cycle error: {e}", exc_info=True)


# ── Core scan cycle ───────────────────────────────────────────────────────────

def _check_kill_mode(ctx: AppContext) -> bool:
    return _kill_mode.check(
        ctx.client, ctx.executor, ctx.options_executor,
        vix_level=cfg.KILL_MODE_VIX_LEVEL,
        spy_drop_pct=cfg.KILL_MODE_SPY_DROP_PCT,
        vix_roc_pct=cfg.KILL_MODE_VIX_ROC_PCT,
    )


def _load_intraday_state(today: datetime.date) -> str:
    try:
        state = json.loads(_WINDOW_STATE_PATH.read_text(encoding="utf-8"))
        if state.get("date") == today.isoformat():
            return state.get("phase", "pre_session")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return "pre_session"


def _save_intraday_state(today: datetime.date, phase: str) -> None:
    try:
        _WINDOW_STATE_PATH.write_text(
            json.dumps({"date": today.isoformat(), "phase": phase}),
            encoding="utf-8",
        )
    except OSError as e:
        log.warning(f"Could not save intraday session state: {e}")


def _run_live_probe_scale_check(ctx: AppContext, reason: str) -> None:
    """Keep live-probe scale-ins active even while the bot is paused during cutoffs."""
    try:
        if ctx.executor is not None:
            ctx.executor.check_live_probe_scale_ins(ctx.options_executor)
    except Exception as exc:
        log.warning(f"[{reason}] live probe scale check skipped: {exc}")


def _manage_intraday_window(ctx: AppContext) -> bool:
    """Allow three ET trading windows and flatten at each session boundary."""
    now = datetime.datetime.now(pytz.timezone("America/New_York"))
    if now.weekday() >= 5:
        return False

    def minutes(value: str) -> int:
        hour, minute = map(int, value.split(":"))
        return hour * 60 + minute

    current = now.hour * 60 + now.minute
    start = minutes(cfg.INTRADAY_WINDOW_START)
    morning_cutoff = minutes(cfg.INTRADAY_MORNING_CUTOFF)
    morning_reset = minutes(cfg.INTRADAY_MORNING_RESET)
    reset = minutes(cfg.INTRADAY_RESET_TIME)
    cutoff = minutes(cfg.INTRADAY_FINAL_CUTOFF)
    final_reset = minutes(cfg.INTRADAY_FINAL_RESET)
    end = minutes(cfg.AFTERHOURS_END)
    today = now.date()
    phase_state = _load_intraday_state(today)

    if current < start or current >= end:
        if current >= end and phase_state != "final_reset":
            if not ctx.executor.flatten_portfolio(
                f"INTRADAY FINAL RESET {cfg.AFTERHOURS_END} ET",
                allow_momentum_exemptions=True,
            ):
                return False
            _save_intraday_state(today, "final_reset")
        return False
    
    # ── Morning cutoff: 10:55 - 11:00 ──
    if morning_cutoff <= current < morning_reset:
        return False
    
    # ── Afternoon cutoff: 14:58 - 15:03 ──
    if cutoff <= current < final_reset:
        return False

    if current < reset:
        phase = "first"
    elif current < final_reset:
        phase = "second"
    else:
        phase = "third"
    key = (today, phase)
    if phase == "second" and phase_state == "pre_session":
        # A restart after 11:00 must not retroactively flatten Session 2.
        _save_intraday_state(today, "session_2")
        phase_state = "session_2"

    if phase == "first" and phase_state != "session_1":
        reason = (
            f"INTRADAY {cfg.INTRADAY_WINDOW_START} ET START"
        )
        if not ctx.executor.flatten_portfolio(reason, allow_momentum_exemptions=True):
            return False
        _save_intraday_state(today, "session_1")
    elif phase == "second" and phase_state == "session_1":
        if not ctx.executor.flatten_portfolio(
            f"INTRADAY {cfg.INTRADAY_MORNING_RESET} ET RESET",
            allow_momentum_exemptions=True,
        ):
            return False
        _save_intraday_state(today, "session_2")
    elif phase == "third" and phase_state not in ("session_3", "final_reset", "pre_session"):
        if not ctx.executor.flatten_portfolio(
            f"INTRADAY {cfg.INTRADAY_FINAL_RESET} ET RESET",
            allow_momentum_exemptions=True,
        ):
            return False
        _save_intraday_state(today, "session_3")
    elif phase == "third" and phase_state in ("final_reset", "pre_session"):
        _save_intraday_state(today, "session_3")
    _WINDOW_FLAT_STATE[key] = True
    # Options tracking is intentionally retained across equity session resets:
    # EnhancedExecutor.flatten_portfolio excludes us_option positions, so tracked
    # options are still open and must keep being managed (theta guard, stops,
    # targets) by OptionsExecutor.
    if ctx.crypto_trader is not None:
        ctx.crypto_trader._sync_positions()
    return True


def scan_and_trade(ctx: AppContext) -> None:
    """One complete scan-and-trade cycle.

    Sequence:
      1. Session reset / daily guards
      2. Options cycle
      3. Market-hours + kill-mode gates
      4. Session P&L guards
      5. Discovery refresh
      6. Universe assembly + scan
      7. Signal filtering
      8. Execution (bear or bull plan)
    """
    _session.reset_daily(ctx.client)

    # Initialize the market snapshot before any early-return path. Probe
    # management can run outside an active trading window and still needs the
    # executor's market state for regime and timestamp decisions.
    if getattr(ctx, "market_state", None) is None:
        ctx.market_state = MarketState.from_now()
    ctx.executor.update_market_state(ctx.market_state)

    # ── Weekend / FORCE_CRYPTO: crypto must be evaluated BEFORE the equity
    # intraday-window gate below, which returns early outside 04:00-20:00 ET
    # and on weekends — otherwise crypto could never run post-market or on
    # weekends regardless of these flags. Uses a direct date check (not the
    # cached ctx.market_state, which is only refreshed once the equity window
    # is open) so weekend/weekday detection stays correct across midnight.
    import pytz as _ptz
    _now_et = __import__('datetime').datetime.now(_ptz.timezone("America/New_York"))
    _is_weekend = _now_et.weekday() >= 5
    if _is_weekend or cfg.FORCE_CRYPTO:
        if cfg.CRYPTO_ENABLED:
            if cfg.FORCE_CRYPTO and not _is_weekend:
                log.info("[SYSTEM] FORCE_CRYPTO=true — running crypto cycle")
            else:
                log.info("[SYSTEM] Weekend — running CRYPTO-ONLY cycle (equity/options suspended)")
            _run_crypto_cycle(ctx)
            if _is_weekend and not cfg.FORCE_EQUITY:
                return  # weekends: stop here, no equity
            # weekday FORCE_CRYPTO or FORCE_EQUITY: fall through to equity/options below
            if cfg.FORCE_EQUITY and _is_weekend:
                log.info("[SYSTEM] FORCE_EQUITY=true — running equity/options cycle on weekend")
        else:
            if not cfg.FORCE_EQUITY:
                log.info("[SYSTEM] Weekend — equity market closed, CRYPTO_ENABLED=false, nothing to do")
                return

    if not _manage_intraday_window(ctx):
        _run_live_probe_scale_check(ctx, "SYSTEM")
        log.info("[SYSTEM] Outside an active intraday window or waiting for portfolio flatten")
        return

    # ── Hard cutoff: no trading after AFTERHOURS_END (20:00 ET) ──────────────
    _end_h, _end_m = map(int, cfg.AFTERHOURS_END.split(":"))
    if _now_et.hour > _end_h or (_now_et.hour == _end_h and _now_et.minute >= _end_m):
        log.debug(f"[SYSTEM] After {cfg.AFTERHOURS_END} ET — no scanning")
        return

    ctx.market_state = MarketState.from_now()
    ctx.market_state.resolve_regime()

    ctx.executor.update_market_state(ctx.market_state)
    _run_options_cycle(ctx, ctx.market_state)

    # Inform equity executor how much capital is already committed to open options
    # positions so it deducts that from available buying power before sizing equity trades.
    if ctx.options_executor is not None:
        ctx.executor.set_options_cost_reserve(ctx.options_executor.current_options_cost())

    market_state = ctx.market_state
    if not market_state.is_market_open:
        if not cfg.FORCE_SCAN and not cfg.EXTENDED_HOURS_EQUITY_TRADING:
            log.info("[SYSTEM] Market closed — skipping scan")
            return
        if cfg.FORCE_SCAN:
            log.warning("[SYSTEM] FORCE_SCAN active — bypassing market-hours gate")
        else:
            log.info("[SYSTEM] Extended-hours equity trading active — scanning equities")

    if _check_kill_mode(ctx):
        log.info("[SYSTEM] Kill mode active — aborting cycle")
        return

    _session.refresh_daily_pnl(ctx.client)
    loss_pct          = cfg.DAILY_LOSS_LIMIT_BEAR_PCT if ctx.last_market_regime == "bear" else cfg.DAILY_LOSS_LIMIT_BULL_PCT
    daily_loss_limit  = -(_session.daily_start_equity * loss_pct / 100) if _session.daily_start_equity > 0 else -999_999

    if _session.daily_pnl <= daily_loss_limit:
        log.warning(f"[SYSTEM] Daily loss limit ({loss_pct:.0f}% {ctx.last_market_regime}): ${_session.daily_pnl:.2f} — halting")
        return
    if _session.daily_pnl >= cfg.DAILY_PROFIT_TARGET:
        log.info(f"[SYSTEM] Daily profit target reached: ${_session.daily_pnl:.2f}")
        return

    _session.check_quarterly(ctx.client, cfg.USE_QUARTERLY_TARGET, cfg.QUARTERLY_PROFIT_TARGET_PCT)

    sentiment = market_state.resolve_sentiment()

    # ── SA v2: blend market_outlook into sentiment ────────────────────────────
    # If SA outlook strongly disagrees with local sentiment, prefer SA (more data).
    # If SA is unavailable the local sentiment is unchanged.
    try:
        from engine.data.seeking_alpha import get_sa_market_outlook
        sa_outlook = get_sa_market_outlook()
        if sa_outlook:
            sa_sent = sa_outlook.get("sentiment", "neutral")
            bull_pct = sa_outlook.get("bullish_pct", 0.5)
            # Only override when SA is confident (>65%) and disagrees
            if bull_pct >= 0.65 and sentiment != "bullish":
                sentiment = "bullish"
                log.info(f"[SA2] Sentiment overridden → bullish (SA outlook {bull_pct:.0%} bull)")
            elif bull_pct <= 0.35 and sentiment != "bearish":
                sentiment = "bearish"
                log.info(f"[SA2] Sentiment overridden → bearish (SA outlook {bull_pct:.0%} bull)")
            else:
                log.info(f"[SA2] Market outlook: {sa_sent} ({bull_pct:.0%} bull) — local={sentiment}")
    except Exception:
        pass

    log.info(f"[SCAN] Market sentiment: {sentiment}")

    ctx.executor.update_stale_orders()
    ctx.executor.check_tp_targets()
    ctx.executor.check_dead_money()

    _run_discovery(ctx, market_state)

    scan_targets, excluded = _build_scan_targets(ctx)
    if not scan_targets:
        ctx.executor.check_live_probe_scale_ins(ctx.options_executor)
        log.info("[SCAN] No targets after filtering — skipping scan")
        return

    ctx.executor._swap_cycle_closed.clear()
    regime, signals_cap = _resolve_market_regime(ctx, market_state)

    signals, hit_counts, scan_errors = scan_universe(scan_targets, sentiment, market_state)

    if cfg.LONG_ONLY_MODE:
        pre = len(signals)
        signals = [s for s in signals if s.action == "buy"]
        log.warning(f"LONG_ONLY_MODE: filtered {pre} → {len(signals)} (buy-only)")

    breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(hit_counts.items()))
    log.info(f"[SCAN] Breakdown — {breakdown or 'none'} | Errors: {scan_errors} | Total: {len(signals)}")
    if not hit_counts:
        if not market_state.is_market_open:
            log.info("[SCAN] No signals — after hours (stale daily bars, intraday gates not met)")
        else:
            log.info("[SCAN] No signals — market likely in downtrend or momentum gates not met")

    for idx, s in enumerate(sorted(signals, key=lambda s: s.confidence, reverse=True)[:5], 1):
        log.info(f"[SCAN] TOP5_RAW #{idx}: {s.symbol} {s.action.upper()} ${s.price:.2f} conf={s.confidence:.0%} [{s.strategy}] — {s.reason}")

    if not signals:
        ctx.executor.check_live_probe_scale_ins(ctx.options_executor)
        log.info("[SCAN] No signals this cycle")
        return

    _, _, fresh_held = get_live_holdings(ctx.client)
    fresh_held = fresh_held or excluded
    log.info(f"Live holdings: {len(fresh_held)} excluded")

    eligible = _filter_eligible(ctx, signals, fresh_held, regime)
    _log_skipped(signals, eligible, fresh_held, regime, ctx.executor)

    for idx, s in enumerate(eligible[:5], 1):
        log.info(f"[TRADE] TOP5_ELIGIBLE #{idx}: {s.symbol} {s.action.upper()} ${s.price:.2f} conf={s.confidence:.0%} [{s.strategy}] — {s.reason}")

    save_day_picks(eligible[:5], regime)
    notify_scan_results(eligible[:5], datetime.date.today(), sentiment, regime)

    if not eligible:
        ctx.executor.check_live_probe_scale_ins(ctx.options_executor)
        log.info("[SCAN] No eligible signals after filtering")
        return

    # Fresh strategy evaluation is complete; now validate existing probes for scale-in.
    ctx.executor.check_live_probe_scale_ins(ctx.options_executor)

    entries_ok, entry_block_reason = _entries_allowed(market_state)
    if not entries_ok:
        log.info(f"[SCAN] New entries blocked — {entry_block_reason}; managing existing positions only")
        return

    if regime == "bear":
        _execute_bear_plan(ctx, eligible, daily_loss_limit, loss_pct)
    else:
        _execute_bull_plan(ctx, eligible, signals_cap, regime, daily_loss_limit, loss_pct)


# ── Status + interval helpers ─────────────────────────────────────────────────

def _fetch_account_and_positions(ctx: AppContext, timeout_seconds: int = 30):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: (ctx.client.get_account(), ctx.client.get_all_positions()))
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"Account status call timed out after {timeout_seconds}s")


def log_status(ctx: AppContext) -> None:
    try:
        account, positions = _fetch_account_and_positions(ctx, timeout_seconds=20)
        log.info("=" * 70)
        log.info("STATUS")
        log.info(f"Equity:     ${float(account.equity):,.2f}")
        log.info(f"Daily P&L:  ${_session.daily_pnl:.2f}  |  Trades: {_session.trades}")
        if cfg.USE_QUARTERLY_TARGET and _session.quarterly_start_equity > 0:
            q_gain = ((float(account.equity) - _session.quarterly_start_equity) / _session.quarterly_start_equity) * 100
            log.info(f"Quarterly:  {q_gain:+.1f}% (target >= {cfg.QUARTERLY_PROFIT_TARGET_PCT:.0f}%)")
        log.info(f"Positions:  {len(positions)}")
        if positions:
            total_pnl = sum(float(getattr(p, "unrealized_pl", 0) or 0) for p in positions)
            log.info(f"Unrealized: ${total_pnl:.2f}")
            for p in positions:
                entry_price = float(getattr(p, "avg_entry_price", 0) or 0)
                unrealized_pnl = float(getattr(p, "unrealized_pl", 0) or 0)
                pct = float(getattr(p, "unrealized_plpc", 0) or 0) * 100
                log.info(
                    f"  {p.symbol}: {getattr(p, 'qty', 0)} @ ${entry_price:.2f} "
                    f"| ${unrealized_pnl:.2f} ({pct:+.2f}%)"
                )
        log.info("=" * 70)
    except Exception as e:
        log.error(f"Status error: {e}")


def get_adaptive_interval(ctx: AppContext) -> int:
    """Return next scan interval in minutes based on VIX, market phase, and position count."""
    if not cfg.ADAPTIVE_INTERVALS:
        return cfg.SCAN_INTERVAL_MIN

    market_state = ctx.market_state or MarketState.from_now()
    vix, vix_interval, vol = market_state.resolve_vix()
    interval     = vix_interval
    market_phase = "ALL DAY"

    if cfg.USE_MARKET_HOURS_TUNING:
        mkt_interval, market_phase = get_market_hours_interval(market_state.hour, {
            "PREMARKET_SCAN_INTERVAL":     cfg.PREMARKET_SCAN_INTERVAL,
            "REGULAR_HOURS_SCAN_INTERVAL": cfg.REGULAR_HOURS_SCAN_INTERVAL,
            "AFTERHOURS_SCAN_INTERVAL":    cfg.AFTERHOURS_SCAN_INTERVAL,
        })
        if mkt_interval is not None:
            interval = mkt_interval

    pos_status = "DISABLED"
    if cfg.USE_POSITION_TUNING:
        try:
            pos_count  = len(ctx.client.get_all_positions())
            pos_interval, pos_status = get_position_tuning_interval(pos_count, {
                "HIGH_POSITION_INTERVAL":   cfg.HIGH_POSITION_INTERVAL,
                "NORMAL_POSITION_INTERVAL": cfg.NORMAL_POSITION_INTERVAL,
                "LOW_POSITION_INTERVAL":    cfg.LOW_POSITION_INTERVAL,
            })
            if pos_interval is not None:
                interval = max(interval, pos_interval)
        except Exception as e:
            log.debug(f"Position tuning check failed: {e}")
            pos_status = "POS CHECK ERROR"

    log.info(f"VIX: {vix:.2f} ({vol}) | {market_phase} | {pos_status} | Scan: {interval} min")
    return interval


def _prune_universe_job() -> None:
    try:
        from .equity.universe import prune as _prune
        removed = _prune()
        if removed:
            log.info(f"Universe pruned: {len(removed)} expired ticker(s): {removed[:10]}{'…' if len(removed) > 10 else ''}")
        else:
            log.info("Universe pruned: no expired tickers")
    except Exception as e:
        log.warning(f"Universe prune failed: {e}")


# ── Top3-only (dry-run) mode ──────────────────────────────────────────────────

def scan_top3_only(ctx: AppContext) -> None:
    market_state = ctx.market_state or MarketState.from_now()
    ctx.market_state = market_state
    sentiment = get_market_sentiment()
    log.info(f"Market sentiment: {sentiment}")
    _run_discovery(ctx, market_state)
    _, _, excluded = get_live_holdings(ctx.client)
    scan_targets   = get_scan_targets(excluded)
    log.info(f"Top3 mode: scanning {len(scan_targets)} symbols ({len(excluded)} pre-excluded)")
    signals, _, scan_errors = scan_universe(scan_targets, sentiment, market_state)
    log.info(f"Scan errors: {scan_errors} | Signals: {len(signals)}")
    if not signals:
        log.info("No signals found in Top3 mode")
        return
    _, _, fresh_held = get_live_holdings(ctx.client)
    fresh_held = fresh_held or excluded
    top5 = [s for s in signals if s.symbol not in fresh_held][:5]
    if not top5:
        log.info("No signals (all candidates already held)")
        return
    log.info("TOP 5 SCAN PICKS:")
    for idx, s in enumerate(top5, 1):
        log.info(f"#{idx}: {s.symbol} {s.action.upper()} ${s.price:.2f} conf={s.confidence:.0%} [{s.strategy}] — {s.reason}")
    notify_scan_results(top5, datetime.date.today(), sentiment, ctx.last_market_regime)


# ── Main loop ─────────────────────────────────────────────────────────────────

# ── Software-stop / scalp fast-poll thread ───────────────────────────────────
# PDT-blocked stops need frequent polling regardless of the adaptive scan
# interval (which can stretch to 20 min in calm markets). MomentumScalp
# positions ("go huge, sell fast") likewise need their +5% scale-out and
# ratchet exits evaluated far faster than the 3-minute scan cadence.
# This thread runs independently at a fixed 10-second cadence and only makes
# broker calls when there is something to monitor. Non-scalp profit management
# stays on the scan cadence; EnhancedExecutor.check_tp_targets holds a
# nonblocking lock so the two paths can never race into duplicate exits.

def _software_stop_poll_once(ctx: AppContext) -> None:
    """One 10-second fast-monitor iteration: PDT software stops plus fast
    profit monitoring for active MomentumScalp positions only."""
    if ctx.executor._pdt_stop_blocked:
        ctx.executor.check_software_stops()
    if ctx.executor.has_active_scalp_positions():
        ctx.executor.check_tp_targets(only_profiles={"scalp"})


def _start_software_stop_thread(ctx: AppContext) -> None:
    """Spawn a daemon thread that polls software stops / scalp exits every 10 seconds."""
    import threading

    def _loop() -> None:
        while True:
            try:
                _software_stop_poll_once(ctx)
            except Exception as e:
                log.error(f"[STOP-THREAD] fast-monitor error: {e}", exc_info=True)
            time.sleep(10)

    t = threading.Thread(target=_loop, name="SoftwareStopPoller", daemon=True)
    t.start()
    log.info("[STOP-THREAD] Software-stop + scalp fast-poll thread started (10s interval)")


def start() -> None:
    ctx = _build_context()

    _session.load_quarterly_state()
    log.info("=" * 70)
    log.info("APEXTRADER - Priority-Based Momentum Trading")
    log.info("=" * 70)
    log.info(f"Priority 1 (Momentum): {len(cfg.PRIORITY_1_MOMENTUM)} stocks")
    log.info(f"Priority 2 (Established): {len(cfg.PRIORITY_2_ESTABLISHED)} stocks")
    log.info(f"Total Universe: {sum(len(v) for v in cfg.STOCKS.values())} stocks")
    log.info(f"Scan: {'ADAPTIVE (VIX-based)' if cfg.ADAPTIVE_INTERVALS else f'{cfg.SCAN_INTERVAL_MIN} min fixed'}")
    log.info("=" * 70)

    try:
        account = ctx.client.get_account()
        log.info(f"Equity:          ${float(account.equity):,.2f}")
        log.info(f"Buying Power:    ${float(account.buying_power):,.2f}")
        log.info(f"PDT Status:      {'Yes' if account.pattern_day_trader else 'No'}")
        log.info(f"Day Trade Count: {account.daytrade_count}")
    except Exception as e:
        log.error(f"Account info error: {e}")

    log.info("=" * 70)
    log.info("Starting… Press Ctrl+C to stop")
    log.info("=" * 70)

    try:
        if cfg.PROTECT_POSITIONS_ENABLED:
            ctx.executor.protect_positions()
    except Exception as e:
        log.error(f"protect_positions startup error: {e}", exc_info=True)

    # Start the dedicated software-stop monitor thread
    _start_software_stop_thread(ctx)

    # Block until startup TI capture completes (up to 90s)
    # Skip on weekends — only crypto runs, TI universe is irrelevant
    import datetime as _dt
    _startup_weekend = _dt.datetime.now(pytz.timezone("America/New_York")).weekday() >= 5
    if cfg.USE_TRADEIDEAS_DISCOVERY and not _startup_weekend:
        try:
            log.info("Startup TI capture — refreshing universe before first scan…")
            _discovery.scan_tradeideas_universe(
                enabled=cfg.USE_TRADEIDEAS_DISCOVERY,
                scan_interval_min=cfg.TRADEIDEAS_SCAN_INTERVAL_MIN,
                headless=cfg.TRADEIDEAS_HEADLESS,
                chrome_profile=cfg.TRADEIDEAS_CHROME_PROFILE,
                update_config=cfg.TRADEIDEAS_UPDATE_CONFIG_FILE,
                priority_1=cfg.PRIORITY_1_MOMENTUM,
                priority_2=cfg.PRIORITY_2_ESTABLISHED,
                browser=cfg.TRADEIDEAS_BROWSER,
                remote_debug_port=9222,
            )
            fut = getattr(_discovery, "_ti_future", None)
            if fut is not None:
                if cfg.STARTUP_TI_CAPTURE_TIMEOUT_S > 0:
                    log.info(
                        f"Waiting up to {cfg.STARTUP_TI_CAPTURE_TIMEOUT_S}s for startup TI capture…"
                    )
                    try:
                        fut.result(timeout=cfg.STARTUP_TI_CAPTURE_TIMEOUT_S)
                    except concurrent.futures.TimeoutError:
                        log.warning("Startup TI capture timed out — proceeding with current universe")
                    except Exception as e:
                        log.warning(f"Startup TI capture failed: {e}")
                else:
                    log.info(
                        "Startup TI capture is running in background; first scan will use current universe. "
                        "Use this only if fresh TI tickers are not required at startup."
                    )
        except Exception as e:
            log.warning(f"Startup TI capture error: {e}")

    try:
        scan_and_trade(ctx)
    except Exception as e:
        log.error(f"Initial scan error: {e}", exc_info=True)

    last_vix_check    = time.time()
    _is_weekend_now   = _dt.datetime.now(pytz.timezone("America/New_York")).weekday() >= 5
    current_interval  = cfg.CRYPTO_SCAN_INTERVAL_MIN if (_is_weekend_now and cfg.CRYPTO_ENABLED) else get_adaptive_interval(ctx)
    last_scan         = time.time()

    schedule.every(30).minutes.do(log_status, ctx)
    schedule.every(30).minutes.do(_prune_universe_job)

    try:
        while True:
            try:
                # Refresh interval every 15 min
                if (time.time() - last_vix_check) >= 900:
                    import datetime as _dt
                    _weekend = _dt.datetime.now(pytz.timezone("America/New_York")).weekday() >= 5
                    if _weekend and cfg.CRYPTO_ENABLED:
                        new_interval = cfg.CRYPTO_SCAN_INTERVAL_MIN
                    elif cfg.ADAPTIVE_INTERVALS:
                        new_interval = get_adaptive_interval(ctx)
                    else:
                        new_interval = current_interval
                    if new_interval != current_interval:
                        log.info(f"Scan interval: {current_interval} → {new_interval} min")
                        current_interval = new_interval
                    last_vix_check = time.time()

                if (time.time() - last_scan) >= (current_interval * 60):
                    try:
                        if cfg.PROTECT_POSITIONS_ENABLED:
                            ctx.executor.protect_positions()
                    except Exception as e:
                        log.error(f"protect_positions error: {e}", exc_info=True)
                    # check_software_stops() runs in its dedicated 10s thread — not here

                    try:
                        scan_and_trade(ctx)
                    except Exception as e:
                        log.error(f"Scan cycle error: {e}", exc_info=True)

                    last_scan = time.time()
                    log.info(f"Heartbeat: {datetime.datetime.now().isoformat()}")

                schedule.run_pending()
                time.sleep(5)

            except KeyboardInterrupt:
                log.info("Stopped by user")
                log_status(ctx)
                break
            except Exception as e:
                log.error(f"Main loop error: {e}", exc_info=True)
                time.sleep(10)

    except KeyboardInterrupt:
        log.info("Stopped by user")
        log_status(ctx)


# ── Public entry point ────────────────────────────────────────────────────────

def run(*, force: bool = False, once: bool = False, top3_only: bool = False) -> None:
    if force:
        cfg.FORCE_SCAN = True

    if top3_only:
        ctx = _build_context()
        log.info("APEXTRADER — Top3 scan mode")
        scan_top3_only(ctx)
        log_status(ctx)
        return

    if once:
        ctx = _build_context()
        log.info("=" * 70)
        log.info("APEXTRADER — Single Scan Cycle")
        log.info("=" * 70)
        scan_and_trade(ctx)
        log_status(ctx)
        return

    start()
