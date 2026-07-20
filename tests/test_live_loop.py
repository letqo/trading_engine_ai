from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from engine.config.settings import RiskLimits
from engine.data.universe import Instrument, Universe
from engine.domain import Signal, SignalAction
from engine.execution.broker import BrokerOrder
from engine.execution.live_loop import (
    LiveLoopState,
    _check_and_submit_stop,
    _submit_signals,
    fetch_new_bars,
    fetch_new_news,
    run_live_cycle,
    seed_bar_history,
)
from engine.risk.gate import RiskGate
from engine.risk.models import AccountState, Position, Side

T0 = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)


def make_universe():
    return Universe(
        instruments=(Instrument(symbol="TEST", tier=1, asset_class="equity", news_topics=("boj",)),),
        source_text="x",
    )


def _bars_df(symbol, rows, interval="1h"):
    return pd.DataFrame(
        [
            {"symbol": symbol, "timestamp": ts, "open": o, "high": h, "low": lo, "close": c,
             "volume": 1000, "timeframe": interval}
            for ts, o, h, lo, c in rows
        ]
    )


class FakeBroker:
    def __init__(self):
        self.submitted = []
        self._next_id = 0

    def get_account_equity(self):
        raise NotImplementedError

    def get_positions(self):
        raise NotImplementedError

    def get_open_orders(self):
        return []

    def submit_order(self, order):
        self._next_id += 1
        self.submitted.append(order)
        return BrokerOrder(
            broker_order_id=f"order-{self._next_id}", symbol=order.symbol, side=order.side,
            quantity=order.quantity, status="filled", filled_avg_price=order.price, submitted_at=order.timestamp,
        )

    def cancel_all_orders(self):
        pass

    def close_all_positions(self):
        pass


class ScriptedStrategy:
    strategy_id = "scripted"

    def __init__(self):
        self.bar_calls = []
        self.news_calls = []
        self.bar_signal = None
        self.news_signal = None

    def on_bar(self, ctx):
        self.bar_calls.append(ctx.timestamp)
        return [self.bar_signal] if self.bar_signal else []

    def on_news(self, ctx, item):
        self.news_calls.append((ctx.timestamp, item.headline))
        return [self.news_signal] if self.news_signal else []


def test_seed_bar_history_populates_without_dispatching_anything():
    universe = make_universe()
    state = LiveLoopState()
    bars = _bars_df("TEST", [
        (T0, 100, 101, 99, 100),
        (T0 + timedelta(hours=1), 100, 102, 99, 101),
    ])
    with patch("engine.execution.live_loop.fetch_bars", return_value=bars):
        seed_bar_history(universe, "1h", state, lookback_periods=5)

    assert len(state.bar_history["TEST"]) == 2
    assert state.latest_bars["TEST"].close == 101


def test_fetch_new_bars_excludes_already_seen():
    universe = make_universe()
    state = LiveLoopState()
    old_bar_df = _bars_df("TEST", [(T0, 100, 101, 99, 100)])
    with patch("engine.execution.live_loop.fetch_bars", return_value=old_bar_df):
        seed_bar_history(universe, "1h", state)

    mixed_df = _bars_df("TEST", [
        (T0, 100, 101, 99, 100),  # already seen -- must be excluded
        (T0 + timedelta(hours=1), 100, 102, 99, 101),  # new
    ])
    with patch("engine.execution.live_loop.fetch_bars", return_value=mixed_df):
        new_bars = fetch_new_bars(universe, "1h", state)

    assert len(new_bars) == 1
    assert new_bars[0].timestamp == T0 + timedelta(hours=1)


def test_fetch_new_news_filters_by_last_decision_timestamp():
    from engine.domain import NewsItem

    universe = make_universe()
    state = LiveLoopState()
    state.last_news_decision_timestamp = T0

    old_item = NewsItem(id="1", source="rss", published_at=T0 - timedelta(hours=1), ingested_at=T0 - timedelta(hours=1),
                         headline="old news", url=None, raw_payload={})
    new_item = NewsItem(id="2", source="rss", published_at=T0 + timedelta(hours=1), ingested_at=T0 + timedelta(hours=1),
                         headline="fresh news", url=None, raw_payload={})
    with patch("engine.execution.live_loop.fetch_all_rss", return_value=[old_item, new_item]):
        items = fetch_new_news(universe, state)

    assert len(items) == 1
    assert items[0].headline == "fresh news"


def test_check_and_submit_stop_triggers_on_adverse_price():
    from engine.domain import Bar

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(stop_loss_pct=0.02))
    account = AccountState(equity=10_000.0, cash=5_000.0, equity_at_session_start=10_000.0)
    account.positions["TEST"] = Position(symbol="TEST", quantity=50.0, avg_entry_price=100.0, strategy_id="scripted")

    bar = Bar(symbol="TEST", timestamp=T0, open=99, high=99.5, low=97.0, close=98.0, volume=1000, timeframe="1h")
    triggered = _check_and_submit_stop(bar, risk_gate, broker, account)

    assert triggered is True
    assert len(broker.submitted) == 1
    assert broker.submitted[0].side == Side.SELL
    assert "TEST" not in account.positions
    assert account.trades_today == 1


