"""
Graph edge construction and refresh.

Builds ``requires``, ``exports``, ``belongs_to_domain``, and
``mapped_to_instance`` edges from the indexed scripts.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.entities import GraphEdge, EdgeKind, Script
from app.models.schemas import GraphDelta
from app.storage.database import get_session


def build_require_edges(repo_id: int) -> int:
    """
    Walk all scripts for *repo_id* and create ``requires`` edges
    wherever script A's require list references script B's instance path.

    Returns the number of edges created.
    """
    session = get_session()
    try:
        scripts = session.execute(
            select(Script).where(Script.repo_id == repo_id)
        ).scalars().all()

        # Build lookup: instance_path → script id
        path_to_id: dict[str, int] = {}
        for s in scripts:
            if s.instance_path:
                path_to_id[s.instance_path] = s.id

        count = 0
        for s in scripts:
            for req_path in s.requires:
                # Try exact match first
                target_id = path_to_id.get(req_path)
                if target_id is None:
                    # Try matching the tail (e.g. "game.ReplicatedStorage.Foo" → last parts)
                    for ip, sid in path_to_id.items():
                        if ip.endswith(req_path) or req_path.endswith(ip.split(".")[-1]):
                            target_id = sid
                            break
                if target_id and target_id != s.id:
                    session.add(GraphEdge(
                        source_id=s.id,
                        source_type="script",
                        target_id=target_id,
                        target_type="script",
                        edge_kind=EdgeKind.requires,
                    ))
                    count += 1

        session.commit()
        return count
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def build_or_refresh_graph(repo_id: int) -> GraphDelta:
    """
    Build (or rebuild) all graph edges for a repository.
    Currently handles ``requires`` edges;
    additional edge types will be added in later milestones.
    """
    session = get_session()
    try:
        # Count existing edges
        existing = session.execute(
            select(GraphEdge).join(
                Script, GraphEdge.source_id == Script.id
            ).where(Script.repo_id == repo_id, GraphEdge.source_type == "script")
        ).scalars().all()
        old_count = len(existing)

        # Delete old require edges for this repo
        for edge in existing:
            if edge.edge_kind == EdgeKind.requires:
                session.delete(edge)
        session.commit()
    finally:
        session.close()

    new_count = build_require_edges(repo_id)

    return GraphDelta(
        added_edges=new_count,
        removed_edges=old_count,
    )
