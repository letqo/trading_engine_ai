import pytest
from sqlmodel import Session, SQLModel, create_engine

import engine.journal.models  # noqa: F401  registers tables on SQLModel.metadata


@pytest.fixture()
def db_session():
    engine_ = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine_)
    with Session(engine_) as session:
        yield session
