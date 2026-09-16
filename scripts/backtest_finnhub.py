"""
backtest_finnhub.py
-------------------
Compare 2 weeks of historical OHLCV data across Alpaca, Finnhub, and yfinance.
This is useful when Alpaca pricing looks inaccurate and you want an independent
free-API cross-check over the same date range.

Usage:
    python scripts/backtest_finnhub.py --symbols AAPL TSLA --days 14 --interval 15m --finnhub
"""

import sys
import argparse
import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import pandas as pd
import pytz
import yfinance as yf

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    import finnhub
    FINNHUB_SDK_AVAILABLE = True
except ImportError:
    FINNHUB_SDK_AVAILABLE = False

try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False

from engine.config import API_KEY, API_SECRET, FINNHUB_API_KEY

FINNHUB_RES_MAP = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "60m": "60",
    "1h": "60",
    "1d": "D",
}

ET = pytz.timezone("America/New_York")


def parse_interval(interval: str) -> str:
    interval = interval.strip().lower()
    if interval in FINNHUB_RES_MAP:
        return interval
    raise ValueError(f"Unsupported interval: {interval}")


def utc_timestamp(dt: datetime.datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.astimezone(datetime.timezone.utc).timestamp())


def normalize_times(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if "time" not in df.columns and "timestamp" in df.columns:
        df = df.rename(columns={"timestamp": "time"})
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True) if df["time"].dtype == "int64" or df["time"].dtype == "int32" else pd.to_datetime(df["time"])
        if df["time"].dt.tz is None:
            df["time"] = df["time"].dt.tz_localize("UTC")
        else:
            df["time"] = df["time"].dt.tz_convert("UTC")
    return df


def fetch_finnhub_bars(symbol: str, start: datetime.datetime, end: datetime.datetime, interval: str) -> pd.DataFrame:
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY not configured")

    resolution = FINNHUB_RES_MAP[interval]
    start_ts = utc_timestamp(start)
    end_ts = utc_timestamp(end)

    if FINNHUB_SDK_AVAILABLE:
        client = finnhub.Client(api_key=FINNHUB_API_KEY)
        data = client.stock_candles(symbol, resolution, start_ts, end_ts)
        if data.get("s") != "ok":
            raise RuntimeError(f"Finnhub error for {symbol}: {data.get('s')} {data.get('error', '')}")
    else:
        if not REQUESTS_AVAILABLE:
            raise RuntimeError("requests is not installed")
        import requests
        url = (
            f"https://finnhub.io/api/v1/stock/candle?symbol={symbol}&resolution={resolution}"
            f"&from={start_ts}&to={end_ts}&token={FINNHUB_API_KEY}"
        )
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()
        if data.get("s") != "ok":
            raise RuntimeError(f"Finnhub error for {symbol}: {data.get('s')} {data.get('error', '')}")

    df = pd.DataFrame({
        "time": data.get("t", []),
        "open": data.get("o", []),
        "high": data.get("h", []),
        "low": data.get("l", []),
        "close": data.get("c", []),
        "volume": data.get("v", []),
    })
    return normalize_times(df)


def alpaca_client() -> StockHistoricalDataClient:
    if not ALPACA_AVAILABLE:
        raise RuntimeError("alpaca-py is not installed")
    if not API_KEY or not API_SECRET:
        raise RuntimeError("Alpaca credentials not configured")
    return StockHistoricalDataClient(API_KEY, API_SECRET)


def fetch_alpaca_bars(symbol: str, start: datetime.datetime, end: datetime.datetime, interval: str) -> pd.DataFrame:
    client = alpaca_client()
    tf = interval
    if interval.endswith("m"):
        timeframe = TimeFrame(int(interval[:-1]), TimeFrameUnit.Minute)
    elif interval.endswith("h"):
        timeframe = TimeFrame(int(interval[:-1]), TimeFrameUnit.Hour)
    elif interval.endswith("d"):
        timeframe = TimeFrame(int(interval[:-1]), TimeFrameUnit.Day)
    else:
        raise ValueError(f"Unsupported interval: {interval}")

    bars = client.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbol, timeframe=timeframe, start=start, end=end))
    if symbol not in bars:
        return pd.DataFrame()
    df = bars[symbol].df.reset_index()
    df.columns = [c.lower() for c in df.columns]
    if "timestamp" in df.columns:
        df = df.rename(columns={"timestamp": "time"})
    return normalize_times(df)


def fetch_yfinance_bars(symbol: str, start: datetime.datetime, end: datetime.datetime, interval: str) -> pd.DataFrame:
    ticker = yf.Ticker(symbol)
    data = ticker.history(start=start, end=end, interval=interval, prepost=False)
    if data is None or data.empty:
        return pd.DataFrame()
    df = data.reset_index()
    df.columns = [c.lower() for c in df.columns]
    if "datetime" in df.columns:
        df = df.rename(columns={"datetime": "time"})
    return normalize_times(df)


