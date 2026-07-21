"""News ingestion: free RSS sources, no API key required.

These feeds have no historical archive -- they only ever return
currently-live items. For a real historical news corpus (e.g. to backtest
over a past date range), see engine.data.alpaca_news, which is used
automatically by `engine ingest`/`engine backtest` whenever ALPACA_API_KEY
is set; RSS here remains the fallback.

`Settings.news_api_key` / `Settings.finnhub_api_key` exist as config fields
for a future NewsAPI/Finnhub source (SPEC.md: "one API free tier") but are
not wired to a fetcher yet -- RSS is the only live source today. Don't
assume setting those env vars does anything until this docstring changes.

Raw payloads are always kept (SPEC.md: "never discard source data") -- the
full feedparser entry dict rides along in NewsItem.raw_payload.

publication vs ingestion time: an RSS entry's `published` field is the
source's claimed publication time. Our own `ingested_at` is set the moment
this process parses the feed. A live/production poller may lag true
publication by up to its poll interval; a backtest must never use
`published_at` alone to decide visibility -- see NewsItem.decision_timestamp
in engine.domain, and docs/bias_review.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests

from engine.domain import NewsItem
from engine.logging_setup import get_logger

logger = get_logger(__name__)

# Verified-reachable, free, no-key-required financial news RSS feeds.
RSS_FEEDS: dict[str, str] = {
    "yahoo_finance_top": "https://finance.yahoo.com/news/rssindex",
    "marketwatch_top": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "prnewswire_all": "https://www.prnewswire.com/rss/news-releases-list.rss",
}

# Fixed rotation order for predict-loop's hourly source rotation -- its own
# constant, decoupled from RSS_FEEDS's dict order, so adding/reordering feeds
# later can't silently reshuffle whose rotation slot is whose. Without
# rotation, yahoo's ~47 live items vs marketwatch's ~10/prnewswire's ~20
# structurally starve the other two sources every cycle (yahoo is iterated
# first and fills the whole predict_limit before they're ever reached).
RSS_ROTATION_ORDER: tuple[str, ...] = ("yahoo_finance_top", "marketwatch_top", "prnewswire_all")

_USER_AGENT = "trading-research-engine/0.1 (paper-trading research; contact via repo)"


def _parse_published(entry: dict) -> datetime:
    for key in ("published", "updated", "pubDate"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
    return datetime.now(timezone.utc)


def fetch_rss_feed(source_name: str, url: str) -> list[NewsItem]:
    """Fetch and parse one RSS feed. Network/parse errors surface as an
    empty list rather than raising -- one dead feed must not take down a
    multi-source ingest run; the caller logs the miss."""
    response = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=15)
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    now = datetime.now(timezone.utc)

    items = []
    for entry in parsed.entries:
        raw_payload = dict(entry)
        items.append(
            NewsItem(
                id=str(uuid.uuid4()),
                source=source_name,
                published_at=_parse_published(entry),
                ingested_at=now,
                headline=entry.get("title", "").strip(),
                url=entry.get("link"),
                raw_payload=raw_payload,
            )
        )
    return items


def fetch_all_rss(feeds: dict[str, str] | None = None) -> list[NewsItem]:
    feeds = feeds or RSS_FEEDS
    items: list[NewsItem] = []
    for source_name, url in feeds.items():
        try:
            items.extend(fetch_rss_feed(source_name, url))
        except Exception as exc:
            logger.warning("rss feed fetch failed", extra={"extra_fields": {"source": source_name, "error": str(exc)}})
            continue
    return items


def active_rss_source(
    anchor: datetime,
    now: datetime,
    rotation_hours: float,
    order: tuple[str, ...] = RSS_ROTATION_ORDER,
) -> str:
    """Which RSS source predict-loop should pull from this cycle, as a pure
    function of wall-clock time -- deliberately not a persisted "current
    index" counter. A stored counter needs something to advance it and can
    drift or double-advance across restarts; this gives the same answer from
    any process, at any time, after any number of restarts, with zero
    coordination."""
    # SQLite drops tzinfo on round-trip (anchor comes back from
    # PredictLoopConfig.rotation_anchor naive) -- treat naive as UTC so this
    # works identically against SQLite (dev) and Postgres (prod). See
    # registry._hours_elapsed for the same pattern.
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    elapsed_hours = (now - anchor).total_seconds() / 3600.0
    if rotation_hours <= 0:
        rotation_hours = 1.0  # a bad dashboard value must not wedge the loop
    slot = int(elapsed_hours // rotation_hours)
    return order[slot % len(order)]
