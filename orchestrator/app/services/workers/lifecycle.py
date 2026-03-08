"""
Worker lifecycle — invoke Gemini CLI workers, handle timeouts, retry,
and persist run traces.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from app.adapters.gemini_cli import invoke_standalone, invoke_subagent
from app.config import settings
from app.models.entities import RunTrace, EditProposal
from app.models.schemas import ContextPacketSchema, WorkerResult
from app.storage.database import get_session


def _utcnow():
    return _dt.datetime.now(_dt.timezone.utc)


_EDIT_WORKER_PROMPT = """\
You are a Luau code editor.  You will receive a ContextPacket describing
a focused edit task for a Roblox game.  Produce a unified diff patch that
implements the requested change.

Rules:
- Only modify files listed in the packet's file_bodies.
- Respect the local_invariants and known_risks.
- Output ONLY the unified diff, nothing else.

--- CONTEXT PACKET ---
{packet_json}
--- END PACKET ---
"""


def invoke_edit_worker(
    packet: ContextPacketSchema,
    *,
    cwd: str | None = None,
    timeout: int | None = None,
) -> WorkerResult:
    """
    Launch a fresh Gemini CLI worker to produce a patch from a context packet.
    Retries once with a smaller packet on timeout.
    """
    timeout = timeout or settings.worker_timeout_secs
    packet_json = packet.model_dump_json(indent=2)

    prompt = _EDIT_WORKER_PROMPT.format(packet_json=packet_json)
    result = invoke_standalone(prompt, timeout=timeout, cwd=cwd)

    # Retry once on timeout with truncated file bodies
    if result.exit_code == -1 and settings.max_worker_retries > 0:
        smaller = packet.model_copy()
        # Keep only target file bodies
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
        prompt = _EDIT_WORKER_PROMPT.format(packet_json=smaller_json)
        result = invoke_standalone(prompt, timeout=timeout, cwd=cwd)

    # Persist run trace
    _record_trace(packet.task_id, result)

    # If successful, try to extract a patch
    if result.exit_code == 0 and result.stdout.strip():
        result.patch_content = result.stdout.strip()

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


def save_proposal(task_id: int, patch_content: str, source: str = "gemini_worker") -> int:
    """Persist a patch proposal and return its ID."""
    session = get_session()
    try:
        proposal = EditProposal(
            task_id=task_id,
            patch_content=patch_content,
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
