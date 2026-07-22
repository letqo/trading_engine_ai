from engine.config.settings import RiskLimits, Settings
from engine.journal.models import RiskGateConfig
from engine.risk.resolve import resolve_risk_limits


def _settings() -> Settings:
    return Settings(_env_file=None, risk=RiskLimits(max_capital_per_position_pct=0.05, stop_loss_pct=0.02))


def test_resolve_risk_limits_uses_env_defaults_when_use_defaults_true():
    config = RiskGateConfig(use_defaults=True, max_capital_per_position_pct=0.99)  # override present but ignored
    resolved = resolve_risk_limits(_settings(), config)
    assert resolved.max_capital_per_position_pct == 0.05  # from Settings.risk, not the config row


def test_resolve_risk_limits_uses_db_overrides_when_use_defaults_false():
    config = RiskGateConfig(
        use_defaults=False,
        max_capital_per_position_pct=0.1,
        max_total_exposure_pct=0.3,
        stop_loss_pct=0.04,
        max_daily_drawdown_pct=0.05,
        max_consecutive_losses_per_day=6,
        allow_overnight_positions=True,
    )
    resolved = resolve_risk_limits(_settings(), config)
    assert resolved.max_capital_per_position_pct == 0.1
    assert resolved.max_total_exposure_pct == 0.3
    assert resolved.stop_loss_pct == 0.04
    assert resolved.max_daily_drawdown_pct == 0.05
    assert resolved.max_consecutive_losses_per_day == 6
    assert resolved.allow_overnight_positions is True
