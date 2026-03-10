"""
Session token tracker — accumulates token usage across Gemini CLI invocations.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.schemas import WorkerResult


@dataclass
class _SessionStats:
    invocation_count: int = 0
    session_input_tokens: int = 0
    session_output_tokens: int = 0
    last_input_tokens: int = 0
    last_output_tokens: int = 0


_stats = _SessionStats()


def record(result: WorkerResult) -> None:
    """Record token usage from a WorkerResult."""
    _stats.invocation_count += 1
    _stats.last_input_tokens = result.input_tokens
    _stats.last_output_tokens = result.output_tokens
    _stats.session_input_tokens += result.input_tokens
    _stats.session_output_tokens += result.output_tokens


def summary() -> dict:
    """Return current session stats."""
    return {
        "invocations": _stats.invocation_count,
        "last_input": _stats.last_input_tokens,
        "last_output": _stats.last_output_tokens,
        "session_input": _stats.session_input_tokens,
        "session_output": _stats.session_output_tokens,
        "session_total": _stats.session_input_tokens + _stats.session_output_tokens,
    }


def reset() -> None:
    """Reset all session counters."""
    global _stats
    _stats = _SessionStats()


def _fmt(n: int) -> str:
    """Format token count as human-readable (e.g. 1.2k, 15.4k)."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def last_line() -> str | None:
    """Return a compact one-line summary of the last invocation, or None if no data."""
    if _stats.invocation_count == 0:
        return None
    last_total = _stats.last_input_tokens + _stats.last_output_tokens
    sess_total = _stats.session_input_tokens + _stats.session_output_tokens
    if last_total == 0 and sess_total == 0:
        return None
    return (
        f"{_fmt(_stats.last_input_tokens)} in · "
        f"{_fmt(_stats.last_output_tokens)} out · "
        f"session: {_fmt(sess_total)} total"
    )
