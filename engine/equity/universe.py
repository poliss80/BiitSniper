"""
engine/universe.py — Dynamic universe manager
===============================================
All tickers added by Trade Ideas, predict_tomorrow, or manual injection live
here in ``data/universe.json``.  Tickers expire automatically after their TTL
so the list never balloons.

Schema (data/universe.json)
----------------------------
{
  "version": 1,
  "updated": "YYYY-MM-DD",
  "tickers": {
    "KOD":  {"tier": 1, "added": "2026-03-26"},
    "MDGL": {"tier": 2, "added": "2026-03-26"},
    ...
  }
}

TTL rules (configurable via config.py or env):
  Tier 1 (momentum)   → 30 minutes
  Tier 2 (established)→ 30 minutes
  Tier 3 (following)  → 30 minutes

The 'added' field stores a full ISO-8601 datetime (UTC) so sub-day TTLs work.

Core tickers hardcoded in config.py are NOT managed here and never expire.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

# ── Paths ─────────────────────────────────────────────────────────────────────
_REPO_ROOT   = Path(__file__).resolve().parents[2]
DATA_DIR     = _REPO_ROOT / "data"
UNIVERSE_FILE = DATA_DIR / "universe.json"
TI_PRIMARY_FILE = DATA_DIR / "ti_primary.json"
TI_PRIMARY_TTL_MINUTES = int(os.getenv("TI_PRIMARY_TTL_MINUTES", "15"))

# ── TTL per tier (minutes) ────────────────────────────────────────────────────
# The primary dynamic universe should refresh frequently; default TTL is 15 min.
TIER_TTL: dict[int, int] = {
    1: int(os.getenv("UNIVERSE_TTL_TIER1", "15")),
    2: int(os.getenv("UNIVERSE_TTL_TIER2", "15")),
    3: int(os.getenv("UNIVERSE_TTL_TIER3", "15")),
}

_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_raw() -> dict:
    """Read universe.json from disk.  Returns empty schema on missing/corrupt file."""
    if not UNIVERSE_FILE.exists():
        return {"version": 1, "updated": str(date.today()), "tickers": {}}
    try:
        return json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "updated": str(date.today()), "tickers": {}}


def _save_raw(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data["updated"] = str(date.today())
    UNIVERSE_FILE.write_text(
        json.dumps(data, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _is_expired(entry: dict) -> bool:
    tier = int(entry.get("tier", 1))
    ttl_minutes = TIER_TTL.get(tier, 30)
    try:
        added_str = entry["added"]
        # Support both legacy date-only strings and new full datetimes
        if len(added_str) <= 10:
            # old format "YYYY-MM-DD" — treat as start-of-day UTC
            added_dt = datetime.fromisoformat(added_str).replace(tzinfo=timezone.utc)
        else:
            added_dt = datetime.fromisoformat(added_str)
            if added_dt.tzinfo is None:
                added_dt = added_dt.replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return False  # unknown format → keep
    return datetime.now(timezone.utc) - added_dt > timedelta(minutes=ttl_minutes)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def add_tickers(
    symbols: list[str],
    tier: Literal[1, 2, 3] = 1,
    today: str | None = None,
) -> int:
    """Add or refresh tickers.  Returns the number of *new* tickers inserted."""
    # Use full UTC datetime so minute-level TTL works correctly.
    # The legacy `today` param (date string) is still accepted for back-compat.
    added_ts = today or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock:
        data = _load_raw()
        tickers = data.setdefault("tickers", {})
        fresh = 0
        for sym in symbols:
            sym = sym.strip().upper()
            if not sym:
                continue
            if sym not in tickers:
                fresh += 1
            # Refresh timestamp whether new or existing — keeps active ones alive
            tickers[sym] = {"tier": tier, "added": added_ts}
        _save_raw(data)
    return fresh


def get_tier(tier: int) -> list[str]:
    """Return all non-expired tickers for a given tier, newest-added first."""
    data = _load_raw()
    items = [
        (sym, entry)
        for sym, entry in data.get("tickers", {}).items()
        if int(entry.get("tier", 1)) == tier and not _is_expired(entry)
    ]
    # ISO timestamp strings sort lexicographically = chronologically; newest first
    items.sort(key=lambda x: x[1].get("added", ""), reverse=True)
    return [sym for sym, _ in items]


def get_latest_batch(window_minutes: int = 5) -> list[str]:
    """Return all non-expired tickers from the most recent scrape run.

    The TI scraper writes 3 sub-batches in quick succession (each page gets its
    own timestamp a few seconds apart).  This function collects every ticker
    whose 'added' timestamp falls within *window_minutes* of the most recent
    timestamp — i.e. the full output of the last scrape run.  Results are
    sorted newest-first within the window.
    """
    data = _load_raw()
    now_utc = datetime.now(timezone.utc)
    entries: list[tuple[str, dict]] = []
    for sym, entry in data.get("tickers", {}).items():
        if not isinstance(entry, dict) or _is_expired(entry):
            continue
        entries.append((sym, entry))

    if not entries:
        return []

    # Find the newest timestamp present
    newest_str = max(e.get("added", "") for _, e in entries)
    if not newest_str:
        return []
    try:
        newest_dt = datetime.fromisoformat(newest_str)
        if newest_dt.tzinfo is None:
            newest_dt = newest_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return []

    cutoff = newest_dt - timedelta(minutes=window_minutes)

    batch: list[tuple[str, str]] = []  # (sym, added_ts)
    for sym, entry in entries:
        added_str = entry.get("added", "")
        if not added_str:
            continue
        try:
            added_dt = datetime.fromisoformat(added_str)
            if added_dt.tzinfo is None:
                added_dt = added_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if added_dt >= cutoff:
            batch.append((sym, added_str))

    # newest first within batch (preserves sub-batch order)
    batch.sort(key=lambda x: x[1], reverse=True)
    return [sym for sym, _ in batch]


def _load_ti_primary_raw() -> dict:
    """Read the TI primary file from disk and return raw JSON data."""
    if not TI_PRIMARY_FILE.exists():
        return {"updated": "", "tickers": []}
    try:
        return json.loads(TI_PRIMARY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"updated": "", "tickers": []}


def _is_ti_primary_stale(data: dict) -> bool:
    """Return True if the ti_primary.json data is too old to be used."""
    updated = str(data.get("updated", "")).strip()
    if not updated:
        return True
    try:
        updated_dt = datetime.fromisoformat(updated)
        if updated_dt.tzinfo is None:
            # Legacy timestamps were written without timezone info.
            # Interpret them as local time so the freshness check is accurate.
            updated_dt = updated_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        updated_dt = updated_dt.astimezone(timezone.utc)
    except ValueError:
        try:
            updated_dt = datetime.strptime(updated, "%Y-%m-%d %H:%M:%S")
            updated_dt = updated_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
            updated_dt = updated_dt.astimezone(timezone.utc)
        except Exception:
            return True
    return datetime.now(timezone.utc) - updated_dt > timedelta(minutes=TI_PRIMARY_TTL_MINUTES)


def get_ti_primary() -> list[str]:
    """Return the latest TI capture tickers from ti_primary.json, or [] if stale."""
    data = _load_ti_primary_raw()
    if _is_ti_primary_stale(data):
        return []
    tickers = [str(t).strip().upper() for t in data.get("tickers", []) if isinstance(t, str) and t.strip()]
    return list(dict.fromkeys(tickers))


def prune_ti_primary(dry_run: bool = False) -> list[str]:
    """Remove stale TI primary tickers when the source file is too old."""
    data = _load_ti_primary_raw()
    if not data.get("tickers") or not _is_ti_primary_stale(data):
        return []

    removed = [str(t).strip().upper() for t in data.get("tickers", []) if isinstance(t, str) and t.strip()]
    if not dry_run:
        TI_PRIMARY_FILE.write_text(
            json.dumps({"updated": "", "tickers": []}, indent=2),
            encoding="utf-8",
        )
    return sorted(dict.fromkeys(removed))


def prune(dry_run: bool = False) -> list[str]:
    """Remove expired tickers.  Returns the list of removed symbols."""
    with _lock:
        data  = _load_raw()
        tickers = data.get("tickers", {})
        expired = [sym for sym, entry in tickers.items() if _is_expired(entry)]
        if not dry_run:
            for sym in expired:
                del tickers[sym]
            _save_raw(data)
    return sorted(expired)


def stats() -> dict:
    """Return a summary dict for logging/display."""
    data    = _load_raw()
    tickers = data.get("tickers", {})
    alive   = {s: e for s, e in tickers.items() if not _is_expired(e)}
    expired = {s: e for s, e in tickers.items() if _is_expired(e)}
    by_tier: dict[int, int] = {}
    for e in alive.values():
        t = int(e.get("tier", 1))
        by_tier[t] = by_tier.get(t, 0) + 1
    return {
        "total_alive":   len(alive),
        "total_expired": len(expired),
        "by_tier":       by_tier,
        "file":          str(UNIVERSE_FILE),
    }

def filter_universe_by_positions(universe: list[str], held_symbols: set[str], exclude_unfilled_buys: bool = True) -> list[str]:
    """Filter out symbols already held or with unfilled buy orders from the scan universe."""
    return [s for s in universe if s not in held_symbols]


def merge_live(dyn: list[str], core: list[str], exclude: set[str]) -> list[str]:
    """Merge dynamic (TTL-managed) tickers with core static list, deduplicating and excluding.

    Dynamic tickers appear first so recently-added ones are prioritised during
    scan slicing.  Core tickers that are not yet in *dyn* are appended after.
    """
    seen: set[str] = set(exclude)
    out: list[str] = []
    for s in list(dyn) + list(core):
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out
