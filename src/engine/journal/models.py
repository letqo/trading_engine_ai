"""SQLModel tables: the durable state the whole system is built to protect.

SPEC.md Storage section is authoritative: Postgres from day one via
DATABASE_URL, SQLModel + Alembic migrations. (The one line under "Backtester
requirements" that says "SQLite table" for the experiment journal is a slip
against that -- everything durable lives behind DATABASE_URL, which is
Postgres in Docker/Railway and may be SQLite only for a zero-setup local
dev/test run.)

Nothing here is a container-filesystem artifact. Losing this on redeploy is
a critical bug per the Deployment section.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import ClassVar

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class RunMode(str, Enum):
    BACKTEST = "backtest"
    PAPER_LIVE = "paper_live"


class TradeSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class ExperimentRun(SQLModel, table=True):
    """One row per backtest (or live paper-trading session). This is the
    'experiment registry': config + git hash + data snapshot + seed +
    metrics, so every result is reproducible and traceable."""

    __tablename__ = "experiment_run"

    id: str = Field(default_factory=_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    mode: RunMode = Field(index=True)
    strategy_name: str = Field(index=True)

    config_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    git_hash: str
    data_snapshot_id: str | None = Field(default=None, index=True)
    random_seed: int

    period_start: datetime | None = None
    period_end: datetime | None = None
    is_validation_run: bool = Field(default=False, index=True)
    validation_access_reason: str | None = None

    total_return_pct: float | None = None
    max_drawdown_pct: float | None = None
    sharpe: float | None = None
    win_rate: float | None = None
    profit_factor: float | None = None
    num_trades: int | None = None
    avg_holding_hours: float | None = None
    exposure_pct: float | None = None

    notes: str | None = None


class TradeRecord(SQLModel, table=True):
    """Every fill, backtest or live. This table is the trade journal --
    losing it on redeploy is the specific critical-bug scenario called out
    in SPEC.md's Deployment section, which is why it lives in Postgres and
    nowhere else."""

    __tablename__ = "trade_record"

    id: str = Field(default_factory=_uuid, primary_key=True)
    run_id: str = Field(foreign_key="experiment_run.id", index=True)
    timestamp: datetime = Field(index=True)
    symbol: str = Field(index=True)
    side: TradeSide
    quantity: float
    price: float
    fees: float = 0.0
    slippage: float = 0.0
    strategy_id: str
    broker_order_id: str | None = Field(default=None, index=True)
    realized_pnl: float | None = None
    exit_reason: str | None = None


class NewsItemRecord(SQLModel, table=True):
    """Raw + scored news. 'Never discard source data' -- raw_payload always
    holds the untouched source response."""

    __tablename__ = "news_item_record"

    id: str = Field(default_factory=_uuid, primary_key=True)
    source: str = Field(index=True)
    published_at: datetime = Field(index=True)
    ingested_at: datetime = Field(default_factory=_now, index=True)
    headline: str
    url: str | None = None
    raw_payload: dict = Field(default_factory=dict, sa_column=Column(JSON))
    routed_symbols: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    sentiment_score: float | None = None
    sentiment_model: str | None = None


class DataSnapshot(SQLModel, table=True):
    """A named, immutable pointer to 'the data as of ingestion time X', so a
    backtest run's data_snapshot_id can be traced back to exactly what it
    saw -- required for the determinism/audit constraint."""

    __tablename__ = "data_snapshot"

    id: str = Field(default_factory=_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=_now)
    description: str
    universe_hash: str
    bar_start: datetime | None = None
    bar_end: datetime | None = None
    news_count: int = 0
    bar_row_count: int = 0


class RiskHaltEvent(SQLModel, table=True):
    """Every time RiskGate halts trading or the kill switch fires. This is
    the audit trail for the risk system, independent of trade outcomes."""

    __tablename__ = "risk_halt_event"

    id: str = Field(default_factory=_uuid, primary_key=True)
    timestamp: datetime = Field(default_factory=_now, index=True)
    run_id: str | None = Field(default=None, foreign_key="experiment_run.id", index=True)
    reason: str
    account_equity: float
    triggered_by: str


class ReconciliationReport(SQLModel, table=True):
    """Weekly paper-vs-backtest reconciliation, per the anti-self-deception
    protocol: divergence beyond tolerance must be investigated before any
    further iteration."""

    __tablename__ = "reconciliation_report"

    id: str = Field(default_factory=_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=_now)
    week_start: datetime
    week_end: datetime
    backtest_run_id: str = Field(foreign_key="experiment_run.id")
    backtest_expected_return_pct: float
    realized_return_pct: float
    divergence_pct: float
    tolerance_pct: float
    within_tolerance: bool
    notes: str | None = None


class PredictionDirection(str, Enum):
    UP = "up"
    DOWN = "down"


class PredictionStatus(str, Enum):
    PENDING = "pending"
    RESOLVED = "resolved"
    INVALID = "invalid"  # e.g. no price data available to resolve against


class Prediction(SQLModel, table=True):
    """One row per (news item, predicted symbol): the consequence-prediction
    pipeline's forward-testing log (engine.prediction).

    This is deliberately NOT a backtest artifact. It exists because an LLM
    cannot be backtested honestly against historical events it may already
    know the outcome of -- see docs/prediction_pipeline.md. Instead, every
    prediction is written here *before* its outcome is known, and scored
    later in a separate step (resolve_pending_predictions). The row is never
    edited after outcome resolution fills in the resolution fields, so the
    log can never be quietly adjusted once the answer is known.

    `forward_safe` is the load-bearing field: True only if the news item's
    decision_timestamp is after the reasoning model's stated knowledge
    cutoff, i.e. the event could not possibly be in that model's training
    data. Only forward_safe=True rows may ever be counted as evidence the
    prediction pipeline has real skill; forward_safe=False rows are kept for
    inspection but must never be scored as if they were a genuine test.
    """

    __tablename__ = "prediction"

    id: str = Field(default_factory=_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=_now, index=True)

    news_headline: str = Field(index=True)  # dedup key -- see registry.headline_already_predicted
    news_source: str
    news_published_at: datetime
    news_decision_timestamp: datetime = Field(index=True)  # see engine.domain.NewsItem.decision_timestamp
    topics: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    symbol: str = Field(index=True)
    direction: PredictionDirection
    confidence: float
    rationale: str
    # False means the model named a symbol outside universe.yaml -- kept
    # and scored (still real evidence) but never traded, since only vetted,
    # Alpaca-tradable, risk-calibrated symbols are eligible for real orders.
    # See `engine ticker-suggestions` for accumulated evidence on these,
    # ahead of a human decision to add one to universe.yaml.
    in_tracked_universe: bool = Field(index=True)

    model_name: str
    model_knowledge_cutoff: datetime
    forward_safe: bool = Field(index=True)

    retrieved_context_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    status: PredictionStatus = Field(default=PredictionStatus.PENDING, index=True)
    resolution_window_hours: float
    resolved_at: datetime | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    actual_return_pct: float | None = None
    outcome_correct: bool | None = None
    # Path context the entry/exit snapshot alone discards: how far price
    # moved in the predicted direction's favor (mfe_pct) and against it
    # (mae_pct) at any point during the window, not just at the endpoints.
    # Both are non-negative pct magnitudes relative to entry_price. A
    # prediction can be outcome_correct=True with a large mae_pct -- that
    # means it was right at the 24h mark but would have needed a wider stop
    # to actually capture it live. See docs/prediction_pipeline.md.
    mfe_pct: float | None = None
    mae_pct: float | None = None

    # Set only if this prediction was acted on with a real (paper) order --
    # see engine.prediction.trading. None means "never traded, log-only."
    # traded_quantity is the actual approved fill size, needed to close the
    # exact same size later without recomputing (equity may have moved).
    traded_order_id: str | None = Field(default=None, index=True)
    traded_quantity: float | None = None
    exit_order_id: str | None = None

    # Set if a real order submission was attempted and the broker itself
    # rejected it (e.g. Alpaca refusing a short-sell on an asset that
    # isn't shortable) -- distinct from a RiskGate rejection, which is
    # transient (position/exposure caps can free up) and so is retried
    # every cycle. A broker-level rejection is a structural fact about
    # this symbol/order that retrying won't change, so it's excluded from
    # load_actionable_predictions instead of silently re-attempted (and
    # re-failing) forever. See engine.prediction.trading.
    trade_rejected: bool = Field(default=False, index=True)
    trade_rejection_reason: str | None = None


class PredictionTopic(SQLModel, table=True):
    """One row per (prediction, topic) pair -- lets
    registry.load_resolved_predictions_by_topics query by topic with a real
    index instead of loading every resolved Prediction into Python to
    filter Prediction.topics (a JSON blob) in memory. Prediction.topics
    itself is unchanged and stays the source of truth for display/audit
    ("never discard source data") -- this table is a derived index over it,
    populated alongside every record_prediction() call, not a replacement."""

    __tablename__ = "prediction_topic"

    id: str = Field(default_factory=_uuid, primary_key=True)
    prediction_id: str = Field(foreign_key="prediction.id", index=True)
    topic: str = Field(index=True)


class PredictLoopConfig(SQLModel, table=True):
    """Single-row live-tunable config for `engine predict-loop`, polled once
    per cycle so an operator can change strategy from the dashboard without
    a redeploy. Deliberately NOT engine.config.settings.Settings -- that's
    env-var-only and cached for process lifetime via @lru_cache
    get_settings(), so it can never change without a restart.

    rotation_anchor + rotation_hours let predict_loop compute which RSS
    source is "active" this cycle as a pure function of wall-clock time
    (engine.data.news.active_rss_source) -- there is no separate "current
    source index" column here on purpose, since a persisted counter would
    need something to advance it and could drift or double-advance across
    restarts."""

    __tablename__ = "predict_loop_config"
    SINGLETON_ID: ClassVar[str] = "singleton"

    id: str = Field(default="singleton", primary_key=True)
    updated_at: datetime = Field(default_factory=_now)
    # Stamped once per loop iteration (paused or not) -- distinct from
    # updated_at, which only changes when a setting is edited. This is the
    # dashboard's only way to tell "the process is alive and iterating"
    # from "Railway says Online but it's actually crash-looping" (the
    # ON_FAILURE restart policy masks that distinction otherwise).
    last_cycle_at: datetime | None = Field(default=None)

    enabled: bool = Field(default=True)  # False = pause cycle body only, loop keeps polling
    poll_seconds: int = Field(default=3600)
    rotation_hours: float = Field(default=1.0)
    rotation_anchor: datetime = Field(default_factory=_now)
    headlines_per_source: int = Field(default=10)
    near_dup_window_hours: float = Field(default=48.0)
    near_dup_threshold: float = Field(default=90.0)


class HypothesisStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class Hypothesis(SQLModel, table=True):
    """One row per tracked Polymarket market -- see
    docs/anticipatory_prediction_mode.md. Deliberately NOT an extension of
    Prediction: a Prediction is written once and resolved exactly once,
    which is the wrong shape for a belief that gets revised repeatedly
    over an event's life (see HypothesisBelief). `market_id` is
    Polymarket's conditionId and is the dedup key -- one Hypothesis per
    market, never re-created once tracked (even after it closes).

    Position tracking is fields directly on this row (traded_order_id/
    traded_quantity/position_side), mirroring how Prediction tracks its
    own trade lifecycle -- NOT TradeRecord. TradeRecord.run_id is a
    required FK to experiment_run and is currently backtest-only; no
    live/paper trading path in this codebase writes TradeRecord rows, so
    reusing it here would be a new integration, not an extension of an
    existing one."""

    __tablename__ = "hypothesis"

    id: str = Field(default_factory=_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=_now)
    market_id: str = Field(index=True, unique=True)
    question: str
    symbol: str = Field(index=True)
    direction_if_yes: PredictionDirection  # fixed at creation from the initial LLM estimate --
    # only probability gets re-estimated on revision, not exposure/direction (see engine.anticipatory.pipeline)
    status: HypothesisStatus = Field(default=HypothesisStatus.OPEN, index=True)
    closed_at: datetime | None = None
    resolution_outcome: bool | None = None  # True=YES, False=NO -- set once, on close

    position_side: str | None = None  # "long" / "short", None if currently flat
    traded_order_id: str | None = Field(default=None, index=True)
    traded_quantity: float | None = None
    exit_order_id: str | None = None

    # See Prediction.trade_rejected's docstring -- same distinction (a
    # broker-level rejection is structural, not retried) applied here.
    # act_on_hypothesis_beliefs skips calling _open_position again once
    # this is set, even though belief revision keeps re-estimating and
    # may keep producing OPENED actions -- tracking the model's belief
    # stays useful even for a symbol that turned out untradeable.
    trade_rejected: bool = Field(default=False, index=True)
    trade_rejection_reason: str | None = None


class HypothesisAction(str, Enum):
    OPENED = "opened"
    ADDED = "added"
    TRIMMED = "trimmed"
    EXITED = "exited"
    HELD = "held"
    NO_GAP = "no_gap"


class HypothesisBelief(SQLModel, table=True):
    """Append-only, one row per re-estimation of a Hypothesis -- never
    edited after written, same anti-hindsight principle that makes
    Prediction trustworthy today, just shaped for a persisting belief
    instead of a single call. `gap = p_model - p_market`, stored
    (rather than only derived) so the audit trail doesn't depend on
    recomputing it correctly later."""

    __tablename__ = "hypothesis_belief"

    id: str = Field(default_factory=_uuid, primary_key=True)
    hypothesis_id: str = Field(foreign_key="hypothesis.id", index=True)
    created_at: datetime = Field(default_factory=_now, index=True)

    p_model: float
    p_market: float
    gap: float
    confidence: float
    rationale: str
    action: HypothesisAction
    order_id: str | None = None
    order_quantity: float | None = None


class AnticipatoryLoopConfig(SQLModel, table=True):
    """Single-row live-tunable config for `engine anticipatory-loop`,
    mirroring PredictLoopConfig exactly (see its docstring for why this
    isn't engine.config.settings.Settings)."""

    __tablename__ = "anticipatory_loop_config"
    SINGLETON_ID: ClassVar[str] = "singleton"

    id: str = Field(default="singleton", primary_key=True)
    updated_at: datetime = Field(default_factory=_now)
    last_cycle_at: datetime | None = Field(default=None)  # see PredictLoopConfig's docstring

    enabled: bool = Field(default=True)
    poll_seconds: int = Field(default=3600)
    min_gap_threshold: float = Field(default=0.05)  # |p_model - p_market| below this: no trade
    max_open_hypotheses: int = Field(default=10)  # discovery stops creating new ones above this
    discovery_limit: int = Field(default=20)  # candidate events considered per discovery sweep
    # Polymarket often splits one underlying question into several
    # threshold markets (e.g. "hit $110" and "hit $120"), which all map to
    # the same tradable symbol via the same LLM reasoning -- capping per
    # symbol keeps max_open_hypotheses from being spent entirely on
    # correlated bets on one instrument. Checked after the relevance LLM
    # call returns a symbol (that call's cost can't be avoided -- the
    # symbol isn't known before asking), so this only prevents the
    # Hypothesis row/position from being created, not the paid call itself.
    max_open_hypotheses_per_symbol: int = Field(default=2)


class StrategyTrade(SQLModel, table=True):
    """One row per real position opened live by engine.execution.live_loop
    -- the `worker` service's --strategy path (momentum, mean_reversion,
    multi_factor, dumb_news, overnight_gap). Before this table existed,
    that path submitted real broker orders with zero journal footprint,
    which is exactly how the orphaned-SPY-position mystery happened: a
    live position nothing else could see, attribute, or protect with a
    stop-loss sweep. Written the moment the broker order is submitted --
    a RiskGate rejection here is never journaled, since the strategy just
    re-decides from a fresh signal next cycle rather than needing a
    trade_rejected suppression flag like Prediction/Hypothesis do.

    exit_reason distinguishes a normal opposite-signal close ("signal"),
    live_loop's own stop-loss sweep ("stop_loss", bypasses RiskGate by
    design -- see live_loop's module docstring), and a close initiated
    through the dashboard's generic close-any-position flow
    ("manual_close")."""

    __tablename__ = "strategy_trade"

    id: str = Field(default_factory=_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    strategy_id: str = Field(index=True)
    symbol: str = Field(index=True)
    side: str  # "buy" / "sell", the opening side (engine.risk.models.Side.value)
    entry_order_id: str = Field(index=True)
    entry_quantity: float
    entry_price: float
    reason: str | None = None  # the strategy signal's own reason string, if any

    exit_order_id: str | None = Field(default=None, index=True)
    exit_quantity: float | None = None
    exit_price: float | None = None
    exit_reason: str | None = None


class ManualTrade(SQLModel, table=True):
    """One row per raw manual order submitted from the dashboard with no
    Prediction/Hypothesis behind it (engine.execution.manual_trading
    .open_manual_trade). Converting an existing Prediction/Hypothesis into
    a trade does NOT create a row here -- it reuses that row's own
    traded_order_id/traded_quantity/exit_order_id/trade_rejected fields via
    the existing mark_prediction_traded/mark_hypothesis_traded machinery,
    exactly like an automatically-traded one. This table exists only for
    the "no journal row exists yet" case.

    Only ever created once an order is approved by RiskGate and attempted
    at the broker -- a RiskGate rejection on a raw manual order is
    surfaced to the operator directly and never journaled (there's no
    retry loop to suppress here, unlike Prediction/Hypothesis's
    trade_rejected, which exists specifically to stop the *automatic* loop
    from retrying a structurally-doomed order forever)."""

    __tablename__ = "manual_trade"

    id: str = Field(default_factory=_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    submitted_by: str  # HTTP Basic username from require_auth -- audit trail only, not an authz boundary
    symbol: str = Field(index=True)
    side: str  # "buy" / "sell", the requested opening side (engine.risk.models.Side.value)
    requested_quantity: float  # what the operator typed, pre-RiskGate-clip
    note: str | None = None

    traded_order_id: str | None = Field(default=None, index=True)
    traded_quantity: float | None = None
    exit_order_id: str | None = None

    trade_rejected: bool = Field(default=False, index=True)
    trade_rejection_reason: str | None = None


class RiskGateConfig(SQLModel, table=True):
    """Single-row live-tunable override of engine.config.settings.RiskLimits,
    same get-or-create singleton pattern as PredictLoopConfig (see its
    docstring). Field defaults mirror RiskLimits' env-var defaults exactly,
    but that's only the seed value shown before anyone edits this row --
    the two are otherwise independent, resolved by engine.risk.resolve
    .resolve_risk_limits().

    use_defaults=True (the default) means every live trading path ignores
    this row entirely and uses Settings.risk (env-configured) instead --
    "activate default mode" without discarding whatever's been typed into
    the overrides below, so toggling back to manual doesn't mean
    re-entering every number. Only the three continuously-running loops
    (papertrade, predict-loop, anticipatory-loop) and the two one-shot
    live-trading CLI commands (act-on-predictions, resolve-predictions)
    read this row; `engine backtest`/perturbation analysis stay
    Settings.risk-only so results stay reproducible regardless of what's
    been tuned live in production."""

    __tablename__ = "risk_gate_config"
    SINGLETON_ID: ClassVar[str] = "singleton"

    id: str = Field(default="singleton", primary_key=True)
    updated_at: datetime = Field(default_factory=_now)

    use_defaults: bool = Field(default=True)
    max_capital_per_position_pct: float = Field(default=0.05)
    max_total_exposure_pct: float = Field(default=0.20)
    stop_loss_pct: float = Field(default=0.02)
    max_daily_drawdown_pct: float = Field(default=0.03)
    max_consecutive_losses_per_day: int = Field(default=4)
    allow_overnight_positions: bool = Field(default=False)


class PapertradeConfig(SQLModel, table=True):
    """Single-row live-tunable strategy selection for `engine papertrade`
    (the `worker` service), same get-or-create singleton pattern as
    PredictLoopConfig (see its docstring for why this isn't
    engine.config.settings.Settings / the PAPERTRADE_STRATEGY env var).

    strategy=None means the reconcile/kill-switch skeleton only, no
    trading -- same meaning as the CLI's `--strategy` being omitted. The
    `--strategy`/PAPERTRADE_STRATEGY CLI option only *seeds* this row on
    first run when explicitly passed; once set, papertrade re-reads this
    row every iteration (see mark_papertrade_cycle) and hot-switches
    strategies without a restart, the same way PredictLoopConfig.enabled
    is re-checked every predict-loop cycle."""

    __tablename__ = "papertrade_config"
    SINGLETON_ID: ClassVar[str] = "singleton"

    id: str = Field(default="singleton", primary_key=True)
    updated_at: datetime = Field(default_factory=_now)
    last_cycle_at: datetime | None = Field(default=None)  # see PredictLoopConfig's docstring

    strategy: str | None = Field(default=None)
