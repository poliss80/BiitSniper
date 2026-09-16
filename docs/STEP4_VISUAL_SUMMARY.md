# STEP 4 VISUAL SUMMARY: Schwab Pricing & Order Construction

## Complete Order Flow with Step 4

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    COMPLETE ORDER FLOW WITH STEP 4                          │
└─────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────┐
│ SIGNAL RECEIVED                                                              │
│ ────────────────                                                             │
│ Strategy emits: SMH 550/555 Bull Call Spread                                 │
│   confidence: 88%                                                            │
│   mid_price: $8.45 (long leg from scanner)                                   │
│   spread_sell_mid: $5.88 (short leg estimated)                               │
│   action: buy (debit)                                                        │
│   contracts_base: 10 → scaled to 9 by confidence                             │
└──────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌──────────────────────────────────────────────────────────────────────────────┐
│ STEP 1-3: Pre-flight Checks & Strategy Analysis                              │
│ ───────────────────────────────────────────────                              │
│ ✓ Account check: $50k available, no restrictions                             │
│ ✓ Budget: 9 contracts × $2.57 = $2,313 buying power needed                   │
│ ✓ PDT: Not small account, or DTE > min, no day trade impact                  │
│ ✓ Strategy type: is_spread=True, is_mleg=True (multi-leg detected)           │
│ ✓ IV gate: IV rank 42% < 35% → Keep as spread (no force)                     │
│ ✓ Net debit calc:                                                            │
│   - Long: $8.45 (signal.mid_price)                                           │
│   - Short: $5.88 (signal.spread_sell_mid provided)                           │
│   - Net debit: $8.45 - $5.88 = $2.57 ← ESTIMATED entry_price                │
└──────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌──────────────────────────────────────────────────────────────────────────────┐
│ ✨ STEP 4: SCHWAB PRICING FETCH & ORDER CONSTRUCTION ✨                       │
│ ────────────────────────────────────────────────────                         │
│                                                                              │
│ A) Build legs_list (Alpaca format)                                           │
│    ├─ {"symbol": "SMH550C", "side": BUY, "ratio_qty": 1}                     │
│    └─ {"symbol": "SMH555C", "side": SELL, "ratio_qty": 1}                    │
│                                                                              │
│ B) Convert to Schwab format for pricing                                      │
│    ├─ {"occ_symbol": "SMH550C", "side": "buy", "strike": 550.0, ...}         │
│    └─ {"occ_symbol": "SMH555C", "side": "sell", "strike": 555.0", ...}       │
│                                                                              │
│ C) FETCH REAL-TIME SCHWAB PRICING                                            │
│    │                                                                         │
│    │   GET /marketdata/v1/chains?symbol=SMH&contractType=ALL                │
│    │   ↓                                                                      │
│    │   Response includes:                                                    │
│    │   ├─ callExpDateMap[2026-05-08]                                         │
│    │   │  ├─ [550.0]: [bid: 8.40, mark: 8.45, ask: 8.50, ...]              │
│    │   │  └─ [555.0]: [bid: 5.85, mark: 5.88, ask: 5.95, ...]              │
│    │   └─ putExpDateMap: [...]                                              │
│    │                                                                         │
│    ↓                                                                          │
│    CALCULATE SPREAD PRICING (from leg bids/marks/asks)                       │
│    ├─ Bid:  550C_bid - 555C_ask = 8.40 - 5.95 = $2.45                       │
│    ├─ Mark: 550C_mark - 555C_mark = 8.45 - 5.88 = $2.57                     │
│    ├─ Ask:  550C_ask - 555C_bid = 8.50 - 5.85 = $2.65                       │
│    └─ DTE: 2 days (from expiration: 2026-05-08)                              │
│                                                                              │
│ D) COMPARE ESTIMATED vs SCHWAB                                               │
│    Estimated net debit: $2.57                                                │
│    Schwab spread mark:  $2.57                                                │
│    Difference:          $0.00 (lucky! usually differs by $0.05-0.20)         │
│                                                                              │
│    LOG: "[OPTIONS] SMH Schwab pricing: bid=$2.45 mark=$2.57 ask=$2.65"       │
│                                                                              │
│ E) CALCULATE LIMIT PRICE (1% improvement)                                    │
│    For DEBIT (buy): 99% of mark = 0.99 × $2.57 = $2.54                      │
│    For CREDIT (sell): 101% of mark = 1.01 × mark = negative for Alpaca      │
│                                                                              │
│    LOG: "[OPTIONS] SMH limit price: estimated=$2.54 → schwab=$2.54"         │
│                                                                              │
│    This is the limit price we'll use for the order!                          │
└──────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌──────────────────────────────────────────────────────────────────────────────┐
│ ORDER CONSTRUCTION & SUBMISSION                                              │
│ ─────────────────────────────────                                            │
│                                                                              │
│ Build Payload (Alpaca MLEG Order):                                           │
│ {                                                                            │
│   "symbol": "",                      ← Empty for mleg                        │
│   "qty": "9",                        ← Contracts (scaled by confidence)       │
│   "type": "limit",                                                           │
│   "order_class": "mleg",                                                     │
│   "limit_price": "2.54",             ← FROM SCHWAB (Step 4!)                 │
│   "time_in_force": "day",                                                    │
│   "legs": [                                                                  │
│     {                                                                        │
│       "symbol": "SMH550C",                                                   │
│       "side": "buy",                                                         │
│       "ratio_qty": "1",                                                      │
│       "position_intent": "buy_to_open"                                       │
│     },                                                                       │
│     {                                                                        │
│       "symbol": "SMH555C",                                                   │
│       "side": "sell",                                                        │
│       "ratio_qty": "1",                                                      │
│       "position_intent": "sell_to_open"                                      │
│     }                                                                        │
│   ]                                                                          │
│ }                                                                            │
│                                                                              │
│ Submit to Alpaca: POST /orders → OrderID: 87654321                           │
│                                                                              │
│ LOG: "[OPTIONS] Submitting MLEG SMH: {payload...}"                           │
│      "[OPTIONS] Order submitted, monitoring for fill..."                     │
└──────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌──────────────────────────────────────────────────────────────────────────────┐
│ ADAPTIVE LIMIT RETRY (Background Thread)                                     │
│ ──────────────────────────────────────                                       │
│                                                                              │
│ Polls order status every 5-10 seconds:                                       │
│ • Order PENDING → Wait                                                       │
│ • Order FILLED → Record fill price, create OptionsPosition                   │
│ • Order REJECTED → Log error                                                 │
│ • Order CANCELLED → Retry with adjusted limit                                │
│                                                                              │
│ Status: FILLED @ $2.55 (slightly better than limit!)                         │
└──────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌──────────────────────────────────────────────────────────────────────────────┐
│ POSITION TRACKING (STEP 5)                                                   │
│ ───────────────────────                                                      │
│                                                                              │
│ Create OptionsPosition:                                                      │
│ {                                                                            │
│   occ_symbol: "SMH550C",             ← Primary leg                           │
│   symbol: "SMH",                                                             │
│   option_type: "call",                                                       │
│   action: "buy",                                                             │
│   strike: 550.0,                                                             │
│   expiry: 2026-05-08,                                                        │
│   contracts: 9,                                                              │
│   entry_price: 2.55,                 ← ACTUAL FILL (was estimated $2.57)    │
│   strategy: "bull_call_spread",                                              │
│   entry_legs: [                      ← COMPLETE RECORD                       │
│     {                                                                        │
│       occ_symbol: "SMH550C",                                                 │
│       side: "buy",                                                           │
│       ratio_qty: 1,                                                          │
│       strike: 550.0,                                                         │
│       opt_type: "call"                ← ADDED in Step 4 improvements         │
│     },                                                                       │
│     {                                                                        │
│       occ_symbol: "SMH555C",                                                 │
│       side: "sell",                                                          │
│       ratio_qty: 1,                                                          │
│       strike: 555.0,                                                         │
│       opt_type: "call"                                                       │
│     }                                                                        │
│   ],                                                                         │
│   entry_bid: [None],                 ← Available if needed later             │
│   entry_iv: 42%,                                                             │
│   peak_pnl_pct: 0%,                  ← At entry                              │
│ }                                                                            │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Pricing Details: Before vs After Step 4

