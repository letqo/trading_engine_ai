"""Connects the consequence-prediction forward-test log to real (paper)
orders: act on confident predictions immediately, then close the position
again once the resolution window ends -- rather than only ever logging a
hypothetical outcome computed from historical bars.

This does not change what makes the log honest: forward_safe still gates
which predictions may ever count as evidence of skill (engine.prediction.pipeline),
and resolve_pending_predictions still scores every prediction -- traded or
not -- against real price data the same way. This module only adds a
second, parallel consequence for a *subset* of predictions (confidence >=
threshold): submitting and later closing a real order.

Every order still goes through RiskGate.evaluate() -- no exceptions, same
as every other order path in this codebase (SPEC.md hard constraint #2).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from engine.data.bars import fetch_bars
from engine.data.universe import Universe
from engine.execution.broker import Broker
from engine.execution.position_bookkeeping import apply_closing_fill, apply_opening_fill
from engine.journal.models import Prediction, PredictionDirection
from engine.journal.registry import (
    load_actionable_predictions,
    load_expired_open_trades,
    mark_prediction_exited,
    mark_prediction_traded,
)
from engine.logging_setup import get_logger
from engine.risk.gate import RiskGate
from engine.risk.models import AccountState, OrderRequest, Side

logger = get_logger(__name__)


def _latest_price(symbol: str) -> float | None:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=7)
    df = fetch_bars([symbol], start=str(start), end=str(end + timedelta(days=1)), interval="1d")
    if df.empty:
        return None
    return float(df.sort_values("timestamp").iloc[-1]["close"])


def act_on_pending_predictions(
    session, broker: Broker, risk_gate: RiskGate, account: AccountState, universe: Universe, min_confidence: float,
) -> list[Prediction]:
    """Submit a real paper order for every actionable prediction (pending,
    forward_safe, confidence >= threshold, not yet traded). "up" -> BUY
    (long), "down" -> SELL (short) -- both directions, sized the same way
    RiskGate sizes every other opening order."""
    tradable = universe.tradable_symbols()
    acted: list[Prediction] = []
    for prediction in load_actionable_predictions(session, min_confidence=min_confidence):
        if prediction.symbol not in tradable:
            logger.warning(
                "actionable prediction named a symbol outside the tradable universe -- skipped",
                extra={"extra_fields": {"symbol": prediction.symbol}},
            )
            continue

        price = _latest_price(prediction.symbol)
        if price is None:
            logger.warning("no price data to size prediction trade -- skipped", extra={"extra_fields": {"symbol": prediction.symbol}})
            continue

        side = Side.BUY if prediction.direction == PredictionDirection.UP else Side.SELL
        cap_value = account.equity * risk_gate.limits.max_capital_per_position_pct
        candidate_qty = (cap_value * 2) / price  # oversized on purpose; RiskGate clips to the real cap

        order = OrderRequest(
            symbol=prediction.symbol, side=side, quantity=candidate_qty, price=price,
            timestamp=datetime.now(timezone.utc), strategy_id="consequence_prediction",
        )
        decision = risk_gate.evaluate(order, account, tradable)
        if not decision.approved:
            logger.info(
                "prediction trade rejected by RiskGate",
                extra={"extra_fields": {"symbol": prediction.symbol, "reason": decision.reason.value}},
            )
            continue

        broker_order = broker.submit_order(
            OrderRequest(
                symbol=prediction.symbol, side=side, quantity=decision.approved_quantity, price=price,
                timestamp=order.timestamp, strategy_id="consequence_prediction",
            )
        )
        apply_opening_fill(account, prediction.symbol, side, decision.approved_quantity, price, "consequence_prediction")
        mark_prediction_traded(session, prediction, order_id=broker_order.broker_order_id, quantity=decision.approved_quantity)
        acted.append(prediction)
    return acted


def close_expired_prediction_trades(
    session, broker: Broker, risk_gate: RiskGate, account: AccountState, universe: Universe, as_of: datetime | None = None,
) -> list[Prediction]:
    """Close the real position linked to every traded prediction whose
    resolution window has closed. Runs independently of (and after) the
    scoring resolve -- scoring uses historical bars as ground truth either
    way; this just realizes the actual paper P&L for the subset that traded."""
    as_of = as_of or datetime.now(timezone.utc)
    tradable = universe.tradable_symbols()
    closed: list[Prediction] = []
    for prediction in load_expired_open_trades(session, as_of):
        existing = account.positions.get(prediction.symbol)
        if existing is None or existing.quantity == 0:
            logger.warning(
                "no open broker position found for a traded prediction -- marking exited without an order "
                "(likely already closed by something else, e.g. a kill-switch flatten)",
                extra={"extra_fields": {"symbol": prediction.symbol, "prediction_id": prediction.id}},
            )
            closed.append(mark_prediction_exited(session, prediction, order_id="none:no_open_position"))
            continue

        exit_side = Side.SELL if existing.quantity > 0 else Side.BUY
        qty = prediction.traded_quantity or abs(existing.quantity)
        price = _latest_price(prediction.symbol) or existing.avg_entry_price

        order = OrderRequest(
            symbol=prediction.symbol, side=exit_side, quantity=qty, price=price,
            timestamp=as_of, strategy_id="consequence_prediction",
        )
        decision = risk_gate.evaluate(order, account, tradable)
        if not decision.approved:
            logger.warning(
                "prediction exit rejected by RiskGate -- position stays open, will retry next run",
                extra={"extra_fields": {"symbol": prediction.symbol, "reason": decision.reason.value}},
            )
            continue

        broker_order = broker.submit_order(
            OrderRequest(
                symbol=prediction.symbol, side=exit_side, quantity=decision.approved_quantity, price=price,
                timestamp=as_of, strategy_id="consequence_prediction",
            )
        )
        apply_closing_fill(account, risk_gate, prediction.symbol, existing, decision.approved_quantity, price)
        mark_prediction_exited(session, prediction, order_id=broker_order.broker_order_id)
        closed.append(prediction)
    return closed
