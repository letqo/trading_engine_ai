"""Pure price-action strategies -- no news, no sentiment. They complement
the news-driven family (dumb_news, overnight_gap) with a different signal
source entirely, and they're the first strategies in this repo to actually
trade both directions, exercising the short-selling support added to
engine.backtest.engine on 2026-07-20. All indicators are computed from
`MarketContext.bar_history`, which both the backtester and the live loop
populate identically -- no separate indicator-computation path to keep in
sync with backtest vs. live.

Bias review (SPEC.md anti-self-deception protocol) for all three: see
docs/bias_review.md. Short version: every indicator here only ever looks at
bars up to and including the current one (`bar_history` is append-only as
the backtester walks forward), so there is no look-ahead; the earliest a
signal computed *from* a bar can act is that bar's own on_bar call, which
queues the order for the *next* bar's open, same as every other strategy.
"""

from __future__ import annotations

from engine.domain import MarketContext, NewsItem, Signal, SignalAction


def _pct_change(closes: list[float], lookback: int) -> float | None:
    if len(closes) < lookback + 1:
        return None
    past = closes[-(lookback + 1)]
    if past <= 0:
        return None
    return (closes[-1] - past) / past * 100.0


class MomentumStrategy:
    """Trend-following: if price moved more than `entry_threshold_pct` over
    the last `lookback_bars`, bet the move continues -- long on strength,
    short on weakness. Exits unconditionally after `exit_after_bars`; it
    doesn't try to detect a reversal, only caps how long it rides one."""

    strategy_id = "momentum"

    def __init__(
        self, symbols: list[str], lookback_bars: int = 20,
        entry_threshold_pct: float = 2.0, exit_after_bars: int = 10,
    ):
        self.symbols = symbols
        self.lookback_bars = lookback_bars
        self.entry_threshold_pct = entry_threshold_pct
        self.exit_after_bars = exit_after_bars
        self._bars_held: dict[str, int] = {}

    def on_bar(self, ctx: MarketContext) -> list[Signal]:
        signals = list(self._exits(ctx))
        signals.extend(self._entries(ctx))
        return signals

    def _exits(self, ctx: MarketContext) -> list[Signal]:
        signals = []
        for symbol in list(self._bars_held):
            if symbol not in ctx.latest_bars:
                continue
            self._bars_held[symbol] += 1
            if self._bars_held[symbol] >= self.exit_after_bars:
                del self._bars_held[symbol]
                signals.append(
                    Signal(symbol=symbol, action=SignalAction.CLOSE, strategy_id=self.strategy_id,
                           timestamp=ctx.timestamp, reason="momentum_exit_horizon")
                )
        return signals

    def _entries(self, ctx: MarketContext) -> list[Signal]:
        signals = []
        for symbol in self.symbols:
            if symbol in self._bars_held or symbol not in ctx.tradable_symbols or symbol not in ctx.latest_bars:
                continue
            closes = [b.close for b in ctx.bar_history.get(symbol, [])]
            momentum_pct = _pct_change(closes, self.lookback_bars)
            if momentum_pct is None or abs(momentum_pct) < self.entry_threshold_pct:
                continue
            action = SignalAction.BUY if momentum_pct > 0 else SignalAction.SELL
            self._bars_held[symbol] = 0
            signals.append(
                Signal(symbol=symbol, action=action, strategy_id=self.strategy_id, timestamp=ctx.timestamp,
                       confidence=min(1.0, abs(momentum_pct) / 10.0), reason=f"momentum:{momentum_pct:+.2f}%")
            )
        return signals

    def on_news(self, ctx: MarketContext, item: NewsItem) -> list[Signal]:
        return []


