"""Mostly-read-only reporting dashboard. Never imports anything that can
submit, modify, or cancel an order -- engine.execution.broker/RiskGate are
not imported here. The only broker calls made are AlpacaPaperClient's read
methods (get_account_equity, get_positions), same as any other dashboard
consumer of a paper account's public state.

The one exception is /predict-loop-config: a GET/POST pair that reads and
writes PredictLoopConfig (poll interval, RSS rotation, headline quotas,
near-duplicate thresholds, pause/resume). It can pause or retune the
predict-loop research loop, but it cannot place or cancel an order -- same
trust boundary as everything else here, just no longer read-only for that
one setting.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from engine.config.guard import PaperOnlyViolation, enforce_paper_only
from engine.config.settings import Settings, get_settings
from engine.dashboard.auth import require_auth
from engine.journal.db import get_session
from engine.journal.models import Prediction, PredictionStatus
from engine.journal.registry import (
    get_predict_loop_config,
    load_off_universe_symbol_stats,
    load_prediction_trades,
    load_recent_experiment_runs,
    load_recent_risk_halts,
    update_predict_loop_config,
)

app = FastAPI(title="Trading engine dashboard")
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _prediction_stats(settings: Settings) -> dict:
    with get_session(settings) as session:
        rows = session.exec(
            select(Prediction).where(Prediction.status == PredictionStatus.RESOLVED, Prediction.forward_safe == True)  # noqa: E712
        ).all()
        traded_count = len(load_prediction_trades(session))
        recent = session.exec(select(Prediction).order_by(Prediction.news_decision_timestamp.desc()).limit(100)).all()

    resolved_count = len(rows)
    correct_count = sum(1 for r in rows if r.outcome_correct)
    accuracy_pct = (correct_count / resolved_count * 100.0) if resolved_count else None

    with_excursion = [r for r in rows if r.mae_pct is not None]
    avg_mfe = sum(r.mfe_pct for r in with_excursion) / len(with_excursion) if with_excursion else None
    avg_mae = sum(r.mae_pct for r in with_excursion) / len(with_excursion) if with_excursion else None
    stop_pct = settings.risk.stop_loss_pct * 100.0
    would_have_stopped = sum(1 for r in with_excursion if r.outcome_correct and r.mae_pct >= stop_pct)

    return {
        "resolved_count": resolved_count,
        "correct_count": correct_count,
        "accuracy_pct": accuracy_pct,
        "avg_mfe": avg_mfe,
        "avg_mae": avg_mae,
        "stop_pct": stop_pct,
        "would_have_stopped": would_have_stopped,
        "traded_count": traded_count,
        "recent_predictions": recent,
    }


@app.get("/", response_class=HTMLResponse)
def overview(request: Request, _user: str = Depends(require_auth), settings: Settings = Depends(get_settings)):
    stats = _prediction_stats(settings)

    equity_display = "n/a"
    open_positions_count = 0
    positions = []
    positions_note = "ALPACA_API_KEY not set -- account state unavailable."
    if settings.alpaca_api_key and settings.alpaca_api_secret:
        try:
            enforce_paper_only(settings)
            from engine.execution.alpaca import AlpacaPaperClient

            client = AlpacaPaperClient(settings)
            equity = client.get_account_equity()
            equity_display = f"${equity:,.2f}"
            live_positions = client.get_positions()
            positions = [p for p in live_positions.values() if p.quantity != 0]
            open_positions_count = len(positions)
            positions_note = "No open positions." if not positions else ""
        except PaperOnlyViolation as exc:
            positions_note = f"Paper-only guard refused: {exc}"
        except Exception as exc:  # noqa: BLE001 -- dashboard must render even if the broker call fails
            positions_note = f"Could not reach broker: {exc}"

    with get_session(settings) as session:
        backtest_count = len(load_recent_experiment_runs(session, limit=1000))

    return _templates.TemplateResponse(
        request,
        "index.html",
        {
            "equity_display": equity_display,
            "open_positions_count": open_positions_count,
            "positions": positions,
            "positions_note": positions_note,
            "backtest_count": backtest_count,
            **stats,
        },
    )


@app.get("/predictions", response_class=HTMLResponse)
def predictions(request: Request, _user: str = Depends(require_auth), settings: Settings = Depends(get_settings)):
    stats = _prediction_stats(settings)
    return _templates.TemplateResponse(
        request, "predictions.html", {"predictions": stats.pop("recent_predictions"), **stats}
    )


@app.get("/trades", response_class=HTMLResponse)
def trades(request: Request, _user: str = Depends(require_auth), settings: Settings = Depends(get_settings)):
    with get_session(settings) as session:
        rows = load_prediction_trades(session)
    return _templates.TemplateResponse(request, "trades.html", {"trades": rows})


@app.get("/ticker-suggestions", response_class=HTMLResponse)
def ticker_suggestions(request: Request, _user: str = Depends(require_auth), settings: Settings = Depends(get_settings)):
    with get_session(settings) as session:
        rows = load_off_universe_symbol_stats(session)
    return _templates.TemplateResponse(
        request,
        "ticker_suggestions.html",
        {"suggestions": rows, "min_resolved": 5, "min_accuracy_pct": 65.0},
    )


@app.get("/backtests", response_class=HTMLResponse)
def backtests(request: Request, _user: str = Depends(require_auth), settings: Settings = Depends(get_settings)):
    with get_session(settings) as session:
        rows = load_recent_experiment_runs(session, limit=200)
    return _templates.TemplateResponse(request, "backtests.html", {"runs": rows})


@app.get("/risk-events", response_class=HTMLResponse)
def risk_events(request: Request, _user: str = Depends(require_auth), settings: Settings = Depends(get_settings)):
    with get_session(settings) as session:
        rows = load_recent_risk_halts(session, limit=200)
    return _templates.TemplateResponse(request, "risk_events.html", {"events": rows})


@app.get("/predict-loop-config", response_class=HTMLResponse)
def predict_loop_config_view(request: Request, _user: str = Depends(require_auth), settings: Settings = Depends(get_settings)):
    with get_session(settings) as session:
        config = get_predict_loop_config(session)
    return _templates.TemplateResponse(request, "predict_loop_config.html", {"config": config, "saved": False})


@app.post("/predict-loop-config", response_class=HTMLResponse)
def predict_loop_config_update(
    request: Request,
    _user: str = Depends(require_auth),
    settings: Settings = Depends(get_settings),
    enabled: bool = Form(False),
    poll_seconds: int = Form(...),
    rotation_hours: float = Form(...),
    headlines_per_source: int = Form(...),
    near_dup_window_hours: float = Form(...),
    near_dup_threshold: float = Form(...),
):
    with get_session(settings) as session:
        config = update_predict_loop_config(
            session,
            enabled=enabled,
            poll_seconds=poll_seconds,
            rotation_hours=rotation_hours,
            headlines_per_source=headlines_per_source,
            near_dup_window_hours=near_dup_window_hours,
            near_dup_threshold=near_dup_threshold,
        )
    return _templates.TemplateResponse(request, "predict_loop_config.html", {"config": config, "saved": True})


@app.get("/health")
def health():
    return {"status": "ok"}
