from datetime import datetime, timedelta, timezone

import pytest

from engine.backtest.costs import CostModel
from engine.backtest.engine import BacktestEngine
from engine.config.settings import RiskLimits
from engine.data.universe import Instrument, Universe
from engine.domain import Bar, MarketContext, NewsItem, SignalAction
from engine.risk.gate import RiskGate
from engine.strategy.baselines import BuyAndHoldStrategy, RandomEntryStrategy
from engine.strategy.dumb_news import DumbNewsStrategy
from engine.strategy.overnight_gap import OvernightGapStrategy, is_outside_us_market_hours

T0 = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
ZERO_COST = CostModel(commission_per_share=0.0, min_commission=0.0, tick_size=0.0)


def bar(symbol, ts, price=100.0, timeframe="1d"):
    return Bar(symbol=symbol, timestamp=ts, open=price, high=price, low=price, close=price, volume=100, timeframe=timeframe)


def ctx(ts, bars: dict):
    return MarketContext(timestamp=ts, latest_bars=bars, bar_history={s: [b] for s, b in bars.items()}, tradable_symbols=frozenset(bars.keys()))


def news(headline, published, ingested=None, sentiment=None, routed=()):
    ingested = ingested or published
    return NewsItem(
        id=headline, source="test", published_at=published, ingested_at=ingested, headline=headline,
        url=None, raw_payload={}, routed_symbols=routed, sentiment_score=sentiment,
    )


# -- BuyAndHold ----------------------------------------------------------
def test_buy_and_hold_buys_each_symbol_exactly_once():
    strat = BuyAndHoldStrategy(symbols=["SPY", "QQQ"])
    c = ctx(T0, {"SPY": bar("SPY", T0), "QQQ": bar("QQQ", T0)})
    signals = strat.on_bar(c)
    assert {s.symbol for s in signals} == {"SPY", "QQQ"}
    assert all(s.action == SignalAction.BUY for s in signals)
    assert strat.on_bar(c) == []  # never buys again


# -- RandomEntry -----------------------------------------------------------
def test_random_entry_exits_after_configured_bars():
    strat = RandomEntryStrategy(symbols=["SPY"], entry_probability_per_bar=1.0, exit_after_bars=2, seed=1)
    c = ctx(T0, {"SPY": bar("SPY", T0)})
    entry = strat.on_bar(c)
    assert len(entry) == 1 and entry[0].action == SignalAction.BUY
    assert strat.on_bar(c) == []  # bar 1 since entry, not yet exit
    exit_signals = strat.on_bar(c)
    assert len(exit_signals) == 1 and exit_signals[0].action == SignalAction.CLOSE


def test_random_entry_is_deterministic_given_seed():
    a = RandomEntryStrategy(symbols=["SPY"], entry_probability_per_bar=0.3, seed=42)
    b = RandomEntryStrategy(symbols=["SPY"], entry_probability_per_bar=0.3, seed=42)
    c = ctx(T0, {"SPY": bar("SPY", T0)})
    assert a.on_bar(c) == b.on_bar(c)


# -- DumbNews ---------------------------------------------------------------
def test_dumb_news_buys_on_positive_sentiment_above_threshold():
    strat = DumbNewsStrategy(sentiment_threshold=0.5, exit_after_hours=4.0)
    c = ctx(T0, {"AAPL": bar("AAPL", T0)})
    item = news("great earnings", T0, sentiment=0.8, routed=("AAPL",))
    signals = strat.on_news(c, item)
    assert len(signals) == 1
    assert signals[0].symbol == "AAPL"
    assert signals[0].action == SignalAction.BUY


def test_dumb_news_ignores_below_threshold_sentiment():
    strat = DumbNewsStrategy(sentiment_threshold=0.5)
    c = ctx(T0, {"AAPL": bar("AAPL", T0)})
    item = news("mildly positive", T0, sentiment=0.2, routed=("AAPL",))
    assert strat.on_news(c, item) == []


def test_dumb_news_exits_after_configured_hours():
    strat = DumbNewsStrategy(sentiment_threshold=0.5, exit_after_hours=4.0)
    c1 = ctx(T0, {"AAPL": bar("AAPL", T0)})
    strat.on_news(c1, news("great", T0, sentiment=0.9, routed=("AAPL",)))
    too_soon = ctx(T0 + timedelta(hours=2), {"AAPL": bar("AAPL", T0)})
    assert strat.on_bar(too_soon) == []
    right_on_time = ctx(T0 + timedelta(hours=4), {"AAPL": bar("AAPL", T0)})
    signals = strat.on_bar(right_on_time)
    assert len(signals) == 1 and signals[0].action == SignalAction.CLOSE


