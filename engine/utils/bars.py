"""
engine.utils.bars
-----------------
Bar data fetching, per-cycle cache, technical indicators (RSI, MACD, ATR).

All public functions here are re-exported from engine.utils for backward compat.
"""

from __future__ import annotations


import datetime
import logging
import threading
import time
from typing import Dict, Tuple

# Add tenacity for retry logic
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

import pandas as pd
import pytz

ET = pytz.timezone("America/New_York")


# ── Schwab rate limiting ──────────────────────────────────────────────────────
# Schwab has aggressive rate limits (~10 req/sec). Use a global lock with min
# interval between requests to avoid 429 errors.
_schwab_request_lock = threading.Lock()
_schwab_last_request_time = 0.0
_SCHWAB_MIN_REQUEST_INTERVAL = 0.15  # 150ms between requests (≈6-7 req/sec)


# ── Per-cycle bar cache ───────────────────────────────────────────────────────
# Keyed by (symbol, period, interval). Thread-safe via lock.
_bar_cache: Dict[Tuple[str, str, str], pd.DataFrame] = {}
_bar_cache_lock = threading.Lock()

# ── Negative cache (clean-empty results) ─────────────────────────────────────
# Symbols that legitimately return no candles (e.g. illiquid microcaps) are
# remembered for _NEG_TTL_SEC so they are not re-fetched every scan cycle
# (prevents 429 saturation). Only clean-empty results are cached — never
# exceptions/timeouts. Bounded to 2000 entries.
_NEG_TTL_SEC = 1800  # 30 min
_neg_cache: Dict[str, float] = {}
_NEG_CACHE_MAX = 2000


def _neg_cache_lookup(symbol: str) -> bool:
    """True if symbol has a fresh negative-cache entry."""
    with _bar_cache_lock:
        ts = _neg_cache.get(symbol)
        if ts is None:
            return False
        if (time.time() - ts) < _NEG_TTL_SEC:
            return True
        # Expired — drop it
        _neg_cache.pop(symbol, None)
        return False


def _neg_cache_record(symbol: str, empty: bool) -> None:
    """Record a clean-empty result (empty=True) or clear entry on success."""
    with _bar_cache_lock:
        if empty:
            if len(_neg_cache) > _NEG_CACHE_MAX:
                _neg_cache.clear()  # bound size — drop oldest wholesale
            _neg_cache[symbol] = time.time()
        else:
            _neg_cache.pop(symbol, None)


# ── Feed tracking ────────────────────────────────────────────────────────────
# _last_feed_used: records which feed was used per symbol in this cycle.
# Cleared by clear_bar_cache() at the start of each scan cycle.
_last_feed_used: Dict[str, str] = {}

# ── Dead ticker suppression (disabled — all tickers eligible) ─────────────────
_DEAD_TICKER_THRESHOLD = 999_999
_dead_ticker_hits: Dict[str, int] = {}
_dead_tickers: set = set()
_dead_ticker_lock = threading.Lock()


def _record_empty_bars(symbol: str) -> None:
    """No-op stub — suppression disabled."""
    return


def _record_ok_bars(symbol: str) -> None:
    """No-op stub."""
    return


def is_dead_ticker(symbol: str) -> bool:
    """Always False — dead-ticker suppression is disabled."""
    return False


def clear_bar_cache() -> None:
    """Flush the per-cycle bar cache. Call once at the start of each scan cycle."""
    global _bar_cache, _last_feed_used
    with _bar_cache_lock:
        _bar_cache = {}
        _last_feed_used = {}


def get_feed_used(symbol: str) -> str:
    """Return the data feed used for the last bar fetch of *symbol* this cycle.

    Returns 'sip' or 'iex'. Defaults to 'iex' when unknown (conservative —
    IEX-adjusted thresholds are applied if the feed is uncertain).
    """
    return _last_feed_used.get(symbol.strip().upper(), "iex")


# ── TimeFrame helper (for other sources) ─────────────────────────────────────


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    """Standardize column names and convert 'time' to ET-aware timestamps."""
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    for alias in ("timestamp", "datetime", "date"):
        if alias in df.columns:
            df = df.rename(columns={alias: "time"})
    if "time" in df.columns:
        col = pd.to_datetime(df["time"])
        try:
            col = col.dt.tz_convert(ET) if col.dt.tz is not None else col.dt.tz_localize("UTC").dt.tz_convert(ET)
        except Exception:
            pass
        df["time"] = col
    return df


