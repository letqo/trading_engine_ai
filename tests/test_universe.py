from pathlib import Path

from engine.data.universe import load_universe

UNIVERSE_PATH = Path(__file__).resolve().parent.parent / "universe.yaml"


def test_loads_all_tiers():
    universe = load_universe(UNIVERSE_PATH)
    assert len(universe.tier(1)) == 17  # SPY, QQQ, IWM + 10 mega-caps + 4 non-tech diversifiers
    assert len(universe.tier(2)) == 10
    assert len(universe.tier(3)) == 7


def test_tradable_symbols_excludes_tier_3():
    universe = load_universe(UNIVERSE_PATH)
    assert "ES" not in universe.tradable_symbols()
    assert "SPY" in universe.tradable_symbols()


def test_no_duplicate_symbols():
    universe = load_universe(UNIVERSE_PATH)
    symbols = [i.symbol for i in universe.instruments]
    assert len(symbols) == len(set(symbols))


def test_route_topics_fed_hits_macro_etfs_and_not_unrelated_names():
    universe = load_universe(UNIVERSE_PATH)
    routed = universe.route_topics({"fed"})
    assert {"SPY", "QQQ", "TLT", "XLF"} <= routed
    assert "COIN" not in routed


def test_route_topics_boj_hits_ewj_only_among_regions():
    universe = load_universe(UNIVERSE_PATH)
    routed = universe.route_topics({"boj"})
    assert routed == {"EWJ"}


def test_content_hash_stable_for_same_content():
    u1 = load_universe(UNIVERSE_PATH)
    u2 = load_universe(UNIVERSE_PATH)
    assert u1.content_hash == u2.content_hash
