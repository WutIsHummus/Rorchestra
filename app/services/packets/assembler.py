"""
Context packet assembler.

Builds compact ContextPacket objects that are the *only* thing given to
fresh external edit workers beyond direct file access.
"""

from __future__ import annotations

import json
from pathlib import Path

import tiktoken
from sqlalchemy import select

from app.config import settings
from app.models.entities import (
    Contract,
    GraphEdge,
    EdgeKind,
    MemoryRecord,
    Script,
    Task,
    ContextPacket as ContextPacketRow,
)
from app.models.schemas import ContextPacketSchema
from app.storage.database import get_session


def _estimate_tokens(text: str) -> int:
    """Rough token count using tiktoken cl100k_base."""
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        # Fallback: ~4 chars per token
        return len(text) // 4


def truncate_to_tokens(text: str, max_tokens: int, suffix: str = "\n... (truncated)") -> str:
    """Return text truncated to at most max_tokens (approximate)."""
    if not text or max_tokens <= 0:
        return text
    n = _estimate_tokens(text)
    if n <= max_tokens:
        return text
    # Approximate character count to stay under max_tokens
    target_len = int(len(text) * max_tokens / n) - len(suffix)
    if target_len <= 0:
        return text[: max_tokens * 4] + suffix
    return text[:target_len] + suffix


def _gather_1hop_deps(script_id: int, session) -> list[int]:
    """Return script IDs that are 1-hop require neighbours."""
    edges = session.execute(
        select(GraphEdge).where(
            GraphEdge.source_id == script_id,
            GraphEdge.source_type == "script",
            GraphEdge.edge_kind == EdgeKind.requires,
        )
    ).scalars().all()
    return [e.target_id for e in edges]


def assemble_packet(
    task_id: int,
    repo_root: str | Path,
) -> ContextPacketSchema:
    """
    Build a ContextPacket for *task_id*.

    1. Find the target script(s) from the task's scope.
    2. Gather 1-hop dependency summaries.
    3. Include file bodies only for target + 1-hop deps.
    4. Attach relevant contracts and memories.
    5. Enforce token budget.
    """
    repo_root = Path(repo_root)
    session = get_session()
    try:
        task = session.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")

        # Find target scripts by scope match (supports comma-separated scopes)
        target_scripts: list[Script] = []
        if task.target_scope:
            scopes = [s.strip() for s in task.target_scope.split(",") if s.strip()]
            from sqlalchemy import or_
            target_scripts = list(
                session.execute(
                    select(Script).where(
                        Script.repo_id == task.repo_id,
                        or_(*(Script.instance_path.contains(s) for s in scopes)),
                    )
                ).scalars().all()
            )

        # 1-hop deps
        dep_ids: set[int] = set()
        for ts in target_scripts:
            dep_ids.update(_gather_1hop_deps(ts.id, session))

        dep_scripts = []
        if dep_ids:
            dep_scripts = list(
                session.execute(
                    select(Script).where(Script.id.in_(dep_ids))
                ).scalars().all()
            )

        # Build script summaries
        relevant: list[dict] = []
        for s in target_scripts + dep_scripts:
            relevant.append({
                "instance_path": s.instance_path,
                "file_path": s.file_path,
                "script_type": s.script_type,
                "summary": s.summary or "(no summary)",
                "is_target": s in target_scripts,
            })

        # File bodies (targets + 1-hop only)
        file_bodies: dict[str, str] = {}
        budget_remaining = settings.default_token_budget
        for s in target_scripts + dep_scripts:
            fp = repo_root / s.file_path
            if fp.exists():
                body = fp.read_text(encoding="utf-8", errors="replace")
                tokens = _estimate_tokens(body)
                if tokens <= budget_remaining:
                    file_bodies[s.file_path] = body
                    budget_remaining -= tokens

        # Contracts
        contracts = list(
            session.execute(
                select(Contract).where(Contract.repo_id == task.repo_id)
            ).scalars().all()
        )
        relevant_contracts = [
            {"name": c.name, "kind": c.kind, "summary": c.summary}
            for c in contracts
        ]

        # Memories as invariants / risks
        invariants: list[str] = []
        risks: list[str] = []
        for s in target_scripts:
            mems = session.execute(
                select(MemoryRecord).where(
                    MemoryRecord.scope_id == f"script:{s.id}",
                    MemoryRecord.invalidated_by.is_(None),
                )
            ).scalars().all()
            for m in mems:
                if m.memory_type.value == "procedural":
                    invariants.append(m.content)
                elif m.memory_type.value == "episodic":
                    risks.append(m.content)

        # Inject matching skills as procedural invariants
        from app.services.memory.skill_loader import get_relevant_skills
        skill_rules = get_relevant_skills(
            runtime_side=task.runtime_side or "",
            target_scope=task.target_scope or "",
        )
        invariants.extend(skill_rules)

        packet = ContextPacketSchema(
            task_id=task_id,
            objective=task.description,
            target_scope=task.target_scope or "",
            runtime_side=task.runtime_side or "unknown",
            relevant_scripts=relevant,
            relevant_contracts=relevant_contracts,
            local_invariants=invariants,
            known_risks=risks,
            file_bodies=file_bodies,
            token_budget=settings.default_token_budget,
        )

        # Persist
        packet_json = packet.model_dump_json(indent=2)
        row = ContextPacketRow(
            task_id=task_id,
            packet_json=packet_json,
            token_estimate=_estimate_tokens(packet_json),
        )
        session.add(row)
        session.commit()

        return packet
    finally:
        session.close()
