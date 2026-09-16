from alpaca.data.requests import OptionSnapshotRequest
from alpaca.data.historical import OptionHistoricalDataClient
import os

# Use environment variables for Alpaca API keys
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_API_SECRET = os.getenv("ALPACA_API_SECRET")

if not ALPACA_API_KEY or not ALPACA_API_SECRET:
    raise ValueError("Set ALPACA_API_KEY and ALPACA_API_SECRET in your environment.")

client = OptionHistoricalDataClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_API_SECRET)

# OCC symbol for AAOI 2026-05-01 147C
legs = [
    "AAOI260501C00147000",  # 147C
]

snapshots = client.get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=legs))

def get_mid(snap):
    if snap and snap.latest_quote:
        bid = float(snap.latest_quote.bid_price)
        ask = float(snap.latest_quote.ask_price)
        return (bid + ask) / 2
    return 0.0

mid_147c = get_mid(snapshots.get(legs[0]))

print(f"AAOI 2026-05-01 147C: Mid price = ${mid_147c:.2f}")
if snapshots.get(legs[0]) and snapshots.get(legs[0]).latest_quote:
    print(f"Bid = ${snapshots.get(legs[0]).latest_quote.bid_price}, Ask = ${snapshots.get(legs[0]).latest_quote.ask_price}")
else:
    print("No quote available for AAOI 2026-05-01 147C.")
