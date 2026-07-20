"""Hits real Yahoo Finance via yfinance. Marked so CI can skip it if the
runner has no network egress; local dev should run it to sanity-check the
data layer against real data.
"""

import pytest

from engine.data.bars import bars_to_domain, fetch_bars

pytestmark = pytest.mark.network


def test_fetch_bars_returns_real_spy_data():
    df = fetch_bars(["SPY"], start="2026-06-01", end="2026-06-10", interval="1d")
    assert not df.empty
    assert set(df["symbol"]) == {"SPY"}
    assert (df["close"] > 0).all()
    assert (df["high"] >= df["low"]).all()


def test_bars_to_domain_round_trip():
    df = fetch_bars(["SPY"], start="2026-06-01", end="2026-06-10", interval="1d")
    bars = bars_to_domain(df)
    assert len(bars) == len(df)
    assert all(b.timestamp.tzinfo is not None for b in bars)
