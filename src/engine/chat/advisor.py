"""Dashboard chat: discuss a Prediction/Hypothesis (or anything else) with
Claude and, if the discussion converges on a concrete trade, let the human
confirm it. Strictly human-in-the-loop: the model can only *propose* a
trade via the `propose_trade` tool, which is never itself wired to
broker.submit_order -- see engine.dashboard.app's /chat/confirm route for
the one path that actually executes anything, which reuses
engine.execution.manual_trading / engine.prediction.trading /
engine.anticipatory.trading exactly like every other order path in this
codebase (RiskGate.evaluate is never bypassed).

Conversation state is round-tripped through the browser as a hidden form
field (JSON-serialized Anthropic Messages-API `messages` list) rather than
kept server-side -- there's a single operator, and this avoids a new DB
table / session-affinity concern for what's a low-volume, human-paced
conversation.
"""

from __future__ import annotations

import json

import anthropic

from engine.anticipatory.trading import open_hypothesis_trade
from engine.config.settings import Settings
from engine.data.universe import Universe
from engine.execution.broker import Broker
from engine.execution.manual_trading import open_manual_trade
from engine.execution.pricing import latest_price
from engine.execution.trade_result import TradeAttemptResult
from engine.journal.registry import (
    find_open_trade_by_symbol,
    get_anticipatory_loop_config,
    load_latest_beliefs_by_hypothesis,
)
from engine.journal.models import Hypothesis, Prediction, PredictionStatus
from engine.prediction.trading import open_prediction_trade
from engine.risk.gate import RiskGate
from engine.risk.models import AccountState, Side

MAX_TOOL_ITERATIONS = 6

SYSTEM_PROMPT = """You help a human operator think through a trading decision \
by discussing a Prediction or Hypothesis already logged in this system, or a \
symbol/idea more generally. You have tools to look up specifics -- use them \
rather than guessing.

You are strictly advisory: you cannot execute trades yourself. The only way \
you can act on a conclusion is the `propose_trade` tool, which records a \
proposal the human sees with explicit Confirm/Reject buttons in the UI -- it \
does not place any order. Only call it once you have a specific, sized \
recommendation, not while you're still gathering context. Never claim you \
have "placed" or "executed" a trade -- you haven't and can't.

Be concise. Be honest about uncertainty and small sample sizes rather than \
sounding more confident than the evidence supports."""

TOOLS = [
    {
        "name": "lookup_prediction",
        "description": "Look up a single Prediction row by id: symbol, direction, confidence, rationale, status, whether it's been traded, and outcome if resolved.",
        "input_schema": {
            "type": "object",
            "properties": {"prediction_id": {"type": "string"}},
            "required": ["prediction_id"],
        },
    },
    {
        "name": "lookup_hypothesis",
        "description": "Look up a single Hypothesis (Polymarket market) row by id: question, symbol, expected direction if YES, status, current position, and the latest belief (model probability vs market probability, gap, confidence, rationale) if any.",
        "input_schema": {
            "type": "object",
            "properties": {"hypothesis_id": {"type": "string"}},
            "required": ["hypothesis_id"],
        },
    },
    {
        "name": "get_price",
        "description": "Latest known price for a symbol.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_position",
        "description": "Current open account position for a symbol, if any: quantity, average entry price, market value, and which journal row (if any) opened it.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "propose_trade",
        "description": (
            "Propose a concrete trade for the human to review. This does NOT execute "
            "anything -- it only records a proposal shown with Confirm/Reject buttons."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "quantity": {"type": "number"},
                "rationale": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["prediction", "hypothesis", "manual"],
                    "description": "'prediction'/'hypothesis' if this converts an existing row (pass item_id too); 'manual' otherwise.",
                },
                "item_id": {"type": "string", "description": "Prediction or Hypothesis id, required when kind is one of those"},
            },
            "required": ["symbol", "side", "quantity", "rationale", "kind"],
        },
    },
]


class ChatConfigError(RuntimeError):
    pass


