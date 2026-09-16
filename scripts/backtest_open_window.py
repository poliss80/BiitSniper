"""
backtest_open_window.py
-----------------------
Backtest the open-window naked/spread strategy for a given date range.

Logic simulated:
  - Each trading day: determine regime (SPY vs 200-SMA)
  - Check pre-market gap (today open vs prev close)
  - Compute IV rank from 30d HV; decide naked vs spread
  - Enter ATM put (bear) or call (bull) at 9:35 open price, DTE=7
  - Stop: 25% of premium (open-window rule)
  - Target: 50% of premium
  - Spread short leg: 10% further OTM, valued via Black-Scholes
  - Hold up to 5 days or until stop/target hit

Usage:
    apextrader\\Scripts\\python.exe -m scripts.backtest_open_window
    apextrader\\Scripts\\python.exe -m scripts.backtest_open_window --start 2026-04-07 --end 2026-04-18
    apextrader\\Scripts\\python.exe -m scripts.backtest_open_window --symbols SPY QQQ AAPL --start 2026-04-07
"""

import sys, argparse, math, datetime
from pathlib import Path
from typing import Optional
import warnings
warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import pandas as pd
from engine.utils import get_bars
from engine.options._options_today import _calc_iv_rank

# â”€â”€ Constants â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
GAP_SPREAD_THRESH = 0.03   # force spread if gap > 3%
IV_NAKED_MAX      = 35.0   # force spread if IV rank > 35 (normal session) / 50 (open window)
OPEN_WIN_IV_MAX   = 50.0   # IV rank threshold for naked in the open window
STOP_PCT          = 25.0   # 25% of premium stop
TARGET_PCT        = 50.0   # 50% of premium target
MAX_HOLD_DAYS     = 5
DTE               = 7      # target DTE at entry
RISK_FREE         = 0.05
CONTRACT_SIZE     = 100

# â”€â”€ Default universe (Tier A + common options names) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
DEFAULT_SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "GOOGL", "META", "AMZN"]

# â”€â”€ Black-Scholes helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _norm_cdf(x: float) -> float:
    a = abs(x)
    t = 1.0 / (1.0 + 0.2316419 * a)
    p = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    r = 1.0 - (1 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * a * a) * p
    return r if x >= 0 else 1.0 - r

