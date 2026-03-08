"""
Safety policy — permission gates, high-risk detection, escalation.
"""

from __future__ import annotations

from typing import Any


# Keywords that flag a task as potentially high-risk
_HIGH_RISK_KEYWORDS = {
    "delete",
    "destroy",
    "datastore",
    "marketplace",
    "purchase",
    "robux",
    "ban",
    "kick",
    "admin",
    "httpservice",
    "loadstring",
}


def is_high_risk(task_description: str) -> bool:
    """
    Heuristic check: does *task_description* mention patterns that
    warrant human review before automated execution?
    """
    lower = task_description.lower()
    return any(kw in lower for kw in _HIGH_RISK_KEYWORDS)


def gate_mcp_write(worker_type: str) -> bool:
    """
    Return True if *worker_type* is allowed to use MCP write/mutate tools.

    In the MVP only admin/operator workers have write access.
    """
    return worker_type in ("admin", "operator")


def require_review(task_description: str, patch_content: str) -> bool:
    """
    Determine whether a proposed patch must go through human review
    before being applied.

    Returns True if the task is high-risk OR the patch is unusually large.
    """
    if is_high_risk(task_description):
        return True
    # Large patches (>200 changed lines) warrant review
    changed_lines = sum(
        1 for line in patch_content.splitlines()
        if line.startswith("+") or line.startswith("-")
    )
    return changed_lines > 200
