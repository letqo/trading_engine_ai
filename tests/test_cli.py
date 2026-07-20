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


class _FakePredictionClient:
    """Stands in for ConsequencePredictionClient: never calls the real API,
    never finds any impacts, so predict-loop's cycle body runs for real but
    produces zero predictions -- enough to exercise the loop's own control
    flow (kill switch, drawdown halt, day rollover, max_iterations)."""

    model = "claude-opus-4-8"

    from datetime import date as _date
    knowledge_cutoff = _date(2026, 1, 31)

    def __init__(self, settings):
        pass

    def is_forward_safe(self, decision_timestamp):
        return True

    def analyze(self, headline, tracked_symbols, past_cases=None):
        from engine.prediction.schema import ConsequenceAnalysis
        return ConsequenceAnalysis(impacts=[], overall_reasoning="none")


class _FakeAlpacaClient:
    def __init__(self, settings):
        self.canceled = False
        self.closed = False

    def get_account_equity(self):
        return 10_000.0

    def get_positions(self):
        return {}

    def get_open_orders(self):
        return []

    def submit_order(self, order):
        raise NotImplementedError

    def cancel_all_orders(self):
        self.canceled = True

    def close_all_positions(self):
        self.closed = True


def test_papertrade_rejects_backtest_only_strategy(monkeypatch, tmp_path):
    _isolated_env(monkeypatch, tmp_path)
    result = runner.invoke(app, ["papertrade", "--strategy", "buy_and_hold", "--max-iterations", "1"])
    assert result.exit_code == 1


def test_papertrade_with_strategy_runs_live_cycle_cleanly(monkeypatch, tmp_path):
    _isolated_env(monkeypatch, tmp_path)
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_API_SECRET", "secret")
    get_settings.cache_clear()
    fake_broker = _FakeAlpacaClient(get_settings())
    monkeypatch.setattr("engine.cli.main.AlpacaPaperClient", lambda settings: fake_broker)
    monkeypatch.setattr("engine.execution.live_loop.fetch_bars", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr("engine.execution.live_loop.fetch_all_rss", lambda: [])

    result = runner.invoke(app, ["papertrade", "--strategy", "momentum", "--max-iterations", "2", "--poll-seconds", "0"])
    assert result.exit_code == 0, result.stdout
    assert "papertrade worker stopped" in result.stdout


def test_papertrade_without_strategy_still_runs_skeleton_only(monkeypatch, tmp_path):
    _isolated_env(monkeypatch, tmp_path)
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_API_SECRET", "secret")
    get_settings.cache_clear()
    fake_broker = _FakeAlpacaClient(get_settings())
    monkeypatch.setattr("engine.cli.main.AlpacaPaperClient", lambda settings: fake_broker)

    result = runner.invoke(app, ["papertrade", "--max-iterations", "1", "--poll-seconds", "0"])
    assert result.exit_code == 0, result.stdout
    assert "papertrade worker stopped" in result.stdout


def test_predict_loop_refuses_without_anthropic_key(monkeypatch, tmp_path):
    _isolated_env(monkeypatch, tmp_path)
    result = runner.invoke(app, ["predict-loop", "--max-iterations", "1"])
    assert result.exit_code == 1


def test_predict_loop_runs_log_only_without_alpaca_key(monkeypatch, tmp_path):
    _isolated_env(monkeypatch, tmp_path)
    monkeypatch.setattr("engine.cli.main.ConsequencePredictionClient", _FakePredictionClient)
    monkeypatch.setattr("engine.cli.main.fetch_all_rss", lambda: [])

    result = runner.invoke(app, ["predict-loop", "--max-iterations", "2", "--poll-seconds", "0"])
    assert result.exit_code == 0, result.stdout
    assert "predict-loop stopped" in result.stdout


def test_predict_loop_stops_immediately_when_kill_switch_engaged(monkeypatch, tmp_path):
    halt_path = _isolated_env(monkeypatch, tmp_path)
    halt_path.write_text("halted for test\n")
    monkeypatch.setattr("engine.cli.main.ConsequencePredictionClient", _FakePredictionClient)
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_API_SECRET", "secret")
    get_settings.cache_clear()
    fake_broker = _FakeAlpacaClient(get_settings())
    monkeypatch.setattr("engine.cli.main.AlpacaPaperClient", lambda settings: fake_broker)

    result = runner.invoke(app, ["predict-loop", "--max-iterations", "5", "--poll-seconds", "0"])
    assert result.exit_code == 0, result.stdout
    assert fake_broker.canceled is True
    assert fake_broker.closed is True


def test_predict_loop_respects_max_iterations_in_log_only_mode(monkeypatch, tmp_path):
    _isolated_env(monkeypatch, tmp_path)
    monkeypatch.setattr("engine.cli.main.ConsequencePredictionClient", _FakePredictionClient)
    calls = []
    monkeypatch.setattr("engine.cli.main.fetch_all_rss", lambda: (calls.append(1), [])[1])

    result = runner.invoke(app, ["predict-loop", "--max-iterations", "3", "--poll-seconds", "0"])
    assert result.exit_code == 0, result.stdout
    assert len(calls) == 3
