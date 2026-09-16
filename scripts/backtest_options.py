"""
backtest_options.py
-------------------
Simplified historical backtest for ApexTrader options strategies.

Simulates BreakoutRetest and MeanReversion strategy entries on the OPTIONS_ELIGIBLE_UNIVERSE
using yfinance historical price data, then prices hypothetical options via the
Black-Scholes model (no actual options chain data required for backtesting).

Assumptions:
  - Long calls: buy 0.40-delta ATM+5% strike call; entry at BS theoretical mid
  - Long puts:  buy 0.35-delta ATM-5% strike put; entry at BS theoretical mid
  - Hold rules: exit at +50% profit OR -40% loss, or DTE=1.
  - IV proxy: realized 30-day historical vol × 1.15 (options usually trade at premium)
  - IV rank: current 30d HV vs trailing 252-day HV range
  - Allocation: 15% of initial capital, max 3 positions, sized equally.

Usage:
    python scripts/backtest_options.py [--symbols AAPL TSLA NVDA] [--start 2024-01-01] [--end 2024-12-31]
"""

import sys
import argparse
import datetime
import math
from pathlib import Path
from typing import List, Optional, Tuple
import warnings
warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")


import pandas as pd
from engine.options._options_today import _calc_iv_rank
from engine.utils import get_bars

from engine.config import (
    HIGH_SHORT_FLOAT_STOCKS,
    OPTIONS_ALLOCATION_PCT,
    OPTIONS_MAX_POSITIONS,
    OPTIONS_DTE_MIN,
    OPTIONS_DTE_MAX,
    OPTIONS_PROFIT_TARGET_PCT,
    OPTIONS_STOP_LOSS_PCT,
    OPTIONS_MIN_SIGNAL_CONFIDENCE,
    MEMORY_WARN_MB,
    get_options_universe,
)

# Inverse ETFs profit from market declines — their CALLS are the bear play.
# Must match engine/strategies.py definition.
_INVERSE_ETFS: frozenset = frozenset({
    "SQQQ", "SPXU", "UVXY", "TZA", "FAZ", "SOXS", "LABD", "DUST",
})


# ── Black-Scholes Pricing ────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Approximate normal CDF using Abramowitz & Stegun."""
    a = abs(x)
    t = 1.0 / (1.0 + 0.2316419 * a)
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    result = 1.0 - (1 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * a * a) * poly
    return result if x >= 0 else 1.0 - result


def _bs_price(spot: float, strike: float, dte: int, iv: float, rate: float = 0.05, call: bool = True) -> float:
    """Black-Scholes option price.
    iv: annual implied volatility as fraction (e.g. 0.30 = 30%)
    """
    T = dte / 365.0
    if T <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    try:
        d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
        d2 = d1 - iv * math.sqrt(T)
        if call:
            price = spot * _norm_cdf(d1) - strike * math.exp(-rate * T) * _norm_cdf(d2)
        else:
            price = strike * math.exp(-rate * T) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        return max(0.0, price)
    except Exception:
        return 0.0


def _bs_delta(spot: float, strike: float, dte: int, iv: float, rate: float = 0.05, call: bool = True) -> float:
    T = dte / 365.0
    if T <= 0 or iv <= 0:
        return 0.5 if call else -0.5
    try:
        d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
        return _norm_cdf(d1) if call else _norm_cdf(d1) - 1.0
    except Exception:
        return 0.5 if call else -0.5


def _pick_strike(spot: float, call: bool, target_delta: float, iv: float, dte: int) -> float:
    """Find a strike price (to nearest $1) whose BS delta ≈ target_delta."""
    best_strike = spot
    best_dist   = 999.0
    step = max(0.5, spot * 0.005)
    low  = spot * 0.70
    high = spot * 1.30

    s = low
    while s <= high:
        d = abs(_bs_delta(spot, s, dte, iv, call=call))
        dist = abs(d - target_delta)
        if dist < best_dist:
            best_dist   = dist
            best_strike = s
        s += step
    return round(best_strike, 0)


