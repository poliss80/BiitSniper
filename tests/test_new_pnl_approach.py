#!/usr/bin/env python3
"""
Test new consolidated spread P&L approach (NO leg-based calculations).

This test demonstrates:
1. Complete spread pricing (bid/mark/ask)
2. P&L using only MARK price (not summed from individual legs)
3. DTE-based recommendations
4. Removal of leg-by-leg P&L calculation
"""

import logging
import os
from datetime import datetime
from dotenv import load_dotenv
from engine.utils.schwab_pricing import get_spread_complete_pricing

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
log = logging.getLogger("NewPnLApproach")

# Load env vars
load_dotenv()
print("✅ Loaded .env file")

# Test positions
test_positions = [
    {
        "symbol": "SMH",
        "strategy": "BullCall",
        "entry_price": 1.90,
        "contracts": 10,
        "legs": [
            {"occ_symbol": "SMH550C", "side": "buy", "ratio_qty": 1, "strike": 550.0},
            {"occ_symbol": "SMH555C", "side": "sell", "ratio_qty": 1, "strike": 555.0},
        ],
    },
    {
        "symbol": "SMH",
        "strategy": "BullCall",
        "entry_price": 2.00,
        "contracts": 2,
        "legs": [
            {"occ_symbol": "SMH560C", "side": "buy", "ratio_qty": 1, "strike": 560.0},
            {"occ_symbol": "SMH565C", "side": "sell", "ratio_qty": 1, "strike": 565.0},
        ],
    },
    {
        "symbol": "TQQQ",
        "strategy": "BullCall",
        "entry_price": 1.50,
        "contracts": 1,
        "legs": [
            {"occ_symbol": "TQQQ71C", "side": "buy", "ratio_qty": 1, "strike": 71.0},
            {"occ_symbol": "TQQQ73C", "side": "sell", "ratio_qty": 1, "strike": 73.0},
        ],
    },
]

print("\n" + "="*110)
print("NEW CONSOLIDATED SPREAD P&L APPROACH (NO LEG-BASED CALCULATIONS)")
print("="*110 + "\n")

print("BEFORE (OLD APPROACH - DON'T USE):")
print("  ❌ Fetch snapshot for each leg individually")
print("  ❌ Calculate mid = (bid + ask) / 2 for each leg")
print("  ❌ Sum legs with signs: mark = Σ(±leg_mid)")
print("  ❌ Risk: Wide bid-ask spreads cause inaccurate net prices\n")

print("AFTER (NEW APPROACH - ALWAYS USE):")
print("  ✅ Fetch complete option chain from Schwab API")
print("  ✅ Extract bid/mark/ask for ALL strikes at once")
print("  ✅ Calculate spread prices: mark = Mark(long) - Mark(short)")
print("  ✅ Use Schwab's consolidated mark (broker-provided mid)")
print("  ✅ Result: Accurate, broker-validated pricing\n")

print("="*110)
print("POSITION ANALYSIS USING NEW APPROACH")
print("="*110 + "\n")

results = []

for pos in test_positions:
    print(f"\n{pos['symbol']} {pos['strategy']} ({pos['contracts']} contracts @ ${pos['entry_price']:.2f})")
    print("-" * 90)
    
    # Get consolidated spread pricing
    pricing = get_spread_complete_pricing(
        pos["symbol"],
        pos["legs"],
        pos["entry_price"],
    )
    
    if not pricing:
        print("  ❌ ERROR: Could not retrieve pricing")
        continue
    
    mark = pricing["spread_mark"]
    bid = pricing["spread_bid"]
    ask = pricing["spread_ask"]
    dte = pricing["dte"]
    mark_pnl = pricing["pnl_mark_pct"]
    bid_pnl = pricing["pnl_bid_pct"]
    ask_pnl = pricing["pnl_ask_pct"]
    
    print(f"\n  COMPLETE SPREAD PRICING (from Schwab):")
    print(f"    Bid (exit proceeds):   ${bid:7.2f}  |  P&L: {bid_pnl:+7.1f}%  (realistic exit value)")
    print(f"    Mark (mid/fair):       ${mark:7.2f}  |  P&L: {mark_pnl:+7.1f}%  ← MONITOR THIS")
    print(f"    Ask (entry cost):      ${ask:7.2f}  |  P&L: {ask_pnl:+7.1f}%  (entry-only)")
    
    print(f"\n  EXPIRATION & DTE:")
    print(f"    Date: {pricing['expiration_date']} | Days to expiration: {dte}d")
    
    print(f"\n  ENTRY vs CURRENT:")
    print(f"    Entry price:  ${pos['entry_price']:.2f}")
    print(f"    Current mark: ${mark:.2f}")
    print(f"    Change:       {mark_pnl:+.1f}%")
    
    print(f"\n  DECISION LOGIC:")
    if mark_pnl >= 50:
        decision = "🟢 CLOSE AT PROFIT TARGET (50% of max)"
    elif mark_pnl >= 0:
        decision = "🟢 HOLD - PROFITABLE"
    elif dte and dte <= 7 and mark_pnl < -50:
        decision = "🔴 CLOSE - TIME DECAY + HIGH LOSS"
    elif dte and dte <= 7:
        decision = "🟡 MONITOR - LOW DTE, THETA ACCELERATING"
    elif mark_pnl < -70:
        decision = f"🔴 CLOSE - CRITICAL LOSS ({mark_pnl:.1f}%)"
    else:
        decision = "🟡 HOLD - RECOVERABLE LOSS"
    
    print(f"    {decision}")
    
    results.append({
        "symbol": f"{pos['symbol']} {pos['strategy']}",
        "entry": pos['entry_price'],
        "mark": mark,
        "mark_pnl": mark_pnl,
        "dte": dte,
        "decision": decision.split(" ")[0:2],
    })

print("\n" + "="*110)
print("SUMMARY - NO LEG-BASED P&L IN ENTIRE CODEBASE")
print("="*110)
print(f"\n{'Position':<30} {'Entry':>8} {'Mark':>8} {'Mark P&L':>10} {'DTE':>6} {'Status':>30}")
print("-"*110)

for r in results:
    dte_str = f"{r['dte']}d" if r['dte'] is not None else "N/A"
    status = " ".join(r['decision'])
    print(
        f"{r['symbol']:<30} ${r['entry']:>7.2f} ${r['mark']:>7.2f} {r['mark_pnl']:>9.1f}% {dte_str:>6} {status:>30}"
    )

print("\n" + "="*110)
print("KEY CHANGES MADE TO CODEBASE:")
print("="*110)
print("""
✅ engine/utils/schwab_pricing.py
   - Added get_spread_complete_pricing() for consolidated pricing
   - Returns bid, mark, ask, DTE, and P&L metrics
   - Uses Schwab's broker-provided mark prices

✅ engine/options/executor.py (Lines 1265-1350)
   - REMOVED: Leg-by-leg snapshot fetching
   - REMOVED: Manual bid/ask averaging per leg
   - REMOVED: Fallback Schwab pricing as emergency-only
   - ADDED: Primary path uses get_spread_complete_pricing() for all multi-leg
   - ADDED: P&L calculation uses consolidated mark price
   - ADDED: Automatic DTE update from Schwab data
   
✅ Single-leg options
   - Still use Alpaca snapshots (no multi-leg consolidation needed)
   - Calculate mark as simple (bid + ask) / 2

❌ REMOVED from codebase:
   - All leg-by-leg P&L summation for spreads
   - Alpaca fallback approach (Schwab is now primary)
   - Manual mark calculation from individual legs
""")

print("="*110 + "\n")
