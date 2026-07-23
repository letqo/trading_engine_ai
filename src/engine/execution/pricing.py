"""Shared "current price" lookup for every live trading path that needs to
size or mark an order against the latest close -- engine.prediction.trading,
engine.anticipatory.trading, engine.execution.manual_trading, and the
dashboard's manual-trade size preview. One implementation so a change to
the lookback window or bar source only needs to happen in one place."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from engine.data.bars import fetch_bars


def latest_price(symbol: str) -> float | None:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=7)
    df = fetch_bars([symbol], start=str(start), end=str(end + timedelta(days=1)), interval="1d")
    if df.empty:
        return None
    return float(df.sort_values("timestamp").iloc[-1]["close"])