# -- OvernightGap ------------------------------------------------------------
def make_universe():
    return Universe(
        instruments=(
            Instrument(symbol="EWJ", tier=2, asset_class="equity_etf", news_topics=("boj",)),
            Instrument(symbol="SPY", tier=1, asset_class="equity_etf", news_topics=("fed",)),
        ),
        source_text="x",
    )


def test_is_outside_us_market_hours():
    assert is_outside_us_market_hours(datetime(2026, 1, 5, 3, tzinfo=timezone.utc))  # 3am UTC, overnight
    assert not is_outside_us_market_hours(datetime(2026, 1, 5, 16, tzinfo=timezone.utc))  # midday US session


def test_overnight_gap_only_reacts_to_overnight_tier2_news():
    universe = make_universe()
    strat = OvernightGapStrategy(universe, sentiment_threshold=0.5, exit_after_hours=3.0)
    overnight_news = news("BOJ hikes rates", datetime(2026, 1, 5, 2, tzinfo=timezone.utc), sentiment=0.8, routed=("EWJ",))
    strat.on_news(ctx(overnight_news.published_at, {}), overnight_news)
    assert "EWJ" in strat._pending

    # Tier-1 symbol routed news should never populate pending, even overnight
    tier1_news = news("Fed hints at cuts", datetime(2026, 1, 5, 3, tzinfo=timezone.utc), sentiment=0.9, routed=("SPY",))
    strat.on_news(ctx(tier1_news.published_at, {}), tier1_news)
    assert "SPY" not in strat._pending


def test_overnight_gap_enters_at_us_open_not_before():
    universe = make_universe()
    strat = OvernightGapStrategy(universe, sentiment_threshold=0.5, exit_after_hours=3.0)
    item = news("BOJ hikes rates", datetime(2026, 1, 5, 2, tzinfo=timezone.utc), sentiment=0.8, routed=("EWJ",))
    strat.on_news(ctx(item.published_at, {}), item)

    before_open = ctx(datetime(2026, 1, 5, 12, tzinfo=timezone.utc), {"EWJ": bar("EWJ", T0)})
    assert strat.on_bar(before_open) == []

    at_open = ctx(datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc), {"EWJ": bar("EWJ", T0)})
    signals = strat.on_bar(at_open)
    assert len(signals) == 1
    assert signals[0].symbol == "EWJ"
    assert signals[0].action == SignalAction.BUY


def test_overnight_gap_end_to_end_through_backtester():
    from engine.data.router import tag_and_route

    universe = make_universe()
    strat = OvernightGapStrategy(universe, sentiment_threshold=0.5, exit_after_hours=2.0)
    engine = BacktestEngine(
        strategy=strat, universe=universe, risk_gate=RiskGate(RiskLimits()),
        initial_equity=10_000.0, cost_model=ZERO_COST,
    )
    # entry SIGNAL generated at 14:30 (the open), FILLED at 15:30's open (51.0).
    # exit SIGNAL generated at 16:30 (2h after the 14:30 decision), FILLED at
    # 17:30's open (52.0) -- signals always fill on the *next* bar, never the
    # bar that produced them, so a trailing bar is needed to realize the exit.
    b_before_open = bar("EWJ", datetime(2026, 1, 5, 13, tzinfo=timezone.utc), price=50.0, timeframe="1h")
    b_at_open = bar("EWJ", datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc), price=50.0, timeframe="1h")
    b_entry_fill = bar("EWJ", datetime(2026, 1, 5, 15, 30, tzinfo=timezone.utc), price=51.0, timeframe="1h")
    b_exit_signal = bar("EWJ", datetime(2026, 1, 5, 16, 30, tzinfo=timezone.utc), price=51.5, timeframe="1h")
    b_exit_fill = bar("EWJ", datetime(2026, 1, 5, 17, 30, tzinfo=timezone.utc), price=52.0, timeframe="1h")

    raw_item = news("BOJ hikes rates unexpectedly", datetime(2026, 1, 5, 2, tzinfo=timezone.utc), sentiment=0.7)
    tagged_item = tag_and_route(raw_item, universe)
    assert tagged_item.routed_symbols == ("EWJ",)
    scored_item = news(  # tag_and_route drops sentiment_score (set pre-routing), so re-attach it
        tagged_item.headline, tagged_item.published_at, sentiment=0.7, routed=tagged_item.routed_symbols,
    )

    result = engine.run(
        [b_before_open, b_at_open, b_entry_fill, b_exit_signal, b_exit_fill], news=[scored_item]
    )

    assert len(result.closed_trades) == 1
    trade = result.closed_trades[0]
    assert trade.symbol == "EWJ"
    assert trade.exit_reason == "overnight_gap_exit_horizon"
    assert trade.realized_pnl == pytest.approx((52.0 - 51.0) * (500.0 / 51.0))
