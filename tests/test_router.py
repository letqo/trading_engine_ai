from datetime import datetime, timezone
from pathlib import Path

from engine.data.router import extract_topics, tag_and_route
from engine.data.universe import load_universe
from engine.domain import NewsItem

UNIVERSE_PATH = Path(__file__).resolve().parent.parent / "universe.yaml"


def make_item(headline: str) -> NewsItem:
    now = datetime(2026, 1, 5, tzinfo=timezone.utc)
    return NewsItem(
        id="1", source="test", published_at=now, ingested_at=now, headline=headline,
        url=None, raw_payload={},
    )


def test_extract_topics_fed_headline():
    topics = extract_topics("Fed holds interest rates steady, Powell signals patience")
    assert "fed" in topics


def test_extract_topics_boj_headline():
    topics = extract_topics("Bank of Japan surprises markets with rate hike")
    assert "boj" in topics


def test_extract_topics_no_match_returns_empty():
    topics = extract_topics("Local bakery wins award for best croissant")
    assert topics == frozenset()


def test_tag_and_route_boj_routes_to_ewj():
    universe = load_universe(UNIVERSE_PATH)
    item = make_item("BOJ raises rates in surprise move")
    tagged = tag_and_route(item, universe)
    assert tagged.routed_symbols == ("EWJ",)
    assert tagged.topics == frozenset({"boj"})
    # raw payload and other fields preserved
    assert tagged.id == item.id
    assert tagged.headline == item.headline