def summarize_df(name: str, df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return f"{name}: no data"
    start = df["time"].iloc[0]
    end = df["time"].iloc[-1]
    first = float(df["close"].iloc[0])
    last = float(df["close"].iloc[-1])
    returns = (last / first - 1) * 100 if first else 0.0
    return f"{name}: rows={len(df)}, start={start.isoformat()}, end={end.isoformat()}, close={last:.4f}, return={returns:+.2f}%"


def compare_series(name_a: str, df_a: pd.DataFrame, name_b: str, df_b: pd.DataFrame) -> None:
    if df_a is None or df_a.empty or df_b is None or df_b.empty:
        print(f"Cannot compare {name_a} and {name_b}: missing data")
        return
    merged = pd.merge(df_a, df_b, on="time", suffixes=(f"_{name_a}", f"_{name_b}"))
    if merged.empty:
        print(f"No overlapping timestamp rows between {name_a} and {name_b}")
        return

    merged["close_diff"] = merged[f"close_{name_a}"] - merged[f"close_{name_b}"]
    merged["close_pct_diff"] = merged["close_diff"] / merged[f"close_{name_b}"]
    merged["abs_close_diff"] = merged["close_diff"].abs()
    merged["abs_pct_diff"] = merged["close_pct_diff"].abs() * 100

    print(f"{name_a} vs {name_b}: overlap_rows={len(merged)}, mean_abs_diff={merged['abs_close_diff'].mean():.4f}, max_abs_diff={merged['abs_close_diff'].max():.4f}, mean_abs_pct_diff={merged['abs_pct_diff'].mean():.3f}%")
    top = merged.sort_values("abs_pct_diff", ascending=False).head(5)
    print("Top mismatches:")
    for _, row in top.iterrows():
        print(
            f"  {row['time'].isoformat()} {name_a}={row[f'close_{name_a}']:.4f} {name_b}={row[f'close_{name_b}']:.4f} "
            f"diff={row['close_diff']:.4f} pct={row['close_pct_diff'] * 100:+.3f}%"
        )


def format_return(name: str, df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return f"{name}: no data"
    first = float(df["close"].iloc[0])
    last = float(df["close"].iloc[-1])
    return f"{name} return: {(last / first - 1) * 100:+.2f}%"


def parse_date(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value).replace(tzinfo=datetime.timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest price data against Finnhub for 2 weeks")
    parser.add_argument("--symbols", nargs="+", default=["AAPL"], help="Symbols to compare")
    parser.add_argument("--days", type=int, default=14, help="Number of calendar days to backtest")
    parser.add_argument("--interval", default="15m", help="Bar interval, e.g. 1m, 5m, 15m, 60m, 1d")
    parser.add_argument("--start", help="Override start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="Override end date (YYYY-MM-DD)")
    parser.add_argument("--no-alpaca", action="store_true", help="Skip Alpaca fetch")
    parser.add_argument("--no-yfinance", action="store_true", help="Skip yfinance fetch")
    parser.add_argument("--no-finnhub", action="store_true", help="Skip Finnhub fetch")
    args = parser.parse_args()

    interval = parse_interval(args.interval)
    if args.start:
        start = parse_date(args.start)
    else:
        start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)
    if args.end:
        end = parse_date(args.end)
    else:
        end = datetime.datetime.now(datetime.timezone.utc)

    for symbol in args.symbols:
        symbol = symbol.strip().upper().lstrip("$")
        print("\n" + "=" * 80)
        print(f"Symbol: {symbol} | Range: {start.date()} → {end.date()} | Interval: {interval}")

        alpaca_df = None
        yf_df = None
        finnhub_df = None

        if not args.no_alpaca:
            try:
                alpaca_df = fetch_alpaca_bars(symbol, start, end, interval)
                print(summarize_df("Alpaca", alpaca_df))
            except Exception as e:
                print(f"Alpaca error: {e}")

        if not args.no_yfinance:
            try:
                yf_df = fetch_yfinance_bars(symbol, start, end, interval)
                print(summarize_df("yfinance", yf_df))
            except Exception as e:
                print(f"yfinance error: {e}")

        if not args.no_finnhub:
            try:
                finnhub_df = fetch_finnhub_bars(symbol, start, end, interval)
                print(summarize_df("Finnhub", finnhub_df))
            except Exception as e:
                print(f"Finnhub error: {e}")

        if alpaca_df is not None and not alpaca_df.empty and finnhub_df is not None and not finnhub_df.empty:
            compare_series("alpaca", alpaca_df, "finnhub", finnhub_df)
        if yf_df is not None and not yf_df.empty and finnhub_df is not None and not finnhub_df.empty:
            compare_series("yfinance", yf_df, "finnhub", finnhub_df)
        if alpaca_df is not None and not alpaca_df.empty and yf_df is not None and not yf_df.empty:
            compare_series("alpaca", alpaca_df, "yfinance", yf_df)

        print(format_return("Alpaca", alpaca_df))
        print(format_return("yfinance", yf_df))
        print(format_return("Finnhub", finnhub_df))

    return 0


if __name__ == "__main__":
    sys.exit(main())
