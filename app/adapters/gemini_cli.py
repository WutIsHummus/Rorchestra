"""
Gemini CLI adapter — subprocess wrapper for standalone and subagent invocations.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from app.config import settings
from app.models.schemas import WorkerResult
from app.storage.artifacts import save_artifact

# On Windows, .cmd wrappers (e.g. gemini.cmd) need shell=True
_SHELL = sys.platform == "win32"

# Lines of noise that Gemini CLI appends to stdout
_NOISE_PREFIXES = [
    "MCP issues detected.",
    "Run /mcp list for status.",
    "Loaded cached credentials.",
]


import re


def _strip_cli_noise(text: str) -> str:
    """Strip Gemini CLI noise from stdout."""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if any(stripped.startswith(p) for p in _NOISE_PREFIXES):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _parse_json_output(raw: str) -> tuple[str, int, int]:
    """
    Parse Gemini CLI ``--output-format json`` output.

    Each line is a separate JSON object. We extract:
      - concatenated text parts as the model response
      - summed input/output token counts from usageMetadata

    Returns (response_text, input_tokens, output_tokens).
    """
    text_parts: list[str] = []
    input_tokens = 0
    output_tokens = 0

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Extract text from candidates/parts
        for candidate in obj.get("candidates", []):
            content = candidate.get("content", {})
            for part in content.get("parts", []):
                if "text" in part:
                    text_parts.append(part["text"])

        # Extract token counts from usageMetadata
        usage = obj.get("usageMetadata", {})
        if usage:
            input_tokens += usage.get("promptTokenCount", 0)
            output_tokens += usage.get("candidatesTokenCount", 0)

        # Also check top-level "result" or "response" key (some CLI versions)
        if "result" in obj and isinstance(obj["result"], str):
            text_parts.append(obj["result"])
        if "response" in obj and isinstance(obj["response"], str):
            text_parts.append(obj["response"])

        # Check for modelUsage key variant
        model_usage = obj.get("modelUsage", {})
        if model_usage:
            input_tokens += model_usage.get("inputTokens", 0)
            output_tokens += model_usage.get("outputTokens", 0)

    response_text = "".join(text_parts) if text_parts else raw
    return response_text, input_tokens, output_tokens


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
    Uses ``--output-format json`` to capture structured token usage.
    """
    timeout = timeout or settings.worker_timeout_secs
    cmd = [settings.gemini_cli_bin, "--output-format", "json"]

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
            shell=_SHELL,
            encoding="utf-8",
        )
        elapsed = time.monotonic() - t0

        # Parse JSON output for text + token counts
        response_text, in_tok, out_tok = _parse_json_output(result.stdout)

        # Store transcript
        transcript_ref = save_artifact(
            "transcripts",
            "gemini_standalone",
            {
                "prompt_preview": prompt[:500],
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-500:],
                "exit_code": result.returncode,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
            },
        )

        # Strip CLI noise from the parsed response
        clean_stdout = _strip_cli_noise(response_text)

        return WorkerResult(
            worker_type="gemini_standalone",
            exit_code=result.returncode,
            stdout=clean_stdout,
            stderr=result.stderr,
            transcript_ref=str(transcript_ref),
            elapsed_secs=elapsed,
            input_tokens=in_tok,
            output_tokens=out_tok,
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
