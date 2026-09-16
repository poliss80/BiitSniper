"""
backtest_sa_preopen.py
----------------------
1. Fetches live Seeking Alpha data (outlook, day-watch, leading-story)
2. Builds a candidate ticker list (SA-injected + provided tickers)
3. Runs each ticker through all equity strategies to get confidence signals
4. Backtests the top signals over the last N trading days using daily bars
   (simple long equity sim: entry at next-day open, exit at TP or SL or EOD)
5. Prints a ranked summary table

Usage:
    apextrader\\Scripts\\python.exe scripts\\backtest_sa_preopen.py
    apextrader\\Scripts\\python.exe scripts\\backtest_sa_preopen.py --days 30
    apextrader\\Scripts\\python.exe scripts\\backtest_sa_preopen.py --tickers ESPR INTC AUID --days 20
    apextrader\\Scripts\\python.exe scripts\\backtest_sa_preopen.py --conf 0.65
"""

import sys
import argparse
import math
import datetime
import warnings
warnings.filterwarnings("ignore")
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import pandas as pd

# ── CLI args ──────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="SA preopen backtest")
parser.add_argument("--tickers", nargs="*", default=[], help="Extra tickers to include")
parser.add_argument("--days",    type=int,   default=20,  help="Lookback trading days (default 20)")
parser.add_argument("--conf",    type=float, default=0.60, help="Min confidence to backtest (default 0.60)")
parser.add_argument("--tp",      type=float, default=4.0,  help="Take-profit %% (default 4.0)")
parser.add_argument("--sl",      type=float, default=2.5,  help="Stop-loss %% (default 2.5)")
parser.add_argument("--no-sa",   action="store_true",       help="Skip SA API calls (offline mode)")
args = parser.parse_args()

# ── Step 1: Fetch SA data ─────────────────────────────────────────────────────
print("=" * 65)
print("STEP 1 — Seeking Alpha live data")
print("=" * 65)

sa_tickers: List[str] = []

if not args.no_sa:
    try:
        from engine.data.seeking_alpha import (
            get_sa_market_outlook, get_sa_day_watch, get_sa_leading_story,
        )
        outlook = get_sa_market_outlook()
        sentiment = outlook.get("sentiment", "neutral")
        bull_pct  = outlook.get("bullish_pct", 0.5)
        bear_pct  = outlook.get("bearish_pct", 0.5)
        print(f"  Market outlook : {sentiment.upper()}  bull={bull_pct:.0%}  bear={bear_pct:.0%}")
        for t in outlook.get("titles", [])[:3]:
            print(f"    \"{t[:80]}\"")

        dw = get_sa_day_watch()
        gainers      = dw.get("top_gainers",   [])[:8]
        sp500_gain   = dw.get("sp500_gainers", [])[:5]
        most_active  = dw.get("most_active",   [])[:5]
        print(f"  Top gainers    : {gainers}")
        print(f"  S&P500 gainers : {sp500_gain}")
        print(f"  Most active    : {most_active}")

        ls = get_sa_leading_story()
        print(f"  Leading story  : {ls[:6]}")

        sa_tickers = list(dict.fromkeys(gainers + sp500_gain + most_active + ls[:3]))
        print(f"\n  SA candidate tickers ({len(sa_tickers)}): {sa_tickers}")
    except Exception as e:
        print(f"  [WARN] SA API error: {e} — continuing without SA data")
else:
    print("  [SKIP] --no-sa flag set")

# Merge with CLI tickers
preopen_tickers = [
    "ESPR", "INTC", "AUID", "BTCY", "OLMA", "HNOI", "SCNX", "ORBS", "HLEO", "DEVS"
]
all_tickers = list(dict.fromkeys(preopen_tickers + args.tickers + sa_tickers))
print(f"\n  Combined candidate list ({len(all_tickers)}): {all_tickers}")

# ── Step 2: Strategy scan ─────────────────────────────────────────────────────
print("\n" + "=" * 65)
print("STEP 2 — Strategy confidence scan")
print("=" * 65)

from engine.equity.strategies import (
    SweepeaStrategy, TrendBreakerStrategy, SentimentStrategy,
    TechnicalStrategy, MomentumStrategy, GapBreakoutStrategy,
)
from engine.utils.market import MarketState

ms = MarketState.from_now()
ms.resolve_regime()
local_sent = ms.resolve_sentiment()
print(f"  Regime: {ms.regime}   Local sentiment: {local_sent}")

strategies = [
    SweepeaStrategy(),
    TrendBreakerStrategy(),
    SentimentStrategy(),
    TechnicalStrategy(),
    MomentumStrategy(),
    GapBreakoutStrategy(),
]

# Collect best signal per ticker
signals_map: dict = {}  # sym -> (strategy_name, action, confidence)

for sym in all_tickers:
    best = None
    for strat in strategies:
        try:
            sig = strat.scan(sym)
            if sig and sig.confidence >= args.conf:
                if best is None or sig.confidence > best[2]:
                    best = (type(strat).__name__.replace("Strategy", ""), sig.action, sig.confidence)
        except Exception:
            pass
    if best:
        signals_map[sym] = best

if not signals_map:
    print(f"\n  No signals above conf={args.conf:.0%} found.")
    print("  (This is expected on weekends — equity bar data is unavailable.)")
    print("  Try again Monday during market hours, or use --no-sa with known tickers.")
    sys.exit(0)

