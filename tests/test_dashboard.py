import base64
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

import engine.journal.models  # noqa: F401  registers tables on SQLModel.metadata
from engine.config.settings import Settings, get_settings
from engine.dashboard.app import app
from engine.journal.models import HypothesisAction, PredictionDirection, PredictionStatus
from engine.journal.registry import (
    create_hypothesis,
    get_anticipatory_loop_config,
    get_predict_loop_config,
    record_halt,
    record_hypothesis_belief,
    record_prediction,
)


def _auth_header(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "dashboard_test.db"
    db_url = f"sqlite:///{db_path}"
    create_engine(db_url).dispose()  # create the file
    engine_ = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine_)

    test_settings = Settings(database_url=db_url, dashboard_password="testpass")
    app.dependency_overrides[get_settings] = lambda: test_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_rejects_missing_credentials(client):
    resp = client.get("/")
    assert resp.status_code == 401


def test_rejects_wrong_password(client):
    resp = client.get("/", headers=_auth_header("admin", "wrong"))
    assert resp.status_code == 401


def test_all_routes_render_with_empty_database(client):
    for path in (
        "/", "/predictions", "/trades", "/ticker-suggestions", "/backtests", "/risk-events",
        "/predict-loop-config", "/hypotheses", "/anticipatory-loop-config", "/risk-gate-config",
    ):
        resp = client.get(path, headers=_auth_header("admin", "testpass"))
        assert resp.status_code == 200, f"{path} returned {resp.status_code}: {resp.text[:300]}"


def test_predict_loop_config_get_requires_auth(client):
    resp = client.get("/predict-loop-config")
    assert resp.status_code == 401


def test_predict_loop_config_post_requires_auth(client):
    resp = client.post("/predict-loop-config", data={
        "poll_seconds": "3600", "rotation_hours": "1", "headlines_per_source": "10",
        "near_dup_window_hours": "48", "near_dup_threshold": "90",
    })
    assert resp.status_code == 401


def test_predict_loop_config_post_updates_the_row(client):
    resp = client.post(
        "/predict-loop-config",
        headers=_auth_header("admin", "testpass"),
        data={
            "enabled": "on",
            "poll_seconds": "1800",
            "rotation_hours": "2",
            "headlines_per_source": "7",
            "near_dup_window_hours": "24",
            "near_dup_threshold": "85",
        },
    )
    assert resp.status_code == 200
    assert "1800" in resp.text
    assert "checked" in resp.text.split('name="enabled"')[1].split(">")[0]

    # A follow-up GET must reflect the saved values, not stale defaults.
    resp2 = client.get("/predict-loop-config", headers=_auth_header("admin", "testpass"))
    assert 'value="1800"' in resp2.text
    assert 'value="7"' in resp2.text


def test_predict_loop_config_post_omitting_enabled_saves_as_disabled(client):
    # An unchecked HTML checkbox sends no field at all -- the route must
    # treat that as enabled=False, not error or silently keep the old value.
    resp = client.post(
        "/predict-loop-config",
        headers=_auth_header("admin", "testpass"),
        data={
            "poll_seconds": "3600", "rotation_hours": "1", "headlines_per_source": "10",
            "near_dup_window_hours": "48", "near_dup_threshold": "90",
        },
    )
    assert resp.status_code == 200
    assert "checked" not in resp.text.split('name="enabled"')[1].split(">")[0]


def test_health_check_requires_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_prediction_stats_match_hand_computed_values_and_rationale_renders(tmp_path):
    # Proves the SQL-aggregate rewrite of _prediction_stats produces the
    # same numbers the old full-table Python scan would have -- not just
    # that it doesn't crash -- and that rationale (previously never
    # rendered) shows up on /predictions.
    db_path = tmp_path / "dashboard_stats_test.db"
    db_url = f"sqlite:///{db_path}"
    engine_ = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine_)

    now = datetime.now(timezone.utc)

    def seed(session, *, outcome_correct, mfe, mae, forward_safe=True, status=PredictionStatus.RESOLVED):
        pred = record_prediction(
            session,
            news_headline="h", news_source="rss", news_published_at=now, news_decision_timestamp=now,
            topics=[], symbol="EWJ", direction="up", confidence=0.5,
            rationale="rate hike strengthens yen, hurts exporters",
            model_name="test", model_knowledge_cutoff=now, forward_safe=forward_safe,
            resolution_window_hours=24.0, in_tracked_universe=True,
        )
        pred.status = status
        pred.outcome_correct = outcome_correct
        pred.mfe_pct = mfe
        pred.mae_pct = mae
        session.add(pred)
        session.commit()

    with Session(engine_) as session:
        seed(session, outcome_correct=True, mfe=5.0, mae=1.0)
        seed(session, outcome_correct=False, mfe=2.0, mae=6.0)
        seed(session, outcome_correct=True, mfe=3.0, mae=3.0)
        seed(session, outcome_correct=True, mfe=1.0, mae=1.0, forward_safe=False)  # excluded

    test_settings = Settings(database_url=db_url, dashboard_password="testpass")
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        client = TestClient(app)
        resp = client.get("/", headers=_auth_header("admin", "testpass"))
        assert resp.status_code == 200
        assert "66.7%" in resp.text  # accuracy: 2 of 3 forward_safe+resolved correct

        resp2 = client.get("/predictions", headers=_auth_header("admin", "testpass"))
        assert resp2.status_code == 200
        assert "66.7%" in resp2.text
        assert "rate hike strengthens yen, hurts exporters" in resp2.text
    finally:
        app.dependency_overrides.clear()