def test_check_and_submit_stop_noop_when_not_triggered():
    from engine.domain import Bar

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(stop_loss_pct=0.02))
    account = AccountState(equity=10_000.0, cash=5_000.0, equity_at_session_start=10_000.0)
    account.positions["TEST"] = Position(symbol="TEST", quantity=50.0, avg_entry_price=100.0, strategy_id="scripted")

    bar = Bar(symbol="TEST", timestamp=T0, open=100, high=100.5, low=99.5, close=100.2, volume=1000, timeframe="1h")
    triggered = _check_and_submit_stop(bar, risk_gate, broker, account)

    assert triggered is False
    assert broker.submitted == []
    assert "TEST" in account.positions


def test_submit_signals_opens_long_sized_to_cap():
    state = LiveLoopState()
    from engine.domain import Bar
    state.latest_bars["TEST"] = Bar(symbol="TEST", timestamp=T0, open=100, high=100, low=100, close=100, volume=1000, timeframe="1h")

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    signal = Signal(symbol="TEST", action=SignalAction.BUY, strategy_id="scripted", timestamp=T0)
    result = _submit_signals([signal], risk_gate, broker, account, state, {"TEST"})

    assert result == {"orders": 1, "rejected": 0}
    assert broker.submitted[0].side == Side.BUY
    assert account.positions["TEST"].quantity == pytest.approx(50.0)  # 5,000 cap / 100 price


def test_submit_signals_opens_short_for_sell_signal():
    state = LiveLoopState()
    from engine.domain import Bar
    state.latest_bars["TEST"] = Bar(symbol="TEST", timestamp=T0, open=100, high=100, low=100, close=100, volume=1000, timeframe="1h")

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    signal = Signal(symbol="TEST", action=SignalAction.SELL, strategy_id="scripted", timestamp=T0)
    result = _submit_signals([signal], risk_gate, broker, account, state, {"TEST"})

    assert result == {"orders": 1, "rejected": 0}
    assert broker.submitted[0].side == Side.SELL
    assert account.positions["TEST"].quantity == pytest.approx(-50.0)


def test_submit_signals_closes_existing_long_on_close_signal():
    state = LiveLoopState()
    from engine.domain import Bar
    state.latest_bars["TEST"] = Bar(symbol="TEST", timestamp=T0, open=110, high=110, low=110, close=110, volume=1000, timeframe="1h")

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=5_000.0, equity_at_session_start=10_000.0)
    account.positions["TEST"] = Position(symbol="TEST", quantity=50.0, avg_entry_price=100.0, strategy_id="scripted")

    signal = Signal(symbol="TEST", action=SignalAction.CLOSE, strategy_id="scripted", timestamp=T0)
    result = _submit_signals([signal], risk_gate, broker, account, state, {"TEST"})

    assert result == {"orders": 1, "rejected": 0}
    assert broker.submitted[0].side == Side.SELL
    assert broker.submitted[0].quantity == pytest.approx(50.0)
    assert "TEST" not in account.positions
    assert account.trades_today == 1


def test_submit_signals_respects_risk_gate_rejection():
    state = LiveLoopState()
    from engine.domain import Bar
    state.latest_bars["TEST"] = Bar(symbol="TEST", timestamp=T0, open=100, high=100, low=100, close=100, volume=1000, timeframe="1h")

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)
    account.positions["TEST"] = Position(symbol="TEST", quantity=50.0, avg_entry_price=100.0)  # already at cap

    signal = Signal(symbol="TEST", action=SignalAction.BUY, strategy_id="scripted", timestamp=T0)
    result = _submit_signals([signal], risk_gate, broker, account, state, {"TEST"})

    assert result == {"orders": 0, "rejected": 1}
    assert broker.submitted == []


def test_run_live_cycle_dispatches_new_bars_then_news_and_submits_orders():
    universe = make_universe()
    state = LiveLoopState()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0))
    broker = FakeBroker()
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    strategy = ScriptedStrategy()
    strategy.bar_signal = Signal(symbol="TEST", action=SignalAction.BUY, strategy_id="scripted", timestamp=T0)

    bars = _bars_df("TEST", [(T0, 100, 100, 100, 100)])
    with patch("engine.execution.live_loop.fetch_bars", return_value=bars), \
         patch("engine.execution.live_loop.fetch_all_rss", return_value=[]):
        summary = run_live_cycle(strategy, universe, risk_gate, broker, account, state, "1h")

    assert summary["bars"] == 1
    assert summary["orders"] == 1
    assert len(strategy.bar_calls) == 1
    assert account.positions["TEST"].quantity == pytest.approx(50.0)
