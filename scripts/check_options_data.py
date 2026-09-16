"""
check_options_data.py
--------------------
Cross-checks options data availability for the OPTIONS_ELIGIBLE_UNIVERSE
from both yfinance and Alpaca. Prints a report showing which tickers have:
  - yfinance options chain (expiry dates available)
  - Liquid near-term expiry (7–21 DTE) with OI >= threshold
  - Alpaca options tradability (requires Alpaca Options data API)

Usage:
    python scripts/check_options_data.py [--verbose]
"""

import sys
import os
import datetime
import argparse
from pathlib import Path

# Make engine importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import yfinance as yf

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetOptionContractsRequest
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False

from engine.config import (
    OPTIONS_ELIGIBLE_UNIVERSE,
    OPTIONS_DTE_MIN,
    OPTIONS_DTE_MAX,
    OPTIONS_MIN_OPEN_INTEREST,
    API_KEY,
    API_SECRET,
    PAPER,
)


def check_yfinance(symbol: str, verbose: bool) -> dict:
    """Check yfinance options availability for a symbol."""
    result = {
        "symbol":         symbol,
        "yf_has_options": False,
        "yf_near_term":   False,
        "yf_expiry":      None,
        "yf_call_oi":     0,
        "yf_put_oi":      0,
        "yf_iv_pct":      0.0,
        "yf_spot":        0.0,
        "error":          None,
    }
    try:
        ticker = yf.Ticker(symbol)
        expirations = ticker.options
        if not expirations:
            return result

        result["yf_has_options"] = True
        today = datetime.date.today()

        for exp_str in expirations:
            exp = datetime.date.fromisoformat(exp_str)
            dte = (exp - today).days
            if OPTIONS_DTE_MIN <= dte <= OPTIONS_DTE_MAX:
                result["yf_near_term"] = True
                result["yf_expiry"]    = exp_str

                chain = ticker.option_chain(exp_str)
                calls = chain.calls
                puts  = chain.puts

                # Spot price
                hist = ticker.history(period="1d")
                spot = float(hist["Close"].iloc[-1]) if not hist.empty else 0.0
                result["yf_spot"] = spot

                # OI: sum for strikes ±10% of spot
                if spot > 0 and not calls.empty and "strike" in calls.columns:
                    near = calls[
                        (calls["strike"] >= spot * 0.90) &
                        (calls["strike"] <= spot * 1.10)
                    ]
                    if "openInterest" in near.columns:
                        result["yf_call_oi"] = int(near["openInterest"].sum())
                    elif "openinterest" in near.columns:
                        result["yf_call_oi"] = int(near["openinterest"].sum())

                if spot > 0 and not puts.empty and "strike" in puts.columns:
                    near = puts[
                        (puts["strike"] >= spot * 0.90) &
                        (puts["strike"] <= spot * 1.10)
                    ]
                    if "openInterest" in near.columns:
                        result["yf_put_oi"] = int(near["openInterest"].sum())
                    elif "openinterest" in near.columns:
                        result["yf_put_oi"] = int(near["openinterest"].sum())

                # IV
                if not calls.empty:
                    iv_col = "impliedVolatility" if "impliedVolatility" in calls.columns else "impliedvolatility"
                    if iv_col in calls.columns:
                        mid_calls = calls[
                            (calls["strike"] >= spot * 0.97) &
                            (calls["strike"] <= spot * 1.03)
                        ]
                        if not mid_calls.empty:
                            result["yf_iv_pct"] = round(float(mid_calls[iv_col].mean() * 100), 1)
                break

    except Exception as e:
        result["error"] = str(e)

    return result


