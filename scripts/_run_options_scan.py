"""
Standalone options flow — scan full universe, print all signals, NO orders placed.
Usage:  apextrader\\Scripts\\python.exe scripts\\_run_options_scan.py
"""
import sys, os, warnings
from pathlib import Path
warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import logging
logging.basicConfig(
    level=logging.WARNING,          # suppress DEBUG/INFO noise
    format="%(levelname)s | %(name)s | %(message)s",
)
# Let options-strategies log at INFO so we can see skip reasons
logging.getLogger("engine.options.strategies").setLevel(logging.WARNING)

from engine.options.strategies import scan_options_universe
from engine.utils import MarketState

print("=" * 70)
print("OPTIONS FLOW — full universe scan (no orders placed)")
print("=" * 70)

signals = scan_options_universe(
    held_positions={},
    existing_option_symbols=set(),
    market_state=MarketState.from_now(),
)

if not signals:
    print("\n  No signals this cycle.\n")
    sys.exit(0)

print(f"\n  {len(signals)} signal(s) found, ranked by composite score\n")
print(f"{'#':>2}  {'SYM':<6}  {'TYPE':<5}  {'ACT':<12}  {'STRIKE':>7}  {'EXPIRY':<12}  "
      f"{'MID':>6}  {'CONF':>6}  {'RR':>5}  {'IVRNK':>6}  REASON")
print("-" * 130)

for i, s in enumerate(signals, 1):
    spread_tag = ""
    if hasattr(s, "spread_sell_strike") and s.spread_sell_strike:
        spread_tag = f"/{s.spread_sell_strike:.0f}"
    strike_str = f"{s.strike:.2f}{spread_tag}"
    conf_str   = f"{s.confidence:.0%}"
    rr_str     = f"{s.rr_ratio:.1f}x" if s.rr_ratio else "  —"
    iv_str     = f"{s.iv_rank:.0f}" if s.iv_rank else " —"
    reason     = s.reason[:80] if s.reason else ""
    print(
        f"{i:>2}  {s.symbol:<6}  {s.option_type:<5}  {s.action:<12}  "
        f"{strike_str:>10}  {str(s.expiry):<12}  "
        f"{s.mid_price:>6.2f}  {conf_str:>6}  {rr_str:>5}  {iv_str:>6}  {reason}"
    )

print("-" * 130)
print(f"\nTOP PICK: {signals[0].symbol} | {signals[0].reason}\n")