def test_predictions_page_filters_by_symbol_outcome_and_date(tmp_path):
    db_path = tmp_path / "dashboard_filters_test.db"
    db_url = f"sqlite:///{db_path}"
    engine_ = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine_)

    now = datetime.now(timezone.utc)

    def seed(session, *, symbol, headline, outcome_correct, decision_ts):
        pred = record_prediction(
            session,
            news_headline=headline, news_source="rss", news_published_at=decision_ts,
            news_decision_timestamp=decision_ts,
            topics=[], symbol=symbol, direction="up", confidence=0.5,
            rationale="r", model_name="test", model_knowledge_cutoff=now, forward_safe=True,
            resolution_window_hours=24.0, in_tracked_universe=True,
        )
        pred.status = PredictionStatus.RESOLVED
        pred.outcome_correct = outcome_correct
        session.add(pred)
        session.commit()

    with Session(engine_) as session:
        seed(session, symbol="EWJ", headline="ewj headline old", outcome_correct=True,
             decision_ts=now - timedelta(days=10))
        seed(session, symbol="SPY", headline="spy headline recent", outcome_correct=False,
             decision_ts=now - timedelta(days=1))

    test_settings = Settings(database_url=db_url, dashboard_password="testpass")
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        client = TestClient(app)
        auth = _auth_header("admin", "testpass")

        resp = client.get("/predictions?symbol=EWJ", headers=auth)
        assert "ewj headline old" in resp.text
        assert "spy headline recent" not in resp.text

        resp = client.get("/predictions?outcome=incorrect", headers=auth)
        assert "spy headline recent" in resp.text
        assert "ewj headline old" not in resp.text

        start_date = (now - timedelta(days=2)).strftime("%Y-%m-%d")
        resp = client.get(f"/predictions?start_date={start_date}", headers=auth)
        assert "spy headline recent" in resp.text
        assert "ewj headline old" not in resp.text

        # Unfiltered: both present, symbol dropdown lists both.
        resp = client.get("/predictions", headers=auth)
        assert "ewj headline old" in resp.text
        assert "spy headline recent" in resp.text
        assert '<option value="EWJ"' in resp.text
        assert '<option value="SPY"' in resp.text
    finally:
        app.dependency_overrides.clear()


def test_hypotheses_page_requires_auth(tmp_path):
    db_path = tmp_path / "dashboard_hyp_auth.db"
    engine_ = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine_)
    test_settings = Settings(database_url=f"sqlite:///{db_path}", dashboard_password="testpass")
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        resp = TestClient(app).get("/hypotheses")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_hypotheses_page_renders_symbol_and_latest_belief(tmp_path):
    db_path = tmp_path / "dashboard_hyp_test.db"
    db_url = f"sqlite:///{db_path}"
    engine_ = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine_)

    with Session(engine_) as session:
        hyp = create_hypothesis(
            session, market_id="m1", question="Will the Fed cut rates?", symbol="XLE",
            direction_if_yes=PredictionDirection.UP,
        )
        record_hypothesis_belief(
            session, hyp, p_model=0.7, p_market=0.5, confidence=0.6,
            rationale="fed cut would boost energy demand", action=HypothesisAction.OPENED,
        )

    test_settings = Settings(database_url=db_url, dashboard_password="testpass")
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        resp = TestClient(app).get("/hypotheses", headers=_auth_header("admin", "testpass"))
        assert resp.status_code == 200
        assert "Will the Fed cut rates?" in resp.text
        assert "XLE" in resp.text
        assert "fed cut would boost energy demand" in resp.text
        assert "opened" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_anticipatory_loop_config_get_requires_auth(tmp_path):
    db_path = tmp_path / "dashboard_ant_auth.db"
    engine_ = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine_)
    test_settings = Settings(database_url=f"sqlite:///{db_path}", dashboard_password="testpass")
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        resp = TestClient(app).get("/anticipatory-loop-config")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_anticipatory_loop_config_post_updates_the_row(tmp_path):
    db_path = tmp_path / "dashboard_ant_test.db"
    db_url = f"sqlite:///{db_path}"
    engine_ = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine_)

    test_settings = Settings(database_url=db_url, dashboard_password="testpass")
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        client = TestClient(app)
        auth = _auth_header("admin", "testpass")
        resp = client.post(
            "/anticipatory-loop-config", headers=auth,
            data={
                "poll_seconds": "1800", "min_gap_threshold": "0.1", "max_open_hypotheses": "5",
                "max_open_hypotheses_per_symbol": "3", "discovery_limit": "15",
            },
        )
        assert resp.status_code == 200
        assert "1800" in resp.text
        assert "checked" not in resp.text.split('name="enabled"')[1].split(">")[0]  # omitted -> disabled

        resp2 = client.get("/anticipatory-loop-config", headers=auth)
        assert 'value="1800"' in resp2.text
        assert 'value="5"' in resp2.text
    finally:
        app.dependency_overrides.clear()