def _bear_put_signal(daily: pd.DataFrame, idx: int, is_bear: bool) -> bool:
    pass

# --- Unified Signal Logic for Backtest ---
def _signal_for_strategy(strategy: str, daily: pd.DataFrame, idx: int, is_bear: bool = False) -> bool:
    """Unified signal logic for all strategies used in backtest."""
    if strategy == "MeanReversion":
        return _mean_reversion_signal(daily, idx)
    if strategy == "TrendPullbackSpread":
        return _trend_pullback_signal(daily, idx)
    if strategy == "BreakoutRetest":
        return _breakout_retest_signal(daily, idx)
    if strategy == "IronCondor":
        # Neutral: price between 20 and 50 EMA, IV rank >= 40
        if idx < 55:
            return False
        df = daily.iloc[:idx + 1]
        closes = df["Close"]
        spot = float(closes.iloc[-1])
        ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(closes.ewm(span=50, adjust=False).mean().iloc[-1])
        if not (min(ema20, ema50) <= spot <= max(ema20, ema50)):
            return False
        # Estimate current IV as 30-day stddev annualized (proxy)
        cur_iv = closes.pct_change().dropna().iloc[-30:].std() * (252 ** 0.5) * 100 if len(closes) >= 30 else 30.0
        iv_rank = _calc_iv_rank(cur_iv, closes)
        if iv_rank < 40:
            return False
        return True
    if strategy == "Butterfly":
        # Low IV, price near resistance (use 20d high as proxy)
        if idx < 22:
            return False
        df = daily.iloc[:idx + 1]
        closes = df["Close"]
        spot = float(closes.iloc[-1])
        cur_iv = closes.pct_change().dropna().iloc[-30:].std() * (252 ** 0.5) * 100 if len(closes) >= 30 else 30.0
        iv_rank = _calc_iv_rank(cur_iv, closes)
        if iv_rank > 30:
            return False
        high20 = float(closes.rolling(20).max().iloc[-1])
        if abs(spot - high20) / max(high20, 1) > 0.01:
            return False
        return True
    return False


def _backtest_rsi(closes: pd.Series) -> float:
    """RSI-14 helper for backtest signal functions."""
    if len(closes) < 15:
        return 50.0
    deltas = closes.diff()
    gains  = deltas.clip(lower=0).rolling(14).mean()
    losses = (-deltas).clip(lower=0).rolling(14).mean()
    avg_l  = float(losses.iloc[-1])
    if avg_l <= 0:
        return 100.0
    rs = float(gains.iloc[-1]) / avg_l
    return 100 - (100 / (1 + rs))


def _breakout_retest_signal(daily: pd.DataFrame, idx: int) -> bool:
    """True if breakout-retest pattern is confirmed at index `idx`.
    Uses the sub-DataFrame up to idx.
    """
    if idx < 38:
        return False
    df = daily.iloc[:idx + 1]
    if len(df) < 38:
        return False
    closes = df["Close"]
    lows   = df["Low"]
    spot   = float(closes.iloc[-1])
    if spot < 5.0:
        return False

    # Resistance: max close from 20-35 sessions ago
    resistance = float(closes.iloc[-35:-20].max())
    if resistance <= 0:
        return False

    # Breakout occurred in past 5-15 sessions
    breakout = any(float(c) > resistance * 1.01 for c in closes.iloc[-15:-2])
    if not breakout:
        return False

    # Retest: a low since breakout touched within 2% above resistance
    retest = any(float(lw) <= resistance * 1.02 for lw in lows.iloc[-10:-1])
    if not retest:
        return False

    # Currently above resistance
    if spot < resistance * 1.01:
        return False

    # RSI 50-60 (higher-quality continuation window)
    rsi = _backtest_rsi(closes.iloc[-15:])
    if not (50 <= rsi <= 60):
        return False

    # Volume >= 1.5x average (strong conviction)
    avg_vol = float(df["Volume"].iloc[-21:-1].mean())
    cur_vol = float(df["Volume"].iloc[-1])
    return avg_vol > 0 and cur_vol >= avg_vol * 1.5


