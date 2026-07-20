"""Data snapshot mechanism: a named, immutable capture of bars + news for a
date range, so a backtest's data_snapshot_id can always be traced back to
exactly what data it saw (determinism/audit hard constraint).

Bars go to a Parquet file on disk (dev-side bulk artifact, per SPEC.md).
News + the snapshot pointer itself go to Postgres via the journal registry,
since the live worker needs them and the container filesystem is ephemeral.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlmodel import Session

from engine.config.settings import Settings, get_settings
from engine.data.alpaca_news import AlpacaNewsAuthError, fetch_alpaca_news
from engine.data.bars import fetch_bars, save_bars_parquet
from engine.data.news import fetch_all_rss
from engine.data.router import tag_and_route
from engine.data.universe import Universe
from engine.journal.models import DataSnapshot
from engine.journal.registry import record_news_item, register_snapshot
from engine.logging_setup import get_logger

logger = get_logger(__name__)


def create_snapshot(
    session: Session,
    universe: Universe,
    start: str,
    end: str,
    data_dir: Path | str,
    interval: str = "1d",
    include_news: bool = True,
    description: str = "",
    settings: Settings | None = None,
) -> DataSnapshot:
    settings = settings or get_settings()
    symbols = sorted(universe.tradable_symbols())
    bars_df = fetch_bars(symbols, start=start, end=end, interval=interval)

    news_count = 0
    if include_news:
        start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
        end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)

        raw_news = None
        use_alpaca_ingested_at = False
        if settings.alpaca_api_key:
            try:
                raw_news = fetch_alpaca_news(start_dt, end_dt, settings, symbols=symbols)
                use_alpaca_ingested_at = True
            except AlpacaNewsAuthError as exc:
                logger.warning("alpaca news fetch failed, falling back to RSS", extra={"extra_fields": {"error": str(exc)}})
        if raw_news is None:
            # No Alpaca key, or Alpaca auth failed: RSS only ever returns
            # currently-live items, so this is only meaningful for an
            # ingest run whose [start, end] covers "now".
            raw_news = fetch_all_rss()

        for item in raw_news:
            tagged = tag_and_route(item, universe)
            record_news_item(
                session,
                source=tagged.source,
                published_at=tagged.published_at,
                headline=tagged.headline,
                raw_payload=tagged.raw_payload,
                url=tagged.url,
                routed_symbols=list(tagged.routed_symbols),
                ingested_at=tagged.ingested_at if use_alpaca_ingested_at else None,
            )
            news_count += 1

    snapshot = register_snapshot(
        session,
        description=description or f"{start}..{end} {interval} bars, {len(symbols)} symbols",
        universe_hash=universe.content_hash,
        bar_start=_to_datetime(bars_df["timestamp"].min()) if not bars_df.empty else None,
        bar_end=_to_datetime(bars_df["timestamp"].max()) if not bars_df.empty else None,
        news_count=news_count,
        bar_row_count=len(bars_df),
    )

    if not bars_df.empty:
        parquet_path = Path(data_dir) / f"{snapshot.id}_bars_{interval}.parquet"
        save_bars_parquet(bars_df, parquet_path)

    return snapshot


def _to_datetime(value) -> datetime:
    return pd.Timestamp(value).to_pydatetime()