def test_risk_gate_config_get_requires_auth(client):
    resp = client.get("/risk-gate-config")
    assert resp.status_code == 401


def test_risk_gate_config_post_requires_auth(client):
    resp = client.post("/risk-gate-config", data={
        "max_capital_per_position_pct": "0.05", "max_total_exposure_pct": "0.2",
        "stop_loss_pct": "0.02", "max_daily_drawdown_pct": "0.03", "max_consecutive_losses_per_day": "4",
    })
    assert resp.status_code == 401


def test_risk_gate_config_defaults_to_use_defaults_true(client):
    resp = client.get("/risk-gate-config", headers=_auth_header("admin", "testpass"))
    assert resp.status_code == 200
    assert "checked" in resp.text.split('name="use_defaults"')[1].split(">")[0]


def test_risk_gate_config_post_updates_the_row_and_unchecking_use_defaults_persists(client):
    auth = _auth_header("admin", "testpass")
    resp = client.post(
        "/risk-gate-config",
        headers=auth,
        data={
            "max_capital_per_position_pct": "0.1",
            "max_total_exposure_pct": "0.3",
            "stop_loss_pct": "0.04",
            "max_daily_drawdown_pct": "0.05",
            "max_consecutive_losses_per_day": "6",
        },
    )
    assert resp.status_code == 200
    assert 'value="0.1"' in resp.text
    assert "checked" not in resp.text.split('name="use_defaults"')[1].split(">")[0]  # omitted -> False

    # A follow-up GET must reflect the saved override, not stale defaults.
    resp2 = client.get("/risk-gate-config", headers=auth)
    assert 'value="0.1"' in resp2.text
    assert 'value="6"' in resp2.text


def test_risk_gate_config_post_omitting_use_defaults_and_overnight_saves_as_false(client):
    resp = client.post(
        "/risk-gate-config",
        headers=_auth_header("admin", "testpass"),
        data={
            "use_defaults": "on",
            "max_capital_per_position_pct": "0.05", "max_total_exposure_pct": "0.2",
            "stop_loss_pct": "0.02", "max_daily_drawdown_pct": "0.03", "max_consecutive_losses_per_day": "4",
        },
    )
    assert resp.status_code == 200
    assert "checked" in resp.text.split('name="use_defaults"')[1].split(">")[0]
    assert "checked" not in resp.text.split('name="allow_overnight_positions"')[1].split(">")[0]


def _status_settings(tmp_path, db_url, halted=False):
    # _env_file=None: this machine's local .env (real ALPACA_API_KEY etc.)
    # must not leak into these tests -- Settings otherwise reads it by
    # default, same isolation pattern as tests/test_prediction_client.py.
    halt_file = tmp_path / "HALT"
    if halted:
        halt_file.write_text("halted for test\n")
    return Settings(_env_file=None, database_url=db_url, dashboard_password="testpass", halt_file=halt_file)


