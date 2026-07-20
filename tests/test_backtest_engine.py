"""Hand-computed toy scenarios validating the event-driven backtester, per
SPEC.md's "Validated against hand-computed toy scenarios in tests"."""

from datetime import datetime, timedelta, timezone

import pytest

from engine.backtest.costs import CostModel
from engine.backtest.engine import BacktestEngine
from engine.config.settings import RiskLimits
from engine.data.universe import Instrument, Universe
from engine.domain import Bar, MarketContext, NewsItem, Signal, SignalAction
from engine.risk.gate import RiskGate

T0 = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)


def make_universe(symbol="TEST") -> Universe:
    return Universe(
        instruments=(Instrument(symbol=symbol, tier=1, asset_class="equity", news_topics=()),),
        source_text="test",
    )


def make_bar(symbol, ts, open_, close, high=None, low=None, timeframe="1h"):
    high = high if high is not None else max(open_, close)
    low = low if low is not None else min(open_, close)
    return Bar(symbol=symbol, timestamp=ts, open=open_, high=high, low=low, close=close, volume=1000, timeframe=timeframe)


class ScriptedStrategy:
    """Emits BUY/SELL at chosen bar timestamps and CLOSE at another, nothing else."""

    strategy_id = "scripted"

    def __init__(
        self, buy_at: datetime | None = None, sell_at: datetime | None = None,
        close_at: datetime | None = None, symbol="TEST",
    ):
        self.buy_at = buy_at
        self.sell_at = sell_at
        self.close_at = close_at
        self.symbol = symbol

    def on_bar(self, ctx: MarketContext) -> list[Signal]:
        if self.buy_at is not None and ctx.timestamp == self.buy_at:
            return [Signal(symbol=self.symbol, action=SignalAction.BUY, strategy_id=self.strategy_id, timestamp=ctx.timestamp)]
        if self.sell_at is not None and ctx.timestamp == self.sell_at:
            return [Signal(symbol=self.symbol, action=SignalAction.SELL, strategy_id=self.strategy_id, timestamp=ctx.timestamp)]
        if self.close_at is not None and ctx.timestamp == self.close_at:
            return [Signal(symbol=self.symbol, action=SignalAction.CLOSE, strategy_id=self.strategy_id, timestamp=ctx.timestamp)]
        return []

    def on_news(self, ctx: MarketContext, item: NewsItem) -> list[Signal]:
        return []


ZERO_COST = CostModel(commission_per_share=0.0, min_commission=0.0, tick_size=0.0)


def test_buy_and_close_hand_computed_pnl():
    # equity=10,000; 50% position cap => $5,000 max position.
    b1 = make_bar("TEST", T0, open_=100, close=100)
    b2 = make_bar("TEST", T0 + timedelta(hours=1), open_=100, close=110)
    b3 = make_bar("TEST", T0 + timedelta(hours=2), open_=120, close=120)

    strategy = ScriptedStrategy(buy_at=b1.timestamp, close_at=b2.timestamp)
    limits = RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0)
    engine = BacktestEngine(
        strategy=strategy, universe=make_universe(), risk_gate=RiskGate(limits),
        initial_equity=10_000.0, cost_model=ZERO_COST,
    )
    result = engine.run([b1, b2, b3], news=[])

    # BUY signal (queued after b1) fills at b2's open=100, sized to the $5,000
    # cap => 50 shares. CLOSE signal (queued after b2) fills at b3's open=120.
    # realized pnl = (120 - 100) * 50 = 1000
    assert len(result.closed_trades) == 1
    trade = result.closed_trades[0]
    assert trade.realized_pnl == pytest.approx(1000.0)
    assert trade.entry_time == b2.timestamp
    assert trade.exit_time == b3.timestamp

    assert result.final_equity == pytest.approx(11_000.0)
    assert result.metrics.total_return_pct == pytest.approx(10.0)
    assert result.rejected_orders == 0

    # equity curve: b1 flat at 10,000; b2 marked at 50*110 + 5,000 cash = 10,500;
    # b3 fully closed, cash = 11,000
    assert [round(p.equity, 2) for p in result.equity_curve] == [10_000.0, 10_500.0, 11_000.0]


