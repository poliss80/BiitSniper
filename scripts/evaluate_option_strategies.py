import os
from alpaca.data.requests import OptionSnapshotRequest
from alpaca.data.historical import OptionHistoricalDataClient
from datetime import datetime

# User: set your tickers and strikes/expiries here
TICKERS = [
    {"symbol": "AAOI", "expiry": "2026-05-01", "strikes": [147]},
    # Add more tickers/strikes as needed
]

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_API_SECRET = os.getenv("ALPACA_API_SECRET")

if not ALPACA_API_KEY or not ALPACA_API_SECRET:
    raise ValueError("Set ALPACA_API_KEY and ALPACA_API_SECRET in your environment.")

client = OptionHistoricalDataClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_API_SECRET)

for t in TICKERS:
    symbol = t["symbol"]
    expiry = datetime.strptime(t["expiry"], "%Y-%m-%d").strftime("%y%m%d")
    for strike in t["strikes"]:
        occ = f"{symbol}{expiry}C{int(strike*1000):08d}"
        print(f"\nEvaluating {symbol} {t['expiry']} {strike}C (OCC: {occ})")
        try:
            snap = client.get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=[occ])).get(occ)
            if snap and snap.latest_quote:
                bid = float(snap.latest_quote.bid_price)
                ask = float(snap.latest_quote.ask_price)
                mid = (bid + ask) / 2
                print(f"Bid: ${bid:.2f}, Ask: ${ask:.2f}, Mid: ${mid:.2f}")
                # Simple strategy suggestions
                print("Strategy ideas:")
                print(f"- Buy call: Max risk = premium paid (${mid:.2f}), unlimited upside above breakeven (${strike+mid:.2f})")
                print(f"- Bull call spread: Sell higher strike to reduce cost, cap max profit.")
                print(f"- Butterfly: Buy lower, sell 2x this, buy higher for low cost, max profit if pin at strike.")
            else:
                print("No quote available.")
        except Exception as e:
            print(f"Error fetching data: {e}")
