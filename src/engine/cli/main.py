"""SPEC.md cli/ commands: ingest, backtest, papertrade, report, kill (+ replay
for the Phase 1 demo). One typer app, thin wrappers around the real modules
-- no business logic lives here."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import typer

from engine.anticipatory.pipeline import discover_hypotheses, revise_open_hypotheses
from engine.anticipatory.trading import act_on_hypothesis_beliefs, flatten_resolved_hypotheses
from engine.backtest.engine import BacktestEngine
from engine.backtest.perturbation import run_perturbation_analysis
from engine.config.guard import PaperOnlyViolation, enforce_paper_only
from engine.config.settings import get_settings
from engine.data.alpaca_news import AlpacaNewsAuthError, fetch_alpaca_news
from engine.data.bars import bars_to_domain, fetch_bars
from engine.data.events import EventType, build_event_stream
from engine.data.news import RSS_FEEDS, active_rss_source, fetch_all_rss, fetch_rss_feed
from engine.data.router import tag_and_route
from engine.data.snapshot import create_snapshot
from engine.data.universe import load_universe
from engine.execution.alpaca import AlpacaAuthError, AlpacaPaperClient
from engine.execution.live_loop import LiveLoopState, run_live_cycle, seed_bar_history
from engine.execution.reconcile import cancel_stale_orders, reconcile_account_state, refresh_account_state
from engine.features.sentiment import score_news_item
from engine.journal.db import get_session
from engine.journal.models import RunMode, TradeSide
from engine.journal.registry import (
    current_git_hash,
    get_anticipatory_loop_config,
    get_predict_loop_config,
    headline_already_predicted,
    headline_near_duplicate,
    load_news_items,
    load_off_universe_symbol_stats,
    load_open_hypotheses,
    get_risk_gate_config,
    load_prediction_trades,
    mark_anticipatory_loop_cycle,
    mark_predict_loop_cycle,
    record_halt,
    record_metrics,
    record_news_item,
    record_reconciliation,
    record_trade,
    register_run,
    update_anticipatory_loop_config,
    update_predict_loop_config,
)
from engine.logging_setup import configure_logging, get_logger
from engine.observability import alert_kill_switch, alert_risk_halt, alert_service_restart
from engine.prediction.client import PredictionConfigError
from engine.prediction.factory import build_prediction_client
from engine.prediction.pipeline import resolve_pending_predictions, run_prediction_for_news_item
from engine.prediction.trading import act_on_pending_predictions, close_expired_prediction_trades
from engine.risk.gate import RiskGate
from engine.risk.kill_switch import disengage_kill_switch, engage_kill_switch, is_kill_switch_engaged
from engine.risk.resolve import resolve_risk_limits
from engine.strategy.baselines import BuyAndHoldStrategy, RandomEntryStrategy
from engine.strategy.dumb_news import DumbNewsStrategy
from engine.strategy.overnight_gap import OvernightGapStrategy
from engine.strategy.technical import MeanReversionStrategy, MomentumStrategy, MultiFactorStrategy

app = typer.Typer(add_completion=False, help="News-driven trading research engine (paper trading only).")
logger = get_logger("engine.cli")

STRATEGY_FACTORIES = {
    "buy_and_hold": lambda universe, seed: BuyAndHoldStrategy(symbols=sorted(universe.tradable_symbols())),
    "random_entry": lambda universe, seed: RandomEntryStrategy(symbols=sorted(universe.tradable_symbols()), seed=seed),
    "dumb_news": lambda universe, seed: DumbNewsStrategy(),
    "overnight_gap": lambda universe, seed: OvernightGapStrategy(universe),
    "momentum": lambda universe, seed: MomentumStrategy(symbols=sorted(universe.tradable_symbols())),
    "mean_reversion": lambda universe, seed: MeanReversionStrategy(symbols=sorted(universe.tradable_symbols())),
    "multi_factor": lambda universe, seed: MultiFactorStrategy(symbols=sorted(universe.tradable_symbols())),
}
NEWS_DRIVEN_STRATEGIES = {"dumb_news", "overnight_gap"}

# buy_and_hold/random_entry are explicitly documented reference benchmarks
# ("never a candidate for live trading" -- see engine/strategy/baselines.py)
# -- backtest-only by design, not eligible for `papertrade --strategy`.
LIVE_ELIGIBLE_STRATEGIES = {"dumb_news", "overnight_gap", "momentum", "mean_reversion", "multi_factor"}

def _int_cast(kw: dict, *keys: str) -> dict:
    return {k: (int(v) if k in keys else v) for k, v in kw.items()}


# Separate from STRATEGY_FACTORIES: these actually forward perturbed
# parameter values into the strategy constructor (--perturb varies one
# param at a time and needs the perturbed value to reach the strategy, not
# just rebuild the same default-param strategy every iteration).
STRATEGY_PERTURBATION_FACTORIES = {
    "random_entry": lambda universe, seed, **kw: RandomEntryStrategy(
        symbols=sorted(universe.tradable_symbols()), seed=seed, **_int_cast(kw, "exit_after_bars"),
    ),
    "dumb_news": lambda universe, seed, **kw: DumbNewsStrategy(**kw),
    "overnight_gap": lambda universe, seed, **kw: OvernightGapStrategy(universe, **kw),
    "momentum": lambda universe, seed, **kw: MomentumStrategy(
        symbols=sorted(universe.tradable_symbols()), **_int_cast(kw, "exit_after_bars"),
    ),
    "mean_reversion": lambda universe, seed, **kw: MeanReversionStrategy(symbols=sorted(universe.tradable_symbols()), **kw),
    "multi_factor": lambda universe, seed, **kw: MultiFactorStrategy(
        symbols=sorted(universe.tradable_symbols()), **_int_cast(kw, "exit_after_bars"),
    ),
}


def _fetch_scored_news(universe) -> list:
    items = []
    for raw in fetch_all_rss():
        tagged = tag_and_route(raw, universe)
        items.append(score_news_item(tagged))
    return items


def _backfill_alpaca_news(universe, settings, start_dt, end_dt) -> list:
    """Fetch real historical news for [start_dt, end_dt] from Alpaca, tag +
    score it, and persist it to the journal DB so a repeat backtest over the
    same window hits the DB cache (load_news_items) instead of re-fetching."""
    raw_items = fetch_alpaca_news(start_dt, end_dt, settings, symbols=sorted(universe.tradable_symbols()))
    items = []
    with get_session(settings) as session:
        for raw in raw_items:
            tagged = tag_and_route(raw, universe)
            scored = score_news_item(tagged)
            record_news_item(
                session,
                source=scored.source,
                published_at=scored.published_at,
                headline=scored.headline,
                raw_payload=scored.raw_payload,
                url=scored.url,
                routed_symbols=list(scored.routed_symbols),
                sentiment_score=scored.sentiment_score,
                ingested_at=scored.ingested_at,
            )
            items.append(scored)
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
            settings=settings,
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

    news_items = []
    if strategy in NEWS_DRIVEN_STRATEGIES:
        start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
        end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
        with get_session(settings) as session:
            news_items = load_news_items(session, start_dt, end_dt)
        if not news_items and settings.alpaca_api_key:
            try:
                news_items = _backfill_alpaca_news(universe, settings, start_dt, end_dt)
                typer.echo(f"backfilled {len(news_items)} historical news items from Alpaca for {start}..{end}")
            except AlpacaNewsAuthError as exc:
                typer.echo(f"alpaca news backfill failed ({exc}), falling back to today's live RSS", err=True)
                news_items = _fetch_scored_news(universe)
        elif not news_items:
            typer.echo(
                f"no stored news for {start}..{end} -- free RSS feeds have no historical archive, "
                f"so this range only has news if `engine ingest` was run while it was current, and "
                f"no ALPACA_API_KEY is set to backfill real historical news instead. "
                f"Falling back to today's live RSS feed, which will NOT match this backtest window "
                f"and will likely produce zero trades. Set ALPACA_API_KEY/ALPACA_API_SECRET, or run "
                f"`engine ingest` regularly, to build a real historical corpus. See JOURNAL.md.",
                err=True,
            )
            news_items = _fetch_scored_news(universe)

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
            perturbation_factory = STRATEGY_PERTURBATION_FACTORIES[strategy]
            report = run_perturbation_analysis(
                strategy_factory=lambda **kw: perturbation_factory(universe, seed, **kw),
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
    if strategy == "momentum":
        return {"entry_threshold_pct": 2.0, "exit_after_bars": 10}
    if strategy == "mean_reversion":
        return {"entry_zscore": 1.5, "exit_zscore": 0.3}
    if strategy == "multi_factor":
        return {"max_volatility_pct": 5.0, "exit_after_bars": 10}
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


@app.command(name="predict-news")
def predict_news(
    limit: int = typer.Option(10, help="max headlines to analyze this run (each is a paid LLM call)"),
) -> None:
    """Consequence-prediction forward-test: for each current headline, ask
    the LLM to identify indirect impacts on the tracked universe and log
    the prediction *before* any outcome is known. Never scored at write
    time -- see `engine resolve-predictions`. Requires ANTHROPIC_API_KEY and
    ANTHROPIC_MODEL_KNOWLEDGE_CUTOFF (see engine/prediction/client.py)."""
    settings = get_settings()
    try:
        client = build_prediction_client(settings)
    except PredictionConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    universe = load_universe(settings.universe_path)
    raw_items = fetch_all_rss()[:limit]
    total_predictions = 0
    with get_session(settings) as session:
        for raw in raw_items:
            tagged = tag_and_route(raw, universe)
            predictions = run_prediction_for_news_item(
                session, client, tagged, universe,
                resolution_window_hours=settings.prediction_resolution_hours,
                retrieval_limit=settings.prediction_retrieval_limit,
            )
            total_predictions += len(predictions)
            for p in predictions:
                typer.echo(f"  {p.symbol:6s} {p.direction.value:5s} conf={p.confidence:.2f} "
                           f"forward_safe={p.forward_safe} -- {tagged.headline[:70]}")
    typer.echo(f"analyzed {len(raw_items)} headlines, logged {total_predictions} predictions")


@app.command(name="act-on-predictions")
def act_on_predictions_cmd(
    min_confidence: float = typer.Option(None, help="override PREDICTION_ACTION_CONFIDENCE_THRESHOLD for this run"),
) -> None:
    """Submit a real paper order for every actionable prediction (pending,
    forward_safe, confident enough, not yet traded) -- "up" goes long,
    "down" goes short, both through RiskGate like every other order path.
    Predictions below the confidence threshold are left log-only. Requires
    ALPACA_API_KEY/ALPACA_API_SECRET; refuses to run against anything but
    the paper endpoint (engine.config.guard)."""
    settings = get_settings()
    try:
        enforce_paper_only(settings)
    except PaperOnlyViolation as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        typer.echo("ALPACA_API_KEY / ALPACA_API_SECRET not set -- cannot trade predictions.", err=True)
        raise typer.Exit(1)

    threshold = min_confidence if min_confidence is not None else settings.prediction_action_confidence_threshold
    universe = load_universe(settings.universe_path)
    client = AlpacaPaperClient(settings)
    with get_session(settings) as session:
        risk_gate = RiskGate(resolve_risk_limits(settings, get_risk_gate_config(session)))
    account = reconcile_account_state(client)
    risk_gate.start_new_session(account)

    with get_session(settings) as session:
        acted = act_on_pending_predictions(session, client, risk_gate, account, universe, threshold)
    for p in acted:
        typer.echo(f"  TRADED {p.symbol:6s} {p.direction.value:5s} qty={p.traded_quantity:.4f} conf={p.confidence:.2f} order={p.traded_order_id}")
    typer.echo(f"acted on {len(acted)} predictions (confidence >= {threshold})")


@app.command(name="resolve-predictions")
def resolve_predictions_cmd() -> None:
    """Score every pending prediction whose resolution window has closed,
    against real price data. Never re-scores an already-resolved row. Also
    closes the real paper position for any traded prediction whose window
    has closed, if ALPACA_API_KEY is set -- scoring itself never needs a
    broker connection, only realizing the linked trade's P&L does."""
    settings = get_settings()
    with get_session(settings) as session:
        resolved = resolve_pending_predictions(session)
    for p in resolved:
        outcome = p.status.value if p.status.value == "invalid" else ("correct" if p.outcome_correct else "incorrect")
        excursion = f" mfe={p.mfe_pct:.2f}% mae={p.mae_pct:.2f}%" if p.mfe_pct is not None else ""
        typer.echo(f"  {p.symbol:6s} predicted={p.direction.value:5s} -> {outcome}{excursion}")
    typer.echo(f"resolved {len(resolved)} predictions")

    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        return
    try:
        enforce_paper_only(settings)
    except PaperOnlyViolation as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    universe = load_universe(settings.universe_path)
    client = AlpacaPaperClient(settings)
    with get_session(settings) as session:
        risk_gate = RiskGate(resolve_risk_limits(settings, get_risk_gate_config(session)))
    account = reconcile_account_state(client)
    risk_gate.start_new_session(account)
    with get_session(settings) as session:
        closed = close_expired_prediction_trades(session, client, risk_gate, account, universe)
    for p in closed:
        typer.echo(f"  CLOSED {p.symbol:6s} order={p.exit_order_id}")
    typer.echo(f"closed {len(closed)} prediction trades")


