#!/usr/bin/env python
"""Debug Schwab chain response structure."""

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
    level=logging.DEBUG,
    format='%(asctime)s | %(levelname)-8s | %(message)s'
)
log = logging.getLogger("ApexTrader")

from engine.broker.schwab_client import get_schwab_market_data_client

def main():
    client = get_schwab_market_data_client()
    
    log.info("Fetching AAPL options chain from Schwab...")
    response = client.get_option_chains("AAPL", contract_type="ALL")
    
    if response:
        log.info(f"Response keys: {list(response.keys())}")
        
        if "chains" in response:
            chains = response["chains"]
            log.info(f"Number of chains: {len(chains)}")
            
            if chains:
                first_chain = chains[0]
                log.info(f"First chain keys: {list(first_chain.keys())}")
                
                # Print first few fields
                for key in ["expirationDate", "daysToExpiration", "isIndex", "interestRate", "underlyingPrice", "volatility"]:
                    if key in first_chain:
                        log.info(f"  {key}: {first_chain[key]}")
                
                # Check for options field
                if "options" in first_chain:
                    opts = first_chain["options"]
                    log.info(f"First chain has {len(opts)} options")
                    
                    if opts:
                        log.info(f"First option keys: {list(opts[0].keys())}")
                        log.info(f"First option: {opts[0]}")
                else:
                    log.warning("First chain has no 'options' field")
                    log.info(f"First chain (full, limited): {json.dumps(first_chain, indent=2, default=str)[:1000]}")
        else:
            log.warning("Response has no 'chains' key")
            log.info(f"Full response (limited): {json.dumps(response, indent=2, default=str)[:1000]}")
    else:
        log.error("Failed to fetch response")

if __name__ == "__main__":
    main()
