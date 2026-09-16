"""
Trade Ideas — Screenshot + Universe Updater
============================================
Navigates to three Trade Ideas TIPro scan pages with Selenium Chrome,
captures screenshots, extracts ticker symbols, and optionally persists
results into data/universe.json so the universe is kept current.

Pages scraped
-------------
  HIGH_SHORT_FLOAT      https://www.trade-ideas.com/TIPro/highshortfloat/
  MARKET_SCOPE_360      https://www.trade-ideas.com/TIPro/marketscope360/
  UNUSUAL_OPTIONS_VOL   https://www.trade-ideas.com/TIPro/unusualoptionsvolume/
                        Unusual-options-volume tickers → tier 1 (directional conviction)

Usage
-----
  # Single run — screenshot + show extracted tickers
  python scripts/capture_tradeideas.py

  # Single run AND persist results to data/universe.json
  python scripts/capture_tradeideas.py --update-config

  # Loop every 5 minutes AND persist results to data/universe.json
  python scripts/capture_tradeideas.py --loop 300 --update-config

  # Use your existing Chrome profile (already logged in to Trade-Ideas)
  python scripts/capture_tradeideas.py --chrome-profile "Default" --update-config

Requirements
------------
  pip install selenium webdriver-manager pillow
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("ApexTrader")

# ── optional PIL for timestamp overlay ──────────────────────────
try:
    from PIL import Image, ImageDraw
    PIL_OK = True
except ImportError:
    PIL_OK = False

# ── Selenium ─────────────────────────────────────────────────────
try:
    from selenium import webdriver
    from selenium.webdriver.edge.options import Options as EdgeOptions
    from selenium.webdriver.edge.service import Service as EdgeService
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import SessionNotCreatedException, TimeoutException
    from webdriver_manager.microsoft import EdgeChromiumDriverManager
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False

# ── Paths ────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT   = SCRIPT_DIR.parent.parent
OUTPUT_DIR  = REPO_ROOT / "screenshots"
CONFIG_FILE = REPO_ROOT / "engine" / "config.py"
TI_UNUSUAL_OPTIONS_FILE = REPO_ROOT / "data" / "ti_unusual_options.json"
TI_PRIMARY_FILE = REPO_ROOT / "data" / "ti_primary.json"

# ── Trade Ideas scan URLs ────────────────────────────────────────
SCANS: dict[str, dict] = {
    "highshortfloat": {
        "url":    "https://www.trade-ideas.com/stock-market-dashboard/",
        "label":  "high_short_float",
        "target": "PRIORITY_2_ESTABLISHED",   # squeeze / short-float candidates
    },
    "marketscope360": {
        "url":    "https://www.trade-ideas.com/stock-market-dashboard/",
        "label":  "market_scope_360",
        "target": "PRIORITY_1_MOMENTUM",      # momentum leaders
    },
    "momentumscanner": {
        "url":    "https://www.trade-ideas.com/momentum-scanner/",
        "label":  "momentum_scanner",
        "target": "PRIORITY_1_MOMENTUM",      # public momentum candidates
    },
    "unusualoptionsvolume": {
        "url":    "https://www.trade-ideas.com/TIPro/unusualoptionsvolume/",
        "label":  "unusual_options_volume",
        "target": "PRIORITY_1_MOMENTUM",   # directional-conviction tickers → tier 1
    },
    "toplists": {
        "url":    "https://www.trade-ideas.com/momentum-scanner/",
        "label":  "momentum_scanner_races",
        "target": "PRIORITY_1_MOMENTUM",   # public momentum candidates
        "interaction": "stock_races",
    },
}

# Words to exclude from ticker extraction (common UI/nav/HTML words)
_IGNORE = {
    "A", "AN", "AND", "OR", "NOT", "THE", "FOR", "ALL", "NEW", "NO", "PM", "AM",
    "NA", "GO", "BE", "IN", "ON", "TO", "AT", "BY", "IF", "IS", "IT", "AS", "OF",
    "MY", "US", "UP", "DO", "SO", "ME", "HE", "WE", "VS",
    # UI / nav words visible on Trade Ideas pages
    "MIN", "PRE", "POST", "EST", "USD", "ETF", "ETH", "BTC",
    "HIGH", "LOW", "BUY", "SELL", "OPEN", "CLOSE", "MARKET", "PRICE",
    "FLOAT", "SHORT", "CHANGE", "VOLUME", "SCAN", "TRADE", "IDEAS", "SCOPE",
    "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN",
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
    "NAS", "DOW", "EPS", "RSI", "SMA", "EMA", "ATR", "ADX", "MACD",
    "HOLLY", "PRO", "MY", "COPY", "WAVE", "DEEP", "DIVE", "PLAY",
    "TGT",
    "UNUSUAL", "OPTIONS", "SECTORS", "EXPLORE", "GROUPS", "TRADING",
    "COMPETITION", "WATCHLISTS", "SETTINGS", "DASHBOARDS", "CHANNELS",
    "MOMENTUM", "WAVES", "STOCK", "SCOPE", "BIGGEST", "GAINERS", "LOSERS",
    "DELAYED", "LIVE", "ALERT", "ALERTS", "FILTER", "FILTERS",
    # unusualoptionsvolume UI words
    "CALL", "PUT", "CALLS", "PUTS", "SWEEP", "SWEEP", "FLOW", "FLOWS",
    "STRIKE", "EXPIRY", "EXPIRATION", "PREMIUM", "CONTRACT", "CONTRACTS",
    "OI", "BULLISH", "BEARISH", "NEUTRAL",
    "RACE", "CENTRAL", "LEADER", "LEADERS", "LAGGARD", "LAGGARDS",
    "WINNER", "WINNERS", "LOSER", "LOSERS", "RANK", "RANKED",
    # Index / non-tradeable symbols seen in TI page text
    "DJI", "NASD", "ADR", "TX", "TI", "LLC", "SWING", "SMART",
    "SP", "NDX", "RUT", "VIX", "DJIA",
}

_TICKER_RE = re.compile(r'\b([A-Z]{2,5})\b')

# ── Secondary blocklist used when applying scraped tickers to the live universe ──
# Words that survive the broad regex above but are never real tradeable tickers.
_TI_SCRAPE_GARBAGE: set[str] = {
    "TI", "NASD", "SWING", "SMART", "CBD", "LLC", "DJI", "SPY", "ARTL",  # known artifacts
    "BUY", "SELL", "SHORT", "LONG", "ALL", "NEW", "TOP", "HOT",            # action words
    "NYSE", "AMEX", "OTC", "ETF", "ADR",                                   # exchange/type labels
    "HIGH", "LOW", "OPEN", "CLOSE", "VOL", "RVOL", "FLOAT",               # column headers
    "BF", "NOTE",                                                          # feeds with no data
    "AI", "CA", "AZ", "CO",                                                # state/generic abbrevs
    "SPDR", "SSGA", "IVV", "VOO", "VTI",                                  # ETF brand names / broad index ETFs
}


def _is_valid_ti_ticker(sym: str) -> bool:
    """Return False for obvious scraper garbage: too short, too long, non-alpha, or block-listed."""
    if not sym or not isinstance(sym, str):
        return False
    s = sym.strip().upper()
    if not s:
        return False
    # Must be 1–5 uppercase letters (optionally ending in one digit for share classes)
    if not re.fullmatch(r"[A-Z]{1,5}[0-9]?", s):
        return False
    if s in _IGNORE or s in _TI_SCRAPE_GARBAGE:
        return False
    return True


# How long to wait (seconds) for page to render
TABLE_WAIT_SEC = 20
PAGE_LOAD_SEC  = 15
RENDER_GRACE_SEC = 2
DROPDOWN_REFRESH_SEC = 2


# ── Persistent Edge driver singleton ─────────────────────────────
# The Edge window stays open across scrape cycles so the TI login session is
# preserved. A new window is only created if the driver is dead/missing.
_edge_driver: Optional["webdriver.Edge"] = None


def _find_existing_edgedriver() -> Optional[str]:
    """Locate msedgedriver.exe — checks repo .drivers/ first, then ~/.wdm cache."""
    import glob, os

    # 1) Repo-local driver (committed or manually placed) — highest priority
    repo_driver = REPO_ROOT / ".drivers" / "msedgedriver.exe"
    if repo_driver.is_file():
        return str(repo_driver)

    # 2) webdriver_manager cache
    wdm_root = os.path.expandvars(r"%USERPROFILE%\.wdm\drivers\msedgedriver")
    patterns = [
        os.path.join(wdm_root, "**", "msedgedriver.exe"),
        os.path.join(wdm_root, "**", "win64", "msedgedriver.exe"),
        os.path.join(wdm_root, "**", "win32", "msedgedriver.exe"),
    ]
    candidates = []
    for pattern in patterns:
        candidates.extend(glob.glob(pattern, recursive=True))
    candidates = [c for c in candidates if os.path.isfile(c)]
    if candidates:
        return max(candidates, key=lambda p: os.path.getmtime(p))
    return None


def _is_driver_alive(driver: "webdriver.Edge") -> bool:
    """Return True if the Edge WebDriver session is still responsive."""
    try:
        _ = driver.title   # any property access pings the driver
        return True
    except Exception:
        return False


# Default CDP remote debugging port — Edge will listen here so the next
# script run can re-attach without needing a new login.
_REMOTE_DEBUG_PORT = 9222


def _try_attach_edge(port: int) -> Optional["webdriver.Edge"]:
    """
    Try to attach to an already-running Edge instance that was started with
    --remote-debugging-port=<port>.  Returns the driver if successful, or
    None if no Edge is listening on that port.
    """
    try:
        opts = EdgeOptions()
        opts.add_experimental_option("debuggerAddress", f"localhost:{port}")
        existing = _find_existing_edgedriver()
        if existing:
            service = EdgeService(existing)
        else:
            service = EdgeService(EdgeChromiumDriverManager().install())
        import subprocess as _sp2, sys as _sys2
        if _sys2.platform == "win32":
            service.creation_flags = _sp2.CREATE_NO_WINDOW
        driver = webdriver.Edge(service=service, options=opts)
        _ = driver.title   # verify connection is live
        print(f"[INFO ] Re-attached to existing Edge session on port {port}.")
        return driver
    except Exception as _e:
        print(f"[INFO ] No live Edge on port {port} ({type(_e).__name__}: {_e}) — will open a new window.")
        return None


def _create_edge_driver(chrome_profile: Optional[str] = None, remote_debug_port: int = 0) -> "webdriver.Edge":
    """Spawn a new visible Edge window and return the driver (never headless)."""
    import os, subprocess as _sp, sys as _sys

    os.environ.setdefault("WDM_LOG", "0")
    os.environ.setdefault("WDM_LOG_LEVEL", "0")
    logging.getLogger("WDM").setLevel(logging.ERROR)
    logging.getLogger("webdriver_manager").setLevel(logging.ERROR)

    opts = EdgeOptions()
    opts.add_argument("--start-maximized")
    # NOTE: --no-sandbox and --disable-blink-features=AutomationControlled crash Edge v151+
    # on Windows (GetHandleVerifier fault). Do NOT add them back.
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--log-level=3")
    opts.add_argument("--disable-logging")
    opts.add_argument("--silent")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    # Keep the browser open after Python exits (prevents Selenium __del__ / quit()
    # from closing Edge — essential for the re-attach-on-next-run feature).
    opts.add_experimental_option("detach", True)

    # Enable remote debugging so a subsequent script run can re-attach to this
    # same window (preserving the Trade Ideas login session).
    if remote_debug_port > 0:
        opts.add_argument(f"--remote-debugging-port={remote_debug_port}")

    # Always use a dedicated temp profile dir — avoids locking the main Edge
    # installation (Profile 3 / User Data) which causes DevToolsActivePort crashes.
    # Login state is preserved across bot restarts via the re-attach mechanism
    # (remote-debugging-port=9222 + _try_attach_edge).
    import tempfile as _tf
    _edge_user_data = os.path.join(os.environ.get("TEMP", _tf.gettempdir()), "edge_ti_scraper")
    os.makedirs(_edge_user_data, exist_ok=True)
    opts.add_argument(f"--user-data-dir={_edge_user_data}")
    # Note: --profile-directory is intentionally omitted — combining it with a
    # custom user-data-dir causes Edge to crash on Windows (DevToolsActivePort fault).

    existing = _find_existing_edgedriver()
    if existing:
        print(f"[INFO ] Using cached msedgedriver: {existing}")
        service = EdgeService(existing)
    else:
        service = EdgeService(EdgeChromiumDriverManager().install())

    if _sys.platform == "win32":
        service.creation_flags = _sp.CREATE_NO_WINDOW

    try:
        driver = webdriver.Edge(service=service, options=opts)
    except SessionNotCreatedException as e:
        if chrome_profile and "locked" in str(e).lower():
            # Profile is locked by a running Edge instance that wasn't started with
            # --remote-debugging-port.  Re-try without a profile so the bot can still
            # open a fresh Edge window.  User will need to log in to Trade Ideas manually.
            log.warning(
                f"Edge profile '{chrome_profile}' is locked — opening a fresh Edge window instead. "
                "Log in to trade-ideas.com in the new window to enable TI scraping."
            )
            opts_fresh = EdgeOptions()
            for arg in opts.arguments:
                if "--user-data-dir" not in arg and "--profile-directory" not in arg:
                    opts_fresh.add_argument(arg)
            for k, v in (opts.experimental_options or {}).items():
                opts_fresh.add_experimental_option(k, v)
            driver = webdriver.Edge(service=service, options=opts_fresh)
        # Cached driver version doesn't match the installed Edge — download the correct one
        elif existing and "only supports Microsoft Edge version" in str(e):
            log.warning(f"Cached msedgedriver version mismatch — downloading correct driver. ({e})")
            service = EdgeService(EdgeChromiumDriverManager().install())
            if _sys.platform == "win32":
                service.creation_flags = _sp.CREATE_NO_WINDOW
            driver = webdriver.Edge(service=service, options=opts)
        else:
            # A prior interrupted run can leave the persistent scraper profile
            # unusable. Retry once with a fresh profile before failing the scan.
            retry_opts = EdgeOptions()
            for arg in opts.arguments:
                if "--user-data-dir" not in arg:
                    retry_opts.add_argument(arg)
            for key, value in (opts.experimental_options or {}).items():
                retry_opts.add_experimental_option(key, value)
            clean_profile = _tf.mkdtemp(prefix="edge_ti_scraper_")
            retry_opts.add_argument(f"--user-data-dir={clean_profile}")
            log.warning("Persistent Edge scraper profile failed; retrying with a clean profile.")
            driver = webdriver.Edge(service=service, options=retry_opts)

    driver.set_page_load_timeout(45)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    debug_hint = f" (remote-debug port {remote_debug_port})" if remote_debug_port > 0 else ""
    print(f"[INFO ] Edge browser opened{debug_hint}. Stays open across scrape cycles; re-attachable on restart.")
    return driver


def _get_driver(
    chrome_profile: Optional[str] = None,
    remote_debug_port: int = _REMOTE_DEBUG_PORT,
) -> "webdriver.Edge":
    """
    Return the persistent Edge driver.
    1. If already alive in-process → reuse it.
    2. Else try to re-attach to an existing Edge on *remote_debug_port*.
    3. Else spawn a new Edge window (with remote debugging enabled so it can
       be re-attached on the next script run without a fresh login).
    """
    global _edge_driver
    if _edge_driver is not None and _is_driver_alive(_edge_driver):
        return _edge_driver

    if _edge_driver is not None:
        print("[WARN ] Edge session lost — attempting re-attach before reopening.")

    # Step 2: try CDP re-attach
    if remote_debug_port > 0:
        _edge_driver = _try_attach_edge(remote_debug_port)

    # Step 3: open a fresh Edge window
    if _edge_driver is None:
        _edge_driver = _create_edge_driver(
            chrome_profile=chrome_profile,
            remote_debug_port=remote_debug_port,
        )

    return _edge_driver


# ── Ticker extraction ─────────────────────────────────────────────
def _extract_tickers(driver: "webdriver.Edge") -> list[str]:
    """
    Extract ticker symbols from the loaded Trade Ideas heatmap page.
    Primary: body.innerText scan (works for React/JS-rendered heatmaps).
    Fallback: href link pattern + data-symbol attributes.
    Returns a de-duped ordered list of up to 50 tickers.
    """
    found: list[str] = []

    # Strategy 1: body.innerText — most reliable for JS-rendered heatmap tiles
    try:
        body_text = driver.execute_script("return document.body.innerText;") or ""
        for m in _TICKER_RE.finditer(body_text):
            t = m.group(1)
            if t not in _IGNORE:
                found.append(t)
    except Exception:
        pass

    # Strategy 2: data-symbol / data-ticker / data-code attributes
    try:
        attrs = driver.execute_script("""
            var r = [];
            document.querySelectorAll('[data-symbol],[data-ticker],[data-code]').forEach(function(el){
                var v = el.getAttribute('data-symbol') || el.getAttribute('data-ticker') || el.getAttribute('data-code');
                if (v) r.push(v.toUpperCase().trim());
            });
            return r;
        """) or []
        for t in attrs:
            if _TICKER_RE.fullmatch(t) and t not in _IGNORE:
                found.append(t)
    except Exception:
        pass

    # Strategy 3: href links containing /stock/TICKER
    try:
        for anchor in driver.find_elements(By.TAG_NAME, "a"):
            href = anchor.get_attribute("href") or ""
            m = re.search(r'/stock/([A-Z]{1,5})(?:[/?]|$)', href)
            if m:
                candidate = m.group(1)
                if _TICKER_RE.fullmatch(candidate) and candidate not in _IGNORE:
                    found.append(candidate)
    except Exception:
        pass

    # De-dup preserving order, max 50
    seen: set[str] = set()
    clean: list[str] = []
    for t in found:
        if t not in seen and t not in _IGNORE:
            seen.add(t)
            clean.append(t)
    return clean[:50]


# ── Screenshot helper ─────────────────────────────────────────────
def _save_screenshot(driver: "webdriver.Chrome", label: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"tradeideas_{label}_{ts}.png"
    driver.save_screenshot(str(out_path))

    if PIL_OK:
        try:
            img  = Image.open(out_path)
            draw = ImageDraw.Draw(img)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + f"  |  {label}"
            draw.rectangle([(0, 0), (len(stamp) * 7 + 8, 18)], fill=(0, 0, 0, 200))
            draw.text((4, 2), stamp, fill=(255, 255, 255))
            img.save(out_path)
        except Exception:
            pass

    print(f"[OK   ] screenshot → {out_path}")
    return out_path


# ── Config patcher ────────────────────────────────────────────────
# Minimum number of valid tickers a scrape must return before we trust it.
# A login/redirect page produces very few tokens that pass validation; real
# scan pages return dozens.  Set to 5 as a conservative floor.
_MIN_SCRAPE_TICKERS = 5


def _patch_config(list_name: str, new_tickers: list[str]) -> int:
    """
    Add *new_tickers* to data/universe.json (TTL-managed) instead of patching
    config.py source code.  list_name determines the tier:
      PRIORITY_1_MOMENTUM   → tier 1 (TTL 14 days)
      PRIORITY_2_ESTABLISHED → tier 2 (TTL 30 days)
    Returns the number of *new* tickers inserted.

    Applies the full _is_valid_ti_ticker filter + a minimum-count guard so
    login-page scrapes (no TI session) don't pollute universe.json.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT))
    from engine.equity.universe import add_tickers  # noqa: E402

    # Apply the same validation used in _apply_tradeideas_results so both write
    # paths (in-memory list and universe.json) are consistent.
    clean = [t for t in new_tickers if _is_valid_ti_ticker(t)]

    if len(clean) < _MIN_SCRAPE_TICKERS:
        print(
            f"[WARN ] _patch_config({list_name}): only {len(clean)} valid ticker(s) "
            f"after filtering (need ≥{_MIN_SCRAPE_TICKERS}) — skipping write to "
            f"universe.json (possible login-page scrape or empty scan)"
        )
        return 0

    tier = 1 if "PRIORITY_1" in list_name else 2
    added = add_tickers(clean, tier=tier)
    if added:
        print(f"[UNI  ] {added} new ticker(s) added to universe.json (tier {tier}): {clean[:5]}{'…' if len(clean)>5 else ''}")
    return added