def _trend_pullback_signal(daily: pd.DataFrame, idx: int) -> bool:
    """True if EMA-20 pullback in a 50-EMA uptrend with bullish reversal candle."""
    if idx < 55:
        return False
    df     = daily.iloc[:idx + 1]
    closes = df["Close"]
    spot   = float(closes.iloc[-1])
    if spot < 5.0:
        return False

    # 50 EMA uptrend
    ema50 = float(closes.ewm(span=50, adjust=False).mean().iloc[-1])
    if spot <= ema50:
        return False

    # Spot within 1.5% of 20 EMA
    ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
    if abs(spot - ema20) / max(ema20, 1) > 0.015:
        return False

    # RSI 35-52
    rsi = _backtest_rsi(closes.iloc[-15:])
    if not (35 <= rsi <= 52):
        return False

    # Bullish reversal candle (hammer or engulfing)
    if len(df) < 2:
        return False
    o = float(df["Open"].iloc[-1]); h = float(df["High"].iloc[-1])
    l = float(df["Low"].iloc[-1]);  c = float(df["Close"].iloc[-1])
    full_range = h - l
    if full_range < 1e-6:
        return False
    body = abs(c - o)
    lower_wick = min(o, c) - l
    hammer = lower_wick >= 2 * max(body, 1e-9) and c > l + full_range * 0.40
    prev_o = float(df["Open"].iloc[-2]); prev_c = float(df["Close"].iloc[-2])
    engulf = c > o and prev_c < prev_o and o <= prev_c and c >= prev_o
    return hammer or engulf


def _mean_reversion_signal(daily: pd.DataFrame, idx: int) -> bool:
    """True if RSI <35, lower Bollinger Band near-touch, bullish reversal candle."""
    if idx < 25:
        return False
    df     = daily.iloc[:idx + 1]
    closes = df["Close"]
    spot   = float(closes.iloc[-1])
    if spot < 4.0:
        return False

    # RSI < 33 (tighter oversold bounce)
    rsi = _backtest_rsi(closes.iloc[-15:])
    if rsi >= 33:
        return False

    # Lower Bollinger Band touch or near-touch
    if len(closes) >= 22:
        sma20 = float(closes.rolling(20).mean().iloc[-1])
        std20 = float(closes.rolling(20).std().iloc[-1])
        lower_bb = sma20 - 2 * std20
        if spot > lower_bb * 1.01:
            return False

    # Volume spike confirmation
    avg_vol = float(df["Volume"].iloc[-15:-1].mean())
    if avg_vol > 0 and float(df["Volume"].iloc[-1]) < avg_vol * 1.2:
        return False

    # Bullish reversal candle
    if len(df) < 2:
        return False
    o = float(df["Open"].iloc[-1]); h = float(df["High"].iloc[-1])
    l = float(df["Low"].iloc[-1]);  c = float(df["Close"].iloc[-1])
    full_range = h - l
    if full_range < 1e-6:
        return False
    body = abs(c - o)
    lower_wick = min(o, c) - l
    hammer = lower_wick >= 2 * max(body, 1e-9) and c > l + full_range * 0.40
    prev_o = float(df["Open"].iloc[-2]); prev_c = float(df["Close"].iloc[-2])
    engulf = c > o and prev_c < prev_o and o <= prev_c and c >= prev_o
    return hammer or engulf


_earnings_cache: dict = {}   # symbol -> (fetch_date, bool: has_earnings_soon)

