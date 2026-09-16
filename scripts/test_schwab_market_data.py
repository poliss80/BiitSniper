#!/usr/bin/env python3
"""Test Schwab market data integration (candles + options chains)."""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv('.env')

from engine.utils import get_bars
from engine.options.strategies import _get_options_chain
import logging
import datetime

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ApexTrader")

def test_schwab_integration():
    """Test Schwab market data (candles + options chains)."""
    test_symbols = ["AAPL", "SPY", "QQQ"]
    
    log.info("=" * 60)
    log.info("TESTING SCHWAB MARKET DATA INTEGRATION")
    log.info("=" * 60)
    
    # Test 1: Candles via Schwab
    log.info("\n[TEST 1] SCHWAB CANDLES (5-day 15-min)")
    log.info("-" * 60)
    for symbol in test_symbols:
        try:
            bars = get_bars(symbol, period="5d", interval="15m")
            if not bars.empty:
                log.info(f"✓ {symbol}: {len(bars)} bars fetched")
            else:
                log.warning(f"✗ {symbol}: Empty bars")
        except Exception as e:
            log.error(f"✗ {symbol}: {e}")
    
    # Test 2: Daily bars for options chain (65d)
    log.info("\n[TEST 2] SCHWAB DAILY BARS (65d for options chain)")
    log.info("-" * 60)
    for symbol in test_symbols:
        try:
            bars = get_bars(symbol, period="65d", interval="1d")
            if not bars.empty:
                log.info(f"✓ {symbol}: {len(bars)} daily bars fetched")
            else:
                log.warning(f"✗ {symbol}: Empty daily bars")
        except Exception as e:
            log.error(f"✗ {symbol}: {e}")
    
    # Test 3: Options chains via Schwab
    log.info("\n[TEST 3] SCHWAB OPTIONS CHAINS")
    log.info("-" * 60)
    for symbol in test_symbols[:1]:  # Just test AAPL to avoid rate limiting
        try:
            chain = _get_options_chain(symbol)
            if chain is not None:
                log.info(f"✓ {symbol}:")
                log.info(f"  - Expiry: {chain.expiry}")
                log.info(f"  - Calls: {len(chain.calls)}")
                log.info(f"  - Puts: {len(chain.puts)}")
                log.info(f"  - IV Rank: {chain.iv_rank:.1f}%")
                log.info(f"  - HV-30: {chain.hv_30:.2f}")
                if not chain.calls.empty:
                    atm = chain.calls[(chain.calls["strike"] >= chain.spot_price * 0.95) & 
                                      (chain.calls["strike"] <= chain.spot_price * 1.05)]
                    if not atm.empty:
                        log.info(f"  - ATM IV: {atm['iv_pct'].mean():.1f}%")
            else:
                log.warning(f"✗ {symbol}: No chain returned")
        except Exception as e:
            log.error(f"✗ {symbol}: {e}")
            import traceback
            traceback.print_exc()
    
    log.info("\n" + "=" * 60)
    log.info("✓ All tests completed!")
    log.info("=" * 60)

if __name__ == "__main__":
    test_schwab_integration()
