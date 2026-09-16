# Intraday Data Audit Script for ApexTrader
# Checks if each ticker in ti_primary.json has sufficient 1-min bars for the last 6 sessions

import json
import pandas as pd
from engine.utils.bars import get_bars

def audit_intraday_coverage(tickers, days=6, min_bars_per_day=350):
    missing = []
    for symbol in tickers:
        df = get_bars(symbol, f"{days}d", "1m")
        if df.empty:
            print(f"{symbol}: No data returned!")
            missing.append(symbol)
            continue
        # Group by date
        df['date'] = df['time'].dt.date
        daily_counts = df.groupby('date').size()
        for date, count in daily_counts.items():
            if count < min_bars_per_day:
                print(f"{symbol}: {date} only {count} bars (expected ~390)")
                missing.append(symbol)
    if not missing:
        print("All tickers have sufficient intraday data.")
    else:
        print(f"\nTickers with missing/insufficient data: {set(missing)}")

if __name__ == "__main__":
    with open("data/ti_primary.json") as f:
        data = json.load(f)
    tickers = data["tickers"]
    audit_intraday_coverage(tickers)
