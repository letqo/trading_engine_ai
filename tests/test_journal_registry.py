from datetime import datetime, timezone

import pytest

from engine.journal.models import RunMode, TradeSide
from engine.journal.registry import (
    load_news_items,
    record_metrics,
    record_news_item,
    record_trade,
    register_run,
)

NOW = datetime(2026, 1, 5, tzinfo=timezone.utc)


def test_register_run_round_trip(db_session):
    run = register_run(
        db_session,
        mode=RunMode.BACKTEST,
        strategy_name="buy_and_hold",
        config={"symbol": "SPY"},
        random_seed=42,
        data_snapshot_id="snap-1",
        git_hash="deadbeef",
    )
    assert run.id
    assert run.git_hash == "deadbeef"

    updated = record_metrics(db_session, run, {"total_return_pct": 12.3, "sharpe": 1.1})
    assert updated.total_return_pct == 12.3
    assert updated.sharpe == 1.1


def test_validation_run_requires_reason(db_session):
    with pytest.raises(ValueError):
        register_run(
            db_session,
            mode=RunMode.BACKTEST,
            strategy_name="overnight_gap",
            config={},
            random_seed=1,
            is_validation_run=True,
        )


def test_record_trade_round_trip(db_session):
    run = register_run(
        db_session, mode=RunMode.BACKTEST, strategy_name="x", config={}, random_seed=1
    )
    trade = record_trade(
        db_session,
        run_id=run.id,
        timestamp=NOW,
        symbol="AAPL",
        side=TradeSide.BUY,
        quantity=10,
        price=150.0,
        strategy_id="x",
    )
    assert trade.id
    assert trade.symbol == "AAPL"


def test_record_news_item_preserves_raw_payload(db_session):
    raw = {"headline": "Fed holds rates", "source_id": "abc123", "nested": {"k": "v"}}
    item = record_news_item(
        db_session,
        source="reuters_rss",
        published_at=NOW,
        headline="Fed holds rates",
        raw_payload=raw,
        routed_symbols=["SPY", "QQQ", "TLT"],
    )
    assert item.raw_payload == raw
    assert item.routed_symbols == ["SPY", "QQQ", "TLT"]


def test_record_news_item_defaults_ingested_at_to_now(db_session):
    before = datetime.now(timezone.utc)
    item = record_news_item(
        db_session, source="rss", published_at=NOW, headline="live", raw_payload={},
    )
    after = datetime.now(timezone.utc)
    assert before <= item.ingested_at.replace(tzinfo=timezone.utc) <= after


def test_record_news_item_accepts_explicit_ingested_at_for_backfill(db_session):
    from datetime import timedelta

    backfilled_ingested_at = NOW + timedelta(minutes=15)
    item = record_news_item(
        db_session, source="alpaca_benzinga", published_at=NOW, headline="historical",
        raw_payload={}, ingested_at=backfilled_ingested_at,
    )
    assert item.ingested_at.replace(tzinfo=timezone.utc) == backfilled_ingested_at


def test_load_news_items_filters_by_published_at_range(db_session):
    from datetime import timedelta

    record_news_item(
        db_session, source="rss", published_at=NOW, headline="in range",
        raw_payload={}, routed_symbols=["SPY"], sentiment_score=0.5,
    )
    record_news_item(
        db_session, source="rss", published_at=NOW - timedelta(days=30), headline="too old",
        raw_payload={}, routed_symbols=["SPY"],
    )
    results = load_news_items(db_session, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1))
    assert len(results) == 1
    assert results[0].headline == "in range"
    assert results[0].routed_symbols == ("SPY",)
    assert results[0].sentiment_score == 0.5


def test_load_news_items_returns_tz_aware_timestamps(db_session):
    from datetime import timedelta

    # SQLite drops tzinfo on datetime round-trip regardless of what the
    # column declares -- a naive published_at/ingested_at here would raise
    # "can't compare offset-naive and offset-aware datetimes" the moment a
    # backtest sorts these against tz-aware Bar timestamps in
    # build_event_stream (only reproduces on a *second* read of an
    # already-cached range, since a fresh in-memory backfill never
    # round-trips through the DB before use).
    record_news_item(
        db_session, source="rss", published_at=NOW, headline="tz check",
        raw_payload={}, routed_symbols=["SPY"],
    )
    results = load_news_items(db_session, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1))
    assert results[0].published_at.tzinfo is not None
    assert results[0].ingested_at.tzinfo is not None
