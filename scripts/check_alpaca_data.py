"""
check_alpaca_data.py
-------------------
Compare Alpaca OHLCV bars against yfinance and optional Finnhub quote data.
Use this when Alpaca intraday data looks stale or inaccurate.

Usage:
    python scripts/check_alpaca_data.py --symbol AAPL --period 1d --interval 1m
    python scripts/check_alpaca_data.py --symbols AAPL MSFT TSLA --yfinance --finnhub
"""

import sys
import os
import argparse
import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import yfinance as yf
import pandas as pd
import pytz

try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

from engine.config import API_KEY, API_SECRET, FINNHUB_API_KEY


def parse_timeframe(interval: str):
    interval = interval.strip().lower()
    if interval.endswith("m"):
        return TimeFrame(int(interval[:-1]), TimeFrameUnit.Minute)
    if interval.endswith("h"):
        return TimeFrame(int(interval[:-1]), TimeFrameUnit.Hour)
    if interval.endswith("d"):
        return TimeFrame(int(interval[:-1]), TimeFrameUnit.Day)
    raise ValueError(f"Unsupported interval: {interval}")


def parse_period_days(period: str) -> int:
    if period.endswith("d"):
        return int(period[:-1])
    raise ValueError(f"Unsupported period: {period}")


def alpaca_client() -> StockHistoricalDataClient:
    if not ALPACA_AVAILABLE:
        raise RuntimeError("alpaca-py is not installed")
    if not API_KEY or not API_SECRET:
        raise RuntimeError("Alpaca credentials not configured")
    return StockHistoricalDataClient(API_KEY, API_SECRET)


def fetch_alpaca_bars(symbol: str, period: str, interval: str):
    client = alpaca_client()
    ET = pytz.timezone("America/New_York")
    now_et = datetime.datetime.now(ET)
    days = parse_period_days(period)
    start = now_et - datetime.timedelta(days=days)
    timeframe = parse_timeframe(interval)

    bars = client.get_stock_bars(
        StockBarsRequest(symbol_or_symbols=symbol, timeframe=timeframe, start=start)
    )
    if symbol not in bars:
        return None
    data = bars[symbol].df.reset_index()
    data.columns = [c.lower() for c in data.columns]
    if "timestamp" in data.columns:
        data = data.rename(columns={"timestamp": "time"})
    return data


def fetch_yfinance_bars(symbol: str, period: str, interval: str):
    ticker = yf.Ticker(symbol)
    data = ticker.history(period=period, interval=interval)
    if data is None or data.empty:
        return None
    data = data.reset_index()
    data.columns = [c.lower() for c in data.columns]
    if "datetime" in data.columns:
        data = data.rename(columns={"datetime": "time"})
    return data


def fetch_finnhub_quote(symbol: str):
    if not REQUESTS_AVAILABLE:
        raise RuntimeError("requests is not installed")
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY not configured")
    url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()


def summarize_bars(name: str, data, interval: str):
    if data is None or data.empty:
        return f"{name}: no data"
    first_ts = data["time"].iloc[0]
    last_ts = data["time"].iloc[-1]
    last_close = float(data["close"].iloc[-1])
    count = len(data)
    if hasattr(first_ts, "tzinfo") and first_ts.tzinfo is not None:
        first_ts = first_ts.isoformat()
        last_ts = last_ts.isoformat()
    return f"{name}: rows={count}, first={first_ts}, last={last_ts}, close={last_close:.4f}"


def compare_symbol(symbol: str, period: str, interval: str, use_yf: bool, use_finnhub: bool):
    symbol = symbol.strip().upper().lstrip("$")
    print(f"\n=== {symbol} ({period} / {interval}) ===")

    alpaca_data = None
    yf_data = None
    finnhub_data = None

    if ALPACA_AVAILABLE and API_KEY and API_SECRET:
        try:
            alpaca_data = fetch_alpaca_bars(symbol, period, interval)
            print(summarize_bars("Alpaca", alpaca_data, interval))
        except Exception as e:
            print(f"Alpaca error: {e}")
    else:
        print("Alpaca: skipped (missing package or credentials)")

    if use_yf:
        try:
            yf_data = fetch_yfinance_bars(symbol, period, interval)
            print(summarize_bars("yfinance", yf_data, interval))
        except Exception as e:
            print(f"yfinance error: {e}")

    if use_finnhub:
        try:
            finnhub_data = fetch_finnhub_quote(symbol)
            current = finnhub_data.get("c")
            print(f"Finnhub quote: current={current}, open={finnhub_data.get('o')}, high={finnhub_data.get('h')}, low={finnhub_data.get('l')}")
        except Exception as e:
            print(f"Finnhub error: {e}")

    if alpaca_data is not None and yf_data is not None and not alpaca_data.empty and not yf_data.empty:
        alpaca_close = float(alpaca_data["close"].iloc[-1])
        yf_close = float(yf_data["close"].iloc[-1])
        diff = alpaca_close - yf_close
        pct = diff / yf_close * 100 if yf_close else 0.0
        print(f"Close diff: Alpaca - yfinance = {diff:.4f} ({pct:+.2f}%)")

        # compare recent bars if available
        if len(alpaca_data) >= 1 and len(yf_data) >= 1:
            alpaca_last_time = pd.to_datetime(alpaca_data["time"].iloc[-1])
            yf_last_time = pd.to_datetime(yf_data["time"].iloc[-1])
            if alpaca_last_time.tzinfo is None:
                alpaca_last_time = alpaca_last_time.tz_localize("UTC").tz_convert("US/Eastern")
            if yf_last_time.tzinfo is None:
                yf_last_time = yf_last_time.tz_localize("UTC").tz_convert("US/Eastern")
            print(f"Last timestamp diff: Alpaca={alpaca_last_time.isoformat()}, yfinance={yf_last_time.isoformat()}")

    if alpaca_data is None and yf_data is not None:
        print("Using yfinance as independent cross-check source.")


def main():
    parser = argparse.ArgumentParser(description="Compare Alpaca OHLCV data against free public sources")
    parser.add_argument("--symbols", nargs="+", default=["AAPL"], help="Symbols to compare")
    parser.add_argument("--period", default="1d", help="Bar period (e.g. 1d, 5d")
    parser.add_argument("--interval", default="1m", help="Bar interval (e.g. 1m, 5m, 1d)")
    parser.add_argument("--yfinance", action="store_true", help="Fetch yfinance data for cross-check")
    parser.add_argument("--finnhub", action="store_true", help="Fetch Finnhub quote if FINNHUB_API_KEY is configured")
    args = parser.parse_args()

    if not args.yfinance and not args.finnhub:
        args.yfinance = True

    for symbol in args.symbols:
        compare_symbol(symbol, args.period, args.interval, args.yfinance, args.finnhub)

    return 0


if __name__ == "__main__":
    sys.exit(main())
