"""
ApexTrader - Configuration
Professional Automated Trading System
Modular architecture with multiple strategies and PDT compliance
"""


import os
# .env is loaded once by main.py at process start.
# config.py reads env vars directly via os.getenv — no dotenv call here.

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Broker Selection
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
STOCKS_BROKER = os.getenv("STOCKS_BROKER", "alpaca")   # 'alpaca' or 'etrade'
OPTIONS_BROKER = "alpaca"                               # Only Alpaca supports options

# ─────────────────────────────────────────────────────────────────
# Options Trading Configuration (Level 3 account)
#
# MASTER KILL-SWITCH: set OPTIONS_ENABLED=false in .env to disable
# all options trading system-wide (scanner, executor, main loop).
# Default: true (enabled). Safe to flip live without restart via env.
#
# Allocation: 15% of portfolio. Strategies: momentum calls, bear puts,
# covered calls on held positions. Expiry: 7–21 DTE (near-term).
# ─────────────────────────────────────────────────────────────────
OPTIONS_ENABLED             = os.getenv("OPTIONS_ENABLED", "true").lower() in ("1", "true", "yes")
OPTIONS_STANDALONE_ENTRIES_ENABLED = os.getenv("OPTIONS_STANDALONE_ENTRIES_ENABLED", "true").lower() in ("1", "true", "yes")
OPTIONS_ALLOCATION_PCT      = float(os.getenv("OPTIONS_ALLOCATION_PCT", "15.0"))  # % of equity for all options (override via .env)
OPTIONS_MAX_POSITIONS       = int(os.getenv("OPTIONS_MAX_POSITIONS", "4"))        # max open options positions total
OPTIONS_MAX_MLEG_POSITIONS  = int(os.getenv("OPTIONS_MAX_MLEG_POSITIONS", "2"))   # hard limit: max open spreads/butterflies/condors
OPTIONS_MAX_MLEG_CONTRACTS  = int(os.getenv("OPTIONS_MAX_MLEG_CONTRACTS", "2"))   # hard limit: max contracts per spread entry
# Comma-separated list of allowed strategy names. Empty = all strategies enabled.
# Example: OPTIONS_ALLOWED_STRATEGIES=MomentumCall,CoveredCall
_raw_allowed = os.getenv("OPTIONS_ALLOWED_STRATEGIES", "")
OPTIONS_ALLOWED_STRATEGIES  = {s.strip() for s in _raw_allowed.split(",") if s.strip()} if _raw_allowed.strip() else set()
OPTIONS_DTE_MIN             = int(os.getenv("OPTIONS_DTE_MIN", "14"))             # min days-to-expiry at entry (14 avoids forced same-day close = PDT hit)
OPTIONS_DTE_MAX             = int(os.getenv("OPTIONS_DTE_MAX", "40"))             # max days-to-expiry at entry
OPTIONS_DELTA_TARGET        = float(os.getenv("OPTIONS_DELTA_TARGET", "0.55"))    # target delta — 0.55 = ATM/slight ITM (higher profit/point)
OPTIONS_MIN_OPEN_INTEREST   = int(os.getenv("OPTIONS_MIN_OPEN_INTEREST", "300"))   # per-strike OI floor — weeds out illiquid strikes
OPTIONS_MAX_SPREAD_PCT      = float(os.getenv("OPTIONS_MAX_SPREAD_PCT", "10.0"))  # max bid/ask spread % of mid
OPTIONS_MAX_IV_PCT          = float(os.getenv("OPTIONS_MAX_IV_PCT", "150.0"))     # skip when IV is extreme
OPTIONS_MIN_IV_PCT          = float(os.getenv("OPTIONS_MIN_IV_PCT", "15.0"))      # skip when IV is too flat
# ─────────────────────────────────────────────────────────────────
# Options Stop-Loss & Profit-Taking Strategy (OPTIMIZED)
# ─────────────────────────────────────────────────────────────────
# *** SPREADS: Use strategy-specific SL (profit target + underlying movement + DTE)
# *** NAKED: Use -25% debit SL (percentage-based) — for long calls/puts without caps
# Tightened from -35%: naked calls decay fast; cut losers quicker to preserve capital for re-entries.
# Grace period extended to 3 days — options need time to settle after entry.
# Scale-out strategy: Close 50% at first target, hold 50% with tighter stop for max profit.
OPTIONS_STOP_LOSS_PCT       = float(os.getenv("OPTIONS_STOP_LOSS_PCT", "25.0"))      # -25% loss for NAKED options (NOT spreads) — unchanged
OPTIONS_PROFIT_TARGET_1_PCT = float(os.getenv("OPTIONS_PROFIT_TARGET_1_PCT", "25.0"))  # was 50% — close 50% of position at +25% (lock profit faster)
OPTIONS_PROFIT_TARGET_1_STOP_PCT = float(os.getenv("OPTIONS_PROFIT_TARGET_1_STOP_PCT", "15.0"))  # was 20% — tighter stop on 2nd half (aggressive)
OPTIONS_PROFIT_TARGET_2_PCT = float(os.getenv("OPTIONS_PROFIT_TARGET_2_PCT", "50.0"))  # was 100% — close remaining 50% at +50% (aggressive exit)
OPTIONS_PROFIT_TARGET_PCT   = OPTIONS_PROFIT_TARGET_1_PCT  # Backward compatibility alias
OPTIONS_ENTRY_GRACE_DAYS    = int(os.getenv("OPTIONS_ENTRY_GRACE_DAYS", "3"))        # extended from 2 to 3 days for better settling
OPTIONS_THETA_EXIT_DTE      = int(os.getenv("OPTIONS_THETA_EXIT_DTE", "4"))           # exit by DTE ≤ 4 (was 2) — avoid theta acceleration spike
OPTIONS_COVERED_CALL_DELTA  = float(os.getenv("OPTIONS_COVERED_CALL_DELTA", "0.25")) # sell OTM calls ~0.25 delta
OPTIONS_MIN_SIGNAL_CONFIDENCE = float(os.getenv("OPTIONS_MIN_SIGNAL_CONFIDENCE", "0.80"))  # entry threshold for major caps (A+ grade)
OPTIONS_MIN_SIGNAL_CONFIDENCE_TI = float(os.getenv("OPTIONS_MIN_SIGNAL_CONFIDENCE_TI", "0.65"))  # TI universe: lower threshold for unusual options
OPTIONS_MIN_STOCK_PRICE     = float(os.getenv("OPTIONS_MIN_STOCK_PRICE", "8.0"))   # sub-$8 stocks have wide spreads and thin option chains
OPTIONS_MIN_MOVE_PCT        = float(os.getenv("OPTIONS_MIN_MOVE_PCT", "1.5"))      # min % daily move for major caps
OPTIONS_MIN_MOVE_PCT_TI     = float(os.getenv("OPTIONS_MIN_MOVE_PCT_TI", "0.8"))   # min % daily move for TI unusual (lower threshold)
OPTIONS_MIN_RVOL            = float(os.getenv("OPTIONS_MIN_RVOL", "1.5"))          # min relative volume for major caps
OPTIONS_MIN_RVOL_TI         = float(os.getenv("OPTIONS_MIN_RVOL_TI", "1.0"))       # min relative volume for TI unusual (lower threshold)
OPTIONS_MIN_ADV             = float(os.getenv("OPTIONS_MIN_ADV", "500_000"))     # min avg dollar volume for major caps
OPTIONS_MIN_ADV_TI          = float(os.getenv("OPTIONS_MIN_ADV_TI", "150_000"))   # min ADV for TI unusual options (lower threshold for micro-cap unusual picks)
OPTIONS_UNIVERSE_OVERRIDE   = os.getenv("OPTIONS_UNIVERSE_OVERRIDE", "").strip()  # comma-separated tickers to force a smaller options universe
OPTIONS_STOP_COOLDOWN_DAYS  = int(os.getenv("OPTIONS_STOP_COOLDOWN_DAYS", "2"))   # no re-entry within N days after a stop on same symbol
OPTIONS_IV_RANK_SPREAD_THRESHOLD = int(os.getenv("OPTIONS_IV_RANK_SPREAD_THRESHOLD", "60"))  # IV rank above this → force spread; below → allow naked
OPTIONS_EARNINGS_AVOID_DAYS = int(os.getenv("OPTIONS_EARNINGS_AVOID_DAYS", "15")) # skip entries if earnings within N calendar days
# Trailing stop: locks in gains after big moves. Tight for aggressive no-PDT trading.
OPTIONS_TRAIL_ACTIVATE_PCT  = float(os.getenv("OPTIONS_TRAIL_ACTIVATE_PCT", "15.0"))  # was 25% — trailing stop arms at +15% P&L (aggressive)
OPTIONS_TRAIL_DRAWDOWN_PCT  = float(os.getenv("OPTIONS_TRAIL_DRAWDOWN_PCT", "10.0"))  # was 20% — close if drops 10pp from peak (tight exit)
OPTIONS_LIVE_PROBE_MODE = os.getenv("OPTIONS_LIVE_PROBE_MODE", "false").lower() in ("1", "true", "yes")
OPTIONS_LIVE_PROBE_STOP_LOSS_PCT = float(os.getenv("OPTIONS_LIVE_PROBE_STOP_LOSS_PCT", "10.0"))

