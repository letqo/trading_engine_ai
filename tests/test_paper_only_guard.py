import pytest

from engine.config.guard import PaperOnlyViolation, enforce_paper_only
from engine.config.settings import Settings


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_clean_settings_pass():
    enforce_paper_only(_settings())


def test_base_url_override_trips_guard():
    with pytest.raises(PaperOnlyViolation):
        enforce_paper_only(_settings(alpaca_base_url="https://paper-api.alpaca.markets"))


def test_live_marker_in_alert_webhook_trips_guard():
    with pytest.raises(PaperOnlyViolation):
        enforce_paper_only(_settings(alert_webhook_url="https://api.alpaca.markets/hook"))


def test_paper_prefixed_url_in_a_field_does_not_trip_guard():
    enforce_paper_only(_settings(alpaca_api_key="paper-api.alpaca.markets-lookalike-key"))


def test_alpaca_live_flag_trips_guard():
    with pytest.raises(PaperOnlyViolation):
        enforce_paper_only(_settings(alpaca_live="true"))
