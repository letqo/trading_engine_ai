"""Read-only client for Polymarket's public Gamma API: market discovery
and live prices for the anticipatory prediction engine (see
docs/anticipatory_prediction_mode.md). No authentication is required or
used here -- unlike CLOB order placement (subject to real geographic/CFTC
restrictions), Gamma reads are unauthenticated and unrestricted from the
US. This project never places an order on Polymarket's CLOB; it only
reads prices here and trades the underlying equity/ETF via
engine.execution.alpaca -- so that restriction never applies to this
codebase. Verified live against the real API on 2026-07-22: /events
returns category tags plus nested binary markets, and
/markets?condition_ids=<id> refreshes a single tracked market's price.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"

# First-pass heuristic (see docs/anticipatory_prediction_mode.md's open
# items -- the full tag taxonomy hasn't been exhaustively catalogued).
# Markets under these tags essentially never have real equity/ETF
# exposure, so excluding them up front saves a paid LLM relevance call per
# candidate rather than relying on the model to say "not relevant" every
# time. Tuned against real /events output, not guessed in the abstract.
EXCLUDED_TAG_SLUGS = frozenset(
    {
        "sports", "nba", "nfl", "nhl", "mlb", "soccer", "epl", "ufc", "tennis", "golf",
        "esports", "games", "gaming", "league-of-legends",
        "entertainment", "pop-culture", "music", "awards", "tv", "movies",
        "crypto", "crypto-prices",
    }
)

_RETRYABLE = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)


@dataclass(frozen=True)
class PolymarketMarket:
    market_id: str  # Polymarket's conditionId -- stable identity, dedup key
    question: str
    description: str
    price_yes: float  # outcomePrices[0] -- the market-implied P(yes), 0-1
    closed: bool
    tags: tuple[str, ...]  # tag slugs from the parent event, for relevance filtering/audit


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type(_RETRYABLE),
    reraise=True,
)
def _get(path: str, params: dict) -> object:
    response = requests.get(f"{GAMMA_BASE_URL}{path}", params=params, timeout=10)
    response.raise_for_status()
    return response.json()


def _parse_market(raw: dict, tags: tuple[str, ...]) -> PolymarketMarket | None:
    """None for anything not a clean binary Yes/No market -- Polymarket's
    share price is only cleanly interpretable as P(yes) when there are
    exactly two outcomes and the first is "Yes" (see design doc)."""
    try:
        outcomes = json.loads(raw.get("outcomes", "[]"))
        prices = json.loads(raw.get("outcomePrices", "[]"))
    except (json.JSONDecodeError, TypeError):
        return None
    if len(outcomes) != 2 or str(outcomes[0]).strip().lower() != "yes":
        return None
    condition_id = raw.get("conditionId")
    question = raw.get("question")
    if not condition_id or not question:
        return None
    try:
        price_yes = float(prices[0])
    except (IndexError, ValueError, TypeError):
        return None
    return PolymarketMarket(
        market_id=condition_id,
        question=question,
        description=raw.get("description") or "",
        price_yes=price_yes,
        closed=bool(raw.get("closed", False)),
        tags=tags,
    )


def fetch_candidate_markets(limit: int = 20) -> list[PolymarketMarket]:
    """Discovery: active, not-yet-closed events ordered by 24h volume
    (favors markets with real trading activity over obscure/illiquid
    ones), filtered to drop categories that structurally never have
    equity/ETF exposure (EXCLUDED_TAG_SLUGS). One entry per qualifying
    binary market -- an event can contain more than one market (e.g. a
    multi-candidate event), each considered independently."""
    events = _get(
        "/events",
        {"active": "true", "closed": "false", "limit": limit, "order": "volume24hr", "ascending": "false"},
    )
    markets: list[PolymarketMarket] = []
    for event in events:
        tags = tuple(t.get("slug", "") for t in event.get("tags", []))
        if EXCLUDED_TAG_SLUGS.intersection(tags):
            continue
        for raw_market in event.get("markets", []):
            parsed = _parse_market(raw_market, tags)
            if parsed is not None and not parsed.closed:
                markets.append(parsed)
    return markets


def fetch_market_price(market_id: str) -> PolymarketMarket | None:
    """Refresh a single tracked hypothesis's live price by conditionId --
    used by the revision loop, one call per open hypothesis per poll
    cycle. None if the market can no longer be found (defensive -- not
    expected in practice once a market_id has been seen)."""
    results = _get("/markets", {"condition_ids": market_id})
    if not results:
        return None
    return _parse_market(results[0], tags=())
