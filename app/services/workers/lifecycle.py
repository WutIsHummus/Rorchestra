"""
Worker lifecycle — invoke Gemini CLI workers, handle timeouts, retry,
and persist run traces.

The edit worker uses Gemini CLI's built-in tools (read_file, edit_file,
grep_search, list_dir, run_shell_command) to explore the codebase and
make changes directly, instead of generating a blind one-shot diff.
Changed files are detected via `git diff --name-only`.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

from app.adapters.gemini_cli import invoke_standalone, invoke_subagent
from app.adapters.luau_lsp import run_analyze
from app.config import settings
from app.models.entities import RunTrace, EditProposal
from app.models.schemas import ContextPacketSchema, WorkerResult
from app.storage.database import get_session


def _utcnow():
    return _dt.datetime.now(_dt.timezone.utc)


# Tools the edit worker is allowed to use via Gemini CLI
_EDIT_TOOLS = [
    "read_file",
    "edit_file",
    "write_file",
    "grep_search",
    "list_dir",
]

_EDIT_WORKER_PROMPT = """\
You are a Luau/Roblox code editor. You have access to file tools.
Your job is to implement the described change by reading the relevant files,
planning your edits, making the changes directly, and verifying the result.

## Workflow
1. Read the target files listed below to understand the current code.
2. Plan your changes — identify what needs to change and why.
3. Make edits using the edit_file tool. Make precise, surgical edits.
4. After editing, read the changed files to verify correctness.
5. If you spot issues, fix them immediately.

## Rules
- ONLY modify files within the repository root.
- Respect the invariants listed below — do NOT break them.
- Heed the known risks — test edge cases in your edits.
- If the packet includes a migration_brief, treat it as the stable target
  architecture and align your edits with it.
- Do NOT output a diff. Use the edit_file tool to make changes directly.
- When done, output a brief summary of what you changed and why.

## Task
**Objective:** {objective}
**Target scope:** {target_scope}
**Runtime side:** {runtime_side}

## Files to work with
{file_listing}

## Invariants (do not break these)
{invariants}

## Known risks
{risks}

## Uncertainties
{uncertainties}

## Relevant contracts
{contracts}

{migration_section}
"""

# Legacy prompt for fallback mode (no tools available)
_EDIT_WORKER_PROMPT_LEGACY = """\
You are a Luau code editor.  You will receive a ContextPacket describing
a focused edit task for a Roblox game.  Produce a unified diff patch that
implements the requested change.

Rules:
- Only modify files listed in the packet's file_bodies.
- Respect the local_invariants and known_risks.
- If the packet includes a migration_brief (target_state, invariants_to_preserve, migration_steps), treat it as the stable target architecture for this change and align your edit with it.
- Output ONLY the unified diff, nothing else.

--- CONTEXT PACKET ---
{packet_json}
--- END PACKET ---
"""


def _get_changed_files(cwd: str) -> list[str]:
    """Snapshot changed files via git diff --name-only (unstaged + staged)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            shell=sys.platform == "win32",
        )
        unstaged = [f.strip() for f in result.stdout.splitlines() if f.strip()]

        result2 = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            shell=sys.platform == "win32",
        )
        staged = [f.strip() for f in result2.stdout.splitlines() if f.strip()]

        # Combine and deduplicate, preserving order
        seen = set()
        combined = []
        for f in unstaged + staged:
            if f not in seen:
                seen.add(f)
                combined.append(f)
        return combined
    except Exception:
        return []


def _find_sourcemap(cwd: str) -> Path | None:
    """Find sourcemap.json in typical Rojo project locations."""
    for name in ["sourcemap.json", "default.project.json"]:
        p = Path(cwd) / name
        if p.exists():
            return p
    return None


def _run_luau_lsp_check(
    cwd: str,
    changed_files: list[str],
) -> list[dict]:
    """
    Run luau-lsp analyze on changed .luau/.lua files.
    Returns list of diagnostic dicts with file, severity, message, line, col.
    """
    luau_files = [
        Path(cwd) / f for f in changed_files
        if f.endswith((".luau", ".lua")) and (Path(cwd) / f).exists()
    ]
    if not luau_files:
        return []

    try:
        sourcemap = _find_sourcemap(cwd)
        output = run_analyze(
            Path(cwd),
            sourcemap_path=sourcemap,
            target_files=luau_files,
        )
        return output.get("diagnostics", [])
    except Exception:
        return []



