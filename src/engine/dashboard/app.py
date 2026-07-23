"""Reporting + manual-trading dashboard.

Every order submitted from here -- raw manual trades, Prediction/Hypothesis
conversions, and closes (the /manual-trade* routes) -- goes through the
exact same RiskGate.evaluate() -> broker.submit_order() ->
apply_opening_fill/apply_closing_fill -> mark_* path as predict-loop/
anticipatory-loop (SPEC.md hard constraint #2, never bypassed). See
engine.execution.manual_trading and engine.prediction.trading
.open_prediction_trade / engine.anticipatory.trading.open_hypothesis_trade.

Auth is unchanged: a single shared HTTP Basic password
(engine.dashboard.auth), originally proportionate for a read-only
reporting view. That is no longer strictly true now that anyone with the
password can place and close real (paper) orders directly -- a deliberate
call-out, not a silent change; revisit if the trust model ever needs to
change (per-user credentials, etc.).

Manual orders share RiskGate's position/exposure caps with the automatic
loops (recomputed fresh from live broker state every request), but NOT the
automatic loops' in-memory daily-drawdown/consecutive-loss halt state --
AccountState is process-local and never persisted (see
engine.execution.reconcile), and this is the first entrypoint that reaches
it from a separate process than the one that set it. Manual *opens* are
hard-blocked while the kill switch is engaged, as partial mitigation
(reuses engine.risk.kill_switch.is_kill_switch_engaged, same check the
loops use); manual *closes* are never blocked on it, since closing a
position is exactly what you want to be able to do while other trading is
halted.

The /predict-loop-config, /anticipatory-loop-config, and /risk-gate-config
GET/POST pairs (poll interval, thresholds, pause/resume, position/
exposure/drawdown caps) predate the /manual-trade* routes and are unaffected.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func
from sqlmodel import select

from engine.anticipatory.trading import MAX_SEVERITY, open_hypothesis_trade
from engine.config.guard import PaperOnlyViolation, enforce_paper_only
from engine.config.settings import Settings, get_settings
from engine.dashboard.auth import require_auth
from engine.data.universe import Universe, load_universe
from engine.execution.alpaca import AlpacaPaperClient
from engine.execution.broker import Broker
from engine.execution.manual_trading import close_any_position, open_manual_trade
from engine.execution.pricing import latest_price
from engine.execution.reconcile import reconcile_account_state
from engine.journal.db import get_session
from engine.journal.models import Hypothesis, Prediction, PredictionStatus
from engine.journal.registry import (
    find_open_trade_by_symbol,
    get_anticipatory_loop_config,
    get_predict_loop_config,
    get_risk_gate_config,
    load_latest_beliefs_by_hypothesis,
    load_off_universe_symbol_stats,
    load_prediction_trades,
    load_recent_experiment_runs,
    load_recent_hypotheses,
    load_recent_hypothesis_trade_rejections,
    load_recent_manual_trades,
    load_recent_risk_halts,
    load_recent_trade_rejections,
    update_anticipatory_loop_config,
    update_predict_loop_config,
    update_risk_gate_config,
)
from engine.prediction.trading import open_prediction_trade
from engine.risk.gate import RiskGate
from engine.risk.kill_switch import is_kill_switch_engaged
from engine.risk.models import AccountState, Side
from engine.risk.resolve import resolve_risk_limits

app = FastAPI(title="Trading engine dashboard")
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_broker(settings: Settings = Depends(get_settings)) -> Broker:
    enforce_paper_only(settings)
    return AlpacaPaperClient(settings)


def _trading_context(settings: Settings, broker: Broker) -> tuple[RiskGate, AccountState, Universe]:
    """RiskGate + a fresh AccountState + Universe, built the same way every
    live trading path in engine.cli.main builds them -- see this module's
    docstring for the cross-process halt-state caveat."""
    with get_session(settings) as session:
        risk_gate = RiskGate(resolve_risk_limits(settings, get_risk_gate_config(session)))
    account = reconcile_account_state(broker)
    universe = load_universe(settings.universe_path)
    return risk_gate, account, universe


def _redirect(path: str, *, ok: bool, msg: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?ok={ok}&msg={quote(msg)}", status_code=303)


def _convert_blocked_reason(kind: str, row: Prediction | Hypothesis) -> str | None:
    """Shared between the convert GET (advisory display) and POST (actual
    enforcement) so the two can never drift apart."""
    if row.trade_rejected:
        return f"broker already rejected this {kind}: {row.trade_rejection_reason}"
    if kind == "prediction":
        if row.traded_order_id is not None or row.exit_order_id is not None:
            return "already traded or exited"
        if row.status != PredictionStatus.PENDING:
            return "prediction is no longer pending (already resolved/invalid)"
        if not row.forward_safe:
            return "forward_safe is False -- blocked even for manual trades (hindsight-leakage integrity, not a quality bar)"
    else:
        if row.position_side is not None:
            return "already has an open position"
        if row.status.value != "open":
            return "hypothesis market is closed"
    return None


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


def _format_ago(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = max((now - dt).total_seconds(), 0)
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


def _loop_status(config, now: datetime) -> dict:
    """A loop is "stale" if it hasn't heartbeated within 2x its own poll
    interval -- generous enough to absorb a slow cycle or a redeploy, but
    catches a real crash-loop that Railway's ON_FAILURE restart policy
    would otherwise mask as still "Online"."""
    last_cycle_at = config.last_cycle_at
    if last_cycle_at is not None and last_cycle_at.tzinfo is None:
        last_cycle_at = last_cycle_at.replace(tzinfo=timezone.utc)
    stale = last_cycle_at is None or (now - last_cycle_at).total_seconds() > 2 * config.poll_seconds
    return {
        "enabled": config.enabled,
        "stale": stale,
        "last_cycle_display": _format_ago(config.last_cycle_at, now),
    }


def _system_status(settings: Settings) -> dict:
    """Answers "is everything green for trading to happen right now" --
    the dashboard's own visibility into the two long-running loops and
    the kill switch, not just the read-only account snapshot below."""
    now = datetime.now(timezone.utc)
    with get_session(settings) as session:
        # Computed while still inside the session -- the config rows'
        # attributes expire once the session closes (DetachedInstanceError
        # on access), so _loop_status must run before that, not after.
        predict_loop_status = _loop_status(get_predict_loop_config(session), now)
        anticipatory_loop_status = _loop_status(get_anticipatory_loop_config(session), now)
        recent_halts = list(load_recent_risk_halts(session, limit=5))
        recent_rejected_predictions = list(load_recent_trade_rejections(session, limit=5))
        recent_rejected_hypotheses = list(load_recent_hypothesis_trade_rejections(session, limit=5))
    return {
        "kill_switch_engaged": is_kill_switch_engaged(settings),
        "alpaca_configured": bool(settings.alpaca_api_key and settings.alpaca_api_secret),
        "predict_loop": predict_loop_status,
        "anticipatory_loop": anticipatory_loop_status,
        "recent_halts": recent_halts,
        "recent_rejected_predictions": recent_rejected_predictions,
        "recent_rejected_hypotheses": recent_rejected_hypotheses,
    }


@app.get("/", response_class=HTMLResponse)
def overview(request: Request, _user: str = Depends(require_auth), settings: Settings = Depends(get_settings)):
    stats = _prediction_stats(settings)
    status = _system_status(settings)

    equity_display = "n/a"
    open_positions_count = 0
    positions = []
    positions_note = "ALPACA_API_KEY not set -- account state unavailable."
    if settings.alpaca_api_key and settings.alpaca_api_secret:
        try:
            enforce_paper_only(settings)
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
            "status": status,
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
    max_open_hypotheses_per_symbol: int = Form(...),
    discovery_limit: int = Form(...),
):
    with get_session(settings) as session:
        config = update_anticipatory_loop_config(
            session,
            enabled=enabled,
            poll_seconds=poll_seconds,
            min_gap_threshold=min_gap_threshold,
            max_open_hypotheses=max_open_hypotheses,
            max_open_hypotheses_per_symbol=max_open_hypotheses_per_symbol,
            discovery_limit=discovery_limit,
        )
    return _templates.TemplateResponse(request, "anticipatory_loop_config.html", {"config": config, "saved": True})


@app.get("/risk-gate-config", response_class=HTMLResponse)
def risk_gate_config_view(request: Request, _user: str = Depends(require_auth), settings: Settings = Depends(get_settings)):
    with get_session(settings) as session:
        config = get_risk_gate_config(session)
    return _templates.TemplateResponse(
        request, "risk_gate_config.html", {"config": config, "env_defaults": settings.risk, "saved": False}
    )


@app.post("/risk-gate-config", response_class=HTMLResponse)
def risk_gate_config_update(
    request: Request,
    _user: str = Depends(require_auth),
    settings: Settings = Depends(get_settings),
    use_defaults: bool = Form(False),
    max_capital_per_position_pct: float = Form(...),
    max_total_exposure_pct: float = Form(...),
    stop_loss_pct: float = Form(...),
    max_daily_drawdown_pct: float = Form(...),
    max_consecutive_losses_per_day: int = Form(...),
    allow_overnight_positions: bool = Form(False),
):
    with get_session(settings) as session:
        config = update_risk_gate_config(
            session,
            use_defaults=use_defaults,
            max_capital_per_position_pct=max_capital_per_position_pct,
            max_total_exposure_pct=max_total_exposure_pct,
            stop_loss_pct=stop_loss_pct,
            max_daily_drawdown_pct=max_daily_drawdown_pct,
            max_consecutive_losses_per_day=max_consecutive_losses_per_day,
            allow_overnight_positions=allow_overnight_positions,
        )
    return _templates.TemplateResponse(
        request, "risk_gate_config.html", {"config": config, "env_defaults": settings.risk, "saved": True}
    )


@app.get("/manual-trade", response_class=HTMLResponse)
def manual_trade_view(
    request: Request,
    _user: str = Depends(require_auth),
    settings: Settings = Depends(get_settings),
    broker: Broker = Depends(get_broker),
    ok: str | None = None,
    msg: str | None = None,
):
    risk_gate, account, universe = _trading_context(settings, broker)
    with get_session(settings) as session:
        positions = []
        for symbol, pos in account.positions.items():
            if pos.quantity == 0:
                continue
            match = find_open_trade_by_symbol(session, symbol)
            positions.append({
                "symbol": symbol,
                "quantity": pos.quantity,
                "avg_entry_price": pos.avg_entry_price,
                "market_value": pos.market_value,
                "attribution": match[0] if match else "unattributed",
            })
        recent_manual_trades = load_recent_manual_trades(session, limit=50)
    return _templates.TemplateResponse(
        request,
        "manual_trade.html",
        {
            "positions": positions,
            "recent_manual_trades": recent_manual_trades,
            "symbols": sorted(universe.tradable_symbols()),
            "equity_display": f"${account.equity:,.2f}",
            "kill_switch_engaged": is_kill_switch_engaged(settings),
            "ok": ok,
            "msg": msg,
        },
    )


@app.post("/manual-trade/raw")
def manual_trade_raw(
    _user: str = Depends(require_auth),
    settings: Settings = Depends(get_settings),
    broker: Broker = Depends(get_broker),
    symbol: str = Form(...),
    side: str = Form(...),
    quantity: float = Form(...),
    note: str = Form(""),
):
    if is_kill_switch_engaged(settings):
        return _redirect("/manual-trade", ok=False, msg="kill switch engaged -- manual opens are blocked")
    risk_gate, account, universe = _trading_context(settings, broker)
    with get_session(settings) as session:
        result = open_manual_trade(
            session, broker, risk_gate, account, universe,
            symbol=symbol.strip().upper(), side=Side(side), quantity=quantity,
            submitted_by=_user, note=note or None,
        )
    return _redirect("/manual-trade", ok=result.ok, msg="submitted" if result.ok else result.reason)


@app.post("/manual-trade/close/{symbol}")
def manual_trade_close(
    symbol: str,
    _user: str = Depends(require_auth),
    settings: Settings = Depends(get_settings),
    broker: Broker = Depends(get_broker),
):
    risk_gate, account, universe = _trading_context(settings, broker)
    with get_session(settings) as session:
        result = close_any_position(session, broker, risk_gate, account, universe, symbol)
    msg = f"closed, attributed to {result.attribution}" if result.ok else result.reason
    return _redirect("/manual-trade", ok=result.ok, msg=msg)


@app.get("/manual-trade/convert/{kind}/{item_id}", response_class=HTMLResponse)
def manual_trade_convert_view(
    request: Request,
    kind: Literal["prediction", "hypothesis"],
    item_id: str,
    _user: str = Depends(require_auth),
    settings: Settings = Depends(get_settings),
    broker: Broker = Depends(get_broker),
    ok: str | None = None,
    msg: str | None = None,
):
    risk_gate, account, universe = _trading_context(settings, broker)
    with get_session(settings) as session:
        belief = None
        if kind == "prediction":
            row = session.get(Prediction, item_id)
            if row is None:
                raise HTTPException(status_code=404, detail="prediction not found")
        else:
            row = session.get(Hypothesis, item_id)
            if row is None:
                raise HTTPException(status_code=404, detail="hypothesis not found")
            belief = load_latest_beliefs_by_hypothesis(session, [row.id]).get(row.id)

        blocked_reason = _convert_blocked_reason(kind, row)
        if kind == "hypothesis" and blocked_reason is None and belief is None:
            blocked_reason = "no belief recorded yet -- nothing to size a trade from"

        price = latest_price(row.symbol)
        suggested_qty = None
        if price:
            cap_value = account.equity * risk_gate.limits.max_capital_per_position_pct
            if kind == "prediction":
                suggested_qty = (cap_value * 2) / price
            elif belief is not None:
                config = get_anticipatory_loop_config(session)
                severity = min(abs(belief.gap) / max(config.min_gap_threshold, 1e-6), MAX_SEVERITY)
                suggested_qty = (cap_value * severity * belief.confidence * 2) / price

    return _templates.TemplateResponse(
        request,
        "manual_trade_convert.html",
        {
            "kind": kind, "row": row, "belief": belief, "blocked_reason": blocked_reason,
            "price": price, "suggested_qty": suggested_qty, "ok": ok, "msg": msg,
        },
    )


@app.post("/manual-trade/convert/{kind}/{item_id}")
def manual_trade_convert_submit(
    kind: Literal["prediction", "hypothesis"],
    item_id: str,
    _user: str = Depends(require_auth),
    settings: Settings = Depends(get_settings),
    broker: Broker = Depends(get_broker),
    override_quantity: float = Form(...),
):
    redirect_path = f"/manual-trade/convert/{kind}/{item_id}"
    if is_kill_switch_engaged(settings):
        return _redirect(redirect_path, ok=False, msg="kill switch engaged -- manual opens are blocked")

    risk_gate, account, universe = _trading_context(settings, broker)
    tradable = universe.tradable_symbols()
    with get_session(settings) as session:
        if kind == "prediction":
            row = session.get(Prediction, item_id)
            if row is None:
                raise HTTPException(status_code=404, detail="prediction not found")
            blocked_reason = _convert_blocked_reason(kind, row)
            if blocked_reason:
                return _redirect(redirect_path, ok=False, msg=blocked_reason)
            result = open_prediction_trade(
                session, broker, risk_gate, account, tradable, row, override_quantity=override_quantity,
            )
        else:
            row = session.get(Hypothesis, item_id)
            if row is None:
                raise HTTPException(status_code=404, detail="hypothesis not found")
            blocked_reason = _convert_blocked_reason(kind, row)
            if blocked_reason:
                return _redirect(redirect_path, ok=False, msg=blocked_reason)
            belief = load_latest_beliefs_by_hypothesis(session, [row.id]).get(row.id)
            if belief is None:
                return _redirect(redirect_path, ok=False, msg="no belief recorded yet")
            config = get_anticipatory_loop_config(session)
            result = open_hypothesis_trade(
                session, broker, risk_gate, account, tradable, row, belief, config.min_gap_threshold,
                override_quantity=override_quantity,
            )
    return _redirect(redirect_path, ok=result.ok, msg="submitted" if result.ok else result.reason)


@app.get("/health")
def health():
    return {"status": "ok"}
