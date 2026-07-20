"""Live analog of engine.backtest.engine: the same Strategy object,
RiskGate, and Signal->order translation, reacting to real-time bars/news
polled from the same data sources instead of replaying history from a
prebuilt event stream.

Fidelity notes (where this necessarily differs from the backtester, and why):
  - Bars and news are dispatched in two separate passes per cycle (all new
    bars, then all new news), not perfectly interleaved by timestamp the
    way build_event_stream sorts a whole backtest upfront. Over a short
    poll interval this ordering difference is immaterial; it would not be
    for a multi-year backtest, which is exactly why the backtester still
    sorts everything properly.
  - The backtester's no-overnight-position rule relies on knowing, in
    advance, which bar is the last of the day (look-ahead within already-
    fetched historical data) -- that information doesn't exist live. This
    loop does not (yet) replicate that specific safety net; each strategy's
    own exit-after-N-hours/bars logic is what bounds how long a live
    position stays open. See JOURNAL.md.
  - Stop-loss checks bypass RiskGate.evaluate() and write directly to the
    in-memory account, exactly like the backtester's _check_stop -- a
    risk-reducing forced exit is never something evaluate()'s opening caps
    should be able to block.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from engine.data.bars import bars_to_domain, fetch_bars
from engine.data.news import fetch_all_rss
from engine.data.router import tag_and_route
from engine.data.universe import Universe
from engine.domain import Bar, MarketContext, NewsItem
from engine.execution.broker import Broker
from engine.execution.position_bookkeeping import apply_closing_fill, apply_opening_fill
from engine.execution.signal_translation import signal_to_side
from engine.features.sentiment import score_news_item
from engine.logging_setup import get_logger
from engine.risk.gate import RiskGate
from engine.risk.models import AccountState, OrderRequest, Side

logger = get_logger(__name__)

_INTERVAL_TO_TIMEDELTA = {"1d": timedelta(days=1), "1h": timedelta(hours=1), "5m": timedelta(minutes=5)}


@dataclass
class LiveLoopState:
    bar_history: dict[str, list[Bar]] = field(default_factory=lambda: defaultdict(list))
    latest_bars: dict[str, Bar] = field(default_factory=dict)
    last_news_decision_timestamp: datetime | None = None


def _interval_to_timedelta(interval: str) -> timedelta:
    return _INTERVAL_TO_TIMEDELTA.get(interval, timedelta(days=1))


def seed_bar_history(universe: Universe, interval: str, state: LiveLoopState, lookback_periods: int = 30) -> None:
    """Populate bar_history/latest_bars with real recent history *without*
    dispatching any on_bar calls -- indicator strategies (momentum, mean
    reversion, multi_factor) need real lookback to say anything meaningful,
    and firing signals off a startup history-replay would react to stale
    information as if it just happened. Call this once before polling."""
    symbols = sorted(universe.tradable_symbols())
    end = datetime.now(timezone.utc)
    start = end - _interval_to_timedelta(interval) * lookback_periods
    df = fetch_bars(symbols, start=str(start.date()), end=str((end + timedelta(days=1)).date()), interval=interval)
    if df.empty:
        return
    for bar in sorted(bars_to_domain(df), key=lambda b: b.timestamp):
        state.bar_history[bar.symbol].append(bar)
        state.latest_bars[bar.symbol] = bar


def fetch_new_bars(universe: Universe, interval: str, state: LiveLoopState, lookback_periods: int = 3) -> list[Bar]:
    """Fetch a short recent window (enough to catch the latest completed
    bar even with some fetch latency) and return only bars strictly newer
    than whatever was last seen for that symbol, sorted chronologically
    across symbols -- the same merged-timestamp-order guarantee the
    backtester's event stream gives, built incrementally instead."""
    symbols = sorted(universe.tradable_symbols())
    end = datetime.now(timezone.utc)
    start = end - _interval_to_timedelta(interval) * lookback_periods
    df = fetch_bars(symbols, start=str(start.date()), end=str((end + timedelta(days=1)).date()), interval=interval)
    if df.empty:
        return []

    new_bars = []
    for bar in bars_to_domain(df):
        last_seen = state.latest_bars.get(bar.symbol)
        if last_seen is not None and bar.timestamp <= last_seen.timestamp:
            continue
        new_bars.append(bar)
    new_bars.sort(key=lambda b: b.timestamp)
    return new_bars


def fetch_new_news(universe: Universe, state: LiveLoopState) -> list[NewsItem]:
    """Current RSS headlines, tagged/routed/scored, filtered to ones
    decided after whatever was last processed. No seeding needed here --
    unlike bars, RSS only ever shows currently-live items anyway, so the
    first cycle's items are genuinely current, not stale history."""
    items = []
    for raw in fetch_all_rss():
        tagged = tag_and_route(raw, universe)
        scored = score_news_item(tagged)
        if state.last_news_decision_timestamp is not None and scored.decision_timestamp <= state.last_news_decision_timestamp:
            continue
        items.append(scored)
    items.sort(key=lambda i: i.decision_timestamp)
    return items


def _build_context(timestamp: datetime, state: LiveLoopState, tradable_symbols: set[str]) -> MarketContext:
    return MarketContext(
        timestamp=timestamp,
        latest_bars=dict(state.latest_bars),
        bar_history={s: list(h) for s, h in state.bar_history.items()},
        tradable_symbols=frozenset(tradable_symbols),
    )


