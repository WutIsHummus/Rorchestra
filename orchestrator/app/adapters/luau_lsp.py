"""
luau-lsp adapter — run standalone analysis for symbols, diagnostics,
and references using ``luau-lsp analyze``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from app.config import settings


def run_analyze(
    repo_root: Path,
    sourcemap_path: Path | None = None,
    *,
    target_files: list[Path] | None = None,
) -> dict[str, Any]:
    """
    Run ``luau-lsp analyze`` and parse its JSON diagnostic output.

    Returns::

        {
            "diagnostics": [ { "file": ..., "severity": ..., "message": ..., ... } ],
            "raw_stderr": "..."
        }
    """
    cmd = [settings.luau_lsp_bin, "analyze"]

    if sourcemap_path and sourcemap_path.exists():
        cmd += ["--sourcemap", str(sourcemap_path)]

    if target_files:
        cmd += [str(f) for f in target_files]
    else:
        cmd.append(str(repo_root))

    result = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True)

    diagnostics: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            diagnostics.append(json.loads(line))
        except json.JSONDecodeError:
            # luau-lsp may emit non-JSON lines (progress, info)
            pass

    return {
        "diagnostics": diagnostics,
        "raw_stderr": result.stderr,
        "exit_code": result.returncode,
    }


def check_patch(
    repo_root: Path,
    patched_file: Path,
    sourcemap_path: Path | None = None,
) -> list[dict[str, Any]]:
    """
    Run analysis on a single patched file and return only *new* diagnostics.
    """
    output = run_analyze(
        repo_root,
        sourcemap_path,
        target_files=[patched_file],
    )
    return output["diagnostics"]
