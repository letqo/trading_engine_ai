from datetime import datetime, timedelta, timezone

from engine.backtest.perturbation import _is_fragile, run_perturbation_analysis
from engine.config.settings import RiskLimits
from engine.data.universe import Instrument, Universe
from engine.domain import Bar
from engine.risk.gate import RiskGate
from engine.strategy.baselines import RandomEntryStrategy

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


def test_is_fragile_collapse_beyond_threshold():
    assert _is_fragile(base=2.0, perturbed=0.5, drop_fraction=0.5)  # dropped 75%


def test_is_fragile_stable_within_threshold():
    assert not _is_fragile(base=2.0, perturbed=1.8, drop_fraction=0.5)  # dropped 10%


def test_is_fragile_sign_flip_is_always_fragile():
    assert _is_fragile(base=1.0, perturbed=-0.5, drop_fraction=0.5)


def test_is_fragile_unprofitable_base_is_not_flagged():
    assert not _is_fragile(base=-1.0, perturbed=-5.0, drop_fraction=0.5)


def _make_bars(n=60):
    bars = []
    price = 100.0
    for i in range(n):
        price *= 1.001  # gentle drift so there's something to trade
        bars.append(
            Bar(symbol="SPY", timestamp=T0 + timedelta(days=i), open=price, high=price * 1.01,
                low=price * 0.99, close=price, volume=1000, timeframe="1d")
        )
    return bars


def test_run_perturbation_analysis_covers_all_numeric_params():
    universe = Universe(
        instruments=(Instrument(symbol="SPY", tier=1, asset_class="equity_etf", news_topics=()),),
        source_text="x",
    )
    bars = _make_bars()

    def factory(entry_probability_per_bar, exit_after_bars):
        return RandomEntryStrategy(
            symbols=["SPY"], entry_probability_per_bar=entry_probability_per_bar,
            exit_after_bars=int(exit_after_bars), seed=7,
        )

    report = run_perturbation_analysis(
        strategy_factory=factory,
        base_params={"entry_probability_per_bar": 0.3, "exit_after_bars": 10},
        bars=bars,
        news=[],
        universe=universe,
        risk_gate_factory=lambda: RiskGate(RiskLimits()),
    )
    assert len(report.results) == 4  # 2 params x (+20%, -20%)
    names = {r.param_name for r in report.results}
    assert names == {"entry_probability_per_bar", "exit_after_bars"}
