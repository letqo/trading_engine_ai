from unittest.mock import patch

import pandas as pd

from engine.anticipatory.trading import act_on_hypothesis_beliefs, flatten_resolved_hypotheses, flatten_stopped_hypotheses
from engine.config.settings import RiskLimits
from engine.data.universe import Instrument, Universe
from engine.execution.broker import BrokerOrder
from engine.journal.models import HypothesisAction, PredictionDirection
from engine.journal.registry import create_hypothesis, mark_hypothesis_traded, record_hypothesis_belief
from engine.risk.gate import RiskGate
from engine.risk.models import AccountState, Position, Side


def make_universe():
    return Universe(instruments=(Instrument(symbol="XLE", tier=2, asset_class="equity_etf", news_topics=("energy",)),), source_text="x")


def make_two_symbol_universe():
    return Universe(
        instruments=(
            Instrument(symbol="XLE", tier=2, asset_class="equity_etf", news_topics=("energy",)),
            Instrument(symbol="USO", tier=2, asset_class="equity_etf", news_topics=("oil",)),
        ),
        source_text="x",
    )


class FakeBroker:
    def __init__(self):
        self.submitted = []
        self._next_id = 0

    def submit_order(self, order):
        self._next_id += 1
        self.submitted.append(order)
        return BrokerOrder(
            broker_order_id=f"order-{self._next_id}", symbol=order.symbol, side=order.side,
            quantity=order.quantity, status="filled", filled_avg_price=order.price, submitted_at=order.timestamp,
        )


class _RaisingBroker(FakeBroker):
    """A broker that rejects orders for specific symbols, like Alpaca
    refusing to short-sell a non-shortable asset (422)."""

    def __init__(self, fail_symbols):
        super().__init__()
        self.fail_symbols = set(fail_symbols)

    def submit_order(self, order):
        if order.symbol in self.fail_symbols:
            raise RuntimeError(f"422 Client Error: Unprocessable Entity for {order.symbol}")
        return super().submit_order(order)


def _price_bars(price):
    from datetime import datetime, timezone
    return pd.DataFrame([{
        "symbol": "XLE", "timestamp": datetime.now(timezone.utc), "open": price, "high": price, "low": price,
        "close": price, "volume": 1000, "timeframe": "1d",
    }])


def _hyp_and_belief(session, *, direction_if_yes, gap, confidence=0.6, action=HypothesisAction.OPENED, market_id="m1"):
    hyp = create_hypothesis(session, market_id=market_id, question="Q?", symbol="XLE", direction_if_yes=direction_if_yes)
    p_market = 0.5
    belief = record_hypothesis_belief(
        session, hyp, p_model=p_market + gap, p_market=p_market, confidence=confidence, rationale="r", action=action,
    )
    return hyp, belief


def test_opens_long_when_direction_up_and_gap_positive(db_session):
    hyp, belief = _hyp_and_belief(db_session, direction_if_yes=PredictionDirection.UP, gap=0.2)
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(100.0)):
        opened, closed = act_on_hypothesis_beliefs(
            db_session, broker, risk_gate, account, make_universe(), {hyp.id: hyp}, [belief], min_gap_threshold=0.05,
        )

    assert (opened, closed) == (1, 0)
    assert broker.submitted[0].side == Side.BUY
    assert account.positions["XLE"].quantity > 0


def test_opens_short_when_direction_down_and_gap_positive(db_session):
    # direction_if_yes=DOWN + gap>0 (YES underpriced) -> the symbol falls if
    # YES happens and we think YES is more likely -> short.
    hyp, belief = _hyp_and_belief(db_session, direction_if_yes=PredictionDirection.DOWN, gap=0.2)
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(100.0)):
        act_on_hypothesis_beliefs(db_session, broker, risk_gate, account, make_universe(), {hyp.id: hyp}, [belief], min_gap_threshold=0.05)

    assert broker.submitted[0].side == Side.SELL
    assert account.positions["XLE"].quantity < 0


