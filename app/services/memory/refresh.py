"""
Memory refresh — dependency-aware invalidation after codebase edits.

Delegates actual invalidation cascade to hierarchy.py, which is the
single source of truth for scope invalidation logic.
"""

from __future__ import annotations

from sqlalchemy import select
from app.models.entities import MemoryRecord, Script
from app.services.agents.tools import search_graph
from app.storage.database import get_session


def _resolve_script_ids(changed_files: list[str]) -> list[int]:
    """Resolve file paths to script IDs."""
    session = get_session()
    try:
        sids = []
        for fp in changed_files:
            s = session.execute(select(Script.id).where(Script.file_path == fp)).scalar()
            if s:
                sids.append(s)
        return sids
    finally:
        session.close()


def analyze_invalidation_impact(changed_files: list[str]) -> dict[str, int]:
    """
    Dependency-aware invalidation analysis (does NOT commit to DB).
    Calculates how many scripts, domains, and contracts would be invalidated
    by the provided changed files.
    """
    counts = {"script": 0, "domain": 0, "contract": 0}
    session = get_session()
    try:
        changed_sids = _resolve_script_ids(changed_files)
        if not changed_sids:
            return counts

        counts["script"] += len(changed_sids)

        parent_scopes = session.execute(
            select(MemoryRecord.parent_scope_id)
            .where(MemoryRecord.scope_id.in_([f"script:{sid}" for sid in changed_sids]))
            .distinct()
        ).scalars().all()

        valid_parents = [p for p in parent_scopes if p]
        if valid_parents:
            domain_count = session.execute(
                select(MemoryRecord.id)
                .where(MemoryRecord.scope_id.in_(valid_parents), MemoryRecord.invalidated_by.is_(None))
            ).scalars().all()
            counts["domain"] += len(domain_count)

        dependent_sids = []
        for sid in changed_sids:
            edges = search_graph(sid, from_type="script", direction="incoming")
            for e in edges:
                if e["source_type"] == "script" and e["source_id"] not in changed_sids:
                    dependent_sids.append(e["source_id"])

        if dependent_sids:
            dep_count = session.execute(
                select(MemoryRecord.id)
                .where(MemoryRecord.scope_id.in_([f"script:{sid}" for sid in dependent_sids]), MemoryRecord.invalidated_by.is_(None))
            ).scalars().all()
            counts["script"] += len(dep_count)

        return counts
    finally:
        session.close()


def invalidate_hierarchy(task_id: int, changed_files: list[str]) -> dict[str, int]:
    """
    Dependency-aware invalidation cascade via hierarchy.py.

    Delegates to hierarchy.propagate_invalidation() which handles:
      1. Self invalidation (changed scripts)
      2. Upward propagation (parent domains → repo)
      3. Sideways propagation (dependents via graph edges)

    Returns counts dict for backward compatibility.
    """
    from app.services.memory.hierarchy import propagate_invalidation

    changed_sids = _resolve_script_ids(changed_files)
    if not changed_sids:
        return {"script": 0, "domain": 0, "contract": 0}

    result = propagate_invalidation(
        changed_sids,
        reason=f"task:{task_id}",
    )

    # Convert to legacy counts format
    script_count = sum(1 for s in result["upward"] if s.startswith("script:"))
    domain_count = sum(1 for s in result["upward"] if s.startswith("domain:"))
    sideways_count = len(result["sideways"])

    return {
        "script": script_count + sideways_count,
        "domain": domain_count,
        "contract": 0,
    }


def list_stale_scopes() -> list[str]:
    """Return scope IDs that have at least one invalidated memory."""
    from app.services.memory.hierarchy import get_stale_scopes
    return get_stale_scopes()