def _build_tool_prompt(packet: ContextPacketSchema) -> str:
    """Build the tool-based edit worker prompt from a context packet."""
    # File listing
    file_lines = []
    for fp, body in packet.file_bodies.items():
        line_count = body.count("\n") + 1
        is_target = any(
            s.get("is_target") for s in packet.relevant_scripts
            if s.get("file_path") == fp
        )
        marker = " ← TARGET" if is_target else ""
        file_lines.append(f"- `{fp}` ({line_count} lines){marker}")

    # Invariants
    inv_text = "\n".join(f"- {inv}" for inv in packet.local_invariants) if packet.local_invariants else "(none)"

    # Risks
    risk_text = "\n".join(f"- {r}" for r in packet.known_risks) if packet.known_risks else "(none)"

    # Uncertainties
    unc_text = "\n".join(f"- {u}" for u in packet.uncertainties) if packet.uncertainties else "(none)"

    # Contracts
    contract_text = "\n".join(
        f"- **{c.get('name', '?')}** ({c.get('kind', '?')}): {c.get('summary', '')}"
        for c in packet.relevant_contracts
    ) if packet.relevant_contracts else "(none)"

    # Migration brief
    migration_section = ""
    if packet.migration_brief and packet.migration_brief.get("target_state"):
        mb = packet.migration_brief
        migration_section = f"""## Migration Brief
**Target state:** {mb.get('target_state', 'N/A')}
**Old state:** {mb.get('old_state', 'N/A')}
**Steps:** {json.dumps(mb.get('migration_steps', []), indent=2)}
**Invariants to preserve:** {json.dumps(mb.get('invariants_to_preserve', []), indent=2)}
"""

    return _EDIT_WORKER_PROMPT.format(
        objective=packet.objective,
        target_scope=packet.target_scope or "(auto)",
        runtime_side=packet.runtime_side or "unknown",
        file_listing="\n".join(file_lines) or "(no files)",
        invariants=inv_text,
        risks=risk_text,
        uncertainties=unc_text,
        contracts=contract_text,
        migration_section=migration_section,
    )


def invoke_edit_worker(
    packet: ContextPacketSchema,
    *,
    cwd: str | None = None,
    timeout: int | None = None,
    use_tools: bool = True,
) -> WorkerResult:
    """
    Launch a fresh Gemini CLI worker to implement edits from a context packet.

    When use_tools=True (default), the worker gets file tools and edits
    directly. Changed files are detected via `git diff --name-only`.

    When use_tools=False, falls back to the legacy one-shot diff prompt.
    """
    timeout = timeout or settings.worker_timeout_secs

    if use_tools and cwd:
        # Snapshot files before the worker runs
        before_files = set(_get_changed_files(cwd))

        prompt = _build_tool_prompt(packet)
        result = invoke_standalone(
            prompt,
            allowed_tools=_EDIT_TOOLS,
            timeout=timeout,
            cwd=cwd,
        )

        # Detect what files the worker changed
        after_files = set(_get_changed_files(cwd))
        newly_changed = sorted(after_files - before_files)

        # Generate a diff for the proposal record
        if newly_changed and result.exit_code == 0:
            try:
                diff_result = subprocess.run(
                    ["git", "diff", "--"] + newly_changed,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    shell=sys.platform == "win32",
                )
                if diff_result.stdout.strip():
                    result.patch_content = diff_result.stdout.strip()
            except Exception:
                pass  # Diff capture failed, but edits are already on disk

        # If no git-detected changes but worker claimed success, check stdout
        if not result.patch_content and result.exit_code == 0 and result.stdout.strip():
            text = result.stdout.strip()
            if any(marker in text for marker in ["--- ", "+++ ", "@@ ", "diff --"]):
                result.patch_content = text

        # Run luau-lsp syntax check on changed Luau files
        if newly_changed and result.exit_code == 0:
            result.luau_diagnostics = _run_luau_lsp_check(cwd, newly_changed)
    else:
        # Legacy fallback: one-shot diff generation
        packet_json = packet.model_dump_json(indent=2)
        prompt = _EDIT_WORKER_PROMPT_LEGACY.format(packet_json=packet_json)
        result = invoke_standalone(prompt, timeout=timeout, cwd=cwd)

        # Retry once on timeout with truncated file bodies
        if result.exit_code == -1 and settings.max_worker_retries > 0:
            smaller = packet.model_copy()
            smaller.file_bodies = {
                k: v
                for k, v in packet.file_bodies.items()
                if any(
                    s.get("is_target")
                    for s in packet.relevant_scripts
                    if s.get("file_path") == k
                )
            }
            smaller_json = smaller.model_dump_json(indent=2)
            prompt = _EDIT_WORKER_PROMPT_LEGACY.format(packet_json=smaller_json)
            result = invoke_standalone(prompt, timeout=timeout, cwd=cwd)

        # Extract patch from stdout
        if result.exit_code == 0 and result.stdout.strip():
            text = result.stdout.strip()
            if any(marker in text for marker in ["--- ", "+++ ", "@@ ", "diff --"]):
                result.patch_content = text

    # Persist run trace
    _record_trace(packet.task_id, result)

    return result


