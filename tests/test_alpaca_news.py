from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests

from engine.config.settings import Settings
from engine.data.alpaca_news import AlpacaNewsAuthError, fetch_alpaca_news

START = datetime(2024, 1, 1, tzinfo=timezone.utc)
END = datetime(2024, 1, 2, tzinfo=timezone.utc)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _article(article_id=1, headline="Fed holds rates", created_at="2024-01-01T12:00:00Z"):
    return {
        "id": article_id,
        "headline": headline,
        "created_at": created_at,
        "url": "https://example.com/a",
        "author": "Staff",
    }


def test_refuses_without_credentials():
    with pytest.raises(AlpacaNewsAuthError, match="ALPACA_API_KEY"):
        fetch_alpaca_news(START, END, _settings())


def test_fetches_single_page_and_preserves_raw_payload():
    settings = _settings(alpaca_api_key="key", alpaca_api_secret="secret")
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"news": [_article()], "next_page_token": None}
    with patch("requests.Session.get", return_value=fake_response) as mock_get:
        items = fetch_alpaca_news(START, END, settings)

    assert len(items) == 1
    item = items[0]
    assert item.source == "alpaca_benzinga"
    assert item.headline == "Fed holds rates"
    assert item.raw_payload == _article()
    call_kwargs = mock_get.call_args.kwargs
    assert call_kwargs["params"]["start"] == START.isoformat()
    assert call_kwargs["params"]["end"] == END.isoformat()


def test_simulated_ingestion_lag_is_added_to_published_at():
    settings = _settings(alpaca_api_key="key", alpaca_api_secret="secret", alpaca_news_backfill_lag_seconds=600.0)
    fake_response = MagicMock(status_code=200)
    fake_response.json.return_value = {"news": [_article()], "next_page_token": None}
    with patch("requests.Session.get", return_value=fake_response):
        items = fetch_alpaca_news(START, END, settings)

    item = items[0]
    assert item.ingested_at == item.published_at + timedelta(seconds=600.0)
    assert item.ingested_at > item.published_at  # never zero-lag -- see module docstring


def test_paginates_until_next_page_token_is_empty():
    settings = _settings(alpaca_api_key="key", alpaca_api_secret="secret")
    page1 = MagicMock(status_code=200)
    page1.json.return_value = {"news": [_article(1)], "next_page_token": "tok2"}
    page2 = MagicMock(status_code=200)
    page2.json.return_value = {"news": [_article(2)], "next_page_token": None}
    with patch("requests.Session.get", side_effect=[page1, page2]) as mock_get:
        items = fetch_alpaca_news(START, END, settings)

    assert len(items) == 2
    assert mock_get.call_count == 2
    assert mock_get.call_args_list[1].kwargs["params"]["page_token"] == "tok2"


def test_raises_auth_error_on_401():
    settings = _settings(alpaca_api_key="bad", alpaca_api_secret="bad")
    fake_response = MagicMock(status_code=401, text="unauthorized")
    fake_response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=fake_response)
    with patch("requests.Session.get", return_value=fake_response):
        with pytest.raises(AlpacaNewsAuthError):
            fetch_alpaca_news(START, END, settings)


def test_retries_on_429_then_succeeds():
    settings = _settings(alpaca_api_key="key", alpaca_api_secret="secret")
    rate_limited = MagicMock(status_code=429)
    rate_limited.raise_for_status.side_effect = requests.exceptions.HTTPError(response=rate_limited)
    ok = MagicMock(status_code=200)
    ok.json.return_value = {"news": [_article()], "next_page_token": None}
    # One real retry, minimum backoff (2s) -- not worth mocking tenacity's
    # internals just to shave two seconds off this one test.
    with patch("requests.Session.get", side_effect=[rate_limited, ok]):
        items = fetch_alpaca_news(START, END, settings)
    assert len(items) == 1
