"""The experiment journal's write API. Every backtest/live run, trade, halt
event, and reconciliation report goes through here so the persistence
format is defined in one place."""

from __future__ import annotations

import subprocess
from datetime import datetime

from sqlmodel import Session, select

from engine.domain import NewsItem
from engine.journal.models import (
    DataSnapshot,
    ExperimentRun,
    NewsItemRecord,
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
