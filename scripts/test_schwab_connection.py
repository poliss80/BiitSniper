#!/usr/bin/env python3
"""Test Schwab OAuth connection and market data API."""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv('.env')

from engine.broker.schwab_client import get_schwab_market_data_client
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ApexTrader")

def test_schwab_connection():
    """Test Schwab OAuth and market data endpoints."""
    try:
        log.info("Testing Schwab connection...")
        client = get_schwab_market_data_client()
        
        # Test 1: Get a quote
        log.info("\n[TEST 1] Fetching quote for AAPL...")
        quote = client.get_quote("AAPL")
        if quote:
            log.info(f"✓ Quote fetched: {quote}")
        else:
            log.warning("✗ Quote fetch failed")
        
        # Test 2: Get candles (5 days of 15-min bars)
        log.info("\n[TEST 2] Fetching 5-day 15-min candles for AAPL...")
        candles = client.get_candles("AAPL", period_type="day", period=5, 
                                     frequency_type="minute", frequency=15)
        if candles:
            log.info(f"✓ Candles fetched: {len(candles.get('candles', []))} bars")
        else:
            log.warning("✗ Candles fetch failed")
        
        # Test 3: Get options chains
        log.info("\n[TEST 3] Fetching options chains for AAPL...")
        chains = client.get_option_chains("AAPL", contract_type="CALL")
        if chains:
            log.info(f"✓ Options chains fetched")
        else:
            log.warning("✗ Options chains fetch failed")
        
        log.info("\n✓ All tests completed!")
        
    except Exception as e:
        log.error(f"✗ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    test_schwab_connection()
