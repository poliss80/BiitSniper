"""Regression trendline breakout signals for OHLCV market data.

Input data must have ``open``, ``high``, ``low``, ``close``, and ``volume``
columns ordered from oldest to newest. The module is intentionally independent
of broker APIs so it can be used in backtests or converted into bot signals.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd


Signal = Literal["buy", "sell", ""]
REQUIRED_COLUMNS = {"open", "high", "low", "close", "volume"}


def calculate_atr(data: pd.DataFrame, period: int = 14) -> pd.Series:
    """Return the simple-moving-average Average True Range for OHLCV data."""
    previous_close = data["close"].shift(1)
    true_range = pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - previous_close).abs(),
            (data["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(period, min_periods=period).mean()


def find_swing_points(
    data: pd.DataFrame, left_bars: int = 5, right_bars: int = 5
) -> tuple[pd.Series, pd.Series]:
    """Identify confirmed swing-high and swing-low prices.

    A pivot is only confirmed after ``right_bars`` future candles. Its value is
    placed at the pivot candle, while subsequent line calculation consumes only
    pivots strictly before the current candle to avoid look-ahead bias.
    """
    if left_bars < 1 or right_bars < 1:
        raise ValueError("left_bars and right_bars must both be at least 1")

    window = left_bars + right_bars + 1
    high_window_max = data["high"].rolling(window, center=True).max()
    low_window_min = data["low"].rolling(window, center=True).min()
    swing_high = data["high"].where(data["high"].eq(high_window_max))
    swing_low = data["low"].where(data["low"].eq(low_window_min))
    return swing_high, swing_low


def _latest_trendline(
    pivots: pd.Series, current_position: int, pivot_count: int
) -> float:
    """Fit a least-squares line to the latest confirmed pivots before a bar."""
    prior_values = pivots.iloc[:current_position]
    pivot_positions = np.flatnonzero(prior_values.notna().to_numpy())
    if len(pivot_positions) < pivot_count:
        return np.nan

    pivot_positions = pivot_positions[-pivot_count:]
    pivot_prices = prior_values.iloc[pivot_positions].to_numpy(dtype=float)
    slope, intercept = np.polyfit(pivot_positions, pivot_prices, deg=1)
    return float(slope * current_position + intercept)


def calculate_trendlines(
    swing_high: pd.Series, swing_low: pd.Series, pivot_count: int = 3
) -> tuple[pd.Series, pd.Series]:
    """Return resistance and support values fitted from recent confirmed pivots."""
    if pivot_count not in (2, 3):
        raise ValueError("pivot_count must be 2 or 3")

    positions = np.arange(len(swing_high))
    resistance = pd.Series(
        [_latest_trendline(swing_high, position, pivot_count) for position in positions],
        index=swing_high.index,
        dtype=float,
    )
    support = pd.Series(
        [_latest_trendline(swing_low, position, pivot_count) for position in positions],
        index=swing_low.index,
        dtype=float,
    )
    return resistance, support


def detect_trendline_breakouts(
    data: pd.DataFrame,
    *,
    left_bars: int = 5,
    right_bars: int = 5,
    pivot_count: int = 3,
    atr_period: int = 14,
    volume_period: int = 20,
    volume_multiplier: float = 1.5,
    atr_offset: float = 0.5,
    risk_reward_ratio: float = 2.0,
) -> pd.DataFrame:
    """Add regression trendline breakout entries and bracket prices to OHLCV data.

    A buy requires a close to freshly cross above resistance by ``atr_offset``
    ATR with volume above ``volume_multiplier`` times its moving average. A sell
    applies the symmetric condition below support. Stops sit one ATR from entry;
    targets use the supplied risk/reward ratio, defaulting to 1:2.
    """
    missing_columns = REQUIRED_COLUMNS.difference(data.columns)
    if missing_columns:
        raise ValueError(f"OHLCV data is missing required columns: {sorted(missing_columns)}")
    if atr_period < 1 or volume_period < 1:
        raise ValueError("atr_period and volume_period must both be at least 1")
    if risk_reward_ratio <= 0:
        raise ValueError("risk_reward_ratio must be positive")

    result = data.copy()
    result["atr"] = calculate_atr(result, atr_period)
    result["volume_sma"] = result["volume"].rolling(
        volume_period, min_periods=volume_period
    ).mean()
    result["swing_high"], result["swing_low"] = find_swing_points(
        result, left_bars, right_bars
    )
    result["resistance_line"], result["support_line"] = calculate_trendlines(
        result["swing_high"], result["swing_low"], pivot_count
    )

    resistance_threshold = result["resistance_line"] + atr_offset * result["atr"]
    support_threshold = result["support_line"] - atr_offset * result["atr"]
    prior_close = result["close"].shift(1)
    prior_resistance = resistance_threshold.shift(1)
    prior_support = support_threshold.shift(1)
    volume_confirmed = result["volume"] > volume_multiplier * result["volume_sma"]

    bullish_cross = (prior_close <= prior_resistance) & (result["close"] > resistance_threshold)
    bearish_cross = (prior_close >= prior_support) & (result["close"] < support_threshold)
    buy_signal = bullish_cross & volume_confirmed
    sell_signal = bearish_cross & volume_confirmed

    result["signal"] = np.select([buy_signal, sell_signal], ["buy", "sell"], default="")
    result["stop_loss"] = np.where(
        buy_signal,
        result["close"] - result["atr"],
        np.where(sell_signal, result["close"] + result["atr"], np.nan),
    )
    result["take_profit"] = np.where(
        buy_signal,
        result["close"] + risk_reward_ratio * result["atr"],
        np.where(sell_signal, result["close"] - risk_reward_ratio * result["atr"], np.nan),
    )
    return result


def make_synthetic_ohlcv(rows: int = 160, seed: int = 7) -> pd.DataFrame:
    """Create deterministic OHLCV data suitable for the example below."""
    if rows < 30:
        raise ValueError("rows must be at least 30 to form a usable trendline")
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0.08, 0.7, size=rows))
    open_price = np.r_[close[0], close[:-1]] + rng.normal(0.0, 0.15, size=rows)
    high = np.maximum(open_price, close) + rng.uniform(0.1, 0.8, size=rows)
    low = np.minimum(open_price, close) - rng.uniform(0.1, 0.8, size=rows)
    volume = rng.integers(80_000, 180_000, size=rows)
    data = pd.DataFrame(
        {"open": open_price, "high": high, "low": low, "close": close, "volume": volume},
        index=pd.date_range("2026-01-02", periods=rows, freq="D"),
    )
    resistance, _ = calculate_trendlines(*find_swing_points(data), pivot_count=3)
    reference = float(resistance.iloc[-1])
    previous_index, final_index = data.index[-2:]
    data.loc[previous_index, ["open", "high", "low", "close"]] = [
        reference - 1.2,
        reference - 0.7,
        reference - 1.7,
        reference - 1.2,
    ]
    data.loc[final_index, ["open", "high", "low", "close", "volume"]] = [
        reference - 1.0,
        reference + 4.5,
        reference + 3.5,
        reference + 4.0,
        1_000_000,
    ]
    return data


if __name__ == "__main__":
    ohlcv = make_synthetic_ohlcv()
    signals = detect_trendline_breakouts(ohlcv)
    print(signals.loc[signals["signal"] != "", ["close", "signal", "stop_loss", "take_profit"]])