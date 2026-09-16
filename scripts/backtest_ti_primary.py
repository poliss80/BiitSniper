"""
backtest_ti_primary.py
-----------------------
Backtest the latest Trade Ideas primary tickers from data/ti_primary.json.
Defaults to the most recent 14-day window and a limit of 50 tickers to keep
execution manageable.

Usage:
    python scripts/backtest_ti_primary.py --limit 50 --days 14
    python scripts/backtest_ti_primary.py --sa-gate               # filter by SA/yfinance sentiment
    python scripts/backtest_ti_primary.py --sa-gate --sa-min 0.55 # custom bullish threshold
"""

import argparse
import datetime
import os
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from engine.equity.universe import get_ti_primary
from scripts.backtest_options import backtest_symbol, OPTIONS_ALLOCATION_PCT, OPTIONS_PROFIT_TARGET_PCT, OPTIONS_STOP_LOSS_PCT, OPTIONS_DTE_MIN, OPTIONS_DTE_MAX


def parse_date(value: str) -> datetime.date:
    return datetime.date.fromisoformat(value)


def _fetch_sentiment_bulk(tickers: list, verbose: bool = False) -> Dict[str, Tuple[bool, float]]:
    """Return {ticker: (passes_gate, bullish_pct)} for each ticker.

    Uses SA quant rating → SA news → yfinance recommendationKey fallback chain
    (via engine.utils.data.check_sentiment_gate).  The SA circuit breaker means
    after the first 3 SA timeouts the remaining tickers resolve instantly via
    yfinance.
    """
    from engine.utils.data import check_sentiment_gate
    results: Dict[str, Tuple[bool, float]] = {}
    print(f"\nFetching SA/yfinance sentiment for {len(tickers)} tickers…", flush=True)
    for sym in tickers:
        passes, pct = check_sentiment_gate(sym)
        results[sym] = (passes, pct)
        if verbose:
            tag = "PASS" if passes else "SKIP"
            print(f"  {sym:6s}  [{tag}]  bullish={pct:.2f}")
    passed = sum(1 for p, _ in results.values() if p)
    threshold_str = os.environ.get("SENTIMENT_BULLISH_THRESHOLD", os.getenv("SENTIMENT_BULLISH_THRESHOLD", "0.60"))
    print(f"SA gate: {passed}/{len(tickers)} tickers pass  "
          f"(threshold={threshold_str})\n")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest latest TI primary tickers")
    parser.add_argument("--limit", type=int, default=50, help="Maximum number of latest TI tickers to test")
    parser.add_argument("--days", type=int, default=14, help="Number of days to backtest")
    parser.add_argument("--start", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", help="End date YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=10000.0, help="Initial capital")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--ignore-stale", action="store_true", help="Use raw data/ti_primary.json even if it is marked stale")
    parser.add_argument("--use-finnhub", action="store_true", help="Fetch bar history from Finnhub API if available")
    # SA sentiment gate
    parser.add_argument("--sa-gate", action="store_true",
                        help="Pre-filter tickers by Seeking Alpha (or yfinance fallback) sentiment before backtesting")
    parser.add_argument("--sa-min", type=float, default=None,
                        help="Override bullish_pct threshold for --sa-gate (default: uses SENTIMENT_BULLISH_THRESHOLD from config)")
    args = parser.parse_args()

    if args.use_finnhub:
        os.environ["USE_FINNHUB_HISTORICAL"] = "true"

    if args.start:
        start = parse_date(args.start)
    else:
        start = datetime.date.today() - datetime.timedelta(days=args.days)
    if args.end:
        end = parse_date(args.end)
    else:
        end = datetime.date.today()

    tickers = get_ti_primary()
    if not tickers and args.ignore_stale:
        import json
        raw_path = Path(ROOT) / "data" / "ti_primary.json"
        if raw_path.exists():
            data = json.loads(raw_path.read_text(encoding="utf-8"))
            tickers = [str(t).strip().upper() for t in data.get("tickers", []) if isinstance(t, str) and t.strip()]
            print("Warning: using raw ti_primary.json data even though it is stale")

    if not tickers:
        print("No fresh TI primary tickers available in data/ti_primary.json")
        return 1

    tickers = tickers[: args.limit] if args.limit and len(tickers) > args.limit else tickers

    # ── SA sentiment pre-filter ──────────────────────────────────────────────
    sentiment: Dict[str, Tuple[bool, float]] = {}
    if args.sa_gate:
        if args.sa_min is not None:
            # propagate override so sa_sentiment_gate uses it
            os.environ["SENTIMENT_BULLISH_THRESHOLD"] = str(args.sa_min)
        sentiment = _fetch_sentiment_bulk(tickers, verbose=args.verbose)
        original_count = len(tickers)
        # Filter on passes (SA's own gate boolean).
        # When SA is unreachable it returns (True, 0.5) — allow-by-default so
        # an API outage never blocks all trades.
        tickers = [t for t in tickers if sentiment.get(t, (True, 0.5))[0]]
        print(f"SA gate removed {original_count - len(tickers)} tickers that SA rated below threshold")

    print(f"Backtesting {len(tickers)} TI tickers: {', '.join(tickers[:10])}{'...' if len(tickers) > 10 else ''}")
    print(f"Date range: {start} → {end}")
    print(f"Capital: ${args.capital:,.0f}")
    print(f"Rules: TP={OPTIONS_PROFIT_TARGET_PCT:.0f}% SL=-{OPTIONS_STOP_LOSS_PCT:.0f}% DTE={OPTIONS_DTE_MIN}–{OPTIONS_DTE_MAX}")
    if args.sa_gate:
        print("SA gate: ON  (SA quant rating → yfinance fallback)")
    print("=" * 80)

    all_trades = []
    for symbol in tickers:
        sa_tag = ""
        if sentiment:
            _, pct = sentiment.get(symbol, (True, 0.5))
            sa_tag = f"  [SA={pct:.2f}]"
        print(f"\n{symbol}{sa_tag}:", end=" ")
        trades = backtest_symbol(symbol, start, end, args.capital, args.verbose)
        if trades.empty:
            print("no trades")
            continue
        trades["symbol"] = symbol
        if sentiment:
            trades["sa_bullish_pct"] = sentiment.get(symbol, (True, 0.5))[1]
        all_trades.append(trades)
        wins = trades[trades['pnl_$'] > 0]
        total_pnl = trades['pnl_$'].sum()
        print(f"{len(trades)} trades | win={len(wins)}/{len(trades)} ({100*len(wins)/len(trades):.0f}%) | P&L=${total_pnl:+,.2f}")

    # ── Summary ──────────────────────────────────────────────────────────────
    if all_trades:
        import pandas as pd
        combined = pd.concat(all_trades, ignore_index=True)
        total_trades = len(combined)
        total_wins   = (combined['pnl_$'] > 0).sum()
        total_pnl    = combined['pnl_$'].sum()
        win_rate     = 100 * total_wins / total_trades if total_trades else 0.0
        print("\n" + "=" * 80)
        print(f"SUMMARY  {len(tickers)} tickers | {total_trades} trades | "
              f"win={total_wins}/{total_trades} ({win_rate:.0f}%) | total P&L=${total_pnl:+,.2f}")
        if args.sa_gate and "sa_bullish_pct" in combined.columns:
            # Breakdown: above vs below median SA score
            med = combined["sa_bullish_pct"].median()
            hi  = combined[combined["sa_bullish_pct"] >= med]
            lo  = combined[combined["sa_bullish_pct"] <  med]
            if not hi.empty:
                hi_wr = 100 * (hi['pnl_$'] > 0).sum() / len(hi)
                print(f"  SA >= {med:.2f} ({len(hi)} trades): win={hi_wr:.0f}%  P&L=${hi['pnl_$'].sum():+,.2f}")
            if not lo.empty:
                lo_wr = 100 * (lo['pnl_$'] > 0).sum() / len(lo)
                print(f"  SA <  {med:.2f} ({len(lo)} trades): win={lo_wr:.0f}%  P&L=${lo['pnl_$'].sum():+,.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
