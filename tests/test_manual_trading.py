from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest
from sqlmodel import select

from engine.config.settings import RiskLimits
from engine.data.universe import Instrument, Universe
from engine.execution.broker import BrokerOrder
from engine.execution.manual_trading import close_any_position, open_manual_trade
from engine.journal.models import ManualTrade, PredictionDirection
from engine.journal.registry import (
    create_hypothesis,
    create_manual_trade,
    find_open_trade_by_symbol,
    mark_hypothesis_traded,
    mark_manual_trade_traded,
    record_prediction,
)
from engine.risk.gate import RiskGate
from engine.risk.models import AccountState, Position, Side

NOW = datetime(2026, 7, 23, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 1, 31, tzinfo=timezone.utc)


def make_universe():
    return Universe(
        instruments=(Instrument(symbol="EWJ", tier=2, asset_class="equity_etf", news_topics=("boj",)),),
        source_text="x",
    )


def make_universe_missing_symbol():
    """A universe that simply doesn't name EWJ -- e.g. it was removed from
    universe.yaml after a position was opened."""
    return Universe(
        instruments=(Instrument(symbol="XLE", tier=2, asset_class="equity_etf", news_topics=("energy",)),),
        source_text="x",
    )


class FakeBroker:
    def __init__(self):
        self.submitted = []
        self._next_id = 0

    def submit_order(self, order):
        self._next_id += 1
        self.submitted.append(order)
        return BrokerOrder(
            broker_order_id=f"order-{self._next_id}", symbol=order.symbol, side=order.side,
            quantity=order.quantity, status="filled", filled_avg_price=order.price, submitted_at=order.timestamp,
        )


class _RaisingBroker(FakeBroker):
    def __init__(self, fail_symbols):
        super().__init__()
        self.fail_symbols = set(fail_symbols)

    def submit_order(self, order):
        if order.symbol in self.fail_symbols:
            raise RuntimeError(f"422 Client Error: Unprocessable Entity for {order.symbol}")
        return super().submit_order(order)


def _price_bars(price):
    return pd.DataFrame([{
        "symbol": "EWJ", "timestamp": NOW, "open": price, "high": price, "low": price,
        "close": price, "volume": 1000, "timeframe": "1d",
    }])


def test_open_manual_trade_approved_creates_row_and_applies_fill(db_session):
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(100.0)):
        result = open_manual_trade(
            db_session, broker, risk_gate, account, make_universe(),
            symbol="EWJ", side=Side.BUY, quantity=10.0, submitted_by="admin", note="testing",
        )

    assert result.ok is True
    assert account.positions["EWJ"].quantity == pytest.approx(10.0)
    rows = db_session.exec(select(ManualTrade)).all()
    assert len(rows) == 1
    assert rows[0].traded_order_id == "order-1"
    assert rows[0].submitted_by == "admin"
    assert rows[0].note == "testing"


def test_open_manual_trade_risk_gate_rejection_creates_no_row(db_session):
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)
    # Already at the position cap for EWJ -> RiskGate must reject the new entry.
    account.positions["EWJ"] = Position(symbol="EWJ", quantity=50.0, avg_entry_price=100.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(100.0)):
        result = open_manual_trade(
            db_session, broker, risk_gate, account, make_universe(),
            symbol="EWJ", side=Side.BUY, quantity=10.0, submitted_by="admin",
        )

    assert result.ok is False
    assert broker.submitted == []
    rows = db_session.exec(select(ManualTrade)).all()
    assert rows == []


def test_open_manual_trade_broker_rejection_marks_trade_rejected(db_session):
    broker = _RaisingBroker(fail_symbols={"EWJ"})
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(100.0)):
        result = open_manual_trade(
            db_session, broker, risk_gate, account, make_universe(),
            symbol="EWJ", side=Side.SELL, quantity=10.0, submitted_by="admin",
        )

    assert result.ok is False
    rows = db_session.exec(select(ManualTrade)).all()
    assert len(rows) == 1
    assert rows[0].trade_rejected is True
    assert rows[0].traded_order_id is None
    assert "EWJ" not in account.positions


