"""
File-based artifact store for packets, raw MCP outputs, diffs, transcripts.
"""

from __future__ import annotations

import json
import datetime as _dt
from pathlib import Path

from app.config import settings


def _artifacts_root() -> Path:
    root = settings.artifacts_dir
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_artifact(category: str, name: str, data: str | dict) -> Path:
    """
    Persist an artifact under ``artifacts/<category>/<name>``.
    Returns the absolute path to the written file.
    """
    folder = _artifacts_root() / category
    folder.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{ts}_{name}"
    path = folder / filename

    if isinstance(data, dict):
        path = path.with_suffix(".json")
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    else:
        if not path.suffix:
            path = path.with_suffix(".txt")
        path.write_text(data, encoding="utf-8")

    return path


def load_artifact(rel_path: str) -> str:
    """Read an artifact file by its path relative to artifacts root."""
    return (_artifacts_root() / rel_path).read_text(encoding="utf-8")
