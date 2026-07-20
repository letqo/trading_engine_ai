from datetime import datetime, timezone

from engine.features.sentiment import score_headline, score_news_item
from engine.domain import NewsItem


def test_positive_headline_scores_positive():
    assert score_headline("Company posts record profit, beats expectations by a wide margin") > 0.3


def test_negative_headline_scores_negative():
    assert score_headline("Company collapses into bankruptcy amid massive fraud scandal") < -0.3


def test_neutral_headline_scores_near_zero():
    assert abs(score_headline("Company to hold quarterly meeting on Tuesday")) < 0.3


def test_score_news_item_preserves_fields_and_sets_score():
    now = datetime(2026, 1, 5, tzinfo=timezone.utc)
    item = NewsItem(
        id="1", source="test", published_at=now, ingested_at=now,
        headline="Stocks surge on strong earnings beat", url=None, raw_payload={"x": 1},
    )
    scored = score_news_item(item)
    assert scored.sentiment_score is not None
    assert scored.sentiment_score > 0
    assert scored.raw_payload == {"x": 1}
    assert scored.id == item.id
