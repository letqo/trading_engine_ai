from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from engine.config.settings import RiskLimits
from engine.data.universe import Instrument, Universe
from engine.execution.broker import BrokerOrder
from engine.journal.models import PredictionDirection
from engine.journal.registry import record_prediction
from engine.prediction.trading import act_on_pending_predictions, close_expired_prediction_trades
from engine.risk.gate import RiskGate
from engine.risk.models import AccountState, Position, Side

NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 1, 31, tzinfo=timezone.utc)


def make_universe():
    return Universe(
        instruments=(
            Instrument(symbol="EWJ", tier=2, asset_class="equity_etf", news_topics=("boj",)),
        ),
        source_text="x",
    )


class FakeBroker:
    def __init__(self):
        self.submitted = []
        self._next_id = 0

    def get_account_equity(self):
        raise NotImplementedError

    def get_positions(self):
        raise NotImplementedError

    def get_open_orders(self):
        return []

    def submit_order(self, order):
        self._next_id += 1
        self.submitted.append(order)
        return BrokerOrder(
            broker_order_id=f"order-{self._next_id}", symbol=order.symbol, side=order.side,
            quantity=order.quantity, status="filled", filled_avg_price=order.price, submitted_at=order.timestamp,
        )

    def cancel_all_orders(self):
        pass

    def close_all_positions(self):
        pass


def make_prediction(session, *, direction, confidence=0.8, decision_ts=None, resolution_window_hours=24.0):
    decision_ts = decision_ts or NOW
    return record_prediction(
        session, news_headline="BOJ hikes rates", news_source="rss", news_published_at=decision_ts,
        news_decision_timestamp=decision_ts, topics=["boj"], symbol="EWJ", direction=direction,
        confidence=confidence, rationale="x", model_name="claude-opus-4-8", model_knowledge_cutoff=CUTOFF,
        forward_safe=True, resolution_window_hours=resolution_window_hours,
    )


def _price_bars(price):
    return pd.DataFrame([{
        "symbol": "EWJ", "timestamp": NOW, "open": price, "high": price, "low": price,
        "close": price, "volume": 1000, "timeframe": "1d",
    }])


def test_act_on_pending_predictions_opens_long_for_up_direction(db_session):
    make_prediction(db_session, direction=PredictionDirection.UP)
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    with patch("engine.prediction.trading.fetch_bars", return_value=_price_bars(100.0)):
        acted = act_on_pending_predictions(db_session, broker, risk_gate, account, make_universe(), min_confidence=0.6)

    assert len(acted) == 1
    assert acted[0].traded_order_id == "order-1"
    assert broker.submitted[0].side == Side.BUY
    assert account.positions["EWJ"].quantity == pytest.approx(50.0)  # 5,000 cap / 100 price


def test_act_on_pending_predictions_opens_short_for_down_direction(db_session):
    make_prediction(db_session, direction=PredictionDirection.DOWN)
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    with patch("engine.prediction.trading.fetch_bars", return_value=_price_bars(100.0)):
        acted = act_on_pending_predictions(db_session, broker, risk_gate, account, make_universe(), min_confidence=0.6)

    assert len(acted) == 1
    assert broker.submitted[0].side == Side.SELL
    assert account.positions["EWJ"].quantity == pytest.approx(-50.0)


def test_act_on_pending_predictions_skips_below_confidence_threshold(db_session):
    make_prediction(db_session, direction=PredictionDirection.UP, confidence=0.4)
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits())
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    with patch("engine.prediction.trading.fetch_bars", return_value=_price_bars(100.0)):
        acted = act_on_pending_predictions(db_session, broker, risk_gate, account, make_universe(), min_confidence=0.6)

    assert acted == []
    assert broker.submitted == []


