"""Quick EDGAR feed test — run standalone."""
import sys, re, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(message)s")

import requests
import xml.etree.ElementTree as ET
from engine.data.edgar_scraper import (
    _EDGAR_FEED_URL, _TRIGGER_KEYWORDS, _SKIP_KEYWORDS,
    _load_cik_map, _CIK_TICKER_MAP, _CIK_RE, _ticker_from_cik,
    get_edgar_triggered_tickers,
)

# Load CIK map first
_load_cik_map()
print(f"CIK map size: {len(_CIK_TICKER_MAP):,} companies\n")

NS = {"atom": "http://www.w3.org/2005/Atom"}
resp = requests.get(_EDGAR_FEED_URL, headers={"User-Agent": "BitStrider-Research contact@bitstrider.io"}, timeout=12)
root = ET.fromstring(resp.content)
entries = root.findall("atom:entry", NS)
print(f"Total entries in feed: {len(entries)}\n")

hits = []
for entry in entries:
    title_el   = entry.find("atom:title",   NS)
    summary_el = entry.find("atom:summary", NS)
    updated_el = entry.find("atom:updated", NS)
    link_el    = entry.find("atom:link",    NS)
    id_el      = entry.find("atom:id",      NS)
    title   = (title_el.text   or "").strip() if title_el   is not None else ""
    summary = (summary_el.text or "").strip() if summary_el is not None else ""
    updated = (updated_el.text or "")[:16]    if updated_el is not None else ""
    link    = (link_el.get("href", "")        if link_el    is not None else "") or (id_el.text or "")
    full = (title + " " + summary).lower()
    if any(sk in full for sk in _SKIP_KEYWORDS):
        continue
    matched = [kw for kw in _TRIGGER_KEYWORDS if kw in full]
    if not matched:
        continue
    # Resolve via CIK
    cik_m = _CIK_RE.search(link)
    sym = _ticker_from_cik(cik_m.group(1)) if cik_m else "???"
    hits.append((updated, sym or "???", matched[0], title[:85]))

print(f"Keyword-matched filings: {len(hits)}\n")
for ts, sym, kw, title in hits:
    print(f"  [{ts}]  {sym:<6}  [{kw}]  {title}")

print("\n--- get_edgar_triggered_tickers() ---")
tickers = get_edgar_triggered_tickers()
print(f"Triggered tickers: {tickers if tickers else '(none new — weekend/market closed or all already seen)'}")
