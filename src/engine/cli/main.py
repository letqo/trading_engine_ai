"""SPEC.md cli/ commands: ingest, backtest, papertrade, report, kill (+ replay
for the Phase 1 demo). One typer app, thin wrappers around the real modules
-- no business logic lives here."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import typer

from engine.backtest.engine import BacktestEngine
from engine.backtest.perturbation import run_perturbation_analysis
from engine.config.guard import PaperOnlyViolation, enforce_paper_only
from engine.config.settings import get_settings
from engine.data.bars import bars_to_domain, fetch_bars
from engine.data.events import EventType, build_event_stream
from engine.data.news import fetch_all_rss
from engine.data.router import tag_and_route
from engine.data.snapshot import create_snapshot
from engine.data.universe import load_universe
from engine.execution.alpaca import AlpacaAuthError, AlpacaPaperClient
from engine.execution.reconcile import cancel_stale_orders, reconcile_account_state
from engine.features.sentiment import score_news_item
from engine.journal.db import get_session
from engine.journal.models import RunMode, TradeSide
from engine.journal.registry import (
    current_git_hash,
    record_metrics,
    record_reconciliation,
    record_trade,
    register_run,
)
from engine.logging_setup import configure_logging, get_logger
from engine.observability import alert_kill_switch, alert_risk_halt, alert_service_restart
from engine.risk.gate import RiskGate
from engine.risk.kill_switch import disengage_kill_switch, engage_kill_switch, is_kill_switch_engaged
from engine.strategy.baselines import BuyAndHoldStrategy, RandomEntryStrategy
from engine.strategy.dumb_news import DumbNewsStrategy
from engine.strategy.overnight_gap import OvernightGapStrategy

app = typer.Typer(add_completion=False, help="News-driven trading research engine (paper trading only).")
logger = get_logger("engine.cli")

STRATEGY_FACTORIES = {
    "buy_and_hold": lambda universe, seed: BuyAndHoldStrategy(symbols=sorted(universe.tradable_symbols())),
    "random_entry": lambda universe, seed: RandomEntryStrategy(symbols=sorted(universe.tradable_symbols()), seed=seed),
    "dumb_news": lambda universe, seed: DumbNewsStrategy(),
    "overnight_gap": lambda universe, seed: OvernightGapStrategy(universe),
}
NEWS_DRIVEN_STRATEGIES = {"dumb_news", "overnight_gap"}


def _fetch_scored_news(universe) -> list:
    items = []
    for raw in fetch_all_rss():
        tagged = tag_and_route(raw, universe)
        items.append(score_news_item(tagged))
    return items


@app.callback()
def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)


@app.command()
def ingest(
    start: str = typer.Option(..., help="YYYY-MM-DD"),
    end: str = typer.Option(..., help="YYYY-MM-DD"),
    interval: str = typer.Option("1d"),
    news: bool = typer.Option(True, help="also fetch + tag RSS news"),
    description: str = typer.Option("", help="human note for this snapshot"),
) -> None:
    """Fetch bars + news for the full tradable universe and register a
    reproducible DataSnapshot (Phase 1)."""
    settings = get_settings()
    universe = load_universe(settings.universe_path)
    with get_session(settings) as session:
        snapshot = create_snapshot(
            session, universe, start=start, end=end, data_dir=settings.data_dir,
            interval=interval, include_news=news, description=description,
        )
    typer.echo(f"snapshot {snapshot.id}: {snapshot.bar_row_count} bar rows, {snapshot.news_count} news items")


@app.command()
def replay(
    date: str = typer.Option(..., help="YYYY-MM-DD, a single day to replay"),
    symbols: str = typer.Option("SPY", help="comma-separated symbols"),
    interval: str = typer.Option("1h"),
) -> None:
    """Phase 1 demo: replay one past day's bar/news events in strict
    timestamp order, exactly as the backtester would see them."""
    settings = get_settings()
    universe = load_universe(settings.universe_path)
    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    unknown = set(symbol_list) - universe.symbols
    if unknown:
        typer.echo(f"not in universe.yaml: {unknown}", err=True)
        raise typer.Exit(1)

    day = datetime.fromisoformat(date).date()
    next_day = day + timedelta(days=1)
    bars_df = fetch_bars(symbol_list, start=str(day), end=str(next_day), interval=interval)
    bars = bars_to_domain(bars_df)

    news_items = [
        item for item in _fetch_scored_news(universe)
        if item.decision_timestamp.date() == day and set(item.routed_symbols) & set(symbol_list)
    ]

    events = build_event_stream(bars, news_items)
    if not events:
        typer.echo(f"no events found for {date} / {symbol_list}")
        return
    for event in events:
        if event.type == EventType.BAR:
            b = event.payload
            typer.echo(f"{event.timestamp.isoformat()}  BAR   {b.symbol:6s} O:{b.open:.2f} H:{b.high:.2f} L:{b.low:.2f} C:{b.close:.2f}")
        else:
            n = event.payload
            typer.echo(f"{event.timestamp.isoformat()}  NEWS  {','.join(n.routed_symbols) or '-':6s} sentiment={n.sentiment_score:+.2f} {n.headline[:70]}")


@app.command()
def backtest(
    strategy: str = typer.Option(..., help=f"one of: {', '.join(STRATEGY_FACTORIES)}"),
    start: str = typer.Option(..., help="YYYY-MM-DD"),
    end: str = typer.Option(..., help="YYYY-MM-DD"),
    interval: str = typer.Option("1d"),
    seed: int = typer.Option(None),
    equity: float = typer.Option(100_000.0),
    validation: bool = typer.Option(False, "--validation", help="this run touches the held-out validation period"),
    validation_reason: str = typer.Option(None, help="required if --validation is set"),
    perturb: bool = typer.Option(False, "--perturb", help="also run a +-20% parameter perturbation check"),
) -> None:
    """Run a strategy through the event-driven backtester and register the
    run + trades in the experiment journal (Phase 2/3/4/5)."""
    settings = get_settings()
    universe = load_universe(settings.universe_path)
    if strategy not in STRATEGY_FACTORIES:
        typer.echo(f"unknown strategy {strategy!r}, choices: {list(STRATEGY_FACTORIES)}", err=True)
        raise typer.Exit(1)
    seed = seed if seed is not None else settings.random_seed

    symbols = sorted(universe.tradable_symbols())
    bars_df = fetch_bars(symbols, start=start, end=end, interval=interval)
    bars = bars_to_domain(bars_df)
    news_items = _fetch_scored_news(universe) if strategy in NEWS_DRIVEN_STRATEGIES else []

    strategy_obj = STRATEGY_FACTORIES[strategy](universe, seed)
    risk_gate = RiskGate(settings.risk)
    engine = BacktestEngine(strategy_obj, universe, risk_gate, initial_equity=equity)
    result = engine.run(bars, news_items)

    config = {"start": start, "end": end, "interval": interval, "equity": equity, "symbols": symbols}
    with get_session(settings) as session:
        run = register_run(
            session, mode=RunMode.BACKTEST, strategy_name=strategy, config=config, random_seed=seed,
            period_start=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
            period_end=datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
            is_validation_run=validation, validation_access_reason=validation_reason,
            git_hash=current_git_hash(),
        )
        record_metrics(session, run, {
            "total_return_pct": result.metrics.total_return_pct,
            "max_drawdown_pct": result.metrics.max_drawdown_pct,
            "sharpe": result.metrics.sharpe,
            "win_rate": result.metrics.win_rate_pct,
            "profit_factor": result.metrics.profit_factor,
            "num_trades": result.metrics.num_trades,
            "avg_holding_hours": result.metrics.avg_holding_hours,
            "exposure_pct": result.metrics.exposure_pct,
        })
        for trade in result.closed_trades:
            record_trade(
                session, run_id=run.id, timestamp=trade.exit_time, symbol=trade.symbol,
                side=TradeSide.SELL, quantity=trade.quantity, price=trade.exit_price,
                strategy_id=trade.strategy_id, realized_pnl=trade.realized_pnl,
                exit_reason=trade.exit_reason,
            )
        run_id = run.id

    typer.echo(f"run {run_id} ({strategy}): return={result.metrics.total_return_pct:.2f}% "
               f"dd={result.metrics.max_drawdown_pct:.2f}% sharpe={result.metrics.sharpe:.2f} "
               f"win_rate={result.metrics.win_rate_pct:.1f}% pf={result.metrics.profit_factor:.2f} "
               f"trades={result.metrics.num_trades} rejected_orders={result.rejected_orders}")
    if result.halt_events:
        typer.echo(f"halts triggered: {result.halt_events}")

    if perturb:
        base_params = _default_params(strategy, universe, seed)
        if not base_params:
            typer.echo("no numeric params to perturb for this strategy")
        else:
            report = run_perturbation_analysis(
                strategy_factory=lambda **kw: STRATEGY_FACTORIES[strategy](universe, seed),
                base_params=base_params, bars=bars, news=news_items, universe=universe,
                risk_gate_factory=lambda: RiskGate(settings.risk),
            )
            typer.echo(f"perturbation base sharpe={report.base_metric:.2f} fragile={report.any_fragile}")
            for r in report.results:
                flag = "FRAGILE" if r.fragile else "ok"
                typer.echo(f"  {r.param_name}: {r.base_value:.4g} -> {r.perturbed_value:.4g} "
                           f"sharpe {r.base_metric:.2f} -> {r.perturbed_metric:.2f} [{flag}]")


def _default_params(strategy: str, universe, seed: int) -> dict:
    if strategy == "random_entry":
        return {"entry_probability_per_bar": 0.02, "exit_after_bars": 8}
    if strategy == "dumb_news":
        return {"sentiment_threshold": 0.5, "exit_after_hours": 4.0}
    if strategy == "overnight_gap":
        return {"sentiment_threshold": 0.5, "exit_after_hours": 3.0}
    return {}


@app.command()
def report(
    limit: int = typer.Option(10, help="most recent runs to show"),
) -> None:
    """Print the most recent experiment runs (config, git hash, metrics)."""
    from sqlmodel import select

    from engine.journal.models import ExperimentRun

    settings = get_settings()
    with get_session(settings) as session:
        runs = session.exec(select(ExperimentRun).order_by(ExperimentRun.created_at.desc()).limit(limit)).all()
    if not runs:
        typer.echo("no runs registered yet")
        return
    for run in runs:
        typer.echo(
            f"{run.created_at.isoformat()}  {run.id[:8]}  {run.mode.value:11s} {run.strategy_name:16s} "
            f"return={run.total_return_pct or 0:.2f}% sharpe={run.sharpe or 0:.2f} "
            f"trades={run.num_trades or 0} git={run.git_hash[:8]} validation={run.is_validation_run}"
        )


@app.command()
def reconcile(
    run_id: str = typer.Option(..., help="backtest run this week's paper trading is compared against"),
    week_start: str = typer.Option(..., help="YYYY-MM-DD"),
    week_end: str = typer.Option(..., help="YYYY-MM-DD"),
    backtest_expected_pct: float = typer.Option(...),
    realized_pct: float = typer.Option(...),
    tolerance_pct: float = typer.Option(2.0),
) -> None:
    """Weekly paper-vs-backtest reconciliation (Phase 6). Divergence beyond
    tolerance must be investigated before iterating further."""
    settings = get_settings()
    with get_session(settings) as session:
        rec = record_reconciliation(
            session,
            week_start=datetime.fromisoformat(week_start).replace(tzinfo=timezone.utc),
            week_end=datetime.fromisoformat(week_end).replace(tzinfo=timezone.utc),
            backtest_run_id=run_id,
            backtest_expected_return_pct=backtest_expected_pct,
            realized_return_pct=realized_pct,
            tolerance_pct=tolerance_pct,
        )
    status = "WITHIN TOLERANCE" if rec.within_tolerance else "*** DIVERGENCE -- INVESTIGATE BEFORE ITERATING ***"
    typer.echo(f"divergence={rec.divergence_pct:.2f}% (tolerance={tolerance_pct}%) -- {status}")


@app.command()
def kill() -> None:
    """Engage the kill switch: cancels all open orders and flattens all
    positions. Works locally (flag file) and remotely (HALT=true env var)."""
    settings = get_settings()
    engage_kill_switch(settings)
    alert_kill_switch(settings)
    typer.echo("kill switch ENGAGED (HALT file written). Restart papertrade to flatten via broker reconciliation.")

    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        typer.echo("no broker credentials configured -- flag file is set, nothing to flatten right now.")
        return
    try:
        client = AlpacaPaperClient(settings)
        client.cancel_all_orders()
        client.close_all_positions()
        typer.echo("broker orders canceled and positions flattened.")
    except (AlpacaAuthError, PaperOnlyViolation) as exc:
        typer.echo(f"could not reach broker to flatten immediately: {exc}", err=True)


@app.command(name="kill-reset")
def kill_reset() -> None:
    """Disengage the local kill-switch flag file (does not affect HALT env var)."""
    settings = get_settings()
    disengage_kill_switch(settings)
    typer.echo("kill switch flag file cleared.")


@app.command()
def papertrade(
    poll_seconds: int = typer.Option(60, help="loop interval"),
    max_iterations: int = typer.Option(None, help="stop after N iterations (testing only; omit to run forever)"),
) -> None:
    """Live paper-trading worker loop (Phase 6). Paper-only guard is
    enforced before anything else; the loop reconciles broker state on
    startup and checks the kill switch every iteration."""
    settings = get_settings()
    try:
        enforce_paper_only(settings)
    except PaperOnlyViolation as exc:
        logger.error("paper-only guard tripped -- refusing to start", extra={"extra_fields": {"error": str(exc)}})
        raise typer.Exit(1)

    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        typer.echo("ALPACA_API_KEY / ALPACA_API_SECRET not set -- cannot paper trade. Exiting.", err=True)
        raise typer.Exit(1)

    client = AlpacaPaperClient(settings)
    risk_gate = RiskGate(settings.risk)

    account = reconcile_account_state(client)
    cancel_stale_orders(client)
    risk_gate.start_new_session(account)
    logger.info("papertrade worker started", extra={"extra_fields": {"equity": account.equity}})
    alert_service_restart(settings)

    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        if is_kill_switch_engaged(settings):
            logger.warning("kill switch engaged -- flattening and halting")
            risk_gate.trigger_kill_switch(account, reason="kill switch engaged")
            alert_kill_switch(settings)
            client.cancel_all_orders()
            client.close_all_positions()
            break

        try:
            account = reconcile_account_state(client)
            if risk_gate.check_daily_drawdown(account):
                logger.error("daily drawdown breached -- flattening", extra={"extra_fields": {"reason": account.halt_reason}})
                alert_risk_halt(settings, account.halt_reason)
                client.cancel_all_orders()
                client.close_all_positions()
                break
            # Signal generation / order submission wiring is intentionally
            # not implemented here yet: this loop is the crash-safe skeleton
            # (reconcile -> check halt/kill -> [strategy hook] -> sleep)
            # required before any live capital-adjacent behavior ships. See
            # JOURNAL.md for why strategy wiring waits on the 3-month paper
            # track record described in SPEC.md Phase 6.
        except Exception:
            logger.exception("unhandled exception in papertrade loop -- failing flat")
            try:
                client.cancel_all_orders()
                client.close_all_positions()
            except Exception:
                logger.exception("failed to flatten after unhandled exception")
            break

        iteration += 1
        if max_iterations is not None and iteration >= max_iterations:
            break
        time.sleep(poll_seconds)

    logger.info("papertrade worker stopped")


if __name__ == "__main__":
    app()