@app.command(name="predictions-report")
def predictions_report(
    forward_safe_only: bool = typer.Option(True, help="only count predictions immune to hindsight leakage"),
) -> None:
    """Accuracy of resolved, forward-safe predictions -- the actual
    evidence of whether the consequence-prediction pipeline has skill."""
    from sqlmodel import select

    from engine.journal.models import Prediction, PredictionStatus

    settings = get_settings()
    with get_session(settings) as session:
        query = select(Prediction).where(Prediction.status == PredictionStatus.RESOLVED)
        if forward_safe_only:
            query = query.where(Prediction.forward_safe == True)  # noqa: E712
        rows = session.exec(query).all()

    if not rows:
        typer.echo("no resolved predictions yet" + (" (forward-safe only)" if forward_safe_only else ""))
        return
    correct = sum(1 for r in rows if r.outcome_correct)
    typer.echo(f"resolved: {len(rows)}  correct: {correct}  accuracy: {correct / len(rows) * 100:.1f}%")

    with_excursion = [r for r in rows if r.mae_pct is not None]
    if with_excursion:
        avg_mae = sum(r.mae_pct for r in with_excursion) / len(with_excursion)
        avg_mfe = sum(r.mfe_pct for r in with_excursion) / len(with_excursion)
        stop_pct = settings.risk.stop_loss_pct * 100.0
        # A prediction can be right at the 24h mark yet have moved past the
        # live stop-loss threshold at some point mid-window -- it would
        # never have survived to collect that "correct" outcome for real.
        would_have_stopped = sum(
            1 for r in with_excursion if r.outcome_correct and r.mae_pct >= stop_pct
        )
        typer.echo(f"avg mfe: {avg_mfe:.2f}%  avg mae: {avg_mae:.2f}%")
        if would_have_stopped:
            typer.echo(
                f"  {would_have_stopped} of {correct} 'correct' predictions moved past the "
                f"{stop_pct:.1f}% stop-loss threshold before recovering -- a live position "
                f"would have been stopped out despite the prediction ultimately being right."
            )

    if not forward_safe_only:
        unsafe = sum(1 for r in rows if not r.forward_safe)
        if unsafe:
            typer.echo(f"WARNING: {unsafe} of these are NOT forward_safe (event predates the model's "
                       f"knowledge cutoff) -- they cannot be trusted as genuine evidence of skill.")


