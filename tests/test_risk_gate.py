from datetime import datetime, timezone

import pytest

from engine.config.settings import RiskLimits
from engine.risk import AccountState, OrderRequest, Position, RejectionReason, RiskGate, Side

UNIVERSE = {"SPY", "AAPL", "QQQ"}
NOW = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)


def make_account(equity: float = 100_000.0, **overrides) -> AccountState:
    acct = AccountState(equity=equity, cash=equity, equity_at_session_start=equity)
    for key, value in overrides.items():
        setattr(acct, key, value)
    return acct


def make_order(symbol="AAPL", side=Side.BUY, quantity=100, price=100.0) -> OrderRequest:
    return OrderRequest(
        symbol=symbol, side=side, quantity=quantity, price=price, timestamp=NOW, strategy_id="test"
    )


@pytest.fixture
def gate() -> RiskGate:
    return RiskGate(RiskLimits())


def test_approves_order_within_all_caps(gate):
    account = make_account()
    order = make_order(quantity=10, price=100.0)  # $1,000 on $100k equity, well under 5%
    decision = gate.evaluate(order, account, UNIVERSE)
    assert decision.approved
    assert decision.approved_quantity == 10


def test_rejects_order_outside_universe(gate):
    account = make_account()
    order = make_order(symbol="TSLA")
    decision = gate.evaluate(order, account, UNIVERSE)
    assert not decision.approved
    assert decision.reason == RejectionReason.NOT_IN_UNIVERSE


def test_rejects_zero_or_negative_quantity(gate):
    account = make_account()
    decision = gate.evaluate(make_order(quantity=0), account, UNIVERSE)
    assert decision.reason == RejectionReason.ZERO_OR_NEGATIVE_QUANTITY


def test_position_size_cap_resizes_order(gate):
    # 5% of 100k = $5,000 max position. Order asks for $10,000.
    account = make_account()
    order = make_order(quantity=100, price=100.0)
    decision = gate.evaluate(order, account, UNIVERSE)
    assert decision.approved
    assert decision.approved_quantity == pytest.approx(50.0)  # $5,000 / $100


def test_position_size_cap_rejects_when_already_at_cap(gate):
    account = make_account()
    account.positions["AAPL"] = Position(symbol="AAPL", quantity=50, avg_entry_price=100.0)  # $5,000
    order = make_order(quantity=10, price=100.0)
    decision = gate.evaluate(order, account, UNIVERSE)
    assert not decision.approved
    assert decision.reason == RejectionReason.POSITION_SIZE_EXCEEDS_CAP


def test_total_exposure_cap_blocks_new_symbol(gate):
    # 20% of 100k = $20,000 max total exposure. Fill it with SPY, then try AAPL.
    account = make_account()
    account.positions["SPY"] = Position(symbol="SPY", quantity=40, avg_entry_price=500.0)  # $20,000
    order = make_order(symbol="AAPL", quantity=1, price=100.0)
    decision = gate.evaluate(order, account, UNIVERSE)
    assert not decision.approved
    assert decision.reason == RejectionReason.TOTAL_EXPOSURE_EXCEEDS_CAP


def test_closing_order_bypasses_position_cap(gate):
    account = make_account()
    account.positions["AAPL"] = Position(symbol="AAPL", quantity=500, avg_entry_price=100.0)
    order = make_order(symbol="AAPL", side=Side.SELL, quantity=500, price=100.0)
    decision = gate.evaluate(order, account, UNIVERSE)
    assert decision.approved
    assert decision.approved_quantity == 500


def test_closing_order_clips_to_position_size(gate):
    account = make_account()
    account.positions["AAPL"] = Position(symbol="AAPL", quantity=50, avg_entry_price=100.0)
    order = make_order(symbol="AAPL", side=Side.SELL, quantity=999, price=100.0)
    decision = gate.evaluate(order, account, UNIVERSE)
    assert decision.approved
    assert decision.approved_quantity == 50


def test_daily_drawdown_halts_trading(gate):
    account = make_account(equity=97_000.0, equity_at_session_start=100_000.0)  # 3% down
    decision = gate.evaluate(make_order(), account, UNIVERSE)
    assert not decision.approved
    assert decision.reason == RejectionReason.DAILY_DRAWDOWN_BREACHED
    assert account.halted


def test_daily_drawdown_below_threshold_does_not_halt(gate):
    account = make_account(equity=98_500.0, equity_at_session_start=100_000.0)  # 1.5% down
    decision = gate.evaluate(make_order(quantity=1), account, UNIVERSE)
    assert decision.approved
    assert not account.halted


def test_consecutive_losses_halts_trading(gate):
    account = make_account()
    for _ in range(4):
        gate.record_trade_result(account, realized_pnl=-100.0)
    assert account.halted
    decision = gate.evaluate(make_order(), account, UNIVERSE)
    assert not decision.approved
    assert decision.reason == RejectionReason.HALTED


def test_winning_trade_resets_consecutive_loss_counter(gate):
    account = make_account()
    for _ in range(3):
        gate.record_trade_result(account, realized_pnl=-100.0)
    gate.record_trade_result(account, realized_pnl=50.0)
    assert account.consecutive_losses_today == 0
    assert not account.halted


def test_halted_account_rejects_all_new_orders(gate):
    account = make_account()
    gate.trigger_kill_switch(account, reason="manual kill switch")
    decision = gate.evaluate(make_order(), account, UNIVERSE)
    assert not decision.approved
    assert decision.reason == RejectionReason.HALTED
    assert decision.detail == "manual kill switch"


def test_flatten_orders_generates_one_closing_order_per_position(gate):
    account = make_account()
    account.positions["AAPL"] = Position(symbol="AAPL", quantity=50, avg_entry_price=100.0)
    account.positions["SPY"] = Position(symbol="SPY", quantity=-10, avg_entry_price=500.0)
    orders = gate.flatten_orders(account, NOW, price_lookup=lambda s: 100.0)
    by_symbol = {o.symbol: o for o in orders}
    assert by_symbol["AAPL"].side == Side.SELL
    assert by_symbol["AAPL"].quantity == 50
    assert by_symbol["SPY"].side == Side.BUY
    assert by_symbol["SPY"].quantity == 10


def test_stop_loss_price_long_and_short(gate):
    assert gate.stop_loss_price(100.0, Side.BUY) == pytest.approx(98.0)
    assert gate.stop_loss_price(100.0, Side.SELL) == pytest.approx(102.0)


def test_is_stop_triggered_long_position(gate):
    position = Position(symbol="AAPL", quantity=10, avg_entry_price=100.0)
    assert gate.is_stop_triggered(position, current_price=97.9)
    assert not gate.is_stop_triggered(position, current_price=99.0)


def test_start_new_session_resets_daily_counters(gate):
    account = make_account()
    account.trades_today = 5
    account.consecutive_losses_today = 4
    account.halted = True
    account.equity = 95_000.0
    gate.start_new_session(account)
    assert account.trades_today == 0
    assert account.consecutive_losses_today == 0
    assert not account.halted
    assert account.equity_at_session_start == 95_000.0
