"""Central configuration. All config comes from environment variables (12-factor).

Nothing here reads a broker base URL from the environment on purpose: the
paper/sandbox endpoint is a hardcoded constant in engine.execution.alpaca.
That is enforced, not just documented -- see engine.config.guard.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RiskLimits(BaseSettings):
    """Risk rule defaults from SPEC.md. Configurable via env, always enforced."""

    model_config = SettingsConfigDict(env_prefix="RISK_")

    max_capital_per_position_pct: float = 0.05
    max_total_exposure_pct: float = 0.20
    stop_loss_pct: float = 0.02
    max_daily_drawdown_pct: float = 0.03
    max_consecutive_losses_per_day: int = 4
    allow_overnight_positions: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = Field(default="development")

    database_url: str = Field(default="sqlite:///./local_dev.db")

    universe_path: Path = Field(default=Path("universe.yaml"))
    data_dir: Path = Field(default=Path("data_snapshots"))

    # Broker credentials -- values only, never an endpoint. See guard.py.
    alpaca_api_key: str | None = None
    alpaca_api_secret: str | None = None

    # Any of these env vars being set at all is treated as an attempt to
    # point the broker somewhere other than the hardcoded paper endpoint,
    # and trips the paper-only guard. They exist here only so the guard has
    # something to check for; the app never reads them to build a client.
    alpaca_base_url: str | None = None
    alpaca_live: str | None = None

    news_api_key: str | None = None
    finnhub_api_key: str | None = None

    # Alpaca's historical news endpoint (data.alpaca.markets/v1beta1/news,
    # Benzinga-sourced, back to 2015). Reuses alpaca_api_key/alpaca_api_secret
    # above -- no separate credential. This is a market-data endpoint, not an
    # order-routing one, so it isn't subject to the paper/live guard in
    # engine.config.guard (Alpaca doesn't split market data by paper/live).
    #
    # ingested_at for backfilled articles can't be the real historical
    # ingestion time (we weren't polling back then), so we simulate one:
    # published_at + this lag. See engine/data/alpaca_news.py -- fabricating
    # ingested_at = published_at (zero lag) would be the "quietly optimistic"
    # mistake docs/bias_review.md warns about, so this defaults to a
    # pessimistic worst-case poll interval instead of zero.
    alpaca_news_backfill_lag_seconds: float = 900.0



    # Consequence-prediction pipeline (engine.prediction). Absent key -> the
    # pipeline refuses to run rather than silently doing nothing; see
    # engine/prediction/client.py.
    anthropic_api_key: str | None = None
    # Alternative to anthropic_api_key: a long-lived token from `claude
    # setup-token`, authenticating via a Claude subscription instead of
    # metered API billing. If both are set, the OAuth token wins (see
    # engine.prediction.factory). Authenticates the `claude` CLI, not the
    # Python SDK -- see engine/prediction/cli_client.py.
    claude_code_oauth_token: str | None = None
    anthropic_model: str = "claude-opus-4-8"
    # Deliberately an obvious placeholder, not a guessed real date -- see
    # engine/prediction/client.py. Must be set to the actual training-data
    # knowledge cutoff of whatever model ANTHROPIC_MODEL names.
    anthropic_model_knowledge_cutoff: str = "1970-01-01"
    prediction_resolution_hours: float = 24.0
    prediction_retrieval_limit: int = 5
    # Minimum confidence before a prediction is acted on with a real (paper)
    # order -- see engine.prediction.trading. Predictions below this are
    # still logged and scored, just never traded.
    prediction_action_confidence_threshold: float = 0.6
    # Default cycle interval for `engine predict-loop`, the automatic
    # version of predict-news + act-on-predictions + resolve-predictions.
    prediction_loop_poll_seconds: int = 3600

    alert_webhook_url: str | None = None

    # Read-only reporting dashboard (engine.dashboard). HTTP Basic Auth,
    # single shared password -- unset means the dashboard refuses to start
    # rather than serving trade/prediction history with no auth at all.
    dashboard_password: str | None = None

    # Remote kill switch (Railway-friendly: flip env var + redeploy).
    halt: bool = False
    halt_file: Path = Field(default=Path("HALT"))

    random_seed: int = 1337

    risk: RiskLimits = Field(default_factory=RiskLimits)

    log_level: str = "INFO"
    log_json: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
