"""Manual, human-initiated trades from the dashboard -- the two genuinely
generic, cross-cutting operations that don't belong to either the
prediction or the hypothesis pipeline: a raw order with no Prediction/
Hypothesis behind it, and closing whatever open broker position exists for
a symbol regardless of what opened it. Mirrors engine.prediction.trading
and engine.anticipatory.trading's shape and sizing philosophy exactly.

Every order still goes through RiskGate.evaluate() -- no exceptions, same
as every other order path in this codebase (SPEC.md hard constraint #2).
"""

from __future__ import annotations

from datetime import datetime, timezone

from engine.data.universe import Universe
from engine.execution.broker import Broker
from engine.execution.position_bookkeeping import apply_closing_fill, apply_opening_fill
from engine.execution.pricing import latest_price as _latest_price
from engine.execution.trade_result import CloseResult, TradeAttemptResult
from engine.journal.registry import (
    create_manual_trade,
    find_open_trade_by_symbol,
    mark_hypothesis_flat,
    mark_manual_trade_exited,
    mark_manual_trade_trade_rejected,
    mark_manual_trade_traded,
    mark_prediction_exited,
)
from engine.logging_setup import get_logger
from engine.risk.gate import RiskGate
from engine.risk.models import AccountState, OrderRequest, Side

logger = get_logger(__name__)

_STRATEGY_ID = "manual"


def open_manual_trade(
    session, broker: Broker, risk_gate: RiskGate, account: AccountState, universe: Universe,
    *, symbol: str, side: Side, quantity: float, submitted_by: str, note: str | None = None,
) -> TradeAttemptResult:
    """Raw manual order, no Prediction/Hypothesis behind it. Unlike the
    automatic loops' oversize-and-clip idiom, `quantity` is exactly what
    the operator asked for -- there's no "suggested size" to clip from,
    the human is the sizing input -- but RiskGate.evaluate still clips it
    to the cap like every other opening order. Only writes a ManualTrade
    row once RiskGate has approved the order (see ManualTrade's docstring
    for why a RiskGate rejection is never journaled)."""
    tradable = universe.tradable_symbols()
    price = _latest_price(symbol)
    if price is None:
        return TradeAttemptResult(ok=False, reason="no price data available")

    order = OrderRequest(
        symbol=symbol, side=side, quantity=quantity, price=price,
        timestamp=datetime.now(timezone.utc), strategy_id=_STRATEGY_ID,
    )
    decision = risk_gate.evaluate(order, account, tradable)
    if not decision.approved:
        logger.info(
            "manual trade rejected by RiskGate",
            extra={"extra_fields": {"symbol": symbol, "reason": decision.reason.value}},
        )
        return TradeAttemptResult(ok=False, reason=decision.detail or decision.reason.value)

    trade = create_manual_trade(
        session, symbol=symbol, side=side.value, requested_quantity=quantity, submitted_by=submitted_by, note=note,
    )
    try:
        broker_order = broker.submit_order(
            OrderRequest(
                symbol=symbol, side=side, quantity=decision.approved_quantity, price=price,
                timestamp=order.timestamp, strategy_id=_STRATEGY_ID,
            )
        )
    except Exception as exc:
        logger.error(
            "manual order rejected by broker",
            extra={"extra_fields": {"symbol": symbol, "side": side.value, "error": str(exc)}},
        )
        mark_manual_trade_trade_rejected(session, trade, reason=str(exc))
        return TradeAttemptResult(ok=False, reason=str(exc))

    apply_opening_fill(account, symbol, side, decision.approved_quantity, price, _STRATEGY_ID)
    mark_manual_trade_traded(session, trade, order_id=broker_order.broker_order_id, quantity=decision.approved_quantity)
    return TradeAttemptResult(ok=True)


def close_any_position(
    session, broker: Broker, risk_gate: RiskGate, account: AccountState, universe: Universe, symbol: str,
) -> CloseResult:
    """Close whatever open broker position exists for `symbol`, regardless
    of origin -- the dashboard's one path for closing ANY position. Sized
    and routed exactly like every other close in this codebase (opposite-
    side order for the live quantity; there is no dedicated broker
    "close one symbol" call anywhere in this codebase, see
    engine.execution.broker.Broker -- every existing close submits a plain
    opposite-side order instead).

    NOT_IN_UNIVERSE still applies to closes -- RiskGate.evaluate checks it
    before the closing short-circuit that skips cap checks -- so a
    position in a symbol that isn't (or is no longer) in universe.yaml
    genuinely cannot be closed through this path. That is surfaced as an
    ok=False CloseResult, never routed around; RiskGate is never bypassed."""
    tradable = universe.tradable_symbols()
    existing = account.positions.get(symbol)
    if existing is None or existing.quantity == 0:
        return CloseResult(ok=False, reason="no open position for this symbol")

    exit_side = Side.SELL if existing.quantity > 0 else Side.BUY
    price = _latest_price(symbol) or existing.avg_entry_price
    order = OrderRequest(
        symbol=symbol, side=exit_side, quantity=abs(existing.quantity), price=price,
        timestamp=datetime.now(timezone.utc), strategy_id="manual_close",
    )
    decision = risk_gate.evaluate(order, account, tradable)
    if not decision.approved:
        return CloseResult(ok=False, reason=f"RiskGate rejected close: {decision.detail or decision.reason.value}")

    try:
        broker_order = broker.submit_order(
            OrderRequest(
                symbol=symbol, side=exit_side, quantity=decision.approved_quantity, price=price,
                timestamp=order.timestamp, strategy_id="manual_close",
            )
        )
    except Exception as exc:
        return CloseResult(ok=False, reason=f"broker rejected close: {exc}")

    apply_closing_fill(account, risk_gate, symbol, existing, decision.approved_quantity, price)

    match = find_open_trade_by_symbol(session, symbol)
    if match is None:
        return CloseResult(ok=True, attribution="unattributed", broker_order_id=broker_order.broker_order_id)
    kind, row = match
    if kind == "prediction":
        mark_prediction_exited(session, row, order_id=broker_order.broker_order_id)
    elif kind == "hypothesis":
        mark_hypothesis_flat(session, row, exit_order_id=broker_order.broker_order_id)
    else:
        mark_manual_trade_exited(session, row, order_id=broker_order.broker_order_id)
    return CloseResult(ok=True, attribution=kind, broker_order_id=broker_order.broker_order_id)
