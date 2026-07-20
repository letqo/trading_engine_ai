"""Hand-computed scenarios for the pure price-action strategies, same
approach as tests/test_backtest_engine.py -- deterministic bar sequences
with a known momentum/z-score/multi-factor outcome computed by hand."""

from datetime import datetime, timedelta, timezone

from engine.domain import MarketContext, SignalAction
from engine.strategy.technical import MeanReversionStrategy, MomentumStrategy, MultiFactorStrategy

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


def make_ctx(timestamp, closes: list[float], symbol="TEST") -> MarketContext:
    from engine.domain import Bar

    history = [
        Bar(symbol=symbol, timestamp=T0 + timedelta(hours=i), open=c, high=c, low=c, close=c, volume=1000, timeframe="1h")
        for i, c in enumerate(closes)
    ]
    return MarketContext(
        timestamp=timestamp, latest_bars={symbol: history[-1]}, bar_history={symbol: history},
        tradable_symbols=frozenset({symbol}),
    )


def test_momentum_goes_long_on_strong_upward_move():
    # 21 closes, flat at 100 for 20 bars then a final bar at 110: (110-100)/100*100% = 10% >= 2% threshold.
    closes = [100.0] * 20 + [110.0]
    strategy = MomentumStrategy(symbols=["TEST"], lookback_bars=20, entry_threshold_pct=2.0)
    ctx = make_ctx(T0 + timedelta(hours=20), closes)
    signals = strategy.on_bar(ctx)
    assert len(signals) == 1
    assert signals[0].action == SignalAction.BUY
    assert "TEST" in strategy._bars_held


def test_momentum_goes_short_on_strong_downward_move():
    closes = [100.0] * 20 + [90.0]  # -10%
    strategy = MomentumStrategy(symbols=["TEST"], lookback_bars=20, entry_threshold_pct=2.0)
    ctx = make_ctx(T0 + timedelta(hours=20), closes)
    signals = strategy.on_bar(ctx)
    assert len(signals) == 1
    assert signals[0].action == SignalAction.SELL


def test_momentum_stays_flat_below_threshold():
    closes = [100.0] * 20 + [100.5]  # 0.5%, below the 2% threshold
    strategy = MomentumStrategy(symbols=["TEST"], lookback_bars=20, entry_threshold_pct=2.0)
    ctx = make_ctx(T0 + timedelta(hours=20), closes)
    assert strategy.on_bar(ctx) == []


def test_momentum_exits_after_configured_bar_count():
    strategy = MomentumStrategy(symbols=["TEST"], lookback_bars=20, entry_threshold_pct=2.0, exit_after_bars=2)
    closes = [100.0] * 20 + [110.0]
    entry_ctx = make_ctx(T0 + timedelta(hours=20), closes)
    strategy.on_bar(entry_ctx)  # opens the long
    assert "TEST" in strategy._bars_held

    later_closes = closes + [111.0]
    ctx2 = make_ctx(T0 + timedelta(hours=21), later_closes)
    signals2 = strategy.on_bar(ctx2)  # 1 bar held -- not yet at exit_after_bars=2
    assert signals2 == []

    ctx3 = make_ctx(T0 + timedelta(hours=22), later_closes + [112.0])
    signals3 = strategy.on_bar(ctx3)  # 2 bars held -- exits (and may re-enter
    # immediately in the same call, since momentum is still well above
    # threshold -- that's correct trend-following behavior, not a bug).
    actions = [s.action for s in signals3]
    assert SignalAction.CLOSE in actions


def test_mean_reversion_goes_long_when_oversold():
    # 19 bars flat at 100, then a sharp drop to 80 -> negative z-score.
    closes = [100.0] * 19 + [80.0]
    strategy = MeanReversionStrategy(symbols=["TEST"], lookback_bars=20, entry_zscore=1.5)
    ctx = make_ctx(T0 + timedelta(hours=19), closes)
    signals = strategy.on_bar(ctx)
    assert len(signals) == 1
    assert signals[0].action == SignalAction.BUY


def test_mean_reversion_goes_short_when_overbought():
    closes = [100.0] * 19 + [120.0]
    strategy = MeanReversionStrategy(symbols=["TEST"], lookback_bars=20, entry_zscore=1.5)
    ctx = make_ctx(T0 + timedelta(hours=19), closes)
    signals = strategy.on_bar(ctx)
    assert len(signals) == 1
    assert signals[0].action == SignalAction.SELL


def test_mean_reversion_exits_when_zscore_decays():
    closes = [100.0] * 19 + [120.0]
    strategy = MeanReversionStrategy(symbols=["TEST"], lookback_bars=20, entry_zscore=1.5, exit_zscore=0.3, max_hold_bars=50)
    entry_ctx = make_ctx(T0 + timedelta(hours=19), closes)
    strategy.on_bar(entry_ctx)
    assert "TEST" in strategy._bars_held

    # Price reverts back toward the 100 mean -> z-score decays toward 0.
    reverted_closes = closes + [100.0]
    ctx2 = make_ctx(T0 + timedelta(hours=20), reverted_closes)
    signals = strategy.on_bar(ctx2)
    assert len(signals) == 1
    assert signals[0].action == SignalAction.CLOSE
    assert signals[0].reason == "mean_reversion_exit"


def test_multi_factor_requires_agreement_and_low_volatility():
    # Steady, low-volatility uptrend: both 20-bar and 5-bar momentum positive, vol low.
    closes = [100.0 + i * 0.5 for i in range(21)]  # 100, 100.5, ..., 110
    strategy = MultiFactorStrategy(symbols=["TEST"], long_lookback_bars=20, short_lookback_bars=5, max_volatility_pct=5.0)
    ctx = make_ctx(T0 + timedelta(hours=20), closes)
    signals = strategy.on_bar(ctx)
    assert len(signals) == 1
    assert signals[0].action == SignalAction.BUY


def test_multi_factor_sits_out_when_factors_disagree():
    # Long-term up, but the most recent 5 bars just reversed down -- disagreement.
    closes = [100.0 + i for i in range(16)] + [114.0, 112.0, 110.0, 108.0, 106.0]
    strategy = MultiFactorStrategy(symbols=["TEST"], long_lookback_bars=20, short_lookback_bars=5, max_volatility_pct=50.0)
    ctx = make_ctx(T0 + timedelta(hours=20), closes)
    assert strategy.on_bar(ctx) == []


def test_multi_factor_sits_out_when_volatility_too_high():
    # Strong agreement in direction, but very choppy/volatile day-to-day moves.
    closes = [100.0]
    for i in range(20):
        closes.append(closes[-1] * (1.15 if i % 2 == 0 else 1.05))  # noisy but net upward
    strategy = MultiFactorStrategy(symbols=["TEST"], long_lookback_bars=20, short_lookback_bars=5, max_volatility_pct=1.0)
    ctx = make_ctx(T0 + timedelta(hours=20), closes)
    assert strategy.on_bar(ctx) == []