# Sort by confidence descending
ranked = sorted(signals_map.items(), key=lambda x: x[1][2], reverse=True)
print(f"\n  Signals found: {len(ranked)}")
print(f"  {'TICKER':<7} {'STRATEGY':<16} {'ACTION':<5} {'CONF':>6}")
print("  " + "-" * 38)
for sym, (strat, action, conf) in ranked:
    print(f"  {sym:<7} {strat:<16} {action:<5} {conf:>6.1%}")

# ── Step 3: Backtest ──────────────────────────────────────────────────────────
print("\n" + "=" * 65)
print(f"STEP 3 — Equity backtest  (last {args.days} trading days, TP={args.tp}%, SL={args.sl}%)")
print("=" * 65)

from engine.utils import get_bars

TP_PCT = args.tp / 100
SL_PCT = args.sl / 100

results = []

for sym, (strat, action, conf) in ranked:
    raw = get_bars(sym, f"{args.days + 60}d", "1d")
    if raw is None or raw.empty:
        print(f"  {sym}: no bar data — skipping backtest")
        continue

    df = raw.copy()
    df["_date"] = pd.to_datetime(df["time"]).dt.normalize().dt.date
    df = df.drop_duplicates("_date").set_index("_date").sort_index()

    # Use last N trading days
    trading_days = df.index.tolist()
    if len(trading_days) < args.days + 1:
        print(f"  {sym}: insufficient history ({len(trading_days)} days) — skipping")
        continue

    window = trading_days[-(args.days + 1):]  # +1 because entry is next-day open

    wins = losses = flat = 0
    total_pnl_pct = 0.0
    trade_count   = 0

    for i in range(len(window) - 1):
        entry_date  = window[i + 1]
        entry_open  = float(df.loc[entry_date, "open"])
        entry_high  = float(df.loc[entry_date, "high"])
        entry_low   = float(df.loc[entry_date, "low"])
        entry_close = float(df.loc[entry_date, "close"])

        if entry_open <= 0:
            continue

        if action == "buy":
            tp_price = entry_open * (1 + TP_PCT)
            sl_price = entry_open * (1 - SL_PCT)
            # Intraday: check if TP or SL was hit
            if entry_high >= tp_price:
                pnl = TP_PCT
                wins += 1
            elif entry_low <= sl_price:
                pnl = -SL_PCT
                losses += 1
            else:
                pnl = (entry_close - entry_open) / entry_open
                flat += 1
        else:  # sell / short
            tp_price = entry_open * (1 - TP_PCT)
            sl_price = entry_open * (1 + SL_PCT)
            if entry_low <= tp_price:
                pnl = TP_PCT
                wins += 1
            elif entry_high >= sl_price:
                pnl = -SL_PCT
                losses += 1
            else:
                pnl = (entry_open - entry_close) / entry_open
                flat += 1

        total_pnl_pct += pnl
        trade_count   += 1

    if trade_count == 0:
        continue

    win_rate  = wins / trade_count
    avg_pnl   = total_pnl_pct / trade_count
    expectancy = win_rate * TP_PCT - (1 - win_rate) * SL_PCT

    results.append({
        "sym":        sym,
        "strategy":   strat,
        "action":     action,
        "conf":       conf,
        "trades":     trade_count,
        "wins":       wins,
        "losses":     losses,
        "flat":       flat,
        "win_rate":   win_rate,
        "avg_pnl":    avg_pnl,
        "total_pnl":  total_pnl_pct,
        "expectancy": expectancy,
    })

if not results:
    print("  No backtest results (insufficient bar data for all tickers).")
    sys.exit(0)

# ── Step 4: Print ranked results ──────────────────────────────────────────────
print("\n" + "=" * 65)
print("STEP 4 — Results (ranked by expectancy)")
print("=" * 65)

res_df = pd.DataFrame(results).sort_values("expectancy", ascending=False)

# Summary header
print(f"\n  {'TICKER':<7} {'STRAT':<14} {'ACT':<4} {'CONF':>5}  {'TR':>3}  {'WIN%':>5}  {'AVG%':>6}  {'TOT%':>7}  {'EXP%':>6}")
print("  " + "-" * 68)
for _, row in res_df.iterrows():
    print(
        f"  {row['sym']:<7} {row['strategy']:<14} {row['action']:<4} {row['conf']:>5.1%}  "
        f"{int(row['trades']):>3}  {row['win_rate']:>5.1%}  {row['avg_pnl']:>+6.2%}  "
        f"{row['total_pnl']:>+7.2%}  {row['expectancy']:>+6.2%}"
    )

# Overall
total_trades  = res_df["trades"].sum()
overall_wr    = (res_df["wins"].sum() / total_trades) if total_trades else 0
overall_pnl   = res_df["total_pnl"].mean()
overall_exp   = res_df["expectancy"].mean()

print("\n  " + "-" * 68)
print(f"  {'OVERALL':<7} {'':14} {'':4} {'':5}  {int(total_trades):>3}  {overall_wr:>5.1%}  {'':6}  {overall_pnl:>+7.2%}  {overall_exp:>+6.2%}")

# Top picks
top = res_df[res_df["expectancy"] > 0].head(5)
if not top.empty:
    print("\n  ★  TOP PICKS (positive expectancy):")
    for _, row in top.iterrows():
        print(f"     {row['sym']}  {row['action'].upper()}  conf={row['conf']:.0%}  "
              f"win={row['win_rate']:.0%}  exp={row['expectancy']:+.2%}")
else:
    print("\n  [!] No tickers with positive expectancy in this window.")

print("\nDone.")
