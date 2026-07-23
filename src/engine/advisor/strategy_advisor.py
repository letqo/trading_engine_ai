"""Read-only strategy advisor: aggregates real performance across every
trading path this system has (predict-loop, anticipatory-loop, worker's
technical strategies, manual trades) and has Claude write a short narrative
of tuning suggestions. Never writes config itself -- the human reads the
text and acts on it manually through the existing *-config dashboard pages
(/papertrade-config, /predict-loop-config, /anticipatory-loop-config,
/risk-gate-config). See queue item 11 in the task-queue memory for the
design discussion this came out of.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import anthropic

from engine.config.settings import Settings
from engine.journal.models import HypothesisStatus, PredictionStatus, StrategyTrade
from engine.journal.registry import (
    load_prediction_trades,
    load_recent_hypotheses,
    load_recent_manual_trades,
    load_recent_strategy_trades,
)


class AdvisorConfigError(RuntimeError):
    pass


def _trade_pnl(side: str, entry_price: float, exit_price: float, quantity: float) -> float:
    return (exit_price - entry_price) * quantity if side == "buy" else (entry_price - exit_price) * quantity


@dataclass
class GroupStats:
    label: str
    count: int = 0
    closed: int = 0
    wins: int = 0
    total_pnl: float = 0.0

    @property
    def win_rate_pct(self) -> float | None:
        return (self.wins / self.closed * 100.0) if self.closed else None


def _strategy_trade_stats(rows: list[StrategyTrade]) -> tuple[list[GroupStats], list[GroupStats]]:
    by_strategy: dict[str, GroupStats] = {}
    by_symbol: dict[str, GroupStats] = {}
    for row in rows:
        s = by_strategy.setdefault(row.strategy_id, GroupStats(label=row.strategy_id))
        sym = by_symbol.setdefault(row.symbol, GroupStats(label=row.symbol))
        s.count += 1
        sym.count += 1
        if row.exit_price is not None:
            pnl = _trade_pnl(row.side, row.entry_price, row.exit_price, row.exit_quantity or row.entry_quantity)
            for g in (s, sym):
                g.closed += 1
                g.total_pnl += pnl
                if pnl > 0:
                    g.wins += 1
    by_strategy_sorted = sorted(by_strategy.values(), key=lambda g: g.count, reverse=True)
    by_symbol_sorted = sorted(by_symbol.values(), key=lambda g: g.count, reverse=True)
    return by_strategy_sorted, by_symbol_sorted


def build_performance_summary(session) -> dict:
    """Pulls from the same registry loaders the dashboard's other pages
    already use (load_prediction_trades, load_recent_strategy_trades, etc.)
    -- these are bounded to actually-traded/actually-tracked rows, not the
    full prediction/hypothesis log, so this stays cheap even as the log
    grows."""
    prediction_trades = load_prediction_trades(session)
    strategy_trades = load_recent_strategy_trades(session, limit=1000)
    manual_trades = load_recent_manual_trades(session, limit=1000)
    hypotheses = load_recent_hypotheses(session, limit=1000)

    resolved_predictions = [p for p in prediction_trades if p.status == PredictionStatus.RESOLVED]
    prediction_correct = sum(1 for p in resolved_predictions if p.outcome_correct)

    by_strategy, by_symbol = _strategy_trade_stats(strategy_trades)

    # Approximation: a closed, traded hypothesis is scored as directionally
    # correct if the market resolved YES, since direction_if_yes is fixed at
    # creation and the position is opened expecting that outcome (see
    # Hypothesis.direction_if_yes's docstring). Doesn't account for a
    # position later trimmed/re-added mid-life -- good enough for an
    # advisory narrative, not a precise P&L reconciliation.
    closed_traded_hyps = [h for h in hypotheses if h.status == HypothesisStatus.CLOSED and h.traded_order_id is not None]
    hyp_wins = sum(1 for h in closed_traded_hyps if h.resolution_outcome is True)

    manual_open = sum(1 for m in manual_trades if m.traded_order_id and not m.exit_order_id)
    manual_rejected = sum(1 for m in manual_trades if m.trade_rejected)

    return {
        "predictions": {
            "traded_count": len(prediction_trades),
            "resolved_count": len(resolved_predictions),
            "correct_count": prediction_correct,
            "accuracy_pct": (prediction_correct / len(resolved_predictions) * 100.0) if resolved_predictions else None,
        },
        "hypotheses": {
            "total": len(hypotheses),
            "open": sum(1 for h in hypotheses if h.status == HypothesisStatus.OPEN),
            "closed_traded": len(closed_traded_hyps),
            "win_count": hyp_wins,
            "win_rate_pct": (hyp_wins / len(closed_traded_hyps) * 100.0) if closed_traded_hyps else None,
        },
        "strategy_trades_by_strategy": by_strategy,
        "strategy_trades_by_symbol": by_symbol,
        "manual_trades": {
            "total": len(manual_trades),
            "open": manual_open,
            "rejected": manual_rejected,
        },
    }


SYSTEM_PROMPT = """You are a read-only trading-strategy advisor for a small \
automated trading system. You are given aggregate performance stats across \
four independent trading paths: an LLM consequence-prediction pipeline \
(predict-loop), an LLM prediction-market pipeline (anticipatory-loop, trades \
against Polymarket-implied probabilities), a technical-strategy engine \
(worker, one strategy live at a time: momentum, mean_reversion, \
multi_factor, dumb_news, overnight_gap), and ad-hoc manual trades placed by \
a human.

