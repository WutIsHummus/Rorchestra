"""
Agent tools — functions that investigator subagents call to dynamically
retrieve context from the orchestrator's database.

These are exposed to Gemini CLI subagents as callable tools. Each tool
reads from SQLite and returns structured JSON that the subagent uses to
decide what else to explore.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select, or_

from app.models.entities import (
    Contract,
    Domain,
    DomainKind,
    GraphEdge,
    EdgeKind,
    MemoryRecord,
    MemoryScope,
    Script,
    Repository,
)
from app.config import settings
from app.storage.database import get_session


# ── Script discovery ─────────────────────────────────────────────────────


def list_scripts(
    repo_id: int = 1,
    domain: str | None = None,
    pattern: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """
    List scripts with their summaries and metadata.

    Args:
        repo_id: Repository to query.
        domain: Optional domain name filter (server/client/shared).
        pattern: Optional substring match on instance_path.
        limit: Max results.

    Returns list of dicts with: id, instance_path, script_type, domain,
    summary, line_count, file_path.
    """
    if limit is None:
        limit = getattr(settings, "list_scripts_limit", 200)
    session = get_session()
    try:
        query = select(Script).where(Script.repo_id == repo_id)

        if domain:
            # Join to domain table
            domain_rows = session.execute(
                select(Domain.id).where(
                    Domain.repo_id == repo_id,
                    Domain.name == domain,
                )
            ).scalars().all()
            if domain_rows:
                query = query.where(Script.domain_id.in_(domain_rows))

        if pattern:
            query = query.where(Script.instance_path.contains(pattern))

        query = query.limit(limit)
        scripts = session.execute(query).scalars().all()

        return [
            {
                "id": s.id,
                "instance_path": s.instance_path,
                "script_type": s.script_type,
                "domain_id": s.domain_id,
                "summary": s.summary or "(no summary)",
                "line_count": s.line_count,
                "file_path": s.file_path,
            }
            for s in scripts
        ]
    finally:
        session.close()


def read_script_source(
    script_id: int,
    repo_root: str | None = None,
    max_chars: int = 15_000,
) -> dict[str, Any]:
    """
    Read the full source code of a specific script.

    Returns dict with: instance_path, script_type, source, truncated.
    Only call this when you need the ACTUAL code — summaries are lighter.
    """
    session = get_session()
    try:
        script = session.get(Script, script_id)
        if not script:
            return {"error": f"Script {script_id} not found"}

        if not repo_root:
            repo = session.get(Repository, script.repo_id)
            repo_root = repo.root_path if repo else "."

        fp = Path(repo_root) / script.file_path
        if fp.exists():
            source = fp.read_text(encoding="utf-8", errors="replace")
            truncated = len(source) > max_chars
            if truncated:
                source = source[:max_chars] + "\n... (truncated)"
        else:
            source = "(file not found)"
            truncated = False

        return {
            "id": script.id,
            "instance_path": script.instance_path,
            "script_type": script.script_type,
            "file_path": script.file_path,
            "requires": script.requires,
            "exports": script.exports,
            "source": source,
            "truncated": truncated,
        }
    finally:
        session.close()


# ── Memory access ────────────────────────────────────────────────────────


def read_memory(scope_id: str) -> list[dict[str, Any]]:
    """
    Get all active (non-invalidated) memory records for a scope.

    Returns list of dicts with: memory_type, scope_level, content,
    confidence, freshness_ts.
    """
    session = get_session()
    try:
        records = session.execute(
            select(MemoryRecord).where(
                MemoryRecord.scope_id == scope_id,
                MemoryRecord.invalidated_by.is_(None),
            )
        ).scalars().all()

        return [
            {
                "memory_type": m.memory_type.value if m.memory_type else None,
                "scope_level": m.scope_level.value if m.scope_level else None,
                "content": m.content,
                "confidence": m.confidence,
                "freshness_ts": str(m.freshness_ts) if m.freshness_ts else None,
            }
            for m in records
        ]
    finally:
        session.close()


# ── Graph traversal ──────────────────────────────────────────────────────


def search_graph(
    from_id: int,
    from_type: str = "script",
    edge_kind: str | None = None,
    direction: str = "outgoing",
) -> list[dict[str, Any]]:
    """
    Traverse the dependency graph from a given node.

    Args:
        from_id: Source node ID.
        from_type: Source type (script, domain, contract).
        edge_kind: Optional filter (requires, exports, etc.).
        direction: 'outgoing' (from→to) or 'incoming' (to→from).

    Returns list of edges with target info.
    """
    session = get_session()
    try:
        if direction == "outgoing":
            query = select(GraphEdge).where(
                GraphEdge.source_id == from_id,
                GraphEdge.source_type == from_type,
            )
        else:
            query = select(GraphEdge).where(
                GraphEdge.target_id == from_id,
                GraphEdge.target_type == from_type,
            )

        if edge_kind:
            query = query.where(GraphEdge.edge_kind == EdgeKind(edge_kind))

        edges = session.execute(query).scalars().all()

        results = []
        for e in edges:
            entry = {
                "source_id": e.source_id,
                "source_type": e.source_type,
                "target_id": e.target_id,
                "target_type": e.target_type,
                "edge_kind": e.edge_kind.value if e.edge_kind else None,
            }

            # Enrich with script name if target/source is a script
            other_id = e.target_id if direction == "outgoing" else e.source_id
            other_type = e.target_type if direction == "outgoing" else e.source_type
            if other_type == "script":
                s = session.get(Script, other_id)
                if s:
                    entry["script_path"] = s.instance_path
                    entry["script_summary"] = s.summary

            results.append(entry)

        return results
    finally:
        session.close()


# ── Domain and contract access ───────────────────────────────────────────


def list_domains(repo_id: int = 1) -> list[dict[str, Any]]:
    """List all domains with their summaries and script counts."""
    session = get_session()
    try:
        domains = session.execute(
            select(Domain).where(Domain.repo_id == repo_id)
        ).scalars().all()

        return [
            {
                "id": d.id,
                "name": d.name,
                "kind": d.kind.value if d.kind else None,
                "summary": d.summary or "(no summary)",
                "script_count": len(d.scripts) if d.scripts else 0,
            }
            for d in domains
        ]
    finally:
        session.close()


def get_contracts(
    repo_id: int = 1,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """List contracts (remotes, shared config, UI contracts)."""
    session = get_session()
    try:
        query = select(Contract).where(Contract.repo_id == repo_id)
        if kind:
            query = query.where(Contract.kind == kind)

        contracts = session.execute(query).scalars().all()
        return [
            {
                "id": c.id,
                "name": c.name,
                "kind": c.kind,
                "summary": c.summary or "(no summary)",
            }
            for c in contracts
        ]
    finally:
        session.close()


# ── Tool registry (for subagent dispatch) ────────────────────────────────

AGENT_TOOLS = {
    "list_scripts": list_scripts,
    "read_script_source": read_script_source,
    "read_memory": read_memory,
    "search_graph": search_graph,
    "list_domains": list_domains,
    "get_contracts": get_contracts,
}


def dispatch_tool(tool_name: str, args: dict[str, Any]) -> Any:
    """Call an agent tool by name with the given arguments."""
    func = AGENT_TOOLS.get(tool_name)
    if not func:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        return func(**args)
    except Exception as exc:
        return {"error": str(exc)}
