import pytest

from engine.config.settings import Settings
from engine.prediction.cli_client import ClaudeCLIPredictionClient
from engine.prediction.client import ConsequencePredictionClient, PredictionConfigError
from engine.prediction.factory import build_prediction_client


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, anthropic_model_knowledge_cutoff="2026-01-31", **overrides)


def test_refuses_when_neither_credential_is_set():
    with pytest.raises(PredictionConfigError, match="CLAUDE_CODE_OAUTH_TOKEN.*ANTHROPIC_API_KEY"):
        build_prediction_client(_settings())


def test_uses_api_key_client_when_only_api_key_set():
    client = build_prediction_client(_settings(anthropic_api_key="sk-test"))
    assert isinstance(client, ConsequencePredictionClient)


def test_uses_cli_client_when_only_oauth_token_set():
    client = build_prediction_client(_settings(claude_code_oauth_token="tok-test"))
    assert isinstance(client, ClaudeCLIPredictionClient)


def test_oauth_token_wins_when_both_are_set():
    client = build_prediction_client(_settings(anthropic_api_key="sk-test", claude_code_oauth_token="tok-test"))
    assert isinstance(client, ClaudeCLIPredictionClient)
