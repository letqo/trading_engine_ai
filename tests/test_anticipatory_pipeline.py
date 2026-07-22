from unittest.mock import patch

from engine.anticipatory.pipeline import discover_hypotheses, revise_open_hypotheses
from engine.data.polymarket import PolymarketMarket
from engine.journal.models import HypothesisAction, HypothesisStatus, PredictionDirection
from engine.journal.registry import create_hypothesis, load_open_hypotheses, mark_hypothesis_traded


class FakeEstimate:
    def __init__(self, relevant=True, symbol="XLE", direction_if_yes="up", p_model=0.7, confidence=0.6, rationale="r"):
        self.relevant = relevant
        self.symbol = symbol
        self.direction_if_yes = direction_if_yes
        self.p_model = p_model
        self.confidence = confidence
        self.rationale = rationale


class FakeClient:
    model = "fake"

    def __init__(self, estimate=None):
        self._estimate = estimate or FakeEstimate()
        self.calls = []

    def estimate_hypothesis(self, question, description=""):
        self.calls.append((question, description))
        return self._estimate


def _market(market_id="m1", question="Q?", price_yes=0.5, closed=False, description="d"):
    return PolymarketMarket(market_id=market_id, question=question, description=description, price_yes=price_yes, closed=closed, tags=())


def test_discover_hypotheses_creates_relevant_new_markets(db_session):
    client = FakeClient()
    with patch("engine.anticipatory.pipeline.fetch_candidate_markets", return_value=[_market()]):
        created = discover_hypotheses(db_session, client, discovery_limit=10, max_open_hypotheses=10, min_gap_threshold=0.05)

    assert len(created) == 1
    assert created[0].symbol == "XLE"
    assert created[0].direction_if_yes == PredictionDirection.UP
    assert created[0].market_id == "m1"


def test_discover_hypotheses_skips_already_tracked_markets(db_session):
    create_hypothesis(db_session, market_id="m1", question="Q?", symbol="XLE", direction_if_yes=PredictionDirection.UP)
    client = FakeClient()
    with patch("engine.anticipatory.pipeline.fetch_candidate_markets", return_value=[_market(market_id="m1")]):
        created = discover_hypotheses(db_session, client, discovery_limit=10, max_open_hypotheses=10, min_gap_threshold=0.05)
    assert created == []
    assert client.calls == []  # dedup must happen before the paid LLM call


def test_discover_hypotheses_skips_irrelevant_estimates(db_session):
    client = FakeClient(estimate=FakeEstimate(relevant=False))
    with patch("engine.anticipatory.pipeline.fetch_candidate_markets", return_value=[_market()]):
        created = discover_hypotheses(db_session, client, discovery_limit=10, max_open_hypotheses=10, min_gap_threshold=0.05)
    assert created == []


def test_discover_hypotheses_respects_max_open_hypotheses_cap(db_session):
    create_hypothesis(db_session, market_id="existing", question="Q?", symbol="SPY", direction_if_yes=PredictionDirection.UP)
    client = FakeClient()
    with patch("engine.anticipatory.pipeline.fetch_candidate_markets", return_value=[_market(market_id="new")]):
        created = discover_hypotheses(db_session, client, discovery_limit=10, max_open_hypotheses=1, min_gap_threshold=0.05)
    assert created == []
    assert client.calls == []


def test_discover_hypotheses_respects_per_symbol_cap(db_session):
    create_hypothesis(db_session, market_id="existing", question="Will WTI hit $110?", symbol="USO", direction_if_yes=PredictionDirection.UP)
    client = FakeClient(estimate=FakeEstimate(symbol="USO"))
    with patch("engine.anticipatory.pipeline.fetch_candidate_markets", return_value=[_market(market_id="new", question="Will WTI hit $120?")]):
        created = discover_hypotheses(
            db_session, client,
            discovery_limit=10, max_open_hypotheses=10, min_gap_threshold=0.05,
            max_open_hypotheses_per_symbol=1,
        )
    assert created == []  # USO already at its per-symbol cap of 1, even though max_open_hypotheses (10) has room
    assert len(client.calls) == 1  # the LLM call itself can't be skipped -- the symbol isn't known beforehand


def test_discover_hypotheses_per_symbol_cap_allows_other_symbols(db_session):
    create_hypothesis(db_session, market_id="existing", question="Will WTI hit $110?", symbol="USO", direction_if_yes=PredictionDirection.UP)
    client = FakeClient(estimate=FakeEstimate(symbol="XLE"))
    with patch("engine.anticipatory.pipeline.fetch_candidate_markets", return_value=[_market(market_id="new")]):
        created = discover_hypotheses(
            db_session, client,
            discovery_limit=10, max_open_hypotheses=10, min_gap_threshold=0.05,
            max_open_hypotheses_per_symbol=1,
        )
    assert len(created) == 1
    assert created[0].symbol == "XLE"