# ── High-short-float set patcher ────────────────────────────────
def _patch_high_short_float(new_tickers: list[str]) -> int:
    """
    Merge *new_tickers* into the HIGH_SHORT_FLOAT_STOCKS set in config.py.
    Returns the number of tickers added.
    """
    src = CONFIG_FILE.read_text(encoding="utf-8")

    # Extract current set members
    m = re.search(
        r'HIGH_SHORT_FLOAT_STOCKS\s*=\s*\{([^}]*)\}',
        src, re.DOTALL
    )
    if not m:
        print("[WARN ] Could not locate HIGH_SHORT_FLOAT_STOCKS in config.py — skipping")
        return 0

    existing = set(re.findall(r'"([A-Z]{1,5})"', m.group(1)))
    to_add   = [t for t in new_tickers if t not in existing]
    if not to_add:
        return 0

    new_members = sorted(existing | set(to_add))
    # Rebuild the set block (up to 6 per line for readability)
    lines = []
    chunk = []
    for ticker in new_members:
        chunk.append(f'"{ticker}"')
        if len(chunk) == 6:
            lines.append("    " + ", ".join(chunk) + ",")
            chunk = []
    if chunk:
        lines.append("    " + ", ".join(chunk) + ",")
    new_block = "HIGH_SHORT_FLOAT_STOCKS  = {\n" + "\n".join(lines) + "\n}"

    new_src = re.sub(
        r'HIGH_SHORT_FLOAT_STOCKS\s*=\s*\{[^}]*\}',
        new_block,
        src,
        flags=re.DOTALL,
    )
    CONFIG_FILE.write_text(new_src, encoding="utf-8")
    return len(to_add)