def _enforce_schwab_rate_limit():
    """Enforce minimum interval between Schwab API requests to avoid 429 errors."""
    global _schwab_last_request_time
    with _schwab_request_lock:
        now = time.time()
        elapsed = now - _schwab_last_request_time
        if elapsed < _SCHWAB_MIN_REQUEST_INTERVAL:
            time.sleep(_SCHWAB_MIN_REQUEST_INTERVAL - elapsed)
        _schwab_last_request_time = time.time()


def _filter_current_day_minute_bars(df: pd.DataFrame, now: datetime.datetime | None = None) -> pd.DataFrame:
    """Keep only bars from the current America/New_York calendar date."""
    if df.empty or "time" not in df.columns:
        return df

    reference_time = now if now is not None else datetime.datetime.now(ET)
    current_date = reference_time.astimezone(ET).date()
    timestamps = pd.to_datetime(df["time"])
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize(ET)
    else:
        timestamps = timestamps.dt.tz_convert(ET)
    return df.loc[timestamps.dt.date == current_date].copy()


# ── Core bar fetch ────────────────────────────────────────────────────────────

def _get_bars_schwab(symbol: str, period: str, interval: str, log) -> pd.DataFrame:
    """Fetch OHLCV bars via Schwab API with rate limiting and retry logic.
    
    NOTE: Schwab does not support historical candles for index/volatility symbols
    like ^VIX, ^VXN, ^RVX, etc. These symbols are silently skipped (return empty DataFrame).
    
    Rate limits: Schwab enforces ~10 req/sec globally. This function enforces a
    minimum 150ms interval between requests to stay well under the limit.
    
    Retries: 429 errors are retried up to 3 times with exponential backoff.
    """
    # Skip unsupported index/volatility symbols
    unsupported = {"^VIX", "^VXN", "^RVX", "VIX", "VXN", "RVX"}
    if symbol.upper() in unsupported:
        log.debug(f"{symbol}: Schwab does not support index/volatility symbols — skipping")
        return pd.DataFrame()
    
    # Enforce rate limit before making request
    _enforce_schwab_rate_limit()
    
    try:
        from engine.broker.schwab_client import get_schwab_market_data_client
        from requests.exceptions import HTTPError
    except ImportError:
        return pd.DataFrame()
    
    try:
        client = get_schwab_market_data_client()
        
        # Map our period/interval to Schwab parameters
        # period: "5d", "20d", "65d" etc
        # interval: "1m", "5m", "15m", "30m", "60m", "1h", "1d"
        
        days_match = int(period[:-1]) if period.endswith("d") else 5
        
        # Schwab frequencyType options: minute, daily, weekly, monthly
        # NOTE: Schwab rejects periodType=day&period=N with frequencyType=minute
        # (HTTP 400) for 1-minute bars. For minute-frequency requests, use an
        # explicit ET date range (startDate/endDate, epoch ms) instead.
        use_date_range = interval.endswith("m") or interval in ("1h", "60m")
        if use_date_range:
            frequency_type = "minute"
            frequency = 60 if interval in ("1h", "60m") else int(interval[:-1])
            now_dt = datetime.datetime.now(ET)
            start_dt = now_dt - datetime.timedelta(days=days_match)
            start_ms = int(start_dt.timestamp() * 1000)
            end_ms = int(now_dt.timestamp() * 1000)
        else:  # daily or longer
            frequency_type = "daily"
            frequency = 1
            # Use "year" period for daily bars (safer than month calculations)
            period_type = "year"
            period_val = 1
        
        # Retry on 429 (rate limit) with exponential backoff
        def _fetch_with_retry():
            from requests.exceptions import HTTPError
            for attempt in range(3):
                try:
                    if use_date_range:
                        resp = client.get_candles(
                            symbol,
                            frequency_type=frequency_type,
                            frequency=frequency,
                            start_ms=start_ms,
                            end_ms=end_ms,
                        )
                    else:
                        resp = client.get_candles(
                            symbol,
                            period_type=period_type,
                            period=period_val,
                            frequency_type=frequency_type,
                            frequency=frequency
                        )
                    return resp
                except HTTPError as e:
                    if e.response.status_code == 429:
                        if attempt < 2:  # Not the last attempt
                            wait_time = (2 ** attempt) * 0.5  # 0.5s, 1s, 2s
                            log.debug(f"{symbol}: Schwab rate limited (429) — retrying in {wait_time:.1f}s")
                            time.sleep(wait_time)
                        else:
                            log.warning(f"{symbol}: Schwab rate limited (429) — giving up after 3 attempts")
                            raise
                    else:
                        raise
            return None
        
        response = _fetch_with_retry()
        
        if not response or "candles" not in response or not response["candles"]:
            return pd.DataFrame()
        
        candles = response["candles"]
        
        # Normalize Schwab candle format to our standard format
        df = pd.DataFrame({
            "time": pd.to_datetime([c.get("datetime") for c in candles], unit="ms", utc=True).tz_convert(ET),
            "open": [c.get("open", 0) for c in candles],
            "high": [c.get("high", 0) for c in candles],
            "low": [c.get("low", 0) for c in candles],
            "close": [c.get("close", 0) for c in candles],
            "volume": [c.get("volume", 0) for c in candles],
        })

        if period == "1d" and interval.lower().endswith("m"):
            df = _filter_current_day_minute_bars(df)
        
        if not df.empty:
            _last_feed_used[symbol] = "schwab"
            _record_ok_bars(symbol)
            with _bar_cache_lock:
                _bar_cache[(symbol, period, interval)] = df
            log.debug(f"{symbol}: Schwab bars OK — {len(df)} rows ({period}/{interval})")
            return df
        
        return pd.DataFrame()
    
    except Exception as e:
        log.debug(f"{symbol}: Schwab bars failed: {e}")
        return pd.DataFrame()



