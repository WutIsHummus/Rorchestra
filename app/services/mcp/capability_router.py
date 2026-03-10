"""
Canonical MCP capability router — maps high-level capabilities to
specific MCP server tools with fallback chains.

Agents call `invoke_capability("inspect_instance", path=...)` instead
of raw tool names. The router selects the best MCP server and handles
fallback if the primary fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ToolMapping:
    server: str
    tool_name: str


@dataclass
class CapabilityRoute:
    primary: ToolMapping
    fallback: ToolMapping | None = None


# ── Capability map ───────────────────────────────────────────────────────

CAPABILITY_MAP: dict[str, CapabilityRoute] = {
    "inspect_instance": CapabilityRoute(
        primary=ToolMapping("Roblox_Studio", "inspect_instance"),
        fallback=ToolMapping("robloxstudio-mcp", "get_instance_properties"),
    ),
    "check_existence": CapabilityRoute(
        primary=ToolMapping("Roblox_Studio", "search_game_tree"),
        fallback=ToolMapping("robloxstudio-mcp", "search_objects"),
    ),
    "read_script_live": CapabilityRoute(
        primary=ToolMapping("Roblox_Studio", "script_read"),
        fallback=ToolMapping("robloxstudio-mcp", "get_script_source"),
    ),
    "search_game_tree": CapabilityRoute(
        primary=ToolMapping("Roblox_Studio", "search_game_tree"),
        fallback=ToolMapping("robloxstudio-mcp", "get_file_tree"),
    ),
    "query_docs": CapabilityRoute(
        primary=ToolMapping("mcp-roblox-docs", "roblox_search_docs"),
    ),
}


def get_capability_route(capability: str) -> CapabilityRoute | None:
    """Look up the routing for a canonical capability name."""
    return CAPABILITY_MAP.get(capability)


def list_capabilities() -> list[str]:
    """List all available canonical capabilities."""
    return sorted(CAPABILITY_MAP.keys())


def capability_for_uncertainty(uncertainty_type: str) -> str | None:
    """
    Map an uncertainty type to the canonical capability needed to resolve it.

    Uncertainty types:
        ui_existence → check_existence
        remote_existence → check_existence
        runtime_path_mismatch → inspect_instance
        api_behavior → query_docs
    """
    mapping = {
        "ui_existence": "check_existence",
        "remote_existence": "check_existence",
        "runtime_path_mismatch": "inspect_instance",
        "instance_properties": "inspect_instance",
        "api_behavior": "query_docs",
        "class_info": "query_docs",
    }
    return mapping.get(uncertainty_type)