def test_opens_short_when_direction_up_and_gap_negative(db_session):
    hyp, belief = _hyp_and_belief(db_session, direction_if_yes=PredictionDirection.UP, gap=-0.2)
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(100.0)):
        act_on_hypothesis_beliefs(db_session, broker, risk_gate, account, make_universe(), {hyp.id: hyp}, [belief], min_gap_threshold=0.05)

    assert broker.submitted[0].side == Side.SELL


def test_skips_symbol_outside_tradable_universe(db_session):
    hyp, belief = _hyp_and_belief(db_session, direction_if_yes=PredictionDirection.UP, gap=0.2)
    hyp.symbol = "NOTTRACKED"
    db_session.add(hyp)
    db_session.commit()
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits())
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    opened, closed = act_on_hypothesis_beliefs(
        db_session, broker, risk_gate, account, make_universe(), {hyp.id: hyp}, [belief], min_gap_threshold=0.05,
    )
    assert (opened, closed) == (0, 0)
    assert broker.submitted == []


def test_respects_risk_gate_rejection(db_session):
    hyp, belief = _hyp_and_belief(db_session, direction_if_yes=PredictionDirection.UP, gap=0.2)
    broker = FakeBroker()
    limits = RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0)
    risk_gate = RiskGate(limits)
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)
    account.positions["XLE"] = Position(symbol="XLE", quantity=50.0, avg_entry_price=100.0)  # already at cap

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(100.0)):
        opened, closed = act_on_hypothesis_beliefs(
            db_session, broker, risk_gate, account, make_universe(), {hyp.id: hyp}, [belief], min_gap_threshold=0.05,
        )
    assert (opened, closed) == (0, 0)
    assert broker.submitted == []


def test_open_broker_rejection_marks_untradeable_and_does_not_retry(db_session):
    hyp, belief = _hyp_and_belief(db_session, direction_if_yes=PredictionDirection.UP, gap=0.2)
    broker = _RaisingBroker(fail_symbols={"XLE"})
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(100.0)):
        opened, closed = act_on_hypothesis_beliefs(
            db_session, broker, risk_gate, account, make_universe(), {hyp.id: hyp}, [belief], min_gap_threshold=0.05,
        )

    assert (opened, closed) == (0, 0)
    assert hyp.trade_rejected is True
    assert "422" in hyp.trade_rejection_reason
    assert hyp.traded_order_id is None
    assert "XLE" not in account.positions

    # A later revision cycle producing another OPENED belief for the same
    # (now-rejected) hypothesis must be skipped, not retried.
    another_belief = record_hypothesis_belief(
        db_session, hyp, p_model=0.8, p_market=0.5, confidence=0.6, rationale="r2", action=HypothesisAction.OPENED,
    )
    opened2, closed2 = act_on_hypothesis_beliefs(
        db_session, broker, risk_gate, account, make_universe(), {hyp.id: hyp}, [another_belief], min_gap_threshold=0.05,
    )
    assert (opened2, closed2) == (0, 0)
    assert broker.submitted == []


def test_open_broker_rejection_does_not_block_other_hypotheses(db_session):
    failing, failing_belief = _hyp_and_belief(db_session, direction_if_yes=PredictionDirection.UP, gap=0.2, market_id="m1")
    healthy, healthy_belief = _hyp_and_belief(db_session, direction_if_yes=PredictionDirection.UP, gap=0.2, market_id="m2")
    healthy.symbol = "USO"
    db_session.add(healthy)
    db_session.commit()

    broker = _RaisingBroker(fail_symbols={"XLE"})
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=0.5, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(100.0)):
        opened, closed = act_on_hypothesis_beliefs(
            db_session, broker, risk_gate, account, make_two_symbol_universe(),
            {failing.id: failing, healthy.id: healthy}, [failing_belief, healthy_belief], min_gap_threshold=0.05,
        )

    assert (opened, closed) == (1, 0)  # the rejected XLE hypothesis doesn't block USO from opening
    assert failing.trade_rejected is True
    assert healthy.traded_order_id is not None
    assert "USO" in account.positions


