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

    alert_webhook_url: str | None = None

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