def invoke_review_worker(
    patch_content: str,
    packet: ContextPacketSchema,
    *,
    timeout: int | None = None,
) -> WorkerResult:
    """Invoke the patch-reviewer subagent to validate a proposed patch."""
    context = json.dumps({
        "patch": patch_content,
        "objective": packet.objective,
        "target_scope": packet.target_scope,
        "invariants": packet.local_invariants,
        "risks": packet.known_risks,
    }, indent=2)

    result = invoke_subagent("patch-reviewer", context, timeout=timeout)
    _record_trace(packet.task_id, result, worker_type="patch_reviewer")
    return result


def _normalize_patch_content(raw: str) -> str:
    """
    Normalize patch content at write time.
    Handles: JSON-wrapped diffs, NDJSON, markdown fences, escaped newlines.
    Returns a clean unified diff string.
    """
    import re

    content = raw.strip()
    if not content:
        return content

    # Try JSON unwrapping
    if content.startswith("{"):
        try:
            obj = json.loads(content)
            content = (
                obj.get("response")
                or obj.get("result")
                or obj.get("text")
                or obj.get("output")
                or content
            )
        except json.JSONDecodeError:
            # NDJSON: try each line
            for line in content.splitlines():
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                    extracted = (
                        obj.get("response")
                        or obj.get("result")
                        or obj.get("text")
                        or obj.get("output")
                    )
                    if isinstance(extracted, str) and ("---" in extracted or "+++" in extracted):
                        content = extracted
                        break
                except json.JSONDecodeError:
                    continue

    if isinstance(content, str):
        content = content.strip()
    else:
        content = raw.strip()

    # Unwrap markdown code block (```diff ... ```)
    if content.startswith("```diff"):
        idx = content.find("\n")
        content = content[idx + 1:] if idx != -1 else content[7:]
        end_fence = content.rfind("```")
        if end_fence != -1:
            content = content[:end_fence].strip()
    elif content.startswith("```"):
        idx = content.find("\n")
        content = content[idx + 1:] if idx != -1 else content[3:]
        end_fence = content.rfind("```")
        if end_fence != -1:
            content = content[:end_fence].strip()

    # Normalize escaped newlines (from JSON strings)
    content = content.replace("\\n", "\n")

    return content.strip()


def save_proposal(task_id: int, patch_content: str, source: str = "gemini_worker") -> int:
    """Persist a patch proposal (normalized) and return its ID."""
    clean_patch = _normalize_patch_content(patch_content)
    session = get_session()
    try:
        proposal = EditProposal(
            task_id=task_id,
            patch_content=clean_patch,
            source=source,
        )
        session.add(proposal)
        session.commit()
        return proposal.id
    finally:
        session.close()


def _record_trace(
    task_id: int,
    result: WorkerResult,
    worker_type: str | None = None,
) -> None:
    session = get_session()
    try:
        trace = RunTrace(
            task_id=task_id,
            worker_type=worker_type or result.worker_type,
            finished_at=_utcnow(),
            exit_code=result.exit_code,
            transcript_ref=result.transcript_ref,
            metrics_json=json.dumps({
                "elapsed_secs": result.elapsed_secs,
            }),
        )
        session.add(trace)
        session.commit()
    finally:
        session.close()