### Before Step 4 (Without Schwab Pricing)

```
┌─ Signal received (potentially stale scanner data)
│  ├─ Long 550C: $8.45 (from scanner 10 seconds ago)
│  └─ Short 555C: $5.88 (strategy estimate or scanner)
│
├─ Estimate net debit: $2.57
│
├─ No real-time validation
│
└─ Submit order with estimated limit: $2.54
   ├─ Might be too high (loses money on fill)
   ├─ Might be too low (order doesn't fill)
   └─ No way to know which

RISK: Entry price inaccuracy → P&L calculations wrong from start!
```

### After Step 4 (With Schwab Pricing)

```
┌─ Signal received
│  ├─ Long 550C: $8.45 (from scanner)
│  └─ Short 555C: $5.88 (from strategy)
│
├─ Estimate net debit: $2.57
│
├─ ✨ FETCH REAL-TIME SCHWAB PRICING (0 seconds old)
│  └─ 550C: bid=$8.40, mark=$8.45, ask=$8.50
│     555C: bid=$5.85, mark=$5.88, ask=$5.95
│     Spread: bid=$2.45, mark=$2.57, ask=$2.65
│
├─ Compare and log
│  └─ Estimated: $2.57 | Schwab mark: $2.57 | Diff: $0.00
│     (Or might differ: $2.57 estimated vs $2.52 actual = $0.05 savings!)
│
└─ Submit order with Schwab limit: $2.54
   ├─ Based on real-time market data
   ├─ Optimized for likelihood of fill
   └─ Audit trail of pricing decision

BENEFIT: Entry price accuracy → P&L calculations correct from start!
```