def test_act_on_pending_predictions_respects_risk_gate_rejection(db_session):
    make_prediction(db_session, direction=PredictionDirection.UP)
    broker = FakeBroker()
    limits = RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0)
    risk_gate = RiskGate(limits)
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)
    # Already at the position cap for EWJ -> RiskGate must reject the new entry.
    account.positions["EWJ"] = Position(symbol="EWJ", quantity=50.0, avg_entry_price=100.0)

    with patch("engine.prediction.trading.fetch_bars", return_value=_price_bars(100.0)):
        acted = act_on_pending_predictions(db_session, broker, risk_gate, account, make_universe(), min_confidence=0.6)

    assert acted == []
    assert broker.submitted == []


def test_close_expired_prediction_trades_closes_long_with_profit(db_session):
    pred = make_prediction(db_session, direction=PredictionDirection.UP, decision_ts=NOW - timedelta(days=2))
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=5_000.0, equity_at_session_start=10_000.0)
    account.positions["EWJ"] = Position(symbol="EWJ", quantity=50.0, avg_entry_price=100.0)
    pred.traded_order_id = "order-1"
    pred.traded_quantity = 50.0
    db_session.add(pred)
    db_session.commit()

    with patch("engine.prediction.trading.fetch_bars", return_value=_price_bars(110.0)):
        closed = close_expired_prediction_trades(db_session, broker, risk_gate, account, make_universe(), as_of=NOW)

    assert len(closed) == 1
    assert closed[0].exit_order_id == "order-1"
    assert broker.submitted[0].side == Side.SELL
    assert broker.submitted[0].quantity == pytest.approx(50.0)
    assert "EWJ" not in account.positions
    assert account.trades_today == 1
    assert account.consecutive_losses_today == 0  # price rose -> long profited


def test_close_expired_prediction_trades_closes_short_with_loss(db_session):
    pred = make_prediction(db_session, direction=PredictionDirection.DOWN, decision_ts=NOW - timedelta(days=2))
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=15_000.0, equity_at_session_start=10_000.0)
    account.positions["EWJ"] = Position(symbol="EWJ", quantity=-50.0, avg_entry_price=100.0)
    pred.traded_order_id = "order-1"
    pred.traded_quantity = 50.0
    db_session.add(pred)
    db_session.commit()

    # Price rose -> a short here loses.
    with patch("engine.prediction.trading.fetch_bars", return_value=_price_bars(110.0)):
        closed = close_expired_prediction_trades(db_session, broker, risk_gate, account, make_universe(), as_of=NOW)

    assert len(closed) == 1
    assert broker.submitted[0].side == Side.BUY  # covering a short
    assert "EWJ" not in account.positions
    assert account.consecutive_losses_today == 1


def test_close_expired_prediction_trades_skips_when_no_open_position(db_session):
    pred = make_prediction(db_session, direction=PredictionDirection.UP, decision_ts=NOW - timedelta(days=2))
    pred.traded_order_id = "order-1"
    pred.traded_quantity = 50.0
    db_session.add(pred)
    db_session.commit()

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits())
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)  # no open EWJ position

    closed = close_expired_prediction_trades(db_session, broker, risk_gate, account, make_universe(), as_of=NOW)

    assert len(closed) == 1
    assert closed[0].exit_order_id == "none:no_open_position"
    assert broker.submitted == []


def test_close_expired_prediction_trades_ignores_trades_still_within_window(db_session):
    pred = make_prediction(db_session, direction=PredictionDirection.UP, decision_ts=NOW - timedelta(hours=1))
    pred.traded_order_id = "order-1"
    pred.traded_quantity = 50.0
    db_session.add(pred)
    db_session.commit()

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits())
    account = AccountState(equity=10_000.0, cash=5_000.0, equity_at_session_start=10_000.0)
    account.positions["EWJ"] = Position(symbol="EWJ", quantity=50.0, avg_entry_price=100.0)

    closed = close_expired_prediction_trades(db_session, broker, risk_gate, account, make_universe(), as_of=NOW)

    assert closed == []
    assert broker.submitted == []
    assert "EWJ" in account.positions
