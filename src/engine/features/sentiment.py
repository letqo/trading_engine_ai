"""Sentiment scoring v1: VADER lexicon, runs offline on stored headlines.
SPEC.md explicitly allows VADER as "the dumbest baseline" -- no network
call, no GPU, cheap enough to run on every live headline on a Railway
Hobby-plan worker.
"""

from __future__ import annotations

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from engine.domain import NewsItem

_analyzer = SentimentIntensityAnalyzer()

MODEL_NAME = "vader"


def score_headline(headline: str) -> float:
    """Compound score in [-1, 1]: -1 most negative, +1 most positive."""
    return _analyzer.polarity_scores(headline)["compound"]


def score_news_item(item: NewsItem) -> NewsItem:
    score = score_headline(item.headline)
    return NewsItem(
        id=item.id,
        source=item.source,
        published_at=item.published_at,
        ingested_at=item.ingested_at,
        headline=item.headline,
        url=item.url,
        raw_payload=item.raw_payload,
        topics=item.topics,
        routed_symbols=item.routed_symbols,
        sentiment_score=score,
    )
