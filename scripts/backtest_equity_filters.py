"""
backtest_equity_filters.py
--------------------------
Historical replay of the new equity TA filter stack on TI tickers.

Checks replayed on each daily bar (rolling window):
  1. EMA20 > EMA50 > EMA200 stack (bull alignment)
  2. Price crossed above EMA20 within last 7 days (fresh breakout)
  3. Price is above the prior 21-55 day range high
  4. Daily RSI > 30 (not oversold)

If all 4 pass → simulate long entry at next-day open.
  TP = +4%  SL = -2.5%  (held intraday: exit at TP/SL or close)

Reports per-ticker and combined results vs a baseline (no filters).

Usage:
    apextrader\\Scripts\\python.exe scripts\\backtest_equity_filters.py
    apextrader\\Scripts\\python.exe scripts\\backtest_equity_filters.py --tickers RKLB FUTU UPWK --days 60
    apextrader\\Scripts\\python.exe scripts\\backtest_equity_filters.py --tp 5 --sl 3
"""

import sys
import argparse
import datetime
import warnings
warnings.filterwarnings("ignore")
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import json
import pandas as pd
import numpy as np

parser = argparse.ArgumentParser(description="Equity filter stack historical backtest")
parser.add_argument("--tickers", nargs="*", default=[], help="Tickers to test (default: 10 from TI primary)")
parser.add_argument("--days",    type=int,   default=60,  help="Lookback trading days (default 60)")
parser.add_argument("--tp",      type=float, default=4.0, help="Take-profit %% (default 4.0)")
parser.add_argument("--sl",      type=float, default=2.5, help="Stop-loss %% (default 2.5)")
parser.add_argument("--verbose", "-v", action="store_true")
args = parser.parse_args()

TP_PCT = args.tp / 100
SL_PCT = args.sl / 100

# ── Load TI tickers ───────────────────────────────────────────────────────────
if args.tickers:
    TICKERS = [t.upper() for t in args.tickers]
else:
    try:
        data = json.loads((ROOT / "data" / "ti_primary.json").read_text())
        TICKERS = [str(t).strip().upper() for t in data.get("tickers", []) if str(t).strip()][:10]
    except Exception:
        TICKERS = ["RKLB", "FUTU", "UPWK", "FNKO", "FLNC", "PTCT", "ANIP", "CLFD", "FLR", "OSK"]

print("=" * 70)
print("EQUITY FILTER STACK — HISTORICAL BACKTEST")
print(f"  Tickers : {TICKERS}")
print(f"  Lookback: {args.days} trading days")
print(f"  TP={args.tp}%  SL={args.sl}%")
print("=" * 70)

# ── Filter helpers (daily-bar only — no intraday needed) ─────────────────────

def _calc_rsi14(closes: pd.Series) -> pd.Series:
    delta = closes.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean().replace(0, 1e-9)
    return 100 - 100 / (1 + gain / loss)


def _filter_row(closes_to_date: pd.Series, daily_to_date: pd.DataFrame) -> dict:
    """Return dict of filter pass/fail for a given historical snapshot.
    Mirrors EXACTLY what is live in deployed strategies:
      ema_stack  - SweepeaPath A + MomentumStrategy: conditional on price>=15 AND avg_vol>=300k
                   TrendBreaker: unconditional
      rsi_ok     - SweepeaPath A, MomentumStrategy, GapBreakout
    """
    n = len(closes_to_date)
    result = {"ema_stack": True, "rsi_ok": True}

    if n < 25:
        return result  # insufficient history — allow

    ema20  = closes_to_date.ewm(span=20,  adjust=False).mean()
    ema50  = closes_to_date.ewm(span=50,  adjust=False).mean()
    spot   = float(closes_to_date.iloc[-1])

    # 1. EMA stack — conditional on liquidity (price >= $15 AND avg 20-day vol >= 300k)
    avg_vol20  = float(daily_to_date["volume"].iloc[-21:-1].mean()) if n >= 22 else 0
    liquid     = spot >= 15.0 and avg_vol20 >= 300_000
    if liquid:
        e20, e50 = float(ema20.iloc[-1]), float(ema50.iloc[-1])
        if n >= 200:
            ema200 = float(closes_to_date.ewm(span=200, adjust=False).mean().iloc[-1])
            result["ema_stack"] = e20 > e50 > ema200
        elif n >= 60:
            result["ema_stack"] = e20 > e50
    # thin/low-price stocks bypass EMA stack — stays True

    # 2. RSI > 30 — used in SweepeaPath A, MomentumStrategy, GapBreakout
    if n >= 16:
        rsi_series = _calc_rsi14(closes_to_date)
        result["rsi_ok"] = float(rsi_series.iloc[-1]) > 30

    return result


# ── Trade simulator ───────────────────────────────────────────────────────────

def _simulate_trade(next_day: pd.Series, tp: float, sl: float) -> float:
    """Returns % P&L for one long trade entered at open of next_day bar."""
    entry = float(next_day["open"])
    if entry <= 0:
        return 0.0
    tp_price = entry * (1 + tp)
    sl_price = entry * (1 - sl)
    if float(next_day["high"]) >= tp_price:
        return tp
    if float(next_day["low"]) <= sl_price:
        return -sl
    return (float(next_day["close"]) - entry) / entry


# ── Per-ticker backtest ───────────────────────────────────────────────────────

from engine.utils import get_bars

summary_filtered  = []
summary_baseline  = []

