"""
EDGAR 8-K RSS Scraper
=====================
Polls the SEC's free public 8-K ATOM feed for material event filings.
Filters for high-value keywords (supply agreements, contracts, revenue guidance)
and resolves company → ticker via SEC's own company_tickers.json (CIK lookup).

No API key or authentication required.
Uses stdlib xml.etree + requests only — no extra dependencies.

Feed updates every ~10 minutes. We poll on the same cadence.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as XmlET
from typing import Dict, List, Optional, Set

log = logging.getLogger("ApexTrader")

# ── EDGAR ATOM feed — free, public, updated ~every 10 min ─────────────────
_EDGAR_FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=8-K&dateb=&owner=include&count=40&output=atom"
)

# ── SEC CIK → ticker lookup — loaded once, cached for the session ──────────
# https://www.sec.gov/files/company_tickers.json  (free, no auth)
# Format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
_CIK_TICKER_MAP: Dict[str, str] = {}   # "0000320193" → "AAPL"
_cik_map_loaded: bool = False
_CIK_RE = re.compile(r"/edgar/data/(\d+)/")  # CIK is in the Archives link path

_SEC_HEADERS = {"User-Agent": "BitStrider-Research contact@bitstrider.io"}


def _load_cik_map() -> None:
    """Fetch SEC company_tickers.json and build CIK → ticker lookup (once per session)."""
    global _CIK_TICKER_MAP, _cik_map_loaded
    if _cik_map_loaded:
        return
    try:
        import requests
        resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=_SEC_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        for rec in data.values():
            cik_padded = str(rec["cik_str"]).zfill(10)
            _CIK_TICKER_MAP[cik_padded] = rec["ticker"].upper()
        log.info(f"[EDGAR] CIK map loaded: {len(_CIK_TICKER_MAP):,} companies")
        _cik_map_loaded = True
    except Exception as exc:
        log.warning(f"[EDGAR] Could not load CIK map: {exc}")


def _ticker_from_cik(cik_raw: str) -> Optional[str]:
    """Resolve a raw CIK string (any length) to a ticker via the CIK map."""
    cik_padded = cik_raw.strip().zfill(10)
    return _CIK_TICKER_MAP.get(cik_padded)


# ── Keyword filter ─────────────────────────────────────────────────────────
_TRIGGER_KEYWORDS = [
    "supply agreement",
    "purchase agreement",
    "customer agreement",
    "license agreement",
    "collaboration agreement",
    "contract award",
    "government contract",
    "defense contract",
    "definitive agreement",
    "merger agreement",
    "acquisition",
    "revenue guidance",
    "preliminary results",
    "unaudited results",
    "material contract",
    "strategic partnership",
]

# ── Penny/shell filters — skip low-quality filings ────────────────────────
_SKIP_KEYWORDS = ["blank check", "shell company", "spac", "special purpose acquisition"]
_SKIP_NAME_FRAGMENTS = ["acquisition corp", "acquisition co.", "blank check"]
_VALID_TICKER  = re.compile(r"^[A-Z]{1,5}$")  # no warrant (W), unit (U), right (R) suffixes
# Warrant/right/unit suffix filter: tickers >3 chars ending in W, R, or U are non-equity securities
_WARRANT_RE    = re.compile(r"^[A-Z]{2,4}[WRU]$")

# ── State ──────────────────────────────────────────────────────────────────
_seen_filing_ids: Set[str] = set()
_last_fetch_ts: float = 0.0
_FETCH_TTL = 600  # 10 minutes


def get_edgar_triggered_tickers() -> List[str]:
    """
    Fetch the latest EDGAR 8-K ATOM feed and return tickers for companies
    that filed material-event 8-Ks matching our keyword filter.

    Ticker resolution order:
      1. CIK extracted from the entry URL → SEC company_tickers.json lookup
      2. Fallback: (UPPERCASE) pattern in the title (some filers include it)

    Returns an empty list on network error or if nothing new is found.
    Results are cached for FETCH_TTL seconds.
    """
    global _last_fetch_ts, _seen_filing_ids

    now = time.time()
    if now - _last_fetch_ts < _FETCH_TTL:
        return []

    _last_fetch_ts = now
    _load_cik_map()

    try:
        import requests
        resp = requests.get(
            _EDGAR_FEED_URL,
            headers=_SEC_HEADERS,
            timeout=12,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.debug(f"[EDGAR] Feed fetch failed: {exc}")
        return []

    try:
        root = XmlET.fromstring(resp.content)
    except XmlET.ParseError as exc:
        log.debug(f"[EDGAR] XML parse error: {exc}")
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}

    def _keyword_match(text: str) -> bool:
        t = text.lower()
        if any(sk in t for sk in _SKIP_KEYWORDS):
            return False
        if any(sf in t for sf in _SKIP_NAME_FRAGMENTS):
            return False
        return any(kw in t for kw in _TRIGGER_KEYWORDS)

    def _is_tradeable(sym: str) -> bool:
        """Filter out warrants, rights, units, and other non-equity suffixes."""
        if not _VALID_TICKER.match(sym):
            return False
        if _WARRANT_RE.match(sym):
            return False
        return True

    triggered: List[str] = []
    seen_tickers: Set[str] = set()

    for entry in root.findall("atom:entry", ns):
        entry_id_el = entry.find("atom:id", ns)
        entry_id = (entry_id_el.text or "").strip() if entry_id_el is not None else ""

        if entry_id in _seen_filing_ids:
            continue

        title_el   = entry.find("atom:title",   ns)
        summary_el = entry.find("atom:summary", ns)
        link_el    = entry.find("atom:link",    ns)

        title   = (title_el.text   or "").strip() if title_el   is not None else ""
        summary = (summary_el.text or "").strip() if summary_el is not None else ""
        link    = (link_el.get("href", "") if link_el is not None else "") or entry_id

        full_text = f"{title} {summary}"

        if not _keyword_match(full_text):
            _seen_filing_ids.add(entry_id)
            continue

        # ── Resolve ticker ──────────────────────────────────────────────
        sym: Optional[str] = None

        # 1. Extract CIK from the entry link/ID URL and look it up
        cik_match = _CIK_RE.search(link)  # CIK lives in /edgar/data/{CIK}/ path
        if cik_match:
            sym = _ticker_from_cik(cik_match.group(1))

        # 2. Fallback: (UPPERCASE) pattern in title (e.g. "COMPANY (AAPL) (8-K)")
        if not sym:
            candidates = re.findall(r"\(([A-Z]{1,5})\)", title)
            sym = next((c for c in reversed(candidates) if _VALID_TICKER.match(c)), None)

        if sym and sym not in seen_tickers and _is_tradeable(sym):
            triggered.append(sym)
            seen_tickers.add(sym)
            log.info(f"[EDGAR] 8-K match: {sym} — {title[:90]}")

        _seen_filing_ids.add(entry_id)

    # Bound the seen-set
    if len(_seen_filing_ids) > 2000:
        _seen_filing_ids = set(list(_seen_filing_ids)[-1000:])

    if triggered:
        log.info(f"[EDGAR] {len(triggered)} newly triggered tickers: {triggered}")

    return triggered
