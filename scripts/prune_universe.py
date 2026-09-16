"""
scripts/prune_universe.py — Universe maintenance tool
======================================================
Show and optionally remove expired tickers from data/universe.json.

Usage:
  # Show what would be pruned (dry run)
  python scripts/prune_universe.py

  # Actually prune expired tickers
  python scripts/prune_universe.py --apply

  # Show full stats
  python scripts/prune_universe.py --stats
"""
import sys, os, argparse
import psutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.equity.universe import prune, prune_ti_primary, stats, TIER_TTL
from engine.config import MEMORY_WARN_MB

def _check_memory():
    process = psutil.Process()
    mem_mb = process.memory_info().rss / 1024 / 1024
    if mem_mb > MEMORY_WARN_MB:
        print(f"[OOM WARNING] Memory usage high: {mem_mb:.0f} MB (limit {MEMORY_WARN_MB} MB)")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="Actually remove expired tickers")
    p.add_argument("--stats", action="store_true", help="Show full stats and exit")
    args = p.parse_args()

    _check_memory()

    s = stats()
    print(f"\nUniverse: {s['total_alive']} alive | {s['total_expired']} expired")
    print(f"  Tier 1 (momentum,    TTL {TIER_TTL[1]}min): {s['by_tier'].get(1, 0)}")
    print(f"  Tier 2 (established, TTL {TIER_TTL[2]}min): {s['by_tier'].get(2, 0)}")
    print(f"  Tier 3 (following,   TTL {TIER_TTL[3]}min): {s['by_tier'].get(3, 0)}")
    print(f"  File: {s['file']}\n")

    if args.stats:
        return

    expired = prune(dry_run=not args.apply)
    stale_primary = prune_ti_primary(dry_run=not args.apply)

    if not expired and not stale_primary:
        print("Nothing to prune.")
        return

    if expired:
        if args.apply:
            print(f"Pruned {len(expired)} expired tickers:")
        else:
            print(f"Would prune {len(expired)} expired tickers (run --apply to remove):")
        for sym in expired:
            print(f"  {sym}")

    if stale_primary:
        if args.apply:
            print(f"Pruned stale ti_primary.json tickers ({len(stale_primary)}):")
        else:
            print(f"Would prune stale ti_primary.json tickers ({len(stale_primary)}) (run --apply to remove):")
        for sym in stale_primary:
            print(f"  {sym}")

if __name__ == "__main__":
    main()
