"""
Rojo adapter — parse project files, generate / read sourcemaps,
and map file paths ↔ DataModel instance paths.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from app.config import settings
from app.models.schemas import SourcemapNode


# ── Project File Parsing ──────────────────────────────────────────────────


def find_project_file(repo_root: Path) -> Path | None:
    """Locate the Rojo project file (``default.project.json`` or first ``*.project.json``)."""
    default = repo_root / "default.project.json"
    if default.exists():
        return default
    candidates = sorted(repo_root.glob("*.project.json"))
    return candidates[0] if candidates else None


def parse_project_file(project_path: Path) -> dict[str, Any]:
    """Return the parsed JSON content of a Rojo project file."""
    return json.loads(project_path.read_text(encoding="utf-8"))


# ── Sourcemap ─────────────────────────────────────────────────────────────


def generate_sourcemap(repo_root: Path, project_path: Path | None = None) -> Path:
    """
    Run ``rojo sourcemap`` and write ``sourcemap.json`` into *repo_root*.
    Returns the path to the generated file.
    """
    cmd = [settings.rojo_bin, "sourcemap"]
    if project_path:
        cmd.append(str(project_path))
    cmd += ["--output", str(repo_root / "sourcemap.json")]

    result = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"rojo sourcemap failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    return repo_root / "sourcemap.json"


def read_sourcemap(sourcemap_path: Path) -> SourcemapNode:
    """Parse an existing ``sourcemap.json`` into a typed tree."""
    raw = json.loads(sourcemap_path.read_text(encoding="utf-8"))
    return SourcemapNode.model_validate(raw)


# ── Tree Traversal ────────────────────────────────────────────────────────


def walk_sourcemap(
    node: SourcemapNode,
    *,
    parent_path: str = "",
) -> list[dict[str, Any]]:
    """
    Flatten the sourcemap tree into a list of dicts::

        {
            "name": ...,
            "className": ...,
            "instancePath": "game.Workspace.Folder.Script",
            "filePaths": [...],
        }
    """
    current_path = f"{parent_path}.{node.name}" if parent_path else node.name
    entries: list[dict[str, Any]] = []

    if node.filePaths:
        entries.append(
            {
                "name": node.name,
                "className": node.className,
                "instancePath": current_path,
                "filePaths": node.filePaths,
            }
        )

    for child in node.children:
        entries.extend(walk_sourcemap(child, parent_path=current_path))

    return entries


def file_to_instance(sourcemap: SourcemapNode, file_path: str) -> str | None:
    """Given a relative file path, return the DataModel instance path (or None)."""
    normalised = file_path.replace("\\", "/")
    for entry in walk_sourcemap(sourcemap):
        for fp in entry["filePaths"]:
            if fp.replace("\\", "/") == normalised:
                return entry["instancePath"]
    return None


def instance_to_file(sourcemap: SourcemapNode, instance_path: str) -> str | None:
    """Given a DataModel instance path, return the first matching file path."""
    for entry in walk_sourcemap(sourcemap):
        if entry["instancePath"] == instance_path and entry["filePaths"]:
            return entry["filePaths"][0]
    return None