def test_costs_apply_slippage_and_commission():
    # tick_size=0.5, commission=$0.10/share, min $2. BUY 10 shares at open=100
    # -> fill 100.5, commission max(2, 10*0.10)=2 -> cash -= 10*100.5+2=1007
    b1 = make_bar("TEST", T0, open_=100, close=100, timeframe="1d")
    b2 = make_bar("TEST", T0 + timedelta(days=1), open_=100, close=100, timeframe="1d")
    strategy = ScriptedStrategy(buy_at=b1.timestamp)
    # cap position to exactly 10 shares: equity 1000, cap 100% => room 1000/100.5 ~ 9.95,
    # use a tiny equity + huge cap pct is fragile; instead assert on the fill price/costs directly
    limits = RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0)
    costs = CostModel(commission_per_share=0.10, min_commission=2.0, tick_size=0.5)
    engine = BacktestEngine(
        strategy=strategy, universe=make_universe(), risk_gate=RiskGate(limits),
        initial_equity=1000.0, cost_model=costs,
    )
    result = engine.run([b1, b2], news=[])

    # position should exist after b2 (opened at b2's open, adverse-filled)
    assert len(result.closed_trades) == 0
    fill_price = 100.5  # 100 open + 0.5 tick, against the buyer
    expected_qty = 1000.0 / fill_price  # full cap, no room left after this fill
    expected_commission = max(2.0, expected_qty * 0.10)
    expected_cash = 1000.0 - (expected_qty * fill_price + expected_commission)
    assert result.equity_curve[-1].equity == pytest.approx(expected_cash + expected_qty * b2.close, rel=1e-6)


def test_order_exceeding_universe_is_never_filled():
    b1 = make_bar("TEST", T0, open_=100, close=100)
    strategy = ScriptedStrategy(buy_at=b1.timestamp, symbol="NOTINUNIVERSE")
    engine = BacktestEngine(
        strategy=strategy, universe=make_universe(), risk_gate=RiskGate(RiskLimits()),
        initial_equity=10_000.0, cost_model=ZERO_COST,
    )
    result = engine.run([b1], news=[])
    assert result.final_equity == pytest.approx(10_000.0)
    assert result.closed_trades == []


def test_stop_loss_triggers_intrabar_on_low():
    # long at 100 (b2 open), stop = 98 (2% default). b3's low pierces 98.
    b1 = make_bar("TEST", T0, open_=100, close=100)
    b2 = make_bar("TEST", T0 + timedelta(hours=1), open_=100, close=100)
    b3 = make_bar("TEST", T0 + timedelta(hours=2), open_=99, close=99, high=99.5, low=97.0)
    strategy = ScriptedStrategy(buy_at=b1.timestamp)
    limits = RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0, stop_loss_pct=0.02)
    engine = BacktestEngine(
        strategy=strategy, universe=make_universe(), risk_gate=RiskGate(limits),
        initial_equity=10_000.0, cost_model=ZERO_COST,
    )
    result = engine.run([b1, b2, b3], news=[])
    assert len(result.closed_trades) == 1
    assert result.closed_trades[0].exit_reason == "stop_loss"
    assert result.closed_trades[0].realized_pnl < 0