@app.command(name="ticker-suggestions")
def ticker_suggestions(
    min_resolved: int = typer.Option(5, help="resolved forward-safe predictions needed before flagging as strong evidence"),
    min_accuracy_pct: float = typer.Option(65.0, help="accuracy threshold (with min_resolved) to flag as strong evidence"),
) -> None:
    """Every symbol the model has named outside universe.yaml, with
    accumulated evidence of how good that suggestion has been. Purely
    informational -- nothing here ever adds a symbol to the universe
    automatically; adding one to universe.yaml is always a human decision."""
    settings = get_settings()
    with get_session(settings) as session:
        stats = load_off_universe_symbol_stats(session)
    if not stats:
        typer.echo("no off-universe suggestions yet")
        return
    for s in stats:
        acc = f"{s.accuracy_pct:.1f}%" if s.accuracy_pct is not None else "n/a"
        flag = ""
        if s.resolved_count >= min_resolved and s.accuracy_pct is not None and s.accuracy_pct >= min_accuracy_pct:
            flag = "  <-- STRONG EVIDENCE, consider adding to universe.yaml"
        typer.echo(
            f"  {s.symbol:8s} named={s.times_named:3d} resolved={s.resolved_count:3d} "
            f"accuracy={acc:>6s} avg_conf={s.avg_confidence:.2f}{flag}"
        )
        typer.echo(f"      last: \"{s.most_recent_headline[:70]}\" -> {s.most_recent_rationale[:100]}")


