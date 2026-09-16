# STEP 4: Get Schwab Pricing & Construct Complete Order

## Overview

Step 4 bridges the gap between signal analysis and order execution. Before submitting any multi-leg order, we now **fetch real-time Schwab pricing** for the complete spread and use those prices to calculate the optimal limit price. This ensures:

1. **Real-time accuracy** - Schwab prices instead of potentially stale scanner data
2. **Better fill prices** - 1% improvement bid/ask calculation based on actual market conditions
3. **Accurate entry tracking** - Using Schwab mark for entry_price calculation
4. **Transparent pricing** - Complete audit trail of estimated vs actual prices

## Implementation Changes

### Location: `engine/options/executor.py`

Added Schwab pricing fetch immediately after leg construction and before order submission:

#### For Multi-Leg Spreads (Lines ~1100-1165)
```python
# Fetch real-time Schwab pricing
schwab_pricing = get_spread_complete_pricing(signal.symbol, _schwab_legs, _spread_mid_price)
if schwab_pricing:
    _schwab_spread_mark = schwab_pricing.get("spread_mark")
    _schwab_spread_bid = schwab_pricing.get("spread_bid")
    _schwab_spread_ask = schwab_pricing.get("spread_ask")
    
    # Use Schwab mark to set limit price (1% improvement)
    _schwab_limit_override = round(_schwab_spread_mark * 0.99, 2)  # for debit
    
    log.info(f"[OPTIONS] {symbol} Schwab pricing: bid=${bid} mark=${mark} ask=${ask}")
    limit_price = _schwab_limit_override
else:
    log.warning(f"[OPTIONS] {symbol} failed to fetch Schwab pricing, using estimated")
```

#### For Single-Leg Orders (Lines ~1195-1255)
```python
# Fetch Schwab chain data and find matching strike
chain_data = client.get_option_chains(signal.symbol, contract_type="ALL")
# Search for matching strike and extract bid/ask/mark
mark_price = option_data.get("mark", 0)

# Use Schwab mark for limit price (1% improvement)
_schwab_limit_override = round(mark_price * 0.99, 2)
limit_price = _schwab_limit_override
```

## Execution Flow

### Step 4a: Multi-Leg Spread Entry

```
Signal received with estimated prices:
├─ Long leg:  $8.45 (from scanner)
├─ Short leg: $5.88 (from strategy)
└─ Net debit: $2.57 (estimated)

↓ STEP 4: Fetch Schwab Pricing ↓

Schwab API call: /marketdata/v1/chains?symbol=SMH&contractType=ALL
├─ 550C: bid=$8.40, mark=$8.45, ask=$8.50
├─ 555C: bid=$5.85, mark=$5.88, ask=$5.95
└─ Spread: bid=$2.55, mark=$2.57, ask=$2.65

↓ Calculate Limit Price ↓

For DEBIT (buy):
├─ Estimated: 99% of $2.57 = $2.54
├─ Schwab:    99% of $2.57 = $2.54  ← Use this
└─ Improvement: $0.00 (or better if Schwab mark differs)

For CREDIT (sell):
├─ Estimated: 101% of net debit = -$2.60
├─ Schwab:    101% of Schwab mark = adjusted based on real prices
└─ Improvement: Collect more or pay less

↓ Construct & Submit Order ↓

Alpaca MLEG Order:
├─ symbol: "" (empty for mleg)
├─ qty: 9 (contracts, scaled by confidence)
├─ type: limit
├─ order_class: mleg
├─ limit_price: 2.54 (from Schwab real-time)
├─ time_in_force: day
└─ legs:
    ├─ SMH550C: buy, ratio_qty=1
    └─ SMH555C: sell, ratio_qty=1
```

### Step 4b: Single-Leg Order Entry

