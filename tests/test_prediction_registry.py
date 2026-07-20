from datetime import datetime, timedelta, timezone

from engine.journal.models import PredictionDirection, PredictionStatus
from engine.journal.registry import (
    load_pending_predictions_past_window,
    load_resolved_predictions_by_topics,
    record_prediction,
    resolve_prediction,
)

NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 1, 31, tzinfo=timezone.utc)


def _make(session, symbol="EWJ", topics=("boj",), status=None, decision_ts=NOW, resolution_hours=24.0):
    pred = record_prediction(
        session,
        news_headline="BOJ hikes rates unexpectedly",
        news_source="rss",
        news_published_at=decision_ts,
        news_decision_timestamp=decision_ts,
        topics=list(topics),
        symbol=symbol,
        direction=PredictionDirection.DOWN,
        confidence=0.7,
        rationale="rate hike strengthens yen, hurts exporters",
        model_name="claude-opus-4-8",
        model_knowledge_cutoff=CUTOFF,
        forward_safe=decision_ts > CUTOFF,
        resolution_window_hours=resolution_hours,
    )
    if status is not None:
        pred.status = status
        pred.resolved_at = NOW
        session.add(pred)
        session.commit()
        session.refresh(pred)
    return pred


def test_record_prediction_defaults_to_pending(db_session):
    pred = _make(db_session)
    assert pred.status == PredictionStatus.PENDING
    assert pred.forward_safe is True


def test_forward_safe_false_for_pre_cutoff_event(db_session):
    pred = _make(db_session, decision_ts=datetime(2025, 6, 1, tzinfo=timezone.utc))
    assert pred.forward_safe is False


def test_load_resolved_predictions_by_topics_filters_status_and_topic(db_session):
    resolved_match = _make(db_session, topics=("boj",), status=PredictionStatus.RESOLVED)
    _make(db_session, topics=("ecb",), status=PredictionStatus.RESOLVED)  # different topic
    _make(db_session, topics=("boj",), status=PredictionStatus.PENDING)  # not resolved

    results = load_resolved_predictions_by_topics(db_session, {"boj"})
    assert [r.id for r in results] == [resolved_match.id]


def test_load_resolved_predictions_respects_limit(db_session):
    for _ in range(3):
        _make(db_session, topics=("boj",), status=PredictionStatus.RESOLVED)
    results = load_resolved_predictions_by_topics(db_session, {"boj"}, limit=2)
    assert len(results) == 2


def test_load_pending_predictions_past_window(db_session):
    old_enough = _make(db_session, decision_ts=NOW - timedelta(hours=25), resolution_hours=24.0)
    too_recent = _make(db_session, decision_ts=NOW - timedelta(hours=1), resolution_hours=24.0)

    pending = load_pending_predictions_past_window(db_session, as_of=NOW)
    ids = {p.id for p in pending}
    assert old_enough.id in ids
    assert too_recent.id not in ids


def test_resolve_prediction_correct_direction(db_session):
    pred = _make(db_session)  # predicted DOWN
    resolved = resolve_prediction(db_session, pred, entry_price=100.0, exit_price=95.0, resolved_at=NOW)
    assert resolved.status == PredictionStatus.RESOLVED
    assert resolved.outcome_correct is True
    assert resolved.actual_return_pct == -5.0


def test_resolve_prediction_wrong_direction(db_session):
    pred = _make(db_session)  # predicted DOWN
    resolved = resolve_prediction(db_session, pred, entry_price=100.0, exit_price=110.0, resolved_at=NOW)
    assert resolved.outcome_correct is False


def test_resolve_prediction_invalid_when_price_missing(db_session):
    pred = _make(db_session)
    resolved = resolve_prediction(db_session, pred, entry_price=None, exit_price=95.0, resolved_at=NOW)
    assert resolved.status == PredictionStatus.INVALID
    assert resolved.outcome_correct is None
