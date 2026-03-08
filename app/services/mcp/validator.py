"""
MCP validator — runs isolated Studio checks and returns compact
ValidationArtifact results.  Raw MCP output is stored out of band.
"""

from __future__ import annotations

import json
from typing import Any

from app.adapters.roblox_mcp import dispatcher
from app.models.entities import MemoryType
from app.models.schemas import ValidationResult
from app.services.memory.store import upsert_memory
from app.storage.artifacts import save_artifact


def validate_ui_existence(instance_path: str) -> ValidationResult:
    """Check whether a UI instance exists in the live Studio DataModel."""
    result = dispatcher.call("inspect_instance", {"path": instance_path})
    return _to_validation_result(instance_path, result)


def validate_remote_existence(remote_path: str) -> ValidationResult:
    """Check whether a RemoteEvent / RemoteFunction exists at the given path."""
    result = dispatcher.call("inspect_instance", {"path": remote_path})
    return _to_validation_result(remote_path, result)


def validate_runtime_path(expected_path: str) -> ValidationResult:
    """Search the game tree to verify an expected instance path."""
    result = dispatcher.call("search_tree", {"path": expected_path})
    return _to_validation_result(expected_path, result)


def run_mcp_check(
    uncertainty_type: str,
    target_ref: str,
) -> ValidationResult:
    """
    Dispatch an MCP check based on uncertainty type.
    Store the result as an environment memory.
    """
    handlers = {
        "ui_existence": validate_ui_existence,
        "remote_existence": validate_remote_existence,
        "runtime_path_mismatch": validate_runtime_path,
    }

    handler = handlers.get(uncertainty_type)
    if handler is None:
        return ValidationResult(
            target=target_ref,
            status="uncertain",
            key_findings=f"No handler for uncertainty type: {uncertainty_type}",
        )

    vr = handler(target_ref)

    # Store as environment memory so future checks skip MCP
    upsert_memory(
        scope_id=f"mcp:{target_ref}",
        memory_type=MemoryType.environment,
        content=json.dumps(vr.model_dump()),
        confidence=vr.confidence,
        source_refs=[f"mcp_check:{uncertainty_type}"],
    )

    return vr


def _to_validation_result(target: str, mcp_result: dict) -> ValidationResult:
    """Convert raw MCP dispatcher output to a compact ValidationResult."""
    status = mcp_result.get("status", "uncertain")

    if status == "error":
        return ValidationResult(
            target=target,
            status="fail",
            key_findings=mcp_result.get("error", "MCP call failed"),
            confidence=0.3,
        )

    data = mcp_result.get("data")
    if data is None:
        return ValidationResult(
            target=target,
            status="uncertain",
            key_findings="No data returned from MCP",
            confidence=0.5,
        )

    return ValidationResult(
        target=target,
        status="pass",
        key_findings="Instance found in live Studio DataModel",
        actual_paths=[target],
        confidence=0.95,
    )
