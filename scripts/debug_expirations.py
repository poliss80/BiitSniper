#!/usr/bin/env python
"""Debug Schwab chain expirations."""

import logging
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load environment variables first
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s'
)
log = logging.getLogger("ApexTrader")

from engine.broker.schwab_client import get_schwab_market_data_client
from datetime import date, timedelta

def main():
    client = get_schwab_market_data_client()
    
    log.info("Fetching AAPL options chain from Schwab...")
    response = client.get_option_chains("AAPL", contract_type="ALL")
    
    if response:
        # Extract expirations
        call_map = response.get("callExpDateMap", {})
        put_map = response.get("putExpDateMap", {})
        
        log.info(f"\nAll call expirations ({len(call_map)}):")
        for i, exp_str in enumerate(call_map.keys()):
            date_part = exp_str.split(":")[0]
            print(f"  {i}: {exp_str}")
        
        log.info(f"\nAll put expirations ({len(put_map)}):")
        for i, exp_str in enumerate(put_map.keys()):
            date_part = exp_str.split(":")[0]
            print(f"  {i}: {exp_str}")
        
        # Show current date and DTE window
        today = date.today()
        window_start = today + timedelta(days=2)
        window_end = today + timedelta(days=35)
        
        log.info(f"\nToday: {today}")
        log.info(f"DTE window: {window_start} to {window_end}")
        
        # Parse first expiration
        if call_map:
            first_exp = list(call_map.keys())[0]
            log.info(f"\nFirst call expiration string: {first_exp}")
            date_part = first_exp.split(":")[0]
            log.info(f"Date part extracted: {date_part}")

if __name__ == "__main__":
    main()
