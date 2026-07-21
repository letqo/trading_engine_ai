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
    for path in ("/", "/predictions", "/trades", "/ticker-suggestions", "/backtests", "/risk-events"):
        resp = client.get(path, headers=_auth_header("admin", "testpass"))
        assert resp.status_code == 200, f"{path} returned {resp.status_code}: {resp.text[:300]}"


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
