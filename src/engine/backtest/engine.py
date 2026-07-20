"""Event-driven backtester.

SPEC.md requirements this satisfies:
  - process bars/news in strict timestamp order from a unified event queue,
    no vectorized shortcuts (engine.data.events.build_event_stream).
  - every order flows through RiskGate before it can affect the account --
    there is no path from a Signal to a fill that skips evaluate().
  - fills are pessimistic: next bar's open, one tick against the trader,
    plus commission (engine.backtest.costs.CostModel).
  - daily-drawdown halt, consecutive-loss halt, and no-overnight flattening
    are enforced identically to how the live loop would enforce them.

Both directions are supported: a BUY signal opens/adds to a long (or covers
an existing short), a SELL signal opens/adds to a short (or closes an
existing long), and CLOSE flattens whatever is open, whichever direction
that is. Position.quantity is signed (positive = long, negative = short)
throughout -- RiskGate.evaluate/_is_closing/is_stop_triggered/flatten_orders
were already written direction-agnostically; only this module's fill/P&L
logic needed to stop assuming long-only. One deliberate simplification
carried forward: margin requirements and stock-borrow fees for short
positions are not modeled -- opening a short here just credits the sale
proceeds to cash and debits them back on cover, with no borrow cost or
margin-call mechanic. That's an acceptable simplification for a paper-
trading research engine, but means the backtest is somewhat optimistic
about the true cost of holding a short relative to a real margin account.

Scope decision (v1): the no-overnight-positions rule is enforced for
intraday timeframes (where "before market close" is a real moment within
the data) and not for daily bars, where a bar already spans the full
session and there is no intraday close to flatten before. This is what
lets the buy-and-hold baseline -- itself a static reference computed on
daily bars, never a strategy that would run live -- hold a position across
the whole backtest, exactly as buy-and-hold must, while Overnight-Gap and
the dumb news strategy (which trade intraday bars) get the rule enforced.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from engine.backtest.costs import CostModel
from engine.backtest.metrics import ClosedTrade, EquityPoint, Metrics, compute_metrics
from engine.data.events import EventType, build_event_stream
from engine.data.universe import Universe
from engine.domain import Bar, MarketContext, NewsItem, SignalAction
from engine.risk.gate import RiskGate
from engine.risk.models import AccountState, OrderRequest, Position, Side


@dataclass
class PendingOrder:
    symbol: str
    side: Side
    strategy_id: str
    reason: str


@dataclass
class BacktestResult:
    metrics: Metrics
    equity_curve: list[EquityPoint]
    closed_trades: list[ClosedTrade]
    rejected_orders: int
    halt_events: list[str]
    final_equity: float


class BacktestEngine:
    def __init__(
        self,
        strategy,
        universe: Universe,
        risk_gate: RiskGate,
        initial_equity: float = 100_000.0,
        cost_model: CostModel | None = None,
        no_overnight_close_hour_utc: int = 20,
    ):
        self.strategy = strategy
        self.universe = universe
        self.risk_gate = risk_gate
        self.initial_equity = initial_equity
        self.costs = cost_model or CostModel()
        self._close_hour_utc = no_overnight_close_hour_utc

    def run(self, bars: list[Bar], news: list[NewsItem]) -> BacktestResult:
        events = build_event_stream(bars, news)
        universe_symbols = self.universe.tradable_symbols()

        account = AccountState(equity=self.initial_equity, cash=self.initial_equity)
        self.risk_gate.start_new_session(account)

        pending_orders: dict[str, list[PendingOrder]] = defaultdict(list)
        latest_bars: dict[str, Bar] = {}
        bar_history: dict[str, list[Bar]] = defaultdict(list)
        position_open_time: dict[str, datetime] = {}

        equity_curve: list[EquityPoint] = []
        closed_trades: list[ClosedTrade] = []
        halt_events: list[str] = []
        rejected_orders = 0

        last_bar_of_day = self._last_bar_of_day_flags(bars)
        current_date: date | None = None

        for event in events:
            if event.type == EventType.BAR:
                bar: Bar = event.payload

                if current_date is None or bar.timestamp.date() != current_date:
                    current_date = bar.timestamp.date()
                    self.risk_gate.start_new_session(account)

                rejected_orders += self._fill_pending(
                    account, pending_orders, bar, universe_symbols, closed_trades, position_open_time
                )

                latest_bars[bar.symbol] = bar
                bar_history[bar.symbol].append(bar)

                self._mark_to_market(account, latest_bars)
                self._check_stop(account, bar, closed_trades, position_open_time)
                self._mark_to_market(account, latest_bars)

                if self.risk_gate.check_daily_drawdown(account):
                    self._flatten_all(
                        account, bar.timestamp, latest_bars, closed_trades, position_open_time,
                        reason="daily_drawdown_halt",
                    )
                    halt_events.append(account.halt_reason)

                if bar.timeframe != "1d" and last_bar_of_day.get((bar.symbol, bar.timestamp)):
                    self._flatten_symbol(
                        account, bar.symbol, bar.timestamp, bar.close, closed_trades,
                        position_open_time, reason="no_overnight",
                    )
                    self._mark_to_market(account, latest_bars)

                if not account.halted:
                    ctx = self._context(bar.timestamp, latest_bars, bar_history, universe_symbols)
                    signals = self.strategy.on_bar(ctx)
                    self._queue_signals(pending_orders, signals, account)

                equity_curve.append(
                    EquityPoint(timestamp=bar.timestamp, equity=account.equity, exposure=account.total_exposure())
                )

            else:  # NEWS
                item: NewsItem = event.payload
                if account.halted:
                    continue
                ctx = self._context(item.decision_timestamp, latest_bars, bar_history, universe_symbols)
                signals = self.strategy.on_news(ctx, item)
                self._queue_signals(pending_orders, signals, account)

        metrics = compute_metrics(equity_curve, closed_trades)
        return BacktestResult(
            metrics=metrics,
            equity_curve=equity_curve,
            closed_trades=closed_trades,
            rejected_orders=rejected_orders,
            halt_events=halt_events,
            final_equity=account.equity,
        )

    # -- helpers --------------------------------------------------------------
    @staticmethod
    def _context(timestamp, latest_bars, bar_history, universe_symbols) -> MarketContext:
        return MarketContext(
            timestamp=timestamp,
            latest_bars=dict(latest_bars),
            bar_history={s: list(h) for s, h in bar_history.items()},
            tradable_symbols=frozenset(universe_symbols),
        )

    @staticmethod
    def _queue_signals(pending_orders, signals, account: AccountState) -> None:
        for signal in signals:
            existing = account.positions.get(signal.symbol)
            signed_qty = existing.quantity if existing is not None else 0.0

            if signal.action == SignalAction.BUY:
                side = Side.BUY
            elif signal.action == SignalAction.SELL:
                side = Side.SELL
            else:  # CLOSE -- flatten whichever direction is actually open
                if signed_qty == 0:
                    continue
                side = Side.SELL if signed_qty > 0 else Side.BUY

            pending_orders[signal.symbol].append(
                PendingOrder(symbol=signal.symbol, side=side, strategy_id=signal.strategy_id, reason=signal.reason)
            )

    def _fill_pending(self, account, pending_orders, bar: Bar, universe_symbols, closed_trades, position_open_time) -> int:
        orders = pending_orders.pop(bar.symbol, [])
        rejected = 0
        for po in orders:
            fill_price = self.costs.adverse_fill_price(bar.open, po.side)
            if fill_price <= 0:
                continue
            existing = account.positions.get(bar.symbol)
            is_closing_long = po.side == Side.SELL and existing is not None and existing.quantity > 0
            is_covering_short = po.side == Side.BUY and existing is not None and existing.quantity < 0

            if is_closing_long or is_covering_short:
                quantity = abs(existing.quantity)
            else:
                cap_value = account.equity * self.risk_gate.limits.max_capital_per_position_pct
                quantity = (cap_value * 2) / fill_price  # oversize on purpose; RiskGate clips to the real cap

            if quantity <= 0:
                continue

            order = OrderRequest(
                symbol=bar.symbol, side=po.side, quantity=quantity, price=fill_price,
                timestamp=bar.timestamp, strategy_id=po.strategy_id,
            )
            decision = self.risk_gate.evaluate(order, account, universe_symbols)
            if not decision.approved:
                rejected += 1
                continue
            self._execute_fill(account, decision, bar.timestamp, closed_trades, position_open_time, po.reason)
        return rejected

    def _execute_fill(self, account: AccountState, decision, timestamp, closed_trades, position_open_time, exit_reason: str) -> None:
        order = decision.order
        qty = decision.approved_quantity
        existing = account.positions.get(order.symbol)
        is_closing_long = order.side == Side.SELL and existing is not None and existing.quantity > 0
        is_covering_short = order.side == Side.BUY and existing is not None and existing.quantity < 0

        if is_closing_long or is_covering_short:
            self._realize_close(
                account, order.symbol, existing, qty, order.price, timestamp,
                closed_trades, position_open_time, exit_reason, order.strategy_id,
            )
            return

        # Opening or adding to a position: BUY opens/adds long, SELL opens/adds short.
        fees = self.costs.commission(qty)
        if order.side == Side.BUY:
            account.cash -= qty * order.price + fees
        else:
            account.cash += qty * order.price - fees  # short-sale proceeds

        signed_qty = qty if order.side == Side.BUY else -qty
        if existing is None or existing.quantity == 0:
            account.positions[order.symbol] = Position(
                symbol=order.symbol, quantity=signed_qty, avg_entry_price=order.price, opened_at=timestamp,
                strategy_id=order.strategy_id,
            )
            position_open_time[order.symbol] = timestamp
        else:
            total_cost = existing.avg_entry_price * abs(existing.quantity) + order.price * qty
            new_qty_abs = abs(existing.quantity) + qty
            existing.avg_entry_price = total_cost / new_qty_abs
            existing.quantity = new_qty_abs if existing.quantity > 0 else -new_qty_abs

    def _realize_close(
        self, account, symbol, position, qty, fill_price, timestamp,
        closed_trades, position_open_time, reason, strategy_id,
    ) -> None:
        """Shared close/cover P&L for a signed position -- used by
        _execute_fill (a strategy's own SELL/CLOSE signal), _check_stop, and
        _flatten_symbol, so the long-vs-short sign logic lives in one place."""
        fees = self.costs.commission(qty)
        if position.quantity > 0:  # closing a long
            realized_pnl = (fill_price - position.avg_entry_price) * qty - fees
            account.cash += qty * fill_price - fees
        else:  # covering a short
            realized_pnl = (position.avg_entry_price - fill_price) * qty - fees
            account.cash -= qty * fill_price + fees

        closed_trades.append(
            ClosedTrade(
                symbol=symbol,
                entry_time=position_open_time.get(symbol, timestamp),
                exit_time=timestamp,
                realized_pnl=realized_pnl,
                strategy_id=strategy_id,
                quantity=qty,
                exit_price=fill_price,
                exit_reason=reason,
            )
        )
        self.risk_gate.record_trade_result(account, realized_pnl)
        remaining = abs(position.quantity) - qty
        if remaining <= 1e-9:
            del account.positions[symbol]
            position_open_time.pop(symbol, None)
        else:
            position.quantity = remaining if position.quantity > 0 else -remaining

    @staticmethod
    def _mark_to_market(account: AccountState, latest_bars: dict[str, Bar]) -> None:
        market_value = 0.0
        for symbol, position in account.positions.items():
            bar = latest_bars.get(symbol)
            if bar is not None:
                market_value += position.quantity * bar.close
        account.equity = account.cash + market_value

    def _check_stop(self, account: AccountState, bar: Bar, closed_trades, position_open_time) -> None:
        position = account.positions.get(bar.symbol)
        if position is None or position.quantity == 0:
            return
        # Worst-case price within the bar: the low hurts a long, the high hurts a short.
        adverse_price = bar.high if position.quantity < 0 else bar.low
        if not self.risk_gate.is_stop_triggered(position, current_price=adverse_price):
            return
        entry_side = Side.BUY if position.quantity > 0 else Side.SELL
        exit_side = Side.SELL if position.quantity > 0 else Side.BUY
        stop_price = self.risk_gate.stop_loss_price(position.avg_entry_price, entry_side)
        fill_price = self.costs.adverse_fill_price(stop_price, exit_side)
        self._realize_close(
            account, bar.symbol, position, abs(position.quantity), fill_price, bar.timestamp,
            closed_trades, position_open_time, "stop_loss", position.strategy_id,
        )

    def _flatten_symbol(self, account, symbol, timestamp, price, closed_trades, position_open_time, reason: str) -> None:
        position = account.positions.get(symbol)
        if position is None or position.quantity == 0:
            return
        exit_side = Side.SELL if position.quantity > 0 else Side.BUY
        fill_price = self.costs.adverse_fill_price(price, exit_side)
        self._realize_close(
            account, symbol, position, abs(position.quantity), fill_price, timestamp,
            closed_trades, position_open_time, reason, position.strategy_id,
        )

    def _flatten_all(self, account, timestamp, latest_bars, closed_trades, position_open_time, reason: str) -> None:
        for symbol in list(account.positions.keys()):
            bar = latest_bars.get(symbol)
            price = bar.close if bar else account.positions[symbol].avg_entry_price
            self._flatten_symbol(account, symbol, timestamp, price, closed_trades, position_open_time, reason)
        self._mark_to_market(account, latest_bars)

    @staticmethod
    def _last_bar_of_day_flags(bars: list[Bar]) -> dict[tuple[str, datetime], bool]:
        by_symbol: dict[str, list[Bar]] = defaultdict(list)
        for bar in bars:
            by_symbol[bar.symbol].append(bar)
        flags: dict[tuple[str, datetime], bool] = {}
        for symbol, symbol_bars in by_symbol.items():
            ordered = sorted(symbol_bars, key=lambda b: b.timestamp)
            for i, bar in enumerate(ordered):
                is_last = i == len(ordered) - 1 or ordered[i + 1].timestamp.date() != bar.timestamp.date()
                flags[(symbol, bar.timestamp)] = is_last
        return flags
