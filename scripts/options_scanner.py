"""
options_scanner.py
------------------
Runnable every 5-30 minutes during market hours to:
  1. Pull fresh TI tickers + Seeking Alpha market data
  2. Scan all tickers through every options strategy
  3. Show ranked signals with backtest P&L (last N days)
  4. Optionally place live orders via the OptionsExecutor

Usage:
    # Scan only (dry-run, no orders):
    apextrader\\Scripts\\python.exe scripts\\options_scanner.py

    # Place orders for signals above threshold:
    apextrader\\Scripts\\python.exe scripts\\options_scanner.py --execute

    # Set minimum confidence:
    apextrader\\Scripts\\python.exe scripts\\options_scanner.py --conf 0.70 --execute

    # Limit universe (faster):
    apextrader\\Scripts\\python.exe scripts\\options_scanner.py --tickers AAPL NVDA TSLA --execute

    # Run once every N minutes in a loop:
    apextrader\\Scripts\\python.exe scripts\\options_scanner.py --loop 15 --execute
"""

import sys
import os
import argparse
import math
import time
import datetime
import warnings
warnings.filterwarnings("ignore")
import logging
from pathlib import Path
from typing import List, Optional, Dict

# Force UTF-8 output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

# ── CLI ───────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="ApexTrader Options Scanner")
parser.add_argument("--tickers",  nargs="*", default=[],         help="Override universe (space-separated)")
parser.add_argument("--conf",     type=float, default=0.65,       help="Min confidence to show/execute (default 0.65)")
parser.add_argument("--execute",  action="store_true",            help="Place live orders via OptionsExecutor")
parser.add_argument("--dry-run",  action="store_true",            help="Alias for no --execute (default)")
parser.add_argument("--loop",     type=int,   default=0,          help="Repeat every N minutes (0 = run once)")
parser.add_argument("--top",      type=int,   default=10,         help="Max signals to show per run (default 10)")
parser.add_argument("--bt-days",  type=int,   default=20,         help="Backtest lookback days (default 20)")
parser.add_argument("--ti-only",  action="store_true",            help="Use only latest TI tickers")
parser.add_argument("--no-sa",    action="store_true",            help="Skip Seeking Alpha API calls")
parser.add_argument("--no-bt",    action="store_true",            help="Skip backtest (faster scans)")
parser.add_argument("--paper",    action="store_true",            help="Force paper mode for this run")
parser.add_argument("--highprob", action="store_true",            help="90%% PoP mode: liquid ETFs/mega-caps, IronCondor only, IVR>=50")
args = parser.parse_args()

if args.paper:
    os.environ["TRADE_MODE"] = "paper"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,  # suppress internal lib noise
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("OptionsScanner")
log.setLevel(logging.INFO)

# ── Imports after env ─────────────────────────────────────────────────────────
from engine import config as cfg
from engine.utils.market import MarketState
from engine.utils import get_bars
from engine.options.strategies import (
    MomentumCallStrategy,
    BearPutStrategy,
    BearCallSpreadStrategy,
    ShortSqueezeStrategy,
    IronCondorStrategy,
    ButterflyStrategy,
    BreakoutRetestCallStrategy,
    TrendPullbackSpreadStrategy,
    MeanReversionCallStrategy,
    OptionSignal,
    _bar_ctx_cache,
    _market_state as _strat_ms,
)
import engine.options.strategies as _strats

STRATEGIES = [
    ("MomentumCall",        MomentumCallStrategy()),
    ("BearPut",             BearPutStrategy()),
    ("BearCallSpread",      BearCallSpreadStrategy()),
    ("ShortSqueeze",        ShortSqueezeStrategy()),
    ("IronCondor",          IronCondorStrategy()),
    ("Butterfly",           ButterflyStrategy()),
    ("BreakoutRetest",      BreakoutRetestCallStrategy()),
    ("TrendPullbackSpread", TrendPullbackSpreadStrategy()),
    ("MeanReversionCall",   MeanReversionCallStrategy()),
]

