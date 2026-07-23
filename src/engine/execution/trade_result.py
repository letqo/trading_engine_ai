"""Shared result types for every code path that attempts a real (paper)
order -- the automatic loops' per-item bodies and the dashboard's manual
trade routes alike (engine.prediction.trading, engine.anticipatory.trading,
engine.execution.manual_trading). A single shared shape means the dashboard
can render "why did this fail" the same way regardless of which of those
three modules produced it."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TradeAttemptResult:
    ok: bool
    reason: str = ""


@dataclass
class CloseResult:
    ok: bool
    reason: str = ""
    attribution: str = "unattributed"  # "prediction" | "hypothesis" | "manual_trade" | "unattributed"
    broker_order_id: str | None = None