def test_exits_close_the_open_position(db_session):
    hyp = create_hypothesis(db_session, market_id="m1", question="Q?", symbol="XLE", direction_if_yes=PredictionDirection.UP)
    mark_hypothesis_traded(db_session, hyp, order_id="o1", quantity=50.0, side="long")
    exit_belief = record_hypothesis_belief(db_session, hyp, p_model=0.5, p_market=0.5, confidence=0.6, rationale="r", action=HypothesisAction.EXITED)

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=5_000.0, equity_at_session_start=10_000.0)
    account.positions["XLE"] = Position(symbol="XLE", quantity=50.0, avg_entry_price=100.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(110.0)):
        opened, closed = act_on_hypothesis_beliefs(
            db_session, broker, risk_gate, account, make_universe(), {hyp.id: hyp}, [exit_belief], min_gap_threshold=0.05,
        )

    assert (opened, closed) == (0, 1)
    assert broker.submitted[0].side == Side.SELL
    assert "XLE" not in account.positions


def test_exit_broker_rejection_leaves_position_open_for_retry(db_session):
    hyp = create_hypothesis(db_session, market_id="m1", question="Q?", symbol="XLE", direction_if_yes=PredictionDirection.UP)
    mark_hypothesis_traded(db_session, hyp, order_id="o1", quantity=50.0, side="long")
    exit_belief = record_hypothesis_belief(db_session, hyp, p_model=0.5, p_market=0.5, confidence=0.6, rationale="r", action=HypothesisAction.EXITED)

    broker = _RaisingBroker(fail_symbols={"XLE"})
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=5_000.0, equity_at_session_start=10_000.0)
    account.positions["XLE"] = Position(symbol="XLE", quantity=50.0, avg_entry_price=100.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(110.0)):
        opened, closed = act_on_hypothesis_beliefs(
            db_session, broker, risk_gate, account, make_universe(), {hyp.id: hyp}, [exit_belief], min_gap_threshold=0.05,
        )

    # Unlike an open rejection, a close must not be given up on -- stays
    # open (and traded) so it's retried, rather than being marked flat
    # when no real exit happened.
    assert (opened, closed) == (0, 0)
    assert hyp.exit_order_id is None
    assert hyp.trade_rejected is False  # rejection tracking is open-only, closes always retry
    assert "XLE" in account.positions


def test_exit_with_no_open_position_marks_flat_without_order(db_session):
    hyp = create_hypothesis(db_session, market_id="m1", question="Q?", symbol="XLE", direction_if_yes=PredictionDirection.UP)
    mark_hypothesis_traded(db_session, hyp, order_id="o1", quantity=50.0, side="long")
    exit_belief = record_hypothesis_belief(db_session, hyp, p_model=0.5, p_market=0.5, confidence=0.6, rationale="r", action=HypothesisAction.EXITED)

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits())
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)  # no open position

    opened, closed = act_on_hypothesis_beliefs(
        db_session, broker, risk_gate, account, make_universe(), {hyp.id: hyp}, [exit_belief], min_gap_threshold=0.05,
    )
    assert (opened, closed) == (0, 1)
    assert broker.submitted == []
    assert hyp.exit_order_id == "none:no_open_position"


def test_held_and_no_gap_beliefs_trade_nothing(db_session):
    hyp = create_hypothesis(db_session, market_id="m1", question="Q?", symbol="XLE", direction_if_yes=PredictionDirection.UP)
    held = record_hypothesis_belief(db_session, hyp, p_model=0.55, p_market=0.5, confidence=0.6, rationale="r", action=HypothesisAction.HELD)
    no_gap = record_hypothesis_belief(db_session, hyp, p_model=0.51, p_market=0.5, confidence=0.6, rationale="r", action=HypothesisAction.NO_GAP)

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits())
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    opened, closed = act_on_hypothesis_beliefs(
        db_session, broker, risk_gate, account, make_universe(), {hyp.id: hyp}, [held, no_gap], min_gap_threshold=0.05,
    )
    assert (opened, closed) == (0, 0)
    assert broker.submitted == []