def _bs_price(spot: float, strike: float, dte: int, iv: float, call: bool = True) -> float:
    T = dte / 365.0
    if T <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return max(0.0, (spot - strike) if call else (strike - spot))
    try:
        d1 = (math.log(spot / strike) + (RISK_FREE + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
        d2 = d1 - iv * math.sqrt(T)
        if call:
            return max(0.0, spot * _norm_cdf(d1) - strike * math.exp(-RISK_FREE * T) * _norm_cdf(d2))
        else:
            return max(0.0, strike * math.exp(-RISK_FREE * T) * _norm_cdf(-d2) - spot * _norm_cdf(-d1))
    except Exception:
        return 0.0

def _hv30(closes: pd.Series) -> float:
    if len(closes) < 32:
        return 0.30
    return float(closes.pct_change().dropna().iloc[-30:].std()) * math.sqrt(252)

# â”€â”€ Regime â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _prep_bars(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalise bars: set date-only index from 'time' column."""
    df = raw.copy()
    df["_date"] = pd.to_datetime(df["time"]).dt.normalize().dt.date
    df = df.drop_duplicates("_date").set_index("_date").sort_index()
    return df


def _load_spy_200sma(start: datetime.date, end: datetime.date) -> pd.Series:
    """Returns a dateâ†’bool (True=bull) series."""
    bars = _prep_bars(get_bars("SPY", "300d", "1d"))
    bars["sma200"] = bars["close"].rolling(200).mean()
    bars["bull"]   = bars["close"] > bars["sma200"]
    idx_arr = pd.Index(bars.index)
    mask    = [(start <= d <= end) for d in idx_arr]
    return bars.loc[mask, "bull"]

# â”€â”€ Main backtest â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def backtest(symbols: list, start: datetime.date, end: datetime.date) -> pd.DataFrame:
    spy_regime = _load_spy_200sma(start, end)

    trades = []

    for sym in symbols:
        raw = get_bars(sym, "300d", "1d")
        if raw is None or raw.empty:
            print(f"  {sym}: no bar data â€” skipping")
            continue
        bars = _prep_bars(raw)

        dates = [d for d in bars.index if start <= d <= end and d.weekday() < 5]
        closes_all = bars["close"]

        for i, trade_date in enumerate(dates):
            ts = pd.Timestamp(trade_date)
            ts = trade_date  # already a date object
            if ts not in bars.index:
                continue
            idx = bars.index.get_loc(ts)
            if idx < 30:
                continue  # need history for HV30

            # â”€â”€ Regime â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            if trade_date not in spy_regime.index:
                continue
            is_bull = bool(spy_regime.loc[trade_date])

            # â”€â”€ Price context â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            today_row  = bars.loc[ts]
            prev_row   = bars.iloc[idx - 1]
            spot_open  = float(today_row["open"])   # 9:35 proxy (first bar open)
            prev_close = float(prev_row["close"])

            # â”€â”€ Pre-market gap â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            gap = (spot_open - prev_close) / prev_close

            # â”€â”€ IV estimate (30d HV Ã-- 1.15 options premium) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            hist_closes = closes_all.iloc[:idx]
            hv = _hv30(hist_closes)
            iv  = hv * 1.15   # options usually trade at premium to HV
            iv_rank = float(_calc_iv_rank(hv * 100, hist_closes))

            # â”€â”€ Naked vs Spread decision â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            force_spread = abs(gap) > GAP_SPREAD_THRESH or iv_rank > OPEN_WIN_IV_MAX
            is_naked = not force_spread
            reason   = "naked" if is_naked else (
                f"spread(gap={gap:+.1%})" if abs(gap) > GAP_SPREAD_THRESH else f"spread(ivrank={iv_rank:.0f})"
            )

            # â”€â”€ Strike selection â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            is_call = is_bull   # bear â†’ put, bull â†’ call
            strike  = round(spot_open / 0.5) * 0.5  # ATM to nearest $0.50

            # Long leg premium
            entry_premium = _bs_price(spot_open, strike, DTE, iv, call=is_call)
            if entry_premium < 0.01:
                continue

            # Spread: short leg 10% further OTM (same expiry)
            if not is_naked:
                if is_call:
                    short_strike = round(strike * 1.10 / 0.5) * 0.5
                else:
                    short_strike = round(strike * 0.90 / 0.5) * 0.5
                short_credit  = _bs_price(spot_open, short_strike, DTE, iv, call=is_call)
                net_premium   = max(0.01, entry_premium - short_credit)
            else:
                short_strike  = None
                short_credit  = 0.0
                net_premium   = entry_premium

            stop_price   = net_premium * (1 - STOP_PCT / 100)
            target_price = net_premium * (1 + TARGET_PCT / 100)

            # â”€â”€ Simulate hold â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            exit_pnl_pct = None
            exit_day     = None
            exit_reason  = None
            remaining_dte = DTE

            future_dates = [d for d in bars.index if d > trade_date and d.weekday() < 5]

            for j, fdate in enumerate(future_dates[:MAX_HOLD_DAYS]):
                remaining_dte = max(1, DTE - (j + 1))
                if fdate not in bars.index:
                    continue
                fspot = float(bars.loc[fdate, "close"])

                long_val  = _bs_price(fspot, strike, remaining_dte, iv, call=is_call)
                if not is_naked and short_strike is not None:
                    short_val = _bs_price(fspot, short_strike, remaining_dte, iv, call=is_call)
                    cur_val   = long_val - short_val
                else:
                    cur_val = long_val

                pnl_pct = (cur_val - net_premium) / net_premium * 100

                if cur_val >= target_price:
                    exit_pnl_pct = pnl_pct
                    exit_day     = fdate
                    exit_reason  = "TARGET"
                    break
                if cur_val <= stop_price:
                    exit_pnl_pct = pnl_pct
                    exit_day     = fdate
                    exit_reason  = "STOP"
                    break
                if j == MAX_HOLD_DAYS - 1:
                    exit_pnl_pct = pnl_pct
                    exit_day     = fdate
                    exit_reason  = "EXPIRE"

            if exit_pnl_pct is None:
                continue

            trades.append({
                "symbol"      : sym,
                "date"        : trade_date,
                "regime"      : "bull" if is_bull else "bear",
                "direction"   : "call" if is_call else "put",
                "spot"        : spot_open,
                "strike"      : strike,
                "short_strike": short_strike,
                "gap_pct"     : round(gap * 100, 2),
                "iv_rank"     : round(iv_rank, 1),
                "structure"   : reason,
                "net_premium" : round(net_premium, 2),
                "exit_reason" : exit_reason,
                "exit_date"   : exit_day,
                "pnl_pct"     : round(exit_pnl_pct, 1),
                "pnl_$"       : round((exit_pnl_pct / 100) * net_premium * CONTRACT_SIZE, 2),
            })

    return pd.DataFrame(trades)


# â”€â”€ CLI / display â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def main():
    parser = argparse.ArgumentParser(description="Open-window naked/spread backtest")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--start",   default=None, help="Start date YYYY-MM-DD (default: 10 trading days ago)")
    parser.add_argument("--end",     default=None, help="End date YYYY-MM-DD (default: yesterday)")
    args = parser.parse_args()

    today = datetime.date.today()
    end   = datetime.date.fromisoformat(args.end)   if args.end   else today - datetime.timedelta(days=1)
    start = datetime.date.fromisoformat(args.start) if args.start else today - datetime.timedelta(days=14)

    print(f"\nOpen-Window Backtest  |  {start} â†’ {end}  |  {len(args.symbols)} symbols\n")

    df = backtest(args.symbols, start, end)

    if df.empty:
        print("No trades generated in this window.")
        return

    # â”€â”€ Results table â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    RESET='\033[0m'; GREEN='\033[92m'; YELLOW='\033[93m'; RED='\033[91m'; BOLD='\033[1m'; CYAN='\033[96m'

    print(f"{BOLD}{'DATE':<12} {'SYM':<6} {'REG':<5} {'DIR':<5} {'GAP%':>6} {'IVRANK':>7} {'STRUCT':<22} {'PREM':>6} {'EXIT':<7} {'PNL%':>6} {'PNL$':>8}{RESET}")
    print("â”€" * 100)

    for _, r in df.iterrows():
        color = GREEN if r["pnl_pct"] >= 0 else RED
        struct_disp = r["structure"][:22]
        print(
            f"{str(r['date']):<12} {r['symbol']:<6} {r['regime']:<5} {r['direction']:<5} "
            f"{r['gap_pct']:>+6.1f}% {r['iv_rank']:>7.1f} {struct_disp:<22} "
            f"${r['net_premium']:>5.2f} {r['exit_reason']:<7} "
            f"{color}{r['pnl_pct']:>+6.1f}%{RESET} {color}${r['pnl_$']:>7.2f}{RESET}"
        )

    print("â”€" * 100)

    # â”€â”€ Summary stats â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    wins  = df[df["pnl_pct"] >= 0]
    loses = df[df["pnl_pct"] <  0]
    win_rate = len(wins) / len(df) * 100
    total_pnl = df["pnl_$"].sum()
    avg_win   = wins["pnl_$"].mean()  if not wins.empty  else 0
    avg_loss  = loses["pnl_$"].mean() if not loses.empty else 0

    print(f"\n{BOLD}SUMMARY{RESET}")
    print(f"  Trades     : {len(df)}")
    print(f"  Win rate   : {CYAN}{win_rate:.1f}%{RESET}  ({len(wins)}W / {len(loses)}L)")
    print(f"  Total P&L  : {(GREEN if total_pnl >= 0 else RED)}${total_pnl:+.2f}{RESET}")
    print(f"  Avg win    : ${avg_win:+.2f}  |  Avg loss: ${avg_loss:+.2f}")

    naked_df  = df[df["structure"] == "naked"]
    spread_df = df[df["structure"] != "naked"]
    if not naked_df.empty:
        print(f"\n  Naked  : {len(naked_df)} trades  WR={len(naked_df[naked_df['pnl_pct']>=0])/len(naked_df)*100:.0f}%  P&L=${naked_df['pnl_$'].sum():+.2f}")
    if not spread_df.empty:
        print(f"  Spread : {len(spread_df)} trades  WR={len(spread_df[spread_df['pnl_pct']>=0])/len(spread_df)*100:.0f}%  P&L=${spread_df['pnl_$'].sum():+.2f}")

    bull_df = df[df["regime"] == "bull"]
    bear_df = df[df["regime"] == "bear"]
    if not bull_df.empty:
        print(f"\n  Bull regime : {len(bull_df)} trades  WR={len(bull_df[bull_df['pnl_pct']>=0])/len(bull_df)*100:.0f}%  P&L=${bull_df['pnl_$'].sum():+.2f}")
    if not bear_df.empty:
        print(f"  Bear regime : {len(bear_df)} trades  WR={len(bear_df[bear_df['pnl_pct']>=0])/len(bear_df)*100:.0f}%  P&L=${bear_df['pnl_$'].sum():+.2f}")

    by_exit = df.groupby("exit_reason")["pnl_$"].agg(["count","sum"]).rename(columns={"count":"n","sum":"pnl"})
    print(f"\n  Exit breakdown:")
    for reason, row in by_exit.iterrows():
        print(f"    {reason:<8}: {int(row['n']):>3} trades  P&L=${row['pnl']:+.2f}")

    print()


if __name__ == "__main__":
    main()

