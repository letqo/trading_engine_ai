"""SPEC.md strategy interface: the same object must run unmodified in both
the backtester and the live paper-trading loop."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from engine.domain import MarketContext, NewsItem, Signal


@runtime_checkable
class Strategy(Protocol):
    strategy_id: str

    def on_bar(self, ctx: MarketContext) -> list[Signal]: ...

    def on_news(self, ctx: MarketContext, item: NewsItem) -> list[Signal]: ...
