from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from engine.config.settings import RiskLimits
from engine.data.universe import Instrument, Universe
from engine.execution.broker import BrokerOrder
from engine.journal.models import PredictionDirection
from engine.journal.registry import record_prediction
from engine.prediction.trading import act_on_pending_predictions, close_expired_prediction_trades, close_stopped_prediction_trades
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


def make_two_symbol_universe():
    return Universe(
        instruments=(
            Instrument(symbol="EWJ", tier=2, asset_class="equity_etf", news_topics=("boj",)),
            Instrument(symbol="XLE", tier=2, asset_class="equity_etf", news_topics=("energy",)),
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


class _RaisingBroker(FakeBroker):
    """A broker that rejects orders for specific symbols, like Alpaca
    refusing to short-sell a non-shortable asset (422)."""

    def __init__(self, fail_symbols):
        super().__init__()
        self.fail_symbols = set(fail_symbols)

    def submit_order(self, order):
        if order.symbol in self.fail_symbols:
            raise RuntimeError(f"422 Client Error: Unprocessable Entity for {order.symbol}")
        return super().submit_order(order)


def make_prediction(session, *, direction, confidence=0.8, decision_ts=None, resolution_window_hours=24.0, in_tracked_universe=True, symbol="EWJ"):
    decision_ts = decision_ts or NOW
    return record_prediction(
        session, news_headline="BOJ hikes rates", news_source="rss", news_published_at=decision_ts,
        news_decision_timestamp=decision_ts, topics=["boj"], symbol=symbol, direction=direction,
        confidence=confidence, rationale="x", model_name="claude-opus-4-8", model_knowledge_cutoff=CUTOFF,
        forward_safe=True, resolution_window_hours=resolution_window_hours, in_tracked_universe=in_tracked_universe,
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


def test_act_on_pending_predictions_marks_broker_rejection_without_retrying(db_session):
    pred = make_prediction(db_session, direction=PredictionDirection.DOWN)  # short -> the one Alpaca might refuse
    broker = _RaisingBroker(fail_symbols={"EWJ"})
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    with patch("engine.prediction.trading.fetch_bars", return_value=_price_bars(100.0)):
        acted = act_on_pending_predictions(db_session, broker, risk_gate, account, make_universe(), min_confidence=0.6)

    assert acted == []
    assert pred.trade_rejected is True
    assert "422" in pred.trade_rejection_reason
    assert pred.traded_order_id is None  # never actually traded
    assert "EWJ" not in account.positions  # no fill applied

    # Re-running must not retry it -- load_actionable_predictions excludes
    # trade_rejected rows.
    with patch("engine.prediction.trading.fetch_bars", return_value=_price_bars(100.0)):
        acted_again = act_on_pending_predictions(db_session, broker, risk_gate, account, make_universe(), min_confidence=0.6)
    assert acted_again == []
    assert broker.submitted == []


def test_act_on_pending_predictions_broker_rejection_does_not_block_other_predictions(db_session):
    failing = make_prediction(db_session, direction=PredictionDirection.DOWN, symbol="EWJ")
    healthy = make_prediction(db_session, direction=PredictionDirection.UP, symbol="XLE")
    broker = _RaisingBroker(fail_symbols={"EWJ"})
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    with patch("engine.prediction.trading.fetch_bars", return_value=_price_bars(100.0)):
        acted = act_on_pending_predictions(db_session, broker, risk_gate, account, make_two_symbol_universe(), min_confidence=0.6)

    # The rejected EWJ prediction must not abort the batch -- XLE still trades.
    assert [p.id for p in acted] == [healthy.id]
    assert failing.trade_rejected is True
    assert healthy.traded_order_id is not None
    assert "XLE" in account.positions


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


def test_close_expired_prediction_trades_broker_rejection_leaves_position_open_for_retry(db_session):
    pred = make_prediction(db_session, direction=PredictionDirection.UP, decision_ts=NOW - timedelta(days=2))
    broker = _RaisingBroker(fail_symbols={"EWJ"})
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=5_000.0, equity_at_session_start=10_000.0)
    account.positions["EWJ"] = Position(symbol="EWJ", quantity=50.0, avg_entry_price=100.0)
    pred.traded_order_id = "order-1"
    pred.traded_quantity = 50.0
    db_session.add(pred)
    db_session.commit()

    with patch("engine.prediction.trading.fetch_bars", return_value=_price_bars(110.0)):
        closed = close_expired_prediction_trades(db_session, broker, risk_gate, account, make_universe(), as_of=NOW)

    # Unlike an open rejection, a close must NOT be given up on -- the
    # position stays open (and traded) so it's retried next cycle, rather
    # than being marked exited when no real exit ever happened.
    assert closed == []
    assert pred.exit_order_id is None
    assert pred.trade_rejected is False  # rejection tracking is open-only, closes always retry
    assert "EWJ" in account.positions


def test_close_stopped_prediction_trades_closes_long_when_stop_triggered(db_session):
    pred = make_prediction(db_session, direction=PredictionDirection.UP, decision_ts=NOW - timedelta(hours=1))
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0, stop_loss_pct=0.02))
    account = AccountState(equity=10_000.0, cash=5_000.0, equity_at_session_start=10_000.0)
    account.positions["EWJ"] = Position(symbol="EWJ", quantity=50.0, avg_entry_price=100.0)
    pred.traded_order_id = "order-1"
    pred.traded_quantity = 50.0
    db_session.add(pred)
    db_session.commit()

    # 2% stop on a 100.0 long entry triggers at/below 98.0.
    with patch("engine.prediction.trading.fetch_bars", return_value=_price_bars(97.0)):
        stopped = close_stopped_prediction_trades(db_session, broker, risk_gate, account, make_universe())

    assert len(stopped) == 1
    assert stopped[0].exit_order_id == "order-1"
    assert broker.submitted[0].side == Side.SELL
    assert "EWJ" not in account.positions
    assert account.consecutive_losses_today == 1


