"""
engine.utils.data
-----------------
External data integrations: Finnhub trending discovery, sentiment gate,
logging setup, and small env/format helpers.

All public functions here are re-exported from engine.utils for backward compat.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import List, Tuple

# ── Finnhub SDK availability ──────────────────────────────────────────────────
try:
    import finnhub as _finnhub_mod
    FINNHUB_SDK_AVAILABLE = True
except ImportError:
    FINNHUB_SDK_AVAILABLE = False


# ── Env / format helpers ──────────────────────────────────────────────────────

def bool_env(name: str, default: str = "false") -> bool:
    """Parse a boolean environment variable. Truthy: '1', 'true', 'yes'."""
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


def get_env(name: str, default: str = "") -> str:
    """Return stripped env var value or *default*."""
    return os.getenv(name, default).strip()


def format_currency(value: float) -> str:
    return f"${value:,.2f}"


# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging() -> logging.Logger:
    """Configure root logger with console + rotating file handlers.

    Level is driven by APEXTRADER_LOG_LEVEL env var (default INFO).
    Safe to call multiple times — duplicate handlers are removed first.
    """
    fmt       = "%(asctime)s [%(levelname)s] %(message)s"
    formatter = logging.Formatter(fmt)
    root      = logging.getLogger()

    level_name = os.getenv("APEXTRADER_LOG_LEVEL", "INFO").upper()
    root.setLevel(getattr(logging, level_name, logging.INFO))

    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    file_h = logging.FileHandler(
        filename=f"apextrader.log.{date.today().isoformat()}",
        mode="a",
        encoding="utf-8",
    )
    file_h.setFormatter(formatter)

    root.addHandler(console)
    root.addHandler(file_h)

    for noisy in ("urllib3", "selenium", "webdriver_manager", "WDM"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("ApexTrader")


# ── Finnhub client ────────────────────────────────────────────────────────────

def get_finnhub_client():
    from engine.config import FINNHUB_API_KEY
    if not FINNHUB_API_KEY:
        raise ValueError("FINNHUB_API_KEY not configured")
    if not FINNHUB_SDK_AVAILABLE:
        raise ImportError("finnhub-python is not installed")
    return _finnhub_mod.Client(api_key=FINNHUB_API_KEY)


# ── Trending discovery ────────────────────────────────────────────────────────

def get_trending_tickers(max_results: int = 20) -> List[str]:
    """Return trending tickers from Seeking Alpha (primary) or yfinance.

    This is the fallback used by equity discovery when live trending is enabled.
    The function never raises and returns at most *max_results* symbols.
    """
    try:
        tickers = get_finnhub_trending_tickers()
        if tickers:
            return tickers[:max_results]
    except Exception:
        pass
    return []


def filter_trending_momentum(
    trending_tickers: list, min_momentum_pct: float = 3.0
) -> list:
    """Filter a list of ticker dicts to those with >= min_momentum_pct 5-day move."""
    from engine.utils.bars import get_bars
    filtered = []
    for symbol in trending_tickers:
        try:
            bars = get_bars(symbol, "5d", "1d")
            if bars.empty or len(bars) < 2:
                continue
            cur = float(bars["close"].iloc[-1])
            old = float(bars["close"].iloc[0])
            if old <= 0:
                continue
            mom = ((cur - old) / old) * 100
            if mom >= min_momentum_pct:
                filtered.append({"symbol": symbol, "momentum_pct": mom, "current_price": cur})
        except Exception:
            continue
    filtered.sort(key=lambda x: x["momentum_pct"], reverse=True)
    return filtered


def get_finnhub_trending_tickers() -> List[str]:
    """Return trending tickers from Seeking Alpha. SA-only — no fallback.

    Uses SA `/news/v2/list-trending`. Returns [] when SA is unreachable or
    the circuit breaker is open.  The function never raises.
    """
    log = logging.getLogger("ApexTrader")
    try:
        from engine.data.seeking_alpha import get_sa_trending_tickers
        tickers = get_sa_trending_tickers(size=40)
        if tickers:
            log.debug(f"[SA] Trending via Seeking Alpha: {len(tickers)} tickers")
        return tickers
    except Exception as e:
        log.debug(f"[SA] get_sa_trending_tickers failed: {e}")
        return []


def check_sentiment_gate(ticker: str) -> Tuple[bool, float]:
    """Return (passes_gate, bullish_pct) for *ticker*. SA-only — no fallback.

    Resolution order:
      1. Seeking Alpha quant rating  — most authoritative (analyst consensus).
      2. Seeking Alpha news headlines — when SA key present but rating missing.
      3. Allow-by-default (True, 0.5) — SA unreachable / circuit breaker open,
         so no trade is blocked by a data outage.
    """
    log = logging.getLogger("ApexTrader")

    # ── 1 & 2: Seeking Alpha ─────────────────────────────────────────────────
    try:
        from engine.data.seeking_alpha import sa_sentiment_gate
        passes, bpct = sa_sentiment_gate(ticker)
        log.debug(f"[SA] check_sentiment_gate({ticker}): passes={passes} pct={bpct:.2f}")
        return passes, bpct
    except Exception as e:
        log.debug(f"[SA] sentiment gate failed for {ticker}: {e}")

    # ── 3: allow by default (SA unavailable) ─────────────────────────────────
    return True, 0.5


def get_sa_market_movers() -> dict:
    """Return SA v2 day-watch market movers.

    Keys: top_gainers, top_losers, most_active, sp500_gainers, sp500_losers,
          cap400_gainers, cap400_losers  (each a List[str] of tickers).
    Returns {} on failure.
    """
    try:
        from engine.data.seeking_alpha import get_sa_day_watch
        return get_sa_day_watch()
    except Exception:
        return {}


def get_sa_factor_grades(ticker: str) -> Optional[dict]:
    """Return SA v2 factor grades for *ticker*.

    Keys: momentum_category, growth_category, eps_revisions_category,
          profitability_category, value_category  (scale ~1–12, higher = stronger).
    Returns None on failure.
    """
    try:
        from engine.data.seeking_alpha import get_sa_metrics_grades
        return get_sa_metrics_grades(ticker)
    except Exception:
        return None


def get_sa_market_outlook() -> dict:
    """Return SA v2 market outlook parsed sentiment.

    Keys: bullish_pct, bearish_pct, sentiment ("bullish"|"bearish"|"neutral"), titles.
    Returns {} on failure.
    """
    try:
        from engine.data.seeking_alpha import get_sa_market_outlook as _impl
        return _impl()
    except Exception:
        return {}