# Spread-Specific Stop-Loss Strategy (NEW)
# ─────────────────────────────────────────────────────────────────
# Spreads have CAPPED risk (max loss = debit paid) so -35% debit SL is wrong.
# Instead: profit_target (50-75% of max gain) + underlying_movement SL + DTE SL
SPREAD_PROFIT_TARGET_LOWER = float(os.getenv("SPREAD_PROFIT_TARGET_LOWER", "50.0"))  # close 50% of max gain
SPREAD_PROFIT_TARGET_UPPER = float(os.getenv("SPREAD_PROFIT_TARGET_UPPER", "75.0"))  # close 75% of max gain (prefer 50%)
SPREAD_UNDERLYING_SL_PCTFROM_LONG = float(os.getenv("SPREAD_UNDERLYING_SL_PCTFROM_LONG", "1.0"))  # SL if underlying breaches 1% below long strike
SPREAD_DTE_EXIT_THRESHOLD   = int(os.getenv("SPREAD_DTE_EXIT_THRESHOLD", "7"))         # exit spread if DTE < 7 days and profit < 50%

# Tickers that actively trade liquid options.
# Loaded dynamically from data/ti_unusual_options.json (written by capture_tradeideas.py
# every time the TI unusualoptionsvolume scan is scraped).  Falls back to the
# hardcoded list below if the file doesn't exist or is empty.
_OPTIONS_FALLBACK_UNIVERSE = [
    # SPX/NDX liquid proxies — tightest spreads, deepest chains
    "SPY", "QQQ", "IWM", "DIA",
    "SPXL", "SPXS", "TQQQ", "SQQQ", "SPXU", "UVXY", "VXX",
    # Sector ETFs with liquid options
    "XLF", "XLE", "XLK", "XLV", "XLU", "XLP", "SMH", "ARKK",
    # Mega-cap tech — always liquid options
    "AAPL", "MSFT", "NVDA", "AMD", "GOOGL", "GOOG", "META", "TSLA", "AMZN", "NFLX",
    "ORCL", "CRM", "ADBE", "INTC", "QCOM",
    # High-beta momentum / high-OI
    "MARA", "COIN", "PLTR", "SMCI", "CRWD", "NET", "SNOW", "MSTR",
    "SOFI", "RIVN", "LCID", "NIO", "BABA", "JD",
    # Financials with deep chains
    "JPM", "BAC", "GS", "MS", "C",
    # Energy
    "XOM", "CVX", "OXY",
    # Biotech / speculative with options
    "MRNA", "BNTX", "BCRX",
    # Short squeeze candidates — high short float + improving fundamentals
    "LITE", "AAOI",
]

def _load_options_universe() -> list:
    """Load live TI unusual-options-volume tickers.

    Returns the scraped unusual options universe, falling back to a hardcoded list
    only when the TI file is unavailable or empty.
    """
    import json as _json
    import re as _re
    _VALID_TICKER = _re.compile(r'^[A-Z]{1,5}$')
    _ti_file = os.path.join(os.path.dirname(__file__), "..", "data", "ti_unusual_options.json")
    try:
        with open(_ti_file, encoding="utf-8") as _f:
            _d = _json.load(_f)
        _tickers = [
            str(t).upper().strip()
            for t in _d.get("tickers", [])
            if t and _VALID_TICKER.match(str(t).upper().strip())
        ]
        if _tickers:
            return _tickers
    except Exception:
        pass
    return _OPTIONS_FALLBACK_UNIVERSE


def get_options_universe(require_ti_file: bool = False) -> list:
    """Return the live options universe, applying override rules.

    Core liquid names (SPY, QQQ, mega-caps, sector ETFs) are ALWAYS prepended so
    the options scanner always evaluates names with deep chains, regardless of what
    the TI equity universe contains (which is often micro-cap momentum names that
    have thin or no options chains).

    Primary TI source appended after the core set: latest ti_primary.json.
    Fallback: universe.json tier 1+2, then static _OPTIONS_FALLBACK_UNIVERSE.
    """
    if OPTIONS_UNIVERSE_OVERRIDE:
        import re as _re
        _VALID_TICKER_OVERRIDE = _re.compile(r'^[A-Z]{1,5}$')
        _override_symbols = [
            t.strip().upper()
            for t in OPTIONS_UNIVERSE_OVERRIDE.split(",")
            if t and _VALID_TICKER_OVERRIDE.match(t.strip().upper())
        ]
        if _override_symbols:
            return list(dict.fromkeys(_override_symbols))

    # Core liquid options names — always included first regardless of TI data.
    # These have the tightest spreads, deepest chains, and highest OI.

    # Always include index tickers in paper trading mode
    _index_tickers = ["SPX", "NDX", "RUT", "VIX"]
    _core = list(dict.fromkeys(_OPTIONS_FALLBACK_UNIVERSE))
    try:
        if PAPER:
            # Prepend index tickers if not already present
            for idx in reversed(_index_tickers):
                if idx not in _core:
                    _core.insert(0, idx)
    except Exception:
        pass

    ti_universe = []
    try:
        from engine.equity.universe import get_ti_primary as _get_ti_primary
        ti_universe = list(dict.fromkeys(_get_ti_primary()))
    except Exception:
        pass

    if not ti_universe:
        try:
            from engine.equity.universe import get_tier as _get_tier
            ti_universe = list(dict.fromkeys(_get_tier(1) + _get_tier(2)))
        except Exception:
            pass

    if not ti_universe and require_ti_file:
        raise FileNotFoundError("Primary TI universe (data/ti_primary.json or data/universe.json tiers 1+2) is missing or empty")

    # Merge: core first, then TI names not already in core
    core_set = set(_core)
    combined = _core + [s for s in ti_universe if s not in core_set]
    return combined


OPTIONS_ELIGIBLE_UNIVERSE = get_options_universe()

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Alpaca API Configuration
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# PAPER mode is strongly recommended for development/testing.
# Set environment variable TRADE_MODE=paper or TRADE_MODE=live.
TRADE_MODE = os.getenv("TRADE_MODE", "paper").lower()
PAPER      = TRADE_MODE == "paper"
LIVE       = not PAPER
_MODE      = "PAPER" if PAPER else "LIVE"

API_KEY    = os.getenv(f"{_MODE}_ALPACA_API_KEY", "")
API_SECRET = os.getenv(f"{_MODE}_ALPACA_API_SECRET", "")
# SDK picks the correct endpoint automatically via paper=True/False — no URL override needed
ALPACA_BASE_URL = "https://paper-api.alpaca.markets" if PAPER else "https://api.alpaca.markets"

# Market data feed: "sip" for paid Alpaca subscribers (full consolidated tape),
# "iex" for free-tier users (IEX exchange only, ~15-50% of consolidated volume).
# Set ALPACA_DATA_FEED=sip in .env when you have an Alpaca Algo Trader or higher plan.
ALPACA_DATA_FEED: str = os.getenv("ALPACA_DATA_FEED", "iex").lower()

# Convenience for switching: override per branch by env var if needed.
MIN_POSITION_DOLLARS = float(os.getenv("MIN_POSITION_DOLLARS", "500"))
MIN_BUYING_POWER_PCT = float(os.getenv("MIN_BUYING_POWER_PCT", "10.0"))

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# E*TRADE API Configuration
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
ETRADE_CONSUMER_KEY    = os.getenv("ETRADE_CONSUMER_KEY", "")
ETRADE_CONSUMER_SECRET = os.getenv("ETRADE_CONSUMER_SECRET", "")
ETRADE_ACCOUNT_ID      = os.getenv("ETRADE_ACCOUNT_ID", "")
ETRADE_SANDBOX         = os.getenv("ETRADE_SANDBOX", "false").lower() == "true"

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Stock Universe
# Priority 1: Momentum stocks (scanned FIRST, highest allocation)
# Priority 2: Established tech and high short-float stocks
# Priority 3: Market ETFs for context
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
PRIORITY_1_MOMENTUM = [
    # ── Permanent core (never expire, always scanned) ──────────────
    # Crypto-leveraged / popular momentum plays
    "MARA", "WULF", "CORZ", "HUT", "IREN",
    # Biotech / speculative momentum
    "MRNA", "BCRX", "SNDX", "IMVT",
    # Energy / commodities momentum
    "RIG", "NOG", "CNX", "BTU", "DK",
    # ── Bear-market long plays (inverse ETFs — go UP when market falls) ──
    # Valid LONG buys in bear regime as LONG_ONLY_MODE=True
    "SQQQ", "SPXU", "UVXY", "TZA", "FAZ", "SOXS", "LABD", "DUST",
]

PRIORITY_2_ESTABLISHED = [
    # ── Permanent core (never expire) ─────────────────────────────
    # Tech giants — liquid at all times
    "AAPL", "MSFT", "NVDA", "AMD", "GOOGL", "META", "TSLA", "AMZN",
    # High short-float perennials
    "LCID", "MVIS", "WKHS", "SNDX", "FUBO", "INDO", "SOXS", "UCO",
]

