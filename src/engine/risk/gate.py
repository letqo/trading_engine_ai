"""RiskGate: the single choke point every order must pass through.

SPEC.md hard constraint #2: every order passes through RiskGate before
submission; there is no code path to the broker that bypasses it. Strategies
emit Signals; the execution layer turns approved signals into broker calls
only after RiskGate.evaluate() returns approved=True.
"""

from __future__ import annotations

from engine.config.settings import RiskLimits
from engine.risk.models import (
    AccountState,
    OrderRequest,
    RejectionReason,
    RiskDecision,
    Side,
)


class RiskGate:
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    # -- the single entrypoint every order must go through -----------------
    def evaluate(
        self,
        order: OrderRequest,
        account: AccountState,
        universe: set[str],
    ) -> RiskDecision:
        if order.quantity <= 0:
            return self._reject(order, RejectionReason.ZERO_OR_NEGATIVE_QUANTITY)

        if order.symbol not in universe:
            return self._reject(
                order, RejectionReason.NOT_IN_UNIVERSE, f"{order.symbol} not in trading universe"
            )

        if account.halted:
            return self._reject(order, RejectionReason.HALTED, account.halt_reason)

        if self._check_and_apply_daily_drawdown_halt(account):
            return self._reject(order, RejectionReason.DAILY_DRAWDOWN_BREACHED, account.halt_reason)

        if account.consecutive_losses_today >= self.limits.max_consecutive_losses_per_day:
            account.halted = True
            account.halt_reason = (
                f"{account.consecutive_losses_today} consecutive losing trades today "
                f">= limit {self.limits.max_consecutive_losses_per_day}"
            )
            return self._reject(order, RejectionReason.CONSECUTIVE_LOSSES_BREACHED, account.halt_reason)

        existing = account.positions.get(order.symbol)
        if existing is not None and self._is_closing(existing, order.side):
            approved_qty = min(order.quantity, abs(existing.quantity))
            return RiskDecision(approved=True, order=order, approved_quantity=approved_qty)

        return self._evaluate_opening_order(order, account, existing)

    def _evaluate_opening_order(
        self, order: OrderRequest, account: AccountState, existing
    ) -> RiskDecision:
        max_position_value = account.equity * self.limits.max_capital_per_position_pct
        existing_value = existing.market_value if existing else 0.0
        room_for_position = max_position_value - existing_value
        if room_for_position <= 0:
            return self._reject(
                order,
                RejectionReason.POSITION_SIZE_EXCEEDS_CAP,
                f"position already at/above cap ({existing_value:.2f} >= {max_position_value:.2f})",
            )

        max_total_exposure_value = account.equity * self.limits.max_total_exposure_pct
        room_for_exposure = max_total_exposure_value - account.total_exposure()
        if room_for_exposure <= 0:
            return self._reject(
                order,
                RejectionReason.TOTAL_EXPOSURE_EXCEEDS_CAP,
                f"total exposure at/above cap ({account.total_exposure():.2f} >= "
                f"{max_total_exposure_value:.2f})",
            )

        max_qty_by_position_cap = room_for_position / order.price
        max_qty_by_exposure_cap = room_for_exposure / order.price
        approved_qty = min(order.quantity, max_qty_by_position_cap, max_qty_by_exposure_cap)

        if approved_qty <= 0:
            return self._reject(order, RejectionReason.POSITION_SIZE_EXCEEDS_CAP)

        detail = ""
        if approved_qty < order.quantity:
            detail = f"resized {order.quantity} -> {approved_qty:.6f} to respect risk caps"

        return RiskDecision(approved=True, order=order, approved_quantity=approved_qty, detail=detail)

    # -- stop loss -----------------------------------------------------------
    def stop_loss_price(self, entry_price: float, side: Side) -> float:
        """Price at which the 2%-adverse-move stop triggers."""
        if side == Side.BUY:
            return entry_price * (1 - self.limits.stop_loss_pct)
        return entry_price * (1 + self.limits.stop_loss_pct)

    def is_stop_triggered(self, position, current_price: float) -> bool:
        if position.quantity == 0:
            return False
        stop_side = Side.BUY if position.quantity > 0 else Side.SELL
        stop_price = self.stop_loss_price(position.avg_entry_price, stop_side)
        if position.quantity > 0:
            return current_price <= stop_price
        return current_price >= stop_price

    # -- daily drawdown / session lifecycle -----------------------------------
    def _check_and_apply_daily_drawdown_halt(self, account: AccountState) -> bool:
        if account.equity_at_session_start <= 0:
            return False
        drawdown = (account.equity_at_session_start - account.equity) / account.equity_at_session_start
        if drawdown >= self.limits.max_daily_drawdown_pct:
            account.halted = True
            account.halt_reason = (
                f"daily drawdown {drawdown:.2%} >= limit {self.limits.max_daily_drawdown_pct:.2%}"
            )
            return True
        return False

    def check_daily_drawdown(self, account: AccountState) -> bool:
        """Call every loop iteration / bar, independent of order submission,
        so a drawdown breach halts trading even with no pending order."""
        return self._check_and_apply_daily_drawdown_halt(account)

    def start_new_session(self, account: AccountState) -> None:
        account.equity_at_session_start = account.equity
        account.trades_today = 0
        account.consecutive_losses_today = 0
        account.realized_pnl_today = 0.0
        account.halted = False
        account.halt_reason = ""

    def record_trade_result(self, account: AccountState, realized_pnl: float) -> None:
        account.trades_today += 1
        account.realized_pnl_today += realized_pnl
        if realized_pnl < 0:
            account.consecutive_losses_today += 1
        else:
            account.consecutive_losses_today = 0
        if account.consecutive_losses_today >= self.limits.max_consecutive_losses_per_day:
            account.halted = True
            account.halt_reason = (
                f"{account.consecutive_losses_today} consecutive losing trades today "
                f">= limit {self.limits.max_consecutive_losses_per_day}"
            )

    # -- flatten / kill switch -------------------------------------------------
    def flatten_orders(self, account: AccountState, timestamp, price_lookup) -> list[OrderRequest]:
        """Build closing orders for every open position. Used by the daily
        drawdown halt, the no-overnight-positions rule, and the kill switch.
        These bypass evaluate()'s caps by construction -- they only ever
        reduce risk -- but they still flow through the same order path to
        the broker so they're logged and journaled identically."""
        orders: list[OrderRequest] = []
        for symbol, position in account.positions.items():
            if position.quantity == 0:
                continue
            side = Side.SELL if position.quantity > 0 else Side.BUY
            orders.append(
                OrderRequest(
                    symbol=symbol,
                    side=side,
                    quantity=abs(position.quantity),
                    price=price_lookup(symbol),
                    timestamp=timestamp,
                    strategy_id="risk_gate_flatten",
                )
            )
        return orders

    def trigger_kill_switch(self, account: AccountState, reason: str = "kill switch engaged") -> None:
        account.halted = True
        account.halt_reason = reason

    @staticmethod
    def _is_closing(position, order_side: Side) -> bool:
        if position.quantity > 0 and order_side == Side.SELL:
            return True
        if position.quantity < 0 and order_side == Side.BUY:
            return True
        return False

    @staticmethod
    def _reject(order: OrderRequest, reason: RejectionReason, detail: str = "") -> RiskDecision:
        return RiskDecision(approved=False, order=order, reason=reason, detail=detail)
