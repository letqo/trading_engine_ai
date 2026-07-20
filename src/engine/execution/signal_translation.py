"""Signal -> broker Side translation, shared by the backtester
(engine.backtest.engine) and the live loop (engine.execution.live_loop) so
the long/short semantics can never drift between the two -- one of
SPEC.md's architecture requirements is that a Strategy's signals mean the
same thing regardless of which of those two calls it.

BUY opens/adds a long (or covers an existing short). SELL opens/adds a
short (or closes an existing long). CLOSE flattens whichever direction is
actually open; if nothing is open, it's a no-op (None).
"""

from __future__ import annotations

from engine.domain import Signal, SignalAction
from engine.risk.models import Side


def signal_to_side(signal: Signal, existing_quantity: float) -> Side | None:
    if signal.action == SignalAction.BUY:
        return Side.BUY
    if signal.action == SignalAction.SELL:
        return Side.SELL
    # CLOSE
    if existing_quantity == 0:
        return None
    return Side.SELL if existing_quantity > 0 else Side.BUY
