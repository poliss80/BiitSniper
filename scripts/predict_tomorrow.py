#!/usr/bin/env python
"""
ApexTrader — Tomorrow Prediction Scanner
=========================================
Scores the full universe using TODAY's data to surface likely candidates
for the 4 early-momentum strategies:
  • PreMarketMomentumStrategy  (7:00–10:00 AM ET)
  • OpeningBellSurgeStrategy   (9:30–9:45 AM ET)
  • PMHighBreakoutStrategy     (9:31–10:30 AM ET)
  • EarlySqueezeDetector       (9:30–10:15 AM ET)

Scoring weights:
  gap_pct      35%  — today's gap vs yesterday close
  vol_ratio    25%  — today's volume vs 4-day average
  trend        20%  — fraction of last 4 days that closed higher
  float_bonus  20%  — low-float (<20M) = full bonus, <50M = half

Flags:
  --save          Write top picks to predictions/watchlist.json and inject
                  them into PRIORITY_FOLLOWING in engine/config.py so the
                  bot scans them with high priority at the next open.
  --top N         How many tickers to save (default 20).
  --protect       Place GTC trailing stops on all open live equity positions
                  that have no active sell/buy-to-cover order.  Uses the same
                  ATR-tiered trail% as the bot's protect_positions() call.
                  Safe to run after market close on any live session.

Run:
  python scripts/predict_tomorrow.py
  python scripts/predict_tomorrow.py --save
  python scripts/predict_tomorrow.py --save --top 15
  python scripts/predict_tomorrow.py --protect
  python scripts/predict_tomorrow.py --save --protect
"""

import sys
import os
import json
import argparse
import warnings
import logging
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import psutil
from engine.config import MEMORY_WARN_MB

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

warnings.filterwarnings("ignore")
logging.getLogger("peewee").setLevel(logging.CRITICAL)

from engine.config import (
    PRIORITY_1_MOMENTUM,
    PRIORITY_2_ESTABLISHED,
    PRIORITY_FOLLOWING,
    EARLY_SQUEEZE,
    PRE_MARKET_MOMENTUM,
)

# ──────────────────────────────────────────────────────────────────────────────
# Universe — exclude ETF / index proxies
# ──────────────────────────────────────────────────────────────────────────────
_SKIP = {"SPY", "QQQ", "IWM", "^VIX", "DJI", "JNUG", "NUGT", "DUST",
         "SOXS", "LABD", "UCO", "ZSL", "GLL", "GDXU", "GDXD",
         "YANG", "YINN", "KORU", "CONL", "MSTX", "SMCX", "SMCZ",
         "MSOX", "ETHT", "ETHD", "ETHA", "UXRP", "XXRP"}

UNIVERSE = [
    s for s in (
        list(PRIORITY_1_MOMENTUM)
        + list(PRIORITY_2_ESTABLISHED)
        + list(PRIORITY_FOLLOWING)
    )
    if s not in _SKIP
]

# ──────────────────────────────────────────────────────────────────────────────
# Scoring weights
# ──────────────────────────────────────────────────────────────────────────────
W_GAP   = 0.35
W_VOL   = 0.25
W_TREND = 0.20
W_FLOAT = 0.20

MIN_PRICE = 0.50   # skip sub-penny / OTC micro-caps

# min gap and RVOL thresholds to tag a ticker as a strategy candidate
SQUEEZE_GAP_MIN  = PRE_MARKET_MOMENTUM.get("min_gap_pct", 3.0)
SQUEEZE_FLOAT    = EARLY_SQUEEZE.get("max_float_shares", 20_000_000)
GAP_RUN_GAP_MIN  = 3.0
GAP_RUN_VOL_MIN  = 2.0


