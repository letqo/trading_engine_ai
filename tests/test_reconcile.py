from datetime import datetime, timezone

from engine.execution.reconcile import cancel_stale_orders, reconcile_account_state
from engine.execution.broker import BrokerOrder
from engine.risk.models import Position, Side


class FakeBroker:
    def __init__(self, equity, positions, open_orders):
        self._equity = equity
        self._positions = positions
        self._open_orders = open_orders
        self.canceled = False

    def get_account_equity(self):
        return self._equity

    def get_positions(self):
        return self._positions

    def get_open_orders(self):
        return self._open_orders

    def cancel_all_orders(self):
        self.canceled = True

    def submit_order(self, order):
        raise NotImplementedError

    def close_all_positions(self):
        raise NotImplementedError


def test_reconcile_account_state_reflects_broker_truth():
    positions = {"AAPL": Position(symbol="AAPL", quantity=10, avg_entry_price=150.0)}
    broker = FakeBroker(equity=50_000.0, positions=positions, open_orders=[])
    account = reconcile_account_state(broker)
    assert account.equity == 50_000.0
    assert account.cash == 50_000.0
    assert account.positions == positions
    assert account.equity_at_session_start == 50_000.0


def test_cancel_stale_orders_cancels_when_orders_exist():
    order = BrokerOrder(
        broker_order_id="1", symbol="AAPL", side=Side.BUY, quantity=10, status="new",
        filled_avg_price=None, submitted_at=datetime.now(timezone.utc),
    )
    broker = FakeBroker(equity=1000.0, positions={}, open_orders=[order])
    stale = cancel_stale_orders(broker)
    assert stale == [order]
    assert broker.canceled is True


def test_cancel_stale_orders_noop_when_none_open():
    broker = FakeBroker(equity=1000.0, positions={}, open_orders=[])
    stale = cancel_stale_orders(broker)
    assert stale == []
    assert broker.canceled is False