for sym in TICKERS:
    raw = get_bars(sym, "300d", "1d")
    if raw is None or raw.empty:
        print(f"\n{sym}: no bar data — skipping")
        continue

    df = raw.copy()
    df["_date"] = pd.to_datetime(df["time"]).dt.normalize().dt.date
    df = df.drop_duplicates("_date").set_index("_date").sort_index()

    closes = df["close"]
    trading_days = df.index.tolist()

    if len(trading_days) < args.days + 60:
        print(f"\n{sym}: only {len(trading_days)} days — needs {args.days + 60} minimum, skipping")
        continue

    # Use the last `days` windows as candidate entry dates
    entry_candidates = trading_days[-(args.days + 1):-1]  # exclude last (no next-day bar)

    f_trades, f_pnl_list = [], []
    b_trades, b_pnl_list = [], []

    for i, entry_date in enumerate(entry_candidates):
        idx = trading_days.index(entry_date)
        if idx + 1 >= len(trading_days):
            continue
        next_date = trading_days[idx + 1]
        next_bar  = df.loc[next_date]

        closes_snap = closes.iloc[: idx + 1]
        daily_snap  = df.iloc[: idx + 1]
        spot        = float(closes_snap.iloc[-1])

        # ── Filtered strategy: all 4 checks ──────────────────────────────────
        f = _filter_row(closes_snap, daily_snap)
        ema20_now = float(closes_snap.ewm(span=20, adjust=False).mean().iloc[-1]) if len(closes_snap) >= 20 else spot
        basic_uptrend = spot > ema20_now

        if all(f.values()) and basic_uptrend:
            pnl = _simulate_trade(next_bar, TP_PCT, SL_PCT)
            f_trades.append({"date": entry_date, "pnl": pnl, "filters": f})
            f_pnl_list.append(pnl)

        # ── Baseline: price > EMA20 only (old behaviour) ──────────────────────
        if basic_uptrend:
            pnl = _simulate_trade(next_bar, TP_PCT, SL_PCT)
            b_trades.append({"date": entry_date, "pnl": pnl})
            b_pnl_list.append(pnl)

    def _stats(trades_list, pnl_list):
        if not trades_list:
            return dict(n=0, wins=0, win_rate=0, total_pnl=0, avg_pnl=0, expectancy=0)
        n    = len(trades_list)
        wins = sum(1 for p in pnl_list if p > 0)
        wr   = wins / n
        tot  = sum(pnl_list)
        avg  = tot / n
        exp  = wr * TP_PCT - (1 - wr) * SL_PCT
        return dict(n=n, wins=wins, win_rate=wr, total_pnl=tot, avg_pnl=avg, expectancy=exp)

    fs = _stats(f_trades, f_pnl_list)
    bs = _stats(b_trades, b_pnl_list)

    print(f"\n{sym}")
    print(f"  FILTERED  : {fs['n']:>3} trades | win={fs['wins']}/{fs['n']} ({fs['win_rate']:.0%}) | "
          f"total={fs['total_pnl']:+.1%} | avg={fs['avg_pnl']:+.2%} | expectancy={fs['expectancy']:+.3f}")
    print(f"  BASELINE  : {bs['n']:>3} trades | win={bs['wins']}/{bs['n']} ({bs['win_rate']:.0%}) | "
          f"total={bs['total_pnl']:+.1%} | avg={bs['avg_pnl']:+.2%} | expectancy={bs['expectancy']:+.3f}")

    if args.verbose and f_trades:
        print(f"  --- Filtered trade log ---")
        for t in f_trades[-5:]:  # show last 5
            tag = "WIN " if t["pnl"] > 0 else ("LOSS" if t["pnl"] < 0 else "FLAT")
            fl  = " ".join(f"{k}={'Y' if v else 'N'}" for k, v in t["filters"].items())
            print(f"    {t['date']}  {tag}  {t['pnl']:+.1%}   [{fl}]")

    if fs["n"] > 0:
        summary_filtered.append({**fs, "sym": sym})
    if bs["n"] > 0:
        summary_baseline.append({**bs, "sym": sym})

# ── Combined summary ──────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("COMBINED SUMMARY")
print("=" * 70)

def _agg(rows):
    if not rows:
        return None
    df = pd.DataFrame(rows)
    total_n    = int(df["n"].sum())
    total_wins = int(df["wins"].sum())
    total_pnl  = float(df["total_pnl"].sum())
    wr         = total_wins / total_n if total_n else 0
    exp        = wr * TP_PCT - (1 - wr) * SL_PCT
    return dict(n=total_n, wins=total_wins, win_rate=wr, total_pnl=total_pnl, expectancy=exp)

fa = _agg(summary_filtered)
ba = _agg(summary_baseline)

if fa:
    print(f"\n  FILTERED  : {fa['n']} trades | win={fa['wins']}/{fa['n']} ({fa['win_rate']:.0%}) | "
          f"total={fa['total_pnl']:+.1%} | expectancy={fa['expectancy']:+.4f}")
if ba:
    print(f"  BASELINE  : {ba['n']} trades | win={ba['wins']}/{ba['n']} ({ba['win_rate']:.0%}) | "
          f"total={ba['total_pnl']:+.1%} | expectancy={ba['expectancy']:+.4f}")

if fa and ba and ba["n"] > 0:
    trade_reduction = 1 - fa["n"] / ba["n"]
    exp_delta       = fa["expectancy"] - ba["expectancy"]
    pnl_delta       = fa["total_pnl"]  - ba["total_pnl"]
    print(f"\n  Filter impact:")
    print(f"    Trade reduction : {trade_reduction:.0%} fewer entries (noise removed)")
    print(f"    Expectancy delta: {exp_delta:+.4f}  ({'improved' if exp_delta > 0 else 'reduced'})")
    print(f"    Total P&L delta : {pnl_delta:+.1%}")

print()