def _check_and_submit_stop(bar: Bar, risk_gate: RiskGate, broker: Broker, account: AccountState) -> bool:
    """Mirrors the backtester's _check_stop: a triggered stop bypasses
    RiskGate.evaluate() entirely and writes straight to the account, same
    as every other risk-reducing forced exit in this codebase (flatten,
    kill switch)."""
    position = account.positions.get(bar.symbol)
    if position is None or position.quantity == 0:
        return False
    adverse_price = bar.high if position.quantity < 0 else bar.low
    if not risk_gate.is_stop_triggered(position, current_price=adverse_price):
        return False

    exit_side = Side.SELL if position.quantity > 0 else Side.BUY
    qty = abs(position.quantity)
    broker_order = broker.submit_order(
        OrderRequest(symbol=bar.symbol, side=exit_side, quantity=qty, price=bar.close,
                     timestamp=bar.timestamp, strategy_id=position.strategy_id)
    )
    apply_closing_fill(account, risk_gate, bar.symbol, position, qty, bar.close)
    logger.warning(
        "live stop-loss triggered",
        extra={"extra_fields": {"symbol": bar.symbol, "order_id": broker_order.broker_order_id}},
    )
    return True


def _submit_signals(signals, risk_gate: RiskGate, broker: Broker, account: AccountState, state: LiveLoopState, tradable: set[str]) -> dict:
    """Translate strategy signals into real orders. Opening orders are
    oversized on purpose and let RiskGate.evaluate() clip them to the real
    cap, same pattern used everywhere else in this codebase
    (engine.backtest.engine, engine.prediction.trading)."""
    orders = rejected = 0
    for signal in signals:
        if signal.symbol not in tradable:
            continue
        latest_bar = state.latest_bars.get(signal.symbol)
        if latest_bar is None:
            continue
        price = latest_bar.close

        existing = account.positions.get(signal.symbol)
        signed_qty = existing.quantity if existing is not None else 0.0
        side = signal_to_side(signal, signed_qty)
        if side is None:
            continue

        is_closing = existing is not None and (
            (side == Side.SELL and existing.quantity > 0) or (side == Side.BUY and existing.quantity < 0)
        )
        if is_closing:
            candidate_qty = abs(existing.quantity)
        else:
            cap_value = account.equity * risk_gate.limits.max_capital_per_position_pct
            candidate_qty = (cap_value * 2) / price  # oversized on purpose; RiskGate clips to the real cap

        order = OrderRequest(
            symbol=signal.symbol, side=side, quantity=candidate_qty, price=price,
            timestamp=signal.timestamp, strategy_id=signal.strategy_id,
        )
        decision = risk_gate.evaluate(order, account, tradable)
        if not decision.approved:
            rejected += 1
            logger.info(
                "live signal rejected by RiskGate",
                extra={"extra_fields": {"symbol": signal.symbol, "reason": decision.reason.value}},
            )
            continue

        broker_order = broker.submit_order(
            OrderRequest(
                symbol=signal.symbol, side=side, quantity=decision.approved_quantity, price=price,
                timestamp=signal.timestamp, strategy_id=signal.strategy_id,
            )
        )
        if is_closing:
            apply_closing_fill(account, risk_gate, signal.symbol, existing, decision.approved_quantity, price)
        else:
            apply_opening_fill(account, signal.symbol, side, decision.approved_quantity, price, signal.strategy_id)
        orders += 1
        logger.info(
            "live order submitted",
            extra={"extra_fields": {
                "symbol": signal.symbol, "side": side.value, "qty": decision.approved_quantity,
                "order_id": broker_order.broker_order_id, "reason": signal.reason,
            }},
        )
    return {"orders": orders, "rejected": rejected}


def run_live_cycle(
    strategy, universe: Universe, risk_gate: RiskGate, broker: Broker, account: AccountState,
    state: LiveLoopState, interval: str,
) -> dict:
    """One polling cycle: fetch new bars+news, dispatch to the strategy in
    chronological order (bars first, then news -- see module docstring),
    checking stops on every new bar and translating resulting signals into
    real orders through RiskGate. Returns a summary dict for logging."""
    tradable = universe.tradable_symbols()
    summary = {"bars": 0, "news": 0, "signals": 0, "orders": 0, "rejected": 0, "stops": 0}

    for bar in fetch_new_bars(universe, interval, state):
        state.latest_bars[bar.symbol] = bar
        state.bar_history[bar.symbol].append(bar)
        summary["bars"] += 1

        if _check_and_submit_stop(bar, risk_gate, broker, account):
            summary["stops"] += 1

        if account.halted:
            continue
        ctx = _build_context(bar.timestamp, state, tradable)
        signals = strategy.on_bar(ctx)
        result = _submit_signals(signals, risk_gate, broker, account, state, tradable)
        summary["signals"] += len(signals)
        summary["orders"] += result["orders"]
        summary["rejected"] += result["rejected"]

    for item in fetch_new_news(universe, state):
        state.last_news_decision_timestamp = item.decision_timestamp
        summary["news"] += 1
        if account.halted:
            continue
        ctx = _build_context(item.decision_timestamp, state, tradable)
        signals = strategy.on_news(ctx, item)
        result = _submit_signals(signals, risk_gate, broker, account, state, tradable)
        summary["signals"] += len(signals)
        summary["orders"] += result["orders"]
        summary["rejected"] += result["rejected"]

    return summary
