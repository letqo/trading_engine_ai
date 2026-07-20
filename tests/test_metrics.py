from datetime import datetime, timedelta, timezone

import pytest

from engine.backtest.metrics import (
    ClosedTrade,
    EquityPoint,
    avg_exposure_pct,
    avg_holding_hours,
    max_drawdown_pct,
    profit_factor,
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