PRIORITY_3_MARKET = ["SPY", "QQQ", "IWM", "^VIX"]

# Delisted or broken tickers — filtered out at runtime
DELISTED_STOCKS = [
    # Truly delisted
    "IMV", "EKV", "AMTK", "SUNE",
    "CGV", "CHAC", "CIFG", "CNVS",
    # Index tickers (not tradeable)
    "DJI", "$DJI",
    # Broken / no-data tickers seen in live scans
    "ADR", "BF", "AMEX", "ADVB",
]

# Remove delisted from core lists
PRIORITY_1_MOMENTUM = [s for s in PRIORITY_1_MOMENTUM if s not in DELISTED_STOCKS]
PRIORITY_2_ESTABLISHED = [s for s in PRIORITY_2_ESTABLISHED if s not in DELISTED_STOCKS]

# ─── Dynamic universe: load TTL-managed tickers from data/universe.json ───────
# Trade Ideas updates and prediction picks live there, NOT in this file.
# Universe TTL values are configurable via env vars:
#   UNIVERSE_TTL_TIER1, UNIVERSE_TTL_TIER2, UNIVERSE_TTL_TIER3
# Defaults are currently 15 minutes per tier for live scan freshness.
# get_dynamic_universe() is called live each scan cycle so newly scraped TI
# tickers are picked up without restarting the bot.
from engine.equity.universe import get_tier as _get_tier, merge_live as _merge_live


def get_dynamic_universe() -> tuple:
    """Return (p1, p2, p3) merged lists, re-reading universe.json on every call."""
    _ex = set(DELISTED_STOCKS)
    p1 = _merge_live(_get_tier(1), PRIORITY_1_MOMENTUM,    _ex)
    p2 = _merge_live(_get_tier(2), PRIORITY_2_ESTABLISHED, _ex)
    p3 = _merge_live(_get_tier(3), [],                     _ex)
    return p1, p2, p3


# Module-level lists: populated once at startup as fallback / for any code that
# imports them directly.  get_scan_targets() always calls get_dynamic_universe()
# so the running bot never relies on these being fresh.
_dyn1, _dyn2, _dyn3 = get_dynamic_universe()
PRIORITY_1_MOMENTUM    = _dyn1
PRIORITY_2_ESTABLISHED = _dyn2
PRIORITY_FOLLOWING     = _dyn3
del _dyn1, _dyn2, _dyn3

STOCKS = {
    "priority_1": PRIORITY_1_MOMENTUM,
    "priority_2": PRIORITY_2_ESTABLISHED,
    "priority_3": PRIORITY_3_MARKET,
    "following":  PRIORITY_FOLLOWING,
}

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Trading Parameters ΓÇö Swing Trading Optimized
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
MAX_POSITIONS        = int(os.getenv("MAX_POSITIONS", "10"))  # max open portfolio positions; override via .env
# When full, close the weakest position to make room if new signal conf > this threshold
SWAP_ON_FULL         = True   # enabled — close weakest position for a better signal when full
SWAP_MIN_CONFIDENCE  = 0.68   # was 0.75 — lower threshold → more aggressive swaps
POSITION_SIZE_PCT    = float(os.getenv("POSITION_SIZE_PCT", "6.0"))  # % of equity per position; override via .env
LIVE_PROBE_MODE      = os.getenv("LIVE_PROBE_MODE", "false").lower() in ("1", "true", "yes")
LIVE_PROBE_SHARES    = max(1, int(os.getenv("LIVE_PROBE_SHARES", "1")))
LIVE_PROBE_MAX_ENTRIES_PER_DAY = max(1, int(os.getenv("LIVE_PROBE_MAX_ENTRIES_PER_DAY", "10")))
LIVE_PROBE_SCALE_IN_ENABLED = os.getenv("LIVE_PROBE_SCALE_IN_ENABLED", "false").lower() in ("1", "true", "yes")
LIVE_PROBE_SCALE_IN_ATM_OPTION_ENABLED = os.getenv("LIVE_PROBE_SCALE_IN_ATM_OPTION_ENABLED", "false").lower() in ("1", "true", "yes")
LIVE_PROBE_SCALE_IN_MIN_GAIN_PCT = float(os.getenv("LIVE_PROBE_SCALE_IN_MIN_GAIN_PCT", "0.5"))
LIVE_PROBE_SCALE_IN_BUYING_POWER_PCT = float(os.getenv("LIVE_PROBE_SCALE_IN_BUYING_POWER_PCT", "25.0"))
LIVE_PROBE_SCALE_IN_MAX_MULTIPLE = float(os.getenv("LIVE_PROBE_SCALE_IN_MAX_MULTIPLE", "1.0"))
LIVE_PROBE_SCALE_IN_MIN_HOLD_MINUTES = int(os.getenv("LIVE_PROBE_SCALE_IN_MIN_HOLD_MINUTES", "5"))
LIVE_PROBE_SCALE_IN_REQUIRE_VWAP = os.getenv("LIVE_PROBE_SCALE_IN_REQUIRE_VWAP", "true").lower() in ("1", "true", "yes")
LIVE_PROBE_SCALE_IN_REQUIRE_NEW_HIGH = os.getenv("LIVE_PROBE_SCALE_IN_REQUIRE_NEW_HIGH", "true").lower() in ("1", "true", "yes")
LIVE_PROBE_SCALE_IN_MAX_TOTAL_RISK_PCT = float(os.getenv("LIVE_PROBE_SCALE_IN_MAX_TOTAL_RISK_PCT", "0.5"))
LIVE_PROBE_MAX_TOTAL_BUYING_POWER_PCT = float(os.getenv("LIVE_PROBE_MAX_TOTAL_BUYING_POWER_PCT", "25.0"))
# Margin leverage multiplier: 1.0 = no leverage, 4.0 = 4× intraday margin (requires margin account + marginable stock)
# Only stocks flagged marginable=True by Alpaca are eligible when MARGIN_LEVERAGE > 1.0
MARGIN_LEVERAGE      = float(os.getenv("MARGIN_LEVERAGE", "1.0"))
USE_RISK_EQUALIZED_SIZING = False  # use fixed position sizing instead of risk-scaled
RISK_PER_TRADE_PCT   = 0.8    # Risk 0.8% of account per trade (unused with fixed sizing)

# Confidence-based position scaling: low-confidence signals get smaller allocations.
# Multiplier scales linearly from CONF_SCALE_MIN_MULT at MIN_SIGNAL_CONFIDENCE
# up to 1.0× at CONF_SCALE_FULL_CONF. Above that threshold: always full size.
CONF_SCALE_MIN_MULT  = 0.50   # 50% of normal size at the confidence floor (0.72)
CONF_SCALE_FULL_CONF = 0.85   # 100% of normal size at this confidence and above

# Small account reduction caps (sub-$5k equity)
SMALL_ACCOUNT_POSITION_SIZE_PCT = 5.0   # same 5.0% allocation for small accounts
SMALL_ACCOUNT_RISK_PER_TRADE_PCT = 0.5 # lower risk per trade for small accounts
SMALL_ACCOUNT_MIN_POSITION_DOLLARS = 5.0  # lowered to allow ~$5 entry for cheap tickers

# Tiered Profit Targets — all tiers overridable via .env (e.g. TAKE_PROFIT_NORMAL=15)
TAKE_PROFIT_EXTREME  = float(os.getenv("TAKE_PROFIT_EXTREME", "25.0"))
TAKE_PROFIT_HIGH     = float(os.getenv("TAKE_PROFIT_HIGH",    "25.0"))
TAKE_PROFIT_MEDIUM   = float(os.getenv("TAKE_PROFIT_MEDIUM",  "20.0"))
TAKE_PROFIT_NORMAL   = float(os.getenv("TAKE_PROFIT_NORMAL",  "15.0"))

# Tiered Trailing Stops — much tighter: lock in gains faster, no PDT concern
TRAILING_STOP_EXTREME = 8.0   # was 12% — tighter on extreme volatility
TRAILING_STOP_HIGH    = 6.0   # was 10% — tight on high momentum (6% drawdown triggers close)
TRAILING_STOP_MEDIUM  = 5.0   # was 8% — very tight for normal stocks
TRAILING_STOP_NORMAL  = 5.0   # was 8% — 5% trailing drawdown = quick exit

# Scale-out at TP: sell 50% of position, then trail the remaining 50%
# SCALEOUT_TRAIL_PCT: trailing stop % on the remaining half after TP is hit
SCALEOUT_TRAIL_PCT    = float(os.getenv("SCALEOUT_TRAIL_PCT", "5.0"))   # tight trail on remaining half
# PROTECT_POSITIONS_ENABLED: set false to skip protect_positions() — no trailing stops at entry
PROTECT_POSITIONS_ENABLED = os.getenv("PROTECT_POSITIONS_ENABLED", "false").lower() in ("1", "true", "yes")

# Legacy (backward compat)
STOP_LOSS_PCT   = 3.0
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "25.0"))  # legacy alias; override via .env