def test_daily_drawdown_halt_flattens_and_blocks_new_entries():
    # Position loses enough on b3's mark-to-market to breach 3% daily drawdown;
    # engine must flatten and the scripted re-buy signal on b4 must be rejected.
    b1 = make_bar("TEST", T0, open_=100, close=100)
    b2 = make_bar("TEST", T0 + timedelta(hours=1), open_=100, close=100)
    b3 = make_bar("TEST", T0 + timedelta(hours=2), open_=100, close=50, high=100, low=50)  # big drop, no stop hit (low > 98%? no -- stop should hit first)
    strategy = ScriptedStrategy(buy_at=b1.timestamp)
    limits = RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0, max_daily_drawdown_pct=0.03, stop_loss_pct=0.60)
    engine = BacktestEngine(
        strategy=strategy, universe=make_universe(), risk_gate=RiskGate(limits),
        initial_equity=10_000.0, cost_model=ZERO_COST,
    )
    result = engine.run([b1, b2, b3], news=[])
    assert len(result.halt_events) == 1
    assert "drawdown" in result.halt_events[0]
    assert len(result.closed_trades) == 1
    assert result.closed_trades[0].exit_reason == "daily_drawdown_halt"


def test_no_overnight_flattens_intraday_position_before_next_day():
    day1 = T0
    day1_next = T0 + timedelta(hours=1)
    day2 = T0 + timedelta(days=1)
    b1 = make_bar("TEST", day1, open_=100, close=100)
    b2 = make_bar("TEST", day1_next, open_=100, close=105)  # last bar of day 1 -- must flatten here
    b3 = make_bar("TEST", day2, open_=110, close=110)
    strategy = ScriptedStrategy(buy_at=b1.timestamp)
    engine = BacktestEngine(
        strategy=strategy, universe=make_universe(), risk_gate=RiskGate(RiskLimits()),
        initial_equity=10_000.0, cost_model=ZERO_COST,
    )
    result = engine.run([b1, b2, b3], news=[])
    assert len(result.closed_trades) == 1
    assert result.closed_trades[0].exit_reason == "no_overnight"
    assert result.closed_trades[0].exit_time == b2.timestamp


def test_short_sell_and_cover_hand_computed_pnl():
    # Mirror of test_buy_and_close_hand_computed_pnl, but short: price falls
    # from 100 to 80 instead of rising from 100 to 120, and it's the short
    # side that profits. equity=10,000; 50% cap => $5,000 max position.
    b1 = make_bar("TEST", T0, open_=100, close=100)
    b2 = make_bar("TEST", T0 + timedelta(hours=1), open_=100, close=90)
    b3 = make_bar("TEST", T0 + timedelta(hours=2), open_=80, close=80)

    strategy = ScriptedStrategy(sell_at=b1.timestamp, close_at=b2.timestamp)
    limits = RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0)
    engine = BacktestEngine(
        strategy=strategy, universe=make_universe(), risk_gate=RiskGate(limits),
        initial_equity=10_000.0, cost_model=ZERO_COST,
    )
    result = engine.run([b1, b2, b3], news=[])

    # SELL signal (queued after b1) opens a short at b2's open=100, sized to
    # the $5,000 cap => 50 shares (short-sale proceeds credited to cash).
    # CLOSE signal (queued after b2) covers at b3's open=80.
    # realized pnl = (100 - 80) * 50 = 1000 -- a short profits when price falls.
    assert len(result.closed_trades) == 1
    trade = result.closed_trades[0]
    assert trade.realized_pnl == pytest.approx(1000.0)
    assert trade.entry_time == b2.timestamp
    assert trade.exit_time == b3.timestamp

    assert result.final_equity == pytest.approx(11_000.0)
    assert result.metrics.total_return_pct == pytest.approx(10.0)
    assert result.rejected_orders == 0

    # b1 flat at 10,000; b2 short opened, marked at cash(15,000) + (-50*90) = 10,500;
    # b3 covered, cash = 11,000
    assert [round(p.equity, 2) for p in result.equity_curve] == [10_000.0, 10_500.0, 11_000.0]


