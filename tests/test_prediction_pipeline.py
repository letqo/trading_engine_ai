from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd

from engine.data.universe import Instrument, Universe
from engine.domain import NewsItem
from engine.journal.models import PredictionDirection, PredictionStatus
from engine.journal.registry import record_prediction, resolve_prediction
from engine.prediction.pipeline import resolve_pending_predictions, run_prediction_for_news_item
from engine.prediction.schema import ConsequenceAnalysis, PredictedImpact

NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 1, 31, tzinfo=timezone.utc)


def make_universe():
    return Universe(
        instruments=(
            Instrument(symbol="EWJ", tier=2, asset_class="equity_etf", news_topics=("boj",)),
            Instrument(symbol="SPY", tier=1, asset_class="equity_etf", news_topics=("fed",)),
        ),
        source_text="x",
    )


def make_news_item():
    return NewsItem(
        id="1", source="rss", published_at=NOW, ingested_at=NOW,
        headline="BOJ hikes rates unexpectedly", url=None, raw_payload={},
        topics=frozenset({"boj"}), routed_symbols=("EWJ",),
    )


class FakeClient:
    model = "claude-opus-4-8"
    knowledge_cutoff = CUTOFF.date()

    def __init__(self, analysis, forward_safe=True):
        self._analysis = analysis
        self._forward_safe = forward_safe

    def is_forward_safe(self, decision_timestamp):
        return self._forward_safe

    def analyze(self, headline, tracked_symbols, past_cases=None):
        self.last_call = {"headline": headline, "tracked_symbols": tracked_symbols, "past_cases": past_cases}
        return self._analysis


def test_run_prediction_persists_one_row_per_impact(db_session):
    universe = make_universe()
    analysis = ConsequenceAnalysis(
        impacts=[PredictedImpact(symbol="EWJ", direction="down", confidence=0.7, rationale="yen strengthens")],
        overall_reasoning="only EWJ affected",
    )
    client = FakeClient(analysis)

    predictions = run_prediction_for_news_item(
        db_session, client, make_news_item(), universe, resolution_window_hours=24.0
    )
    assert len(predictions) == 1
    pred = predictions[0]
    assert pred.symbol == "EWJ"
    assert pred.direction == PredictionDirection.DOWN
    assert pred.forward_safe is True
    assert pred.status == PredictionStatus.PENDING
    assert pred.model_name == "claude-opus-4-8"
    assert pred.in_tracked_universe is True


def test_run_prediction_keeps_but_flags_symbols_outside_universe(db_session):
    # The model is not restricted to the tracked universe -- an off-universe
    # name is logged (real evidence, see load_off_universe_symbol_stats), just
    # flagged as untradable rather than silently dropped.
    universe = make_universe()
    analysis = ConsequenceAnalysis(
        impacts=[
            PredictedImpact(symbol="EWJ", direction="down", confidence=0.7, rationale="ok"),
            PredictedImpact(symbol="NOTINUNIVERSE", direction="up", confidence=0.9, rationale="off-universe pick"),
        ],
        overall_reasoning="x",
    )
    client = FakeClient(analysis)
    predictions = run_prediction_for_news_item(
        db_session, client, make_news_item(), universe, resolution_window_hours=24.0
    )
    assert len(predictions) == 2
    by_symbol = {p.symbol: p for p in predictions}
    assert by_symbol["EWJ"].in_tracked_universe is True
    assert by_symbol["NOTINUNIVERSE"].in_tracked_universe is False


def test_run_prediction_passes_forward_safe_flag_from_client(db_session):
    universe = make_universe()
    analysis = ConsequenceAnalysis(impacts=[], overall_reasoning="none")
    client = FakeClient(analysis, forward_safe=False)
    item = make_news_item()
    # even with no impacts, forward_safe would apply if any were returned;
    # verify no rows means no assertion needed on forward_safe here, so
    # add one impact to actually exercise the flag
    analysis.impacts = [PredictedImpact(symbol="EWJ", direction="up", confidence=0.5, rationale="x")]
    predictions = run_prediction_for_news_item(db_session, client, item, universe, resolution_window_hours=24.0)
    assert predictions[0].forward_safe is False


def test_run_prediction_includes_retrieved_past_cases_in_call(db_session):
    universe = make_universe()
    past = record_prediction(
        db_session, news_headline="past BOJ move", news_source="rss", news_published_at=NOW - timedelta(days=10),
        news_decision_timestamp=NOW - timedelta(days=10), topics=["boj"], symbol="EWJ",
        direction=PredictionDirection.DOWN, confidence=0.6, rationale="x", model_name="claude-opus-4-8",
        model_knowledge_cutoff=CUTOFF, forward_safe=True, resolution_window_hours=24.0, in_tracked_universe=True,
    )
    resolve_prediction(db_session, past, entry_price=100.0, exit_price=98.0, resolved_at=NOW)

    analysis = ConsequenceAnalysis(impacts=[], overall_reasoning="none")
    client = FakeClient(analysis)
    run_prediction_for_news_item(db_session, client, make_news_item(), universe, resolution_window_hours=24.0)
    assert len(client.last_call["past_cases"]) == 1
    assert "past BOJ move" in client.last_call["past_cases"][0]


def _bars_df(symbol, rows):
    return pd.DataFrame(
        [
            {"symbol": symbol, "timestamp": ts, "open": o, "high": max(o, c), "low": min(o, c), "close": c,
             "volume": 1000, "timeframe": "1h"}
            for ts, o, c in rows
        ]
    )


def test_resolve_pending_predictions_computes_correct_outcome(db_session):
    pred = record_prediction(
        db_session, news_headline="BOJ hikes", news_source="rss",
        news_published_at=NOW - timedelta(days=2), news_decision_timestamp=NOW - timedelta(days=2),
        topics=["boj"], symbol="EWJ", direction=PredictionDirection.DOWN, confidence=0.7, rationale="x",
        model_name="claude-opus-4-8", model_knowledge_cutoff=CUTOFF, forward_safe=True,
        resolution_window_hours=24.0, in_tracked_universe=True,
    )
    decision_ts = pred.news_decision_timestamp
    bars = _bars_df("EWJ", [
        (decision_ts, 100.0, 100.0),
        (decision_ts + timedelta(hours=12), 100.0, 97.0),
        (decision_ts + timedelta(hours=24), 97.0, 95.0),
    ])
    with patch("engine.prediction.pipeline.fetch_bars", return_value=bars):
        resolved = resolve_pending_predictions(db_session, as_of=NOW)

    assert len(resolved) == 1
    assert resolved[0].status == PredictionStatus.RESOLVED
    assert resolved[0].outcome_correct is True  # predicted DOWN, price fell


def test_resolve_pending_predictions_marks_invalid_when_no_bars(db_session):
    record_prediction(
        db_session, news_headline="BOJ hikes", news_source="rss",
        news_published_at=NOW - timedelta(days=2), news_decision_timestamp=NOW - timedelta(days=2),
        topics=["boj"], symbol="EWJ", direction=PredictionDirection.DOWN, confidence=0.7, rationale="x",
        model_name="claude-opus-4-8", model_knowledge_cutoff=CUTOFF, forward_safe=True,
        resolution_window_hours=24.0, in_tracked_universe=True,
    )
    with patch("engine.prediction.pipeline.fetch_bars", return_value=pd.DataFrame()):
        resolved = resolve_pending_predictions(db_session, as_of=NOW)
    assert resolved[0].status == PredictionStatus.INVALID
