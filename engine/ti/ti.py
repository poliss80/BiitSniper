"""
ApexTrader TI helpers.

Encapsulates Trade Ideas scraper module loading and shared TI validation logic.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("ApexTrader.TI")
_capture_module = None


def _load_capture_module() -> Any:
    global _capture_module
    if _capture_module is not None:
        return _capture_module

    try:
        import engine.ti.capture_tradeideas as _mod  # type: ignore
    except ImportError as exc:
        raise
    _capture_module = _mod
    return _capture_module


def is_valid_ti_ticker(symbol: str) -> bool:
    if not symbol or not isinstance(symbol, str):
        return False
    symbol = symbol.strip().upper()
    return 1 <= len(symbol) <= 5 and symbol.isalpha()


def get_scans() -> dict[str, dict[str, str]]:
    module = _load_capture_module()
    return getattr(module, "SCANS")


def scrape_tradeideas(
    *,
    update_config: bool = False,
    headless: bool = False,
    chrome_profile: str | None = None,
    select_minutes: int | None = None,
    include_toplists: bool = False,
    scan_keys: list[str] | None = None,
    select_30min: bool = False,
    browser: str = "edge",
    remote_debug_port: int = 9222,
) -> dict[str, list[str]]:
    module = _load_capture_module()
    return module.scrape_tradeideas(
        update_config=update_config,
        headless=headless,
        chrome_profile=chrome_profile,
        select_minutes=select_minutes,
        include_toplists=include_toplists,
        scan_keys=scan_keys,
        select_30min=select_30min,
        browser=browser,
        remote_debug_port=remote_debug_port,
    )
