"""Slope-adaptive support/resistance trendlines for OHLC data.

The engine replays completed bars, chooses a confirmed swing anchor, selects
an extreme-slope trigger, detects thresholded breaks, and archives broken
lines. It is independent of broker APIs and can be used by strategies or
backtests.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class TrendlineConfig:
    primary_lookback: int = 35
    bars_from_edge: int = 8
    include_previous_bar: bool = True
    enable_breakout_detection: bool = True
    breakout_threshold: float = 5.0
    breakout_confirm_bars: int = 1
    auto_adjust_on_break: bool = True
    convert_to_sup_res: bool = True
    point: Optional[float] = None
    breakout_threshold_price: Optional[float] = None
    max_history_lines: int = 3
    dedupe_breaks: bool = True

    def __post_init__(self) -> None:
        if self.primary_lookback < 2:
            raise ValueError("primary_lookback must be >= 2")
        if self.bars_from_edge < 0:
            raise ValueError("bars_from_edge cannot be negative")
        if self.breakout_confirm_bars < 1:
            raise ValueError("breakout_confirm_bars must be >= 1")
        if self.max_history_lines < 0:
            raise ValueError("max_history_lines cannot be negative")

    @property
    def min_bars(self) -> int:
        return self.primary_lookback * 2 + self.bars_from_edge * 2


@dataclass(frozen=True)
class Level:
    index: int
    time: pd.Timestamp
    price: float


@dataclass(frozen=True)
class Trendline:
    anchor: Level
    trigger: Level
    side: str

    @property
    def slope(self) -> float:
        distance = self.trigger.index - self.anchor.index
        return ((self.trigger.price - self.anchor.price) / distance
                if distance else 0.0)

    def value_at(self, index: int) -> float:
        return self.trigger.price + self.slope * (index - self.trigger.index)

    def key(self) -> tuple:
        return self.side, self.anchor.index, self.trigger.index


@dataclass(frozen=True)
class BreakoutEvent:
    index: int
    time: pd.Timestamp
    price: float
    is_support: bool
    line_price: float
    line: Trendline


@dataclass
class TrendlineResult:
    index: pd.DatetimeIndex
    lower_line: Optional[Trendline] = None
    upper_line: Optional[Trendline] = None
    history_lower: List[Trendline] = field(default_factory=list)
    history_upper: List[Trendline] = field(default_factory=list)
    breakouts: List[BreakoutEvent] = field(default_factory=list)
    lower_values: np.ndarray = field(default_factory=lambda: np.empty(0))
    upper_values: np.ndarray = field(default_factory=lambda: np.empty(0))
    lower_break: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=bool))
    upper_break: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=bool))

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame({
            "support": self.lower_values,
            "resistance": self.upper_values,
            "support_break": self.lower_break,
            "resistance_break": self.upper_break,
        }, index=self.index)


class AutoTrendline:
    """Replayable slope-adaptive support/resistance line engine."""

    def __init__(self, config: Optional[TrendlineConfig] = None) -> None:
        self.cfg = config or TrendlineConfig()

    def run(self, df: pd.DataFrame) -> TrendlineResult:
        high, low, close, index = self._unpack(df)
        n = len(close)
        if n <= self.cfg.min_bars:
            raise ValueError(f"need more than {self.cfg.min_bars} bars, got {n}")

        valley_idx = np.flatnonzero(self._pivot_flags(low, self.cfg.primary_lookback, True))
        peak_idx = np.flatnonzero(self._pivot_flags(high, self.cfg.primary_lookback, False))
        point = self._point_value(close)
        threshold = (self.cfg.breakout_threshold_price
                     if self.cfg.breakout_threshold_price is not None
                     else self.cfg.breakout_threshold * point * 10.0)
        lower = upper = None
        history_lower: Deque[Trendline] = deque(maxlen=max(self.cfg.max_history_lines, 1))
        history_upper: Deque[Trendline] = deque(maxlen=max(self.cfg.max_history_lines, 1))
        breakouts: List[BreakoutEvent] = []
        lower_values = np.full(n, np.nan)
        upper_values = np.full(n, np.nan)
        lower_break = np.zeros(n, dtype=bool)
        upper_break = np.zeros(n, dtype=bool)
        lower_streak = upper_streak = 0
        last_lower_key = last_upper_key = None

        for rates_total in range(self.cfg.min_bars + 1, n + 1):
            cur = rates_total - 2
            if self.cfg.enable_breakout_detection:
                for is_support in (True, False):
                    line = lower if is_support else upper
                    if line is None:
                        if is_support:
                            lower_streak = 0
                        else:
                            upper_streak = 0
                        continue
                    line_price = line.value_at(cur)
                    beyond = (close[cur] < line_price - threshold if is_support
                              else close[cur] > line_price + threshold)
                    streak = (lower_streak if is_support else upper_streak)
                    streak = streak + 1 if beyond else 0
                    if is_support:
                        lower_streak = streak
                    else:
                        upper_streak = streak
                    last_key = last_lower_key if is_support else last_upper_key
                    if not beyond or streak < self.cfg.breakout_confirm_bars:
                        continue
                    if self.cfg.dedupe_breaks and last_key == line.key():
                        continue
                    breakouts.append(BreakoutEvent(
                        cur, index[cur], float(close[cur]), is_support,
                        float(line_price), line))
                    if is_support:
                        lower_break[cur] = True
                        last_lower_key = line.key()
                    else:
                        upper_break[cur] = True
                        last_upper_key = line.key()
                    if self.cfg.auto_adjust_on_break:
                        stored = line
                        if self.cfg.convert_to_sup_res:
                            stored = Trendline(line.anchor, line.trigger,
                                               "upper" if is_support else "lower")
                        if stored.side == "upper":
                            history_upper.appendleft(stored)
                        else:
                            history_lower.appendleft(stored)
                        if is_support:
                            lower = None
                            lower_streak = 0
                        else:
                            upper = None
                            upper_streak = 0

            new_lower = self._scan_side(rates_total, index, low, valley_idx, True)
            new_upper = self._scan_side(rates_total, index, high, peak_idx, False)
            if new_lower is not None:
                lower = new_lower
            if new_upper is not None:
                upper = new_upper
            if lower is not None:
                lower_values[cur] = lower.value_at(cur)
            if upper is not None:
                upper_values[cur] = upper.value_at(cur)

        if lower is not None:
            lower_values[-1] = lower.value_at(n - 1)
        if upper is not None:
            upper_values[-1] = upper.value_at(n - 1)
        return TrendlineResult(index, lower, upper, list(history_lower),
                               list(history_upper), breakouts, lower_values,
                               upper_values, lower_break, upper_break)

    def latest(self, df: pd.DataFrame) -> dict:
        result = self.run(df)
        cur = len(result.index) - 2
        lower = result.lower_line
        upper = result.upper_line
        point = self._point_value(df["close"].to_numpy(float))
        threshold = (self.cfg.breakout_threshold_price
                     if self.cfg.breakout_threshold_price is not None
                     else self.cfg.breakout_threshold * point * 10.0)
        support_price = lower.value_at(cur) if lower else np.nan
        resistance_price = upper.value_at(cur) if upper else np.nan
        return {
            "time": result.index[cur],
            "close": float(df["close"].iloc[cur]),
            "support": lower,
            "resistance": upper,
            "support_price": support_price,
            "resistance_price": resistance_price,
            "support_break": bool(lower and df["close"].iloc[cur] < support_price - threshold),
            "resistance_break": bool(upper and df["close"].iloc[cur] > resistance_price + threshold),
        }

    def _scan_side(self, rates_total, index, prices, pivot_idx, is_support):
        start = rates_total - self.cfg.primary_lookback - 2
        anchor_idx = self._pivot_at_or_before(pivot_idx, start)
        trigger_idx = rates_total - self.cfg.bars_from_edge - 2
        if anchor_idx <= 0 or trigger_idx <= anchor_idx:
            return None
        anchor_price = prices[anchor_idx]
        gradient = ((prices[trigger_idx] - anchor_price) / (trigger_idx - anchor_idx)
                    if is_support else (anchor_price - prices[trigger_idx]) / (trigger_idx - anchor_idx))
        scan_start = anchor_idx if self.cfg.include_previous_bar else anchor_idx + 1
        for candidate in range(trigger_idx - 1, scan_start, -1):
            span = candidate - scan_start
            if not span:
                continue
            test = ((prices[candidate] - anchor_price) / span if is_support
                    else (anchor_price - prices[candidate]) / span)
            if test < gradient:
                gradient = test
                trigger_idx = candidate
        return Trendline(Level(anchor_idx, index[anchor_idx], float(anchor_price)),
                         Level(trigger_idx, index[trigger_idx], float(prices[trigger_idx])),
                         "lower" if is_support else "upper")

    @staticmethod
    def _pivot_flags(prices, radius, find_low):
        series = pd.Series(prices)
        if len(prices) < 2 * radius + 1:
            return np.zeros(len(prices), dtype=bool)
        left = (series.shift(1).rolling(radius).min() if find_low
                else series.shift(1).rolling(radius).max()).to_numpy()
        right = (series.shift(-radius).rolling(radius).min() if find_low
                 else series.shift(-radius).rolling(radius).max()).to_numpy()
        core = prices < left if find_low else prices > left
        core &= prices < right if find_low else prices > right
        return (~(np.isnan(left) | np.isnan(right)) & core).astype(bool)

    @staticmethod
    def _pivot_at_or_before(pivot_idx, start):
        if start <= 0 or pivot_idx.size == 0:
            return -1
        position = np.searchsorted(pivot_idx, start, side="right") - 1
        return int(pivot_idx[position]) if position >= 0 else -1

    def _point_value(self, close):
        if self.cfg.point is not None:
            return self.cfg.point
        last = float(close[-1])
        return 0.00001 if last < 20 else 0.001 if last < 500 else 0.01

    @staticmethod
    def _unpack(df):
        columns = {str(c).lower(): c for c in df.columns}
        missing = [name for name in ("high", "low", "close") if name not in columns]
        if missing:
            raise ValueError(f"DataFrame is missing column(s): {missing}")
        if isinstance(df.index, pd.DatetimeIndex):
            index = df.index
        else:
            index = None
            for name in ("time", "date", "datetime", "timestamp"):
                if name in columns:
                    index = pd.DatetimeIndex(pd.to_datetime(df[columns[name]]))
                    break
            if index is None:
                index = pd.date_range("2000-01-01", periods=len(df), freq="min")
        return (df[columns["high"]].to_numpy(float),
                df[columns["low"]].to_numpy(float),
                df[columns["close"]].to_numpy(float),
                pd.DatetimeIndex(index))
