from datetime import datetime, timedelta, timezone

import pytest

from engine.backtest.metrics import (
    ClosedTrade,
    EquityPoint,
    avg_exposure_pct,
    avg_holding_hours,
    max_drawdown_pct,
    profit_factor,
    sharpe_ratio,
    total_return_pct,
    win_rate,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def eq(values: list[float]) -> list[EquityPoint]:
    return [EquityPoint(timestamp=T0 + timedelta(days=i), equity=v) for i, v in enumerate(values)]


def test_total_return_hand_computed():
    assert total_return_pct(eq([100, 110, 105, 120])) == pytest.approx(20.0)


def test_max_drawdown_hand_computed():
    # peak sequence: 100, 110, 110, 120 -> worst dip is at 105 vs peak 110
    assert max_drawdown_pct(eq([100, 110, 105, 120])) == pytest.approx((110 - 105) / 110 * 100)


def test_max_drawdown_monotonic_up_is_zero():
    assert max_drawdown_pct(eq([100, 105, 110, 120])) == pytest.approx(0.0)


def test_max_drawdown_all_the_way_down():
    assert max_drawdown_pct(eq([100, 80, 60])) == pytest.approx(40.0)


def test_win_rate_hand_computed():
    trades = [
        ClosedTrade("AAPL", T0, T0, 10.0, "s"),
        ClosedTrade("AAPL", T0, T0, -5.0, "s"),
        ClosedTrade("AAPL", T0, T0, 3.0, "s"),
        ClosedTrade("AAPL", T0, T0, -1.0, "s"),
    ]
    assert win_rate(trades) == pytest.approx(50.0)


def test_profit_factor_hand_computed():
    trades = [
        ClosedTrade("AAPL", T0, T0, 30.0, "s"),
        ClosedTrade("AAPL", T0, T0, -10.0, "s"),
        ClosedTrade("AAPL", T0, T0, -10.0, "s"),
    ]
    assert profit_factor(trades) == pytest.approx(1.5)  # 30 / 20


def test_profit_factor_no_losses_is_inf():
    trades = [ClosedTrade("AAPL", T0, T0, 10.0, "s")]
    assert profit_factor(trades) == float("inf")


def test_avg_holding_hours_hand_computed():
    trades = [
        ClosedTrade("AAPL", T0, T0 + timedelta(hours=2), 1.0, "s"),
        ClosedTrade("AAPL", T0, T0 + timedelta(hours=6), 1.0, "s"),
    ]
    assert avg_holding_hours(trades) == pytest.approx(4.0)


def test_avg_exposure_pct_hand_computed():
    points = [
        EquityPoint(timestamp=T0, equity=100.0, exposure=20.0),
        EquityPoint(timestamp=T0, equity=100.0, exposure=40.0),
    ]
    assert avg_exposure_pct(points) == pytest.approx(30.0)


def test_sharpe_ratio_hand_computed_daily():
    # 5 points, one per calendar day -- 4 daily returns, annualized against
    # 365.25 elapsed calendar days/year (not a 252-trading-day convention;
    # see sharpe_ratio's docstring for why).
    assert sharpe_ratio(eq([100, 102, 101, 103, 105])) == pytest.approx(15.9810364208707, rel=1e-9)


def test_sharpe_ratio_collapses_same_timestamp_points_from_a_multi_symbol_universe():
    # Simulates a 3-symbol universe: 3 EquityPoints per calendar day (one
    # BAR event per symbol), only the last of which is the true end-of-day
    # mark-to-market equity. Must give the identical result to the plain
    # daily curve above -- this is the bug fix itself: before deduping, this
    # curve had 15 points instead of 5, and sqrt(252) was applied to a
    # returns series that wasn't actually one-per-trading-day.
    values = [100, 102, 101, 103, 105]
    padded = []
    for i, v in enumerate(values):
        ts = T0 + timedelta(days=i)
        padded.append(EquityPoint(timestamp=ts, equity=v - 1))  # symbol A's intra-day mark, discarded
        padded.append(EquityPoint(timestamp=ts, equity=v - 0.5))  # symbol B's intra-day mark, discarded
        padded.append(EquityPoint(timestamp=ts, equity=v))  # symbol C's mark -- last one wins
    assert sharpe_ratio(padded) == pytest.approx(sharpe_ratio(eq(values)), rel=1e-9)


def test_sharpe_ratio_annualizes_hourly_data_differently_from_daily():
    # Same 4 returns, same shape, but spaced 1 hour apart instead of 1 day
    # -- periods_per_year must scale up accordingly (24x more periods per
    # elapsed year), giving a materially different Sharpe than the daily
    # case even though the underlying return sequence is identical. This is
    # exactly the case (overnight_gap's hourly-bar backtest runs) that a
    # hardcoded periods_per_year=252 got wrong.
    values = [100, 102, 101, 103, 105]
    hourly = [EquityPoint(timestamp=T0 + timedelta(hours=i), equity=v) for i, v in enumerate(values)]
    assert sharpe_ratio(hourly) == pytest.approx(78.29076958393433, rel=1e-9)
    assert sharpe_ratio(hourly) != pytest.approx(sharpe_ratio(eq(values)))


def test_sharpe_ratio_zero_with_fewer_than_three_points():
    assert sharpe_ratio(eq([100, 105])) == 0.0


def test_sharpe_ratio_zero_when_all_returns_identical():
    # Exactly zero variance (doubling each step is exactly representable in
    # floating point) -- must return 0.0, not divide by zero.
    assert sharpe_ratio(eq([100, 200, 400, 800])) == 0.0
