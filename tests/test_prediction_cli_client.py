import json
import subprocess
from unittest.mock import patch

import pytest

from engine.config.settings import Settings
from engine.prediction.cli_client import ClaudeCLIError, ClaudeCLIPredictionClient, _parse_cli_output
from engine.prediction.client import PredictionConfigError
from engine.prediction.schema import ConsequenceAnalysis, PredictedImpact


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_refuses_to_construct_without_oauth_token():
    with pytest.raises(PredictionConfigError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        ClaudeCLIPredictionClient(_settings(anthropic_model_knowledge_cutoff="2026-01-31"))


def test_refuses_to_construct_with_placeholder_cutoff():
    with pytest.raises(PredictionConfigError, match="placeholder"):
        ClaudeCLIPredictionClient(_settings(claude_code_oauth_token="tok-test"))


def test_refuses_to_construct_when_claude_cli_not_on_path():
    with patch("engine.prediction.cli_client.shutil.which", return_value=None):
        with pytest.raises(ClaudeCLIError, match="not found on PATH"):
            ClaudeCLIPredictionClient(
                _settings(claude_code_oauth_token="tok-test", anthropic_model_knowledge_cutoff="2026-01-31")
            )


def test_is_forward_safe_gates_on_configured_cutoff():
    from datetime import datetime, timezone

    client = ClaudeCLIPredictionClient(
        _settings(claude_code_oauth_token="tok-test", anthropic_model_knowledge_cutoff="2026-01-31")
    )
    assert not client.is_forward_safe(datetime(2025, 6, 1, tzinfo=timezone.utc))
    assert client.is_forward_safe(datetime(2026, 7, 1, tzinfo=timezone.utc))


def _fake_analysis() -> ConsequenceAnalysis:
    return ConsequenceAnalysis(
        impacts=[PredictedImpact(symbol="EWJ", direction="down", confidence=0.6, rationale="test")],
        overall_reasoning="test reasoning",
    )


class TestParseCliOutput:
    def test_parses_documented_envelope_with_json_string_result(self):
        analysis = _fake_analysis()
        envelope = json.dumps({"type": "result", "subtype": "success", "result": analysis.model_dump_json()})
        parsed = _parse_cli_output(envelope)
        assert parsed == analysis

    def test_parses_envelope_with_dict_result_defensively(self):
        analysis = _fake_analysis()
        envelope = json.dumps({"result": analysis.model_dump(mode="json")})
        parsed = _parse_cli_output(envelope)
        assert parsed == analysis

    def test_raises_on_invalid_top_level_json(self):
        with pytest.raises(ClaudeCLIError, match="did not return valid JSON"):
            _parse_cli_output("not json at all")

    def test_raises_when_result_field_missing(self):
        with pytest.raises(ClaudeCLIError, match="no 'result' field"):
            _parse_cli_output(json.dumps({"type": "result", "subtype": "success"}))

    def test_raises_when_result_is_not_schema_valid(self):
        with pytest.raises(Exception):  # pydantic ValidationError
            _parse_cli_output(json.dumps({"result": json.dumps({"unrelated": "shape"})}))


def test_analyze_invokes_subprocess_and_returns_parsed_analysis():
    client = ClaudeCLIPredictionClient(
        _settings(claude_code_oauth_token="tok-test", anthropic_model_knowledge_cutoff="2026-01-31")
    )
    analysis = _fake_analysis()
    envelope = json.dumps({"result": analysis.model_dump_json()})
    fake_completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=envelope, stderr="")

    with patch("engine.prediction.cli_client.subprocess.run", return_value=fake_completed) as mock_run:
        result = client.analyze("BOJ hikes rates", ["EWJ", "SPY"])

    assert result == analysis
    args = mock_run.call_args.args[0]
    assert "claude" in args[0].lower()
    assert "-p" in args
    assert "BOJ hikes rates" in args[args.index("-p") + 1]
    assert "--json-schema" in args
    kwargs = mock_run.call_args.kwargs
    assert kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-test"


def test_analyze_raises_on_nonzero_exit_code():
    client = ClaudeCLIPredictionClient(
        _settings(claude_code_oauth_token="tok-test", anthropic_model_knowledge_cutoff="2026-01-31")
    )
    fake_completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="auth error")

    with patch("engine.prediction.cli_client.subprocess.run", return_value=fake_completed):
        with pytest.raises(ClaudeCLIError, match="exited 1"):
            client.analyze("headline", ["EWJ"])


def test_analyze_includes_past_cases_in_prompt():
    client = ClaudeCLIPredictionClient(
        _settings(claude_code_oauth_token="tok-test", anthropic_model_knowledge_cutoff="2026-01-31")
    )
    analysis = _fake_analysis()
    envelope = json.dumps({"result": analysis.model_dump_json()})
    fake_completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=envelope, stderr="")

    with patch("engine.prediction.cli_client.subprocess.run", return_value=fake_completed) as mock_run:
        client.analyze("ECB cuts rates", ["VGK"], past_cases=["past case: BOJ hike -> EWJ fell 2%"])

    args = mock_run.call_args.args[0]
    prompt = args[args.index("-p") + 1]
    assert "past case: BOJ hike" in prompt
