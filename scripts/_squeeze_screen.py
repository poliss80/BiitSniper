"""One-shot short squeeze screener — runs on HIGH_SHORT_FLOAT_STOCKS."""
import sys, os
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from engine.config import HIGH_SHORT_FLOAT_STOCKS, FINNHUB_API_KEY
import yfinance as yf
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

HSF = sorted(HIGH_SHORT_FLOAT_STOCKS)
print(f"Total HSF tickers: {len(HSF)}")

results = []

def fetch_one(sym):
    try:
        info   = yf.Ticker(sym).info
        price  = info.get("regularMarketPrice") or info.get("currentPrice") or 0
        spf    = info.get("shortPercentOfFloat") or 0
        sr     = info.get("shortRatio") or 0
        gm     = info.get("grossMargins") or 0
        rg     = info.get("revenueGrowth") or 0
        flt    = info.get("floatShares") or 0
        mktcap = info.get("marketCap") or 0
        return dict(sym=sym, price=price, spf=spf, sr=sr, gm=gm, rg=rg, flt=flt, mktcap=mktcap)
    except Exception:
        return None

with ThreadPoolExecutor(max_workers=20) as pool:
    futs = {pool.submit(fetch_one, s): s for s in HSF}
    for f in as_completed(futs):
        r = f.result()
        if r:
            results.append(r)

# Gate 1: liquid enough to have real options
liquid = [r for r in results if r["price"] > 5 and r["flt"] > 5_000_000 and r["mktcap"] > 100_000_000]

# Gate 2: squeeze fuel
candidates = [r for r in liquid if r["spf"] >= 0.12 and r["gm"] > 0 and r["rg"] > 0.08]

# Fetch RS vs SPY 13W from Finnhub for candidates
def fetch_rs(sym):
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/stock/metric",
            params={"symbol": sym, "metric": "all", "token": FINNHUB_API_KEY},
            timeout=5,
        )
        m = r.json().get("metric", {})
        return sym, m.get("priceRelativeToS&P50013Week"), m.get("5DayPriceReturnDaily")
    except Exception:
        return sym, None, None

rs_map = {}
ret5d_map = {}
with ThreadPoolExecutor(max_workers=10) as pool:
    for sym, rs, ret5d in pool.map(fetch_rs, [c["sym"] for c in candidates]):
        rs_map[sym] = rs
        ret5d_map[sym] = ret5d

for c in candidates:
    c["rs13w"] = rs_map.get(c["sym"])
    c["ret5d"] = ret5d_map.get(c["sym"])

# Score: short_float weight + revenue growth + RS
def score(c):
    s = c["spf"] * 40
    s += min(c["rg"], 2.0) * 10
    if c["rs13w"] is not None:
        s += min(c["rs13w"], 200) * 0.05
    return s

candidates.sort(key=score, reverse=True)

print(f"\nLiquid (price>$5, float>5M, mktcap>$100M): {len(liquid)}")
print(f"Squeeze candidates (spf>=12%, gm>0, revg>8%): {len(candidates)}")
print()
hdr = f"{'Sym':<7} {'Price':>7} {'Short%':>7} {'DTC':>5} {'GrossM':>7} {'RevGrow':>8} {'RS13W':>7} {'5d%':>6} {'Float':>9}"
print(hdr)
print("-" * len(hdr))
for c in candidates[:30]:
    rs  = f"{c['rs13w']:+.0f}%" if c["rs13w"] is not None else "  N/A"
    r5  = f"{c['ret5d']:+.1f}%" if c["ret5d"] is not None else "  N/A"
    print(f"{c['sym']:<7} {c['price']:>7.2f} {c['spf']:>7.1%} {c['sr']:>5.1f} {c['gm']:>7.1%} {c['rg']:>8.1%} {rs:>7} {r5:>6} {c['flt']/1e6:>7.1f}M")
