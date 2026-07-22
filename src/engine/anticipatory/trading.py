"""Connects anticipatory hypothesis beliefs to real (paper) orders --
mirrors engine.prediction.trading's shape and sizing philosophy. Every
order still goes through RiskGate.evaluate() -- no exceptions, same as
every other order path in this codebase (SPEC.md hard constraint #2).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from engine.data.bars import fetch_bars
from engine.data.universe import Universe
from engine.execution.broker import Broker
from engine.execution.position_bookkeeping import apply_closing_fill, apply_opening_fill
from engine.journal.models import Hypothesis, HypothesisAction, HypothesisBelief, PredictionDirection
from engine.journal.registry import load_open_hypotheses, mark_hypothesis_flat, mark_hypothesis_traded
from engine.logging_setup import get_logger
from engine.risk.gate import RiskGate
from engine.risk.models import AccountState, OrderRequest, Side

logger = get_logger(__name__)

_STRATEGY_ID = "anticipatory"
_MAX_SEVERITY = 3.0  # caps sizing at 3x the base cap even for an extreme gap


def _latest_price(symbol: str) -> float | None:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=7)
    df = fetch_bars([symbol], start=str(start), end=str(end + timedelta(days=1)), interval="1d")
    if df.empty:
        return None
    return float(df.sort_values("timestamp").iloc[-1]["close"])


def act_on_hypothesis_beliefs(
    session,
    broker: Broker,
    risk_gate: RiskGate,
    account: AccountState,
    universe: Universe,
    hypotheses: dict[str, Hypothesis],
    beliefs: list[HypothesisBelief],
    min_gap_threshold: float,
) -> tuple[int, int]:
    """Acts on OPENED (open a new position, sized by gap magnitude x
    confidence) and EXITED (close the whole open position -- V1 never
    partially trims/adds, mirrors engine.prediction.trading) beliefs.
    HELD/NO_GAP beliefs are journal-only, nothing to trade. `hypotheses`
    maps hypothesis_id -> Hypothesis; the caller already has them from the
    same revision sweep (engine.anticipatory.pipeline.revise_open_hypotheses),
    so this avoids a redundant re-query per belief."""
    tradable = universe.tradable_symbols()
    opened = closed = 0
    for belief in beliefs:
        hyp = hypotheses[belief.hypothesis_id]
        if belief.action == HypothesisAction.OPENED:
            if _open_position(session, broker, risk_gate, account, tradable, hyp, belief, min_gap_threshold):
                opened += 1
        elif belief.action == HypothesisAction.EXITED:
            if _close_position(session, broker, risk_gate, account, tradable, hyp):
                closed += 1
    return opened, closed


def flatten_resolved_hypotheses(
    session, broker: Broker, risk_gate: RiskGate, account: AccountState, universe: Universe, resolved: list[Hypothesis]
) -> int:
    """Force-close any real position still open on a Hypothesis that just
    got closed by resolution (revise_open_hypotheses's `resolved` return
    value) -- belt-and-suspenders in case the position wasn't already
    exited by a prior EXITED belief before Polymarket reported the market
    closed."""
    tradable = universe.tradable_symbols()
    flattened = 0
    for hyp in resolved:
        if hyp.position_side is None:
            continue
        if _close_position(session, broker, risk_gate, account, tradable, hyp):
            flattened += 1
    return flattened


def flatten_stopped_hypotheses(
    session, broker: Broker, risk_gate: RiskGate, account: AccountState, universe: Universe,
) -> int:
    """Close the real position on every open, currently-positioned
    Hypothesis whose current price has crossed RiskGate's stop-loss
    threshold -- independent of gap-based EXITED beliefs and Polymarket
    resolution. Hypotheses traded by this pipeline otherwise have no
    per-position stop protection at all (mirrors
    engine.prediction.trading.close_stopped_prediction_trades). Meant to
    be called every cycle regardless of anticipatory-loop's pause state,
    same as the daily-drawdown check -- see
    engine.cli.main.anticipatory_loop's docstring."""
    tradable = universe.tradable_symbols()
    flattened = 0
    for hyp in load_open_hypotheses(session):
        if hyp.position_side is None:
            continue
        existing = account.positions.get(hyp.symbol)
        if existing is None or existing.quantity == 0:
            continue  # already flat -- something else (e.g. a kill-switch flatten) closed it first

        price = _latest_price(hyp.symbol)
        if price is None or not risk_gate.is_stop_triggered(existing, current_price=price):
            continue

        logger.warning(
            "hypothesis stop-loss triggered", extra={"extra_fields": {"symbol": hyp.symbol, "price": price}},
        )
        if _close_position(session, broker, risk_gate, account, tradable, hyp):
            flattened += 1
    return flattened


