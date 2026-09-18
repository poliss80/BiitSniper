"""Cached Alpaca news catalyst analysis for candidate symbols."""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List

from engine.config import API_KEY, API_SECRET

log = logging.getLogger("ApexTrader")


@dataclass(frozen=True)
class NewsAnalysis:
    direction: str = "neutral"
    score: float = 0.0
    catalyst: str = "none"
    risk_flags: tuple[str, ...] = field(default_factory=tuple)
    article_count: int = 0
    latest_age_minutes: float | None = None
    source: str = "alpaca"


_CACHE: Dict[str, tuple[float, NewsAnalysis]] = {}
_CACHE_TTL_SECONDS = 300.0

_POSITIVE = {
    "contract": "contract_win",
    "agreement": "partnership",
    "partnership": "partnership",
    "fda approval": "fda_approval",
    "clinical success": "clinical_success",
    "acquisition": "acquisition",
    "merger": "acquisition",
    "guidance raised": "guidance_raise",
    "raises guidance": "guidance_raise",
    "upgrade": "analyst_upgrade",
}
_NEGATIVE = {
    "offering": "dilution",
    "public offering": "dilution",
    "direct offering": "dilution",
    "shelf registration": "dilution",
    "bankruptcy": "bankruptcy",
    "delisting": "delisting",
    "going concern": "going_concern",
    "lawsuit": "legal_risk",
    "guidance cut": "guidance_cut",
    "downgrade": "analyst_downgrade",
}


def _text(article) -> str:
    headline = getattr(article, "headline", "") or ""
    summary = getattr(article, "summary", "") or ""
    return f"{headline} {summary}".lower()


def _published(article) -> dt.datetime | None:
    value = getattr(article, "created_at", None) or getattr(article, "updated_at", None)
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def analyze_symbol_news(symbol: str, *, now: dt.datetime | None = None) -> NewsAnalysis:
    """Analyze fresh Alpaca news for one candidate; failures return neutral."""
    now = now or dt.datetime.now(dt.timezone.utc)
    key = symbol.upper()
    cached = _CACHE.get(key)
    if cached and dt.datetime.now(dt.timezone.utc).timestamp() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    if not API_KEY or not API_SECRET:
        return NewsAnalysis()
    try:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest

        request = NewsRequest(
            symbols=key,
            start=now - dt.timedelta(hours=48),
            end=now,
            sort="desc",
            limit=20,
            include_content=False,
            exclude_contentless=True,
        )
        response = NewsClient(API_KEY, API_SECRET).get_news(request)
        response_data = getattr(response, "data", {}) if hasattr(response, "data") else {}
        articles = list(response_data.get(key, [])) if isinstance(response_data, dict) else []
        if not articles and isinstance(response, dict):
            articles = response.get("news", [])
        articles = [a for a in articles if _published(a) is not None]
        if not articles:
            result = NewsAnalysis()
            _CACHE[key] = (dt.datetime.now(dt.timezone.utc).timestamp(), result)
            return result

        positive = []
        negative = []
        ages = []
        for article in articles:
            published = _published(article)
            ages.append(max(0.0, (now - published).total_seconds() / 60.0))
            body = _text(article)
            for phrase, catalyst in _POSITIVE.items():
                if phrase in body:
                    positive.append(catalyst)
                    break
            for phrase, risk in _NEGATIVE.items():
                if phrase in body:
                    negative.append(risk)
                    break

        if negative:
            result = NewsAnalysis("negative", -0.08, negative[0], tuple(sorted(set(negative))), len(articles), min(ages))
        elif positive:
            result = NewsAnalysis("positive", min(0.08, 0.03 + 0.01 * (len(set(positive)) - 1)), positive[0], tuple(), len(articles), min(ages))
        else:
            result = NewsAnalysis("neutral", 0.0, "unclassified", tuple(), len(articles), min(ages))
        _CACHE[key] = (dt.datetime.now(dt.timezone.utc).timestamp(), result)
        return result
    except Exception as error:
        log.debug(f"Alpaca news unavailable for {key}: {error}")
        result = NewsAnalysis()
        _CACHE[key] = (dt.datetime.now(dt.timezone.utc).timestamp(), result)
        return result


def annotate_signal_with_news(signal):
    """Attach news metadata and a small confidence adjustment to a candidate."""
    analysis = analyze_symbol_news(signal.symbol)
    signal.news_direction = analysis.direction
    signal.news_score = analysis.score
    signal.news_catalyst = analysis.catalyst
    signal.news_risk_flags = analysis.risk_flags
    if analysis.direction == "negative" and analysis.risk_flags:
        signal.confidence = max(0.0, float(signal.confidence) + analysis.score)
    elif analysis.direction == "positive":
        signal.confidence = min(0.99, float(signal.confidence) + analysis.score)
    return signal