You never execute trades or change any configuration yourself -- you only \
write a short, specific narrative of suggestions a human operator can act on \
manually through the existing dashboard config pages (/papertrade-config to \
change the active technical strategy, /predict-loop-config and \
/anticipatory-loop-config for polling/thresholds, /risk-gate-config for \
position sizing and stop-loss). Be concrete: name which strategy or \
threshold you'd change and why, referencing the actual numbers given. If a \
sample size is too small to support a real conclusion, say so explicitly \
rather than sounding more confident than the evidence supports -- a wrong \
suggestion from thin data is worse than no suggestion. Do not recommend \
anything you weren't given evidence for."""


def build_advisor_prompt(summary: dict) -> str:
    lines = ["## Prediction pipeline (predict-loop)"]
    p = summary["predictions"]
    lines.append(
        f"- Traded: {p['traded_count']}, resolved: {p['resolved_count']}, correct: {p['correct_count']}"
        + (f" ({p['accuracy_pct']:.1f}% accuracy)" if p["accuracy_pct"] is not None else " (no resolved trades yet)")
    )

    lines.append("\n## Prediction-market pipeline (anticipatory-loop)")
    h = summary["hypotheses"]
    lines.append(
        f"- Tracked: {h['total']} ({h['open']} still open), closed+traded: {h['closed_traded']}, "
        + (
            f"directionally correct: {h['win_count']} ({h['win_rate_pct']:.1f}%)"
            if h["win_rate_pct"] is not None
            else "no closed+traded hypotheses yet"
        )
    )

    lines.append("\n## Technical strategies (worker), by strategy_id")
    for g in summary["strategy_trades_by_strategy"]:
        lines.append(
            f"- {g.label}: {g.count} opened, {g.closed} closed"
            + (f", win rate {g.win_rate_pct:.1f}%, total P&L {g.total_pnl:+.2f}" if g.closed else "")
        )
    if not summary["strategy_trades_by_strategy"]:
        lines.append("- none yet")

    lines.append("\n## Technical strategies (worker), by symbol")
    for g in summary["strategy_trades_by_symbol"][:15]:
        lines.append(
            f"- {g.label}: {g.count} opened, {g.closed} closed"
            + (f", win rate {g.win_rate_pct:.1f}%, total P&L {g.total_pnl:+.2f}" if g.closed else "")
        )
    if not summary["strategy_trades_by_symbol"]:
        lines.append("- none yet")

    lines.append("\n## Manual trades")
    m = summary["manual_trades"]
    lines.append(f"- Total: {m['total']}, currently open: {m['open']}, broker-rejected: {m['rejected']}")

    return "\n".join(lines)


def generate_advice(settings: Settings, summary: dict) -> str:
    if not settings.anthropic_api_key:
        raise AdvisorConfigError("ANTHROPIC_API_KEY is not set -- strategy advisor is unavailable.")
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    prompt = build_advisor_prompt(summary)
    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return "\n".join(b.text for b in response.content if b.type == "text")
