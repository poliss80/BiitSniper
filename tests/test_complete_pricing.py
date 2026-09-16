#!/usr/bin/env python3
"""
Test complete spread pricing with mark, ask, bid prices and DTE analysis.
Proposes exit recommendations based on mark P&L and time decay.
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
log = logging.getLogger("TestPricing")

# Load env vars
load_dotenv()
print("✅ Loaded .env file from", os.path.abspath(".env"))

# Test positions
positions = [
    {
        "name": "SMH 550/555 Bull Call (10 contracts)",
        "underlying": "SMH",
        "entry_price": 1.90,
        "legs": [
            {"occ_symbol": "SMH550C", "side": "buy", "ratio_qty": 1, "strike": 550.0},
            {"occ_symbol": "SMH555C", "side": "sell", "ratio_qty": 1, "strike": 555.0},
        ],
        "max_profit": 5.0 - 1.90,  # 5.00 width - entry
        "quantity": 10,
    },
    {
        "name": "SMH 560/565 Bull Call (2 contracts)",
        "underlying": "SMH",
        "entry_price": 2.00,
        "legs": [
            {"occ_symbol": "SMH560C", "side": "buy", "ratio_qty": 1, "strike": 560.0},
            {"occ_symbol": "SMH565C", "side": "sell", "ratio_qty": 1, "strike": 565.0},
        ],
        "max_profit": 5.0 - 2.00,
        "quantity": 2,
    },
    {
        "name": "TQQQ 71/73 Bull Call",
        "underlying": "TQQQ",
        "entry_price": 1.50,
        "legs": [
            {"occ_symbol": "TQQQ71C", "side": "buy", "ratio_qty": 1, "strike": 71.0},
            {"occ_symbol": "TQQQ73C", "side": "sell", "ratio_qty": 1, "strike": 73.0},
        ],
        "max_profit": 2.0 - 1.50,
        "quantity": 1,
    },
    {
        "name": "IWM 290/295 Bull Call",
        "underlying": "IWM",
        "entry_price": 2.25,
        "legs": [
            {"occ_symbol": "IWM290C", "side": "buy", "ratio_qty": 1, "strike": 290.0},
            {"occ_symbol": "IWM295C", "side": "sell", "ratio_qty": 1, "strike": 295.0},
        ],
        "max_profit": 5.0 - 2.25,
        "quantity": 1,
    },
]


def get_recommendation(pricing_data, max_profit, dte):
    """Generate recommendation based on mark P&L and DTE."""
    if not pricing_data:
        return "❌ UNABLE_TO_PRICE", "gray"
    
    mark_pnl = pricing_data["pnl_mark_pct"]
    mark_price = pricing_data["spread_mark"]
    entry_price = pricing_data["entry_price"]
    
    # Profit target: 50% of max profit
    profit_target_price = entry_price + (max_profit * 0.5)
    
    # Status indicators
    if mark_pnl >= 50:
        return f"🟢 CLOSE_PROFIT (mark={mark_pnl:+.1f}%)", "green"
    elif mark_pnl >= 0:
        return f"🟢 HOLD_PROFIT (mark={mark_pnl:+.1f}%)", "green"
    elif dte and dte <= 7 and mark_pnl < -50:
        return f"🔴 CLOSE_DTE_DECAY (dte={dte}d, mark={mark_pnl:+.1f}%)", "red"
    elif dte and dte <= 7:
        return f"🟡 MONITOR_DTE (dte={dte}d, mark={mark_pnl:+.1f}%)", "yellow"
    elif mark_pnl < -70:
        return f"🔴 CLOSE_CRITICAL_LOSS (mark={mark_pnl:+.1f}%)", "red"
    elif mark_pnl < -50:
        return f"🟠 CLOSE_HIGH_LOSS (mark={mark_pnl:+.1f}%)", "orange"
    else:
        return f"🟡 HOLD_RECOVERY (mark={mark_pnl:+.1f}%)", "yellow"


print("\n" + "="*100)
print("COMPLETE SPREAD PRICING ANALYSIS WITH DTE-BASED RECOMMENDATIONS")
print("="*100 + "\n")

results = []

for pos in positions:
    print(f"\n{pos['name']}")
    print(f"  Underlying: {pos['underlying']} | Entry: ${pos['entry_price']:.2f}")
    
    pricing = get_spread_complete_pricing(
        pos["underlying"],
        pos["legs"],
        pos["entry_price"],
    )
    
    if pricing:
        mark = pricing["spread_mark"]
        bid = pricing["spread_bid"]
        ask = pricing["spread_ask"]
        dte = pricing["dte"]
        exp = pricing["expiration_date"]
        
        mark_pnl = pricing["pnl_mark_pct"]
        bid_pnl = pricing["pnl_bid_pct"]
        ask_pnl = pricing["pnl_ask_pct"]
        
        recommendation, color = get_recommendation(pricing, pos["max_profit"], dte)
        
        print(f"\n  COMPLETE PRICING:")
        print(f"    Bid (exit):  ${bid:6.2f}  | P&L: {bid_pnl:+7.1f}%")
        print(f"    Mark (mid):  ${mark:6.2f}  | P&L: {mark_pnl:+7.1f}% ← MAIN METRIC")
        print(f"    Ask (enter): ${ask:6.2f}  | P&L: {ask_pnl:+7.1f}%")
        
        print(f"\n  EXPIRATION: {exp} ({dte} DTE)" if dte is not None else f"\n  EXPIRATION: {exp} (DTE unknown)")
        print(f"  Max Profit: ${pos['max_profit']:.2f} | Qty: {pos['quantity']}")
        
        print(f"\n  RECOMMENDATION: {recommendation}")
        
        # Profit target info
        profit_target = pos["entry_price"] + (pos["max_profit"] * 0.5)
        print(f"  Target Price (50% max): ${profit_target:.2f} (currently ${mark:.2f})")
        
        results.append({
            "name": pos["name"],
            "mark": mark,
            "mark_pnl": mark_pnl,
            "bid": bid,
            "dte": dte,
            "recommendation": recommendation,
        })
    else:
        print("  ❌ ERROR: Could not retrieve pricing data")

print("\n" + "="*100)
print("SUMMARY TABLE")
print("="*100)
print(f"{'Position':<40} {'Mark':>8} {'Mark P&L':>10} {'DTE':>6} {'Status':>35}")
print("-"*100)

for r in results:
    dte_str = f"{r['dte']}d" if r['dte'] is not None else "N/A"
    status = r["recommendation"].split(" (")[0]  # Get emoji + action
    print(
        f"{r['name']:<40} ${r['mark']:>7.2f} {r['mark_pnl']:>9.1f}% {dte_str:>6} {status:>35}"
    )

print("\n" + "="*100)
print("RECOMMENDATION GUIDE:")
print("="*100)
print("""
🟢 GREEN ZONES:
  - Mark P&L > 0%: Position is profitable; hold or close at profit target
  - Mark P&L > 50%: Close at profit target (50% of max gain)

🟡 YELLOW ZONES:
  - DTE ≤ 7 days: Time decay accelerating; monitor closely
  - Mark P&L -50% to 0%: Recoverable; hold unless hitting DTE
  
🔴 RED ZONES:
  - Mark P&L < -50%: High loss; close unless strong recovery expected
  - Mark P&L < -70%: Critical loss; close immediately
  - DTE ≤ 7 AND Mark P&L < -50%: Close due to time decay risk

🔵 LOGIC:
  - Use MARK price for position monitoring (true mid/theoretical value)
  - Use BID price if closing (realistic exit proceeds)
  - Use DTE to weight urgency (shorter expiration = more theta decay)
""")
print("="*100 + "\n")
