from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import Engine
from sqlmodel import Session, create_engine

from engine.config.settings import Settings, get_settings


def _connect_args(database_url: str) -> dict:
    if database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


@lru_cache
def _engine_for_url(database_url: str) -> Engine:
    return create_engine(
        database_url,
        connect_args=_connect_args(database_url),
        pool_pre_ping=True,
    )


def get_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    return _engine_for_url(settings.database_url)


@contextmanager
def get_session(settings: Settings | None = None) -> Iterator[Session]:
    engine = get_engine(settings)
    with Session(engine) as session:
        yield session
