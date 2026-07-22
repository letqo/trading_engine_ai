from datetime import datetime, timedelta, timezone

from sqlmodel import select

from engine.journal.models import PredictionDirection, PredictionStatus, PredictionTopic
from engine.journal.registry import (
    get_predict_loop_config,
    headline_near_duplicate,
    load_actionable_predictions,
    load_expired_open_trades,
    load_off_universe_symbol_stats,
    load_pending_predictions_past_window,
    load_prediction_trades,
    load_resolved_predictions_by_topics,
    mark_predict_loop_cycle,
    mark_prediction_exited,
    mark_prediction_traded,
    record_prediction,
    resolve_prediction,
    update_predict_loop_config,
)

NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 1, 31, tzinfo=timezone.utc)


def _make(session, symbol="EWJ", topics=("boj",), status=None, decision_ts=NOW, resolution_hours=24.0,
          confidence=0.7, in_tracked_universe=True):
    pred = record_prediction(
        session,
        news_headline="BOJ hikes rates unexpectedly",
        news_source="rss",
        news_published_at=decision_ts,
        news_decision_timestamp=decision_ts,
        topics=list(topics),
        symbol=symbol,
        direction=PredictionDirection.DOWN,
        confidence=confidence,
        rationale="rate hike strengthens yen, hurts exporters",
        model_name="claude-opus-4-8",
        model_knowledge_cutoff=CUTOFF,
        forward_safe=decision_ts > CUTOFF,
        resolution_window_hours=resolution_hours,
        in_tracked_universe=in_tracked_universe,
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


def test_load_resolved_predictions_by_topics_includes_most_recent_miss_over_recency_alone(db_session):
    # If the top-`limit` most recent topic matches all happen to be correct,
    # the model would never see its own mistakes on that topic. The most
    # recent incorrect match should still be included, bumping the oldest
    # of the recency window rather than being dropped or added on top.
    old_miss = _make(db_session, topics=("boj",), status=PredictionStatus.RESOLVED)
    old_miss.outcome_correct = False
    old_miss.resolved_at = NOW - timedelta(days=10)
    db_session.add(old_miss)
    db_session.commit()

    hits = []
    for i in range(3):
        hit = _make(db_session, topics=("boj",), status=PredictionStatus.RESOLVED)
        hit.outcome_correct = True
        hit.resolved_at = NOW - timedelta(hours=i)
        db_session.add(hit)
        db_session.commit()
        hits.append(hit)

    results = load_resolved_predictions_by_topics(db_session, {"boj"}, limit=3)
    result_ids = {r.id for r in results}

    assert len(results) == 3  # limit is not raised to fit the miss in
    assert old_miss.id in result_ids
    assert hits[2].id not in result_ids  # the oldest of the recency window got bumped


def test_load_resolved_predictions_by_topics_does_not_duplicate_an_already_included_miss(db_session):
    miss = _make(db_session, topics=("boj",), status=PredictionStatus.RESOLVED)
    miss.outcome_correct = False
    miss.resolved_at = NOW
    db_session.add(miss)
    db_session.commit()

    results = load_resolved_predictions_by_topics(db_session, {"boj"}, limit=5)
    assert [r.id for r in results] == [miss.id]


def test_record_prediction_writes_a_prediction_topic_row_per_topic(db_session):
    # load_resolved_predictions_by_topics queries this table instead of
    # loading every resolved Prediction into Python -- record_prediction
    # must keep it populated, one row per topic, for that to work.
    pred = _make(db_session, topics=("boj", "rates"))
    rows = db_session.exec(select(PredictionTopic).where(PredictionTopic.prediction_id == pred.id)).all()
    assert {r.topic for r in rows} == {"boj", "rates"}


def test_load_resolved_predictions_by_topics_matches_only_once_with_multiple_shared_topics(db_session):
    # A prediction matching more than one requested topic must still only
    # appear once -- the query uses Prediction.id.in_(subquery) rather than
    # a JOIN specifically to avoid per-topic row duplication.
    pred = _make(db_session, topics=("boj", "rates"), status=PredictionStatus.RESOLVED)
    results = load_resolved_predictions_by_topics(db_session, {"boj", "rates"})
    assert [r.id for r in results] == [pred.id]


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


def test_load_actionable_predictions_filters_confidence_and_forward_safe(db_session):
    confident = _make(db_session)  # confidence=0.7, forward_safe=True (decision_ts=NOW > CUTOFF)
    _make(db_session, decision_ts=datetime(2025, 6, 1, tzinfo=timezone.utc))  # forward_safe=False

    low_confidence = record_prediction(
        db_session, news_headline="x", news_source="rss", news_published_at=NOW, news_decision_timestamp=NOW,
        topics=["boj"], symbol="EWJ", direction=PredictionDirection.DOWN, confidence=0.3, rationale="x",
        model_name="claude-opus-4-8", model_knowledge_cutoff=CUTOFF, forward_safe=True, resolution_window_hours=24.0,
        in_tracked_universe=True,
    )

    results = load_actionable_predictions(db_session, min_confidence=0.6)
    ids = {p.id for p in results}
    assert confident.id in ids
    assert low_confidence.id not in ids
    assert len(ids) == 1


def test_load_actionable_predictions_excludes_already_traded(db_session):
    pred = _make(db_session)
    mark_prediction_traded(db_session, pred, order_id="order-1", quantity=10.0)
    results = load_actionable_predictions(db_session, min_confidence=0.6)
    assert results == []


def test_mark_prediction_traded_sets_fields(db_session):
    pred = _make(db_session)
    updated = mark_prediction_traded(db_session, pred, order_id="order-1", quantity=42.0)
    assert updated.traded_order_id == "order-1"
    assert updated.traded_quantity == 42.0
    assert updated.exit_order_id is None


def test_load_expired_open_trades_filters_window_and_exit_state(db_session):
    expired_open = _make(db_session, decision_ts=NOW - timedelta(hours=25), resolution_hours=24.0)
    mark_prediction_traded(db_session, expired_open, order_id="order-1", quantity=10.0)

    still_open_within_window = _make(db_session, decision_ts=NOW - timedelta(hours=1), resolution_hours=24.0)
    mark_prediction_traded(db_session, still_open_within_window, order_id="order-2", quantity=10.0)

    _make(db_session, decision_ts=NOW - timedelta(hours=25), resolution_hours=24.0)  # never traded -- not in scope

    already_exited = _make(db_session, decision_ts=NOW - timedelta(hours=25), resolution_hours=24.0)
    mark_prediction_traded(db_session, already_exited, order_id="order-3", quantity=10.0)
    mark_prediction_exited(db_session, already_exited, order_id="order-4")

    results = load_expired_open_trades(db_session, as_of=NOW)
    ids = {p.id for p in results}
    assert ids == {expired_open.id}


def test_mark_prediction_exited_sets_field(db_session):
    pred = _make(db_session)
    mark_prediction_traded(db_session, pred, order_id="order-1", quantity=10.0)
    updated = mark_prediction_exited(db_session, pred, order_id="order-2")
    assert updated.exit_order_id == "order-2"


def test_load_off_universe_symbol_stats_aggregates_by_symbol(db_session):
    p1 = _make(db_session, symbol="RANDOMCO", in_tracked_universe=False)
    resolve_prediction(db_session, p1, entry_price=100.0, exit_price=95.0, resolved_at=NOW)  # DOWN predicted, correct
    p2 = _make(db_session, symbol="RANDOMCO", in_tracked_universe=False, decision_ts=NOW - timedelta(days=1))
    resolve_prediction(db_session, p2, entry_price=100.0, exit_price=110.0, resolved_at=NOW)  # DOWN predicted, wrong
    _make(db_session, symbol="EWJ", in_tracked_universe=True)  # tracked -- must not appear

    stats = load_off_universe_symbol_stats(db_session)
    assert len(stats) == 1
    s = stats[0]
    assert s.symbol == "RANDOMCO"
    assert s.times_named == 2
    assert s.resolved_count == 2
    assert s.correct_count == 1
    assert s.accuracy_pct == 50.0


def test_load_off_universe_symbol_stats_excludes_non_forward_safe_from_accuracy(db_session):
    p1 = _make(
        db_session, symbol="RANDOMCO", in_tracked_universe=False,
        decision_ts=datetime(2025, 6, 1, tzinfo=timezone.utc),  # before CUTOFF -> forward_safe=False
    )
    resolve_prediction(db_session, p1, entry_price=100.0, exit_price=95.0, resolved_at=NOW)

    stats = load_off_universe_symbol_stats(db_session)
    assert stats[0].times_named == 1
    assert stats[0].resolved_count == 0  # not forward_safe -- doesn't count as evidence


def test_load_off_universe_symbol_stats_sorts_by_resolved_count_first(db_session):
    lots_named_never_resolved = _make(db_session, symbol="NEVERRESOLVED", in_tracked_universe=False)
    for _ in range(4):
        _make(db_session, symbol="NEVERRESOLVED", in_tracked_universe=False)

    p = _make(db_session, symbol="STRONGEVIDENCE", in_tracked_universe=False)
    resolve_prediction(db_session, p, entry_price=100.0, exit_price=95.0, resolved_at=NOW)

    stats = load_off_universe_symbol_stats(db_session)
    assert stats[0].symbol == "STRONGEVIDENCE"  # 1 resolved beats 5 named-but-unresolved
    assert lots_named_never_resolved.symbol == "NEVERRESOLVED"


def test_load_prediction_trades_returns_only_traded_predictions(db_session):
    traded = _make(db_session, symbol="EWJ")
    mark_prediction_traded(db_session, traded, order_id="order-1", quantity=10.0)
    _make(db_session, symbol="SPY")  # never traded -- must not appear

    trades = load_prediction_trades(db_session)
    assert len(trades) == 1
    assert trades[0].id == traded.id


def test_get_predict_loop_config_creates_defaults_then_is_idempotent(db_session):
    first = get_predict_loop_config(db_session)
    assert first.enabled is True
    assert first.headlines_per_source == 10

    second = get_predict_loop_config(db_session)
    assert second.id == first.id
    assert second.headlines_per_source == first.headlines_per_source


def test_update_predict_loop_config_updates_given_fields_and_bumps_updated_at(db_session):
    original = get_predict_loop_config(db_session)
    original_updated_at = original.updated_at

    updated = update_predict_loop_config(db_session, enabled=False, headlines_per_source=3)
    assert updated.enabled is False
    assert updated.headlines_per_source == 3
    # Fields not passed must be left untouched.
    assert updated.poll_seconds == original.poll_seconds
    assert updated.updated_at >= original_updated_at


def test_mark_predict_loop_cycle_stamps_last_cycle_at_without_touching_updated_at(db_session):
    original = get_predict_loop_config(db_session)
    assert original.last_cycle_at is None
    original_updated_at = original.updated_at

    mark_predict_loop_cycle(db_session)

    refreshed = get_predict_loop_config(db_session)
    assert refreshed.last_cycle_at is not None
    # Heartbeat is a distinct signal from "a setting was edited" -- must
    # not bump updated_at, or the dashboard couldn't tell the two apart.
    assert refreshed.updated_at == original_updated_at


def _record_with_headline(session, headline, symbol="SPY", decision_ts=NOW):
    return record_prediction(
        session,
        news_headline=headline,
        news_source="rss",
        news_published_at=decision_ts,
        news_decision_timestamp=decision_ts,
        topics=["macro"],
        symbol=symbol,
        direction=PredictionDirection.UP,
        confidence=0.7,
        rationale="test",
        model_name="claude-opus-4-8",
        model_knowledge_cutoff=CUTOFF,
        forward_safe=True,
        resolution_window_hours=24.0,
        in_tracked_universe=True,
    )


def test_headline_near_duplicate_true_for_in_window_paraphrase(db_session):
    _record_with_headline(db_session, "Fed cuts interest rates by half a point")
    assert headline_near_duplicate(
        db_session, "Federal Reserve cuts interest rates by half a point",
        window_hours=48.0, threshold=90.0,
    )


def test_headline_near_duplicate_false_for_different_event_sharing_a_company_name(db_session):
    _record_with_headline(db_session, "Apple beats quarterly earnings estimates")
    assert not headline_near_duplicate(
        db_session, "Apple faces new antitrust investigation",
        window_hours=48.0, threshold=90.0,
    )


def test_headline_near_duplicate_false_outside_window(db_session):
    pred = _record_with_headline(db_session, "Fed cuts interest rates by half a point")
    # record_prediction always stamps created_at=now(); backdate it past the
    # lookback window to test the cutoff itself, same pattern _make() uses to
    # backdate status/resolved_at after insert.
    pred.created_at = datetime.now(timezone.utc) - timedelta(hours=100)
    db_session.add(pred)
    db_session.commit()

    assert not headline_near_duplicate(
        db_session, "Federal Reserve cuts interest rates by half a point",
        window_hours=48.0, threshold=90.0,
    )
