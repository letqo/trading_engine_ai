"""The experiment journal's write API. Every backtest/live run, trade, halt
event, and reconciliation report goes through here so the persistence
format is defined in one place."""

from __future__ import annotations

import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from rapidfuzz import fuzz
from sqlmodel import Session, select

from engine.domain import NewsItem
from engine.journal.models import (
    AnticipatoryLoopConfig,
    DataSnapshot,
    ExperimentRun,
    Hypothesis,
    HypothesisAction,
    HypothesisBelief,
    HypothesisStatus,
    NewsItemRecord,
    PredictLoopConfig,
    Prediction,
    PredictionDirection,
    PredictionStatus,
    PredictionTopic,
    ReconciliationReport,
    RiskGateConfig,
    RiskHaltEvent,
    RunMode,
    TradeRecord,
    TradeSide,
)


def current_git_hash() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def register_run(
    session: Session,
    *,
    mode: RunMode,
    strategy_name: str,
    config: dict,
    random_seed: int,
    data_snapshot_id: str | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    is_validation_run: bool = False,
    validation_access_reason: str | None = None,
    git_hash: str | None = None,
) -> ExperimentRun:
    if is_validation_run and not validation_access_reason:
        raise ValueError(
            "touching the validation set requires a logged reason "
            "(anti-self-deception protocol: log every validation-set access)"
        )
    run = ExperimentRun(
        mode=mode,
        strategy_name=strategy_name,
        config_json=config,
        git_hash=git_hash or current_git_hash(),
        data_snapshot_id=data_snapshot_id,
        random_seed=random_seed,
        period_start=period_start,
        period_end=period_end,
        is_validation_run=is_validation_run,
        validation_access_reason=validation_access_reason,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def record_metrics(session: Session, run: ExperimentRun, metrics: dict) -> ExperimentRun:
    for key, value in metrics.items():
        if hasattr(run, key):
            setattr(run, key, value)
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def record_trade(
    session: Session,
    *,
    run_id: str,
    timestamp: datetime,
    symbol: str,
    side: TradeSide,
    quantity: float,
    price: float,
    strategy_id: str,
    fees: float = 0.0,
    slippage: float = 0.0,
    broker_order_id: str | None = None,
    realized_pnl: float | None = None,
    exit_reason: str | None = None,
) -> TradeRecord:
    trade = TradeRecord(
        run_id=run_id,
        timestamp=timestamp,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        fees=fees,
        slippage=slippage,
        strategy_id=strategy_id,
        broker_order_id=broker_order_id,
        realized_pnl=realized_pnl,
        exit_reason=exit_reason,
    )
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return trade


def record_halt(
    session: Session,
    *,
    reason: str,
    account_equity: float,
    triggered_by: str,
    run_id: str | None = None,
) -> RiskHaltEvent:
    event = RiskHaltEvent(
        reason=reason, account_equity=account_equity, triggered_by=triggered_by, run_id=run_id
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def record_news_item(
    session: Session,
    *,
    source: str,
    published_at: datetime,
    headline: str,
    raw_payload: dict,
    url: str | None = None,
    routed_symbols: list[str] | None = None,
    sentiment_score: float | None = None,
    sentiment_model: str | None = None,
    ingested_at: datetime | None = None,
) -> NewsItemRecord:
    """`ingested_at` defaults to the row's own default (now), which is
    correct for live RSS ingestion -- the process really is seeing the item
    right now. Backfilled historical news (engine.data.alpaca_news) must
    pass an explicit `ingested_at`; otherwise every backfilled row would get
    stamped with today's date regardless of how long ago it was published,
    pushing NewsItem.decision_timestamp past the entire backtest window and
    silently making the backfill useless for backtesting.
    """
    kwargs = {} if ingested_at is None else {"ingested_at": ingested_at}
    item = NewsItemRecord(
        source=source,
        published_at=published_at,
        headline=headline,
        raw_payload=raw_payload,
        url=url,
        routed_symbols=routed_symbols or [],
        sentiment_score=sentiment_score,
        sentiment_model=sentiment_model,
        **kwargs,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def register_snapshot(
    session: Session,
    *,
    description: str,
    universe_hash: str,
    bar_start: datetime | None = None,
    bar_end: datetime | None = None,
    news_count: int = 0,
    bar_row_count: int = 0,
) -> DataSnapshot:
    snapshot = DataSnapshot(
        description=description,
        universe_hash=universe_hash,
        bar_start=bar_start,
        bar_end=bar_end,
        news_count=news_count,
        bar_row_count=bar_row_count,
    )
    session.add(snapshot)
    session.commit()
    session.refresh(snapshot)
    return snapshot


def record_reconciliation(
    session: Session,
    *,
    week_start: datetime,
    week_end: datetime,
    backtest_run_id: str,
    backtest_expected_return_pct: float,
    realized_return_pct: float,
    tolerance_pct: float,
    notes: str | None = None,
) -> ReconciliationReport:
    divergence_pct = abs(realized_return_pct - backtest_expected_return_pct)
    report = ReconciliationReport(
        week_start=week_start,
        week_end=week_end,
        backtest_run_id=backtest_run_id,
        backtest_expected_return_pct=backtest_expected_return_pct,
        realized_return_pct=realized_return_pct,
        divergence_pct=divergence_pct,
        tolerance_pct=tolerance_pct,
        within_tolerance=divergence_pct <= tolerance_pct,
        notes=notes,
    )
    session.add(report)
    session.commit()
    session.refresh(report)
    return report


def _as_utc(dt: datetime) -> datetime:
    """SQLite (used for local dev/backtests) silently drops tzinfo on
    datetime round-trip through SQLAlchemy, regardless of the column's
    declared type -- every datetime this codebase writes is UTC by
    convention (see engine.data.alpaca_news, engine.data.bars), so a naive
    read is always UTC, never local time. Re-attaching tzinfo here is what
    keeps a DB-loaded NewsItem comparable to tz-aware Bar timestamps in
    build_event_stream; without it, backtests that reuse an already-cached
    news range (anything after the first strategy to warm the cache) raise
    "can't compare offset-naive and offset-aware datetimes"."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_news_items(session: Session, start: datetime, end: datetime) -> list[NewsItem]:
    """Read back previously-ingested news for a date range (published_at
    within [start, end]) as domain NewsItem objects, for backtesting over a
    historical window.

    This matters because free RSS feeds have no historical archive -- they
    only ever expose currently-live items. `engine ingest` is what builds
    up a real historical news corpus over time (each run persists whatever
    RSS currently shows, with real published/ingested timestamps, via
    record_news_item). A backtest over a past window must read that stored
    corpus rather than re-fetching live RSS, which would return today's
    headlines regardless of the requested date range and silently produce
    an empty/misleading backtest. See JOURNAL.md.
    """
    rows = session.exec(
        select(NewsItemRecord)
        .where(NewsItemRecord.published_at >= start, NewsItemRecord.published_at <= end)
        .order_by(NewsItemRecord.published_at)
    ).all()
    return [
        NewsItem(
            id=row.id,
            source=row.source,
            published_at=_as_utc(row.published_at),
            ingested_at=_as_utc(row.ingested_at),
            headline=row.headline,
            url=row.url,
            raw_payload=row.raw_payload,
            routed_symbols=tuple(row.routed_symbols),
            sentiment_score=row.sentiment_score,
        )
        for row in rows
    ]


def headline_already_predicted(session: Session, headline: str) -> bool:
    """predict-loop calls this before spending a Claude call on a headline --
    live RSS feeds (engine.data.news.fetch_all_rss) have no memory of their
    own, so the exact same "top stories" can and do reappear across
    consecutive hourly cycles on a quiet news day. Without this check,
    predict-loop would silently re-pay for (and re-log a duplicate
    Prediction row for) the same headline every cycle it stays on the feed --
    wasted subscription usage, and a false sense of sample size in
    predictions-report from correlated, non-independent duplicates of the
    same event. See JOURNAL.md 2026-07-21."""
    return (
        session.exec(select(Prediction.id).where(Prediction.news_headline == headline).limit(1)).first()
        is not None
    )


def headline_near_duplicate(session: Session, headline: str, *, window_hours: float, threshold: float) -> bool:
    """Catches the same real-world event covered by a *different* outlet
    with different wording -- headline_already_predicted only catches exact
    text matches, so it misses this. Compared against Prediction.created_at
    (when we logged it), not news_published_at (each source's own publish
    skew) -- the question is "did we already spend a call on something like
    this recently," not when the source claims it went out. Uses
    rapidfuzz.token_set_ratio rather than an LLM call: cheap, deterministic,
    and cost is the whole reason this check exists in the first place."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    recent = session.exec(select(Prediction.news_headline).where(Prediction.created_at >= cutoff)).all()
    return any(fuzz.token_set_ratio(headline, other) >= threshold for other in recent)


def get_predict_loop_config(session: Session) -> PredictLoopConfig:
    """Live-tunable predict-loop config, polled fresh every cycle (see
    engine.cli.main.predict_loop) so the dashboard can change strategy
    without a redeploy. Get-or-create keeps defaults defined in exactly one
    place -- PredictLoopConfig's field defaults -- rather than duplicated
    into a migration seed row."""
    row = session.get(PredictLoopConfig, PredictLoopConfig.SINGLETON_ID)
    if row is None:
        row = PredictLoopConfig(id=PredictLoopConfig.SINGLETON_ID)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def update_predict_loop_config(session: Session, **fields) -> PredictLoopConfig:
    row = get_predict_loop_config(session)
    for key, value in fields.items():
        setattr(row, key, value)
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_risk_gate_config(session: Session) -> RiskGateConfig:
    """Live-tunable RiskGate override, read fresh by every live trading
    path before each RiskGate is built/refreshed -- same get-or-create
    singleton pattern as get_predict_loop_config, see its docstring."""
    row = session.get(RiskGateConfig, RiskGateConfig.SINGLETON_ID)
    if row is None:
        row = RiskGateConfig(id=RiskGateConfig.SINGLETON_ID)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def update_risk_gate_config(session: Session, **fields) -> RiskGateConfig:
    row = get_risk_gate_config(session)
    for key, value in fields.items():
        setattr(row, key, value)
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def mark_predict_loop_cycle(session: Session) -> PredictLoopConfig:
    """Stamp last_cycle_at -- called once per loop iteration, paused or
    not, distinct from updated_at (which only changes when a setting is
    edited). Lets the dashboard tell "the process is alive and iterating"
    apart from "Railway says Online but it's actually crash-looping" (the
    ON_FAILURE restart policy masks that distinction otherwise).

    Returns the refreshed row so callers can use it directly instead of a
    separate get_predict_loop_config call -- commit() here expires all of
    the session's tracked objects' attributes (SQLAlchemy's default), so
    a config fetched *before* this call would raise DetachedInstanceError
    on any attribute access after the session closes, which every loop's
    cycle body does many lines later."""
    row = get_predict_loop_config(session)
    row.last_cycle_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def record_prediction(
    session: Session,
    *,
    news_headline: str,
    news_source: str,
    news_published_at: datetime,
    news_decision_timestamp: datetime,
    topics: list[str],
    symbol: str,
    direction: PredictionDirection,
    confidence: float,
    rationale: str,
    model_name: str,
    model_knowledge_cutoff: datetime,
    forward_safe: bool,
    resolution_window_hours: float,
    in_tracked_universe: bool,
    retrieved_context_ids: list[str] | None = None,
) -> Prediction:
    """Write one prediction row. Called before the outcome is known --
    nothing here ever gets edited except by resolve_prediction(), once, when
    the resolution window closes. See engine.journal.models.Prediction.

    Also writes one PredictionTopic row per topic -- a derived index over
    Prediction.topics (which stays the source of truth) so
    load_resolved_predictions_by_topics can query by topic without loading
    every resolved prediction into Python. See PredictionTopic's docstring."""
    prediction = Prediction(
        news_headline=news_headline,
        news_source=news_source,
        news_published_at=news_published_at,
        news_decision_timestamp=news_decision_timestamp,
        topics=topics,
        symbol=symbol,
        direction=direction,
        confidence=confidence,
        rationale=rationale,
        model_name=model_name,
        model_knowledge_cutoff=model_knowledge_cutoff,
        forward_safe=forward_safe,
        resolution_window_hours=resolution_window_hours,
        in_tracked_universe=in_tracked_universe,
        retrieved_context_ids=retrieved_context_ids or [],
    )
    session.add(prediction)
    session.commit()
    session.refresh(prediction)
    for topic in topics:
        session.add(PredictionTopic(prediction_id=prediction.id, topic=topic))
    session.commit()
    return prediction


def load_resolved_predictions_by_topics(
    session: Session, topics: set[str], limit: int = 5
) -> list[Prediction]:
    """Retrieval for the prediction pipeline's few-shot context: past
    resolved cases sharing at least one topic tag, most recent first --
    plus the single most recent *wrong* call on these topics, if one
    exists and isn't already in the recency window. Pure recency risks
    the model never seeing its own mistakes on a topic (if the last
    `limit` matches all happened to be correct, it never gets the
    corrective example); guaranteeing one miss is in the mix replaces the
    oldest slot rather than adding one, so `limit` still hard-caps how
    many past cases get fed to the LLM per prediction -- no extra token
    cost.

    Queries via the PredictionTopic index (populated by record_prediction)
    rather than loading every resolved Prediction into Python to filter
    Prediction.topics (a JSON blob) in memory -- this call runs on every
    single headline predict-loop analyzes, so an unbounded full-table scan
    here gets slower every day the prediction log grows. Matching by
    Prediction.id.in_(subquery) rather than a JOIN avoids duplicate rows
    when a prediction matches more than one requested topic, so ORDER BY +
    LIMIT stay correct entirely at the DB level, on both SQLite (dev) and
    Postgres (prod) -- no query-syntax divergence between the two.
    """
    if not topics or limit <= 0:
        return []
    matching_ids = select(PredictionTopic.prediction_id).where(PredictionTopic.topic.in_(topics)).distinct()
    base = select(Prediction).where(Prediction.status == PredictionStatus.RESOLVED, Prediction.id.in_(matching_ids))

    recent = list(session.exec(base.order_by(Prediction.resolved_at.desc()).limit(limit)).all())

    if not any(p.outcome_correct is False for p in recent):
        worst = session.exec(
            base.where(Prediction.outcome_correct == False)  # noqa: E712
            .order_by(Prediction.resolved_at.desc())
            .limit(1)
        ).first()
        if worst is not None:
            recent = recent[:-1] + [worst]

    recent.sort(key=lambda p: p.resolved_at, reverse=True)
    return recent


def load_pending_predictions_past_window(session: Session, as_of: datetime) -> list[Prediction]:
    """Predictions whose resolution window has closed but haven't been
    scored yet -- what `engine resolve-predictions` operates on."""
    rows = session.exec(select(Prediction).where(Prediction.status == PredictionStatus.PENDING)).all()
    return [row for row in rows if _hours_elapsed(as_of, row.news_decision_timestamp) >= row.resolution_window_hours]


def load_actionable_predictions(session: Session, min_confidence: float) -> list[Prediction]:
    """Pending, forward-safe, confident-enough predictions not yet traded --
    what `engine act-on-predictions` operates on. forward_safe is required
    here too even though its real purpose is scoring integrity: trading on
    a prediction that might reflect hindsight leakage would be reckless
    regardless of which reason we'd be using it for."""
    rows = session.exec(
        select(Prediction).where(
            Prediction.status == PredictionStatus.PENDING,
            Prediction.forward_safe == True,  # noqa: E712
            Prediction.in_tracked_universe == True,  # noqa: E712 -- only vetted, tradable symbols
            Prediction.confidence >= min_confidence,
            Prediction.traded_order_id.is_(None),
        )
    ).all()
    return list(rows)


def mark_prediction_traded(session: Session, prediction: Prediction, *, order_id: str, quantity: float) -> Prediction:
    prediction.traded_order_id = order_id
    prediction.traded_quantity = quantity
    session.add(prediction)
    session.commit()
    session.refresh(prediction)
    return prediction


def mark_prediction_exited(session: Session, prediction: Prediction, *, order_id: str) -> Prediction:
    prediction.exit_order_id = order_id
    session.add(prediction)
    session.commit()
    session.refresh(prediction)
    return prediction


def load_expired_open_trades(session: Session, as_of: datetime) -> list[Prediction]:
    """Traded predictions whose resolution window has closed but whose
    linked paper position hasn't been closed yet -- what closes real
    exposure back out once the forward-test window ends."""
    rows = session.exec(
        select(Prediction).where(
            Prediction.traded_order_id.is_not(None),
            Prediction.exit_order_id.is_(None),
        )
    ).all()
    return [row for row in rows if _hours_elapsed(as_of, row.news_decision_timestamp) >= row.resolution_window_hours]


def _hours_elapsed(later: datetime, earlier: datetime) -> float:
    # SQLite drops tzinfo on round-trip; treat naive timestamps as UTC so
    # comparisons work identically against SQLite (dev) and Postgres (prod).
    if later.tzinfo is None:
        later = later.replace(tzinfo=timezone.utc)
    if earlier.tzinfo is None:
        earlier = earlier.replace(tzinfo=timezone.utc)
    return (later - earlier).total_seconds() / 3600.0


def resolve_prediction(
    session: Session,
    prediction: Prediction,
    *,
    entry_price: float | None,
    exit_price: float | None,
    resolved_at: datetime,
    mfe_pct: float | None = None,
    mae_pct: float | None = None,
) -> Prediction:
    """Fill in the outcome fields exactly once. If price data wasn't
    available for either side, mark INVALID rather than guessing -- an
    unresolvable prediction must never silently count as a miss.

    mfe_pct/mae_pct are path context alongside the binary outcome, not a
    replacement for it -- direction correctness is still decided purely by
    entry_price vs. exit_price at the window boundary (see
    docs/prediction_pipeline.md on why the horizon is never re-litigated)."""
    if entry_price is None or exit_price is None or entry_price <= 0:
        prediction.status = PredictionStatus.INVALID
        prediction.resolved_at = resolved_at
        session.add(prediction)
        session.commit()
        session.refresh(prediction)
        return prediction

    actual_return_pct = (exit_price - entry_price) / entry_price * 100.0
    actual_direction = PredictionDirection.UP if actual_return_pct >= 0 else PredictionDirection.DOWN

    prediction.status = PredictionStatus.RESOLVED
    prediction.resolved_at = resolved_at
    prediction.entry_price = entry_price
    prediction.exit_price = exit_price
    prediction.actual_return_pct = actual_return_pct
    prediction.outcome_correct = actual_direction == prediction.direction
    prediction.mfe_pct = mfe_pct
    prediction.mae_pct = mae_pct
    session.add(prediction)
    session.commit()
    session.refresh(prediction)
    return prediction


@dataclass(frozen=True)
class OffUniverseSymbolStats:
    """One symbol the model named that isn't in universe.yaml, and
    everything resolved so far about how good that suggestion has been --
    the evidence a human should look at before deciding to add it."""

    symbol: str
    times_named: int
    resolved_count: int
    correct_count: int
    avg_confidence: float
    most_recent_headline: str
    most_recent_rationale: str

    @property
    def accuracy_pct(self) -> float | None:
        return (self.correct_count / self.resolved_count * 100.0) if self.resolved_count else None


def load_off_universe_symbol_stats(session: Session) -> list[OffUniverseSymbolStats]:
    """Aggregate every off-universe prediction by symbol, sorted by
    resolved sample size first (most evidence first) -- a single lucky
    guess with one resolved prediction is not the same kind of evidence as
    ten resolved predictions at 70% accuracy. Only forward_safe rows count
    toward resolved/correct, same integrity rule as everywhere else in this
    pipeline. See `engine ticker-suggestions`."""
    rows = session.exec(
        select(Prediction)
        .where(Prediction.in_tracked_universe == False)  # noqa: E712
        .order_by(Prediction.created_at.desc())
    ).all()

    by_symbol: dict[str, list[Prediction]] = defaultdict(list)
    for row in rows:
        by_symbol[row.symbol].append(row)  # already created_at-desc from the query

    stats = []
    for symbol, preds in by_symbol.items():
        resolved = [p for p in preds if p.status == PredictionStatus.RESOLVED and p.forward_safe]
        correct = sum(1 for p in resolved if p.outcome_correct)
        most_recent = preds[0]
        stats.append(
            OffUniverseSymbolStats(
                symbol=symbol,
                times_named=len(preds),
                resolved_count=len(resolved),
                correct_count=correct,
                avg_confidence=sum(p.confidence for p in preds) / len(preds),
                most_recent_headline=most_recent.news_headline,
                most_recent_rationale=most_recent.rationale,
            )
        )
    stats.sort(key=lambda s: (s.resolved_count, s.times_named), reverse=True)
    return stats


def load_prediction_trades(session: Session) -> list[Prediction]:
    """Every prediction that was actually acted on with a real (paper)
    order, most recent first -- the AI's trade history. Distinct from the
    full prediction log: most predictions are logged and scored but never
    traded (confidence too low, or the symbol isn't tradable)."""
    rows = session.exec(
        select(Prediction)
        .where(Prediction.traded_order_id.is_not(None))
        .order_by(Prediction.news_decision_timestamp.desc())
    ).all()
    return list(rows)


def load_recent_experiment_runs(session: Session, limit: int = 50) -> list[ExperimentRun]:
    """Most recent backtest/live-session runs, newest first -- the
    reproducible experiment registry (engine backtest / engine papertrade)."""
    rows = session.exec(select(ExperimentRun).order_by(ExperimentRun.created_at.desc()).limit(limit)).all()
    return list(rows)


def load_recent_risk_halts(session: Session, limit: int = 100) -> list[RiskHaltEvent]:
    """Most recent risk-gate halts/kill-switch triggers, newest first --
    the audit trail independent of trade outcomes."""
    rows = session.exec(select(RiskHaltEvent).order_by(RiskHaltEvent.timestamp.desc()).limit(limit)).all()
    return list(rows)


def get_anticipatory_loop_config(session: Session) -> AnticipatoryLoopConfig:
    """Live-tunable anticipatory-loop config, polled fresh every cycle (see
    engine.cli.main.anticipatory_loop) -- same get-or-create singleton
    pattern as get_predict_loop_config, see its docstring."""
    row = session.get(AnticipatoryLoopConfig, AnticipatoryLoopConfig.SINGLETON_ID)
    if row is None:
        row = AnticipatoryLoopConfig(id=AnticipatoryLoopConfig.SINGLETON_ID)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def update_anticipatory_loop_config(session: Session, **fields) -> AnticipatoryLoopConfig:
    row = get_anticipatory_loop_config(session)
    for key, value in fields.items():
        setattr(row, key, value)
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def mark_anticipatory_loop_cycle(session: Session) -> AnticipatoryLoopConfig:
    """See mark_predict_loop_cycle's docstring -- same heartbeat purpose
    and same reason it returns the refreshed row instead of None."""
    row = get_anticipatory_loop_config(session)
    row.last_cycle_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def hypothesis_exists_for_market(session: Session, market_id: str) -> bool:
    """True if any Hypothesis (open or closed) already tracks this
    Polymarket market -- discovery's dedup check. A closed hypothesis is
    never re-created even if the same market_id somehow reappears as an
    open candidate (shouldn't happen -- Polymarket markets don't reopen --
    but this is the cheap, unconditional guard either way)."""
    return session.exec(select(Hypothesis.id).where(Hypothesis.market_id == market_id)).first() is not None


def create_hypothesis(
    session: Session, *, market_id: str, question: str, symbol: str, direction_if_yes: PredictionDirection
) -> Hypothesis:
    hyp = Hypothesis(market_id=market_id, question=question, symbol=symbol, direction_if_yes=direction_if_yes)
    session.add(hyp)
    session.commit()
    session.refresh(hyp)
    return hyp


def load_open_hypotheses(session: Session) -> list[Hypothesis]:
    rows = session.exec(select(Hypothesis).where(Hypothesis.status == HypothesisStatus.OPEN)).all()
    return list(rows)


def record_hypothesis_belief(
    session: Session,
    hypothesis: Hypothesis,
    *,
    p_model: float,
    p_market: float,
    confidence: float,
    rationale: str,
    action: HypothesisAction,
    order_id: str | None = None,
    order_quantity: float | None = None,
) -> HypothesisBelief:
    """Append-only -- never edit a HypothesisBelief row after writing it,
    same anti-hindsight principle as Prediction (see the model's
    docstring)."""
    belief = HypothesisBelief(
        hypothesis_id=hypothesis.id,
        p_model=p_model,
        p_market=p_market,
        gap=p_model - p_market,
        confidence=confidence,
        rationale=rationale,
        action=action,
        order_id=order_id,
        order_quantity=order_quantity,
    )
    session.add(belief)
    session.commit()
    session.refresh(belief)
    return belief


def mark_hypothesis_traded(
    session: Session, hypothesis: Hypothesis, *, order_id: str, quantity: float, side: str
) -> Hypothesis:
    hypothesis.traded_order_id = order_id
    hypothesis.traded_quantity = quantity
    hypothesis.position_side = side
    session.add(hypothesis)
    session.commit()
    session.refresh(hypothesis)
    return hypothesis


def mark_hypothesis_flat(session: Session, hypothesis: Hypothesis, *, exit_order_id: str) -> Hypothesis:
    """A trim/exit closed the whole open position (not a partial add) --
    clears the position fields so the revision loop treats this hypothesis
    as flat again (a subsequent "opened"-shaped belief can re-establish a
    position without looking like a stale add)."""
    hypothesis.exit_order_id = exit_order_id
    hypothesis.position_side = None
    hypothesis.traded_order_id = None
    hypothesis.traded_quantity = None
    session.add(hypothesis)
    session.commit()
    session.refresh(hypothesis)
    return hypothesis


def load_recent_hypotheses(session: Session, limit: int = 200) -> list[Hypothesis]:
    rows = session.exec(select(Hypothesis).order_by(Hypothesis.created_at.desc()).limit(limit)).all()
    return list(rows)


def load_latest_beliefs_by_hypothesis(session: Session, hypothesis_ids: list[str]) -> dict[str, HypothesisBelief]:
    """One hypothesis_id -> its single most recent HypothesisBelief, for
    the dashboard's hypotheses list. One query for all requested ids
    (ordered newest-first, first-seen-per-id kept), not one query per
    hypothesis -- same reasoning as _prediction_stats' SQL-aggregate
    rewrite: this runs on every dashboard page load."""
    if not hypothesis_ids:
        return {}
    rows = session.exec(
        select(HypothesisBelief)
        .where(HypothesisBelief.hypothesis_id.in_(hypothesis_ids))
        .order_by(HypothesisBelief.created_at.desc())
    ).all()
    latest: dict[str, HypothesisBelief] = {}
    for row in rows:
        latest.setdefault(row.hypothesis_id, row)
    return latest


def close_hypothesis(
    session: Session, hypothesis: Hypothesis, *, resolution_outcome: bool, closed_at: datetime | None = None
) -> Hypothesis:
    hypothesis.status = HypothesisStatus.CLOSED
    hypothesis.resolution_outcome = resolution_outcome
    hypothesis.closed_at = closed_at or datetime.now(timezone.utc)
    session.add(hypothesis)
    session.commit()
    session.refresh(hypothesis)
    return hypothesis
