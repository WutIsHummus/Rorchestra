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


# ── Rojo sourcemap types ─────────────────────────────────────────────────


class SourcemapNode(BaseModel):
    """One node in a Rojo sourcemap tree."""
    name: str
    className: str
    filePaths: list[str] = Field(default_factory=list)
    children: list[SourcemapNode] = Field(default_factory=list)


SourcemapNode.model_rebuild()  # resolve forward ref
