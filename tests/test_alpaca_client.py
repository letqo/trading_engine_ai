from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from engine.config.guard import ALPACA_PAPER_BASE_URL, PaperOnlyViolation
from engine.config.settings import Settings
from engine.execution.alpaca import AlpacaAuthError, AlpacaPaperClient
from engine.risk.models import OrderRequest, Side


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_refuses_to_construct_without_credentials():
    with pytest.raises(AlpacaAuthError):
        AlpacaPaperClient(_settings())


def test_refuses_to_construct_if_paper_only_guard_trips():
    with pytest.raises(PaperOnlyViolation):
        AlpacaPaperClient(
            _settings(alpaca_api_key="k", alpaca_api_secret="s", alpaca_base_url="anything")
        )


def test_base_url_is_always_the_hardcoded_paper_endpoint():
    client = AlpacaPaperClient(_settings(alpaca_api_key="k", alpaca_api_secret="s"))
    assert client._base_url == ALPACA_PAPER_BASE_URL == "https://paper-api.alpaca.markets"
    # there is no constructor parameter to override it
    import inspect

    params = inspect.signature(AlpacaPaperClient.__init__).parameters
    assert "base_url" not in params
    assert list(params) == ["self", "settings"]


def _mock_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = str(json_data)
    resp.raise_for_status = MagicMock()
    return resp


def test_get_account_equity_parses_response():
    client = AlpacaPaperClient(_settings(alpaca_api_key="k", alpaca_api_secret="s"))
    with patch.object(client._session, "request", return_value=_mock_response({"equity": "12345.67"})) as mock_req:
        equity = client.get_account_equity()
    assert equity == 12345.67
    called_url = mock_req.call_args[0][1]
    assert called_url.startswith(ALPACA_PAPER_BASE_URL)


def test_submit_order_posts_market_day_order():
    client = AlpacaPaperClient(_settings(alpaca_api_key="k", alpaca_api_secret="s"))
    raw_order = {
        "id": "abc-123", "symbol": "AAPL", "side": "buy", "qty": "10", "status": "new",
        "filled_avg_price": None, "submitted_at": "2026-01-05T14:30:00Z",
    }
    order = OrderRequest(symbol="AAPL", side=Side.BUY, quantity=10, price=150.0,
                          timestamp=datetime.now(timezone.utc), strategy_id="test")
    with patch.object(client._session, "request", return_value=_mock_response(raw_order)) as mock_req:
        result = client.submit_order(order)
    assert result.broker_order_id == "abc-123"
    assert result.status == "new"
    kwargs = mock_req.call_args
    assert kwargs[0][0] == "POST"
    payload = kwargs[1]["json"]
    assert payload["type"] == "market"
    assert payload["time_in_force"] == "day"
    assert payload["side"] == "buy"


def test_auth_error_on_401():
    client = AlpacaPaperClient(_settings(alpaca_api_key="bad", alpaca_api_secret="bad"))
    with patch.object(client._session, "request", return_value=_mock_response({}, status_code=401)):
        with pytest.raises(AlpacaAuthError):
            client.get_account_equity()
