"""The experiment journal's write API. Every backtest/live run, trade, halt
event, and reconciliation report goes through here so the persistence
format is defined in one place."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from sqlmodel import Session, select

from engine.domain import NewsItem
from engine.journal.models import (
    DataSnapshot,
    ExperimentRun,
    NewsItemRecord,
    Prediction,
    PredictionDirection,
    PredictionStatus,
    ReconciliationReport,
    RiskHaltEvent,
    RunMode,
    TradeRecord,
    TradeSide,
)


def current_git_hash() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def register_run(
    session: Session,
    *,
    mode: RunMode,
    strategy_name: str,
    config: dict,
    random_seed: int,
    data_snapshot_id: str | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    is_validation_run: bool = False,
    validation_access_reason: str | None = None,
    git_hash: str | None = None,
) -> ExperimentRun:
    if is_validation_run and not validation_access_reason:
        raise ValueError(
            "touching the validation set requires a logged reason "
            "(anti-self-deception protocol: log every validation-set access)"
        )
    run = ExperimentRun(
        mode=mode,
        strategy_name=strategy_name,
        config_json=config,
        git_hash=git_hash or current_git_hash(),
        data_snapshot_id=data_snapshot_id,
        random_seed=random_seed,
        period_start=period_start,
        period_end=period_end,
        is_validation_run=is_validation_run,
        validation_access_reason=validation_access_reason,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def record_metrics(session: Session, run: ExperimentRun, metrics: dict) -> ExperimentRun:
    for key, value in metrics.items():
        if hasattr(run, key):
            setattr(run, key, value)
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def record_trade(
    session: Session,
    *,
    run_id: str,
    timestamp: datetime,
    symbol: str,
    side: TradeSide,
    quantity: float,
    price: float,
    strategy_id: str,
    fees: float = 0.0,
    slippage: float = 0.0,
    broker_order_id: str | None = None,
    realized_pnl: float | None = None,
    exit_reason: str | None = None,
) -> TradeRecord:
    trade = TradeRecord(
        run_id=run_id,
        timestamp=timestamp,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        fees=fees,
        slippage=slippage,
        strategy_id=strategy_id,
        broker_order_id=broker_order_id,
        realized_pnl=realized_pnl,
        exit_reason=exit_reason,
    )
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return trade


def record_halt(
    session: Session,
    *,
    reason: str,
    account_equity: float,
    triggered_by: str,
    run_id: str | None = None,
) -> RiskHaltEvent:
    event = RiskHaltEvent(
        reason=reason, account_equity=account_equity, triggered_by=triggered_by, run_id=run_id
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def record_news_item(
    session: Session,
    *,
    source: str,
    published_at: datetime,
    headline: str,
    raw_payload: dict,
    url: str | None = None,
    routed_symbols: list[str] | None = None,
    sentiment_score: float | None = None,
    sentiment_model: str | None = None,
) -> NewsItemRecord:
    item = NewsItemRecord(
        source=source,
        published_at=published_at,
        headline=headline,
        raw_payload=raw_payload,
        url=url,
        routed_symbols=routed_symbols or [],
        sentiment_score=sentiment_score,
        sentiment_model=sentiment_model,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def register_snapshot(
    session: Session,
    *,
    description: str,
    universe_hash: str,
    bar_start: datetime | None = None,
    bar_end: datetime | None = None,
    news_count: int = 0,
    bar_row_count: int = 0,
) -> DataSnapshot:
    snapshot = DataSnapshot(
        description=description,
        universe_hash=universe_hash,
        bar_start=bar_start,
        bar_end=bar_end,
        news_count=news_count,
        bar_row_count=bar_row_count,
    )
    session.add(snapshot)
    session.commit()
    session.refresh(snapshot)
    return snapshot


def record_reconciliation(
    session: Session,
    *,
    week_start: datetime,
    week_end: datetime,
    backtest_run_id: str,
    backtest_expected_return_pct: float,
    realized_return_pct: float,
    tolerance_pct: float,
    notes: str | None = None,
) -> ReconciliationReport:
    divergence_pct = abs(realized_return_pct - backtest_expected_return_pct)
    report = ReconciliationReport(
        week_start=week_start,
        week_end=week_end,
        backtest_run_id=backtest_run_id,
        backtest_expected_return_pct=backtest_expected_return_pct,
        realized_return_pct=realized_return_pct,
        divergence_pct=divergence_pct,
        tolerance_pct=tolerance_pct,
        within_tolerance=divergence_pct <= tolerance_pct,
        notes=notes,
    )
    session.add(report)
    session.commit()
    session.refresh(report)
    return report


def load_news_items(session: Session, start: datetime, end: datetime) -> list[NewsItem]:
    """Read back previously-ingested news for a date range (published_at
    within [start, end]) as domain NewsItem objects, for backtesting over a
    historical window.

    This matters because free RSS feeds have no historical archive -- they
    only ever expose currently-live items. `engine ingest` is what builds
    up a real historical news corpus over time (each run persists whatever
    RSS currently shows, with real published/ingested timestamps, via
    record_news_item). A backtest over a past window must read that stored
    corpus rather than re-fetching live RSS, which would return today's
    headlines regardless of the requested date range and silently produce
    an empty/misleading backtest. See JOURNAL.md.
    """
    rows = session.exec(
        select(NewsItemRecord)
        .where(NewsItemRecord.published_at >= start, NewsItemRecord.published_at <= end)
        .order_by(NewsItemRecord.published_at)
    ).all()
    return [
        NewsItem(
            id=row.id,
            source=row.source,
            published_at=row.published_at,
            ingested_at=row.ingested_at,
            headline=row.headline,
            url=row.url,
            raw_payload=row.raw_payload,
            routed_symbols=tuple(row.routed_symbols),
            sentiment_score=row.sentiment_score,
        )
        for row in rows
    ]


def record_prediction(
    session: Session,
    *,
    news_headline: str,
    news_source: str,
    news_published_at: datetime,
    news_decision_timestamp: datetime,
    topics: list[str],
    symbol: str,
    direction: PredictionDirection,
    confidence: float,
    rationale: str,
    model_name: str,
    model_knowledge_cutoff: datetime,
    forward_safe: bool,
    resolution_window_hours: float,
    retrieved_context_ids: list[str] | None = None,
) -> Prediction:
    """Write one prediction row. Called before the outcome is known --
    nothing here ever gets edited except by resolve_prediction(), once, when
    the resolution window closes. See engine.journal.models.Prediction."""
    prediction = Prediction(
        news_headline=news_headline,
        news_source=news_source,
        news_published_at=news_published_at,
        news_decision_timestamp=news_decision_timestamp,
        topics=topics,
        symbol=symbol,
        direction=direction,
        confidence=confidence,
        rationale=rationale,
        model_name=model_name,
        model_knowledge_cutoff=model_knowledge_cutoff,
        forward_safe=forward_safe,
        resolution_window_hours=resolution_window_hours,
        retrieved_context_ids=retrieved_context_ids or [],
    )
    session.add(prediction)
    session.commit()
    session.refresh(prediction)
    return prediction


def load_resolved_predictions_by_topics(
    session: Session, topics: set[str], limit: int = 5
) -> list[Prediction]:
    """Retrieval for the prediction pipeline's few-shot context: past
    resolved cases sharing at least one topic tag, most recent first.

    Filters in Python rather than with a JSON-containment SQL query so the
    same code works identically against SQLite (local dev) and Postgres
    (prod) -- deliberate simplicity over query-side optimization while this
    table stays small. Revisit if/when it doesn't.
    """
    if not topics:
        return []
    rows = session.exec(
        select(Prediction)
        .where(Prediction.status == PredictionStatus.RESOLVED)
        .order_by(Prediction.resolved_at.desc())
    ).all()
    matched = [row for row in rows if set(row.topics) & topics]
    return matched[:limit]


def load_pending_predictions_past_window(session: Session, as_of: datetime) -> list[Prediction]:
    """Predictions whose resolution window has closed but haven't been
    scored yet -- what `engine resolve-predictions` operates on."""
    rows = session.exec(select(Prediction).where(Prediction.status == PredictionStatus.PENDING)).all()
    return [row for row in rows if _hours_elapsed(as_of, row.news_decision_timestamp) >= row.resolution_window_hours]


def _hours_elapsed(later: datetime, earlier: datetime) -> float:
    # SQLite drops tzinfo on round-trip; treat naive timestamps as UTC so
    # comparisons work identically against SQLite (dev) and Postgres (prod).
    if later.tzinfo is None:
        later = later.replace(tzinfo=timezone.utc)
    if earlier.tzinfo is None:
        earlier = earlier.replace(tzinfo=timezone.utc)
    return (later - earlier).total_seconds() / 3600.0


def resolve_prediction(
    session: Session,
    prediction: Prediction,
    *,
    entry_price: float | None,
    exit_price: float | None,
    resolved_at: datetime,
) -> Prediction:
    """Fill in the outcome fields exactly once. If price data wasn't
    available for either side, mark INVALID rather than guessing -- an
    unresolvable prediction must never silently count as a miss."""
    if entry_price is None or exit_price is None or entry_price <= 0:
        prediction.status = PredictionStatus.INVALID
        prediction.resolved_at = resolved_at
        session.add(prediction)
        session.commit()
        session.refresh(prediction)
        return prediction

    actual_return_pct = (exit_price - entry_price) / entry_price * 100.0
    actual_direction = PredictionDirection.UP if actual_return_pct >= 0 else PredictionDirection.DOWN

    prediction.status = PredictionStatus.RESOLVED
    prediction.resolved_at = resolved_at
    prediction.entry_price = entry_price
    prediction.exit_price = exit_price
    prediction.actual_return_pct = actual_return_pct
    prediction.outcome_correct = actual_direction == prediction.direction
    session.add(prediction)
    session.commit()
    session.refresh(prediction)
    return prediction