def _tool_lookup_prediction(session, prediction_id: str) -> dict:
    row = session.get(Prediction, prediction_id)
    if row is None:
        return {"error": "no prediction with that id"}
    return {
        "id": row.id,
        "symbol": row.symbol,
        "direction": row.direction.value,
        "confidence": row.confidence,
        "rationale": row.rationale,
        "status": row.status.value,
        "in_tracked_universe": row.in_tracked_universe,
        "forward_safe": row.forward_safe,
        "already_traded": row.traded_order_id is not None,
        "already_exited": row.exit_order_id is not None,
        "trade_rejected": row.trade_rejected,
        "outcome_correct": row.outcome_correct,
        "actual_return_pct": row.actual_return_pct,
    }


def _tool_lookup_hypothesis(session, hypothesis_id: str) -> dict:
    row = session.get(Hypothesis, hypothesis_id)
    if row is None:
        return {"error": "no hypothesis with that id"}
    belief = load_latest_beliefs_by_hypothesis(session, [row.id]).get(row.id)
    return {
        "id": row.id,
        "question": row.question,
        "symbol": row.symbol,
        "direction_if_yes": row.direction_if_yes.value,
        "status": row.status.value,
        "position_side": row.position_side,
        "trade_rejected": row.trade_rejected,
        "latest_belief": (
            {
                "p_model": belief.p_model,
                "p_market": belief.p_market,
                "gap": belief.gap,
                "confidence": belief.confidence,
                "rationale": belief.rationale,
            }
            if belief is not None
            else None
        ),
    }


def _tool_get_price(symbol: str) -> dict:
    price = latest_price(symbol)
    return {"symbol": symbol, "price": price} if price is not None else {"symbol": symbol, "error": "no price data available"}


def _tool_get_position(session, account: AccountState, symbol: str) -> dict:
    pos = account.positions.get(symbol)
    if pos is None or pos.quantity == 0:
        return {"symbol": symbol, "open_position": False}
    match = find_open_trade_by_symbol(session, symbol)
    return {
        "symbol": symbol,
        "open_position": True,
        "quantity": pos.quantity,
        "avg_entry_price": pos.avg_entry_price,
        "market_value": pos.market_value,
        "attributed_to": match[0] if match else "unattributed",
    }


def _execute_tool(name: str, tool_input: dict, *, session, account: AccountState) -> tuple[dict, dict | None]:
    """Returns (tool_result, proposal). proposal is non-None only for
    propose_trade, so the caller can surface it to the UI without having to
    re-parse every tool call the model made this turn."""
    if name == "lookup_prediction":
        return _tool_lookup_prediction(session, tool_input["prediction_id"]), None
    if name == "lookup_hypothesis":
        return _tool_lookup_hypothesis(session, tool_input["hypothesis_id"]), None
    if name == "get_price":
        return _tool_get_price(tool_input["symbol"]), None
    if name == "get_position":
        return _tool_get_position(session, account, tool_input["symbol"]), None
    if name == "propose_trade":
        return {"status": "proposal recorded, awaiting human confirmation in the UI -- not executed"}, dict(tool_input)
    return {"error": f"unknown tool {name!r}"}, None


def run_chat_turn(
    *, settings: Settings, session, account: AccountState, messages: list[dict], user_message: str,
) -> tuple[list[dict], str, dict | None]:
    """Runs one user turn through Claude's tool-use loop. `messages` is the
    prior conversation in raw Anthropic Messages-API dict form (round-
    tripped through the browser -- see module docstring); returns the
    updated list, the assistant's final text, and a trade proposal dict if
    `propose_trade` was called this turn (last call wins if called more
    than once)."""
    if not settings.anthropic_api_key:
        raise ChatConfigError("ANTHROPIC_API_KEY is not set -- chat is unavailable.")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    messages = list(messages) + [{"role": "user", "content": user_message}]
    proposal: dict | None = None
    final_text = ""

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        content_blocks = [block.model_dump() for block in response.content]
        messages.append({"role": "assistant", "content": content_blocks})
        final_text = "\n".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in content_blocks:
            if block.get("type") != "tool_use":
                continue
            result, maybe_proposal = _execute_tool(block["name"], block["input"], session=session, account=account)
            if maybe_proposal is not None:
                proposal = maybe_proposal
            tool_results.append({"type": "tool_result", "tool_use_id": block["id"], "content": json.dumps(result)})
        messages.append({"role": "user", "content": tool_results})

    return messages, final_text, proposal


