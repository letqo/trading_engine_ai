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
    impact. `item.topics` must already be set (engine.data.router.tag_and_route).

    The model is not restricted to the tracked universe (see
    engine/prediction/client.py) -- every impact it names gets logged and
    later scored, tracked or not. `in_tracked_universe` records which is
    which: only tracked-universe predictions are eligible for real orders
    (engine.prediction.trading), since only those symbols are vetted as
    tradable and risk-calibrated. Off-universe predictions still count as
    real evidence -- see `engine ticker-suggestions` for accumulating that
    evidence toward a human decision to add a symbol to universe.yaml.
    """
    tracked_symbols = sorted(universe.tradable_symbols())
    past_cases = load_resolved_predictions_by_topics(session, set(item.topics), limit=retrieval_limit)
    past_case_strings = [_format_past_case(p) for p in past_cases]

    analysis = client.analyze(item.headline, tracked_symbols, past_case_strings)
    forward_safe = client.is_forward_safe(item.decision_timestamp)
    knowledge_cutoff_dt = datetime.combine(client.knowledge_cutoff, datetime.min.time(), tzinfo=timezone.utc)
    context_ids = [p.id for p in past_cases]

    predictions: list[Prediction] = []
    for impact in analysis.impacts:
        in_tracked_universe = impact.symbol in tracked_symbols
        if not in_tracked_universe:
            logger.info(
                "prediction named a symbol outside the tracked universe -- logged, not tradable",
                extra={"extra_fields": {"symbol": impact.symbol, "headline": item.headline}},
            )
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
                in_tracked_universe=in_tracked_universe,
                retrieved_context_ids=context_ids,
            )
        )
    return predictions


def _fetch_resolution_data(
    symbol: str, decision_timestamp: datetime, window_hours: float, direction: PredictionDirection
) -> tuple[float | None, float | None, float | None, float | None]:
    """Entry/exit prices for scoring, plus max-favorable/max-adverse
    excursion (mfe_pct/mae_pct) during the window -- the intermediate bars
    were always fetched here, only entry/exit were ever kept. Excursions
    are relative to `direction`: mfe_pct is the best the price got in the
    predicted direction's favor, mae_pct the worst it moved against it,
    both non-negative pct of entry_price. See docs/prediction_pipeline.md."""
    interval = "1h" if window_hours <= 72 else "1d"
    start = decision_timestamp.date()
    end = (decision_timestamp + timedelta(hours=window_hours, days=2)).date()
    df = fetch_bars([symbol], start=str(start), end=str(end), interval=interval)
    if df.empty:
        return None, None, None, None

    df = df.sort_values("timestamp").reset_index(drop=True)

    # SQLite/Postgres both hand back a naive datetime for
    # Prediction.news_decision_timestamp (see engine.journal.registry
    # ._as_utc's docstring for the same round-trip gap), while
    # engine.data.bars.fetch_bars always returns tz-aware UTC timestamps --
    # comparing the two directly raises "Cannot compare tz-naive and
    # tz-aware datetime-like objects" the first time this runs against a
    # real (non-empty) bars frame. Align decision_timestamp to whatever
    # df["timestamp"] actually is, rather than assuming a fixed direction,
    # so this is correct for both the real caller and any test fixture that
    # builds bars with naive timestamps to match a naive decision_timestamp.
    df_is_tz_aware = df["timestamp"].dt.tz is not None
    if df_is_tz_aware and decision_timestamp.tzinfo is None:
        decision_timestamp = decision_timestamp.replace(tzinfo=timezone.utc)
    elif not df_is_tz_aware and decision_timestamp.tzinfo is not None:
        decision_timestamp = decision_timestamp.replace(tzinfo=None)

    exit_deadline = decision_timestamp + timedelta(hours=window_hours)

    entry_positions = df.index[df["timestamp"] >= decision_timestamp]
    if len(entry_positions) == 0:
        return None, None, None, None
    entry_pos = int(entry_positions[0])
    entry_price = float(df.loc[entry_pos, "open"])

    exit_positions = df.index[df["timestamp"] <= exit_deadline]
    if len(exit_positions) == 0:
        exit_price = float(df.loc[entry_pos, "close"])
        window = df.loc[[entry_pos]]
    else:
        exit_pos = int(exit_positions[-1])
        exit_price = float(df.loc[exit_pos, "close"])
        lo, hi = sorted((entry_pos, exit_pos))
        window = df.iloc[lo : hi + 1]

    highest = float(window["high"].max())
    lowest = float(window["low"].min())
    if direction == PredictionDirection.UP:
        mfe_pct = (highest - entry_price) / entry_price * 100.0
        mae_pct = (entry_price - lowest) / entry_price * 100.0
    else:
        mfe_pct = (entry_price - lowest) / entry_price * 100.0
        mae_pct = (highest - entry_price) / entry_price * 100.0
    return entry_price, exit_price, mfe_pct, mae_pct


def resolve_pending_predictions(session: Session, as_of: datetime | None = None) -> list[Prediction]:
    """Find every prediction whose resolution window has closed and score
    it against real price data. Safe to run repeatedly (already-resolved
    rows are never revisited)."""
    as_of = as_of or datetime.now(timezone.utc)
    pending = load_pending_predictions_past_window(session, as_of)
    resolved = []
    for prediction in pending:
        entry_price, exit_price, mfe_pct, mae_pct = _fetch_resolution_data(
            prediction.symbol, prediction.news_decision_timestamp, prediction.resolution_window_hours, prediction.direction
        )
        resolved.append(
            resolve_prediction(
                session, prediction, entry_price=entry_price, exit_price=exit_price,
                resolved_at=as_of, mfe_pct=mfe_pct, mae_pct=mae_pct,
            )
        )
    return resolved
