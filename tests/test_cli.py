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
    monkeypatch.setattr("engine.cli.main.build_prediction_client", _FakePredictionClient)
    monkeypatch.setattr("engine.cli.main.fetch_rss_feed", lambda source, url: [])

    result = runner.invoke(app, ["predict-loop", "--max-iterations", "2", "--poll-seconds", "0"])
    assert result.exit_code == 0, result.stdout
    assert "predict-loop stopped" in result.stdout


def test_predict_loop_stops_immediately_when_kill_switch_engaged(monkeypatch, tmp_path):
    halt_path = _isolated_env(monkeypatch, tmp_path)
    halt_path.write_text("halted for test\n")
    monkeypatch.setattr("engine.cli.main.build_prediction_client", _FakePredictionClient)
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
    monkeypatch.setattr("engine.cli.main.build_prediction_client", _FakePredictionClient)
    calls = []
    monkeypatch.setattr("engine.cli.main.fetch_rss_feed", lambda source, url: (calls.append(1), [])[1])

    result = runner.invoke(app, ["predict-loop", "--max-iterations", "3", "--poll-seconds", "0"])
    assert result.exit_code == 0, result.stdout
    assert len(calls) == 3


def test_predict_loop_skips_already_predicted_headlines(monkeypatch, tmp_path):
    # fetch_all_rss has no memory of its own (RSS "top stories" don't
    # necessarily change hour to hour), so predict-loop must dedup by
    # headline itself -- otherwise the same headline would be re-sent to
    # the model (and re-logged as a fresh Prediction) on every cycle it
    # stays on the feed. See headline_already_predicted, JOURNAL.md 2026-07-21.
    from datetime import datetime, timezone

    from engine.prediction.schema import ConsequenceAnalysis, PredictedImpact

    _isolated_env(monkeypatch, tmp_path)

    analyze_calls = []

    class _FakeClientWithOneImpact(_FakePredictionClient):
        def analyze(self, headline, tracked_symbols, past_cases=None):
            analyze_calls.append(headline)
            return ConsequenceAnalysis(
                impacts=[PredictedImpact(symbol="SPY", direction="up", confidence=0.9, rationale="test")],
                overall_reasoning="test",
            )

    monkeypatch.setattr("engine.cli.main.build_prediction_client", _FakeClientWithOneImpact)

    fixed_headline_item = type(
        "FakeRawNewsItem",
        (),
        {
            "id": "fixed-id", "source": "test_feed",
            "published_at": datetime(2026, 7, 21, tzinfo=timezone.utc),
            "ingested_at": datetime(2026, 7, 21, tzinfo=timezone.utc),
            "headline": "Same headline every cycle", "url": None, "raw_payload": {},
        },
    )()
    monkeypatch.setattr("engine.cli.main.fetch_rss_feed", lambda source, url: [fixed_headline_item])
    # tag_and_route normally scores sentiment/topics from real text; patch a
    # minimal pass-through so the pipeline gets a real NewsItem to work with.
    import engine.cli.main as cli_main
    from engine.domain import NewsItem

    def _fake_tag_and_route(raw, universe):
        return NewsItem(
            id=raw.id, source=raw.source, published_at=raw.published_at, ingested_at=raw.ingested_at,
            headline=raw.headline, url=raw.url, raw_payload=raw.raw_payload,
        )

    monkeypatch.setattr(cli_main, "tag_and_route", _fake_tag_and_route)

    result = runner.invoke(app, ["predict-loop", "--max-iterations", "2", "--poll-seconds", "0"])
    assert result.exit_code == 0, result.stdout
    # Same headline on both cycles -- the model should only ever be asked once.
    assert analyze_calls == ["Same headline every cycle"]


