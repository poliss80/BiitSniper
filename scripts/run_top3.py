"""
Dry-run top-3 scanner: scrape TI -> update config -> run all strategies -> print top 3.
No orders are placed.
"""
import warnings, logging, sys, os
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.ERROR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

# ── Step 1: Fresh TI scrape ──────────────────────────────────────
from engine.ti.capture_tradeideas import scrape_tradeideas
print("Step 1/3: Scraping Trade Ideas (15min)...")
scrape_tradeideas(update_config=True, headless=True, select_minutes=15)

# ── Step 2: Reload config & scan ────────────────────────────────
import importlib
import engine.config as cfg
importlib.reload(cfg)

from engine.equity.scan import scan_universe, get_scan_targets
from engine.utils import MarketState

targets = get_scan_targets()
print(f"Step 2/3: Scanning {len(targets)} symbols across 7 strategies...")

signals, hit_counts, scan_errors = scan_universe(targets, "neutral", MarketState.from_now())

if scan_errors:
    print(f"Scan errors: {scan_errors}")

signals.sort(key=lambda x: -x.confidence)

# ── Step 3: Print results ────────────────────────────────────────
print()
print(f"Step 3/3: Total signals found: {len(signals)}")
print()
print("=" * 57)
print("  TOP 3 STRATEGY PICKS  (DRY RUN — no orders placed)")
print("=" * 57)

for i, s in enumerate(signals[:3], 1):
    stop_str = "ATR stop ${:.2f}".format(s.atr_stop) if s.atr_stop else "stop n/a"
    print("  #{:d}  {:<6}  {:<4}  ${:.2f}  conf={:.0%}  [{}]".format(
        i, s.symbol, s.action.upper(), s.price, s.confidence, s.strategy))
    print("       {}".format(s.reason))
    print("       {}".format(stop_str))
    print()

if not signals:
    print("  No signals (market closed / no setups matching criteria)")
