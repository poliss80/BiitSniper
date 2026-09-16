from alpaca.data.requests import OptionSnapshotRequest
from alpaca.data.historical import OptionHistoricalDataClient
import os

# Use environment variables for Alpaca API keys
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_API_SECRET = os.getenv("ALPACA_API_SECRET")

if not ALPACA_API_KEY or not ALPACA_API_SECRET:
    raise ValueError("Set ALPACA_API_KEY and ALPACA_API_SECRET in your environment.")

client = OptionHistoricalDataClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_API_SECRET)

# OCC symbols for AEHR May 15, 2026 calls: 80C, 85C, 90C
legs = [
    "AEHR260515C00080000",  # 80C
    "AEHR260515C00085000",  # 85C
    "AEHR260515C00090000",  # 90C
]

snapshots = client.get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=legs))

def get_mid(snap):
    if snap and snap.latest_quote:
        bid = float(snap.latest_quote.bid_price)
        ask = float(snap.latest_quote.ask_price)
        return (bid + ask) / 2
    return 0.0

mid_80c = get_mid(snapshots.get(legs[0]))
mid_85c = get_mid(snapshots.get(legs[1]))
mid_90c = get_mid(snapshots.get(legs[2]))

butterfly_value = mid_80c - 2 * mid_85c + mid_90c
total_value = butterfly_value * 6 * 100  # 6 contracts, 100 shares each

print(f"AEHR 80/85/90C Butterfly (per spread): ${butterfly_value:.2f}")
print(f"Total value for 6 contracts: ${total_value:.2f}")
print(f"Legs: 80C=${mid_80c:.2f}, 85C=${mid_85c:.2f}, 90C=${mid_90c:.2f}")