# ── Dynamic TP tightening (Option B) ───────────────────────────────────────────
# Phase 1 (+TP_INTERMEDIATE_PCT): cancel 10% trail, place tighter TP_INTERMEDIATE_TRAIL_PCT%
# Phase 2 (+TP_FINAL_PCT): close full position at market — guaranteed profit booking
TP_INTERMEDIATE_PCT       = float(os.getenv("TP_INTERMEDIATE_PCT",       "5.0"))  # tighten trail at +5%
TP_INTERMEDIATE_TRAIL_PCT = float(os.getenv("TP_INTERMEDIATE_TRAIL_PCT", "3.0"))  # new trail pct after +5%
TP_FINAL_PCT              = float(os.getenv("TP_FINAL_PCT",              "10.0")) # close full at +10%

# Peak-ratchet exit — let winners run. When enabled, the +10% hard close and
# the +5% tight-trail are replaced by a software high-water-mark ratchet: once a
# position gains TP_RATCHET_ARM_PCT it is allowed to run, and only exits when it
# retraces TP_RATCHET_GIVEBACK_PCT from its peak. Self-managed each scan cycle,
# so it does not depend on broker GTC trailing stops (which are often rejected).
TP_RATCHET_ENABLED        = os.getenv("TP_RATCHET_ENABLED", "true").lower() in ("1", "true", "yes")
TP_RATCHET_ARM_PCT        = float(os.getenv("TP_RATCHET_ARM_PCT",      "8.0"))   # arm the ratchet once gain >= this
TP_RATCHET_GIVEBACK_PCT   = float(os.getenv("TP_RATCHET_GIVEBACK_PCT", "8.0"))   # exit on this % retrace from peak

# ── Time-based loss exit ────────────────────────────────────────────────────
# Close positions still moving adversely after N minutes using 0.35 × ATR%,
# bounded between the configured minimum and maximum thresholds.
DEAD_MONEY_MINUTES                   = int(os.getenv("DEAD_MONEY_MINUTES", "90"))
DEAD_MONEY_MAX_ADVERSE_DRIFT_PCT     = float(os.getenv("DEAD_MONEY_MAX_ADVERSE_DRIFT_PCT", "1.5"))
TIME_LOSS_ATR_MULTIPLIER             = float(os.getenv("TIME_LOSS_ATR_MULTIPLIER", "0.35"))
TIME_LOSS_ATR_MIN_PCT                = float(os.getenv("TIME_LOSS_ATR_MIN_PCT", "1.0"))
TIME_LOSS_ATR_MAX_PCT                = float(os.getenv("TIME_LOSS_ATR_MAX_PCT", "2.5"))

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Dynamic ATR-Based Tier Assignment
# Lower thresholds = more stocks classified as high-volatility = tighter TP/SL
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
USE_DYNAMIC_TIERS  = True
ATR_TIER_EXTREME   = 5.0   # was 7.0
ATR_TIER_HIGH      = 3.0   # was 5.0
ATR_TIER_MEDIUM    = 1.5   # was 3.0

# Legacy static lists (used only if USE_DYNAMIC_TIERS=False)
EXTREME_MOMENTUM_STOCKS = ["UGRO", "VCX", "PTLE", "BIAF", "SATL", "ELAB"]
HIGH_MOMENTUM_STOCKS    = ["QNTM", "MRLN", "DMRA", "RCAX", "ALDX", "NAMM", "PAYP", "SER", "NAUT", "CGV"]

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Adaptive Scan Intervals (VIX-Based)
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
ADAPTIVE_INTERVALS          = os.getenv("ADAPTIVE_INTERVALS", "true").lower() in ("1", "true", "yes")
SCAN_INTERVAL_EXTREME_VOL   = 3    # VIX > 30
SCAN_INTERVAL_HIGH_VOL      = 5    # VIX 26-30
SCAN_INTERVAL_MODERATE_VOL  = 10   # VIX 22-26
SCAN_INTERVAL_NORMAL_VOL    = 15   # VIX 18-22
SCAN_INTERVAL_CALM_VOL      = 20   # VIX 15-18
SCAN_INTERVAL_LOW_VOL       = 30   # VIX < 15
SCAN_INTERVAL_MIN            = int(os.getenv("SCAN_INTERVAL_MIN", "10"))

# ─────────────────────────────────────────────────────────────────
# Kill Mode — Emergency Capital Protection
# Triggers a full portfolio close when extreme bear conditions hit.
# ─────────────────────────────────────────────────────────────────
KILL_MODE_VIX_LEVEL    = 40.0   # Absolute VIX level that triggers kill mode (2008/2020: 80+, crash: 40+)
KILL_MODE_SPY_DROP_PCT =  3.0   # SPY intraday drop from open (%) triggers kill mode
KILL_MODE_VIX_ROC_PCT  = 50.0   # VIX spike: up >50% in last 5 hours triggers kill mode
KILL_MODE_TRAIL_PCT    =  0.5   # PDT-safe hairpin trailing stop % placed on today's positions

# Market Hours Tuning
USE_MARKET_HOURS_TUNING    = True
PREMARKET_SCAN_INTERVAL    = 10
REGULAR_HOURS_SCAN_INTERVAL = 3
AFTERHOURS_SCAN_INTERVAL   = 10

# Position-Based Adaptive Scanning
USE_POSITION_TUNING      = True
HIGH_POSITION_INTERVAL   = 5    # was 10 — check more frequently when holding many positions
NORMAL_POSITION_INTERVAL = 3    # was 5
LOW_POSITION_INTERVAL    = 2    # was 3

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# VIX Rate-of-Change Filter
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
USE_VIX_ROC_FILTER  = True
VIX_ROC_THRESHOLD   = 20.0   # Block entries if VIX up >20% in last hour
VIX_ROC_PERIOD      = 5

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Live Trending Discovery
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
USE_LIVE_TRENDING       = False
TRENDING_SCAN_INTERVAL  = 60
TRENDING_MAX_RESULTS    = 20
TRENDING_MIN_MOMENTUM   = 3.0

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Finnhub Integration
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
USE_FINNHUB_DISCOVERY      = False
FINNHUB_API_KEY            = os.getenv("FINNHUB_API_KEY", "")
PRICE_DATA_SOURCE          = os.getenv("PRICE_DATA_SOURCE", "alpaca").strip().lower()
USE_FINNHUB_HISTORICAL     = PRICE_DATA_SOURCE == "finnhub" or os.getenv("USE_FINNHUB_HISTORICAL", "false").strip().lower() in ("1", "true", "yes")
SENTIMENT_BULLISH_THRESHOLD = 0.6

# ── Seeking Alpha Finance API (RapidAPI) ──────────────────────────────────────
SEEKING_ALPHA_API_KEY = os.getenv("SEEKING_ALPHA_API_KEY", "")
SEEKING_ALPHA_HOST    = os.getenv("SEEKING_ALPHA_HOST", "seeking-alpha.p.rapidapi.com")
USE_SEEKING_ALPHA     = bool(SEEKING_ALPHA_API_KEY)
# Sentiment gate is auto-enabled when a Seeking Alpha key is configured.
# It can also be force-enabled for yfinance-only fallback via env var.
USE_SENTIMENT_GATE    = USE_SEEKING_ALPHA or os.getenv("USE_SENTIMENT_GATE", "false").strip().lower() in ("1", "true", "yes")

# Trade Ideas Discovery
# Scrapes TIPro highshortfloat + marketscope360 with Selenium.
# Requires a logged-in Chrome profile (TRADEIDEAS_CHROME_PROFILE) to get real data.
# Disabled by default — without a profile the scraper only hits the TI login page.
# To enable: set USE_TRADEIDEAS_DISCOVERY=true and TRADEIDEAS_CHROME_PROFILE=<profile>
# TRADEIDEAS_BROWSER: "edge" (pre-installed on Windows) or "chrome"
USE_TRADEIDEAS_DISCOVERY                          = __import__('os').getenv('USE_TRADEIDEAS_DISCOVERY', 'false').lower() == 'true'
USE_TRADEIDEAS_UNUSUAL_OPTIONS_DISCOVERY         = __import__('os').getenv('USE_TRADEIDEAS_UNUSUAL_OPTIONS_DISCOVERY', 'true').lower() == 'true'
USE_TRADEIDEAS_TOPLISTS_DISCOVERY                = __import__('os').getenv('USE_TRADEIDEAS_TOPLISTS_DISCOVERY', 'false').lower() == 'true'
TRADEIDEAS_SCAN_INTERVAL_MIN                     = 15
TRADEIDEAS_UNUSUAL_OPTIONS_SCAN_INTERVAL_MIN     = 30
TRADEIDEAS_TOPLISTS_SCAN_INTERVAL_MIN            = 180
TRADEIDEAS_HEADLESS                              = __import__('os').getenv('TRADEIDEAS_HEADLESS', 'false').lower() == 'true'
TRADEIDEAS_CHROME_PROFILE                        = __import__('os').getenv('TRADEIDEAS_CHROME_PROFILE', '')
TRADEIDEAS_BROWSER                                = __import__('os').getenv('TRADEIDEAS_BROWSER', 'edge')
TRADEIDEAS_UPDATE_CONFIG_FILE                     = True
# Wait this many seconds for the startup Trade Ideas capture before the first scan.
# Default 90s preserves fresh TI tickers for the initial universe. Set to 0 only
# for advanced starts where background TI loading is acceptable.
STARTUP_TI_CAPTURE_TIMEOUT_S                     = int(__import__('os').getenv('STARTUP_TI_CAPTURE_TIMEOUT_S', '90'))
TI_PRIMARY_SCAN_BATCH_LIMIT                       = int(__import__('os').getenv('TI_PRIMARY_SCAN_BATCH_LIMIT', '50'))

