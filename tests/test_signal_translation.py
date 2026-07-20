from datetime import datetime, timezone

from engine.domain import Signal, SignalAction
from engine.execution.signal_translation import signal_to_side
from engine.risk.models import Side

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


def _signal(action: SignalAction) -> Signal:
    return Signal(symbol="TEST", action=action, strategy_id="x", timestamp=T0)


def test_buy_signal_is_always_buy_side():
    assert signal_to_side(_signal(SignalAction.BUY), existing_quantity=0.0) == Side.BUY
    assert signal_to_side(_signal(SignalAction.BUY), existing_quantity=-50.0) == Side.BUY  # covers a short


def test_sell_signal_is_always_sell_side():
    assert signal_to_side(_signal(SignalAction.SELL), existing_quantity=0.0) == Side.SELL
    assert signal_to_side(_signal(SignalAction.SELL), existing_quantity=50.0) == Side.SELL  # closes a long


def test_close_signal_flattens_whichever_direction_is_open():
    assert signal_to_side(_signal(SignalAction.CLOSE), existing_quantity=50.0) == Side.SELL
    assert signal_to_side(_signal(SignalAction.CLOSE), existing_quantity=-50.0) == Side.BUY


def test_close_signal_with_nothing_open_is_a_noop():
    assert signal_to_side(_signal(SignalAction.CLOSE), existing_quantity=0.0) is None
