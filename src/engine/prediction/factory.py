"""Picks which prediction-client backend to construct. Both
ConsequencePredictionClient and ClaudeCLIPredictionClient expose the same
interface (model, knowledge_cutoff, is_forward_safe, analyze), so nothing
downstream needs to know or care which one is active."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from engine.config.settings import Settings
from engine.prediction.client import ConsequencePredictionClient, PredictionConfigError
from engine.prediction.cli_client import ClaudeCLIPredictionClient
from engine.prediction.schema import ConsequenceAnalysis, HypothesisEstimate


@runtime_checkable
class PredictionClient(Protocol):
    model: str

    def is_forward_safe(self, decision_timestamp) -> bool: ...

    def analyze(
        self, headline: str, tracked_symbols: list[str], past_cases: list[str] | None = None
    ) -> ConsequenceAnalysis: ...

    def estimate_hypothesis(self, question: str, description: str = "") -> HypothesisEstimate: ...


def build_prediction_client(settings: Settings) -> PredictionClient:
    """CLAUDE_CODE_OAUTH_TOKEN wins if both are set -- a subscription
    already being configured is a deliberate choice, not an accident, so
    it shouldn't lose silently to an API key left over from testing."""
    if settings.claude_code_oauth_token:
        return ClaudeCLIPredictionClient(settings)
    if settings.anthropic_api_key:
        return ConsequencePredictionClient(settings)
    raise PredictionConfigError(
        "Neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY is set -- refusing to "
        "construct a prediction client rather than silently doing nothing."
    )
