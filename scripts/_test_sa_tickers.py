"""Scan SA-injected tickers through all equity strategies and report confidence."""
import sys
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()

from engine.equity.strategies import (
    SweepeaStrategy, TrendBreakerStrategy, SentimentStrategy,
    TechnicalStrategy, MomentumStrategy, GapBreakoutStrategy,
)
from engine.utils.market import MarketState

ms = MarketState.from_now()
ms.resolve_regime()
sentiment = ms.resolve_sentiment()
print(f"Regime: {ms.regime}  Sentiment: {sentiment}")
print()

strategies = [
    SweepeaStrategy(),
    TrendBreakerStrategy(),
    SentimentStrategy(),
    TechnicalStrategy(),
    MomentumStrategy(),
    GapBreakoutStrategy(),
]

tickers = ["ATOM", "CERS", "FIVN", "TEAM", "EGHT", "TWLO", "ACCO", "CBOE", "PSKY"]

header = f"{'TICKER':<6}  {'STRATEGY':<22}  {'ACTION':<5}  CONF"
print(header)
print("-" * 50)

results = {}
for sym in tickers:
    signals = []
    for strat in strategies:
        try:
            sig = strat.scan(sym)
            if sig:
                signals.append((type(strat).__name__, sig.action, sig.confidence))
        except Exception as e:
            pass
    results[sym] = signals

for sym, signals in results.items():
    if signals:
        # sort by confidence descending
        signals.sort(key=lambda x: x[2], reverse=True)
        for name, action, conf in signals:
            print(f"{sym:<6}  {name:<22}  {action:<5}  {conf:.0%}")
    else:
        print(f"{sym:<6}  (no signal)")
    print()

print("=" * 50)
print("TOP PICKS (highest confidence per ticker):")
print()
top = []
for sym, signals in results.items():
    if signals:
        best = max(signals, key=lambda x: x[2])
        top.append((sym, best[0], best[1], best[2]))

top.sort(key=lambda x: x[3], reverse=True)
for sym, strat, action, conf in top:
    print(f"  {sym:<6} {action:<5} {conf:.0%}  [{strat}]")
