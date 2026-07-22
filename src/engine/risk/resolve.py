"""Resolves the RiskLimits a live trading path should actually enforce --
either Settings.risk (env-configured, process-lifetime-fixed) or a
dashboard-tunable RiskGateConfig override, per RiskGateConfig.use_defaults.

Only imported by continuously-running/one-shot *live* trading paths
(papertrade, predict-loop, anticipatory-loop, act-on-predictions,
resolve-predictions). engine.backtest deliberately never imports this --
backtests build RiskGate straight from settings.risk so a result stays
reproducible no matter what's been tuned live in production since."""

from __future__ import annotations

from engine.config.settings import RiskLimits, Settings
from engine.journal.models import RiskGateConfig


def resolve_risk_limits(settings: Settings, config: RiskGateConfig) -> RiskLimits:
    if config.use_defaults:
        return settings.risk
    return RiskLimits(
        max_capital_per_position_pct=config.max_capital_per_position_pct,
        max_total_exposure_pct=config.max_total_exposure_pct,
        stop_loss_pct=config.stop_loss_pct,
        max_daily_drawdown_pct=config.max_daily_drawdown_pct,
        max_consecutive_losses_per_day=config.max_consecutive_losses_per_day,
        allow_overnight_positions=config.allow_overnight_positions,
    )
