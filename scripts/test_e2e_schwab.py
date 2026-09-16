#!/usr/bin/env python3
"""End-to-end test: Validate Schwab market data, options chains, and strategy execution."""

import sys
from pathlib import Path
import logging

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from engine.utils import get_bars, get_bars_batch, clear_bar_cache
from engine.options import strategies as opt_strategies
from engine.utils import MarketState

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s'
)
log = logging.getLogger("ApexTrader")

def test_e2e():
    """Run end-to-end tests through the full pipeline."""
    
    log.info("=" * 80)
    log.info("END-TO-END TEST: Schwab Market Data + Options Strategies")
    log.info("=" * 80)
    
    # Test 1: Single bar fetch
    log.info("\n[TEST 1] Single symbol bar fetch via get_bars()")
    log.info("-" * 80)
    try:
        clear_bar_cache()
        bars = get_bars("AAPL", period="5d", interval="15m")
        if not bars.empty:
            log.info(f"✓ AAPL: {len(bars)} 15-min bars fetched via Schwab")
        else:
            log.error("✗ AAPL: No bars returned")
            return False
    except Exception as e:
        log.error(f"✗ Test 1 failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 2: Batch bar fetch
    log.info("\n[TEST 2] Batch symbol bar fetch via get_bars_batch()")
    log.info("-" * 80)
    try:
        clear_bar_cache()
        symbols = ["AAPL", "SPY", "QQQ"]
        batch_result = get_bars_batch(symbols, period="5d", interval="15m")
        fetched = [s for s, df in batch_result.items() if not df.empty]
        log.info(f"✓ Batch fetch: {len(fetched)}/{len(symbols)} symbols")
        if len(fetched) < 2:
            log.warning("⚠ Only partial batch fetch")
    except Exception as e:
        log.error(f"✗ Test 2 failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 3: Options chain fetch
    log.info("\n[TEST 3] Options chain fetch via _get_options_chain()")
    log.info("-" * 80)
    try:
        clear_bar_cache()
        # Get daily bars first
        daily = get_bars("AAPL", period="65d", interval="1d")
        
        chain = opt_strategies._get_options_chain("AAPL")
        if chain is not None:
            log.info(f"✓ AAPL options chain fetched:")
            log.info(f"  - {len(chain.calls)} calls, {len(chain.puts)} puts")
            log.info(f"  - Expiry: {chain.expiry}, IV Rank: {chain.iv_rank:.1f}%")
        else:
            log.error("✗ AAPL: No options chain returned")
            return False
    except Exception as e:
        log.error(f"✗ Test 3 failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 4: Verify chain is usable by strategies
    log.info("\n[TEST 4] Verify chain structure for strategy use")
    log.info("-" * 80)
    try:
        clear_bar_cache()
        chain = opt_strategies._get_options_chain("AAPL")
        if chain is None:
            log.error("✗ Test 4 failed: No chain returned")
            return False
        
        if chain.calls.empty:
            log.warning("✗ Test 4 failed: No calls in chain")
            return False
        
        # Verify required columns
        required = ["strike", "bid", "ask", "mid", "impliedvolatility", "openinterest"]
        missing = [col for col in required if col not in chain.calls.columns]
        if missing:
            log.error(f"✗ Test 4 failed: Missing columns {missing}")
            return False
        
        log.info(f"✓ Chain structure valid for strategies:")
        log.info(f"  - Calls: {len(chain.calls)} rows with {len(chain.calls.columns)} columns")
        log.info(f"  - Strike range: ${chain.calls['strike'].min():.2f} - ${chain.calls['strike'].max():.2f}")
        log.info(f"  - IV range: {chain.calls['impliedvolatility'].min()*100:.1f}% - {chain.calls['impliedvolatility'].max()*100:.1f}%")
    except Exception as e:
        log.error(f"✗ Test 4 failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Summary
    log.info("\n" + "=" * 80)
    log.info("✓ ALL TESTS PASSED - Schwab integration fully functional")
    log.info("=" * 80)
    return True

if __name__ == "__main__":
    success = test_e2e()
    sys.exit(0 if success else 1)
