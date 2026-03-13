"""
Gemini CLI adapter — subprocess wrapper for standalone and subagent invocations.
"""

from __future__ import annotations

import json
import os
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
    "ClearcutLogger:",
    "Error flushing log events:",
    "[MESSAGE_BUS]",
    "Flushing log events to Clearcut.",
]

# Patterns that indicate Node.js stack trace lines (from node-pty, etc.)
_NOISE_PATTERNS = [
    "at Module._compile",
    "at Object..js",
    "at Module.load",
    "at Function._load",
    "at TracingChannel.traceSync",
    "at wrapModuleLoad",
    "at Function.executeUserEntryPoint",
    "at node:internal/",
    "conpty_console_list_agent.js",
    "Node.js v",
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
        if any(p in stripped for p in _NOISE_PATTERNS):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _parse_json_output(raw: str) -> tuple[str, int, int]:
    """
    Parse Gemini CLI ``--output-format json`` output.

    Handles multiple output formats:
      1. Single JSON object: {"session_id": ..., "response": "...", "stats": {...}}
      2. NDJSON (one JSON object per line) with candidates/parts
      3. Mixed output with JSON embedded in noise lines

    Returns (response_text, input_tokens, output_tokens).
    """
    text_parts: list[str] = []
    input_tokens = 0
    output_tokens = 0

    # --- Strategy 1: Try parsing the entire output as a single JSON object ---
    stripped = raw.strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            # New format: {"session_id": ..., "response": "text", "stats": {...}}
            if "response" in obj and isinstance(obj["response"], str):
                text_parts.append(obj["response"])

                # Extract tokens from stats.models
                stats = obj.get("stats", {})
                for model_info in stats.get("models", {}).values():
                    tokens = model_info.get("tokens", {})
                    input_tokens += tokens.get("input", 0) or tokens.get("prompt", 0)
                    output_tokens += tokens.get("candidates", 0)

                return "".join(text_parts), input_tokens, output_tokens

            # Older format: top-level "result" key
            if "result" in obj and isinstance(obj["result"], str):
                text_parts.append(obj["result"])
                return "".join(text_parts), input_tokens, output_tokens
    except (json.JSONDecodeError, ValueError):
        pass

    # --- Strategy 2: NDJSON (one JSON object per line) ---
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(obj, dict):
            continue

        # Extract text from candidates/parts (Gemini API format)
        for candidate in obj.get("candidates", []):
            content = candidate.get("content", {})
            for part in content.get("parts", []):
                if "text" in part:
                    text_parts.append(part["text"])

        # Check top-level text keys
        if "response" in obj and isinstance(obj["response"], str):
            text_parts.append(obj["response"])
        elif "result" in obj and isinstance(obj["result"], str):
            text_parts.append(obj["result"])

        # Extract token counts
        usage = obj.get("usageMetadata", {})
        if usage:
            input_tokens += usage.get("promptTokenCount", 0)
            output_tokens += usage.get("candidatesTokenCount", 0)

        model_usage = obj.get("modelUsage", {})
        if model_usage:
            input_tokens += model_usage.get("inputTokens", 0)
            output_tokens += model_usage.get("outputTokens", 0)

        # Stats block
        stats = obj.get("stats", {})
        for model_info in stats.get("models", {}).values():
            tokens = model_info.get("tokens", {})
            input_tokens += tokens.get("input", 0) or tokens.get("prompt", 0)
            output_tokens += tokens.get("candidates", 0)

    response_text = "".join(text_parts) if text_parts else raw
    return response_text, input_tokens, output_tokens


def invoke_standalone(
    prompt: str,
    *,
    allowed_tools: list[str] | None = None,
    timeout: int | None = None,
    cwd: str | None = None,
    debug: bool = False,
    no_mcp: bool = False,
) -> WorkerResult:
    """
    Launch a fresh Gemini CLI process with a one-shot prompt.

    The prompt is passed via stdin so it can be arbitrarily long.
    Uses ``-p`` for proper headless mode and ``--output-format json``
    for structured token usage.

    no_mcp: If True, blocks MCP server initialization via
            --allowed-mcp-server-names (faster startup).
    """
    timeout = timeout or settings.worker_timeout_secs
    cmd = [
        settings.gemini_cli_bin,
        "--output-format", "json",
    ]

    if allowed_tools:
        # Auto-approve all tool usage in headless worker mode
        cmd += ["--yolo"]

    # Block MCP if requested or if using allowed_tools (workers don't need MCP)
    if no_mcp or allowed_tools:
        cmd += ["--allowed-mcp-server-names", "_no_mcp_"]

    if debug:
        cmd += ["--debug"]

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
            env={**os.environ, "GEMINI_CLI_HEADLESS": "1"},
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
    no_mcp: bool = True,
) -> WorkerResult:
    """
    Invoke a Gemini CLI subagent by name.

    The agent Markdown definition is expected at
    ``<agents_dir>/<agent_name>.md``.

    no_mcp: Blocks MCP server init by default for faster startup.
    """
    timeout = timeout or settings.worker_timeout_secs

    # Build the prompt that tells Gemini to use the subagent
    prompt = f"@{agent_name} {context}"

    return invoke_standalone(
        prompt,
        timeout=timeout,
        cwd=cwd,
        no_mcp=no_mcp,
    )
