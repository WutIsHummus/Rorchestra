"""
Apply a unified diff to a repository directory.

Tries `git apply` first when the repo is a git repo; otherwise parses the diff
and applies hunks in Python.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path


def _normalize_path(path: str) -> str:
    """Strip a/ b/ prefix and normalize slashes."""
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
    return path.replace("\\", "/").lstrip("/")


def _denormalize_added_line(line: str) -> str:
    """Replace literal \\t and \\\" from model/diff with real tab and quote."""
    line = line.replace("\\t", "\t")
    line = line.replace('\\"', '"')
    return line


def _normalize_patch_output(text: str) -> str:
    """Fix common escape sequences in final file content (from model or prior bad apply)."""
    text = text.replace("\\t", "\t")
    text = text.replace('\\"', '"')
    return text


def _parse_unified_diff(diff_text: str) -> list[tuple[str, list[tuple[int, int, list[str]]]]]:
    """
    Parse unified diff into a list of (file_path, hunks).
    Each hunk is (old_start, old_count, lines) where lines are the hunk body
    (prefixes " ", "-", "+" preserved).
    """
    files: list[tuple[str, list[tuple[int, int, list[str]]]]] = []
    current_path: str | None = None
    current_hunks: list[tuple[int, int, list[str]]] = []
    in_hunk = False
    hunk_lines: list[str] = []

    # Looser @@ regex (allow trailing text after @@, e.g. "@@ -1,5 +1,6 @@ optional")
    hunk_re = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    for line in diff_text.splitlines():
        if line.startswith("--- ") and len(line) > 4:
            if current_path is not None and current_hunks:
                files.append((current_path, current_hunks))
            current_path = None
            current_hunks = []
            in_hunk = False
        elif line.startswith("+++ ") and len(line) > 4:
            path_part = line[4:].strip()
            if path_part and path_part != "/dev/null":
                current_path = _normalize_path(path_part)
            current_hunks = []
            in_hunk = False
        elif line.startswith("@@ "):
            if current_path is None:
                continue
            m = hunk_re.search(line)
            if m:
                old_start = int(m.group(1))
                old_count = int(m.group(2) or 1)
                current_hunks.append((old_start, old_count, []))
                in_hunk = True
        elif in_hunk and current_hunks:
            if line.startswith(" ") or line.startswith("-") or line.startswith("+"):
                current_hunks[-1][2].append(line)
            else:
                # Corrupt or unexpected line: try to recover (new hunk / new file)
                if line.startswith("@@ "):
                    m = hunk_re.search(line)
                    if m:
                        old_start = int(m.group(1))
                        old_count = int(m.group(2) or 1)
                        current_hunks.append((old_start, old_count, []))
                elif line.startswith("--- ") and len(line) > 4:
                    if current_path is not None and current_hunks:
                        files.append((current_path, current_hunks))
                    current_path = None
                    current_hunks = []
                    in_hunk = False
                elif line.startswith("+++ ") and len(line) > 4:
                    path_part = line[4:].strip()
                    if path_part and path_part != "/dev/null":
                        current_path = _normalize_path(path_part)
                    current_hunks = []
                    in_hunk = False
                else:
                    in_hunk = False  # skip bad line; next @@ will start a new hunk

    if current_path is not None and current_hunks:
        files.append((current_path, current_hunks))

    return files


def _apply_hunks_to_content(file_path: str, hunks: list[tuple[int, int, list[str]]], old_lines: list[str]) -> str:
    """Apply hunks to old file lines; return new file content."""
    result: list[str] = []
    old_idx = 0  # 0-based index into old_lines

    for old_start, _old_count, hunk_lines in hunks:
        # old_start is 1-based in unified diff
        old_start_0 = max(0, old_start - 1)
        # Emit lines from old file before this hunk
        while old_idx < old_start_0 and old_idx < len(old_lines):
            result.append(old_lines[old_idx])
            old_idx += 1
        # Process hunk: each " " or "-" consumes one old line; " " or "+" produces output
        for hunk_line in hunk_lines:
            if hunk_line.startswith("-"):
                old_idx += 1
            elif hunk_line.startswith("+"):
                result.append(_denormalize_added_line(hunk_line[1:]))
            else:
                # context line, or malformed (treat as context: keep one old line)
                if old_idx < len(old_lines):
                    result.append(old_lines[old_idx])
                old_idx += 1

    # Rest of file
    while old_idx < len(old_lines):
        result.append(old_lines[old_idx])
        old_idx += 1

    return "\n".join(result) + ("\n" if result else "")


def apply_patch_to_dir(diff_content: str, repo_root: str) -> tuple[list[str], list[str]]:
    """
    Apply a unified diff to the given directory.

    Returns (list of applied file paths, list of error messages).
    """
    repo_path = Path(repo_root).resolve()
    applied: list[str] = []
    errors: list[str] = []

    # Ensure we have a valid diff (strip markdown fence if present)
    diff = diff_content.strip()
    if diff.startswith("```diff"):
        idx = diff.find("\n")
        diff = diff[idx + 1:] if idx != -1 else diff[7:]
    end = diff.rfind("```")
    if end != -1:
        diff = diff[:end].strip()
    # Normalize: drop BOM, LF line endings, end with newline (git apply is strict)
    diff = diff.lstrip("\ufeff").strip()
    diff = "\n".join(diff.splitlines())
    if diff:
        diff = diff + "\n"

    if not diff or ("--- " not in diff and "+++ " not in diff):
        errors.append("No valid unified diff found in content.")
        return applied, errors

    # Try git apply first
    git_dir = repo_path / ".git"
    if git_dir.exists():
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".diff", delete=False, encoding="utf-8", newline="\n"
            ) as f:
                f.write(diff)
                tmp_path = f.name
            try:
                out = subprocess.run(
                    ["git", "apply", "--ignore-whitespace", "-p1", tmp_path],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if out.returncode == 0:
                    for line in diff.splitlines():
                        if line.startswith("+++ b/"):
                            applied.append(_normalize_path(line[6:].strip()))
                        elif line.startswith("+++ ") and not line.startswith("+++ b/"):
                            applied.append(_normalize_path(line[4:].strip()))
                    return applied, []
                # "No valid patches" / empty patch: fall through to Python applier, don't report as error
                stderr = (out.stderr or "").strip()
                if "No valid patches" not in stderr and "allow-empty" not in stderr:
                    errors.append(stderr or f"git apply exited {out.returncode}")
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except FileNotFoundError:
            pass  # git not in PATH
        except Exception as e:
            errors.append(f"git apply failed: {e}")

    # Python fallback: parse and apply hunks
    try:
        parsed = _parse_unified_diff(diff)
    except Exception as e:
        errors.append(f"Failed to parse diff: {e}")
        return applied, errors

    for file_path, hunks in parsed:
        if not file_path:
            continue
        full_path = repo_path / file_path
        try:
            if full_path.exists():
                old_lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
            else:
                old_lines = []
            new_content = _apply_hunks_to_content(file_path, hunks, old_lines)
            new_content = _normalize_patch_output(new_content)
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(new_content, encoding="utf-8")
            applied.append(file_path)
        except Exception as e:
            errors.append(f"{file_path}: {e}")

    return applied, errors
