"""Shared in-memory position bookkeeping for real (paper) order fills --
used by both engine.prediction.trading (prediction-driven trades) and
engine.execution.live_loop (strategy-driven live trades), so this long/
short open/close math has exactly one implementation to stay correct in.

Mirrors engine.backtest.engine's fill logic (_execute_fill/_realize_close),
translated for a real broker fill instead of a simulated one: there's no
cash tracking here, since the broker owns real cash and the next
reconcile/refresh reads the true value back from it.
"""

from __future__ import annotations

from engine.risk.gate import RiskGate
from engine.risk.models import AccountState, Position, Side


def apply_opening_fill(
    account: AccountState, symbol: str, side: Side, quantity: float, price: float, strategy_id: str,
) -> None:
    """BUY opens/adds a long, SELL opens/adds a short."""
    existing = account.positions.get(symbol)
    signed_qty = quantity if side == Side.BUY else -quantity
    if existing is None or existing.quantity == 0:
        account.positions[symbol] = Position(
            symbol=symbol, quantity=signed_qty, avg_entry_price=price, strategy_id=strategy_id,
        )
    else:
        total_cost = existing.avg_entry_price * abs(existing.quantity) + price * quantity
        new_qty_abs = abs(existing.quantity) + quantity
        existing.avg_entry_price = total_cost / new_qty_abs
        existing.quantity = new_qty_abs if existing.quantity > 0 else -new_qty_abs


def apply_closing_fill(
    account: AccountState, risk_gate: RiskGate, symbol: str, position: Position, quantity: float, price: float,
) -> None:
    """Reduce/remove an existing position and feed realized P&L into
    RiskGate's consecutive-loss tracking, same as every other close path
    in this codebase."""
    if position.quantity > 0:  # was long
        realized_pnl = (price - position.avg_entry_price) * quantity
    else:  # was short
        realized_pnl = (position.avg_entry_price - price) * quantity
    risk_gate.record_trade_result(account, realized_pnl)

    remaining = abs(position.quantity) - quantity
    if remaining <= 1e-9:
        del account.positions[symbol]
    else:
        position.quantity = remaining if position.quantity > 0 else -remaining