def test_overview_shows_kill_switch_engaged(tmp_path):
    db_path = tmp_path / "status_kill_switch.db"
    db_url = f"sqlite:///{db_path}"
    engine_ = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine_)

    app.dependency_overrides[get_settings] = lambda: _status_settings(tmp_path, db_url, halted=True)
    try:
        resp = TestClient(app).get("/", headers=_auth_header("admin", "testpass"))
        assert resp.status_code == 200
        assert "ENGAGED" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_overview_shows_kill_switch_clear_by_default(tmp_path):
    db_path = tmp_path / "status_clear.db"
    db_url = f"sqlite:///{db_path}"
    engine_ = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine_)

    app.dependency_overrides[get_settings] = lambda: _status_settings(tmp_path, db_url, halted=False)
    try:
        resp = TestClient(app).get("/", headers=_auth_header("admin", "testpass"))
        assert "ENGAGED" not in resp.text
        assert "clear" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_overview_shows_predict_loop_paused(tmp_path):
    db_path = tmp_path / "status_paused.db"
    db_url = f"sqlite:///{db_path}"
    engine_ = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine_)

    with Session(engine_) as session:
        # A paused loop still heartbeats every iteration in production
        # (mark_predict_loop_cycle runs before the enabled check) -- so a
        # realistic "paused but alive" row needs a fresh heartbeat too,
        # not just enabled=False. Without one, last_cycle_at is None and
        # correctly reads as STALE ("we don't know if this ever ran"),
        # not "paused" -- that's the code's actual, defensible behavior.
        config = get_predict_loop_config(session)
        config.enabled = False
        config.last_cycle_at = datetime.now(timezone.utc)
        session.add(config)
        session.commit()

    app.dependency_overrides[get_settings] = lambda: _status_settings(tmp_path, db_url)
    try:
        resp = TestClient(app).get("/", headers=_auth_header("admin", "testpass"))
        assert "paused" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_overview_shows_stale_when_heartbeat_is_old(tmp_path):
    db_path = tmp_path / "status_stale.db"
    db_url = f"sqlite:///{db_path}"
    engine_ = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine_)

    with Session(engine_) as session:
        config = get_predict_loop_config(session)
        config.enabled = True
        # Default poll_seconds is 3600 -- 5 hours old is well past the 2x threshold.
        config.last_cycle_at = datetime.now(timezone.utc) - timedelta(hours=5)
        session.add(config)
        session.commit()

    app.dependency_overrides[get_settings] = lambda: _status_settings(tmp_path, db_url)
    try:
        resp = TestClient(app).get("/", headers=_auth_header("admin", "testpass"))
        assert "STALE" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_overview_shows_running_when_heartbeat_is_fresh(tmp_path):
    db_path = tmp_path / "status_running.db"
    db_url = f"sqlite:///{db_path}"
    engine_ = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine_)

    with Session(engine_) as session:
        # Both loops seeded fresh -- predict-loop defaults to enabled=True
        # with no heartbeat yet, which is legitimately STALE, so it must
        # also be given a fresh heartbeat here or it'd contaminate the
        # "nothing stale" assertion below with an unrelated true STALE.
        get_predict_loop_config(session).last_cycle_at = datetime.now(timezone.utc)
        config = get_anticipatory_loop_config(session)
        config.enabled = True
        config.last_cycle_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        session.add(config)
        session.commit()

    app.dependency_overrides[get_settings] = lambda: _status_settings(tmp_path, db_url)
    try:
        resp = TestClient(app).get("/", headers=_auth_header("admin", "testpass"))
        assert "running" in resp.text
        assert "STALE" not in resp.text
    finally:
        app.dependency_overrides.clear()


def test_overview_shows_alpaca_not_configured(tmp_path):
    db_path = tmp_path / "status_no_alpaca.db"
    db_url = f"sqlite:///{db_path}"
    engine_ = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine_)

    app.dependency_overrides[get_settings] = lambda: _status_settings(tmp_path, db_url)
    try:
        resp = TestClient(app).get("/", headers=_auth_header("admin", "testpass"))
        assert "not set" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_overview_shows_recent_halts(tmp_path):
    db_path = tmp_path / "status_halts.db"
    db_url = f"sqlite:///{db_path}"
    engine_ = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine_)

    with Session(engine_) as session:
        record_halt(session, reason="daily drawdown breached", account_equity=9000.0, triggered_by="daily_drawdown")

    app.dependency_overrides[get_settings] = lambda: _status_settings(tmp_path, db_url)
    try:
        resp = TestClient(app).get("/", headers=_auth_header("admin", "testpass"))
        assert "Recent halts" in resp.text
        assert "daily_drawdown" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_refuses_to_serve_when_password_unset(tmp_path):
    db_path = tmp_path / "dashboard_nopass.db"
    db_url = f"sqlite:///{db_path}"
    engine_ = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine_)

    test_settings = Settings(database_url=db_url, dashboard_password=None)
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        resp = TestClient(app).get("/", headers=_auth_header("admin", "anything"))
        assert resp.status_code == 503
    finally:
        app.dependency_overrides.clear()
