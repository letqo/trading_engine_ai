"""Loader for the fixed, config-driven trading universe (universe.yaml).

SPEC.md: "The engine trades only instruments on this watchlist. It never
scans or trades outside it." Every other module that needs "is this symbol
tradable" or "which instruments care about this news topic" goes through
here rather than hardcoding the list again.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Instrument:
    symbol: str
    tier: int
    asset_class: str
    news_topics: tuple[str, ...] = field(default_factory=tuple)
    futures_twin: str | None = None
    underlying: str | None = None
    rehearsal_etf: str | None = None


@dataclass(frozen=True)
class Universe:
    instruments: tuple[Instrument, ...]
    source_text: str

    def __post_init__(self) -> None:
        symbols = [i.symbol for i in self.instruments]
        if len(symbols) != len(set(symbols)):
            raise ValueError("duplicate symbol in universe.yaml")

    @property
    def symbols(self) -> set[str]:
        return {i.symbol for i in self.instruments}

    def tier(self, tier: int) -> tuple[Instrument, ...]:
        return tuple(i for i in self.instruments if i.tier == tier)

    def tradable_symbols(self) -> set[str]:
        """v1 trades Tier 1 + Tier 2 only; Tier 3 futures are real-money-era."""
        return {i.symbol for i in self.instruments if i.tier in (1, 2)}

    def get(self, symbol: str) -> Instrument | None:
        for instrument in self.instruments:
            if instrument.symbol == symbol:
                return instrument
        return None

    def route_topics(self, topics: set[str]) -> set[str]:
        """Given a set of news topic tags, return the tradable-universe
        symbols whose news_topics intersect them."""
        if not topics:
            return set()
        matched = set()
        for instrument in self.instruments:
            if instrument.tier not in (1, 2):
                continue
            if set(instrument.news_topics) & topics:
                matched.add(instrument.symbol)
        return matched

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()[:16]


def load_universe(path: Path | str = "universe.yaml") -> Universe:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    instruments = tuple(
        Instrument(
            symbol=entry["symbol"],
            tier=entry["tier"],
            asset_class=entry["asset_class"],
            news_topics=tuple(entry.get("news_topics", [])),
            futures_twin=entry.get("futures_twin"),
            underlying=entry.get("underlying"),
            rehearsal_etf=entry.get("rehearsal_etf"),
        )
        for entry in raw["instruments"]
    )
    return Universe(instruments=instruments, source_text=text)
