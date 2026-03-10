"""
Memory record store — CRUD, invalidation-driven freshness, and cascade.

Memory records are NOT invalidated by wall-clock time.  They become stale
only when their source files change (accepted patches, detected edits) and
the orchestrator explicitly invalidates them.
"""

from __future__ import annotations

import datetime as _dt
from typing import Sequence

from sqlalchemy import select, update

from app.models.entities import MemoryRecord, MemoryType
from app.storage.database import get_session


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# ── CRUD ──────────────────────────────────────────────────────────────────


def upsert_memory(
    scope_id: str,
    memory_type: MemoryType,
    content: str,
    *,
    confidence: float = 1.0,
    source_refs: list[str] | None = None,
    promotion_policy: str = "default",
) -> MemoryRecord:
    """Create or update a memory record for the given scope."""
    session = get_session()
    try:
        existing = session.execute(
            select(MemoryRecord).where(
                MemoryRecord.scope_id == scope_id,
                MemoryRecord.memory_type == memory_type,
            )
        ).scalar_one_or_none()

        if existing:
            existing.content = content
            existing.confidence = confidence
            existing.freshness_ts = _utcnow()
            existing.invalidated_by = None          # clear any prior invalidation
            if source_refs is not None:
                import json
                existing.source_refs_json = json.dumps(source_refs)
            session.commit()
            session.refresh(existing)
            return existing

        import json
        record = MemoryRecord(
            scope_id=scope_id,
            memory_type=memory_type,
            content=content,
            confidence=confidence,
            freshness_ts=_utcnow(),
            source_refs_json=json.dumps(source_refs or []),
            promotion_policy=promotion_policy,
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_memories(scope_id: str) -> list[MemoryRecord]:
    """Return all non-invalidated memory records for a scope."""
    session = get_session()
    try:
        return list(
            session.execute(
                select(MemoryRecord).where(
                    MemoryRecord.scope_id == scope_id,
                    MemoryRecord.invalidated_by.is_(None),
                )
            ).scalars().all()
        )
    finally:
        session.close()


def get_memory(scope_id: str, memory_type: MemoryType) -> MemoryRecord | None:
    """Return a specific memory record, or None."""
    session = get_session()
    try:
        return session.execute(
            select(MemoryRecord).where(
                MemoryRecord.scope_id == scope_id,
                MemoryRecord.memory_type == memory_type,
                MemoryRecord.invalidated_by.is_(None),
            )
        ).scalar_one_or_none()
    finally:
        session.close()


# ── Invalidation ──────────────────────────────────────────────────────────


def invalidate_scope(scope_id: str, reason: str = "source_changed") -> int:
    """
    Mark all memory records for *scope_id* as invalidated.
    Returns the number of records invalidated.
    """
    session = get_session()
    try:
        result = session.execute(
            update(MemoryRecord)
            .where(
                MemoryRecord.scope_id == scope_id,
                MemoryRecord.invalidated_by.is_(None),
            )
            .values(invalidated_by=reason, updated_at=_utcnow())
        )
        session.commit()
        return result.rowcount  # type: ignore[return-value]
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def invalidate_by_file(file_path: str, reason: str = "file_changed") -> int:
    """
    Invalidate all memories whose source_refs mention *file_path*.
    This is the primary mechanism: when a file changes, the memories
    derived from it are marked stale.
    """
    session = get_session()
    try:
        # Filter in Python because JSON containment isn't portable across SQLite/PG
        candidates = session.execute(
            select(MemoryRecord).where(MemoryRecord.invalidated_by.is_(None))
        ).scalars().all()

        count = 0
        import json
        for rec in candidates:
            refs = json.loads(rec.source_refs_json or "[]")
            # source_refs_json may be a list of file path strings or a provenance dict (phase, chunk_id, script_ids)
            if not isinstance(refs, list):
                continue
            normalised = file_path.replace("\\", "/")
            if any(normalised in r.replace("\\", "/") for r in refs if isinstance(r, str)):
                rec.invalidated_by = reason
                rec.updated_at = _utcnow()
                count += 1

        session.commit()
        return count
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
