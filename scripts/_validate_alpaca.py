"""Validate Alpaca live API: account, quotes, intraday bars."""
import sys, os, datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from engine.config import API_KEY, API_SECRET, TRADE_MODE, PAPER, LIVE
print(f"TRADE_MODE : {TRADE_MODE}")
print(f"PAPER={PAPER}  LIVE={LIVE}")
print(f"API_KEY    : {'SET (' + API_KEY[:6] + '...)' if API_KEY else 'EMPTY'}")
print(f"API_SECRET : {'SET' if API_SECRET else 'EMPTY'}")
print()

# ── 1. Trading account ────────────────────────────────────────────────────────
try:
    from alpaca.trading.client import TradingClient
    tc = TradingClient(API_KEY, API_SECRET, paper=PAPER)
    acct = tc.get_account()
    print(f"[OK] Trading API ({TRADE_MODE.upper()})")
    print(f"     Equity:       ${float(acct.equity):,.2f}")
    print(f"     Buying Power: ${float(acct.buying_power):,.2f}")
    print(f"     PDT flag:     {acct.pattern_day_trader}")
    print(f"     Status:       {acct.status}")
except Exception as exc:
    print(f"[FAIL] Trading API: {exc}")

print()

# ── 2. Latest quotes ──────────────────────────────────────────────────────────
try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestQuoteRequest, StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    import pytz

    dc = StockHistoricalDataClient(API_KEY, API_SECRET)
    req = StockLatestQuoteRequest(symbol_or_symbols=["AAPL", "SPY", "NVDA"])
    quotes = dc.get_stock_latest_quote(req)
    for sym, q in quotes.items():
        print(f"[OK] Quote {sym}: bid={q.bid_price}  ask={q.ask_price}")
except Exception as exc:
    print(f"[FAIL] Latest quotes: {exc}")

print()

# ── 3. Intraday bars (last 30 min) ────────────────────────────────────────────
try:
    tz = pytz.timezone("America/New_York")
    now = datetime.datetime.now(tz)
    start = now - datetime.timedelta(minutes=30)
    req2 = StockBarsRequest(
        symbol_or_symbols=["AAPL"],
        timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=start,
        end=now,
    )
    bars = dc.get_stock_bars(req2)
    df = bars.df
    print(f"[OK] Intraday 1m bars AAPL (last 30m): {len(df)} rows")
    if len(df) > 0:
        print(f"     Latest: {df.index[-1]}  close={df['close'].iloc[-1]:.2f}  volume={int(df['volume'].iloc[-1])}")
    else:
        print("     WARNING: 0 bars returned — possible data subscription gap")
except Exception as exc:
    print(f"[FAIL] Intraday bars: {exc}")


# ── 4. IEX feed direct test ───────────────────────────────────────────────────
print("--- Feed comparison (SIP vs IEX) ---")
import datetime as dt2
tz2 = pytz.timezone("America/New_York")
now2 = dt2.datetime.now(tz2)
start2 = now2 - dt2.timedelta(days=1)

for feed in ["sip", "iex"]:
    try:
        req3 = StockBarsRequest(
            symbol_or_symbols=["AAPL"],
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=start2,
            end=now2,
            feed=feed,
        )
        bars3 = dc.get_stock_bars(req3)
        df3 = bars3.df
        nrows = len(df3)
        if nrows > 0:
            last_ts = df3.index[-1]
            last_close = df3["close"].iloc[-1]
            print(f"[OK ] feed={feed}: {nrows} rows, last={last_ts}, close={last_close:.2f}")
        else:
            print(f"[WARN] feed={feed}: 0 rows returned")
    except Exception as exc:
        print(f"[FAIL] feed={feed}: {exc}")

print()
print("Validation complete.")
