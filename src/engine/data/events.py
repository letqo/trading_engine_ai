"""Unified, strictly time-ordered event stream over bars + news.

SPEC.md backtester requirement: "process bars and news items in strict
timestamp order from a unified event queue. No vectorized shortcuts." This
module builds that queue; both the backtester and the `replay` CLI command
consume it the same way.

No-look-ahead is enforced structurally here: a NewsItem's ordering key is
`decision_timestamp` (max(published_at, ingested_at)), never published_at
alone, so a headline can never be visible earlier than our pipeline could
plausibly have seen it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from engine.domain import Bar, NewsItem


class EventType(str, Enum):
    BAR = "bar"
    NEWS = "news"


@dataclass(frozen=True)
class Event:
    timestamp: datetime
    type: EventType
    payload: Bar | NewsItem

    def __lt__(self, other: "Event") -> bool:
        if self.timestamp != other.timestamp:
            return self.timestamp < other.timestamp
        # deterministic tie-break: bars resolve before news at the same
        # instant, so a same-timestamp headline can't retroactively affect
        # a fill that already happened on that bar's close.
        return (self.type == EventType.NEWS) and (other.type == EventType.BAR)


def build_event_stream(bars: list[Bar], news: list[NewsItem]) -> list[Event]:
    events = [Event(timestamp=b.timestamp, type=EventType.BAR, payload=b) for b in bars]
    events += [Event(timestamp=n.decision_timestamp, type=EventType.NEWS, payload=n) for n in news]
    return sorted(events, key=lambda e: (e.timestamp, e.type == EventType.NEWS))
