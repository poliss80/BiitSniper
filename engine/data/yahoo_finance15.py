"""
engine.data.yahoo_finance15
---------------------------
Yahoo Finance15 API (via RapidAPI / SteadyAPI) client.

Provides:
  get_yh_day_gainers(count)   -> List[str]   (top day-gainer tickers)
  get_yh_day_losers(count)    -> List[str]   (top day-loser tickers)
  get_yh_most_active(count)   -> List[str]   (most active by volume)
  get_yh_news_tickers(count)  -> List[str]   (tickers extracted from latest news)

All results cached to minimise RapidAPI quota:
  Screener (gainers/losers/active) : 15-minute cache
  News tickers                     : 10-minute cache

Circuit breaker: trips after 3 consecutive network failures, resets after 10 min.
Enabled only when YAHOO_FINANCE15_API_KEY is set in the environment.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("ApexTrader")

# ── Config ────────────────────────────────────────────────────────────────────
_API_KEY  = os.getenv("YAHOO_FINANCE15_API_KEY", "")
_HOST     = "yahoo-finance15.p.rapidapi.com"
_BASE_URL = f"https://{_HOST}/api/v1/markets"
_NEWS_URL = f"https://{_HOST}/api/v2/markets/news"

_SCREENER_TTL = 900   # 15 min
_NEWS_TTL     = 600   # 10 min

# ── Cache ─────────────────────────────────────────────────────────────────────
_screener_cache: Dict[str, Tuple[float, List[str]]] = {}  # list_name -> (ts, tickers)
_news_cache:     Tuple[float, List[str]]             = (0.0, [])

# ── Circuit breaker ───────────────────────────────────────────────────────────
_cb_failures:   int   = 0
_cb_open_until: float = 0.0
_CB_THRESHOLD   = 3
_CB_RESET_S     = 600


def _cb_is_open() -> bool:
    global _cb_open_until
    if _cb_open_until and time.monotonic() < _cb_open_until:
        return True
    if _cb_open_until:
        _cb_open_until = 0.0
        log.info("[YH15] Circuit breaker reset — retrying Yahoo Finance15 API")
    return False


def _cb_record_failure(exc: Exception) -> None:
    global _cb_failures, _cb_open_until
    err = str(exc).lower()
    if any(k in err for k in ("timeout", "timed out", "connectionerror", "ssl", "connection")):
        _cb_failures += 1
        if _cb_failures >= _CB_THRESHOLD:
            _cb_open_until = time.monotonic() + _CB_RESET_S
            _cb_failures = 0
            log.warning(f"[YH15] Circuit breaker tripped — pausing for {_CB_RESET_S}s")
    else:
        _cb_failures = 0


def _cb_record_success() -> None:
    global _cb_failures
    _cb_failures = 0


def _is_enabled() -> bool:
    return bool(_API_KEY)


def _headers() -> dict:
    return {"x-rapidapi-host": _HOST, "x-rapidapi-key": _API_KEY}


def _fetch_screener(list_name: str, count: int = 25) -> List[str]:
    """Fetch tickers from a named screener list (day_gainers, day_losers, most_actives)."""
    if not _is_enabled() or _cb_is_open():
        return []

    now = time.monotonic()
    cached = _screener_cache.get(list_name)
    if cached and (now - cached[0]) < _SCREENER_TTL:
        return cached[1]

    try:
        import requests
        r = requests.get(
            f"{_BASE_URL}/screener",
            params={"list": list_name, "start": 0, "count": count},
            headers=_headers(),
            timeout=10,
        )
        r.raise_for_status()
        body = r.json().get("body", [])
        tickers = []
        for item in body:
            sym = item.get("symbol", "")
            # Skip ETFs/funds (quoteType != EQUITY) and symbols with dots (preferred shares)
            if item.get("quoteType") == "EQUITY" and sym and "." not in sym:
                tickers.append(sym.upper())
        _screener_cache[list_name] = (now, tickers)
        _cb_record_success()
        log.debug(f"[YH15] screener={list_name} → {len(tickers)} tickers")
        return tickers
    except Exception as e:
        _cb_record_failure(e)
        log.debug(f"[YH15] screener {list_name} error: {e}")
        return []


def get_yh_day_gainers(count: int = 25) -> List[str]:
    """Return top day-gainer ticker symbols (EQUITY only, no ETFs)."""
    return _fetch_screener("day_gainers", count)


def get_yh_day_losers(count: int = 25) -> List[str]:
    """Return top day-loser ticker symbols (EQUITY only)."""
    return _fetch_screener("day_losers", count)


def get_yh_most_active(count: int = 25) -> List[str]:
    """Return most active tickers by volume (EQUITY only)."""
    return _fetch_screener("most_actives", count)


def get_yh_news_tickers(count: int = 30) -> List[str]:
    """Extract unique tickers mentioned in the latest Yahoo Finance15 news articles.

    Each news item contains a `tickers` array with $ prefixed equity symbols
    (e.g. "$AAPL") and # prefixed ETF symbols. Only $ symbols are extracted.
    """
    global _news_cache

    if not _is_enabled() or _cb_is_open():
        return []

    now = time.monotonic()
    if now - _news_cache[0] < _NEWS_TTL:
        return _news_cache[1]

    try:
        import requests
        r = requests.get(
            _NEWS_URL,
            params={"count": count},
            headers=_headers(),
            timeout=10,
        )
        r.raise_for_status()
        body = r.json().get("body", [])
        seen = set()
        tickers = []
        for article in body:
            for tag in article.get("tickers", []):
                if tag.startswith("$"):
                    sym = tag[1:].upper()
                    if sym not in seen and sym.isalpha() and len(sym) <= 5:
                        seen.add(sym)
                        tickers.append(sym)

        _news_cache = (now, tickers)
        _cb_record_success()
        log.debug(f"[YH15] news → {len(tickers)} unique tickers")
        return tickers
    except Exception as e:
        _cb_record_failure(e)
        log.debug(f"[YH15] news tickers error: {e}")
        return []
