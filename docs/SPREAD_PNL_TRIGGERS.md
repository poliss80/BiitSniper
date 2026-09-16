# Spread P&L Calculation & Trade Triggers

## 1. P&L Calculation (Correct Way)

### Entry vs Exit Prices
```
Entry: What you PAID to enter the spread (Ask price at entry)
Exit:  What you'd GET to close the spread (Bid price at exit)
```

### P&L Formula
```
Position Value = Bid Price (what you can sell for now)
Entry Cost = Entry Price (what you paid)
P&L $ = Bid Price - Entry Cost
P&L % = (Bid Price - Entry Cost) / Entry Cost × 100
```

### Example: SMH 550/555 Spread
```
Entry Cost: $1.90 (you paid this to open)
Current Bid: $0.35 (you'd get this to close)
Current Mark: $2.57 (theoretical mid)
Current Ask: $4.80 (cost to buy more)

WRONG: Using Ask ($4.80) → +152% (unrealistic, you can't exit at ask)
CORRECT: Using Bid ($0.35) → -81.6% (realistic, you'd close at bid)
```

**✅ Use BID price for P&L calculation (most conservative, realistic exit)**

---

## 2. Spread Exit Triggers (Recommended)

### Trigger 1: Profit Taking at 50% of Max Gain
```
Max Profit = Long Strike - Short Strike - Entry Cost
Example: (555 - 550) - $1.90 = $3.10 max profit

Target: 50% of max gain
Exit when: Bid Price ≥ Entry Cost + (Max Profit × 0.50)
         = $1.90 + ($3.10 × 0.50) = $3.45 bid

Current SMH 550/555: Bid=$0.35 (not yet)
Need Bid to reach $3.45 to capture 50% of max
```

### Trigger 2: Underlying Movement Stop Loss
```
Example: SMH 550/555 spread
Long Strike: 550
Risk Level: 1% below long strike = $549.50

If SMH falls below $549.50 → Close spread (underlying too weak)
Rationale: Spread was bullish, if market breaks key level, exit

Current SMH: ~$549.76 (CHECK LIVE DATA)
Status: WATCH - Getting close to SL
```

### Trigger 3: Time Decay at Low DTE
```
If Days to Expiration ≤ 7 days:
  AND P&L % < 50% of max:
    → Close spread (avoid theta crush)
    
Rationale: In final week, theta accelerates, position becomes volatile
Better to close at current value than risk last-week collapse
```

### Trigger 4: Bid-Ask Spread Widening (Liquidity Alert)
```
If Bid-Ask Spread > 10% of mid price:
  → Close ASAP (illiquid, slippage risk)
  
Example: Mid=$2.57, Ask=$4.80
  Spread = $4.80 - $0.35 = $4.45 (wide!)
  % of mid = $4.45 / $2.57 = 173% (VERY WIDE)
  
Action: This is a warning to close before liquidity dries up
```

---

## 3. Your Current Portfolio Analysis

### SMH 550/555 (10 contracts, Entry $1.90)
```
Current: Bid=$0.35, Mark=$2.57, Ask=$4.80
P&L (realistic/bid): -81.6%
Max Profit: $3.10
Status: DEEP LOSS - Consider closing
Reason: Spread moved against you significantly
```

### SMH 560/565 (2 contracts, Entry $2.00)
```
Current: Bid=$0.05, Mark=$1.56, Ask=$3.05
P&L (realistic/bid): -97.5%
Max Profit: $3.00
Status: CRITICAL - Close immediately
Reason: Nearly worthless, no recovery likely
```

### TQQQ 71/73 (Entry $1.50)
```
Current: Bid=$0.96, Mark=$1.05, Ask=$1.13
P&L (realistic/bid): -36.0%
Max Profit: $2.00
Status: Recoverable
Profit Target (50%): $1.50 + ($2.00 × 0.50) = $2.50 bid
Action: Hold, monitor for recovery
```