def _no_earnings_soon_bt(symbol: str, today: datetime.date, days: int = 15) -> bool:
    """Earnings avoidance check for backtest.
    Caches per symbol (refresh once per backtest run, not per bar).
    Fails-safe to True if calendar is unavailable.
    """
    cached = _earnings_cache.get(symbol)
    if cached is not None:
        return cached

    result = True
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar
        if cal is not None:
            cutoff = today + datetime.timedelta(days=days)
            dates: list = []
            if isinstance(cal, pd.DataFrame):
                for col in cal.columns:
                    for val in cal[col]:
                        dates.append(val)
            elif isinstance(cal, dict):
                for v in cal.values():
                    dates.extend(v if isinstance(v, (list, tuple)) else [v])
            for d in dates:
                try:
                    if hasattr(d, "date"):
                        ed = d.date()
                    elif isinstance(d, datetime.date):
                        ed = d
                    else:
                        continue
                    if today <= ed <= cutoff:
                        result = False
                        break
                except Exception:
                    pass
    except Exception:
        result = True

    _earnings_cache[symbol] = result
    return result


# ── Backtest Engine ───────────────────────────────────────────────────────────

def _calc_hv(closes: pd.Series, window: int = 30) -> float:
    """30-day historical volatility (annualised)."""
    if len(closes) < window + 2:
        return 0.30
    returns = closes.pct_change().dropna()
    hv      = float(returns.iloc[-window:].std()) * math.sqrt(252)
    return max(0.05, hv)


def _iv_proxy(closes: pd.Series) -> float:
    """IV proxy = 30d HV × 1.15 (typical options premium over realized vol)."""
    return min(3.0, _calc_hv(closes) * 1.15)


