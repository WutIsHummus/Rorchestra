"""
MCP trigger policy — decides when to invoke live Studio validation.

Memory records are invalidation-driven, so uncertainty comes from
missing or invalidated environment-type memories, not wall-clock age.
"""

from __future__ import annotations

from typing import Any

from app.models.entities import MemoryType
from app.services.memory.store import get_memory


# ── Uncertainty classes ───────────────────────────────────────────────────

TRIGGER_CLASSES = {
    "ui_existence",
    "remote_existence",
    "runtime_path_mismatch",
    "dynamic_instance_origin",
    "selection_dependent_edit",
}


def should_trigger_mcp(
    uncertainty_type: str,
    target_ref: str,
) -> bool:
    """
    Determine whether an MCP call is justified.

    Returns True when:
      - The uncertainty_type is a known trigger class, AND
      - Repository evidence is insufficient (no valid environment memory
        for the target, or the relevant memory has been invalidated).
    """
    if uncertainty_type not in TRIGGER_CLASSES:
        return False

    # Check for an existing, non-invalidated environment memory
    scope_id = f"mcp:{target_ref}"
    mem = get_memory(scope_id, MemoryType.environment)

    if mem is not None:
        # We have a live environment memory — no need to re-query Studio
        return False

    # No evidence → trigger MCP
    return True
