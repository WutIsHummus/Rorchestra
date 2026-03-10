"""
Memory hierarchy manager — handles parent/child scope relationships,
upward/sideways invalidation, and parent re-derivation.

Hierarchy:
    repository  (knows less-specific, sufficient info about domains)
      └── domain  (knows about its scripts)
            └── script  (per-file understanding)
                  └── contract  (cross-cutting shared state)
    environment  (live Studio state, independent)
    skill        (permanent procedural rules, never auto-invalidated)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.models.entities import (
    GraphEdge,
    EdgeKind,
    MemoryRecord,
    MemoryScope,
    Script,
)
from app.storage.database import get_session


# ── Scope ID conventions ─────────────────────────────────────────────────
#
# repository:<repo_id>
# domain:<domain_id>
# script:<script_id>
# contract:<contract_id>
# environment:<instance_path>
# skill:<skill_name>


def parent_scope_for(scope_id: str) -> str | None:
    """
    Determine the parent scope_id for a given scope.

    script:42  →  looks up script.domain_id → domain:<domain_id>
    domain:5   →  looks up domain.repo_id   → repository:<repo_id>
    repository/environment/skill → None (top-level)
    """
    prefix, _, raw_id = scope_id.partition(":")
    if not raw_id:
        return None

    session = get_session()
    try:
        if prefix == "script":
            script = session.get(Script, int(raw_id))
            if script and script.domain_id:
                return f"domain:{script.domain_id}"
            if script:
                return f"repository:{script.repo_id}"
        elif prefix == "domain":
            from app.models.entities import Domain
            domain = session.get(Domain, int(raw_id))
            if domain:
                return f"repository:{domain.repo_id}"
        # repository, environment, skill, contract → no parent
        return None
    finally:
        session.close()


def get_ancestors(scope_id: str) -> list[str]:
    """Walk up from a scope to the root, returning [parent, grandparent, ...]."""
    ancestors: list[str] = []
    current = parent_scope_for(scope_id)
    while current:
        ancestors.append(current)
        current = parent_scope_for(current)
    return ancestors


def get_children(scope_id: str) -> list[str]:
    """Find all direct child scope_ids for a given scope."""
    session = get_session()
    try:
        records = session.execute(
            select(MemoryRecord.scope_id).where(
                MemoryRecord.parent_scope_id == scope_id,
                MemoryRecord.invalidated_by.is_(None),
            )
        ).scalars().all()
        return list(set(records))
    finally:
        session.close()


def invalidate_scope(scope_id: str, reason: str) -> None:
    """Mark all active memories for a scope as invalidated."""
    session = get_session()
    try:
        session.execute(
            update(MemoryRecord)
            .where(
                MemoryRecord.scope_id == scope_id,
                MemoryRecord.invalidated_by.is_(None),
                MemoryRecord.promotion_policy != "permanent",
            )
            .values(
                invalidated_by=reason,
                updated_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    finally:
        session.close()


def invalidate_upward(scope_id: str, reason: str) -> list[str]:
    """
    Invalidate a scope and all its ancestors.

    When a script changes, its domain and repo summaries become stale.
    Returns list of all invalidated scope_ids.
    """
    invalidated = [scope_id]
    invalidate_scope(scope_id, reason)

    for ancestor in get_ancestors(scope_id):
        invalidate_scope(ancestor, f"child_invalidated:{scope_id}")
        invalidated.append(ancestor)

    return invalidated


def invalidate_sideways(script_id: int, reason: str) -> list[str]:
    """
    Invalidate memories of scripts that depend on the changed script.

    If script A requires script B, and B changes, A's memories
    may reference stale information about B's API.
    """
    session = get_session()
    try:
        # Find scripts that require this one (reverse edges)
        dependents = session.execute(
            select(GraphEdge.source_id).where(
                GraphEdge.target_id == script_id,
                GraphEdge.target_type == "script",
                GraphEdge.source_type == "script",
                GraphEdge.edge_kind == EdgeKind.requires,
            )
        ).scalars().all()

        invalidated: list[str] = []
        for dep_id in dependents:
            dep_scope = f"script:{dep_id}"
            invalidate_scope(dep_scope, reason)
            invalidated.append(dep_scope)

        return invalidated
    finally:
        session.close()


def propagate_invalidation(
    changed_script_ids: list[int],
    reason: str = "source_changed",
) -> dict[str, list[str]]:
    """
    Full invalidation cascade for one or more changed scripts.

    1. Invalidate each script's own memory
    2. Propagate upward to domain and repo
    3. Propagate sideways to dependents

    Returns a dict:
        {
            "upward": [list of scope_ids invalidated upward],
            "sideways": [list of scope_ids invalidated sideways],
        }
    """
    all_upward: list[str] = []
    all_sideways: list[str] = []

    for sid in changed_script_ids:
        scope_id = f"script:{sid}"
        upward = invalidate_upward(scope_id, reason)
        sideways = invalidate_sideways(sid, f"dependency_changed:{sid}")
        all_upward.extend(upward)
        all_sideways.extend(sideways)

    return {
        "upward": list(set(all_upward)),
        "sideways": list(set(all_sideways)),
    }


def get_stale_scopes(scope_prefix: str | None = None) -> list[str]:
    """
    List scope_ids that have only invalidated (stale) memories.
    These need re-investigation by subagents.
    """
    session = get_session()
    try:
        query = select(MemoryRecord.scope_id).where(
            MemoryRecord.invalidated_by.isnot(None),
        )
        if scope_prefix:
            query = query.where(MemoryRecord.scope_id.like(f"{scope_prefix}%"))

        stale_ids = set(session.execute(query).scalars().all())

        # Exclude scopes that also have fresh (non-invalidated) memories
        fresh_query = select(MemoryRecord.scope_id).where(
            MemoryRecord.invalidated_by.is_(None),
        )
        if scope_prefix:
            fresh_query = fresh_query.where(
                MemoryRecord.scope_id.like(f"{scope_prefix}%")
            )
        fresh_ids = set(session.execute(fresh_query).scalars().all())

        return sorted(stale_ids - fresh_ids)
    finally:
        session.close()

