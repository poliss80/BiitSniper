"""Short squeeze screener — runs on live TI HSF + UOV tickers."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from engine.config import FINNHUB_API_KEY
import json
import yfinance as yf
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Load fresh TI tickers ────────────────────────────────────────
hsf = json.load(open(ROOT / "data" / "ti_primary.json"))
uov = json.load(open(ROOT / "data" / "ti_unusual_options.json"))

TI_TICKERS = sorted(set(hsf["tickers"]) | set(uov["tickers"]))
print(f"TI HSF ({len(hsf['tickers'])}) + UOV ({len(uov['tickers'])}) = {len(TI_TICKERS)} unique tickers")
print(f"  HSF updated: {hsf['updated']}  |  UOV updated: {uov['updated']}")

# ── Fetch yfinance fundamentals in parallel ──────────────────────
def fetch_yf(sym):
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

results = []
with ThreadPoolExecutor(max_workers=20) as pool:
    futs = {pool.submit(fetch_yf, s): s for s in TI_TICKERS}
    for f in as_completed(futs):
        r = f.result()
        if r:
            results.append(r)

# ── Gate 1: liquid enough for real options ───────────────────────
liquid = [r for r in results if r["price"] > 5 and r["flt"] > 5_000_000 and r["mktcap"] > 100_000_000]

# ── Gate 2: squeeze fundamentals ────────────────────────────────
# Relaxed gm/rg for UOV tickers (options flow IS the signal)
uov_set = set(uov["tickers"])

def passes_gates(r):
    if r["sym"] in uov_set:
        return r["spf"] >= 0.08 and r["gm"] > 0   # UOV: lower bar, flow IS signal
    return r["spf"] >= 0.12 and r["gm"] > 0 and r["rg"] > 0.08

candidates = [r for r in liquid if passes_gates(r)]

# ── Fetch Finnhub RS + 5d return in parallel ─────────────────────
def fetch_rs(sym):
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/stock/metric",
            params={"symbol": sym, "metric": "all", "token": FINNHUB_API_KEY},
            timeout=5,
        )
        m = r.json().get("metric", {})
        return sym, m.get("priceRelativeToS&P50013Week"), m.get("5DayPriceReturnDaily"), m.get("52WeekPriceReturnDaily")
    except Exception:
        return sym, None, None, None

rs_map, ret5d_map, ret52w_map = {}, {}, {}
with ThreadPoolExecutor(max_workers=12) as pool:
    for sym, rs, r5d, r52w in pool.map(fetch_rs, [c["sym"] for c in candidates]):
        rs_map[sym] = rs
        ret5d_map[sym] = r5d
        ret52w_map[sym] = r52w

for c in candidates:
    c["rs13w"] = rs_map.get(c["sym"])
    c["ret5d"] = ret5d_map.get(c["sym"])
    c["ret52w"] = ret52w_map.get(c["sym"])
    c["in_uov"] = c["sym"] in uov_set

# ── Score & rank ─────────────────────────────────────────────────
def score(c):
    s = c["spf"] * 40
    s += min(c["rg"], 2.0) * 10
    if c["rs13w"] is not None:
        s += min(c["rs13w"], 200) * 0.05
    if c["in_uov"]:
        s += 5   # bonus for unusual options flow confirmation
    return s

candidates.sort(key=score, reverse=True)

# ── Print results ─────────────────────────────────────────────────
print()
print(f"Liquid (price>$5, float>5M, mktcap>$100M): {len(liquid)}")
print(f"Squeeze candidates: {len(candidates)}  (HSF>=12%+gm>0+revg>8%  OR  UOV>=8%+gm>0)")
print()
hdr = f"{'Sym':<7} {'Price':>7} {'Short%':>7} {'DTC':>5} {'GrossM':>7} {'RevGrow':>8} {'RS13W':>7} {'5d%':>6} {'52W%':>6} {'UOV':>4}"
print(hdr)
print("-" * len(hdr))
for c in candidates[:35]:
    rs   = f"{c['rs13w']:+.0f}%" if c["rs13w"] is not None else "  N/A"
    r5   = f"{c['ret5d']:+.1f}%" if c["ret5d"] is not None else "  N/A"
    r52  = f"{c['ret52w']:+.0f}%" if c["ret52w"] is not None else "  N/A"
    uov_flag = " *" if c["in_uov"] else ""
    print(f"{c['sym']:<7} {c['price']:>7.2f} {c['spf']:>7.1%} {c['sr']:>5.1f} {c['gm']:>7.1%} {c['rg']:>8.1%} {rs:>7} {r5:>6} {r52:>6}{uov_flag}")
