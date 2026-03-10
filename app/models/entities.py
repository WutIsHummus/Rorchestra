"""
SQLAlchemy ORM models for the orchestrator data model.

Tables:  repositories, domains, scripts, symbols, contracts,
         graph_edges, memory_records, tasks, context_packets,
         edit_proposals, validation_artifacts, run_traces
"""

from __future__ import annotations

import datetime as _dt
import enum
import json
from typing import Any, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


# ── Base ──────────────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


# ── Enums ─────────────────────────────────────────────────────────────────


class DomainKind(str, enum.Enum):
    server = "server"
    client = "client"
    shared = "shared"
    ui = "ui"


class MemoryType(str, enum.Enum):
    semantic = "semantic"
    episodic = "episodic"
    procedural = "procedural"
    environment = "environment"


class MemoryPhase(str, enum.Enum):
    """During a large revamp: stable = pre-revamp, migration = transitional, finalized = post-validation."""
    stable = "stable"
    migration = "migration"
    finalized = "finalized"


class MemoryScope(str, enum.Enum):
    repository = "repository"    # Whole-repo architectural knowledge
    domain = "domain"            # Domain-level (server/client/shared)
    script = "script"            # Per-script understanding
    contract = "contract"        # Cross-cutting contracts (remotes, config)
    environment = "environment"  # Live Studio state (from MCP)
    skill = "skill"              # Procedural rules (permanent)


class EdgeKind(str, enum.Enum):
    requires = "requires"
    exports = "exports"
    references = "references"
    belongs_to_domain = "belongs_to_domain"
    mapped_to_instance = "mapped_to_instance"
    consumes_contract = "consumes_contract"
    provides_contract = "provides_contract"
    depends_on = "depends_on"
    invalidates = "invalidates"


class ValidationStatus(str, enum.Enum):
    passed = "pass"
    failed = "fail"
    uncertain = "uncertain"


class TaskStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    validating = "validating"
    completed = "completed"
    failed = "failed"


# ── Helpers ───────────────────────────────────────────────────────────────


def _utcnow():
    return _dt.datetime.now(_dt.timezone.utc)


def _json_col():
    """Text column that stores JSON-serialisable data."""
    return Column(Text, default="{}")


# ── Repository ────────────────────────────────────────────────────────────


class Repository(Base):
    __tablename__ = "repositories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    root_path = Column(String, nullable=False, unique=True)
    rojo_project_path = Column(String, nullable=True)
    sourcemap_path = Column(String, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    domains = relationship("Domain", back_populates="repository", cascade="all, delete-orphan")
    scripts = relationship("Script", back_populates="repository", cascade="all, delete-orphan")


# ── Domain ────────────────────────────────────────────────────────────────


class Domain(Base):
    __tablename__ = "domains"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_id = Column(Integer, ForeignKey("repositories.id"), nullable=False)
    name = Column(String, nullable=False)
    kind = Column(Enum(DomainKind), nullable=False)
    summary = Column(Text, default="")
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    repository = relationship("Repository", back_populates="domains")
    scripts = relationship("Script", back_populates="domain")


# ── Script ────────────────────────────────────────────────────────────────


class Script(Base):
    __tablename__ = "scripts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_id = Column(Integer, ForeignKey("repositories.id"), nullable=False)
    domain_id = Column(Integer, ForeignKey("domains.id"), nullable=True)
    file_path = Column(String, nullable=False)
    rojo_path = Column(String, nullable=True)
    instance_path = Column(String, nullable=True)
    script_type = Column(String, nullable=True)          # Script, LocalScript, ModuleScript
    line_count = Column(Integer, default=0)
    summary = Column(Text, default="")
    exports_json = Column(Text, default="[]")            # JSON list of exported symbols
    requires_json = Column(Text, default="[]")           # JSON list of require paths
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    repository = relationship("Repository", back_populates="scripts")
    domain = relationship("Domain", back_populates="scripts")
    symbols = relationship("Symbol", back_populates="script", cascade="all, delete-orphan")

    # Convenience helpers for the JSON columns
    @property
    def exports(self) -> list[str]:
        return json.loads(self.exports_json or "[]")

    @exports.setter
    def exports(self, value: list[str]):
        self.exports_json = json.dumps(value)

    @property
    def requires(self) -> list[str]:
        return json.loads(self.requires_json or "[]")

    @requires.setter
    def requires(self, value: list[str]):
        self.requires_json = json.dumps(value)


# ── Symbol ────────────────────────────────────────────────────────────────


class Symbol(Base):
    __tablename__ = "symbols"

    id = Column(Integer, primary_key=True, autoincrement=True)
    script_id = Column(Integer, ForeignKey("scripts.id"), nullable=False)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=True)                 # function, type, constant, …
    line_start = Column(Integer, nullable=True)
    line_end = Column(Integer, nullable=True)
    signature = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow)

    script = relationship("Script", back_populates="symbols")


