"""
Memory refresh — detect stale memories after accepted edits and re-summarise.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.models.entities import MemoryRecord, MemoryType, Script
from app.services.memory.store import invalidate_by_file, get_memories, upsert_memory
from app.storage.database import get_session


def refresh_after_edit(changed_files: list[str]) -> dict[str, int]:
    """
    Given a list of changed file paths:
      1. Invalidate all memories sourced from those files.
      2. Return a summary of what was invalidated per scope.

    Re-summarisation is a separate step (call the summariser explicitly)
    so the caller controls when the Gemini CLI cost is incurred.
    """
    counts: dict[str, int] = {}
    for fp in changed_files:
        n = invalidate_by_file(fp)
        if n:
            counts[fp] = n
    return counts


def list_stale_scopes() -> list[str]:
    """Return scope IDs that have at least one invalidated memory."""
    session = get_session()
    try:
        rows = session.execute(
            select(MemoryRecord.scope_id)
            .where(MemoryRecord.invalidated_by.isnot(None))
            .distinct()
        ).scalars().all()
        return list(rows)
    finally:
        session.close()
