import base64
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

import engine.journal.models  # noqa: F401  registers tables on SQLModel.metadata
from engine.config.settings import Settings, get_settings
from engine.dashboard.app import app
from engine.journal.models import HypothesisAction, PredictionDirection, PredictionStatus
from engine.journal.registry import create_hypothesis, record_hypothesis_belief, record_prediction


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
        "/predict-loop-config", "/hypotheses", "/anticipatory-loop-config",
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
            data={"poll_seconds": "1800", "min_gap_threshold": "0.1", "max_open_hypotheses": "5", "discovery_limit": "15"},
        )
        assert resp.status_code == 200
        assert "1800" in resp.text
        assert "checked" not in resp.text.split('name="enabled"')[1].split(">")[0]  # omitted -> disabled

        resp2 = client.get("/anticipatory-loop-config", headers=auth)
        assert 'value="1800"' in resp2.text
        assert 'value="5"' in resp2.text
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
