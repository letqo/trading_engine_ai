"""Structured output schema for the consequence-prediction LLM call.
Validated automatically via anthropic's `messages.parse()` -- see client.py.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PredictedImpact(BaseModel):
    symbol: str = Field(
        description="A real, exchange-listed ticker symbol for the affected company or ETF -- not a "
        "company name, not a made-up symbol. Not restricted to any fixed list: name whatever the "
        "single best real ticker is for this effect, tracked or not. If you are not confident of the "
        "exact ticker for a real company, say so in the rationale rather than guessing one."
    )
    direction: Literal["up", "down"] = Field(description="Expected price direction for this symbol.")
    confidence: float = Field(ge=0.0, le=1.0, description="0 = pure guess, 1 = very confident.")
    rationale: str = Field(description="One or two sentences: the causal mechanism connecting the news to this symbol.")


class ConsequenceAnalysis(BaseModel):
    impacts: list[PredictedImpact] = Field(
        default_factory=list,
        description="Zero or more indirect/second-order impacts, on any real ticker. Empty if none apply.",
    )
    overall_reasoning: str = Field(description="Brief summary of the analysis, including symbols considered and rejected.")


class HypothesisEstimate(BaseModel):
    """Structured output for the anticipatory-mode probability call (see
    engine.anticipatory.pipeline). Two independent judgments in one call:
    the model's own probability of the event (formed with no visibility
    into Polymarket's price, to avoid anchoring -- see
    docs/anticipatory_prediction_mode.md), and whether/how a real
    equity/ETF is exposed to the outcome."""

    p_model: float = Field(
        ge=0.0, le=1.0,
        description="Your own independent probability that this event resolves YES, from your own "
        "reasoning and knowledge only -- you have not been shown any market price for this event.",
    )
    relevant: bool = Field(
        description="Whether a real, exchange-listed equity or ETF has genuine, tradable second-order "
        "exposure to this event's outcome. False is a valid and often correct answer -- most events "
        "have no clean exposure."
    )
    symbol: str | None = Field(
        default=None,
        description="A real, exchange-listed ticker for the single best-exposed company or ETF, if "
        "relevant is True, else null. Not restricted to any fixed list. If you are not confident of "
        "the exact ticker for a real company, set relevant=False rather than guessing one.",
    )
    direction_if_yes: Literal["up", "down"] | None = Field(
        default=None, description="Expected price direction for `symbol` if this event resolves YES, else null."
    )
    confidence: float = Field(ge=0.0, le=1.0, description="0 = pure guess, 1 = very confident, in the symbol/direction call.")
    rationale: str = Field(description="One or two sentences covering both the probability estimate and the exposure call.")
