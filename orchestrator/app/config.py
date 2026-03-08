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
        default="gemini", description="Path to Gemini CLI binary"
    )

    # MCP server executables / entrypoints
    roblox_studio_mcp_exe: str = Field(
        default=r"C:\Tools\rbx-studio-mcp\rbx-studio-mcp.exe",
        description="Path to official Roblox Studio MCP executable",
    )
    community_mcp_entrypoint: str = Field(
        default=r"D:\SPTS\robloxstudio-mcp\packages\robloxstudio-mcp\dist\index.js",
        description="Path to community robloxstudio-mcp entrypoint",
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

    # ── Tunables ───────────────────────────────────────────────────────
    default_token_budget: int = Field(
        default=8000,
        description="Default token budget for context packets",
    )
    worker_timeout_secs: int = Field(
        default=120,
        description="Default timeout in seconds for worker invocations",
    )
    max_worker_retries: int = Field(
        default=1,
        description="How many times to retry a timed-out worker with smaller packet",
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