def check_alpaca_options(symbols: list, verbose: bool) -> dict:
    """Check which symbols have optionable contracts on Alpaca."""
    alpaca_result = {s: False for s in symbols}

    if not ALPACA_AVAILABLE:
        print("  [SKIP] alpaca-py not installed — skipping Alpaca options check")
        return alpaca_result

    if not API_KEY or not API_SECRET:
        print("  [SKIP] Alpaca credentials not configured")
        return alpaca_result

    try:
        client = TradingClient(API_KEY, API_SECRET, paper=PAPER)
        today = datetime.date.today()
        exp_min = today + datetime.timedelta(days=OPTIONS_DTE_MIN)
        exp_max = today + datetime.timedelta(days=OPTIONS_DTE_MAX)

        for symbol in symbols:
            try:
                req = GetOptionContractsRequest(
                    underlying_symbols=[symbol],
                    expiration_date_gte=exp_min,
                    expiration_date_lte=exp_max,
                    limit=5,
                )
                contracts = client.get_option_contracts(req)
                alpaca_result[symbol] = len(contracts) > 0
                if verbose:
                    print(f"  Alpaca {symbol}: {len(contracts)} contract(s) in {OPTIONS_DTE_MIN}–{OPTIONS_DTE_MAX} DTE window")
            except Exception as e:
                if verbose:
                    print(f"  Alpaca {symbol}: error — {e}")
    except Exception as e:
        print(f"  [ERROR] Alpaca client init failed: {e}")

    return alpaca_result


def main():
    parser = argparse.ArgumentParser(description="Check options data availability")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed output")
    args = parser.parse_args()

    symbols = OPTIONS_ELIGIBLE_UNIVERSE
    today   = datetime.date.today()
    print(f"\nOptions Data Availability Check — {today}")
    print(f"Universe: {len(symbols)} tickers | DTE window: {OPTIONS_DTE_MIN}–{OPTIONS_DTE_MAX} | Min OI: {OPTIONS_MIN_OPEN_INTEREST}")
    print("=" * 80)

    # --- yfinance ---
    print("\n[1] yfinance options chains:\n")
    yf_results = []
    for sym in symbols:
        r = check_yfinance(sym, args.verbose)
        yf_results.append(r)

    # Print table
    header = f"{'Symbol':<8} {'Spot':>8} {'yf':>4} {'NearTerm':>9} {'Expiry':>12} {'CallOI':>8} {'PutOI':>7} {'IV%':>6}"
    print(header)
    print("-" * len(header))
    for r in yf_results:
        yf_ok  = "YES" if r["yf_has_options"] else "NO"
        nt_ok  = "YES" if r["yf_near_term"] else "NO"
        spot   = f"${r['yf_spot']:.2f}" if r["yf_spot"] > 0 else "—"
        expiry = r["yf_expiry"] or "—"
        c_oi   = str(r["yf_call_oi"]) if r["yf_call_oi"] else "—"
        p_oi   = str(r["yf_put_oi"])  if r["yf_put_oi"]  else "—"
        iv     = f"{r['yf_iv_pct']:.0f}%" if r["yf_iv_pct"] > 0 else "—"
        err    = f" ERR: {r['error'][:30]}" if r["error"] else ""
        print(f"{r['symbol']:<8} {spot:>8} {yf_ok:>4} {nt_ok:>9} {expiry:>12} {c_oi:>8} {p_oi:>7} {iv:>6}{err}")

    liquid = [r["symbol"] for r in yf_results if r["yf_near_term"] and r["yf_call_oi"] >= OPTIONS_MIN_OPEN_INTEREST]
    print(f"\n  Liquid (near-term + OI >= {OPTIONS_MIN_OPEN_INTEREST}): {len(liquid)}/{len(symbols)} — {liquid}")

    # --- Alpaca ---
    print("\n[2] Alpaca options contracts:\n")
    alpaca_map = check_alpaca_options(symbols, args.verbose)

    tradable = [s for s, ok in alpaca_map.items() if ok]
    not_tradable = [s for s, ok in alpaca_map.items() if not ok]
    print(f"  Tradable on Alpaca  : {tradable}")
    print(f"  Not found on Alpaca : {not_tradable}")

    # --- Summary ---
    both_ok = [s for s in liquid if alpaca_map.get(s, False)]
    print("\n" + "=" * 80)
    print(f"SUMMARY: {len(both_ok)} tickers fully ready (liquid yfinance chain + Alpaca tradable):")
    print(f"  {both_ok}")
    print()

    if not both_ok:
        print("  No fully verified tickers. Check credentials or DTE window in config.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
