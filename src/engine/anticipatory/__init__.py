"""Anticipatory prediction engine -- see docs/anticipatory_prediction_mode.md.

Deliberately separate from engine.prediction.* (the reactive
headline-consequence engine): this mode tracks an ongoing belief about a
not-yet-resolved Polymarket event and revises it repeatedly over the
event's life, instead of writing a prediction once and resolving it once.
That needs its own data model (Hypothesis, HypothesisBelief) and its own
trigger loop (poll each open hypothesis's market, not a fixed headline
cadence) -- but it shares the LLM-call plumbing
(engine.prediction.factory.build_prediction_client) and the
execution/risk layer (RiskGate, Broker) with the reactive engine.
"""

from __future__ import annotations