# ── Dropdown helper ──────────────────────────────────────────────
def _try_select_timeframe(driver: "webdriver.Chrome", minutes: int) -> bool:
    """
    Attempt to select 'Change Last <minutes> Min (%)' from a page dropdown.
    Returns True if successful.
    """
    target_text = f"{minutes} Min"

    # Strategy 1: native <select>
    try:
        from selenium.webdriver.support.select import Select as SeleniumSelect
        for sel_el in driver.find_elements(By.TAG_NAME, "select"):
            for opt in sel_el.find_elements(By.TAG_NAME, "option"):
                if str(minutes) in opt.text and "min" in opt.text.lower():
                    SeleniumSelect(sel_el).select_by_visible_text(opt.text)
                    print(f"[OK   ] Dropdown selected (native <select>): {opt.text}")
                    return True
    except Exception:
        pass

    # Strategy 2: React custom dropdown — click trigger then option
    try:
        trigger = WebDriverWait(driver, 6).until(
            EC.element_to_be_clickable((By.XPATH,
                "//*[contains(@class,'select') or contains(@class,'Select')"
                " or contains(@class,'dropdown') or contains(@class,'Dropdown')]"
                "[contains(normalize-space(.),'Change') or contains(normalize-space(.),'%')]"
            ))
        )
        trigger.click()
        time.sleep(1.5)
        option = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH,
                f"//*[contains(text(),'{target_text}') or contains(text(),'{target_text.lower()}')]"
            ))
        )
        print(f"[OK   ] Dropdown selected (React): {option.text}")
        option.click()
        return True
    except Exception:
        pass

    # Strategy 3: JS inject into any <select> with a matching option
    try:
        result = driver.execute_script(f"""
            var selects = document.querySelectorAll('select');
            for (var s of selects) {{
                for (var o of s.options) {{
                    if (o.text.includes('{minutes}') && o.text.toLowerCase().includes('min')) {{
                        s.value = o.value;
                        s.dispatchEvent(new Event('change', {{bubbles: true}}));
                        return o.text;
                    }}
                }}
            }}
            return null;
        """)
        if result:
            print(f"[OK   ] Dropdown selected (JS inject): {result}")
            return True
    except Exception:
        pass

    return False


