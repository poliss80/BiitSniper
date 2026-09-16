"""Deep-dive ShortSqueeze gate check for a single symbol. Usage: python -m scripts._squeeze_deep COHU"""
import sys
import logging

logging.basicConfig(level=logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

sym = sys.argv[1].upper() if len(sys.argv) > 1 else "COHU"

from engine.options.strategies import _fetch_squeeze_fundamentals, _fetch_squeeze_rs, _fetch_bar_context
from engine.utils import get_bars

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(val):
    return (GREEN + "PASS" + RESET) if val else (RED + "FAIL" + RESET)

print(f"\n{BOLD}=== ShortSqueeze deep-dive: {sym} ==={RESET}\n")

fund = _fetch_squeeze_fundamentals(sym)
print("--- Fundamentals (Yahoo Finance) ---")
if fund:
    sp = fund["short_pct_float"]
    gm = fund["gross_margins"]
    rg = fund["rev_growth"]
    print(f"  Short % of Float : {sp:.1%}     {ok(sp >= 0.12)}  (need >= 12%)")
    print(f"  Gross Margin     : {gm:.1%}     {ok(gm > 0)}   (need > 0%)")
    print(f"  Revenue Growth   : {rg:.1%}     {ok(rg >= 0.08)}  (need >= 8%)")
else:
    print(f"  {RED}No yfinance data — symbol may be unsupported or API rate-limited{RESET}")
    sp = gm = rg = 0.0

print()
ctx = _fetch_bar_context(sym)
if ctx is None:
    print(f"  {RED}No price bar data from Alpaca — cannot continue{RESET}")
    sys.exit(1)

ema8  = float(ctx.closes.ewm(span=8,  adjust=False).mean().iloc[-1])
ema21 = float(ctx.closes.ewm(span=21, adjust=False).mean().iloc[-1])

spy_bars  = get_bars("SPY", "10d", "1d")
spy_ret5d = 0.0
if len(spy_bars) >= 6:
    spy_ret5d = float(
        (spy_bars["close"].iloc[-1] - spy_bars["close"].iloc[-6])
        / spy_bars["close"].iloc[-6] * 100
    )
stk_ret5d = 0.0
if len(ctx.closes) >= 6:
    stk_ret5d = float(
        (ctx.closes.iloc[-1] - ctx.closes.iloc[-6])
        / ctx.closes.iloc[-6] * 100
    )
rs_5d  = stk_ret5d - spy_ret5d
rs_13w = _fetch_squeeze_rs(sym)

print("--- Price & Momentum ---")
print(f"  Spot             : ${ctx.spot:.2f}")
print(f"  RSI              : {ctx.rsi:.1f}" if ctx.rsi else "  RSI              : N/A")
print(f"  Vol Ratio        : {ctx.vol_ratio:.2f}x")
print(f"  EMA8             : {ema8:.2f}   EMA21: {ema21:.2f}")
print(f"  5d return        : {stk_ret5d:+.1f}%   SPY 5d: {spy_ret5d:+.1f}%   RS vs SPY: {rs_5d:+.1f}%")
rs13_str = f"{rs_13w:+.0f}%" if rs_13w is not None else "N/A"
print(f"  RS 13W vs SP500  : {rs13_str}")

confirmed = rs_13w is not None and rs_13w >= 10.0
early     = rs_5d >= 15.0
rsi_ok    = ctx.rsi is not None and 38 <= ctx.rsi <= 70
vol_ok    = ctx.vol_ratio >= 1.1
ema_ok    = ema8 > ema21
fund_ok   = fund is not None and sp >= 0.12 and gm > 0 and rg >= 0.08
rs_ok     = confirmed or early

print()
print("--- Gate Results ---")
print(f"  [1] Short% >= 12%          {ok(fund is not None and sp >= 0.12)}  ({sp:.1%})")
print(f"  [2] Gross margin > 0%      {ok(fund is not None and gm > 0)}  ({gm:.1%})")
print(f"  [3] Rev growth >= 8%       {ok(fund is not None and rg >= 0.08)}  ({rg:.1%})")
print(f"  [4] RS confirmed (13W>10%) {ok(confirmed)}  ({rs13_str})")
print(f"  [5] RS early (5d>15% SPY)  {ok(early)}  ({rs_5d:+.1f}%)")
print(f"  [6] RSI 38-70              {ok(rsi_ok)}  ({ctx.rsi:.0f})" if ctx.rsi else f"  [6] RSI 38-70              {ok(False)}  (N/A)")
print(f"  [7] Vol ratio >= 1.1x      {ok(vol_ok)}  ({ctx.vol_ratio:.2f}x)")
print(f"  [8] EMA8 > EMA21           {ok(ema_ok)}  ({ema8:.2f} vs {ema21:.2f})")

all_ok = fund_ok and rs_ok and rsi_ok and vol_ok and ema_ok

print()
if all_ok:
    mode = "CONFIRMED SQUEEZE" if confirmed else "EARLY SQUEEZE"
    print(f"  {GREEN}{BOLD}VERDICT: {mode} — ready to enter{RESET}")
elif fund_ok and rs_ok:
    missing = []
    if not rsi_ok:
        rsi_val = f"{ctx.rsi:.0f}" if ctx.rsi else "N/A"
        missing.append(f"RSI={rsi_val} (need 38-70)")
    if not vol_ok:
        missing.append(f"Vol={ctx.vol_ratio:.2f}x (need >=1.1x)")
    if not ema_ok:
        missing.append(f"EMA8({ema8:.2f}) <= EMA21({ema21:.2f})")
    print(f"  {YELLOW}{BOLD}VERDICT: RS qualifies but not ready — needs: {', '.join(missing)}{RESET}")
elif fund_ok:
    rs_gap = ""
    if rs_13w is not None:
        rs_gap = f" (RS13W={rs_13w:+.0f}%, need >=10% OR RS5d={rs_5d:+.1f}%, need >=15%)"
    print(f"  {RED}VERDICT: Fundamentals pass but RS too low{rs_gap}{RESET}")
else:
    print(f"  {RED}VERDICT: Does not qualify — failed fundamentals gate{RESET}")
print()