### IWM 290/295 (Entry $2.25)
```
Current: Bid=-$0.01 (invalid), Mark=$0.00, Ask=$0.01
P&L (realistic/bid): -100.0%
Max Profit: $3.00
Status: DEAD
Action: Close immediately (worthless)
```

---

## 4. Recommended Action Plan

### IMMEDIATE (Next 5 minutes)
1. **SMH 560/565**: Close at bid ($0.05) - Stop the bleeding
2. **IWM 290/295**: Close at bid ($0.01) - Already dead

### SHORT TERM (Next 1 hour)
3. Monitor **SMH 550/555** for any recovery toward $0.50-$0.70 bid
   - If rebounds → Close at +5-10% improvement
   - If continues falling → Close before additional losses

### ONGOING (Daily)
4. **TQQQ 71/73**: Watch for recovery, target close at $2.50 bid
5. Set alerts on underlying prices (SMH $549.50 SL, TQQQ support level)

---

## 5. Code Implementation Strategy

### P&L Calculation Function
```python
def calculate_spread_pnl(bid_price, ask_price, entry_price, mark_price):
    """
    Calculate realistic spread P&L using bid price (exit proceeds)
    
    Args:
        bid_price: What you'd GET to close the spread
        ask_price: What you'd PAY to enter more
        entry_price: What you paid to open
        mark_price: Theoretical mid (for reference only)
    
    Returns:
        pnl_pct: Realistic P&L % (using bid)
        status: "PROFIT", "LOSS", "CRITICAL"
    """
    pnl_pct = (bid_price - entry_price) / entry_price * 100
    
    if pnl_pct >= 50:
        status = "PROFIT"
    elif pnl_pct >= 0:
        status = "BREAKEVEN"
    elif pnl_pct >= -50:
        status = "LOSS"
    else:
        status = "CRITICAL"
    
    return pnl_pct, status
```

### Trigger Decision Function
```python
def should_close_spread(bid, entry_price, max_profit, underlying_price, 
                        long_strike, dte, underlying_name):
    """
    Check all 4 triggers to decide if should close
    
    Returns:
        (should_close: bool, reason: str)
    """
    
    # Trigger 1: Profit target hit (50% of max)
    if bid >= entry_price + (max_profit * 0.5):
        return True, "PROFIT_TARGET_HIT"
    
    # Trigger 2: Underlying break below long strike
    if underlying_price < (long_strike * 0.99):  # 1% below
        return True, "UNDERLYING_SL_BROKEN"
    
    # Trigger 3: DTE ≤ 7 days AND not profitable
    pnl_pct = (bid - entry_price) / entry_price * 100
    if dte <= 7 and pnl_pct < (max_profit * 0.5 / entry_price * 100):
        return True, "DTE_THETA_RISK"
    
    # Trigger 4: Critical loss (>80%)
    if pnl_pct < -80:
        return True, "CRITICAL_LOSS"
    
    return False, "HOLD"
```

---

## 6. Key Principles

✅ **Always use BID price for P&L** (that's your real exit value)
✅ **Profit targets**: 50% of max spread profit
✅ **Stop losses**: Underlying breaks 1% below long strike
✅ **Time-based**: Exit if DTE ≤ 7 and not near profit target
✅ **Liquidity check**: Close if bid-ask spread > 10% of mid
✅ **Critical losses**: Auto-close spreads at -80% or worse

---

## 7. Integration into Bot

### In executor.py watchdog loop:
```python
for pos in positions:
    if pos.is_spread:
        # Get Schwab bid/ask prices
        bid, ask, mark = get_spread_prices_from_schwab(pos)
        
        # Calculate realistic P&L
        pnl_pct = (bid - pos.entry_price) / pos.entry_price * 100
        
        # Check triggers
        should_close, reason = should_close_spread(
            bid, pos.entry_price, pos.max_profit,
            underlying_price, pos.long_strike, pos.dte
        )
        
        if should_close:
            log.info(f"CLOSE {pos.symbol}: {reason} - "
                    f"Bid=${bid:.2f}, P&L={pnl_pct:+.1f}%")
            close_position(pos, bid)  # Close at market bid
```