class MeanReversionStrategy:
    """Contrarian: bets a price extended too far from its own recent
    average snaps back. Entry on |z-score| >= entry_zscore (long when
    oversold, short when overbought); exits when the z-score decays back
    toward zero or after max_hold_bars, whichever comes first."""

    strategy_id = "mean_reversion"

    def __init__(
        self, symbols: list[str], lookback_bars: int = 20,
        entry_zscore: float = 1.5, exit_zscore: float = 0.3, max_hold_bars: int = 15,
    ):
        self.symbols = symbols
        self.lookback_bars = lookback_bars
        self.entry_zscore = entry_zscore
        self.exit_zscore = exit_zscore
        self.max_hold_bars = max_hold_bars
        self._bars_held: dict[str, int] = {}

    def _zscore(self, closes: list[float]) -> float | None:
        if len(closes) < self.lookback_bars:
            return None
        window = closes[-self.lookback_bars:]
        mean = sum(window) / len(window)
        variance = sum((c - mean) ** 2 for c in window) / len(window)
        std = variance**0.5
        if std == 0:
            return None
        return (window[-1] - mean) / std

    def on_bar(self, ctx: MarketContext) -> list[Signal]:
        signals = list(self._exits(ctx))
        signals.extend(self._entries(ctx))
        return signals

    def _exits(self, ctx: MarketContext) -> list[Signal]:
        signals = []
        for symbol in list(self._bars_held):
            if symbol not in ctx.latest_bars:
                continue
            self._bars_held[symbol] += 1
            closes = [b.close for b in ctx.bar_history.get(symbol, [])]
            z = self._zscore(closes)
            decayed = z is not None and abs(z) <= self.exit_zscore
            if decayed or self._bars_held[symbol] >= self.max_hold_bars:
                del self._bars_held[symbol]
                signals.append(
                    Signal(symbol=symbol, action=SignalAction.CLOSE, strategy_id=self.strategy_id,
                           timestamp=ctx.timestamp, reason="mean_reversion_exit")
                )
        return signals

    def _entries(self, ctx: MarketContext) -> list[Signal]:
        signals = []
        for symbol in self.symbols:
            if symbol in self._bars_held or symbol not in ctx.tradable_symbols or symbol not in ctx.latest_bars:
                continue
            closes = [b.close for b in ctx.bar_history.get(symbol, [])]
            z = self._zscore(closes)
            if z is None or abs(z) < self.entry_zscore:
                continue
            # oversold (z very negative) -> expect reversion up -> long;
            # overbought (z very positive) -> expect reversion down -> short.
            action = SignalAction.BUY if z < 0 else SignalAction.SELL
            self._bars_held[symbol] = 0
            signals.append(
                Signal(symbol=symbol, action=action, strategy_id=self.strategy_id, timestamp=ctx.timestamp,
                       confidence=min(1.0, abs(z) / 3.0), reason=f"mean_reversion:z={z:+.2f}")
            )
        return signals

    def on_news(self, ctx: MarketContext, item: NewsItem) -> list[Signal]:
        return []


class MultiFactorStrategy:
    """Trend-following gated by a regime filter: only trades when a
    short-term and a long-term momentum measure agree on direction *and*
    recent realized volatility is under `max_volatility_pct` -- sitting out
    choppy conditions where a pure single-factor momentum signal tends to
    whipsaw. Confidence scales with how strongly the two momentum measures
    agree, not with volatility."""

    strategy_id = "multi_factor"

    def __init__(
        self, symbols: list[str], long_lookback_bars: int = 20, short_lookback_bars: int = 5,
        max_volatility_pct: float = 5.0, exit_after_bars: int = 10,
    ):
        self.symbols = symbols
        self.long_lookback_bars = long_lookback_bars
        self.short_lookback_bars = short_lookback_bars
        self.max_volatility_pct = max_volatility_pct
        self.exit_after_bars = exit_after_bars
        self._bars_held: dict[str, int] = {}

    @staticmethod
    def _recent_volatility_pct(closes: list[float], lookback: int) -> float | None:
        if len(closes) < lookback + 1:
            return None
        window = closes[-(lookback + 1):]
        returns = [(window[i] - window[i - 1]) / window[i - 1] for i in range(1, len(window)) if window[i - 1] > 0]
        if not returns:
            return None
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        return (variance**0.5) * 100.0

    def on_bar(self, ctx: MarketContext) -> list[Signal]:
        signals = list(self._exits(ctx))
        signals.extend(self._entries(ctx))
        return signals

    def _exits(self, ctx: MarketContext) -> list[Signal]:
        signals = []
        for symbol in list(self._bars_held):
            if symbol not in ctx.latest_bars:
                continue
            self._bars_held[symbol] += 1
            if self._bars_held[symbol] >= self.exit_after_bars:
                del self._bars_held[symbol]
                signals.append(
                    Signal(symbol=symbol, action=SignalAction.CLOSE, strategy_id=self.strategy_id,
                           timestamp=ctx.timestamp, reason="multi_factor_exit_horizon")
                )
        return signals

    def _entries(self, ctx: MarketContext) -> list[Signal]:
        signals = []
        for symbol in self.symbols:
            if symbol in self._bars_held or symbol not in ctx.tradable_symbols or symbol not in ctx.latest_bars:
                continue
            closes = [b.close for b in ctx.bar_history.get(symbol, [])]
            long_mom = _pct_change(closes, self.long_lookback_bars)
            short_mom = _pct_change(closes, self.short_lookback_bars)
            vol = self._recent_volatility_pct(closes, self.long_lookback_bars)
            if long_mom is None or short_mom is None or vol is None or vol > self.max_volatility_pct:
                continue
            agree_up = long_mom > 0 and short_mom > 0
            agree_down = long_mom < 0 and short_mom < 0
            if not agree_up and not agree_down:
                continue
            action = SignalAction.BUY if agree_up else SignalAction.SELL
            confidence = min(1.0, (abs(long_mom) + abs(short_mom)) / 10.0)
            self._bars_held[symbol] = 0
            signals.append(
                Signal(symbol=symbol, action=action, strategy_id=self.strategy_id, timestamp=ctx.timestamp,
                       confidence=confidence,
                       reason=f"multi_factor:long={long_mom:+.2f}%,short={short_mom:+.2f}%,vol={vol:.2f}%")
            )
        return signals

    def on_news(self, ctx: MarketContext, item: NewsItem) -> list[Signal]:
        return []
