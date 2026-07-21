"""LLM consequence-prediction client (Anthropic Claude).

This is a genuinely different mechanism from engine.features.sentiment: VADER
scores headline *tone*; this asks a model to reason about *indirect,
second-order consequences* ("X happened -> who is exposed and why").

The one rule that keeps this honest (see docs/prediction_pipeline.md):
forward_safe is computed from the configured model's stated training-data
knowledge cutoff vs. the news item's decision_timestamp. This module
deliberately does NOT guess that cutoff date -- ANTHROPIC_MODEL_KNOWLEDGE_CUTOFF
must be set explicitly to whatever the actual cutoff is for the model named
in ANTHROPIC_MODEL. Getting this wrong silently defeats the entire anti-
look-ahead mechanism, so a placeholder value refuses to run rather than
producing predictions that look forward-safe but aren't.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import anthropic

from engine.config.settings import Settings
from engine.prediction.schema import ConsequenceAnalysis

_PLACEHOLDER_CUTOFF = "1970-01-01"


class PredictionConfigError(RuntimeError):
    pass


def parse_knowledge_cutoff(settings: Settings) -> date:
    """Shared by every prediction-client backend (API key or CLI/OAuth) --
    the cutoff-date validation is about the model, not the transport."""
    cutoff_str = settings.anthropic_model_knowledge_cutoff
    if not cutoff_str or cutoff_str == _PLACEHOLDER_CUTOFF:
        raise PredictionConfigError(
            f"ANTHROPIC_MODEL_KNOWLEDGE_CUTOFF is unset or still the placeholder "
            f"({_PLACEHOLDER_CUTOFF}). Set it to the actual training-data knowledge "
            f"cutoff date for the model in ANTHROPIC_MODEL ({settings.anthropic_model!r}). "
            f"This field is not guessable -- see engine/prediction/client.py docstring."
        )
    return datetime.strptime(cutoff_str, "%Y-%m-%d").date()


def is_forward_safe(knowledge_cutoff: date, decision_timestamp: datetime) -> bool:
    cutoff_dt = datetime.combine(knowledge_cutoff, datetime.min.time(), tzinfo=timezone.utc)
    return decision_timestamp > cutoff_dt


def build_prompt(headline: str, tracked_symbols: list[str], past_cases: list[str]) -> str:
    lines = [
        f"Headline: {headline}",
        "",
        f"Symbols we can currently trade on (context, not a limit -- see system prompt): "
        f"{', '.join(tracked_symbols)}",
    ]
    if past_cases:
        lines += ["", "Past cases:"] + [f"- {case}" for case in past_cases]
    return "\n".join(lines)

SYSTEM_PROMPT = """You analyze financial news for indirect, second-order market consequences \
-- the kind of reasoning that connects "a pandemic starts in China" to "cruise line and \
airline stocks are exposed" without either company being named in the headline.

You are given a headline and a list of symbols we currently have the ability to trade on. \
That list is not a constraint on your reasoning -- name whichever real, exchange-listed \
ticker is genuinely the best answer for a given effect, whether or not it's on that list. \
Think about which sectors, themes, and specific companies plausibly win or lose from the \
news first -- through supply chains, sector exposure, competitive dynamics, macro \
sensitivity, or similar mechanisms, even if never mentioned directly -- then name the ticker. \
Do not force a connection that isn't there; an empty result is a valid and often correct \
answer. Do not guess at a ticker you're not confident actually exists for the company you \
mean; say so in the rationale instead of inventing one.

You may also be given a list of past cases: an earlier headline, what was predicted then, \
and what actually happened. Use them as precedent -- notice if a similar mechanism worked or \
didn't -- but each new headline is a new case; do not assume the same symbols are affected \
just because a past case involved a similar topic.

For each impact you identify, give the expected direction, a 0-1 confidence, and a short \
rationale stating the causal mechanism. Be conservative with confidence -- most indirect \
effects are uncertain."""


class ConsequencePredictionClient:
    """Metered-API backend: authenticates with ANTHROPIC_API_KEY via the
    Python SDK. See engine.prediction.cli_client.ClaudeCLIPredictionClient
    for the subscription/OAuth-token alternative -- engine.prediction.factory
    picks between the two; nothing else needs to know which is active."""

    def __init__(self, settings: Settings):
        if not settings.anthropic_api_key:
            raise PredictionConfigError(
                "ANTHROPIC_API_KEY is not set -- refusing to construct a prediction client "
                "rather than silently doing nothing."
            )
        self.model = settings.anthropic_model
        self.knowledge_cutoff: date = parse_knowledge_cutoff(settings)
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def is_forward_safe(self, decision_timestamp: datetime) -> bool:
        return is_forward_safe(self.knowledge_cutoff, decision_timestamp)

    def analyze(
        self,
        headline: str,
        tracked_symbols: list[str],
        past_cases: list[str] | None = None,
    ) -> ConsequenceAnalysis:
        user_prompt = build_prompt(headline, tracked_symbols, past_cases or [])
        response = self._client.messages.parse(
            model=self.model,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            output_format=ConsequenceAnalysis,
        )
        return response.parsed_output