# Sector sympathy scanner — injects peer tickers when a leader stock fires
USE_SECTOR_SYMPATHY          = False  # disabled — EDGAR 8-K is the primary discovery signal
SECTOR_SYMPATHY_INTERVAL_MIN = int(os.getenv("SECTOR_SYMPATHY_INTERVAL_MIN", "15"))

# EDGAR 8-K feed scanner — injects tickers from material event filings (free, no auth)
USE_EDGAR_SCANNER            = os.getenv("USE_EDGAR_SCANNER",    "true").lower() in ("1", "true", "yes")
EDGAR_SCANNER_INTERVAL_MIN   = int(os.getenv("EDGAR_SCANNER_INTERVAL_MIN",   "2"))
USE_PREOPEN_INTELLIGENCE     = os.getenv("USE_PREOPEN_INTELLIGENCE", "true").lower() in ("1", "true", "yes")
PREOPEN_INTELLIGENCE_SCAN_INTERVAL_MIN = int(os.getenv("PREOPEN_INTELLIGENCE_SCAN_INTERVAL_MIN", "15"))
PREOPEN_INTELLIGENCE_MAX_TICKERS = int(os.getenv("PREOPEN_INTELLIGENCE_MAX_TICKERS", "20"))
PREOPEN_USE_REGIME_GATING    = os.getenv("PREOPEN_USE_REGIME_GATING", "true").lower() in ("1", "true", "yes")
PREOPEN_USE_SENTIMENT_GATING = os.getenv("PREOPEN_USE_SENTIMENT_GATING", "true").lower() in ("1", "true", "yes")

# ─────────────────────────────────────────────────────────────────
# Crypto Weekend Trader
# ─────────────────────────────────────────────────────────────────
# Master switch — set CRYPTO_ENABLED=false in .env to disable entirely.
# When enabled the bot runs crypto-only on Saturday + Sunday and skips
# all equity / options logic for those two days.
CRYPTO_ENABLED          = os.getenv("CRYPTO_ENABLED", "true").lower() in ("1", "true", "yes")
# Force crypto to run on weekdays too (for testing). Set FORCE_CRYPTO=true in .env.
FORCE_CRYPTO            = os.getenv("FORCE_CRYPTO", "false").lower() in ("1", "true", "yes")
# Force equity/options to run on weekends too (for testing). Set FORCE_EQUITY=true in .env.
FORCE_EQUITY            = os.getenv("FORCE_EQUITY", "false").lower() in ("1", "true", "yes")

# Universe: Alpaca crypto pairs (slash format: "BTC/USD")
# Full Alpaca-supported list (stablecoins USDC/USDT/USDG excluded — no signal value)
CRYPTO_UNIVERSE: list = [
    p.strip() for p in os.getenv(
        "CRYPTO_UNIVERSE",
        # ── Majors ──────────────────────────────────────────────
        "BTC/USD,ETH/USD,"
        # ── Layer-1 Ecosystems ───────────────────────────────────
        "SOL/USD,ADA/USD,AVAX/USD,DOT/USD,LINK/USD,LTC/USD,BCH/USD,XTZ/USD,XRP/USD,POL/USD,"
        # ── DeFi Infrastructure ──────────────────────────────────
        "RENDER/USD,FIL/USD,GRT/USD,ARB/USD,LDO/USD,"
        # ── DeFi / DEX tokens ────────────────────────────────────
        "AAVE/USD,UNI/USD,SUSHI/USD,YFI/USD,CRV/USD,ONDO/USD,HYPE/USD,SKY/USD,"
        # ── Engagement ───────────────────────────────────────────
        "BAT/USD,"
        # ── Gold-backed ──────────────────────────────────────────
        "PAXG/USD,"
        # ── Community / Meme ─────────────────────────────────────
        "DOGE/USD,SHIB/USD,BONK/USD,PEPE/USD,WIF/USD,TRUMP/USD",
    ).split(",") if p.strip()
]

# Position sizing
CRYPTO_MAX_POSITIONS = int(os.getenv("CRYPTO_MAX_POSITIONS", "12"))    # max simultaneous crypto positions
CRYPTO_POSITION_PCT  = float(os.getenv("CRYPTO_POSITION_PCT",  "0.0"))   # legacy override; 0 = auto (BP / CRYPTO_MAX_POSITIONS)
CRYPTO_MIN_NOTIONAL  = float(os.getenv("CRYPTO_MIN_NOTIONAL",  "100.0")) # minimum order in USD

# Exit thresholds
CRYPTO_TP_PCT        = float(os.getenv("CRYPTO_TP_PCT",  "4.0"))         # take-profit % above entry
CRYPTO_SL_PCT        = float(os.getenv("CRYPTO_SL_PCT",  "2.5"))         # stop-loss % below entry

# Signal thresholds (RSI-based, 1h bars)
CRYPTO_RSI_BUY_MIN   = float(os.getenv("CRYPTO_RSI_BUY_MIN",  "42.0"))  # RSI must be above this to buy
CRYPTO_RSI_BUY_MAX   = float(os.getenv("CRYPTO_RSI_BUY_MAX",  "70.0"))  # RSI must be below this to buy
CRYPTO_RSI_SELL_MAX  = float(os.getenv("CRYPTO_RSI_SELL_MAX", "52.0"))  # RSI must be below this to close

# Scan interval during weekend (minutes)
CRYPTO_SCAN_INTERVAL_MIN = int(os.getenv("CRYPTO_SCAN_INTERVAL_MIN", "30"))

# ─────────────────────────────────────────────────────────────────
# Daily Limits
# ─────────────────────────────────────────────────────────────────
POSITION_CHECK_MIN       = 5
DAILY_LOSS_LIMIT_BULL_PCT = 1.0   # Halt if down >1% of start equity in bull regime
DAILY_LOSS_LIMIT_BEAR_PCT = 2.0   # Halt if down >2% of start equity in bear regime (wider room)
DAILY_PROFIT_TARGET       = 3500.0

# Entry time window — restrict NEW entries to the high-edge session. Exits and
# position management always run. Momentum edge is early; late-day and
# extended-hours entries churn on thin liquidity.
ENTRY_WINDOW_ENABLED         = os.getenv("ENTRY_WINDOW_ENABLED", "true").lower() in ("1", "true", "yes")
ENTRY_CUTOFF_HOUR            = float(os.getenv("ENTRY_CUTOFF_HOUR", "15.0"))  # no new entries at/after 15:00 ET
BLOCK_EXTENDED_HOURS_ENTRIES = os.getenv("BLOCK_EXTENDED_HOURS_ENTRIES", "true").lower() in ("1", "true", "yes")

# Quarterly Profit Target
USE_QUARTERLY_TARGET        = True
QUARTERLY_PROFIT_TARGET_PCT = 50.0   # Halt new entries once +50% equity this quarter

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Extended Hours Trading
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
EXTENDED_HOURS   = True
PREMARKET_START  = "07:00"
MARKET_OPEN      = "09:30"
MARKET_CLOSE     = "16:00"
AFTERHOURS_END   = "16:00"

# Set FORCE_SCAN=1 (env var) or pass --force CLI flag to bypass the
# market-hours gate when a high-confidence opportunity is spotted.
FORCE_SCAN = os.getenv("FORCE_SCAN", "false").lower() in ("1", "true", "yes")

# ─────────────────────────────────────────────────────────────────
# EOD (End-of-Day) Position Close
# Intraday strategies should never be held overnight — close by EOD_CLOSE_TIME
# ─────────────────────────────────────────────────────────────────
EOD_CLOSE_ENABLED    = True
EOD_CLOSE_ALL        = os.getenv("EOD_CLOSE_ALL", "false").lower() in ("1", "true", "yes")  # close ALL positions at EOD regardless of strategy
EOD_CLOSE_TIME       = os.getenv("EOD_CLOSE_TIME", "15:50")  # override via .env; default = 10 min before regular close
EOD_AFTERHOURS_LIMIT_BUFFER_PCT = float(os.getenv("EOD_AFTERHOURS_LIMIT_BUFFER_PCT", "1.0"))
INTRADAY_WINDOW_START = os.getenv("INTRADAY_WINDOW_START", "07:00")
INTRADAY_RESET_TIME    = os.getenv("INTRADAY_RESET_TIME", "11:00")
INTRADAY_MORNING_CUTOFF = os.getenv("INTRADAY_MORNING_CUTOFF", "10:55")  # Morning flatten window start
INTRADAY_MORNING_RESET  = os.getenv("INTRADAY_MORNING_RESET", "11:00")   # Morning flatten window end / Session 2 start
INTRADAY_FINAL_CUTOFF  = os.getenv("INTRADAY_FINAL_CUTOFF", "14:58")
INTRADAY_FINAL_RESET   = os.getenv("INTRADAY_FINAL_RESET", "15:03")  # Start Session 3 after flatten
INTRADAY_MOMENTUM_EXEMPTIONS = int(os.getenv("INTRADAY_MOMENTUM_EXEMPTIONS", "2"))
INTRADAY_MOMENTUM_MIN_GAIN_PCT = float(os.getenv("INTRADAY_MOMENTUM_MIN_GAIN_PCT", "1.0"))
INTRADAY_MOMENTUM_MIN_RVOL = float(os.getenv("INTRADAY_MOMENTUM_MIN_RVOL", "1.5"))
INTRADAY_MOMENTUM_MIN_5M_RETURN_PCT = float(os.getenv("INTRADAY_MOMENTUM_MIN_5M_RETURN_PCT", "0.5"))
# Force-close positions whose market value exceeds cash, so no margin carries overnight.
MARGIN_EOD_FORCE_CLOSE = os.getenv("MARGIN_EOD_FORCE_CLOSE", "true").lower() in ("1", "true", "yes")
EOD_CLOSE_STRATEGIES = {         # Strategy names that must be closed same day
    "FloatRotation",
    "GapBreakout",
    "ORB",
    "TrendlineBreakout",
    "VWAPReclaim",
    "PreMarketMomentum",
    "OpeningBellSurge",
    "PMHighBreakout",
    "EarlySqueeze",
}