@app.command(name="prediction-trades")
def prediction_trades_cmd() -> None:
    """History of every prediction the pipeline actually acted on with a
    real (paper) order -- entry, exit, and outcome once resolved. Most
    recent first. Distinct from predictions-report: most predictions are
    logged and scored but never traded."""
    settings = get_settings()
    with get_session(settings) as session:
        trades = load_prediction_trades(session)
    if not trades:
        typer.echo("no prediction trades yet")
        return
    for t in trades:
        exit_state = "OPEN" if t.exit_order_id is None else "CLOSED"
        if t.status.value == "resolved":
            outcome = "correct" if t.outcome_correct else "incorrect"
        else:
            outcome = t.status.value
        move = f"{t.actual_return_pct:+.2f}%" if t.actual_return_pct is not None else "n/a"
        excursion = f" mfe={t.mfe_pct:.2f}% mae={t.mae_pct:.2f}%" if t.mfe_pct is not None else ""
        typer.echo(
            f"  {t.symbol:6s} {t.direction.value:5s} qty={t.traded_quantity:.4f} conf={t.confidence:.2f} "
            f"[{exit_state}] outcome={outcome} move={move}{excursion}"
        )
        typer.echo(f"      decision={t.news_decision_timestamp.isoformat()}  \"{t.news_headline[:70]}\"")


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
    strategy: str = typer.Option(
        None, envvar="PAPERTRADE_STRATEGY",
        help=f"one of: {', '.join(sorted(LIVE_ELIGIBLE_STRATEGIES))} (omit to run the reconcile/kill-switch "
             f"skeleton only, no trading). Also settable via PAPERTRADE_STRATEGY, so a Railway deployment "
             f"can switch strategies with an env var change instead of a start-command edit.",
    ),
    interval: str = typer.Option("1h", envvar="PAPERTRADE_INTERVAL", help="bar timeframe for the selected strategy"),
    poll_seconds: int = typer.Option(60, help="loop interval"),
    max_iterations: int = typer.Option(None, help="stop after N iterations (testing only; omit to run forever)"),
) -> None:
    """Live paper-trading worker loop. Paper-only guard is enforced before
    anything else; the loop reconciles broker state on startup and checks
    the kill switch and daily-drawdown halt every iteration. With
    `--strategy`, it also polls for new bars/news and trades that strategy
    live through RiskGate -- the same object, same signal semantics, as
    `engine backtest`. Without it, this is the pre-existing crash-safe
    skeleton with no trading at all."""
    settings = get_settings()
    if strategy is not None and strategy not in LIVE_ELIGIBLE_STRATEGIES:
        typer.echo(f"unknown or backtest-only strategy {strategy!r}, choices: {sorted(LIVE_ELIGIBLE_STRATEGIES)}", err=True)
        raise typer.Exit(1)
    try:
        enforce_paper_only(settings)
    except PaperOnlyViolation as exc:
        logger.error("paper-only guard tripped -- refusing to start", extra={"extra_fields": {"error": str(exc)}})
        raise typer.Exit(1)

    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        typer.echo("ALPACA_API_KEY / ALPACA_API_SECRET not set -- cannot paper trade. Exiting.", err=True)
        raise typer.Exit(1)

    client = AlpacaPaperClient(settings)
    with get_session(settings) as session:
        risk_gate = RiskGate(resolve_risk_limits(settings, get_risk_gate_config(session)))

    account = reconcile_account_state(client)
    cancel_stale_orders(client)
    risk_gate.start_new_session(account)
    current_date = datetime.now(timezone.utc).date()

    universe = None
    strategy_obj = None
    live_state = None
    if strategy is not None:
        universe = load_universe(settings.universe_path)
        strategy_obj = STRATEGY_FACTORIES[strategy](universe, settings.random_seed)
        live_state = LiveLoopState()
        seed_bar_history(universe, interval, live_state)

    logger.info(
        "papertrade worker started",
        extra={"extra_fields": {"equity": account.equity, "strategy": strategy, "interval": interval}},
    )
    alert_service_restart(settings)

    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        if is_kill_switch_engaged(settings):
            logger.warning("kill switch engaged -- flattening and halting")
            risk_gate.trigger_kill_switch(account, reason="kill switch engaged")
            alert_kill_switch(settings)
            client.cancel_all_orders()
            client.close_all_positions()
            with get_session(settings) as session:
                record_halt(session, reason="kill switch engaged", account_equity=account.equity, triggered_by="kill_switch")
            break

        try:
            with get_session(settings) as session:
                risk_gate.limits = resolve_risk_limits(settings, get_risk_gate_config(session))

            today = datetime.now(timezone.utc).date()
            if today != current_date:
                # New trading day: this is the only point a fresh
                # equity_at_session_start baseline should be taken -- doing
                # this every iteration would make the daily-drawdown halt
                # permanently see ~0% drawdown, since it'd always be
                # comparing equity to itself. See engine.execution.reconcile.
                account = reconcile_account_state(client)
                risk_gate.start_new_session(account)
                current_date = today
            else:
                refresh_account_state(client, account)
            if risk_gate.check_daily_drawdown(account):
                logger.error("daily drawdown breached -- flattening", extra={"extra_fields": {"reason": account.halt_reason}})
                alert_risk_halt(settings, account.halt_reason)
                client.cancel_all_orders()
                client.close_all_positions()
                with get_session(settings) as session:
                    record_halt(session, reason=account.halt_reason or "daily drawdown breached", account_equity=account.equity, triggered_by="daily_drawdown")
                break

            if strategy_obj is not None:
                summary = run_live_cycle(strategy_obj, universe, risk_gate, client, account, live_state, interval)
                if summary["bars"] or summary["news"]:
                    logger.info("papertrade live cycle", extra={"extra_fields": summary})
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


