import pandas as pd
from sqlmodel import SQLModel
from typer.testing import CliRunner

import engine.journal.models  # noqa: F401  registers tables on SQLModel.metadata
from engine.cli.main import app
from engine.config.settings import get_settings
from engine.journal.db import _engine_for_url

runner = CliRunner()


def _isolated_env(monkeypatch, tmp_path):
    db_path = tmp_path / "cli_test.db"
    halt_path = tmp_path / "HALT"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("HALT_FILE", str(halt_path))
    get_settings.cache_clear()
    _engine_for_url.cache_clear()
    SQLModel.metadata.create_all(_engine_for_url(db_url))
    return halt_path


def test_report_with_no_runs(monkeypatch, tmp_path):
    _isolated_env(monkeypatch, tmp_path)
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "no runs registered yet" in result.stdout


def test_kill_then_kill_reset_toggles_flag_file(monkeypatch, tmp_path):
    halt_path = _isolated_env(monkeypatch, tmp_path)
    result = runner.invoke(app, ["kill"])
    assert result.exit_code == 0
    assert halt_path.exists()

    result = runner.invoke(app, ["kill-reset"])
    assert result.exit_code == 0
    assert not halt_path.exists()


def test_backtest_end_to_end_with_mocked_data(monkeypatch, tmp_path):
    _isolated_env(monkeypatch, tmp_path)

    def fake_fetch_bars(symbols, start, end, interval="1d"):
        import datetime as dt

        rows = []
        for s in symbols[:2]:  # keep it fast
            for i in range(5):
                price = 100.0 + i
                rows.append({
                    "symbol": s,
                    "timestamp": pd.Timestamp(dt.datetime(2026, 1, 5 + i, tzinfo=dt.timezone.utc)),
                    "open": price, "high": price, "low": price, "close": price,
                    "volume": 1000, "timeframe": interval,
                })
        return pd.DataFrame(rows)

    monkeypatch.setattr("engine.cli.main.fetch_bars", fake_fetch_bars)

    result = runner.invoke(
        app, ["backtest", "--strategy", "buy_and_hold", "--start", "2026-01-05", "--end", "2026-01-10"]
    )
    assert result.exit_code == 0, result.stdout
    assert "buy_and_hold" in result.stdout

    report_result = runner.invoke(app, ["report"])
    assert "buy_and_hold" in report_result.stdout


def test_backtest_rejects_unknown_strategy(monkeypatch, tmp_path):
    _isolated_env(monkeypatch, tmp_path)
    result = runner.invoke(
        app, ["backtest", "--strategy", "not_a_real_strategy", "--start", "2026-01-01", "--end", "2026-01-02"]
    )
    assert result.exit_code == 1
