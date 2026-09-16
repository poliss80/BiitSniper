"""Run ShortSqueeze screening — pass a ticker as argv[1] for single-symbol deep-dive, or run with no args for full TI universe scan."""
import json
import logging
import sys

logging.basicConfig(level=logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

with open("data/ti_unusual_options.json") as f:
    d = json.load(f)
tickers = d.get("tickers", [])
print(f"TI universe: {len(tickers)} tickers (updated {d.get('updated', '?')})\n")

from engine.options.strategies import (
    _fetch_squeeze_fundamentals,
    _fetch_squeeze_rs,
    _fetch_bar_context,
)
from engine.utils import get_bars

# Pre-fetch SPY 5d return once
spy_bars = get_bars("SPY", "10d", "1d")
spy_ret5d = 0.0
if len(spy_bars) >= 6:
    spy_ret5d = float(
        (spy_bars["close"].iloc[-1] - spy_bars["close"].iloc[-6])
        / spy_bars["close"].iloc[-6] * 100
    )

results = []
skipped = {"no_fund": 0, "low_short": 0, "neg_margin": 0, "low_revg": 0, "no_ctx": 0, "other": 0}

for sym in tickers:
    try:
        fund = _fetch_squeeze_fundamentals(sym)
        if fund is None:
            skipped["no_fund"] += 1
            continue
        sp = fund["short_pct_float"]
        gm = fund["gross_margins"]
        rg = fund["rev_growth"]
        if sp < 0.12:
            skipped["low_short"] += 1
            continue
        if gm <= 0:
            skipped["neg_margin"] += 1
            continue
        if rg < 0.08:
            skipped["low_revg"] += 1
            continue

        ctx = _fetch_bar_context(sym)
        if ctx is None:
            skipped["no_ctx"] += 1
            continue

        stk_ret5d = 0.0
        if len(ctx.closes) >= 6:
            stk_ret5d = float(
                (ctx.closes.iloc[-1] - ctx.closes.iloc[-6])
                / ctx.closes.iloc[-6] * 100
            )
        rs_5d  = stk_ret5d - spy_ret5d
        rs_13w = _fetch_squeeze_rs(sym)

        ema8  = float(ctx.closes.ewm(span=8,  adjust=False).mean().iloc[-1])
        ema21 = float(ctx.closes.ewm(span=21, adjust=False).mean().iloc[-1])

        results.append({
            "sym":          sym,
            "short_pct":    sp,
            "gross_margin": gm,
            "rev_growth":   rg,
            "rs_13w":       rs_13w,
            "rs_5d":        rs_5d,
            "rsi":          ctx.rsi,
            "vol_ratio":    ctx.vol_ratio,
            "ema_ok":       ema8 > ema21,
            "spot":         ctx.spot,
            "confirmed":    rs_13w is not None and rs_13w >= 10.0,
            "early":        rs_5d >= 15.0,
        })
    except Exception as e:
        skipped["other"] += 1


def _rank(r):
    return (int(r["confirmed"]) * 2 + int(r["early"]), r["short_pct"])

results.sort(key=_rank, reverse=True)

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

hdr = f"{'SYM':<6} {'SHORT%':>7} {'GM%':>6} {'REVG%':>7} {'RS13W':>7} {'RS5d':>7} {'RSI':>5} {'VRAT':>5} {'EMA':>5} {'SPOT':>8}  STATUS"
print(BOLD + hdr + RESET)
print("-" * 95)

for r in results:
    rs13_str  = f"{r['rs_13w']:+.0f}%" if r["rs_13w"] is not None else "  N/A"
    rsi_str   = f"{r['rsi']:.0f}" if r["rsi"] is not None else "N/A"
    ema_str   = "YES" if r["ema_ok"] else "NO"
    rsi_ok    = r["rsi"] is not None and 38 <= r["rsi"] <= 70
    vol_ok    = r["vol_ratio"] >= 1.1

    if r["confirmed"] and rsi_ok and vol_ok and r["ema_ok"]:
        status = GREEN + BOLD + "CONFIRMED SQUEEZE" + RESET
    elif r["early"] and rsi_ok and vol_ok and r["ema_ok"]:
        status = YELLOW + BOLD + "EARLY SQUEEZE" + RESET
    elif r["confirmed"] or r["early"]:
        missing = []
        if not rsi_ok:
            missing.append(f"RSI={rsi_str}")
        if not vol_ok:
            missing.append(f"VOL={r['vol_ratio']:.1f}x")
        if not r["ema_ok"]:
            missing.append("EMA")
        status = RED + "needs: " + " ".join(missing) + RESET
    else:
        status = "RS too low"

    line = (
        f"{r['sym']:<6} {r['short_pct']:>7.1%} {r['gross_margin']:>6.1%} "
        f"{r['rev_growth']:>7.1%} {rs13_str:>7} {r['rs_5d']:>+7.1f}% "
        f"{rsi_str:>5} {r['vol_ratio']:>5.2f} {ema_str:>5} ${r['spot']:>7.2f}  {status}"
    )
    print(line)

print()
print(
    f"RS-qualified: {len(results)} | "
    f"no yf data: {skipped['no_fund']} | "
    f"short% < 12%: {skipped['low_short']} | "
    f"neg margin: {skipped['neg_margin']} | "
    f"rev growth < 8%: {skipped['low_revg']} | "
    f"no price bars: {skipped['no_ctx']} | "
    f"errors: {skipped['other']}"
)
