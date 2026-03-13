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

    import re
    result = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True)

    diagnostics: list[dict[str, Any]] = []

    # Strategy 1: Parse stdout as JSON lines (if --formatter=json is used or emits JSON)
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            diagnostics.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    # Strategy 2: Parse stderr text format: file(line,col-col): severity: message
    _DIAG_PATTERN = re.compile(
        r"^(.+?)\((\d+),(\d+)(?:-\d+)?\):\s*(Error|Warning|Information)\s*[:\-]\s*(.+)$",
        re.IGNORECASE,
    )
    for line in result.stderr.splitlines():
        line = line.strip()
        m = _DIAG_PATTERN.match(line)
        if m:
            diagnostics.append({
                "file": m.group(1).strip(),
                "line": int(m.group(2)),
                "col": int(m.group(3)),
                "severity": m.group(4).capitalize(),
                "message": m.group(5).strip(),
            })

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
