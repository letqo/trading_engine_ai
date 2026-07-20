"""Shared domain types used across data/strategy/backtest/execution.

Living in one leaf module (no internal imports) avoids circular imports
between the layers that all need to talk about "a bar", "a news item", and
"a signal" using the same shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime  # UTC, bar CLOSE time -- the earliest instant this bar's data may be used
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str  # "1d", "1h", "5m", ...


@dataclass(frozen=True)
class NewsItem:
    id: str
    source: str
    published_at: datetime  # UTC, wall-clock time the source says it was published
    ingested_at: datetime  # UTC, when our pipeline actually saw it -- see note below
    headline: str
    url: str | None
    raw_payload: dict
    topics: frozenset[str] = field(default_factory=frozenset)
    routed_symbols: tuple[str, ...] = field(default_factory=tuple)
    sentiment_score: float | None = None

    @property
    def decision_timestamp(self) -> datetime:
        """The timestamp a backtest must use to decide 'is this visible yet'.

        No-look-ahead requires ingested_at, not published_at: a real trading
        system only learns about news when its pipeline fetches it, which
        lags true publication time (RSS poll interval, API latency). Using
        published_at here would silently leak a small amount of foresight
        into every backtest. See docs/bias_review.md.
        """
        return max(self.published_at, self.ingested_at)


class SignalAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    CLOSE = "close"


@dataclass(frozen=True)
class Signal:
    symbol: str
    action: SignalAction
    strategy_id: str
    timestamp: datetime
    confidence: float = 1.0  # 0..1, strategy's own confidence -- sizing still goes through RiskGate
    reason: str = ""
    exit_after_hours: float | None = None


@dataclass
class MarketContext:
    """What a Strategy sees at decision time. Backtester and live loop both
    construct this the same way, which is what lets the same Strategy object
    run unmodified in both (SPEC.md architecture requirement)."""

    timestamp: datetime
    latest_bars: dict[str, Bar]
    bar_history: dict[str, list[Bar]]
    tradable_symbols: frozenset[str]
