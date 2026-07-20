"""Cost model. SPEC.md: "start pessimistic: fill at next bar's open ± 1 tick
against you." Slippage always works against the trader; commission is a
simple per-share cost."""

from __future__ import annotations

from dataclasses import dataclass

from engine.risk.models import Side


@dataclass(frozen=True)
class CostModel:
    commission_per_share: float = 0.005
    min_commission: float = 1.0
    tick_size: float = 0.01

    def commission(self, quantity: float) -> float:
        return max(self.min_commission, quantity * self.commission_per_share)

    def adverse_fill_price(self, bar_open: float, side: Side) -> float:
        """Next bar's open, moved one tick against the trader."""
        if side == Side.BUY:
            return bar_open + self.tick_size
        return bar_open - self.tick_size
