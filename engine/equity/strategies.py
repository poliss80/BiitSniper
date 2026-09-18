"""
ApexTrader - Strategies
Trading strategy implementations:
  - SweepeaStrategy      : Liquidity Sweep + Pinbar (Donchian Channel)
  - TechnicalStrategy    : Multi-indicator technical analysis
  - MomentumStrategy     : Pure momentum with volume confirmation
  - GapBreakoutStrategy  : Gap-up from prior close with volume confirmation
  - ORBStrategy          : Opening Range Breakout (first 15-min high)
  - VWAPReclaimStrategy  : Price reclaims VWAP from below with volume
  - FloatRotationStrategy: High volume relative to float (low-float runners)
"""

import datetime
import logging
import time
import pytz
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional



from engine.utils import get_bars, calc_rsi, calc_macd, get_premarket_bars
from engine.config import (
    SWEEPEA, TECHNICAL, MOMENTUM, GAP_BREAKOUT, ORB, VWAP_RECLAIM, FLOAT_ROTATION, LONG_ONLY_MODE,
    ATR_STOP_MULTIPLIER, ATR_TP_RATIO, HIGH_SHORT_FLOAT_STOCKS, is_high_short_float,
    PRE_MARKET_MOMENTUM, OPENING_BELL_SURGE, PM_HIGH_BREAKOUT, EARLY_SQUEEZE, BEAR_BREAKDOWN,
    SENTIMENT_STRATEGY, TRENDLINE_BREAKOUT,
    SWEEPEA_DYNAMIC_CONFIDENCE, SWEEPEA_REQUIRE_TREND, SWEEPEA_TREND_EMA_RISING_BARS,
    MOMENTUM_CONTINUATION, MARKET_STRUCTURE_BREAKOUT,
)
from scripts.trendline_breakout import detect_trendline_breakouts
from scripts.auto_trendline import AutoTrendline, TrendlineConfig

ET = pytz.timezone("America/New_York")
log = logging.getLogger("ApexTrader")

# Inverse ETFs profit from market declines — treat as LONG buys in bear regime
_INVERSE_ETFS: frozenset = frozenset({
    "SQQQ", "SPXU", "UVXY", "TZA", "FAZ", "SOXS", "LABD", "DUST",
})


@dataclass
class Signal:
    symbol:     str
    action:     str    # 'buy', 'sell' (close-long or enter-short), or 'short' (enter-short)
    price:      float
    confidence: float
    reason:     str
    strategy:   str
    atr_stop:   Optional[float] = None   # ATR-based stop distance ($); None = use % fallback
    trailing_stop: Optional[float] = None  # Trailing stop price (updated dynamically)
    highest_price: Optional[float] = None  # Highest price since entry
    news_direction: str = "neutral"
    news_score: float = 0.0
    news_catalyst: str = "none"
    news_risk_flags: tuple = ()


