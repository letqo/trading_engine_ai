from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from engine.config.settings import Settings
from engine.data.alpaca_news import AlpacaNewsAuthError
from engine.data.snapshot import create_snapshot
from engine.data.universe import load_universe
from engine.domain import NewsItem

UNIVERSE_PATH = Path(__file__).resolve().parent.parent / "universe.yaml"
EMPTY_BARS = pd.DataFrame({"timestamp": [], "symbol": [], "open": [], "high": [], "low": [], "close": [], "volume": []})


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _news_item(headline="Fed holds rates", published_at=None, ingested_at=None):
    published_at = published_at or datetime(2024, 1, 1, tzinfo=timezone.utc)
    return NewsItem(
        id="1", source="alpaca_benzinga", published_at=published_at,
        ingested_at=ingested_at or (published_at + timedelta(minutes=15)),
        headline=headline, url=None, raw_payload={},
    )


def test_uses_alpaca_when_key_present_and_preserves_backfilled_ingested_at(db_session):
    universe = load_universe(UNIVERSE_PATH)
    settings = _settings(alpaca_api_key="key", alpaca_api_secret="secret")
    item = _news_item()

    with patch("engine.data.snapshot.fetch_bars", return_value=EMPTY_BARS), \
         patch("engine.data.snapshot.fetch_alpaca_news", return_value=[item]) as mock_alpaca, \
         patch("engine.data.snapshot.fetch_all_rss") as mock_rss, \
         patch("engine.data.snapshot.record_news_item") as mock_record:
        create_snapshot(db_session, universe, start="2024-01-01", end="2024-01-02", data_dir="/tmp", settings=settings)

    mock_alpaca.assert_called_once()
    mock_rss.assert_not_called()
    assert mock_record.call_args.kwargs["ingested_at"] == item.ingested_at


def test_falls_back_to_rss_without_alpaca_key(db_session):
    universe = load_universe(UNIVERSE_PATH)
    settings = _settings()
    item = _news_item()

    with patch("engine.data.snapshot.fetch_bars", return_value=EMPTY_BARS), \
         patch("engine.data.snapshot.fetch_alpaca_news") as mock_alpaca, \
         patch("engine.data.snapshot.fetch_all_rss", return_value=[item]) as mock_rss, \
         patch("engine.data.snapshot.record_news_item") as mock_record:
        create_snapshot(db_session, universe, start="2024-01-01", end="2024-01-02", data_dir="/tmp", settings=settings)

    mock_alpaca.assert_not_called()
    mock_rss.assert_called_once()
    assert mock_record.call_args.kwargs["ingested_at"] is None


def test_falls_back_to_rss_when_alpaca_auth_fails(db_session):
    universe = load_universe(UNIVERSE_PATH)
    settings = _settings(alpaca_api_key="bad", alpaca_api_secret="bad")
    item = _news_item()

    with patch("engine.data.snapshot.fetch_bars", return_value=EMPTY_BARS), \
         patch("engine.data.snapshot.fetch_alpaca_news", side_effect=AlpacaNewsAuthError("nope")), \
         patch("engine.data.snapshot.fetch_all_rss", return_value=[item]) as mock_rss, \
         patch("engine.data.snapshot.record_news_item") as mock_record:
        create_snapshot(db_session, universe, start="2024-01-01", end="2024-01-02", data_dir="/tmp", settings=settings)

    mock_rss.assert_called_once()
    assert mock_record.call_args.kwargs["ingested_at"] is None