def test_predict_loop_uses_active_rss_source_for_rotation(monkeypatch, tmp_path):
    # predict_loop must fetch from whichever single source active_rss_source
    # names each cycle -- not all three sources every cycle -- and pass that
    # source's real RSS_FEEDS URL through unchanged.
    _isolated_env(monkeypatch, tmp_path)
    monkeypatch.setattr("engine.cli.main.build_prediction_client", _FakePredictionClient)

    rotation = ["yahoo_finance_top", "marketwatch_top", "prnewswire_all"]
    calls = []

    def fake_active_source(anchor, now, rotation_hours):
        return rotation[len(calls) % len(rotation)]

    def fake_fetch_rss_feed(source, url):
        calls.append((source, url))
        return []

    monkeypatch.setattr("engine.cli.main.active_rss_source", fake_active_source)
    monkeypatch.setattr("engine.cli.main.fetch_rss_feed", fake_fetch_rss_feed)

    result = runner.invoke(app, ["predict-loop", "--max-iterations", "3", "--poll-seconds", "0"])
    assert result.exit_code == 0, result.stdout

    from engine.data.news import RSS_FEEDS

    assert [c[0] for c in calls] == rotation
    assert calls[0][1] == RSS_FEEDS["yahoo_finance_top"]
    assert calls[1][1] == RSS_FEEDS["marketwatch_top"]
    assert calls[2][1] == RSS_FEEDS["prnewswire_all"]


def test_predict_loop_pause_skips_cycle_body_but_keeps_looping(monkeypatch, tmp_path):
    # PredictLoopConfig.enabled=False must skip the fetch/predict body while
    # the loop keeps running and sleeping -- it must NOT exit early, since
    # that's how it resumes automatically once re-enabled, no redeploy.
    _isolated_env(monkeypatch, tmp_path)
    monkeypatch.setattr("engine.cli.main.build_prediction_client", _FakePredictionClient)
    calls = []
    monkeypatch.setattr("engine.cli.main.fetch_rss_feed", lambda source, url: (calls.append(1), [])[1])

    from engine.journal.db import get_session
    from engine.journal.registry import update_predict_loop_config

    with get_session(get_settings()) as session:
        update_predict_loop_config(session, enabled=False)

    result = runner.invoke(app, ["predict-loop", "--max-iterations", "3", "--poll-seconds", "0"])
    assert result.exit_code == 0, result.stdout
    assert "predict-loop stopped" in result.stdout
    assert calls == []


