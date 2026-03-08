"""
Canonical MCP capability dispatcher.

Implements the routing table from the design doc:
  primary → Roblox_Studio tools
  fallback → robloxstudio-mcp tools

Raw MCP output is stored in the artifact store.
Only compact ValidationArtifact-shaped dicts are returned.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.storage.artifacts import save_artifact


# ── Capability Routing Table ──────────────────────────────────────────────

# Each entry: canonical_name → (primary_tool, fallback_tool)
# Tool names match the MCP tool names from the design doc.
CAPABILITY_MAP: dict[str, tuple[str, str | None]] = {
    "list_studios": ("Roblox_Studio.list_roblox_studios", None),
    "set_active_studio": ("Roblox_Studio.set_active_studio", None),
    "search_tree": (
        "Roblox_Studio.search_game_tree",
        "robloxstudio-mcp.get_project_structure",
    ),
    "inspect_instance": (
        "Roblox_Studio.inspect_instance",
        "robloxstudio-mcp.get_instance_properties",
    ),
    "read_script": (
        "Roblox_Studio.script_read",
        "robloxstudio-mcp.get_script_source",
    ),
    "search_scripts": (
        "Roblox_Studio.script_grep",
        "robloxstudio-mcp.grep_scripts",
    ),
    "playtest_control": (
        "Roblox_Studio.start_stop_play",
        "robloxstudio-mcp.start_playtest",
    ),
    "playtest_logs": (
        "Roblox_Studio.get_console_output",
        "robloxstudio-mcp.get_playtest_output",
    ),
    "edit_script_bounded": (
        "Roblox_Studio.multi_edit",
        "robloxstudio-mcp.edit_script_lines",
    ),
    "execute_validation_luau": (
        "Roblox_Studio.execute_luau",
        None,  # robloxstudio-mcp.execute_luau only if explicitly allowed
    ),
    "get_class_metadata": (
        "robloxstudio-mcp.get_class_info",
        None,  # could fall back to docs MCP
    ),
}


# ── Dispatcher ────────────────────────────────────────────────────────────


class MCPDispatcher:
    """
    Routes canonical capability calls to the correct MCP tool,
    with automatic fallback and raw-output isolation.

    In the MVP, the actual MCP calls are delegated to callback functions
    that the orchestrator registers.  This keeps the dispatcher decoupled
    from the transport (subprocess, HTTP, direct MCP client, etc.).
    """

    def __init__(self) -> None:
        # tool_name → callable that actually executes the MCP tool
        self._executors: dict[str, Callable[..., Any]] = {}

    def register_executor(self, tool_name: str, fn: Callable[..., Any]) -> None:
        """Register a function that can execute the given raw MCP tool."""
        self._executors[tool_name] = fn

    def call(
        self,
        capability: str,
        params: dict[str, Any] | None = None,
        *,
        allow_fallback: bool = True,
    ) -> dict[str, Any]:
        """
        Invoke a canonical capability.

        1. Try the primary tool.
        2. If it fails and a fallback exists, try the fallback.
        3. Store raw output as an artifact.
        4. Return a compact result dict.
        """
        if capability not in CAPABILITY_MAP:
            return {
                "status": "error",
                "error": f"Unknown capability: {capability}",
            }

        primary, fallback = CAPABILITY_MAP[capability]
        params = params or {}

        # Try primary
        result = self._try_tool(primary, params)
        if result is not None:
            self._store_raw(capability, primary, result)
            return self._compact(capability, result)

        # Try fallback
        if allow_fallback and fallback:
            result = self._try_tool(fallback, params)
            if result is not None:
                self._store_raw(capability, fallback, result)
                return self._compact(capability, result)

        return {
            "status": "fail",
            "error": f"No executor available for {capability}",
        }

    def _try_tool(self, tool_name: str, params: dict[str, Any]) -> Any | None:
        fn = self._executors.get(tool_name)
        if fn is None:
            return None
        try:
            return fn(**params)
        except Exception as exc:
            return None

    def _store_raw(self, capability: str, tool_name: str, raw: Any) -> str:
        """Persist raw MCP output out of band."""
        ref = save_artifact(
            "mcp_raw",
            f"{capability}__{tool_name.replace('.', '_')}",
            json.dumps(raw, default=str) if not isinstance(raw, str) else raw,
        )
        return str(ref)

    @staticmethod
    def _compact(capability: str, raw: Any) -> dict[str, Any]:
        """
        Reduce raw MCP output to a compact result.

        The exact transformation is capability-specific;
        this default just wraps the raw data.
        """
        return {
            "status": "pass",
            "capability": capability,
            "data": raw,
        }


# Singleton
dispatcher = MCPDispatcher()
