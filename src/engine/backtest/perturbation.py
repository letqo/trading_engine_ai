"""Overfitting check: perturb each numeric strategy parameter ±20% and see
if the metric collapses. SPEC.md anti-self-deception protocol: "If
performance collapses under small perturbations, flag the run as fragile."
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from engine.backtest.engine import BacktestEngine, BacktestResult
from engine.data.universe import Universe
from engine.domain import Bar, NewsItem
from engine.risk.gate import RiskGate

DEFAULT_METRIC: Callable[[BacktestResult], float] = lambda r: r.metrics.sharpe  # noqa: E731


@dataclass(frozen=True)
class PerturbationResult:
    param_name: str
    base_value: float
    perturbed_value: float
    base_metric: float
    perturbed_metric: float
    fragile: bool


@dataclass(frozen=True)
class PerturbationReport:
    base_metric: float
    results: list[PerturbationResult]

    @property
    def any_fragile(self) -> bool:
        return any(r.fragile for r in self.results)


def run_perturbation_analysis(
    strategy_factory: Callable[..., object],
    base_params: dict[str, float],
    bars: list[Bar],
    news: list[NewsItem],
    universe: Universe,
    risk_gate_factory: Callable[[], RiskGate],
    initial_equity: float = 100_000.0,
    metric_fn: Callable[[BacktestResult], float] = DEFAULT_METRIC,
    perturbation_pct: float = 0.20,
    fragility_drop_fraction: float = 0.5,
) -> PerturbationReport:
    """Runs the base config, then each numeric param at +pct and -pct, all
    else held fixed. A perturbation is 'fragile' if the metric drops by more
    than `fragility_drop_fraction` of the base value, or flips sign entirely.
    """

    def run_once(**kwargs) -> float:
        strategy = strategy_factory(**kwargs)
        engine = BacktestEngine(strategy, universe, risk_gate_factory(), initial_equity=initial_equity)
        result = engine.run(bars, news)
        return metric_fn(result)

    base_metric = run_once(**base_params)

    results: list[PerturbationResult] = []
    for name, value in base_params.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        for direction in (1 + perturbation_pct, 1 - perturbation_pct):
            perturbed_value = value * direction
            kwargs = dict(base_params)
            kwargs[name] = perturbed_value
            perturbed_metric = run_once(**kwargs)
            fragile = _is_fragile(base_metric, perturbed_metric, fragility_drop_fraction)
            results.append(
                PerturbationResult(
                    param_name=name,
                    base_value=value,
                    perturbed_value=perturbed_value,
                    base_metric=base_metric,
                    perturbed_metric=perturbed_metric,
                    fragile=fragile,
                )
            )

    return PerturbationReport(base_metric=base_metric, results=results)


def _is_fragile(base: float, perturbed: float, drop_fraction: float) -> bool:
    if base <= 0:
        # base isn't even profitable/positive -- fragility isn't the
        # interesting question here, the strategy is just dead already.
        return False
    if perturbed <= 0:
        return True
    return (base - perturbed) / base >= drop_fraction
