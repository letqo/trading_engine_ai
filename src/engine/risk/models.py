from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class OrderRequest:
    """A proposed order, not yet approved. Nothing downstream of RiskGate
    should accept an order that didn't come out of RiskGate.evaluate()."""

    symbol: str
    side: Side
    quantity: float
    price: float
    timestamp: datetime
    strategy_id: str


class RejectionReason(str, Enum):
    NONE = "none"
    HALTED = "halted"
    POSITION_SIZE_EXCEEDS_CAP = "position_size_exceeds_cap"
    TOTAL_EXPOSURE_EXCEEDS_CAP = "total_exposure_exceeds_cap"
    DAILY_DRAWDOWN_BREACHED = "daily_drawdown_breached"
    CONSECUTIVE_LOSSES_BREACHED = "consecutive_losses_breached"
    OUTSIDE_TRADING_SESSION = "outside_trading_session"
    NOT_IN_UNIVERSE = "not_in_universe"
    ZERO_OR_NEGATIVE_QUANTITY = "zero_or_negative_quantity"


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    order: OrderRequest
    reason: RejectionReason = RejectionReason.NONE
    approved_quantity: float | None = None
    detail: str = ""


@dataclass
class Position:
    symbol: str
    quantity: float = 0.0
    avg_entry_price: float = 0.0
    opened_at: datetime | None = None
    strategy_id: str = ""

    @property
    def market_value(self) -> float:
        return abs(self.quantity) * self.avg_entry_price


@dataclass
class AccountState:
    """Mutable state RiskGate consults on every order. Owned by the caller
    (backtester or live loop) and updated after fills / mark-to-market."""

    equity: float
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    trades_today: int = 0
    consecutive_losses_today: int = 0
    realized_pnl_today: float = 0.0
    equity_at_session_start: float = 0.0
    halted: bool = False
    halt_reason: str = ""

    def total_exposure(self) -> float:
        return sum(p.market_value for p in self.positions.values())
