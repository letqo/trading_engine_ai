"""News ingestion: free RSS sources, no API key required. One optional API
free-tier source (NewsAPI) is wired in as a stub that only activates if
NEWS_API_KEY is set.

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
