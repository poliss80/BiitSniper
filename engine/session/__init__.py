from .session import (
    # Mutable state — always access via the module reference (_session.daily_pnl)
    # not as a local import, so callers always see the live value.
    # Re-exported here only so `from engine import session as _session` works.
    daily_pnl,
    daily_start_equity,
    daily_reset,
    trades,
    quarterly_start_equity,
    quarterly_reset,
    # Functions
    get_quarter_start,
    load_quarterly_state,
    save_quarterly_state,
    reset_daily,
    refresh_daily_pnl,
    check_quarterly,
)