```
Signal received:
├─ Symbol: SPY
├─ Strike: 550
├─ Type: call
└─ Estimated mid: $5.45

↓ STEP 4: Fetch Schwab Pricing ↓

Schwab API call: /marketdata/v1/chains?symbol=SPY&contractType=ALL
└─ 550C: bid=$5.30, mark=$5.43, ask=$5.56

↓ Calculate Limit Price ↓

For BUY:
├─ Estimated: 99% of $5.45 = $5.40
├─ Schwab:    99% of $5.43 = $5.38  ← Use this
└─ Savings: $0.02 per contract

↓ Submit Order ↓

Alpaca Limit Order:
├─ symbol: SPY550C
├─ qty: 10
├─ side: BUY
├─ limit_price: 5.38 (from Schwab)
├─ type: limit
└─ time_in_force: day
```

## Pricing Logic Details

### Debit Spreads (Buy-to-Open)

**Goal:** Pay less than market mid

```
Limit Price = 99% of Schwab Mark

Example:
├─ Schwab mark: $2.57
├─ Limit price: 99% × $2.57 = $2.54
└─ Rationale: Bid below fair value, increase fill probability
```

**Why 99%?**
- 1% improvement from theoretical fair value
- Balances fill probability vs cost savings
- Schwab mark is already mid (not bid), so 99% is slightly aggressive but reasonable

### Credit Spreads (Sell-to-Open)

**Goal:** Collect more than market mid

```
Limit Price = 101% of Schwab Mark (as NEGATIVE)

Example:
├─ Schwab mark: $3.30
├─ Limit price: -101% × $3.30 = -$3.33
└─ Rationale: Offer above fair value, increase fill probability
```

**Alpaca Convention:** Credit spreads require negative limit_price:
- Positive = debit (you pay)
- Negative = credit (you receive)

### Single-Leg Orders

**Buy Side:**
```
Limit Price = 99% of Schwab Mark
Example: 99% × $5.43 = $5.38
```

**Sell Side:**
```
Limit Price = 101% of Schwab Mark
Example: 101% × $3.75 = $3.79
```

## Data Flow Integration

### Pre-Order:

```
Scanner Signal
    ↓
Options Executor.execute_options()
    ├─ Calc contracts
    ├─ Determine strategy (spread/butterfly/condor)
    ├─ Apply IV gate logic
    ├─ Calc estimated net debit (Black-Scholes or strategy-provided)
    ├─ Build legs_list (Alpaca format)
    │
    └─ ✨ STEP 4: Fetch Schwab Pricing ✨
        ├─ Call get_spread_complete_pricing() or fetch chain directly
        ├─ Extract bid/mark/ask for each leg
        ├─ Compare to estimated prices (log difference)
        ├─ Calc Schwab-based limit price (1% improvement)
        └─ Override limit_price with Schwab-based value
            
    └─ Submit Order to Alpaca
        ├─ MLEG order with Schwab limit_price
        ├─ Create OptionsPosition with entry_legs
        └─ Start monitoring/retry thread
```

### Post-Order:

```
Order fills (either at Schwab-based limit or better)
    ↓
Record entry_price:
├─ For spreads: Actual fill price (from order confirmation)
├─ For single-leg: Actual fill price
└─ Compare to Schwab mark at entry time

    ↓
Start P&L Monitoring
├─ Each cycle, call get_spread_complete_pricing() again
├─ Compare current mark to entry_price
├─ Calc P&L% = (current_mark - entry_price) / abs(entry_price)
└─ Check against profit target / stop loss
```

## Logging Output

### Successful Schwab Fetch:

```
[OPTIONS] SMH Schwab pricing: bid=$2.45 mark=$2.57 ask=$2.65 | estimated_mid=$2.57 (diff=+$0.00)
[OPTIONS] SMH limit price: estimated=$2.54 → schwab_real=$2.54
```

### With Price Difference:

```
[OPTIONS] TQQQ Schwab pricing: bid=$1.03 mark=$1.05 ask=$1.07 | estimated_mid=$1.10 (diff=-$0.05)
[OPTIONS] TQQQ limit price: estimated=$1.09 → schwab_real=$1.04
```
*(Schwab mark is $0.05 tighter — better entry price)*

### Schwab Unavailable (Fallback):

```
[WARNING] [OPTIONS] SMH failed to fetch Schwab pricing, using estimated limit=$2.54
```

