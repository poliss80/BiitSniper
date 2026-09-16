"""Gate-by-gate debug for ShortSqueezeStrategy on a single symbol."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import logging
logging.basicConfig(level=logging.DEBUG, format="%(message)s")

from engine.options.strategies import (
    ShortSqueezeStrategy,
    _fetch_squeeze_fundamentals,
    _fetch_squeeze_rs,
    _fetch_bar_context,
    _get_options_chain,
)
from engine.utils import get_bars

for sym in ["BBNX", "DOCN", "BKSY"]:
    strat = ShortSqueezeStrategy()
    fund  = _fetch_squeeze_fundamentals(sym)
    rs13w = _fetch_squeeze_rs(sym)
    ctx   = _fetch_bar_context(sym)

    spy   = get_bars("SPY", "10d", "1d")
    spy_r5 = float((spy["close"].iloc[-1] - spy["close"].iloc[-6]) / spy["close"].iloc[-6] * 100) if len(spy) >= 6 else 0
    stk_r5 = float((ctx.closes.iloc[-1] - ctx.closes.iloc[-6]) / ctx.closes.iloc[-6] * 100) if len(ctx.closes) >= 6 else 0
    rs_5d  = stk_r5 - spy_r5

    ema8  = float(ctx.closes.ewm(span=8,  adjust=False).mean().iloc[-1])
    ema21 = float(ctx.closes.ewm(span=21, adjust=False).mean().iloc[-1])

    chain = _get_options_chain(sym)

    confirmed = rs13w is not None and rs13w >= 10.0
    early     = rs_5d >= 15.0

    print(f"\n=== {sym} ===")
    spf = fund["short_pct_float"]; gm = fund["gross_margins"]; rg = fund["rev_growth"]
    print(f"  short_pct_float : {spf:.1%}  -> {'PASS' if spf >= 0.12 else 'FAIL'}")
    print(f"  gross_margins   : {gm:.1%}  -> {'PASS' if gm > 0 else 'FAIL'}")
    print(f"  rev_growth      : {rg:.1%}  -> {'PASS' if rg > 0.08 else 'FAIL'}")
    print(f"  RS 13W          : {rs13w}  -> {'CONFIRMED' if confirmed else 'not confirmed'}")
    print(f"  RS 5d vs SPY    : stk={stk_r5:+.1f}% spy={spy_r5:+.1f}% diff={rs_5d:+.1f}%  -> {'EARLY SQUEEZE' if early else 'not early'}")
    rsi_max = 65 if early and not confirmed else 70
    print(f"  RSI             : {ctx.rsi:.1f}  (need 38-{rsi_max})  -> {'PASS' if 38 <= ctx.rsi <= rsi_max else 'FAIL'}")
    print(f"  vol_ratio       : {ctx.vol_ratio:.2f}  -> {'PASS' if ctx.vol_ratio >= 1.1 else 'FAIL'}")
    print(f"  EMA8 > EMA21    : {ema8:.3f} > {ema21:.3f}  -> {'PASS' if ema8 > ema21 else 'FAIL'}")
    if chain:
        use_spread = chain.iv_rank > 50 or (not confirmed and early)
        print(f"  IV rank         : {chain.iv_rank:.1f}  -> mode: {'BULL CALL SPREAD' if use_spread else 'NAKED CALL'}")
    print()

    sig = strat.scan(sym)
    if sig:
        print(f"  SIGNAL: {sig.reason}")
        print(f"  confidence={sig.confidence}  strike={sig.strike}  expiry={sig.expiry}  rr={sig.rr_ratio}")
    else:
        print(f"  No signal produced")
