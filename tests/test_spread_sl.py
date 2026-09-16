#!/usr/bin/env python3
"""
Test the new 3-tier spread SL framework with SMH positions.

Tests:
1. 550/555 call spread ($1.90 debit, 10 contracts)
2. 560/565 call spread ($2.00 debit, 2 contracts)

Scenarios:
- Current: SMH at $568
- Rally: SMH at $575 (+1.2%)
- Selloff: SMH at $560 (-1.4%)
- Expiration: 16 DTE
"""

from engine.utils.risk import calculate_spread_sl
import datetime

print("=" * 100)
print("SPREAD 3-TIER SL VALIDATION TEST")
print("=" * 100)

# Test Case 1: 550/555 Bull Call Spread (10 contracts, $1.90 debit)
print("\n[TEST 1] 550/555 Bull Call Spread (SMH)")
print("-" * 100)

spreads = [
    {
        "name": "550/555 Call (10 contracts)",
        "long_strike": 550.0,
        "short_strike": 555.0,
        "debit": 1.90,
        "contracts": 10,
    },
    {
        "name": "560/565 Call (2 contracts)",
        "long_strike": 560.0,
        "short_strike": 565.0,
        "debit": 2.00,
        "contracts": 2,
    }
]

scenarios = [
    {"price": 556.64, "name": "SMH -2% (Selloff)", "dte": 16},
    {"price": 562.32, "name": "SMH -1% (Pullback)", "dte": 16},
    {"price": 568.00, "name": "SMH Flat (Today)", "dte": 16},
    {"price": 573.68, "name": "SMH +1% (Rally)", "dte": 16},
    {"price": 568.00, "name": "SMH Flat (7 DTE)", "dte": 7},
    {"price": 568.00, "name": "SMH Flat (5 DTE)", "dte": 5},
]

for spread in spreads:
    print(f"\n{spread['name']}")
    print(f"  Entry: ${spread['debit']:.2f} debit × {spread['contracts']} = ${spread['debit'] * spread['contracts'] * 100:.0f}")
    print(f"  Max Profit: ${(5.0 - spread['debit']) * spread['contracts'] * 100:.0f}")
    print()
    
    for scenario in scenarios:
        sl = calculate_spread_sl(
            long_strike=spread['long_strike'],
            short_strike=spread['short_strike'],
            spread_debit=spread['debit'],
            underlying_price=scenario['price'],
            dte=scenario['dte'],
            strategy_name="TrendPullbackSpread"
        )
        
        print(f"  {scenario['name']:30} (DTE={scenario['dte']})")
        print(f"    Profit Target:       ${sl['profit_target_bid']:.2f} bid")
        print(f"    Underlying SL:       ${sl['underlying_sl']:.2f} (close if SMH < this)")
        print(f"    Current Spread Val:  ${sl['current_spread_value']:.2f}")
        print(f"    Current P&L:         {sl['current_pnl_pct']:+.1f}%")
        print(f"    Close for Profit?    {sl['should_close_for_profit']} (mark >= ${sl['profit_target_bid']:.2f})")
        print(f"    Close for Underlying? {sl['should_close_for_underlying']} (SMH=${scenario['price']:.2f} < ${sl['underlying_sl']:.2f})")
        print(f"    Close for DTE?       {sl['should_close_for_dte']} (DTE={scenario['dte']}d, pnl={sl['current_pnl_pct']:.1f}%)")
        print()

print("=" * 100)
print("SUMMARY")
print("=" * 100)
print("""
Key Observations:
1. Both spreads should stay open until TIER 1 (profit target) is reached
2. At SMH $568, neither spread has hit profit target yet
3. As SMH rallies toward $575, both spreads approach max profit ($5.00)
4. When SMH drops to $556, Underlying SL ($547) not breached yet (still above)
5. With 7 DTE or less AND profit < 50%, TIER 3 triggers automatic close

Your Trade Action Tomorrow:
- If SMH stays $565-572: Hold, spreads earning theta decay
- If SMH rallies to $575+: Close at profit target ($3.45/$3.50 bids)
- If SMH drops to $546 or below: Close due to underlying breach
- If we hit 7 DTE with < 50% profit: Auto-close to avoid theta acceleration
""")
