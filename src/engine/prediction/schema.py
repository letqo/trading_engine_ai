"""Structured output schema for the consequence-prediction LLM call.
Validated automatically via anthropic's `messages.parse()` -- see client.py.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PredictedImpact(BaseModel):
    symbol: str = Field(description="A ticker from the provided tracked-universe list. Never invent a symbol.")
    direction: Literal["up", "down"] = Field(description="Expected price direction for this symbol.")
    confidence: float = Field(ge=0.0, le=1.0, description="0 = pure guess, 1 = very confident.")
    rationale: str = Field(description="One or two sentences: the causal mechanism connecting the news to this symbol.")


class ConsequenceAnalysis(BaseModel):
    impacts: list[PredictedImpact] = Field(
        default_factory=list,
        description="Zero or more indirect/second-order impacts on tracked-universe symbols. Empty if none apply.",
    )
    overall_reasoning: str = Field(description="Brief summary of the analysis, including symbols considered and rejected.")