## Key Differences by Scenario

### Scenario 1: Sideways Market (No Major Price Move)

```
Signal sent: 09:30:15
Scanner price: 550C=$8.45, 555C=$5.88, spread=$2.57
Schwab real-time (09:30:15): Same
Order: Submitted with limit $2.54
Result: Filled at $2.54-$2.55
Entry price recorded: $2.55
P&L accuracy: 100% ✅
```

### Scenario 2: Underlying Moving UP (Good for our long call)

```
Signal sent: 09:30:15
Scanner price: 550C=$8.45, 555C=$5.88, spread=$2.57
Schwab real-time (09:30:20): Underlying moved up $1.00
  550C=$9.25, 555C=$6.65, spread=$2.60
Order: Submitted with Schwab-based limit $2.57 (99% of $2.60)
Result: Filled at $2.57 (better than estimated $2.54!)
Entry price recorded: $2.57
Advantage: Got in at better price due to real-time update
```

### Scenario 3: Wide Bid-Ask (Illiquid)

```
Signal sent: 09:30:15
Scanner price: 550C=$8.45, 555C=$5.88, spread=$2.57 (mid-estimate)
Schwab real-time (09:30:20): Wide market
  550C bid=$8.20, ask=$8.70
  555C bid=$5.60, ask=$6.10
  Spread bid=$2.10, mark=$2.60, ask=$3.10
Order: Submitted with Schwab limit $2.57 (99% of $2.60 mark)
Result: Filled at $2.57 (inside the ask=$3.10!)
Entry price recorded: $2.57
Advantage: Got reasonable fill despite wide spreads
```

## Summary Table

| Aspect | Without Step 4 | With Step 4 |
|--------|---|---|
| **Pricing Source** | Scanner (potentially stale) | Real-time Schwab API |
| **Limit Calc** | Estimated via Black-Scholes | Actual market bids/asks/marks |
| **Price Update Timing** | 10-30 seconds old | 0-2 seconds old |
| **Fill Quality** | Depends on estimation accuracy | Optimized for real conditions |
| **Audit Trail** | No record of pricing decision | Complete "estimated vs actual" log |
| **Fallback Available** | N/A | Yes, if Schwab fails |
| **Entry Price Accuracy** | Risk of error | Baseline accurate |
| **P&L Monitoring** | Starts from uncertain base | Starts from accurate base |

## Code Location

**File:** `engine/options/executor.py`

**For Multi-Leg Spreads:**
- Lines ~1050-1090: Calculate estimated net debit
- Lines ~1100-1165: **← STEP 4 NEW CODE**
  - Fetch Schwab pricing
  - Compare to estimated
  - Calculate Schwab-based limit
  - Override limit_price

**For Single-Leg Orders:**
- Lines ~1050-1060: Calculate estimated limit price
- Lines ~1195-1255: **← STEP 4 NEW CODE**
  - Fetch Schwab chain data
  - Find matching strike
  - Extract bid/mark/ask
  - Calculate Schwab-based limit
  - Override limit_price

## Next: Step 5 - Position Tracking

Once order fills:
- Record actual fill price as entry_price
- Store entry_legs with complete leg info (including opt_type)
- Begin P&L monitoring with get_spread_complete_pricing() every cycle
- Check against profit targets and stop losses
- Execute exits when conditions met

---

**Step 4 Status: ✅ COMPLETE**
- Code implemented and syntax validated
- Logic tested with mock data
- Ready for live testing when market opens