def _convert_blocked_reason(kind: str, row: Prediction | Hypothesis) -> str | None:
    """Same rule as engine.dashboard.app._convert_blocked_reason (duplicated
    rather than imported to avoid a dashboard <-> chat import cycle -- keep
    the two in sync if either changes)."""
    if row.trade_rejected:
        return f"broker already rejected this {kind}: {row.trade_rejection_reason}"
    if kind == "prediction":
        if row.traded_order_id is not None or row.exit_order_id is not None:
            return "already traded or exited"
        if row.status != PredictionStatus.PENDING:
            return "prediction is no longer pending (already resolved/invalid)"
        if not row.forward_safe:
            return "forward_safe is False -- blocked even for manual trades"
    else:
        if row.position_side is not None:
            return "already has an open position"
        if row.status.value != "open":
            return "hypothesis market is closed"
    return None


def execute_proposal(
    session, broker: Broker, risk_gate: RiskGate, account: AccountState, universe: Universe,
    *, symbol: str, side: str, quantity: float, rationale: str, kind: str, item_id: str | None, submitted_by: str,
) -> TradeAttemptResult:
    """The one path that actually places an order out of a chat proposal --
    called only from engine.dashboard.app's /chat/confirm route, i.e. only
    after the human clicks Confirm. Reuses the exact same execution
    functions as the manual-trade-convert UI (open_prediction_trade /
    open_hypothesis_trade / open_manual_trade) -- RiskGate.evaluate is never
    bypassed, same as every other order path in this codebase."""
    tradable = universe.tradable_symbols()
    note = f"AI chat proposal: {rationale}"[:500]

    if kind == "prediction" and item_id:
        row = session.get(Prediction, item_id)
        if row is None:
            return TradeAttemptResult(ok=False, reason="prediction not found")
        blocked = _convert_blocked_reason("prediction", row)
        if blocked:
            return TradeAttemptResult(ok=False, reason=blocked)
        return open_prediction_trade(session, broker, risk_gate, account, tradable, row, override_quantity=quantity)

    if kind == "hypothesis" and item_id:
        row = session.get(Hypothesis, item_id)
        if row is None:
            return TradeAttemptResult(ok=False, reason="hypothesis not found")
        blocked = _convert_blocked_reason("hypothesis", row)
        if blocked:
            return TradeAttemptResult(ok=False, reason=blocked)
        belief = load_latest_beliefs_by_hypothesis(session, [row.id]).get(row.id)
        if belief is None:
            return TradeAttemptResult(ok=False, reason="no belief recorded yet")
        config = get_anticipatory_loop_config(session)
        return open_hypothesis_trade(
            session, broker, risk_gate, account, tradable, row, belief, config.min_gap_threshold,
            override_quantity=quantity,
        )

    return open_manual_trade(
        session, broker, risk_gate, account, universe,
        symbol=symbol.strip().upper(), side=Side(side), quantity=quantity, submitted_by=submitted_by, note=note,
    )


def display_messages(messages: list[dict]) -> list[dict]:
    """Collapses the raw API message list down to the human-readable text
    turns for rendering -- tool_use/tool_result blocks are internal
    plumbing, not conversation content."""
    display = []
    for msg in messages:
        content = msg["content"]
        if isinstance(content, str):
            display.append({"role": msg["role"], "text": content})
            continue
        text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        if text:
            display.append({"role": msg["role"], "text": text})
    return display
