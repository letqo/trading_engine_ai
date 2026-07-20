"""SPEC.md anti-self-deception protocol: baselines first. Any "smart"
strategy must beat both of these, after costs, or it is dead."""

from __future__ import annotations

import random

from engine.domain import MarketContext, NewsItem, Signal, SignalAction


class BuyAndHoldStrategy:
    """Buys each symbol in the universe on the first bar it sees, then never
    trades again. This is a static reference benchmark -- it is meant to be
    run with the backtester's no-overnight rule disabled (daily bars, where
    that rule doesn't apply anyway; see engine.backtest.engine), since
    holding continuously is the entire point of buy-and-hold. It is never a
    candidate for live trading."""

    strategy_id = "buy_and_hold"

    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self._bought: set[str] = set()

    def on_bar(self, ctx: MarketContext) -> list[Signal]:
        signals = []
        for symbol in self.symbols:
            if symbol in self._bought:
                continue
            if symbol not in ctx.latest_bars:
                continue
            self._bought.add(symbol)
            signals.append(
                Signal(symbol=symbol, action=SignalAction.BUY, strategy_id=self.strategy_id, timestamp=ctx.timestamp)
            )
        return signals

    def on_news(self, ctx: MarketContext, item: NewsItem) -> list[Signal]:
        return []


class RandomEntryStrategy:
    """Random entries at a fixed trade frequency, same risk rules as
    everything else (RiskGate sizes/caps it identically). This isolates
    "is the signal better than noise" from "does the risk/cost model itself
    produce a plausible-looking equity curve."""

    strategy_id = "random_entry"

    def __init__(
        self,
        symbols: list[str],
        entry_probability_per_bar: float = 0.02,
        exit_after_bars: int = 8,
        seed: int = 1337,
    ):
        self.symbols = symbols
        self.entry_probability_per_bar = entry_probability_per_bar
        self.exit_after_bars = exit_after_bars
        self._rng = random.Random(seed)
        self._bars_since_entry: dict[str, int] = {}

    def on_bar(self, ctx: MarketContext) -> list[Signal]:
        signals = []
        for symbol in self.symbols:
            if symbol not in ctx.latest_bars:
                continue
            if symbol in self._bars_since_entry:
                self._bars_since_entry[symbol] += 1
                if self._bars_since_entry[symbol] >= self.exit_after_bars:
                    del self._bars_since_entry[symbol]
                    signals.append(
                        Signal(symbol=symbol, action=SignalAction.CLOSE, strategy_id=self.strategy_id, timestamp=ctx.timestamp)
                    )
                continue
            if self._rng.random() < self.entry_probability_per_bar:
                self._bars_since_entry[symbol] = 0
                signals.append(
                    Signal(symbol=symbol, action=SignalAction.BUY, strategy_id=self.strategy_id, timestamp=ctx.timestamp)
                )
        return signals

    def on_news(self, ctx: MarketContext, item: NewsItem) -> list[Signal]:
        return []
