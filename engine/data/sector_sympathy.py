"""
Sector Sympathy Scanner
=======================
Monitors "leader" stocks for significant gap/move events.
When a leader fires, returns the corresponding sympathy tickers
for injection into the active scan universe.

Logic:
  - Check each leader via Finnhub quote (free, ~8 req/s on free tier)
  - If leader's intraday move >= threshold → return its sympathy peers
  - Results are cached for CACHE_TTL seconds to avoid hammering the API
  - Sympathies are ordered: strongest historical correlation first
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

log = logging.getLogger("ApexTrader")

# ── Sympathy map ───────────────────────────────────────────────────────────
# Structure: leader_ticker → {"min_move_pct": float, "sympathies": [str, ...]}
# Both up AND down moves trigger (abs check), so a COHR gap-down also alerts LITE/AAOI.
SYMPATHY_MAP: Dict[str, Dict] = {

    # ── Optical / photonics networking ───────────────────────────────────
    # COHR or IIVI gap → LITE, AAOI, VIAV, NPTN tend to follow same session
    "COHR": {"min_move_pct": 3.0, "sympathies": ["LITE", "AAOI", "VIAV", "NPTN"]},
    "IIVI": {"min_move_pct": 3.0, "sympathies": ["LITE", "AAOI", "VIAV", "COHR", "NPTN"]},

    # ── Aerospace / superalloys / defense supply chain ────────────────────
    # RTX, GE, HON, LMT earnings/contract win → HWM, TGI, HEICO, CW follow
    "RTX":  {"min_move_pct": 2.5, "sympathies": ["HWM", "TGI", "HEICO", "CW", "TDG"]},
    "GE":   {"min_move_pct": 2.5, "sympathies": ["HWM", "TGI", "HEICO", "CW", "SPR"]},
    "HON":  {"min_move_pct": 2.0, "sympathies": ["HWM", "CW", "HEICO", "TDG"]},
    "LMT":  {"min_move_pct": 2.5, "sympathies": ["HWM", "TGI", "HEICO", "NOC", "TDG"]},

    # ── Flash / NAND storage ──────────────────────────────────────────────
    # WDC or MU guidance → SNDK (WD spinoff), STX, NTAP follow
    "WDC":  {"min_move_pct": 3.0, "sympathies": ["SNDK", "STX", "NTAP", "MU"]},
    "MU":   {"min_move_pct": 3.0, "sympathies": ["SNDK", "WDC", "STX", "NTAP"]},

    # ── AI infra / datacenter buildout ───────────────────────────────────
    # SMCI or DELL move → ANET, VRT, AMBA, POWI benefit from capex narrative
    "SMCI": {"min_move_pct": 4.0, "sympathies": ["ANET", "VRT", "AMBA", "POWI", "CIEN"]},
    "DELL": {"min_move_pct": 3.0, "sympathies": ["SMCI", "ANET", "VRT", "AMBA"]},
    "MSFT": {"min_move_pct": 2.5, "sympathies": ["SMCI", "ANET", "VRT", "DELL"]},
    "META": {"min_move_pct": 3.0, "sympathies": ["SMCI", "ANET", "VRT", "NVDA"]},
    "AMZN": {"min_move_pct": 2.5, "sympathies": ["SMCI", "ANET", "VRT", "DELL"]},

    # ── Semiconductors / AI chips ─────────────────────────────────────────
    "NVDA": {"min_move_pct": 3.0, "sympathies": ["AMD", "SMCI", "ANET", "AMBA", "MPWR", "MRVL"]},
    "AMD":  {"min_move_pct": 3.0, "sympathies": ["NVDA", "SMCI", "MPWR", "MRVL"]},
    "AVGO": {"min_move_pct": 3.0, "sympathies": ["MRVL", "MPWR", "CIEN", "ANET"]},

    # ── EV / battery supply chain ─────────────────────────────────────────
    "TSLA": {"min_move_pct": 4.0, "sympathies": ["RIVN", "LCID", "LTHM", "ALB", "SQM", "NIO"]},
    "ALB":  {"min_move_pct": 3.0, "sympathies": ["LTHM", "SQM", "LAC"]},

    # ── Biotech catalyst sympathy ─────────────────────────────────────────
    # Same-indication readouts lift peer basket same or next session
    "MRNA": {"min_move_pct": 5.0, "sympathies": ["BNTX", "NVAX", "VXRT"]},
    "BNTX": {"min_move_pct": 5.0, "sympathies": ["MRNA", "NVAX", "VXRT"]},

    # ── Solar / clean energy ──────────────────────────────────────────────
    "ENPH": {"min_move_pct": 4.0, "sympathies": ["SEDG", "RUN", "FSLR", "ARRY"]},
    "FSLR": {"min_move_pct": 3.0, "sympathies": ["ENPH", "SEDG", "CSIQ", "JKS"]},
}

# ── Quote cache ────────────────────────────────────────────────────────────
_quote_cache: Dict[str, Dict] = {}
_cache_ts: float = 0.0
_CACHE_TTL = 300  # 5 minutes — sufficient resolution for sympathy detection


def _fetch_quotes(symbols: List[str]) -> Dict[str, Dict]:
    """Fetch Finnhub quotes for *symbols*; returns {sym: {"c", "pc", "dp"}}."""
    global _quote_cache, _cache_ts

    now = time.time()
    if now - _cache_ts < _CACHE_TTL and _quote_cache:
        return _quote_cache

    try:
        from engine.utils import get_finnhub_client
        client = get_finnhub_client()
    except Exception as exc:
        log.debug(f"[SYMPATHY] Finnhub client unavailable: {exc}")
        return {}

    results: Dict[str, Dict] = {}
    for sym in symbols:
        try:
            q = client.quote(sym)
            if q and q.get("c", 0) > 0:
                results[sym] = q
            time.sleep(0.13)  # ~7.5 req/s — safely within 60/min free limit
        except Exception as exc:
            log.debug(f"[SYMPATHY] Quote error {sym}: {exc}")

    _quote_cache = results
    _cache_ts = now
    return results


def get_active_sympathies(min_move_override: Optional[float] = None) -> List[str]:
    """
    Check all leaders and return a deduplicated list of sympathy tickers
    for any leader whose abs(intraday move) exceeds its configured threshold.

    Returns an empty list if Finnhub is unavailable or no leaders fired.
    """
    all_leaders = list(SYMPATHY_MAP.keys())
    quotes = _fetch_quotes(all_leaders)

    if not quotes:
        return []

    fired_leaders: List[str] = []
    triggered: List[str] = []
    seen: set = set()

    for leader, cfg in SYMPATHY_MAP.items():
        q = quotes.get(leader)
        if not q:
            continue

        threshold = min_move_override if min_move_override is not None else cfg["min_move_pct"]
        pct_change = q.get("dp", 0.0)  # Finnhub: dp = daily % change

        if abs(pct_change) >= threshold:
            direction = "▲" if pct_change > 0 else "▼"
            fired_leaders.append(f"{leader}({direction}{abs(pct_change):.1f}%)")
            for sym in cfg["sympathies"]:
                if sym not in seen:
                    triggered.append(sym)
                    seen.add(sym)

    if fired_leaders:
        log.info(
            f"[SYMPATHY] Leaders fired: {', '.join(fired_leaders)} "
            f"→ {len(triggered)} sympathy tickers queued: {triggered[:10]}"
        )

    return triggered
