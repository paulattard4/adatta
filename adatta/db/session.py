from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from adatta.config import get_settings


def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or get_settings().database_url, pool_pre_ping=True)


def get_session(url: str | None = None) -> Session:
    return sessionmaker(get_engine(url), expire_on_commit=False)()
