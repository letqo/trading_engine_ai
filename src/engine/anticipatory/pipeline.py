"""Discovery and belief-revision for the anticipatory prediction engine --
see engine/anticipatory/__init__.py and docs/anticipatory_prediction_mode.md.

This module only decides and journals (creates/updates Hypothesis and
HypothesisBelief rows); it never places an order. Acting on an OPENED/
EXITED belief is engine.anticipatory.trading's job -- see
engine.cli.main.anticipatory_loop for how the two get wired together.
"""

from __future__ import annotations

from collections import Counter

from engine.data.polymarket import fetch_candidate_markets, fetch_market_price
from engine.journal.models import Hypothesis, HypothesisAction, HypothesisBelief, PredictionDirection
from engine.journal.registry import (
    close_hypothesis,
    create_hypothesis,
    hypothesis_exists_for_market,
    load_open_hypotheses,
    record_hypothesis_belief,
)
from engine.logging_setup import get_logger
from engine.prediction.factory import PredictionClient

logger = get_logger(__name__)


def _decide_action(*, gap: float, min_gap_threshold: float, position_side: str | None) -> HypothesisAction:
    """Shared by discovery and every later revision, so the decision logic
    lives in exactly one place. V1 keeps this binary -- flat or fully
    positioned, no partial adds/trims (mirrors engine.prediction.trading's
    close-the-whole-thing pattern). HypothesisAction.ADDED/TRIMMED stay
    defined for a future partial-sizing pass but are never produced here."""
    significant = abs(gap) >= min_gap_threshold
    wants_long = gap > 0
    if position_side is None:
        return HypothesisAction.OPENED if significant else HypothesisAction.NO_GAP
    currently_long = position_side == "long"
    if significant and currently_long == wants_long:
        return HypothesisAction.HELD
    return HypothesisAction.EXITED  # gap closed, or flipped direction -- either way, close before any future re-open


def discover_hypotheses(
    session,
    client: PredictionClient,
    *,
    discovery_limit: int,
    max_open_hypotheses: int,
    min_gap_threshold: float,
    max_open_hypotheses_per_symbol: int = 2,
) -> list[Hypothesis]:
    """One discovery sweep: fetch candidate Polymarket markets (already
    category-filtered, see engine.data.polymarket.EXCLUDED_TAG_SLUGS),
    skip already-tracked ones, ask the LLM whether each has real,
    tradable equity/ETF exposure, and create a Hypothesis for each
    relevant new one -- capped at max_open_hypotheses total open at any
    time, since each candidate costs one paid LLM call regardless of
    whether it turns out relevant.

    Also capped per-symbol at max_open_hypotheses_per_symbol: Polymarket
    often splits one underlying question into several threshold markets
    (e.g. "hit $110" and "hit $120"), which the LLM will legitimately map
    to the same tradable symbol every time -- without this cap, one
    commodity/instrument could consume most of max_open_hypotheses on
    correlated bets. This is checked only after the relevance LLM call
    returns a symbol, since the symbol isn't known beforehand -- it can't
    avoid the paid call's cost, only stop the resulting Hypothesis/position
    from being created."""
    open_hypotheses = load_open_hypotheses(session)
    open_count = len(open_hypotheses)
    if open_count >= max_open_hypotheses:
        return []
    symbol_counts = Counter(h.symbol for h in open_hypotheses)

    created: list[Hypothesis] = []
    for market in fetch_candidate_markets(limit=discovery_limit):
        if open_count + len(created) >= max_open_hypotheses:
            break
        if hypothesis_exists_for_market(session, market.market_id):
            continue

        estimate = client.estimate_hypothesis(market.question, market.description)
        if not estimate.relevant or not estimate.symbol or not estimate.direction_if_yes:
            continue

        if symbol_counts[estimate.symbol] >= max_open_hypotheses_per_symbol:
            logger.info(
                "skipping hypothesis -- symbol already at correlated-hypothesis cap",
                extra={"extra_fields": {
                    "market_id": market.market_id, "symbol": estimate.symbol,
                    "max_open_hypotheses_per_symbol": max_open_hypotheses_per_symbol,
                }},
            )
            continue

        direction = PredictionDirection.UP if estimate.direction_if_yes == "up" else PredictionDirection.DOWN
        hyp = create_hypothesis(
            session,
            market_id=market.market_id,
            question=market.question,
            symbol=estimate.symbol,
            direction_if_yes=direction,
        )
        gap = estimate.p_model - market.price_yes
        action = _decide_action(gap=gap, min_gap_threshold=min_gap_threshold, position_side=None)
        record_hypothesis_belief(
            session, hyp,
            p_model=estimate.p_model, p_market=market.price_yes,
            confidence=estimate.confidence, rationale=estimate.rationale, action=action,
        )
        created.append(hyp)
        symbol_counts[estimate.symbol] += 1
        logger.info(
            "new hypothesis tracked",
            extra={"extra_fields": {
                "market_id": market.market_id, "symbol": estimate.symbol,
                "p_model": estimate.p_model, "p_market": market.price_yes, "action": action.value,
            }},
        )
    return created


def revise_open_hypotheses(
    session, client: PredictionClient, *, min_gap_threshold: float
) -> tuple[list[HypothesisBelief], list[Hypothesis]]:
    """One sweep over every open Hypothesis, one Polymarket price refresh
    each. If Polymarket now reports the market closed, close the
    Hypothesis out -- a settled market's price converges to ~0 or ~1,
    used as the resolution outcome since Gamma's read API doesn't expose
    a separate "which side won" field. Otherwise, re-estimate P_model
    fresh -- never anchored to the prior belief, each re-estimation is
    independent, same principle as the reactive engine treating each new
    headline as its own case, not assuming continuity with the last one --
    compute the gap, and record what should happen next.

    Returns (new beliefs, hypotheses just closed by resolution) --
    engine.anticipatory.trading acts on OPENED/EXITED beliefs and must
    also flatten any lingering position on a just-resolved hypothesis.
    """
    beliefs: list[HypothesisBelief] = []
    resolved: list[Hypothesis] = []
    for hyp in load_open_hypotheses(session):
        market = fetch_market_price(hyp.market_id)
        if market is None:
            logger.warning(
                "could not refresh Polymarket price -- skipping this cycle",
                extra={"extra_fields": {"market_id": hyp.market_id, "hypothesis_id": hyp.id}},
            )
            continue

        if market.closed:
            outcome = market.price_yes > 0.5
            resolved.append(close_hypothesis(session, hyp, resolution_outcome=outcome))
            continue

        estimate = client.estimate_hypothesis(hyp.question)
        gap = estimate.p_model - market.price_yes
        action = _decide_action(gap=gap, min_gap_threshold=min_gap_threshold, position_side=hyp.position_side)
        belief = record_hypothesis_belief(
            session, hyp,
            p_model=estimate.p_model, p_market=market.price_yes,
            confidence=estimate.confidence, rationale=estimate.rationale, action=action,
        )
        beliefs.append(belief)
    return beliefs, resolved
