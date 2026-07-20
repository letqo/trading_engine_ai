from unittest.mock import patch

from engine.config.settings import Settings
from engine.observability import alert_kill_switch, alert_risk_halt, send_alert


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_send_alert_noop_without_webhook_configured():
    with patch("engine.observability.requests.post") as mock_post:
        send_alert(_settings(), "hello")
    mock_post.assert_not_called()


def test_send_alert_posts_json_when_webhook_configured():
    settings = _settings(alert_webhook_url="https://discord.example/hook")
    with patch("engine.observability.requests.post") as mock_post:
        send_alert(settings, "hello")
    mock_post.assert_called_once()
    assert mock_post.call_args[0][0] == "https://discord.example/hook"
    assert mock_post.call_args[1]["json"] == {"content": "hello"}


def test_send_alert_swallows_webhook_errors():
    settings = _settings(alert_webhook_url="https://discord.example/hook")
    with patch("engine.observability.requests.post", side_effect=Exception("boom")):
        send_alert(settings, "hello")  # must not raise


def test_alert_helpers_format_messages():
    settings = _settings(alert_webhook_url="https://discord.example/hook")
    with patch("engine.observability.requests.post") as mock_post:
        alert_risk_halt(settings, "daily drawdown 3.5%")
        alert_kill_switch(settings)
    messages = [call.kwargs["json"]["content"] for call in mock_post.call_args_list]
    assert any("RISK HALT" in m for m in messages)
    assert any("KILL SWITCH" in m for m in messages)
