from datetime import datetime, timezone

from engine.data.events import EventType, build_event_stream
from engine.domain import Bar, NewsItem


def make_bar(ts, symbol="SPY", close=100.0):
    return Bar(symbol=symbol, timestamp=ts, open=close, high=close, low=close, close=close, volume=1000, timeframe="1h")


def make_news(published, ingested, headline="x"):
    return NewsItem(
        id=headline, source="test", published_at=published, ingested_at=ingested,
        headline=headline, url=None, raw_payload={},
    )


def test_events_sorted_strictly_by_timestamp():
    t0 = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 5, 10, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 5, 11, tzinfo=timezone.utc)
    bars = [make_bar(t2), make_bar(t0)]
    news = [make_news(t1, t1)]
    events = build_event_stream(bars, news)
    timestamps = [e.timestamp for e in events]
    assert timestamps == sorted(timestamps)


def test_news_uses_ingested_at_not_published_at_when_later():
    published = datetime(2026, 1, 5, 8, tzinfo=timezone.utc)
    ingested = datetime(2026, 1, 5, 10, tzinfo=timezone.utc)  # pipeline lag
    bar_at_9 = make_bar(datetime(2026, 1, 5, 9, tzinfo=timezone.utc))
    events = build_event_stream([bar_at_9], [make_news(published, ingested)])
    # the bar at 9am must come before the news event, since the news wasn't
    # actually visible to the pipeline until 10am despite an 8am publish time
    assert events[0].type == EventType.BAR
    assert events[1].type == EventType.NEWS
    assert events[1].timestamp == ingested


def test_same_timestamp_bar_resolves_before_news():
    t = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
    events = build_event_stream([make_bar(t)], [make_news(t, t)])
    assert events[0].type == EventType.BAR
    assert events[1].type == EventType.NEWS
