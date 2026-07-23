from engine.journal.models import HypothesisAction, HypothesisStatus, PredictionDirection
from engine.journal.registry import (
    close_hypothesis,
    create_hypothesis,
    get_anticipatory_loop_config,
    hypothesis_exists_for_market,
    load_latest_beliefs_by_hypothesis,
    load_open_hypotheses,
    load_recent_hypotheses,
    load_recent_hypothesis_trade_rejections,
    mark_anticipatory_loop_cycle,
    mark_hypothesis_flat,
    mark_hypothesis_trade_rejected,
    mark_hypothesis_traded,
    record_hypothesis_belief,
    update_anticipatory_loop_config,
)


def _make_hyp(session, market_id="m1", symbol="XLE", direction=PredictionDirection.UP):
    return create_hypothesis(session, market_id=market_id, question="Will X happen?", symbol=symbol, direction_if_yes=direction)


def test_get_anticipatory_loop_config_creates_defaults_on_first_call(db_session):
    config = get_anticipatory_loop_config(db_session)
    assert config.enabled is True
    assert config.poll_seconds == 3600
    assert config.min_gap_threshold == 0.05
    assert config.max_open_hypotheses == 10
    assert config.discovery_limit == 20


def test_update_anticipatory_loop_config_persists_partial_changes(db_session):
    get_anticipatory_loop_config(db_session)  # seed defaults first
    updated = update_anticipatory_loop_config(db_session, enabled=False, min_gap_threshold=0.1)
    assert updated.enabled is False
    assert updated.min_gap_threshold == 0.1
    assert updated.poll_seconds == 3600  # untouched field keeps its default

    reloaded = get_anticipatory_loop_config(db_session)
    assert reloaded.enabled is False
    assert reloaded.min_gap_threshold == 0.1


def test_mark_anticipatory_loop_cycle_stamps_last_cycle_at_without_touching_updated_at(db_session):
    original = get_anticipatory_loop_config(db_session)
    assert original.last_cycle_at is None
    original_updated_at = original.updated_at

    mark_anticipatory_loop_cycle(db_session)

    refreshed = get_anticipatory_loop_config(db_session)
    assert refreshed.last_cycle_at is not None
    assert refreshed.updated_at == original_updated_at


def test_hypothesis_exists_for_market_dedup(db_session):
    assert not hypothesis_exists_for_market(db_session, "m1")
    _make_hyp(db_session)
    assert hypothesis_exists_for_market(db_session, "m1")
    assert not hypothesis_exists_for_market(db_session, "m2")


def test_create_hypothesis_defaults_to_open_and_flat(db_session):
    hyp = _make_hyp(db_session)
    assert hyp.status == HypothesisStatus.OPEN
    assert hyp.position_side is None
    assert hyp.direction_if_yes == PredictionDirection.UP


def test_load_open_hypotheses_excludes_closed(db_session):
    open_hyp = _make_hyp(db_session, market_id="m1")
    closed_hyp = _make_hyp(db_session, market_id="m2")
    close_hypothesis(db_session, closed_hyp, resolution_outcome=True)

    open_ids = {h.id for h in load_open_hypotheses(db_session)}
    assert open_ids == {open_hyp.id}


def test_record_hypothesis_belief_computes_gap(db_session):
    hyp = _make_hyp(db_session)
    belief = record_hypothesis_belief(
        db_session, hyp, p_model=0.7, p_market=0.5, confidence=0.6, rationale="r", action=HypothesisAction.OPENED,
    )
    assert belief.gap == 0.7 - 0.5
    assert belief.hypothesis_id == hyp.id


def test_mark_hypothesis_traded_sets_position_fields(db_session):
    hyp = _make_hyp(db_session)
    hyp = mark_hypothesis_traded(db_session, hyp, order_id="order-1", quantity=10.0, side="long")
    assert hyp.traded_order_id == "order-1"
    assert hyp.traded_quantity == 10.0
    assert hyp.position_side == "long"


def test_mark_hypothesis_flat_clears_position_fields(db_session):
    hyp = _make_hyp(db_session)
    hyp = mark_hypothesis_traded(db_session, hyp, order_id="order-1", quantity=10.0, side="long")
    hyp = mark_hypothesis_flat(db_session, hyp, exit_order_id="order-2")
    assert hyp.position_side is None
    assert hyp.traded_order_id is None
    assert hyp.traded_quantity is None
    assert hyp.exit_order_id == "order-2"


def test_mark_hypothesis_trade_rejected_sets_fields_and_leaves_traded_order_id_none(db_session):
    hyp = _make_hyp(db_session)
    updated = mark_hypothesis_trade_rejected(db_session, hyp, reason="422 not shortable: XLE")
    assert updated.trade_rejected is True
    assert updated.trade_rejection_reason == "422 not shortable: XLE"
    assert updated.traded_order_id is None  # a rejection is not a trade


def test_load_recent_hypothesis_trade_rejections_returns_only_rejected_most_recent_first(db_session):
    older = _make_hyp(db_session, market_id="m1")
    mark_hypothesis_trade_rejected(db_session, older, reason="r1")
    newer = _make_hyp(db_session, market_id="m2")
    mark_hypothesis_trade_rejected(db_session, newer, reason="r2")
    _make_hyp(db_session, market_id="m3")  # never rejected -- not in scope

    results = load_recent_hypothesis_trade_rejections(db_session)
    assert [h.id for h in results] == [newer.id, older.id]


def test_close_hypothesis_sets_status_and_outcome(db_session):
    hyp = _make_hyp(db_session)
    hyp = close_hypothesis(db_session, hyp, resolution_outcome=True)
    assert hyp.status == HypothesisStatus.CLOSED
    assert hyp.resolution_outcome is True
    assert hyp.closed_at is not None


def test_load_recent_hypotheses_orders_newest_first(db_session):
    first = _make_hyp(db_session, market_id="m1")
    second = _make_hyp(db_session, market_id="m2")
    rows = load_recent_hypotheses(db_session)
    assert [r.id for r in rows] == [second.id, first.id]


def test_load_latest_beliefs_by_hypothesis_keeps_only_the_most_recent(db_session):
    hyp = _make_hyp(db_session)
    record_hypothesis_belief(db_session, hyp, p_model=0.6, p_market=0.5, confidence=0.5, rationale="old", action=HypothesisAction.OPENED)
    latest = record_hypothesis_belief(db_session, hyp, p_model=0.65, p_market=0.5, confidence=0.5, rationale="new", action=HypothesisAction.HELD)

    result = load_latest_beliefs_by_hypothesis(db_session, [hyp.id])
    assert result[hyp.id].id == latest.id
    assert result[hyp.id].rationale == "new"


def test_load_latest_beliefs_by_hypothesis_empty_ids_returns_empty(db_session):
    assert load_latest_beliefs_by_hypothesis(db_session, []) == {}