@app.command(name="predict-loop")
def predict_loop(
    poll_seconds: int = typer.Option(None, help="seed the dashboard-tunable poll interval on first run; defaults to PREDICTION_LOOP_POLL_SECONDS"),
    predict_limit: int = typer.Option(None, help="seed the dashboard-tunable per-source headline quota on first run (each is a paid LLM call)"),
    max_iterations: int = typer.Option(None, help="stop after N iterations (testing only; omit to run forever)"),
) -> None:
    """Automatic version of predict-news + act-on-predictions +
    resolve-predictions: runs all three every cycle, forever, checking the
    kill switch and daily-drawdown halt each cycle exactly like `papertrade`
    does. The individual commands remain available for manual/one-off runs
    -- this is just the default always-on mode.

    Poll interval, rotation length, per-source headline quota,
    near-duplicate detection, and pause/resume are all read fresh from
    PredictLoopConfig (engine.journal.models) every cycle -- live-tunable
    from the dashboard's /predict-loop-config page with no redeploy. The
    poll_seconds/predict_limit CLI options here only seed that config on
    first run; after that the DB row is authoritative, the same way
    is_kill_switch_engaged(settings) is re-checked every cycle rather than
    read once.

    Each cycle pulls headlines from exactly one RSS source -- whichever
    engine.data.news.active_rss_source says is active this hour -- rather
    than all three sources every cycle. Without rotation, yahoo's much
    larger item count structurally starves the other two sources of any of
    the cycle's headline budget.

    Requires ANTHROPIC_API_KEY (+ ANTHROPIC_MODEL_KNOWLEDGE_CUTOFF). If
    ALPACA_API_KEY isn't set, trading/closing is skipped and this becomes a
    log-only forward-test loop (predict + resolve, never act-on/close).

    Unlike papertrade, an unhandled exception inside one cycle is logged
    and the loop continues to the next cycle rather than exiting -- a
    transient RSS/API hiccup shouldn't permanently kill the whole
    automatic mechanism the way it would a continuously-open-position
    guardian process. A daily-drawdown breach or the kill switch still
    stops it and flattens everything, same as papertrade. Pausing from the
    dashboard (PredictLoopConfig.enabled=False) is deliberately much
    gentler than either of those -- it only skips the cycle body; positions
    are left alone and the loop keeps polling so it resumes the instant
    it's re-enabled, no redeploy either way.
    """
    settings = get_settings()
    try:
        client = build_prediction_client(settings)
    except PredictionConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    universe = load_universe(settings.universe_path)
    can_trade = bool(settings.alpaca_api_key and settings.alpaca_api_secret)
    broker = risk_gate = account = current_date = None
    if can_trade:
        try:
            enforce_paper_only(settings)
        except PaperOnlyViolation as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        broker = AlpacaPaperClient(settings)
        with get_session(settings) as session:
            risk_gate = RiskGate(resolve_risk_limits(settings, get_risk_gate_config(session)))
        account = reconcile_account_state(broker)
        risk_gate.start_new_session(account)
        current_date = datetime.now(timezone.utc).date()
    else:
        typer.echo("ALPACA_API_KEY not set -- running log-only (predictions will be scored but never traded).", err=True)

    seed_fields = {}
    if poll_seconds is not None:
        seed_fields["poll_seconds"] = poll_seconds
    elif settings.prediction_loop_poll_seconds:
        seed_fields["poll_seconds"] = settings.prediction_loop_poll_seconds
    if predict_limit is not None:
        seed_fields["headlines_per_source"] = predict_limit
    with get_session(settings) as session:
        if seed_fields:
            config = update_predict_loop_config(session, **seed_fields)
        else:
            config = get_predict_loop_config(session)

    logger.info("predict-loop started", extra={"extra_fields": {"poll_seconds": config.poll_seconds, "can_trade": can_trade}})

    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        if is_kill_switch_engaged(settings):
            logger.warning("kill switch engaged -- stopping predict-loop")
            alert_kill_switch(settings)
            if can_trade:
                risk_gate.trigger_kill_switch(account, reason="kill switch engaged")
                broker.cancel_all_orders()
                broker.close_all_positions()
                with get_session(settings) as session:
                    record_halt(session, reason="kill switch engaged", account_equity=account.equity, triggered_by="kill_switch")
            break

        with get_session(settings) as session:
            config = mark_predict_loop_cycle(session)

        if not config.enabled:
            logger.info("predict-loop paused -- skipping cycle body", extra={"extra_fields": {"poll_seconds": config.poll_seconds}})
            iteration += 1
            if max_iterations is not None and iteration >= max_iterations:
                break
            time.sleep(config.poll_seconds)
            continue

        acted = closed = 0
        try:
            active_source = active_rss_source(config.rotation_anchor, datetime.now(timezone.utc), config.rotation_hours)
            raw_items = fetch_rss_feed(active_source, RSS_FEEDS[active_source])
            predicted = 0
            skipped_duplicate = 0
            skipped_near_duplicate = 0
            fresh_items = []
            with get_session(settings) as session:
                # Filter duplicates across the whole feed before capping to
                # headlines_per_source -- otherwise a quiet-news hour where
                # the top results happen to repeat would waste the cycle's
                # entire budget on nothing, even if genuinely new headlines
                # exist further down the same feed. See
                # headline_already_predicted / headline_near_duplicate.
                for raw in raw_items:
                    if headline_already_predicted(session, raw.headline):
                        skipped_duplicate += 1
                        continue
                    if headline_near_duplicate(
                        session, raw.headline,
                        window_hours=config.near_dup_window_hours, threshold=config.near_dup_threshold,
                    ):
                        skipped_near_duplicate += 1
                        continue
                    fresh_items.append(raw)
                    if len(fresh_items) >= config.headlines_per_source:
                        break

                for raw in fresh_items:
                    tagged = tag_and_route(raw, universe)
                    predicted += len(run_prediction_for_news_item(
                        session, client, tagged, universe,
                        resolution_window_hours=settings.prediction_resolution_hours,
                        retrieval_limit=settings.prediction_retrieval_limit,
                    ))

            if can_trade:
                with get_session(settings) as session:
                    risk_gate.limits = resolve_risk_limits(settings, get_risk_gate_config(session))

                today = datetime.now(timezone.utc).date()
                if today != current_date:
                    account = reconcile_account_state(broker)
                    risk_gate.start_new_session(account)
                    current_date = today
                else:
                    refresh_account_state(broker, account)

                if risk_gate.check_daily_drawdown(account):
                    logger.error(
                        "daily drawdown breached -- flattening and stopping predict-loop",
                        extra={"extra_fields": {"reason": account.halt_reason}},
                    )
                    alert_risk_halt(settings, account.halt_reason)
                    broker.cancel_all_orders()
                    broker.close_all_positions()
                    with get_session(settings) as session:
                        record_halt(session, reason=account.halt_reason or "daily drawdown breached", account_equity=account.equity, triggered_by="daily_drawdown")
                    break

                with get_session(settings) as session:
                    acted = len(act_on_pending_predictions(
                        session, broker, risk_gate, account, universe,
                        settings.prediction_action_confidence_threshold,
                    ))
                with get_session(settings) as session:
                    closed = len(close_expired_prediction_trades(session, broker, risk_gate, account, universe))

            with get_session(settings) as session:
                resolved = len(resolve_pending_predictions(session))

            logger.info(
                "predict-loop cycle complete",
                extra={"extra_fields": {
                    "active_source": active_source,
                    "headlines_fetched": len(raw_items), "headlines_new": len(fresh_items),
                    "skipped_duplicate": skipped_duplicate, "skipped_near_duplicate": skipped_near_duplicate,
                    "predicted": predicted, "acted": acted, "closed": closed, "resolved": resolved,
                }},
            )
        except Exception:
            logger.exception("unhandled exception in predict-loop cycle -- continuing to next cycle")

        iteration += 1
        if max_iterations is not None and iteration >= max_iterations:
            break
        time.sleep(config.poll_seconds)

    logger.info("predict-loop stopped")