def score_ticker(symbol: str) -> dict | None:
    try:
        from engine.utils import get_bars
        hist = get_bars(symbol, period="7d", interval="1d")
        if hist.empty or len(hist) < 3:
            return None

        closes  = hist["close"].values
        volumes = hist["volume"].values

        yesterday_close = float(closes[-1])
        prior_close     = float(closes[-2])

        if yesterday_close < MIN_PRICE or prior_close <= 0:
            return None

        avg_vol = float(volumes[:-1].mean()) if len(volumes) > 1 else 0.0
        if avg_vol == 0:
            return None

        gap_pct   = ((yesterday_close - prior_close) / prior_close) * 100
        vol_ratio = float(volumes[-1]) / avg_vol

        # trend: fraction of last ≤4 sessions that closed higher
        n_trend = min(4, len(closes) - 1)
        up_days = sum(closes[i] > closes[i - 1] for i in range(len(closes) - n_trend, len(closes)))
        trend_score = up_days / n_trend if n_trend > 0 else 0.0

        # float — shares float data not available from Alpaca; skip scoring
        shares_float = None
        float_label  = "—"
        float_score  = 0.0

        # normalise gap and vol for [0,1] scoring
        gap_norm = max(min(gap_pct / 10.0, 1.0), 0.0)
        vol_norm = max(min((vol_ratio - 1.0) / 5.0, 1.0), 0.0)

        composite = (
            W_GAP   * gap_norm
            + W_VOL   * vol_norm
            + W_TREND * trend_score
            + W_FLOAT * float_score
        )

        return {
            "symbol":            symbol,
            "price":             round(yesterday_close, 2),
            "gap_pct":           round(gap_pct, 2),
            "vol_ratio":         round(vol_ratio, 2),
            "trend":             round(trend_score, 2),
            "float":             float_label,
            "score":             round(composite, 4),
            "shares_float":      shares_float,
            "squeeze_candidate": (
                shares_float is not None
                and shares_float <= SQUEEZE_FLOAT
                and gap_pct >= SQUEEZE_GAP_MIN
            ),
            "gap_candidate": gap_pct >= GAP_RUN_GAP_MIN and vol_ratio >= GAP_RUN_VOL_MIN,
            "high_vol":      vol_ratio >= 5.0,
        }

    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="ApexTrader prediction scanner")
    parser.add_argument("--save", action="store_true",
                        help="Save top picks to predictions/watchlist.json and inject into config.py")
    parser.add_argument("--top", type=int, default=20,
                        help="Number of tickers to save (default: 20)")
    parser.add_argument("--protect", action="store_true",
                        help="Place GTC trailing stops on all uncovered live equity positions")
    args = parser.parse_args()

    # --protect-only: skip the scan, just place stops and exit
    if args.protect and not args.save:
        _place_trailing_stops()
        return

    print(f"\nApexTrader — Tomorrow Prediction Scanner")
    print(f"Universe: {len(UNIVERSE)} tickers\n")

    results = []
    done_count = 0
    total = len(UNIVERSE)

    with ThreadPoolExecutor(max_workers=24) as pool:
        futures = {pool.submit(score_ticker, sym): sym for sym in UNIVERSE}
        for fut in as_completed(futures):
            done_count += 1
            if done_count % 50 == 0 or done_count == total:
                print(f"  Fetching data... {done_count}/{total}", end="\r", flush=True)
            result = fut.result()
            if result:
                results.append(result)

    print(f"\n  Done — {len(results)} tickers scored.\n")

    if not results:
        print("No data returned — check Alpaca credentials / market hours.")
        return

    df = pd.DataFrame(results).sort_values("score", ascending=False).reset_index(drop=True)

    # ── TOP 25 OVERALL ────────────────────────────────────────────────────────
    W = 84
    print("═" * W)
    print(f"  TOP 25 CANDIDATES FOR TOMORROW  (scored on today's session data)")
    print("═" * W)
    hdr = f"{'#':<4}{'Sym':<7}{'Price':>7}  {'Gap%':>7}  {'RVOL':>6}  {'Trend':>6}  {'Float':>9}  {'Score':>7}  Tags"
    print(hdr)
    print("─" * W)

    for rank, row in df.head(25).iterrows():
        tags = []
        if row["squeeze_candidate"]: tags.append("SQUEEZE")
        if row["gap_candidate"]:     tags.append("GAP-RUN")
        if row["high_vol"]:          tags.append("HIGH-VOL")
        tag_str = " ".join(tags) if tags else "—"
        print(
            f"{rank + 1:<4}{row['symbol']:<7}${row['price']:>6.2f}  "
            f"{row['gap_pct']:>+7.1f}%  {row['vol_ratio']:>6.1f}x  "
            f"{row['trend']:>6.2f}  {row['float']:>9}  {row['score']:>7.4f}  {tag_str}"
        )

    # ── LOW-FLOAT SQUEEZE ─────────────────────────────────────────────────────
    squeeze = df[df["squeeze_candidate"]].head(15)
    if not squeeze.empty:
        print()
        print("═" * W)
        print(f"  LOW-FLOAT SQUEEZE CANDIDATES  (EarlySqueezeDetector + PreMarketMomentum)")
        print("═" * W)
        print(f"{'Sym':<7}{'Price':>7}  {'Gap%':>7}  {'RVOL':>6}  {'Float':>9}  {'Score':>7}")
        print("─" * W)
        for _, row in squeeze.iterrows():
            print(
                f"{row['symbol']:<7}${row['price']:>6.2f}  "
                f"{row['gap_pct']:>+7.1f}%  {row['vol_ratio']:>6.1f}x  "
                f"{row['float']:>9}  {row['score']:>7.4f}"
            )

    # ── GAP BREAKOUT ──────────────────────────────────────────────────────────
    gap_run = df[df["gap_candidate"] & ~df["squeeze_candidate"]].head(10)
    if not gap_run.empty:
        print()
        print("═" * W)
        print(f"  GAP-RUN CANDIDATES  (PreMarketMomentum + OpeningBellSurge + PMHighBreakout)")
        print("═" * W)
        print(f"{'Sym':<7}{'Price':>7}  {'Gap%':>7}  {'RVOL':>6}  {'Float':>9}  {'Score':>7}")
        print("─" * W)
        for _, row in gap_run.iterrows():
            print(
                f"{row['symbol']:<7}${row['price']:>6.2f}  "
                f"{row['gap_pct']:>+7.1f}%  {row['vol_ratio']:>6.1f}x  "
                f"{row['float']:>9}  {row['score']:>7.4f}"
            )

    print()
    print("═" * W)
    print("  LEGEND")
    print("  SQUEEZE  = low-float (≤20M) + gap ≥3%  → EarlySqueezeDetector primary target")
    print("  GAP-RUN  = gap ≥3% + vol ≥2x avg       → PreMarketMomentum / OpeningBellSurge")
    print("  HIGH-VOL = vol ≥5x avg (unusual activity)")
    print(f"  Score = weighted composite (gap {W_GAP*100:.0f}% | "
          f"vol {W_VOL*100:.0f}% | trend {W_TREND*100:.0f}% | float {W_FLOAT*100:.0f}%)")
    print("═" * W)
    print()

    # ── SAVE + INJECT ─────────────────────────────────────────────────────────
    if args.save:
        _save_and_inject(df, top_n=args.top)

    # ── PROTECT POSITIONS ─────────────────────────────────────────────────────
    if args.protect:
        _place_trailing_stops()