def _try_select_30min(driver: "webdriver.Chrome") -> bool:
    return _try_select_timeframe(driver, 30)


def _try_select_15min(driver: "webdriver.Chrome") -> bool:
    return _try_select_timeframe(driver, 15)


def _scrape_toplists(
    driver: "webdriver.Chrome",
    select_minutes: Optional[int] = 15,
    update_config: bool = False,
) -> dict[str, list[str]]:
    """Scrape each toplist on the TIPro/toplists page and return their tickers."""
    results: dict[str, list[str]] = {}

    WebDriverWait(driver, TABLE_WAIT_SEC).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "#top-list-selector-card-div"))
    )

    if select_minutes is not None:
        found = _try_select_timeframe(driver, select_minutes)
        if not found:
            print(f"[WARN ] Could not find {select_minutes}-min dropdown — scraping current toplist view")
        else:
            time.sleep(DROPDOWN_REFRESH_SEC)

    items = driver.find_elements(By.CSS_SELECTOR, "div.top-list-setup-div")
    print(f"[INFO ] Found {len(items)} toplist items")

    for idx, item in enumerate(items, start=1):
        label = None
        try:
            label = item.find_element(By.TAG_NAME, "img").get_attribute("data-bs-original-title")
        except Exception:
            pass
        if not label:
            label = item.get_attribute("data") or f"toplist_{idx}"
        log_label = label.strip()[:60]
        print(f"[....] Selecting toplist {idx}/{len(items)}: {log_label}")

        try:
            target = driver.find_element(By.CSS_SELECTOR, "#toplist-card-div")
            driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", item)
            driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", target)
            time.sleep(0.5)
            ActionChains(driver).click_and_hold(item).pause(0.2).move_to_element(target).pause(0.4).release().perform()
        except Exception:
            try:
                img = item.find_element(By.TAG_NAME, "img")
                driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", img)
                driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", target)
                time.sleep(0.5)
                ActionChains(driver).click_and_hold(img).pause(0.2).move_to_element(target).pause(0.4).release().perform()
            except Exception:
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", item)
                    time.sleep(0.5)
                    driver.execute_script("arguments[0].click();", item)
                except Exception:
                    try:
                        img = item.find_element(By.TAG_NAME, "img")
                        driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", img)
                        time.sleep(0.5)
                        driver.execute_script("arguments[0].click();", img)
                    except Exception as exc:
                        print(f"[WARN ] Unable to click toplist {log_label}: {exc}")
                        continue

        time.sleep(RENDER_GRACE_SEC + 1)

        rows = driver.execute_script("""
            var card = document.querySelector('#toplistGridCard');
            if (!card) return [];
            var rows = card.querySelectorAll('table.top-list-table tbody tr');
            return Array.from(rows).map(function(r){ return r.innerText || ''; });
        """) or []
        tickers: list[str] = []
        for row in rows:
            m = _TICKER_RE.match(row.strip())
            if m:
                tickers.append(m.group(1))

        tickers = [t for t in dict.fromkeys(tickers) if _is_valid_ti_ticker(t)]
        slug = re.sub(r'[^A-Za-z0-9]+', '_', label.strip().lower()).strip('_')
        if not slug:
            slug = f"toplist_{idx}"
        results[f"toplists_{slug}"] = tickers
        print(f"[OK   ] {log_label}: {len(tickers)} tickers — {tickers[:10]}{'…' if len(tickers)>10 else ''}")

        if update_config and tickers:
            added = _patch_config("PRIORITY_1_MOMENTUM", tickers)
            if added:
                print(f"[OK   ] universe.json: +{added} {log_label} tickers → tier 1")

    return results