# Regression trendline breakout: confirmed swing pivots + ATR/volume confirmation.
TRENDLINE_BREAKOUT = {
    # "regression" preserves the original strategy; "adaptive" uses the
    # replayable support/resistance engine in scripts.auto_trendline.
    "engine": os.getenv("TRENDLINE_BREAKOUT_ENGINE", "regression").lower(),
    "left_bars": int(os.getenv("TRENDLINE_BREAKOUT_LEFT_BARS", "5")),
    "right_bars": int(os.getenv("TRENDLINE_BREAKOUT_RIGHT_BARS", "5")),
    "pivot_count": int(os.getenv("TRENDLINE_BREAKOUT_PIVOT_COUNT", "3")),
    "atr_offset": float(os.getenv("TRENDLINE_BREAKOUT_ATR_OFFSET", "0.5")),
    "volume_multiplier": float(os.getenv("TRENDLINE_BREAKOUT_VOLUME_MULTIPLIER", "1.5")),
    "risk_reward_ratio": float(os.getenv("TRENDLINE_BREAKOUT_RISK_REWARD_RATIO", "2.0")),
    "primary_lookback": int(os.getenv("AUTO_TRENDLINE_PRIMARY_LOOKBACK", "35")),
    "bars_from_edge": int(os.getenv("AUTO_TRENDLINE_BARS_FROM_EDGE", "8")),
    "breakout_threshold": float(os.getenv("AUTO_TRENDLINE_BREAKOUT_THRESHOLD", "5.0")),
    "breakout_confirm_bars": int(os.getenv("AUTO_TRENDLINE_CONFIRM_BARS", "1")),
    "max_history_lines": int(os.getenv("AUTO_TRENDLINE_MAX_HISTORY_LINES", "3")),
}

# Stale order upgrade: unfilled orders older than this get re-submitted as market/limit
STALE_ORDER_MINUTES          = 360  # minutes before an unfilled order is considered stale
STALE_ORDER_MINUTES_INTRADAY =  30  # intraday strategies (ORB, surge, etc.) — cancel if unfilled after 30 min

# ─────────────────────────────────────────────────────────────────
# PDT Rules
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
PDT_ACCOUNT_MIN = float(os.getenv("PDT_ACCOUNT_MIN", "25000.0"))  # set to 0 in .env to treat account as always PDT-exempt
PDT_MAX_TRADES  = 3
PDT_OPTIONS_DAY_TRADE_RESERVE = int(os.getenv("PDT_OPTIONS_DAY_TRADE_RESERVE", "1"))  # keep at least N day trades free for stock exits

# ─────────────────────────────────────────────────────────────────
# Email Notifications
# ─────────────────────────────────────────────────────────────────
USE_EMAIL_NOTIFICATIONS = os.getenv("USE_EMAIL_NOTIFICATIONS", "false").lower() in ("1", "true", "yes")
EMAIL_SMTP_SERVER       = os.getenv("EMAIL_SMTP_SERVER", "smtp.gmail.com")
EMAIL_SMTP_PORT         = int(os.getenv("EMAIL_SMTP_PORT", "587"))
EMAIL_SMTP_USER         = os.getenv("EMAIL_SMTP_USER", "")
EMAIL_SMTP_PASSWORD     = os.getenv("EMAIL_SMTP_PASSWORD", "")
EMAIL_FROM_ADDRESS      = os.getenv("EMAIL_FROM_ADDRESS", "apextrader_bot@gmail.com")
EMAIL_TO_ADDRESSES      = [a.strip() for a in os.getenv("EMAIL_TO_ADDRESSES", "spolisetti.archive@gmail.com,alerts@apextrader.example.com").split(",") if a.strip()]
EMAIL_SUBJECT_PREFIX    = os.getenv("EMAIL_SUBJECT_PREFIX", "ApexTrader EOD Report")
EMAIL_SCAN_MIN_INTERVAL_SEC = int(os.getenv("EMAIL_SCAN_MIN_INTERVAL_SEC", "600"))
EMAIL_SCAN_SEND_ON_CHANGE   = os.getenv("EMAIL_SCAN_SEND_ON_CHANGE", "true").lower() in ("1", "true", "yes")

# Enterprise Risk Controls (environment-overridable)
MIN_BUYING_POWER_PCT  = float(os.getenv("MIN_BUYING_POWER_PCT", "5.0"))   # Reserve this % of equity as free buffer (never spend it)
MIN_POSITION_DOLLARS  = float(os.getenv("MIN_POSITION_DOLLARS", "5"))   # Minimum trade size in $ — skip if downsized below this
PDT_WARN_AT_REMAINING = int(os.getenv("PDT_WARN_AT_REMAINING", "1"))      # Warn log when PDT trades remaining falls to this level

# Small account smart sizing (for ~$1k buying power)
SMALL_ACCOUNT_EQUITY_THRESHOLD = float(os.getenv("SMALL_ACCOUNT_EQUITY_THRESHOLD", "5000"))
SMALL_ACCOUNT_MAX_POSITIONS     = int(os.getenv("SMALL_ACCOUNT_MAX_POSITIONS", "24"))

# Sniper Mode Controls — all overridable via .env
LONG_ONLY_MODE            = os.getenv("LONG_ONLY_MODE", "false").lower() in ("1", "true", "yes")  # false = long AND short allowed
MIN_SIGNAL_CONFIDENCE     = float(os.getenv("MIN_SIGNAL_CONFIDENCE",     "0.72"))
MIN_SHORT_CONFIDENCE_BEAR = float(os.getenv("MIN_SHORT_CONFIDENCE_BEAR", "0.65"))
SHORT_FAIL_COOLDOWN_MIN   = 5    # Re-try failed short symbols immediately
MAX_SIGNALS_PER_CYCLE     = int(os.getenv("MAX_SIGNALS_PER_CYCLE", "5"))

# Parallel Scanning
SCAN_WORKERS        = 8    # Threads scanning symbols concurrently (kept below Alpaca pool defaults)
SCAN_SYMBOL_TIMEOUT = 15   # Max seconds per symbol before it is skipped
SCAN_MAX_SYMBOLS    = 75   # Max symbols to scan per cycle (increased for better bear regime coverage)
BEAR_SHORT_TARGET_RESERVE = 30  # In bear regime, reserve more scan slots for short universe backups

# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Strategy Parameters
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
SWEEPEA = {
    "timeframe":        15,
    "pinbar_threshold": 80.0,
    "sweep_bars":       1,
    "min_sweep":        0.10,
    "min_price":        float(os.getenv("SWEEPEA_MIN_PRICE", "8.0")),
    "min_avg_volume":   float(os.getenv("SWEEPEA_MIN_AVG_VOLUME", "500000")),
    "min_avg_dollar_volume": float(os.getenv("SWEEPEA_MIN_AVG_DOLLAR_VOLUME", "5000000")),
    "liquidity_lookback_days": int(os.getenv("SWEEPEA_LIQUIDITY_LOOKBACK_DAYS", "20")),
    "use_ma":           True,
    "ma_fast":          20,
    "ma_slow":          50,
    "use_bb":           True,
    "bb_period":        20,
    "bb_std":           2.0,
}

# Sweepea Path A levers — dynamic confidence + trend gate (flag-gated, .env-toggleable)
SWEEPEA_DYNAMIC_CONFIDENCE    = os.getenv("SWEEPEA_DYNAMIC_CONFIDENCE", "true").lower() in ("1","true","yes")
SWEEPEA_REQUIRE_TREND         = os.getenv("SWEEPEA_REQUIRE_TREND", "true").lower() in ("1","true","yes")
SWEEPEA_TREND_EMA_RISING_BARS = int(os.getenv("SWEEPEA_TREND_EMA_RISING_BARS", "5"))

