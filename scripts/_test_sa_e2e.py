"""End-to-end Seeking Alpha integration test."""
import sys
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()

from engine.data.seeking_alpha import (
    get_sa_market_outlook, get_sa_day_watch,
    get_sa_leading_story, get_sa_metrics_grades,
)

print("=== 1. SA RAW ENDPOINTS ===")
outlook = get_sa_market_outlook()
print(f"market_outlook  : {outlook.get('sentiment')} | bull={outlook.get('bullish_pct', 0):.0%} bear={outlook.get('bearish_pct', 0):.0%}")
print(f"  titles        : {outlook.get('titles', [])[:2]}")

dw = get_sa_day_watch()
print(f"day_watch       : top_gainers={dw.get('top_gainers', [])[:4]}")
print(f"                  sp500_gainers={dw.get('sp500_gainers', [])[:4]}")
print(f"                  most_active={dw.get('most_active', [])[:4]}")

ls = get_sa_leading_story()
print(f"leading_story   : {ls[:6]}")

grades = get_sa_metrics_grades("NVDA")
print(f"metrics_grades  : {grades}")

print()
print("=== 2. UTILS WRAPPERS ===")
from engine.utils import get_sa_market_movers, get_sa_factor_grades, get_sa_market_outlook as util_outlook
movers = get_sa_market_movers()
print(f"get_sa_market_movers keys : {list(movers.keys())}")
print(f"get_sa_factor_grades(NVDA): {get_sa_factor_grades('NVDA')}")
print(f"get_sa_market_outlook     : {util_outlook().get('sentiment')}")

print()
print("=== 3. METRICS BOOST (equity strategies) ===")
from engine.equity.strategies import _sa_metrics_boost
for sym, base in [("NVDA", 0.80), ("SPY", 0.75), ("TSLA", 0.70)]:
    boosted = _sa_metrics_boost(sym, base)
    delta = boosted - base
    print(f"  {sym}: {base:.2f} -> {boosted:.3f}  (delta={delta:+.3f})")

print()
print("=== 4. ORCHESTRATOR SENTIMENT BLEND ===")
from engine.utils.market import MarketState
ms = MarketState.from_now()
ms.resolve_regime()
local_sent = ms.resolve_sentiment()
print(f"Local sentiment : {local_sent}")
bull_pct = outlook.get("bullish_pct", 0.5)
if bull_pct >= 0.65:
    final = "bullish (SA override)"
elif bull_pct <= 0.35:
    final = "bearish (SA override)"
else:
    final = f"{local_sent} (SA neutral — no override)"
print(f"SA bull_pct     : {bull_pct:.0%}")
print(f"Final sentiment : {final}")

print()
print("=== 5. SCAN TARGETS (SA injection) ===")
from engine.equity.scan import get_scan_targets
targets = get_scan_targets(set())
sa_candidates = dw.get("top_gainers", [])[:8] + dw.get("sp500_gainers", [])[:5] + ls[:3]
sa_in_targets = [t for t in sa_candidates if t in targets]
print(f"Total targets       : {len(targets)}")
print(f"SA tickers in scan  : {sa_in_targets[:10]}")

print()
print("=== 6. PREOPEN PROVIDERS ===")
from engine.equity.discovery import PreopenIntelligenceScanner
scanner = PreopenIntelligenceScanner()
names = [p.name for p in scanner.providers]
print(f"Registered providers   : {names}")
print(f"sa_day_watch present   : {'sa_day_watch' in names}")

print()
print("ALL CHECKS COMPLETE")
