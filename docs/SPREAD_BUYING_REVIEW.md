#!/usr/bin/env python3
"""
REVIEW: Current Spread/Multi-Leg Code During Buying

This document reviews the spread creation logic in executor.py and identifies
any issues with the new consolidated P&L approach.
"""

import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

print("\n" + "="*110)
print("SPREAD/MULTI-LEG BUYING CODE REVIEW")
print("="*110)

print("""

## CODE FLOW: BUYING A MULTI-LEG POSITION (executor.py lines 850-1185)

### PHASE 1: INITIALIZATION (Lines 850-943)
✅ Entry checks:
  - Account restriction guard (PDT, liquidation-only)
  - Corporate actions guard (reverse split, merger)
  - Budget calculation
  - Confidence-based contract scaling
  - Strategy type detection (butterfly, condor, vertical spread)

⚠️  KEY VARIABLE: _net_entry_price = signal.mid_price (LINE 943)
  - Initialized to long-leg premium only
  - Will be updated if auto-spread net debit is calculated

### PHASE 2: OPEN WINDOW / IV GATE LOGIC (Lines 950-1000)
✅ Market timing decision:
  - If in open window (9:35-9:45 ET):
    - Higher confidence bar (85%+)
    - Max 1 open position during open window
    - 50% contract count (premium is peak)
    - DEFAULT TO SPREAD (unless IV very low + gap small)
  - If normal session:
    - If IV rank > 35: force spread
    - Else: allow naked

✅ Auto-spread strike derivation:
  - For PUTS: short_strike = long_strike * 0.90 (10 OTM)
  - For CALLS: short_strike = long_strike * 1.10 (10 OTM)

### PHASE 3: NET DEBIT/CREDIT CALCULATION (Lines 1010-1020)
⚠️  KEY ISSUE: Only handles auto-derived spreads!
  
  if is_mleg and signal.spread_sell_mid is None and _eff_spread_sell_strike is not None:
    # Black-Scholes estimate of short-leg credit
    _short_credit = _bs_option_price(...)
    _spread_mid_price = signal.mid_price - _short_credit
    _net_entry_price = _spread_mid_price  ← UPDATED!
  
  ❌ PROBLEM: This ONLY calculates net debit if:
     1. is_mleg = TRUE
     2. signal.spread_sell_mid is None (strategy didn't provide)
     3. _eff_spread_sell_strike is not None
  
  ❌ What if strategy PROVIDED spread_sell_mid?
     - Signal has both signal.mid_price AND signal.spread_sell_mid
     - Code assumes mid_price is long-leg only... but is it accurate?
     - Net debit should be: long_premium - short_credit
     - BUT: signal.spread_sell_mid might be a stale estimate from pre-market scan

### PHASE 4: LIMIT PRICE CALCULATION (Lines 1022-1030)
✅ Price improvement logic:
  - For DEBIT (buy): bid 1% below mid = round(price * 0.99, 2)
  - For CREDIT (sell): offer 1% above mid = round(price * 1.01, 2)
  
  ⚠️  BUT: For multi-leg credit spreads, uses NEGATIVE limit_price
  - Alpaca convention: positive = debit, NEGATIVE = credit
  - limit_price = -round(_spread_mid_price * 1.01, 2)
  
  ✅ This is correct per Alpaca docs

### PHASE 5: MULTI-LEG ORDER CONSTRUCTION (Lines 1035-1080)
✅ Correct legs_list construction:
  
  For VERTICAL SPREADS:
    is_credit = "sell" in signal.action
    primary_side = SELL if credit, else BUY
    secondary_side = BUY if credit, else SELL
    
    legs_list = [
      {"symbol": long_strike_symbol, "side": primary_side, "ratio_qty": 1},
      {"symbol": short_strike_symbol, "side": secondary_side, "ratio_qty": 1}
    ]
  
  ✅ Correctly handles both debit and credit spreads
  ✅ Correct position_intent: buy_to_open or sell_to_open per leg

### PHASE 6: TRACKING / ENTRY LEGS CONSTRUCTION (Lines 1126-1145)
⚠️  CRITICAL ISSUE: entry_legs construction and entry_price

  CODE (Lines 1130-1145):
  
    if is_condor:
      entry_legs = [
        {"occ_symbol": l["symbol"], "side": l["side"].value, "ratio_qty": l["ratio_qty"], "strike": strike}
        for l, strike in zip(legs_list, [put_long, put_short, call_short, call_long])
      ]
    
    elif is_butterfly:
      entry_legs = [
        {"occ_symbol": l["symbol"], "side": l["side"].value, "ratio_qty": l["ratio_qty"], "strike": strike}
        for l, strike in zip(legs_list, [low_strike, mid_strike, high_strike])
      ]
    
    else:  # Vertical spread
      entry_legs = [
        {"occ_symbol": l["symbol"], "side": l["side"].value, "ratio_qty": l["ratio_qty"], "strike": strike}
        for l, strike in zip(legs_list, [signal.strike, _eff_spread_sell_strike])
      ]
  
  ✅ Correctly maps legs to their strike prices
  ✅ Includes side (buy/sell) and ratio_qty

### PHASE 7: POSITION TRACKING (Lines 1147-1173)
⚠️  ENTRY PRICE HANDLING - THIS IS THE CRITICAL PART:

  CODE (Lines 1147-1160):
  
    self._positions[primary_occ] = OptionsPosition(
      occ_symbol=primary_occ,
      symbol=signal.symbol,
      option_type=signal.option_type,
      action=signal.action,
      strike=signal.strike,
      expiry=signal.expiry,
      contracts=contracts,
      entry_price=(-abs(_net_entry_price) if "sell" in signal.action else _net_entry_price),
      strategy=signal.strategy,
      legs=entry_legs,
      entry_iv=_entry_iv,
      is_naked=_is_naked_entry,
      open_stop_pct=_open_stop_pct,
      entry_confidence=signal.confidence,
    )
  
  ✅ CORRECT: Applies sign based on action:
     - BUY (debit):  entry_price = +_net_entry_price (e.g., +1.90)
     - SELL (credit): entry_price = -_net_entry_price (e.g., -1.50)

---

## ISSUES IDENTIFIED

### ISSUE 1: Net Debit Only Calculated for Auto-Derived Spreads ⚠️ MAJOR

**Problem:**
  When strategy provides a spread_sell_strike AND spread_sell_mid:
  - _net_entry_price still = signal.mid_price (long leg only)
  - Does NOT subtract signal.spread_sell_mid
  - Results in OVERSTATED entry price!

**Example:**
  Signal provides:
  - signal.mid_price = $2.50 (long call 550 mark)
  - signal.spread_sell_mid = $0.70 (short call 555 mark, estimated)
  - Actual net debit = $2.50 - $0.70 = $1.80
  
  BUT code records:
  - entry_price = $2.50 (WRONG! Too high)
  
  Result:
  - P&L calculation will be WRONG
  - get_spread_complete_pricing() expects accurate entry_price
  - If entry_price is overstated, P&L % will be understated

**Fix Needed:**
  ```python
  # Around line 1010-1020, BEFORE limit_price calculation:
  if is_mleg:
    if signal.spread_sell_mid is not None:
        # Strategy provided explicit spread_sell_mid
        _spread_mid_price = signal.mid_price - signal.spread_sell_mid
        _net_entry_price = _spread_mid_price
        log.debug(
            f"[OPTIONS] Spread net debit: long=${signal.mid_price:.2f} "
            f"- short=${signal.spread_sell_mid:.2f} = net=${_spread_mid_price:.2f}"
        )
    elif _eff_spread_sell_strike is not None:
        # Auto-derive via Black-Scholes (existing code)
        ...
  ```

### ISSUE 2: Spread Bid Not Being Tracked at Entry ⚠️ MEDIUM

**Problem:**
  OptionsPosition stores only entry_price (mark)
  Does NOT store spread_bid at entry time
  
  Later, during monitoring:
  - get_spread_complete_pricing() returns spread_bid
  - But we have no entry_bid to compare against
  - If exiting, we should use (spread_bid - entry_bid) / entry_bid for P&L
  - Instead, we use (spread_bid - entry_mark) / entry_mark

**Impact:**
  - Overstates P&L if bid is lower than mark at entry
  - Example:
    - Entry: mark=$2.50, bid=$0.30
    - Exit: mark=$2.60, bid=$0.40
    - Current: P&L = (2.60 - 2.50) / 2.50 = +4% (WRONG)
    - Should be: P&L = (0.40 - 0.30) / 0.30 = +33% (realistic exit)

**Fix Needed:**
  Store entry_bid alongside entry_price in OptionsPosition:
  ```python
  # At entry time, get initial spread pricing
  if is_mleg:
    initial_pricing = get_spread_complete_pricing(signal.symbol, entry_legs, _net_entry_price)
    if initial_pricing:
      entry_bid = initial_pricing["spread_bid"]
    else:
      entry_bid = _net_entry_price  # fallback
  
  # Store in position
  entry_bid = entry_bid  # NEW FIELD
  ```

### ISSUE 3: Incomplete Leg Data in entry_legs ⚠️ MEDIUM

**Problem:**
  entry_legs only contains: occ_symbol, side, ratio_qty, strike
  MISSING: leg type (CALL vs PUT)
  
  Later, get_spread_complete_pricing() must infer:
  ```python
  opt_type = "CALL" if "C" in occ_sym else "PUT"
  ```
  
  This works for OCC symbols but is fragile

**Fix Needed:**
  Add opt_type to entry_legs:
  ```python
  entry_legs = [
    {
      "occ_symbol": l["symbol"],
      "side": l["side"].value,
      "ratio_qty": l["ratio_qty"],
      "strike": strike,
      "opt_type": "CALL" if cp_type == "call" else "PUT"  # ADD THIS
    }
    for l, strike in zip(legs_list, ...)
  ]
  ```

---

## CONSOLIDATED P&L COMPATIBILITY CHECK

### What get_spread_complete_pricing() Expects:

```python
legs: [
  {
    "occ_symbol": "SMH550C",
    "side": "buy",
    "ratio_qty": 1,
    "strike": 550.0
  },
  {
    "occ_symbol": "SMH555C",
    "side": "sell",
    "ratio_qty": 1,
    "strike": 555.0
  }
]
entry_price: 1.90  # Net debit (positive for buys, negative for sells)
```

### What executor.py Currently Stores:

✅ CORRECT:
  - occ_symbol: Correct (from Alpaca OCC format)
  - side: Correct (buy or sell)
  - ratio_qty: Correct (1 for vertical, 2 for butterfly mid-leg, etc.)
  - strike: Correct (passed from signal or derived)

❌ INCORRECT/MISSING:
  - entry_price: May be WRONG for strategy-provided spreads (overstated)
  - entry_bid: NOT STORED (would improve exit P&L accuracy)
  - opt_type: NOT STORED (inferred from OCC symbol - fragile)

---

## RECOMMENDATIONS

### Priority 1 (CRITICAL):
✅ FIX ISSUE 1: Calculate net debit for strategy-provided spreads
  - Ensure entry_price is accurate (long_premium - short_premium)
  - Affects P&L accuracy for ~50% of spread trades

### Priority 2 (HIGH):
⚠️  FIX ISSUE 2: Track entry_bid alongside entry_price
  - More realistic P&L at exit time
  - Requires: Add entry_bid field to OptionsPosition
  - Affects: Exit P&L accuracy

### Priority 3 (MEDIUM):
⚠️  FIX ISSUE 3: Store opt_type in entry_legs
  - Make pricing module more robust
  - Eliminate OCC symbol parsing dependency
  - Affects: Debugging, clarity

---

## TESTING NEEDED

After fixes, run:

1. Test auto-derived spread (IV gate):
   - signal.spread_sell_strike = auto-calculated
   - signal.spread_sell_mid = None
   - Verify: entry_price = long_mid - bs_estimate

2. Test strategy-provided spread:
   - signal.spread_sell_strike = provided
   - signal.spread_sell_mid = provided
   - Verify: entry_price = signal.mid_price - signal.spread_sell_mid

3. Test credit spread (sell_to_open):
   - signal.action = "sell_to_open"
   - Verify: entry_price = -net_credit
   - Verify: P&L = (spread_mark - (-net_credit)) / abs(-net_credit)

4. Test P&L monitoring:
   - Call get_spread_complete_pricing() with entry_legs + entry_price
   - Verify: returned pnl_mark_pct is accurate
   - Verify: spread_bid is realistic exit value

---
""")

print("="*110)
print("END OF REVIEW")
print("="*110 + "\n")