TECHNICAL = {
    "rsi_oversold":   30,
    "rsi_overbought": 70,
    "volume_surge":   2.0,   # was 1.5 — stronger volume required
}

MOMENTUM = {
    "min_momentum": 4.0,   # 4%+ move required (was 5 — too tight)
    "volume_surge": 2.5,   # 2.5x volume confirmation (was 3 — too tight)
}

MOMENTUM_CONTINUATION = {
    "min_rvol": float(os.getenv("MC_MIN_RVOL", "2.0")),          # relative volume floor
    "min_price_up_pct": float(os.getenv("MC_MIN_PRICE_UP_PCT", "1.5")),  # % above session open
    "break_lookback_min": int(os.getenv("MC_BREAK_LOOKBACK_MIN", "30")),   # session high lookback
    "enabled": os.getenv("MC_ENABLED", "true").lower() in ("1","true","yes"),
}

SENTIMENT_STRATEGY = {
    "enabled": True,
    "min_sentiment_score": 0.6,
    "min_sentiment_confidence": 0.55,
    "volume_surge": 2.0,
}

# ─────────────────────────────────────────────────────────────────
# Gap Breakout Strategy
# ─────────────────────────────────────────────────────────────────
GAP_BREAKOUT = {
    "min_gap_pct":       5.0,   # Minimum gap-up % from prior close
    "volume_multiplier": 2.5,   # Recent vol must be > X * session avg (raised from 1.5 — x1.5 was noise-level)
    "entry_window_min":  90,    # Only enter within first 90 min of open
}

# ─────────────────────────────────────────────────────────────────
# Opening Range Breakout (ORB) Strategy
# ─────────────────────────────────────────────────────────────────
ORB = {
    "range_minutes":       15,   # ORB formed in first 15 min (9:30-9:45)
    "entry_start_min":     15,   # Start looking for breakouts after ORB forms
    "entry_end_min":       90,   # Stop entering after 90 min into session
    "breakout_buffer_pct": 0.1,  # Fallback buffer when ATR is unavailable
    "breakout_buffer_atr": 0.15, # Adaptive breakout buffer as a fraction of ATR-14
    "volume_surge":        1.8,  # Baseline post-ORB volume confirmation
    "volume_surge_min":    1.5,  # Lower threshold for narrow opening ranges
    "volume_surge_max":    2.0,  # Higher threshold for wide opening ranges
    "range_vol_low_pct":   1.0,  # Narrow-range threshold as % of entry price
    "range_vol_high_pct":  3.0,  # Wide-range threshold as % of entry price
    "require_above_vwap":   True, # Breakout must have intraday VWAP support
    "time_stop_minutes":   45,   # Exit an ORB trade that remains flat
    "flat_max_gain_pct":    0.5,  # Flat means price has not exceeded this gain
}

# ─────────────────────────────────────────────────────────────────
# VWAP Reclaim Strategy
# ─────────────────────────────────────────────────────────────────
VWAP_RECLAIM = {
    "volume_surge": 2.0,   # Volume in last 3 bars vs session avg
    "rsi_max":      72,    # Don't enter if already overbought
}

# ─────────────────────────────────────────────────────────────────
# Float Rotation Strategy
# ─────────────────────────────────────────────────────────────────
FLOAT_ROTATION = {
    "max_float_shares":   15_000_000,  # Only stocks with float < 15M shares
    "volume_float_ratio": 0.25,        # Today's volume already > 25% of float
    "min_price_up_pct":   5.0,         # Price must be up >5% on the day
}

# ─────────────────────────────────────────────────────────────────
# Early Momentum / Opening Strategies
# ─────────────────────────────────────────────────────────────────
PRE_MARKET_MOMENTUM = {
    "min_gap_pct":       3.0,   # Gap from prior close must be >= 3%
    "pm_vol_pct_of_avg": 15.0,  # Pre-market volume must be >= 15% of avg daily vol
    "pm_trend_bars":     5,     # Last N pre-market bars must trend up
    "entry_window_end":  10.0,  # Stop firing after 10:00 AM ET (hour decimal)
}

OPENING_BELL_SURGE = {
    "surge_bars":      5,     # Number of first 1-min bars after open to measure
    "vol_multiplier":  4.0,   # First N bars total vol vs baseline (N * avg_1min)
    "min_price_up_pct": 2.0,  # Price must be up >= 2% from open after N bars
    "window_min":      15,    # Only valid for first 15 min after open
}

PM_HIGH_BREAKOUT = {
    "breakout_buffer_pct": 0.2,  # Must clear PM high by 0.2%
    "volume_surge":        1.5,  # Volume in last 3 bars vs session avg
    "entry_window_min":    60,   # Only valid for first 60 min after open
}

EARLY_SQUEEZE = {
    "max_float_shares":  20_000_000,  # Low-float stocks only
    "min_gap_pct":        3.0,         # Gap from prior close >= 3%
    "rvol_multiplier":    4.0,         # Projected full-day RVOL must exceed 4x
    "entry_window_min":  45,           # Only valid for first 45 min after open
    "rsi_max":           75,           # Not yet overbought
}

# ─────────────────────────────────────────────────────────────────
# Bear Breakdown Strategy (short-selling)
# Fires only in bear regime (SPY < 200SMA). Inverse of TrendBreaker.
# ─────────────────────────────────────────────────────────────────
BEAR_BREAKDOWN = {
    "volume_multiplier":  1.5,   # Volume today vs 20-day avg (raised from 1.2 — filters x1.3/x1.4 noise)
    "rsi_max":           65,    # Allow earlier distribution entries before full trend extension
    "rsi_min":           30,    # Raised from 20 — avoid shorting deeply oversold stocks (bounce risk)
    "above_sma_min_days": 1,    # Loosen freshness requirement in fast bear tapes
    "breakdown_buffer_pct": 0.30,  # Allow entry if within 0.30% above 10-day low
}

# ─────────────────────────────────────────────────────────────────
# Golden Ratio Scanner Guardrails
# ─────────────────────────────────────────────────────────────────
RVOL_MIN                 = 1.0         # Require relative volume ≥ 1.0x before entering (adaptive can reduce to 0.4–0.8)
MIN_STOCK_PRICE          = float(os.getenv("MIN_STOCK_PRICE", "0.5"))  # override via .env
ALPACA_MOVER_SCAN_INTERVAL_MIN = 10   # Re-poll Alpaca screener every 10 min (resets at market open)
MIN_DOLLAR_VOLUME        = 1_000_000   # Skip illiquid setups: price × day_vol < $1M
# Data-quality gate: block entries when RVOL@TIME cannot be computed from real
# prior-session intraday bars (Schwab lacks minute history for the name). The
# crude legacy fallback is unreliable, so no confirmation = no trade.
REQUIRE_PRIOR_SESSION_BARS = os.getenv("REQUIRE_PRIOR_SESSION_BARS", "true").lower() == "true"
MAX_GAP_CHASE_PCT        = float(os.getenv("MAX_GAP_CHASE_PCT",     "8.0"))   # Skip if already up >8% (reduced from 15%)
GAP_CHASE_CONSOL_BARS    = 5          # Number of 1-min bars to check for tight base

# ─────────────────────────────────────────────────────────────────
# Trade Ideas (TI) Momentum/HSF Stocks — Tighter Guardrails
# ─────────────────────────────────────────────────────────────────
# TI stocks run hard and fast; earlier entries are preferred to avoid chasing
# tail-end moves with poor risk/reward. These thresholds prevent late entries.
TI_MAX_GAP_CHASE_PCT     = float(os.getenv("TI_MAX_GAP_CHASE_PCT",   "7.0"))   # Only enter if <7% up from open (tightened from 10%)
TI_RVOL_MIN              = float(os.getenv("TI_RVOL_MIN",             "1.5"))   # Require 1.5x RVOL (raised from 1.3x)
TI_MIN_DOLLAR_VOLUME     = float(os.getenv("TI_MIN_DOLLAR_VOLUME",    "1500000"))  # $1.5M liquidity floor
TI_MAX_OVERNIGHT_GAP_PCT = 12.0       # Skip if >12% overnight/pre-market gap

# ─────────────────────────────────────────────────────────────────
# Midday Chop Filter (11:30 AM – 1:00 PM ET)
# ─────────────────────────────────────────────────────────────────
# Midday is the lowest-quality period for intraday entries (fading volume,
# choppy price action, algos pulling liquidity). Only A+ setups allowed.
MIDDAY_CHOP_START        = os.getenv("MIDDAY_CHOP_START", "11:30")   # ET time
MIDDAY_CHOP_END          = os.getenv("MIDDAY_CHOP_END",   "13:00")   # ET time
MIDDAY_MIN_CONFIDENCE    = float(os.getenv("MIDDAY_MIN_CONFIDENCE", "0.88"))   # require A+ setup midday

USE_MARKET_REGIME_FILTER = True       # SPY below 200-day MA → cut signals to 1
MARKET_REGIME_SIGNALS_CAP  = 5        # Max LONG entries per cycle in bear regime (swap-only); tries until one succeeds
BEAR_SHORT_SIGNALS_CAP     = 3        # Max SHORT entries per cycle in bear regime
ATR_STOP_MULTIPLIER      = 1.5        # Stop loss = entry − ATR × 1.5
ATR_TP_RATIO             = 2.0        # Take-profit at 2:1 R:R (risk × 2)
MAX_SHORT_FLOAT_PCT      = 20.0       # Never exceed this % of equity per squeeze ticker

