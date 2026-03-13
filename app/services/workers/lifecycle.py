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

## Editing Philosophy — SIMPLICITY FIRST
- **Prefer the simplest correct fix.** Remove broken code over wrapping it.
- If a variable is undefined and an equivalent constant/value already exists
  in scope, USE the existing one — do NOT add elaborate lookup chains.
- Do NOT defensively guard undefined globals with getfenv(), _G, shared,
  or ReplicatedStorage lookups unless the task specifically asks for it.
- If removing a helper function and inlining a constant is correct, do that.
- If a module or config is referenced but does not exist anywhere in the
  codebase, the right fix is to remove the reference, not to create new
  infrastructure for something that was never wired up.
- Fewer lines changed = better. Do not restructure working code.

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

# Lint fix-up prompt — sent to the worker after initial edits if lints are found
_LINT_FIXUP_PROMPT = """\
You just edited Luau files and the linter found issues. Fix them.

## Linter Diagnostics
{diagnostics}

## Rules
- Fix ONLY the problems reported above.
- If a lint error says a global is unknown/undefined, remove the reference
  rather than wrapping it in pcall/rawget — prefer the simplest fix.
- Do NOT restructure or refactor beyond what the linter flagged.
- When done, output a one-line summary of what you fixed.
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
    debug: bool = False,
) -> WorkerResult:
    """
    Launch a fresh Gemini CLI worker to implement edits from a context packet.

    When use_tools=True (default), the worker gets file tools and edits
    directly. Changed files are detected via `git diff --name-only`.

    When use_tools=False, falls back to the legacy one-shot diff prompt.
    """
    timeout = timeout or settings.worker_timeout_secs

    if use_tools and cwd:
        import time as _time
        from rich.console import Console as _Con
        _t = _Con()

        # Snapshot files before the worker runs
        _t0 = _time.monotonic()
        before_files = set(_get_changed_files(cwd))
        _t.print(f"[dim]  ⏱  git snapshot: {_time.monotonic() - _t0:.1f}s[/dim]")

        _t0 = _time.monotonic()
        prompt = _build_tool_prompt(packet)
        prompt_tokens = len(prompt) // 4  # rough estimate
        _t.print(f"[dim]  ⏱  prompt built: ~{prompt_tokens:,} tokens ({len(prompt):,} chars)[/dim]")

        _t0 = _time.monotonic()
        result = invoke_standalone(
            prompt,
            allowed_tools=_EDIT_TOOLS,
            timeout=timeout,
            cwd=cwd,
            debug=debug,
        )
        worker_elapsed = _time.monotonic() - _t0
        _t.print(f"[dim]  ⏱  worker done: {worker_elapsed:.1f}s (exit={result.exit_code}, in={result.input_tokens}, out={result.output_tokens})[/dim]")

        # Detect what files the worker changed
        _t0 = _time.monotonic()
        after_files = set(_get_changed_files(cwd))
        newly_changed = sorted(after_files - before_files)
        _t.print(f"[dim]  ⏱  git detect: {_time.monotonic() - _t0:.1f}s → {len(newly_changed)} file(s) changed[/dim]")

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

        # ---- Lint-driven fix-up loop ----
        # Run luau-lsp on changed files; if errors/warnings found, give the
        # worker a second pass with the diagnostics so it can fix them.
        if newly_changed and result.exit_code == 0:
            from rich.console import Console as _Con
            _console = _Con()
            _console.print(f"[dim]  Lint: checking {len(newly_changed)} changed file(s)...[/dim]")
            result.luau_diagnostics = _run_luau_lsp_check(cwd, newly_changed)

            # Filter to actionable diagnostics (errors + warnings)
            actionable = [
                d for d in (result.luau_diagnostics or [])
                if d.get("severity", "").lower() in ("error", "warning")
            ]

            if actionable:
                _console.print(f"[yellow]  Lint: {len(actionable)} issue(s) found:[/yellow]")
                for d in actionable[:5]:
                    f = d.get("file", "?")
                    sev = d.get("severity", "?")
                    msg = d.get("message", "?")
                    ln = d.get("line", d.get("range", {}).get("start", {}).get("line", "?"))
                    _console.print(f"    [dim]{sev}: {f}:{ln} — {msg}[/dim]")
                if len(actionable) > 5:
                    _console.print(f"    [dim]... and {len(actionable) - 5} more[/dim]")
            else:
                _console.print(f"[green]  Lint: clean ✓[/green]")

            if actionable and len(actionable) <= 20:  # Don't retry if too many
                diag_lines = []
                for d in actionable:
                    f = d.get("file", "?")
                    sev = d.get("severity", "?")
                    msg = d.get("message", "?")
                    line = d.get("line", d.get("range", {}).get("start", {}).get("line", "?"))
                    diag_lines.append(f"{sev}: {f}:{line} — {msg}")

                fixup_prompt = _LINT_FIXUP_PROMPT.format(
                    diagnostics="\n".join(diag_lines),
                )

                _console.print(f"[cyan]  Lint fix-up: sending {len(actionable)} diagnostic(s) back to worker...[/cyan]")

                # Second pass — shorter timeout
                fixup_result = invoke_standalone(
                    fixup_prompt,
                    allowed_tools=_EDIT_TOOLS,
                    timeout=min(timeout, 120),
                    cwd=cwd,
                    debug=debug,
                )

                # Re-check lint after fix-up
                if fixup_result.exit_code == 0:
                    after_fixup = set(_get_changed_files(cwd))
                    fixup_changed = sorted(after_fixup - before_files)
                    result.luau_diagnostics = _run_luau_lsp_check(cwd, fixup_changed or newly_changed)

                    remaining = [
                        d for d in (result.luau_diagnostics or [])
                        if d.get("severity", "").lower() in ("error", "warning")
                    ]
                    if remaining:
                        _console.print(f"[yellow]  Lint fix-up: {len(remaining)} issue(s) remain[/yellow]")
                    else:
                        _console.print(f"[green]  Lint fix-up: all issues resolved ✓[/green]")

                    # Update the diff to include fix-up changes
                    try:
                        all_changed = sorted(set(newly_changed) | set(fixup_changed))
                        diff_result = subprocess.run(
                            ["git", "diff", "--"] + all_changed,
                            cwd=cwd,
                            capture_output=True,
                            text=True,
                            timeout=15,
                            shell=sys.platform == "win32",
                        )
                        if diff_result.stdout.strip():
                            result.patch_content = diff_result.stdout.strip()
                    except Exception:
                        pass
                else:
                    _console.print(f"[red]  Lint fix-up: worker failed (exit={fixup_result.exit_code})[/red]")

        # Tool-mode fallback: if worker exited 0 and files changed on disk
        # but diff capture failed, generate a synthetic patch_content so the
        # REPL treats it as a success.
        if not result.patch_content and newly_changed and result.exit_code == 0:
            # Try one more time with all changed files (unstaged + staged combined)
            try:
                all_files = sorted(set(_get_changed_files(cwd)))
                if all_files:
                    diff_result = subprocess.run(
                        ["git", "diff", "HEAD", "--"] + all_files,
                        cwd=cwd,
                        capture_output=True,
                        text=True,
                        timeout=15,
                        shell=sys.platform == "win32",
                    )
                    if diff_result.stdout.strip():
                        result.patch_content = diff_result.stdout.strip()
            except Exception:
                pass

            # Last resort: mark as changed even without diff text
            if not result.patch_content:
                result.patch_content = (
                    f"# Tool-mode: {len(newly_changed)} file(s) modified on disk\n"
                    + "\n".join(f"# - {f}" for f in newly_changed)
                )
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