_MOMENTUM_SCANNER_RACES = (
    "Relative Volume",
    "FANG and Friends",
    "SP500 Moving Up",
    "SP500 Moving Down",
)


def _scrape_momentum_scanner_races(driver: "webdriver.Edge") -> dict[str, list[str]]:
    """Drag requested Stock Race tiles into the Momentum Scanner and capture each result."""
    WebDriverWait(driver, TABLE_WAIT_SEC).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "#race-selector-card-div"))
    )
    results: dict[str, list[str]] = {}

    for label in _MOMENTUM_SCANNER_RACES:
        selector = (
            ".race-setup-div img[data-bs-original-title="
            f"'{label}'"
            "]"
        )
        try:
            source = WebDriverWait(driver, TABLE_WAIT_SEC).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            target = driver.find_element(By.CSS_SELECTOR, "#ssw-race-div-1")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", source)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
            ActionChains(driver).click_and_hold(source).pause(0.2).move_to_element(target).pause(0.4).release().perform()
        except Exception as exc:
            print(f"[WARN ] Unable to select Momentum Scanner race {label}: {exc}")
            continue

        time.sleep(RENDER_GRACE_SEC + 1)
        race_text = target.text
        tickers = [
            match.group(1)
            for match in _TICKER_RE.finditer(race_text)
            if _is_valid_ti_ticker(match.group(1))
        ]
        tickers = list(dict.fromkeys(tickers))
        key = f"momentum_{re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')}"
        results[key] = tickers
        print(f"[OK   ] {label}: {len(tickers)} tickers — {tickers[:10]}{'…' if len(tickers) > 10 else ''}")

    return results


