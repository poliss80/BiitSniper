"""
ApexTrader — Kill Mode
Extreme bear-market circuit breaker.
Extracted from main.py to keep the main entry point lean.
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional

log = logging.getLogger("ApexTrader")

# ── State ──────────────────────────────────────────────────────────────────
_active: bool                       = False
_date:   Optional[datetime.date]    = None


def check(
    client,
    executor,
    options_executor,
    *,
    vix_level:      float,
    spy_drop_pct:   float,
    vix_roc_pct:    float,
) -> bool:
    """Check extreme bear conditions and trigger emergency close if needed.

    Returns True while kill mode is active (blocks all new entries for the day).

    Triggers on ANY of:
      1. VIX >= *vix_level*
      2. SPY intraday drop >= *spy_drop_pct* from today's open
      3. VIX spike >= *vix_roc_pct* in last 5 hours
    """
    global _active, _date

    today = datetime.date.today()
    if _date != today:
        _active = False
        _date   = today

    if _active:
        log.warning("KILL MODE ACTIVE — all new entries blocked for today")
        return True

    trigger_reason: Optional[str] = None

    # 1. Absolute VIX level
    try:
        from engine.utils import get_vix
        vix = get_vix()
        if vix >= vix_level:
            trigger_reason = f"VIX={vix:.1f} >= threshold {vix_level:.0f}"
    except Exception:
        pass

    # 2 & 3. SPY intraday drop + VIX rate-of-change
    if trigger_reason is None:
        try:
            import pandas as pd
            from engine.utils import get_bars_batch, get_bars
            bars_batch  = get_bars_batch(["SPY", "^VIX"], "5d", "1m")
            spy_bars    = bars_batch.get("SPY", pd.DataFrame())
            if not spy_bars.empty and len(spy_bars) >= 2:
                spy_open = float(spy_bars["open"].iloc[0])
                spy_now  = float(spy_bars["close"].iloc[-1])
                drop_pct = ((spy_now - spy_open) / spy_open) * 100
                if drop_pct <= -spy_drop_pct:
                    trigger_reason = (
                        f"SPY intraday {drop_pct:.2f}% "
                        f"(open ${spy_open:.2f} → now ${spy_now:.2f})"
                    )

            if trigger_reason is None:
                vix_bars_1h = get_bars("^VIX", "5d", "1h")
                if not vix_bars_1h.empty and len(vix_bars_1h) >= 5:
                    past_vix    = float(vix_bars_1h["close"].iloc[-5])
                    current_vix = float(vix_bars_1h["close"].iloc[-1])
                    if past_vix > 0:
                        roc = ((current_vix - past_vix) / past_vix) * 100
                        if roc >= vix_roc_pct:
                            trigger_reason = (
                                f"VIX +{roc:.0f}% in 5h "
                                f"({past_vix:.1f} → {current_vix:.1f})"
                            )
        except Exception:
            pass

    if trigger_reason is None:
        return False

    log.warning("=" * 70)
    log.warning(f"KILL MODE TRIGGERED: {trigger_reason}")
    log.warning("EXTREME BEAR MARKET — CLOSING ALL POSITIONS TO PROTECT CAPITAL")
    log.warning("=" * 70)
    _active = True
    _date   = today

    try:
        _acct = client.get_account()
        executor.emergency_close_all(float(_acct.equity))
        if options_executor is not None:
            options_executor.close_all()
    except Exception as e:
        log.error(f"Kill mode close error: {e}")

    return True


def is_active() -> bool:
    """Return True when kill mode is engaged for today."""
    return _active
