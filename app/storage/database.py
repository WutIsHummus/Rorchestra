"""
Database engine, session factory, and table creation helpers.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models.entities import Base


def _make_engine(url: str | None = None):
    db_url = url or settings.db_url
    connect_args = {}
    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(
        db_url,
        connect_args=connect_args,
        echo=False,
        pool_size=20,
        max_overflow=30,
    )


_engine = _make_engine()
SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def _run_migrations(engine) -> None:
    """Add new columns and tables for existing DBs (SQLite). Idempotent."""
    with engine.connect() as conn:
        # SQLite: check table/column via pragma table_info
        if engine.dialect.name != "sqlite":
            return
        # 1. Create revamp_sessions if missing
        result = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='revamp_sessions'"))
        if result.fetchone() is None:
            conn.execute(text("""
                CREATE TABLE revamp_sessions (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    repo_id INTEGER NOT NULL,
                    status VARCHAR DEFAULT 'active',
                    migration_brief_json TEXT DEFAULT '{}',
                    created_at DATETIME,
                    updated_at DATETIME,
                    FOREIGN KEY(repo_id) REFERENCES repositories (id)
                )
            """))
            conn.commit()
        # 2. Add tasks columns if missing
        result = conn.execute(text("PRAGMA table_info(tasks)"))
        cols = {row[1] for row in result.fetchall()}
        for col, sql in [
            ("large_change_mode", "ALTER TABLE tasks ADD COLUMN large_change_mode INTEGER DEFAULT 0"),
            ("revamp_session_id", "ALTER TABLE tasks ADD COLUMN revamp_session_id INTEGER REFERENCES revamp_sessions(id)"),
            ("batch_index", "ALTER TABLE tasks ADD COLUMN batch_index INTEGER"),
        ]:
            if col not in cols:
                conn.execute(text(sql))
                conn.commit()
        # 3. Add memory_records.memory_phase if missing
        result = conn.execute(text("PRAGMA table_info(memory_records)"))
        cols = {row[1] for row in result.fetchall()}
        if "memory_phase" not in cols:
            conn.execute(text("ALTER TABLE memory_records ADD COLUMN memory_phase VARCHAR DEFAULT 'stable'"))
            conn.commit()


def init_db(url: str | None = None) -> None:
    """Create all tables (idempotent), then run any pending migrations."""
    global _engine, SessionLocal
    if url:
        _engine = _make_engine(url)
        SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    Base.metadata.create_all(_engine)
    _run_migrations(_engine)


def get_session() -> Session:
    """Return a new SQLAlchemy session.  Caller is responsible for closing."""
    return SessionLocal()
