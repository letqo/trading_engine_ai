"""Bar ingestion via yfinance (free, no API key). Normalizes to engine.domain.Bar
and persists to Parquet -- bulk historical bars are dev-side artifacts per
SPEC.md; only metadata about a snapshot lives in Postgres.
"""

from __future__ import annotations

from datetime import timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

from engine.domain import Bar

# yfinance interval strings we support, mapped to our timeframe label.
_SUPPORTED_INTERVALS = {"1d": "1d", "1h": "1h", "5m": "5m"}


def fetch_bars(
    symbols: list[str],
    start: str,
    end: str,
    interval: str = "1d",
) -> pd.DataFrame:
    """Fetch OHLCV bars for a list of symbols. Returns a long-format frame:
    columns [symbol, timestamp, open, high, low, close, volume, timeframe].
    """
    if interval not in _SUPPORTED_INTERVALS:
        raise ValueError(f"unsupported interval {interval!r}, use one of {list(_SUPPORTED_INTERVALS)}")

    raw = yf.download(
        symbols,
        start=start,
        end=end,
        interval=interval,
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )
    if raw.empty:
        return _empty_frame()

    frames = []
    for symbol in symbols:
        try:
            sub = raw[symbol] if isinstance(raw.columns, pd.MultiIndex) else raw
        except KeyError:
            continue
        sub = sub.dropna(how="all")
        if sub.empty:
            continue
        df = pd.DataFrame(
            {
                "symbol": symbol,
                "timestamp": sub.index.tz_localize(timezone.utc)
                if sub.index.tz is None
                else sub.index.tz_convert(timezone.utc),
                "open": sub["Open"].to_numpy(),
                "high": sub["High"].to_numpy(),
                "low": sub["Low"].to_numpy(),
                "close": sub["Close"].to_numpy(),
                "volume": sub["Volume"].to_numpy(),
                "timeframe": interval,
            }
        )
        frames.append(df)

    if not frames:
        return _empty_frame()
    return pd.concat(frames, ignore_index=True).sort_values(["timestamp", "symbol"]).reset_index(
        drop=True
    )


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["symbol", "timestamp", "open", "high", "low", "close", "volume", "timeframe"]
    )


def bars_to_domain(df: pd.DataFrame) -> list[Bar]:
    return [
        Bar(
            symbol=row.symbol,
            timestamp=row.timestamp.to_pydatetime(),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
            timeframe=row.timeframe,
        )
        for row in df.itertuples(index=False)
    ]


def save_bars_parquet(df: pd.DataFrame, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return path


def load_bars_parquet(path: Path | str) -> pd.DataFrame:
    return pd.read_parquet(Path(path))
