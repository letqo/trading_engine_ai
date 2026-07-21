"""Historical news backfill via Alpaca's News API (Benzinga-sourced, back to
2015) -- data.alpaca.markets/v1beta1/news. Reuses the same credentials as
the paper broker client, but this is a market-data endpoint, not an
order-routing one, so engine.config.guard's paper/live check does not apply
(Alpaca does not split market data by paper/live account).

Why this exists: engine/data/news.py's free RSS feeds have no historical
archive -- they only ever return currently-live items, so a backtest over a
past window could only use news if `engine ingest` happened to run while
that news was current (see JOURNAL.md, 2026-07-20 "news-driven backtests
were silently using today's news"). This endpoint has real dated articles
going back to 2015, so a backtest over any past window can get news that
actually existed in that window.

ingested_at for backfilled articles: see Settings.alpaca_news_backfill_lag_seconds.
We were not actually polling in, say, 2019, so there is no real historical
ingestion timestamp to use. Setting ingested_at = published_at (zero lag)
would be exactly the "quietly optimistic" mistake docs/bias_review.md warns
about -- it would assume a live poller learns about every headline the
instant it's published, which no real polling pipeline does. Instead we add
a fixed, pessimistic simulated poll lag, consistent with SPEC.md's
"start pessimistic" default.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from engine.config.settings import Settings
from engine.domain import NewsItem
from engine.logging_setup import get_logger

logger = get_logger(__name__)

ALPACA_NEWS_BASE_URL = "https://data.alpaca.markets"
_NEWS_PATH = "/v1beta1/news"
_PAGE_LIMIT = 50  # API max per page
_MAX_PAGES = 500  # safety cap against a runaway pagination loop, per chunk (see _CHUNK_DAYS)
_CHUNK_DAYS = 90  # split [start, end] into windows this wide before paginating each


def _is_rate_limit_error(exc: BaseException) -> bool:
    return (
        isinstance(exc, requests.exceptions.HTTPError)
        and exc.response is not None
        and exc.response.status_code == 429
    )


@retry(
    stop=stop_after_attempt(6),
    # A multi-year, multi-symbol backfill can legitimately need real wait
    # time to clear a rate-limit window -- longer/more attempts than the
    # broker client's connection-error retry (engine.execution.alpaca).
    wait=wait_exponential(multiplier=2, min=2, max=60),
    retry=retry_if_exception(_is_rate_limit_error),
    reraise=True,
)
def _get_page(session: requests.Session, params: dict) -> requests.Response:
    response = session.get(f"{ALPACA_NEWS_BASE_URL}{_NEWS_PATH}", params=params, timeout=15)
    response.raise_for_status()
    return response


class AlpacaNewsAuthError(RuntimeError):
    pass


def _parse_timestamp(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _to_news_item(article: dict, ingestion_lag_seconds: float) -> NewsItem:
    published_at = _parse_timestamp(article["created_at"])
    return NewsItem(
        id=str(article.get("id", uuid.uuid4())),
        source="alpaca_benzinga",
        published_at=published_at,
        ingested_at=published_at + timedelta(seconds=ingestion_lag_seconds),
        headline=(article.get("headline") or "").strip(),
        url=article.get("url"),
        raw_payload=article,
    )


def _fetch_window(
    session: requests.Session,
    start: datetime,
    end: datetime,
    symbols: list[str] | None,
    ingestion_lag_seconds: float,
) -> list[NewsItem]:
    """Paginate a single [start, end] window. _MAX_PAGES is a safety net for
    a runaway loop within this window, not a budget for the whole caller
    range -- see fetch_alpaca_news for why the range gets chunked before
    this is called."""
    params: dict[str, str] = {
        "start": start.astimezone(timezone.utc).isoformat(),
        "end": end.astimezone(timezone.utc).isoformat(),
        "limit": str(_PAGE_LIMIT),
        "sort": "asc",
    }
    if symbols:
        params["symbols"] = ",".join(symbols)

    items: list[NewsItem] = []
    page_token: str | None = None
    for _ in range(_MAX_PAGES):
        page_params = dict(params)
        if page_token:
            page_params["page_token"] = page_token

        try:
            response = _get_page(session, page_params)
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (401, 403):
                raise AlpacaNewsAuthError(f"Alpaca news auth failed: {status} {exc.response.text}") from exc
            raise
        payload = response.json()

        for article in payload.get("news", []):
            items.append(_to_news_item(article, ingestion_lag_seconds))

        page_token = payload.get("next_page_token")
        if not page_token:
            break
    else:
        logger.warning(
            "alpaca news pagination hit the safety cap within a chunk -- "
            "some articles in this window were silently dropped",
            extra={"extra_fields": {"max_pages": _MAX_PAGES, "start": str(start), "end": str(end)}},
        )

    return items


def fetch_alpaca_news(
    start: datetime,
    end: datetime,
    settings: Settings,
    symbols: list[str] | None = None,
) -> list[NewsItem]:
    """Fetch every article published in [start, end], paginating as needed.

    [start, end] is split into _CHUNK_DAYS-wide windows before paginating,
    each with its own _MAX_PAGES budget. A multi-year backfill can easily
    exceed 500 pages (25,000 articles) as a single range -- sort=asc means
    hitting that cap would silently drop everything after wherever the cap
    landed (in practice, the most recent portion of the range) rather than
    raising. Chunking keeps each window's article count well under the cap
    so the safety net stays a safety net, not a silent data-loss bug.

    Raises AlpacaNewsAuthError if credentials are missing -- callers should
    catch this and fall back to RSS rather than silently returning nothing.
    """
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise AlpacaNewsAuthError(
            "ALPACA_API_KEY / ALPACA_API_SECRET are not set -- refusing to call "
            "Alpaca's news API rather than silently doing nothing."
        )

    session = requests.Session()
    session.headers.update(
        {
            "APCA-API-KEY-ID": settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
        }
    )

    items: list[NewsItem] = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=_CHUNK_DAYS), end)
        items.extend(_fetch_window(session, chunk_start, chunk_end, symbols, settings.alpaca_news_backfill_lag_seconds))
        chunk_start = chunk_end

    return items
