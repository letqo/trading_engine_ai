"""Mostly-read-only reporting dashboard. Never imports anything that can
submit, modify, or cancel an order -- engine.execution.broker/RiskGate are
not imported here. The only broker calls made are AlpacaPaperClient's read
methods (get_account_equity, get_positions), same as any other dashboard
consumer of a paper account's public state.

The exceptions are /predict-loop-config and /anticipatory-loop-config:
GET/POST pairs that read and write PredictLoopConfig/AnticipatoryLoopConfig
(poll interval, thresholds, pause/resume). Either can pause or retune its
respective research loop, but neither can place or cancel an order -- same
trust boundary as everything else here, just no longer read-only for those
two settings.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func
from sqlmodel import select

from engine.config.guard import PaperOnlyViolation, enforce_paper_only
from engine.config.settings import Settings, get_settings
from engine.dashboard.auth import require_auth
from engine.journal.db import get_session
from engine.journal.models import Prediction, PredictionStatus
from engine.journal.registry import (
    get_anticipatory_loop_config,
    get_predict_loop_config,
    load_latest_beliefs_by_hypothesis,
    load_off_universe_symbol_stats,
    load_prediction_trades,
    load_recent_experiment_runs,
    load_recent_hypotheses,
    load_recent_risk_halts,
    update_anticipatory_loop_config,
    update_predict_loop_config,
)

app = FastAPI(title="Trading engine dashboard")
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _parse_date_filters(start_date: str | None, end_date: str | None) -> tuple[datetime | None, datetime | None]:
    """Parses "YYYY-MM-DD" strings from an HTML date input into the naive
    UTC-assumed datetimes Prediction.news_decision_timestamp is stored as
    (see registry._hours_elapsed's docstring on the same SQLite tzinfo-drop
    convention). end_date is inclusive of the whole day, so the WHERE clause
    uses a `<` on the following midnight rather than `<=` on the bare date."""
    start = datetime.strptime(start_date, "%Y-%m-%d") if start_date else None
    end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1) if end_date else None
    return start, end


def _prediction_stats(
    settings: Settings,
    *,
    symbol: str | None = None,
    outcome: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    """Aggregate stats computed SQL-side (COUNT/AVG/conditional SUM), not by
    loading every resolved prediction into Python and summing over it --
    this runs on every dashboard page load, and a full-table Python scan
    gets slower every day the prediction log grows (months of predict-loop
    running easily reaches tens of thousands of rows). `recent` is already
    bounded (limit 100), left as-is.

    symbol/outcome/start/end are optional display filters (from /predictions'
    filter form) -- when all None, behavior is identical to unfiltered."""
    stop_pct = settings.risk.stop_loss_pct * 100.0

    with get_session(settings) as session:
        display_filters = []
        if symbol:
            display_filters.append(Prediction.symbol == symbol)
        if outcome == "correct":
            display_filters.append(Prediction.outcome_correct == True)  # noqa: E712
        elif outcome == "incorrect":
            display_filters.append(Prediction.outcome_correct == False)  # noqa: E712
        if start is not None:
            display_filters.append(Prediction.news_decision_timestamp >= start)
        if end is not None:
            display_filters.append(Prediction.news_decision_timestamp < end)

        resolved_filter = (
            Prediction.status == PredictionStatus.RESOLVED,
            Prediction.forward_safe == True,  # noqa: E712
            *display_filters,
        )

        resolved_count, correct_count = session.exec(
            select(
                func.count(),
                func.sum(case((Prediction.outcome_correct == True, 1), else_=0)),  # noqa: E712
            ).where(*resolved_filter)
        ).one()

        avg_mfe, avg_mae, would_have_stopped = session.exec(
            select(
                func.avg(Prediction.mfe_pct),
                func.avg(Prediction.mae_pct),
                func.sum(
                    case(
                        (
                            (Prediction.outcome_correct == True) & (Prediction.mae_pct >= stop_pct),  # noqa: E712
                            1,
                        ),
                        else_=0,
                    )
                ),
            ).where(*resolved_filter, Prediction.mae_pct.is_not(None))
        ).one()

        traded_count = len(load_prediction_trades(session))
        recent = session.exec(
            select(Prediction)
            .where(*display_filters)
            .order_by(Prediction.news_decision_timestamp.desc())
            .limit(100)
        ).all()

    accuracy_pct = (correct_count / resolved_count * 100.0) if resolved_count else None

    return {
        "resolved_count": resolved_count,
        "correct_count": correct_count or 0,
        "accuracy_pct": accuracy_pct,
        "avg_mfe": avg_mfe,
        "avg_mae": avg_mae,
        "stop_pct": stop_pct,
        "would_have_stopped": would_have_stopped or 0,
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
def predictions(
    request: Request,
    _user: str = Depends(require_auth),
    settings: Settings = Depends(get_settings),
    symbol: str | None = None,
    outcome: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
):
    start, end = _parse_date_filters(start_date, end_date)
    stats = _prediction_stats(settings, symbol=symbol or None, outcome=outcome or None, start=start, end=end)
    with get_session(settings) as session:
        symbols = session.exec(select(Prediction.symbol).distinct().order_by(Prediction.symbol)).all()
    return _templates.TemplateResponse(
        request,
        "predictions.html",
        {
            "predictions": stats.pop("recent_predictions"),
            "symbols": symbols,
            "filters": {
                "symbol": symbol or "",
                "outcome": outcome or "",
                "start_date": start_date or "",
                "end_date": end_date or "",
            },
            **stats,
        },
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


@app.get("/hypotheses", response_class=HTMLResponse)
def hypotheses(request: Request, _user: str = Depends(require_auth), settings: Settings = Depends(get_settings)):
    with get_session(settings) as session:
        rows = load_recent_hypotheses(session, limit=200)
        latest_beliefs = load_latest_beliefs_by_hypothesis(session, [h.id for h in rows])
    return _templates.TemplateResponse(
        request, "hypotheses.html", {"hypotheses": rows, "latest_beliefs": latest_beliefs}
    )


@app.get("/anticipatory-loop-config", response_class=HTMLResponse)
def anticipatory_loop_config_view(request: Request, _user: str = Depends(require_auth), settings: Settings = Depends(get_settings)):
    with get_session(settings) as session:
        config = get_anticipatory_loop_config(session)
    return _templates.TemplateResponse(request, "anticipatory_loop_config.html", {"config": config, "saved": False})


@app.post("/anticipatory-loop-config", response_class=HTMLResponse)
def anticipatory_loop_config_update(
    request: Request,
    _user: str = Depends(require_auth),
    settings: Settings = Depends(get_settings),
    enabled: bool = Form(False),
    poll_seconds: int = Form(...),
    min_gap_threshold: float = Form(...),
    max_open_hypotheses: int = Form(...),
    discovery_limit: int = Form(...),
):
    with get_session(settings) as session:
        config = update_anticipatory_loop_config(
            session,
            enabled=enabled,
            poll_seconds=poll_seconds,
            min_gap_threshold=min_gap_threshold,
            max_open_hypotheses=max_open_hypotheses,
            discovery_limit=discovery_limit,
        )
    return _templates.TemplateResponse(request, "anticipatory_loop_config.html", {"config": config, "saved": True})


@app.get("/health")
def health():
    return {"status": "ok"}