def test_flatten_resolved_hypotheses_closes_lingering_positions(db_session):
    hyp = create_hypothesis(db_session, market_id="m1", question="Q?", symbol="XLE", direction_if_yes=PredictionDirection.UP)
    mark_hypothesis_traded(db_session, hyp, order_id="o1", quantity=50.0, side="long")

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0))
    account = AccountState(equity=10_000.0, cash=5_000.0, equity_at_session_start=10_000.0)
    account.positions["XLE"] = Position(symbol="XLE", quantity=50.0, avg_entry_price=100.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(105.0)):
        flattened = flatten_resolved_hypotheses(db_session, broker, risk_gate, account, make_universe(), [hyp])

    assert flattened == 1
    assert "XLE" not in account.positions


def test_flatten_resolved_hypotheses_skips_already_flat(db_session):
    hyp = create_hypothesis(db_session, market_id="m1", question="Q?", symbol="XLE", direction_if_yes=PredictionDirection.UP)
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits())
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    flattened = flatten_resolved_hypotheses(db_session, broker, risk_gate, account, make_universe(), [hyp])
    assert flattened == 0
    assert broker.submitted == []


def test_flatten_stopped_hypotheses_closes_when_stop_triggered(db_session):
    hyp = create_hypothesis(db_session, market_id="m1", question="Q?", symbol="XLE", direction_if_yes=PredictionDirection.UP)
    mark_hypothesis_traded(db_session, hyp, order_id="o1", quantity=50.0, side="long")

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0, stop_loss_pct=0.02))
    account = AccountState(equity=10_000.0, cash=5_000.0, equity_at_session_start=10_000.0)
    account.positions["XLE"] = Position(symbol="XLE", quantity=50.0, avg_entry_price=100.0)

    # 2% stop on a 100.0 long entry triggers at/below 98.0.
    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(97.0)):
        flattened = flatten_stopped_hypotheses(db_session, broker, risk_gate, account, make_universe())

    assert flattened == 1
    assert "XLE" not in account.positions


def test_flatten_stopped_hypotheses_leaves_position_open_within_stop(db_session):
    hyp = create_hypothesis(db_session, market_id="m1", question="Q?", symbol="XLE", direction_if_yes=PredictionDirection.UP)
    mark_hypothesis_traded(db_session, hyp, order_id="o1", quantity=50.0, side="long")

    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(max_capital_per_position_pct=1.0, max_total_exposure_pct=1.0, stop_loss_pct=0.02))
    account = AccountState(equity=10_000.0, cash=5_000.0, equity_at_session_start=10_000.0)
    account.positions["XLE"] = Position(symbol="XLE", quantity=50.0, avg_entry_price=100.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(99.0)):
        flattened = flatten_stopped_hypotheses(db_session, broker, risk_gate, account, make_universe())

    assert flattened == 0
    assert broker.submitted == []
    assert "XLE" in account.positions


def test_flatten_stopped_hypotheses_ignores_untraded_hypotheses(db_session):
    create_hypothesis(db_session, market_id="m1", question="Q?", symbol="XLE", direction_if_yes=PredictionDirection.UP)
    broker = FakeBroker()
    risk_gate = RiskGate(RiskLimits(stop_loss_pct=0.02))
    account = AccountState(equity=10_000.0, cash=10_000.0, equity_at_session_start=10_000.0)

    with patch("engine.execution.pricing.fetch_bars", return_value=_price_bars(1.0)):
        flattened = flatten_stopped_hypotheses(db_session, broker, risk_gate, account, make_universe())

    assert flattened == 0
    assert broker.submitted == []
