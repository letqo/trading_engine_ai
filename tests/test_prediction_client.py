from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from engine.config.settings import Settings
from engine.prediction.client import ConsequencePredictionClient, PredictionConfigError
from engine.prediction.schema import ConsequenceAnalysis, PredictedImpact


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_refuses_to_construct_without_api_key():
    with pytest.raises(PredictionConfigError, match="ANTHROPIC_API_KEY"):
        ConsequencePredictionClient(_settings(anthropic_model_knowledge_cutoff="2026-01-31"))


def test_refuses_to_construct_with_placeholder_cutoff():
    with pytest.raises(PredictionConfigError, match="placeholder"):
        ConsequencePredictionClient(_settings(anthropic_api_key="sk-test"))


def test_is_forward_safe_gates_on_configured_cutoff():
    client = ConsequencePredictionClient(
        _settings(anthropic_api_key="sk-test", anthropic_model_knowledge_cutoff="2026-01-31")
    )
    before_cutoff = datetime(2025, 6, 1, tzinfo=timezone.utc)
    after_cutoff = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert not client.is_forward_safe(before_cutoff)
    assert client.is_forward_safe(after_cutoff)


def test_analyze_calls_messages_parse_with_expected_params():
    client = ConsequencePredictionClient(
        _settings(anthropic_api_key="sk-test", anthropic_model_knowledge_cutoff="2026-01-31")
    )
    fake_result = ConsequenceAnalysis(
        impacts=[PredictedImpact(symbol="EWJ", direction="down", confidence=0.6, rationale="test")],
        overall_reasoning="test reasoning",
    )
    fake_response = MagicMock(parsed_output=fake_result)
    with patch.object(client._client.messages, "parse", return_value=fake_response) as mock_parse:
        result = client.analyze("BOJ hikes rates", ["EWJ", "SPY"])

    assert result == fake_result
    kwargs = mock_parse.call_args.kwargs
    assert kwargs["model"] == "claude-opus-4-8"
    assert kwargs["output_format"] is ConsequenceAnalysis
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert "BOJ hikes rates" in kwargs["messages"][0]["content"]
    assert "EWJ, SPY" in kwargs["messages"][0]["content"]


def test_analyze_includes_past_cases_in_prompt():
    client = ConsequencePredictionClient(
        _settings(anthropic_api_key="sk-test", anthropic_model_knowledge_cutoff="2026-01-31")
    )
    fake_response = MagicMock(parsed_output=ConsequenceAnalysis(impacts=[], overall_reasoning="x"))
    with patch.object(client._client.messages, "parse", return_value=fake_response) as mock_parse:
        client.analyze("ECB cuts rates", ["VGK"], past_cases=["past case: BOJ hike -> EWJ fell 2%"])
    prompt = mock_parse.call_args.kwargs["messages"][0]["content"]
    assert "past case: BOJ hike" in prompt
