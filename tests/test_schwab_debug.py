#!/usr/bin/env python3
"""Debug Schwab credentials and market data client initialization."""

import os
import sys
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
    print(f"✅ Loaded .env file from {ENV_FILE}")
else:
    print(f"⚠️  .env file not found at {ENV_FILE}")

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("TestDebug")

print("\n" + "=" * 80)
print("STEP 1: Check Environment Variables")
print("=" * 80)
client_id = os.environ.get("SCHWAB_CLIENT_ID")
client_secret = os.environ.get("SCHWAB_CLIENT_SECRET")
print(f"SCHWAB_CLIENT_ID: {client_id[:10] + '...' if client_id else 'NOT SET'}")
print(f"SCHWAB_CLIENT_SECRET: {client_secret[:10] + '...' if client_secret else 'NOT SET'}")

print("\n" + "=" * 80)
print("STEP 2: Try importing schwab_client module")
print("=" * 80)
try:
    from engine.broker.schwab_client import get_schwab_market_data_client, get_schwab_oauth_client
    print("✅ Successfully imported schwab_client functions")
except Exception as e:
    print(f"❌ Failed to import: {e}")
    sys.exit(1)

print("\n" + "=" * 80)
print("STEP 3: Try getting OAuth client")
print("=" * 80)
try:
    oauth = get_schwab_oauth_client()
    print(f"✅ Successfully got OAuth client")
    print(f"   Access token: {oauth.access_token[:20] + '...' if oauth.access_token else 'NONE'}")
except Exception as e:
    print(f"❌ Failed to get OAuth client: {e}")
    sys.exit(1)

print("\n" + "=" * 80)
print("STEP 4: Try getting market data client")
print("=" * 80)
try:
    client = get_schwab_market_data_client()
    print(f"✅ Successfully got market data client")
except Exception as e:
    print(f"❌ Failed to get market data client: {e}")
    sys.exit(1)

print("\n" + "=" * 80)
print("STEP 5: Try getting option chain for SMH")
print("=" * 80)
try:
    chain = client.get_option_chains("SMH", contract_type="ALL")
    print(f"✅ Successfully fetched SMH option chain")
    print(f"   Response keys: {list(chain.keys())[:5]}...")
    print(f"   Symbol: {chain.get('symbol')}")
    print(f"   Underlying price: ${chain.get('underlyingPrice', 0):.2f}")
    
    # Check if we have both call and put data
    has_calls = "callExpDateMap" in chain and chain["callExpDateMap"]
    has_puts = "putExpDateMap" in chain and chain["putExpDateMap"]
    print(f"   Has calls: {has_calls}, Has puts: {has_puts}")
    
except Exception as e:
    print(f"❌ Failed to fetch chain: {e}")
    sys.exit(1)

print("\n" + "=" * 80)
print("STEP 6: Try calling get_spread_mark_from_schwab")
print("=" * 80)
try:
    from engine.utils.schwab_pricing import get_spread_mark_from_schwab
    
    legs = [
        {"occ_symbol": "SMH550C", "side": "buy", "ratio_qty": 1, "strike": 550.0},
        {"occ_symbol": "SMH555C", "side": "sell", "ratio_qty": 1, "strike": 555.0},
    ]
    
    mark, pnl = get_spread_mark_from_schwab("SMH", legs, 1.90)
    if mark is not None:
        print(f"✅ Successfully got SMH 550/555 spread mark")
        print(f"   Current mark: ${mark:.2f}")
        print(f"   P&L: {pnl:+.1f}%")
    else:
        print(f"⚠️  Mark pricing returned None")
        
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n" + "=" * 80)
print("ALL TESTS PASSED ✅")
print("=" * 80)