# Bear short scan supplement — liquid large/mid caps with clean SMA structure that
# BearBreakdownStrategy and TechnicalStrategy can fire on during a bear regime.
# These stocks have stable 20/50 SMA patterns and meaningful distribution moves.
BEAR_SHORT_UNIVERSE = [
    "NVDA", "AMD", "TSLA", "META", "AMZN", "AAPL", "MSFT", "NFLX",
    "PLTR", "MSTR", "COIN", "SMCI", "SNOW", "CRM", "CRWD", "NET",
    "ARKK", "SOXS", "LABD",   # sector ETFs (can be shorted directly)
    "MARA", "WULF", "CLSK",   # crypto miners — high-beta bear breakdowns
    "IONQ", "RGTI", "QUBT",   # quantum/AI overhyped names
]
HIGH_SHORT_FLOAT_STOCKS  = {
    "AAP", "ABTS", "ACHC", "ACXP", "ADMA", "AESI",
    "AEVA", "AGQ", "AGX", "AI", "AIFF", "AIRS",
    "AISP", "ALBT", "ALMU", "AMC", "AMPG", "ANAB",
    "ANNA", "ANNX", "ANTX", "APGE", "APLD", "APP",
    "APPX", "ARCT", "ARMG", "ARTL", "ARWR", "ASAN",
    "ASPI", "ASST", "ASTI", "ASTS", "ATAI", "ATPC",
    "AVBP", "AVTX", "AVXL", "AXTI", "AZ", "AZN",
    "BABX", "BAIG", "BAK", "BATL", "BBNX", "BBW",
    "BCRX", "BEAM", "BETR", "BF", "BFLY", "BHVN",
    "BIAF", "BIRD", "BITU", "BKD", "BKKT", "BKSY",
    "BLSH", "BMEA", "BMNZ", "BNAI", "BNRG", "BOIL",
    "BOXL", "BTBD", "BTBT", "BTDR", "BTGO", "BTU",
    "BUR", "BWET", "BZUN", "CABA", "CAR", "CBIO",
    "CBUS", "CDIO", "CELC", "CGEM", "CGON", "CHAC",
    "CHPT", "CHRS", "CIFG", "CIFR", "CISS", "CNVS",
    "CNXC", "COIG", "CONI", "CONL", "CORZ", "CPB",
    "CRCA", "CRCG", "CRDF", "CRK", "CRSR", "CRVS",
    "CRWD", "CRWG", "CRWL", "CRWV", "CSIQ", "CTXR",
    "CV", "CVI", "CVV", "CYN", "DAMD", "DBGI",
    "DBI", "DBVT", "DERM", "DIN", "DNA",
    "DNTH", "DNUT", "DOCN", "DRVN", "DTCX", "DUOG",
    "DUOL", "DUST", "DVLT", "DWSN", "DXST", "DXYZ",
    "EAF", "EBS", "EDSA", "EEIQ", "ELVN", "ENLT",
    "EOSE", "ERAS", "ETHD", "ETHT", "ETR", "EUDA",
    "EVH", "EVMN", "EVTV", "EWTX", "EYE", "FATN",
    "FBGL", "FBIO", "FBYD", "FCHL", "FEED", "FFAI",
    "FGL", "FLNC", "FOSL", "FOUR", "FROG", "GDXD",
    "GDXU", "GEF", "GLND", "GLSI", "GLUE", "GLWG",
    "GNPX", "GOGO", "GPRE", "GRND", "GRPN", "HCTI",
    "HNRG", "HOOG", "HOOZ", "HPK", "HRTX", "HTCO",
    "HTZ", "HUBC", "HUMA", "HUT", "HYPD", "IBG",
    "IBRX", "IBTA", "ICU", "IDYA", "IEP", "IMAX",
    "IMTE", "INDI", "INDO", "IONZ", "IRE", "IREG",
    "ISSC", "IXHL", "JACK", "JBLU", "JDZG", "JNUG",
    "KALV", "KIDZ", "KLRS", "KOD", "KOLD", "KOPN",
    "KORU", "KPTI", "KRRO", "KRUS", "KSCP", "KULR",
    "KVYO", "LAR", "LASE", "LBGJ", "LCID", "LE",
    "LENZ", "LEU", "LGN", "LGVN", "LICN", "LMND",
    "LMRI", "LOVE", "LUD", "LUNR", "LVWR", "MARA",
    "MDCX", "MDGL", "MED", "MEOH", "METC", "METU",
    "MGTX", "MKDW", "MKT", "MLKN", "MNPR", "MNTS",
    "MRAL", "MRLN", "MRNO", "MSS", "MSTX", "MULL",
    "MUU", "MUX", "MVIS", "MVO", "NAMM", "NAUT",
    "NAVN", "NBIG", "NBIL", "NBIS", "NCI", "NDRA",
    "NEXT", "NFE", "NGNE", "NMAX", "NOAH", "NOTE",
    "NSRX", "NTLA", "NUGT", "NVTS", "OGEN", "OKLL",
    "OKLO", "OKLS", "OKTA", "OKUR", "OLPX", "ONCO",
    "ONDG", "ONDS", "ONEG", "OPTX", "ORGN", "ORGO",
    "ORIC", "ORIS", "OXM", "PALI", "PANW", "PAR",
    "PCRX", "PGEN", "PGY", "PHAT", "PHGE", "PL",
    "PLCE", "PLTZ", "POLA", "PONY", "PRME", "PROF",
    "PROP", "PSIX", "QBTZ", "QLYS", "QNCX", "QNRX",
    "QNTM", "QTTB", "QVCGA", "RBNE", "RCAT", "RCAX",
    "RCKT", "RDTL", "REED", "RENX", "REPL", "RETO",
    "RGTZ", "RILY", "RIME", "RIOX", "RKLX", "RKLZ",
    "RLYB", "RNAC", "ROMA", "RR", "RUM", "RVI",
    "RXT", "RZLT", "SAIL", "SATL", "SATS", "SBIT",
    "SCVL", "SER", "SGML", "SHMD", "SHNY", "SIGA",
    "SION", "SKIL", "SKIN", "SKLZ", "SLNH", "SLON",
    "SLS", "SMCX", "SMCZ", "SMST", "SMX", "SNBR",
    "SND", "SNSE", "SOC", "SOLT", "SOWG", "SOXS",
    "SPCE", "SPIR", "SPRC", "SPRY", "SQM", "SRFM",
    "SRPT", "STIM", "SUNE", "SWMR", "TASK", "TBCH",
    "TDUP", "TEAD", "TECX", "TENB", "TERN", "TMDE",
    "TNGX", "TONX", "TPET", "TRIP", "TRON", "TROX",
    "TSSI", "TTEC", "TURB", "TWST", "UAMY", "UGRO",
    "UNG", "UPB", "UPXI", "UUUG", "UWMC", "VCIC",
    "VCX", "VERI", "VIVO", "VNET", "VOR", "VRCA",
    "VSA", "VSTM", "VTAK", "VTIX", "VTS", "VWAV",
    "WATT", "WKHS", "WOLF", "WRAP", "WS", "WT",
    "WTI", "WULF", "WVE", "WYFI", "XRX", "XTIA",
    "XYF", "YANG", "YDDL", "YINN", "ZBIO", "ZNTL",
    "ZS", "ZSL",
}

# Remove any DELISTED_STOCKS entries that crept into HIGH_SHORT_FLOAT_STOCKS
HIGH_SHORT_FLOAT_STOCKS = {s for s in HIGH_SHORT_FLOAT_STOCKS if s not in DELISTED_STOCKS}

# Live HSF lookup — merges the static set above with tier-2 universe.json entries
# so newly TI-scraped tickers are recognised as HSF without restarting the bot.
_hsf_tier2_cache: dict = {"ts": 0.0, "symbols": frozenset()}
_HSF_CACHE_TTL = 300  # 5 minutes — re-read universe.json at most every 5 min

def is_high_short_float(symbol: str) -> bool:
    """Return True if symbol is in the static HSF set OR in the live tier-2 universe."""
    if symbol in HIGH_SHORT_FLOAT_STOCKS:
        return True
    import time as _time
    now = _time.monotonic()
    if now - _hsf_tier2_cache["ts"] > _HSF_CACHE_TTL:
        try:
            from engine.equity.universe import get_tier as _gt
            _hsf_tier2_cache["symbols"] = frozenset(_gt(2))
        except Exception:
            _hsf_tier2_cache["symbols"] = frozenset()
        _hsf_tier2_cache["ts"] = now
    return symbol in _hsf_tier2_cache["symbols"]

# OOM and cache management
OPTIONS_CHAIN_CACHE_MAX = int(os.getenv("OPTIONS_CHAIN_CACHE_MAX", "300"))  # max symbols in options chain cache

# Global memory warning threshold (in MB)
MEMORY_WARN_MB = int(os.getenv("MEMORY_WARN_MB", "1500"))
