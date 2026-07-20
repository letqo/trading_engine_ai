from datetime import datetime, timezone

import pytest

from engine.journal.models import RunMode, TradeSide
from engine.journal.registry import (
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
