"""
ApexTrader — Discovery
Manages live trending-stock scans and Trade Ideas universe refresh.
Extracted from main.py to keep the main entry point lean.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from engine.utils import (
    MarketState,
    get_bars,
    get_premarket_bars,
    get_trending_tickers,
    filter_trending_momentum,
    check_sentiment_gate,
)
from engine.config import PRIORITY_1_MOMENTUM as _P1, PRIORITY_2_ESTABLISHED as _P2
from engine.ti.ti import get_scans, is_valid_ti_ticker, scrape_tradeideas

log = logging.getLogger("ApexTrader")

REPO_ROOT = Path(__file__).resolve().parent.parent
TRADEIDEAS_ALL_SCAN_KEYS = [
    "marketscope360",
    "highshortfloat",
    "momentumscanner",
    "unusualoptionsvolume",
    "toplists",
]

# ── Module-level state ─────────────────────────────────────────────────────
trending_stocks:           List[Dict] = []
last_trending_scan:        float      = 0.0
# Tickers discovered via trending feeds — kept separately so we never mutate
# the imported config list (PRIORITY_1_MOMENTUM is a module-level constant).
_discovered_trending:      List[str]  = []
last_ti_scan:              float      = 0.0
last_ti_options_scan:      float      = 0.0
last_ti_toplists_scan:     float      = 0.0
last_sympathy_scan:        float      = 0.0
last_edgar_scan:           float      = 0.0
# Sympathy + EDGAR tickers queued for the NEXT scan cycle.
# get_scan_targets() pops these to the front so they are guaranteed to be scanned.
_priority_scan_queue:      List[str]  = []
last_alpaca_mover_scan:    float      = 0.0
_ti_started_at:            float      = 0.0
_ti_warned_running:        bool       = False
_ti_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
_ti_future: Optional[concurrent.futures.Future] = None


def get_priority_scan_queue() -> List[str]:
    """Return the current sympathy/EDGAR/screener tickers (read-only peek).

    Does NOT drain the queue — symbols remain until the next screener/discovery
    refresh replaces them, so both equity and options scans see the same set.
    """
    return list(_priority_scan_queue)


@dataclass
class PreopenSignalProvider:
    name: str
    apply: Callable[["PreopenIntelligenceScanner", dict[str, dict], MarketState, list, list, int, "PreopenSignalProvider"], None]
    weight: float = 1.0
    active: bool = True
    description: str = ""


class PreopenIntelligenceScanner:
    def __init__(self) -> None:
        self.last_scan: float = 0.0
        self.watchlist: List[dict] = []
        self.providers: List[PreopenSignalProvider] = []
        self.use_regime_gating: bool = True
        self.use_sentiment_gating: bool = True
        self.signal_performance: Dict[str, Dict[str, float]] = {}
        self._register_default_providers()

    def get_watchlist(self) -> List[str]:
        return [item["symbol"] for item in self.watchlist]

    def register_provider(self, provider: PreopenSignalProvider) -> None:
        self.providers.append(provider)

    def clear_providers(self) -> None:
        self.providers.clear()

    def set_providers(self, providers: List[PreopenSignalProvider]) -> None:
        self.providers = list(providers)

    def get_provider_metrics(self) -> Dict[str, Dict[str, float]]:
        return {name: dict(metrics) for name, metrics in self.signal_performance.items()}

    def _get_provider_weight(self, name: str) -> float:
        for provider in self.providers:
            if provider.name == name:
                return provider.weight
        return 1.0

    def adjust_provider_weights(self) -> None:
        for provider in self.providers:
            metrics = self.signal_performance.get(provider.name, {})
            hits = metrics.get("hits", 0.0)
            contributions = metrics.get("contributions", 1.0)
            if contributions > 0:
                provider.weight = max(0.25, min(2.0, hits / contributions if contributions else 1.0))

    def record_provider_outcome(self, provider_name: str, symbol: str, success: bool) -> None:
        perf = self.signal_performance.setdefault(provider_name, {"runs": 0.0, "contributions": 0.0, "hits": 0.0})
        perf["runs"] += 1.0
        if success:
            perf["hits"] += 1.0

    def scan(
        self,
        *,
        enabled: bool,
        interval_min: float,
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int = 20,
        use_regime_gating: bool = True,
        use_sentiment_gating: bool = True,
    ) -> None:
        """Build a scored pre-open watchlist and inject high-priority tickers.

        This is meant for closed-market / pre-open cycles where the bot can
        proactively gather event-driven ideas and churn signals before the next
        live scan.
        """
        if not enabled:
            return

        self.use_regime_gating = use_regime_gating
        self.use_sentiment_gating = use_sentiment_gating

        now = time.time()
        if (now - self.last_scan) < (interval_min * 60):
            return
        self.last_scan = now

        if market_state.is_market_open:
            return

        scores: dict[str, dict] = {}
        self._run_providers(scores, market_state, priority_1, priority_2, max_watchlist)
        self.watchlist = self._build_watchlist(scores, max_watchlist)

        self._inject_watchlist_to_priority_queue(priority_1, priority_2)
        self._log_watchlist_summary(market_state)

    def _register_default_providers(self) -> None:
        self.providers = [
            PreopenSignalProvider(
                name="priority_queue",
                apply=self._provider_priority_queue,
                description="Existing high-priority tickers from sympathy/EDGAR/scan backlog",
            ),
            PreopenSignalProvider(
                name="universe_seed",
                apply=self._provider_universe_seed,
                description="Fallback universe seeds for robustness and coverage",
            ),
            PreopenSignalProvider(
                name="sentiment",
                apply=self._provider_sentiment,
                description="News sentiment gate and regime-aligned signal boost",
            ),
            PreopenSignalProvider(
                name="premarket",
                apply=self._provider_premarket,
                description="Pre-market gap and volume scoring for overnight churn",
            ),
            PreopenSignalProvider(
                name="sa_day_watch",
                apply=self._provider_sa_day_watch,
                description="SA v2 day-watch market movers and leading stories",
            ),
            PreopenSignalProvider(
                name="yh_screener",
                apply=self._provider_yh_screener,
                description="Yahoo Finance15 day gainers, losers, most-active screener",
            ),
            PreopenSignalProvider(
                name="yh_news",
                apply=self._provider_yh_news,
                description="Yahoo Finance15 news ticker extraction",
            ),
            PreopenSignalProvider(
                name="mda_movers",
                apply=self._provider_mda_movers,
                description="Market Data App bulk quotes: detect pre-market gappers and high-volume movers",
            ),
            PreopenSignalProvider(
                name="mboum_analyst",
                apply=self._provider_mboum_analyst,
                description="Mboum analyst consensus: boost tickers with strong Buy ratings and rising price targets",
            ),
        ]

    def _run_providers(
        self,
        scores: dict[str, dict],
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int,
    ) -> None:
        for provider in self.providers:
            if not provider.active:
                continue
            try:
                provider.apply(scores, market_state, priority_1, priority_2, max_watchlist, provider)
            except Exception as exc:
                log.warning(f"[PREOPEN] provider failed: {provider.name} -> {exc}")

    def _add_candidate(
        self,
        scores: dict[str, dict],
        symbol: str,
        weight: float,
        reason: str,
        provider_name: Optional[str] = None,
    ) -> None:
        if not is_valid_ti_ticker(symbol):
            return
        if symbol not in scores:
            scores[symbol] = {
                "symbol": symbol,
                "score": 0.0,
                "reasons": set(),
                "source_weight": 0.0,
            }
        scores[symbol]["score"] += weight
        scores[symbol]["reasons"].add(reason)
        if provider_name is not None:
            perf = self.signal_performance.setdefault(
                provider_name,
                {"runs": 0.0, "contributions": 0.0, "hits": 0.0},
            )
            perf["contributions"] += abs(weight)

    def _provider_priority_queue(
        self,
        scores: dict[str, dict],
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int,
        provider: PreopenSignalProvider,
    ) -> None:
        for sym in _priority_scan_queue:
            self._add_candidate(scores, sym, 1.5 * provider.weight, "priority-queue", provider_name=provider.name)

    def _provider_universe_seed(
        self,
        scores: dict[str, dict],
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int,
        provider: PreopenSignalProvider,
    ) -> None:
        for sym in priority_1 + priority_2:
            if sym in scores:
                continue
            if len(scores) >= max_watchlist * 3:
                break
            self._add_candidate(scores, sym, 0.1 * provider.weight, "universe-seed", provider_name=provider.name)

    def _provider_sentiment(
        self,
        scores: dict[str, dict],
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int,
        provider: PreopenSignalProvider,
    ) -> None:
        sentiment_cache: dict[str, tuple[bool, float]] = {}

        for symbol in list(scores.keys()):
            if symbol in sentiment_cache:
                allow, bullish_pct = sentiment_cache[symbol]
            else:
                allow, bullish_pct = check_sentiment_gate(symbol)
                sentiment_cache[symbol] = (allow, bullish_pct)

            if allow:
                self._add_candidate(scores, symbol, 0.25 * provider.weight, "news-bullish", provider_name=provider.name)
                if market_state.resolve_sentiment() == "bull":
                    self._add_candidate(scores, symbol, 0.1 * provider.weight, "sentiment-aligned", provider_name=provider.name)
            else:
                penalty = 0.4 if self.use_sentiment_gating else 0.15
                self._add_candidate(
                    scores,
                    symbol,
                    -penalty * provider.weight,
                    "sentiment-block" if self.use_sentiment_gating else "news-bearish",
                    provider_name=provider.name,
                )
                if self.use_sentiment_gating and market_state.resolve_sentiment() == "bear":
                    self._add_candidate(scores, symbol, -0.1 * provider.weight, "bearish-regime-penalty", provider_name=provider.name)

    def _provider_premarket(
        self,
        scores: dict[str, dict],
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int,
        provider: PreopenSignalProvider,
    ) -> None:
        ranked = sorted(scores.values(), key=lambda d: d["score"], reverse=True)
        premkt_candidates = [item["symbol"] for item in ranked[: max_watchlist * 2]]
        for symbol in premkt_candidates:
            self._score_premarket(symbol, scores, provider)

    def _score_premarket(self, symbol: str, scores: dict[str, dict], provider: PreopenSignalProvider) -> None:
        try:
            pm = get_premarket_bars(symbol)
            if pm.empty:
                return
            t = pm["time"]
            if t.dt.tz is None:
                t = t.dt.tz_localize(datetime.timezone.utc).dt.tz_convert("America/New_York")
            else:
                t = t.dt.tz_convert("America/New_York")
            premkt = pm[(t.dt.hour < 9) | ((t.dt.hour == 9) & (t.dt.minute < 30))]
            if premkt.empty:
                return

            daily = get_bars(symbol, "2d", "1d")
            if daily.empty or len(daily) < 2:
                return

            prev_close = float(daily["close"].iloc[-2])
            last_price = float(premkt["close"].iloc[-1])
            gap_pct = ((last_price - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0
            pm_volume = float(premkt["volume"].sum())
            avg_daily_vol = float(daily["volume"].iloc[-2:-1].mean()) if len(daily) >= 2 else 0.0
            pm_vol_pct = (pm_volume / max(avg_daily_vol, 1.0)) * 100

            if gap_pct >= 1.0 and pm_vol_pct >= 10.0:
                self._add_candidate(scores, symbol, 0.5 * provider.weight, f"pre-market gap +{gap_pct:.1f}%", provider_name=provider.name)
            elif gap_pct >= 0.5 and pm_vol_pct >= 5.0:
                self._add_candidate(scores, symbol, 0.25 * provider.weight, f"pre-market gap +{gap_pct:.1f}%", provider_name=provider.name)
            if pm_vol_pct >= 15.0:
                self._add_candidate(scores, symbol, 0.2 * provider.weight, f"PM vol {pm_vol_pct:.0f}%", provider_name=provider.name)
        except Exception:
            return

    def _build_watchlist(self, scores: dict[str, dict], max_watchlist: int) -> List[dict]:
        ranked = sorted(scores.values(), key=lambda d: d["score"], reverse=True)
        return ranked[:max_watchlist]

    def _inject_watchlist_to_priority_queue(self, priority_1: list, priority_2: list) -> None:
        added = []
        qset = set(_priority_scan_queue)
        for item in self.watchlist:
            sym = item["symbol"]
            if sym in qset or sym in priority_1 or sym in priority_2:
                continue
            _priority_scan_queue.append(sym)
            qset.add(sym)
            added.append(sym)
        if added:
            log.info(f"[PREOPEN] Injected {len(added)} pre-open watchlist tickers into priority queue")

    def _provider_sa_day_watch(
        self,
        scores: dict[str, dict],
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int,
        provider: PreopenSignalProvider,
    ) -> None:
        try:
            from engine.data.seeking_alpha import get_sa_day_watch, get_sa_leading_story, get_sa_market_outlook
            dw = get_sa_day_watch()
            if not dw:
                return

            # Boost weight in bull regime; reduce slightly in bear
            regime_mult = 1.1 if getattr(market_state, "regime", "bull") == "bull" else 0.8

            for sym in dw.get("top_gainers", [])[:10]:
                self._add_candidate(scores, sym, 1.3 * provider.weight * regime_mult,
                                    "SA-top-gainer", provider_name=provider.name)
            for sym in dw.get("sp500_gainers", [])[:8]:
                self._add_candidate(scores, sym, 1.1 * provider.weight * regime_mult,
                                    "SA-sp500-gainer", provider_name=provider.name)
            for sym in dw.get("most_active", [])[:8]:
                self._add_candidate(scores, sym, 0.9 * provider.weight * regime_mult,
                                    "SA-most-active", provider_name=provider.name)

            for sym in get_sa_leading_story()[:5]:
                self._add_candidate(scores, sym, 1.0 * provider.weight * regime_mult,
                                    "SA-leading-story", provider_name=provider.name)

            # Market outlook: log for visibility; also reduces weight of all SA signals in bear
            outlook = get_sa_market_outlook()
            if outlook:
                log.info(
                    f"[PREOPEN][SA2] market_outlook={outlook.get('sentiment','?')} "
                    f"bull={outlook.get('bullish_pct',0):.0%}"
                )
        except Exception as _e:
            log.debug(f"[PREOPEN] sa_day_watch provider error: {_e}")

    def _provider_yh_screener(
        self,
        scores: dict[str, dict],
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int,
        provider: PreopenSignalProvider,
    ) -> None:
        try:
            from engine.data.yahoo_finance15 import get_yh_day_gainers, get_yh_most_active
            regime_mult = 1.1 if getattr(market_state, "regime", "bull") == "bull" else 0.85

            for sym in get_yh_day_gainers(25)[:15]:
                self._add_candidate(scores, sym, 1.2 * provider.weight * regime_mult,
                                    "YH-gainer", provider_name=provider.name)
            for sym in get_yh_most_active(20)[:10]:
                self._add_candidate(scores, sym, 0.8 * provider.weight * regime_mult,
                                    "YH-active", provider_name=provider.name)
        except Exception as _e:
            log.debug(f"[PREOPEN] yh_screener provider error: {_e}")

    def _provider_mda_movers(
        self,
        scores: dict[str, dict],
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int,
        provider: PreopenSignalProvider,
    ) -> None:
        """Score pre-market movers using MDA bulk quotes on watchlist + P1 universe."""
        import os
        key = os.environ.get("MARKETDATA_API_KEY")
        if not key:
            return
        try:
            import requests
            candidates = list(dict.fromkeys(
                [item["symbol"] for item in sorted(scores.values(), key=lambda d: d["score"], reverse=True)[:40]]
                + priority_1[:20]
            ))
            if not candidates:
                return
            sym_str = ",".join(candidates[:50])
            r = requests.get(
                "https://api.marketdata.app/v1/stocks/bulkquotes/",
                params={"symbols": sym_str},
                headers={"Authorization": f"Bearer {key}"},
                timeout=10,
            )
            if r.status_code not in (200, 203):
                return
            data = r.json()
            if data.get("s") != "ok":
                return
            symbols = data.get("symbol", [])
            changes = data.get("changepct", [])
            volumes = data.get("volume", [])
            regime_mult = 1.1 if getattr(market_state, "regime", "bull") == "bull" else 0.85
            for sym, chg, vol in zip(symbols, changes, volumes):
                if chg is None or vol is None:
                    continue
                chg_pct = float(chg) * 100  # MDA returns as decimal
                # Strong gap up: high confidence signal
                if chg_pct >= 3.0:
                    self._add_candidate(scores, sym, 1.5 * provider.weight * regime_mult,
                                        f"MDA-gap+{chg_pct:.1f}%", provider_name=provider.name)
                elif chg_pct >= 1.5:
                    self._add_candidate(scores, sym, 0.9 * provider.weight * regime_mult,
                                        f"MDA-move+{chg_pct:.1f}%", provider_name=provider.name)
                # Gap down in bear regime = short opportunity
                elif chg_pct <= -3.0 and getattr(market_state, "regime", "bull") == "bear":
                    self._add_candidate(scores, sym, 0.7 * provider.weight,
                                        f"MDA-gap{chg_pct:.1f}%", provider_name=provider.name)
        except Exception as _e:
            log.debug(f"[PREOPEN] mda_movers provider error: {_e}")

    def _provider_mboum_analyst(
        self,
        scores: dict[str, dict],
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int,
        provider: PreopenSignalProvider,
    ) -> None:
        """Boost tickers where analyst consensus shows strong Buy rating and
        current price is meaningfully below analyst mean price target (upside)."""
        try:
            import requests, os, time
            key = "e8623e01d7msh66804a8ade71af0p120d0ejsn20ee85188848"
            h = {
                "x-rapidapi-host": "mboum-finance.p.rapidapi.com",
                "x-rapidapi-key": key,
            }
            # Check top candidates only — stay within BASIC rate limit (10 req/min)
            top_candidates = [
                item["symbol"]
                for item in sorted(scores.values(), key=lambda d: d["score"], reverse=True)[:8]
            ]
            regime_mult = 1.1 if getattr(market_state, "regime", "bull") == "bull" else 0.75
            for sym in top_candidates:
                try:
                    r = requests.get(
                        "https://mboum-finance.p.rapidapi.com/v1/markets/stock/modules",
                        params={"symbol": sym, "module": "financial-data"},
                        headers=h, timeout=8,
                    )
                    if r.status_code != 200:
                        continue
                    body = r.json().get("body", {})
                    current = float(body.get("currentPrice", 0) or 0)
                    target_mean = float(body.get("targetMeanPrice", 0) or 0)
                    rec = str(body.get("recommendationKey", "")).lower()
                    if current <= 0 or target_mean <= 0:
                        continue
                    upside_pct = (target_mean - current) / current * 100
                    # Strong buy + meaningful upside = high-conviction boost
                    if rec in ("strong_buy", "buy") and upside_pct >= 10:
                        boost = min(1.8, 0.8 + upside_pct / 50) * provider.weight * regime_mult
                        self._add_candidate(scores, sym, boost,
                                            f"analyst-{rec}-upside{upside_pct:.0f}%",
                                            provider_name=provider.name)
                        log.debug(f"[PREOPEN][MBOUM] {sym}: {rec} upside={upside_pct:.1f}% boost={boost:.2f}")
                    elif rec == "hold" and upside_pct >= 15:
                        self._add_candidate(scores, sym, 0.4 * provider.weight * regime_mult,
                                            f"analyst-hold-upside{upside_pct:.0f}%",
                                            provider_name=provider.name)
                    time.sleep(0.35)  # respect 10 req/min BASIC limit
                except Exception:
                    continue
        except Exception as _e:
            log.debug(f"[PREOPEN] mboum_analyst provider error: {_e}")

    def _provider_yh_news(
        self,
        scores: dict[str, dict],
        market_state: MarketState,
        priority_1: list,
        priority_2: list,
        max_watchlist: int,
        provider: PreopenSignalProvider,
    ) -> None:
        try:
            from engine.data.yahoo_finance15 import get_yh_news_tickers
            regime_mult = 1.0 if getattr(market_state, "regime", "bull") == "bull" else 0.8
            for sym in get_yh_news_tickers(30)[:15]:
                self._add_candidate(scores, sym, 0.9 * provider.weight * regime_mult,
                                    "YH-news", provider_name=provider.name)
        except Exception as _e:
            log.debug(f"[PREOPEN] yh_news provider error: {_e}")

    def _log_watchlist_summary(self, market_state: MarketState) -> None:
        sentiment_label = "bull" if market_state.resolve_sentiment() == "bull" else "bear"
        if not self.watchlist:
            log.info("[PREOPEN] Intelligence watchlist produced no candidates")
            return

        top_list = []
        for item in self.watchlist[:10]:
            reasons = ", ".join(sorted(item["reasons"]))
            top_list.append(f"{item['symbol']}({item['score']:.2f}:{reasons})")

        log.info(
            f"[PREOPEN] Intelligence watchlist ({sentiment_label}, "
            f"regime={market_state.regime}): top {len(self.watchlist)} "
            f"tickers → {', '.join(top_list)}"
        )

        if self.signal_performance:
            provider_stats = []
            for name, metrics in self.signal_performance.items():
                provider_stats.append(
                    f"{name}=w{self._get_provider_weight(name):.2f}/"
                    f"{int(metrics.get('hits', 0))}/{int(metrics.get('contributions', 0))}"
                )
            log.info(f"[PREOPEN] Provider performance → {', '.join(provider_stats)}")


_preopen_intelligence_scanner = PreopenIntelligenceScanner()


def get_preopen_watchlist() -> List[str]:
    """Return the current pre-open intelligence watchlist."""
    return _preopen_intelligence_scanner.get_watchlist()


def scan_preopen_intelligence(
    *,
    enabled: bool,
    interval_min: float,
    market_state: MarketState,
    priority_1: list,
    priority_2: list,
    max_watchlist: int = 20,
    use_regime_gating: bool = True,
    use_sentiment_gating: bool = True,
) -> None:
    return _preopen_intelligence_scanner.scan(
        enabled=enabled,
        interval_min=interval_min,
        market_state=market_state,
        priority_1=priority_1,
        priority_2=priority_2,
        max_watchlist=max_watchlist,
        use_regime_gating=use_regime_gating,
        use_sentiment_gating=use_sentiment_gating,
    )

# ── Trending scan ──────────────────────────────────────────────────────────

def get_discovered_trending() -> List[str]:
    """Return tickers found by trending scans this session (read-only copy)."""
    return list(_discovered_trending)


def scan_trending_stocks(
    *,
    use_live_trending: bool,
    use_finnhub: bool,
    use_sentiment_gate: bool,
    trending_max: int,
    trending_interval_min: float,
    trending_min_momentum: float,
    priority_1: list,
) -> None:
    """Refresh ``trending_stocks`` from live feeds (Finnhub, etc.).

    New tickers are stored in the module-level ``_discovered_trending`` list
    rather than mutating the caller-supplied ``priority_1`` config list.
    Callers can read discovered tickers via ``get_discovered_trending()``.
    """
    global trending_stocks, last_trending_scan

    if not use_live_trending and not use_finnhub:
        return

    now = time.time()
    if now - last_trending_scan < (trending_interval_min * 60):
        return

    from engine.utils import (
        get_trending_tickers, filter_trending_momentum,
        get_finnhub_trending_tickers, check_sentiment_gate,
    )

    try:
        log.info("[SCAN] Scanning for live trending stocks…")
        all_tickers: List[str] = []

        if use_live_trending:
            tickers = get_trending_tickers(trending_max)
            if tickers:
                all_tickers.extend(tickers)

        if use_finnhub:
            tickers = get_finnhub_trending_tickers()
            if tickers:
                all_tickers.extend(tickers)

        # ── SA v2: day-watch market movers + leading stories ─────────────────
        try:
            from engine.data.seeking_alpha import get_sa_day_watch, get_sa_leading_story
            dw = get_sa_day_watch()
            if dw:
                # most_active and S&P 500 gainers are the most actionable
                for key in ("most_active", "sp500_gainers", "top_gainers"):
                    all_tickers.extend(dw.get(key, []))
                log.debug(
                    f"[SA2] day_watch injected: most_active={len(dw.get('most_active',[]))}"
                    f" sp500_gainers={len(dw.get('sp500_gainers',[]))}"
                    f" top_gainers={len(dw.get('top_gainers',[]))}"
                )
            story_tickers = get_sa_leading_story()
            if story_tickers:
                all_tickers.extend(story_tickers)
                log.debug(f"[SA2] leading_story injected: {len(story_tickers)} tickers")
        except Exception as _sa2_e:
            log.debug(f"[SA2] day_watch/leading_story error: {_sa2_e}")

        unique = list(set(all_tickers))

        if not unique:
            log.info("[SCAN] No trending tickers found — using existing universe")
            trending_stocks    = [{"symbol": s, "momentum_pct": 0, "current_price": 0}
                                   for s in priority_1[:trending_max]]
            last_trending_scan = now
            return

        momentum_stocks = filter_trending_momentum(unique, trending_min_momentum)

        if not momentum_stocks:
            log.info(f"[SCAN] No trending stocks with >{trending_min_momentum}% momentum — using universe")
            trending_stocks    = [{"symbol": s, "momentum_pct": 0, "current_price": 0}
                                   for s in priority_1[:trending_max]]
            last_trending_scan = now
            return

        if use_sentiment_gate:
            filtered = []
            for stock in momentum_stocks:
                allow, bullish_pct = check_sentiment_gate(stock["symbol"])
                if allow:
                    stock["sentiment"] = bullish_pct
                    filtered.append(stock)
            momentum_stocks = filtered
            log.info(f"[SCAN] Sentiment filter: {len(filtered)} passed")

        new_stocks = [s for s in momentum_stocks if s["symbol"] not in priority_1]
        if new_stocks:
            log.info(f"[SCAN] Found {len(new_stocks)} new trending stocks: " + ", ".join(f"{s['symbol']} (+{s['momentum_pct']:.1f}% @ ${s['current_price']:.2f})" for s in new_stocks[:5]))
            # Store in module-level set — never mutate the config list
            _discovered_syms = {s for s in _discovered_trending}
            for s in new_stocks:
                if s["symbol"] not in _discovered_syms:
                    _discovered_trending.append(s["symbol"])
            log.info(f"[SCAN] Discovered trending: {len(_discovered_trending)} tickers this session")

        trending_stocks    = momentum_stocks
        last_trending_scan = now

    except Exception as e:
        log.error(f"[SCAN] Trending scan failed: {e}", exc_info=True)
        trending_stocks = [{"symbol": s, "momentum_pct": 0, "current_price": 0}
                           for s in priority_1[:trending_max]]


# ── Trade Ideas universe refresh ───────────────────────────────────────────

def _apply_tradeideas_results(results: dict, scans: dict, priority_1: list, priority_2: list) -> None:
    """Merge TI scrape results into *priority_1* / *priority_2* lists in-place."""
    from engine.config import PRIORITY_1_MOMENTUM as _P1, PRIORITY_2_ESTABLISHED as _P2

    by_dest: dict = {
        "PRIORITY_1_MOMENTUM":   [],
        "PRIORITY_2_ESTABLISHED": [],
    }

    for scan_key, tickers in results.items():
        if scan_key in scans:
            target_list_name = scans[scan_key]["target"]
            label            = scans[scan_key]["label"]
            if target_list_name == "BOTH":
                continue
        elif scan_key.endswith("_leaders"):
            target_list_name = "PRIORITY_1_MOMENTUM"
            label            = "stock_race_central_leaders"
        elif scan_key.endswith("_laggards"):
            target_list_name = "PRIORITY_2_ESTABLISHED"
            label            = "stock_race_central_laggards"
        else:
            continue

        valid = [t for t in tickers if is_valid_ti_ticker(t)]
        if len(valid) < 5:
            log.warning(
                f"Trade Ideas {label}: only {len(valid)} valid ticker(s) after filtering "
                f"(need ≥5) — likely a login-page scrape or empty scan; "
                f"skipping to preserve {target_list_name}"
            )
            continue
        by_dest[target_list_name].append((label, valid))

    PRIMARY_SLOTS   = 35
    SECONDARY_SLOTS = 50 - PRIMARY_SLOTS

    for target_list_name, sources in by_dest.items():
        if not sources:
            continue

        dest        = priority_1 if target_list_name == "PRIORITY_1_MOMENTUM" else priority_2
        existing_set = set(dest)

        if len(sources) == 1:
            merged = sources[0][1][:]
        else:
            seen: set = set()
            primary_part: List[str] = []
            for t in sources[0][1]:
                if len(primary_part) >= PRIMARY_SLOTS:
                    break
                if t not in seen:
                    primary_part.append(t)
                    seen.add(t)

            secondary_part: List[str] = []
            for _, src in sources[1:]:
                for t in src:
                    if len(secondary_part) >= SECONDARY_SLOTS:
                        break
                    if t not in seen:
                        secondary_part.append(t)
                        seen.add(t)
            merged = primary_part + secondary_part

        merged_set = set(merged)
        new_tickers = [t for t in merged if t not in existing_set]
        fresh       = [t for t in merged if t in existing_set]
        demote      = [t for t in dest  if t not in merged_set]

        dest.clear()
        dest.extend(merged[:50])
        for t in demote:
            if t not in merged_set and t not in dest:
                dest.append(t)

        labels = " + ".join(f"{s[0]}({len(s[1])})" for s in sources)
        if new_tickers:
            log.info(
                f"Trade Ideas [{target_list_name}] {labels}: "
                f"+{len(new_tickers)} new, {len(fresh)} existing → top-10: {merged[:10]}"
            )
        else:
            log.info(
                f"Trade Ideas [{target_list_name}] {labels}: "
                f"{len(fresh)} merged → top-10: {merged[:10]}"
            )
        log.info(
            f"── TI top-20 [{target_list_name}] "
            f"(primary={len(sources[0][1]) if sources else 0} "
            f"secondary={sum(len(s[1]) for s in sources[1:]) if len(sources) > 1 else 0}): "
            + ", ".join(dest[:20])
        )


def scan_tradeideas_universe(
    *,
    enabled: bool,
    scan_interval_min: float,
    headless: bool,
    chrome_profile,
    update_config: bool,
    priority_1: list,
    priority_2: list,
    browser: str = "edge",
    remote_debug_port: int = 9222,
) -> None:
    """Submit or check a background TI scrape for all configured Trade Ideas pages."""
    global last_ti_scan, _ti_future, _ti_started_at, _ti_warned_running

    if not enabled:
        return

    try:
        SCANS = get_scans()
    except ImportError as e:
        log.warning(f"Trade Ideas scraper unavailable (selenium not installed?): {e}")
        last_ti_scan = time.time()
        return

    now = time.time()

    # 1) Apply finished results.
    if _ti_future is not None and _ti_future.done():
        try:
            results = _ti_future.result()
            _apply_tradeideas_results(results, SCANS, priority_1, priority_2)
        except Exception as e:
            log.error(f"Trade Ideas scan failed: {e}")
        finally:
            _ti_future         = None
            _ti_warned_running = False
            last_ti_scan       = now

    # 2) If still running, back off with escalating timeouts.
    if _ti_future is not None:
        elapsed = now - _ti_started_at
        if elapsed > 180:
            log.error(
                f"Trade Ideas scrape hard-timeout ({elapsed:.0f}s) — "
                "killing Chrome/chromedriver and resetting future"
            )
            import subprocess as _hk
            for _exe in ("chromedriver.exe", "chrome.exe"):
                try:
                    _hk.run(["taskkill", "/F", "/IM", _exe, "/T"],
                            capture_output=True, timeout=5)
                except Exception:
                    pass
            _ti_future         = None
            _ti_warned_running = False
            last_ti_scan       = now
            return
        if elapsed > 90 and not _ti_warned_running:
            log.warning(f"Trade Ideas scan still running ({elapsed:.0f}s) — trading loop continues")
            _ti_warned_running = True
        return

    # 3) Schedule next scrape when due.
    if (now - last_ti_scan) < (scan_interval_min * 60):
        return

    ti_profile  = (chrome_profile or "").strip() or None

    log.info(
        f"Scanning all Trade Ideas pages in background (browser=edge, profile={ti_profile or 'none'}) …"
    )
    _ti_started_at     = now
    _ti_warned_running = False
    _ti_future         = _ti_executor.submit(
        scrape_tradeideas,
        update_config=update_config,
        chrome_profile=ti_profile,
        select_minutes=15,
        include_toplists=True,
        scan_keys=TRADEIDEAS_ALL_SCAN_KEYS,
        remote_debug_port=remote_debug_port,
    )


def scan_tradeideas_unusual_options(
    *,
    enabled: bool,
    scan_interval_min: float,
    headless: bool,
    chrome_profile,
    update_config: bool,
    priority_1: list,
    priority_2: list,
    browser: str = "edge",
    remote_debug_port: int = 9222,
) -> None:
    """Submit or check a background Trade Ideas unusual options scrape."""
    global last_ti_options_scan, _ti_future, _ti_started_at, _ti_warned_running

    if not enabled:
        return

    try:
        SCANS = get_scans()
    except ImportError as e:
        log.warning(f"Trade Ideas scraper unavailable (selenium not installed?): {e}")
        last_ti_options_scan = time.time()
        return

    now = time.time()

    if _ti_future is not None and _ti_future.done():
        try:
            results = _ti_future.result()
            _apply_tradeideas_results(results, SCANS, priority_1, priority_2)
        except Exception as e:
            log.error(f"Trade Ideas unusual options scan failed: {e}")
        finally:
            _ti_future         = None
            _ti_warned_running = False
            last_ti_options_scan = now

    if _ti_future is not None:
        elapsed = now - _ti_started_at
        if elapsed > 180:
            log.error(
                f"Trade Ideas unusual options scrape hard-timeout ({elapsed:.0f}s) — "
                "killing Chrome/chromedriver and resetting future"
            )
            import subprocess as _hk
            for _exe in ("chromedriver.exe", "chrome.exe"):
                try:
                    _hk.run(["taskkill", "/F", "/IM", _exe, "/T"],
                            capture_output=True, timeout=5)
                except Exception:
                    pass
            _ti_future         = None
            _ti_warned_running = False
            last_ti_options_scan = now
            return
        if elapsed > 90 and not _ti_warned_running:
            log.warning(f"Trade Ideas unusual options scan still running ({elapsed:.0f}s) — trading loop continues")
            _ti_warned_running = True
        return

    if (now - last_ti_options_scan) < (scan_interval_min * 60):
        return

    ti_profile  = (chrome_profile or "").strip() or None

    log.info(
        f"Scanning Trade Ideas unusual options in background (browser=edge, profile={ti_profile or 'none'}) …"
    )
    _ti_started_at     = now
    _ti_warned_running = False
    _ti_future         = _ti_executor.submit(
        scrape_tradeideas,
        update_config=update_config,
        chrome_profile=ti_profile,
        select_minutes=15,
        scan_keys=["unusualoptionsvolume"],
        remote_debug_port=remote_debug_port,
    )


def scan_sympathy_and_edgar(
    *,
    sympathy_enabled: bool,
    edgar_enabled: bool,
    sympathy_interval_min: float,
    edgar_interval_min: float,
    priority_1: list,
    priority_2: list,
) -> None:
    """
    Sector sympathy + EDGAR 8-K scanner.

    Sympathy: checks leader stocks (via Finnhub quote) for gap/momentum moves
    that historically pull related peers. Triggered sympathies are injected
    into priority_1 for immediate scanning.

    EDGAR: polls the SEC 8-K ATOM feed for material filings (supply agreements,
    contract awards, acquisitions) and injects matched tickers into priority_2
    for follow-on monitoring.
    """
    global last_sympathy_scan, last_edgar_scan

    now = time.time()
    delisted: set = set()
    try:
        from engine.config import DELISTED_STOCKS
        delisted = set(DELISTED_STOCKS)
    except Exception:
        pass

    # ── Sector sympathy ───────────────────────────────────────────────────
    if sympathy_enabled and (now - last_sympathy_scan) >= (sympathy_interval_min * 60):
        try:
            from engine.data.sector_sympathy import get_active_sympathies
            sympathies = get_active_sympathies()
            if sympathies:
                p1_set = set(priority_1)
                queue_set = set(_priority_scan_queue)
                new_syms = [s for s in sympathies if s not in p1_set and s not in delisted]
                if new_syms:
                    log.info(f"[SYMPATHY] Injecting {len(new_syms)} sympathy tickers into P1 + scan queue: {new_syms}")
                    priority_1.extend(new_syms)
                    for s in new_syms:
                        if s not in queue_set:
                            _priority_scan_queue.append(s)
                            queue_set.add(s)
        except Exception as exc:
            log.debug(f"[SYMPATHY] Scan error: {exc}")
        finally:
            last_sympathy_scan = now

    # ── EDGAR 8-K feed ───────────────────────────────────────────────────
    if edgar_enabled and (now - last_edgar_scan) >= (edgar_interval_min * 60):
        try:
            from engine.data.edgar_scraper import get_edgar_triggered_tickers
            edgar_tickers = get_edgar_triggered_tickers()
            if edgar_tickers:
                p2_set = set(priority_2)
                p1_set = set(priority_1)
                queue_set = set(_priority_scan_queue)
                for sym in edgar_tickers:
                    if sym not in delisted and sym not in p2_set and sym not in p1_set:
                        log.info(f"[EDGAR] Adding {sym} to P2 + scan queue for monitoring")
                        priority_2.append(sym)
                    if sym not in delisted and sym not in queue_set:
                        _priority_scan_queue.append(sym)
                        queue_set.add(sym)
        except Exception as exc:
            log.debug(f"[EDGAR] Scan error: {exc}")
        finally:
            last_edgar_scan = now


def scan_alpaca_movers(*, interval_min: float = 10.0, market_state: MarketState) -> None:
    """Fetch Alpaca Most Actives + Market Movers and inject qualifying symbols
    into the priority scan queue.

    The endpoint resets at market open — data before 09:30 ET is from the
    previous session, so we only run during regular market hours.
    """
    global last_alpaca_mover_scan, _priority_scan_queue

    if not market_state.is_market_open:
        return

    now = time.time()
    if now - last_alpaca_mover_scan < interval_min * 60:
        return

    try:
        import engine.config as _cfg
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout
        from alpaca.data.historical.screener import ScreenerClient
        from alpaca.data.requests import MostActivesRequest, MarketMoversRequest
        from alpaca.data.enums import MostActivesBy

        _SCREENER_TIMEOUT = 15  # seconds per request

        sc = ScreenerClient(_cfg.API_KEY, _cfg.API_SECRET)

        with ThreadPoolExecutor(max_workers=1) as _pool:
            try:
                actives_resp = _pool.submit(
                    sc.get_most_actives, MostActivesRequest(by=MostActivesBy.VOLUME, top=30)
                ).result(timeout=_SCREENER_TIMEOUT)
            except _FuturesTimeout:
                log.warning("[ALPACA-MOVERS] most_actives timed out — skipping cycle")
                return

        # Build a set of symbols that cleared the trade-count floor (real participation)
        active_syms = {
            a.symbol
            for a in actives_resp.most_actives
            if int(a.trade_count) >= 10_000
        }

        with ThreadPoolExecutor(max_workers=1) as _pool:
            try:
                movers_resp = _pool.submit(
                    sc.get_market_movers, MarketMoversRequest(market_type="stocks", top=20)
                ).result(timeout=_SCREENER_TIMEOUT)
            except _FuturesTimeout:
                log.warning("[ALPACA-MOVERS] market_movers timed out — skipping cycle")
                return

        injected: List[str] = []
        queue_set = set(_priority_scan_queue)
        delisted  = set(_cfg.DELISTED_STOCKS)

        for m in movers_resp.gainers:
            sym = m.symbol
            # Structural filter: warrants/rights have > 5 chars (e.g. BZAIW, GFAIW)
            if len(sym) > 5:
                continue
            # Price band: cheap enough for options chains, not so high position-sizing breaks
            if not (0.50 <= float(m.price) <= 500.0):
                continue
            # Move band: meaningful but not a halt/binary-news situation
            if not (3.0 <= float(m.percent_change) <= 40.0):
                continue
            # Volume confirmation: must also appear in most actives
            if sym not in active_syms:
                continue
            if sym in queue_set or sym in delisted:
                continue
            _priority_scan_queue.append(sym)
            queue_set.add(sym)
            injected.append(sym)
            log.info(
                f"[ALPACA-MOVERS] Adding {sym} to P2 + scan queue for monitoring"
            )

        last_alpaca_mover_scan = now
        if injected:
            log.info(f"[ALPACA-MOVERS] {len(injected)} gainers queued for scan: {injected}")
        else:
            log.info("[ALPACA-MOVERS] No gainers passed filters this cycle")

    except Exception as exc:
        log.warning(f"[ALPACA-MOVERS] Screener fetch failed: {exc}")


def scan_tradeideas_toplists(
    *,
    enabled: bool,
    scan_interval_min: float,
    headless: bool,
    chrome_profile,
    update_config: bool,
    priority_1: list,
    priority_2: list,
    browser: str = "edge",
    remote_debug_port: int = 9222,
) -> None:
    """Submit or check a background TI toplist scrape."""
    global last_ti_toplists_scan, _ti_future, _ti_started_at, _ti_warned_running

    if not enabled:
        return

    try:
        SCANS = get_scans()
    except ImportError as e:
        log.warning(f"Trade Ideas scraper unavailable (selenium not installed?): {e}")
        last_ti_toplists_scan = time.time()
        return

    now = time.time()

    if _ti_future is not None and _ti_future.done():
        try:
            results = _ti_future.result()
            _apply_tradeideas_results(results, SCANS, priority_1, priority_2)
        except Exception as e:
            log.error(f"Trade Ideas toplists scan failed: {e}")
        finally:
            _ti_future         = None
            _ti_warned_running = False
            last_ti_toplists_scan = now

    if _ti_future is not None:
        elapsed = now - _ti_started_at
        if elapsed > 180:
            log.error(
                f"Trade Ideas toplists scrape hard-timeout ({elapsed:.0f}s) — "
                "killing Chrome/chromedriver and resetting future"
            )
            import subprocess as _hk
            for _exe in ("chromedriver.exe", "chrome.exe"):
                try:
                    _hk.run(["taskkill", "/F", "/IM", _exe, "/T"],
                            capture_output=True, timeout=5)
                except Exception:
                    pass
            _ti_future         = None
            _ti_warned_running = False
            last_ti_toplists_scan = now
            return
        if elapsed > 90 and not _ti_warned_running:
            log.warning(f"Trade Ideas toplists scan still running ({elapsed:.0f}s) — trading loop continues")
            _ti_warned_running = True
        return

    if (now - last_ti_toplists_scan) < (scan_interval_min * 60):
        return

    ti_profile  = (chrome_profile or "").strip() or None

    log.info(
        f"Scanning Trade Ideas toplists in background (browser=edge, profile={ti_profile or 'none'}) …"
    )
    _ti_started_at     = now
    _ti_warned_running = False
    _ti_future         = _ti_executor.submit(
        scrape_tradeideas,
        update_config=update_config,
        chrome_profile=ti_profile,
        select_minutes=15,
        include_toplists=True,
        scan_keys=["toplists"],
        remote_debug_port=remote_debug_port,
    )
