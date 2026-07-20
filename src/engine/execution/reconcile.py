"""Startup reconciliation. SPEC.md Deployment/Restart semantics: "On startup
it reconciles state with the broker (open positions, open orders) before
emitting any new signals. Railway restarts and deploys must never result in
duplicate or orphaned orders."

This module builds the AccountState the live loop should start from -- it
never trusts in-memory state left over from a previous process, only what
the broker currently reports.
"""

from __future__ import annotations

from engine.execution.broker import Broker, BrokerOrder
from engine.logging_setup import get_logger
from engine.risk.models import AccountState

logger = get_logger(__name__)


def reconcile_account_state(broker: Broker) -> AccountState:
    equity = broker.get_account_equity()
    positions = broker.get_positions()
    account = AccountState(equity=equity, cash=equity, positions=positions, equity_at_session_start=equity)
    logger.info(
        "startup reconciliation complete",
        extra={"extra_fields": {"equity": equity, "open_positions": list(positions.keys())}},
    )
    return account


def cancel_stale_orders(broker: Broker) -> list[BrokerOrder]:
    """Any order still open from a previous process incarnation is stale --
    this process has no record of why it was submitted, so the safe move is
    to cancel and let the strategy re-decide from current state, never to
    resume tracking it as if this process had submitted it."""
    open_orders = broker.get_open_orders()
    if open_orders:
        logger.warning(
            "canceling stale open orders from a previous run",
            extra={"extra_fields": {"count": len(open_orders)}},
        )
        broker.cancel_all_orders()
    return open_orders
