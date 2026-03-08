"""
Gemini CLI adapter — subprocess wrapper for standalone and subagent invocations.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from app.config import settings
from app.models.schemas import WorkerResult
from app.storage.artifacts import save_artifact


def invoke_standalone(
    prompt: str,
    *,
    allowed_tools: list[str] | None = None,
    timeout: int | None = None,
    cwd: str | None = None,
) -> WorkerResult:
    """
    Launch a fresh Gemini CLI process with a one-shot prompt.

    The prompt is passed via stdin so it can be arbitrarily long.
    """
    timeout = timeout or settings.worker_timeout_secs
    cmd = [settings.gemini_cli_bin]

    if allowed_tools:
        for tool in allowed_tools:
            cmd += ["--tool", tool]

    t0 = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        elapsed = time.monotonic() - t0

        # Store transcript
        transcript_ref = save_artifact(
            "transcripts",
            "gemini_standalone",
            {
                "prompt_preview": prompt[:500],
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-500:],
                "exit_code": result.returncode,
            },
        )

        return WorkerResult(
            worker_type="gemini_standalone",
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            transcript_ref=str(transcript_ref),
            elapsed_secs=elapsed,
        )

    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        return WorkerResult(
            worker_type="gemini_standalone",
            exit_code=-1,
            stderr=f"Timed out after {timeout}s",
            elapsed_secs=elapsed,
        )


def invoke_subagent(
    agent_name: str,
    context: str,
    *,
    agents_dir: Path | None = None,
    timeout: int | None = None,
    cwd: str | None = None,
) -> WorkerResult:
    """
    Invoke a Gemini CLI subagent by name.

    The agent Markdown definition is expected at
    ``<agents_dir>/<agent_name>.md``.

    This uses the experimental Gemini CLI subagent system.
    """
    timeout = timeout or settings.worker_timeout_secs

    # Build the prompt that tells Gemini to use the subagent
    prompt = f"@{agent_name} {context}"

    return invoke_standalone(
        prompt,
        timeout=timeout,
        cwd=cwd,
    )
