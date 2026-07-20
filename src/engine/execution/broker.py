"""Broker interface. SPEC.md: "the broker client must be an interface so it
can be swapped" -- Alpaca now, IBKR/Tradovate sandboxes for futures later.
Nothing outside engine.execution may import a concrete broker class
directly; everything talks to `Broker`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from engine.risk.models import OrderRequest, Position, Side


@dataclass(frozen=True)
class BrokerOrder:
    broker_order_id: str
    symbol: str
    side: Side
    quantity: float
    status: str  # "new" | "filled" | "partially_filled" | "canceled" | "rejected"
    filled_avg_price: float | None
    submitted_at: datetime


class Broker(Protocol):
    def get_account_equity(self) -> float: ...

    def get_positions(self) -> dict[str, Position]: ...

    def get_open_orders(self) -> list[BrokerOrder]: ...

    def submit_order(self, order: OrderRequest) -> BrokerOrder: ...

    def cancel_all_orders(self) -> None: ...

    def close_all_positions(self) -> None: ...