# Liquid large-caps / ETFs ideal for 90% PoP iron condors (high OI, tight spreads)
HIGH_PROB_TICKERS = [
    "SPY", "QQQ", "IWM", "GLD", "TLT", "SLV", "EEM", "EFA",
    "XLF", "XLE", "XLK", "XLV", "XLU", "XLI", "XLP", "XLB",
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
    "JPM", "BAC", "GS", "C",
    "AMGN", "COST", "HD",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_ti_tickers() -> List[str]:
    ti_file = ROOT / "data" / "ti_primary.json"
    if not ti_file.exists():
        return []
    import json
    try:
        data = json.loads(ti_file.read_text(encoding="utf-8"))
        return [t.strip().upper() for t in data.get("tickers", []) if t.strip()]
    except Exception as e:
        log.warning(f"TI tickers load error: {e}")
        return []


def _load_sa_tickers(market_state) -> tuple:
    """Returns (sa_tickers, sentiment_str, bull_pct)"""
    if args.no_sa:
        return [], "neutral", 0.5
    try:
        from engine.data.seeking_alpha import (
            get_sa_market_outlook, get_sa_day_watch, get_sa_leading_story,
        )
        outlook  = get_sa_market_outlook()
        sentiment = outlook.get("sentiment", "neutral")
        bull_pct  = outlook.get("bullish_pct", 0.5)

        dw       = get_sa_day_watch()
        gainers  = dw.get("top_gainers",   [])[:8]
        sp500g   = dw.get("sp500_gainers", [])[:5]
        active   = dw.get("most_active",   [])[:5]
        ls       = get_sa_leading_story()[:3]
        tickers  = list(dict.fromkeys(gainers + sp500g + active + ls))
        return tickers, sentiment, bull_pct
    except Exception as e:
        log.warning(f"SA API error: {e}")
        return [], "neutral", 0.5


def _backtest_signal(sig: OptionSignal, days: int) -> Optional[dict]:
    """Simple equity-proxy backtest: simulate entering long/short on the underlying."""
    try:
        raw = get_bars(sig.symbol, f"{days + 30}d", "1d")
        if raw is None or raw.empty or len(raw) < days + 2:
            return None
        import pandas as pd
        df = raw.copy()
        df["_date"] = pd.to_datetime(df["time"]).dt.normalize().dt.date
        df = df.drop_duplicates("_date").set_index("_date").sort_index()
        window = df.index.tolist()[-(days + 1):]
        direction = "buy" if "call" in sig.option_type.lower() else "sell"
        TP = cfg.OPTIONS_PROFIT_TARGET_PCT / 100
        SL = cfg.OPTIONS_STOP_LOSS_PCT / 100
        wins = losses = flat = 0
        total_pnl = 0.0
        for i in range(len(window) - 1):
            entry_date  = window[i + 1]
            row         = df.loc[entry_date]
            entry_open  = float(row["open"])
            entry_high  = float(row["high"])
            entry_low   = float(row["low"])
            entry_close = float(row["close"])
            if entry_open <= 0:
                continue
            if direction == "buy":
                tp_p = entry_open * (1 + TP)
                sl_p = entry_open * (1 - SL)
                if entry_high >= tp_p:
                    pnl = TP; wins += 1
                elif entry_low <= sl_p:
                    pnl = -SL; losses += 1
                else:
                    pnl = (entry_close - entry_open) / entry_open; flat += 1
            else:
                tp_p = entry_open * (1 - TP)
                sl_p = entry_open * (1 + SL)
                if entry_low <= tp_p:
                    pnl = TP; wins += 1
                elif entry_high >= sl_p:
                    pnl = -SL; losses += 1
                else:
                    pnl = (entry_open - entry_close) / entry_open; flat += 1
            total_pnl += pnl
        n = wins + losses + flat
        if n == 0:
            return None
        win_rate  = wins / n
        avg_pnl   = total_pnl / n
        expectancy = win_rate * TP - (1 - win_rate) * SL
        return {"trades": n, "win_rate": win_rate, "avg_pnl": avg_pnl,
                "expectancy": expectancy, "total_pnl": total_pnl}
    except Exception:
        return None


def _sep(char="-", width=78):
    print(char * width)


def _run_scan():
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _sep("=")
    print(f"  ApexTrader Options Scanner   {ts}   mode={'PAPER' if cfg.PAPER else 'LIVE'}")
    _sep("=")

    # ── 1. Market state ───────────────────────────────────────────────────────
    ms = MarketState.from_now()
    ms.resolve_regime()
    sentiment = ms.resolve_sentiment()
    _strats._market_state = ms  # inject into strategies module

    print(f"\n  Regime: {ms.regime.upper():10} | Sentiment: {sentiment.upper():10} | "
          f"Market open: {'YES' if ms.is_market_open else 'NO'}")

    # ── 2. SA data ────────────────────────────────────────────────────────────
    sa_tickers, sa_sentiment, sa_bull = _load_sa_tickers(ms)
    if sa_tickers:
        # Blend SA sentiment
        if sa_bull >= 0.65:
            sa_sent_str = "bullish"
        elif sa_bull <= 0.35:
            sa_sent_str = "bearish"
        else:
            sa_sent_str = "neutral"
        print(f"  SA Outlook: {sa_sent_str.upper():10} (bull={sa_bull:.0%}) | "
              f"SA tickers: {sa_tickers[:8]}")

    # ── 3. Build universe ─────────────────────────────────────────────────────
    if args.highprob:
        # 90% PoP mode: only liquid tickers, only IronCondor strategy
        universe = HIGH_PROB_TICKERS
        print(f"\n  Universe: HIGH-PROB 90%% PoP mode ({len(universe)} liquid tickers, IronCondor only)")
        print(f"  Short legs at 0.10 delta => ~90%% probability of profit (expires worthless)")
        print(f"  IV rank >= 50 required (sell expensive premium)")
        active_strategies = [("IronCondor", IronCondorStrategy())]
    elif args.tickers:
        universe = [t.upper() for t in args.tickers]
        active_strategies = STRATEGIES
        print(f"\n  Universe: CLI override ({len(universe)}): {universe}")
    elif args.ti_only:
        ti_tickers = _load_ti_tickers()
        universe = list(dict.fromkeys(ti_tickers))
        active_strategies = STRATEGIES
        print(f"\n  Universe: TI latest only ({len(universe)}): {universe[:20]}{' ...' if len(universe) > 20 else ''}")
    else:
        ti_tickers  = _load_ti_tickers()
        options_uni = cfg.get_options_universe()
        universe    = list(dict.fromkeys(
            options_uni[:40] + ti_tickers[:30] + sa_tickers
        ))
        print(f"\n  Universe: {len(universe)} tickers  "
              f"(options_universe={min(40,len(options_uni))}, "
              f"TI={min(30,len(ti_tickers))}, SA={len(sa_tickers)})")
        active_strategies = STRATEGIES

    # ── 4. Scan ───────────────────────────────────────────────────────────────
    print(f"\n  Scanning {len(universe)} tickers x {len(active_strategies)} strategies ...")
    _bar_ctx_cache.clear()

    signals: List[tuple] = []  # (confidence, strat_name, signal)

    for sym in universe:
        for strat_name, strat in active_strategies:
            try:
                sig = strat.scan(sym)
                if sig and sig.confidence >= args.conf:
                    signals.append((sig.confidence, strat_name, sig))
            except Exception as e:
                log.debug(f"  {sym}/{strat_name} error: {e}")

    signals.sort(key=lambda x: x[0], reverse=True)
    top_signals = signals[:args.top]

    if not top_signals:
        print(f"\n  No signals above conf={args.conf:.0%}")
        if not ms.is_market_open:
            print("  (Market is closed — options chain data requires open market)")
        return

    # ── 5. Backtest ───────────────────────────────────────────────────────────
    bt_results: Dict[str, Optional[dict]] = {}
    if not args.no_bt:
        print(f"  Running {args.bt_days}-day backtest on top {len(top_signals)} signals ...")
        for _, _, sig in top_signals:
            key = f"{sig.symbol}:{sig.option_type}"
            if key not in bt_results:
                bt_results[key] = _backtest_signal(sig, args.bt_days)

    # ── 6. Print results ──────────────────────────────────────────────────────
    print(f"\n  {'#':<3} {'TICKER':<7} {'TYPE':<18} {'STRAT':<18} {'CONF':>5}  "
          f"{'STRIKE':>7}  {'DTE':>4}  {'MID':>6}  {'IV%':>5}  "
          f"{'WIN%':>5}  {'EXP%':>6}  REASON")
    _sep()

    for rank, (conf, strat_name, sig) in enumerate(top_signals, 1):
        key = f"{sig.symbol}:{sig.option_type}"
        bt  = bt_results.get(key)
        dte = (sig.expiry - datetime.date.today()).days
        win_str = f"{bt['win_rate']:>5.0%}" if bt else "  n/a"
        exp_str = f"{bt['expectancy']:>+6.2%}" if bt else "   n/a"
        iv_str  = f"{sig.iv_pct:>5.1f}" if sig.iv_pct else "  n/a"
        reason_short = sig.reason[:45] if sig.reason else ""
        print(
            f"  {rank:<3} {sig.symbol:<7} {sig.option_type:<18} {strat_name:<18} {conf:>5.0%}  "
            f"{sig.strike:>7.2f}  {dte:>4}  {sig.mid_price:>6.3f}  {iv_str}  "
            f"{win_str}  {exp_str}  {reason_short}"
        )

    _sep()

    # Positive expectancy picks
    if not args.no_bt:
        top_picks = [(c, s, sg) for (c, s, sg) in top_signals
                     if bt_results.get(f"{sg.symbol}:{sg.option_type}", {}) and
                     bt_results[f"{sg.symbol}:{sg.option_type}"]["expectancy"] > 0]
        if top_picks:
            print(f"\n  [*] POSITIVE EXPECTANCY PICKS:")
            for conf, strat_name, sig in top_picks:
                bt = bt_results[f"{sig.symbol}:{sig.option_type}"]
                dte = (sig.expiry - datetime.date.today()).days
                print(
                    f"    {sig.symbol:>6} {sig.option_type:<18} strike={sig.strike:.2f}  "
                    f"exp={sig.expiry}  DTE={dte}  mid=${sig.mid_price:.3f}  "
                    f"conf={conf:.0%}  win={bt['win_rate']:.0%}  exp={bt['expectancy']:+.2%}"
                )

    # ── 7. Execute ────────────────────────────────────────────────────────────
    if args.execute and not args.dry_run:
        if not ms.is_market_open and not cfg.FORCE_SCAN:
            print(f"\n  [SKIP EXECUTION] Market is closed. Use FORCE_SCAN=true to override.")
            return

        print(f"\n  Placing orders (conf >= {args.conf:.0%}, mode={'PAPER' if cfg.PAPER else 'LIVE'}) ...")

        from alpaca.trading.client import TradingClient
        from engine.options.executor import OptionsExecutor

        client   = TradingClient(cfg.API_KEY, cfg.API_SECRET, paper=cfg.PAPER)
        executor = OptionsExecutor(client)
        executor.update_market_state(ms)

        placed = 0
        for conf, strat_name, sig in top_signals:
            # Only execute positive-expectancy signals when backtest data is available
            key = f"{sig.symbol}:{sig.option_type}"
            bt  = bt_results.get(key)
            if bt and bt["expectancy"] < 0:
                log.info(f"  [SKIP] {sig.symbol} {sig.option_type} - negative expectancy ({bt['expectancy']:+.2%})")
                continue
            try:
                ok = executor.place_option_order(sig, ms)
                if ok:
                    print(f"  [OK] ORDER PLACED: {sig.symbol} {sig.option_type.upper()} "
                          f"strike={sig.strike}  exp={sig.expiry}  mid=${sig.mid_price:.3f}  "
                          f"conf={conf:.0%}  [{strat_name}]")
                    placed += 1
                else:
                    print(f"  [NO] REJECTED:     {sig.symbol} {sig.option_type.upper()} "
                          f"(executor rejected - see logs)")
            except Exception as e:
                print(f"  [ERR] ERROR:        {sig.symbol} {sig.option_type.upper()} - {e}")

        if placed == 0:
            print("  No orders placed this cycle.")
        else:
            print(f"\n  {placed} order(s) placed.")
    else:
        print(f"\n  [DRY-RUN] Pass --execute to place live orders.")

    print()


# ── Main / loop ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if args.loop > 0:
        print(f"Running in loop mode - every {args.loop} minutes. Ctrl-C to stop.")
        while True:
            try:
                _run_scan()
            except KeyboardInterrupt:
                print("\nStopped by user.")
                break
            except Exception as e:
                print(f"\n[ERROR] {e}")
            next_run = datetime.datetime.now() + datetime.timedelta(minutes=args.loop)
            print(f"  Next run at {next_run.strftime('%H:%M:%S')} ...")
            try:
                time.sleep(args.loop * 60)
            except KeyboardInterrupt:
                print("\nStopped by user.")
                break
    else:
        try:
            _run_scan()
        except KeyboardInterrupt:
            print("\nStopped by user.")