# ── Contract ──────────────────────────────────────────────────────────────


class Contract(Base):
    __tablename__ = "contracts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_id = Column(Integer, ForeignKey("repositories.id"), nullable=False)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=False)                # remote, shared_config, ui_contract
    definition_json = Column(Text, default="{}")
    summary = Column(Text, default="")
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


# ── Graph Edge ────────────────────────────────────────────────────────────


class GraphEdge(Base):
    __tablename__ = "graph_edges"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(Integer, nullable=False)
    source_type = Column(String, nullable=False)         # script, domain, contract, symbol
    target_id = Column(Integer, nullable=False)
    target_type = Column(String, nullable=False)
    edge_kind = Column(Enum(EdgeKind), nullable=False)
    metadata_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=_utcnow)


# ── Memory Record ────────────────────────────────────────────────────────


class MemoryRecord(Base):
    __tablename__ = "memory_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope_id = Column(String, nullable=False)            # e.g. "script:42" or "domain:server"
    scope_level = Column(Enum(MemoryScope), nullable=True)  # hierarchy layer
    parent_scope_id = Column(String, nullable=True)      # parent in hierarchy (e.g. domain:server for script:42)
    memory_type = Column(Enum(MemoryType), nullable=False)
    memory_phase = Column(Enum(MemoryPhase), default=MemoryPhase.stable)  # stable | migration | finalized
    content = Column(Text, nullable=False)
    confidence = Column(Float, default=1.0)
    freshness_ts = Column(DateTime, default=_utcnow)
    source_refs_json = Column(Text, default="[]")
    derived_from_json = Column(Text, default="[]")       # child scope_ids this was derived from
    promotion_policy = Column(String, default="default")
    invalidated_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


# ── Task ──────────────────────────────────────────────────────────────────


class RevampSession(Base):
    """One large-change revamp: holds migration brief and batches; tasks can attach to it."""
    __tablename__ = "revamp_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_id = Column(Integer, ForeignKey("repositories.id"), nullable=False)
    status = Column(String, default="active")           # active, completed, abandoned
    migration_brief_json = Column(Text, default="{}")   # old_state, target_state, invariants, migration_steps
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_id = Column(Integer, ForeignKey("repositories.id"), nullable=False)
    description = Column(Text, nullable=False)
    status = Column(Enum(TaskStatus), default=TaskStatus.pending)
    target_scope = Column(String, nullable=True)
    runtime_side = Column(String, nullable=True)         # server, client, shared
    large_change_mode = Column(Integer, default=0)        # 0 = no, 1 = yes (impact planning, migration brief, broader retrieval)
    revamp_session_id = Column(Integer, ForeignKey("revamp_sessions.id"), nullable=True)
    batch_index = Column(Integer, nullable=True)          # 1-based batch within revamp (e.g. "introduce transport", "update consumers")
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


# ── Context Packet ────────────────────────────────────────────────────────


class ContextPacket(Base):
    __tablename__ = "context_packets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    packet_json = Column(Text, nullable=False)
    token_estimate = Column(Integer, default=0)
    created_at = Column(DateTime, default=_utcnow)


# ── Edit Proposal ─────────────────────────────────────────────────────────


class EditProposal(Base):
    __tablename__ = "edit_proposals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    patch_content = Column(Text, nullable=False)         # unified diff or JSON
    source = Column(String, default="gemini_worker")     # which worker produced it
    accepted = Column(Integer, default=0)                # 0 = pending, 1 = accepted, -1 = rejected
    review_notes = Column(Text, default="")
    created_at = Column(DateTime, default=_utcnow)


# ── Validation Artifact ──────────────────────────────────────────────────


class ValidationArtifact(Base):
    __tablename__ = "validation_artifacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)
    target = Column(String, nullable=False)
    status = Column(Enum(ValidationStatus), nullable=False)
    key_findings = Column(Text, default="")
    actual_paths_json = Column(Text, default="[]")
    confidence = Column(Float, default=1.0)
    recommended_action = Column(Text, default="")
    raw_artifact_ref = Column(String, nullable=True)     # path to raw MCP output file
    created_at = Column(DateTime, default=_utcnow)


# ── Run Trace ─────────────────────────────────────────────────────────────


class RunTrace(Base):
    __tablename__ = "run_traces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)
    worker_type = Column(String, nullable=False)
    started_at = Column(DateTime, default=_utcnow)
    finished_at = Column(DateTime, nullable=True)
    exit_code = Column(Integer, nullable=True)
    transcript_ref = Column(String, nullable=True)       # path to transcript file
    metrics_json = Column(Text, default="{}")
