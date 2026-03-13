"""
Centralised configuration for the orchestrator.

All paths and tunables are loaded from environment variables with sensible
defaults.  Import ``settings`` from this module wherever you need config.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Orchestrator-wide settings loaded from env vars / .env."""

    model_config = {"env_prefix": "ORCH_"}

    # ── Paths ──────────────────────────────────────────────────────────
    rojo_bin: str = Field(default="rojo", description="Path to rojo binary")
    luau_lsp_bin: str = Field(
        default="luau-lsp", description="Path to luau-lsp binary"
    )
    gemini_cli_bin: str = Field(
        default="gemini",
        description="Path to Gemini CLI binary (assumes on PATH; override with ORCH_GEMINI_CLI_BIN)",
    )
    # CWD when invoking Gemini CLI so it loads .gemini/settings.json and .gemini/agents/ from the orchestrator.
    gemini_cli_cwd: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent,
        description="Project directory for Gemini CLI (orchestrator root, where .gemini lives)",
    )

    # MCP server executables / entrypoints
    roblox_studio_mcp_exe: str = Field(
        default="",
        description="Path to official Roblox Studio MCP executable (set via ORCH_ROBLOX_STUDIO_MCP_EXE)",
    )
    community_mcp_entrypoint: str = Field(
        default="npx -y robloxstudio-mcp@latest",
        description="Command or path for community robloxstudio-mcp (override with ORCH_COMMUNITY_MCP_ENTRYPOINT)",
    )

    # Storage
    db_url: str = Field(
        default="sqlite:///orchestrator.db",
        description="SQLAlchemy database URL",
    )
    artifacts_dir: Path = Field(
        default=Path("artifacts"),
        description="Directory for runtime artifacts (packets, diffs, MCP raw output)",
    )
    skills_dir: Path = Field(
        default=Path("skills"),
        description="Directory containing skill Markdown files with procedural rules",
    )

    # ── Tunables ───────────────────────────────────────────────────────
    default_token_budget: int = Field(
        default=64_000,
        description="Token budget for context packet file_bodies (shared across all scripts so none are dropped)",
    )
    worker_timeout_secs: int = Field(
        default=300,
        description="Default timeout in seconds for worker invocations",
    )
    max_worker_retries: int = Field(
        default=1,
        description="How many times to retry a timed-out worker with smaller packet",
    )
    investigation_workers: int = Field(
        default=2,
        description="Max parallel workers for investigation (docs + deep read, and chunked domain-investigator)",
    )
    investigation_concurrency: int = Field(
        default=10,
        description="Global cap on concurrent investigation work (outer chunk workers + inner source-read).",
    )
    investigation_phase_timeout_secs: int = Field(
        default=300,
        description="Per-phase timeout (docs, deep-read). On timeout, phase returns partial/empty and investigation continues.",
    )
    # File/scope limits for revamps (raise these for full-codebase changes)
    max_domains_triage: int = Field(
        default=10,
        description="Max domains to include in Phase 1 triage (0 = no limit; revamps need more than 3).",
    )
    max_scripts_per_investigation: int = Field(
        default=0,
        description="Max scripts to include in investigation (0 = no limit; set e.g. 500 for very large revamps).",
    )
    max_scripts_per_deep_read_chunk: int = Field(
        default=25,
        description="Max scripts per deep-read chunk (keeps each prompt bounded; more chunks run in parallel).",
    )
    max_deep_read_parallel_chunks: int = Field(
        default=8,
        description="Max parallel deep-read chunks so revamps can process many files.",
    )
    list_scripts_limit: int = Field(
        default=200,
        description="Default max scripts returned by list_scripts (for agents exploring the codebase).",
    )
    max_files_per_edit: int = Field(
        default=25,
        description="Max files per edit-worker run; when packet has more, run worker in batches and apply each patch.",
    )

    # ── Hybrid triage ─────────────────────────────────────────────────
    max_ai_domain_additions: int = Field(
        default=2,
        description="Max extra domains the AI investigator can add beyond the deterministic prefilter set.",
    )
    max_ai_neighbor_requests: int = Field(
        default=5,
        description="Max neighbor scripts the AI investigator can request during script triage.",
    )
    triage_ai_timeout_secs: int = Field(
        default=180,
        description="Timeout for each AI triage review call (Phase 1b, Phase 2b). On timeout, falls back to prefilter.",
    )

    # ── Domain mapping ────────────────────────────────────────────────
    # Optional override: Rojo tree prefixes → logical domains.
    # When empty (default), domains are auto-detected from the Rojo
    # project tree at ingest time so that non-standard layouts work.
    domain_map_overrides: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional overrides for Rojo tree prefix → domain name.  "
            "Leave empty to auto-detect from the project's sourcemap."
        ),
    )

    # Well-known hints used during auto-detection when no override is set.
    # These are matched case-insensitively against top-level tree names.
    domain_hints: dict[str, str] = Field(
        default={
            "ServerScriptService": "server",
            "ServerStorage": "server",
            "StarterPlayerScripts": "client",
            "StarterCharacterScripts": "client",
            "StarterGui": "client",
            "ReplicatedStorage": "shared",
            "ReplicatedFirst": "shared",
        },
        description=(
            "Well-known Roblox services → domain mapping used as hints "
            "during auto-detection.  Projects can override via domain_map_overrides."
        ),
    )

    # ── Log level ─────────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


settings = Settings()
