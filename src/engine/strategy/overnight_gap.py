"""SPEC.md Phase 5 first named candidate: Overnight-Gap.

Score Asia/Europe macro news published outside US market hours (BoJ, ECB,
PBoC, overnight geopolitical/commodity events), route it to the tagged
Tier 2 ETFs, and act at or shortly after the US open with a defined exit
horizon. Targets one well-defined decision moment (the open) rather than
millisecond reaction, which suits retail infrastructure.

Known v1 simplifications (revisit only via a journaled experiment, per the
anti-self-deception protocol -- see JOURNAL.md):
  - Long-only, like the rest of v1 (see engine.backtest.engine). Only
    positive-sentiment overnight macro news produces a signal; negative
    sentiment is currently dropped rather than expressed as a hedge/short.
  - "Overnight" and "the US open" are UTC hour-of-day windows that ignore
    DST transitions -- acceptable slop for a first pass, flagged here so
    nobody mistakes it for a deliberate design choice.
"""

from __future__ import annotations

from datetime import datetime

from engine.data.universe import Universe
from engine.domain import MarketContext, NewsItem, Signal, SignalAction

# UTC hour-of-day approximations (US/Eastern, ignoring DST): market open
# ~14:30 UTC (9:30am ET), close ~21:00 UTC (4pm ET).
US_MARKET_OPEN_UTC_HOUR = 14
US_MARKET_CLOSE_UTC_HOUR = 21


def is_outside_us_market_hours(dt: datetime) -> bool:
    return dt.hour < US_MARKET_OPEN_UTC_HOUR or dt.hour >= US_MARKET_CLOSE_UTC_HOUR


class OvernightGapStrategy:
    strategy_id = "overnight_gap"

    def __init__(
        self,
        universe: Universe,
        sentiment_threshold: float = 0.5,
        exit_after_hours: float = 3.0,
        max_signal_age_hours: float = 20.0,
    ):
        self.universe = universe
        self.sentiment_threshold = sentiment_threshold
        self.exit_after_hours = exit_after_hours
        self.max_signal_age_hours = max_signal_age_hours
        self._pending: dict[str, tuple[datetime, NewsItem]] = {}  # symbol -> (news_time, item)
        self._entries: dict[str, datetime] = {}

    def _tier2_symbols(self, symbols: tuple[str, ...]) -> list[str]:
        out = []
        for symbol in symbols:
            instrument = self.universe.get(symbol)
            if instrument is not None and instrument.tier == 2:
                out.append(symbol)
        return out

    def on_news(self, ctx: MarketContext, item: NewsItem) -> list[Signal]:
        if item.sentiment_score is None or item.sentiment_score < self.sentiment_threshold:
            return []
        if not is_outside_us_market_hours(item.decision_timestamp):
            return []
        symbols = self._tier2_symbols(item.routed_symbols)
        for symbol in symbols:
            self._pending[symbol] = (item.decision_timestamp, item)
        return []

    def on_bar(self, ctx: MarketContext) -> list[Signal]:
        signals = list(self._exit_signals(ctx))
        signals.extend(self._entry_signals(ctx))
        return signals

    def _exit_signals(self, ctx: MarketContext) -> list[Signal]:
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
                        timestamp=ctx.timestamp, reason="overnight_gap_exit_horizon",
                    )
                )
        return signals

    def _entry_signals(self, ctx: MarketContext) -> list[Signal]:
        if ctx.timestamp.hour < US_MARKET_OPEN_UTC_HOUR:
            return []
        signals = []
        for symbol, (news_time, item) in list(self._pending.items()):
            age_hours = (ctx.timestamp - news_time).total_seconds() / 3600.0
            if age_hours > self.max_signal_age_hours:
                del self._pending[symbol]
                continue
            if symbol not in ctx.tradable_symbols or symbol not in ctx.latest_bars:
                continue
            if symbol in self._entries:
                del self._pending[symbol]
                continue
            del self._pending[symbol]
            self._entries[symbol] = ctx.timestamp
            signals.append(
                Signal(
                    symbol=symbol, action=SignalAction.BUY, strategy_id=self.strategy_id,
                    timestamp=ctx.timestamp, confidence=item.sentiment_score,
                    reason=f"overnight_gap:{item.headline[:80]}",
                    exit_after_hours=self.exit_after_hours,
                )
            )
        return signals
