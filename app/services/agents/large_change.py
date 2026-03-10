"""
Large-change (revamp) workflow: impact analysis, migration brief,
and retrieval behavior for multi-step redesigns.

- Impact analysis: affected domains, scripts, contracts, dependency neighborhoods.
- Migration brief: old state, target state, invariants, migration steps (stable target for workers).
- Memory phases: stable (pre-revamp), migration (transitional), finalized (post-validation).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import (
    Contract,
    Domain,
    GraphEdge,
    EdgeKind,
    RevampSession,
    Script,
)
from app.services.agents.tools import search_graph


def run_impact_analysis(
    domain_ids: list[int],
    session: Session,
    *,
    max_hops: int = 2,
) -> tuple[list[int], list[int], list[int]]:
    """
    Identify scripts, contracts, and domains likely affected by a revamp.
    Starts from scripts in the given domains and expands via requires,
    provides_contract, consumes_contract for up to max_hops.
    Returns (affected_script_ids, affected_contract_ids, affected_domain_ids).
    """
    if not domain_ids:
        return [], [], []

    rows = session.execute(
        select(Script.id).where(Script.domain_id.in_(domain_ids))
    ).all()
    script_ids = list({r[0] for r in rows})
    contract_ids: set[int] = set()
    edge_kinds = ["requires", "provides_contract", "consumes_contract"]

    for _ in range(max_hops):
        next_script_ids: set[int] = set()
        for sid in script_ids:
            for edge_kind in edge_kinds:
                for e in search_graph(sid, "script", edge_kind, "outgoing"):
                    tid = e.get("target_id")
                    ttype = e.get("target_type", "")
                    if ttype == "script" and tid:
                        next_script_ids.add(tid)
                    elif ttype == "contract" and tid:
                        contract_ids.add(tid)
        if not next_script_ids or next_script_ids <= set(script_ids):
            break
        script_ids = list(set(script_ids) | next_script_ids)

    # Domains that contain any affected script
    if script_ids:
        domain_ids_affected = session.execute(
            select(Script.domain_id).where(Script.id.in_(script_ids), Script.domain_id.isnot(None))
        ).scalars().all()
        affected_domain_ids = list({r[0] for r in domain_ids_affected if r[0]})
    else:
        affected_domain_ids = list(domain_ids)

    return script_ids, list(contract_ids), affected_domain_ids


def generate_migration_brief_from_task(task: Any) -> dict[str, Any]:
    """
    Produce a minimal architecture delta brief from task description.
    Workers can use this as the stable target during the revamp.
    """
    return {
        "old_state": "",
        "target_state": task.description or "",
        "invariants_to_preserve": [],
        "migration_steps": [],
        "notes": "Fill old_state, invariants, and steps as the revamp progresses.",
    }


def ensure_migration_brief(
    task: Any,
    session: Session,
    console: Any,
    *,
    impact_script_ids: list[int] | None = None,
    impact_contract_ids: list[int] | None = None,
) -> tuple[int | None, dict[str, Any]]:
    """
    Ensure a RevampSession exists for this task with a migration brief.
    If task.revamp_session_id is set, use that session and optionally update brief.
    Otherwise create a new RevampSession and set task.revamp_session_id.
    Returns (revamp_session_id, brief_dict).
    """
    brief = generate_migration_brief_from_task(task)
    if impact_script_ids is not None:
        brief["affected_script_count"] = len(impact_script_ids)
    if impact_contract_ids is not None:
        brief["affected_contract_count"] = len(impact_contract_ids)

    revamp_id = getattr(task, "revamp_session_id", None)
    if revamp_id:
        revamp = session.get(RevampSession, revamp_id)
        if revamp:
            revamp.migration_brief_json = json.dumps(brief)
            session.commit()
            if console:
                console.print("[dim]  [large-change] Updated migration brief for existing revamp session.[/dim]")
            return revamp_id, brief

    revamp = RevampSession(
        repo_id=task.repo_id,
        status="active",
        migration_brief_json=json.dumps(brief),
    )
    session.add(revamp)
    session.flush()
    session.refresh(revamp)
    if hasattr(task, "revamp_session_id"):
        task.revamp_session_id = revamp.id
    session.commit()
    if console:
        console.print(f"[cyan]  [large-change] Created revamp session {revamp.id} with migration brief.[/cyan]")
    return revamp.id, brief


def get_migration_brief(session: Session, revamp_session_id: int | None) -> dict[str, Any]:
    """Load migration brief JSON for a revamp session; empty dict if none."""
    if not revamp_session_id:
        return {}
    revamp = session.get(RevampSession, revamp_session_id)
    if not revamp or not revamp.migration_brief_json:
        return {}
    try:
        return json.loads(revamp.migration_brief_json)
    except (json.JSONDecodeError, TypeError):
        return {}
