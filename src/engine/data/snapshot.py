"""Data snapshot mechanism: a named, immutable capture of bars + news for a
date range, so a backtest's data_snapshot_id can always be traced back to
exactly what data it saw (determinism/audit hard constraint).

Bars go to a Parquet file on disk (dev-side bulk artifact, per SPEC.md).
News + the snapshot pointer itself go to Postgres via the journal registry,
since the live worker needs them and the container filesystem is ephemeral.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlmodel import Session

from engine.data.bars import fetch_bars, save_bars_parquet
from engine.data.news import fetch_all_rss
from engine.data.router import tag_and_route
from engine.data.universe import Universe
from engine.journal.models import DataSnapshot
from engine.journal.registry import record_news_item, register_snapshot


def create_snapshot(
    session: Session,
    universe: Universe,
    start: str,
    end: str,
    data_dir: Path | str,
    interval: str = "1d",
    include_news: bool = True,
    description: str = "",
) -> DataSnapshot:
    symbols = sorted(universe.tradable_symbols())
    bars_df = fetch_bars(symbols, start=start, end=end, interval=interval)

    news_count = 0
    if include_news:
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
