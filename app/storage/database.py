"""
Database engine, session factory, and table creation helpers.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models.entities import Base


def _make_engine(url: str | None = None):
    db_url = url or settings.db_url
    connect_args = {}
    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(db_url, connect_args=connect_args, echo=False)


_engine = _make_engine()
SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def init_db(url: str | None = None) -> None:
    """Create all tables (idempotent)."""
    global _engine, SessionLocal
    if url:
        _engine = _make_engine(url)
        SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    Base.metadata.create_all(_engine)


def get_session() -> Session:
    """Return a new SQLAlchemy session.  Caller is responsible for closing."""
    return SessionLocal()
