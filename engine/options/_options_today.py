"""Options IV rank helper utilities for ApexTrader."""

import math
from typing import Optional

import pandas as pd


def _calc_iv_rank(cur_iv: float, closes: pd.Series) -> float:
    """Calculate IV rank as current IV versus trailing historical volatility range.

    Uses 30-day historical volatility (annualized) over the available price history,
    then computes the percentile rank of current IV within that range.
    """
    try:
        if cur_iv is None or cur_iv <= 0:
            return 50.0
        if closes is None or len(closes) < 30:
            return 50.0

        returns = closes.pct_change().dropna()
        if len(returns) < 30:
            return 50.0

        hv30 = returns.rolling(30).std() * math.sqrt(252) * 100
        hv30 = hv30.dropna()
        if hv30.empty:
            return 50.0

        hist = hv30.iloc[-252:] if len(hv30) >= 252 else hv30
        hv_min = float(hist.min())
        hv_max = float(hist.max())
        if hv_max <= hv_min:
            return 50.0

        rank = (cur_iv - hv_min) / (hv_max - hv_min) * 100.0
        return float(max(0.0, min(100.0, rank)))
    except Exception:
        return 50.0
