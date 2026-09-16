#!/usr/bin/env python3
"""Backtest options strategies on 25 fresh TI tickers using Schwab market data."""

import sys
import json
from pathlib import Path
from datetime import datetime, timedelta
import logging

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from engine.options.strategies import (
    _get_options_chain, momentum_call, bear_put_spread, bear_call_spread,
    iron_condor, butterfly_spread, mean_reversion, breakout_retest,
    trend_pullback_spread, short_squeeze, covered_call
)
from engine.utils import get_bars, MarketState
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s'
)
log = logging.getLogger("ApexTrader")

# Fresh TI tickers (first 25)
TI_TICKERS_25 = [
    "DGXX", "BLZE", "SLQT", "EMBC", "BRCC", "VRDN", "AVNW", "HNGE", "IPGP",
    "WYY", "CPSH", "LINC", "FATE", "EOLS", "BLDP", "DSX", "NVCT", "WGS",
    "BRBR", "FMS", "OSIS", "CIFG", "BLLN", "ECG", "AHCO"
]

def run_backtest():
    """Run options strategy backtest on 25 TI tickers."""
    
    log.info("=" * 80)
    log.info(f"BACKTEST: Options Strategies on 25 Fresh TI Tickers (Schwab Data)")
    log.info(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 80)
    
    # Simulate current market state (bull regime, mid-high IV)
    market_state = MarketState(
        market_open=True,
        market_hours=(9, 30, 16, 0),
        bullish=True,
        iv_percentile=65,
        volatility_regime="normal",
    )
    
    results = {
        "date": datetime.now().isoformat(),
        "tickers_tested": 0,
        "strategies": {},
        "signals_by_symbol": {},
    }
    
    for i, symbol in enumerate(TI_TICKERS_25, 1):
        log.info(f"\n[{i}/25] Testing {symbol}...")
        
        try:
            # Get daily bars for technical analysis
            daily = get_bars(symbol, period="65d", interval="1d")
            if daily.empty or len(daily) < 20:
                log.warning(f"  ✗ {symbol}: Insufficient daily bars")
                continue
            
            # Get options chain
            chain = _get_options_chain(symbol)
            if chain is None:
                log.warning(f"  ✗ {symbol}: No options chain available")
                continue
            
            log.info(f"  ✓ {symbol}: {len(daily)} daily bars, {len(chain.calls)} calls, {len(chain.puts)} puts")
            results["tickers_tested"] += 1
            results["signals_by_symbol"][symbol] = []
            
            # Test each strategy
            strategies_to_test = [
                ("MomentumCall", momentum_call),
                ("BearPutSpread", bear_put_spread),
                ("BearCallSpread", bear_call_spread),
                ("IronCondor", iron_condor),
                ("Butterfly", butterfly_spread),
                ("MeanReversion", mean_reversion),
                ("BreakoutRetest", breakout_retest),
                ("TrendPullback", trend_pullback_spread),
                ("ShortSqueeze", short_squeeze),
                ("CoveredCall", covered_call),
            ]
            
            for strat_name, strat_func in strategies_to_test:
                try:
                    signal = strat_func(symbol, daily, chain, market_state)
                    if signal:
                        confidence = getattr(signal, 'confidence', 0.0)
                        move_pct = getattr(signal, 'move_pct', 0.0)
                        log.info(f"    → {strat_name}: {signal.action} @ ${signal.entry_price:.2f} "
                                f"(conf={confidence:.2f}, move={move_pct:.1f}%)")
                        
                        results["signals_by_symbol"][symbol].append({
                            "strategy": strat_name,
                            "action": signal.action,
                            "entry_price": signal.entry_price,
                            "confidence": confidence,
                            "move_pct": move_pct,
                        })
                        
                        # Track strategy totals
                        if strat_name not in results["strategies"]:
                            results["strategies"][strat_name] = 0
                        results["strategies"][strat_name] += 1
                except Exception as e:
                    log.debug(f"    ✗ {strat_name}: {e}")
        
        except Exception as e:
            log.error(f"  ✗ {symbol}: {e}")
    
    # Summary
    log.info("\n" + "=" * 80)
    log.info(f"RESULTS SUMMARY")
    log.info("=" * 80)
    log.info(f"Tickers tested: {results['tickers_tested']}/{len(TI_TICKERS_25)}")
    log.info(f"Total signals generated: {sum(len(v) for v in results['signals_by_symbol'].values())}")
    log.info(f"\nSignals by strategy:")
    for strat, count in sorted(results["strategies"].items(), key=lambda x: -x[1]):
        log.info(f"  {strat:20s}: {count:3d} signals")
    
    # Save results
    output_file = ROOT / "predictions" / f"backtest_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    log.info(f"\n✓ Results saved to {output_file.name}")
    
    return results

if __name__ == "__main__":
    run_backtest()