def get_bars(symbol: str, period: str = "5d", interval: str = "15m") -> pd.DataFrame:
    """Fetch OHLCV bars via Schwab (no fallback).

    Results are cached per (symbol, period, interval) for the current scan
    cycle. Call clear_bar_cache() to reset at cycle start.
    """
    symbol = symbol.strip().upper().lstrip("$")
    log = logging.getLogger("ApexTrader")

    if is_dead_ticker(symbol):
        return pd.DataFrame()

    cache_key = (symbol, period, interval)
    with _bar_cache_lock:
        if cache_key in _bar_cache:
            log.debug(f"{symbol}: bar cache hit ({period}/{interval})")
            return _bar_cache[cache_key]

    # Negative cache: skip symbols that recently returned clean-empty
    if _neg_cache_lookup(symbol):
        log.debug(f"{symbol}: negative cache hit ({period}/{interval})")
        return pd.DataFrame()

    # ── Schwab (primary, only source) ──
    try:
        data = _get_bars_schwab(symbol, period, interval, log)
        if not data.empty:
            _neg_cache_record(symbol, empty=False)
            return data
    except Exception as e:
        # Do NOT negative-cache on exceptions/timeouts — only clean-empty.
        log.debug(f"{symbol}: Schwab fetch failed: {e}")
        return pd.DataFrame()

    _neg_cache_record(symbol, empty=True)
    _record_empty_bars(symbol)
    return pd.DataFrame()


def get_bars_batch(symbols, period: str = "5d", interval: str = "15m") -> Dict[str, pd.DataFrame]:
    """Fetch OHLCV bars for multiple symbols via Schwab.

    Cache-backed: already-fetched symbols in the current cycle are returned
    immediately without a network call. Uncached symbols are fetched with
    per-request rate limiting (150ms minimum interval) to avoid 429 errors.
    """
    log     = logging.getLogger("ApexTrader")
    symbols = [s.strip().upper().lstrip("$") for s in symbols]
    results: Dict[str, pd.DataFrame] = {}
    uncached = []

    with _bar_cache_lock:
        for s in symbols:
            key = (s, period, interval)
            if key in _bar_cache:
                log.debug(f"{s}: bar cache hit ({period}/{interval}) [batch]")
                results[s] = _bar_cache[key]
            else:
                uncached.append(s)

    # ── Schwab path (only source) ──
    # Rate limiting is enforced per-request in _get_bars_schwab
    for s in uncached:
        # Negative cache: skip symbols that recently returned clean-empty
        if _neg_cache_lookup(s):
            results[s] = pd.DataFrame()
            continue
        try:
            data = _get_bars_schwab(s, period, interval, log)
            if not data.empty:
                _neg_cache_record(s, empty=False)
                results[s] = data
            else:
                _neg_cache_record(s, empty=True)
                _record_empty_bars(s)
                results[s] = pd.DataFrame()
        except Exception as e:
            # Do NOT negative-cache on exceptions/timeouts — only clean-empty.
            log.debug(f"{s}: Schwab batch bars failed: {e}")
            _record_empty_bars(s)
            results[s] = pd.DataFrame()

    # Fill missing entries with empty DataFrame
    for s in symbols:
        if s not in results:
            _record_empty_bars(s)
            results[s] = pd.DataFrame()
    return results


