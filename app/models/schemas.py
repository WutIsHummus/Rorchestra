"""
Pydantic v2 schemas for request/response shapes and packet serialisation.
These are the API-layer representations — ORM models live in entities.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Ingestion ─────────────────────────────────────────────────────────────


class ScriptInfo(BaseModel):
    file_path: str
    rojo_path: str | None = None
    instance_path: str | None = None
    script_type: str | None = None
    line_count: int = 0
    requires: list[str] = Field(default_factory=list)
    exports: list[str] = Field(default_factory=list)


class DomainInfo(BaseModel):
    name: str
    kind: str
    scripts: list[ScriptInfo] = Field(default_factory=list)


class RepoSnapshot(BaseModel):
    """Result of ingesting a repository."""
    repo_id: int
    name: str
    root_path: str
    rojo_project_path: str | None = None
    domains: list[DomainInfo] = Field(default_factory=list)
    script_count: int = 0
    edge_count: int = 0


# ── Graph ─────────────────────────────────────────────────────────────────


class GraphDelta(BaseModel):
    added_edges: int = 0
    removed_edges: int = 0
    updated_edges: int = 0


# ── Memory ────────────────────────────────────────────────────────────────


# ── Investigation merge (provenance + deterministic dedupe) ──────────────────

# Phase priority for conflict resolution: higher = preferred when deduping.
PHASE_PRIORITY: dict[str, int] = {
    "deep_read": 4,
    "docs": 3,
    "env": 2,
    "memory": 1,
    "skills": 0,
}


class InvestigationProvenance(BaseModel):
    """Provenance for a single invariant or risk from investigation."""
    phase: str  # docs | deep_read | skills | memory | env
    chunk_id: int | None = None
    script_ids: list[int] = Field(default_factory=list)


class InvariantEntry(BaseModel):
    """Single invariant with provenance for deterministic merge."""
    text: str
    provenance: InvestigationProvenance


class RiskEntry(BaseModel):
    """Single risk with provenance for deterministic merge."""
    text: str
    provenance: InvestigationProvenance


def _normalize_merge_key(text: str) -> str:
    """Deterministic key for deduplication: strip, lower, collapse whitespace."""
    return " ".join(str(text).strip().lower().split())


def merge_invariant_entries(entries: list[InvariantEntry]) -> list[InvariantEntry]:
    """
    Dedupe by normalized text; on conflict keep one entry by schema-driven rule:
    prefer phase with higher PHASE_PRIORITY, then more script_ids.
    Order of output is deterministic (sorted by key then by priority).
    """
    by_key: dict[str, list[InvariantEntry]] = {}
    for e in entries:
        k = _normalize_merge_key(e.text)
        if not k:
            continue
        by_key.setdefault(k, []).append(e)
    result: list[InvariantEntry] = []
    for k, candidates in sorted(by_key.items()):
        best = max(
            candidates,
            key=lambda x: (
                PHASE_PRIORITY.get(x.provenance.phase, -1),
                len(x.provenance.script_ids),
            ),
        )
        result.append(best)
    return sorted(result, key=lambda e: (_normalize_merge_key(e.text), -PHASE_PRIORITY.get(e.provenance.phase, -1)))


def merge_risk_entries(entries: list[RiskEntry]) -> list[RiskEntry]:
    """Same as merge_invariant_entries for risks."""
    by_key: dict[str, list[RiskEntry]] = {}
    for e in entries:
        k = _normalize_merge_key(e.text)
        if not k:
            continue
        by_key.setdefault(k, []).append(e)
    result: list[RiskEntry] = []
    for k, candidates in sorted(by_key.items()):
        best = max(
            candidates,
            key=lambda x: (
                PHASE_PRIORITY.get(x.provenance.phase, -1),
                len(x.provenance.script_ids),
            ),
        )
        result.append(best)
    return sorted(result, key=lambda e: (_normalize_merge_key(e.text), -PHASE_PRIORITY.get(e.provenance.phase, -1)))


class MemoryRecordSchema(BaseModel):
    scope_id: str
    memory_type: str
    content: str
    confidence: float = 1.0
    freshness_ts: datetime | None = None
    source_refs: list[str] = Field(default_factory=list)


# ── Context Packet ────────────────────────────────────────────────────────


class ContextPacketSchema(BaseModel):
    """The compact context given to a fresh external edit worker."""
    task_id: int
    objective: str
    target_scope: str
    runtime_side: str                          # server | client | shared
    rojo_path: str | None = None
    expected_instance_path: str | None = None
    relevant_scripts: list[dict[str, Any]] = Field(default_factory=list)
    relevant_contracts: list[dict[str, Any]] = Field(default_factory=list)
    local_invariants: list[str] = Field(default_factory=list)
    known_risks: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    validation_requirements: list[str] = Field(default_factory=list)
    file_bodies: dict[str, str] = Field(default_factory=dict)
    migration_brief: dict[str, Any] = Field(default_factory=dict)  # large-change: old_state, target_state, invariants, steps
    token_budget: int = 8000


# ── Validation ────────────────────────────────────────────────────────────


class ValidationResult(BaseModel):
    target: str
    status: str                                # pass | fail | uncertain
    key_findings: str = ""
    actual_paths: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    recommended_action: str = ""
    raw_artifact_ref: str | None = None


# ── Worker ────────────────────────────────────────────────────────────────


class WorkerResult(BaseModel):
    worker_type: str
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    patch_content: str | None = None
    transcript_ref: str | None = None
    elapsed_secs: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    luau_diagnostics: list[dict] = Field(default_factory=list, description="luau-lsp diagnostics for changed files")


# ── Rojo sourcemap types ─────────────────────────────────────────────────


class SourcemapNode(BaseModel):
    """One node in a Rojo sourcemap tree."""
    name: str
    className: str
    filePaths: list[str] = Field(default_factory=list)
    children: list[SourcemapNode] = Field(default_factory=list)


SourcemapNode.model_rebuild()  # resolve forward ref
