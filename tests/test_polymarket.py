import json
from unittest.mock import MagicMock, patch

from engine.data.polymarket import EXCLUDED_TAG_SLUGS, fetch_candidate_markets, fetch_market_price


def _event(tags, markets):
    return {"tags": [{"slug": t} for t in tags], "markets": markets}


def _market(condition_id="0xabc", question="Will X happen?", outcomes=("Yes", "No"),
            prices=("0.4", "0.6"), closed=False, description="desc"):
    return {
        "conditionId": condition_id,
        "question": question,
        "description": description,
        "outcomes": json.dumps(list(outcomes)),
        "outcomePrices": json.dumps(list(prices)),
        "closed": closed,
    }


def _fake_get(return_value):
    return patch("engine.data.polymarket.requests.get", return_value=MagicMock(
        json=lambda: return_value, raise_for_status=lambda: None,
    ))


def test_fetch_candidate_markets_parses_a_clean_binary_market():
    events = [_event(["economy", "politics"], [_market()])]
    with _fake_get(events):
        markets = fetch_candidate_markets(limit=10)
    assert len(markets) == 1
    assert markets[0].market_id == "0xabc"
    assert markets[0].price_yes == 0.4
    assert markets[0].tags == ("economy", "politics")


def test_fetch_candidate_markets_excludes_sports_tagged_events():
    events = [_event(["nba", "sports"], [_market()])]
    with _fake_get(events):
        markets = fetch_candidate_markets(limit=10)
    assert markets == []


def test_fetch_candidate_markets_excludes_any_tag_in_the_blocklist():
    # Only one tag needs to match -- an event otherwise about politics but
    # co-tagged "crypto" is still dropped.
    assert "crypto" in EXCLUDED_TAG_SLUGS
    events = [_event(["politics", "crypto"], [_market()])]
    with _fake_get(events):
        markets = fetch_candidate_markets(limit=10)
    assert markets == []


def test_fetch_candidate_markets_skips_non_binary_outcomes():
    multi = _market(outcomes=("Republican", "Democrat", "Other"), prices=("0.5", "0.4", "0.1"))
    events = [_event(["politics"], [multi])]
    with _fake_get(events):
        markets = fetch_candidate_markets(limit=10)
    assert markets == []


def test_fetch_candidate_markets_skips_outcomes_not_starting_with_yes():
    swapped = _market(outcomes=("No", "Yes"), prices=("0.6", "0.4"))
    events = [_event(["politics"], [swapped])]
    with _fake_get(events):
        markets = fetch_candidate_markets(limit=10)
    assert markets == []


def test_fetch_candidate_markets_skips_closed_markets():
    events = [_event(["politics"], [_market(closed=True)])]
    with _fake_get(events):
        markets = fetch_candidate_markets(limit=10)
    assert markets == []


def test_fetch_candidate_markets_skips_malformed_price_data():
    bad = _market(prices=("not_a_number", "0.6"))
    events = [_event(["politics"], [bad])]
    with _fake_get(events):
        markets = fetch_candidate_markets(limit=10)
    assert markets == []


def test_fetch_market_price_refreshes_a_single_market():
    with _fake_get([_market(condition_id="0xdef", prices=("0.7", "0.3"))]):
        market = fetch_market_price("0xdef")
    assert market is not None
    assert market.market_id == "0xdef"
    assert market.price_yes == 0.7


def test_fetch_market_price_returns_none_when_not_found():
    with _fake_get([]):
        market = fetch_market_price("0xmissing")
    assert market is None