@app.command(name="anticipatory-loop")
def anticipatory_loop(
    poll_seconds: int = typer.Option(None, help="seed the dashboard-tunable poll interval on first run"),
    max_iterations: int = typer.Option(None, help="stop after N iterations (testing only; omit to run forever)"),
) -> None:
    """Anticipatory-mode counterpart to predict-loop -- see
    docs/anticipatory_prediction_mode.md. Each cycle: discover new
    Polymarket-anchored hypotheses (one paid LLM relevance call per
    not-yet-tracked candidate market), re-estimate every open hypothesis's
    probability fresh, trade the gap against Polymarket's price through
    the same RiskGate as everywhere else, and close out any hypothesis
    Polymarket itself now reports resolved.

    Poll interval, minimum gap threshold, max concurrent hypotheses, and
    pause/resume are read fresh from AnticipatoryLoopConfig every cycle --
    live-tunable from the dashboard with no redeploy, same mechanism as
    PredictLoopConfig (see predict_loop's docstring for why this isn't
    engine.config.settings.Settings).

    Requires ANTHROPIC_API_KEY (+ ANTHROPIC_MODEL_KNOWLEDGE_CUTOFF), same
    as predict-loop. If ALPACA_API_KEY isn't set, hypotheses are still
    discovered and revised but never traded (log-only forward test).

    Same crash-safety split as predict-loop: an unhandled exception inside
    one cycle is logged and the loop continues to the next cycle; the
    kill switch or a daily-drawdown breach stops it and flattens
    everything, including any open anticipatory positions.
    """
    settings = get_settings()
    try:
        client = build_prediction_client(settings)
    except PredictionConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    universe = load_universe(settings.universe_path)
    can_trade = bool(settings.alpaca_api_key and settings.alpaca_api_secret)
    broker = risk_gate = account = current_date = None
    if can_trade:
        try:
            enforce_paper_only(settings)
        except PaperOnlyViolation as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        broker = AlpacaPaperClient(settings)
        with get_session(settings) as session:
            risk_gate = RiskGate(resolve_risk_limits(settings, get_risk_gate_config(session)))
        account = reconcile_account_state(broker)
        risk_gate.start_new_session(account)
        current_date = datetime.now(timezone.utc).date()
    else:
        typer.echo("ALPACA_API_KEY not set -- running log-only (hypotheses will be scored but never traded).", err=True)

    seed_fields = {}
    if poll_seconds is not None:
        seed_fields["poll_seconds"] = poll_seconds
    with get_session(settings) as session:
        if seed_fields:
            config = update_anticipatory_loop_config(session, **seed_fields)
        else:
            config = get_anticipatory_loop_config(session)

    logger.info("anticipatory-loop started", extra={"extra_fields": {"poll_seconds": config.poll_seconds, "can_trade": can_trade}})

    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        if is_kill_switch_engaged(settings):
            logger.warning("kill switch engaged -- stopping anticipatory-loop")
            alert_kill_switch(settings)
            if can_trade:
                risk_gate.trigger_kill_switch(account, reason="kill switch engaged")
                broker.cancel_all_orders()
                broker.close_all_positions()
                with get_session(settings) as session:
                    record_halt(session, reason="kill switch engaged", account_equity=account.equity, triggered_by="kill_switch")
            break

        with get_session(settings) as session:
            config = mark_anticipatory_loop_cycle(session)

        if not config.enabled:
            logger.info("anticipatory-loop paused -- skipping cycle body", extra={"extra_fields": {"poll_seconds": config.poll_seconds}})
            iteration += 1
            if max_iterations is not None and iteration >= max_iterations:
                break
            time.sleep(config.poll_seconds)
            continue

        discovered = acted_open = acted_closed = flattened = 0
        beliefs, resolved = [], []
        try:
            with get_session(settings) as session:
                discovered = len(discover_hypotheses(
                    session, client,
                    discovery_limit=config.discovery_limit,
                    max_open_hypotheses=config.max_open_hypotheses,
                    min_gap_threshold=config.min_gap_threshold,
                    max_open_hypotheses_per_symbol=config.max_open_hypotheses_per_symbol,
                ))

            with get_session(settings) as session:
                open_before = {h.id: h for h in load_open_hypotheses(session)}
                beliefs, resolved = revise_open_hypotheses(session, client, min_gap_threshold=config.min_gap_threshold)

            if can_trade:
                with get_session(settings) as session:
                    risk_gate.limits = resolve_risk_limits(settings, get_risk_gate_config(session))

                today = datetime.now(timezone.utc).date()
                if today != current_date:
                    account = reconcile_account_state(broker)
                    risk_gate.start_new_session(account)
                    current_date = today
                else:
                    refresh_account_state(broker, account)

                if risk_gate.check_daily_drawdown(account):
                    logger.error(
                        "daily drawdown breached -- flattening and stopping anticipatory-loop",
                        extra={"extra_fields": {"reason": account.halt_reason}},
                    )
                    alert_risk_halt(settings, account.halt_reason)
                    broker.cancel_all_orders()
                    broker.close_all_positions()
                    with get_session(settings) as session:
                        record_halt(session, reason=account.halt_reason or "daily drawdown breached", account_equity=account.equity, triggered_by="daily_drawdown")
                    break

                with get_session(settings) as session:
                    acted_open, acted_closed = act_on_hypothesis_beliefs(
                        session, broker, risk_gate, account, universe, open_before, beliefs, config.min_gap_threshold,
                    )
                with get_session(settings) as session:
                    flattened = flatten_resolved_hypotheses(session, broker, risk_gate, account, universe, resolved)

            logger.info(
                "anticipatory-loop cycle complete",
                extra={"extra_fields": {
                    "discovered": discovered, "revised": len(beliefs), "resolved": len(resolved),
                    "opened": acted_open, "closed": acted_closed, "flattened": flattened,
                }},
            )
        except Exception:
            logger.exception("unhandled exception in anticipatory-loop cycle -- continuing to next cycle")

        iteration += 1
        if max_iterations is not None and iteration >= max_iterations:
            break
        time.sleep(config.poll_seconds)

    logger.info("anticipatory-loop stopped")


if __name__ == "__main__":
    app()