def test_predict_loop_respects_headlines_per_source_quota(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    from engine.prediction.schema import ConsequenceAnalysis

    _isolated_env(monkeypatch, tmp_path)

    analyze_calls = []

    class _FakeClientWithOneImpact(_FakePredictionClient):
        def analyze(self, headline, tracked_symbols, past_cases=None):
            analyze_calls.append(headline)
            return ConsequenceAnalysis(impacts=[], overall_reasoning="none")

    monkeypatch.setattr("engine.cli.main.build_prediction_client", _FakeClientWithOneImpact)

    items = [
        type(
            "FakeRawNewsItem",
            (),
            {
                "id": f"id-{i}", "source": "test_feed",
                "published_at": datetime(2026, 7, 21, tzinfo=timezone.utc),
                "ingested_at": datetime(2026, 7, 21, tzinfo=timezone.utc),
                "headline": f"Headline {i}", "url": None, "raw_payload": {},
            },
        )()
        for i in range(5)
    ]
    monkeypatch.setattr("engine.cli.main.fetch_rss_feed", lambda source, url: items)

    import engine.cli.main as cli_main
    from engine.domain import NewsItem

    def _fake_tag_and_route(raw, universe):
        return NewsItem(
            id=raw.id, source=raw.source, published_at=raw.published_at, ingested_at=raw.ingested_at,
            headline=raw.headline, url=raw.url, raw_payload=raw.raw_payload,
        )

    monkeypatch.setattr(cli_main, "tag_and_route", _fake_tag_and_route)

    result = runner.invoke(
        app, ["predict-loop", "--max-iterations", "1", "--poll-seconds", "0", "--predict-limit", "2"]
    )
    assert result.exit_code == 0, result.stdout
    assert len(analyze_calls) == 2


def test_predict_loop_skips_near_duplicate_headlines_across_sources(monkeypatch, tmp_path):
    # Two different outlets covering the same real-world event with
    # different wording -- headline_already_predicted (exact match) would
    # miss this; headline_near_duplicate (fuzzy match) must catch it.
    from datetime import datetime, timezone

    from engine.prediction.schema import ConsequenceAnalysis, PredictedImpact

    _isolated_env(monkeypatch, tmp_path)

    item_a = type(
        "FakeRawNewsItem",
        (),
        {
            "id": "a", "source": "yahoo_finance_top",
            "published_at": datetime(2026, 7, 21, tzinfo=timezone.utc),
            "ingested_at": datetime(2026, 7, 21, tzinfo=timezone.utc),
            "headline": "Fed cuts interest rates by half a point", "url": None, "raw_payload": {},
        },
    )()
    item_b = type(
        "FakeRawNewsItem",
        (),
        {
            "id": "b", "source": "marketwatch_top",
            "published_at": datetime(2026, 7, 21, tzinfo=timezone.utc),
            "ingested_at": datetime(2026, 7, 21, tzinfo=timezone.utc),
            "headline": "Federal Reserve cuts interest rates by half a point", "url": None, "raw_payload": {},
        },
    )()

    fetch_calls = []

    def fake_fetch_rss_feed(source, url):
        fetch_calls.append(source)
        return [item_a] if len(fetch_calls) == 1 else [item_b]

    monkeypatch.setattr("engine.cli.main.fetch_rss_feed", fake_fetch_rss_feed)

    import engine.cli.main as cli_main
    from engine.domain import NewsItem

    def _fake_tag_and_route(raw, universe):
        return NewsItem(
            id=raw.id, source=raw.source, published_at=raw.published_at, ingested_at=raw.ingested_at,
            headline=raw.headline, url=raw.url, raw_payload=raw.raw_payload,
        )

    monkeypatch.setattr(cli_main, "tag_and_route", _fake_tag_and_route)

    analyze_calls = []

    class _FakeClientWithOneImpact(_FakePredictionClient):
        def analyze(self, headline, tracked_symbols, past_cases=None):
            analyze_calls.append(headline)
            # Must record at least one impact -- headline_near_duplicate
            # matches against *recorded* Prediction rows, and
            # run_prediction_for_news_item only writes a row per impact.
            # An empty-impacts response (as in most other fakes here) would
            # leave nothing in the DB for item_b to be flagged against.
            return ConsequenceAnalysis(
                impacts=[PredictedImpact(symbol="SPY", direction="up", confidence=0.9, rationale="test")],
                overall_reasoning="test",
            )

    monkeypatch.setattr("engine.cli.main.build_prediction_client", _FakeClientWithOneImpact)

    result = runner.invoke(app, ["predict-loop", "--max-iterations", "2", "--poll-seconds", "0"])
    assert result.exit_code == 0, result.stdout
    assert analyze_calls == ["Fed cuts interest rates by half a point"]


def test_predict_loop_cli_flag_seeds_config_only_on_first_run(monkeypatch, tmp_path):
    _isolated_env(monkeypatch, tmp_path)
    monkeypatch.setattr("engine.cli.main.build_prediction_client", _FakePredictionClient)
    monkeypatch.setattr("engine.cli.main.fetch_rss_feed", lambda source, url: [])

    result = runner.invoke(
        app, ["predict-loop", "--max-iterations", "1", "--poll-seconds", "0", "--predict-limit", "3"]
    )
    assert result.exit_code == 0, result.stdout

    from engine.journal.db import get_session
    from engine.journal.registry import get_predict_loop_config

    with get_session(get_settings()) as session:
        assert get_predict_loop_config(session).headlines_per_source == 3

    # Second invocation with no --predict-limit -- the DB value must persist,
    # not silently reset back to the CLI option's own default.
    result2 = runner.invoke(app, ["predict-loop", "--max-iterations", "1", "--poll-seconds", "0"])
    assert result2.exit_code == 0, result2.stdout
    with get_session(get_settings()) as session:
        assert get_predict_loop_config(session).headlines_per_source == 3
