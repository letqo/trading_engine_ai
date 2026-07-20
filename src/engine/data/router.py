"""Tags raw news headlines with topics and routes them to universe symbols.

RSS headlines don't arrive pre-tagged, so topic extraction here is a simple,
auditable keyword matcher -- deliberately dumb (v1 explicitly excludes ML
beyond off-the-shelf sentiment). Each topic keyword must exist as a
news_topics tag on at least one universe.yaml instrument, or it can never
route anywhere.
"""

from __future__ import annotations

import re

from engine.data.universe import Universe
from engine.domain import NewsItem

# keyword/phrase -> topic tag. Matched case-insensitively against the
# headline. Extend this as new topics are needed; it is the only place
# free-text news gets mapped onto the fixed topic vocabulary in universe.yaml.
KEYWORD_TOPICS: dict[str, str] = {
    r"\bfed\b|federal reserve|\bfomc\b|powell": "fed",
    r"\bcpi\b|inflation report|consumer price index": "cpi",
    r"\bboj\b|bank of japan": "boj",
    r"\becb\b|european central bank": "ecb",
    r"\bpboc\b|people's bank of china|china stimulus": "pboc",
    r"\bopec\b|crude oil|\beia\b|oil inventories": "oil",
    r"\bgold\b": "gold",
    r"\btreasur(y|ies)\b|30-year bond|long bond": "long_bonds",
    r"\bapple\b|\baapl\b|\biphone\b": "aapl",
    r"\bmicrosoft\b|\bmsft\b|\bazure\b": "msft",
    r"\bnvidia\b|\bnvda\b": "nvda",
    r"\btesla\b|\btsla\b": "tsla",
    r"\bmeta\b|\bfacebook\b|\binstagram\b": "meta",
    r"\bamazon\b|\bamzn\b": "amzn",
    r"\bgoogle\b|\balphabet\b|\bgoogl\b": "googl",
    r"\bamd\b|advanced micro devices": "amd",
    r"\bjpmorgan\b|\bjpm\b": "jpm",
    r"\bcoinbase\b|\bcoin\b|\bbitcoin\b|crypto": "crypto",
    r"\bsemiconductor(s)?\b|\bchips?\b|\btsmc\b": "semiconductors",
    r"\bnasdaq\b": "nasdaq100",
    r"\bs&p ?500\b|\bs&p\b": "sp500",
    r"\brussell ?2000\b|small.cap": "small_cap",
    r"\byen\b|\bnikkei\b": "nikkei",
    r"\bemerging market(s)?\b": "emerging_markets",
    r"\beurope\b|eurozone|\beuro\b": "europe",
}

_COMPILED = [(re.compile(pattern, re.IGNORECASE), topic) for pattern, topic in KEYWORD_TOPICS.items()]


def extract_topics(headline: str) -> frozenset[str]:
    matched = {topic for pattern, topic in _COMPILED if pattern.search(headline)}
    return frozenset(matched)


def tag_and_route(item: NewsItem, universe: Universe) -> NewsItem:
    topics = extract_topics(item.headline)
    routed = tuple(sorted(universe.route_topics(set(topics))))
    return NewsItem(
        id=item.id,
        source=item.source,
        published_at=item.published_at,
        ingested_at=item.ingested_at,
        headline=item.headline,
        url=item.url,
        raw_payload=item.raw_payload,
        topics=topics,
        routed_symbols=routed,
        sentiment_score=item.sentiment_score,
    )
