"""
engine.data.seeking_alpha
-------------------------
Seeking Alpha Finance API (via RapidAPI) client.

Provides:
  get_sa_quant_rating(ticker)       → float | None   (1=StrongSell … 5=StrongBuy)
  get_sa_news_sentiment(ticker)     → (bullish_pct, n_articles)
  get_sa_trending_tickers(size)     → List[str]

  ── New (seeking-alpha-api.p.rapidapi.com) ───────────────────────────────────
  get_sa_day_watch()                → Dict  (top_gainers/losers/most_active/sp500_gainers/losers)
  get_sa_metrics_grades(ticker)     → Dict | None  (momentum/growth/eps_revisions/profitability/value)
  get_sa_leading_story()            → List[str]   (tickers extracted from headline stories)

All results are cached to minimise RapidAPI quota usage:
  • Ratings     : 4-hour cache  (changes infrequently)
  • News        : 15-minute cache
  • Trending    : 10-minute cache
  • Day-watch   : 30-minute cache
  • Metrics grades : 4-hour cache
  • Leading story  : 10-minute cache

Circuit breaker: after 3 consecutive timeouts/connection errors the SA host is
marked unavailable for 10 minutes. This prevents every scan-cycle ticker from
burning 4 seconds of timeout while SA is unreachable, allowing yfinance fallback
to kick in immediately.

When SEEKING_ALPHA_API_KEY is absent or a call fails the function returns None
(ratings) or an empty/neutral result — callers must handle the fallback.
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("ApexTrader")

# ── Cache stores ──────────────────────────────────────────────────────────────
_rating_cache:   Dict[str, Tuple[float, float]]              = {}  # ticker → (ts, rating)
_news_cache:     Dict[str, Tuple[float, float, int]]         = {}  # ticker → (ts, bullish_pct, n)
_trending_cache: Tuple[float, List[str]]                     = (0.0, [])

_RATING_TTL   = 4 * 3600   # 4 h
_NEWS_TTL     = 900        # 15 min
_TRENDING_TTL = 600        # 10 min

# ── Cache stores (SA v2 — seeking-alpha-api.p.rapidapi.com) ──────────────────
_day_watch_cache:     Tuple[float, dict]              = (0.0, {})
_metrics_grades_cache: Dict[str, Tuple[float, dict]]  = {}  # ticker → (ts, grades)
_leading_story_cache: Tuple[float, List[str]]         = (0.0, [])

_DAY_WATCH_TTL     = 1800   # 30 min
_METRICS_TTL       = 4 * 3600  # 4 h
_LEADING_STORY_TTL = 600    # 10 min

# ── Circuit breaker ───────────────────────────────────────────────────────────
# Trips after _CB_THRESHOLD consecutive network failures; resets after _CB_RESET_S.
_cb_failures:    int   = 0
_cb_open_until:  float = 0.0          # monotonic timestamp; 0 = closed (healthy)
_CB_THRESHOLD    = 3
_CB_RESET_S      = 600                # 10 minutes


def _cb_is_open() -> bool:
    """Return True when the circuit breaker is tripped (SA unreachable)."""
    global _cb_open_until
    if _cb_open_until and time.monotonic() < _cb_open_until:
        return True
    if _cb_open_until:
        # Reset after cooldown
        _cb_open_until = 0.0
        log.info("[SA] Circuit breaker reset — retrying Seeking Alpha API")
    return False


def _cb_record_failure(exc: Exception) -> None:
    """Record a network-level failure; trip the breaker if threshold reached."""
    global _cb_failures, _cb_open_until
    # Only trip on network/timeout errors, not 4xx HTTP errors
    err = str(exc).lower()
    if any(k in err for k in ("timeout", "timed out", "connectionerror", "ssl", "connection")):
        _cb_failures += 1
        if _cb_failures >= _CB_THRESHOLD:
            _cb_open_until = time.monotonic() + _CB_RESET_S
            _cb_failures   = 0
            log.warning(
                f"[SA] Circuit breaker OPEN — SA API unreachable "
                f"({_CB_THRESHOLD} consecutive failures). "
                f"Falling back to yfinance for {_CB_RESET_S // 60} min."
            )
    else:
        # HTTP errors (4xx/5xx) reset the consecutive-failure counter
        _cb_failures = 0


def _cb_record_success() -> None:
    global _cb_failures
    _cb_failures = 0


# ── SA rating scale ───────────────────────────────────────────────────────────
# SA quant ratings use a 1–5 scale:
#   5.0 = Strong Buy   4.0 = Buy   3.0 = Hold   2.0 = Sell   1.0 = Strong Sell
# We normalise to [0, 1] bullish_pct: (rating - 1) / 4
#   → 5 = 1.00, 4 = 0.75, 3 = 0.50, 2 = 0.25, 1 = 0.00

_BULLISH_KW = frozenset({
    "upgrade", "beat", "beats", "surge", "surges", "raised", "raises", "record",
    "strong", "buy", "outperform", "growth", "bullish", "upside", "overweight",
    "positive", "higher", "gains", "rally", "momentum", "exceeds",
})
_BEARISH_KW = frozenset({
    "downgrade", "miss", "misses", "decline", "cut", "weak", "sell",
    "underperform", "loss", "warning", "recall", "bearish", "downside",
    "underweight", "negative", "lower", "drops", "slump", "layoffs", "worse",
})

# ── Request timeout ───────────────────────────────────────────────────────────
# (connect_timeout, read_timeout) — short connect so TLS hangs fail fast.
_TIMEOUT = (4, 6)


def _headers() -> Optional[dict]:
    """Return RapidAPI headers, or None when key is absent."""
    from engine.config import SEEKING_ALPHA_API_KEY, SEEKING_ALPHA_HOST
    if not SEEKING_ALPHA_API_KEY:
        return None
    return {
        "x-rapidapi-key":  SEEKING_ALPHA_API_KEY,
        "x-rapidapi-host": SEEKING_ALPHA_HOST,
    }


def _base() -> str:
    from engine.config import SEEKING_ALPHA_HOST
    # strip any accidental whitespace; default host is seeking-alpha.p.rapidapi.com
    return f"https://{SEEKING_ALPHA_HOST.strip()}"


# ── Public API ────────────────────────────────────────────────────────────────

def get_sa_quant_rating(ticker: str) -> Optional[float]:
    """Return SA quant rating (1–5) for *ticker*, or None on failure.

    Uses `/v2/symbols/get-ratings` endpoint.
    Rating meaning: 5=StrongBuy 4=Buy 3=Hold 2=Sell 1=StrongSell
    """
    hdrs = _headers()
    if hdrs is None:
        return None
    if _cb_is_open():
        return None

    sym = ticker.upper()
    now = time.monotonic()
    cached = _rating_cache.get(sym)
    if cached and (now - cached[0]) < _RATING_TTL:
        return cached[1]

    try:
        import requests as _req
        resp = _req.get(
            f"{_base()}/v2/symbols/get-ratings",
            params={"symbols": sym},
            headers=hdrs,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        # Response: {"data": [{"id": "AAPL", "attributes": {"quantRating": 3.52, ...}}]}
        items = data.get("data", [])
        if not items:
            _cb_record_success()
            return None
        attrs = items[0].get("attributes", {})
        rating = attrs.get("quantRating")
        if rating is None:
            _cb_record_success()
            return None
        rating = float(rating)
        _rating_cache[sym] = (now, rating)
        _cb_record_success()
        log.debug(f"[SA] {sym} quant_rating={rating:.2f}")
        return rating
    except Exception as e:
        _cb_record_failure(e)
        log.debug(f"[SA] get_sa_quant_rating({sym}) failed: {e}")
        return None


def get_sa_news_sentiment(ticker: str, size: int = 20) -> Tuple[float, int]:
    """Return (bullish_pct, n_articles) from SA news headlines for *ticker*.

    Falls back to (0.5, 0) on any error — neutral, no block.
    Uses `/news/v2/list` endpoint.
    """
    hdrs = _headers()
    if hdrs is None:
        return 0.5, 0
    if _cb_is_open():
        return 0.5, 0

    sym  = ticker.upper()
    slug = sym.lower()   # SA uses lowercase slugs
    now  = time.monotonic()
    cached = _news_cache.get(sym)
    if cached and (now - cached[0]) < _NEWS_TTL:
        return cached[1], cached[2]

    try:
        import requests as _req
        resp = _req.get(
            f"{_base()}/news/v2/list",
            params={"id": slug, "size": size, "until": 0},
            headers=hdrs,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        articles = resp.json().get("data", [])
        if not articles:
            _news_cache[sym] = (now, 0.5, 0)
            _cb_record_success()
            return 0.5, 0

        bullish = bearish = 0
        for art in articles:
            headline = art.get("attributes", {}).get("title", "").lower()
            if any(w in headline for w in _BULLISH_KW):
                bullish += 1
            elif any(w in headline for w in _BEARISH_KW):
                bearish += 1

        total     = bullish + bearish
        bpct      = bullish / max(total, 1) if total else 0.5
        _news_cache[sym] = (now, bpct, total)
        _cb_record_success()
        log.debug(f"[SA] {sym} news: bullish={bullish} bearish={bearish} pct={bpct:.2f}")
        return bpct, total
    except Exception as e:
        _cb_record_failure(e)
        log.debug(f"[SA] get_sa_news_sentiment({sym}) failed: {e}")
        return 0.5, 0


def get_sa_trending_tickers(size: int = 40) -> List[str]:
    """Return a deduplicated list of tickers from SA trending news.

    Uses `/news/v2/list-trending` endpoint.
    Falls back to [] on any failure.
    """
    hdrs = _headers()
    if hdrs is None:
        return []
    if _cb_is_open():
        return []

    now = time.monotonic()
    global _trending_cache
    if (now - _trending_cache[0]) < _TRENDING_TTL:
        return list(_trending_cache[1])

    try:
        import requests as _req
        resp = _req.get(
            f"{_base()}/news/v2/list-trending",
            params={"size": size, "since": 0},
            headers=hdrs,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        articles = resp.json().get("data", [])
        tickers: List[str] = []
        for art in articles:
            # primary: tickers field in attributes
            attrs = art.get("attributes", {})
            for t in attrs.get("tickers", []):
                sym = str(t.get("symbol") or t).upper().strip()
                if sym and sym.isalpha() and 1 <= len(sym) <= 5:
                    tickers.append(sym)
            # secondary: parse from related field if present
            for s in str(attrs.get("related", "")).split(","):
                s = s.strip().upper()
                if s and s.isalpha() and 1 <= len(s) <= 5:
                    tickers.append(s)

        # deduplicate preserving order
        seen: set = set()
        unique = [t for t in tickers if not (t in seen or seen.add(t))]
        _trending_cache = (now, unique)
        _cb_record_success()
        log.debug(f"[SA] Trending tickers ({len(unique)}): {unique[:10]}")
        return unique
    except Exception as e:
        _cb_record_failure(e)
        log.debug(f"[SA] get_sa_trending_tickers failed: {e}")
        return []


def sa_sentiment_gate(ticker: str) -> Tuple[bool, float]:
    """High-level sentiment gate using SA quant ratings then SA news as fallback.

    Returns (passes_gate, bullish_pct).
    - Primary  : SA quant rating (most authoritative — analyst consensus)
    - Secondary: SA news headline sentiment
    - Always returns (True, 0.5) on complete failure so a key outage never
      blocks all trades.
    """
    from engine.config import SENTIMENT_BULLISH_THRESHOLD

    # 1. Try quant rating
    rating = get_sa_quant_rating(ticker)
    if rating is not None:
        bullish_pct = (rating - 1.0) / 4.0   # normalise [1,5] → [0,1]
        passes = bullish_pct >= SENTIMENT_BULLISH_THRESHOLD
        log.debug(f"[SA] {ticker} quant_gate: rating={rating:.2f} bullish_pct={bullish_pct:.2f} passes={passes}")
        return passes, bullish_pct

    # 2. Fall back to SA news headlines
    bpct, n = get_sa_news_sentiment(ticker)
    if n > 0:
        passes = bpct >= SENTIMENT_BULLISH_THRESHOLD
        log.debug(f"[SA] {ticker} news_gate: bullish_pct={bpct:.2f} n={n} passes={passes}")
        return passes, bpct

    # 3. No SA data available — allow by default
    return True, 0.5


# ══════════════════════════════════════════════════════════════════════════════
# SA v2 API  (seeking-alpha-api.p.rapidapi.com)
# Same RapidAPI key — different provider / endpoint set.
# ══════════════════════════════════════════════════════════════════════════════

_SA2_HOST = "seeking-alpha-api.p.rapidapi.com"

# Set True the first time we receive a 403 (not subscribed) — suppresses all
# subsequent v2 calls for the remainder of the process lifetime.
_sa2_unsubscribed: bool = False


def _base2() -> str:
    return f"https://{_SA2_HOST}"


def _headers2() -> Optional[dict]:
    """Return RapidAPI headers for the v2 host, or None when key is absent or not subscribed."""
    global _sa2_unsubscribed
    if _sa2_unsubscribed:
        return None
    from engine.config import SEEKING_ALPHA_API_KEY
    if not SEEKING_ALPHA_API_KEY:
        return None
    return {
        "x-rapidapi-key":  SEEKING_ALPHA_API_KEY,
        "x-rapidapi-host": _SA2_HOST,
    }


def _sa2_handle_response(resp) -> bool:
    """Raise for status; on 403 mark provider as unsubscribed and return False.
    Returns True when response is usable."""
    global _sa2_unsubscribed
    if resp.status_code == 403:
        _sa2_unsubscribed = True
        log.warning(
            "[SA2] 403 Forbidden — not subscribed to seeking-alpha-api.p.rapidapi.com. "
            "SA v2 endpoints (day-watch, metrics-grades, leading-story) disabled. "
            "Subscribe at https://rapidapi.com/belchiorarkad-FqvHs2EDOtP/api/seeking-alpha-api"
        )
        return False
    resp.raise_for_status()
    return True


# ── get_sa_day_watch ──────────────────────────────────────────────────────────

def get_sa_day_watch() -> dict:
    """Return today's market-mover lists from SA /day-watch.

    Returns a dict with keys:
      top_gainers, top_losers, most_active,
      sp500_gainers, sp500_losers, cap400_gainers, cap400_losers
    Each value is a List[str] of ticker symbols.
    Falls back to an empty dict on any failure.
    """
    hdrs = _headers2()
    if hdrs is None:
        return {}
    if _cb_is_open():
        return {}

    global _day_watch_cache
    now = time.monotonic()
    if (now - _day_watch_cache[0]) < _DAY_WATCH_TTL and _day_watch_cache[1]:
        return dict(_day_watch_cache[1])

    try:
        import requests as _req
        resp = _req.get(
            f"{_base2()}/day-watch",
            headers=hdrs,
            timeout=_TIMEOUT,
        )
        if not _sa2_handle_response(resp):
            return {}
        attrs = resp.json().get("day_watch", {}).get("attributes", {})

        def _extract(key: str) -> List[str]:
            return [
                str(item.get("slug", "")).upper().strip()
                for item in attrs.get(key, [])
                if item.get("slug") and str(item["slug"]).isalpha()
            ]

        result = {
            "top_gainers":    _extract("top_gainers"),
            "top_losers":     _extract("top_losers"),
            "most_active":    _extract("most_active"),
            "sp500_gainers":  _extract("sp500_gainers"),
            "sp500_losers":   _extract("sp500_losers"),
            "cap400_gainers": _extract("cap400_gainers"),
            "cap400_losers":  _extract("cap400_losers"),
        }
        _day_watch_cache = (now, result)
        _cb_record_success()
        total = sum(len(v) for v in result.values())
        log.debug(f"[SA2] day_watch: {total} tickers across {len(result)} categories")
        return result
    except Exception as e:
        _cb_record_failure(e)
        log.debug(f"[SA2] get_sa_day_watch failed: {e}")
        return {}


# ── get_sa_metrics_grades ─────────────────────────────────────────────────────

def get_sa_metrics_grades(ticker: str) -> Optional[dict]:
    """Return SA factor grades for *ticker* from /metrics-grades.

    Returns a dict with integer keys:
      momentum_category, growth_category, eps_revisions_category,
      profitability_category, value_category
    Higher values indicate stronger factor strength (scale ~1–12).
    Returns None on failure.
    """
    hdrs = _headers2()
    if hdrs is None:
        return None
    if _cb_is_open():
        return None

    sym = ticker.upper()
    slug = sym.lower()
    now = time.monotonic()
    cached = _metrics_grades_cache.get(sym)
    if cached and (now - cached[0]) < _METRICS_TTL:
        return dict(cached[1])

    try:
        import requests as _req
        resp = _req.get(
            f"{_base2()}/metrics-grades",
            params={"slugs": slug},
            headers=hdrs,
            timeout=_TIMEOUT,
        )
        if not _sa2_handle_response(resp):
            return None
        items = resp.json().get("metrics_grades", [])
        # Find the matching slug (API may return multiple)
        for item in items:
            if str(item.get("slug", "")).upper() == sym:
                grades = {
                    "momentum_category":      int(item.get("momentum_category", 0) or 0),
                    "growth_category":        int(item.get("growth_category", 0) or 0),
                    "eps_revisions_category": int(item.get("eps_revisions_category", 0) or 0),
                    "profitability_category": int(item.get("profitability_category", 0) or 0),
                    "value_category":         int(item.get("value_category", 0) or 0),
                }
                _metrics_grades_cache[sym] = (now, grades)
                _cb_record_success()
                log.debug(f"[SA2] {sym} grades: {grades}")
                return grades
        # Slug not found in response
        _cb_record_success()
        return None
    except Exception as e:
        _cb_record_failure(e)
        log.debug(f"[SA2] get_sa_metrics_grades({sym}) failed: {e}")
        return None


# ── get_sa_leading_story ──────────────────────────────────────────────────────

def get_sa_leading_story() -> List[str]:
    """Return tickers extracted from SA /leading-story market headlines.

    Parses ticker slugs from headline URLs and headline text.
    Falls back to [] on any failure.
    """
    hdrs = _headers2()
    if hdrs is None:
        return []
    if _cb_is_open():
        return []

    global _leading_story_cache
    now = time.monotonic()
    if (now - _leading_story_cache[0]) < _LEADING_STORY_TTL and _leading_story_cache[1]:
        return list(_leading_story_cache[1])

    try:
        import re
        import requests as _req
        resp = _req.get(
            f"{_base2()}/leading-story",
            headers=hdrs,
            timeout=_TIMEOUT,
        )
        if not _sa2_handle_response(resp):
            return []
        stories = resp.json().get("leading_news_story", [])

        # Common English words that look like tickers but aren't
        _STOPWORDS = {
            "THE", "AND", "FOR", "WITH", "FROM", "THIS", "THAT", "WHAT", "HAVE",
            "WILL", "BULL", "BEAR", "HIKE", "JUNE", "JULY", "OPEC", "RATE", "RATE",
            "STOCK", "NEWS", "WALL", "BEAT", "MISS", "RISE", "FALL", "DROP", "JUMP",
            "SELL", "HOLD", "WEEK", "YEAR", "HIGH", "NEXT", "OVER", "INTO", "DEAL",
            "CUTS", "BANK", "GOLD", "DATA", "CALL", "PUTS", "MARK", "BEAT", "MISS",
            "SAYS", "SEES", "OPEN", "HALF", "DEBT", "CASH", "PLAN", "SAYS", "GETS",
            "HITS", "TOPS", "ALSO", "JUST", "MORE", "LESS", "BACK", "MOST", "LAST",
            "NEAR", "HUGE", "FAST", "LATE", "EARN", "BEAT", "LOSS", "GAIN", "COST",
            "ZERO", "LONG", "TERM", "FULL", "YEAR", "BEST", "DOWN", "GOES", "ADDS",
            "CUTS", "ENDS", "DAYS", "TIME", "WELL", "PART", "BOTH", "SIDE", "PLAN",
            "TAKE", "MAKE", "MOVE", "ONCE", "ONLY", "EVEN", "EACH", "AMID", "PUSH",
            "VALE", "VALE", "THEM", "THEY", "THEN", "THAN", "BEEN", "WHEN", "WERE",
        }

        tickers: List[str] = []
        for story in stories:
            attrs    = story.get("attributes", {})
            headline = attrs.get("headline", "")
            url      = attrs.get("url", "")

            # Strategy 1: URL slug contains "{ticker}-stock-" pattern
            stock_match = re.search(r"/([a-z]{1,5})-stock[-/]", url)
            if stock_match:
                tickers.append(stock_match.group(1).upper())
                continue  # URL match is most reliable — skip headline parse

            # Strategy 2: Headline contains "Bull v. Bear: TICKER" or ": TICKER"
            colon_match = re.search(r":\s+([A-Z]{2,5})\b", headline)
            if colon_match:
                cand = colon_match.group(1)
                if cand not in _STOPWORDS:
                    tickers.append(cand)
                    continue

            # Strategy 3: First word of headline is ALL-CAPS 2-5 chars (e.g. "AMD beats...")
            first_word = headline.split()[0] if headline.split() else ""
            clean = re.sub(r"[^A-Z]", "", first_word.upper())
            if 2 <= len(clean) <= 5 and clean.isalpha() and clean not in _STOPWORDS:
                tickers.append(clean)

        seen: set = set()
        unique = [t for t in tickers if not (t in seen or seen.add(t))]
        _leading_story_cache = (now, unique)
        _cb_record_success()
        log.debug(f"[SA2] leading_story tickers ({len(unique)}): {unique[:10]}")
        return unique
    except Exception as e:
        _cb_record_failure(e)
        log.debug(f"[SA2] get_sa_leading_story failed: {e}")
        return []


# ── get_sa_market_outlook ─────────────────────────────────────────────────────

_market_outlook_cache: Tuple[float, dict] = (0.0, {})
_MARKET_OUTLOOK_TTL = 3600   # 1 h — articles don't change fast

def get_sa_market_outlook() -> dict:
    """Return SA /market-outlook parsed sentiment.

    Returns dict with keys:
      bullish_pct  : float  0.0–1.0  (fraction of recent outlook articles bullish)
      bearish_pct  : float
      sentiment    : str    "bullish" | "bearish" | "neutral"
      titles       : List[str]  recent article titles (up to 5)
    Falls back to {} on any failure.
    """
    hdrs = _headers2()
    if hdrs is None:
        return {}
    if _cb_is_open():
        return {}

    global _market_outlook_cache
    now = time.monotonic()
    if (now - _market_outlook_cache[0]) < _MARKET_OUTLOOK_TTL and _market_outlook_cache[1]:
        return dict(_market_outlook_cache[1])

    try:
        import re as _re
        import requests as _req
        resp = _req.get(
            f"{_base2()}/market-outlook",
            headers=hdrs,
            timeout=_TIMEOUT,
        )
        if not _sa2_handle_response(resp):
            return {}

        articles = resp.json().get("market_outlook", [])
        _BULL_KEYWORDS = {"bull", "bullish", "rally", "surge", "gain", "rise", "jump", "beat"}
        _BEAR_KEYWORDS = {"bear", "bearish", "sell", "crash", "decline", "drop", "loss", "miss", "recession"}

        bull_count = 0
        bear_count = 0
        titles: List[str] = []
        for art in articles[:10]:
            title = art.get("attributes", {}).get("title", "").lower()
            if title:
                titles.append(art.get("attributes", {}).get("title", ""))
                words = set(_re.sub(r"[^a-z ]", " ", title).split())
                if words & _BULL_KEYWORDS:
                    bull_count += 1
                elif words & _BEAR_KEYWORDS:
                    bear_count += 1

        total = bull_count + bear_count
        bull_pct = bull_count / total if total > 0 else 0.5
        bear_pct = bear_count / total if total > 0 else 0.5
        if bull_pct > 0.6:
            sentiment = "bullish"
        elif bear_pct > 0.6:
            sentiment = "bearish"
        else:
            sentiment = "neutral"

        result = {
            "bullish_pct": round(bull_pct, 3),
            "bearish_pct": round(bear_pct, 3),
            "sentiment":   sentiment,
            "titles":      titles[:5],
        }
        _market_outlook_cache = (now, result)
        _cb_record_success()
        log.info(f"[SA2] market_outlook: {sentiment} (bull={bull_pct:.0%} bear={bear_pct:.0%})")
        return result
    except Exception as e:
        _cb_record_failure(e)
        log.debug(f"[SA2] get_sa_market_outlook failed: {e}")
        return {}