def test_discover_hypotheses_records_initial_belief_with_gap(db_session):
    client = FakeClient(estimate=FakeEstimate(p_model=0.7))
    with patch("engine.anticipatory.pipeline.fetch_candidate_markets", return_value=[_market(price_yes=0.5)]):
        created = discover_hypotheses(db_session, client, discovery_limit=10, max_open_hypotheses=10, min_gap_threshold=0.05)
    from engine.journal.registry import load_latest_beliefs_by_hypothesis
    belief = load_latest_beliefs_by_hypothesis(db_session, [created[0].id])[created[0].id]
    assert belief.gap == 0.7 - 0.5
    assert belief.action == HypothesisAction.OPENED  # gap 0.2 >= threshold 0.05, no existing position


def test_revise_open_hypotheses_computes_fresh_gap_each_time(db_session):
    hyp = create_hypothesis(db_session, market_id="m1", question="Q?", symbol="XLE", direction_if_yes=PredictionDirection.UP)
    client = FakeClient(estimate=FakeEstimate(p_model=0.6))
    with patch("engine.anticipatory.pipeline.fetch_market_price", return_value=_market(market_id="m1", price_yes=0.55, closed=False)):
        beliefs, resolved = revise_open_hypotheses(db_session, client, min_gap_threshold=0.05)

    assert len(beliefs) == 1
    assert beliefs[0].hypothesis_id == hyp.id
    assert round(beliefs[0].gap, 2) == 0.05
    assert resolved == []


def test_revise_open_hypotheses_holds_when_position_open_and_gap_still_significant(db_session):
    hyp = create_hypothesis(db_session, market_id="m1", question="Q?", symbol="XLE", direction_if_yes=PredictionDirection.UP)
    mark_hypothesis_traded(db_session, hyp, order_id="o1", quantity=10.0, side="long")
    client = FakeClient(estimate=FakeEstimate(p_model=0.7))
    with patch("engine.anticipatory.pipeline.fetch_market_price", return_value=_market(market_id="m1", price_yes=0.5)):
        beliefs, resolved = revise_open_hypotheses(db_session, client, min_gap_threshold=0.05)
    assert beliefs[0].action == HypothesisAction.HELD


def test_revise_open_hypotheses_exits_when_gap_closes(db_session):
    hyp = create_hypothesis(db_session, market_id="m1", question="Q?", symbol="XLE", direction_if_yes=PredictionDirection.UP)
    mark_hypothesis_traded(db_session, hyp, order_id="o1", quantity=10.0, side="long")
    client = FakeClient(estimate=FakeEstimate(p_model=0.51))
    with patch("engine.anticipatory.pipeline.fetch_market_price", return_value=_market(market_id="m1", price_yes=0.5)):
        beliefs, resolved = revise_open_hypotheses(db_session, client, min_gap_threshold=0.05)
    assert beliefs[0].action == HypothesisAction.EXITED


def test_revise_open_hypotheses_exits_when_gap_flips_direction(db_session):
    hyp = create_hypothesis(db_session, market_id="m1", question="Q?", symbol="XLE", direction_if_yes=PredictionDirection.UP)
    mark_hypothesis_traded(db_session, hyp, order_id="o1", quantity=10.0, side="long")
    client = FakeClient(estimate=FakeEstimate(p_model=0.2))  # now well below market -> gap flips negative
    with patch("engine.anticipatory.pipeline.fetch_market_price", return_value=_market(market_id="m1", price_yes=0.5)):
        beliefs, resolved = revise_open_hypotheses(db_session, client, min_gap_threshold=0.05)
    assert beliefs[0].action == HypothesisAction.EXITED


def test_revise_open_hypotheses_closes_out_on_resolution(db_session):
    create_hypothesis(db_session, market_id="m1", question="Q?", symbol="XLE", direction_if_yes=PredictionDirection.UP)
    client = FakeClient()
    with patch("engine.anticipatory.pipeline.fetch_market_price", return_value=_market(market_id="m1", price_yes=0.95, closed=True)):
        beliefs, resolved = revise_open_hypotheses(db_session, client, min_gap_threshold=0.05)

    assert beliefs == []  # no LLM call needed once resolved
    assert client.calls == []
    assert len(resolved) == 1
    assert resolved[0].status == HypothesisStatus.CLOSED
    assert resolved[0].resolution_outcome is True
    assert load_open_hypotheses(db_session) == []


def test_revise_open_hypotheses_resolution_outcome_false_below_half(db_session):
    create_hypothesis(db_session, market_id="m1", question="Q?", symbol="XLE", direction_if_yes=PredictionDirection.UP)
    client = FakeClient()
    with patch("engine.anticipatory.pipeline.fetch_market_price", return_value=_market(market_id="m1", price_yes=0.02, closed=True)):
        _, resolved = revise_open_hypotheses(db_session, client, min_gap_threshold=0.05)
    assert resolved[0].resolution_outcome is False


def test_revise_open_hypotheses_skips_when_price_unavailable(db_session):
    create_hypothesis(db_session, market_id="m1", question="Q?", symbol="XLE", direction_if_yes=PredictionDirection.UP)
    client = FakeClient()
    with patch("engine.anticipatory.pipeline.fetch_market_price", return_value=None):
        beliefs, resolved = revise_open_hypotheses(db_session, client, min_gap_threshold=0.05)
    assert beliefs == []
    assert resolved == []
    assert len(load_open_hypotheses(db_session)) == 1