def test_close_stopped_prediction_trades_leaves_position_open_within_stop(db_session):
    pred = make_prediction(db_session, direction=PredictionDirection.UP, decision_ts=NOW - timedelta(hours=1))
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0, stop_loss_pct=0.02))
    account = AccountState(equity=10_000.0, cash=5_000.0, equity_at_session_start=10_000.0)
    account.positions["EWJ"] = Position(symbol="EWJ", quantity=50.0, avg_entry_price=100.0)
    pred.traded_order_id = "order-1"
    pred.traded_quantity = 50.0
    db_session.add(pred)
    db_session.commit()

    # 99.0 is above the 98.0 stop -- must not close, even though this
    # cycle runs regardless of predict-loop's pause state.
    with patch("engine.prediction.trading.fetch_bars", return_value=_price_bars(99.0)):
        stopped = close_stopped_prediction_trades(db_session, broker, risk_gate, account, make_universe())

    assert stopped == []
    assert broker.submitted == []
    assert "EWJ" in account.positions


def test_close_stopped_prediction_trades_ignores_predictions_never_traded(db_session):
    make_prediction(db_session, direction=PredictionDirection.UP, decision_ts=NOW - timedelta(hours=1))
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(stop_loss_pct=0.02))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    with patch("engine.prediction.trading.fetch_bars", return_value=_price_bars(1.0)):
        stopped = close_stopped_prediction_trades(db_session, broker, risk_gate, account, make_universe())

    assert stopped == []
    assert broker.submitted == []


def test_close_stopped_prediction_trades_skips_when_no_open_position(db_session):
    pred = make_prediction(db_session, direction=PredictionDirection.UP, decision_ts=NOW - timedelta(hours=1))
    pred.traded_order_id = "order-1"
    pred.traded_quantity = 50.0
    db_session.add(pred)
    db_session.commit()

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(stop_loss_pct=0.02))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)  # no open EWJ position

    stopped = close_stopped_prediction_trades(db_session, broker, risk_gate, account, make_universe())

    # No open position to stop out of -- must NOT be marked exited (that
    # would be a false audit trail; a real exit never happened here).
    assert stopped == []
    assert broker.submitted == []
    assert pred.exit_order_id is None


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
