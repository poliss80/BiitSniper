"""Persistence helpers for daily trade predictions."""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

import pytz

_ET = pytz.timezone("America/New_York")
_PICKS_PATH = Path(__file__).parent.parent / "predictions" / "day_picks.json"

log = logging.getLogger("ApexTrader")


def save_day_picks(picks: list[Any], market_regime: str) -> None:
    """Serialise *picks* (up to 5) to ``predictions/day_picks.json``.

    Each entry in *picks* must expose ``.symbol``, ``.action``, ``.price``,
    ``.confidence``, ``.strategy``, and ``.reason`` attributes.
    """
    try:
        _PICKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "generated_at": datetime.datetime.now(_ET).isoformat(timespec="seconds"),
            "date": str(datetime.date.today()),
            "market_regime": market_regime,
            "picks": [
                {
                    "symbol":     s.symbol,
                    "action":     s.action,
                    "price":      round(s.price, 4),
                    "confidence": round(s.confidence, 4),
                    "strategy":   s.strategy,
                    "reason":     s.reason,
                }
                for s in picks[:5]
            ],
        }
        _PICKS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning(f"day_picks.json write failed: {exc}")