def _calc_atr14(bars: pd.DataFrame, period: int = 14) -> float:
    """Calculate Average True Range over the last `period` bars."""
    try:
        hi  = bars["high"]
        lo  = bars["low"]
        pc  = bars["close"].shift(1)
        tr  = pd.concat([(hi - lo), (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
        val = tr.rolling(period).mean().iloc[-1]
        return float(val) if pd.notna(val) else 0.0
    except Exception:
        return 0.0


# ── TA Filter Helpers ─────────────────────────────────────────────────────────
# Check 1a: EMA stack — 20 EMA > 50 EMA > 200 EMA (daily closes required).
# Returns True (pass) when insufficient data to avoid blocking good signals.
def _ema_stack_ok(closes: pd.Series) -> bool:
    """True if daily 20 EMA > 50 EMA > 200 EMA (bull EMA alignment)."""
    if len(closes) < 60:
        return True   # not enough history — don't block
    ema20  = float(closes.ewm(span=20,  adjust=False).mean().iloc[-1])
    ema50  = float(closes.ewm(span=50,  adjust=False).mean().iloc[-1])
    if len(closes) >= 200:
        ema200 = float(closes.ewm(span=200, adjust=False).mean().iloc[-1])
        return ema20 > ema50 > ema200
    return ema20 > ema50   # fallback when < 200 days available


def _sweepea_confidence(daily, cur, atr14, is_htf, trend_strength=1.0) -> float:
    """Quality-weighted Sweepea confidence spread across ~0.70-0.95 so position
    sizing differentiates by setup strength instead of a flat 0.82.
    Trend strength (0-1) is folded in as advisory modulation: strong trend
    pullbacks score high, weak/broken-trend pullbacks score low and fall below
    the MIN_SIGNAL_CONFIDENCE floor instead of being hard-blocked."""
    base = 0.70
    closes = daily["close"]
    ema8  = float(closes.ewm(span=8,  adjust=False).mean().iloc[-1])
    close = float(cur["close"])
    q = 0.0
    q += trend_strength * 0.12
    if atr14 and atr14 > 0:
        q += max(0.0, min((close - ema8) / atr14, 1.0)) * 0.06
    if len(daily) >= 21:
        avg_vol = float(daily["volume"].iloc[-21:-1].mean())
        if avg_vol > 0:
            rvol = float(cur["volume"]) / avg_vol
            q += max(0.0, min(rvol - 1.0, 2.0)) * 0.03
    if is_htf:
        q += 0.06
    return round(min(base + q, 0.95), 2)



# Check 3: Price is above the high from the 21–55 day ago window (prior consolidation breakout).
def _above_prior_range_high(daily: pd.DataFrame, near_days: int = 21, far_days: int = 55) -> bool:
    """True if current price broke above the resistance high of the 21–55 day ago window."""
    needed = far_days + 2
    if len(daily) < needed:
        return True   # insufficient data — don't block
    spot       = float(daily["close"].iloc[-1])
    prior_high = float(daily["high"].iloc[-(far_days + 1):-(near_days)].max())
    return spot >= prior_high * 0.995   # within 0.5% counts as breakout


# Check 2: 4H bars synthesised from 60-min Schwab bars (Schwab has no native 4H frequency).
def _get_4h_bars(symbol: str, days: int = 10) -> pd.DataFrame:
    """Fetch 60-min bars and resample to 4-hour OHLCV candles."""
    try:
        bars_60m = get_bars(symbol, f"{days}d", "60m")
        if bars_60m.empty or len(bars_60m) < 4:
            return pd.DataFrame()
        bars_60m = bars_60m.copy().set_index("time")
        bars_4h  = bars_60m.resample("4h", closed="left", label="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna(subset=["close"])
        return bars_4h.reset_index()
    except Exception:
        return pd.DataFrame()


# Check 2: Full Fibonacci HL confluence check across 4H + 15-min.
def _fib_hl_ok(symbol: str) -> bool:
    """Check 4H swing fib level + 15-min Higher Low + bullish reversal candle.

    Returns True (pass) when pattern is detected OR data is insufficient.
    Returns False only when 4H data confirms price is NOT near a fib level
    AND no Higher Low is present — i.e. active rejection.
    """
    try:
        # ── 4H: identify swing high/low and fib levels ───────────────────────
        bars_4h = _get_4h_bars(symbol, days=10)
        if bars_4h.empty or len(bars_4h) < 6:
            return True   # no 4H data — don't block
        swing_high = float(bars_4h["high"].iloc[-7:-1].max())
        swing_low  = float(bars_4h["low"].iloc[:-1].min())
        if swing_high <= swing_low:
            return True
        swing_range = swing_high - swing_low
        fib_levels  = [
            swing_high - 0.500 * swing_range,   # 0.500
            swing_high - 0.618 * swing_range,   # 0.618
            swing_high - 0.786 * swing_range,   # 0.786
        ]
        cur_4h   = float(bars_4h["close"].iloc[-1])
        near_fib = any(abs(cur_4h - lvl) / max(lvl, 0.01) <= 0.008 for lvl in fib_levels)
        if not near_fib:
            return False   # price not near any fib — reject

        # ── 15-min: Higher Low + bullish reversal candle ─────────────────────
        bars_15m = get_bars(symbol, "5d", "15m")
        if bars_15m.empty or len(bars_15m) < 10:
            return True   # no 15m data — don't block (fib level alone is sufficient signal)

        # Find local lows in last 30 bars (candle lower than both neighbours)
        lows = bars_15m["low"].iloc[-30:].reset_index(drop=True)
        local_lows = [
            float(lows.iloc[i])
            for i in range(1, len(lows) - 1)
            if lows.iloc[i] < lows.iloc[i - 1] and lows.iloc[i] < lows.iloc[i + 1]
        ]
        if len(local_lows) < 2:
            return True   # not enough swing structure — don't block
        higher_low = local_lows[-1] > local_lows[-2]

        # Bullish reversal candle: either bullish body or hammer wick ≥ 40%
        last         = bars_15m.iloc[-2]
        candle_range = float(last["high"]) - float(last["low"])
        if candle_range == 0:
            return True
        is_bullish_body  = float(last["close"]) > float(last["open"])
        lower_wick       = float(min(last["open"], last["close"])) - float(last["low"])
        is_hammer        = (lower_wick / candle_range) >= 0.40
        bullish_candle   = is_bullish_body or is_hammer

        return higher_low and bullish_candle
    except Exception:
        return True   # never block on error


# ── Market Regime Filter ──────────────────────────────────────────────────────
# Canonical implementation lives in engine.utils.market.
# Imported here so existing strategy code continues to call _is_bull_regime() directly.
from engine.utils.market import _is_bull_regime  # canonical regime function — shared across all strategy modules


# ── SA Metrics Boost ──────────────────────────────────────────────────────────
# Shared helper: apply SA v2 metrics-grades confidence boost to any buy signal.
# max_boost caps total addition to avoid over-weighting SA on noisy data.
def _sa_metrics_boost(symbol: str, confidence: float, max_boost: float = 0.08) -> float:
    """Return confidence + SA metrics-grades boost, then Mboum analyst-consensus boost.

    Both signals are layered: SA grade boost (up to max_boost) is applied first,
    then Mboum analyst consensus boost/penalty is applied on top (cached 10 min).
    """
    try:
        from engine.data.seeking_alpha import get_sa_metrics_grades
        grades = get_sa_metrics_grades(symbol)
        if grades:
            boost = 0.0
            mom  = grades.get("momentum_category",      0)
            epsr = grades.get("eps_revisions_category", 0)
            prof = grades.get("profitability_category", 0)
            grw  = grades.get("growth_category",        0)
            if mom  >= 8:  boost += 0.03
            elif mom >= 6: boost += 0.015
            if epsr >= 8:  boost += 0.02
            elif epsr >= 6: boost += 0.01
            if prof >= 8:  boost += 0.015
            if grw  >= 8:  boost += 0.015
            confidence = min(confidence + min(boost, max_boost), 0.97)
    except Exception:
        pass
    # Layer Mboum analyst consensus on top (cached — no extra API hit per symbol)
    return _mboum_analyst_boost(symbol, confidence)


# Cache: {symbol: (ts, boost_value)}  — avoids hammering Mboum BASIC (10 req/min)
_mboum_boost_cache: dict = {}
_MBOUM_CACHE_TTL = 600  # 10 min


def _mboum_analyst_boost(symbol: str, confidence: float, max_boost: float = 0.07) -> float:
    """Return confidence + Mboum analyst-consensus boost.

    Adds up to max_boost when:
      - recommendation is 'strong_buy' or 'buy'
      - analyst mean price target is meaningfully above current price (upside ≥10%)

    Result is cached per symbol for 10 minutes to respect Mboum BASIC rate limits.
    """
    import time as _time
    try:
        import requests as _req, os as _os
        now = _time.monotonic()
        cached = _mboum_boost_cache.get(symbol)
        if cached and (now - cached[0]) < _MBOUM_CACHE_TTL:
            return min(confidence + cached[1], 0.97)
        key = "e8623e01d7msh66804a8ade71af0p120d0ejsn20ee85188848"
        r = _req.get(
            "https://mboum-finance.p.rapidapi.com/v1/markets/stock/modules",
            params={"symbol": symbol, "module": "financial-data"},
            headers={"x-rapidapi-host": "mboum-finance.p.rapidapi.com", "x-rapidapi-key": key},
            timeout=6,
        )
        if r.status_code != 200:
            _mboum_boost_cache[symbol] = (_time.monotonic(), 0.0)
            return confidence
        body = r.json().get("body", {})
        current = float(body.get("currentPrice", 0) or 0)
        target_mean = float(body.get("targetMeanPrice", 0) or 0)
        rec = str(body.get("recommendationKey", "")).lower()
        if current <= 0 or target_mean <= 0:
            _mboum_boost_cache[symbol] = (_time.monotonic(), 0.0)
            return confidence
        upside_pct = (target_mean - current) / current * 100
        boost = 0.0
        if rec in ("strong_buy", "buy") and upside_pct >= 10:
            boost = min(max_boost, 0.03 + upside_pct / 500)
        elif rec == "hold" and upside_pct >= 20:
            boost = 0.02
        # Sell consensus → small penalty
        elif rec in ("sell", "strong_sell"):
            boost = -0.04
        _mboum_boost_cache[symbol] = (_time.monotonic(), boost)
        return min(confidence + boost, 0.97)
    except Exception:
        return confidence


# ── MDA real-time quote helper ────────────────────────────────────────────────
# Cache: {symbol: (monotonic_ts, quote_dict)}
_mda_quote_cache: dict = {}
_MDA_QUOTE_TTL = 300  # 5-minute TTL — quote data stays fresh enough for signal confirmation


def _mda_quote(symbol: str) -> dict:
    """Return a dict with MDA real-time quote fields for *symbol*.

    Keys returned (all float, 0.0 when unavailable):
      price, change_pct, volume, week52_high, week52_low

    Results are cached for 5 minutes. Returns empty dict on any error.
    """
    import time as _t, os as _os, requests as _req
    now = _t.monotonic()
    cached = _mda_quote_cache.get(symbol)
    if cached and (now - cached[0]) < _MDA_QUOTE_TTL:
        return cached[1]
    key = _os.environ.get("MARKETDATA_API_KEY", "")
    if not key:
        return {}
    try:
        r = _req.get(
            f"https://api.marketdata.app/v1/stocks/quotes/{symbol}/",
            headers={"Authorization": f"Bearer {key}"},
            timeout=6,
        )
        if r.status_code not in (200, 203):
            return {}
        d = r.json()
        if d.get("s") != "ok":
            return {}
        def _first(lst):
            return float(lst[0]) if lst else 0.0
        q = {
            "price":       _first(d.get("last",      [])),
            "change_pct":  _first(d.get("changepct", [])) * 100,  # MDA decimal → pct
            "volume":      _first(d.get("volume",    [])),
            "week52_high": _first(d.get("52weekHigh", [])),
            "week52_low":  _first(d.get("52weekLow",  [])),
        }
        _mda_quote_cache[symbol] = (now, q)
        return q
    except Exception:
        return {}


def _near_52w_high(symbol: str, within_pct: float = 15.0) -> bool:
    """Return True when the current price is within *within_pct*% of the 52-week high.

    This is a strong bullish structural filter: breakouts near a 52-week high
    have significantly higher follow-through rates than breakouts from mid-range.
    Uses MDA real-time quote (cached 5 min). Returns False on any data failure.
    """
    q = _mda_quote(symbol)
    if not q or q["week52_high"] <= 0 or q["price"] <= 0:
        return False
    pct_from_high = (q["week52_high"] - q["price"]) / q["week52_high"] * 100
    return pct_from_high <= within_pct


def _sweepea_liquidity_ok(symbol: str, price: float, daily: Optional[pd.DataFrame] = None) -> bool:
    if price <= 0:
        return False

    min_price = float(SWEEPEA.get("min_price", 0.0) or 0.0)
    if min_price > 0 and price < min_price:
        log.debug("Sweepea reject %s: price %.2f below %.2f", symbol, price, min_price)
        return False

    lookback = max(1, int(SWEEPEA.get("liquidity_lookback_days", 20) or 20))
    if daily is None or daily.empty or "volume" not in daily.columns:
        daily = get_bars(symbol, f"{max(lookback + 5, 30)}d", "1d")
    if daily.empty or "volume" not in daily.columns:
        log.debug("Sweepea reject %s: missing daily volume history", symbol)
        return False

    recent_volume = daily["volume"].dropna().tail(lookback)
    if recent_volume.empty:
        log.debug("Sweepea reject %s: empty daily volume history", symbol)
        return False

    avg_volume = float(recent_volume.mean())
    min_avg_volume = float(SWEEPEA.get("min_avg_volume", 0.0) or 0.0)
    min_avg_dollar_volume = float(SWEEPEA.get("min_avg_dollar_volume", 0.0) or 0.0)
    if min_avg_volume > 0 and avg_volume < min_avg_volume:
        log.debug("Sweepea reject %s: avg volume %.0f below %.0f", symbol, avg_volume, min_avg_volume)
        return False
    if min_avg_dollar_volume > 0 and avg_volume * price < min_avg_dollar_volume:
        log.debug(
            "Sweepea reject %s: avg dollar volume %.0f below %.0f",
            symbol,
            avg_volume * price,
            min_avg_dollar_volume,
        )
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Sweepea Strategy
# ──────────────────────────────────────────────────────────────────────────────
class SweepeaStrategy:
    """Liquidity Sweep + Pinbar with Donchian Channel swing detection.
    Also fires on daily 8/20-EMA pullback after an initial squeeze (secondary move)."""

    def scan(self, symbol: str, open_position: Optional[dict] = None) -> Optional[Signal]:
        daily = pd.DataFrame()
        try:
            # Daily bar only meaningful after 10 AM ET — skip pre-market noise
            if datetime.datetime.now(ET).hour >= 10:
              daily = get_bars(symbol, "90d", "1d")
            else:
              daily = pd.DataFrame()
            if not daily.empty and len(daily) >= 25:
                daily = daily.copy()
                daily["ema8"]  = daily["close"].ewm(span=8,  adjust=False).mean()
                daily["ema20"] = daily["close"].ewm(span=20, adjust=False).mean()
                daily["ema50"] = daily["close"].ewm(span=50, adjust=False).mean()
                cur  = daily.iloc[-1]
                prev = daily.iloc[-2]
                if not _sweepea_liquidity_ok(symbol, float(cur["close"]), daily):
                    return None
                # Price touched or slightly undercut EMA and recovered above it
                pb8  = (cur["low"] <= float(cur["ema8"])  * 1.005
                        and cur["close"] >= float(cur["ema8"]) * 0.995)
                pb20 = (cur["low"] <= float(cur["ema20"]) * 1.005
                        and cur["close"] >= float(cur["ema20"]) * 0.995)
                # Prior trend must be up (close > 8-bar lookback mean)
                uptrend = float(prev["close"]) > float(daily["close"].iloc[-10:-2].mean())
                is_inverse = symbol in _INVERSE_ETFS
                # Inverse ETFs are valid LONG buys in bear regime
                regime_ok = is_inverse or _is_bull_regime()
                # Check 1: EMA stack (20 > 50 > 200) — skip for inverse ETFs and thin/low-price
                # stocks. Small/mid-cap speculative names (<$15 or avg vol <300k) frequently trend
                # strongly with a broken EMA stack (post-catalyst, early breakout). Applying the
                # stack filter to them produces false rejections with no expectancy improvement.
                _avg_vol20 = float(daily["volume"].iloc[-21:-1].mean()) if len(daily) >= 22 else 0
                _liquid_stock = float(cur["close"]) >= 15.0 and _avg_vol20 >= 300_000
                ema_stack = is_inverse or not _liquid_stock or _ema_stack_ok(daily["close"])
                # NOTE: _above_prior_range_high not applied here — SweepeaPath A is a pullback
                # strategy; requiring a new 21-55d high contradicts the pullback setup.
                # Check 4: RSI > 30 on daily (not oversold) — skip for inverse ETFs
                _rsi_daily = float(daily["close"].ewm(com=13, adjust=False).mean().iloc[-1])  # fast proxy
                _rsi_series = daily["close"].diff().pipe(
                    lambda d: 100 - 100 / (1 + d.clip(lower=0).rolling(14).mean() / (-d.clip(upper=0).rolling(14).mean()).replace(0, 1e-9))
                )
                _daily_rsi = float(_rsi_series.iloc[-1]) if len(_rsi_series) >= 14 else 50.0
                rsi_ok = is_inverse or _daily_rsi > 30
                # Lever C: trend is ADVISORY — computed as a 0-1 strength score
                # regardless of SWEEPEA_REQUIRE_TREND and folded into confidence
                # instead of hard-blocking entries. trend_ok kept for logging.
                _ema20_prev = float(daily["close"].iloc[:-SWEEPEA_TREND_EMA_RISING_BARS]
                                    .ewm(span=20, adjust=False).mean().iloc[-1])
                _stacked = float(cur["ema8"]) > float(cur["ema20"]) > float(cur["ema50"])
                _rising  = float(cur["ema20"]) > _ema20_prev
                _above50 = float(cur["close"]) > float(cur["ema50"])
                trend_ok = _stacked and _rising and _above50
                trend_strength = (0.4 if _stacked else 0.0) \
                               + (0.3 if _rising  else 0.0) \
                               + (0.3 if _above50 else 0.0)
                if (pb8 or pb20) and uptrend and regime_ok and ema_stack and rsi_ok:
                    atr14   = _calc_atr14(daily)
                    ema_lbl = "8-EMA" if pb8 else "20-EMA"
                    # High-Tight Flag: up ≥50% in last 4 weeks + tight 5-day consolidation
                    is_htf   = False
                    htf_note = ""
                    if len(daily) >= 22:
                        price_4w    = float(daily["close"].iloc[-22])
                        gain_4w_pct = ((float(cur["close"]) - price_4w) / price_4w * 100
                                       if price_4w > 0 else 0.0)
                        last5_hi = float(daily["high"].iloc[-6:-1].max())
                        last5_lo = float(daily["low"].iloc[-6:-1].min())
                        tight    = (last5_hi - last5_lo) < float(cur["close"]) * 0.10
                        if gain_4w_pct >= 50.0 and tight:
                            is_htf   = True
                            htf_note = f" | HTF +{gain_4w_pct:.0f}% / 4w"
                    if SWEEPEA_DYNAMIC_CONFIDENCE and not is_inverse:
                        conf = _sweepea_confidence(daily, cur, atr14, is_htf, trend_strength)
                    else:
                        conf = 0.88 if is_htf else 0.82
                    conf = _sa_metrics_boost(symbol, conf)
                    entry_price = float(cur["close"])
                    # Trailing stop logic: set initial highest_price and trailing_stop
                    trailing_stop = entry_price * 0.90  # 10% trailing stop
                    highest_price = entry_price
                    # If open_position is provided, update trailing stop and highest price
                    if open_position is not None:
                        prev_high = open_position.get("highest_price", entry_price)
                        last_price = entry_price
                        highest_price = max(prev_high, last_price)
                        trailing_stop = highest_price * 0.90
                        # If price falls to or below trailing stop, trigger sell
                        if last_price <= trailing_stop:
                            return Signal(
                                symbol, "sell", last_price, conf,
                                f"Trailing stop-loss hit at {trailing_stop:.2f}",
                                "Sweepea",
                                trailing_stop=trailing_stop,
                                highest_price=highest_price
                            )
                    return Signal(
                        symbol, "buy", entry_price, conf,
                        f"Daily Sweepea pullback to {ema_lbl}{htf_note} | ATR ${atr14:.2f}",
                        "Sweepea",
                        atr_stop=atr14 * ATR_STOP_MULTIPLIER if atr14 > 0 else None,
                        trailing_stop=trailing_stop,
                        highest_price=highest_price
                    )
                elif (pb8 or pb20) and uptrend and regime_ok and rsi_ok:
                    log.debug(
                        "Sweepea reject %s: ema_stack filter failed "
                        "(trend advisory only: trend_ok=%s trend_strength=%.2f)",
                        symbol, trend_ok, trend_strength,
                    )
        except Exception:
            pass

        # ── Path B: intraday liquidity sweep + pinbar ────────────────────────────
        bars = get_bars(symbol, "10d", f"{SWEEPEA['timeframe']}m")
        if bars.empty or len(bars) < 30:
            return None
        bars = bars.copy()

        # Moving averages
        bars["ma_fast"] = bars["close"].rolling(SWEEPEA["ma_fast"]).mean()
        bars["ma_slow"] = bars["close"].rolling(SWEEPEA["ma_slow"]).mean()

        # Bollinger Bands
        sma = bars["close"].rolling(SWEEPEA["bb_period"]).mean()
        std = bars["close"].rolling(SWEEPEA["bb_period"]).std()
        bars["bb_up"] = sma + SWEEPEA["bb_std"] * std
        bars["bb_lo"] = sma - SWEEPEA["bb_std"] * std

        # Donchian Channel (20-period, shift to avoid lookahead bias)
        lookback = 20
        bars["swing_low"]  = bars["low"].shift(1).rolling(lookback).min()
        bars["swing_high"] = bars["high"].shift(1).rolling(lookback).max()

        # Volume MA
        bars["vol_ma"] = bars["volume"].rolling(20).mean()

        cur = bars.iloc[-2]  # Last closed candle
        range_val = cur["high"] - cur["low"]
        if range_val == 0:
            return None
        if not _sweepea_liquidity_ok(symbol, float(cur["close"]), daily):
            return None

        # Liquidity sweep detection with volatility-aware threshold.
        # Absolute min_sweep alone is too small for high-priced names and too large
        # for microcaps, so blend it with a small % of price and 15m ATR proxy.
        tr = (bars["high"] - bars["low"]).rolling(14).mean()
        atr14_i = float(tr.iloc[-2]) if not pd.isna(tr.iloc[-2]) else 0.0
        sweep_threshold = max(
            float(SWEEPEA["min_sweep"]),
            float(cur["close"]) * 0.0015,   # 0.15% of price
            atr14_i * 0.20,                  # 20% of local ATR
        )

        # Use configurable sweep_bars window (>=1).
        sweep_bars = max(1, int(SWEEPEA.get("sweep_bars", 1)))
        recent = bars.iloc[-(sweep_bars + 1):-1]
        if recent.empty:
            recent = bars.iloc[-2:-1]

        swept_below = float(recent["low"].min())  < (float(cur["swing_low"])  - sweep_threshold)
        swept_above = float(recent["high"].max()) > (float(cur["swing_high"]) + sweep_threshold)

        # Pinbar wick ratios
        lower_wick       = min(cur["open"], cur["close"]) - cur["low"]
        lower_wick_ratio = (lower_wick / range_val) * 100
        upper_wick       = cur["high"] - max(cur["open"], cur["close"])
        upper_wick_ratio = (upper_wick / range_val) * 100

        # Volume confirmation
        high_volume = cur["volume"] >= (cur["vol_ma"] * 1.05)

        bull_pin = swept_below and lower_wick_ratio >= SWEEPEA["pinbar_threshold"] and high_volume
        bear_pin = swept_above and upper_wick_ratio >= SWEEPEA["pinbar_threshold"] and high_volume

        # Optional Bollinger touch filter (configured but previously unused).
        if SWEEPEA.get("use_bb", False):
            if bull_pin and not pd.isna(cur["bb_lo"]):
                if float(cur["low"]) > float(cur["bb_lo"]) * 1.01:
                    bull_pin = False
            if bear_pin and not pd.isna(cur["bb_up"]):
                if float(cur["high"]) < float(cur["bb_up"]) * 0.99:
                    bear_pin = False

        # MA Filter
        if SWEEPEA["use_ma"]:
            if pd.isna(cur["ma_fast"]) or pd.isna(cur["ma_slow"]):
                return None

            if bull_pin:
                ma_touch       = cur["low"]   <= cur["ma_fast"]
                close_recovery = cur["close"] >= cur["ma_fast"] * 0.98
                if not (ma_touch and close_recovery):
                    return None

            if bear_pin:
                ma_touch        = cur["high"]  >= cur["ma_fast"]
                close_rejection = cur["close"] <= cur["ma_fast"] * 1.02
                if not (ma_touch and close_rejection):
                    return None

        # Confidence scales with wick quality and volume expansion.
        vol_ratio = float(cur["volume"] / cur["vol_ma"]) if cur["vol_ma"] and cur["vol_ma"] > 0 else 1.0
        bull_conf = min(0.72 + max(lower_wick_ratio - SWEEPEA["pinbar_threshold"], 0) / 200 + max(vol_ratio - 1.0, 0) * 0.05, 0.88)
        bear_conf = min(0.72 + max(upper_wick_ratio - SWEEPEA["pinbar_threshold"], 0) / 200 + max(vol_ratio - 1.0, 0) * 0.05, 0.88)

        _is_inv = symbol in _INVERSE_ETFS
        if bull_pin and (_is_bull_regime() or _is_inv):
            # Check 2: 4H+15m Fib HL confluence (skip for inverse ETFs — no meaningful fib on them)
            if not _is_inv and not _fib_hl_ok(symbol):
                return None
            bull_conf = _sa_metrics_boost(symbol, bull_conf)
            return Signal(symbol, "buy",  float(cur["close"]), bull_conf,
                          f"Liquidity sweep + bullish pinbar | wick {lower_wick_ratio:.0f}% | vol x{vol_ratio:.1f}", "Sweepea")
        # Shorts are globally disabled
        # elif bear_pin and not LONG_ONLY_MODE:
        #     return Signal(symbol, "sell", float(cur["close"]), bear_conf,
        #                   f"Liquidity sweep + bearish pinbar | wick {upper_wick_ratio:.0f}% | vol x{vol_ratio:.1f}", "Sweepea")

        return None


# ──────────────────────────────────────────────────────────────────────────────
# TrendBreaker Strategy
# ──────────────────────────────────────────────────────────────────────────────
class TrendBreakerStrategy:
    """Detects upside trend breaks on high short float stocks.

    Pattern (short-squeeze breakout):
      - Stock was below 20-day SMA for ≥5 consecutive days (bears in control)
      - Current price breaks back above 20SMA AND above 10-day high
      - Volume spike ≥2x average (shorts forced to cover)
      - RSI crossing above 50 (momentum flip confirmation)
      - Bonus: in HIGH_SHORT_FLOAT_STOCKS set → higher confidence
    """

    def scan(self, symbol: str) -> Optional[Signal]:
        # Daily bar only meaningful after 10 AM ET — pre-market bar has <5% of normal volume
        if datetime.datetime.now(ET).hour < 10:
            return None
        daily = get_bars(symbol, "60d", "1d")
        if daily.empty or len(daily) < 22:
            return None

        closes  = daily["close"]
        volumes = daily["volume"]
        sma20   = closes.rolling(20).mean()
        price   = float(closes.iloc[-1])
        sma20_now = float(sma20.iloc[-1])
        if sma20_now <= 0:
            return None

        # Was below 20SMA for at least 5 of the last 6 days (excl. today)
        below_count = sum(closes.iloc[-7:-1].values < sma20.iloc[-7:-1].values)
        if below_count < 5:
            return None

        # Today broke back above 20SMA
        if price <= sma20_now:
            return None

        # Price must also clear the 21-day high (prior to today) — confirms real breakout
        high_21d = float(daily["high"].iloc[-22:-1].max())
        if price < high_21d * 1.002:   # requires at least a clean break (0.2% above)
            return None

        # Check 1: EMA stack (20 > 50 > 200) must be aligned for a valid squeeze breakout
        if not _ema_stack_ok(closes):
            return None

        # Volume spike: today vs 20-day average
        vol_today = float(volumes.iloc[-1])
        vol_avg   = float(volumes.iloc[-21:-1].mean())
        if vol_avg <= 0:
            return None
        vol_ratio = vol_today / vol_avg
        if vol_ratio < 3.0:   # "Volume Gift": need 3×–5× for aggressive squeeze entry
            return None
        if vol_ratio > 40:    # extreme outlier = likely pump/manipulation, not a squeeze
            return None

        # Minimum price gate: sub-$5 penny stocks are too volatile and pump-prone
        if price < 5.0:
            return None

        # RSI crossing above 50
        rsi = calc_rsi(closes, period=14)
        rsi_now  = float(rsi.iloc[-1])
        rsi_prev = float(rsi.iloc[-2])
        if not (rsi_now > 50 and rsi_prev <= 55):   # just crossed or near cross
            return None

        atr14 = _calc_atr14(daily)

        # Small-cap gate: disabled until a market-cap data source is wired into utils.
        # Wire in get_market_cap(symbol) here when available; check < $1B for squeeze plays.

        # Confidence: base 0.78, scales with volume gift (3×→5× = 0.78→0.83)
        confidence = 0.78 + min((vol_ratio - 3.0) * 0.025, 0.12)
        if is_high_short_float(symbol):
            confidence = min(confidence + 0.07, 0.95)
        # Near 52-week high = structural breakout, not just an intraday noise squeeze
        if _near_52w_high(symbol, within_pct=20.0):
            confidence = min(confidence + 0.04, 0.95)
        confidence = _sa_metrics_boost(symbol, confidence)

        return Signal(
            symbol, "buy", price, round(confidence, 2),
            f"Trend break above 20SMA + 10d high | vol x{vol_ratio:.1f} | RSI {rsi_now:.0f}",
            "TrendBreaker",
            atr_stop=atr14 * ATR_STOP_MULTIPLIER if atr14 > 0 else None,
        )


class SentimentStrategy:
    """Trade based on market sentiment with technical confirmation."""

    def scan(self, symbol: str, market_sentiment: str = "neutral") -> Optional[Signal]:
        if not SENTIMENT_STRATEGY.get("enabled", False):
            return None
        if market_sentiment not in ("bullish", "bearish"):
            return None

        bars = get_bars(symbol, "10d", "15m")
        if bars.empty or len(bars) < 20:
            return None

        price = float(bars["close"].iloc[-1])
        sma20 = float(bars["close"].rolling(20).mean().iloc[-1])
        vol_ratio = float(bars["volume"].iloc[-5:].mean()) / max(float(bars["volume"].mean()), 1.0)

        if vol_ratio < SENTIMENT_STRATEGY.get("volume_surge", 2.0):
            return None

        confidence = min(0.55 + (vol_ratio - 1.0) * 0.1, 0.92)

        if market_sentiment == "bullish":
            if price > sma20:
                confidence = _sa_metrics_boost(symbol, confidence)
                return Signal(
                    symbol, "buy", price, confidence,
                    f"Sentiment bullish + vol x{vol_ratio:.2f} + price>20SMA", "Sentiment",
                    atr_stop=None,
                )
            return None

        # Shorts are globally disabled
        # if market_sentiment == "bearish" and not LONG_ONLY_MODE:
        #     if price < sma20:
        #         return Signal(
        #             symbol, "short", price, confidence,
        #             f"Sentiment bearish + vol x{vol_ratio:.2f} + price<20SMA", "Sentiment",
        #             atr_stop=None,
        #         )
        #     return None

        return None


# ──────────────────────────────────────────────────────────────────────────────
# Technical Strategy
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
class TechnicalStrategy:
    """Multi-indicator technical analysis (RSI, MACD, MA, Volume)."""

    def scan(self, symbol: str, market_sentiment: str = "neutral") -> Optional[Signal]:
        bars = get_bars(symbol, "10d", "15m")
        if bars.empty or len(bars) < 50:
            return None

        # Inverse ETFs are LONG buys in bear market — flip sentiment and relax thresholds
        is_inverse = symbol in _INVERSE_ETFS
        if is_inverse and market_sentiment in ("bearish", "neutral"):
            market_sentiment = "bullish"  # bear/neutral market = tailwind for inverse ETFs

        price   = float(bars["close"].iloc[-1])
        rsi     = calc_rsi(bars["close"])
        cur_rsi = rsi.iloc[-1]
        macd    = calc_macd(bars["close"])
        sma20   = bars["close"].rolling(20).mean().iloc[-1]
        sma50   = bars["close"].rolling(50).mean().iloc[-1]
        vol_ratio = bars["volume"].iloc[-1] / bars["volume"].mean()

        score   = 0.0
        reasons = []

        # Check 4: RSI > 30 — reject oversold entries (price may continue falling).
        # Exception: inverse ETFs can be bought even when RSI is low (crash-day entries).
        if cur_rsi <= 30 and not is_inverse:
            return None

        if cur_rsi > TECHNICAL["rsi_overbought"]:
            # Inverse ETFs can stay overbought during sustained bear markets — don't penalize
            if not is_inverse:
                score -= 0.3
            reasons.append("Overbought")

        if macd["hist"].iloc[-1] > 0 and macd["hist"].iloc[-1] > macd["hist"].iloc[-2]:
            score += 0.2
            reasons.append("Bullish MACD (accelerating)")
        elif macd["hist"].iloc[-1] > 0:
            score += 0.1
            reasons.append("Bullish MACD")
        else:
            score -= 0.2
            reasons.append("Bearish MACD")

        if price > sma20 > sma50:
            score += 0.2
            reasons.append("Uptrend")
        elif price < sma20 < sma50:
            score -= 0.2
            reasons.append("Downtrend")

        if vol_ratio > TECHNICAL["volume_surge"]:
            score += 0.1
            reasons.append("High volume")

        if market_sentiment == "bullish":
            score += 0.1
        elif market_sentiment == "bearish":
            score -= 0.1

        # Inverse ETFs: boost score when today's price is above yesterday's close
        # (crash-day gap signals bear tailwind even if intraday technicals are mixed).
        if is_inverse:
            try:
                _daily = get_bars(symbol, "3d", "1d")
                if not _daily.empty and len(_daily) >= 2:
                    _prev = float(_daily["close"].iloc[-2])
                    if _prev > 0:
                        _gap_pct = (price - _prev) / _prev * 100
                        if _gap_pct >= 2.0:
                            score += 0.20
                            reasons.append(f"Daily gap +{_gap_pct:.1f}%")
                        elif _gap_pct >= 0.5:
                            score += 0.10
                            reasons.append(f"Daily gap +{_gap_pct:.1f}%")
            except Exception:
                pass

        # ATR-14 for stop sizing (use daily bars; fall back to 15m if unavailable)
        _atr14 = 0.0
        try:
            _daily_atr = get_bars(symbol, "20d", "1d")
            if not _daily_atr.empty:
                _atr14 = _calc_atr14(_daily_atr)
        except Exception:
            pass

        # Inverse ETFs: lower entry bar; guarantee confidence meets minimum
        buy_threshold = 0.38 if is_inverse else 0.50
        if score >= buy_threshold:
            conf = max(score, 0.73) if is_inverse else score
            if not is_inverse:
                conf = _sa_metrics_boost(symbol, conf)
            return Signal(symbol, "buy", price, conf, ", ".join(reasons), "Technical",
                          atr_stop=_atr14 * ATR_STOP_MULTIPLIER if _atr14 > 0 else None)
        elif not LONG_ONLY_MODE and score <= -0.45:
            return Signal(symbol, "short", price, abs(score), ", ".join(reasons), "Technical",
                          atr_stop=_atr14 * ATR_STOP_MULTIPLIER if _atr14 > 0 else None)

        return None


# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
# Momentum Strategy
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
class MomentumStrategy:
    """Pure momentum trading with volume confirmation."""

    def scan(self, symbol: str, market_regime: str = "bull") -> Optional[Signal]:
        is_inverse = symbol in _INVERSE_ETFS

        if is_inverse:
            # Inverse ETFs: measure 5-day daily momentum (more reliable than 30-min intraday).
            # Also accepts crash-day entries where today's gap vs yesterday >= 2% even if the
            # 5-day trend is temporarily negative (market was recovering before today's crash).
            daily = get_bars(symbol, "10d", "1d")
            if daily.empty or len(daily) < 5:
                return None
            price      = float(daily["close"].iloc[-1])
            prev_close = float(daily["close"].iloc[-2]) if len(daily) >= 2 else price
            price_5d   = float(daily["close"].iloc[-6]) if len(daily) >= 6 else float(daily["close"].iloc[0])
            momentum_5d = ((price / price_5d) - 1) * 100
            gap_today   = ((price - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0
            sma5  = float(daily["close"].rolling(5).mean().iloc[-1])
            has_5d_trend  = momentum_5d >= 2.0 and price >= sma5 * 0.98
            has_crash_gap = gap_today >= 2.0 and price >= sma5 * 0.92
            if has_5d_trend or has_crash_gap:
                momentum   = max(momentum_5d, gap_today)
                confidence = min(0.73 + (momentum / 100), 0.95)
                label      = "5d trend" if has_5d_trend else "crash-day gap"
                return Signal(symbol, "buy", price, confidence,
                              f"Bear inverse ETF momentum ({momentum:.1f}% / {label})", "Momentum")
            return None

        bars = get_bars(symbol, "1d", "1m")
        if bars.empty or len(bars) < 30:
            return None

        price    = float(bars["close"].iloc[-1])
        price_30 = float(bars["close"].iloc[-30])
        momentum = ((price / price_30) - 1) * 100

        vol_ratio = bars["volume"].iloc[-10:].mean() / bars["volume"].mean()
        sma20     = float(bars["close"].rolling(20).mean().iloc[-1])

        if (momentum >= MOMENTUM["min_momentum"]
                and vol_ratio >= MOMENTUM["volume_surge"]
                and price > sma20):
            # Check 1: EMA stack (20 > 50 > 200) on daily bars — only for liquid stocks
            # (price >= $15, avg vol >= 300k). Thin/low-price names trend strongly post-catalyst
            # even with broken EMA stacks; the filter produces false rejections there.
            _daily_mom = get_bars(symbol, "60d", "1d")
            if not _daily_mom.empty and len(_daily_mom) >= 22:
                _avg_vol_mom = float(_daily_mom["volume"].iloc[-21:-1].mean()) if len(_daily_mom) >= 22 else 0
                _liquid_mom  = price >= 15.0 and _avg_vol_mom >= 300_000
                if _liquid_mom and not _ema_stack_ok(_daily_mom["close"]):
                    return None
                # Check 4: RSI > 30 on daily
                _rsi_mom = _daily_mom["close"].diff().pipe(
                    lambda d: 100 - 100 / (1 + d.clip(lower=0).rolling(14).mean() / (-d.clip(upper=0).rolling(14).mean()).replace(0, 1e-9))
                )
                if float(_rsi_mom.iloc[-1]) <= 30:
                    return None
            confidence = min(0.73 + (momentum / 100), 0.95)
            # MDA real-time confirmation: ensure momentum is still intact
            q = _mda_quote(symbol)
            if q and q["price"] > 0:
                # If live price has pulled back >1% from the bar close, skip
                if q["price"] < price * 0.99:
                    return None
                # Near 52-week high boosts conviction
                if q["week52_high"] > 0 and (q["week52_high"] - q["price"]) / q["week52_high"] * 100 <= 10:
                    confidence = min(confidence + 0.03, 0.95)
            confidence = _sa_metrics_boost(symbol, confidence)
            return Signal(symbol, "buy", price, confidence,
                          f"Strong momentum ({momentum:.1f}%) + volume x{vol_ratio:.1f} + above SMA20", "Momentum")

        # Bear-market momentum reversal short signal
        if market_regime == "bear" and not LONG_ONLY_MODE:
            if (momentum <= -MOMENTUM["min_momentum"] * 0.8
                    and vol_ratio >= MOMENTUM["volume_surge"]
                    and price < sma20):
                confidence = min(0.73 + (-momentum / 100), 0.95)
                return Signal(symbol, "short", price, confidence,
                              f"Bear momentum short (-{momentum:.1f}%) + volume x{vol_ratio:.1f} + below SMA20", "Momentum")

        return None


# ──────────────────────────────────────────────────────────────
# Gap Breakout Strategy
# ──────────────────────────────────────────────────────────────
class GapBreakoutStrategy:
    """Gap-up continuation: stock opens significantly above prior close.

    Logic:
      - Load last 2 daily bars to get prior-day close
      - Compare today's current price to prior close
      - Require intraday volume already > GAP_BREAKOUT['volume_multiplier'] * recent avg
      - Only trade within first GAP_BREAKOUT['entry_window_min'] minutes of open
    """

    def scan(self, symbol: str) -> Optional[Signal]:
        # Daily bars — need at least 2 to get prior close
        daily = get_bars(symbol, "5d", "1d")
        if daily.empty or len(daily) < 2:
            return None

        prior_close = float(daily["close"].iloc[-2])
        if prior_close <= 0:
            return None

        # Intraday 1-min bars for current price and volume
        intraday = get_bars(symbol, "1d", "1m")
        if intraday.empty or len(intraday) < 5:
            return None

        price = float(intraday["close"].iloc[-1])
        gap_pct = ((price - prior_close) / prior_close) * 100

        if gap_pct < GAP_BREAKOUT["min_gap_pct"]:
            return None

        # Volume: recent 5 bars vs full-day average
        vol_recent = intraday["volume"].iloc[-5:].mean()
        vol_avg    = intraday["volume"].mean()
        if vol_avg == 0:
            return None
        vol_ratio = vol_recent / vol_avg

        if vol_ratio < GAP_BREAKOUT["volume_multiplier"]:
            return None

        # Entry window: only in first N minutes after open
        now_et = datetime.datetime.now(ET)
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        minutes_since_open = (now_et - market_open).total_seconds() / 60
        if not (0 <= minutes_since_open <= GAP_BREAKOUT["entry_window_min"]):
            return None

        # Price still holding above prior close + gap buffer
        if price < prior_close * (1 + GAP_BREAKOUT["min_gap_pct"] / 100 * 0.5):
            return None

        atr14 = _calc_atr14(daily)
        # Check 4: RSI > 30 on daily (gap into oversold is a trap, not a breakout)
        if len(daily) >= 15:
            _rsi_gap = daily["close"].diff().pipe(
                lambda d: 100 - 100 / (1 + d.clip(lower=0).rolling(14).mean() / (-d.clip(upper=0).rolling(14).mean()).replace(0, 1e-9))
            )
            if float(_rsi_gap.iloc[-1]) <= 30:
                return None
        confidence = min(0.73 + (gap_pct / 100), 0.95)
        # Near 52-week high = institutional momentum, not just an intraday gap
        if _near_52w_high(symbol, within_pct=10.0):
            confidence = min(confidence + 0.04, 0.95)
        # MDA change% confirmation: gap must still be intact at trade time
        q = _mda_quote(symbol)
        if q and q["change_pct"] < gap_pct * 0.4:
            # Live price has given back >60% of the gap — skip
            return None
        confidence = _sa_metrics_boost(symbol, confidence)
        return Signal(
            symbol, "buy", price, confidence,
            f"Gap up {gap_pct:.1f}% from ${prior_close:.2f} | volume x{vol_ratio:.1f}",
            "GapBreakout",
            atr_stop=atr14 * ATR_STOP_MULTIPLIER if atr14 > 0 else None,
        )


# ──────────────────────────────────────────────────────────────
# Market Structure Breakout Strategy
# ──────────────────────────────────────────────────────────────
class MarketStructureBreakoutStrategy:
    """Buy a breakout from higher-low compression under flat resistance."""

    def scan(self, symbol: str) -> Optional[Signal]:
        if not MARKET_STRUCTURE_BREAKOUT["enabled"]:
            return None
        try:
            daily = get_bars(symbol, "60d", "1d")
            intraday = get_bars(symbol, "1d", "1m")
            lookback = MARKET_STRUCTURE_BREAKOUT["lookback_bars"]
            if len(daily) < 10 or len(intraday) < 6:
                return None
            structure = daily.iloc[-min(lookback, len(daily)):]
            resistance = float(structure["high"].quantile(0.75))
            peaks = structure[structure["high"] >= resistance]["high"]
            if len(peaks) < 3 or (float(peaks.max()) - float(peaks.min())) / resistance * 100 > MARKET_STRUCTURE_BREAKOUT["resistance_tolerance_pct"]:
                return None
            lows = structure["low"].iloc[-5:].tolist()
            if len(lows) < 3 or not all(lows[i] > lows[i - 1] for i in range(1, len(lows))):
                return None
            recent_volume = float(intraday["volume"].iloc[-1])
            average_volume = float(intraday["volume"].rolling(20).mean().iloc[-1])
            price = float(intraday["close"].iloc[-1])
            if average_volume <= 0 or price <= resistance or recent_volume / average_volume < MARKET_STRUCTURE_BREAKOUT["min_volume_expansion"]:
                return None
            atr14 = _calc_atr14(daily)
            confidence = min(0.95, 0.78 + min(0.10, (recent_volume / average_volume - 2.5) * 0.03))
            return Signal(
                symbol, "buy", price, confidence,
                f"Market structure breakout above ${resistance:.2f} with higher lows and volume x{recent_volume / average_volume:.1f}",
                "MarketStructureBreakout", atr_stop=atr14 * ATR_STOP_MULTIPLIER if atr14 > 0 else None,
            )
        except Exception:
            return None


# ──────────────────────────────────────────────────────────────
# Opening Range Breakout (ORB) Strategy
# ──────────────────────────────────────────────────────────────
class ORBStrategy:
    """Opening Range Breakout: buy when price breaks above the first-N-minute high.

    Logic:
      - ORB window: first ORB['range_minutes'] after open (default 15 min)
      - Entry window: ORB['entry_start_min'] to ORB['entry_end_min'] after open
      - Signal: current price > ORB high + breakout requires volume surge
    """

    def scan(self, symbol: str) -> Optional[Signal]:
        now_et = datetime.datetime.now(ET)
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        minutes_since_open = (now_et - market_open).total_seconds() / 60

        # Only run during valid entry window
        if not (ORB["entry_start_min"] <= minutes_since_open <= ORB["entry_end_min"]):
            return None

        intraday = get_bars(symbol, "1d", "1m")
        if intraday.empty or len(intraday) < ORB["range_minutes"] + 5:
            return None

        # ORB = first range_minutes candles
        orb_bars  = intraday.iloc[: ORB["range_minutes"]]
        orb_high  = float(orb_bars["high"].max())
        orb_low   = float(orb_bars["low"].min())
        orb_range = orb_high - orb_low
        if orb_range <= 0:
            return None

        price = float(intraday["close"].iloc[-1])
        atr14 = _calc_atr14(intraday)
        buffer_distance = (
            atr14 * ORB["breakout_buffer_atr"]
            if atr14 > 0
            else price * ORB["breakout_buffer_pct"] / 100
        )

        # Require an adaptive volatility-scaled break above the range high.
        if price <= orb_high + buffer_distance:
            return None

        # Avoid breakouts that remain below the session's volume-weighted price.
        volume_total = float(intraday["volume"].sum())
        if ORB["require_above_vwap"] and volume_total > 0:
            typical_price = (intraday["high"] + intraday["low"] + intraday["close"]) / 3
            session_vwap = float((typical_price * intraday["volume"]).sum() / volume_total)
            if price <= session_vwap:
                return None

        # Volume confirmation: last 3 bars vs ORB avg
        vol_post = intraday["volume"].iloc[ORB["range_minutes"]:].iloc[-3:].mean()
        vol_orb  = orb_bars["volume"].mean()
        if vol_orb == 0:
            return None
        vol_ratio = vol_post / vol_orb

        range_pct = orb_range / price * 100
        range_span = max(ORB["range_vol_high_pct"] - ORB["range_vol_low_pct"], 0.01)
        range_factor = min(
            1.0,
            max(0.0, (range_pct - ORB["range_vol_low_pct"]) / range_span),
        )
        volume_threshold = ORB["volume_surge_min"] + (
            ORB["volume_surge_max"] - ORB["volume_surge_min"]
        ) * range_factor
        if vol_ratio < volume_threshold:
            return None

        # R-multiple: reward = price - orb_high, risk = orb_range
        r_multiple = (price - orb_high) / orb_range if orb_range > 0 else 0
        confidence = min(0.73 + r_multiple * 0.1, 0.95)

        return Signal(
            symbol, "buy", price, confidence,
            f"ORB breakout above ${orb_high:.2f} | range ${orb_range:.2f} | "
            f"buffer ${buffer_distance:.2f} | vol x{vol_ratio:.1f}/{volume_threshold:.1f}",
            "ORB",
            atr_stop=max(price - orb_low, orb_range),
        )


# ──────────────────────────────────────────────────────────────
# VWAP Reclaim Strategy
# ──────────────────────────────────────────────────────────────
class VWAPReclaimStrategy:
    """Price reclaims VWAP from below with accelerating volume — second-leg setup.

    Logic:
      - Calculate intraday VWAP from 1-min bars
      - Signal: prior candle closed below VWAP, current candle closes above
      - Volume in last 3 bars > VWAP_RECLAIM['volume_surge'] * session avg
      - RSI not yet overbought (room to run)
    """

    def scan(self, symbol: str) -> Optional[Signal]:
        bars = get_bars(symbol, "1d", "1m")
        if bars.empty or len(bars) < 30:
            return None

        # Calculate VWAP
        bars = bars.copy()
        bars["tp"]           = (bars["high"] + bars["low"] + bars["close"]) / 3
        bars["cum_vol"]      = bars["volume"].cumsum()
        bars["cum_tp_vol"]   = (bars["tp"] * bars["volume"]).cumsum()
        bars["vwap"]         = bars["cum_tp_vol"] / bars["cum_vol"].replace(0, float("nan"))

        cur_close  = float(bars["close"].iloc[-1])
        prev_close = float(bars["close"].iloc[-2])
        cur_vwap   = float(bars["vwap"].iloc[-1])
        prev_vwap  = float(bars["vwap"].iloc[-2])

        if pd.isna(cur_vwap) or pd.isna(prev_vwap):
            return None

        # Reclaim: was below, now above
        if not (prev_close < prev_vwap and cur_close > cur_vwap):
            return None

        # Volume surge
        vol_recent = bars["volume"].iloc[-3:].mean()
        vol_avg    = bars["volume"].mean()
        if vol_avg == 0:
            return None
        vol_ratio = vol_recent / vol_avg

        if vol_ratio < VWAP_RECLAIM["volume_surge"]:
            return None

        # RSI not overbought
        rsi = calc_rsi(bars["close"])
        if not rsi.empty and rsi.iloc[-1] > VWAP_RECLAIM["rsi_max"]:
            return None

        # Confidence: scales with how far above VWAP
        vwap_gap_pct = ((cur_close - cur_vwap) / cur_vwap) * 100
        confidence = min(0.68 + vwap_gap_pct * 0.05, 0.92)

        return Signal(
            symbol, "buy", cur_close, confidence,
            f"VWAP reclaim ${cur_vwap:.2f} | vol x{vol_ratio:.1f} | RSI {rsi.iloc[-1]:.0f}",
            "VWAPReclaim",
        )


# ──────────────────────────────────────────────────────────────
# Float Rotation Strategy
# ──────────────────────────────────────────────────────────────
# Module-level caches — persist across scan cycles and strategy instances
_float_info_cache: dict = {}   # {symbol: float_shares or 0.0 if unavailable}
_mcap_cache:        dict = {}  # {symbol: market_cap_float}

def _get_float_shares(symbol: str) -> Optional[float]:
    """Cached float share count — Mboum key-statistics primary, yfinance fallback.

    Mboum BASIC `key-statistics` provides floatShares and sharesOutstanding
    via the same RapidAPI key already in use for analyst data.  This replaces
    the yfinance dependency which is unreliable for real-time data.
    Caches session-level (float is stable intraday).
    """
    if symbol in _float_info_cache:
        v = _float_info_cache[symbol]
        return v if v > 0 else None
    # ── Primary: Mboum key-statistics ──────────────────────────────────────────
    try:
        import requests as _req
        _mboum_key = "e8623e01d7msh66804a8ade71af0p120d0ejsn20ee85188848"
        r = _req.get(
            "https://mboum-finance.p.rapidapi.com/v1/markets/stock/modules",
            params={"symbol": symbol, "module": "default-key-statistics"},
            headers={"x-rapidapi-host": "mboum-finance.p.rapidapi.com", "x-rapidapi-key": _mboum_key},
            timeout=6,
        )
        if r.status_code == 200:
            body = r.json().get("body", {})
            shares = body.get("floatShares", {}).get("raw") or body.get("sharesOutstanding", {}).get("raw")
            if shares and float(shares) > 0:
                _float_info_cache[symbol] = float(shares)
                return float(shares)
    except Exception:
        pass
    
    _float_info_cache[symbol] = 0.0   # cache miss
    return None


class FloatRotationStrategy:
    """Low-float stock with volume > X% of float = stock is 'in play'.

    Logic:
    - Fetch float shares from Alpaca or fallback (cached per session)
      - If float < FLOAT_ROTATION['max_float_shares']
        and today's volume already > float * FLOAT_ROTATION['volume_float_ratio']
        and price is up > FLOAT_ROTATION['min_price_up_pct'] on the day
      => Stock is rotating its entire float: extreme squeeze potential
    """

    def _get_float(self, symbol: str) -> Optional[float]:
        return _get_float_shares(symbol)

    def scan(self, symbol: str) -> Optional[Signal]:
        shares_float = self._get_float(symbol)
        if shares_float is None or shares_float > FLOAT_ROTATION["max_float_shares"]:
            return None

        intraday = get_bars(symbol, "1d", "1m")
        if intraday.empty or len(intraday) < 10:
            return None

        price     = float(intraday["close"].iloc[-1])
        open_px   = float(intraday["open"].iloc[0])
        day_vol   = float(intraday["volume"].sum())
        price_chg = ((price - open_px) / open_px) * 100 if open_px > 0 else 0

        if price_chg < FLOAT_ROTATION["min_price_up_pct"]:
            return None

        vol_float_ratio = day_vol / shares_float
        if vol_float_ratio < FLOAT_ROTATION["volume_float_ratio"]:
            return None

        float_m    = shares_float / 1_000_000
        confidence = min(0.72 + vol_float_ratio * 0.1, 0.96)
        daily_bars = get_bars(symbol, "5d", "1d")
        atr14      = _calc_atr14(daily_bars) if not daily_bars.empty and len(daily_bars) >= 5 else 0.0
        return Signal(
            symbol, "buy", price, confidence,
            f"Float rotation: {vol_float_ratio:.1f}x float ({float_m:.1f}M) | +{price_chg:.1f}% day",
            "FloatRotation",
            atr_stop=atr14 * ATR_STOP_MULTIPLIER if atr14 > 0 else None,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Early Momentum / Opening Strategies
# ─────────────────────────────────────────────────────────────────────────────

class PreMarketMomentumStrategy:
    """Fires 7:00–10:00 AM ET when a stock shows a gap ≥3%, strong pre-market
    volume (≥15% of average daily vol), and an upward PM price trend.
    Classic KOD / EEIQ style — catch the runner before the open.
    """

    def scan(self, symbol: str) -> Optional[Signal]:
        now_et    = datetime.datetime.now(ET)
        now_float = now_et.hour + now_et.minute / 60.0
        # Valid window: 7:00 AM to configured end hour (default 10:00)
        entry_end = PRE_MARKET_MOMENTUM.get("entry_window_end", 10.0)
        if not (7.0 <= now_float < entry_end):
            return None

        pm_bars = get_premarket_bars(symbol)
        if pm_bars.empty or "time" not in pm_bars.columns:
            return None

        # Pre-market bars only (before 9:30 ET)
        pm_only = pm_bars[
            (pm_bars["time"].dt.hour < 9)
            | ((pm_bars["time"].dt.hour == 9) & (pm_bars["time"].dt.minute < 30))
        ].copy()
        if len(pm_only) < PRE_MARKET_MOMENTUM["pm_trend_bars"]:
            return None

        daily = get_bars(symbol, "5d", "1d")
        if daily.empty or len(daily) < 2:
            return None
        prior_close   = float(daily["close"].iloc[-2])
        avg_daily_vol = float(daily["volume"].iloc[:-1].mean())
        if prior_close <= 0 or avg_daily_vol <= 0:
            return None

        pm_price = float(pm_only["close"].iloc[-1])
        gap_pct  = ((pm_price - prior_close) / prior_close) * 100
        if gap_pct < PRE_MARKET_MOMENTUM["min_gap_pct"]:
            return None

        pm_vol     = float(pm_only["volume"].sum())
        pm_vol_pct = (pm_vol / avg_daily_vol) * 100
        if pm_vol_pct < PRE_MARKET_MOMENTUM["pm_vol_pct_of_avg"]:
            return None

        # Last N PM bars must show upward trend (final close > initial close)
        n      = PRE_MARKET_MOMENTUM["pm_trend_bars"]
        trend  = pm_only["close"].values[-n:]
        if trend[-1] <= trend[0]:
            return None

        atr14      = _calc_atr14(daily)
        confidence = min(0.68 + (gap_pct / 50) * 0.15 + (pm_vol_pct / 100) * 0.10, 0.94)
        return Signal(
            symbol, "buy", pm_price, confidence,
            f"Pre-mkt momentum: gap +{gap_pct:.1f}% | PM vol {pm_vol_pct:.0f}% of avg daily",
            "PreMarketMomentum",
            atr_stop=atr14 * ATR_STOP_MULTIPLIER if atr14 > 0 else None,
        )


class OpeningBellSurgeStrategy:
    """Fires 9:30–9:45 AM ET when the first N 1-min bars show volume ≥4×
    the expected baseline AND price is up ≥2% from the open.
    Catches explosive gap-and-go moves right at the bell.
    """

    def scan(self, symbol: str) -> Optional[Signal]:
        now_et       = datetime.datetime.now(ET)
        market_open  = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        mins_since   = (now_et - market_open).total_seconds() / 60.0
        window       = OPENING_BELL_SURGE["window_min"]
        if not (0.0 <= mins_since <= window):
            return None

        intraday = get_bars(symbol, "1d", "1m")
        surge_n  = OPENING_BELL_SURGE["surge_bars"]
        if intraday.empty or len(intraday) < surge_n:
            return None

        open_bars  = intraday.iloc[:surge_n]
        open_vol   = float(open_bars["volume"].sum())
        open_px    = float(open_bars["open"].iloc[0])
        cur_price  = float(intraday["close"].iloc[-1])

        avg_1min_vol = float(intraday["volume"].mean())
        if avg_1min_vol <= 0:
            return None

        vol_ratio = open_vol / (avg_1min_vol * surge_n)
        if vol_ratio < OPENING_BELL_SURGE["vol_multiplier"]:
            return None

        if open_px <= 0:
            return None
        price_up_pct = ((cur_price - open_px) / open_px) * 100
        if price_up_pct < OPENING_BELL_SURGE["min_price_up_pct"]:
            return None

        # First candle must be bullish
        if open_bars["close"].iloc[0] < open_bars["open"].iloc[0]:
            return None

        daily = get_bars(symbol, "5d", "1d")
        atr14 = _calc_atr14(daily) if not daily.empty and len(daily) >= 5 else 0.0
        confidence = min(0.73 + (vol_ratio - OPENING_BELL_SURGE["vol_multiplier"]) * 0.03, 0.95)
        return Signal(
            symbol, "buy", cur_price, confidence,
            f"Opening bell surge: vol x{vol_ratio:.1f} first {surge_n} bars | +{price_up_pct:.1f}% from open",
            "OpeningBellSurge",
            atr_stop=atr14 * ATR_STOP_MULTIPLIER if atr14 > 0 else None,
        )


class PMHighBreakoutStrategy:
    """Fires 9:31–10:30 AM ET when the regular session price breaks out
    above the pre-market high with volume confirmation.  The breakout must
    be fresh (prior bar still ≤ PM high) to avoid chasing old moves.
    """

    def scan(self, symbol: str) -> Optional[Signal]:
        now_et      = datetime.datetime.now(ET)
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        mins_since  = (now_et - market_open).total_seconds() / 60.0
        window      = PM_HIGH_BREAKOUT["entry_window_min"]
        if not (1.0 <= mins_since <= window):
            return None

        pm_bars = get_premarket_bars(symbol)
        if pm_bars.empty or "time" not in pm_bars.columns:
            return None
        pm_only = pm_bars[
            (pm_bars["time"].dt.hour < 9)
            | ((pm_bars["time"].dt.hour == 9) & (pm_bars["time"].dt.minute < 30))
        ]
        if len(pm_only) < 3:
            return None
        pm_high = float(pm_only["high"].max())
        if pm_high <= 0:
            return None

        intraday = get_bars(symbol, "1d", "1m")
        if intraday.empty or len(intraday) < 3:
            return None

        cur_price  = float(intraday["close"].iloc[-1])
        prev_price = float(intraday["close"].iloc[-2])

        breakout_level = pm_high * (1 + PM_HIGH_BREAKOUT["breakout_buffer_pct"] / 100)
        # Require fresh breakout: current bar above buffer, prior bar not already extended
        if not (cur_price >= breakout_level and prev_price <= pm_high * 1.005):
            return None

        vol_recent = float(intraday["volume"].iloc[-3:].mean())
        vol_avg    = float(intraday["volume"].mean())
        if vol_avg <= 0:
            return None
        vol_ratio = vol_recent / vol_avg
        if vol_ratio < PM_HIGH_BREAKOUT["volume_surge"]:
            return None

        daily = get_bars(symbol, "5d", "1d")
        if daily.empty or len(daily) < 2:
            return None
        prior_close = float(daily["close"].iloc[-2])
        gap_pct     = ((pm_high - prior_close) / prior_close) * 100 if prior_close > 0 else 0.0

        atr14      = _calc_atr14(daily)
        confidence = min(0.73 + (vol_ratio - 1.5) * 0.04, 0.94)
        return Signal(
            symbol, "buy", cur_price, confidence,
            f"PM high breakout: cleared ${pm_high:.2f} | gap {gap_pct:+.1f}% | vol x{vol_ratio:.1f}",
            "PMHighBreakout",
            atr_stop=atr14 * ATR_STOP_MULTIPLIER if atr14 > 0 else None,
        )


class EarlySqueezeDetector:
    """Fires 9:30–10:15 AM ET for low-float stocks showing gap + projected
    RVOL >4× + price above VWAP + RSI not yet overbought.
    Designed to catch KOD / EEIQ / IMTE style small-float squeeze plays.
    """

    def _get_float(self, symbol: str) -> Optional[float]:
        return _get_float_shares(symbol)

    def scan(self, symbol: str) -> Optional[Signal]:
        now_et      = datetime.datetime.now(ET)
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        mins_since  = (now_et - market_open).total_seconds() / 60.0
        if not (0.0 <= mins_since <= EARLY_SQUEEZE["entry_window_min"]):
            return None

        shares_float = self._get_float(symbol)
        if shares_float is None or shares_float > EARLY_SQUEEZE["max_float_shares"]:
            return None

        daily = get_bars(symbol, "5d", "1d")
        if daily.empty or len(daily) < 2:
            return None
        prior_close   = float(daily["close"].iloc[-2])
        avg_daily_vol = float(daily["volume"].iloc[:-1].mean())
        if prior_close <= 0 or avg_daily_vol <= 0:
            return None

        intraday = get_bars(symbol, "1d", "1m")
        if intraday.empty or len(intraday) < 5:
            return None

        open_px   = float(intraday["open"].iloc[0])
        cur_price = float(intraday["close"].iloc[-1])
        gap_pct   = ((open_px - prior_close) / prior_close) * 100
        if gap_pct < EARLY_SQUEEZE["min_gap_pct"]:
            return None

        # Projected full-day RVOL
        day_vol       = float(intraday["volume"].sum())
        elapsed_frac  = max(mins_since / 390.0, 0.005)
        projected_vol = day_vol / elapsed_frac
        rvol          = projected_vol / avg_daily_vol
        if rvol < EARLY_SQUEEZE["rvol_multiplier"]:
            return None

        # VWAP check — price must be above session VWAP
        df       = intraday.copy()
        df["tp"] = (df["high"] + df["low"] + df["close"]) / 3
        cum_tpv  = (df["tp"] * df["volume"]).cumsum()
        cum_vol  = df["volume"].cumsum().replace(0, float("nan"))
        vwap_now = float((cum_tpv / cum_vol).iloc[-1])
        if cur_price < vwap_now:
            return None

        # RSI check — not yet overbought
        rsi = calc_rsi(df["close"])
        if not rsi.empty and not pd.isna(rsi.iloc[-1]):
            if rsi.iloc[-1] > EARLY_SQUEEZE["rsi_max"]:
                return None

        float_m    = shares_float / 1_000_000
        atr14      = _calc_atr14(daily)
        confidence = min(0.75 + (rvol / 10) * 0.04 + (gap_pct / 50) * 0.08, 0.96)
        return Signal(
            symbol, "buy", cur_price, confidence,
            f"Early squeeze: float {float_m:.1f}M | gap +{gap_pct:.1f}% | RVOL x{rvol:.1f} projected | above VWAP",
            "EarlySqueeze",
            atr_stop=atr14 * ATR_STOP_MULTIPLIER if atr14 > 0 else None,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Momentum Continuation Strategy
# ──────────────────────────────────────────────────────────────────────────────
class MomentumContinuationStrategy:
    """Buys STRENGTH (not pullbacks) in bull tape: names spiking on high
    relative volume that are breaking/at session high above the session open.
    Complements the pullback/mean-reversion inventory (e.g. SUNB 2.9x, SKUU 1.8x
    high-RVOL spikes that never pull back to an EMA)."""

    def scan(self, symbol: str) -> Optional[Signal]:
        def reject(reason: str) -> None:
            log.debug("MomentumContinuation %s rejected: %s", symbol, reason)

        try:
            if not MOMENTUM_CONTINUATION["enabled"]:
                return None
            if symbol in _INVERSE_ETFS:
                return None

            # Regular hours only — skip premarket and the final minutes
            now_et = datetime.datetime.now(ET)
            mins_of_day = now_et.hour * 60 + now_et.minute
            if mins_of_day < 9 * 60 + 30 or mins_of_day > 15 * 60 + 45:
                return None
            market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            elapsed_min = max((now_et - market_open).total_seconds() / 60.0, 1.0)

            intraday = get_bars(symbol, "1d", "1m")
            daily    = get_bars(symbol, "20d", "1d")
            if intraday.empty or daily.empty or len(daily) < 2:
                reject("missing data")
                return None

            # Data providers can return multiple dates and extended-hours bars
            # for a 1d/1m request. Momentum measurements use today's 7 AM ET
            # premarket-to-current window only.
            timestamp_values = intraday["time"] if "time" in intraday.columns else intraday.index
            timestamps = pd.Series(pd.to_datetime(timestamp_values, errors="coerce"), index=intraday.index)
            if timestamps.dt.tz is not None:
                timestamps = timestamps.dt.tz_convert(ET)
            else:
                timestamps = timestamps.dt.tz_localize(ET)
            minutes = timestamps.dt.hour * 60 + timestamps.dt.minute
            session_mask = (
                (timestamps.dt.date == now_et.date())
                & (minutes >= 7 * 60)
                & (minutes <= 16 * 60)
            )
            intraday = intraday.loc[session_mask]
            if intraday.empty:
                reject("missing current-day session data")
                return None

            cur_close    = float(intraday["close"].iloc[-1])
            session_open = float(intraday["open"].iloc[0])
            if session_open <= 0:
                session_open = float(daily["close"].iloc[-2])   # prior close fallback
            if session_open <= 0:
                reject("missing session open")
                return None

            # Must be up >= min_price_up_pct above the session open
            price_up_pct = (cur_close - session_open) / session_open * 100
            if price_up_pct < MOMENTUM_CONTINUATION["min_price_up_pct"]:
                reject(f"insufficient move ({price_up_pct:.1f}% above session open)")
                return None

            # Must be breaking / at the session high (last break_lookback_min minutes)
            lookback     = min(int(MOMENTUM_CONTINUATION["break_lookback_min"]), len(intraday))
            recent_window = intraday.iloc[-lookback:]
            session_high = float(recent_window["high"].max())
            session_low = float(recent_window["low"].min())
            near_recent_high = cur_close >= session_high * 0.998

            # RVOL: intraday volume so far vs elapsed fraction of avg daily volume
            day_vol       = float(intraday["volume"].sum())
            avg_daily_vol = float(daily["volume"].iloc[:-1].mean())
            if avg_daily_vol <= 0:
                reject("missing average daily volume")
                return None
            elapsed_frac = max(elapsed_min / 390.0, 0.005)
            rvol         = day_vol / (avg_daily_vol * elapsed_frac)
            if rvol < MOMENTUM_CONTINUATION["min_rvol"]:
                reject(f"insufficient RVOL ({rvol:.1f}x)")
                return None

            def momentum_confirmation() -> tuple[float, float] | None:
                macd = calc_macd(intraday["close"])
                rsi = calc_rsi(intraday["close"])
                if len(macd["hist"]) < 2 or rsi.empty:
                    reject("insufficient MACD/RSI data")
                    return None
                macd_hist = float(macd["hist"].iloc[-1])
                previous_macd_hist = float(macd["hist"].iloc[-2])
                current_rsi = float(rsi.iloc[-1])
                if not all(np.isfinite(value) for value in (macd_hist, previous_macd_hist, current_rsi)):
                    reject("invalid MACD/RSI data")
                    return None
                if macd_hist <= 0 or macd_hist < previous_macd_hist:
                    reject(f"weak MACD histogram ({macd_hist:.4f})")
                    return None
                if not 45 <= current_rsi <= 82:
                    reject(f"RSI outside continuation range ({current_rsi:.1f})")
                    return None
                return macd_hist, current_rsi

            # Keep the original breakout path preferred. A high-RVOL pullback can
            # reclaim only above the filtered session VWAP and range midpoint.
            if not near_recent_high:
                reject(f"not near recent high ({cur_close:.2f} vs {session_high:.2f})")
                volume_total = float(intraday["volume"].sum())
                if rvol < 3.0 or volume_total <= 0:
                    return None
                typical_price = (intraday["high"] + intraday["low"] + intraday["close"]) / 3
                session_vwap = float((typical_price * intraday["volume"]).sum() / volume_total)
                range_midpoint = (session_high + session_low) / 2
                if (cur_close <= session_vwap or cur_close <= range_midpoint):
                    return None

                confirmation = momentum_confirmation()
                if confirmation is None:
                    return None
                macd_hist, current_rsi = confirmation

                conf = 0.78
                conf += min((rvol - 3.0) * 0.02, 0.04)
                conf += min((price_up_pct - MOMENTUM_CONTINUATION["min_price_up_pct"]) * 0.005, 0.02)
                conf = min(_sa_metrics_boost(symbol, conf), 0.84)
                atr14 = _calc_atr14(daily)
                return Signal(
                    symbol, "buy", cur_close, round(conf, 2),
                    f"Momentum continuation high-RVOL pullback/reclaim rvol={rvol:.1f}x "
                    f"up={price_up_pct:.1f}% above VWAP and range midpoint "
                    f"MACD hist={macd_hist:.4f} RSI={current_rsi:.1f}",
                    "MomentumContinuationPullback",
                    atr_stop=atr14 * ATR_STOP_MULTIPLIER if atr14 > 0 else None,
                )

            confirmation = momentum_confirmation()
            if confirmation is None:
                return None
            macd_hist, current_rsi = confirmation

            conf  = 0.74
            conf += min((rvol - MOMENTUM_CONTINUATION["min_rvol"]) * 0.05, 0.12)
            conf += min((price_up_pct - MOMENTUM_CONTINUATION["min_price_up_pct"]) * 0.01, 0.06)
            conf  = min(conf, 0.95)
            conf  = _sa_metrics_boost(symbol, conf)

            atr14 = _calc_atr14(daily)
            return Signal(
                symbol, "buy", cur_close, round(conf, 2),
                f"Momentum continuation rvol={rvol:.1f}x up={price_up_pct:.1f}% break of session high "
                f"MACD hist={macd_hist:.4f} RSI={current_rsi:.1f}",
                "MomentumContinuation",
                atr_stop=atr14 * ATR_STOP_MULTIPLIER if atr14 > 0 else None,
            )
        except Exception:
            return None


# ──────────────────────────────────────────────────────────────────────────────
# Bear Breakdown Strategy
# ──────────────────────────────────────────────────────────────────────────────
class BearBreakdownStrategy:
    """Short-entry: daily breakdown below 20-SMA + 10-day low with volume spike.

    Only fires in bear regime (SPY < 200SMA). Inverse of TrendBreaker.

    Pattern:
      - Bear regime + shorts enabled
      - Price below 50SMA (macro downtrend context)
      - Was above/touching 20SMA for \u22652 of last 10 days (fresh, not exhausted break)
      - Today closes below 20SMA AND at/below 10-day low (confirmed breakdown)
      - Volume spike \u22651.5\u00d7 20-day avg (distribution volume)
      - RSI 25-55: momentum still declining, not yet snap-back oversold
    """

    def scan(self, symbol: str) -> Optional[Signal]:
        if LONG_ONLY_MODE or _is_bull_regime():
            return None
        # Never short inverse ETFs — they're already bearish instruments
        if symbol in _INVERSE_ETFS:
            return None
        # Daily bar only meaningful after 10 AM ET
        if datetime.datetime.now(ET).hour < 10:
            return None

        daily = get_bars(symbol, "60d", "1d")
        if daily.empty or len(daily) < 25:
            return None

        closes  = daily["close"]
        volumes = daily["volume"]
        sma20   = closes.rolling(20).mean()
        sma50   = closes.rolling(50).mean()

        price     = float(closes.iloc[-1])
        sma20_now = float(sma20.iloc[-1])
        sma50_now = float(sma50.iloc[-1])

        if sma20_now <= 0 or sma50_now <= 0:
            return None

        # Macro context: price must be below 50SMA
        if price >= sma50_now:
            return None

        # Today broke below 20SMA
        if price >= sma20_now:
            return None

        # Was above/at 20SMA for at least N of the last 10 days (fresh breakdown)
        recent_closes = closes.iloc[-11:-1]
        recent_sma20  = sma20.iloc[-11:-1]
        above_count   = int((recent_closes.values >= recent_sma20.values).sum())
        if above_count < BEAR_BREAKDOWN["above_sma_min_days"]:
            return None

        # Also broke below 10-day low (confirms continuation, not just a 20SMA touch)
        low_10d = float(daily["low"].iloc[-11:-1].min())
        buffer_pct = float(BEAR_BREAKDOWN.get("breakdown_buffer_pct", 0.20))
        if price > low_10d * (1 + (buffer_pct / 100.0)):
            return None

        # Volume spike vs 20-day avg
        vol_today = float(volumes.iloc[-1])
        vol_avg   = float(volumes.iloc[-21:-1].mean())
        if vol_avg <= 0:
            return None
        vol_ratio = vol_today / vol_avg
        if vol_ratio < BEAR_BREAKDOWN["volume_multiplier"]:
            return None

        # RSI: declining, not already oversold
        rsi      = calc_rsi(closes, period=14)
        rsi_now  = float(rsi.iloc[-1])
        rsi_prev = float(rsi.iloc[-2])
        if rsi_now >= BEAR_BREAKDOWN["rsi_max"] or rsi_now < BEAR_BREAKDOWN["rsi_min"]:
            return None

        atr14      = _calc_atr14(daily)
        confidence = 0.78 + min((vol_ratio - 1.5) * 0.03, 0.10)
        if rsi_now < rsi_prev:       # RSI still falling — extra confirmation
            confidence += 0.02
        confidence = round(min(confidence, 0.92), 2)

        return Signal(
            symbol, "short", price, confidence,
            f"Bear breakdown: below 20SMA + 10d low | vol x{vol_ratio:.1f} | RSI {rsi_now:.0f}",
            "BearBreakdown",
            atr_stop=atr14 * ATR_STOP_MULTIPLIER if atr14 > 0 else None,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Power of 3 Strategy  (ICT: Accumulation → Manipulation → Distribution)
# ──────────────────────────────────────────────────────────────────────────────
class PowerOf3Strategy:
    """ICT Power of 3: tight morning accumulation → sweep below the range low
    (manipulation) → recovery and breakout above morning high (distribution).

    Entry window: 11:30 AM–2:30 PM ET (pattern must have fully formed).
    Stop: just below the manipulation low — very tight relative to target.
    Target: morning range high (distribution leg).
    """

    def scan(self, symbol: str) -> Optional[Signal]:
        now_et      = datetime.datetime.now(ET)
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        mins_since  = (now_et - market_open).total_seconds() / 60.0

        # Pattern needs ≥120 min to form; stale after 2:30 PM
        if not (120.0 <= mins_since <= 300.0):
            return None

        bars = get_bars(symbol, "1d", "1m")
        if bars.empty or len(bars) < 125:
            return None

        # ── Accumulation: first 120 bars (9:30–11:30) ────────────────────
        accum  = bars.iloc[:120]
        a_high = float(accum["high"].max())
        a_low  = float(accum["low"].min())
        if a_low <= 0:
            return None
        range_pct = (a_high - a_low) / a_low * 100
        if range_pct > 3.0:       # must be a tight consolidation (≤3%)
            return None

        # ── Post-accumulation bars ────────────────────────────────────────
        post = bars.iloc[120:]
        if len(post) < 3:
            return None

        cur_close  = float(bars["close"].iloc[-1])
        prev_close = float(bars["close"].iloc[-2])

        # ── Manipulation: price swept below accumulation low ──────────────
        post_low = float(post["low"].min())
        if post_low >= a_low:      # no sweep — pattern not triggered
            return None

        # ── Distribution entry ────────────────────────────────────────────
        # Case A: fresh reclaim of morning low (prev ≤ a_low, cur > a_low)
        # Case B: already breaking above morning high (distribution fully underway)
        fresh_reclaim    = prev_close <= a_low   and cur_close >  a_low
        breaking_high    = prev_close <= a_high * 1.002 and cur_close > a_high
        if not (fresh_reclaim or breaking_high):
            return None

        # ── Volume: post-accum bars must be livelier than accumulation avg ─
        vol_accum_avg = float(accum["volume"].mean())
        vol_recent    = float(post["volume"].iloc[-3:].mean())
        if vol_accum_avg <= 0:
            return None
        vol_ratio = vol_recent / vol_accum_avg
        if vol_ratio < 1.5:
            return None

        # ── RSI not yet overbought ────────────────────────────────────────
        rsi = calc_rsi(bars["close"])
        if not rsi.empty and not pd.isna(rsi.iloc[-1]):
            if rsi.iloc[-1] > 75:
                return None

        daily = get_bars(symbol, "5d", "1d")
        atr14 = _calc_atr14(daily) if not daily.empty and len(daily) >= 5 else 0.0

        # Tight stop: just below the manipulation sweep low
        stop_dist  = max(cur_close - post_low, atr14 * 0.3)
        stage      = "distribution" if breaking_high else "reclaim"
        base_conf  = 0.79 if breaking_high else 0.74
        confidence = round(min(base_conf + max(vol_ratio - 1.5, 0) * 0.03, 0.92), 2)

        return Signal(
            symbol, "buy", cur_close, confidence,
            f"Power of 3 {stage}: accum {range_pct:.1f}% range | sweep ${post_low:.2f} | vol x{vol_ratio:.1f}",
            "PowerOf3",
            atr_stop=stop_dist if stop_dist > 0 else None,
        )


# ──────────────────────────────────────────────────────────────
# Regression Trendline Breakout
# ──────────────────────────────────────────────────────────────
class TrendlineBreakoutStrategy:
    """Trade confirmed, high-volume breaks of pivot-regression trendlines."""

    def scan(self, symbol: str) -> Optional[Signal]:
        bars = get_bars(symbol, "1d", "1m")
        if TRENDLINE_BREAKOUT["engine"] == "adaptive":
            return self._scan_adaptive(symbol, bars)
        minimum_bars = (
            TRENDLINE_BREAKOUT["left_bars"]
            + TRENDLINE_BREAKOUT["right_bars"]
            + 1
        )
        if bars.empty or len(bars) < max(20, minimum_bars):
            return None

        try:
            analysis = detect_trendline_breakouts(
                bars,
                left_bars=TRENDLINE_BREAKOUT["left_bars"],
                right_bars=TRENDLINE_BREAKOUT["right_bars"],
                pivot_count=TRENDLINE_BREAKOUT["pivot_count"],
                volume_multiplier=TRENDLINE_BREAKOUT["volume_multiplier"],
                atr_offset=TRENDLINE_BREAKOUT["atr_offset"],
                risk_reward_ratio=TRENDLINE_BREAKOUT["risk_reward_ratio"],
            )
        except (TypeError, ValueError) as error:
            log.warning(f"Trendline breakout skipped for {symbol}: {error}")
            return None

        latest = analysis.iloc[-1]
        direction = str(latest["signal"])
        if direction not in ("buy", "sell") or pd.isna(latest["atr"]):
            return None

        price = float(latest["close"])
        trendline = float(latest["resistance_line"] if direction == "buy" else latest["support_line"])
        stop_loss = float(latest["stop_loss"])
        take_profit = float(latest["take_profit"])
        stop_distance = abs(price - stop_loss)
        if not np.isfinite(trendline) or stop_distance <= 0:
            return None

        volume_sma = float(latest["volume_sma"])
        volume_ratio = float(latest["volume"]) / volume_sma if volume_sma > 0 else 0.0
        confidence = min(0.75 + max(volume_ratio - 1.5, 0.0) * 0.05, 0.92)
        line_name = "resistance" if direction == "buy" else "support"
        action = "buy" if direction == "buy" else "short"
        return Signal(
            symbol, action, price, round(confidence, 2),
            f"Trendline {direction} breakout {line_name} ${trendline:.2f} | "
            f"vol x{volume_ratio:.1f} | stop ${stop_loss:.2f} | target ${take_profit:.2f}",
            "TrendlineBreakout",
            atr_stop=stop_distance,
        )

    @staticmethod
    def _scan_adaptive(symbol: str, bars: pd.DataFrame) -> Optional[Signal]:
        config = TrendlineConfig(
            primary_lookback=TRENDLINE_BREAKOUT["primary_lookback"],
            bars_from_edge=TRENDLINE_BREAKOUT["bars_from_edge"],
            breakout_threshold=TRENDLINE_BREAKOUT["breakout_threshold"],
            breakout_confirm_bars=TRENDLINE_BREAKOUT["breakout_confirm_bars"],
            max_history_lines=TRENDLINE_BREAKOUT["max_history_lines"],
        )
        if bars.empty or len(bars) <= config.min_bars:
            return None
        try:
            snapshot = AutoTrendline(config).latest(bars)
        except (TypeError, ValueError):
            return None

        direction = "buy" if snapshot["resistance_break"] else (
            "short" if snapshot["support_break"] else ""
        )
        if not direction:
            return None
        price = float(snapshot["close"])
        line = snapshot["resistance"] if direction == "buy" else snapshot["support"]
        line_price = (snapshot["resistance_price"] if direction == "buy"
                      else snapshot["support_price"])
        atr_stop = _calc_atr14(bars)
        if line is None or not np.isfinite(atr_stop) or atr_stop <= 0:
            return None
        side = "resistance" if direction == "buy" else "support"
        return Signal(
            symbol, direction, price, 0.75,
            f"Adaptive trendline {direction} breakout {side} ${line_price:.2f} | "
            f"stop ${atr_stop:.2f}",
            "TrendlineBreakout",
            atr_stop=float(atr_stop),
        )


def get_strategy_instances(bull_regime: bool = True):
    """Return instantiated strategy objects for the current market regime."""
    strategies = [
        GapBreakoutStrategy(),
        MarketStructureBreakoutStrategy(),
        ORBStrategy(),
        VWAPReclaimStrategy(),
        FloatRotationStrategy(),
        MomentumStrategy(),
        TechnicalStrategy(),
        SweepeaStrategy(),
        TrendBreakerStrategy(),
        SentimentStrategy(),
        PreMarketMomentumStrategy(),
        OpeningBellSurgeStrategy(),
        PMHighBreakoutStrategy(),
        EarlySqueezeDetector(),
        MomentumContinuationStrategy(),
        PowerOf3Strategy(),
        TrendlineBreakoutStrategy(),
    ]

    # Only add bear-regime strategies when we are actually in a bear regime
    if not bull_regime:
        strategies.append(BearBreakdownStrategy())
    return strategies

