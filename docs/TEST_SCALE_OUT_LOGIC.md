# Scale-Out Exit Logic Implementation

## Overview
Implemented refined profit-taking strategy: **Close 50% at +50%, hold 50% with +20% breakeven stop for max profit.**

## Configuration Changes

**engine/config.py:**
```python
OPTIONS_PROFIT_TARGET_1_PCT      = 50.0  # Close 50% of position at +50% (lock solid profit)
OPTIONS_PROFIT_TARGET_1_STOP_PCT = 20.0  # New stop for 2nd half at +20% (breakeven guard)
OPTIONS_PROFIT_TARGET_2_PCT     = 100.0  # Close remaining 50% at +100% or max profit
```

## Position Tracking

**OptionsPosition dataclass additions:**
- `scaled_out_qty`: int = 0  # Tracks how many contracts closed at tier1
- `tier1_scale_out_stop`: float = 0.0  # New stop price for second half

## Exit Logic Flow

### FIRST HALF - Scale Out at +50%
```
Condition: pnl_pct >= 50.0% AND not pos.tier1_closed

Actions:
├─ Calculate qty_to_close = contracts // 2
├─ Submit market order to close first half
├─ Log proceeds and P&L
├─ Update position.contracts = remaining qty
├─ Set position.scaled_out_qty = qty closed
├─ Set position.tier1_closed = True
├─ Calculate new stop: entry_price * 1.20 (+20% breakeven)
└─ Store in position.tier1_scale_out_stop

Example (10 contracts):
  Entry: $1.90 per contract
  At mark: $2.85 (P&L: +50%)
  → Close: 5 contracts @ $2.85 = $14.25 proceeds
  → Remaining: 5 contracts
  → New stop: $1.90 * 1.20 = $2.28 (+20%)
```

### SECOND HALF - New Stop Logic at +20%
```
Condition: pos.tier1_closed AND pnl_pct <= 20.0%

Actions:
├─ Hit new stop (+20% breakeven guard)
├─ Close remaining contracts at market
├─ Log loss recovery position
└─ Mark position for closure

Example (continued):
  Second half at $2.28 (P&L: +20%)
  → Hit new stop
  → Close: 5 remaining @ $2.28 = $11.40 proceeds
  → Total: $14.25 + $11.40 = $25.65
  → Original cost: 10 * $1.90 = $19.00
  → Total P&L: +$6.65 (+35% blended)
```

### SECOND HALF - Max Profit Target at +100%
```
Condition: pos.tier1_closed AND pnl_pct >= 100.0%

Actions:
├─ Hit max profit target (+100%)
├─ Close remaining contracts at market
├─ Lock full max profit
└─ Mark position for closure

Example (best case):
  Second half at $3.80 (P&L: +100%)
  → Hit max profit
  → Close: 5 remaining @ $3.80 = $19.00 proceeds
  → Total: $14.25 + $19.00 = $33.25
  → Original cost: 10 * $1.90 = $19.00
  → Total P&L: +$14.25 (+75% blended)
```

## Expected Behavior Summary

| Scenario | P&L Path | Execution | Result |
|----------|----------|-----------|--------|
| **Normal Win** | 0% → +50% → +80% → stop | Close 50% @ +50%, close 50% @ +20% | +35% blended |
| **Big Winner** | 0% → +50% → +100% | Close 50% @ +50%, close 50% @ +100% | +75% blended |
| **Recovery** | 0% → +50% → -10% → +20% | Close 50% @ +50%, close 50% @ +20% | +35% blended |
| **Quick Reversal** | 0% → +40% → -40% | Hold, hit stop, full position closes | -40% loss |

## Code Changes

**engine/options/executor.py:**

1. **Added config import:**
   - `OPTIONS_PROFIT_TARGET_1_STOP_PCT`

2. **Updated OptionsPosition dataclass:**
   - `scaled_out_qty: int = 0`

3. **Updated exit trigger logic (lines 1673-1738):**
   - First-half scale-out at +50% (market order)
   - Position update with remaining qty
   - New stop calculation for second half
   - Second-half stop loss check at +20%
   - Second-half profit target at +100%

## Risk Management Benefits

✅ **Locks solid profit early** (50% closed at +50%)
✅ **Reduces theta risk** (only 50% exposed to decay)
✅ **Preserves upside** (second half can capture +100%)
✅ **Protects against reversals** (+20% floor on second half)
✅ **Better sleep** (half secured before expiration)
✅ **Industry standard** for options traders

## Testing

Run the options scan to see scale-out in action:
```bash
$env:TRADE_MODE="paper"
apextrader\Scripts\python.exe scripts\_run_options_scan.py
```

Monitor position P&L in logs for:
- "scale-out triggered at +50%" messages
- "second half position" update logs
- "second-half target hit" or "second-half stop hit" closures

## Example Log Output

```
[INFO] OPTIONS: MNKD scale-out triggered at +50.2% — closing 5/10 contracts at market
[INFO] OPTIONS: MNKD first half closed: 5 contracts @ $2.85 = $14.25 (P&L: +50.2%)
[INFO] OPTIONS: MNKD second half position: 5 contracts, new stop at +20% ($2.28), target at +100%
...
[WARNING] OPTIONS: MNKD second-half stop hit at +20.1% (stop=20.0%) — closing remaining 5
[INFO] OPTIONS: MNKD second half closed: 5 contracts @ $2.28 = $11.40 (P&L: +20%)
```

## Backward Compatibility

- Existing positions without `scaled_out_qty` default to 0 (works fine)
- Single-entry positions still work (no-scale-out if never hit +50%)
- All other exit logic unchanged (DTE guards, grace periods, etc.)