# ── Stock Race Central: leaders vs laggards extraction ───────────
def _extract_race_sides(driver: "webdriver.Chrome") -> tuple[list[str], list[str]]:
    """
    Try to split stockracecentral tickers into:
      leaders  — top/green tiles (long candidates)  → tier 1
      laggards — bottom/red tiles (short candidates) → tier 2

    Strategy:
      1. Look for elements with positive vs negative change values
         (e.g. '+5.2%' vs '-3.1%') alongside ticker symbols.
      2. Fallback: read DOM order — first half = leaders, second half = laggards.
      3. If no split is possible, return (all, []) so everything goes to tier 1.
    """
    leaders:  list[str] = []
    laggards: list[str] = []

    try:
        result = driver.execute_script("""
            var leaders = [];
            var laggards = [];
            var seen = {};

            // Walk all elements looking for ticker+change pairs
            var allEls = document.querySelectorAll(
                '[class*="tile"],[class*="card"],[class*="row"],[class*="item"],[class*="stock"],[class*="race"]'
            );

            allEls.forEach(function(el) {
                var text = (el.innerText || el.textContent || '').trim();
                var tickerM = text.match(/\\b([A-Z]{2,5})\\b/);
                if (!tickerM) return;
                var ticker = tickerM[1];
                if (seen[ticker]) return;

                // Look for pct change in the element or its parent
                var combined = text + ' ' + (el.parentElement ? (el.parentElement.innerText || '') : '');
                var posM = combined.match(/\\+([0-9]+\\.?[0-9]*)%/);
                var negM = combined.match(/-([0-9]+\\.?[0-9]*)%/);

                // Also check for green/red background color hints
                var style = window.getComputedStyle(el);
                var bg = style.backgroundColor || '';
                // rgb(r,g,b) — green dominates if g>r and g>b, red if r>g
                var isGreen = bg.match(/rgb\\((\\d+),(\\d+),(\\d+)\\)/) &&
                              (function(m){ return parseInt(m[2])>parseInt(m[1]) && parseInt(m[2])>parseInt(m[3]); })
                              (bg.match(/rgb\\((\\d+),(\\d+),(\\d+)\\)/));
                var isRed   = bg.match(/rgb\\((\\d+),(\\d+),(\\d+)\\)/) &&
                              (function(m){ return parseInt(m[1])>parseInt(m[2]) && parseInt(m[1])>parseInt(m[3]); })
                              (bg.match(/rgb\\((\\d+),(\\d+),(\\d+)\\)/));

                seen[ticker] = true;
                if (negM && !posM || isRed)  { laggards.push(ticker); }
                else                          { leaders.push(ticker);  }
            });

            return {leaders: leaders, laggards: laggards};
        """) or {}

        leaders  = [t for t in (result.get("leaders", [])  or []) if t not in _IGNORE]
        laggards = [t for t in (result.get("laggards", []) or []) if t not in _IGNORE]
    except Exception:
        pass

    # Fallback: use standard extraction then split by DOM order
    if not leaders and not laggards:
        all_tickers = _extract_tickers(driver)
        mid = max(1, len(all_tickers) // 2)
        leaders  = all_tickers[:mid]
        laggards = all_tickers[mid:]
        print("[INFO ] Race side detection fell back to DOM-order split")

    # De-dup each list
    def _dedup(lst: list[str]) -> list[str]:
        seen: set[str] = set()
        return [t for t in lst if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]

    return _dedup(leaders[:25]), _dedup(laggards[:25])


# ── Main scrape function ──────────────────────────────────────────
def scrape_tradeideas(
    update_config: bool = False,
    headless: bool = False,
    chrome_profile: Optional[str] = None,
    select_minutes: Optional[int] = None,
    include_toplists: bool = False,
    scan_keys: Optional[list[str]] = None,
    select_30min: bool = False,
    browser: str = "edge",  # kept for signature compatibility; Edge is always used
    remote_debug_port: int = _REMOTE_DEBUG_PORT,
) -> dict[str, list[str]]:
    """
    Scrape Trade Ideas scan pages using a persistent Edge window.
    The browser stays open across calls so the TI login session is preserved.
    On the first run (or after a crash) the script tries to re-attach to an
    already-running Edge on *remote_debug_port* before opening a new window.
    If select_minutes is set, attempts to pick 'Change Last N Min (%)'
    from the heatmap dropdown before extracting tickers.
    Returns {scan_key: [tickers, …]}.
    """
    if not SELENIUM_OK:
        raise ImportError(
            "selenium / webdriver-manager not installed. "
            "Install packages: pip install selenium webdriver-manager pillow"
        )

    results: dict[str, list[str]] = {}
    # Reuse the persistent Edge window; re-attach if already running, else open new.
    driver = _get_driver(chrome_profile=chrome_profile, remote_debug_port=remote_debug_port)

    # Watchdog: if the scrape hangs for > 90 s, null out the driver singleton
    # and let the next cycle start fresh.  We don't kill Edge — the user may
    # have manually navigated away; just mark it dead so _get_driver re-opens.
    import threading as _ti_thread, subprocess as _ti_sp, sys as _ti_sys
    _scrape_done = _ti_thread.Event()

    def _hard_kill():
        global _edge_driver
        if _scrape_done.wait(90):
            return  # finished cleanly — nothing to do
        print("[WARN ] TI scrape hard-timeout (90 s) — marking Edge session dead")
        _edge_driver = None  # force re-open on next cycle

    _killer = _ti_thread.Thread(target=_hard_kill, daemon=True)
    _killer.start()

    if select_minutes is None:
        select_minutes = 15
    if select_30min:
        select_minutes = 30

    try:
        if scan_keys is not None:
            scan_keys = set(scan_keys)

        for scan_key, scan in SCANS.items():
            if scan_keys is not None and scan_key not in scan_keys:
                continue
            if scan_key == "toplists" and not include_toplists:
                continue

            url   = scan["url"]
            label = scan["label"]

            print(f"\n[....] Loading {url}")
            try:
                driver.get(url)
            except TimeoutException:
                print(f"[WARN ] Page load timeout for {scan_key}; continuing with partial DOM")

            # Wait for body/div to appear
            for sel in ["body", "div"]:
                try:
                    WebDriverWait(driver, TABLE_WAIT_SEC).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                    )
                    break
                except Exception:
                    continue

            # Short grace period for React heatmap to render.
            time.sleep(RENDER_GRACE_SEC)

            if scan.get("interaction") == "drag_drop":
                local_select_minutes = 15 if select_minutes is None else select_minutes
                toplist_results = _scrape_toplists(
                    driver,
                    select_minutes=local_select_minutes,
                    update_config=update_config,
                )
                results.update(toplist_results)
                tickers = [t for lst in toplist_results.values() for t in lst]
            elif scan.get("interaction") == "stock_races":
                race_results = _scrape_momentum_scanner_races(driver)
                results.update(race_results)
                tickers = [t for lst in race_results.values() for t in lst]
            else:
                # Optionally select a timeframe dropdown before scraping.
                if select_minutes is not None:
                    found = _try_select_timeframe(driver, select_minutes)
                    if not found:
                        print(f"[WARN ] Could not find {select_minutes}-min dropdown — scraping current view")
                    else:
                        time.sleep(DROPDOWN_REFRESH_SEC)

                tickers = _extract_tickers(driver)
                results[scan_key] = tickers
            print(f"[OK   ] {scan_key}: {len(tickers)} tickers — {tickers[:10]}{'…' if len(tickers)>10 else ''}")

            if scan["target"] == "BOTH":
                # stockracecentral: split leaders (tier 1) vs laggards (tier 2)
                leaders, laggards = _extract_race_sides(driver)
                results[f"{scan_key}_leaders"]  = leaders
                results[f"{scan_key}_laggards"] = laggards
                print(f"[OK   ] {scan_key} leaders  ({len(leaders)}):  {leaders[:10]}{'…' if len(leaders)>10 else ''}")
                print(f"[OK   ] {scan_key} laggards ({len(laggards)}): {laggards[:10]}{'…' if len(laggards)>10 else ''}")
                if update_config:
                    if leaders:
                        added = _patch_config("PRIORITY_1_MOMENTUM", leaders)
                        print(f"[OK   ] universe.json: +{added} leader(s) → tier 1 (long candidates)")
                    if laggards:
                        added = _patch_config("PRIORITY_2_ESTABLISHED", laggards)
                        print(f"[OK   ] universe.json: +{added} laggard(s) → tier 2 (short candidates)")
            elif update_config and tickers:
                added = _patch_config(scan["target"], tickers)
                if added:
                    print(f"[OK   ] universe.json: +{added} new tickers added to tier {1 if 'PRIORITY_1' in scan['target'] else 2}")
                else:
                    print(f"[INFO ] universe.json: all tickers already present")
                # HSF tickers are persisted in universe.json tier-2, NOT config.py
                # (_patch_high_short_float rewrites config.py — disabled to prevent
                # continuous source-file modifications during live trading)

            # ── Persist unusual options volume tickers for the live options engine ──
            if scan_key == "unusualoptionsvolume" and tickers:
                clean_opts = [t for t in tickers if _is_valid_ti_ticker(t)]
                if len(clean_opts) >= _MIN_SCRAPE_TICKERS:
                    import json as _json, datetime as _dt
                    _data = {
                        "updated": _dt.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "tickers": clean_opts,
                    }
                    TI_UNUSUAL_OPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
                    TI_UNUSUAL_OPTIONS_FILE.write_text(
                        _json.dumps(_data, indent=2), encoding="utf-8"
                    )
                    print(f"[OK   ] ti_unusual_options.json updated: {clean_opts[:10]}{'…' if len(clean_opts)>10 else ''}")
                else:
                    print(f"[WARN ] Unusual options scrape too sparse ({len(clean_opts)}) — ti_unusual_options.json not updated")

            # Navigate away so the tab goes blank
            try:
                driver.get("about:blank")
            except Exception:
                pass

    finally:
        _scrape_done.set()  # signal the watchdog: scrape finished normally

    # Persist the latest captured TI universe as the primary scan source.
    all_tickers: list[str] = []
    for tickers in results.values():
        all_tickers.extend(tickers)
    clean_primary = [t for t in dict.fromkeys(all_tickers) if _is_valid_ti_ticker(t)]
    if len(clean_primary) >= _MIN_SCRAPE_TICKERS:
        try:
            import json as _json, datetime as _dt
            _data = {
                "updated": _dt.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "tickers": clean_primary,
            }
            TI_PRIMARY_FILE.parent.mkdir(parents=True, exist_ok=True)
            TI_PRIMARY_FILE.write_text(_json.dumps(_data, indent=2), encoding="utf-8")
            print(f"[OK   ] ti_primary.json updated: {clean_primary[:10]}{'…' if len(clean_primary)>10 else ''}")

            if update_config:
                try:
                    import sys as _sys
                    _sys.path.insert(0, str(REPO_ROOT))
                    from engine.equity.universe import add_tickers  # noqa: E402
                    added = add_tickers(clean_primary, tier=1)
                    if added:
                        print(f"[OK   ] universe.json mirrored {added} latest TI primary tickers as tier 1")
                except Exception as exc:
                    print(f"[WARN ] universe.json mirror failed: {exc}")
        except Exception as exc:
            print(f"[WARN ] ti_primary.json update failed: {exc}")
    else:
        print(f"[WARN ] ti_primary.json not updated: only {len(clean_primary)} valid tickers")

    print("[OK   ] Scrape done. Edge window stays open.")

    return results