def _open_position(
    session, broker: Broker, risk_gate: RiskGate, account: AccountState, tradable: set[str],
    hyp: Hypothesis, belief: HypothesisBelief, min_gap_threshold: float,
) -> bool:
    if hyp.symbol not in tradable:
        logger.warning(
            "hypothesis named a symbol outside the tradable universe -- skipped",
            extra={"extra_fields": {"symbol": hyp.symbol, "hypothesis_id": hyp.id}},
        )
        return False
    price = _latest_price(hyp.symbol)
    if price is None:
        logger.warning("no price data to size hypothesis trade -- skipped", extra={"extra_fields": {"symbol": hyp.symbol}})
        return False

    # gap > 0 means P_model says YES is more likely than the market prices
    # it -- go the same direction the symbol moves if YES happens
    # (direction_if_yes). gap < 0 means the market overprices YES relative
    # to our estimate -- go the opposite direction. This generalizes the
    # design doc's "long if underpriced" framing to a symbol whose
    # direction_if_yes is DOWN, not just UP.
    yes_more_likely_than_priced = belief.gap > 0
    direction_up = hyp.direction_if_yes == PredictionDirection.UP
    long_position = yes_more_likely_than_priced == direction_up
    side = Side.BUY if long_position else Side.SELL
    position_side = "long" if long_position else "short"

    cap_value = account.equity * risk_gate.limits.max_capital_per_position_pct
    severity = min(abs(belief.gap) / max(min_gap_threshold, 1e-6), _MAX_SEVERITY)  # 1.0 at threshold
    size_fraction = severity * belief.confidence
    candidate_qty = (cap_value * size_fraction * 2) / price  # oversized on purpose; RiskGate clips to the real cap

    order = OrderRequest(
        symbol=hyp.symbol, side=side, quantity=candidate_qty, price=price,
        timestamp=datetime.now(timezone.utc), strategy_id=_STRATEGY_ID,
    )
    decision = risk_gate.evaluate(order, account, tradable)
    if not decision.approved:
        logger.info(
            "hypothesis trade rejected by RiskGate",
            extra={"extra_fields": {"symbol": hyp.symbol, "reason": decision.reason.value}},
        )
        return False

    broker_order = broker.submit_order(
        OrderRequest(
            symbol=hyp.symbol, side=side, quantity=decision.approved_quantity, price=price,
            timestamp=order.timestamp, strategy_id=_STRATEGY_ID,
        )
    )
    apply_opening_fill(account, hyp.symbol, side, decision.approved_quantity, price, _STRATEGY_ID)
    mark_hypothesis_traded(session, hyp, order_id=broker_order.broker_order_id, quantity=decision.approved_quantity, side=position_side)
    return True


def _close_position(session, broker: Broker, risk_gate: RiskGate, account: AccountState, tradable: set[str], hyp: Hypothesis) -> bool:
    existing = account.positions.get(hyp.symbol)
    if existing is None or existing.quantity == 0:
        logger.warning(
            "no open broker position found for a hypothesis marked EXITED -- marking flat without an order "
            "(likely already closed by something else, e.g. a kill-switch flatten)",
            extra={"extra_fields": {"symbol": hyp.symbol, "hypothesis_id": hyp.id}},
        )
        mark_hypothesis_flat(session, hyp, exit_order_id="none:no_open_position")
        return True

    exit_side = Side.SELL if existing.quantity > 0 else Side.BUY
    qty = hyp.traded_quantity or abs(existing.quantity)
    price = _latest_price(hyp.symbol) or existing.avg_entry_price

    order = OrderRequest(
        symbol=hyp.symbol, side=exit_side, quantity=qty, price=price,
        timestamp=datetime.now(timezone.utc), strategy_id=_STRATEGY_ID,
    )
    decision = risk_gate.evaluate(order, account, tradable)
    if not decision.approved:
        logger.warning(
            "hypothesis exit rejected by RiskGate -- position stays open, will retry next cycle",
            extra={"extra_fields": {"symbol": hyp.symbol, "reason": decision.reason.value}},
        )
        return False

    broker_order = broker.submit_order(
        OrderRequest(
            symbol=hyp.symbol, side=exit_side, quantity=decision.approved_quantity, price=price,
            timestamp=order.timestamp, strategy_id=_STRATEGY_ID,
        )
    )
    apply_closing_fill(account, risk_gate, hyp.symbol, existing, decision.approved_quantity, price)
    mark_hypothesis_flat(session, hyp, exit_order_id=broker_order.broker_order_id)
    return True
