"""
ApexTrader — Session
Daily and quarterly P&L tracking state.
Extracted from main.py to keep the main entry point lean.
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger("ApexTrader")

_QUARTERLY_STATE_FILE = Path(__file__).resolve().parent.parent / ".quarterly_state.json"
_quarterly_state_lock = threading.Lock()

# ── Daily state ────────────────────────────────────────────────────────────
daily_pnl:          float               = 0.0
daily_start_equity: float               = 0.0
daily_reset:        Optional[datetime.date] = None
trades:             int                 = 0

# ── Quarterly state ────────────────────────────────────────────────────────
quarterly_start_equity: float               = 0.0
quarterly_reset:        Optional[datetime.date] = None


# ── Helpers ────────────────────────────────────────────────────────────────

def get_quarter_start(d: datetime.date) -> datetime.date:
    """Return the first date of the calendar quarter containing *d*."""
    quarter_month = ((d.month - 1) // 3) * 3 + 1
    return datetime.date(d.year, quarter_month, 1)


def load_quarterly_state() -> None:
    """Load persisted quarter-start equity from disk (survives restarts)."""
    global quarterly_start_equity, quarterly_reset
    try:
        if _QUARTERLY_STATE_FILE.exists():
            state              = json.loads(_QUARTERLY_STATE_FILE.read_text())
            quarterly_reset    = datetime.date.fromisoformat(state["quarterly_reset"])
            quarterly_start_equity = float(state["quarterly_start_equity"])
            log.info(
                f"Loaded quarterly state: start equity ${quarterly_start_equity:,.2f} "
                f"since {quarterly_reset}"
            )
    except Exception as e:
        log.warning(f"Could not load quarterly state: {e}")


def save_quarterly_state() -> None:
    """Persist current quarter-start equity to disk (thread-safe)."""
    try:
        payload = json.dumps({
            "quarterly_reset":        str(quarterly_reset),
            "quarterly_start_equity": quarterly_start_equity,
        })
        with _quarterly_state_lock:
            _QUARTERLY_STATE_FILE.write_text(payload)
    except Exception as e:
        log.warning(f"Could not save quarterly state: {e}")


def reset_daily(client) -> None:
    """Reset daily counters for a new trading day and prune the universe."""
    global daily_pnl, daily_start_equity, daily_reset, trades

    today = datetime.date.today()
    if daily_reset == today:
        return

    try:
        _day_acct          = client.get_account()
        daily_start_equity = float(_day_acct.equity)
    except Exception as e:
        log.warning(f"Could not read start-of-day equity: {e}")
        daily_start_equity = 0.0

    daily_pnl   = 0.0
    trades      = 0
    daily_reset = today

    log.info("=" * 70)
    log.info(f"NEW DAY: {today} | Start equity: ${daily_start_equity:,.2f}")

    try:
        from engine.equity.universe import prune as _prune
        removed = _prune()
        if removed:
            log.info(
                f"Universe pruned: removed {len(removed)} expired ticker(s): "
                f"{removed[:10]}{'…' if len(removed) > 10 else ''}"
            )
        else:
            log.info("Universe pruned: no expired tickers")
    except Exception as _e:
        log.warning(f"Universe prune failed: {_e}")

    log.info("=" * 70)


def refresh_daily_pnl(client) -> float:
    """Re-read equity from broker and return current daily P&L."""
    global daily_pnl
    if daily_start_equity > 0:
        try:
            _acct     = client.get_account()
            daily_pnl = float(_acct.equity) - daily_start_equity
        except Exception as e:
            log.warning(f"Could not refresh daily P&L: {e}")
    return daily_pnl


def check_quarterly(client, use_quarterly_target: bool, quarterly_profit_target_pct: float) -> None:
    """Check/initialise the quarterly profit target; logs progress each cycle."""
    global quarterly_start_equity, quarterly_reset

    if not use_quarterly_target:
        return

    today   = datetime.date.today()
    q_start = get_quarter_start(today)

    try:
        _acct   = client.get_account()
        _equity = float(_acct.equity)

        if quarterly_reset != q_start:
            quarterly_start_equity = _equity
            quarterly_reset        = q_start
            save_quarterly_state()
            log.info(f"New quarter {q_start} | Starting equity: ${quarterly_start_equity:,.2f}")

        if quarterly_start_equity > 0:
            q_gain_pct = ((_equity - quarterly_start_equity) / quarterly_start_equity) * 100
            log.info(f"Quarterly P&L: {q_gain_pct:+.1f}% (target >= {quarterly_profit_target_pct:.0f}%)")
            if q_gain_pct >= quarterly_profit_target_pct:
                log.info(
                    f"QUARTERLY TARGET HIT: +{q_gain_pct:.1f}% >= {quarterly_profit_target_pct:.0f}% | "
                    f"${quarterly_start_equity:,.2f} -> ${_equity:,.2f} | Target reached (continuing)"
                )
    except Exception as e:
        log.warning(f"Quarterly target check error: {e}")