# ── CLI ───────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture Trade Ideas scans and optionally update the stock universe"
    )
    parser.add_argument(
        "--update-config", action="store_true",
        help="Persist newly discovered tickers into data/universe.json (TTL-managed)",
    )
    parser.add_argument(
        "--loop", type=int, metavar="SECONDS", default=0,
        help="Repeat every N seconds (0 = single shot, default)",
    )
    parser.add_argument(
        "--chrome-profile", metavar="PROFILE", default=None,
        help='Use an existing Edge profile, e.g. "Default" (keeps TI login session)',
    )
    parser.add_argument(
        "--toplists", dest="include_toplists", action="store_true",
        help="Scrape the TIPro/toplists page in addition to the built-in scans",
    )
    parser.add_argument(
        "--30min", dest="select_30min", action="store_true",
        help="Select 'Change Last 30 Min (%%)' dropdown on each page before scraping",
    )
    parser.add_argument(
        "--15min", dest="select_15min", action="store_true",
        help="Select 'Change Last 15 Min (%%)' dropdown on each page before scraping",
    )
    parser.add_argument(
        "--remote-debug-port", dest="remote_debug_port",
        type=int, default=_REMOTE_DEBUG_PORT,
        metavar="PORT",
        help=(
            f"CDP remote-debugging port (default {_REMOTE_DEBUG_PORT}).  "
            "On the first run a new Edge window is opened on this port so that "
            "subsequent runs can re-attach to the same session (preserving TI login). "
            "Set to 0 to disable."
        ),
    )
    args = parser.parse_args()

    if args.select_15min and args.select_30min:
        print("[WARN ] Both --15min and --30min were passed; using --15min.")

    select_minutes = None
    if args.select_15min:
        select_minutes = 15
    elif args.select_30min:
        select_minutes = 30

    if args.loop > 0:
        print(f"[INFO ] Loop mode — capturing every {args.loop}s. Ctrl+C to stop.")
        while True:
            scrape_tradeideas(
                update_config=args.update_config,
                chrome_profile=args.chrome_profile,
                select_minutes=select_minutes,
                include_toplists=args.include_toplists,
                remote_debug_port=args.remote_debug_port,
            )
            print(f"[INFO ] Sleeping {args.loop}s …")
            time.sleep(args.loop)
    else:
        scrape_tradeideas(
            update_config=args.update_config,
            chrome_profile=args.chrome_profile,
            select_minutes=select_minutes,
            include_toplists=args.include_toplists,
            remote_debug_port=args.remote_debug_port,
        )


if __name__ == "__main__":
    main()

