"""Orchestrates one cycle of the consequence-prediction forward-test loop:

    news item (already topic-tagged) -> retrieve past resolved cases with
    overlapping topics -> ask the LLM to analyze consequences -> persist one
    Prediction row per identified impact, forward_safe computed structurally
    -> [later, separately, once the resolution window has closed] fetch real
    price data and resolve the outcome.

Nothing here ever re-touches a prediction after it's first written, except
resolve_pending_predictions filling in the outcome fields exactly once. That
ordering is what makes the log usable as genuine forward-test evidence
instead of something that could have been quietly adjusted after the fact --
see docs/prediction_pipeline.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session

from engine.data.bars import fetch_bars
from engine.data.universe import Universe
from engine.domain import NewsItem
from engine.journal.models import Prediction, PredictionDirection
from engine.journal.registry import (
    load_pending_predictions_past_window,
    load_resolved_predictions_by_topics,
    record_prediction,
    resolve_prediction,
)
from engine.logging_setup import get_logger
from engine.prediction.client import ConsequencePredictionClient

logger = get_logger(__name__)


def _format_past_case(prediction: Prediction) -> str:
    if prediction.status.value != "resolved":
        return f'"{prediction.news_headline}" -> predicted {prediction.symbol} {prediction.direction.value} (unresolved)'
    outcome = "correct" if prediction.outcome_correct else "incorrect"
    return (
        f'"{prediction.news_headline}" -> predicted {prediction.symbol} {prediction.direction.value}, '
        f"actual move {prediction.actual_return_pct:+.1f}% ({outcome})"
    )


def run_prediction_for_news_item(
    session: Session,
    client: ConsequencePredictionClient,
    item: NewsItem,
    universe: Universe,
    resolution_window_hours: float,
    retrieval_limit: int = 5,
) -> list[Prediction]:
    """Analyze one news item and persist a Prediction row per identified
    impact. `item.topics` must already be set (engine.data.router.tag_and_route)."""
    tracked_symbols = sorted(universe.tradable_symbols())
    past_cases = load_resolved_predictions_by_topics(session, set(item.topics), limit=retrieval_limit)
    past_case_strings = [_format_past_case(p) for p in past_cases]

    analysis = client.analyze(item.headline, tracked_symbols, past_case_strings)
    forward_safe = client.is_forward_safe(item.decision_timestamp)
    knowledge_cutoff_dt = datetime.combine(client.knowledge_cutoff, datetime.min.time(), tzinfo=timezone.utc)
    context_ids = [p.id for p in past_cases]

    predictions: list[Prediction] = []
    for impact in analysis.impacts:
        if impact.symbol not in tracked_symbols:
            logger.warning(
                "prediction named a symbol outside the tracked universe -- dropped",
                extra={"extra_fields": {"symbol": impact.symbol, "headline": item.headline}},
            )
            continue
        predictions.append(
            record_prediction(
                session,
                news_headline=item.headline,
                news_source=item.source,
                news_published_at=item.published_at,
                news_decision_timestamp=item.decision_timestamp,
                topics=list(item.topics),
                symbol=impact.symbol,
                direction=PredictionDirection(impact.direction),
                confidence=impact.confidence,
                rationale=impact.rationale,
                model_name=client.model,
                model_knowledge_cutoff=knowledge_cutoff_dt,
                forward_safe=forward_safe,
                resolution_window_hours=resolution_window_hours,
                retrieved_context_ids=context_ids,
            )
        )
    return predictions


def _fetch_entry_exit_prices(symbol: str, decision_timestamp: datetime, window_hours: float) -> tuple[float | None, float | None]:
    interval = "1h" if window_hours <= 72 else "1d"
    start = decision_timestamp.date()
    end = (decision_timestamp + timedelta(hours=window_hours, days=2)).date()
    df = fetch_bars([symbol], start=str(start), end=str(end), interval=interval)
    if df.empty:
        return None, None

    df = df.sort_values("timestamp")
    exit_deadline = decision_timestamp + timedelta(hours=window_hours)

    entry_rows = df[df["timestamp"] >= decision_timestamp]
    if entry_rows.empty:
        return None, None
    entry_price = float(entry_rows.iloc[0]["open"])

    exit_rows = df[df["timestamp"] <= exit_deadline]
    if exit_rows.empty:
        exit_price = float(entry_rows.iloc[0]["close"])
    else:
        exit_price = float(exit_rows.iloc[-1]["close"])
    return entry_price, exit_price


def resolve_pending_predictions(session: Session, as_of: datetime | None = None) -> list[Prediction]:
    """Find every prediction whose resolution window has closed and score
    it against real price data. Safe to run repeatedly (already-resolved
    rows are never revisited)."""
    as_of = as_of or datetime.now(timezone.utc)
    pending = load_pending_predictions_past_window(session, as_of)
    resolved = []
    for prediction in pending:
        entry_price, exit_price = _fetch_entry_exit_prices(
            prediction.symbol, prediction.news_decision_timestamp, prediction.resolution_window_hours
        )
        resolved.append(
            resolve_prediction(session, prediction, entry_price=entry_price, exit_price=exit_price, resolved_at=as_of)
        )
    return resolved
