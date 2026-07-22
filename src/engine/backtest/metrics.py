"""Performance metrics computed from an equity curve and closed-trade log.
Pure functions, hand-verifiable against toy scenarios."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    entry_time: datetime
    exit_time: datetime
    realized_pnl: float
    strategy_id: str
    quantity: float = 0.0
    exit_price: float = 0.0
    exit_reason: str = ""

    @property
    def holding_hours(self) -> float:
        return (self.exit_time - self.entry_time).total_seconds() / 3600.0


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    equity: float
    exposure: float = 0.0  # dollar value of open positions at this point


def total_return_pct(equity_curve: list[EquityPoint]) -> float:
    if len(equity_curve) < 2:
        return 0.0
    start, end = equity_curve[0].equity, equity_curve[-1].equity
    if start == 0:
        return 0.0
    return (end - start) / start * 100.0


def max_drawdown_pct(equity_curve: list[EquityPoint]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0].equity
    worst = 0.0
    for point in equity_curve:
        peak = max(peak, point.equity)
        if peak > 0:
            drawdown = (peak - point.equity) / peak
            worst = max(worst, drawdown)
    return worst * 100.0


def _dedupe_by_timestamp(equity_curve: list[EquityPoint]) -> list[EquityPoint]:
    """Collapse same-timestamp points to the last one seen. The engine
    appends one EquityPoint per BAR event, and a multi-symbol universe emits
    one BAR event per (symbol, timestamp) pair even when many symbols share
    the same timestamp (e.g. every symbol's daily bar closes at the same
    calendar day) -- only the last point at a given timestamp reflects the
    fully mark-to-market portfolio once every symbol at that instant has
    been processed. build_event_stream sorts globally by timestamp, so
    same-timestamp points are always contiguous here."""
    deduped: list[EquityPoint] = []
    for point in equity_curve:
        if deduped and deduped[-1].timestamp == point.timestamp:
            deduped[-1] = point
        else:
            deduped.append(point)
    return deduped


def sharpe_ratio(equity_curve: list[EquityPoint]) -> float:
    """Annualized Sharpe of the (deduplicated) mark-to-market equity curve.

    periods_per_year is derived from the curve's own timestamps rather than
    a caller-supplied constant. A previous version hardcoded 252 (one point
    per trading day) and nothing ever overrode it -- silently wrong for
    anything not literally one EquityPoint per calendar day, which in
    practice included every backtest ever run here (a multi-symbol universe
    produces many same-timestamp points per day, and interval="1h" runs
    produce many points per day at hourly spacing). Deriving the factor from
    real elapsed time between (deduplicated) points is correct regardless of
    bar frequency, and removes an entire class of bug where a future call
    site forgets to pass the right constant for its own data's frequency.

    Deliberate choice: annualizes against 365.25 calendar days/year, not the
    conventional ~252 trading days/year. Using elapsed wall-clock time
    between real data points needs no trading-calendar knowledge (market
    holidays, weekends, exchange hours) to get right; a trading-day
    convention would require exactly that. This makes the absolute Sharpe
    magnitude slightly different from a strict trading-day annualization
    even for genuinely-daily equity curves, but it is comparable across
    every strategy/baseline pair here, which is what a validation
    comparison actually needs.
    """
    resampled = _dedupe_by_timestamp(equity_curve)
    if len(resampled) < 3:
        return 0.0
    returns = []
    for prev, curr in zip(resampled, resampled[1:]):
        if prev.equity == 0:
            continue
        returns.append((curr.equity - prev.equity) / prev.equity)
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(variance)
    if std == 0:
        return 0.0

    total_days = (resampled[-1].timestamp - resampled[0].timestamp).total_seconds() / 86400.0
    if total_days <= 0:
        return 0.0
    periods_per_year = len(returns) / total_days * 365.25
    return (mean / std) * math.sqrt(periods_per_year)


def win_rate(trades: list[ClosedTrade]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.realized_pnl > 0)
    return wins / len(trades) * 100.0


def profit_factor(trades: list[ClosedTrade]) -> float:
    gross_profit = sum(t.realized_pnl for t in trades if t.realized_pnl > 0)
    gross_loss = abs(sum(t.realized_pnl for t in trades if t.realized_pnl < 0))
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def avg_holding_hours(trades: list[ClosedTrade]) -> float:
    if not trades:
        return 0.0
    return sum(t.holding_hours for t in trades) / len(trades)


def avg_exposure_pct(equity_curve: list[EquityPoint]) -> float:
    if not equity_curve:
        return 0.0
    ratios = [p.exposure / p.equity for p in equity_curve if p.equity > 0]
    if not ratios:
        return 0.0
    return sum(ratios) / len(ratios) * 100.0


@dataclass(frozen=True)
class Metrics:
    total_return_pct: float
    max_drawdown_pct: float
    sharpe: float
    win_rate_pct: float
    profit_factor: float
    num_trades: int
    avg_holding_hours: float
    exposure_pct: float


def compute_metrics(equity_curve: list[EquityPoint], trades: list[ClosedTrade]) -> Metrics:
    return Metrics(
        total_return_pct=total_return_pct(equity_curve),
        max_drawdown_pct=max_drawdown_pct(equity_curve),
        sharpe=sharpe_ratio(equity_curve),
        win_rate_pct=win_rate(trades),
        profit_factor=profit_factor(trades),
        num_trades=len(trades),
        avg_holding_hours=avg_holding_hours(trades),
        exposure_pct=avg_exposure_pct(equity_curve),
    )