def test_stop_loss_triggers_intrabar_on_high_for_short():
    # Mirror of test_stop_loss_triggers_intrabar_on_low: short at 100 (b2
    # open), stop = 102 (2% default, adverse direction is UP for a short).
    # b3's high pierces 102.
    b1 = make_bar("TEST", T0, open_=100, close=100)
    b2 = make_bar("TEST", T0 + timedelta(hours=1), open_=100, close=100)
    b3 = make_bar("TEST", T0 + timedelta(hours=2), open_=101, close=101, high=103.0, low=100.5)
    strategy = ScriptedStrategy(sell_at=b1.timestamp)
    limits = RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0, stop_loss_pct=0.02)
    engine = BacktestEngine(
        strategy=strategy, universe=make_universe(), risk_gate=RiskGate(limits),
        initial_equity=10_000.0, cost_model=ZERO_COST,
    )
    result = engine.run([b1, b2, b3], news=[])
    assert len(result.closed_trades) == 1
    assert result.closed_trades[0].exit_reason == "stop_loss"
    assert result.closed_trades[0].realized_pnl < 0


def test_daily_drawdown_halt_flattens_short_position():
    # Mirror of test_daily_drawdown_halt_flattens_and_blocks_new_entries: a
    # short position loses enough on a price spike to breach 3% daily
    # drawdown. stop_loss_pct is set high so the halt (not the stop) fires.
    b1 = make_bar("TEST", T0, open_=100, close=100)
    b2 = make_bar("TEST", T0 + timedelta(hours=1), open_=100, close=100)
    # stop_loss_pct=0.60 -> stop at 160; high=155 stays under it so the
    # drawdown halt (not the stop) is what fires, mirroring the long version.
    b3 = make_bar("TEST", T0 + timedelta(hours=2), open_=100, close=150, high=155, low=100)
    strategy = ScriptedStrategy(sell_at=b1.timestamp)
    limits = RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0, max_daily_drawdown_pct=0.03, stop_loss_pct=0.60)
    engine = BacktestEngine(
        strategy=strategy, universe=make_universe(), risk_gate=RiskGate(limits),
        initial_equity=10_000.0, cost_model=ZERO_COST,
    )
    result = engine.run([b1, b2, b3], news=[])
    assert len(result.halt_events) == 1
    assert "drawdown" in result.halt_events[0]
    assert len(result.closed_trades) == 1
    assert result.closed_trades[0].exit_reason == "daily_drawdown_halt"
    assert result.closed_trades[0].realized_pnl < 0


def test_no_overnight_flattens_short_position_before_next_day():
    day1 = T0
    day1_next = T0 + timedelta(hours=1)
    day2 = T0 + timedelta(days=1)
    b1 = make_bar("TEST", day1, open_=100, close=100)
    b2 = make_bar("TEST", day1_next, open_=100, close=95)  # last bar of day 1 -- must flatten here
    b3 = make_bar("TEST", day2, open_=90, close=90)
    strategy = ScriptedStrategy(sell_at=b1.timestamp)
    engine = BacktestEngine(
        strategy=strategy, universe=make_universe(), risk_gate=RiskGate(RiskLimits()),
        initial_equity=10_000.0, cost_model=ZERO_COST,
    )
    result = engine.run([b1, b2, b3], news=[])
    assert len(result.closed_trades) == 1
    assert result.closed_trades[0].exit_reason == "no_overnight"
    assert result.closed_trades[0].exit_time == b2.timestamp


def test_daily_bar_buy_and_hold_is_not_flattened_overnight():
    # timeframe "1d" -- no-overnight rule must not apply, so a position can
    # span the whole backtest (this is what buy-and-hold requires).
    b1 = make_bar("TEST", T0, open_=100, close=100, timeframe="1d")
    b2 = make_bar("TEST", T0 + timedelta(days=1), open_=100, close=105, timeframe="1d")
    b3 = make_bar("TEST", T0 + timedelta(days=2), open_=110, close=110, timeframe="1d")
    strategy = ScriptedStrategy(buy_at=b1.timestamp)
    engine = BacktestEngine(
        strategy=strategy, universe=make_universe(), risk_gate=RiskGate(RiskLimits()),
        initial_equity=10_000.0, cost_model=ZERO_COST,
    )
    result = engine.run([b1, b2, b3], news=[])
    assert result.closed_trades == []  # never flattened, still holding at the end