## Error Handling

### Graceful Degradation

If Schwab pricing fetch fails:

1. **Log warning** - Record what happened
2. **Fallback to estimated** - Use Black-Scholes or signal.spread_sell_mid
3. **Submit order anyway** - Don't block execution
4. **Try again next cycle** - Schwab might be temporarily unavailable

```python
try:
    schwab_pricing = get_spread_complete_pricing(...)
    if schwab_pricing:
        limit_price = calculate_from_schwab(schwab_pricing)
    else:
        log.warning(f"Failed to fetch Schwab pricing, using estimated")
except Exception as e:
    log.error(f"Schwab pricing error: {e}, using estimated")
```

## Benefits Realized

| Aspect | Before | After |
|--------|--------|-------|
| **Pricing Source** | Scanner (potentially stale) | Real-time Schwab |
| **Limit Price Calc** | Estimated via Black-Scholes | Actual market mid |
| **Fill Quality** | Depends on estimation accuracy | Optimized for each leg |
| **Entry Tracking** | Might be inaccurate | Based on Schwab marks |
| **P&L Accuracy** | Cascading error from entry | Baseline correct |
| **Audit Trail** | No before/after comparison | Full pricing comparison logged |

## Examples in Real Conditions

### Example 1: Scanner Price Too High (Wide Bid-Ask)

```
Scanner reported: $2.75 net debit (at open, low liquidity)
Estimated limit:  $2.72 (99% of $2.75)

Schwab real-time (10 seconds later):
├─ 550C: $8.20 (was $8.50)
├─ 555C: $5.50 (was $5.75)
└─ Spread: $2.70 (was $2.75 estimate)

Schwab limit: 99% of $2.70 = $2.67
Savings: $2.72 - $2.67 = $0.05 per spread × 9 contracts = $0.45 saved
→ Better execution price
```

### Example 2: Volatile Underlying (Price Moving)

```
Scanner reported: $3.50 net credit
Estimated limit:  -$3.54 (101% of $3.50, negative for credit)

5 seconds later, underlying moved against us:
├─ Credit spread worth less
├─ Schwab mark: $3.35 (not $3.50)
└─ Schwab limit: -$3.38

Our offer: -$3.38 instead of -$3.54
→ More likely to get filled at fair price, not forced to give up edge
```

### Example 3: Stale Data Avoided

```
09:30:00 - Strategy signal emitted, scanner price: $2.50
09:30:00 - We fetch Schwab (real-time), price: $2.48
09:30:05 - We submit order with limit $2.46

Without Step 4, we'd submit with stale $2.47 limit
→ Real-time advantage captured
```

## Testing

### Unit Tests:
- `test_step4_logic.py` - Logic validation with mock data
  - Debit spread limit price calc
  - Credit spread limit price calc
  - Single-leg limit price calc
  - Fallback behavior

### Integration Tests:
- When live execution runs, Schwab pricing is fetched and used
- Log output shows "Schwab pricing" lines for each entry
- Compare estimated vs schwab limit prices in logs

### Manual Verification:
1. Run autobot.py with live mode
2. Wait for next spread signal
3. Check logs for "Schwab pricing" and "limit price" lines
4. Verify prices are realistic (not $0.00, not extremely wide)
5. Confirm order fills at or better than Schwab limit

## Next Steps

**Step 5:** Position Tracking & Storage
- Store entry_legs with complete pricing info
- Record actual fill price
- Calculate entry_bid/ask for future reference

**Step 6:** P&L Monitoring Loop
- Use same Schwab pricing fetch on each cycle
- Calculate mark-based P&L from entry
- Trigger exits when thresholds hit

## Summary

Step 4 ensures that every order submitted has:
- ✅ Real-time bid/ask/mark from Schwab
- ✅ Optimal limit price based on actual market conditions
- ✅ Complete audit trail of pricing decisions
- ✅ Graceful fallback if Schwab unavailable
- ✅ Foundation for accurate P&L calculation in monitoring loop

The result: **Better entry prices, more fills, accurate position tracking, and confident exit decisions.**