def _save_and_inject(df: pd.DataFrame, top_n: int = 20) -> None:
    """
    1. Write top_n tickers to predictions/watchlist.json (for reference).
    2. Add them to data/universe.json as tier-3 (following) via engine.universe.
       They will be auto-loaded into PRIORITY_FOLLOWING at next bot startup.
       Tier-3 TTL = 7 days — they expire automatically if not re-scored.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # ── 1. Write JSON record ──────────────────────────────────────────────────
    pred_dir = os.path.join(root, "predictions")
    os.makedirs(pred_dir, exist_ok=True)
    watchlist_path = os.path.join(pred_dir, "watchlist.json")

    top_rows = df.head(top_n)
    payload = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source_date":  str(datetime.date.today()),
        "tickers": [
            {
                "symbol":    row["symbol"],
                "price":     row["price"],
                "gap_pct":   row["gap_pct"],
                "vol_ratio": row["vol_ratio"],
                "score":     row["score"],
                "tags": (
                    (["SQUEEZE"] if row["squeeze_candidate"] else [])
                    + (["GAP-RUN"] if row["gap_candidate"] else [])
                    + (["HIGH-VOL"] if row["high_vol"] else [])
                ),
            }
            for _, row in top_rows.iterrows()
        ],
    }
    with open(watchlist_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"  Saved {top_n} predictions → {watchlist_path}")

    # ── 2. Add to universe.json as tier-3 (7-day TTL) ────────────────────────
    sys.path.insert(0, root)
    from engine.equity.universe import add_tickers, stats  # noqa: E402

    symbols = [t["symbol"] for t in payload["tickers"]]
    fresh   = add_tickers(symbols, tier=3)
    s       = stats()
    print(f"  Added {fresh} new / refreshed {len(symbols) - fresh} tickers in universe.json (tier-3, 7-day TTL)")
    print(f"  Universe totals: tier1={s['by_tier'].get(1,0)}  tier2={s['by_tier'].get(2,0)}  tier3={s['by_tier'].get(3,0)}  alive={s['total_alive']}")
    print(f"  Symbols: {', '.join(symbols)}")
    print()
    print("  These tickers load automatically into PRIORITY_FOLLOWING at next bot startup.")


def _place_trailing_stops() -> None:
    """
    Connect to Alpaca (live or paper, per TRADE_MODE env var) and place a GTC
    trailing stop on every open equity position that has no active sell/
    buy-to-cover order outstanding.  Trail % is determined by get_dynamic_tier()
    — the same ATR-tiered logic used by the bot's protect_positions() cycle.

    Safe to run after market close.  Positions already protected (e.g. stops
    placed live by an earlier bot cycle) are skipped automatically.
    """
    from engine.broker.broker_factory import BrokerFactory
    from engine.utils import get_dynamic_tier
    from alpaca.trading.requests import TrailingStopOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.enums import OrderType as AlpacaOrderType

    W = 72
    print()
    print("═" * W)
    print("  PROTECT POSITIONS — GTC Trailing Stops")
    print("═" * W)

    try:
        client = BrokerFactory.create_stock_client()
    except Exception as e:
        print(f"  [ERROR] Could not connect to broker: {e}")
        return

    try:
        positions   = client.get_all_positions()
        open_orders = client.get_orders()
    except Exception as e:
        print(f"  [ERROR] Could not fetch positions/orders: {e}")
        return

    covered = {o.symbol for o in open_orders}

    if not positions:
        print("  No open positions.")
        print("═" * W)
        return

    print(f"  {'Symbol':<8}  {'Side':<5}  {'Qty':>5}  {'Price':>8}  {'Trail%':>7}  {'Tier':<8}  Status")
    print("  " + "─" * (W - 2))

    placed = 0
    skipped = 0
    errors = 0

    for pos in positions:
        sym = pos.symbol
        try:
            qty           = int(float(pos.qty))
            qty_available = int(float(getattr(pos, "qty_available", qty)))
            current       = float(pos.current_price)
            is_long       = qty > 0
            side_label    = "LONG" if is_long else "SHORT"
            stop_side     = OrderSide.SELL if is_long else OrderSide.BUY
        except (TypeError, ValueError) as e:
            print(f"  {sym:<8}  —      parse error: {e}")
            errors += 1
            continue

        if sym in covered:
            print(f"  {sym:<8}  {side_label:<5}  {abs(qty):>5}  ${current:>7.2f}  {'—':>7}  {'—':<8}  already covered")
            skipped += 1
            continue

        if qty_available <= 0:
            print(f"  {sym:<8}  {side_label:<5}  {abs(qty):>5}  ${current:>7.2f}  {'—':>7}  {'—':<8}  qty_available=0 (bracket-locked)")
            skipped += 1
            continue

        tier_info  = get_dynamic_tier(sym, current)
        trail_pct  = tier_info["ts"]
        tier_label = tier_info["tier"]

        try:
            client.submit_order(TrailingStopOrderRequest(
                symbol        = sym,
                qty           = abs(qty_available),
                side          = stop_side,
                type          = AlpacaOrderType.TRAILING_STOP,
                time_in_force = TimeInForce.GTC,
                trail_percent = trail_pct,
            ))
            print(f"  {sym:<8}  {side_label:<5}  {abs(qty_available):>5}  ${current:>7.2f}  {trail_pct:>6.1f}%  {tier_label:<8}  ✓ placed")
            placed += 1
        except Exception as e:
            err = str(e)
            if "40310100" in err:
                print(f"  {sym:<8}  {side_label:<5}  {abs(qty_available):>5}  ${current:>7.2f}  {trail_pct:>6.1f}%  {tier_label:<8}  PDT-blocked (same-day entry)")
            else:
                print(f"  {sym:<8}  {side_label:<5}  {abs(qty_available):>5}  ${current:>7.2f}  {trail_pct:>6.1f}%  {tier_label:<8}  ERROR: {e}")
            errors += 1

    print("  " + "─" * (W - 2))
    print(f"  Placed: {placed}  |  Already covered: {skipped}  |  Errors/blocked: {errors}")
    print("═" * W)
    print()


if __name__ == "__main__":
    main()
