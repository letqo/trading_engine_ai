import base64

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

import engine.journal.models  # noqa: F401  registers tables on SQLModel.metadata
from engine.config.settings import Settings, get_settings
from engine.dashboard.app import app


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
        "/predict-loop-config",
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
