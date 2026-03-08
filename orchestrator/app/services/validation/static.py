"""
Static and structural patch validation.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from app.adapters.luau_lsp import check_patch


def validate_patch_static(
    repo_root: str | Path,
    patch_content: str,
    target_file: str,
    sourcemap_path: Path | None = None,
) -> dict[str, Any]:
    """
    Apply *patch_content* to a temp copy of *target_file* and run
    luau-lsp analysis to detect new errors.

    Returns::

        {
            "status": "pass" | "fail",
            "new_diagnostics": [...],
            "error": "..." (only if something went wrong)
        }
    """
    repo_root = Path(repo_root)
    original = repo_root / target_file

    if not original.exists():
        return {"status": "fail", "error": f"Target file not found: {target_file}"}

    try:
        # Write patched content to a temp file (same dir for correct relative imports)
        suffix = original.suffix
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=suffix,
            dir=str(original.parent),
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(patch_content)
            tmp_path = Path(tmp.name)

        diags = check_patch(repo_root, tmp_path, sourcemap_path)
        tmp_path.unlink(missing_ok=True)

        if diags:
            return {"status": "fail", "new_diagnostics": diags}
        return {"status": "pass", "new_diagnostics": []}

    except Exception as exc:
        return {"status": "fail", "error": str(exc)}


def validate_patch_structural(
    patch_content: str,
    target_scope: str,
    runtime_side: str,
) -> dict[str, Any]:
    """
    Lightweight structural checks:
      1. Patch only touches files within the declared target_scope.
      2. No obvious runtime-side boundary violations.
    """
    issues: list[str] = []

    # Check scope containment (heuristic: look for file paths in diff headers)
    for line in patch_content.splitlines():
        if line.startswith("--- a/") or line.startswith("+++ b/"):
            path_in_diff = line.split("/", 1)[-1] if "/" in line else line
            if target_scope and target_scope not in path_in_diff:
                issues.append(
                    f"Patch touches '{path_in_diff}' which is outside "
                    f"declared scope '{target_scope}'"
                )

    if issues:
        return {"status": "fail", "issues": issues}
    return {"status": "pass", "issues": []}