def test_close_any_position_no_open_position(db_session):
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits())
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    result = close_any_position(db_session, broker, risk_gate, account, make_universe(), "EWJ")

    assert result.ok is False
    assert broker.submitted == []


def test_close_any_position_unattributed(db_session):
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)
    account.positions["EWJ"] = Position(symbol="EWJ", quantity=10.0, avg_entry_price=100.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(110.0)):
        result = close_any_position(db_session, broker, risk_gate, account, make_universe(), "EWJ")

    assert result.ok is True
    assert result.attribution == "unattributed"
    assert "EWJ" not in account.positions


def test_close_any_position_attributes_to_prediction(db_session):
    pred = record_prediction(
        db_session, news_headline="h", news_source="rss", news_published_at=NOW, news_decision_timestamp=NOW,
        topics=[], symbol="EWJ", direction=PredictionDirection.UP, confidence=0.8, rationale="x",
        model_name="claude-opus-4-8", model_knowledge_cutoff=CUTOFF, forward_safe=True,
        resolution_window_hours=24.0, in_tracked_universe=True,
    )
    pred.traded_order_id = "order-1"
    pred.traded_quantity = 10.0
    db_session.add(pred)
    db_session.commit()

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)
    account.positions["EWJ"] = Position(symbol="EWJ", quantity=10.0, avg_entry_price=100.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(110.0)):
        result = close_any_position(db_session, broker, risk_gate, account, make_universe(), "EWJ")

    assert result.ok is True
    assert result.attribution == "prediction"
    db_session.refresh(pred)
    assert pred.exit_order_id is not None


def test_close_any_position_attributes_to_hypothesis(db_session):
    hyp = create_hypothesis(db_session, market_id="m1", question="Q?", symbol="EWJ", direction_if_yes=PredictionDirection.UP)
    mark_hypothesis_traded(db_session, hyp, order_id="order-1", quantity=10.0, side="long")

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)
    account.positions["EWJ"] = Position(symbol="EWJ", quantity=10.0, avg_entry_price=100.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(110.0)):
        result = close_any_position(db_session, broker, risk_gate, account, make_universe(), "EWJ")

    assert result.ok is True
    assert result.attribution == "hypothesis"
    db_session.refresh(hyp)
    assert hyp.position_side is None


def test_close_any_position_attributes_to_manual_trade(db_session):
    trade = create_manual_trade(db_session, symbol="EWJ", side="buy", requested_quantity=10.0, submitted_by="admin")
    mark_manual_trade_traded(db_session, trade, order_id="order-1", quantity=10.0)

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)
    account.positions["EWJ"] = Position(symbol="EWJ", quantity=10.0, avg_entry_price=100.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(110.0)):
        result = close_any_position(db_session, broker, risk_gate, account, make_universe(), "EWJ")

    assert result.ok is True
    assert result.attribution == "manual_trade"
    db_session.refresh(trade)
    assert trade.exit_order_id is not None


def test_close_any_position_rejected_when_symbol_not_in_universe(db_session):
    """RiskGate's NOT_IN_UNIVERSE check applies even to closes -- a position
    in a symbol no longer in universe.yaml genuinely cannot be closed
    through this path, and that must never be silently routed around."""
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)
    account.positions["EWJ"] = Position(symbol="EWJ", quantity=10.0, avg_entry_price=100.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(110.0)):
        result = close_any_position(db_session, broker, risk_gate, account, make_universe_missing_symbol(), "EWJ")

    assert result.ok is False
    assert "not in trading universe" in result.reason or "NOT_IN_UNIVERSE" in result.reason or "not_in_universe" in result.reason
    assert broker.submitted == []
    assert "EWJ" in account.positions  # never closed


def test_find_open_trade_by_symbol_no_match(db_session):
    assert find_open_trade_by_symbol(db_session, "EWJ") is None
