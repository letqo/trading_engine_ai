"""SPEC.md Phase 4 control group: "Positive-sentiment headline -> long, exit
after N hours or on stop." Expected to lose after costs -- its job is to be
beaten, and to document what it teaches (see JOURNAL.md).

Sentiment scoring must already have been applied upstream (item.sentiment_score
set) -- this strategy only decides what to do with a score, it doesn't compute
one, so the same object works whether sentiment came from VADER live or a
precomputed batch during backtesting.
"""

from __future__ import annotations

from datetime import datetime

from engine.domain import MarketContext, NewsItem, Signal, SignalAction


class DumbNewsStrategy:
    strategy_id = "dumb_news_sentiment"

    def __init__(self, sentiment_threshold: float = 0.5, exit_after_hours: float = 4.0):
        self.sentiment_threshold = sentiment_threshold
        self.exit_after_hours = exit_after_hours
        self._entries: dict[str, datetime] = {}

    def on_bar(self, ctx: MarketContext) -> list[Signal]:
        signals = []
        for symbol, entry_time in list(self._entries.items()):
            if symbol not in ctx.latest_bars:
                continue
            elapsed_hours = (ctx.timestamp - entry_time).total_seconds() / 3600.0
            if elapsed_hours >= self.exit_after_hours:
                del self._entries[symbol]
                signals.append(
                    Signal(
                        symbol=symbol, action=SignalAction.CLOSE, strategy_id=self.strategy_id,
                        timestamp=ctx.timestamp, reason=f"exit_after_{self.exit_after_hours}h",
                    )
                )
        return signals

    def on_news(self, ctx: MarketContext, item: NewsItem) -> list[Signal]:
        if item.sentiment_score is None or item.sentiment_score < self.sentiment_threshold:
            return []
        signals = []
        for symbol in item.routed_symbols:
            if symbol not in ctx.tradable_symbols:
                continue
            if symbol in self._entries:
                continue  # already in a position from an earlier headline
            self._entries[symbol] = ctx.timestamp
            signals.append(
                Signal(
                    symbol=symbol, action=SignalAction.BUY, strategy_id=self.strategy_id,
                    timestamp=ctx.timestamp, confidence=item.sentiment_score,
                    reason=f"positive_sentiment:{item.headline[:80]}",
                    exit_after_hours=self.exit_after_hours,
                )
            )
        return signals