def get_price(symbol: str) -> float:
    """Return the latest close price for symbol, or 0.0 on failure."""
    try:
        data = get_bars(symbol, "1d", "1m")
        return float(data["close"].iloc[-1]) if not data.empty else 0.0
    except Exception:
        return 0.0


def get_premarket_bars(symbol: str) -> pd.DataFrame:
    """Fetch today's 1-min bars from 4:00 AM ET (pre-market included).

    Cached under a '_prepost' period key — invalidated by clear_bar_cache().
    Currently uses Schwab via get_bars() as the fallback.
    """
    log       = logging.getLogger("ApexTrader")
    cache_key = (symbol, "1d_prepost", "1m")
    with _bar_cache_lock:
        if cache_key in _bar_cache:
            return _bar_cache[cache_key]

    # Try Schwab/MDA via the standard get_bars() path
    result = get_bars(symbol, period="1d", interval="1m")

    with _bar_cache_lock:
        _bar_cache[cache_key] = result
    return result


# ── Finnhub bar source ────────────────────────────────────────────────────────

def get_finnhub_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
    """Fetch OHLCV bars from Finnhub (alternative data source)."""
    from engine.config import FINNHUB_API_KEY
    if not FINNHUB_API_KEY:
        raise ValueError("FINNHUB_API_KEY not configured")

    try:
        import finnhub
    except ImportError:
        raise ImportError("finnhub-python is not installed")

    resolution_map = {
        "1m": "1", "5m": "5", "15m": "15", "30m": "30",
        "60m": "60", "1h": "60", "1d": "D",
    }
    resolution = resolution_map.get(interval.lower())
    if resolution is None:
        raise ValueError(f"Unsupported Finnhub interval: {interval}")

    now_utc  = datetime.datetime.now(datetime.timezone.utc)
    days     = int(period[:-1]) if period.endswith("d") else 5
    start    = now_utc - datetime.timedelta(days=days)
    client   = finnhub.Client(api_key=FINNHUB_API_KEY)
    data     = client.stock_candles(symbol, resolution, int(start.timestamp()), int(now_utc.timestamp()))
    if data.get("s") != "ok":
        raise RuntimeError(f"Finnhub error for {symbol}: {data.get('s')}")

    df = pd.DataFrame({
        "time":   data.get("t", []),
        "open":   data.get("o", []),
        "high":   data.get("h", []),
        "low":    data.get("l", []),
        "close":  data.get("c", []),
        "volume": data.get("v", []),
    })
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_convert(ET)
    return df


# ── Technical indicators ──────────────────────────────────────────────────────

def calc_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    delta = prices.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = -delta.clip(upper=0).rolling(period).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))


def calc_macd(prices: pd.Series) -> Dict:
    exp1   = prices.ewm(span=12, adjust=False).mean()
    exp2   = prices.ewm(span=26, adjust=False).mean()
    macd   = exp1 - exp2
    signal = macd.ewm(span=9, adjust=False).mean()
    return {"macd": macd, "signal": signal, "hist": macd - signal}


def calculate_atr(bars: pd.DataFrame, period: int = 14) -> float:
    """Compute Average True Range over the last `period` bars. Returns 0.0 on failure."""
    if bars.empty or len(bars) < period:
        return 0.0
    try:
        hl  = bars["high"] - bars["low"]
        hc  = (bars["high"] - bars["close"].shift()).abs()
        lc  = (bars["low"]  - bars["close"].shift()).abs()
        tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean().iloc[-1]
        return float(atr) if not pd.isna(atr) else 0.0
    except Exception:
        return 0.0
