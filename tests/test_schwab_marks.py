#!/usr/bin/env python3
"""Test current mark prices from Schwab API for multi-leg positions."""

import os
import logging
from pathlib import Path

# Load .env file like the watchdog does
ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    for raw_line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip().strip('"').strip("'")
    print(f"✅ Loaded .env file from {ENV_FILE}\n")
else:
    print(f"⚠️  .env file not found at {ENV_FILE}")
    print("   Place your credentials in .env before running tests\n")

from engine.utils.schwab_pricing import get_spread_mark_from_schwab

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("TestMarks")

# Test cases: [underlying, legs, entry_price, description]
TEST_CASES = [
    # SMH 550/555 bull call spread
    (
        "SMH",
        [
            {"occ_symbol": "SMH550C", "side": "buy", "ratio_qty": 1, "strike": 550.0},
            {"occ_symbol": "SMH555C", "side": "sell", "ratio_qty": 1, "strike": 555.0},
        ],
        1.90,
        "SMH 550/555 Bull Call (10 contracts @ $1.90 debit)"
    ),
    
    # SMH 560/565 bull call spread
    (
        "SMH",
        [
            {"occ_symbol": "SMH560C", "side": "buy", "ratio_qty": 1, "strike": 560.0},
            {"occ_symbol": "SMH565C", "side": "sell", "ratio_qty": 1, "strike": 565.0},
        ],
        2.00,
        "SMH 560/565 Bull Call (2 contracts @ $2.00 debit)"
    ),
    
    # TQQQ 71/73 bull call spread
    (
        "TQQQ",
        [
            {"occ_symbol": "TQQQ71C", "side": "buy", "ratio_qty": 1, "strike": 71.0},
            {"occ_symbol": "TQQQ73C", "side": "sell", "ratio_qty": 1, "strike": 73.0},
        ],
        1.50,
        "TQQQ 71/73 Bull Call @ $1.50 debit"
    ),
    
    # IWM 290/295 bull call spread
    (
        "IWM",
        [
            {"occ_symbol": "IWM290C", "side": "buy", "ratio_qty": 1, "strike": 290.0},
            {"occ_symbol": "IWM295C", "side": "sell", "ratio_qty": 1, "strike": 295.0},
        ],
        2.25,
        "IWM 290/295 Bull Call @ $2.25 debit"
    ),
]

def test_mark_prices():
    """Test Schwab mark pricing for all multi-leg positions."""
    log.info("=" * 80)
    log.info("TESTING SCHWAB MARK PRICES FOR MULTI-LEG POSITIONS")
    log.info("=" * 80)
    
    results = []
    for underlying, legs, entry_price, description in TEST_CASES:
        log.info(f"\n{description}")
        log.info(f"  Underlying: {underlying} | Entry: ${entry_price:.2f}")
        
        try:
            current_mark, pnl_pct = get_spread_mark_from_schwab(
                underlying, legs, entry_price
            )
            
            if current_mark is not None and pnl_pct is not None:
                log.info(f"  ✅ SUCCESS")
                log.info(f"     Current Mark: ${current_mark:.2f}")
                log.info(f"     P&L: {pnl_pct:+.1f}%")
                results.append((description, "✅", current_mark, pnl_pct))
            else:
                log.warning(f"  ⚠️  No pricing data returned")
                results.append((description, "⚠️", None, None))
        except Exception as e:
            log.error(f"  ❌ ERROR: {e}")
            results.append((description, "❌", None, str(e)))
    
    # Summary
    log.info("\n" + "=" * 80)
    log.info("SUMMARY")
    log.info("=" * 80)
    for desc, status, mark, pnl in results:
        if status == "✅":
            log.info(f"{status} {desc}: Mark=${mark:.2f}, P&L={pnl:+.1f}%")
        else:
            log.info(f"{status} {desc}: {pnl if isinstance(pnl, str) else 'No data'}")
    
    success_count = sum(1 for _, status, _, _ in results if status == "✅")
    log.info(f"\nTotal: {success_count}/{len(results)} positions priced successfully")
    log.info("=" * 80)

if __name__ == "__main__":
    test_mark_prices()