def backtest_symbol(
    symbol: str,
    start: datetime.date,
    end: datetime.date,
    initial_capital: float,
    verbose: bool,
) -> pd.DataFrame:
    """Run the backtest for one symbol. Returns a DataFrame of trades."""

    try:
        # Fetch daily bars from Alpaca (via get_bars)
        hist = get_bars(symbol, period="365d", interval="1d")
        if hist.empty or len(hist) < 40:
            if verbose:
                print(f"  {symbol}: insufficient data, skip")
            return pd.DataFrame()
        # Standardize columns to match expected format
        if "date" in hist.columns:
            hist = hist.rename(columns={"date": "Date"})
        elif "datetime" in hist.columns:
            hist = hist.rename(columns={"datetime": "Date"})
        elif "time" in hist.columns:
            hist = hist.rename(columns={"time": "Date"})
        hist = hist.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
        hist["Date"] = pd.to_datetime(hist["Date"]).dt.date
        hist = hist.reset_index(drop=True)
    except Exception as e:
        if verbose:
            print(f"  {symbol}: Alpaca data error — {e}")
        return pd.DataFrame()


    # SPY for regime filter
    try:
        spy = get_bars("SPY", period="365d", interval="1d")
        if "date" in spy.columns:
            spy = spy.rename(columns={"date": "Date"})
        elif "datetime" in spy.columns:
            spy = spy.rename(columns={"datetime": "Date"})
        elif "time" in spy.columns:
            spy = spy.rename(columns={"time": "Date"})
        spy = spy.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
        spy["Date"] = pd.to_datetime(spy["Date"]).dt.date
    except Exception:
        spy = pd.DataFrame()


    # Earnings calendar fetch is not supported via Alpaca; fallback to True (no block)
    # If you have a premium calendar source, integrate here.
    result = True

    open_positions: list[dict] = []
    trades: list[dict] = []
    capital = initial_capital
    stop_cooldown: dict = {}

    # Main backtest loop
    for i, row in hist.iterrows():
        if i < 25:
            continue
        today   = row["Date"]
        spot    = float(row["Close"])
        full_df = hist.iloc[:i + 1]

        # Regime
        is_bear = False
        if not spy.empty:
            spy_to_date = spy[spy["Date"] <= today]
            if len(spy_to_date) >= 200:
                sma200  = float(spy_to_date["Close"].rolling(200).mean().iloc[-1])
                spy_now = float(spy_to_date["Close"].iloc[-1])
                is_bear = spy_now < sma200

        iv = _iv_proxy(full_df["Close"])
        dte_entry = (OPTIONS_DTE_MIN + OPTIONS_DTE_MAX) // 2  # use mid-point DTE

        # Monitor open positions
        to_remove = []
        for pos in open_positions:
            dte_now = (pos["expiry"] - today).days

            # Re-price long leg
            cur_long  = _bs_price(spot, pos["strike"], dte_now, iv, call=True)
            entry_p   = pos["entry_price"]

            # For spreads: net value = long - short
            if pos.get("short_strike") and pos.get("short_entry_price") is not None:
                cur_short = _bs_price(spot, pos["short_strike"], dte_now, iv, call=True)
                net_cur   = cur_long - cur_short
                pnl_pct   = (net_cur - entry_p) / max(entry_p, 0.01) * 100
                # Spread profit target: 60% of max gain (max_gain = spread_width - net_debit ≈ entry_p for fair spreads)
                profit_tgt = 60.0
            else:
                pnl_pct   = (cur_long - entry_p) / max(entry_p, 0.01) * 100
                profit_tgt = OPTIONS_PROFIT_TARGET_PCT

            # Track peak P&L for trailing stop
            if pnl_pct > pos.get("peak_pnl", 0.0):
                pos["peak_pnl"] = pnl_pct

            close_reason = None
            if dte_now <= 1:
                close_reason = "EXPIRY"
            elif pnl_pct >= profit_tgt:
                close_reason = "PROFIT"
            elif pnl_pct <= -OPTIONS_STOP_LOSS_PCT:
                close_reason = "STOP"
            elif pos.get("peak_pnl", 0.0) >= 20.0 and pnl_pct <= pos["peak_pnl"] - 20.0:
                close_reason = "TRAIL"

            if close_reason:
                pnl_dollar = entry_p * 100 * pos["contracts"] * pnl_pct / 100
                capital   += pos["cost"] + pnl_dollar
                trades.append({
                    "date_in":    pos["date_in"],
                    "date_out":   today,
                    "symbol":     symbol,
                    "type":       pos["type"],
                    "strike":     pos["strike"],
                    "expiry":     pos["expiry"],
                    "entry_px":   round(entry_p, 3),
                    "exit_px":    round(cur_long, 3),
                    "contracts":  pos["contracts"],
                    "pnl_pct":    round(pnl_pct, 1),
                    "pnl_$":      round(pnl_dollar, 2),
                    "reason":     close_reason,
                    "strategy":   pos["strategy"],
                })
                to_remove.append(pos)
                if close_reason == "STOP":
                    stop_cooldown[symbol] = today  # block re-entry for 5 days

        for pos in to_remove:
            open_positions.remove(pos)

        # Only open new positions within the requested date window
        if today < start:
            continue

        if len(open_positions) >= OPTIONS_MAX_POSITIONS:
            continue

        # Dollar volume quality gate: skip thinly-traded symbols
        _adv_window = hist.iloc[max(0, i - 20): i + 1]
        if len(_adv_window) >= 5:
            _adv = float((_adv_window["Close"] * _adv_window["Volume"]).mean())
            if _adv < 500_000:
                continue

        is_inverse = symbol in _INVERSE_ETFS

        # Stop cooldown: skip this symbol if it hit a stop within the last 5 days
        if symbol in stop_cooldown and (today - stop_cooldown[symbol]).days < 5:
            continue

        # Earnings avoidance: skip if earnings within 15 days
        # (use cached result — fetched once per symbol at backtest start)
        if not _no_earnings_soon_bt(symbol, today):
            continue


        # --- Try all strategies in priority order ---
        strategies = [
            ("MeanReversion", 0.65),
            ("TrendPullbackSpread", 0.65),
            ("BreakoutRetest", 0.50),
            ("IronCondor", 0.0),
            ("Butterfly", 0.50),
        ]
        fire_strat = None
        target_delta = 0.40
        for strat, delta in strategies:
            # Regime filter: only allow call-based strategies in bull regime
            if strat in ("MeanReversion", "TrendPullbackSpread", "BreakoutRetest", "Butterfly") and not (not is_bear or is_inverse):
                continue
            if _signal_for_strategy(strat, hist.iloc[:i + 1], i, is_bear):
                fire_strat = strat
                target_delta = delta
                break
        if fire_strat is None:
            continue

        # --- Position construction logic ---
        if fire_strat == "TrendPullbackSpread":
            strike = _pick_strike(spot, call=True, target_delta=target_delta, iv=iv, dte=dte_entry)
            short_strike  = strike + 2 * max(1.0, round(spot * 0.02, 0))
            long_price    = _bs_price(spot, strike, dte_entry, iv, call=True)
            short_price   = _bs_price(spot, short_strike, dte_entry, iv, call=True)
            net_debit     = max(0.01, long_price - short_price)
            max_profit    = (short_strike - strike) - net_debit
            if max_profit <= 0:
                continue
            price = net_debit
        elif fire_strat == "IronCondor":
            # Sell OTM put/call, buy wings 2 strikes further OTM
            # Use 0.20 delta for short strikes, 2 strikes for wings
            put_strike = _pick_strike(spot, call=False, target_delta=0.20, iv=iv, dte=dte_entry)
            call_strike = _pick_strike(spot, call=True, target_delta=0.20, iv=iv, dte=dte_entry)
            put_wing = max(1.0, round(spot * 0.02, 0))
            call_wing = max(1.0, round(spot * 0.02, 0))
            long_put_strike = put_strike - 2 * put_wing
            long_call_strike = call_strike + 2 * call_wing
            short_put_price = _bs_price(spot, put_strike, dte_entry, iv, call=False)
            short_call_price = _bs_price(spot, call_strike, dte_entry, iv, call=True)
            long_put_price = _bs_price(spot, long_put_strike, dte_entry, iv, call=False)
            long_call_price = _bs_price(spot, long_call_strike, dte_entry, iv, call=True)
            net_credit = short_put_price + short_call_price - long_put_price - long_call_price
            spread_width = min(abs(call_strike - long_call_strike), abs(put_strike - long_put_strike))
            max_loss = spread_width - net_credit
            if net_credit <= 0 or max_loss <= 0:
                continue
            price = net_credit
            strike = spot
            short_strike = call_strike
        elif fire_strat == "Butterfly":
            # Buy 1 ITM call, sell 2 ATM calls, buy 1 OTM call (equal width)
            strikes = [
                _pick_strike(spot, call=True, target_delta=0.35, iv=iv, dte=dte_entry),
                _pick_strike(spot, call=True, target_delta=0.50, iv=iv, dte=dte_entry),
                _pick_strike(spot, call=True, target_delta=0.65, iv=iv, dte=dte_entry),
            ]
            strikes = sorted(set(strikes))
            if len(strikes) < 3:
                continue
            low_strike, mid_strike, high_strike = strikes
            low_price = _bs_price(spot, low_strike, dte_entry, iv, call=True)
            mid_price = _bs_price(spot, mid_strike, dte_entry, iv, call=True)
            high_price = _bs_price(spot, high_strike, dte_entry, iv, call=True)
            net_debit = low_price + high_price - 2 * mid_price
            spread_width = high_strike - low_strike
            max_profit = spread_width - net_debit
            if net_debit <= 0 or max_profit <= 0:
                continue
            price = net_debit
            strike = mid_strike
            short_strike = mid_strike
        else:
            strike = _pick_strike(spot, call=True, target_delta=target_delta, iv=iv, dte=dte_entry)
            price = _bs_price(spot, strike, dte_entry, iv, call=True)
            short_strike = None

        if price <= 0.05:
            continue

        per_pos_budget = capital / max(1, OPTIONS_MAX_POSITIONS - len(open_positions))
        contracts      = max(1, int(per_pos_budget // (abs(price) * 100)))
        cost           = abs(price) * 100 * contracts
        if cost <= capital:
            capital -= cost
            pos_entry: dict = {
                "type":         "call" if fire_strat != "IronCondor" else "iron_condor",
                "strike":       strike,
                "expiry":       today + datetime.timedelta(days=dte_entry),
                "entry_price":  price,
                "contracts":    contracts,
                "cost":         cost,
                "date_in":      today,
                "strategy":     fire_strat,
                "peak_pnl":     0.0,   # trailing stop tracker
            }
            if fire_strat in ("TrendPullbackSpread", "IronCondor", "Butterfly"):
                pos_entry["short_strike"] = short_strike
            open_positions.append(pos_entry)

    # Mark remaining positions as closed at end-date
    for pos in open_positions:
        last_row  = hist.iloc[-1]
        last_spot = float(last_row["Close"])
        dte_now   = max(1, (pos["expiry"] - hist.iloc[-1]["Date"]).days)
        iv        = _iv_proxy(hist["Close"])
        cur_long  = _bs_price(last_spot, pos["strike"], dte_now, iv, call=True)
        entry_p   = pos["entry_price"]
        if pos.get("short_strike") and pos.get("short_entry_price") is not None:
            cur_short  = _bs_price(last_spot, pos["short_strike"], dte_now, iv, call=True)
            net_cur    = cur_long - cur_short
            pnl_pct    = (net_cur - entry_p) / max(entry_p, 0.01) * 100
        else:
            pnl_pct    = (cur_long - entry_p) / max(entry_p, 0.01) * 100
        pnl_dollar = entry_p * 100 * pos["contracts"] * pnl_pct / 100
        trades.append({
            "date_in":   pos["date_in"],
            "date_out":  hist.iloc[-1]["Date"],
            "symbol":    symbol,
            "type":      pos["type"],
            "strike":    pos["strike"],
            "expiry":    pos["expiry"],
            "entry_px":  round(pos["entry_price"], 3),
            "exit_px":   round(cur_long, 3),
            "contracts": pos["contracts"],
            "pnl_pct":   round(pnl_pct, 1),
            "pnl_$":     round(pnl_dollar, 2),
            "reason":    "EOD",
            "strategy":  pos["strategy"],
        })

    return pd.DataFrame(trades)


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backtest options strategies")
    parser.add_argument("--symbols",  nargs="*", default=None,    help="Tickers to test (default: data/ti_unusual_options.json)")
    parser.add_argument("--highshort", action="store_true",           help="Use HIGH_SHORT_FLOAT_STOCKS from engine.config")
    parser.add_argument("--repeat",   type=int, default=1,           help="Repeat the backtest this many times")
    parser.add_argument("--start",    default="2024-01-01",        help="Start date YYYY-MM-DD")
    parser.add_argument("--end",      default=str(datetime.date.today()), help="End date YYYY-MM-DD")
    parser.add_argument("--capital",  type=float, default=10000.0, help="Initial capital")
    parser.add_argument("--verbose",  "-v", action="store_true")
    args = parser.parse_args()

    if args.symbols:
        symbols = args.symbols
        universe_label = "custom symbols"
    elif args.highshort:
        symbols = sorted(HIGH_SHORT_FLOAT_STOCKS)
        universe_label = "HIGH_SHORT_FLOAT_STOCKS"
    else:
        symbols = get_options_universe(require_ti_file=True)
        universe_label = "primary universe (data/ti_primary.json → fallback data/universe.json tiers 1+2)"
    start   = datetime.date.fromisoformat(args.start)
    end     = datetime.date.fromisoformat(args.end)

    print(f"\nOptions Backtest - {start} to {end}")
    print(f"Symbols : {', '.join(symbols)} (from {universe_label})")
    print(f"Capital : ${args.capital:,.0f} | Options budget: ${args.capital * OPTIONS_ALLOCATION_PCT / 100:,.0f} (15%)")
    print(f"Rules   : TP={OPTIONS_PROFIT_TARGET_PCT:.0f}%  SL=-{OPTIONS_STOP_LOSS_PCT:.0f}%  DTE={OPTIONS_DTE_MIN}-{OPTIONS_DTE_MAX}")
    print("=" * 80)

    total_round_trades = []
    for iteration in range(1, max(1, args.repeat) + 1):
        if args.repeat > 1:
            print(f"\n--- Iteration {iteration}/{args.repeat} ---")
        all_trades = []
        for sym in symbols:
            print(f"\n  {sym}:", end=" ")
            df = backtest_symbol(sym, start, end, args.capital, args.verbose)
            if df.empty:
                print("no trades")
            else:
                wins  = df[df["pnl_$"] > 0]
                total_pnl = df["pnl_$"].sum()
                print(
                    f"{len(df)} trade(s) | win={len(wins)}/{len(df)} ({100*len(wins)/len(df):.0f}%) | "
                    f"P&L=${total_pnl:+,.2f}"
                )
                if args.verbose:
                    print(df[["date_in", "date_out", "type", "strike", "contracts", "pnl_pct", "pnl_$", "reason", "strategy"]].to_string())
                all_trades.append(df)
        if not all_trades:
            print("\nNo trades generated across all symbols.")
        else:
            combined = pd.concat(all_trades, ignore_index=True)
            total_trades = len(combined)
            total_wins   = len(combined[combined["pnl_$"] > 0])
            total_pnl    = combined["pnl_$"].sum()
            print("\n" + "=" * 80)
            print(f"Iteration {iteration} summary")
            print(f"  Total trades : {total_trades}")
            print(f"  Win rate     : {total_wins}/{total_trades} ({100*total_wins/max(total_trades,1):.1f}%)")
            print(f"  Total P&L    : ${total_pnl:+,.2f}")
            total_round_trades.append(combined)
        if args.repeat > 1 and iteration < args.repeat:
            print("\nRestarting next iteration...\n")

    if args.repeat > 1 and total_round_trades:
        final_combined = pd.concat(total_round_trades, ignore_index=True)
        print("\n" + "=" * 80)
        print("Aggregate summary across all iterations")
        total_trades = len(final_combined)
        total_wins   = len(final_combined[final_combined["pnl_$"] > 0])
        total_pnl    = final_combined["pnl_$"].sum()
        print(f"  Total trades : {total_trades}")
        print(f"  Win rate     : {total_wins}/{total_trades} ({100*total_wins/max(total_trades,1):.1f}%)")
        print(f"  Total P&L    : ${total_pnl:+,.2f}")
    return 0
    print(f"  Win rate     : {total_wins}/{total_trades} ({100*total_wins/max(total_trades,1):.1f}%)")
    print(f"  Total P&L    : ${total_pnl:+,.2f}")
    print(f"  Avg win      : ${avg_win:+,.2f}")
    print(f"  Avg loss     : ${avg_loss:+,.2f}")
    losses = total_trades - total_wins
    if losses > 0 and avg_loss != 0:
        print(f"  Profit factor: {abs(avg_win * total_wins / (avg_loss * losses)):.2f}")
    elif total_wins > 0:
        print("  Profit factor: ∞ (no losing trades)")
    print(f"\nBy strategy:\n{by_strategy.rename(columns={'sum':'P&L $','count':'Trades'}).to_string()}")

    out_csv = ROOT / "predictions" / "backtest_options.csv"
    combined.to_csv(out_csv, index=False)
    print(f"\nResults saved: {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
